"""Gray content: gray -> RGB -> augment -> gray, plus the report fields.

A dataset may hold gray pictures either as single channel files or as
three channel files whose planes are equal pixel by pixel. Both travel
through the very same stack as a colour image - the gray picture is
promoted to three channels, augmented with the untouched parameter set
and collapsed back to one channel before it is encoded - so the
albumentations "not applicable to grayscale image" warning can never
fire and the copy keeps the channel count of its source.
"""

import hashlib
import importlib.util
import os
import os.path as osp
import warnings

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from PyQt6 import QtWidgets

from anylabeling.custom.model_validation import dataset
from anylabeling.custom.model_validation import pipeline as pipeline_module
from anylabeling.custom.model_validation import records as records_module
from anylabeling.custom.model_validation.app_config import (
    AugmentParams,
    ValidationConfig,
)
from anylabeling.custom.model_validation.augment import (
    augment_sample,
    black_fill_for,
    build_replay_compose,
    collapse_to_grayscale,
    decode_image,
    encode_image,
    is_grayscale_content,
    promote_to_bgr,
)
from anylabeling.custom.model_validation.pipeline import ValidationWorker
from anylabeling.custom.model_validation.report import build_report

CLASSES = ["a0_dian"]

HAS_ALBUMENTATIONS = importlib.util.find_spec("albumentations") is not None
requires_albumentations = pytest.mark.skipif(
    not HAS_ALBUMENTATIONS,
    reason="the optional albumentations backend is not installed",
)

GRAYSCALE_WARNING = "not applicable to grayscale image"


@pytest.fixture(scope="module")
def qt_app():
    "Create the offscreen application the worker signals need."

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def gray_image(width: int = 64, height: int = 48) -> np.ndarray:
    "Return a single channel image with a gradient."

    rows = np.linspace(20, 240, height, dtype=np.uint8).reshape(-1, 1)
    columns = np.linspace(0, 40, width, dtype=np.uint8).reshape(1, -1)
    return np.clip(rows + columns, 0, 255).astype(np.uint8)


def color_image(width: int = 64, height: int = 48) -> np.ndarray:
    "Return a three channel image that really carries colour."

    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:, :, 0] = np.linspace(0, 255, width, dtype=np.uint8)
    image[:, :, 1] = 90
    image[:, :, 2] = 200
    return image


def base_params(**overrides) -> AugmentParams:
    "Return parameters that change nothing unless told otherwise."

    data = {
        "contrast": 0.0,
        "hsv_v": 0.0,
        "degrees": 0.0,
        "translate": 0.0,
        "scale_min": 1.0,
        "scale_max": 1.0,
        "flipud": False,
        "fliplr": False,
        # a probability of one is the frame a fixture that asserts pixels
        # or counts needs: the draw always picks the whole candidate set
        "select_prob": 1.0,
        "seed": 4242,
    }
    data.update(overrides)
    return AugmentParams(**data)


def base_label() -> dict:
    """Return a label holding the shapes of a real annotation file.

    Both shapes sit near the middle of the frame and stay small enough
    that the shipped transform (a rotation of 15 degrees, a zoom of 0.8
    to 1.5 and a translation of 0.1) keeps every point of them inside a
    64x48 canvas: a sample whose geometry leaves the picture is retried
    with a fresh seed by augment_sample, so an off centre fixture would
    only measure the retry loop instead of the gray path.
    """

    return {
        "version": "3.0.0",
        "flags": {},
        "checked": False,
        "imagePath": "a.jpg",
        "imageData": None,
        "imageHeight": 48,
        "imageWidth": 64,
        "shapes": [
            {
                "label": "a0_dian",
                "shape_type": "rectangle",
                "points": [[22, 17], [42, 17], [42, 31], [22, 31]],
                "group_id": None,
                "description": "",
                "flags": {},
            },
            {
                "label": "a1_xian",
                "shape_type": "polygon",
                "points": [[20, 22], [36, 19], [34, 33]],
                "group_id": None,
                "description": "",
                "flags": {},
            },
        ],
    }


def run(image: np.ndarray, params: AugmentParams, index: int = 0):
    "Augment one image while capturing every warning it raises."

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        outcome = augment_sample(image, base_label(), params, index)
    return outcome, [str(item.message) for item in caught]


def write_image(path: str, image: np.ndarray) -> None:
    "Write an array as a png file."

    import cv2

    os.makedirs(osp.dirname(path), exist_ok=True)
    ok, buffer = cv2.imencode(".png", image)
    assert ok
    buffer.tofile(path)


def staging_layout(scratch: str, suffix: str) -> str:
    "Create the staging folder layout the tool expects."

    staging = osp.join(scratch, dataset.STAGING_PREFIX + suffix)
    for folder in (dataset.ORIGINAL_DIRNAME, dataset.AUGMENTED_DIRNAME):
        for sub in (dataset.IMAGES_DIRNAME, dataset.LABELS_DIRNAME):
            os.makedirs(osp.join(staging, folder, sub), exist_ok=True)
    return staging


def staged_original(staging: str, relpath: str, image: np.ndarray):
    "Create one staged original record holding the given pixels."

    paths = dataset.staging_paths(
        staging, records_module.KIND_ORIGINAL, relpath
    )
    write_image(paths["image"], image)
    payload = base_label()
    payload["imagePath"] = osp.basename(relpath)
    payload["imageHeight"] = int(image.shape[0])
    payload["imageWidth"] = int(image.shape[1])
    dataset.write_json(paths["label"], payload)
    return records_module.make_record(
        records_module.KIND_ORIGINAL, relpath, paths["image"], paths["label"]
    )


def build_worker(staging: str, records, total: int):
    "Return an unstarted worker augmenting the given records."

    config = ValidationConfig(
        dataset_dir="/source",
        model_path="/model.onnx",
        classes_file="/classes.txt",
        augment_enabled=True,
        augment_mode="count",
        total_count=total,
        augment_params=AugmentParams(select_prob=1.0, seed=2024),
    )
    worker = ValidationWorker(config, CLASSES, staging)
    worker.records = list(records)
    return worker


def augmented_digests(staging: str) -> dict:
    "Return the sha256 of every produced augmented file."

    digests = {}
    root = osp.join(staging, dataset.AUGMENTED_DIRNAME)
    for current, _dirs, names in os.walk(root):
        for name in sorted(names):
            path = osp.join(current, name)
            with open(path, "rb") as handle:
                data = handle.read()
            key = osp.relpath(path, root).replace(os.sep, "/")
            digests[key] = hashlib.sha256(data).hexdigest()
    return digests


# ------------------------------------------------------------ detection
def test_the_two_real_shapes_of_gray_content_are_detected():
    "Single channel and R == G == B are gray, real colour is not."

    gray = gray_image()
    assert is_grayscale_content(gray)
    assert is_grayscale_content(gray[:, :, None])
    rgb_gray = np.repeat(gray[:, :, None], 3, axis=2)
    assert is_grayscale_content(rgb_gray)
    assert not is_grayscale_content(color_image())
    assert not is_grayscale_content(None)


def test_decode_keeps_a_single_channel_file_single_channel(tmp_path):
    "An L mode file comes back as a 2D array, an RGB one as 3 channels."

    import cv2

    gray = gray_image()
    single = str(tmp_path / "gray.png")
    ok, buffer = cv2.imencode(".png", gray)
    assert ok
    buffer.tofile(single)
    rgb = str(tmp_path / "gray_rgb.jpg")
    ok, buffer = cv2.imencode(
        ".jpg", promote_to_bgr(gray), [cv2.IMWRITE_JPEG_QUALITY, 100]
    )
    assert ok
    buffer.tofile(rgb)

    decoded_single = decode_image(single)
    decoded_rgb = decode_image(rgb)
    assert decoded_single.ndim == 2
    assert decoded_rgb.ndim == 3
    assert is_grayscale_content(decoded_single)
    assert is_grayscale_content(decoded_rgb)
    assert decode_image(str(tmp_path / "missing.png")) is None


# ---------------------------------------------------------------- the path
@requires_albumentations
def test_the_gray_copy_is_the_promoted_run_collapsed_back():
    """The routing is asserted pixel by pixel, not only described.

    The picture augment_sample produces for a gray source has to be the
    very same stack applied to the promoted three channel copy and
    collapsed with the same helper - so the gray path cannot silently
    change a parameter, a seed, an interpolation or the fill of the canvas
    the geometry uncovers. The replay therefore derives that fill with the
    very helper the augment path uses, instead of assuming one geometry.
    """

    from anylabeling.custom.model_validation.app_config import derive_seed

    gray = gray_image()
    params = base_params(hsv_v=0.4, fliplr=True, degrees=12.0)
    outcome, raised = run(gray, params, 3)

    # the contract of the route, whatever the sampled geometry is
    assert outcome.grayscale is True
    assert outcome.image.ndim == 2
    assert outcome.image.shape == gray.shape
    label = base_label()
    assert sum(outcome.type_counts.values()) == len(label["shapes"])
    assert set(outcome.type_counts) == {
        shape["shape_type"] for shape in label["shapes"]
    }
    assert not [text for text in raised if GRAYSCALE_WARNING in text]

    promoted = promote_to_bgr(gray)
    compose = build_replay_compose(params, black_fill_for(promoted))
    compose.set_random_seed(derive_seed(params.seed, 3))
    replayed = compose(image=promoted, keypoints=[], kp_labels=[])
    expected = collapse_to_grayscale(replayed["image"])
    assert np.array_equal(outcome.image, expected)


@requires_albumentations
def test_a_colour_picture_keeps_the_colour_route():
    "A picture with real colour never enters the gray path."

    outcome, raised = run(color_image(), base_params(hsv_v=0.4), 1)
    assert outcome.grayscale is False
    assert outcome.image.ndim == 3
    assert raised == []


@requires_albumentations
def test_all_zero_parameters_stay_within_the_round_trip_error():
    "An all zero parameter set only pays the gray -> RGB -> gray rounding."

    gray = gray_image()
    outcome, _warnings = run(gray, base_params(), 0)
    difference = np.abs(outcome.image.astype(np.int16) - gray.astype(np.int16))
    # cv2 rounds the two conversions; the measurement on real data is 0
    # and the tolerance documents the worst case of that rounding
    assert int(difference.max()) <= 2


@requires_albumentations
def test_hsv_v_still_changes_a_gray_image():
    "The value channel is the one colour parameter a gray copy feels."

    gray = gray_image()
    brightened, _warnings = run(gray, base_params(hsv_v=0.8), 4)
    difference = np.abs(
        brightened.image.astype(np.int16) - gray.astype(np.int16)
    )
    assert difference.mean() > 1.0
    assert (difference > 0).mean() > 0.5


@requires_albumentations
def test_the_contrast_gain_changes_a_gray_copy():
    """Contrast is the second colour parameter a gray copy feels.

    The gain of RandomBrightnessContrast runs on the promoted RGB
    representation before the picture is collapsed back, so it changes
    the pixels of a gray source like it changes the pixels of a colour
    one, and the copy stays single channel.
    """

    gray = gray_image()
    outcome, raised = run(gray, base_params(contrast=0.5), 9)
    assert outcome.grayscale is True
    assert outcome.image.ndim == 2
    assert outcome.image.shape == gray.shape
    assert raised == []
    difference = np.abs(
        outcome.image.astype(np.int16) - gray.astype(np.int16)
    )
    assert not np.array_equal(outcome.image, gray)
    assert difference.mean() > 1.0
    assert (difference > 0).mean() > 0.5


# ----------------------------------------------------------- the encoding
@requires_albumentations
def test_the_encoded_copy_is_still_a_single_channel_image(tmp_path):
    "A gray source produces a gray file, whatever the source format."

    from PIL import Image

    gray = gray_image()
    outcome, _warnings = run(gray, base_params(hsv_v=0.5), 5)
    for source_ext, expected_mode in ((".jpg", "L"), (".png", "L")):
        data, target, fallback = encode_image(outcome.image, source_ext)
        assert target == source_ext
        assert fallback is False
        path = str(tmp_path / ("copy" + source_ext))
        with open(path, "wb") as handle:
            handle.write(data)
        with Image.open(path) as image:
            assert image.mode == expected_mode
            assert image.size == (outcome.width, outcome.height)
        assert decode_image(path).ndim == 2


@requires_albumentations
def test_the_label_geometry_survives_the_gray_path():
    "Only pixels travel through the conversion, never a coordinate."

    label = base_label()
    outcome, _warnings = run(gray_image(), base_params(degrees=10.0), 7)
    assert [shape["shape_type"] for shape in outcome.label["shapes"]] == [
        shape["shape_type"] for shape in label["shapes"]
    ]
    assert len(outcome.label["shapes"][0]["points"]) == 4
    assert len(outcome.label["shapes"][1]["points"]) == 3
    assert outcome.label["shapes"][0]["label"] == "a0_dian"


@requires_albumentations
def test_the_same_seed_reproduces_the_gray_copy():
    "Determinism is untouched: one seed, one picture."

    params = AugmentParams(degrees=15.0, hsv_v=0.4, select_prob=1.0, seed=99)
    first, _warnings = run(gray_image(), params, 11)
    second, _warnings = run(gray_image(), params, 11)
    assert np.array_equal(first.image, second.image)
    assert first.label == second.label


# ------------------------------------------------------------- the report
@requires_albumentations
def test_the_pipeline_reports_which_copies_went_through_the_gray_path(
    tmp_path, qt_app
):
    "aug_detail, augment_summary and the report carry grayscale: true."

    staging = staging_layout(str(tmp_path), "gray_stage")
    records = [
        staged_original(staging, "gray.png", gray_image()),
        staged_original(staging, "gray_rgb.png", promote_to_bgr(gray_image())),
        staged_original(staging, "colour.png", color_image()),
    ]
    worker = build_worker(staging, records, total=3)
    worker._stage_augment()

    summary = worker.augment_summary
    assert summary["generated"] == 3
    assert summary["grayscale"] is True
    assert summary["grayscale_images"] == 2
    assert "single channel" in summary["grayscale_note"]

    children = {
        record.relpath: record
        for record in worker.records
        if record.kind == records_module.KIND_AUGMENTED
    }
    assert children["gray_aug1.png"].aug_detail["grayscale"] is True
    assert children["gray_rgb_aug1.png"].aug_detail["grayscale"] is True
    assert children["colour_aug1.png"].aug_detail["grayscale"] is False
    # the produced copies keep the channel count of what produced them
    for relpath, channels in (
        ("gray_aug1.png", 2),
        ("gray_rgb_aug1.png", 2),
        ("colour_aug1.png", 3),
    ):
        decoded = decode_image(children[relpath].staging_image_path)
        assert decoded.ndim == channels, relpath

    report = build_report(
        staging,
        worker.records,
        classes=list(CLASSES),
        augment_summary=summary,
    )
    assert report["augment"]["grayscale"] is True
    assert report["augment"]["grayscale_images"] == 2


@requires_albumentations
def test_the_thread_count_never_changes_a_gray_copy(
    tmp_path, qt_app, monkeypatch
):
    """One pool and four pools write the very same gray bytes.

    The concurrency of the tool is fixed, so the two pool sizes are
    driven through the very seam the stage reads its count from: the
    produced gray copies must stay byte for byte identical.
    """

    serial_staging = staging_layout(str(tmp_path), "gray_serial")
    quad_staging = staging_layout(str(tmp_path), "gray_quad")
    images = [gray_image(), gray_image()]
    digests = {}
    for label, staging, workers in (
        ("serial", serial_staging, 1),
        ("quad", quad_staging, 4),
    ):
        monkeypatch.setattr(
            pipeline_module,
            "default_workers",
            lambda cpu_count=None, workers=workers: workers,
        )
        records = [
            staged_original(staging, f"img{index}.png", image)
            for index, image in enumerate(images)
        ]
        worker = build_worker(staging, records, total=4)
        worker._stage_augment()
        assert worker.augment_summary["workers"] == workers
        assert worker.augment_summary["grayscale_images"] == 4
        digests[label] = augmented_digests(staging)
    assert digests["serial"] == digests["quad"]
    assert len(digests["serial"]) == 8
