"""Image encoding tests for the augmentation writer."""

import os
import os.path as osp

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from anylabeling.custom.model_validation.augment import (
    ENCODE_FALLBACK_EXT,
    IMAGE_ENCODING,
    JPEG_QUALITY,
    WEBP_QUALITY,
    decode_image,
    encode_image,
)


def sample_image(size: int = 32) -> np.ndarray:
    image = np.zeros((size, size, 3), dtype=np.uint8)
    image[:, :, 0] = 200
    image[:, :, 1] = np.arange(size, dtype=np.uint8).reshape(1, -1)
    return image


def test_encoding_table_matches_the_plan():
    assert IMAGE_ENCODING[".jpg"][0] == ".jpg"
    assert IMAGE_ENCODING[".jpeg"][0] == ".jpg"
    assert IMAGE_ENCODING[".jpeg"][2] == JPEG_QUALITY
    assert IMAGE_ENCODING[".png"][0] == ".png"
    assert IMAGE_ENCODING[".bmp"][0] == ".bmp"
    assert IMAGE_ENCODING[".webp"][0] == ".webp"
    assert IMAGE_ENCODING[".webp"][2] == WEBP_QUALITY
    assert IMAGE_ENCODING[".tif"][0] == ".tif"
    assert IMAGE_ENCODING[".tiff"][0] == ".tif"


def test_jpg_stays_jpg_with_quality_100():
    data, ext, fallback = encode_image(sample_image(), ".jpg")
    assert ext == ".jpg"
    assert fallback is False
    reference_ok, reference = _encode(".jpg", [int(_flag(".jpg")), 100])
    assert data == bytes(reference.tobytes())


def test_jpeg_maps_to_jpg():
    _data, ext, fallback = encode_image(sample_image(), ".JPEG")
    assert ext == ".jpg"
    assert fallback is False


def test_png_stays_png():
    data, ext, fallback = encode_image(sample_image(), ".png")
    assert ext == ".png"
    assert fallback is False
    assert data[:8] == bytes([137, 80, 78, 71, 13, 10, 26, 10])


def test_bmp_webp_and_tiff_are_kept():
    for source, expected in (
        (".bmp", ".bmp"),
        (".webp", ".webp"),
        (".tif", ".tif"),
    ):
        _data, ext, fallback = encode_image(sample_image(), source)
        assert ext == expected
        assert fallback is False


def test_unknown_extension_falls_back_to_png():
    data, ext, fallback = encode_image(sample_image(), ".xyz")
    assert ext == ENCODE_FALLBACK_EXT
    assert fallback is True
    assert data[:8] == bytes([137, 80, 78, 71, 13, 10, 26, 10])


def test_missing_extension_falls_back_to_png():
    _data, ext, fallback = encode_image(sample_image(), "")
    assert ext == ENCODE_FALLBACK_EXT
    assert fallback is True


def test_non_ascii_path_roundtrip(mv_scratch):
    folder = osp.join(mv_scratch, "数据")
    os.makedirs(folder, exist_ok=True)
    target = osp.join(folder, "增强_一.png")
    data, _ext, _fallback = encode_image(sample_image(), ".png")
    with open(target, "wb") as handle:
        handle.write(data)
    decoded = decode_image(target)
    assert decoded is not None
    assert decoded.shape == (32, 32, 3)


def test_decode_image_handles_missing_files(mv_scratch):
    assert decode_image(osp.join(mv_scratch, "nope.png")) is None
    broken = osp.join(mv_scratch, "broken.png")
    with open(broken, "wb") as handle:
        handle.write(b"not an image")
    assert decode_image(broken) is None


def _flag(ext: str):
    import cv2

    return cv2.IMWRITE_JPEG_QUALITY


def _encode(ext: str, params):
    import cv2

    return cv2.imencode(ext, sample_image(), params)
