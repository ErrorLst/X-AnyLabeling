"""Unit tests of the pure file layer of crop_core.

This module imports no GUI class on purpose: the whole scan, naming,
padding and write path must stay usable without a widget.
"""

import os

import PIL.Image
import pytest

from anylabeling.custom.crop_tool import crop_core as core

from conftest import crop_names, make_image, snapshot, write_pil


class TestNaturalKey:
    """The sort key keeps numeric runs in numeric order."""

    def test_numeric_order(self):
        names = ["10.jpg", "2.jpg", "1.jpg", "20.jpg"]
        assert sorted(names, key=core.natural_key) == [
            "1.jpg",
            "2.jpg",
            "10.jpg",
            "20.jpg",
        ]

    def test_case_folds(self):
        assert core.natural_key("ABC") == core.natural_key("abc")

    def test_non_decimal_digit_is_a_character(self):
        assert core.natural_key("a\u00b2.jpg") == [
            "a",
            "\u00b2",
            ".",
            "j",
            "p",
            "g",
        ]


class TestScanDirectory:
    """Only top level images are listed, in natural order."""

    def test_extension_case_and_order(self, ct_dir):
        write_pil(ct_dir, "b.JPG", (4, 4))
        write_pil(ct_dir, "a10.png", (4, 4))
        write_pil(ct_dir, "a2.png", (4, 4))
        with open(os.path.join(ct_dir, "notes.txt"), "wb") as handle:
            handle.write(b"x")
        names = [entry.name for entry in core.scan_directory(ct_dir)]
        assert names == ["a2.png", "a10.png", "b.JPG"]

    def test_top_level_only(self, ct_dir):
        sub = os.path.join(ct_dir, "sub")
        os.makedirs(sub)
        write_pil(sub, "inside.png", (4, 4))
        write_pil(ct_dir, "top.png", (4, 4))
        assert [entry.name for entry in core.scan_directory(ct_dir)] == [
            "top.png"
        ]

    def test_entries_carry_path_and_name(self, ct_dir):
        write_pil(ct_dir, "one.png", (4, 4))
        entry = core.scan_directory(ct_dir)[0]
        assert entry.path == os.path.join(ct_dir, "one.png")
        assert entry.name == "one.png"

    def test_missing_directory(self, ct_dir):
        assert core.scan_directory(os.path.join(ct_dir, "nope")) == []

    def test_path_is_not_a_directory(self, ct_dir):
        path = write_pil(ct_dir, "one.png", (4, 4))
        assert core.scan_directory(path) == []

    def test_only_own_crops_of_the_output_dir_are_skipped(
        self, ct_dir, ct_out
    ):
        write_pil(ct_dir, "img.png", (8, 8))
        write_pil(ct_dir, "img__x0_y0_w2_h2__px0_py0.png", (2, 2))
        plain = core.scan_directory(ct_dir)
        assert [entry.name for entry in plain] == [
            "img.png",
            "img__x0_y0_w2_h2__px0_py0.png",
        ]
        own = core.scan_directory(ct_dir, ct_dir)
        assert [entry.name for entry in own] == ["img.png"]
        other = core.scan_directory(ct_dir, ct_out)
        assert len(other) == 2


class TestSaveFormat:
    """The output format follows the extension of the source."""

    def test_known_extensions(self):
        assert core.save_format("a.JPG") == (".jpg", "JPEG", ("L", "RGB"))
        assert core.save_format("a.PnG") == (".png", "PNG", None)
        assert core.output_extension("a.tiff") == ".tiff"

    def test_unknown_extension_falls_back(self):
        assert core.save_format("a.gif") == core.DEFAULT_SAVE
        assert core.output_extension("a.gif") == ".png"
        assert core.output_extension("a") == ".png"

    def test_every_source_extension_is_an_image_extension(self):
        assert set(core.SAVE_FORMATS) <= set(core.IMAGE_EXTS)


class TestCropImage:
    """Composition, padding, format convergence and atomic write."""

    def test_creates_the_output_directory(self, ct_dir, ct_make):
        source = write_pil(ct_dir, "img.png", (8, 8))
        target = os.path.join(ct_make(), "deep", "deeper")
        result = core.crop_image(source, 1, 2, 3, 4, output_dir=target)
        assert os.path.isdir(target)
        assert os.path.isfile(result.path)

    def test_result_matches_its_name(self, ct_dir, ct_out):
        source = write_pil(ct_dir, "img.png", (8, 8))
        result = core.crop_image(source, 1, 2, 3, 4, output_dir=ct_out)
        name = os.path.basename(result.path)
        assert core.parse_crop_name(name, ct_out) == result.record
        assert result.record.path == os.path.join(ct_out, name)
        assert result.size == (3, 4)
        assert result.record.source_stem == "img"
        assert (result.record.x, result.record.y) == (1, 2)
        assert (result.record.w, result.record.h) == (3, 4)
        assert result.record.seq == 1
        assert result.record.ext == "png"
        assert name == "img__x1_y2_w3_h4__px0_py0.png"

    @pytest.mark.parametrize(
        "mode,saved",
        [("P", "RGB"), ("1", "L"), ("LA", "RGBA"), ("RGB", "RGB")],
    )
    def test_mode_normalisation(self, ct_dir, ct_out, mode, saved):
        source = write_pil(ct_dir, "img.png", (8, 8), mode)
        result = core.crop_image(source, 0, 0, 4, 4, output_dir=ct_out)
        assert result.mode == saved
        with PIL.Image.open(result.path) as opened:
            assert opened.mode == saved

    def test_rgba_source_is_converted_for_jpeg(self, ct_dir, ct_out):
        # The bytes are a png, only the extension asks for a jpeg.
        source = write_pil(ct_dir, "img.jpg", (8, 8), "RGBA", format="PNG")
        result = core.crop_image(source, 0, 0, 4, 4, output_dir=ct_out)
        assert result.mode == "RGB"
        assert result.record.ext == "jpg"
        with PIL.Image.open(result.path) as opened:
            assert opened.mode == "RGB"

    def test_inner_pixels_equal_a_direct_crop(self, ct_dir, ct_out):
        source = write_pil(ct_dir, "img.png", (8, 8), "RGB")
        result = core.crop_image(source, 2, 3, 4, 4, output_dir=ct_out)
        expected = make_image((8, 8), "RGB").crop((2, 3, 6, 7))
        with PIL.Image.open(result.path) as opened:
            assert opened.tobytes() == expected.tobytes()

    def test_padding_paints_all_four_bands(self, ct_dir, ct_out):
        source = write_pil(ct_dir, "img.png", (8, 8), "RGB")
        result = core.crop_image(
            source, 0, 0, 4, 4, pad_x=1, pad_y=1, output_dir=ct_out
        )
        assert (result.record.pad_x, result.record.pad_y) == (1, 1)
        with PIL.Image.open(result.path) as opened:
            for x in range(4):
                assert opened.getpixel((x, 0)) == (0, 0, 0)
                assert opened.getpixel((x, 3)) == (0, 0, 0)
            for y in range(4):
                assert opened.getpixel((0, y)) == (0, 0, 0)
                assert opened.getpixel((3, y)) == (0, 0, 0)

    def test_padding_is_clamped_to_half(self, ct_dir, ct_out):
        source = write_pil(ct_dir, "img.png", (8, 8))
        result = core.crop_image(
            source, 0, 0, 4, 6, pad_x=99, pad_y=99, output_dir=ct_out
        )
        assert (result.record.pad_x, result.record.pad_y) == (2, 3)

    def test_out_of_bounds_area_is_filled(self, ct_dir, ct_out):
        source = write_pil(ct_dir, "img.png", (4, 4), "RGBA")
        result = core.crop_image(source, 2, 2, 4, 4, output_dir=ct_out)
        expected = PIL.Image.new("RGBA", (4, 4), (0, 0, 0, 255))
        expected.paste(make_image((4, 4), "RGBA").crop((2, 2, 4, 4)), (0, 0))
        with PIL.Image.open(result.path) as opened:
            assert opened.mode == "RGBA"
            assert opened.tobytes() == expected.tobytes()

    def test_no_part_file_is_left_behind(self, ct_dir, ct_out):
        source = write_pil(ct_dir, "img.png", (8, 8))
        core.crop_image(source, 0, 0, 2, 2, output_dir=ct_out)
        leftover = [
            name
            for name in os.listdir(ct_out)
            if name.endswith(core.PART_SUFFIX)
        ]
        assert leftover == []
        assert len(crop_names(ct_out)) == 1

    def test_source_directory_is_untouched(self, ct_dir, ct_out):
        source = write_pil(ct_dir, "img.png", (8, 8), "RGB")
        before = snapshot(ct_dir)
        core.crop_image(source, 1, 1, 4, 4, output_dir=ct_out)
        assert snapshot(ct_dir) == before

    def test_crop_pixel_guard(self, ct_dir, ct_out, monkeypatch):
        source = write_pil(ct_dir, "img.png", (8, 8))
        monkeypatch.setattr(core, "MAX_CROP_PIXELS", 4)
        with pytest.raises(core.CropError, match="裁切尺寸过大"):
            core.crop_image(source, 0, 0, 3, 3, output_dir=ct_out)
        assert os.listdir(ct_out) == []

    def test_empty_crop_is_rejected(self, ct_dir, ct_out):
        source = write_pil(ct_dir, "img.png", (8, 8))
        with pytest.raises(core.CropError, match="裁切尺寸必须为正"):
            core.crop_image(source, 0, 0, 0, 3, output_dir=ct_out)
        assert os.listdir(ct_out) == []

    def test_source_pixel_guard(self, ct_dir, ct_out, monkeypatch):
        source = write_pil(ct_dir, "img.png", (8, 8))
        monkeypatch.setattr(core, "MAX_IMAGE_PIXELS", 4)
        with pytest.raises(core.CropError, match="图片过大"):
            core.crop_image(source, 0, 0, 2, 2, output_dir=ct_out)
        assert os.listdir(ct_out) == []

    def test_broken_image_is_reported(self, ct_dir, ct_out):
        path = os.path.join(ct_dir, "bad.png")
        with open(path, "wb") as handle:
            handle.write(b"not an image")
        with pytest.raises(core.CropError, match="图片无法读取"):
            core.crop_image(path, 0, 0, 2, 2, output_dir=ct_out)
        assert os.listdir(ct_out) == []

    def test_read_only_output_directory(
        self, ct_readonly, ct_dir, monkeypatch
    ):
        source = write_pil(ct_dir, "img.png", (8, 8))
        monkeypatch.chdir(ct_readonly)
        with pytest.raises(core.CropError, match="输出目录不可写"):
            core.crop_image(source, 0, 0, 2, 2)

    def test_default_output_dir_is_the_cwd(self):
        assert core.default_output_dir() == os.getcwd()


class TestNoGuiDependency:
    """The core module must stay free of any GUI toolkit."""

    def test_source_text_has_no_gui_import(self):
        with open(core.__file__, encoding="utf-8") as handle:
            text = handle.read()
        assert "PyQt6" not in text
        assert "QtWidgets" not in text
