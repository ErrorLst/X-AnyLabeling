"""Threaded inference stage: determinism, progress, cancel, isolation.

The exported detector has a static batch of one, therefore the stage
parallelises with several independent sessions instead of one batched
session. These tests pin down what that concurrency may never change:
the verdict, the reasons and the detail of every record (for any session
count and in any completion order), the progress semantics, the
cancellation and the isolation of a broken image.
"""

import hashlib
import os
import os.path as osp
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PyQt6 import QtWidgets

from anylabeling.custom.model_validation import app_config
from anylabeling.custom.model_validation import dataset
from anylabeling.custom.model_validation import inference as inference_module
from anylabeling.custom.model_validation import pipeline as pipeline_module
from anylabeling.custom.model_validation import records as records_module
from anylabeling.custom.model_validation.app_config import (
    DEFAULT_INFER_WORKERS,
    DEFAULT_WORKERS,
    ValidationConfig,
    default_workers,
    infer_session_threads,
    infer_threads_snapshot,
    logical_cores,
    resolve_infer_workers,
)
from anylabeling.custom.model_validation.pipeline import (
    STAGE_INFER,
    ValidationWorker,
)

CLASSES = ["car"]
BOX = [[4, 4], [28, 4], [28, 24], [4, 24]]


@pytest.fixture(scope="module")
def qt_app():
    "Create the offscreen application needed to receive the signals."

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


# --------------------------------------------------------------- fixtures
def make_staging_layout(scratch: str, suffix: str) -> str:
    "Create the staging folder layout the tool expects."

    staging = osp.join(scratch, dataset.STAGING_PREFIX + suffix)
    for folder in (dataset.ORIGINAL_DIRNAME, dataset.AUGMENTED_DIRNAME):
        for sub in (dataset.IMAGES_DIRNAME, dataset.LABELS_DIRNAME):
            os.makedirs(osp.join(staging, folder, sub), exist_ok=True)
    return staging


def label_payload(image_name: str) -> dict:
    "Return the xlabel payload of one staged pair."

    return {
        "version": "3.0.0",
        "flags": {},
        "checked": False,
        "shapes": [
            {
                "label": "car",
                "shape_type": "rectangle",
                "points": [list(point) for point in BOX],
                "group_id": None,
                "description": "",
                "flags": {},
            }
        ],
        "imagePath": image_name,
        "imageData": None,
        "imageHeight": 32,
        "imageWidth": 40,
    }


def staged_original(staging: str, relpath: str, with_label: bool = True):
    "Create one staged original holding a deterministic image."

    import cv2

    paths = dataset.staging_paths(
        staging, records_module.KIND_ORIGINAL, relpath
    )
    os.makedirs(osp.dirname(paths["image"]), exist_ok=True)
    generator = np.random.default_rng(
        int(hashlib.sha256(relpath.encode("utf-8")).hexdigest()[:8], 16)
    )
    image = generator.integers(0, 255, size=(32, 40, 3), dtype=np.uint8)
    ok, buffer = cv2.imencode(".png", image)
    assert ok
    buffer.tofile(paths["image"])
    label_path = ""
    if with_label:
        dataset.write_json(paths["label"], label_payload(relpath))
        label_path = paths["label"]
    return records_module.make_record(
        records_module.KIND_ORIGINAL, relpath, paths["image"], label_path
    )


def build_worker(staging: str, records) -> ValidationWorker:
    "Build an unstarted worker holding the given records."

    config = ValidationConfig(
        dataset_dir="/source",
        model_path="/model.onnx",
        augment_enabled=False,
    )
    worker = ValidationWorker(config, CLASSES, staging)
    worker.records = list(records)
    return worker


def pin_sessions(monkeypatch, sessions: int) -> None:
    """Run the next stage with an explicit amount of sessions.

    The concurrency of the tool is fixed, so the session count is no
    longer reachable through the configuration; the determinism of the
    stage is still proven by running the very same images through
    pools of different sizes, which this seam drives. Both bindings
    are pinned because the stage and the per session budget read the
    same single source.
    """

    monkeypatch.setattr(
        app_config, "default_workers", lambda cpu_count=None: sessions
    )
    monkeypatch.setattr(
        pipeline_module, "default_workers", lambda cpu_count=None: sessions
    )


# ------------------------------------------------------------- the runner
class StubRunner:
    """Stand-in for ModelRunner answering from the bytes of the image.

    The predictions must depend on the file itself: a stage that mixes
    up the images, reuses the runner of another thread or judges a
    record twice would then produce a different verdict, which is
    exactly what these tests have to catch. An image named in the
    broken list raises like an undecodable file does.
    """

    created = 0
    broken: tuple = ()
    forced: dict = {}
    delay = 0.0
    lock = threading.Lock()
    calls = []
    owners = {}

    def __init__(self, model_path, classes, **kwargs):
        with StubRunner.lock:
            self.index = StubRunner.created
            StubRunner.created += 1
        self.model_path = model_path
        self.classes = list(classes)
        self.kwargs = dict(kwargs)
        self.warnings = []

    def predict(self, image_path):
        "Return as many boxes as the first digest byte of the file asks."

        if StubRunner.delay:
            time.sleep(StubRunner.delay)
        with open(image_path, "rb") as handle:
            digest = hashlib.sha256(handle.read()).digest()
        with StubRunner.lock:
            StubRunner.calls.append(image_path)
            StubRunner.owners.setdefault(self.index, set()).add(
                threading.get_ident()
            )
        name = osp.basename(image_path)
        if name in StubRunner.broken:
            raise inference_module.InferenceError("cannot decode " + name)
        count = StubRunner.forced.get(name, digest[0] % 3)
        shift = digest[1] % 3
        return [
            {
                "label": CLASSES[0],
                "shape_type": "rectangle",
                "points": [
                    [float(point[0]) + shift, float(point[1])] for point in BOX
                ],
                "score": 0.5 + digest[0] / 1000.0,
            }
            for _index in range(count)
        ]

    def model_info(self):
        "Return the model snapshot stored inside the report."

        return {"path": self.model_path, "task": "detect"}


@pytest.fixture
def stub_runner(monkeypatch):
    "Replace every ModelRunner of the package by the stand-in above."

    monkeypatch.setattr(inference_module, "ModelRunner", StubRunner)
    StubRunner.created = 0
    StubRunner.calls = []
    StubRunner.owners = {}
    StubRunner.broken = ()
    StubRunner.forced = {}
    StubRunner.delay = 0.0
    return StubRunner


def signature(records):
    "Return the judged fields of every record, in list order."

    return [
        (
            record.relpath,
            record.kind,
            record.verdict,
            list(record.reasons),
            record.detail,
            record.judged,
        )
        for record in records
    ]


REL_PATHS = tuple("img%02d.png" % index for index in range(12))

# a machine derived for a single session runs the stage serially: the
# pool expectations below only apply from two sessions on
requires_parallel_pool = pytest.mark.skipif(
    default_workers() <= 1,
    reason="this machine is derived for a single session (serial path)",
)


# ------------------------------------------------------------- the default
def test_the_session_count_is_the_fixed_half_of_the_logical_cores():
    "The stage runs half of the logical processors, never below one."

    assert logical_cores() == int(os.cpu_count() or 1)
    assert DEFAULT_INFER_WORKERS == DEFAULT_WORKERS == default_workers()
    assert DEFAULT_WORKERS == max(1, (os.cpu_count() or 1) // 2)
    config = ValidationConfig()
    assert config.infer_workers == DEFAULT_INFER_WORKERS
    payload = config.to_dict()
    assert payload["infer_workers"] == DEFAULT_INFER_WORKERS
    assert payload["infer_workers_default"] == DEFAULT_INFER_WORKERS
    assert payload["workers_fixed"] is True
    assert payload["workers_rule"] == "max(1, os.cpu_count() // 2)"


def test_a_stored_session_count_is_clamped_into_the_fixed_range():
    "1 is serial, 0 and negative values are serial too, everything caps."

    assert resolve_infer_workers(1) == 1
    assert resolve_infer_workers(0) == 1
    assert resolve_infer_workers(-3) == 1
    assert resolve_infer_workers(DEFAULT_WORKERS) == DEFAULT_WORKERS
    assert resolve_infer_workers(DEFAULT_WORKERS + 1) == DEFAULT_WORKERS
    assert resolve_infer_workers(10**6) == DEFAULT_WORKERS
    assert resolve_infer_workers(None) == DEFAULT_WORKERS
    assert resolve_infer_workers("many") == DEFAULT_WORKERS
    for value in (-5, 0, 1, 3, DEFAULT_WORKERS, 10**6, None, "many"):
        assert 1 <= resolve_infer_workers(value) <= DEFAULT_WORKERS


def test_the_threads_of_a_machine_are_split_between_the_sessions():
    """Every parallel session keeps a slice of the cores, never all.

    A session with the default options takes every core for itself, so N
    sessions over subscribe the machine and end up as slow as a single
    one; the budget below is what keeps that from happening. The fixed
    count splits the machine evenly - a 22 processor machine runs its
    eleven sessions with max(1, 22 // 11) = 2 threads each - and the
    single session is the exception: it has nothing to share and keeps
    the default of the runtime, which is the fastest serial setting.
    """

    # the fixed count of a machine is half of its logical processors
    assert default_workers(22) == 11
    assert default_workers(8) == 4
    assert default_workers(4) == 2
    assert default_workers(2) == 1
    assert default_workers(1) == 1
    # and every session keeps its own slice of that machine: halving
    # the processors leaves two threads per session from four on
    assert infer_session_threads(cpu_count=22) == (2, 1)
    assert infer_session_threads(cpu_count=8) == (2, 1)
    assert infer_session_threads(cpu_count=4) == (2, 1)
    assert infer_session_threads(cpu_count=6) == (2, 1)
    # one or two processors leave a single session, nothing to share
    assert infer_session_threads(cpu_count=2) == (0, 0)
    assert infer_session_threads(cpu_count=1) == (0, 0)
    # the very same rule describes this machine
    if default_workers() > 1:
        assert infer_session_threads() == (
            max(1, logical_cores() // default_workers()),
            1,
        )
    else:
        assert infer_session_threads() == (0, 0)
    for cores in (3, 4, 6, 8, 12, 16, 22):
        sessions = default_workers(cores)
        intra, inter = infer_session_threads(cpu_count=cores)
        assert inter == (1 if sessions > 1 else 0)
        if sessions > 1:
            assert 1 <= intra <= cores
            assert sessions * intra <= cores


def test_the_effective_threads_reach_the_report_snapshot():
    "The report records the session count and the budget of a session."

    payload = ValidationConfig().to_dict()
    assert payload["infer_workers"] == DEFAULT_INFER_WORKERS
    assert payload["infer_workers_default"] == DEFAULT_INFER_WORKERS
    snapshot = payload["infer_session_threads"]
    assert snapshot == infer_threads_snapshot()
    assert snapshot["workers"] == DEFAULT_INFER_WORKERS
    assert snapshot["workers"] == DEFAULT_WORKERS == default_workers()
    assert snapshot["cpu_count"] == logical_cores()
    intra, inter = infer_session_threads()
    assert snapshot["intra_op_threads"] == intra
    assert snapshot["inter_op_threads"] == inter
    assert snapshot["runtime_default_threads"] is (intra == 0)
    # the fixed inference cut reaches the snapshot under two new keys
    assert payload["inference_conf_threshold"] == (
        app_config.INFERENCE_CONF_THRESHOLD
    )
    assert payload["inference_conf_threshold"] == 0.25
    assert payload["inference_conf_threshold_fixed"] is True
    if default_workers() > 1:
        assert snapshot["inter_op_threads"] == 1
        assert snapshot["intra_op_threads"] >= 1
        assert snapshot["runtime_default_threads"] is False


# ------------------------------------------------------------ determinism
def test_the_session_count_never_changes_a_verdict(
    tmp_path, qt_app, stub_runner, monkeypatch
):
    """One session and four sessions judge the same images identically.

    The records are compared field by field - verdict, reasons, detail -
    and in list order: the verdict of an image may never depend on the
    number of sessions nor on the order the images finished in. A second
    four session run must reproduce them as well. The session count of
    the tool itself is fixed, so the two sizes are driven through the
    very seam the stage reads its count from.
    """

    names = list(REL_PATHS)
    workers = []
    counts = []
    for suffix, count in (("serial", 1), ("quad", 4), ("quad2", 4)):
        staging = make_staging_layout(str(tmp_path), suffix)
        workers.append(
            build_worker(
                staging,
                [staged_original(staging, name) for name in names],
            )
        )
        counts.append(count)
    serial, quad, again = workers
    # three images are pinned so that the batch holds OK and NG verdicts
    # whatever the bytes of the generated files are
    stub_runner.forced = {"img00.png": 0, "img01.png": 1, "img02.png": 2}
    stub_runner.broken = ("img03.png",)

    for worker, count in zip(workers, counts):
        pin_sessions(monkeypatch, count)
        worker._stage_infer()

    assert serial.infer_summary["workers"] == 1
    # the serial run keeps the runtime default: nothing to share
    assert serial.infer_summary["intra_op_threads"] == 0
    assert serial.infer_summary["inter_op_threads"] == 0
    assert serial.infer_summary["runtime_default_threads"] is True
    assert quad.infer_summary["workers"] == 4
    assert quad.infer_summary["images"] == len(names)
    assert quad.infer_summary["inter_op_threads"] == 1
    assert quad.infer_summary["runtime_default_threads"] is False
    assert quad.infer_summary["intra_op_threads"] == (
        max(1, logical_cores() // 4)
    )

    reference = signature(serial.records)
    assert [item[0] for item in reference] == names
    assert signature(quad.records) == reference
    assert signature(again.records) == reference
    # the images really produced different verdicts, otherwise the
    # comparison above would prove nothing
    assert {item[2] for item in reference} == {"OK", "NG"}
    assert reference[0][2] == "NG"
    assert reference[1][2] == "OK"
    assert reference[3][2] == "NG"
    assert reference[3][3] == ["INFER_ERROR"]
    # every image is inferred exactly once whatever the session count
    assert sorted(osp.basename(path) for path in stub_runner.calls) == (
        sorted(names * 3)
    )


def test_every_thread_keeps_its_own_runner(
    tmp_path, qt_app, stub_runner, monkeypatch
):
    "A runner - and so a session - is never shared between two threads."

    staging = make_staging_layout(str(tmp_path), "owners")
    worker = build_worker(
        staging,
        [staged_original(staging, name) for name in REL_PATHS],
    )
    pin_sessions(monkeypatch, 4)
    # the delay keeps the four threads alive at the same time
    stub_runner.delay = 0.02
    worker._stage_infer()

    assert stub_runner.created == 4
    assert sorted(stub_runner.owners) == [0, 1, 2, 3]
    threads = [thread for ids in stub_runner.owners.values() for thread in ids]
    assert len(threads) == 4
    assert len(set(threads)) == 4
    assert all(record.judged for record in worker.records)


def test_a_staged_image_without_a_label_is_never_inferred(
    tmp_path, qt_app, stub_runner
):
    "A SKIPPED record keeps its verdict, the model never sees it."

    staging = make_staging_layout(str(tmp_path), "skipped")
    records = [
        staged_original(staging, "labelled.png"),
        staged_original(staging, "bare.png", with_label=False),
    ]
    records[1].verdict = records_module.SKIPPED
    records[1].reasons = ["NO_LABEL"]
    # one predicted box matches the label of the image: a verdict of its
    # own, whatever the bytes of the generated file are
    stub_runner.forced = {"labelled.png": 1}
    worker = build_worker(staging, records)

    worker._stage_infer()

    assert [record.verdict for record in records] == [
        records_module.OK,
        records_module.SKIPPED,
    ]
    assert stub_runner.calls == [records[0].staging_image_path]


# ---------------------------------------------------------------- progress
def test_the_progress_counts_every_inferred_image(
    tmp_path, qt_app, stub_runner
):
    "The counter rises one image at a time and ends on the total."

    staging = make_staging_layout(str(tmp_path), "progress")
    worker = build_worker(
        staging,
        [staged_original(staging, name) for name in REL_PATHS],
    )
    updates = []
    stages = []
    worker.progress.connect(
        lambda stage, done, total, message: updates.append(
            (stage, done, total, message)
        )
    )
    worker.stage_finished.connect(
        lambda stage, count: stages.append((stage, count))
    )

    worker._stage_infer()

    infer = [item for item in updates if item[0] == STAGE_INFER]
    assert infer[0] == (STAGE_INFER, 0, 12, "Running inference...")
    done = [item[1] for item in infer]
    assert done == sorted(done)
    assert done[1:] == list(range(1, 13))
    assert {item[2] for item in infer} == {12}
    assert infer[-1][3] == "Inference 12/12"
    assert stages == [(STAGE_INFER, 12)]


# ------------------------------------------------------------ cancellation
def test_a_cancelled_run_infers_nothing(tmp_path, qt_app, stub_runner):
    "A cancellation requested before the stage submits no image at all."

    staging = make_staging_layout(str(tmp_path), "cancel0")
    worker = build_worker(
        staging,
        [staged_original(staging, name) for name in REL_PATHS],
    )
    worker.cancel_requested = True

    worker._stage_infer()

    assert stub_runner.calls == []
    assert [record.judged for record in worker.records] == [False] * 12
    assert {record.verdict for record in worker.records} == {
        records_module.PENDING
    }


def test_cancelling_stops_submitting_and_drops_the_results(
    tmp_path, qt_app, stub_runner, monkeypatch
):
    "A cancel stops the submissions and merges no result at all."

    staging = make_staging_layout(str(tmp_path), "cancel1")
    # the window of the pool bounds the submissions, so the size is
    # pinned: the cancel then really has to stop the loop
    pin_sessions(monkeypatch, 4)
    worker = build_worker(
        staging,
        [staged_original(staging, name) for name in REL_PATHS],
    )
    started = []

    def cancelling_predict(self, image_path):
        "Report the image, then ask for the cancellation of the run."

        started.append(image_path)
        worker.cancel_requested = True
        return []

    monkeypatch.setattr(stub_runner, "predict", cancelling_predict)
    stub_runner.delay = 0.05

    worker._stage_infer()

    # the window bounds the images that were submitted before the cancel
    assert 0 < len(started) <= 2 * 4
    assert len(started) < len(REL_PATHS)
    assert [record.judged for record in worker.records] == [False] * 12


# --------------------------------------------------------- error isolation
def test_a_broken_image_is_reported_on_its_own_record_only(
    tmp_path, qt_app, stub_runner
):
    "An infer error never fails the stage nor the other images."

    staging = make_staging_layout(str(tmp_path), "broken")
    worker = build_worker(
        staging,
        [staged_original(staging, name) for name in REL_PATHS],
    )
    stages = []
    failures = []
    worker.stage_finished.connect(
        lambda stage, count: stages.append((stage, count))
    )
    worker.failed.connect(lambda title, text: failures.append(title))
    stub_runner.broken = ("img05.png",)

    worker._stage_infer()

    assert failures == []
    assert stages == [(STAGE_INFER, 12)]
    broken = [r for r in worker.records if r.relpath == "img05.png"][0]
    assert broken.verdict == "NG"
    assert broken.reasons == ["INFER_ERROR"]
    assert "cannot decode" in broken.detail["error"]
    assert broken.judged is True
    assert all(record.judged for record in worker.records)
    # the other images kept a judgement of their own
    others = [r for r in worker.records if r.relpath != "img05.png"]
    assert all(r.detail.get("error") is None for r in others)


def test_an_unexpected_failure_is_never_swallowed(
    tmp_path, qt_app, stub_runner, monkeypatch
):
    """A failure outside the model call still reaches the caller.

    Only the model call of one image is isolated: a failure of the
    judging itself is a real failure and must not disappear inside the
    pool, exactly like it aborted the serial loop before.
    """

    staging = make_staging_layout(str(tmp_path), "hard")
    records = [staged_original(staging, name) for name in REL_PATHS[:3]]
    worker = build_worker(staging, records)

    def exploding_label(record):
        "Fail the way an unreadable label would."

        raise RuntimeError("label explosion")

    monkeypatch.setattr(records_module, "read_staging_label", exploding_label)
    with pytest.raises(RuntimeError) as error:
        worker._stage_infer()
    assert "label explosion" in str(error.value)
    assert [record.judged for record in records] == [False] * 3


def test_the_stage_runs_the_fixed_session_count_whatever_config_says(
    tmp_path, qt_app, stub_runner
):
    """The stored field can not change the amount of sessions.

    The configuration page owns no control for the concurrency any
    more, so a stored configuration that still carries a value - here
    the serial single session - is ignored: the stage builds the fixed
    count of sessions of the machine, every one with its own slice of
    the cores.
    """

    staging = make_staging_layout(str(tmp_path), "fixed_sessions")
    worker = build_worker(
        staging,
        [staged_original(staging, name) for name in REL_PATHS[:3]],
    )
    worker.config.infer_workers = 1

    worker._stage_infer()

    assert worker.infer_summary["workers"] == default_workers()
    assert stub_runner.created == default_workers()
    intra, inter = infer_session_threads()
    assert worker.infer_summary["intra_op_threads"] == intra
    assert worker.infer_summary["inter_op_threads"] == inter
    assert [record.judged for record in worker.records] == [True] * 3


@requires_parallel_pool
def test_every_session_thread_pins_the_opencv_pool(
    tmp_path, qt_app, stub_runner, monkeypatch
):
    """Every thread of the parallel session pool pins OpenCV too.

    The inference preprocesses and postprocesses through the very same
    OpenCV kernels as the augment stage, so its parallel workers pin
    the OpenCV thread pool before their first image as well.
    """

    import cv2

    calls = []
    monkeypatch.setattr(
        cv2, "setNumThreads", lambda count: calls.append(int(count))
    )

    staging = make_staging_layout(str(tmp_path), "opencv_infer")
    worker = build_worker(
        staging,
        [staged_original(staging, name) for name in REL_PATHS[:4]],
    )

    worker._stage_infer()

    assert worker.infer_summary["workers"] == default_workers()
    assert worker.infer_summary["workers"] > 1
    assert calls, "the initializer of the pool never ran"
    assert set(calls) == {app_config.OPENCV_THREADS} == {1}


def test_the_serial_inference_keeps_the_opencv_default(
    tmp_path, qt_app, stub_runner, monkeypatch
):
    "One session has nothing to share: the runtime default stays."

    import cv2

    calls = []
    monkeypatch.setattr(
        cv2, "setNumThreads", lambda count: calls.append(int(count))
    )
    pin_sessions(monkeypatch, 1)

    staging = make_staging_layout(str(tmp_path), "opencv_serial")
    worker = build_worker(
        staging,
        [staged_original(staging, name) for name in REL_PATHS[:4]],
    )

    worker._stage_infer()

    assert worker.infer_summary["workers"] == 1
    assert calls == []


# ----------------------------------------------------------- the pinning
class FakeSession:
    "Stand-in for the ONNX session the engine builds."

    def __init__(self, model_path, device) -> None:
        self.model_path = model_path
        self.device = device

    def get_input_shape(self):
        "Return a static NCHW input shape."

        return [1, 3, 640, 640]


class FakeYOLO:
    "Stand-in for the ultralytics wrapper of the repository."

    def __init__(self, config, on_message=None) -> None:
        self.config = dict(config)
        self.classes = list(self.config.get("classes") or [])
        self.net = FakeSession(self.config.get("model_path", ""), "cpu")

    def predict_shapes(self, image, image_path=None):
        "Answer one shape per class of the effective table."

        return type("Result", (), {"shapes": []})()


@pytest.fixture
def pinned_session(monkeypatch):
    "Record every session the pinned runner path builds."

    recorded = []

    class RecordingSession(FakeSession):
        "Stand-in for ThreadedOnnxSession recording its arguments."

        def __init__(self, model_path, device, **kwargs):
            super().__init__(model_path, device)
            recorded.append(dict(kwargs, path=model_path, device=device))

    monkeypatch.setattr(
        inference_module, "ThreadedOnnxSession", RecordingSession
    )
    return recorded


@pytest.fixture
def fake_engine(monkeypatch, tmp_path):
    "Replace the engine and the wrapper by the stand-ins above."

    import anylabeling.services.auto_labeling.__base__.yolo as yolo_base
    import anylabeling.services.auto_labeling.engines as engines

    model_path = os.path.join(str(tmp_path), "model.onnx")
    with open(model_path, "wb") as handle:
        handle.write(b"onnx-stand-in")
    monkeypatch.setattr(engines, "OnnxBaseModel", FakeSession)
    monkeypatch.setattr(yolo_base, "YOLO", FakeYOLO)
    monkeypatch.setattr(
        inference_module,
        "read_onnx_metadata",
        lambda _path: {
            "task": "detect",
            "imgsz": "[640, 640]",
            "names": "{0: 'car'}",
        },
    )
    monkeypatch.setattr(
        inference_module, "load_image_rgb", lambda _path: [[0, 0, 0]]
    )
    return model_path


def test_a_pinned_runner_replaces_the_session_of_the_engine(
    fake_engine, pinned_session
):
    "intra_op_threads swaps the session for one carrying the budget."

    runner = inference_module.ModelRunner(
        fake_engine,
        CLASSES,
        intra_op_threads=5,
        inter_op_threads=1,
    )
    # the session of the engine is gone, replaced by the pinned one
    assert type(runner.model.net).__name__ == "RecordingSession"
    assert len(pinned_session) == 1
    assert pinned_session[0]["path"] == runner.model_path
    assert pinned_session[0]["intra_op_threads"] == 5
    assert pinned_session[0]["inter_op_threads"] == 1
    assert runner.input_shape == [1, 3, 640, 640]


def test_a_plain_runner_keeps_the_session_of_the_engine(fake_engine):
    "Without a budget the runner behaves exactly like before."

    runner = inference_module.ModelRunner(fake_engine, CLASSES)
    assert isinstance(runner.model.net, FakeSession)


def test_the_pool_builds_one_pinned_runner_per_session(
    fake_engine, pinned_session
):
    "The pool pays the load time once per worker, not once per image."

    runners = inference_module.build_runner_pool(
        fake_engine,
        CLASSES,
        3,
        iou_threshold=0.4,
        intra_op_threads=7,
        inter_op_threads=1,
    )
    assert len(runners) == 3
    assert len({id(runner) for runner in runners}) == 3
    # whatever the caller hands over, every runner keeps the frozen cut
    # and the multi label NMS of a detect model
    assert all(
        runner.model.config["conf_threshold"]
        == app_config.INFERENCE_CONF_THRESHOLD
        == 0.25
        for runner in runners
    )
    assert all(
        runner.model.config["multi_label"] is True for runner in runners
    )
    # every session of the pool carries the very same budget
    assert [item["intra_op_threads"] for item in pinned_session] == [7, 7, 7]
    assert [item["inter_op_threads"] for item in pinned_session] == [1, 1, 1]
    # a nonsense count still builds a usable pool
    assert (
        len(inference_module.build_runner_pool(fake_engine, CLASSES, 0)) == 1
    )
