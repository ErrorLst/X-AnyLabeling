"""Tests of the disk, guard and geometry helpers."""

import os
import tempfile

import numpy as np
import PIL.Image
import pytest

from anylabeling.custom.smudge_tool import operations


def _rgb(height=40, width=60):
    "Return a deterministic RGB image."

    grid = np.indices((height, width))
    data = np.zeros((height, width, 3), np.uint8)
    data[:, :, 0] = (grid[1] * 4) % 256
    data[:, :, 1] = (grid[0] * 6) % 256
    data[:, :, 2] = ((grid[0] + grid[1]) * 3) % 256
    return data


def test_read_image_returns_an_independent_array(st_scratch):
    path = os.path.join(st_scratch, "plain.png")
    image = _rgb()
    PIL.Image.fromarray(image, "RGB").save(path)
    array, image_format, mode, _info = operations.read_image(path)
    assert image_format == "PNG"
    assert mode == "RGB"
    assert np.array_equal(array, image)
    array[0, 0] = 255
    assert not np.array_equal(array, _rgb())


def test_read_image_keeps_sixteen_bit_grayscale(st_scratch):
    path = os.path.join(st_scratch, "gray16.png")
    data = (np.indices((20, 30)).sum(axis=0) * 900).astype(np.uint16)
    PIL.Image.fromarray(data, "I;16").save(path)
    array, _format, mode, _info = operations.read_image(path)
    assert mode in operations.SUPPORTED_MODES
    assert array.dtype == np.uint16
    assert np.array_equal(array, data)


def test_read_image_rejects_an_unsupported_mode(st_scratch):
    "A palette PNG is the one unsupported mode that survives a save."

    path = os.path.join(st_scratch, "palette.png")
    PIL.Image.fromarray(_rgb(), "RGB").convert("P").save(path)
    with pytest.raises(operations.SmudgeError) as error:
        operations.read_image(path)
    assert "暂不支持" in str(error.value)
    assert "模式：P" in str(error.value)


def test_write_image_round_trips_every_supported_mode(st_scratch):
    cases = {
        "L": _rgb()[:, :, 0],
        "LA": np.concatenate(
            [_rgb()[:, :, :1], np.full((40, 60, 1), 128, np.uint8)], axis=2
        ),
        "RGB": _rgb(),
        "RGBA": np.concatenate(
            [_rgb(), np.full((40, 60, 1), 77, np.uint8)], axis=2
        ),
        "I;16": (np.indices((20, 20)).sum(axis=0) * 7).astype(np.uint16),
    }
    for mode, array in cases.items():
        path = os.path.join(st_scratch, mode.replace(";", "_") + ".png")
        operations.write_image(array, path, "PNG")
        back, _format, read_mode, _info = operations.read_image(path)
        assert np.array_equal(back, array), mode
        assert read_mode != ""


def test_write_image_writes_sixteen_bit_input_as_i16(st_scratch):
    path = os.path.join(st_scratch, "i16b.png")
    big_endian = (np.indices((16, 16)).sum(axis=0) * 5).astype(">u2")
    PIL.Image.fromarray(big_endian, "I;16B").save(path)
    array, image_format, _mode, _info = operations.read_image(path)
    assert array.dtype == np.uint16
    operations.write_image(array, path, image_format)
    with PIL.Image.open(path) as written:
        assert written.mode == operations.WRITE_16BIT_MODE
    back, _format, _mode, _info = operations.read_image(path)
    assert np.array_equal(back, array)


def test_backup_original_never_overwrites_a_copy(st_scratch):
    source = os.path.join(st_scratch, "picture.png")
    PIL.Image.fromarray(_rgb(), "RGB").save(source)
    backup_dir = os.path.join(st_scratch, "backup")
    first = operations.backup_original(source, backup_dir)
    second = operations.backup_original(source, backup_dir)
    assert first != second
    assert os.path.isfile(first) and os.path.isfile(second)
    with open(source, "rb") as handle:
        original = handle.read()
    with open(first, "rb") as handle:
        assert handle.read() == original


def test_default_backup_dir_is_inside_the_temp_folder():
    path = operations.default_backup_dir()
    assert os.path.abspath(path).startswith(
        os.path.abspath(tempfile.gettempdir())
    )
    assert operations.BACKUP_ROOT in path
    assert path.endswith(str(os.getpid()))


def test_validate_roi_accepts_a_reasonable_region():
    operations.validate_roi((10, 10, 40, 30), (100, 100))


@pytest.mark.parametrize(
    "roi",
    [
        (10, 10, 10, 30),
        (10, 10, 14, 30),
        (0, 0, 2001, 40),
        (0, 0, 40, 2001),
        (0, 0, 120, 40),
        (-5, 0, 40, 40),
    ],
)
def test_validate_roi_rejects_every_bad_region(roi):
    with pytest.raises(operations.SmudgeError):
        operations.validate_roi(roi, (100, 100))


def test_validate_roi_messages_are_chinese():
    with pytest.raises(operations.SmudgeError) as error:
        operations.validate_roi((0, 0, 4, 4), (100, 100))
    assert "6 像素" in str(error.value)
    with pytest.raises(operations.SmudgeError) as error:
        operations.validate_roi((0, 0, 2002, 10), (10, 3000))
    assert "2000 像素" in str(error.value)


def test_source_window_is_centred_when_it_fits():
    assert operations.source_window((50.0, 40.0), (20, 10), (100, 100)) == (
        40,
        35,
        60,
        45,
    )


def test_source_window_is_clamped_into_the_image():
    assert operations.source_window((2.0, 2.0), (20, 10), (100, 100)) == (
        0,
        0,
        20,
        10,
    )
    assert operations.source_window((99.0, 99.0), (20, 10), (100, 100)) == (
        80,
        90,
        100,
        100,
    )


def test_source_window_of_a_region_larger_than_the_image():
    assert operations.source_window((10.0, 10.0), (400, 300), (100, 100)) == (
        0,
        0,
        100,
        100,
    )


@pytest.mark.parametrize(
    "press,release,expected",
    [
        ((10.4, 20.3), (40.6, 60.2), (10, 20, 41, 60)),
        ((40.6, 60.2), (10.4, 20.3), (10, 20, 41, 60)),
        ((10.0, 20.0), (10.0, 20.0), (10, 20, 10, 20)),
    ],
)
def test_roi_box_normalises_the_drag(press, release, expected):
    assert operations.roi_box(press, release) == expected


@pytest.mark.parametrize(
    "box,expected",
    [
        ((0, 0, 5, 40), True),
        ((0, 0, 40, 5), True),
        ((0, 0, 6, 6), False),
        ((0, 0, 40, 6), False),
    ],
)
def test_too_small_uses_six_pixels(box, expected):
    assert operations.too_small(box) is expected


def test_write_image_reports_an_unsupported_dtype(st_scratch):
    path = os.path.join(st_scratch, "float.png")
    array = _rgb().astype(np.float32)
    with pytest.raises(operations.SmudgeError):
        operations.write_image(array, path, "PNG")
    assert not os.path.exists(path)


def test_read_image_reports_a_missing_file(st_scratch):
    path = os.path.join(st_scratch, "missing.png")
    with pytest.raises(operations.SmudgeError):
        operations.read_image(path)


def test_write_image_keeps_a_big_endian_array(st_scratch):
    "A 16 bit array of any byte order can be written back."

    path = os.path.join(st_scratch, "i16b.tif")
    values = (np.indices((16, 16)).sum(axis=0) * 5).astype(">u2")
    PIL.Image.fromarray(values, "I;16B").save(path)
    array, image_format, _mode, _info = operations.read_image(path)
    # Pillow hands a big endian TIFF over as ">u2": comparing the
    # dtype with np.uint16 used to refuse it only at write time.
    assert array.dtype == np.dtype(">u2")
    operations.write_image(array, path, image_format)
    back, _format, _mode, _info = operations.read_image(path)
    assert np.array_equal(back.astype(np.uint16), values.astype(np.uint16))
