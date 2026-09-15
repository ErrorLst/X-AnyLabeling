"""Unit tests of the crop naming, scanning and deletion of crop_core."""

import os

import pytest

from anylabeling.custom.crop_tool import crop_core as core

from conftest import crop_names, snapshot, write_pil


class TestBuildCropName:
    """The name of a crop encodes its source and its region."""

    def test_template(self):
        name = core.build_crop_name("img", 1, 2, 3, 4, 5, 6, ".png")
        assert name == "img__x1_y2_w3_h4__px5_py6.png"

    def test_sequence_suffix(self):
        first = core.build_crop_name("i", 0, 0, 1, 1, 0, 0, ".png", 1)
        assert first == "i__x0_y0_w1_h1__px0_py0.png"
        second = core.build_crop_name("i", 0, 0, 1, 1, 0, 0, ".png", 2)
        assert second == "i__x0_y0_w1_h1__px0_py0__2.png"
        zero = core.build_crop_name("i", 0, 0, 1, 1, 0, 0, ".png", 0)
        assert zero == first

    def test_round_trip(self):
        name = core.build_crop_name("img", 12, 34, 56, 78, 9, 10, ".jpg", 7)
        record = core.parse_crop_name(name, "/out")
        assert record.source_stem == "img"
        assert (record.x, record.y) == (12, 34)
        assert (record.w, record.h) == (56, 78)
        assert (record.pad_x, record.pad_y) == (9, 10)
        assert record.seq == 7
        assert record.ext == "jpg"
        assert record.path == os.path.join("/out", name)

    def test_round_trip_without_directory(self):
        name = core.build_crop_name("img", 0, 0, 1, 1, 0, 0, ".png")
        assert core.parse_crop_name(name).path == name


class TestParseCropName:
    """Only the exact template is read, and the stem is greedy."""

    def test_legal_sample(self):
        record = core.parse_crop_name("a__x1_y2_w3_h4__px0_py0.png")
        assert record is not None
        assert record.source_stem == "a"
        assert record.seq == 1

    def test_extension_case_is_kept(self):
        record = core.parse_crop_name("a__x1_y2_w3_h4__px0_py0.PNG")
        assert record.ext == "PNG"

    def test_missing_padding_block(self):
        assert core.parse_crop_name("a__x1_y2_w3_h4.jpg") is None

    def test_sequence_is_optional(self):
        record = core.parse_crop_name("a__x1_y2_w3_h4__px0_py0__2.png")
        assert record.seq == 2

    def test_full_width_digits_do_not_match(self):
        assert core.parse_crop_name("a__x\uff11_y2_w3_h4__px0_py0.png") is None
        assert (
            core.parse_crop_name("a__x1_y2_w3_h4__px0_py0__\uff12.png") is None
        )

    def test_non_template_names(self):
        for name in [
            "notes.txt",
            "IMG_1.jpg",
            "a__w3_h4__px0_py0.png",
            "a__x1_y2_w3_h4__px0_py0",
            "a.png",
        ]:
            assert core.parse_crop_name(name) is None

    def test_nested_stem_keeps_the_rightmost_block(self):
        outer = core.build_crop_name("a", 1, 2, 3, 4, 0, 0, "")
        inner = core.build_crop_name(outer, 5, 6, 7, 8, 0, 0, ".png")
        record = core.parse_crop_name(inner)
        assert record.source_stem == "a__x1_y2_w3_h4__px0_py0"
        assert (record.x, record.y, record.w, record.h) == (5, 6, 7, 8)


class TestCropSequence:
    """A taken name only moves the sequence number one step further."""

    def test_repeat_of_one_region_counts_up(self, ct_dir, ct_out):
        source = write_pil(ct_dir, "img.png", (8, 8))
        for _ in range(3):
            core.crop_image(source, 0, 0, 2, 2, output_dir=ct_out)
        assert crop_names(ct_out) == [
            "img__x0_y0_w2_h2__px0_py0.png",
            "img__x0_y0_w2_h2__px0_py0__2.png",
            "img__x0_y0_w2_h2__px0_py0__3.png",
        ]

    def test_other_regions_start_without_a_suffix(self, ct_dir, ct_out):
        source = write_pil(ct_dir, "img.png", (8, 8))
        core.crop_image(source, 0, 0, 2, 2, output_dir=ct_out)
        core.crop_image(source, 1, 1, 2, 2, output_dir=ct_out)
        names = crop_names(ct_out)
        assert len(names) == 2
        assert not any(name.endswith("__2.png") for name in names)

    def test_stale_part_file_moves_the_sequence(self, ct_dir, ct_out):
        source = write_pil(ct_dir, "img.png", (8, 8))
        taken = core.build_crop_name("img", 0, 0, 2, 2, 0, 0, ".png", 1)
        stale = os.path.join(ct_out, taken + core.PART_SUFFIX)
        with open(stale, "xb"):
            pass
        result = core.crop_image(source, 0, 0, 2, 2, output_dir=ct_out)
        assert os.path.basename(result.path) == (
            "img__x0_y0_w2_h2__px0_py0__2.png"
        )
        assert result.record.seq == 2

    def test_existing_crop_is_never_overwritten(self, ct_dir, ct_out):
        source = write_pil(ct_dir, "img.png", (8, 8))
        first = core.crop_image(source, 0, 0, 2, 2, output_dir=ct_out)
        before = snapshot(ct_out)
        second = core.crop_image(source, 0, 0, 2, 2, output_dir=ct_out)
        assert second.path != first.path
        after = snapshot(ct_out)
        for name, value in before.items():
            assert after[name] == value

    def test_name_too_long(self, ct_dir, ct_out):
        source = write_pil(ct_dir, "s" * 220 + ".png", (4, 4))
        with pytest.raises(core.CropError, match="文件名过长"):
            core.crop_image(source, 0, 0, 2, 2, output_dir=ct_out)
        assert os.listdir(ct_out) == []


class TestScanCrops:
    """The badge of a file is the number of its parseable crops."""

    def test_groups_by_source_stem(self, ct_dir, ct_out):
        first = write_pil(ct_dir, "a.png", (8, 8))
        second = write_pil(ct_dir, "b.png", (8, 8))
        core.crop_image(first, 0, 0, 2, 2, output_dir=ct_out)
        core.crop_image(first, 0, 0, 2, 2, output_dir=ct_out)
        core.crop_image(second, 1, 1, 2, 2, output_dir=ct_out)
        groups = core.scan_crops(ct_out)
        assert sorted(groups) == ["a", "b"]
        assert [record.seq for record in groups["a"]] == [1, 2]
        assert len(groups["b"]) == 1
        assert core.crop_count(ct_out, "a") == 2
        assert core.crop_count(ct_out, "zzz") == 0

    def test_group_order_is_natural(self, ct_dir, ct_out):
        source = write_pil(ct_dir, "img.png", (8, 8))
        for _ in range(10):
            core.crop_image(source, 0, 0, 2, 2, output_dir=ct_out)
        records = core.scan_crops(ct_out)["img"]
        assert [record.seq for record in records] == list(range(1, 11))

    def test_ignores_subdirectories_and_foreign_files(self, ct_out):
        os.makedirs(os.path.join(ct_out, "a__x0_y0_w1_h1__px0_py0.png"))
        with open(os.path.join(ct_out, "notes.txt"), "wb") as handle:
            handle.write(b"x")
        keep = os.path.join(ct_out, "b__x0_y0_w1_h1__px0_py0.png")
        with open(keep, "wb") as handle:
            handle.write(b"x")
        groups = core.scan_crops(ct_out)
        assert sorted(groups) == ["b"]
        assert groups["b"][0].path == keep

    def test_missing_directory(self, ct_dir):
        gone = os.path.join(ct_dir, "nope")
        assert core.scan_crops(gone) == {}
        assert core.crop_count(gone, "a") == 0


class TestDeleteCrops:
    """Nothing but an own crop inside the output directory is removed."""

    @staticmethod
    def _crops(ct_dir, ct_out, count=1, stem="img"):
        source = write_pil(ct_dir, stem + ".png", (8, 8))
        paths = []
        for _ in range(count):
            result = core.crop_image(source, 0, 0, 2, 2, output_dir=ct_out)
            paths.append(result.path)
        return paths

    def test_deletes_own_crops(self, ct_dir, ct_out):
        paths = self._crops(ct_dir, ct_out, 2)
        deleted, skipped = core.delete_crops(paths, ct_out)
        assert deleted == paths
        assert skipped == []
        assert core.scan_crops(ct_out) == {}

    def test_directory_is_skipped(self, ct_out):
        path = os.path.join(ct_out, "a__x0_y0_w1_h1__px0_py0.png")
        os.makedirs(path)
        deleted, skipped = core.delete_crops([path], ct_out)
        assert deleted == []
        assert skipped == [(path, "不是普通文件")]
        assert os.path.isdir(path)

    def test_symlink_is_skipped(self, ct_dir, ct_out):
        target = write_pil(ct_dir, "img.png", (4, 4))
        link = os.path.join(ct_out, "img__x0_y0_w1_h1__px0_py0.png")
        os.symlink(target, link)
        deleted, skipped = core.delete_crops([link], ct_out)
        assert deleted == []
        assert skipped == [(link, "符号链接")]
        assert os.path.exists(target)

    def test_foreign_names_are_skipped(self, ct_out):
        notes = os.path.join(ct_out, "notes.txt")
        plain = os.path.join(ct_out, "IMG_1.jpg")
        for path in (notes, plain):
            with open(path, "wb") as handle:
                handle.write(b"x")
        deleted, skipped = core.delete_crops([notes, plain], ct_out)
        assert deleted == []
        assert len(skipped) == 2
        assert os.path.exists(notes)
        assert os.path.exists(plain)

    def test_file_outside_the_output_dir_is_skipped(self, ct_out, ct_make):
        other = ct_make()
        path = os.path.join(other, "img__x0_y0_w1_h1__px0_py0.png")
        with open(path, "wb") as handle:
            handle.write(b"x")
        deleted, skipped = core.delete_crops([path], ct_out)
        assert deleted == []
        assert skipped == [(path, "不在输出目录内")]
        assert os.path.exists(path)

    def test_unusable_output_dir_skips_everything(self, ct_dir, ct_out):
        path = write_pil(ct_out, "a__x0_y0_w1_h1__px0_py0.png", (2, 2))
        gone = os.path.join(ct_dir, "missing")
        for output_dir in ("", gone):
            deleted, skipped = core.delete_crops([path], output_dir)
            assert deleted == []
            assert skipped == [(path, "输出目录不可用")]
        assert os.path.exists(path)

    def test_mixed_result_counts(self, ct_dir, ct_out):
        good = self._crops(ct_dir, ct_out, 1)[0]
        bad = os.path.join(ct_out, "notes.txt")
        with open(bad, "wb") as handle:
            handle.write(b"x")
        deleted, skipped = core.delete_crops([bad, good], ct_out)
        assert deleted == [good]
        assert [item[0] for item in skipped] == [bad]
        assert not os.path.exists(good)
        assert os.path.exists(bad)
