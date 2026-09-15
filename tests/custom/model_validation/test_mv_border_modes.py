"""How the canvas uncovered by a geometric transform is filled.

This module used to hold the tests of a three way option - reflect,
constant, replicate - offered by a combo box of the configuration page.
The user cancelled that option and asked for pure black, unconditionally:
the module now pins the two promises that remain.

1. The pixels of every uncovered area are (0, 0, 0). The fill is read on
   the picture the tool really produces: a strong geometric transform
   uncovers a known part of the canvas whatever direction it samples, and
   every pixel of that part is sampled. A bright picture of 144 - the
   median the tool used to paint into the band - makes the assertion
   unambiguous: the band is black and the content of the picture is still
   bright.
2. There is no fill option anywhere. The configuration page owns no
   control for it, no visible text of the page mentions reflect or
   replicate, and the parameter dataclass carries no border mode field:
   the fill cannot be selected, reordered or re-enabled by accident.

The layout of the page is measured with the very same dialog the user
sees, because removing the combo from the amount row is what the
question was about: the page grew no row and lost one control.
"""

import importlib.util
import json
import os
import os.path as osp

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import cv2
import numpy as np
import pytest
from PyQt6 import QtWidgets

from anylabeling.custom.model_validation import dataset
from anylabeling.custom.model_validation import records as records_module
from anylabeling.custom.model_validation.augment import (
    BLACK_FILL,
    MAX_ATTEMPTS,
    augment_sample,
    black_fill_for,
    black_fill_tuple,
    build_replay_compose,
    build_transforms,
    image_channels,
)
from anylabeling.custom.model_validation.app_config import (
    BORDER_FILL,
    AugmentParams,
    ValidationConfig,
    border_fill_value,
    validate_augment_params,
)
from anylabeling.custom.model_validation.pipeline import ValidationWorker
from anylabeling.custom.model_validation.report import build_report
from anylabeling.custom.model_validation.ui.dialog import ModelValidationDialog

HAS_ALBUMENTATIONS = importlib.util.find_spec("albumentations") is not None
requires_albumentations = pytest.mark.skipif(
    not HAS_ALBUMENTATIONS,
    reason="the optional albumentations backend is not installed",
)

# A synthetic gradient: the horizontal ramp makes every column unique, so
# a filled pixel can be traced back to its source column.
WIDTH = 200
HEIGHT = 100
MEDIAN = 127
EDGE = 255
# The forced translation: half of the width and half of the height, which
# uncovers exactly the right and the bottom band of the canvas.
TRANSLATE = 0.5

# A bright flat picture: the user met the fill on exactly such a dataset
# and read the median of it (144) as a grey band where a rotation
# uncovered the canvas. Its one bright pixel proves that the black of the
# band never comes from the content.
CORNER_WIDTH = 80
CORNER_HEIGHT = 60
BRIGHT = 144
CORNER_PIXEL = (20, 40, 60)
CORNER_BAND = 3
# The geometry of the report: a strong rotation, a strong translation and
# a zoom out uncover canvas whatever the sampled direction is.
STRONG_DEGREES = 25.0
STRONG_TRANSLATE = 0.45
STRONG_SCALE_MIN = 0.6
STRONG_SCALE_MAX = 0.9
# The seed the pixel evidence is measured with: the uncovered canvas of
# that seed reaches the corners of the frame, so the black is read far
# away from the drawn content.
CORNER_SEED = 1

# The four corner bands of a sample, in a fixed order.
CORNER_SLICES = (
    (slice(0, CORNER_BAND), slice(0, CORNER_BAND)),
    (slice(0, CORNER_BAND), slice(-CORNER_BAND, None)),
    (slice(-CORNER_BAND, None), slice(0, CORNER_BAND)),
    (slice(-CORNER_BAND, None), slice(-CORNER_BAND, None)),
)
# The geometry of the band evidence: the picture is moved to one side
# without a rotation or a zoom, so the band the source lost is one whole
# side of the canvas.
BAND_TRANSLATE = 0.4
# The geometry that keeps the content in the middle of the frame while
# the corners are uncovered: between the mild shipped default and the
# strong geometry of the report.
EVIDENCE_DEGREES = 12.0
EVIDENCE_TRANSLATE = 0.25
EVIDENCE_SCALE_MIN = 0.75
EVIDENCE_SCALE_MAX = 0.9
EVIDENCE_GEOMETRY = {
    "degrees": EVIDENCE_DEGREES,
    "translate": EVIDENCE_TRANSLATE,
    "scale_min": EVIDENCE_SCALE_MIN,
    "scale_max": EVIDENCE_SCALE_MAX,
}


def gradient_image() -> np.ndarray:
    "Return a 200x100 gradient holding one fixed colour per channel."

    image = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
    ramp = np.linspace(0, 255, WIDTH, dtype=np.uint8)
    image[:, :, 0] = ramp.reshape(1, -1)
    image[:, :, 1] = 10
    image[:, :, 2] = 200
    return image


def bright_image() -> np.ndarray:
    "Return a flat bright picture holding one single non black pixel."

    image = np.full((CORNER_HEIGHT, CORNER_WIDTH, 3), BRIGHT, dtype=np.uint8)
    image[0, 0] = CORNER_PIXEL
    return image


def strong_params(seed: int = CORNER_SEED, **overrides) -> AugmentParams:
    "Return the strong geometry of the report, colour untouched."

    values = {
        "contrast": 0.0,
        "hsv_v": 0.0,
        "degrees": STRONG_DEGREES,
        "translate": STRONG_TRANSLATE,
        "scale_min": STRONG_SCALE_MIN,
        "scale_max": STRONG_SCALE_MAX,
        "flipud": False,
        "fliplr": False,
        "select_prob": 1.0,
        "seed": seed,
    }
    values.update(overrides)
    return AugmentParams(**values)


def corner_point() -> dict:
    "Return a label whose shape stays inside the canvas whatever is drawn."

    return {
        "shapes": [
            {
                "label": "a0_dian",
                "shape_type": "rectangle",
                "points": [[36, 26], [44, 26], [44, 34], [36, 34]],
            }
        ]
    }


def strong_shifted(
    image=None, seed: int = CORNER_SEED, attempts: int = MAX_ATTEMPTS, **over
) -> np.ndarray:
    "Augment a picture with the strong geometry of the report."

    outcome = augment_sample(
        bright_image() if image is None else image,
        corner_point(),
        strong_params(seed=seed, **over),
        0,
        max_attempts=attempts,
    )
    assert outcome is not None
    return outcome.image


def corner_band(image: np.ndarray, index: int) -> np.ndarray:
    "Return one of the four corner bands of a sample."

    rows, columns = CORNER_SLICES[index]
    return image[rows, columns]


def shifted(seed: int = 7) -> np.ndarray:
    "Augment the gradient with a translation and no other geometry."

    outcome = augment_sample(
        gradient_image(),
        {"shapes": []},
        AugmentParams(
            contrast=0.0,
            hsv_v=0.0,
            degrees=0.0,
            translate=TRANSLATE,
            scale_min=1.0,
            scale_max=1.0,
            flipud=False,
            fliplr=False,
            select_prob=1.0,
            seed=seed,
        ),
        0,
    )
    assert outcome is not None
    return outcome.image


def black_mask(image: np.ndarray) -> np.ndarray:
    "Return the mask of the pixels that are black on every channel."

    return (image == BLACK_FILL).all(axis=2)


# ------------------------------------------------------------ the option
def test_the_parameter_set_carries_no_border_mode():
    "The fill is not a parameter any more: nothing can select it."

    params = AugmentParams()
    assert not hasattr(params, "border_mode")
    assert AugmentParams.from_dict({"border_mode": "reflect"}) == params
    # a stored configuration that still carries the old key is accepted
    # and ignored: the validator has no mode range to check any more
    validate_augment_params(AugmentParams.from_dict({"border_mode": "x"}))
    # the module no longer offers the names of the removed option
    from anylabeling.custom.model_validation import app_config

    for name in (
        "BORDER_MODES",
        "BORDER_MODE_REFLECT",
        "BORDER_MODE_CONSTANT",
        "BORDER_MODE_REPLICATE",
        "BORDER_MODE_CV2",
        "DEFAULT_BORDER_MODE",
        "resolve_border_mode",
    ):
        assert not hasattr(app_config, name), name


def test_the_snapshot_records_the_one_fill_of_the_run():
    "The report states what every copy was built with: black."

    snapshot = AugmentParams().snapshot()
    assert snapshot["border_fill"] == BORDER_FILL == "black"
    # the fill is no official argument, therefore it stays out of the
    # official name table the snapshot is compared with a training YAML by
    assert "border_fill" not in snapshot["official_names"]
    assert "border_mode" not in snapshot
    assert "border_mode" not in snapshot["official_names"]
    assert json.loads(json.dumps(snapshot))["border_fill"] == "black"


def test_the_black_fill_is_pure_black():
    "Every channel of the fill is zero, whatever the picture is."

    assert BLACK_FILL == 0
    assert black_fill_tuple(3) == (0.0, 0.0, 0.0)
    assert black_fill_tuple(4) == (0.0, 0.0, 0.0, 0.0)
    assert black_fill_tuple(1) == (0.0,)
    # a picture the caller could not describe still asks for black
    for broken in (None, "?", 0, -3):
        assert black_fill_tuple(broken) == (0.0, 0.0, 0.0), broken


def test_the_channel_count_follows_the_picture():
    "The fill of a gray copy holds one value, the colour fill three."

    assert image_channels(bright_image()) == 3
    assert image_channels(gradient_image()[:, :, 0]) == 1
    assert image_channels(gradient_image()[:, :, :1]) == 1
    assert image_channels(None) == 3
    assert image_channels(np.zeros((4, 4, 4), dtype=np.uint8)) == 4


def affine_of(transforms):
    "Return the single affine transform of the stack."

    import albumentations as albu

    found = [item for item in transforms if isinstance(item, albu.Affine)]
    assert len(found) == 1
    return found[0]


@requires_albumentations
def test_the_geometric_transform_is_pinned_to_black():
    "The affine transform carries the constant mode and the black fill."

    import albumentations as albu

    image = bright_image()
    assert black_fill_for(image) == (0.0, 0.0, 0.0)
    assert black_fill_for() == (0.0, 0.0, 0.0)
    assert black_fill_for(gradient_image()[:, :, 0]) == (0.0,)
    assert black_fill_for(bright_image()) != border_fill_value(bright_image())
    transforms = build_transforms(AugmentParams(), black_fill_for(image))
    affine = affine_of(transforms)
    assert affine.border_mode == cv2.BORDER_CONSTANT
    assert tuple(affine.fill) == (0.0, 0.0, 0.0)
    # the perspective transform was removed together with its parameter:
    # the affine one is the only transform that can uncover canvas now
    perspective = [
        item for item in transforms if isinstance(item, albu.Perspective)
    ]
    assert perspective == []
    # a caller that hands no fill over still gets the black one: the
    # constant border mode never falls back to the library default
    default = build_transforms(AugmentParams())
    assert affine_of(default).border_mode == cv2.BORDER_CONSTANT
    assert tuple(affine_of(default).fill) == (0.0, 0.0, 0.0)
    assert build_replay_compose(AugmentParams(), black_fill_for(image))


def test_the_median_helper_is_kept_and_marked_unused():
    """The median helper stays readable but no longer feeds the stack.

    The uncovered canvas is filled with black, therefore the median of a
    picture is not part of the augmentation any more. The helper is kept,
    exactly as the reviewer asked, so a reader can still see that the
    median of the bright picture (144) is not what the band carries.
    """

    image = gradient_image()
    assert border_fill_value(image) == (float(MEDIAN), 10.0, 200.0)
    assert border_fill_value(image[:, :, 0]) == (float(MEDIAN),)
    assert border_fill_value(None) is None
    assert "UNUSED" in border_fill_value.__doc__
    assert border_fill_value(bright_image()) == (
        float(BRIGHT),
        float(BRIGHT),
        float(BRIGHT),
    )


# ------------------------------------------------- the black fill, on pixels
@requires_albumentations
def test_the_uncovered_canvas_of_a_bright_picture_is_pure_black():
    """The pixel evidence: every sampled point of the new canvas is (0, 0, 0).

    A flat picture of 144 carries a median of 144, therefore the fill the
    tool used to hand to the stack painted the uncovered canvas grey
    (~144). Every corner band of the sample below is either the black of
    the fill or the bright content of the picture and nothing in between.
    """

    source = bright_image()
    assert float(np.median(source)) == float(BRIGHT)
    image = strong_shifted(source, attempts=1, **EVIDENCE_GEOMETRY)
    assert image.shape == source.shape
    bands = np.concatenate(
        [corner_band(image, index).reshape(-1, 3) for index in range(4)]
    )
    assert np.array_equal(
        np.unique(bands, axis=0),
        np.array([[BLACK_FILL] * 3, [BRIGHT] * 3], np.uint8),
    )
    black = [
        index
        for index in range(4)
        if black_mask(corner_band(image, index)).all()
    ]
    bright = [
        index
        for index in range(4)
        if (corner_band(image, index) == BRIGHT).all()
    ]
    assert black, "no corner of the canvas was uncovered"
    assert bright, "the content of the picture is gone"
    for index in black:
        for channel in range(3):
            assert int(corner_band(image, index)[:, :, channel].max()) == 0
    # the content of the picture carries no dark pixel the fill could
    # have left behind: it is the bright picture, not a darkened copy
    assert int(np.median(image)) == BRIGHT
    assert int(image.max()) == BRIGHT


@requires_albumentations
def test_the_report_geometry_unmasks_the_canvas_as_pure_black():
    """The self check of the report: 144, 25 degrees, translate 0.45.

    The geometry is the strong one of the report - a rotation of 25
    degrees, a translation of up to 0.45 and a zoom of 0.6 to 0.9 - run
    on the flat picture of 144 with one single seed. It uncovers the
    canvas whatever direction it samples: the test counts the newly
    uncovered pixels (every pixel equal to (0, 0, 0)), samples the four
    corners of the frame one by one and proves that the content of the
    picture is still bright and untouched.
    """

    source = bright_image()
    image = strong_shifted(source, attempts=1)
    assert image.shape == source.shape == (CORNER_HEIGHT, CORNER_WIDTH, 3)
    black = black_mask(image)
    count = int(black.sum())
    assert count > 0, "the geometry uncovered no canvas at all"
    # every pixel the geometry uncovered is black on all three channels:
    # the mask is the exact equality with (0, 0, 0)
    assert np.array_equal(
        np.unique(image[black], axis=0), np.zeros((1, 3), np.uint8)
    )
    assert 0.1 <= count / image.shape[0] / image.shape[1] <= 0.95
    # the three corners of the sampled seed, read one by one
    corners = [
        tuple(int(value) for value in image[0, 0]),
        tuple(int(value) for value in image[0, image.shape[1] - 1]),
        tuple(int(value) for value in image[image.shape[0] - 1, 0]),
        tuple(int(value) for value in image[image.shape[0] - 1, -1]),
    ]
    assert corners.count((BLACK_FILL, BLACK_FILL, BLACK_FILL)) == 3, corners
    # the bright content is still there: the fill painted the band that
    # fell outside the picture, it never darkened a pixel of the picture
    # (the strong geometry of that seed uncovers most of the frame, so
    # the count, and not the median, is what proves the content survived)
    bright = int((~black_mask(image)).sum())
    assert bright > 0 and bright < count
    assert int(image.max()) == BRIGHT


@requires_albumentations
def test_a_whole_uncovered_band_is_pure_black():
    """The band the translation uncovered is black, the rest is content.

    The picture is moved to one side without any rotation or zoom, so the
    band the source lost is a whole side of the canvas: the black columns
    (or rows) of that side are counted, the opposite side carries the
    content of the picture, and the band is pure black on every channel.
    """

    outcome = augment_sample(
        bright_image(),
        corner_point(),
        strong_params(
            degrees=0.0,
            translate=BAND_TRANSLATE,
            scale_min=1.0,
            scale_max=1.0,
        ),
        0,
        max_attempts=1,
    )
    assert outcome is not None
    image = outcome.image
    black = black_mask(image)
    assert 0.2 <= float(black.mean()) <= 0.8
    assert np.array_equal(
        np.unique(image[black], axis=0), np.zeros((1, 3), np.uint8)
    )
    corners = [
        bool(black_mask(corner_band(image, index)).all()) for index in range(4)
    ]
    assert any(corners), corners
    assert not all(corners), corners
    assert any(
        (corner_band(image, index) == BRIGHT).all() for index in range(4)
    ), corners


@requires_albumentations
def test_a_gray_picture_is_filled_with_black_too():
    "The gray route keeps a single channel, filled with the same black."

    gray = cv2.cvtColor(bright_image(), cv2.COLOR_BGR2GRAY)
    outcome = augment_sample(
        gray,
        corner_point(),
        strong_params(**EVIDENCE_GEOMETRY),
        0,
        max_attempts=1,
    )
    assert outcome is not None
    image = outcome.image
    assert outcome.grayscale is True
    assert image.ndim == 2
    assert image.shape == gray.shape
    assert int(image.min()) == BLACK_FILL
    black = int((image == BLACK_FILL).sum())
    assert 20 <= black < image.size
    bands = np.concatenate(
        [corner_band(image, index).reshape(-1) for index in range(4)]
    )
    assert set(np.unique(bands).tolist()) <= {BLACK_FILL, BRIGHT}
    assert (bands == BLACK_FILL).any()
    assert (bands == BRIGHT).any()


@requires_albumentations
def test_the_same_seed_still_reproduces_the_very_same_bytes():
    "Pinning the fill never changed the determinism of a sample."

    params = AugmentParams(select_prob=1.0, seed=11)
    first = augment_sample(gradient_image(), {"shapes": []}, params, 5)
    second = augment_sample(gradient_image(), {"shapes": []}, params, 5)
    assert np.array_equal(first.image, second.image)
    assert first.label == second.label


# --------------------------------------------------------- the page itself
@pytest.fixture(scope="module")
def qt_app():
    "Create the offscreen application the page and the signals need."

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def page_labels(page) -> list:
    "Return the visible label texts of the configuration page."

    return [
        label.text()
        for label in page.findChildren(QtWidgets.QLabel)
        if label.isVisibleTo(page)
    ]


def test_the_configuration_page_offers_no_fill_option(qt_app):
    """No control, no label and no text can select the fill any more.

    The check is the one the user asked for: the attribute of the removed
    combo is gone, the visible labels of the page carry no 边缘填充 and no
    text anywhere on the page names reflect or replicate.
    """

    dialog = ModelValidationDialog()
    try:
        page = dialog.config_page
        assert not hasattr(page, "border_mode_combo")
        labels = page_labels(page)
        assert labels
        assert not [text for text in labels if "边缘填充" in text]
        texts = [label.text() for label in page.findChildren(QtWidgets.QLabel)]
        assert not [text for text in texts if "填充" in text]
        widgets = [page] + page.findChildren(QtWidgets.QWidget)
        for widget in widgets:
            for text in (
                widget.toolTip(),
                getattr(widget, "text", lambda: "")(),
                getattr(widget, "placeholderText", lambda: "")(),
            ):
                lowered = str(text).lower()
                assert "reflect" not in lowered, text
                assert "replicate" not in lowered, text
        # no combo box of the page carries a fill entry either
        for combo in page.findChildren(QtWidgets.QComboBox):
            items = [combo.itemText(index) for index in range(combo.count())]
            for item in items:
                assert "填充" not in item, item
                assert "reflect" not in item.lower(), item
                assert "replicate" not in item.lower(), item
        # the collected configuration carries the fixed fill alone: the
        # parameter set has no field to read a selection from
        collected = page.collect_config().augment_params
        assert not hasattr(collected, "border_mode")
        snapshot = page.collect_config().to_dict()["augment_params"]
        assert snapshot["border_fill"] == "black"
    finally:
        dialog.close()


def test_the_compact_page_keeps_its_measured_size(qt_app):
    """The compact page keeps the budget of its layout.

    The height is a function of the font metrics of the environment (563
    on the author machine, 571 with HOME and 532 without it in this
    container), therefore the test pins the budget of the compact form
    instead of one absolute number. The floor of that budget is the 600px
    minimum height of the window: the gauge of
    test_mv_ui_dialog.py:test_the_page_height_stays_inside_the_screen_budget
    is that same 600px floor plus a range around the page hint, it never
    compares the window height to hint + 56.
    """

    dialog = ModelValidationDialog()
    try:
        dialog.show()
        QtWidgets.QApplication.processEvents()
        hint = dialog.config_page.sizeHint()
        assert 520 <= hint.height() <= 580
        assert dialog.minimumWidth() == 1024
        assert dialog.minimumHeight() == 600
        # the window hugs the page, but its own 600px floor wins when the
        # platform font asks for less (532px with DejaVu Sans)
        budget = max(hint.height() + 56.0, float(dialog.minimumHeight()))
        assert dialog.height() <= budget
        assert dialog.height() < 680
    finally:
        dialog.close()


# ------------------------------------------------------------- the report
def make_staging_layout(scratch: str, suffix: str) -> str:
    "Create the staging folder layout the tool expects."

    staging = osp.join(scratch, dataset.STAGING_PREFIX + suffix)
    for folder in (dataset.ORIGINAL_DIRNAME, dataset.AUGMENTED_DIRNAME):
        for sub in (dataset.IMAGES_DIRNAME, dataset.LABELS_DIRNAME):
            os.makedirs(osp.join(staging, folder, sub), exist_ok=True)
    return staging


def stage_original(staging: str, relpath: str):
    "Stage one original pair holding the gradient of the tests."

    paths = dataset.staging_paths(
        staging, records_module.KIND_ORIGINAL, relpath
    )
    os.makedirs(osp.dirname(paths["image"]), exist_ok=True)
    ok, buffer = cv2.imencode(".png", gradient_image())
    assert ok
    buffer.tofile(paths["image"])
    dataset.write_json(
        paths["label"],
        {
            "version": "3.0.0",
            "flags": {},
            "checked": False,
            "shapes": [
                {
                    "label": "a0_dian",
                    "shape_type": "rectangle",
                    "points": [[10, 10], [80, 10], [80, 60], [10, 60]],
                    "group_id": None,
                    "description": "",
                    "flags": {},
                }
            ],
            "imagePath": osp.basename(relpath),
            "imageData": None,
            "imageHeight": HEIGHT,
            "imageWidth": WIDTH,
        },
    )
    return records_module.make_record(
        records_module.KIND_ORIGINAL,
        relpath,
        paths["image"],
        paths["label"],
    )


def run_augment(staging: str) -> ValidationWorker:
    "Stage one original and run the augmentation stage on it."

    worker = ValidationWorker(
        ValidationConfig(
            dataset_dir="/source",
            model_path="/model.onnx",
            augment_enabled=True,
            augment_mode="count",
            total_count=1,
            augment_workers=1,
            augment_params=AugmentParams(
                degrees=10.0,
                translate=0.1,
                scale_min=0.9,
                scale_max=1.1,
                flipud=False,
                fliplr=False,
                select_prob=1.0,
                seed=4321,
            ),
        ),
        ["a0_dian"],
        staging,
    )
    worker.records = [stage_original(staging, "a.png")]
    worker._stage_augment()
    return worker


@requires_albumentations
def test_the_run_records_the_fill_it_really_used(qt_app, tmp_path):
    "aug_detail, augment_summary and the report all carry border_fill."

    staging = make_staging_layout(str(tmp_path), "border_fill")
    worker = run_augment(staging)
    summary = worker.augment_summary
    assert summary["generated"] == 1
    assert summary["planned"] == 1
    assert summary["border_fill"] == "black"
    # the report reads the very same snapshot
    assert summary["params"]["border_fill"] == "black"
    assert "border_mode" not in summary
    assert "border_mode" not in summary["params"]
    children = [
        record
        for record in worker.records
        if record.kind == records_module.KIND_AUGMENTED
    ]
    assert len(children) == 1
    assert children[0].aug_detail["border_fill"] == "black"
    assert children[0].aug_detail["seed"] == 4321

    # the whole document is a JSON document, and the fixed fill travels
    # through it as a plain string
    report = build_report(
        staging,
        worker.records,
        model_info=worker.model_info,
        classes=worker.classes,
        config=worker.config.to_dict(),
        augment_summary=worker.augment_summary,
    )
    restored = json.loads(json.dumps(report))
    assert restored["augment"]["border_fill"] == "black"
    assert restored["augment"]["params"]["border_fill"] == "black"
    assert restored["config"]["augment_params"]["border_fill"] == "black"
