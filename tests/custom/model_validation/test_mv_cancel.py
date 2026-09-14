"""Cancelling a run: stop it, drop it, go back to the form.

A cancel is not a failure. The button asks the worker to stop, cuts its
signals off before anything else it produces can reach the window and
throws the whole run away - the records, the counters, the model snapshot
and the warnings - so that the configuration page is back on screen with
the estimate of the source directory and the next run starts from
scratch. The staging folder itself is kept, like every other staging
folder of this tool.

The worker side of the same story is pinned here as well: the cancel flag
is read once per image in all three stages and a cancelled run reports
itself on a signal of its own instead of the failure path.
"""

import os
import os.path as osp
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from PyQt6 import QtTest, QtWidgets

from anylabeling.custom.model_validation import dataset
from anylabeling.custom.model_validation import records as records_module
from anylabeling.custom.model_validation.app_config import ValidationConfig
from anylabeling.custom.model_validation.pipeline import (
    STAGE_AUGMENT,
    STAGE_INFER,
    STAGE_STAGING,
    ValidationWorker,
    _AugmentJob,
    _InferJob,
)
from anylabeling.custom.model_validation.ui.dialog import (
    CANCELLED_STATUS,
    ModelValidationDialog,
)
from anylabeling.custom.model_validation.ui.progress_page import CANCEL_MESSAGE

CLASSES = ["a0_dian"]


@pytest.fixture(scope="module")
def qt_app():
    "Create the offscreen application shared by the cancel tests."

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


@pytest.fixture
def dialog(qt_app):
    "Create a model validation window and close it afterwards."

    window = ModelValidationDialog()
    try:
        yield window
    finally:
        window.close()


def write_image(path: str, value: int = 40) -> None:
    "Write a tiny deterministic png file."

    import cv2

    os.makedirs(osp.dirname(path), exist_ok=True)
    image = np.full((24, 32, 3), int(value), dtype=np.uint8)
    ok, buffer = cv2.imencode(".png", image)
    assert ok
    buffer.tofile(path)


def label_payload(relpath: str) -> dict:
    "Return the xlabel payload of a staged pair."

    return {
        "version": "3.0.0",
        "flags": {},
        "checked": False,
        "shapes": [
            {
                "label": "a0_dian",
                "shape_type": "rectangle",
                "points": [[4, 4], [20, 4], [20, 20], [4, 20]],
                "group_id": None,
                "description": "",
                "flags": {},
            }
        ],
        "imagePath": osp.basename(relpath),
        "imageData": None,
        "imageHeight": 24,
        "imageWidth": 32,
    }


def staging_layout(scratch: str, suffix: str) -> str:
    "Create the staging folder layout the tool expects."

    staging = osp.join(scratch, dataset.STAGING_PREFIX + suffix)
    for folder in (dataset.ORIGINAL_DIRNAME, dataset.AUGMENTED_DIRNAME):
        for sub in (dataset.IMAGES_DIRNAME, dataset.LABELS_DIRNAME):
            os.makedirs(osp.join(staging, folder, sub), exist_ok=True)
    return staging


def staged_original(staging: str, relpath: str):
    "Create one staged original record with its files."

    paths = dataset.staging_paths(
        staging, records_module.KIND_ORIGINAL, relpath
    )
    write_image(paths["image"])
    dataset.write_json(paths["label"], label_payload(relpath))
    return records_module.make_record(
        records_module.KIND_ORIGINAL, relpath, paths["image"], paths["label"]
    )


def source_pair(directory: str, name: str) -> None:
    "Write one image/label pair into a source dataset directory."

    write_image(osp.join(directory, name))
    dataset.write_json(
        osp.join(directory, osp.splitext(name)[0] + ".json"),
        label_payload(name),
    )


def build_worker(staging: str, records) -> ValidationWorker:
    "Build an unstarted worker holding the given records."

    config = ValidationConfig(
        dataset_dir="/source",
        model_path="/model.onnx",
        classes_file="/classes.txt",
    )
    worker = ValidationWorker(config, CLASSES, staging)
    worker.records = list(records)
    return worker


def wait_for_source_scan(dialog, source: str, timeout_ms: int = 5000) -> bool:
    """Let the debounced scan of the source directory answer.

    The pair count of a source folder is no longer computed inside the
    slot that watches the path line edit (see _on_dataset_changed): the
    window asks the scheduler for a debounced scan, so a test that wants
    the estimate of that folder has to let that answer arrive.
    """

    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if (
            dialog.source_dataset_dir == source
            and not dialog._scan_pending
        ):
            return True
        QtTest.QTest.qWait(10)
    return (
        dialog.source_dataset_dir == source and not dialog._scan_pending
    )


def loaded_dialog(dialog, tmp_path, records=3):
    "Return a window that looks like a run in progress."

    source = osp.join(str(tmp_path), "source")
    for index in range(records):
        source_pair(source, f"a{index}.png")
    staging = staging_layout(str(tmp_path), "cancel")
    staged = [staged_original(staging, f"a{index}.png") for index in range(2)]
    for record in staged:
        record.verdict = records_module.OK
    dialog.config_page.set_dataset(source)
    dialog.classes = list(CLASSES)
    dialog.staging_root = staging
    dialog.records = staged
    worker = build_worker(staging, staged)
    dialog.worker = worker
    dialog.results_page.set_context(list(CLASSES), staging)
    dialog.results_page.set_records(staged)
    # the page opens on NG and this test looks at the whole list; the
    # switch only asks for the rebuild, so the events are driven once
    dialog.results_page.filter_combo.setCurrentIndex(0)
    QtWidgets.QApplication.processEvents()
    dialog.refresh_export_summary()
    # the live preview of a run in progress counts the staged records
    dialog._refresh_preview()
    dialog.progress_page.set_progress(STAGE_INFER, 3, 10, "Inference 3/10")
    dialog.stack.setCurrentWidget(dialog.progress_page)
    return worker, staged, source, staging


# ------------------------------------------------------------- the worker
def test_the_staging_stage_asks_before_every_image(tmp_path, mv_scratch):
    "The optional stop callback ends the copy loop within one image."

    source = osp.join(str(tmp_path), "source")
    for index in range(4):
        source_pair(source, f"a{index}.png")
    staging = staging_layout(str(mv_scratch), "stop")
    asked = []

    def should_stop() -> bool:
        asked.append(1)
        return len(asked) > 2

    meta = dataset.stage_dataset(source, staging, should_stop=should_stop)
    assert len(asked) == 3
    assert meta["counts"]["original"] == 2
    copied = os.listdir(
        osp.join(staging, dataset.ORIGINAL_DIRNAME, dataset.IMAGES_DIRNAME)
    )
    assert len(copied) == 2
    # the pair that was never reached is not in the staged meta either
    assert [item["relpath"] for item in meta["originals"]] == [
        "a0.png",
        "a1.png",
    ]


def test_the_staging_stage_keeps_its_signature(tmp_path, mv_scratch):
    "Without the callback the stage copies everything, as it always did."

    source = osp.join(str(tmp_path), "source")
    for index in range(3):
        source_pair(source, f"a{index}.png")
    staging = staging_layout(str(mv_scratch), "nostop")
    meta = dataset.stage_dataset(source, staging)
    assert meta["counts"]["original"] == 3


def test_a_cancelled_augment_sample_is_dropped_without_a_warning(
    tmp_path, qt_app
):
    "The per sample check returns an empty result, it never decodes."

    staging = staging_layout(str(tmp_path), "cancelsample")
    record = staged_original(staging, "a.png")
    worker = build_worker(staging, [record])
    worker.cancel_requested = True
    job = _AugmentJob(
        ordinal=0,
        record=record,
        sample_index=1,
        copy_index=1,
        image_name="a_aug1.png",
        source_ext=".png",
    )

    result = worker._augment_one(job)

    assert result.child is None
    assert result.warnings == []
    assert (
        os.listdir(
            osp.join(
                staging, dataset.AUGMENTED_DIRNAME, dataset.IMAGES_DIRNAME
            )
        )
        == []
    )


def test_a_cancelled_image_is_never_inferred(tmp_path, qt_app):
    "The per image check returns before the runner is called."

    staging = staging_layout(str(tmp_path), "cancelinfer")
    record = staged_original(staging, "a.png")
    worker = build_worker(staging, [record])
    worker.cancel_requested = True
    calls = []

    class Runner:
        "A runner that records being asked for a prediction."

        def predict(self, path):
            "Record the call and return no shape at all."

            calls.append(path)
            return []

    result = worker._infer_one(Runner(), _InferJob(ordinal=0, record=record))

    assert calls == []
    assert result.verdict == ""
    assert result.detail == {}


def test_a_run_stopped_in_the_last_stage_reports_a_cancel(tmp_path, qt_app):
    "A run cancelled while it infers never reports itself as finished."

    staging = staging_layout(str(tmp_path), "cancellast")
    worker = build_worker(staging, [])
    seen = []
    worker.cancelled.connect(lambda: seen.append("cancelled"))
    worker.finished_ok.connect(lambda: seen.append("finished_ok"))

    def noop() -> None:
        "Stand in for a stage that finished without doing anything."

        return None

    def cancel_stage() -> None:
        "Stand in for the last stage, cancelled while it runs."

        worker.cancel_requested = True

    worker._stage_staging = noop
    worker._stage_augment = noop
    worker._stage_infer = cancel_stage

    worker.run()

    assert seen == ["cancelled"]


def test_the_worker_reports_a_cancel_on_its_own_signal(tmp_path, qt_app):
    "A cancelled run never travels through the failure path."

    staging = staging_layout(str(tmp_path), "cancelsignal")
    worker = build_worker(staging, [staged_original(staging, "a.png")])
    seen = []
    worker.cancelled.connect(lambda: seen.append("cancelled"))
    worker.failed.connect(
        lambda title, message: seen.append(("failed", title))
    )
    worker.finished_ok.connect(lambda: seen.append("finished_ok"))

    worker._emit_cancelled()

    assert seen == ["cancelled"]


# ------------------------------------------------------------- the window
def test_the_cancel_button_goes_back_to_the_form_and_drops_the_run(
    dialog, tmp_path
):
    "One click: the form is back, nothing of the run is left."

    worker, staged, source, staging = loaded_dialog(dialog, tmp_path)
    assert dialog.results_page.table.rowCount() == 2
    assert "有效原图 2" in dialog.config_page.preview_label.text()
    # the cancel falls back to the estimate of the source folder: let the
    # debounced scan of the scheduler answer before the click
    assert wait_for_source_scan(dialog, source)

    dialog.cancel_validation()

    # the worker was asked to stop and is no longer the worker of the run
    assert worker.cancel_requested is True
    assert dialog.worker is None
    # the form is on screen and says what happened
    assert dialog.stack.currentWidget() is dialog.config_page
    assert dialog.config_page.status_label.text() == CANCELLED_STATUS
    assert "#c0392b" not in dialog.config_page.status_label.styleSheet()
    # every piece of the run is gone
    assert dialog.records == []
    assert dialog.meta == {}
    assert dialog.model_info == {}
    assert dialog.augment_summary == {}
    assert dialog.warnings == []
    assert dialog.results_page.records == []
    assert dialog.results_page.table.rowCount() == 0
    assert dialog.results_page.status_label.text() == ""
    assert dialog.results_page.current_record() is None
    assert dialog.results_page.gt_canvas.image_size().width() == 0
    # the preview is back to the estimate of the source directory, and
    # the count of that directory is the one three pairs give
    assert len(dataset.collect_pairs(source).pairs) == 3
    assert dialog.config_page.preview_label.text().startswith("有效原图 3")
    # the progress page is back to a fresh state, with the note that the
    # stage may still be winding down
    assert dialog.progress_page.bar.value() == 0
    assert dialog.progress_page.bar.maximum() == 1
    assert dialog.progress_page.message_label.text() == CANCEL_MESSAGE
    assert all(
        "✓" not in label.text()
        for label in dialog.progress_page.stage_labels.values()
    )
    assert CANCEL_MESSAGE in dialog.progress_page.log.toPlainText()
    # the staging folder is kept: this tool never deletes anything
    assert osp.isdir(staging) and osp.isdir(osp.join(staging, "original"))


def test_the_cancel_cuts_the_worker_signals_off(dialog, tmp_path):
    "Nothing a dropped worker still produces can reach the window."

    worker, staged, _source, _staging = loaded_dialog(dialog, tmp_path)
    dialog.cancel_validation()

    worker.progress.emit(STAGE_INFER, 5, 10, "Inference 5/10")
    worker.stage_finished.emit(STAGE_STAGING, 3)
    worker.warnings_ready.emit(["late warning"])
    worker.dataset_ready.emit(dialog.staging_root, staged, {})
    worker.finished_ok.emit()
    worker.cancelled.emit()
    worker.failed.emit("Validation failed", "late failure")
    QtWidgets.QApplication.processEvents()

    assert dialog.records == []
    assert dialog.warnings == []
    assert dialog.stack.currentWidget() is dialog.config_page
    assert dialog.config_page.status_label.text() == CANCELLED_STATUS
    assert dialog.results_page.table.rowCount() == 0
    assert dialog.progress_page.message_label.text() == CANCEL_MESSAGE
    assert "late warning" not in dialog.progress_page.log.toPlainText()
    # the worker is remembered, so closing the window can wait for it
    assert worker in dialog._detached_workers


def test_the_cancel_never_reports_an_error(dialog, tmp_path):
    "The status line of a cancel is a note, not a failure."

    loaded_dialog(dialog, tmp_path)
    dialog.cancel_validation()

    style = dialog.config_page.status_label.styleSheet()
    text = dialog.config_page.status_label.text()
    assert "#c0392b" not in style
    assert "#27ae60" in style
    assert "Cancelled" not in text
    assert "失败" not in text
    assert dialog.progress_page.log.toPlainText().strip() == CANCEL_MESSAGE


def test_a_worker_reported_cancel_takes_the_same_path(dialog, tmp_path):
    "The dedicated cancel handler drops the run like the button does."

    loaded_dialog(dialog, tmp_path)

    dialog.on_worker_cancelled()

    assert dialog.worker is None
    assert dialog.records == []
    assert dialog.stack.currentWidget() is dialog.config_page
    assert dialog.config_page.status_label.text() == CANCELLED_STATUS
    assert dialog.results_page.table.rowCount() == 0
    assert dialog.progress_page.bar.value() == 0


def test_the_failure_path_is_still_the_failure_path(dialog, tmp_path):
    "A real error keeps its message and its colour."

    loaded_dialog(dialog, tmp_path)

    dialog.on_worker_failed("Validation failed", "boom")

    assert dialog.worker is None
    assert dialog.stack.currentWidget() is dialog.config_page
    assert "boom" in dialog.config_page.status_label.text()
    assert "#c0392b" in dialog.config_page.status_label.styleSheet()


def test_the_cancel_button_comes_back_for_the_next_run(dialog, tmp_path):
    "Winding down refuses a second click; the next run is live again."

    loaded_dialog(dialog, tmp_path)
    dialog.cancel_validation()
    # the stage may still be winding down: the page says so and refuses
    # a second click instead of pretending to be idle
    assert dialog.progress_page.cancel_button.isEnabled() is False
    assert dialog.progress_page.message_label.text() == CANCEL_MESSAGE
    # this is the state start_validation() builds on: a fresh page
    dialog.progress_page.reset()
    assert dialog.progress_page.cancel_button.isEnabled() is True
    assert dialog.progress_page.message_label.text() == ""
    assert dialog.progress_page.log.toPlainText() == ""


def test_the_next_run_starts_from_scratch(dialog, tmp_path):
    "Nothing of the dropped run feeds the next one."

    _worker, _staged, _source, staging = loaded_dialog(dialog, tmp_path)
    dialog.cancel_validation()

    # the form is filled as usual and the run starts on a clean state
    dialog.records = []
    dialog._refresh_preview()
    assert dialog.preview_counts(0, dialog.config_page.collect_config()) == {
        "originals": 0,
        "augmented": 0,
        "judged": 0,
        "exported": 0,
    }
    # a finished staging folder of the dropped run is never reused
    assert dialog.staging_root == staging
    assert dialog.previous_staging_roots == []
