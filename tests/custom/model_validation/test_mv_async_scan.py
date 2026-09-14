"""Asynchronous dataset scanning: debounce, one thread, late answers.

Selecting a source folder must never walk the whole dataset inside a Qt
slot, and typing a path must not scan once per keystroke. These tests
pin the scheduler that carries those two rules: a restarting debounce,
at most one scan thread, only the newest pending request kept, the
token returned untouched, every exception of the scan turned into a
scan_failed signal and every signal silenced after shutdown.

Every wait goes through QTest.qWait, so no test gambles on a sleep
being long enough on a slow machine.
"""

import os
import os.path as osp
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PyQt6 import QtTest
from PyQt6 import QtWidgets

from anylabeling.custom.model_validation import async_scan
from anylabeling.custom.model_validation import dataset


@pytest.fixture(scope="module")
def qt_app():
    "Create the offscreen application the worker signals need."

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


@pytest.fixture
def scheduler(qt_app):
    "Create a scheduler and stop it after the test."

    sched = async_scan.DirectoryScanScheduler()
    try:
        yield sched
    finally:
        sched.shutdown()
        settle(sched)


def wait_for(predicate, timeout_ms: int = 5000) -> bool:
    "Process events until the predicate holds or the timeout expires."

    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if predicate():
            return True
        QtTest.QTest.qWait(10)
    return predicate()


def settle(sched, timeout_ms: int = 5000) -> bool:
    "Wait until the scheduler holds no running scan thread."

    return wait_for(
        lambda: sched._worker is None and not sched._detached_workers,
        timeout_ms,
    )


def spies(sched):
    "Return the (scan_ready, scan_failed) spies of a scheduler."

    return (
        QtTest.QSignalSpy(sched.scan_ready),
        QtTest.QSignalSpy(sched.scan_failed),
    )


def make_pair(directory: str, name: str) -> None:
    "Write one image and its sibling json label into a directory."

    os.makedirs(directory, exist_ok=True)
    with open(osp.join(directory, name + ".png"), "wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n")
    dataset.write_json(osp.join(directory, name + ".json"), {"shapes": []})


def counting_scan(calls, gate=None, entered=None):
    "Return a collect_pairs stand-in that records and can be gated."

    real = dataset.collect_pairs

    def scan(directory):
        calls.append(directory)
        if entered is not None:
            entered.set()
        if gate is not None:
            assert gate.wait(5)
        return real(directory)

    return scan


# --------------------------------------------------------------- ready
def test_schedule_reports_token_directory_and_count(
    qt_app, tmp_path, scheduler
):
    "A scheduled scan answers with the token, the folder and the count."

    directory = str(tmp_path)
    for name in ("a", "b", "c"):
        make_pair(directory, name)
    make_pair(osp.join(directory, "nested"), "d")

    ready, failed = spies(scheduler)
    scheduler.schedule(directory, 17)

    assert wait_for(lambda: len(ready) == 1)
    assert len(failed) == 0
    assert list(ready[0]) == [17, directory, 4]
    assert settle(scheduler)


# ------------------------------------------------------------- debounce
def test_debounce_scans_once_with_the_last_token(
    qt_app, tmp_path, monkeypatch, scheduler
):
    "Three requests inside the window leave one scan, the last token."

    directory = str(tmp_path)
    make_pair(directory, "a")
    calls = []
    monkeypatch.setattr(
        dataset, "collect_pairs", counting_scan(calls)
    )

    ready, failed = spies(scheduler)
    scheduler.schedule(directory, 1, delay_ms=400)
    QtTest.QTest.qWait(20)
    scheduler.schedule(directory, 2, delay_ms=400)
    QtTest.QTest.qWait(20)
    scheduler.schedule(directory, 3, delay_ms=400)

    assert wait_for(lambda: len(ready) == 1)
    # the two replaced requests must not scan behind the last one
    QtTest.QTest.qWait(150)
    assert calls == [directory]
    assert len(ready) == 1
    assert list(ready[0]) == [3, directory, 1]
    assert len(failed) == 0
    assert settle(scheduler)


# ----------------------------------------------------- pending handling
def test_busy_worker_keeps_only_the_newest_pending(
    qt_app, tmp_path, monkeypatch, scheduler
):
    "While a scan runs, only the newest request waits for the thread."

    first = str(tmp_path)
    make_pair(first, "a")
    second = osp.join(first, "second")
    make_pair(second, "b")
    third = osp.join(first, "third")
    make_pair(third, "c")
    calls = []
    gate = threading.Event()
    entered = threading.Event()
    monkeypatch.setattr(
        dataset,
        "collect_pairs",
        counting_scan(calls, gate=gate, entered=entered),
    )

    ready, failed = spies(scheduler)
    scheduler.schedule(first, 1)
    try:
        assert entered.wait(5)
        # the worker of the first request is busy right now: both new
        # requests land in the pending slot, the first one is replaced
        scheduler.schedule(second, 2)
        scheduler.schedule(third, 3)
        assert scheduler._worker is not None
    finally:
        # never leave the worker blocked, not even on a failure: the
        # thread must be able to finish before the scheduler is freed
        gate.set()
    assert wait_for(lambda: len(ready) == 2)
    tokens = [args[0] for args in ready]
    assert tokens == [1, 3]
    # the walk of the first folder also finds its two subfolders
    assert list(ready[0]) == [1, first, 3]
    assert list(ready[1]) == [3, third, 1]
    assert calls == [first, third]
    assert len(failed) == 0
    assert settle(scheduler)


# ------------------------------------------------------------- failures
def test_scan_error_becomes_scan_failed(
    qt_app, tmp_path, monkeypatch, scheduler
):
    "An exception of the scan is reported and never crashes the thread."

    directory = str(tmp_path)

    def broken(_directory):
        raise TypeError("boom")

    monkeypatch.setattr(dataset, "collect_pairs", broken)

    ready, failed = spies(scheduler)
    scheduler.schedule(directory, 5)

    assert wait_for(lambda: len(failed) == 1)
    token, reported, message = list(failed[0])
    assert token == 5
    assert reported == directory
    assert "扫描" in message
    assert "boom" in message
    assert len(ready) == 0
    assert settle(scheduler)


# ------------------------------------------------------- missing folder
def test_missing_directory_answers_without_a_thread(
    qt_app, tmp_path, monkeypatch, scheduler
):
    "A path that is not a folder is answered at once, no thread at all."

    started = []
    monkeypatch.setattr(
        async_scan.DirectoryScanWorker,
        "start",
        lambda worker: started.append(worker),
    )
    missing = osp.join(str(tmp_path), "missing")

    ready, failed = spies(scheduler)
    scheduler.schedule(missing, 9)

    assert len(ready) == 1
    assert list(ready[0]) == [9, missing, 0]
    assert started == []
    assert scheduler._worker is None
    assert len(failed) == 0


# ------------------------------------------------------------- shutdown
def test_shutdown_stops_timer_and_silences_late_signals(
    qt_app, tmp_path, monkeypatch, scheduler
):
    "After shutdown nothing is emitted, not even by a late worker."

    directory = str(tmp_path)
    make_pair(directory, "a")
    other = osp.join(directory, "other")
    gate = threading.Event()
    entered = threading.Event()
    monkeypatch.setattr(
        dataset,
        "collect_pairs",
        counting_scan([], gate=gate, entered=entered),
    )

    ready, failed = spies(scheduler)
    scheduler.schedule(directory, 1)
    try:
        assert entered.wait(5)
        # a debounced request is waiting when the window closes
        scheduler.schedule(other, 2, delay_ms=300)
        assert scheduler._timer.isActive()
        scheduler.shutdown(timeout_ms=100)
        assert not scheduler._timer.isActive()
    finally:
        gate.set()
    QtTest.QTest.qWait(400)
    assert len(ready) == 0
    assert len(failed) == 0

    # the scheduler stays mute, and never starts a thread again
    scheduler.schedule(directory, 3)
    scheduler.schedule(directory, 4, delay_ms=50)
    QtTest.QTest.qWait(200)
    assert len(ready) == 0
    assert len(failed) == 0
    assert settle(scheduler)
