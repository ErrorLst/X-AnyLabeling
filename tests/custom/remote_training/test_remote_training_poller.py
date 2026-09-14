"""Offline regression net for the monitoring step (spec §5.5, §5.6.4).

Everything here runs without a server and without Qt: the HTTP layer is
a fake client, the ledger lives in tmp_path and the clock is never slept
on.  The acceptance items owned by this step are written above the test
that executes them:

CT16 CT23 CT35 CT36 CT37 CT38 CT39 CT40 CT43 CT45 (this file),
CT17 CT24 CT41 (test_remote_training_worker.py).
"""

from __future__ import annotations

import json
import time

import pytest

from anylabeling.custom.remote_training import api_client as api
from anylabeling.custom.remote_training import poller as poll_mod
from anylabeling.custom.remote_training import store as store_mod
from anylabeling.custom.remote_training.poller import (
    CLIENT_ERROR_TABLE,
    DETAIL_INTERVAL,
    ERROR_CODE_COUNT,
    ERROR_PAIR_COUNT,
    FALLBACK_INTERVAL,
    IDLE_INTERVAL,
    JOBS_INTERVAL,
    RESULTS_INTERVAL,
    FailureTracker,
    Scheduler,
    apply_event,
    batch_ids,
    cancel_grace_seconds,
    cancel_message,
    cancel_outcome,
    client_error_view,
    command_outcome,
    files_from_payload,
    is_terminal,
    looks_terminal_by_model,
    merge_job_into_ledger,
    merge_jobs,
    next_interval,
    resume_mode,
    resume_outcome,
    terminal_from_finished_at,
)
from anylabeling.custom.remote_training.store import Store, TaskRecord


# --------------------------------------------------------------- helpers


def job(job_id="job_20260101_000001", status="running", **overrides):
    """One job object of spec §3.4.4 (only the fields a test needs).

    An override set to None omits the key entirely, which is how a
    field the server does not send (the fallback channel case) is
    expressed.
    """

    payload = {
        "job_id": job_id,
        "status": status,
        "is_terminal": False,
        "attempt": 1,
        "max_attempts": 3,
        "resume_cycles": 0,
        "needs_attention": False,
        "needs_attention_reason": None,
        "artifact_suspect": False,
        "partial_available": False,
        "resume_mode_available": [],
        "progress": {"epoch": 3, "total_epochs": 100, "percent": 3.0},
        "metrics": {"mAP50": 0.5},
        "queued_reason": None,
        "created_at": "2026-01-01T10:00:00Z",
        "finished_at": None,
    }
    for key, value in overrides.items():
        if value is None:
            payload.pop(key, None)
            continue
        if isinstance(value, dict) and isinstance(payload.get(key), dict):
            merged = dict(payload[key])
            merged.update(value)
            payload[key] = merged
            continue
        payload[key] = value
    return payload


def row(job_id, **overrides):
    """One ledger row (spec §5.3.2)."""

    defaults = {
        "job_id": job_id,
        "client_job_name": "job-" + job_id[-6:],
        "server_url": "http://server:8000",
        "status": "running",
        "is_terminal": False,
        "attempt": 1,
    }
    defaults.update(overrides)
    return TaskRecord(**defaults)


class FakeClient:
    """A scriptable stand in for RemoteTrainingClient (routes 8 to 13)."""

    def __init__(self, jobs=None, events=None, files=None):
        self.jobs = list(jobs or [])
        self.events = list(events or [])
        self.files = list(files or [])
        self.detail_error = None
        self.list_error = None
        self.events_error = None
        self.calls = []
        self.detail_calls = 0
        self.events_calls = 0
        self.files_calls = 0

    def list_jobs(self, ids=None, status=None, limit=50):
        self.calls.append(("list_jobs", list(ids or []), int(limit)))
        if self.list_error is not None:
            raise self.list_error
        wanted = list(ids or [])
        by_id = {item["job_id"]: item for item in self.jobs}
        found = [by_id[item] for item in wanted if item in by_id]
        missing = [item for item in wanted if item not in by_id]
        return {"jobs": found, "not_found_ids": missing}

    def get_job(self, job_id):
        self.detail_calls += 1
        if self.detail_error is not None:
            raise self.detail_error
        for item in self.jobs:
            if item["job_id"] == job_id:
                return dict(item)
        raise api.JobNotFoundError(404, "JOB_NOT_FOUND", "gone")

    def get_events(self, job_id, after=0):
        self.events_calls += 1
        if self.events_error is not None:
            raise self.events_error
        fresh = [
            item for item in self.events
            if int(item.get("seq", 0)) > int(after)
        ]
        highest = max(
            [int(item.get("seq", 0)) for item in self.events] or [0]
        )
        return {"events": fresh, "last_seq": highest}

    def list_job_files(self, job_id, include_partial=True):
        self.files_calls += 1
        return {"files": list(self.files)}


@pytest.fixture
def workdir(tmp_path):
    """A Store whose ledger is the only writable place."""

    return Store(str(tmp_path / "ledger"))


def seed(store, *records):
    ledger = store.load_ledger()
    for record in records:
        ledger.upsert_record(record)
    store.save_ledger(ledger)
    return ledger


def scheduler(store, client, **kwargs):
    return Scheduler(
        store,
        list_fn=client.list_jobs,
        detail_fn=client.get_job,
        events_fn=client.get_events,
        files_fn=client.list_job_files,
        **kwargs,
    )

# ------------------------------------------- the one terminal formula


def test_terminal_formula_prefers_the_authoritative_field():
    """The only formula of spec §5.5.4."""

    assert is_terminal(job(finished_at="2026-01-01T11:00:00Z")) is False
    assert looks_terminal_by_model(
        job(finished_at="2026-01-01T11:00:00Z")
    ) is False
    assert is_terminal(job(status="failed", finished_at=None)) is False
    assert is_terminal(
        job(status="failed", is_terminal=False, finished_at=None)
    ) is False
    assert is_terminal(
        job(status="failed", is_terminal=True,
            finished_at="2026-01-01T11:00:00Z")
    ) is True
    assert is_terminal(
        job(status="cancelled", is_terminal=True,
            finished_at="2026-01-01T11:00:00Z")
    ) is True


def test_terminal_formula_falls_back_to_finished_at():
    """An old server without the field: finished_at is the fallback."""

    old = {"job_id": "job_1", "status": "interrupted",
           "finished_at": "2026-01-01T11:00:00Z"}
    assert looks_terminal_by_model(old) is None
    assert terminal_from_finished_at(old) is True
    assert is_terminal(old) is True
    assert is_terminal({"job_id": "job_2", "status": "running",
                        "finished_at": None}) is False


def test_terminal_formula_rejects_the_forbidden_spellings():
    """A requeued failed job stays active; a requeued interrupted too."""

    requeued_failed = job(status="failed", is_terminal=False, attempt=2)
    assert is_terminal(requeued_failed) is False
    requeued_interrupted = job(status="queued", is_terminal=False)
    assert is_terminal(requeued_interrupted) is False
    assert poll_mod.stop_reason(requeued_failed) == "auto_retry"
    assert poll_mod.stop_reason(requeued_interrupted) == "requeued"
    assert poll_mod.stop_reason(
        job(status="completed", is_terminal=True,
            finished_at="2026-01-01T11:00:00Z")
    ) == "terminal"


def test_next_interval_table():
    """Spec §5.5.1 / §5.5.2 intervals, including the shared 60 s tier."""

    assert next_interval(poll_mod.PAGE_JOBS) == JOBS_INTERVAL
    assert next_interval(poll_mod.PAGE_JOBS, jobs_interval=5) == 5.0
    assert next_interval(poll_mod.PAGE_JOBS, jobs_interval=600) == 60.0
    assert next_interval(poll_mod.PAGE_JOBS, jobs_interval=1) == 5.0
    assert next_interval(poll_mod.PAGE_DETAIL) == DETAIL_INTERVAL
    assert next_interval(poll_mod.PAGE_RESULTS) == RESULTS_INTERVAL
    assert next_interval(
        poll_mod.PAGE_DETAIL, terminal=True
    ) == FALLBACK_INTERVAL
    assert next_interval(
        poll_mod.PAGE_RESULTS, terminal=True
    ) == FALLBACK_INTERVAL
    assert next_interval(poll_mod.PAGE_DETAIL, idle=True) == IDLE_INTERVAL
    assert FALLBACK_INTERVAL == IDLE_INTERVAL == 60.0


def test_interval_from_the_ledger_row():
    """The row keeps the authoritative flag; the fallback is for old rows."""

    assert poll_mod.job_interval(
        poll_mod.PAGE_DETAIL, row("j1", is_terminal=True)
    ) == FALLBACK_INTERVAL
    assert poll_mod.job_interval(
        poll_mod.PAGE_DETAIL, row("j2", is_terminal=False)
    ) == DETAIL_INTERVAL
    old = TaskRecord.from_dict({
        "job_id": "j3", "status": "interrupted",
        "finished_at": "2026-01-01T11:00:00Z",
    })
    assert poll_mod.close_by_model(old) is False
    assert poll_mod.job_interval(
        poll_mod.PAGE_RESULTS, old
    ) == FALLBACK_INTERVAL

# -------------------------------------------------- CT37 / CT45 ladder


def test_ct37_ct45_ladder_and_kind_alternation():
    """CT37 silent resend and the 5-10-20-30 s ladder.

    CT45: the wait is derived from the consecutive failure count only, so
    alternating 5xx and connection errors never resets the tier.
    """

    tracker = FailureTracker()
    waits = []
    kinds = []
    sequence = [
        api.InternalServerError(500, "INTERNAL_ERROR", "boom"),
        api.TransportError("connection refused"),
        api.ServerUnavailableError(502, "", "bad gateway"),
        api.TransportError("timeout"),
        api.TransportError("timeout"),
        api.TransportError("timeout"),
    ]
    bars = []
    for error in sequence:
        tracker.record_failure(error)
        waits.append(tracker.wait_seconds())
        kinds.append(tracker.state.last_kind)
        bars.append(tracker.state.red_bar)
    assert waits == [0.0, 5.0, 10.0, 20.0, 30.0, 30.0]
    # Only the tail of the sequence has three no-response failures in a row,
    # so the red bar appears there and nowhere earlier (spec §5.5.6).
    assert bars == [False, False, False, False, False, True]
    assert kinds[0] == "http" and kinds[1] == "no_response"
    # The ladder itself is the api_client one, not a second table.
    for count, expected in enumerate(waits, start=1):
        assert poll_mod.backoff_wait(count) == expected


def test_ct37_red_bar_threshold_and_clearing():
    """CT37 three consecutive no-response failures show the red bar."""

    tracker = FailureTracker()
    tracker.record_failure(api.TransportError("a"))
    tracker.record_failure(api.TransportError("b"))
    assert tracker.red_bar is False
    tracker.record_failure(api.TransportError("c"))
    assert tracker.red_bar is True
    assert tracker.status_lines() == [("red", poll_mod.RED_BAR_TEXT)]

    # A 5xx is an HTTP response: it hides the red bar but keeps the tier.
    tracker.record_failure(
        api.InternalServerError(500, "INTERNAL_ERROR", "x")
    )
    assert tracker.red_bar is False
    assert tracker.consecutive_failures == 4
    assert tracker.wait_seconds() == 20.0
    assert tracker.status_lines() == [
        ("yellow", poll_mod.SERVER_RETRY_TEMPLATE.format(500))
    ]

    # A 2xx resets the tier; a non 401 4xx does the same.
    assert tracker.record_response(200) is True
    assert tracker.consecutive_failures == 0
    tracker.record_failure(
        api.InternalServerError(500, "INTERNAL_ERROR", "x")
    )
    tracker.record_failure(
        api.InternalServerError(500, "INTERNAL_ERROR", "x")
    )
    assert tracker.wait_seconds() == 5.0
    assert tracker.record_response(404) is True
    assert tracker.consecutive_failures == 0


class TimingClient(FakeClient):
    """A FakeClient that records the clock of every job GET."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.times = []

    def get_job(self, job_id):
        self.times.append(time.monotonic())
        return super().get_job(job_id)


def test_ct37_first_failure_is_resent_inside_the_tick(workdir):
    """CT37: the first failure waits 0 s and resends the same request.

    The evidence is the request clock, not the outcome interval: the two
    attempts must belong to the same tick, back to back.
    """

    jid = "job_20260101_000001"
    client = TimingClient(jobs=[job(jid)])
    client.detail_error = api.TransportError("connection refused")
    seed(workdir, row(jid, status="running", is_terminal=False))
    sched = scheduler(workdir, client)
    outcome = sched.tick(poll_mod.PAGE_DETAIL, job_id=jid)
    assert len(client.times) == 2, "attempt + silent resend in one tick"
    assert client.times[1] - client.times[0] < 0.5, "no wait between them"
    # The tick ends on the second failure, so the ladder value only takes
    # over from c == 2 (spec §5.5.6).
    assert outcome.failures.consecutive_failures == 2
    assert outcome.interval == 5.0
    assert outcome.interval == poll_mod.backoff_wait(2)


def test_ct37_the_first_5xx_is_also_resent(workdir):
    """The silent resend covers the 5xx row too (spec §5.5.6)."""

    jid = "job_20260101_000001"
    client = TimingClient(jobs=[job(jid)])
    client.detail_error = api.InternalServerError(
        500, "INTERNAL_ERROR", "boom"
    )
    seed(workdir, row(jid, status="running", is_terminal=False))
    sched = scheduler(workdir, client)
    outcome = sched.tick(poll_mod.PAGE_DETAIL, job_id=jid)
    assert len(client.times) == 2
    assert outcome.failures.consecutive_failures == 2
    assert outcome.failures.last_kind == "http"
    assert outcome.failures.red_bar is False
    assert outcome.interval == 5.0


def test_ct37_a_successful_resend_is_silent(workdir):
    """A one-off blip never reaches the page (spec §5.5.6)."""

    jid = "job_20260101_000001"
    client = TimingClient(jobs=[job(jid)])
    attempts = {"count": 0}

    def flaky(job_id):
        client.times.append(time.monotonic())
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise api.TransportError("blip")
        return dict(client.jobs[0])

    client.get_job = flaky
    seed(workdir, row(jid, status="running", is_terminal=False))
    sched = scheduler(workdir, client)
    outcome = sched.tick(poll_mod.PAGE_DETAIL, job_id=jid)
    assert len(client.times) == 2
    assert client.times[1] - client.times[0] < 0.5
    assert outcome.results, "the resend delivered the job"
    assert outcome.error_view is None
    assert outcome.status_lines() == []
    assert outcome.failures.consecutive_failures == 0
    assert outcome.red_bar is False
    assert outcome.interval == DETAIL_INTERVAL


def test_ct37_ladder_advances_from_the_second_failure(workdir):
    """CT37 / CT45: 5 -> 10 -> 20 -> 30 s starting at c == 2."""

    jid = "job_20260101_000001"
    client = TimingClient(jobs=[job(jid)])
    client.detail_error = api.TransportError("down")
    seed(workdir, row(jid, status="running", is_terminal=False))
    sched = scheduler(workdir, client)
    expected = [5.0, 10.0, 20.0, 30.0, 30.0]
    for index, wait in enumerate(expected, start=2):
        outcome = sched.tick(poll_mod.PAGE_DETAIL, job_id=jid)
        assert outcome.failures.consecutive_failures == index
        assert outcome.interval == wait
        assert outcome.interval == poll_mod.backoff_wait(index)
    # Only the very first tick carried the silent resend.
    assert len(client.times) == 2 + len(expected) - 1


def test_ct37_a_failed_sub_request_takes_the_ladder(workdir):
    """N1: the detail tick takes the max with the failure ladder.

    A failed events request is counted by the same schedule as the job
    request, so the 3 s detail cadence must not beat the 5 s position of
    the second consecutive failure (spec §5.5.6).
    """

    jid = "job_20260101_000001"
    client = FakeClient(jobs=[job(jid, status="running")])
    client.events_error = api.TransportError("events down")
    seed(workdir, row(jid, status="running", is_terminal=False))
    sched = scheduler(workdir, client)
    first = sched.tick(poll_mod.PAGE_DETAIL, job_id=jid)
    # The event call is silently resent once (c == 1 -> c == 2).
    assert client.events_calls == 2
    assert first.failures.consecutive_failures == 2
    assert first.interval == 5.0
    assert first.interval == poll_mod.backoff_wait(2)

    # A successful tick is unchanged: the ladder value is 0 there.
    client.events_error = None
    second = sched.tick(poll_mod.PAGE_DETAIL, job_id=jid)
    assert second.failures.consecutive_failures == 0
    assert second.interval == DETAIL_INTERVAL


def test_manual_resume_event_without_counters_keeps_the_ledger(workdir):
    """A mode-only manual_resume must not null the two counters (§5.5.3)."""

    jid = "job_20260101_000001"
    # The response is silent about the two counters (an old server / a
    # partial payload), so the ledger values survive the merge and the
    # event is the only writer that could null them.
    client = FakeClient(
        jobs=[job(jid, status="running", attempt=None, resume_cycles=None)],
        events=[
            {"seq": 1, "ts": "t1", "type": "manual_resume",
             "data": {"mode": "resume"}},
        ],
    )
    seed(workdir, row(jid, status="running", is_terminal=False,
                      attempt=2, resume_cycles=1))
    sched = scheduler(workdir, client)
    outcome = sched.tick(poll_mod.PAGE_DETAIL, job_id=jid)
    assert outcome.results[0].manual_resume_event is True
    record = workdir.load_ledger().record(jid)
    assert record.attempt == 2
    assert record.resume_cycles == 1
    assert record.manual_resume == {"mode": "resume"}


def test_401_keeps_the_tier_but_stops(workdir):
    """401 is one of the two terminal exits, not a reset (spec §5.5.6)."""

    tracker = FailureTracker()
    tracker.record_failure(api.TransportError("down"))
    assert tracker.record_response(401) is False
    assert tracker.consecutive_failures == 1
    client = FakeClient(jobs=[job()])
    client.detail_error = api.UnauthorizedError(
        401, "UNAUTHORIZED", "bad"
    )
    seed(workdir, row("job_20260101_000001"))
    sched = scheduler(workdir, client)
    outcome = sched.tick(
        poll_mod.PAGE_DETAIL, job_id="job_20260101_000001"
    )
    assert outcome.unauthorized is True
    assert outcome.stopped is True
    assert outcome.status_lines() == [("red", poll_mod.UNAUTHORIZED_TEXT)]

# ------------------------------------------------ CT16 / CT23 / CT39


def test_ct16_terminal_interrupted_stops_high_frequency(workdir):
    """CT16: auto_resume=false and a non null finished_at is terminal."""

    terminal = job(
        "job_20260101_000001",
        status="interrupted",
        is_terminal=True,
        finished_at="2026-01-01T11:00:00Z",
    )
    client = FakeClient(jobs=[terminal])
    seed(workdir, row("job_20260101_000001", status="running",
                      is_terminal=False))
    sched = scheduler(workdir, client)

    detail = sched.tick(
        poll_mod.PAGE_DETAIL, job_id="job_20260101_000001"
    )
    assert detail.interval == FALLBACK_INTERVAL
    assert client.events_calls == 0
    assert is_terminal(terminal) is True

    results = sched.tick(
        poll_mod.PAGE_RESULTS, job_id="job_20260101_000001"
    )
    assert results.interval == FALLBACK_INTERVAL
    assert client.files_calls == 0

    listing = sched.tick(
        poll_mod.PAGE_JOBS, active=["job_20260101_000001"]
    )
    assert listing.idle is True
    assert listing.interval == IDLE_INTERVAL
    assert listing.batches == 1


def test_ct23_three_pages_share_the_formula(workdir):
    """CT23: three cases, one formula, and the transition back to false."""

    jid = "job_20260101_000001"
    cases = [
        (job(jid, status="failed", is_terminal=False, attempt=2),
         False, DETAIL_INTERVAL),
        (job(jid, status="interrupted", is_terminal=True,
             finished_at="2026-01-01T11:00:00Z"),
         True, FALLBACK_INTERVAL),
    ]
    for index, (payload, expected, interval) in enumerate(cases):
        store = Store(str(workdir.base_dir) + "-case" + str(index))
        client = FakeClient(jobs=[payload])
        seed(store, row(jid, status="running", is_terminal=False))
        sched = scheduler(store, client)
        assert is_terminal(payload) is expected
        detail = sched.tick(poll_mod.PAGE_DETAIL, job_id=jid)
        assert detail.interval == interval
        listing = sched.tick(poll_mod.PAGE_JOBS, active=[jid])
        expected_list = IDLE_INTERVAL if expected else JOBS_INTERVAL
        assert listing.interval == expected_list

    # Manual resume: the ledger says terminal, the server says queued.
    store = Store(str(workdir.base_dir) + "-resume")
    resumed = job(jid, status="queued", is_terminal=False)
    client = FakeClient(jobs=[resumed])
    seed(store, row(jid, status="interrupted", is_terminal=True,
                    finished_at="2026-01-01T11:00:00Z"))
    sched = scheduler(store, client)
    detail = sched.tick(poll_mod.PAGE_DETAIL, job_id=jid)
    assert detail.interval == DETAIL_INTERVAL
    assert detail.results[0].transitioned is True
    assert store.load_ledger().record(jid).is_terminal is False


def test_ct39_external_resume_and_the_single_files_resend(workdir):
    """CT39: the fallback channel finds an external resume within 60 s."""

    jid = "job_20260101_000001"
    terminal = job(jid, status="cancelled", is_terminal=True,
                   finished_at="2026-01-01T11:00:00Z")
    listed = [{"file_id": "f_1", "path": "weights/best.pt"}]
    client = FakeClient(jobs=[terminal], files=listed)
    seed(workdir, row(jid, status="cancelled", is_terminal=True,
                      finished_at="2026-01-01T11:00:00Z"))
    sched = scheduler(workdir, client)

    # Fallback tick of the results page: the job object only, no files.
    quiet = sched.tick(poll_mod.PAGE_RESULTS, job_id=jid)
    assert quiet.interval == FALLBACK_INTERVAL
    assert client.files_calls == 0
    assert quiet.results[0].transitioned is False

    # The external resume puts the job back to queued with finished_at
    # null: the very tick that judges the transition pulls files once.
    client.jobs = [job(jid, status="queued", is_terminal=False)]
    resumed = sched.tick(poll_mod.PAGE_RESULTS, job_id=jid)
    assert resumed.results[0].transitioned is True
    assert resumed.results[0].files == listed
    assert client.files_calls == 1
    assert resumed.interval == RESULTS_INTERVAL
    record = workdir.load_ledger().record(jid)
    assert record.is_terminal is False
    assert record.status == "queued"


def test_files_from_payload_shapes():
    """The one manifest parser: the tick and the explicit read agree.

    The polling tick and the explicit read of a terminal job
    (worker.ManifestWorker) share this function, so the body of route 13
    renders one and the same table whichever path fetched it.  A bare
    list is the rows too; every other shape - a missing key, a string,
    null - is an empty list and never an exception (spec §3.2.2 #13
    promises the shape, and a violation must not read as "no
    artifacts").
    """

    rows = [{"file_id": "f_1", "path": "weights/best.pt", "partial": False}]
    mapping = {"files": rows}
    parsed = files_from_payload(mapping)
    assert parsed == rows
    assert parsed is not rows, "the payload list must be copied, not shared"
    assert parsed is not mapping["files"]
    parsed.append({"file_id": "f_2"})
    assert mapping["files"] == rows
    assert files_from_payload({"files": tuple(rows)}) == rows

    assert files_from_payload(rows) == rows
    assert files_from_payload(rows) is not rows
    assert files_from_payload(()) == []

    assert files_from_payload({}) == []
    assert files_from_payload(None) == []
    assert files_from_payload("str") == []
    assert files_from_payload({"files": None}) == []
    assert files_from_payload({"files": "str"}) == []


def test_ct39_fallback_whitelist_keeps_the_other_fields(workdir):
    """The fallback channel may only update the seven whitelisted fields."""

    record = row(
        "job_1",
        status="cancelled",
        is_terminal=True,
        attempt=2,
        needs_attention=True,
        needs_attention_reason="attempts_exhausted",
    )
    payload = job(
        "job_1",
        status="queued",
        is_terminal=False,
        needs_attention=False,
        finished_at=None,
    )
    payload.pop("attempt")
    merged = merge_job_into_ledger(
        record, payload, partial_fields=poll_mod.TRANSITION_FIELDS
    )
    assert merged.status == "queued"
    assert merged.is_terminal is False
    assert merged.partial_available is False
    # The whitelist is exhaustive: a field the server did not send keeps
    # no value at all, so the high frequency pass cannot leak into it.
    assert merged.finished_at is None
    # attempt was not part of this response at all: the whitelist leaves
    # the ledger value alone instead of zeroing it out.
    assert merged.attempt == 2
    assert merged.needs_attention is False
    # The ledger row itself is untouched (the merge returns a new row).
    assert record.finished_at is None
    assert record.attempt == 2

# -------------------------------------------------------- CT35 events


def test_ct35_event_increments_and_manual_resume(workdir):
    """CT35: after= / last_seq, the write back and the unknown type."""

    jid = "job_20260101_000001"
    events = [
        {"seq": 1, "ts": "t1", "type": "progress",
         "data": {"epoch": 1, "total_epochs": 100, "percent": 1.0}},
        {"seq": 2, "ts": "t2", "type": "metrics", "data": {"mAP50": 0.1}},
        {"seq": 3, "ts": "t3", "type": "log",
         "data": {"level": "warning", "message": "slow"}},
        {"seq": 4, "ts": "t4", "type": "done",
         "data": {"status": "failed", "exit_code": 1, "attempt": 2,
                  "resume_cycles": 0}},
        {"seq": 5, "ts": "t5", "type": "manual_resume",
         "data": {"attempt": 1, "mode": "resume", "resume_cycles": 1}},
        {"seq": 6, "ts": "t6", "type": "brand_new", "data": {"x": 1}},
    ]
    client = FakeClient(jobs=[job(jid, status="running")], events=events)
    seed(workdir, row(jid, status="running", is_terminal=False, last_seq=0))
    sched = scheduler(workdir, client)

    first = sched.tick(poll_mod.PAGE_DETAIL, job_id=jid)
    result = first.results[0]
    assert [item["seq"] for item in result.events] == [1, 2, 3, 4, 5, 6]
    assert result.unknown_types == ["brand_new"]
    assert result.done_event is True
    assert result.manual_resume_event is True
    record = workdir.load_ledger().record(jid)
    assert record.last_seq == 6
    assert record.attempt == 1
    assert record.resume_cycles == 1
    assert record.manual_resume == {
        "attempt": 1, "resume_cycles": 1, "mode": "resume"
    }

    # The next tick asks for after=6 and gets nothing back: no duplicate.
    second = sched.tick(poll_mod.PAGE_DETAIL, job_id=jid)
    assert second.results[0].events == []
    assert workdir.load_ledger().record(jid).last_seq == 6
    assert workdir.load_ledger().record(jid).attempt == 1


def test_ct35_dedup_never_replays_a_seen_seq(workdir):
    """A server that repeats an event cannot double print it."""

    jid = "job_20260101_000001"
    events = [
        {"seq": 1, "ts": "t", "type": "log", "data": {"message": "a"}},
        {"seq": 1, "ts": "t", "type": "log", "data": {"message": "a"}},
        {"seq": 2, "ts": "t", "type": "log", "data": {"message": "b"}},
    ]
    client = FakeClient(jobs=[job(jid)], events=events)
    seed(workdir, row(jid, last_seq=1))
    sched = scheduler(workdir, client)
    outcome = sched.tick(poll_mod.PAGE_DETAIL, job_id=jid)
    assert [item["seq"] for item in outcome.results[0].events] == [2]
    assert workdir.load_ledger().record(jid).last_seq == 2


def test_ct35_unknown_event_degrades_to_a_log_line():
    """CT35: an unknown type is neither dropped nor an error."""

    unknown = {"seq": 9, "ts": "t", "type": "shiny_new",
               "data": {"a": 1, "b": [2]}}
    kind, text = poll_mod.event_display(unknown)
    assert kind == "log"
    assert json.loads(text) == {"a": 1, "b": [2]}
    kind, text = poll_mod.event_display(
        {"seq": 1, "type": "log",
         "data": {"level": "info", "message": "hi"}}
    )
    assert kind == "log" and text == "hi"
    kind, text = poll_mod.event_display(
        {"seq": 1, "type": "manual_resume",
         "data": {"attempt": 1, "mode": "resume", "resume_cycles": 2}}
    )
    assert "第 2 次人工恢复" in text
    assert "mode=resume" in text
    kind, text = poll_mod.event_display(
        {"seq": 1, "type": "done",
         "data": {"status": "completed", "exit_code": 0}}
    )
    assert kind == "done" and "completed" in text


def test_manual_resume_event_writes_the_ledger_row():
    """The event carries the write back (spec §5.5.3)."""

    record = row("j1", attempt=3, resume_cycles=0)
    updated = apply_event(record, {
        "seq": 7, "type": "manual_resume",
        "data": {"attempt": 1, "mode": "restart", "resume_cycles": 1},
    })
    assert updated.attempt == 1
    assert updated.resume_cycles == 1
    assert updated.manual_resume == {
        "attempt": 1, "resume_cycles": 1, "mode": "restart"
    }
    # The original row is untouched (replace(), never a mutation).
    assert record.attempt == 3 and record.resume_cycles == 0
    # A non manual_resume event changes nothing.
    same = apply_event(record, {"seq": 8, "type": "log", "data": {}})
    assert same is record

# ------------------------------------------------- CT36 batch query


def test_ct36_batches_are_chunked_by_limit():
    """CT36: every batch is at most limit long, order preserved."""

    ids = ["job_{0:03d}".format(index) for index in range(120)]
    chunks = batch_ids(ids, 50)
    assert [len(chunk) for chunk in chunks] == [50, 50, 20]
    assert [item for chunk in chunks for item in chunk] == ids
    assert [len(chunk) for chunk in batch_ids(ids, 200)] == [120]
    with pytest.raises(ValueError):
        batch_ids(ids, 0)


def test_ct36_merge_order_and_not_found_only():
    """CT36: request order merge; not_found_ids[] is the only orphan."""

    merged, orphans = merge_jobs(
        [job("job_a"), job("job_b")],
        orphaned=["job_c"],
    )
    assert [item["job_id"] for item in merged] == ["job_a", "job_b"]
    assert orphans == ["job_c"]
    # Absent from the response is not an orphan on its own.
    merged, orphans = merge_jobs([job("job_a")])
    assert [item["job_id"] for item in merged] == ["job_a"]
    assert orphans == []


def test_ct36_list_page_marks_orphans_and_reports_oversize(workdir):
    """CT36: 120 rows, three batches, orphans marked, no truncation."""

    ids = ["job_{0:03d}".format(index) for index in range(120)]
    payload = [job(item) for item in ids[:100]]
    client = FakeClient(jobs=payload)
    for item in ids:
        seed(workdir, row(item))
    sched = scheduler(workdir, client)
    outcome = sched.tick(poll_mod.PAGE_JOBS, active=ids)
    assert outcome.batches == 3
    assert len(client.calls) == 3
    assert [call[2] for call in client.calls] == [50, 50, 50]
    assert [len(call[1]) for call in client.calls] == [50, 50, 20]
    assert [item.job_id for item in outcome.results] == ids[:100]
    assert outcome.orphaned == ids[100:]
    ledger = workdir.load_ledger()
    for item in ids[100:]:
        assert ledger.record(item).status == (
            store_mod.RECORD_STATUS_ORPHANED
        )
    for item in ids[:100]:
        assert ledger.record(item).status == "running"

    # An oversized batch answers 400 VALIDATION_FAILED: the client never
    # truncates silently, it surfaces the error and keeps the limit.
    over = api.ValidationFailedError(
        400,
        "VALIDATION_FAILED",
        "too many ids",
        {"field": "ids", "limit": 50, "provided": 120},
    )
    client.list_error = over
    bad = sched.tick(poll_mod.PAGE_JOBS, active=ids)
    assert bad.error is over
    assert bad.error_view.code == "VALIDATION_FAILED"
    assert "请求体校验失败" in bad.error_view.text
    assert bad.error_view.details["provided"] == 120
    assert client.calls[-1][2] == 50


def test_list_batch_failure_keeps_the_tier(workdir):
    """A batch failure resends in-tick, then advances the ladder (§5.5.6)."""

    ids = ["job_{0:03d}".format(index) for index in range(60)]
    for item in ids:
        seed(workdir, row(item))
    client = FakeClient(jobs=[job(item) for item in ids])
    client.list_error = api.TransportError("down")
    sched = scheduler(workdir, client)
    first = sched.tick(poll_mod.PAGE_JOBS, active=ids)
    # One tick = the attempt plus its silent resend: the tick therefore
    # ends at position c == 2, whose ladder value is 5 s.
    assert len(client.calls) == 2
    assert first.failures.consecutive_failures == 2
    assert first.failures.wait_seconds() == 5.0
    assert first.interval == JOBS_INTERVAL  # the 10 s page floor is higher
    second = sched.tick(poll_mod.PAGE_JOBS, active=ids)
    assert len(client.calls) == 3
    assert second.failures.consecutive_failures == 3
    assert second.failures.wait_seconds() == 10.0
    assert second.interval == 10.0
    third = sched.tick(poll_mod.PAGE_JOBS, active=ids)
    assert third.failures.consecutive_failures == 4
    assert third.interval == 20.0
    # A success on the next tick resets the tier and the red bar.
    client.list_error = None
    fourth = sched.tick(poll_mod.PAGE_JOBS, active=ids)
    assert fourth.failures.consecutive_failures == 0
    assert fourth.interval == JOBS_INTERVAL


def test_paused_when_the_window_is_hidden(workdir):
    """Spec §5.5.1: minimised / hidden pauses every page."""

    client = FakeClient(jobs=[job()])
    seed(workdir, row("job_20260101_000001"))
    sched = scheduler(workdir, client)
    outcome = sched.tick(
        poll_mod.PAGE_DETAIL,
        job_id="job_20260101_000001",
        visible=False,
    )
    assert outcome.pauses is True
    assert client.detail_calls == 0
    assert outcome.interval == DETAIL_INTERVAL

# ------------------------------------------------ CT38 cancel / resume


def test_ct38_resume_error_branches():
    """CT38: the three JOB_NOT_RESUMABLE and two EXPIRED reasons."""

    reasons = [
        (None, "该任务当前状态不允许恢复（已完成的任务不能恢复）"),
        ("resume_in_progress", "上一次恢复尚未收尾，请稍后重试"),
        ("lock_timeout", "服务端正忙，请重试"),
        ("process_alive", "该任务仍有存活进程，请先取消或等它结束"),
    ]
    for reason, expected in reasons:
        details = {} if reason is None else {"reason": reason}
        error = api.JobNotResumableError(
            409, "JOB_NOT_RESUMABLE", "no", details
        )
        outcome = resume_outcome("job_a", error=error)
        assert outcome.ok is False
        assert outcome.view.text == expected
        assert outcome.view.severity == "yellow"
        assert outcome.view.auto_retry is False
        assert outcome.refresh_detail is True
        expected_retry = reason in ("resume_in_progress", "lock_timeout")
        assert outcome.retryable is expected_retry

    expired = [
        ("checkpoint_missing",
         "没有可用的训练检查点且服务端未启用自动重训"),
        ("dataset_expired", "数据集已过期或被删除"),
    ]
    for reason, expected in expired:
        error = api.JobArtifactsExpiredError(
            409, "JOB_ARTIFACTS_EXPIRED", "gone", {"reason": reason}
        )
        outcome = resume_outcome("job_a", error=error)
        assert outcome.view.text == expected
        assert outcome.view.refresh_detail is True
        assert outcome.view.auto_retry is False
        assert outcome.retryable is False
    generic = client_error_view(api.JobArtifactsExpiredError(
        409, "JOB_ARTIFACTS_EXPIRED", "x", {}
    ))
    assert "无法恢复" in generic.text


def test_ct38_resume_success_writes_the_response_back():
    """CT38: the response resets the row and clears the badge."""

    outcome = resume_outcome("job_a", {
        "job_id": "job_a",
        "status": "queued",
        "mode": "resume",
        "attempt": 1,
        "resume_cycles": 2,
    })
    assert outcome.ok is True
    assert outcome.mode == "resume"
    assert outcome.attempt == 1
    assert outcome.resume_cycles == 2
    record = row(
        "job_a",
        attempt=3,
        resume_cycles=1,
        needs_attention=True,
        needs_attention_reason="attempts_exhausted",
        artifact_suspect=True,
    )
    record.mark_manual_resume(
        outcome.attempt, outcome.resume_cycles, outcome.mode
    )
    record.needs_attention = False
    record.needs_attention_reason = None
    assert record.attempt == 1
    assert record.resume_cycles == 2
    assert record.needs_attention is False
    assert record.needs_attention_reason is None
    # artifact_suspect survives a resume (it is an artifact property).
    assert record.artifact_suspect is True
    assert record.manual_resume == {
        "attempt": 1, "resume_cycles": 2, "mode": "resume"
    }


def test_cancel_and_resume_outcomes():
    """The cancel path is idempotent; the mode is validated locally."""

    done = cancel_outcome("job_a", {
        "job_id": "job_a",
        "status": "cancelled",
        "partial_available": True,
    })
    assert done.ok is True
    assert done.status == "cancelled"
    assert done.partial_available is True
    assert "已发送取消请求" in done.message()
    again = cancel_outcome("job_a", {"job_id": "job_a",
                                     "status": "cancelled"})
    assert again.ok is True
    assert resume_mode(["restart"]) == "restart"
    assert resume_mode(["resume", "restart"]) == "resume"
    assert resume_mode([]) == ""
    bad = command_outcome("resume", "job_a", error=api.ApiError(
        400,
        "VALIDATION_FAILED",
        "bad mode",
        {"field": "mode", "allowed": ["resume"]},
    ))
    assert bad.view.code == "VALIDATION_FAILED"
    assert "当前只允许 resume" in bad.view.text
    cancel_error = command_outcome(
        "cancel",
        "job_a",
        error=api.InternalServerError(500, "INTERNAL_ERROR", "boom"),
    )
    # A POST is never auto retried, whatever the code.
    assert cancel_error.view.auto_retry is False


def test_cancel_message_uses_the_capabilities_value():
    """The stopping line takes N from capabilities (spec §3.11)."""

    class Caps:
        def __init__(self, value):
            self.value = value

        def require_cancel_grace_seconds(self):
            if self.value is None:
                raise api.MalformedResponseError("missing")
            return self.value

    assert cancel_grace_seconds(Caps(45)) == 45
    assert "最长 45 秒" in cancel_message(Caps(45))
    assert cancel_grace_seconds(None) == 15
    assert "最长 15 秒" in cancel_message(None)
    assert cancel_grace_seconds(api.Capabilities(
        {"cancel_grace_seconds": 30}
    )) == 30
    assert "最长 30 秒" in cancel_message(api.Capabilities(
        {"cancel_grace_seconds": 30}
    ))

# --------------------------------------------- CT37 / CT38 error table


def test_error_table_shape_is_the_spec_shape():
    """29 codes over 30 (HTTP, code) pairs of spec §5.6.4."""

    assert ERROR_CODE_COUNT == 29
    assert ERROR_PAIR_COUNT == 30
    distribution = {}
    for entry in CLIENT_ERROR_TABLE.values():
        for status in entry["http"]:
            distribution[status] = distribution.get(status, 0) + 1
    assert distribution == {
        400: 9,
        401: 1,
        404: 3,
        409: 5,
        413: 1,
        422: 7,
        429: 1,
        500: 1,
        503: 2,
    }
    assert CLIENT_ERROR_TABLE["VALIDATION_FAILED"]["http"] == (400, 409)
    assert set(CLIENT_ERROR_TABLE) == set(api.API_ERROR_TYPES)
    assert CLIENT_ERROR_TABLE["QUOTA_EXCEEDED"]["text"] == (
        "超出服务端配额：服务端已触发自动回收，请稍后重试；"
        "若回收仍无法解除，请联系管理员手工清理服务端数据集"
    )


def test_ct37_every_code_renders_a_message():
    """CT37: one wording per code, taken from the spec §5.6.4 table."""

    for code, entry in CLIENT_ERROR_TABLE.items():
        status = entry["http"][0]
        error = api.map_error(
            status,
            code,
            "server message",
            {"field": "mode", "allowed": ["resume"],
             "reason": "lock_timeout"},
        )
        view = client_error_view(error)
        assert view.code == code
        assert view.text, code
        assert view.http_status == status
        assert view.severity in ("red", "yellow")
        assert view.lines()[0][0] == view.severity


def test_generic_5xx_and_network_rows():
    """The two generic fallback rows of spec §5.6.4 carry no code."""

    network = client_error_view(api.TransportError("refused"))
    assert network.code == ""
    assert network.text == poll_mod.RED_BAR_TEXT
    # The first failure is resent silently, so a safe request does retry it.
    assert network.auto_retry is True
    assert poll_mod.failure_exit(
        api.TransportError("refused"), safe_request=False
    ).auto_retry is False
    view = client_error_view(
        api.ServerUnavailableError(502, "", "bad gateway")
    )
    assert view.code == ""
    assert view.text == "服务端暂时不可用（HTTP 502），正在重试…"
    assert view.auto_retry is True
    internal = client_error_view(api.InternalServerError(
        500, "INTERNAL_ERROR", "boom", {"error_id": "abc123"}
    ))
    assert "错误号 abc123" in internal.text
    assert internal.auto_retry is True


def test_unauthenticated_stops_and_highlights():
    """401 stops the polling and highlights the server settings."""

    view = client_error_view(
        api.UnauthorizedError(401, "UNAUTHORIZED", "x")
    )
    assert view.text == "Token 无效或已过期，请在配置页更新后重试"
    assert view.highlight_settings is True
    assert view.stop_polling is True
    assert view.auto_retry is False
    assert view.lines() == [("red", view.text)]


def test_validation_failed_branches():
    """400 VALIDATION_FAILED branches on details.field."""

    def render(status, details):
        error = api.ValidationFailedError(
            status, "VALIDATION_FAILED", "x", details
        )
        return client_error_view(error).text

    assert render(400, {"field": "split"}) == (
        "上传内容校验失败：split 取值非法，或划分结果某一侧为空"
    )
    assert "只支持 per_class" in render(400, {"field": "split_strategy"})
    assert "当前只允许 resume" in render(
        400, {"field": "mode", "allowed": ["resume"]}
    )
    assert render(400, {}) == "请求体校验失败"
    assert render(
        400, {"field": "files", "files": [{"name": "a.jpg"}]}
    ) == "上传内容校验失败"
    assert render(409, {"field": "upload_token"}) == (
        "该上传凭证已提交过且内容不同，将重新预检并上传"
    )
    assert render(409, {"field": "client_submission_id"}) == (
        "该提交请求已存在且内容不同，请确认后重新提交"
    )


def test_file_lists_are_rendered_entry_by_entry():
    """details.files[] must be listed one by one (spec §5.6.4)."""

    error = api.LabelChecksumMismatchError(
        400,
        "LABEL_CHECKSUM_MISMATCH",
        "bad",
        {"files": [
            {"name": "a.txt", "label": "a.jpg", "reason": "changed",
             "declared": "11", "actual": "22"},
            {"name": "b.txt", "label": "b.jpg", "reason": "changed",
             "declared": "33", "actual": "44"},
        ]},
    )
    view = client_error_view(error)
    lines = view.detail_lines()
    assert len(lines) == 2
    assert "a.txt" in lines[0] and "22" in lines[0]
    assert "b.txt" in lines[1]
    assert view.lines()[0][1] == view.text


def test_post_errors_are_never_auto_retried():
    """spec §5.6.4: a 5xx on a POST is not resent automatically."""

    error = api.InternalServerError(500, "INTERNAL_ERROR", "boom")
    assert client_error_view(error).auto_retry is True
    seen = []
    view = poll_mod.failure_exit(
        error, safe_request=False, on_status=seen.extend
    )
    assert view.auto_retry is False
    assert seen == view.lines()
    assert seen[0][0] == "red"


def test_insufficient_vram_renders_the_numbers():
    """422 INSUFFICIENT_VRAM carries the three capacity numbers."""

    view = client_error_view(api.InsufficientVramError(
        422,
        "INSUFFICIENT_VRAM",
        "no",
        {"reason": "insufficient_capacity", "min_device_total_mb": 8192,
         "vram_estimate_mb": 12000, "reserved_mb": 1024},
    ))
    assert "12000 MB" in view.text
    assert "8192 MB" in view.text
    assert "1024 MB" in view.text
    param = client_error_view(api.ParamOutOfRangeError(
        422, "PARAM_OUT_OF_RANGE", "x", {"field": "batch"}
    ))
    assert param.highlight_field is True
    calibration = client_error_view(api.VramEstimateUnavailableError(
        422,
        "VRAM_ESTIMATE_UNAVAILABLE",
        "x",
        {"reason": "VRAM_CALIBRATION_FAILED"},
    ))
    assert "OOM" in calibration.text


def test_upload_progress_and_capacity_rows():
    """409 UPLOAD_IN_PROGRESS and the 429 capacity carrier fields."""

    view = client_error_view(api.UploadInProgressError(
        409, "UPLOAD_IN_PROGRESS", "busy"
    ))
    assert view.text == "同一上传凭证正在上传中"
    assert view.auto_retry is True
    assert view.retry_after == 5.0
    capacity = client_error_view(api.CommittedTokenCapacityError(
        429,
        "COMMITTED_TOKEN_CAPACITY_EXCEEDED",
        "full",
        {"limit": 10000, "current": 10000, "retry_after_seconds": 30},
    ))
    assert "服务端上传凭证保留区已满" in capacity.text
    assert capacity.auto_retry is True
    assert capacity.details["retry_after_seconds"] == 30


def test_orphan_and_artifact_rows():
    """404 JOB_NOT_FOUND is an orphan; 404 ARTIFACT_NOT_FOUND refreshes."""

    view = client_error_view(api.JobNotFoundError(
        404, "JOB_NOT_FOUND", "x"
    ))
    assert view.mark_orphaned is True
    assert view.text == "服务端已不存在该任务"
    artifact = client_error_view(
        api.ArtifactNotFoundError(404, "ARTIFACT_NOT_FOUND", "x")
    )
    assert artifact.refresh_files is True
    assert "产物文件已不存在" in artifact.text


# --------------------------------------------------- red / yellow tiers


def test_severity_buckets():
    """Red = blocking / broken, yellow = hint (spec §5.6.4)."""

    red = [
        "UNAUTHORIZED",
        "VALIDATION_FAILED",
        "MISSING_LABELS",
        "CHECKSUM_MISMATCH",
        "MANIFEST_MISMATCH",
        "LABEL_CHECKSUM_MISMATCH",
        "INVALID_LABEL_FORMAT",
        "UNSUPPORTED_EXTENSION",
        "TOKEN_EXPIRED",
        "UNKNOWN_UPLOAD_TOKEN",
        "DATASET_NOT_FOUND",
        "JOB_ARTIFACTS_EXPIRED",
        "QUOTA_EXCEEDED",
        "PARAM_OUT_OF_RANGE",
        "PARAM_NOT_OVERRIDABLE",
        "OPTIMIZER_UNSUPPORTED",
        "MODEL_FAMILY_UNSUPPORTED",
        "WEIGHT_NOT_AVAILABLE",
        "INSUFFICIENT_VRAM",
        "VRAM_ESTIMATE_UNAVAILABLE",
        "TRAINING_DISABLED",
        "NO_DEVICE_AVAILABLE",
        "INTERNAL_ERROR",
    ]
    yellow = [
        "JOB_NOT_FOUND",
        "ARTIFACT_NOT_FOUND",
        "DATASET_IN_USE",
        "JOB_NOT_RESUMABLE",
        "UPLOAD_IN_PROGRESS",
        "COMMITTED_TOKEN_CAPACITY_EXCEEDED",
    ]
    assert sorted(red + yellow) == sorted(CLIENT_ERROR_TABLE)
    for code in red:
        assert CLIENT_ERROR_TABLE[code]["severity"] == "red", code
    for code in yellow:
        assert CLIENT_ERROR_TABLE[code]["severity"] == "yellow", code


def test_404_resets_the_tier_and_marks_the_orphan(workdir):
    """D1: a 404 is a 4xx answer: reset the ladder, mark the orphan.

    The detail path must carry the job id into the failure handler even
    though no result has been recorded yet (spec §5.5.5: confirm a 404
    with a per id GET and mark the row orphaned).
    """

    jid = "job_20260101_000001"
    client = FakeClient(jobs=[])
    # Only the detail route is broken here, so a tick counts exactly the
    # detail failures (the events route would add more).
    client.events_error = None
    seed(workdir, row(jid, status="running", is_terminal=False))
    sched = scheduler(workdir, client)

    # Plain connection failures first, so the ladder is not at zero.  The
    # very first tick already carries the silent resend (c == 2).
    client.detail_error = api.TransportError("down")
    first = sched.tick(poll_mod.PAGE_DETAIL, job_id=jid)
    second = sched.tick(poll_mod.PAGE_DETAIL, job_id=jid)
    assert first.failures.consecutive_failures == 2
    assert second.failures.consecutive_failures == 3
    assert second.failures.wait_seconds() == 10.0

    # The server then answers 404 JOB_NOT_FOUND for this id.  A 4xx
    # answer is never silently resent: one request, one failure exit.
    client.detail_error = api.JobNotFoundError(404, "JOB_NOT_FOUND", "gone")
    calls = client.detail_calls
    third = sched.tick(poll_mod.PAGE_DETAIL, job_id=jid)
    assert client.detail_calls == calls + 1
    assert third.error_view.code == "JOB_NOT_FOUND"
    assert third.failures.consecutive_failures == 0
    assert third.failures.wait_seconds() == 0.0
    assert third.red_bar is False
    record = workdir.load_ledger().record(jid)
    assert record.status == store_mod.RECORD_STATUS_ORPHANED
    assert record.notes == poll_mod.ORPHAN_NOTE


def test_old_server_terminal_is_backfilled_and_goes_idle(workdir):
    """D4: a response without is_terminal is backfilled from finished_at."""

    jid = "job_20260101_000001"
    old = {"job_id": jid, "status": "interrupted",
           "finished_at": "2026-01-01T11:00:00Z"}
    client = FakeClient(jobs=[old])
    seed(workdir, row(jid, status="running", is_terminal=False))
    sched = scheduler(workdir, client)

    detail = sched.tick(poll_mod.PAGE_DETAIL, job_id=jid)
    assert detail.interval == FALLBACK_INTERVAL
    record = workdir.load_ledger().record(jid)
    assert record.is_terminal is True
    assert poll_mod.close_by_model(record) is True

    listing = sched.tick(poll_mod.PAGE_JOBS, active=[jid])
    assert listing.idle is True
    assert listing.interval == IDLE_INTERVAL

    # Nothing was invented for a response that does carry the field.
    live = {"job_id": jid, "status": "running", "is_terminal": False,
            "finished_at": None}
    merged = poll_mod.merge_job_into_ledger(record, live)
    assert merged.is_terminal is False
    assert merged.finished_at is None

def test_old_server_external_resume_leaves_the_terminal_state(workdir):
    """N-a: an old server that sends neither field must not stay stuck.

    The fallback channel of an old server may answer with no is_terminal
    and no finished_at; a job that is active again is the external-resume
    signal (spec §5.5.2), so the row must leave is_terminal=true instead
    of re-detecting the same transition on every 60 s tick.
    """

    jid = "job_20260101_000001"
    old_resumed = {"job_id": jid, "status": "queued"}
    record = row(
        jid,
        status="cancelled",
        is_terminal=True,
        finished_at="2026-01-01T11:00:00Z",
    )
    merged = merge_job_into_ledger(
        record, old_resumed, partial_fields=poll_mod.TRANSITION_FIELDS
    )
    assert merged.status == "queued"
    assert merged.is_terminal is False
    assert merged.finished_at is None

    # A response silent about both and still terminal keeps the evidence.
    still_terminal = {"job_id": jid, "status": "cancelled"}
    kept = merge_job_into_ledger(
        record, still_terminal, partial_fields=poll_mod.TRANSITION_FIELDS
    )
    assert kept.is_terminal is True
    assert kept.finished_at == "2026-01-01T11:00:00Z"

    # A modern server is unaffected: the authoritative field rules.
    modern = merge_job_into_ledger(
        record,
        {"job_id": jid, "status": "queued", "is_terminal": False},
        partial_fields=poll_mod.TRANSITION_FIELDS,
    )
    assert modern.is_terminal is False


def test_old_server_resume_returns_to_the_active_interval(workdir):
    """N-a: the next tick of that job is an active one again (3 s)."""

    jid = "job_20260101_000001"
    client = FakeClient(jobs=[{"job_id": jid, "status": "queued"}])
    seed(workdir, row(jid, status="cancelled", is_terminal=True,
                      finished_at="2026-01-01T11:00:00Z"))
    sched = scheduler(workdir, client)
    first = sched.tick(poll_mod.PAGE_DETAIL, job_id=jid)
    assert first.results[0].transitioned is True
    assert first.interval == DETAIL_INTERVAL
    assert workdir.load_ledger().record(jid).is_terminal is False
    second = sched.tick(poll_mod.PAGE_DETAIL, job_id=jid)
    assert second.results[0].transitioned is False
    assert second.interval == DETAIL_INTERVAL
