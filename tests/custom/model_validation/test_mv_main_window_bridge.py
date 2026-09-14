"""Main window bridge: the gates of a jump and the sibling json.

The main window is replaced by a stub widget; these tests never import
ui.dialog (the wiring of the validation window is a workstream of its
own) and they never open a real labeling window. Pinned here is the
contract of a jump: which record is refused and with which line of
status, where the sibling json lands, and which calls the main window
really receives.
"""

import json
import os
import os.path as osp
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PyQt6 import QtTest
from PyQt6 import QtWidgets
from PyQt6.QtTest import QSignalSpy

from anylabeling.custom.model_validation import dataset
from anylabeling.custom.model_validation import main_window_bridge
from anylabeling.custom.model_validation import records as records_module
from anylabeling.custom.model_validation.main_window_bridge import (
    MainWindowBridge,
    sibling_label_path,
)

LABEL = {
    "version": "2.4.0",
    "flags": {},
    "shapes": [],
    "imagePath": "a.png",
    "imageData": None,
    "imageHeight": 4,
    "imageWidth": 4,
}


class StubMainWindow(QtWidgets.QWidget):
    "Stand-in for LabelingWidget, recording the calls it receives."

    def __init__(
        self,
        output_dir: str = "",
        may_continue: bool = True,
        minimized: bool = False,
    ):
        super().__init__()
        self.output_dir = output_dir
        self.continue_answer = may_continue
        self.minimized = minimized
        self.loaded = []
        self.asked_may_continue = 0
        # every call that would lift the window or take the keyboard:
        # a jump may not make any of them (see the activation tests)
        self.activation = []

    def load_file(self, filename):
        self.loaded.append(filename)
        return True

    def may_continue(self):
        self.asked_may_continue += 1
        return self.continue_answer

    def isMinimized(self) -> bool:  # noqa: N802
        return bool(self.minimized)

    def showNormal(self):
        self.activation.append("showNormal")
        self.minimized = False

    def raise_(self):
        self.activation.append("raise_")

    def activateWindow(self):
        self.activation.append("activateWindow")


class PlainStub:
    "A main window without window(), only the widget protocol."

    def __init__(self):
        self.output_dir = ""
        self.loaded = []

    def load_file(self, filename):
        self.loaded.append(filename)
        return True

    def may_continue(self):
        return True


@pytest.fixture(scope="module")
def qt_app():
    "Create the offscreen application the bridge needs."

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


@pytest.fixture
def window(qt_app):
    "Create a stub main window and close it after the test."

    widget = StubMainWindow()
    try:
        yield widget
    finally:
        widget.close()
        widget.deleteLater()


@pytest.fixture
def bridge(window):
    "Create the bridge under test and detach it after the test."

    item = MainWindowBridge(window)
    try:
        yield item
    finally:
        item.detach()


def make_record(scratch, name="a.png", raw=None):
    "Create a staging picture and its label, answering the record."

    root = str(scratch)
    image = osp.join(
        root, dataset.ORIGINAL_DIRNAME, dataset.IMAGES_DIRNAME, name
    )
    label_path = osp.join(
        root,
        dataset.ORIGINAL_DIRNAME,
        dataset.LABELS_DIRNAME,
        osp.splitext(name)[0] + ".json",
    )
    os.makedirs(osp.dirname(image), exist_ok=True)
    os.makedirs(osp.dirname(label_path), exist_ok=True)
    with open(image, "wb") as handle:
        handle.write(b"\x89PNG\r\n")
    if raw is None:
        raw = json.dumps(LABEL)
    write_text(label_path, raw)
    return records_module.make_record(
        records_module.KIND_ORIGINAL, name, image, label_path
    )


def read_text(path):
    "Read a text file as utf-8."

    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def write_text(path, text):
    "Write a text file as utf-8."

    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def save_like_main_window(sibling, payload):
    "Replace the sibling the way the upstream writer does."

    directory = osp.dirname(sibling)
    handle, temporary = tempfile.mkstemp(
        prefix=".xal_", suffix=".tmp", dir=directory
    )
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        json.dump(payload, stream)
    os.replace(temporary, sibling)


def hard_links_supported(directory):
    "Return True when the file system of a directory makes hard links."

    source = osp.join(directory, "probe-source")
    target = osp.join(directory, "probe-link")
    write_text(source, "x")
    try:
        os.link(source, target)
    except OSError:
        return False
    return True


def answer_question(monkeypatch, answer):
    "Make QMessageBox.question answer a fixed button."

    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "question",
        staticmethod(lambda *args, **kwargs: answer),
    )


def wait_for(predicate, timeout_ms=4000):
    "Process events until the predicate holds or the timeout expires."

    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if predicate():
            return True
        QtTest.QTest.qWait(10)
    return predicate()


def test_sibling_created_as_hard_link(bridge, tmp_path):
    "A jump puts the annotation of the record next to its picture."

    record = make_record(tmp_path)
    sibling = sibling_label_path(record.staging_image_path)
    assert not osp.exists(sibling)
    if not hard_links_supported(osp.dirname(sibling)):
        pytest.skip("the scratch file system does not support hard links")
    assert bridge.open_record(record) is True
    assert osp.isfile(sibling)
    assert os.path.samefile(sibling, record.staging_label_path)


def test_sibling_falls_back_to_a_copy(bridge, tmp_path, monkeypatch):
    "A file system without links still gets the sibling json."

    record = make_record(tmp_path)
    sibling = sibling_label_path(record.staging_image_path)

    def refuse(source, target, **kwargs):
        raise OSError("no hard links here")

    monkeypatch.setattr(main_window_bridge.os, "link", refuse)
    assert bridge.open_record(record) is True
    assert osp.isfile(sibling)
    assert read_text(sibling) == read_text(record.staging_label_path)
    assert not os.path.samefile(sibling, record.staging_label_path)


def test_an_older_sibling_is_refreshed_from_the_canonical(
    bridge, window, tmp_path
):
    "A sibling an earlier jump left behind never hides a newer label."

    record = make_record(tmp_path)
    sibling = sibling_label_path(record.staging_image_path)
    write_text(sibling, json.dumps(LABEL))
    newer = dict(LABEL)
    newer["shapes"] = [{"label": "edited-in-the-results-page"}]
    write_text(record.staging_label_path, json.dumps(newer))
    before = os.stat(sibling)
    assert bridge.open_record(record) is True
    after = os.stat(sibling)
    assert window.loaded == [record.staging_image_path]
    assert read_text(sibling) == read_text(record.staging_label_path)
    assert json.loads(read_text(sibling))["shapes"][0]["label"] == (
        "edited-in-the-results-page"
    )
    # the mirror is rewritten in place: no replace, so no new inode
    assert after.st_ino == before.st_ino


def test_an_identical_hard_linked_sibling_is_not_written(
    bridge, tmp_path
):
    "A mirror that already carries the label costs no write at all."

    record = make_record(tmp_path)
    sibling = sibling_label_path(record.staging_image_path)
    try:
        os.link(record.staging_label_path, sibling)
    except OSError:
        pytest.skip("the scratch file system does not support hard links")
    before = os.stat(sibling)
    assert bridge.open_record(record) is True
    after = os.stat(sibling)
    assert os.path.samefile(sibling, record.staging_label_path)
    assert after.st_ino == before.st_ino
    assert after.st_mtime_ns == before.st_mtime_ns


def test_a_pending_save_reaches_the_label_before_the_refresh(
    bridge, window, tmp_path
):
    "The debounce never lets a refresh eat the last save of the user."

    record = make_record(tmp_path)
    bridge.attach(str(tmp_path), [record])
    sibling = sibling_label_path(record.staging_image_path)
    saved = dict(LABEL)
    saved["shapes"] = [{"label": "saved-in-the-main-window"}]
    write_text(sibling, json.dumps(saved))
    bridge.sync.note_sibling(record)
    bridge.sync.queue_path(sibling)
    assert bridge.open_record(record) is True
    assert window.loaded == [record.staging_image_path]
    assert record.edited is True
    stored = json.loads(read_text(record.staging_label_path))
    assert stored["shapes"][0]["label"] == "saved-in-the-main-window"
    assert read_text(sibling) == read_text(record.staging_label_path)


def test_open_record_without_main_window_is_refused(tmp_path):
    "The standalone run refuses a jump instead of raising."

    record = make_record(tmp_path)
    item = MainWindowBridge(None)
    spy = QSignalSpy(item.status_message)
    assert item.open_record(record) is False
    assert len(spy) == 1
    assert "主窗口" in spy[0][0]


def test_open_record_without_record_is_refused(bridge, window):
    "No record, no jump, one line of status."

    spy = QSignalSpy(bridge.status_message)
    assert bridge.open_record(None) is False
    assert window.loaded == []
    assert len(spy) == 1


def test_open_record_without_image_is_refused(bridge, window, tmp_path):
    "A record whose staging picture is gone is refused."

    record = make_record(tmp_path)
    record.staging_image_path = osp.join(str(tmp_path), "gone.png")
    spy = QSignalSpy(bridge.status_message)
    assert bridge.open_record(record) is False
    assert window.loaded == []
    assert len(spy) == 1


def test_open_record_without_label_is_refused(bridge, window, tmp_path):
    "A record without a label path (a SKIPPED image) is refused."

    record = make_record(tmp_path)
    record.staging_label_path = ""
    spy = QSignalSpy(bridge.status_message)
    assert bridge.open_record(record) is False
    assert window.loaded == []
    assert len(spy) == 1


def test_open_record_without_label_file_is_refused(
    bridge, window, tmp_path
):
    "A label path that points nowhere is refused."

    record = make_record(tmp_path)
    record.staging_label_path = osp.join(str(tmp_path), "gone.json")
    spy = QSignalSpy(bridge.status_message)
    assert bridge.open_record(record) is False
    assert window.loaded == []
    assert len(spy) == 1


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        "[1, 2]",
        '{"version": "2.4.0"}',
        '{"version": "2.4.0", "shapes": []}',
    ],
)
def test_open_record_refuses_an_unusable_label(
    bridge, window, tmp_path, raw
):
    "A label the main window cannot load never reaches it."

    record = make_record(tmp_path, raw=raw)
    spy = QSignalSpy(bridge.status_message)
    assert bridge.open_record(record) is False
    assert window.loaded == []
    assert len(spy) == 1


def test_output_dir_warning_can_stop_the_jump(
    bridge, window, tmp_path, monkeypatch
):
    "A refusing answer to the output_dir warning stops the jump."

    window.output_dir = osp.join(str(tmp_path), "out")
    record = make_record(tmp_path)
    answer_question(monkeypatch, QtWidgets.QMessageBox.StandardButton.No)
    spy = QSignalSpy(bridge.status_message)
    assert bridge.open_record(record) is False
    assert window.loaded == []
    assert not osp.exists(sibling_label_path(record.staging_image_path))
    assert len(spy) == 1


def test_output_dir_warning_can_let_the_jump_through(
    bridge, window, tmp_path, monkeypatch
):
    "A confirming answer keeps the jump, as the plan requires."

    window.output_dir = osp.join(str(tmp_path), "out")
    record = make_record(tmp_path)
    answer_question(monkeypatch, QtWidgets.QMessageBox.StandardButton.Yes)
    assert bridge.open_record(record) is True
    assert window.loaded == [record.staging_image_path]


def test_unsaved_annotations_stop_the_jump(qt_app, tmp_path):
    "The gate of may_continue stands before the sibling is written."

    widget = StubMainWindow(may_continue=False)
    item = MainWindowBridge(widget)
    record = make_record(tmp_path)
    spy = QSignalSpy(item.status_message)
    try:
        assert item.open_record(record) is False
        assert widget.loaded == []
        assert not osp.exists(
            sibling_label_path(record.staging_image_path)
        )
        assert len(spy) == 1
    finally:
        item.detach()
        widget.close()
        widget.deleteLater()


def test_open_record_loads_the_image_without_lifting_the_window(
    bridge, window, tmp_path
):
    "A passing jump loads the staging picture and lifts nothing."

    record = make_record(tmp_path)
    spy = QSignalSpy(bridge.status_message)
    assert bridge.open_record(record) is True
    assert window.loaded == [record.staging_image_path]
    # the jump switches the current file of the main window alone: no
    # raise and no activation, so the keyboard stays where the user
    # left it and the follow never pulls the focus out of the
    # validation window
    assert window.activation == []
    assert window.minimized is False
    assert window.asked_may_continue == 1
    assert len(spy) == 1
    assert "打开" in spy[0][0]


def test_a_minimized_main_window_stays_in_the_task_bar(qt_app, tmp_path):
    "A minimized main window is not brought back by a jump either."

    widget = StubMainWindow(minimized=True)
    item = MainWindowBridge(widget)
    record = make_record(tmp_path)
    try:
        assert item.open_record(record) is True
        assert widget.loaded == [record.staging_image_path]
        # even a window in the task bar is left there: bringing it
        # back would mean taking the keyboard away from the window
        # the user is working in, which is exactly the bug this
        # contract pins
        assert widget.activation == []
        assert widget.minimized is True
    finally:
        item.detach()
        widget.close()
        widget.deleteLater()


def test_open_record_works_without_a_window_method(qt_app, tmp_path):
    "A main window without window() is still loaded."

    stub = PlainStub()
    item = MainWindowBridge(stub)
    record = make_record(tmp_path)
    try:
        assert item.open_record(record) is True
        assert stub.loaded == [record.staging_image_path]
    finally:
        item.detach()


def test_saved_sibling_reaches_the_bridge_signal(
    bridge, window, tmp_path
):
    "The save of the main window is what the results page hears about."

    record = make_record(tmp_path)
    bridge.attach(str(tmp_path), [record])
    assert bridge.open_record(record) is True
    spy = QSignalSpy(bridge.record_changed)
    sibling = sibling_label_path(record.staging_image_path)
    payload = dict(LABEL)
    payload["shapes"] = [{"label": "renamed"}]
    save_like_main_window(sibling, payload)
    if not wait_for(lambda: len(spy) > 0):
        pytest.skip("QFileSystemWatcher delivered no change event")
    assert spy[0] == [record.record_id]
    assert record.edited is True
    written = json.loads(read_text(record.staging_label_path))
    assert written["shapes"] == [{"label": "renamed"}]


def test_sibling_of_a_record_without_label_is_never_created(
    bridge, window, tmp_path
):
    "The label gate stands before the sibling, so no file appears."

    record = make_record(tmp_path)
    sibling = sibling_label_path(record.staging_image_path)
    write_text(record.staging_label_path, "not json")
    assert bridge.open_record(record) is False
    assert window.loaded == []
    assert not osp.exists(sibling)
