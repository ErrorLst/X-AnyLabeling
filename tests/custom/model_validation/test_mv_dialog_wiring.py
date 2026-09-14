"""Wiring of the validation window to the main window and the scanner.

The entries added to the window are driven here through the very slots
their signals reach:

* the automatic follow of the record the results page shows, with a
  stub main window in place of the real one - no test starts the
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
        # the ordinary state of this tool: the user edits in the main
        # window, which stays dirty until it is saved
        self.dirty = False
        # the modal gate of the old revision, kept as a counter: a
        # follow never asks it (see the no-modal tests below)
        self.requests = 0
        # every call that would lift the main window or take the
        # keyboard: a follow may not make any of them
        self.activation = []

    def load_file(self, path: str) -> bool:
        self.loaded.append(str(path))
        return True

    def may_continue(self) -> bool:
        self.requests += 1
        return True

    def isMinimized(self) -> bool:  # noqa: N802
        return False

    def showNormal(self) -> None:
        self.activation.append("showNormal")

    def raise_(self) -> None:
        self.activation.append("raise_")

    def activateWindow(self) -> None:
        self.activation.append("activateWindow")


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


def show_dialog(dialog) -> None:
    "Show a window offscreen: the follow only runs on a visible page."

    dialog.show()
    QtWidgets.QApplication.processEvents()


def count_questions(monkeypatch) -> list:
    "Record every message box a follow would open, and answer No."

    asked = []

    def question(*args, **kwargs):
        asked.append(args)
        return QtWidgets.QMessageBox.StandardButton.No

    monkeypatch.setattr(
        QtWidgets.QMessageBox, "question", staticmethod(question)
    )
    return asked


def follow_spy(dialog, monkeypatch) -> list:
    "Collect every record the follow hands to the bridge."

    opened = []
    real = dialog.bridge.open_record
    monkeypatch.setattr(
        dialog.bridge,
        "open_record",
        lambda item: opened.append(item) or real(item),
    )
    return opened


def silence_scheduler(dialog, monkeypatch) -> list:
    "Replace the debounced schedule() of the window with a recorder."

    calls = []

    def schedule(directory, token, delay_ms=0):
        calls.append((directory, token, delay_ms))

    monkeypatch.setattr(dialog.scan_scheduler, "schedule", schedule)
    return calls


def monkeypatch_focus(dialog, calls: list) -> None:
    "Record the order of the two focus calls of the window."

    monkeypatch_activate(dialog, lambda: calls.append("activateWindow"))
    page = dialog.results_page
    real_focus = page.focus_results

    def focus_results() -> None:
        calls.append("focus_results")
        real_focus()

    page.focus_results = focus_results


def monkeypatch_activate(dialog, activate) -> None:
    "Replace the activation of the window, keeping Qt out of the test."

    dialog.activateWindow = activate


def monkeypatch_minimized(dialog, minimized: bool) -> None:
    "Report the window as minimized, as a window manager would."

    dialog.isMinimized = lambda: bool(minimized)


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

        # the follow of the results page asks for a jump the window
        # refuses: the line has to land where the user is looking
        dialog._open_current_in_main_window()

        text = dialog.results_page.status_label.text()
        assert "主窗口" in text
        assert text == dialog.config_page.status_label.text()
    finally:
        dialog.close()
        keep_alive(dialog)


# ------------------------------------------------------- automatic follow
def test_a_finished_run_opens_the_record_in_the_main_window(qt_app, tmp_path):
    "The record on screen is loaded in the main window after a run."

    root = staging_root(str(tmp_path))
    record = staged_record(root, "a.png")
    window = StubMainWindow()
    dialog = ModelValidationDialog(window)
    show_dialog(dialog)
    try:
        finish_run(dialog, root, [record])
        assert dialog.results_page.displayed_record() is record
        assert wait_for(lambda: window.loaded == [record.staging_image_path])
        assert window.loaded == [record.staging_image_path]
        # the jump is a follow: it never asks the user anything
        assert window.requests == 0
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
        # the window is hidden and there is no window to follow: the
        # status line still carries the deferred warning of the first
        # screen, no follow has said anything
        assert dialog.config_page.status_label.text() == before
        # asked by hand, the refusal is explained instead of raised
        dialog._open_current_in_main_window()
        assert "主窗口" in dialog.config_page.status_label.text()
    finally:
        dialog.close()
        keep_alive(dialog)


def test_a_standalone_start_says_once_that_it_cannot_follow(
    qt_app, tmp_path
):
    "Without a main window the follow writes one line, not one per switch."

    root = staging_root(str(tmp_path))
    records = [staged_record(root, "a.png"), staged_record(root, "b.png")]
    dialog = ModelValidationDialog()
    show_dialog(dialog)
    try:
        finish_run(dialog, root, records)
        assert wait_for(
            lambda: "主窗口" in dialog.config_page.status_label.text()
        )
        # the line of the follow is written on both status lines
        assert (
            dialog.results_page.status_label.text()
            == dialog.config_page.status_label.text()
        )

        # and the next switch keeps quiet instead of saying it again
        dialog.config_page.set_status("安静")
        dialog.results_page.set_summary("安静")
        page = dialog.results_page
        page.table.setCurrentItem(page.table.topLevelItem(1))
        QtWidgets.QApplication.processEvents()
        QtTest.QTest.qWait(300)

        assert dialog.config_page.status_label.text() == "安静"
        assert dialog.results_page.status_label.text() == "安静"
    finally:
        dialog.close()
        keep_alive(dialog)


# ---------------------------------------------------- filter switch
def test_a_filter_switch_opens_no_message_box(
    qt_app, tmp_path, monkeypatch
):
    "A switch of the filter follows a record and asks the user nothing."

    root = staging_root(str(tmp_path))
    records = [
        staged_record(root, "a.png", records_module.NG),
        staged_record(root, "b.png", records_module.OK),
    ]
    window = StubMainWindow()
    asked = count_questions(monkeypatch)
    dialog = ModelValidationDialog(window)
    show_dialog(dialog)
    try:
        finish_run(dialog, root, records)
        assert wait_for(
            lambda: window.loaded == [records[0].staging_image_path]
        )

        # the filter switch lands on another record, so the follow opens
        # it: the bug was the modal box (and the frozen window) this
        # switch used to trigger
        page = dialog.results_page
        page.filter_combo.setCurrentIndex(
            page.filter_combo.findData(records_module.OK)
        )
        QtWidgets.QApplication.processEvents()

        assert wait_for(
            lambda: window.loaded[-1] == records[1].staging_image_path
        )
        assert asked == []
        assert window.requests == 0
        # the follow moves the current file alone: no raise, no focus
        assert window.activation == []
        assert "打开" in dialog.results_page.status_label.text()
    finally:
        dialog.close()
        window.close()
        keep_alive(dialog, window)


def test_a_dirty_main_window_skips_the_follow_with_a_status_line(
    qt_app, tmp_path, monkeypatch
):
    "Unsaved annotations skip the jump instead of opening a box."

    root = staging_root(str(tmp_path))
    records = [
        staged_record(root, "a.png", records_module.NG),
        staged_record(root, "b.png", records_module.OK),
    ]
    window = StubMainWindow()
    window.dirty = True
    asked = count_questions(monkeypatch)
    dialog = ModelValidationDialog(window)
    show_dialog(dialog)
    try:
        finish_run(dialog, root, records)
        page = dialog.results_page
        page.filter_combo.setCurrentIndex(
            page.filter_combo.findData(records_module.OK)
        )
        QtWidgets.QApplication.processEvents()
        QtTest.QTest.qWait(400)

        # nothing was asked, nothing was loaded and the annotation the
        # user has not saved is still sitting in the main window
        assert asked == []
        assert window.requests == 0
        assert window.loaded == []
        assert "未保存" in dialog.results_page.status_label.text()
        assert (
            dialog.results_page.status_label.text()
            == dialog.config_page.status_label.text()
        )
    finally:
        dialog.close()
        window.close()
        keep_alive(dialog, window)


def test_an_output_dir_skips_the_follow_with_a_status_line(
    qt_app, tmp_path, monkeypatch
):
    "An output_dir skips the jump instead of opening a box."

    root = staging_root(str(tmp_path))
    records = [
        staged_record(root, "a.png", records_module.NG),
        staged_record(root, "b.png", records_module.OK),
    ]
    window = StubMainWindow()
    window.output_dir = osp.join(str(tmp_path), "out")
    asked = count_questions(monkeypatch)
    dialog = ModelValidationDialog(window)
    show_dialog(dialog)
    try:
        finish_run(dialog, root, records)
        page = dialog.results_page
        page.filter_combo.setCurrentIndex(
            page.filter_combo.findData(records_module.OK)
        )
        QtWidgets.QApplication.processEvents()
        QtTest.QTest.qWait(400)

        assert asked == []
        assert window.loaded == []
        assert "输出目录" in dialog.results_page.status_label.text()
    finally:
        dialog.close()
        window.close()
        keep_alive(dialog, window)


def test_a_burst_of_filter_switches_rebuilds_the_table_once(
    qt_app, tmp_path, monkeypatch
):
    "A row of filter switches costs one rebuild, on the last filter."

    root = staging_root(str(tmp_path))
    records = [
        staged_record(root, "a.png", records_module.NG),
        staged_record(root, "b.png", records_module.OK),
        staged_record(root, "c.png", records_module.OK),
    ]
    dialog = ModelValidationDialog()
    try:
        dialog.results_page.set_context(list(CLASSES), root)
        dialog.results_page.set_records(records)
        page = dialog.results_page
        rebuilds = []
        real_refresh = page.refresh
        monkeypatch.setattr(
            page,
            "refresh",
            lambda: rebuilds.append(True) or real_refresh(),
        )
        combo = page.filter_combo

        combo.setCurrentIndex(combo.findData(records_module.OK))
        combo.setCurrentIndex(0)
        combo.setCurrentIndex(combo.findData(records_module.OK))
        # the switch itself rebuilds nothing: the popup of the list is
        # still closing, which is what used to freeze the window
        assert rebuilds == []

        QtWidgets.QApplication.processEvents()
        # one rebuild for the whole burst, and it is the one of the
        # filter the list was left on
        assert rebuilds == [True]
        assert [
            page.record_at(row).record_id
            for row in range(page.table.rowCount())
        ] == [records[1].record_id, records[2].record_id]

        # a new list rebuilds right away and cancels the switch that was
        # still pending: one rebuild again, never two
        rebuilds.clear()
        combo.setCurrentIndex(0)
        page.set_records(records)
        assert rebuilds == [True]
        QtWidgets.QApplication.processEvents()
        assert rebuilds == [True]
        assert page.table.rowCount() == 3
    finally:
        dialog.close()
        keep_alive(dialog)


# -------------------------------------------------- follow the record
def test_a_switch_of_the_record_follows_it_after_the_debounce(
    qt_app, tmp_path, monkeypatch
):
    "The record the page moved to is opened once, after the debounce."

    root = staging_root(str(tmp_path))
    records = [staged_record(root, "a.png"), staged_record(root, "b.png")]
    window = StubMainWindow()
    dialog = ModelValidationDialog(window)
    show_dialog(dialog)
    try:
        opened = follow_spy(dialog, monkeypatch)
        finish_run(dialog, root, records)
        assert wait_for(lambda: opened == [records[0]])
        assert window.loaded == [records[0].staging_image_path]

        # a click on the other row is a switch of the record, and it is
        # what the follow opens
        page = dialog.results_page
        page.table.setCurrentItem(page.table.topLevelItem(1))
        QtWidgets.QApplication.processEvents()

        assert wait_for(lambda: len(opened) == 2)
        assert opened == records
        assert window.loaded == [
            records[0].staging_image_path,
            records[1].staging_image_path,
        ]
        # the follow is over: the record that settled is not opened again
        QtTest.QTest.qWait(300)
        assert opened == records
    finally:
        dialog.close()
        window.close()
        keep_alive(dialog, window)


def test_a_burst_of_switches_opens_only_the_last_record(
    qt_app, tmp_path, monkeypatch
):
    "A / D held down or a quick series of clicks costs one jump."

    root = staging_root(str(tmp_path))
    records = [
        staged_record(root, name) for name in ("a.png", "b.png", "c.png")
    ]
    window = StubMainWindow()
    dialog = ModelValidationDialog(window)
    show_dialog(dialog)
    try:
        opened = follow_spy(dialog, monkeypatch)
        finish_run(dialog, root, records)

        # three switches inside one debounce window: the first one of the
        # run, then two more rows without waiting for the timer
        page = dialog.results_page
        for row in (1, 2):
            page.table.setCurrentItem(page.table.topLevelItem(row))
        QtWidgets.QApplication.processEvents()

        assert wait_for(lambda: len(opened) == 1)
        assert opened == [records[2]]
        QtTest.QTest.qWait(300)
        assert opened == [records[2]]
        assert window.loaded == [records[2].staging_image_path]
    finally:
        dialog.close()
        window.close()
        keep_alive(dialog, window)


def test_the_same_record_is_never_opened_twice(qt_app, tmp_path, monkeypatch):
    "A repeated announcement of the record on screen costs no second jump."

    root = staging_root(str(tmp_path))
    record = staged_record(root, "a.png")
    window = StubMainWindow()
    dialog = ModelValidationDialog(window)
    show_dialog(dialog)
    try:
        opened = follow_spy(dialog, monkeypatch)
        finish_run(dialog, root, [record])
        assert wait_for(lambda: opened == [record])

        # the very same record is announced again: the rebuild of a list,
        # a filter that shows it once more, the save of the main window
        for _ in range(3):
            dialog.results_page.current_record_changed.emit(record.record_id)
        QtTest.QTest.qWait(300)

        assert opened == [record]
        # one jump, therefore no question at all about the unsaved
        # annotations: the record that was already opened costs nothing
        assert window.requests == 0
    finally:
        dialog.close()
        window.close()
        keep_alive(dialog, window)


def test_the_follow_only_runs_while_the_results_page_is_shown(
    qt_app, tmp_path, monkeypatch
):
    "The configuration page on screen follows nothing."

    root = staging_root(str(tmp_path))
    records = [staged_record(root, "a.png"), staged_record(root, "b.png")]
    window = StubMainWindow()
    dialog = ModelValidationDialog(window)
    show_dialog(dialog)
    try:
        opened = follow_spy(dialog, monkeypatch)
        dialog.staging_root = root
        dialog.classes = list(CLASSES)
        dialog.records = list(records)
        dialog.results_page.set_context(dialog.classes, root)
        dialog.stack.setCurrentWidget(dialog.config_page)
        dialog.results_page.set_records(records)

        QtTest.QTest.qWait(300)

        assert dialog.stack.currentWidget() is dialog.config_page
        assert opened == []
        assert window.loaded == []

        # the results page is on screen again: there the switch follows
        dialog.stack.setCurrentWidget(dialog.results_page)
        page = dialog.results_page
        page.table.setCurrentItem(page.table.topLevelItem(1))
        QtWidgets.QApplication.processEvents()

        assert wait_for(
            lambda: [item.record_id for item in opened]
            == [records[1].record_id]
        )
        assert window.loaded[-1] == records[1].staging_image_path
    finally:
        dialog.close()
        window.close()
        keep_alive(dialog, window)


# ------------------------------------------------------- focus of a jump
def test_a_follow_takes_the_focus_back_to_the_validation_window(
    qt_app, tmp_path, monkeypatch
):
    "The follow loads the record and leaves the keyboard in the window."

    root = staging_root(str(tmp_path))
    record = staged_record(root, "a.png")
    window = StubMainWindow()
    dialog = ModelValidationDialog(window)
    show_dialog(dialog)
    try:
        # the real page entry is kept: the focus has to land on the
        # list for real, not on a stub of it
        activations = []
        monkeypatch_focus(dialog, activations)
        finish_run(dialog, root, [record])
        assert wait_for(lambda: window.loaded == [record.staging_image_path])
        assert wait_for(lambda: len(activations) >= 2)
        QtTest.QTest.qWait(50)

        # the upstream load_file ends on canvas.setFocus(): the
        # keyboard is asked back on the next turn, and the main
        # window is neither raised nor activated by the jump
        assert activations == ["activateWindow", "focus_results"]
        assert window.activation == []
        assert dialog.results_page.table.hasFocus() is True
    finally:
        dialog.close()
        window.close()
        keep_alive(dialog, window)


def test_a_refused_follow_never_takes_the_focus(qt_app, tmp_path):
    "A rejected jump loaded no file, so it steals no focus."

    root = staging_root(str(tmp_path))
    record = staged_record(root, "a.png")
    window = StubMainWindow()
    dialog = ModelValidationDialog(window)
    show_dialog(dialog)
    try:
        finish_run(dialog, root, [record])
        assert dialog.results_page.displayed_record() is record
        calls = []
        dialog.bridge.open_record = (
            lambda item: calls.append(item) or False
        )
        activations = []
        monkeypatch_focus(dialog, activations)
        dialog.follow_timer.stop()
        dialog._open_current_in_main_window()
        QtTest.QTest.qWait(50)

        # the jump was asked for and refused: no file was loaded, so
        # the window that refused it never takes the focus either
        assert calls == [record]
        assert window.loaded == []
        assert activations == []
        assert window.activation == []
    finally:
        dialog.close()
        window.close()
        keep_alive(dialog, window)


def test_the_focus_comes_back_only_to_a_visible_window(
    qt_app, monkeypatch
):
    "A hidden or minimized window is left exactly as it is."

    dialog = ModelValidationDialog()
    try:
        activations = []
        monkeypatch_focus(dialog, activations)
        # a window nobody showed: the queued call must not raise the
        # window the user never opened
        assert dialog.isVisible() is False
        dialog._restore_validation_focus()
        assert activations == []

        show_dialog(dialog)
        dialog._restore_validation_focus()
        assert activations == ["activateWindow", "focus_results"]

        monkeypatch_minimized(dialog, True)
        activations.clear()
        dialog._restore_validation_focus()
        assert activations == []
    finally:
        dialog.close()
        keep_alive(dialog)


# --------------------------------------------------------- watcher flow
def test_a_record_change_repaints_without_a_second_jump(
    qt_app, tmp_path, monkeypatch
):
    "record_changed reloads the record and never jumps again."

    root = staging_root(str(tmp_path))
    record = staged_record(root, "a.png")
    window = StubMainWindow()
    dialog = ModelValidationDialog(window)
    show_dialog(dialog)
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
