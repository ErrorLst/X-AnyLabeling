"""Thread and close behaviour of the monitoring workers (spec §5.1.4).

CT17, CT24 and CT41 live here: every test runs a real QThread under the
offscreen platform, watches for the "Destroyed while thread is still
running" warning and asserts the step 4 criterion (isFinished() for every
worker) before the window is allowed to go away.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from types import SimpleNamespace

import pytest
from PyQt6 import QtCore, QtWidgets

from anylabeling.custom.remote_training import api_client as api
from anylabeling.custom.remote_training import poller as poll_mod
from anylabeling.custom.remote_training.store import Store, TaskRecord
from anylabeling.custom.remote_training.ui.dialog import RemoteTrainingDialog
from anylabeling.custom.remote_training.worker import (
    CommandWorker,
    JobMonitor,
    OPERATION_CANCEL,
    OPERATION_RESUME,
    PollWorker,
    SubmitWorker,
    cancellable_wait,
    default_scheduler,
)

JOB_ID = "job_20260101_000001"


class SubmissionRun:
    """The four fields SubmitWorker reads off the packer run."""

    dataset_dir = "/data/annotations/person"
    classes_file = "/data/annotations/person/classes.txt"
    task = "detect"
    val_ratio = 0.2
    seed = 7
    staging_dir = None

    def manifest(self, labels):
        return {
            "schema_version": 1,
            "task": "detect",
            "val_ratio": 0.2,
            "seed": 7,
            "classes": ["person"],
            "images": [],
        }


def job(**overrides):
    payload = {
        "job_id": JOB_ID,
        "status": "running",
        "is_terminal": False,
        "attempt": 1,
        "max_attempts": 3,
        "resume_cycles": 0,
        "needs_attention": False,
        "needs_attention_reason": None,
        "artifact_suspect": False,
        "partial_available": False,
        "resume_mode_available": [],
        "progress": {"epoch": 1, "total_epochs": 10, "percent": 10.0},
        "created_at": "2026-01-01T10:00:00Z",
        "finished_at": None,
    }
    payload.update(overrides)
    return payload


def seed(store, **overrides):
    ledger = store.load_ledger()
    fields = {"status": "running", "is_terminal": False}
    fields.update(overrides)
    ledger.upsert_record(TaskRecord(
        job_id=JOB_ID,
        client_job_name="任务",
        **fields,
    ))
    store.save_ledger(ledger)


class Gate:
    """A one way gate: the worker waits here until the test opens it.

    A queue is used instead of an Event because the wait must stay
    blocked until it is released or until its own timeout expires,
    which is exactly what a parked socket looks like.
    """

    def __init__(self):
        self._queue = queue.Queue()

    def open(self):
        self._queue.put(True)

    def wait(self, timeout=30.0):
        try:
            return bool(self._queue.get(timeout=timeout))
        except queue.Empty:
            return False


class ScriptedClient:
    """A client that answers fast, or blocks until the test releases it."""

    def __init__(self, jobs=None):
        self.jobs = list(jobs or [job()])
        self.block = Gate()
        self.parked = False
        self.entered = threading.Event()
        self.detail_calls = 0
        self.events_calls = 0
        self.files_calls = 0
        self.cancelled = False
        self.resumed = []
        self.resume_cycles = 0

    def list_jobs(self, ids=None, status=None, limit=50):
        wanted = list(ids or [])
        by_id = {item["job_id"]: item for item in self.jobs}
        return {
            "jobs": [by_id[item] for item in wanted if item in by_id],
            "not_found_ids": [
                item for item in wanted if item not in by_id
            ],
        }

    def get_job(self, job_id):
        self.detail_calls += 1
        if self.parked:
            self.entered.set()
            # Stands in for a socket parked in a request: it only returns
            # when the test releases it, or when the request timeout would
            # fire (spec §5.5.1).
            self.block.wait(30)
        for item in self.jobs:
            if item["job_id"] == job_id:
                return dict(item)
        raise api.JobNotFoundError(404, "JOB_NOT_FOUND", "gone")

    def get_events(self, job_id, after=0):
        self.events_calls += 1
        return {"events": [], "last_seq": int(after)}

    def list_job_files(self, job_id, include_partial=True):
        self.files_calls += 1
        return {"files": []}

    def cancel_job(self, job_id):
        self.cancelled = True
        # The server moves the job to its terminal cancelled state, and
        # every later job object shows it (spec §3.4.2).
        for item in self.jobs:
            item["status"] = "cancelled"
            item["is_terminal"] = True
            item["finished_at"] = "2026-01-01T11:05:00Z"
        return {"job_id": job_id, "status": "cancelled"}

    def resume_job(self, job_id, mode="resume"):
        self.resumed.append((job_id, mode))
        self.resume_cycles += 1
        # The server resets the attempt and bumps the cycle count, and
        # every later job object carries the new cycle (spec §3.4.4).
        self.release()
        return {
            "job_id": job_id,
            "status": "queued",
            "mode": mode,
            "attempt": 1,
            "resume_cycles": self.resume_cycles,
        }

    def release(self):
        """Apply the newest resume to every job object the client serves."""

        for item in self.jobs:
            item["attempt"] = 1
            item["resume_cycles"] = self.resume_cycles
            item["status"] = "queued"
            item["finished_at"] = None
            item["is_terminal"] = False


class Parent(QtWidgets.QMainWindow):
    """Stand in for the labeling main window."""


@pytest.fixture
def dialog(qapp, tmp_path):
    """One offscreen window with its own ledger and scripted client."""

    store = Store(str(tmp_path / "ledger"))
    window = RemoteTrainingDialog(Parent(), store=store)
    window._confirm_close = lambda _dialog: True
    window._confirm_action = lambda _title, _text: True
    window.show()
    yield window
    try:
        window.cancel_workers()
        window.reject()
    except RuntimeError:  # pragma: no cover - already destroyed
        pass
    try:
        qapp.processEvents()
    except RuntimeError:  # pragma: no cover - defensive
        pass


def pump(qapp, milliseconds=200):
    timer = QtCore.QElapsedTimer()
    timer.start()
    while timer.elapsed() < milliseconds:
        qapp.processEvents()
        QtCore.QThread.msleep(5)


def open_window(parent, store, client):
    """Open one window with its ledger and its scripted client."""

    parent._remote_training_dialog = None
    window = RemoteTrainingDialog(parent, store=store)
    parent._remote_training_dialog = window
    window._confirm_close = lambda _dialog: True
    window._confirm_action = lambda _title, _text: True
    window.client = client
    window.show()
    return window

# ------------------------------------------------------- CT17 / CT41


def test_ct17_cancellable_wait_returns_on_the_event():
    """The single Event is the only wake up channel (spec §5.5.1)."""

    event = threading.Event()
    assert cancellable_wait(0, event) is False
    assert cancellable_wait(0.05, event) is False
    thread = threading.Thread(target=lambda: event.set())
    thread.start()
    assert cancellable_wait(5.0, event) is True
    thread.join()


def test_ct17_poll_worker_wakes_up_and_finishes(qapp, tmp_path):
    """CT17: cancel() wakes the loop; isFinished() becomes true.

    The list page interval bounds are the one seam that shortens the
    cadence, so the first tick is observable without waiting 10 s.  The
    worker is deliberately unbounded (no max_ticks): the point is that
    cancel() - not a tick budget - ends the loop.
    """

    store = Store(str(tmp_path / "ledger"))
    seed(store)
    client = ScriptedClient()
    worker = PollWorker(
        store=store,
        client=client,
        page="jobs",
        active_ids=lambda: [JOB_ID],
        visible=lambda: True,
        interval_min=0.05,
        interval_max=0.05,
    )
    worker.start()
    deadline = time.time() + 5.0
    while worker.ticks < 1 and time.time() < deadline:
        QtCore.QThread.msleep(5)
    assert worker.ticks >= 1, "the worker really polled"
    # The injected bounds reached the scheduler: without them the list
    # page would tick every 10 s (spec §5.5.1).
    assert worker.intervals[0] == 0.05
    assert worker.isFinished() is False  # unbounded: still polling
    worker.cancel()
    assert worker.wait(5000) is True
    pump(qapp, 50)
    assert worker.isFinished() is True
    assert worker.should_stop() is True
    assert worker._cancel_event.is_set() is True


def test_ct17_dialog_close_waits_for_the_poll_worker(
    qapp, tmp_path, qt_messages
):
    """CT17: closing while polling never destroys a live QThread."""

    store = Store(str(tmp_path / "ledger"))
    seed(store)
    client = ScriptedClient()
    window = open_window(Parent(), store, client)
    worker = window.start_polling("detail", JOB_ID)
    assert worker in window.workers
    assert worker.wait(5000) is False

    # Step 3 wakes the single Event and step 4 vetoes the close.
    window.close()
    assert worker._cancel_event.is_set() is True
    # Step 4 vetoed the close while the worker was alive: no thread is
    # killed and the window stays alive with its worker ownership.
    assert window._stopping is True
    assert worker in window.workers
    assert "正在停止" in " ".join(
        window.detail_page.status_row.lines()
    )

    # The worker (already cancelled) ends on its own, and only then does
    # the window go away.
    assert worker.wait(5000) is True
    pump(qapp, 400)
    assert window.workers == []
    assert "Destroyed while thread is still running" not in qt_messages()


def test_ct41_application_quit_is_vetoed_while_polling(qapp, tmp_path):
    """CT41: quit() while a worker runs is vetoed, then goes through."""

    store = Store(str(tmp_path / "ledger"))
    seed(store)
    client = ScriptedClient()
    client.parked = True  # keep one request in flight for the whole test
    window = open_window(Parent(), store, client)
    worker = window.start_polling("detail", JOB_ID)
    assert client.entered.wait(5) is True
    assert worker.isFinished() is False

    # The guard answers "yes" for this exit request and arms the retry.
    assert window.close_guard is not None
    guard = window.close_guard
    guard._confirm = lambda _dialog: True
    assert guard.eventFilter(
        qapp, QtCore.QEvent(QtCore.QEvent.Type.Quit)
    ) is True
    assert window._pending_quit is True

    client.block.open()
    assert worker.wait(5000) is True
    pump(qapp, 400)
    assert window._pending_quit is False
    assert all(item.isFinished() for item in window.workers)


# ------------------------------------------------------------ CT24


def test_ct24_request_timeout_is_the_600s_bound():
    """CT24: the only bound of a stalled request is the read timeout."""

    timeouts = api.Timeouts()
    assert timeouts.upload_read == 600.0
    assert timeouts.json_request() == (10.0, 60.0)
    assert timeouts.streaming_request() == (10.0, 600.0)


def test_ct24_hung_request_does_not_destroy_the_window(
    qapp, tmp_path, qt_messages
):
    """CT24: a request that never answers keeps the window alive.

    The window may only go away once the parked request has returned:
    the cancellation itself cannot interrupt a socket already waiting
    for its response headers - only the read timeout can (spec §5.4.4
    shape ②, 600 s), which is exactly the window this case covers.
    """

    store = Store(str(tmp_path / "ledger"))
    seed(store)
    client = ScriptedClient()
    client.parked = True  # park the very next request
    window = open_window(Parent(), store, client)
    worker = window.start_polling("detail", JOB_ID)
    assert client.entered.wait(5) is True

    window.close()
    window.close()  # a re-entrant close must behave the same way
    assert window._stopping is True
    assert window._closing is True
    assert worker in window.workers
    assert worker._cancel_event.is_set() is True

    # Releasing the parked request is what lets the window go away.
    client.block.open()
    assert worker.wait(5000) is True
    pump(qapp, 400)
    assert window.workers == []
    assert "Destroyed while thread is still running" not in qt_messages()


# ------------------------------------------- cancel / resume commands


def test_cancel_command_writes_nothing_and_reports(qapp, tmp_path):
    """The CommandWorker runs one POST and reports its outcome."""

    client = ScriptedClient()
    worker = CommandWorker(
        OPERATION_CANCEL, JOB_ID, client=client
    )
    seen = []
    worker.command_done.connect(seen.append)
    worker.start()
    assert worker.wait(5000) is True
    pump(qapp, 50)
    assert client.cancelled is True
    assert worker.result is not None
    assert worker.result.ok is True
    assert worker.result.status == "cancelled"
    assert seen and seen[0].ok is True


def test_resume_command_reports_the_new_cycle(qapp, tmp_path):
    client = ScriptedClient()
    worker = CommandWorker(
        OPERATION_RESUME, JOB_ID, client=client, mode="restart"
    )
    worker.start()
    assert worker.wait(5000) is True
    pump(qapp, 50)
    assert client.resumed == [(JOB_ID, "restart")]
    assert worker.result.ok is True
    assert worker.result.mode == "restart"
    assert worker.result.resume_cycles == 1


def test_dialog_cancel_writes_the_stopping_line(qapp, tmp_path):
    """The N of "最长 N 秒" comes from the capabilities (spec §3.11)."""

    store = Store(str(tmp_path / "ledger"))
    seed(store)
    client = ScriptedClient()
    window = RemoteTrainingDialog(Parent(), store=store)
    window._confirm_close = lambda _dialog: True
    window._confirm_action = lambda _title, _text: True
    window.client = client
    window.capabilities = api.Capabilities(
        {"cancel_grace_seconds": 42}
    )
    window.show()
    worker = window.cancel_job(JOB_ID)
    assert worker is not None
    assert "最长 42 秒" in " ".join(
        window.detail_page.status_row.lines()
    )
    assert worker.wait(5000) is True
    pump(qapp, 100)
    assert client.cancelled is True
    assert window.detail_page.job_id == JOB_ID


def test_dialog_resume_writes_the_ledger_back(qapp, tmp_path):
    """spec §5.5.7 step 2: attempt / cycles / badge, artifacts kept."""

    store = Store(str(tmp_path / "ledger"))
    seed(
        store,
        status="failed",
        is_terminal=True,
        attempt=3,
        resume_cycles=0,
        needs_attention=True,
        needs_attention_reason="attempts_exhausted",
        artifact_suspect=True,
        extra={"resume_mode_available": ["resume"], "max_attempts": 3},
    )
    client = ScriptedClient()
    # The server never answers a job GET here, so the only writer of the
    # row between the resume response and the read is the write back.
    client.jobs = []
    client.parked = True
    window = open_window(Parent(), store, client)
    window.show_detail(JOB_ID)
    assert client.entered.wait(5) is True
    before = store.load_ledger().record(JOB_ID)
    assert before.attempt == 3, "the fixture row is the failed attempt"
    worker = window.resume_job(JOB_ID, "resume")
    assert worker is not None
    assert worker.wait(5000) is True
    pump(qapp, 150)
    # The resume write back happened before the refresh worker could
    # answer: release it and stop the poller, then read the ledger.
    window.cancel_workers()
    client.block.open()
    pump(qapp, 200)
    record = store.load_ledger().record(JOB_ID)
    assert record.attempt == 1
    assert record.resume_cycles == 1
    assert record.needs_attention is False
    assert record.needs_attention_reason is None
    assert record.artifact_suspect is True
    assert record.manual_resume == {
        "attempt": 1, "resume_cycles": 1, "mode": "resume"
    }


def test_dialog_resume_without_a_mode_is_refused(qapp, tmp_path):
    """resume_mode_available == [] greys the button and sends nothing."""

    store = Store(str(tmp_path / "ledger"))
    seed(store, status="completed", is_terminal=True)
    client = ScriptedClient()
    window = open_window(Parent(), store, client)
    window.show_detail(JOB_ID)
    window.detail_page.job_id = JOB_ID
    window.detail_page.resume_modes = []
    assert window.resume_job(JOB_ID) is None
    assert client.resumed == []
    assert window.detail_page.resume_button.isEnabled() is False
    assert "服务端未声明可恢复模式" in " ".join(
        window.detail_page.status_row.lines()
    )


def test_resume_from_the_list_page_reads_the_rendered_job(qapp, tmp_path):
    """§5.5.7 step 1: the bulk resume works from the list page.

    resume_mode_available and max_attempts are job object fields (spec
    §3.4.4) that the ledger does not mirror, so the list row the polling
    rendered is the only source; the confirmation must then print the
    reset semantics of the spec.
    """

    store = Store(str(tmp_path / "ledger"))
    seed(
        store,
        status="failed",
        is_terminal=True,
        attempt=3,
        finished_at="2026-01-01T11:00:00Z",
        needs_attention=True,
        needs_attention_reason="attempts_exhausted",
    )
    client = ScriptedClient()
    client.jobs = [job(
        status="failed",
        is_terminal=True,
        attempt=3,
        max_attempts=3,
        resume_mode_available=["resume"],
        finished_at="2026-01-01T11:00:00Z",
    )]
    window = open_window(Parent(), store, client)
    window.show_jobs()
    pump(qapp, 300)
    assert [
        str(item.get("job_id")) for item in window.jobs_page.jobs
    ] == [JOB_ID]
    # The ledger has neither key: the rendered job is the only source.
    record = store.load_ledger().record(JOB_ID)
    assert "resume_mode_available" not in (record.extra or {})
    assert "max_attempts" not in (record.extra or {})
    window.detail_page.resume_modes = []  # not the detail page's list
    window.stop_polling()
    asked = []
    window._confirm_action = lambda _title, text: (
        asked.append(text), True
    )[1]
    # The production path: the row checkbox and the 恢复 button emit the
    # ids, resume_jobs() fans out, resume_job() confirms and POSTs.
    window.jobs_page.resume_requested.emit([JOB_ID])
    commands = [
        item for item in window.workers
        if getattr(item, "action", "") == "resume"
    ]
    assert commands, "the list page resume must send the request"
    assert commands[0].wait(5000) is True
    pump(qapp, 100)
    assert client.resumed == [(JOB_ID, "resume")]
    assert asked, "the confirmation ran"
    assert "自动重试 3 次" in asked[0]
    assert "最多再自动重试 2 次" in asked[0]
    window.stop_polling()


def test_resume_without_a_mode_warns_on_the_visible_page(qapp, tmp_path):
    """The "no resume mode" hint must land on the page on screen."""

    store = Store(str(tmp_path / "ledger"))
    seed(store, status="completed", is_terminal=True)
    client = ScriptedClient()
    client.jobs = [job(
        status="completed",
        is_terminal=True,
        resume_mode_available=[],
        finished_at="2026-01-01T11:00:00Z",
    )]
    window = open_window(Parent(), store, client)
    window.show_jobs()
    pump(qapp, 300)
    window.stop_polling()
    window.detail_page.resume_modes = []
    # N2: a local hint is one more composed line, never a second writer
    # that wipes the reconciliation banner of the same page.
    window.reconcile_lines = [("yellow", "有 1 个未完成的上传，正在自动对账")]
    window.jobs_page.status_row.set_lines(window.reconcile_lines)
    assert window.resume_job(JOB_ID) is None
    assert client.resumed == []
    jobs_lines = window._status_rows("jobs")[0].lines()
    assert any("服务端未声明可恢复模式" in text for text in jobs_lines)
    assert any("未完成" in text for text in jobs_lines)
    assert window.detail_page.status_row.is_empty() is True


def test_local_hints_keep_the_reconcile_banner(qapp, tmp_path):
    """N2: the "nothing to cancel / resume" hints keep the banner too."""

    store = Store(str(tmp_path / "ledger"))
    seed(store, status="completed", is_terminal=True,
         finished_at="2026-01-01T11:00:00Z")
    client = ScriptedClient()
    # The served job is completed too, so the list tick cannot turn the
    # row back into a cancellable one.
    client.jobs = [job(status="completed", is_terminal=True,
                       finished_at="2026-01-01T11:00:00Z")]
    window = open_window(Parent(), store, client)
    window.show_jobs()
    pump(qapp, 300)
    window.stop_polling()
    window.reconcile_lines = [("yellow", "有 1 个未完成的上传，正在自动对账")]
    window.jobs_page.status_row.set_lines(window.reconcile_lines)

    assert window.cancel_jobs([]) == []
    lines = window._status_rows("jobs")[0].lines()
    assert any("没有可取消的任务" in text for text in lines)
    assert any("未完成" in text for text in lines)

    assert window.resume_jobs([]) == []
    lines = window._status_rows("jobs")[0].lines()
    assert any("没有可恢复的任务" in text for text in lines)
    assert any("未完成" in text for text in lines)


def test_entering_a_page_starts_its_polling_worker(qapp, tmp_path):
    """D2: show_detail() alone is enough to get polling running."""

    store = Store(str(tmp_path / "ledger"))
    seed(store)
    client = ScriptedClient()
    window = open_window(Parent(), store, client)
    assert window.poll_worker is None
    window.show_detail(JOB_ID)
    worker = window.poll_worker
    assert worker is not None, "entering the page must start polling"
    assert worker in window.workers
    assert worker.isFinished() is False
    pump(qapp, 300)
    assert window.detail_page.job_id == JOB_ID
    assert window.detail_page.bar.value() == 10
    assert "进度 10.0%" in window.detail_page.progress_label.text()
    monitor = window.monitors["detail"]
    assert monitor.ticks >= 1
    assert monitor.intervals[0] == 3.0
    window.stop_polling()


def test_jobs_page_entry_polls_and_reconciles(qapp, tmp_path):
    """D2/D5: the jobs page entry starts the list poll and reconcile."""

    store = Store(str(tmp_path / "ledger"))
    seed(store)
    client = ScriptedClient()
    window = open_window(Parent(), store, client)
    window.show_jobs()
    worker = window.poll_worker
    assert worker is not None
    assert getattr(worker, "_page", "") == "jobs"
    assert window.reconcile_worker is None  # nothing pending to reconcile
    pump(qapp, 300)
    assert window.jobs_page.table.rowCount() == 1
    window.stop_polling()


def test_results_page_entry_polls_its_own_page(qapp, tmp_path):
    """D2/D3: the results page polls and renders on its own row."""

    store = Store(str(tmp_path / "ledger"))
    seed(store, status="cancelled", is_terminal=True)
    client = ScriptedClient()
    window = open_window(Parent(), store, client)
    # N3: the start-up scan runs on the production default root too (the
    # injected root is only the seam the drill above uses).
    assert window.staging_reclaim is not None
    window.show_results(JOB_ID)
    worker = window.poll_worker
    assert worker is not None
    assert getattr(worker, "_page", "") == "results"
    pump(qapp, 300)
    assert window.results_page.job_id == JOB_ID
    # The results page has its own status row: the detail row is untouched.
    assert window.detail_page.status_row.is_empty() is True
    window.stop_polling()


#: The manifest of a finished job: the two rows the report is about.
MANIFEST = [
    {
        "file_id": "f_best",
        "path": "run/train/weights/best.pt",
        "size": 1024,
        "mtime": "2026-01-01T11:00:00Z",
        "partial": False,
    },
    {
        "file_id": "f_summary",
        "path": "summary.json",
        "size": 128,
        "mtime": "2026-01-01T11:00:00Z",
        "partial": False,
    },
]
SUMMARY = {"duration_seconds": 612.5, "final_metrics": {"mAP50": 0.512}}


class ArtifactBody:
    """The in-memory body of one route 14 read (no socket involved)."""

    def __init__(self, payload):
        self._raw = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_chunks(self):
        yield self._raw


def test_a_finished_job_fills_the_results_page_on_entry(qapp, tmp_path):
    """§5.2-F: a terminal job shows its manifest on entry, not never.

    The poller skips the files route on a terminal tick (spec §5.5.2),
    so the table show_results() cleared could never be refilled and the
    summary, which is read out of that same table, stayed empty.  The
    page now reads the manifest once, explicitly, on entry.
    """

    store = Store(str(tmp_path / "ledger"))
    seed(store, status="completed", is_terminal=True,
         finished_at="2026-01-01T11:00:00Z")
    client = ScriptedClient()
    client.jobs = [job(status="completed", is_terminal=True,
                       finished_at="2026-01-01T11:00:00Z")]
    calls = []

    def counting(job_id, include_partial=True):
        calls.append((job_id, bool(include_partial)))
        return {"files": [dict(item) for item in MANIFEST]}

    client.list_job_files = counting
    client.open_job_file = lambda job_id, file_id, **kw: ArtifactBody(SUMMARY)
    window = open_window(Parent(), store, client)
    window.show_results(JOB_ID)
    pump(qapp, 600)
    window.stop_polling()

    assert calls == [(JOB_ID, True)]
    assert window.results_page.files_job_id == JOB_ID
    assert window.results_page.tree.rowCount() == len(MANIFEST)
    assert window.results_page.files == MANIFEST
    assert "已加载产物清单" in window.results_page.debug_text()
    assert "duration_seconds: 612.5" in (
        window.results_page.summary_edit.toPlainText()
    )
    # The latch: a second tick never re-reads the same manifest.
    assert window.load_manifest(JOB_ID) is None
    assert calls == [(JOB_ID, True)]


def test_manual_resume_event_reaches_the_debug_area(qapp, tmp_path):
    """A manual_resume event is written back and logged (spec §5.5.3)."""

    store = Store(str(tmp_path / "ledger"))
    seed(store)
    client = ScriptedClient()
    client.get_events = lambda job_id, after=0: {
        "events": [
            {"seq": 1, "ts": "t", "type": "manual_resume",
             "data": {"attempt": 1, "mode": "resume",
                      "resume_cycles": 1}},
        ],
        "last_seq": 1,
    }
    window = open_window(Parent(), store, client)
    window.show_detail(JOB_ID)
    pump(qapp, 400)
    window.stop_polling()
    assert "第 1 次人工恢复" in window.detail_page.log.toPlainText()
    assert "manual_resume" in window.results_page.debug_text()
    record = store.load_ledger().record(JOB_ID)
    assert record.resume_cycles == 1
    assert record.manual_resume["mode"] == "resume"


def test_job_monitor_records_the_ticks():
    """The window side paper trail of one page."""

    monitor = JobMonitor(page="detail")
    outcome = poll_mod.PollOutcome(page="detail", interval=3.0)
    assert monitor.note(outcome) is outcome
    assert monitor.ticks == 1
    assert monitor.intervals == [3.0]
    assert monitor.last_outcome is outcome


# ------------------------------------------------- D6: no GUI thread I/O


class ThreadRecordingClient:
    """A client wrapper that records the thread of every call."""

    def __init__(self, inner, main_thread_id):
        self.inner = inner
        self.main_thread_id = main_thread_id
        self.calls = []

    def _note(self, name):
        self.calls.append((name, threading.get_ident()))

    def __getattr__(self, name):
        target = getattr(self.inner, name)
        if not callable(target):
            return target

        def wrapper(*args, **kwargs):
            self._note(name)
            return target(*args, **kwargs)

        return wrapper

    def main_thread_calls(self):
        return [
            name for name, ident in self.calls
            if ident == self.main_thread_id
        ]


def test_d6_gui_thread_issues_no_request(qapp, tmp_path):
    """D6: every network call of the monitoring paths runs in a worker."""

    store = Store(str(tmp_path / "ledger"))
    seed(store)
    # The synchronous seam is gone: no GUI slot can tick the poller inline.
    assert not hasattr(RemoteTrainingDialog, "poll_once")
    client = ThreadRecordingClient(ScriptedClient(), threading.get_ident())
    window = open_window(Parent(), store, client)
    window.show_detail(JOB_ID)
    pump(qapp, 300)
    window.detail_page.resume_modes = ["resume"]
    resume = window.resume_job(JOB_ID, "resume")
    assert resume is not None and resume.wait(5000) is True
    pump(qapp, 200)
    window.stop_polling()
    pump(qapp, 200)
    assert client.calls, "the flow must have talked to the server"
    assert client.main_thread_calls() == []


def test_d6_stalled_server_keeps_the_close_machine_running(qapp, tmp_path):
    """D6: while a request is stalled the window still closes on demand."""

    store = Store(str(tmp_path / "ledger"))
    seed(store)
    client = ScriptedClient()
    client.parked = True
    window = open_window(Parent(), store, client)
    window.show_detail(JOB_ID)
    assert client.entered.wait(5) is True
    # The GUI thread is idle: the close machine runs and vetoes, exactly
    # like CT24, because no synchronous request holds the event loop.
    window.close()
    assert window._stopping is True
    assert window._closing is True
    client.block.open()
    pump(qapp, 300)


# --------------------------------------------- D5: submit chain (429)


class RecordingUploader:
    """A stand in for the B4 state machine that drives the 429 branch."""

    def __init__(self, retry_after=0.0, fail_first=True):
        self.retry_after = retry_after
        self.fail_first = fail_first
        self.uploads = 0
        self.submitted = 0
        self.completed = []
        self.ledger = SimpleNamespace(
            submission=lambda key: SimpleNamespace(
                client_submission_id=key,
                phase="submitting",
                pending_dir="/tmp/pending/sub",
                server_url="http://server:8000",
                dataset_id="ds_1",
            )
        )

    def plan(self, manifest, **kwargs):
        from anylabeling.custom.remote_training.uploader import (
            PlanOutcome,
        )

        return PlanOutcome(
            ok=True,
            upload_token="ut_" + "a" * 32,
            missing_images=[{"name": "0001.jpg", "split": "train"}],
            total_images=2,
            entry=SimpleNamespace(
                upload_token="ut_" + "a" * 32, phase="planned"
            ),
        )

    def upload(self, client, entry, **kwargs):
        from anylabeling.custom.remote_training.uploader import (
            UploadOutcome,
        )

        self.uploads += 1
        if self.fail_first and self.uploads == 1:
            return UploadOutcome(
                ok=False,
                phase=entry,
                action="retry_after",
                reason="committed_token_capacity",
                retry_after=self.retry_after,
                error_message="429",
            )
        entry.dataset_id = "ds_1"
        entry.phase = "committed"
        return UploadOutcome(
            ok=True, dataset_id="ds_1", phase=entry, action="submit"
        )

    def submit(self, client, entry, body, **kwargs):
        from anylabeling.custom.remote_training.uploader import SubmitOutcome

        self.submitted += 1
        return SubmitOutcome(
            ok=True,
            job_id="job_new",
            client_submission_id="sub_" + "b" * 32,
            response={"job_id": "job_new", "status": "queued"},
        )

    def complete_submission(self, submission, job_id, **kwargs):
        from anylabeling.custom.remote_training.store import TaskRecord

        self.completed.append(job_id)
        return TaskRecord(
            job_id=job_id, client_job_name="新任务", status="queued"
        )

    def reload(self):
        return None


def test_submit_worker_consumes_the_429_schedule(qapp):
    """D5: the worker waits out the UploadSchedule and replays the token."""

    run = SubmissionRun()
    pipeline = SimpleNamespace(labels={"0001": object()}, run=run)
    uploader = RecordingUploader(retry_after=0.2)
    worker = SubmitWorker(
        pipeline,
        run,
        {"schema_version": 1, "task": "detect", "params": {}},
        store=Store(""),
        client=object(),
        uploader=uploader,
        server_url="http://server:8000",
    )
    seen = []
    worker.submitted.connect(seen.append)
    worker.start()
    assert worker.wait(5000) is True
    pump(qapp, 100)
    assert uploader.uploads == 2, "the same token is replayed once"
    assert worker.outcome.resumed_after_429 is True
    assert worker.outcome.wait_seconds == 0.2
    assert worker.outcome.ok is True
    assert worker.outcome.job_id == "job_new"
    assert uploader.completed == ["job_new"]
    assert seen and seen[0].ok is True


def test_submit_worker_can_be_cancelled_during_the_wait(qapp):
    """D5: the 429 wait is cancellable (one Event, like every wait)."""

    run = SubmissionRun()
    pipeline = SimpleNamespace(labels={"0001": object()}, run=run)
    uploader = RecordingUploader(retry_after=5.0)
    worker = SubmitWorker(
        pipeline,
        run,
        {"schema_version": 1, "task": "detect"},
        store=Store(""),
        client=object(),
        uploader=uploader,
    )
    worker.start()
    pump(qapp, 200)
    worker.cancel()
    assert worker.wait(5000) is True
    pump(qapp, 50)
    assert worker.outcome.cancelled is True
    assert uploader.uploads == 1, "the replay never happened"


def test_submit_worker_reports_a_failed_plan(qapp):
    """The chain stops before any upload when the plan is refused."""

    class Refusing(RecordingUploader):
        def plan(self, manifest, **kwargs):
            from anylabeling.custom.remote_training.uploader import (
                PlanOutcome,
            )

            return PlanOutcome(
                ok=False,
                error_message="全部条目都被服务端拒绝",
                error_code="VALIDATION_FAILED",
                local_block="all_entries_rejected",
            )

    run = SubmissionRun()
    pipeline = SimpleNamespace(labels={"0001": object()}, run=run)
    uploader = Refusing()
    worker = SubmitWorker(
        pipeline,
        run,
        {"schema_version": 1, "task": "detect"},
        store=Store(""),
        client=object(),
        uploader=uploader,
    )
    worker.start()
    assert worker.wait(5000) is True
    pump(qapp, 50)
    assert worker.outcome.ok is False
    assert "拒绝" in worker.outcome.error_message
    assert uploader.uploads == 0

# ------------------------------------------------- R2: refresh semantics


def test_r2_command_completion_keeps_the_continuous_poller(qapp, tmp_path):
    """R2: a resume refresh must wake the poller, not replace it."""

    store = Store(str(tmp_path / "ledger"))
    seed(
        store,
        status="failed",
        is_terminal=True,
        attempt=3,
        needs_attention=True,
        needs_attention_reason="attempts_exhausted",
        finished_at="2026-01-01T11:00:00Z",
        extra={"resume_mode_available": ["resume"], "max_attempts": 3},
    )
    client = ScriptedClient()
    client.jobs = [job(status="failed", attempt=3, is_terminal=True,
                       finished_at="2026-01-01T11:00:00Z")]
    window = open_window(Parent(), store, client)
    window.show_detail(JOB_ID)
    worker = window.poll_worker
    assert worker is not None
    pump(qapp, 300)
    ticks_before = worker.ticks
    assert ticks_before >= 1
    assert worker.intervals[-1] == 60.0, "the terminal job falls back to 60 s"

    window.detail_page.resume_modes = ["resume"]
    command = window.resume_job(JOB_ID, "resume")
    assert command is not None and command.wait(5000) is True
    pump(qapp, 400)

    # The very same continuous worker is still there, and the transition
    # back to a live job immediately restored the 3 s tier (§5.5.2 ②).
    assert window.poll_worker is worker
    assert worker.isFinished() is False
    assert worker.ticks > ticks_before
    assert worker.intervals[-1] == 3.0
    assert store.load_ledger().record(JOB_ID).is_terminal is False
    assert "恢复高频轮询" in window.detail_page.log.toPlainText()

    # The cancel path keeps the same worker alive too (its job goes
    # terminal, so the interval moves to the 60 s tier - by design).
    assert window.cancel_job(JOB_ID).wait(5000) is True
    pump(qapp, 300)
    assert window.poll_worker is worker
    assert worker.isFinished() is False
    assert worker.intervals[-1] == 60.0
    window.stop_polling()


def test_r2_cross_page_refresh_does_not_kill_the_jobs_poller(qapp, tmp_path):
    """R2: refreshing the detail page while on the list must not stop it."""

    store = Store(str(tmp_path / "ledger"))
    seed(store, status="failed", is_terminal=True,
         finished_at="2026-01-01T11:00:00Z")
    client = ScriptedClient()
    window = open_window(Parent(), store, client)
    window.show_jobs()
    jobs_worker = window.poll_worker
    assert jobs_worker is not None and jobs_worker._page == "jobs"
    error = api.JobArtifactsExpiredError(
        409, "JOB_ARTIFACTS_EXPIRED", "gone",
        {"reason": "checkpoint_missing"},
    )
    view = poll_mod.client_error_view(error)
    assert view.refresh_detail is True
    window._apply_error_view(view, JOB_ID)
    assert window.poll_worker is jobs_worker, "the list poller must survive"
    assert jobs_worker.isFinished() is False
    pump(qapp, 300)
    assert jobs_worker.ticks >= 1
    window.stop_polling()


def test_r2_manual_refresh_still_works_after_a_command(qapp, tmp_path):
    """R2: the refresh button keeps working after a command completes."""

    store = Store(str(tmp_path / "ledger"))
    seed(store)
    client = ScriptedClient()
    window = open_window(Parent(), store, client)
    window.show_detail(JOB_ID)
    worker = window.poll_worker
    assert worker is not None
    pump(qapp, 150)
    assert window.cancel_job(JOB_ID).wait(5000) is True
    pump(qapp, 200)
    assert window.poll_worker is worker
    ticks = worker.ticks
    window.jobs_page.refresh_requested.emit()  # harmless on the detail page
    assert window.wake_polling("detail") is True
    pump(qapp, 300)
    assert worker.ticks > ticks
    window.stop_polling()


# --------------------------------------------- R3: close during a submit


def test_r3_close_channels_cover_the_new_workers():
    """R3: step 2 must be able to disconnect the new business signals."""

    from anylabeling.custom.remote_training.ui.dialog import CLOSE_CHANNELS

    assert "submitted" in CLOSE_CHANNELS
    assert "reconciled" in CLOSE_CHANNELS
    assert "polled" in CLOSE_CHANNELS and "command_done" in CLOSE_CHANNELS
    assert "manifest_loaded" in CLOSE_CHANNELS


def test_r3_cancelled_submit_worker_emits_nothing(qapp):
    """R3: a cancelled chain must not report success (spec §5.1.4)."""

    run = SubmissionRun()
    pipeline = SimpleNamespace(labels={"0001": object()}, run=run)
    uploader = RecordingUploader(retry_after=5.0)
    worker = SubmitWorker(
        pipeline,
        run,
        {"schema_version": 1, "task": "detect"},
        store=Store(""),
        client=object(),
        uploader=uploader,
    )
    seen = []
    failed = []
    worker.submitted.connect(seen.append)
    worker.failed.connect(failed.append)
    worker.start()
    pump(qapp, 200)
    worker.cancel()
    assert worker.wait(5000) is True
    pump(qapp, 100)
    assert seen == [], "a cancelled submit must not emit submitted"
    assert failed == []
    assert worker.outcome.cancelled is True


def test_r3_closing_submit_result_does_not_open_a_page(qapp, tmp_path):
    """R3: a late submit result must not start a new polling thread."""

    store = Store(str(tmp_path / "ledger"))
    seed(store)
    client = ScriptedClient()
    window = open_window(Parent(), store, client)
    window.show_config()
    assert window.poll_worker is None
    window._closing = True
    outcome = SimpleNamespace(
        ok=True, job_id=JOB_ID, warnings=[], resumed_after_429=False
    )
    window._on_submitted(outcome, None)
    assert window.detail_page.job_id != JOB_ID
    assert window.poll_worker is None
    assert "正在关闭" in window.results_page.debug_text()
