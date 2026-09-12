"""Threaded augmentation stage: determinism, progress, cancel, isolation.

The second stage generates one augmented copy per sample and the copies
are independent of each other, therefore they are produced by a pool of
worker threads. The pool runs the fixed concurrency of the tool - half
of the logical processors of the machine - and these tests pin down what
that concurrency may never change: the produced bytes, the order of the
records, the progress semantics, the cancellation and the isolation of a
broken sample. They also pin down the concurrency itself: it is derived
from the CPU count, it is shared with the inference stage, and no stored
configuration can change it.
"""

import hashlib
import importlib.util
import os
import os.path as osp

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PyQt6 import QtWidgets

from anylabeling.custom.model_validation import app_config
from anylabeling.custom.model_validation import augment as augment_module
from anylabeling.custom.model_validation import dataset
from anylabeling.custom.model_validation import pipeline as pipeline_module
from anylabeling.custom.model_validation import records as records_module
from anylabeling.custom.model_validation.app_config import (
    DEFAULT_INFER_WORKERS,
    DEFAULT_WORKERS,
    AugmentParams,
    ValidationConfig,
    default_workers,
    logical_cores,
    resolve_augment_workers,
)
from anylabeling.custom.model_validation.pipeline import (
    STAGE_AUGMENT,
    ValidationWorker,
)

CLASSES = ["car"]

HAS_ALBUMENTATIONS = importlib.util.find_spec("albumentations") is not None
requires_albumentations = pytest.mark.skipif(
    not HAS_ALBUMENTATIONS,
    reason="the optional albumentations backend is not installed",
)
# a machine derived for one worker runs the stage serially: the pool
# expectations below only apply from two workers on
requires_parallel_pool = pytest.mark.skipif(
    default_workers() <= 1,
    reason="this machine is derived for a single worker (the serial path)",
)


@pytest.fixture(scope="module")
def qt_app():
    "Create the offscreen application needed to receive the signals."

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


# --------------------------------------------------------------- fixtures
def make_staging_layout(scratch: str, suffix: str) -> str:
    """Create the staging folder layout the tool expects."""

    staging = osp.join(scratch, dataset.STAGING_PREFIX + suffix)
    for folder in (dataset.ORIGINAL_DIRNAME, dataset.AUGMENTED_DIRNAME):
        for sub in (dataset.IMAGES_DIRNAME, dataset.LABELS_DIRNAME):
            os.makedirs(osp.join(staging, folder, sub), exist_ok=True)
    return staging


def label_payload(image_path: str) -> dict:
    "Return the xlabel payload of one staged pair."

    return {
        "version": "3.0.0",
        "flags": {},
        "checked": False,
        "shapes": [
            {
                "label": "car",
                "shape_type": "rectangle",
                "points": [[6, 6], [40, 6], [40, 30], [6, 30]],
                "group_id": None,
                "description": "",
                "flags": {},
            }
        ],
        "imagePath": image_path,
        "imageData": None,
        "imageHeight": 48,
        "imageWidth": 64,
    }


def staged_original(staging: str, relpath: str, seed: int):
    "Create one staged original holding a deterministic image."

    import cv2

    paths = dataset.staging_paths(
        staging, records_module.KIND_ORIGINAL, relpath
    )
    os.makedirs(osp.dirname(paths["image"]), exist_ok=True)
    generator = np.random.default_rng(seed)
    image = generator.integers(0, 255, size=(48, 64, 3), dtype=np.uint8)
    ok, buffer = cv2.imencode(osp.splitext(relpath)[1], image)
    assert ok
    buffer.tofile(paths["image"])
    dataset.write_json(paths["label"], label_payload(osp.basename(relpath)))
    return records_module.make_record(
        records_module.KIND_ORIGINAL, relpath, paths["image"], paths["label"]
    )


def build_config(total: int) -> ValidationConfig:
    "Return an enabled augmentation configuration for N copies in total."

    return ValidationConfig(
        dataset_dir="/source",
        model_path="/model.onnx",
        augment_enabled=True,
        augment_mode="count",
        total_count=total,
        augment_params=AugmentParams(
            degrees=10.0,
            translate=0.1,
            scale_min=0.9,
            scale_max=1.1,
            fliplr=0.5,
            seed=1234,
        ),
    )


def build_worker(staging: str, relpaths, total: int):
    "Stage the given originals and return an unstarted worker."

    records = [
        staged_original(staging, relpath, index)
        for index, relpath in enumerate(relpaths)
    ]
    worker = ValidationWorker(build_config(total), CLASSES, staging)
    worker.records = records
    return worker


def pin_workers(monkeypatch, workers: int) -> None:
    """Run the next stage on a pool of an explicit size.

    The concurrency of the tool is fixed, so the size of the pool is no
    longer reachable through the configuration; the determinism of the
    stage is still proven by running the very same plan through pools of
    different sizes, which this seam drives.
    """

    monkeypatch.setattr(
        pipeline_module, "default_workers", lambda cpu_count=None: workers
    )


def digest_tree(root: str) -> dict:
    "Return the sha256 of every file below a staging folder."

    digests = {}
    for current, _dirs, names in os.walk(root):
        for name in sorted(names):
            path = osp.join(current, name)
            with open(path, "rb") as handle:
                data = handle.read()
            relpath = osp.relpath(path, root).replace(os.sep, "/")
            digests[relpath] = hashlib.sha256(data).hexdigest()
    return digests


def augmented_digests(staging: str) -> dict:
    "Return the digests of every produced image and label, by relpath."

    folder = osp.join(staging, dataset.AUGMENTED_DIRNAME)
    digests = {}
    for sub in (dataset.IMAGES_DIRNAME, dataset.LABELS_DIRNAME):
        for relpath, digest in digest_tree(osp.join(folder, sub)).items():
            digests[sub + "/" + relpath] = digest
    return digests


# ------------------------------------------------------------- the default
def test_the_worker_count_is_the_fixed_half_of_the_logical_cores():
    "The stage runs half of the logical processors, never below one."

    assert logical_cores() == int(os.cpu_count() or 1)
    assert DEFAULT_WORKERS == default_workers()
    assert DEFAULT_WORKERS == max(1, (os.cpu_count() or 1) // 2)
    # the inference stage shares that very same single source
    assert DEFAULT_INFER_WORKERS == DEFAULT_WORKERS
    config = ValidationConfig()
    assert config.augment_workers == DEFAULT_WORKERS
    payload = config.to_dict()
    assert payload["augment_workers"] == DEFAULT_WORKERS
    assert payload["augment_workers_default"] == DEFAULT_WORKERS


def test_a_stored_worker_count_is_clamped_into_the_fixed_range():
    "1 is serial, 0 and negative values are serial too, everything caps."

    assert resolve_augment_workers(1) == 1
    assert resolve_augment_workers(0) == 1
    assert resolve_augment_workers(-3) == 1
    assert resolve_augment_workers(DEFAULT_WORKERS) == DEFAULT_WORKERS
    assert resolve_augment_workers(DEFAULT_WORKERS + 1) == DEFAULT_WORKERS
    assert resolve_augment_workers(10**6) == DEFAULT_WORKERS
    assert resolve_augment_workers(None) == DEFAULT_WORKERS
    assert resolve_augment_workers("many") == DEFAULT_WORKERS
    for value in (-5, 0, 1, 3, DEFAULT_WORKERS, 10**6, None, "many"):
        assert 1 <= resolve_augment_workers(value) <= DEFAULT_WORKERS


@requires_albumentations
def test_the_stage_runs_the_fixed_pool_whatever_the_config_says(
    tmp_path, qt_app
):
    """The stored field can not change the concurrency of the stage.

    The configuration page owns no control for the pool any more, so a
    stored configuration that still carries a value - here the serial
    single worker - is ignored: the pool is the fixed count of the
    machine.
    """

    staging = make_staging_layout(str(tmp_path), "fixed")
    worker = build_worker(staging, REL_PATHS, 6)
    worker.config.augment_workers = 1

    worker._stage_augment()

    assert worker.augment_summary["workers"] == default_workers()
    assert worker.augment_summary["generated"] == 6
    assert len(augmented_digests(staging)) == 12


# ------------------------------------------------------------ determinism
REL_PATHS = ("a.jpg", "sub/b.jpg", "c.png", "d.png")


@requires_albumentations
def test_the_thread_count_never_changes_the_produced_files(
    tmp_path, qt_app, monkeypatch
):
    """One thread and several threads write the very same bytes.

    The plan holds four originals and six copies, so two of them carry
    two copies each: the file names, the image bytes and the label json
    of every sample must be identical whatever the pool size, and a
    second run with the same pool size must reproduce them as well. The
    pool of the tool is fixed, so the two sizes are driven through the
    very seam the stage reads its count from.
    """

    staging_one = make_staging_layout(str(tmp_path), "serial")
    staging_four = make_staging_layout(str(tmp_path), "quad")
    staging_again = make_staging_layout(str(tmp_path), "quad2")
    one = build_worker(staging_one, REL_PATHS, 6)
    four = build_worker(staging_four, REL_PATHS, 6)
    again = build_worker(staging_again, REL_PATHS, 6)

    pin_workers(monkeypatch, 1)
    one._stage_augment()
    pin_workers(monkeypatch, 4)
    for worker in (four, again):
        worker._stage_augment()

    assert one.augment_summary["planned"] == 6
    assert one.augment_summary["generated"] == 6
    assert four.augment_summary["workers"] == 4
    assert one.augment_summary["workers"] == 1

    produced = {
        "serial": augmented_digests(staging_one),
        "quad": augmented_digests(staging_four),
        "quad2": augmented_digests(staging_again),
    }
    names = sorted(produced["serial"])
    # four originals, six copies: a and sub/b hold two copies each
    assert names == [
        "images/a_aug1.jpg",
        "images/a_aug2.jpg",
        "images/c_aug1.png",
        "images/d_aug1.png",
        "images/sub/b_aug1.jpg",
        "images/sub/b_aug2.jpg",
        "labels/a_aug1.jpg",
        "labels/a_aug2.jpg",
        "labels/c_aug1.png",
        "labels/d_aug1.png",
        "labels/sub/b_aug1.jpg",
        "labels/sub/b_aug2.jpg",
    ]
    for label in ("quad", "quad2"):
        assert produced[label] == produced["serial"], label

    # the records follow the plan order instead of the completion order
    def children(worker):
        return [
            record.relpath
            for record in worker.records
            if record.kind == records_module.KIND_AUGMENTED
        ]

    assert children(one) == children(four) == children(again)
    assert children(one) == [
        "a_aug1.jpg",
        "a_aug2.jpg",
        "sub/b_aug1.jpg",
        "sub/b_aug2.jpg",
        "c_aug1.png",
        "d_aug1.png",
    ]

    serial_summary = dict(one.augment_summary)
    quad_summary = dict(four.augment_summary)
    assert serial_summary.pop("workers") == 1
    assert quad_summary.pop("workers") == 4
    assert serial_summary == quad_summary


@requires_albumentations
@requires_parallel_pool
def test_every_thread_of_a_parallel_pool_pins_the_opencv_pool(
    tmp_path, qt_app, monkeypatch
):
    """A parallel worker keeps OpenCV at one thread, not its own pool.

    The fixed concurrency already fills the machine, therefore every
    thread of the pool pins the OpenCV thread pool before its first job:
    the default pool of the runtime - one per worker, all of them on the
    same CPUs - would oversubscribe the machine.
    """

    import cv2

    calls = []
    monkeypatch.setattr(
        cv2, "setNumThreads", lambda count: calls.append(int(count))
    )
    pin_workers(monkeypatch, 4)

    staging = make_staging_layout(str(tmp_path), "opencv")
    worker = build_worker(staging, REL_PATHS, 6)
    worker._stage_augment()

    assert worker.augment_summary["workers"] == 4
    assert worker.augment_summary["generated"] == 6
    assert calls, "the initializer of the pool never ran"
    assert set(calls) == {app_config.OPENCV_THREADS} == {1}
    assert len(calls) <= 4


@requires_albumentations
def test_the_serial_path_keeps_the_opencv_default(
    tmp_path, qt_app, monkeypatch
):
    "One worker has nothing to share: the runtime default stays."

    import cv2

    calls = []
    monkeypatch.setattr(
        cv2, "setNumThreads", lambda count: calls.append(int(count))
    )
    pin_workers(monkeypatch, 1)

    staging = make_staging_layout(str(tmp_path), "opencv_serial")
    worker = build_worker(staging, REL_PATHS, 6)
    worker._stage_augment()

    assert worker.augment_summary["workers"] == 1
    assert worker.augment_summary["generated"] == 6
    assert calls == []


@requires_albumentations
def test_the_sample_seeds_follow_the_plan_not_the_threads(tmp_path):
    "Every sample keeps the seed of its own plan position."

    staging = make_staging_layout(str(tmp_path), "seeds")
    worker = build_worker(staging, REL_PATHS, 6)
    sources = worker._augment_sources()
    # six copies over four originals: the first two carry two copies each
    assert worker._augment_plan(sources) == [2, 2, 1, 1]

    jobs = list(worker._augment_jobs(sources, [2, 0, 3, 1]))
    assert [job.ordinal for job in jobs] == list(range(6))
    assert [job.sample_index for job in jobs] == [
        1,
        2,
        2001,
        2002,
        2003,
        3001,
    ]
    assert [job.image_name for job in jobs] == [
        "a_aug1.jpg",
        "a_aug2.jpg",
        "c_aug1.png",
        "c_aug2.png",
        "c_aug3.png",
        "d_aug1.png",
    ]
    assert [job.copy_index for job in jobs] == [1, 2, 1, 2, 3, 1]

    # the plan of the run derives the very same sample positions
    jobs = list(worker._augment_jobs(sources, worker._augment_plan(sources)))
    assert [job.sample_index for job in jobs] == [
        1,
        2,
        1001,
        1002,
        2001,
        3001,
    ]


# ---------------------------------------------------------------- progress
@requires_albumentations
def test_the_progress_counts_every_finished_sample(tmp_path, qt_app):
    "The counter rises one sample at a time and ends on the total."

    staging = make_staging_layout(str(tmp_path), "progress")
    worker = build_worker(staging, REL_PATHS, 6)
    updates = []
    worker.progress.connect(
        lambda stage, done, total, message: updates.append(
            (stage, done, total, message)
        )
    )

    worker._stage_augment()

    augment = [item for item in updates if item[0] == STAGE_AUGMENT]
    assert augment[0] == (STAGE_AUGMENT, 0, 6, "Augmenting images...")
    done = [item[1] for item in augment]
    assert done == sorted(done)
    assert done[1:] == [1, 2, 3, 4, 5, 6]
    assert {item[2] for item in augment} == {6}
    assert augment[-1][3] == "Augmenting 6/6"
    assert worker.augment_summary["generated"] == 6


# ------------------------------------------------------------ cancellation
@requires_albumentations
def test_a_cancelled_run_writes_nothing(tmp_path, qt_app):
    "A cancellation requested before the stage submits no sample at all."

    staging = make_staging_layout(str(tmp_path), "cancel0")
    worker = build_worker(staging, REL_PATHS, 6)
    worker.cancel_requested = True

    worker._stage_augment()

    assert worker.augment_summary["generated"] == 0
    assert augmented_digests(staging) == {}
    assert [
        record
        for record in worker.records
        if record.kind == records_module.KIND_AUGMENTED
    ] == []


@requires_albumentations
def test_cancelling_stops_submitting_and_drops_the_results(
    tmp_path, qt_app, monkeypatch
):
    """A cancel stops the submissions and merges no result at all."""

    staging = make_staging_layout(str(tmp_path), "cancel1")
    # more samples than the window of the pool keeps in flight, so the
    # cancel really has to stop the submissions
    relpaths = ["img%02d.png" % index for index in range(12)]
    pin_workers(monkeypatch, 4)
    worker = build_worker(staging, relpaths, 12)
    real_augment = augment_module.augment_sample
    started = []

    def cancelling_augment(image, label, params, sample_index, **kwargs):
        started.append(sample_index)
        worker.cancel_requested = True
        return real_augment(image, label, params, sample_index, **kwargs)

    monkeypatch.setattr(augment_module, "augment_sample", cancelling_augment)

    worker._stage_augment()

    # the window bounds the samples that were submitted before the cancel
    assert 0 < len(started) <= 2 * 4
    assert len(started) < len(relpaths)
    assert worker.augment_summary["generated"] == 0
    assert worker.augment_summary["failed"] == 0
    assert [
        record
        for record in worker.records
        if record.kind == records_module.KIND_AUGMENTED
    ] == []
    assert worker.warnings == []


# --------------------------------------------------------- error isolation
@requires_albumentations
def test_a_broken_sample_is_skipped_and_counted(tmp_path, qt_app, monkeypatch):
    "An exception inside one sample never fails the whole stage."

    staging = make_staging_layout(str(tmp_path), "broken")
    worker = build_worker(staging, REL_PATHS, 6)
    real_augment = augment_module.augment_sample

    def flaky_augment(image, label, params, sample_index, **kwargs):
        if sample_index == 2001:
            raise RuntimeError("boom")
        return real_augment(image, label, params, sample_index, **kwargs)

    monkeypatch.setattr(augment_module, "augment_sample", flaky_augment)

    worker._stage_augment()

    summary = worker.augment_summary
    assert summary["planned"] == 6
    assert summary["generated"] == 5
    assert summary["failed"] == 1
    assert any("boom" in warning for warning in worker.warnings)
    assert [
        record.relpath
        for record in worker.records
        if record.kind == records_module.KIND_AUGMENTED
    ] == [
        "a_aug1.jpg",
        "a_aug2.jpg",
        "sub/b_aug1.jpg",
        "sub/b_aug2.jpg",
        "d_aug1.png",
    ]


@requires_albumentations
def test_an_undecodable_image_is_reported_once(tmp_path, qt_app, monkeypatch):
    "A decode failure skips the copies of the image and warns once."

    staging = make_staging_layout(str(tmp_path), "decode")
    worker = build_worker(staging, REL_PATHS, 6)
    real_decode = augment_module.decode_image

    def broken_decode(path):
        if osp.basename(path) == "a.jpg":
            return None
        return real_decode(path)

    monkeypatch.setattr(augment_module, "decode_image", broken_decode)

    worker._stage_augment()

    summary = worker.augment_summary
    assert summary["generated"] == 4
    assert summary["failed"] == 2
    assert worker.warnings == ["could not decode a.jpg for augmentation"]
    assert [
        record.relpath
        for record in worker.records
        if record.kind == records_module.KIND_AUGMENTED
    ] == [
        "sub/b_aug1.jpg",
        "sub/b_aug2.jpg",
        "c_aug1.png",
        "d_aug1.png",
    ]
