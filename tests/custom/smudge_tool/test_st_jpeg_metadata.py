"""Tests of the encoder parameters kept across a write back.

The tool writes the whole array back after a repair, so a file saved
without the encoder parameters of the original is quantised again from
scratch: a quality 95 JPEG then loses tens of dB everywhere, not only
inside the region the user painted. These tests pin the parameters of
the original file to the write, and pin the plain Pillow behaviour when
no parameter is handed over.
"""

import os

import numpy as np
import PIL.Image
import pytest
from PIL import JpegImagePlugin
from PyQt6 import QtCore

from anylabeling.custom.smudge_tool import operations
from anylabeling.custom.smudge_tool import smudge_filter

from conftest import drag, press

#: JPEG fixtures: file name, Pillow save options, subsampling code.
JPEG_CASES = (
    ("q95-444.jpg", {"quality": 95, "subsampling": 0}, 0),
    ("q90-422.jpg", {"quality": 90, "subsampling": 1}, 1),
    ("q75-420.jpg", {"quality": 75, "subsampling": 2}, 2),
)


def _texture(height=120, width=160, seed=7):
    "Return a deterministic, detailed RGB image."

    grid = np.indices((height, width))
    data = np.zeros((height, width, 3), np.int16)
    data[:, :, 0] = (grid[1] * 3 + grid[0]) % 256
    data[:, :, 1] = (grid[0] * 5) % 256
    data[:, :, 2] = ((grid[0] + grid[1]) * 2) % 256
    rng = np.random.default_rng(seed)
    data += rng.integers(0, 24, data.shape)
    return np.clip(data, 0, 255).astype(np.uint8)


def _psnr(first, second):
    "Return the peak signal to noise ratio of two arrays, in dB."

    left = first.astype(np.float64)
    right = second.astype(np.float64)
    error = ((left - right) ** 2).mean()
    if error == 0:
        return float("inf")
    return 10.0 * np.log10(255.0 ** 2 / error)


def _write_jpeg(path, options=None, array=None):
    "Write an RGB JPEG fixture with the given encoder options."

    data = _texture() if array is None else array
    PIL.Image.fromarray(data, "RGB").save(
        path, format="JPEG", **(options or {})
    )
    return data


def _open_array(path):
    "Return the decoded pixels of a file."

    with PIL.Image.open(path) as image:
        return np.array(image)


@pytest.mark.parametrize("name,options,code", JPEG_CASES)
def test_read_image_collects_the_jpeg_parameters(
    st_scratch, name, options, code
):
    path = os.path.join(st_scratch, name)
    _write_jpeg(path, options)
    _array, image_format, mode, info = operations.read_image(path)
    assert image_format == "JPEG"
    assert mode == "RGB"
    assert info["subsampling"] == code
    with PIL.Image.open(path) as original:
        assert info["qtables"] == dict(original.quantization)
    assert set(info) == set(operations.INFO_KEYS)


@pytest.mark.parametrize("name,options,code", JPEG_CASES)
def test_write_image_keeps_the_parameters_of_the_file(
    st_scratch, name, options, code
):
    path = os.path.join(st_scratch, name)
    _write_jpeg(path, options)
    array, image_format, _mode, info = operations.read_image(path)
    with PIL.Image.open(path) as original:
        tables = dict(original.quantization)
    operations.write_image(array, path, image_format, info)
    with PIL.Image.open(path) as written:
        assert dict(written.quantization) == tables
        assert JpegImagePlugin.get_sampling(written) == code


def test_a_jpeg_write_back_stays_above_forty_db(st_scratch):
    """A plain save used to drop a q95/4:4:4 file to about 25 dB.

    Only a re-encode with the quantization tables and the sampling of
    the original keeps the difference to one generation of loss.
    """

    path = os.path.join(st_scratch, "sharp.jpg")
    _write_jpeg(path, {"quality": 95, "subsampling": 0})
    array, image_format, _mode, info = operations.read_image(path)
    operations.write_image(array, path, image_format, info)
    assert _psnr(_open_array(path), array) > 40.0


def test_a_write_without_info_keeps_the_pillow_defaults(st_scratch):
    "The default of the new argument is the old plain save."

    source = os.path.join(st_scratch, "source.jpg")
    _write_jpeg(source, {"quality": 95, "subsampling": 0})
    array, image_format, _mode, _info = operations.read_image(source)
    reference = os.path.join(st_scratch, "reference.jpg")
    PIL.Image.fromarray(array, "RGB").save(reference, format="JPEG")
    written = os.path.join(st_scratch, "written.jpg")
    operations.write_image(array, written, image_format)
    with open(reference, "rb") as handle:
        expected = handle.read()
    with open(written, "rb") as handle:
        assert handle.read() == expected


def test_a_grayscale_jpeg_is_written_back_without_a_factor(st_scratch):
    "Pillow reports -1 for a file without a chroma channel."

    path = os.path.join(st_scratch, "gray.jpg")
    gray = np.array(PIL.Image.fromarray(_texture(), "RGB").convert("L"))
    PIL.Image.fromarray(gray, "L").save(path, format="JPEG", quality=95)
    array, image_format, mode, info = operations.read_image(path)
    assert mode == "L"
    # Written back as 4:4:4: the factor means nothing without chroma,
    # and every code produces the very same bytes here.
    assert info["subsampling"] == 0
    operations.write_image(array, path, image_format, info)
    assert _psnr(_open_array(path), array) > 40.0


def test_a_png_write_with_info_stays_byte_identical(st_scratch):
    "Nothing is invented for a format without encoder parameters."

    path = os.path.join(st_scratch, "plain.png")
    array = _texture()
    PIL.Image.fromarray(array, "RGB").save(path)
    back, image_format, _mode, info = operations.read_image(path)
    assert info["qtables"] is None
    assert info["subsampling"] is None
    assert info["exif"] is None
    reference = os.path.join(st_scratch, "reference.png")
    PIL.Image.fromarray(array, "RGB").save(reference)
    operations.write_image(back, path, image_format, info)
    with open(reference, "rb") as handle:
        expected = handle.read()
    with open(path, "rb") as handle:
        assert handle.read() == expected


def test_a_sixteen_bit_png_write_with_info_stays_lossless(st_scratch):
    path = os.path.join(st_scratch, "gray16.png")
    data = (np.indices((24, 32)).sum(axis=0) * 900).astype(np.uint16)
    PIL.Image.fromarray(data, "I;16").save(path)
    array, image_format, _mode, info = operations.read_image(path)
    operations.write_image(array, path, image_format, info)
    back, _format, read_mode, _info = operations.read_image(path)
    assert read_mode == operations.WRITE_16BIT_MODE
    assert np.array_equal(back, array)
    assert np.array_equal(back, data)


def test_a_png_icc_profile_survives_the_write_back(st_scratch):
    path = os.path.join(st_scratch, "profile.png")
    profile = bytes(range(128))
    PIL.Image.fromarray(_texture(), "RGB").save(path, icc_profile=profile)
    array, image_format, _mode, info = operations.read_image(path)
    assert info["icc_profile"] == profile
    operations.write_image(array, path, image_format, info)
    with PIL.Image.open(path) as written:
        assert written.info.get("icc_profile") == profile


def test_a_jpeg_exif_block_survives_the_write_back(st_scratch):
    path = os.path.join(st_scratch, "exif.jpg")
    exif = PIL.Image.Exif()
    exif[271] = "X-AnyLabeling"
    PIL.Image.fromarray(_texture(), "RGB").save(
        path, format="JPEG", quality=95, exif=exif.tobytes()
    )
    array, image_format, _mode, info = operations.read_image(path)
    assert info["exif"]
    operations.write_image(array, path, image_format, info)
    with PIL.Image.open(path) as written:
        assert written.getexif().get(271) == "X-AnyLabeling"


def test_a_png_exif_block_survives_the_write_back(st_scratch):
    "The EXIF of a PNG is a plain payload, like the one of a JPEG."

    path = os.path.join(st_scratch, "tagged.png")
    exif = PIL.Image.Exif()
    exif[271] = "X-AnyLabeling"
    PIL.Image.fromarray(_texture(), "RGB").save(path, exif=exif.tobytes())
    array, image_format, _mode, info = operations.read_image(path)
    assert info["exif"]
    operations.write_image(array, path, image_format, info)
    with PIL.Image.open(path) as written:
        assert written.getexif().get(271) == "X-AnyLabeling"


def test_a_jpeg_repair_only_costs_the_touched_region(
    st_scratch, make_widget
):
    "The frame around the painted box must not be re-encoded afresh."

    path = os.path.join(st_scratch, "repair.jpg")
    _write_jpeg(path, {"quality": 95, "subsampling": 0}, _texture(80, 100))
    before = _open_array(path)
    widget = make_widget(image_path=path)
    controller = smudge_filter.install_smudge_tool(widget)
    canvas = widget.canvas
    controller._action.trigger()
    press(canvas, (60.0, 60.0), QtCore.Qt.MouseButton.RightButton)
    drag(canvas, (10.0, 10.0), (40.0, 30.0))
    assert widget.errors == []
    after = _open_array(path)
    assert not np.array_equal(after[10:30, 10:40], before[10:30, 10:40])
    outside = np.ones(after.shape[:2], bool)
    outside[10:30, 10:40] = False
    assert _psnr(after[outside], before[outside]) > 40.0


def test_a_tiff_write_with_info_stays_byte_identical(st_scratch):
    """A TIFF keeps the plain save: no encoder argument is handed over.

    The source carries both kinds of metadata a write could leak, so a
    parameter that leaked into the write changes the file. A TIFF
    hands the whole image file directory over as EXIF, and merging it
    back puts the tags of the old file into the new one; its profile
    is an IFD tag of its own, which the writer adds from the argument
    alone, because the image written back is a fresh one without an
    ``info``. Neither parameter belongs to the format, so the read
    collects none of them and the write stays the plain save: the file
    on disk is the one Pillow writes for the pixels alone, without the
    profile tag the source carried, and it decodes unchanged.
    """

    path = os.path.join(st_scratch, "packed.tif")
    array = _texture()
    profile = bytes(range(128))
    PIL.Image.fromarray(array, "RGB").save(
        path, compression="tiff_deflate", icc_profile=profile
    )
    back, image_format, _mode, info = operations.read_image(path)
    assert image_format == "TIFF"
    assert info["qtables"] is None
    assert info["subsampling"] is None
    # A bare ``is None`` would hold for a fixture without a profile as
    # well, so the file is saved with one and the collected value is
    # pinned to ``None`` by the format gate.
    assert info["icc_profile"] is None
    assert info["exif"] is None
    reference = os.path.join(st_scratch, "reference.tif")
    PIL.Image.fromarray(array, "RGB").save(reference)
    operations.write_image(back, path, image_format, info)
    with PIL.Image.open(path) as written:
        # Nothing of the profile of the source is left: the plain save
        # writes no such tag, and the file no longer decodes as one.
        assert "icc_profile" not in written.info
        assert np.array_equal(np.array(written), array)
    with open(reference, "rb") as handle:
        expected = handle.read()
    with open(path, "rb") as handle:
        assert handle.read() == expected


@pytest.mark.parametrize("orientation", [5, 6, 7, 8])
def test_a_tiff_orientation_survives_the_write_back(st_scratch, orientation):
    """A TIFF stores its dimensions in the tags read back as EXIF.

    Pillow rotates the pixels of an Orientation 5 to 8 file in place and
    hands the transposed size over, while the stored directory still
    holds the dimensions before the rotation. Handing that directory to
    the writer put the old dimensions back after the writer had set the
    new ones, and the file then decoded to something else entirely.
    """

    path = os.path.join(st_scratch, "orientation-%d.tif" % orientation)
    array = _texture(60, 80)
    PIL.Image.fromarray(array, "RGB").save(path, tiffinfo={274: orientation})
    back, image_format, _mode, info = operations.read_image(path)
    assert image_format == "TIFF"
    assert back.shape == (80, 60, 3)
    assert info["exif"] is None
    operations.write_image(back, path, image_format, info)
    with PIL.Image.open(path) as written:
        assert written.size == (60, 80)
        assert np.array_equal(np.array(written), back)


def test_a_mpo_write_keeps_the_jpeg_parameters(st_scratch):
    """Pillow names a multi picture file MPO, not JPEG.

    Its frames are plain JPEGs, so the quantization tables, the chroma
    factor and the EXIF block all belong to it. The factor and the block
    used to be dropped for that name alone: a 4:4:4 MPO written back
    came out at the Pillow default of 4:2:0.
    """

    path = os.path.join(st_scratch, "pair.mpo")
    exif = PIL.Image.Exif()
    exif[271] = "X-AnyLabeling"
    first = _texture(60, 80)
    second = _texture(60, 80, seed=11)
    PIL.Image.fromarray(first, "RGB").save(
        path,
        format="MPO",
        save_all=True,
        append_images=[PIL.Image.fromarray(second, "RGB")],
        quality=95,
        subsampling=0,
        exif=exif.tobytes(),
    )
    with PIL.Image.open(path) as original:
        tables = dict(original.quantization)
    array, image_format, mode, info = operations.read_image(path)
    assert image_format == "MPO"
    assert mode == "RGB"
    assert info["subsampling"] == 0
    assert info["exif"]
    operations.write_image(array, path, image_format, info)
    with PIL.Image.open(path) as written:
        assert dict(written.quantization) == tables
        assert JpegImagePlugin.get_sampling(written) == 0
        assert written.getexif().get(271) == "X-AnyLabeling"


def test_a_refused_parameter_falls_back_to_the_plain_save(
    st_scratch, monkeypatch
):
    """A writer that refuses an argument still writes every pixel.

    The fall back is invisible to the caller: the file on disk carries
    the Pillow defaults while the info mapping still describes the
    parameters the file was read with.
    """

    path = os.path.join(st_scratch, "refused.jpg")
    _write_jpeg(path, {"quality": 95, "subsampling": 0})
    array, image_format, _mode, info = operations.read_image(path)
    reference = os.path.join(st_scratch, "reference.jpg")
    PIL.Image.fromarray(array, "RGB").save(reference, format="JPEG")
    calls = []
    original = PIL.Image.Image.save

    def save(self, fp, format=None, **params):
        calls.append((format, sorted(params)))
        if params:
            raise ValueError("this writer refuses the parameters")
        return original(self, fp, format=format, **params)

    monkeypatch.setattr(PIL.Image.Image, "save", save)
    operations.write_image(array, path, image_format, info)
    assert calls == [("JPEG", ["qtables", "subsampling"]), ("JPEG", [])]
    # The mapping of the caller is not rewritten by the fall back.
    assert info["subsampling"] == 0 and info["qtables"]
    with open(reference, "rb") as handle:
        expected = handle.read()
    with open(path, "rb") as handle:
        assert handle.read() == expected
