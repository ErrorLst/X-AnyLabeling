"""Staging synchronization: what the main window saves comes back.

Both entries of the synchronization are driven here: queue_path() plus
flush_pending() for the cases whose timing would only make a test flaky,
and a real QFileSystemWatcher with a running event loop for the cases
only a delivered event can prove (the debounce, the path Qt takes off
its watch list after a save). A machine whose watcher delivers nothing
skips those cases with a reason instead of failing: the file system
events of an inotify-less sandbox are not a behaviour a test can pin.
"""

import json
import os
import os.path as osp
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PyQt6 import QtCore
from PyQt6 import QtTest
from PyQt6 import QtWidgets
from PyQt6.QtTest import QSignalSpy

from anylabeling.custom.model_validation import dataset
from anylabeling.custom.model_validation import records as records_module
from anylabeling.custom.model_validation.main_window_bridge import (
    StagingSync,
    sibling_label_path,
)

NO_EVENT_REASON = (
    "QFileSystemWatcher delivered no change event on this machine"
)


@pytest.fixture(scope="module")
def qt_app():
    "Create the offscreen application the watcher needs."

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


@pytest.fixture
def sync(qt_app):
    "Create the synchronization under test and stop it after the test."

    item = StagingSync()
    try:
        yield item
    finally:
        item.detach()


def staging_layout(scratch):
    "Create the staging folder layout the tool expects."

    root = osp.join(str(scratch), dataset.STAGING_PREFIX + "sync")
    for folder in (dataset.ORIGINAL_DIRNAME, dataset.AUGMENTED_DIRNAME):
        for sub in (dataset.IMAGES_DIRNAME, dataset.LABELS_DIRNAME):
            os.makedirs(osp.join(root, folder, sub), exist_ok=True)
    return root


def label_payload(name="a.png", label="first"):
    "Return a minimal xlabel document carrying one labelled box."

    return {
        "version": "2.4.0",
        "flags": {},
        "shapes": [
            {
                "label": label,
                "points": [[0, 0], [1, 1]],
                "shape_type": "rectangle",
                "flags": {},
            }
        ],
        "imagePath": name,
        "imageData": None,
        "imageHeight": 4,
        "imageWidth": 4,
    }


def make_record(root, name="a.png"):
    "Create a staging picture with its canonical label."

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
    write_text(label_path, json.dumps(label_payload(name, "original")))
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


def copy_sibling(record):
    "Give a record the sibling json the bridge would have created."

    sibling = sibling_label_path(record.staging_image_path)
    write_text(sibling, read_text(record.staging_label_path))
    return sibling


def save_like_main_window(path, payload):
    "Replace a label the way the upstream writer does."

    directory = osp.dirname(path)
    handle, temporary = tempfile.mkstemp(
        prefix=".xal_", suffix=".tmp", dir=directory
    )
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        json.dump(payload, stream)
    os.replace(temporary, path)


def stored_label(record):
    "Return the label written in the canonical json of a record."

    return json.loads(read_text(record.staging_label_path))


def wait_for(predicate, timeout_ms=4000):
    "Process events until the predicate holds or the timeout expires."

    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if predicate():
            return True
        QtTest.QTest.qWait(10)
    return predicate()


def test_a_saved_sibling_is_copied_back_to_the_canonical_label(
    sync, tmp_path
):
    "The save of the main window is what the export will read."

    root = staging_layout(tmp_path)
    record = make_record(root)
    sync.attach(root, [record])
    sibling = copy_sibling(record)
    sync.note_sibling(record)
    spy = QSignalSpy(sync.record_changed)
    write_text(sibling, json.dumps(label_payload("a.png", "renamed")))
    sync.queue_path(sibling)
    sync.flush_pending()
    assert record.edited is True
    assert len(spy) == 1
    assert spy[0] == [record.record_id]
    assert stored_label(record)["shapes"][0]["label"] == "renamed"


def test_the_write_back_does_not_report_itself_again(sync, tmp_path):
    "The canonical event our own rewrite feeds the watcher is ignored."

    root = staging_layout(tmp_path)
    record = make_record(root)
    sync.attach(root, [record])
    sibling = copy_sibling(record)
    sync.note_sibling(record)
    spy = QSignalSpy(sync.record_changed)
    write_text(sibling, json.dumps(label_payload("a.png", "renamed")))
    sync.queue_path(sibling)
    sync.flush_pending()
    assert len(spy) == 1
    sync.queue_path(record.staging_label_path)
    sync.flush_pending()
    assert len(spy) == 1


def test_a_canonical_change_is_reported_and_not_written(sync, tmp_path):
    "An edit made behind the tool is news, never a write back."

    root = staging_layout(tmp_path)
    record = make_record(root)
    sync.attach(root, [record])
    spy = QSignalSpy(sync.record_changed)
    save_like_main_window(
        record.staging_label_path, label_payload("a.png", "outside")
    )
    before = os.stat(record.staging_label_path)
    sync.queue_path(record.staging_label_path)
    sync.flush_pending()
    after = os.stat(record.staging_label_path)
    assert record.edited is True
    assert len(spy) == 1
    assert spy[0] == [record.record_id]
    assert after.st_mtime_ns == before.st_mtime_ns
    assert after.st_ino == before.st_ino
    assert stored_label(record)["shapes"][0]["label"] == "outside"


def test_a_broken_sibling_never_reaches_the_canonical_label(
    sync, tmp_path
):
    "A half written sibling (a save in flight) is not copied."

    root = staging_layout(tmp_path)
    record = make_record(root)
    sync.attach(root, [record])
    sibling = copy_sibling(record)
    sync.note_sibling(record)
    before = read_text(record.staging_label_path)
    spy = QSignalSpy(sync.record_changed)
    write_text(sibling, "")
    sync.queue_path(sibling)
    sync.flush_pending()
    assert len(spy) == 0
    assert record.edited is False
    assert read_text(record.staging_label_path) == before


def test_an_unknown_path_is_ignored(sync, tmp_path):
    "A json of another run is not a record of this one."

    root = staging_layout(tmp_path)
    record = make_record(root)
    sync.attach(root, [record])
    spy = QSignalSpy(sync.record_changed)
    sync.queue_path(osp.join(root, "original", "labels", "other.json"))
    sync.flush_pending()
    assert len(spy) == 0


def test_detach_stops_responding(sync, tmp_path):
    "After detach the synchronization answers nothing at all."

    root = staging_layout(tmp_path)
    record = make_record(root)
    sync.attach(root, [record])
    sibling = copy_sibling(record)
    sync.note_sibling(record)
    before = read_text(record.staging_label_path)
    sync.detach()
    spy = QSignalSpy(sync.record_changed)
    write_text(sibling, json.dumps(label_payload("a.png", "renamed")))
    sync.queue_path(sibling)
    sync.flush_pending()
    assert len(spy) == 0
    assert record.edited is False
    assert read_text(record.staging_label_path) == before


def test_detach_adopts_a_pending_save_first(sync, tmp_path):
    "A save inside the debounce window is not lost when the run ends."

    root = staging_layout(tmp_path)
    record = make_record(root)
    sync.attach(root, [record])
    sibling = copy_sibling(record)
    sync.note_sibling(record)
    # the save is queued and the debounce is still running: the window
    # of the tool closes (or the next run starts) right here
    write_text(sibling, json.dumps(label_payload("a.png", "renamed")))
    sync.queue_path(sibling)
    assert sync._timer.isActive()
    assert sync._pending
    spy = QSignalSpy(sync.record_changed)

    sync.detach()

    assert record.edited is True
    assert stored_label(record)["shapes"][0]["label"] == "renamed"
    # the run is over: the write back happened, the report did not
    assert len(spy) == 0
    # the flush happened before the state was thrown away
    assert sync.attached is False
    assert sync._pending == {}
    assert not sync._timer.isActive()


def test_a_missing_staging_folder_is_reported(sync, tmp_path):
    "A run whose staging folder is gone says so instead of raising."

    spy = QSignalSpy(sync.status_message)
    sync.attach(osp.join(str(tmp_path), "gone"), [])
    assert len(spy) == 1


def test_a_watch_limit_is_reported_as_status(sync, tmp_path, monkeypatch):
    "A watcher that cannot take every path degrades with a message."

    monkeypatch.setattr(
        QtCore.QFileSystemWatcher,
        "addPaths",
        lambda self, paths: list(paths),
    )
    root = staging_layout(tmp_path)
    record = make_record(root)
    spy = QSignalSpy(sync.status_message)
    sync.attach(root, [record])
    assert len(spy) == 1
    assert "监听" in spy[0][0]


def test_the_watcher_keeps_watching_after_a_save(sync, tmp_path):
    "Two saves in a row both land, so the path was put back."

    root = staging_layout(tmp_path)
    record = make_record(root)
    sync.attach(root, [record])
    sibling = copy_sibling(record)
    sync.note_sibling(record)
    spy = QSignalSpy(sync.record_changed)
    save_like_main_window(sibling, label_payload("a.png", "first-save"))
    if not wait_for(lambda: len(spy) >= 1):
        pytest.skip(NO_EVENT_REASON)
    assert stored_label(record)["shapes"][0]["label"] == "first-save"
    save_like_main_window(sibling, label_payload("a.png", "second-save"))
    assert wait_for(lambda: len(spy) >= 2)
    assert stored_label(record)["shapes"][0]["label"] == "second-save"


def test_a_burst_of_saves_is_debounced(sync, tmp_path):
    "Three saves in one burst cost one write back and one signal."

    root = staging_layout(tmp_path)
    record = make_record(root)
    sync.attach(root, [record])
    sibling = copy_sibling(record)
    sync.note_sibling(record)
    spy = QSignalSpy(sync.record_changed)
    for label in ("burst-one", "burst-two", "burst-three"):
        save_like_main_window(sibling, label_payload("a.png", label))
    if not wait_for(lambda: len(spy) >= 1):
        pytest.skip(NO_EVENT_REASON)
    QtTest.QTest.qWait(3 * 300)
    assert len(spy) == 1
    assert stored_label(record)["shapes"][0]["label"] == "burst-three"


def test_the_first_save_of_a_linked_sibling_is_adopted(
    sync, tmp_path
):
    "The save that breaks the hard link the bridge made still lands."

    root = staging_layout(tmp_path)
    record = make_record(root)
    sibling = sibling_label_path(record.staging_image_path)
    try:
        os.link(record.staging_label_path, sibling)
    except OSError:
        pytest.skip("the scratch file system does not support hard links")
    sync.attach(root, [record])
    sync.note_sibling(record)
    spy = QSignalSpy(sync.record_changed)
    save_like_main_window(sibling, label_payload("a.png", "linked-save"))
    if not wait_for(lambda: len(spy) >= 1):
        pytest.skip(NO_EVENT_REASON)
    assert stored_label(record)["shapes"][0]["label"] == "linked-save"
    assert not os.path.samefile(record.staging_label_path, sibling)


def test_a_save_reported_under_the_canonical_path_is_adopted(
    sync, tmp_path
):
    "The deterministic half of the linked sibling case."

    root = staging_layout(tmp_path)
    record = make_record(root)
    sibling = sibling_label_path(record.staging_image_path)
    try:
        os.link(record.staging_label_path, sibling)
    except OSError:
        pytest.skip("the scratch file system does not support hard links")
    sync.attach(root, [record])
    sync.note_sibling(record)
    save_like_main_window(sibling, label_payload("a.png", "linked-save"))
    assert not os.path.samefile(record.staging_label_path, sibling)
    spy = QSignalSpy(sync.record_changed)
    sync.queue_path(record.staging_label_path)
    sync.flush_pending()
    assert len(spy) == 1
    assert stored_label(record)["shapes"][0]["label"] == "linked-save"


def test_a_rewritten_canonical_never_takes_an_older_sibling(
    sync, tmp_path
):
    "A canonical event that carries no change never writes."

    root = staging_layout(tmp_path)
    record = make_record(root)
    sync.attach(root, [record])
    sibling = sibling_label_path(record.staging_image_path)
    write_text(sibling, json.dumps(label_payload("a.png", "stale")))
    sync.note_sibling(record)
    synchronous = label_payload("a.png", "original")
    save_like_main_window(record.staging_label_path, synchronous)
    spy = QSignalSpy(sync.record_changed)
    sync.queue_path(record.staging_label_path)
    sync.flush_pending()
    assert len(spy) == 0
    assert stored_label(record)["shapes"][0]["label"] == "original"
    assert json.loads(read_text(sibling))["shapes"][0]["label"] == "stale"
