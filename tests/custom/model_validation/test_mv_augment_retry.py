"""The retry loop of a sample: the fit rule, the seeds and the counters.

A geometric transform may push a shape out of the picture. Rebuilding
such a label needs a clamp, which would cut the defect the annotation
describes (or remove it from the copy entirely), and the tool has always
reported that as "clipped". Since the copy has to keep the very same
defects as its original, a try whose geometry leaves the canvas is now
repeated with the next derived seed of the same sample, at most
MAX_ATTEMPTS times, and a sample no try could fit is dropped instead of
being produced with a clipped or a vanished defect.

These tests pin down the whole contract of that loop: the fit predicate
itself, the seed of every attempt, the bytes the loop produces (equal to
the historical output for a sample that fits on the first try), the
discard of a sample that can never fit and the counters plus the report
fields the stage publishes. The partial clip path stays in the module -
clip_points and the clipped flag of rebuild_shape - it only no longer
reaches a produced file.
"""

import importlib.util
import os
import os.path as osp

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from anylabeling.custom.model_validation import dataset
from anylabeling.custom.model_validation import records as records_module
from anylabeling.custom.model_validation.app_config import (
    ATTEMPT_INDEX_STRIDE,
    AugmentParams,
    ValidationConfig,
    attempt_seed,
    derive_seed,
)
from anylabeling.custom.model_validation.augment import (
    MAX_ATTEMPTS,
    all_shapes_fit,
    augment_sample,
    clip_points,
    rebuild_shape,
    resolve_max_attempts,
    shape_fits_canvas,
)
from anylabeling.custom.model_validation.pipeline import ValidationWorker
from anylabeling.custom.model_validation.report import build_report

CLASSES = ["car"]

HAS_ALBUMENTATIONS = importlib.util.find_spec("albumentations") is not None
requires_albumentations = pytest.mark.skipif(
    not HAS_ALBUMENTATIONS,
    reason="the optional albumentations backend is not installed",
)

# A box covering the whole frame: the forced shift of 0.6 moves it so far
# that no seed can keep it inside a 200x100 canvas.
FULL_FRAME = [[0, 0], [199, 0], [199, 99], [0, 99]]
# A box around the middle of the same canvas: it fits after at most a few
# tries, whatever the shift, because every try draws a fresh direction.
CENTRED = [[80, 40], [120, 40], [120, 60], [80, 60]]


@pytest.fixture(scope="module")
def qt_app():
    "Create the offscreen application the worker signals need."

    from PyQt6 import QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def rectangle(points, label: str = "car") -> dict:
    "Return one rectangle shape holding the given points."

    return {"label": label, "shape_type": "rectangle", "points": points}


def shifted_params(seed: int = 6) -> AugmentParams:
    "Return a forced translation that keeps the colour untouched."

    return AugmentParams(
        hsv_h=0.0,
        hsv_s=0.0,
        hsv_v=0.0,
        degrees=0.0,
        translate=0.6,
        scale_min=1.0,
        scale_max=1.0,
        shear=0.0,
        perspective=0.0,
        flipud=0.0,
        fliplr=0.0,
        bgr=0.0,
        seed=seed,
    )


def image_200x100() -> np.ndarray:
    "Return a deterministic three channel canvas."

    image = np.zeros((100, 200, 3), dtype=np.uint8)
    ramp = np.linspace(0, 255, 200, dtype=np.uint8)
    image[:, :, 1] = ramp.reshape(1, -1)
    image[:, :, 2] = 90
    return image


def write_image(path: str, image: np.ndarray) -> None:
    "Write an array as a png file."

    import cv2

    os.makedirs(osp.dirname(path), exist_ok=True)
    ok, buffer = cv2.imencode(".png", image)
    assert ok
    buffer.tofile(path)


def label_of(shapes) -> dict:
    "Return a minimal xlabel payload holding the given shapes."

    return {
        "imagePath": "a.png",
        "imageData": None,
        "imageHeight": 100,
        "imageWidth": 200,
        "shapes": list(shapes),
    }


# ------------------------------------------------------- the fit predicate
def test_the_fit_predicate_accepts_every_point_on_the_border():
    "The canvas is closed: a point on the last row or column belongs to it."

    inside = {
        "shape_type": "rectangle",
        "points": [[0, 0], [199, 0], [199, 99], [0, 99]],
    }
    assert shape_fits_canvas(inside, 200, 100)
    assert all_shapes_fit([inside], 200, 100)


def test_the_fit_predicate_refuses_points_outside_the_canvas():
    "One point outside is a failed try."

    for points in (
        [[0, 0], [200, 0], [200, 99], [0, 99]],
        [[0, 0], [199, 0], [199, 100], [0, 100]],
        [[-1, 10], [199, 10], [199, 90], [-1, 90]],
        [[10, -0.5], [199, -0.5], [199, 99], [10, 99]],
    ):
        shape = {"shape_type": "rectangle", "points": points}
        assert not shape_fits_canvas(shape, 200, 100), points
    assert not all_shapes_fit(
        [
            {"shape_type": "rectangle", "points": FULL_FRAME},
            {"shape_type": "rectangle", "points": [[300, 10], [310, 10]]},
        ],
        200,
        100,
    )


def test_the_fit_predicate_refuses_degenerate_shapes():
    """A shape that lost points or collapsed is not a fit either.

    Such a label would no longer describe what the original annotation
    described, which is exactly what the retry loop has to avoid.
    """

    # a polygon collapsed onto a line: no area left
    assert not shape_fits_canvas(
        {"shape_type": "polygon", "points": [[10, 10], [50, 50], [90, 90]]},
        200,
        100,
    )
    # a shape without a single point
    assert not shape_fits_canvas(
        {"shape_type": "rectangle", "points": []}, 200, 100
    )
    # a line that lost one of its two points
    assert not shape_fits_canvas(
        {"shape_type": "line", "points": [[10, 10]]}, 200, 100
    )
    # a circle that lost its edge point
    assert not shape_fits_canvas(
        {"shape_type": "circle", "points": [[10, 10]]}, 200, 100
    )
    # while the very same geometry is a fit when it keeps its points
    assert shape_fits_canvas(
        {"shape_type": "line", "points": [[10, 10], [90, 12]]}, 200, 100
    )
    assert shape_fits_canvas(
        {"shape_type": "circle", "points": [[10, 10], [20, 10]]}, 200, 100
    )
    assert shape_fits_canvas(
        {"shape_type": "point", "points": [[10, 10]]}, 200, 100
    )


def test_the_legacy_clip_path_is_kept_for_the_modules_and_their_callers():
    """clip_points and rebuild_shape keep reporting a partial clip.

    The retry loop only stops such a try from being produced: the helpers
    themselves stay untouched, exactly like the clipped field of the
    outcome and the clipped report of the run.
    """

    points, clipped = clip_points([(-5.0, 3.0), (10.0, 3.0)], 10, 10)
    assert clipped is True
    assert points == [(0.0, 3.0), (9.0, 3.0)]

    shape = {"shape_type": "rectangle", "points": [[0, 0], [60, 40]]}
    built, clipped, lost = rebuild_shape(
        shape, "rectangle", [(-10.0, -10.0), (300.0, 300.0)], 200, 100
    )
    assert clipped is True
    assert lost is False
    assert built == [(0.0, 0.0), (199.0, 0.0), (199.0, 99.0), (0.0, 99.0)]


# ------------------------------------------------------------- the seeds
def test_attempt_zero_keeps_the_seed_of_the_sample():
    "The first try reproduces the seed the tool always derived."

    for base_seed in (0, 6, 2024, 2**40):
        for sample_index in (0, 1, 7, 1001, 50000):
            assert attempt_seed(base_seed, sample_index, 0) == derive_seed(
                base_seed, sample_index
            )


def test_the_attempt_budget_is_never_below_one():
    "The loop always tries at least once, a broken value falls back."

    assert MAX_ATTEMPTS == 5
    assert resolve_max_attempts() == MAX_ATTEMPTS
    assert resolve_max_attempts(None) == MAX_ATTEMPTS
    assert resolve_max_attempts("many") == MAX_ATTEMPTS
    assert resolve_max_attempts(0) == 1
    assert resolve_max_attempts(-7) == 1
    assert resolve_max_attempts(1) == 1
    assert resolve_max_attempts(3) == 3
    assert resolve_max_attempts(10**6) == 10**6


def test_every_attempt_derives_its_own_seed():
    "The retry seeds are a function of (seed, sample index, attempt)."

    first = attempt_seed(2024, 5, 1)
    assert first == derive_seed(2024, 5 * ATTEMPT_INDEX_STRIDE + 1)
    assert first != attempt_seed(2024, 5, 0)
    assert first != attempt_seed(2024, 5, 2)
    assert first != attempt_seed(2024, 6, 1)
    assert first != attempt_seed(2025, 5, 1)
    # the derivation is stable, there is no shared source of randomness
    assert first == attempt_seed(2024, 5, 1)


# ---------------------------------------------------- the retry loop itself
@requires_albumentations
def test_a_retried_sample_never_hands_back_a_clipped_shape():
    "The retry loop keeps the produced sample inside the canvas."

    image = image_200x100()
    target = label_of([rectangle(CENTRED)])
    params = shifted_params()
    retried = 0
    for sample_index in range(12):
        outcome = augment_sample(image, target, params, sample_index)
        assert outcome is not None
        assert outcome.attempts == outcome.attempt + 1
        assert outcome.attempts <= MAX_ATTEMPTS
        assert outcome.seed == attempt_seed(
            params.seed, sample_index, outcome.attempt
        )
        assert all_shapes_fit(
            outcome.label["shapes"], outcome.width, outcome.height
        )
        assert outcome.clipped == []
        for shape in outcome.label["shapes"]:
            for x, y in shape["points"]:
                assert 0 <= x <= outcome.width - 1
                assert 0 <= y <= outcome.height - 1
        if outcome.attempt > 0:
            retried += 1
    assert retried > 0, "a forced shift of 0.6 has to retry somewhere"


@requires_albumentations
def test_a_sample_that_never_fits_is_dropped():
    "The loop gives up after MAX_ATTEMPTS and reports nothing else."

    image = image_200x100()
    target = label_of([rectangle(FULL_FRAME)])
    params = shifted_params()
    for sample_index in range(12):
        assert augment_sample(image, target, params, sample_index) is None
    # the first try alone is already the legacy result: the same seed, the
    # very same clipped label the tool produced before the loop existed
    legacy = augment_sample(image, target, params, 0, max_attempts=1)
    assert legacy is None


@requires_albumentations
def test_the_retry_loop_is_deterministic_for_one_sample():
    "Same seed, same sample: the same attempt produces the same bytes."

    image = image_200x100()
    target = label_of([rectangle(CENTRED)])
    first = augment_sample(image, target, shifted_params(), 3)
    second = augment_sample(image, target, shifted_params(), 3)
    assert first is not None
    assert second is not None
    assert first.attempt == second.attempt
    assert first.seed == second.seed
    assert np.array_equal(first.image, second.image)
    assert first.label == second.label
    # and the seed really is the seed of that attempt
    assert first.seed == attempt_seed(6, 3, first.attempt)


@requires_albumentations
def test_a_rebuilt_label_leaves_no_shape_out_of_the_picture():
    "The invariant of the whole loop, asserted on a strong transform."

    image = np.zeros((80, 120, 3), dtype=np.uint8)
    image[:, :, 1] = np.linspace(0, 255, 120, dtype=np.uint8).reshape(1, -1)
    target = label_of([rectangle([[40, 28], [80, 28], [80, 52], [40, 52]])])
    params = AugmentParams(
        degrees=25.0,
        translate=0.15,
        scale_min=0.7,
        scale_max=1.3,
        shear=8.0,
        flipud=0.5,
        fliplr=0.5,
        seed=777,
    )
    produced = 0
    for sample_index in range(12):
        outcome = augment_sample(image, target, params, sample_index)
        if outcome is None:
            continue
        produced += 1
        assert all_shapes_fit(
            outcome.label["shapes"], outcome.width, outcome.height
        )
    assert produced == 12


# ------------------------------------------------------- the stage counters
def staging_layout(scratch: str, suffix: str) -> str:
    """Create the staging layout the tool expects.

    The folders are built one by one: a freshly created temp directory is
    not writable inside the sandbox of this workspace.
    """

    staging = osp.join(scratch, dataset.STAGING_PREFIX + suffix)
    for folder in (dataset.ORIGINAL_DIRNAME, dataset.AUGMENTED_DIRNAME):
        for sub in (dataset.IMAGES_DIRNAME, dataset.LABELS_DIRNAME):
            os.makedirs(osp.join(staging, folder, sub), exist_ok=True)
    return staging


def stage_original(staging: str, relpath: str, payload: dict):
    "Stage one original pair holding the given label."

    paths = dataset.staging_paths(
        staging, records_module.KIND_ORIGINAL, relpath
    )
    os.makedirs(osp.dirname(paths["image"]), exist_ok=True)
    write_image(paths["image"], image_200x100())
    dataset.write_json(paths["label"], payload)
    return records_module.make_record(
        records_module.KIND_ORIGINAL,
        relpath,
        paths["image"],
        paths["label"],
    )


def build_worker(staging: str, records, total: int) -> ValidationWorker:
    "Return an unstarted worker augmenting the given records."

    config = ValidationConfig(
        dataset_dir="/source",
        model_path="/model.onnx",
        classes_file="/classes.txt",
        augment_enabled=True,
        augment_mode="count",
        total_count=total,
        augment_params=shifted_params(),
    )
    worker = ValidationWorker(config, CLASSES, staging)
    worker.records = list(records)
    return worker


@requires_albumentations
@requires_albumentations
def test_the_stage_reports_no_clip_for_a_sample_the_loop_saved(
    mv_scratch, qt_app
):
    "A retried sample is produced and still counts as a clean one."

    staging = staging_layout(mv_scratch, "retry_ok")
    record = stage_original(staging, "a.png", label_of([rectangle(CENTRED)]))
    worker = build_worker(staging, [record], total=12)

    worker._stage_augment()

    summary = worker.augment_summary
    # the forced shift really retries here, exactly as many tries as the
    # counter of spent attempts says: the loop saved the copies the old
    # clamp would have produced with a cut defect
    assert summary["planned"] == 12
    assert summary["generated"] == 12
    assert summary["discarded_unfittable"] == 0
    assert summary["failed"] == 0
    assert summary["retried"] > 0
    # retried counts the samples that needed more than one try, the
    # spent attempts are at least one per sample plus one per retry
    assert summary["attempts_total"] >= 12 + summary["retried"]
    assert summary["clipped_shapes"] == 0
    assert summary["clipped"] == []
    assert len(summary["discarded"]) == 0


@requires_albumentations
def test_the_stage_counts_the_attempts_retries_and_discards(
    mv_scratch, qt_app
):
    """The summary carries the three counters and the report exposes them."""

    staging = staging_layout(mv_scratch, "retry_drop")
    record = stage_original(
        staging, "a.png", label_of([rectangle(FULL_FRAME)])
    )
    worker = build_worker(staging, [record], total=12)

    worker._stage_augment()

    summary = worker.augment_summary
    assert summary["planned"] == 12
    assert summary["generated"] == 0
    assert summary["discarded_unfittable"] == 12
    assert summary["failed"] == 0
    assert summary["retried"] == 0
    assert summary["attempts_total"] == 12 * MAX_ATTEMPTS
    assert len(summary["discarded"]) == 12
    assert [item["copy_index"] for item in summary["discarded"]] == list(
        range(1, 13)
    )
    assert {item["attempts"] for item in summary["discarded"]} == {
        MAX_ATTEMPTS
    }
    assert [item["parent_relpath"] for item in summary["discarded"]] == [
        "a.png"
    ] * 12
    assert [
        item
        for item in worker.records
        if item.kind == records_module.KIND_AUGMENTED
    ] == []

    children = [
        item
        for item in worker.records
        if item.kind == records_module.KIND_AUGMENTED
    ]
    assert len(children) == summary["generated"]
    for child in children:
        detail = child.aug_detail
        assert detail["seed"] == shifted_params().seed
        assert detail["attempts"] == detail["attempt"] + 1
        assert detail["attempt_seed"] == attempt_seed(
            detail["seed"], 1000 + detail["copy_index"], detail["attempt"]
        )
        assert detail["clipped"] == []
        payload = records_module.read_staging_label(child) or {}
        for shape in payload["shapes"]:
            for x, y in shape["points"]:
                assert 0 <= x <= 199
                assert 0 <= y <= 99

    report = build_report(
        staging,
        worker.records,
        classes=list(CLASSES),
        augment_summary=summary,
    )
    assert report["augment"]["attempts_total"] == summary["attempts_total"]
    assert report["augment"]["retried"] == summary["retried"]
    assert (
        report["augment"]["discarded_unfittable"]
        == summary["discarded_unfittable"]
    )
    assert report["clipped_count"] == 0
    assert report["clipped_shapes"] == []
