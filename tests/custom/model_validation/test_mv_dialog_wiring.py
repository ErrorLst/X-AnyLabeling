"""Wiring of the validation window to the main window and the scanner.

The entries added to the window are driven here through the very slots
their signals reach:

* the E key / open_in_main_requested jump to the labeling window, with
  a stub main window in place of the real one - no test starts the
  application;
* the record_changed answer of the staging watcher, which may only
  repaint a row and never jump again;
* the attach / detach lifecycle of the watcher and the debounced scan
  of the source directory, which must keep the folder walk out of the
  Qt slot that watches the path line edit.

The window is never shown: every widget is exercised through its public
state, offscreen.
"""

import os
import os.path as osp
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PyQt6 import QtCore, QtTest, QtWidgets

from anylabeling.custom.model_validation import dataset
from anylabeling.custom.model_validation import records as records_module
from anylabeling.custom.model_validation.ui import dialog as dialog_module
from anylabeling.custom.model_validation.ui.dialog import (
    ModelValidationDialog,
)

CLASSES = ["car"]

# The widgets a test closes are kept referenced until the session ends:
# a window whose Python wrapper is collected while a deferred delete of
# a child is still queued crashes the Qt event loop of the next test.
_ALIVE: list = []


def keep_alive(*widgets) -> None:
    "Hold references to closed widgets for the rest of the session."

    _ALIVE.extend(widgets)


# every symbol the results page dropped with its in place editing
RESIDUE = (
    "edit_requested",
    "shape_moved",
    "on_edit_shape",
    "on_shape_moved",
    "update_shape",
    "apply_edit_result",
    "edit_mode",
)


@pytest.fixture(scope="module")
def qt_app():
    "Create the offscreen application the signals need."

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


@pytest.fixture
def dialog(qt_app):
    "Create a window and close it when the test is over."

    window = ModelValidationDialog()
    try:
        yield window
    finally:
        window.close()


def wait_for(predicate, timeout_ms: int = 5000) -> bool:
    "Process events until the predicate holds or the timeout expires."

    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if predicate():
            return True
        QtTest.QTest.qWait(10)
    return predicate()


def write_image(path: str) -> None:
    "Write a tiny png file."

    import cv2
    import numpy as np

    os.makedirs(osp.dirname(path), exist_ok=True)
    image = np.zeros((24, 32, 3), dtype=np.uint8)
    ok, buffer = cv2.imencode(".png", image)
    assert ok
    buffer.tofile(path)


def label_payload(name: str) -> dict:
    "Return the xlabel document of one staged pair."

    return {
        "version": "3.0.0",
        "flags": {},
        "shapes": [],
        "imagePath": osp.basename(name),
        "imageData": None,
        "imageHeight": 24,
        "imageWidth": 32,
    }


def staging_root(scratch: str) -> str:
    "Create the staging layout the tool expects."

    root = osp.join(str(scratch), dataset.STAGING_PREFIX + "wiring")
    for folder in (dataset.ORIGINAL_DIRNAME, dataset.AUGMENTED_DIRNAME):
        for sub in (dataset.IMAGES_DIRNAME, dataset.LABELS_DIRNAME):
            os.makedirs(osp.join(root, folder, sub), exist_ok=True)
    return root


def staged_record(root: str, name: str, verdict: str = records_module.NG):
    "Create one staged original with its picture, label and verdict."

    paths = dataset.staging_paths(root, records_module.KIND_ORIGINAL, name)
    write_image(paths["image"])
    dataset.write_json(paths["label"], label_payload(name))
    record = records_module.make_record(
        records_module.KIND_ORIGINAL, name, paths["image"], paths["label"]
    )
    record.verdict = verdict
    return record


class StubMainWindow(QtWidgets.QWidget):
    "The labeling window the bridge hands one record to."

    def __init__(self) -> None:
        super().__init__()
        self.loaded = []
        self.output_dir = ""
        self.requests = 0

    def load_file(self, path: str) -> bool:
        self.loaded.append(str(path))
        return True

    def may_continue(self) -> bool:
        self.requests += 1
        return True


class StubWorker(QtCore.QObject):
    "Stand-in for the validation worker, so that no run ever starts."

    progress = QtCore.pyqtSignal(str, int, int, str)
    stage_finished = QtCore.pyqtSignal(str, int)
    warnings_ready = QtCore.pyqtSignal(list)
    dataset_ready = QtCore.pyqtSignal(str, list, dict)
    failed = QtCore.pyqtSignal(str, str)
    cancelled = QtCore.pyqtSignal()
    finished_ok = QtCore.pyqtSignal()

    def __init__(self, config, classes, staging, parent=None) -> None:
        super().__init__(parent)
        self.staging = staging
        self.records = []
        self.started = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        pass

    def wait(self, timeout_ms: int = 0) -> bool:
        return True

    def isRunning(self) -> bool:  # noqa: N802
        return False


def finish_run(dialog, root: str, records: list) -> None:
    "Drive the window into the state a finished run leaves behind."

    dialog.staging_root = root
    dialog.classes = list(CLASSES)
    dialog.records = list(records)
    dialog.worker = SimpleNamespace(records=list(records))
    dialog.on_worker_finished()


def silence_scheduler(dialog, monkeypatch) -> list:
    "Replace the debounced schedule() of the window with a recorder."

    calls = []

    def schedule(directory, token, delay_ms=0):
        calls.append((directory, token, delay_ms))

    monkeypatch.setattr(dialog.scan_scheduler, "schedule", schedule)
    return calls


# ------------------------------------------------------------- residue
def test_the_window_keeps_no_edit_residue(qt_app):
    "The window no longer connects to the signals the page dropped."

    with open(dialog_module.__file__, "r", encoding="utf-8") as handle:
        source = handle.read()
    for gone in RESIDUE:
        assert gone not in source, gone
    dialog = ModelValidationDialog()
    try:
        page = dialog.results_page
        assert not hasattr(page, "edit_requested")
        assert not hasattr(page, "shape_moved")
    finally:
        dialog.close()


# --------------------------------------------------------- status lines
def test_a_status_note_reaches_the_results_page(qt_app, tmp_path):
    "A refusal on screen while the results page is up is visible there."

    root = staging_root(str(tmp_path))
    record = staged_record(root, "a.png")
    dialog = ModelValidationDialog()
    try:
        finish_run(dialog, root, [record])
        assert dialog.stack.currentWidget() is dialog.results_page
        assert dialog.bridge.main_window is None

        # the E key of the results page asks for a jump the window
        # refuses: the line has to land where the user is looking
        dialog._open_current_in_main_window()

        text = dialog.results_page.status_label.text()
        assert "主窗口" in text
        assert text == dialog.config_page.status_label.text()
    finally:
        dialog.close()
        keep_alive(dialog)


# ------------------------------------------------------- automatic jump
def test_a_finished_run_opens_the_record_in_the_main_window(qt_app, tmp_path):
    "The record on screen is loaded in the main window after a run."

    root = staging_root(str(tmp_path))
    record = staged_record(root, "a.png")
    window = StubMainWindow()
    dialog = ModelValidationDialog(window)
    try:
        finish_run(dialog, root, [record])
        assert dialog.results_page.displayed_record() is record
        assert wait_for(lambda: window.loaded == [record.staging_image_path])
        assert window.loaded == [record.staging_image_path]
        assert window.requests == 1
    finally:
        dialog.close()
        window.close()
        keep_alive(dialog, window)


def test_without_a_main_window_the_run_schedules_no_jump(qt_app, tmp_path):
    "A standalone start never jumps, and the entry only writes a line."

    root = staging_root(str(tmp_path))
    record = staged_record(root, "a.png")
    dialog = ModelValidationDialog()
    try:
        assert dialog.bridge.main_window is None
        before = dialog.config_page.status_label.text()
        finish_run(dialog, root, [record])
        QtTest.QTest.qWait(50)
        # nothing was scheduled: the status line still carries the
        # deferred warning of the first screen
        assert dialog.config_page.status_label.text() == before
        # asked by hand, the refusal is explained instead of raised
        dialog._open_current_in_main_window()
        assert "主窗口" in dialog.config_page.status_label.text()
    finally:
        dialog.close()
        keep_alive(dialog)


# ------------------------------------------------------------ E shortcat
def test_the_open_request_opens_the_displayed_record(
    qt_app, tmp_path, monkeypatch
):
    "open_in_main_requested reaches the very entry of the jump."

    root = staging_root(str(tmp_path))
    record = staged_record(root, "a.png")
    window = StubMainWindow()
    dialog = ModelValidationDialog(window)
    try:
        finish_run(dialog, root, [record])
        assert wait_for(lambda: window.loaded == [record.staging_image_path])
        opened = []
        real = dialog.bridge.open_record
        monkeypatch.setattr(
            dialog.bridge,
            "open_record",
            lambda item: opened.append(item) or real(item),
        )
        dialog.results_page.open_in_main_requested.emit()
        assert opened == [record]
        assert window.loaded[-1] == record.staging_image_path
    finally:
        dialog.close()
        window.close()
        keep_alive(dialog, window)


# --------------------------------------------------------- watcher flow
def test_a_record_change_repaints_without_a_second_jump(
    qt_app, tmp_path, monkeypatch
):
    "record_changed reloads the record and never jumps again."

    root = staging_root(str(tmp_path))
    record = staged_record(root, "a.png")
    window = StubMainWindow()
    dialog = ModelValidationDialog(window)
    try:
        finish_run(dialog, root, [record])
        assert wait_for(lambda: window.loaded == [record.staging_image_path])
        page = dialog.results_page
        reloaded = []
        real_reload = page.reload_record
        monkeypatch.setattr(
            page,
            "reload_record",
            lambda record_id: reloaded.append(record_id)
            or real_reload(record_id),
        )
        rebuilds = []
        monkeypatch.setattr(
            page, "refresh", lambda: rebuilds.append(True)
        )
        jumps = []
        real_open = dialog.bridge.open_record
        monkeypatch.setattr(
            dialog.bridge,
            "open_record",
            lambda item: jumps.append(item) or real_open(item),
        )
        rows = page.table.rowCount()

        dialog.bridge.record_changed.emit(record.record_id)

        assert reloaded == [record.record_id]
        assert rows == page.table.rowCount()
        assert rebuilds == []
        assert jumps == []
    finally:
        dialog.close()
        window.close()
        keep_alive(dialog, window)


# ------------------------------------------------- attach and detach
def test_the_watcher_follows_the_run_and_the_window(
    qt_app, tmp_path, monkeypatch
):
    "attach on a finished run, detach on a new run and on close."

    root = staging_root(str(tmp_path))
    record = staged_record(root, "a.png")
    dialog = ModelValidationDialog()
    try:
        assert dialog.bridge.sync.attached is False
        finish_run(dialog, root, [record])
        assert dialog.bridge.sync.attached is True

        # a new run detaches before it builds anything of its own
        source = osp.join(str(tmp_path), "source")
        os.makedirs(source, exist_ok=True)
        model = osp.join(str(tmp_path), "model.onnx")
        with open(model, "wb") as handle:
            handle.write(b"onnx")
        classes = osp.join(str(tmp_path), "classes.txt")
        with open(classes, "w", encoding="utf-8") as handle:
            handle.write("car\n")
        monkeypatch.setattr(dialog_module, "ValidationWorker", StubWorker)
        monkeypatch.setattr(
            dataset, "create_staging_root", lambda parent=None: root
        )
        page = dialog.config_page
        page.set_dataset(source)
        page.set_model(model)
        page.set_classes(classes, list(CLASSES))

        dialog.start_validation()

        assert dialog.bridge.sync.attached is False
        assert isinstance(dialog.worker, StubWorker)
        assert dialog.worker.started is True
    finally:
        dialog.close()
    assert dialog.bridge.sync.attached is False
    assert dialog.scan_scheduler._shutdown is True


# -------------------------------------------------------- source scan
def test_typing_a_path_asks_for_one_debounced_scan(dialog, monkeypatch):
    "Every keystroke moves the token on and leaves the walk to a thread."

    calls = silence_scheduler(dialog, monkeypatch)
    walks = []
    monkeypatch.setattr(
        dataset,
        "collect_pairs",
        lambda directory: walks.append(directory),
    )
    page = dialog.config_page

    page.set_dataset("/first")
    page.set_dataset("/second")

    assert calls == [
        ("/first", 1, dialog_module.SCAN_DEBOUNCE_MS),
        ("/second", 2, dialog_module.SCAN_DEBOUNCE_MS),
    ]
    assert dialog._scan_token == 2
    assert dialog._scan_pending is True
    assert page.preview_label.text() == dialog_module.SCAN_PENDING_TEXT
    assert walks == []


def test_a_matching_answer_feeds_the_cache_and_the_preview(
    dialog, monkeypatch
):
    "The answer of the newest request is cached and shown."

    silence_scheduler(dialog, monkeypatch)
    page = dialog.config_page
    page.set_dataset("/source")

    dialog._on_scan_ready(dialog._scan_token, "/source", 3)

    assert dialog.source_dataset_dir == "/source"
    assert dialog.source_pair_count == 3
    assert dialog._scan_pending is False
    assert page.preview_label.text().startswith("有效原图 3")


def test_a_stale_token_is_dropped(dialog, monkeypatch):
    "The answer of a request the window replaced is dropped."

    silence_scheduler(dialog, monkeypatch)
    page = dialog.config_page
    page.set_dataset("/wanted")
    stale = dialog._scan_token - 1

    dialog._on_scan_ready(stale, "/wanted", 7)

    assert dialog.source_dataset_dir == ""
    assert dialog.source_pair_count == 0
    assert dialog._scan_pending is True


def test_an_answer_of_another_directory_is_dropped(dialog, monkeypatch):
    "The answer of a request for another path is dropped."

    silence_scheduler(dialog, monkeypatch)
    page = dialog.config_page
    page.set_dataset("/now")

    dialog._on_scan_ready(dialog._scan_token, "/then", 7)

    assert dialog.source_dataset_dir == ""
    assert dialog.source_pair_count == 0
    assert dialog._scan_pending is True


def test_a_failed_scan_is_reported_and_counts_zero(dialog, monkeypatch):
    "A scan that failed leaves the count at 0 and explains itself."

    silence_scheduler(dialog, monkeypatch)
    page = dialog.config_page
    page.set_dataset("/broken")

    dialog._on_scan_failed(
        dialog._scan_token, "/broken", "扫描数据目录失败：boom"
    )

    assert dialog.source_pair_count == 0
    assert dialog._scan_pending is False
    assert "boom" in page.status_label.text()


def test_a_synchronous_answer_does_not_recurse(dialog, monkeypatch):
    "A path that is not a folder is answered inside schedule() itself."

    # without a delay the scheduler answers a path that is not a folder
    # in the calling stack (see DirectoryScanScheduler), which is the
    # reentrant case the pending flag has to survive
    monkeypatch.setattr(dialog_module, "SCAN_DEBOUNCE_MS", 0)
    seen = []
    page = dialog.config_page
    real = dialog.scan_scheduler.schedule

    def schedule(directory, token, delay_ms=0):
        seen.append((directory, token, delay_ms))
        return real(directory, token, delay_ms)

    monkeypatch.setattr(dialog.scan_scheduler, "schedule", schedule)

    page.set_dataset("/does-not-exist")

    # one request, and the answer of it was already handled inside it:
    # the flag is down and no second request was made from the slot
    assert seen == [("/does-not-exist", 1, 0)]
    assert dialog._scan_pending is False
    assert dialog.source_pair_count == 0
    assert page.preview_label.text().startswith("有效原图 0")


def test_the_pair_count_is_read_from_the_cache(dialog, monkeypatch):
    "Counting a folder never walks it outside the scheduler."

    walks = []
    monkeypatch.setattr(
        dataset,
        "collect_pairs",
        lambda directory: walks.append(directory),
    )

    assert dialog._count_source_pairs("/source") == 0
    dialog.source_dataset_dir = "/source"
    dialog.source_pair_count = 4
    assert dialog._count_source_pairs("/source") == 4
    assert dialog._count_source_pairs("/other") == 0
    assert walks == []
