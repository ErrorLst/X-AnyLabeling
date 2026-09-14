"""Qt carriers of the workflow steps (spec §5.1.4, §5.4 to §5.6).

Seven workers live here, all plain QThread subclasses so the close state
machine of spec §5.1.4 can treat them exactly like the upload worker:

- PollWorker: one polling thread per visible page.  Every wait is an
  Event.wait(timeout) on the single shared cancel Event, so step 3 wakes
  it in milliseconds and step 4 can wait for isFinished() without ever
  killing a thread; the request itself is bounded by the client read
  timeout (spec §5.5.1).
- CommandWorker: one cancel / resume POST (spec §5.5.7).  A POST is never
  auto retried (spec §5.6.4), it only reports its rendered outcome.
- ReconcileWorker: one pass of the B4 pending reconciliation, run when
  the window opens and on every entry into the jobs page (spec §5.4.1).
- SubmitWorker: the whole plan -> pack -> upload -> submit chain of
  spec §5.4, including the 429 UploadSchedule wait, so the GUI thread
  never blocks and every request stays cancellable.
- DownloadWorker: one artifact / zip download streamed to the path the
  user chose (spec §5.6.3), cancellable inside a chunk.
- SummaryWorker: one `summary.json` read for the results page (spec
  §5.6.3), parsed in memory - the feature never unpacks or copies a
  result onto disk.
- ManifestWorker: the one explicit artifact manifest read (route 13)
  of a job the poller no longer pulls it for, because the job was
  already terminal when the results page was opened (spec §5.5.2,
  §5.6.2).

Every worker exposes cancel() plus a cancel Event, which is the hook
pair the dialog close machine looks for.
"""

from __future__ import annotations

import json
import os.path as osp
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from PyQt6 import QtCore

from . import api_client as api
from . import packer as packer_mod
from . import poller as poll_mod
from . import store as store_mod
from .converter import convert_labels
from .poller import PollOutcome, Scheduler
from .scanner import scan_dataset
from .splitter import SplitResult, stats_from_assignments
from .store import Store
from .uploader import ARCHIVE_FILENAME
from .uploader import EntriesReport, PackOutcome, Packer, PlanOutcome
from .uploader import Uploader, UploadOutcome

__all__ = [
    "CommandWorker",
    "DownloadOutcome",
    "DownloadWorker",
    "JobMonitor",
    "ManifestWorker",
    "OPERATION_CANCEL",
    "OPERATION_RESUME",
    "DatasetPacker",
    "PollWorker",
    "ReconcileWorker",
    "SUBMIT_ABORTED_TEXT",
    "SubmitOutcomeView",
    "SubmitWorker",
    "SummaryWorker",
    "cancellable_wait",
    "default_scheduler",
]

OPERATION_CANCEL = "cancel"
OPERATION_RESUME = "resume"

#: Test seam: a run loop longer than this is one tick at the fallback tier.
MIN_WAIT_SECONDS = 0.05

#: Hard cap of one `summary.json` read (spec §5.6.3): the file is a few
#: kB; the cap only stops a wrong file_id from streaming forever.
SUMMARY_MAX_BYTES = 4 * 1024 * 1024


def default_scheduler(
    store: Store,
    client: Any,
    *,
    policy: Optional[api.RetryPolicy] = None,
    **kwargs: Any,
) -> Scheduler:
    """A Scheduler bound to the real client routes (routes 8/9/10/13)."""

    return Scheduler(
        store,
        list_fn=client.list_jobs,
        detail_fn=client.get_job,
        events_fn=client.get_events,
        files_fn=client.list_job_files,
        policy=policy,
        **kwargs,
    )


def cancellable_wait(duration: float, event: threading.Event) -> bool:
    """Wait up to duration; True when the flag was set (spec §5.1.4).

    The single Event of the worker is the only wake up channel: Event.set
    makes Event.wait return immediately, which is what makes the
    cancellation land in milliseconds instead of at the end of a 60 s
    sleep.
    """

    if event is None:
        return False
    if duration <= 0:
        return bool(event.is_set())
    return bool(event.wait(float(duration)))


class PollWorker(QtCore.QThread):
    """Poll one page until cancelled or until a 401 says stop."""

    polled = QtCore.pyqtSignal(object)
    polled_job = QtCore.pyqtSignal(object, object)
    failed = QtCore.pyqtSignal(object)

    def __init__(
        self,
        scheduler: Optional[Scheduler] = None,
        parent: Optional[QtCore.QObject] = None,
        *,
        page: str = poll_mod.PAGE_JOBS,
        job_id: str = "",
        store: Optional[Store] = None,
        client: Optional[Any] = None,
        policy: Optional[api.RetryPolicy] = None,
        active_ids: Optional[Callable[[], Sequence[str]]] = None,
        visible: Optional[Callable[[], bool]] = None,
        interval_min: Optional[float] = None,
        interval_max: Optional[float] = None,
        jobs_interval: float = poll_mod.JOBS_INTERVAL,
        cancel_event: Optional[threading.Event] = None,
        max_ticks: Optional[int] = None,
    ) -> None:
        super().__init__(parent)
        self._cancel_event = cancel_event or threading.Event()
        self._page = page
        self._job_id = str(job_id)
        self._bound_store = store
        self._client = client
        self._active_ids = active_ids
        self._visible = visible
        self._interval_min = interval_min
        self._interval_max = interval_max
        self._jobs_interval = float(jobs_interval)
        self._scheduler = scheduler
        self._policy = policy
        self._max_ticks = max_ticks
        self.ticks = 0
        self.outcome: Optional[PollOutcome] = None
        self.intervals: List[float] = []
        self.error: Optional[BaseException] = None
        self._wake = threading.Event()
        self._limit_lock = threading.Lock()
        #: Set by one_shot(): at most that many ticks, then the thread
        #: ends on its own (used for a "refresh now" worker).
        self._limit: Optional[int] = None

    def one_shot(self, ticks: int = 1) -> "PollWorker":
        """Bound this worker to `ticks` ticks (a one off refresh).

        The limit is only ever tightened, and never to a value that
        would drop a tick already taken, so a caller can call this right
        after `start()`.
        """

        wanted = max(1, int(ticks))
        with self._limit_lock:
            self._limit = wanted
        return self

    # -- close machine hooks -----------------------------------------

    def cancel(self) -> None:
        """Step 3 hook: set the flag, then wake the waiting loop."""

        self._cancel_event.set()
        self._wake.set()

    def should_stop(self) -> bool:
        return self._cancel_event.is_set()

    def wake(self) -> None:
        """Pull once right now (page entered, window shown)."""

        self._wake.set()

    def value(self) -> None:
        """Consume the wake request, if any."""

        self._wake.clear()

    def wait_for(self, duration: float) -> bool:
        """The only wait of this worker (spec §5.5.1)."""

        if self._wake.is_set():
            self._wake.clear()
            return True
        if duration <= 0:
            return False
        remaining = float(duration)
        step = max(MIN_WAIT_SECONDS, min(0.25, remaining))
        while remaining > 0:
            slice_ = min(step, remaining)
            if self._cancel_event.wait(slice_):
                return False
            if self._wake.is_set():
                self._wake.clear()
                return True
            remaining -= slice_
        return False

    # -- execution ---------------------------------------------------

    def scheduler(self) -> Scheduler:
        """Build the scheduler on first use (still on the GUI thread)."""

        if self._scheduler is None:
            store = self._bound_store if self._bound_store is not None \
                else Store()
            # The two interval bounds are the one seam a caller may use to
            # shrink the list page cadence (a test or a future settings
            # box).  They are forwarded only when set, so the scheduler
            # keeps its own spec defaults (spec §5.5.1) otherwise.
            options: Dict[str, Any] = {}
            if self._interval_min is not None:
                options["interval_min"] = float(self._interval_min)
            if self._interval_max is not None:
                options["interval_max"] = float(self._interval_max)
            self._scheduler = default_scheduler(
                store,
                self._client,
                policy=self._policy,
                jobs_interval=self._jobs_interval,
                record=self._record_callback,
                **options,
            )
        return self._scheduler

    def _record_callback(self, record: Any) -> None:
        """Ledger only: never a widget (the close machine disconnects UI)."""

        self.last_record = record

    def run(self) -> None:  # noqa: D102 - QThread entry point
        try:
            scheduler = self.scheduler()
            while not self.should_stop():
                if self._max_ticks is not None and (
                    self.ticks >= int(self._max_ticks)
                ):
                    break
                with self._limit_lock:
                    limit = self._limit
                if limit is not None and self.ticks >= int(limit):
                    break
                visible = True
                if self._visible is not None:
                    try:
                        visible = bool(self._visible())
                    except Exception:  # pragma: no cover - defensive
                        visible = True
                active = None
                if self._active_ids is not None:
                    try:
                        active = list(self._active_ids())
                    except Exception:  # pragma: no cover - defensive
                        active = None
                outcome = scheduler.tick(
                    self._page,
                    job_id=self._job_id,
                    active=active,
                    visible=visible,
                )
                self.ticks += 1
                self.outcome = outcome
                self.intervals.append(float(outcome.interval))
                if not self.should_stop():
                    self.polled.emit(outcome)
                    for result in outcome.results:
                        self.polled_job.emit(outcome, result)
                if self.should_stop():
                    break
                if outcome.stopped:
                    break
                self.wait_for(outcome.interval)
        except Exception as exc:  # noqa: BLE001 - reported to the UI
            self.error = exc
            self.failed.emit(exc)
        finally:
            self._cancel_event.set()


@dataclass
class _CommandRequest:
    """One cancel / resume call as the worker needs it."""

    action: str = OPERATION_CANCEL
    job_id: str = ""
    mode: str = ""


class CommandWorker(QtCore.QThread):
    """One cancel / resume POST, never auto retried (spec §5.6.4)."""

    command_done = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(object)

    def __init__(
        self,
        action: str,
        job_id: str,
        parent: Optional[QtCore.QObject] = None,
        *,
        client: Any = None,
        mode: str = "",
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        super().__init__(parent)
        self._cancel_event = cancel_event or threading.Event()
        self._request = _CommandRequest(
            action=str(action), job_id=str(job_id), mode=str(mode)
        )
        self._client = client
        self.result: Optional[poll_mod.CommandOutcome] = None
        self.error: Optional[BaseException] = None

    @property
    def action(self) -> str:
        return self._request.action

    @property
    def job_id(self) -> str:
        return self._request.job_id

    def cancel(self) -> None:
        """The step 3 hook (a POST already in flight cannot be undone)."""

        self._cancel_event.set()

    def should_stop(self) -> bool:
        return self._cancel_event.is_set()

    def run(self) -> None:  # noqa: D102 - QThread entry point
        request = self._request
        data: Any = None
        error: Optional[BaseException] = None
        try:
            if request.action == OPERATION_CANCEL:
                data = self._client.cancel_job(request.job_id)
            else:
                mode = request.mode or "resume"
                data = self._client.resume_job(request.job_id, mode)
        except Exception as exc:  # noqa: BLE001 - rendered by the table
            error = exc
        try:
            self.result = poll_mod.command_outcome(
                request.action, request.job_id, data, error
            )
        except Exception as exc:  # pragma: no cover - defensive
            self.error = exc
            self.failed.emit(exc)
        self.command_done.emit(self.result)


@dataclass
class DownloadOutcome:
    """What one finished (or cancelled) download reports (spec §5.6.3)."""

    job_id: str = ""
    file_id: str = ""
    path: str = ""
    bytes_written: int = 0
    content_length: Optional[int] = None
    archive: bool = False
    cancelled: bool = False

    @property
    def complete(self) -> bool:
        """True when the received bytes match the response length."""

        if self.content_length is None:
            return not self.cancelled
        return int(self.bytes_written) == int(self.content_length)


class DownloadWorker(QtCore.QThread):
    """Stream one artifact (or the zip) to the chosen path.

    Spec §5.6.3: the file streams to disk, the progress denominator is
    the response's own Content-Length only (None means "show the
    received amount and a busy indicator"), the completion criterion is
    EOF, and v1 sends no Range / If-Range.  The GUI thread never touches
    the socket, so step 3 of the close machine can cancel the transfer
    from the chunk loop.
    """

    progress = QtCore.pyqtSignal(int, object)
    download_done = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(object)

    def __init__(
        self,
        open_stream: Callable[[], Any],
        path: str,
        parent: Optional[QtCore.QObject] = None,
        *,
        job_id: str = "",
        file_id: str = "",
        archive: bool = False,
        cancel_event: Optional[threading.Event] = None,
        chunk_size: int = api.DOWNLOAD_CHUNK_SIZE,
    ) -> None:
        super().__init__(parent)
        #: The route call itself is injected: the worker owns the
        #: socket, the caller owns which route is opened (single file
        #: vs. the zip), exactly like the upload chain injects its
        #: Packer.
        self._open_stream = open_stream
        self._path = str(path or "")
        self._chunk_size = int(chunk_size)
        self._cancel_event = cancel_event or threading.Event()
        self.job_id = str(job_id)
        self.file_id = str(file_id)
        self.archive = bool(archive)
        self.outcome: Optional[DownloadOutcome] = None
        self.error: Optional[BaseException] = None

    def cancel(self) -> None:
        """The step 3 hook: the chunk loop stops within one chunk."""

        self._cancel_event.set()

    def should_stop(self) -> bool:
        return self._cancel_event.is_set()

    def run(self) -> None:  # noqa: D102 - QThread entry point
        if self.should_stop():
            self._finish(self._cancelled())
            return
        try:
            response = self._open_stream()
            with response:
                result = response.save_to(
                    self._path,
                    on_progress=self._emit_progress,
                    cancel=self.should_stop,
                    chunk_size=self._chunk_size,
                )
        except InterruptedError:
            self._finish(self._cancelled())
            return
        except Exception as exc:  # noqa: BLE001 - rendered by the page
            self.error = exc
            self.failed.emit(exc)
            return
        self._finish(
            DownloadOutcome(
                job_id=self.job_id,
                file_id=self.file_id,
                path=result.path,
                bytes_written=result.bytes_written,
                content_length=result.content_length,
                archive=self.archive,
            )
        )

    def _cancelled(self) -> DownloadOutcome:
        return DownloadOutcome(
            job_id=self.job_id,
            file_id=self.file_id,
            path=self._path,
            archive=self.archive,
            cancelled=True,
        )

    def _finish(self, outcome: DownloadOutcome) -> None:
        self.outcome = outcome
        self.download_done.emit(outcome)

    def _emit_progress(self, received: int, total: Optional[int]) -> None:
        self.progress.emit(int(received), total)


class SummaryWorker(QtCore.QThread):
    """Read one `summary.json` for the results page (spec §5.6.3).

    The artifact is addressed by its manifest `file_id` (route 14) and
    the body is parsed in memory: the result is never written next to
    the real results, because the feature does not unpack or copy
    anything.  The body is size capped (SUMMARY_MAX_BYTES), so a wrong
    file_id (or a misbehaving server) cannot pull an unbounded stream
    into RAM.

    `summary_loaded` and not `loaded`: the close machine walks a fixed
    channel list by name (spec §5.1.4 step 2), so a generic name would
    be picked up by any future worker object that happens to expose a
    `loaded` attribute.
    """

    summary_loaded = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(object)

    def __init__(
        self,
        open_stream: Callable[[], Any],
        parent: Optional[QtCore.QObject] = None,
        *,
        job_id: str = "",
        file_id: str = "",
        cancel_event: Optional[threading.Event] = None,
        limit_bytes: int = SUMMARY_MAX_BYTES,
    ) -> None:
        super().__init__(parent)
        self._open_stream = open_stream
        self._limit = int(limit_bytes)
        self._cancel_event = cancel_event or threading.Event()
        self.job_id = str(job_id)
        self.file_id = str(file_id)
        self.payload: Optional[Dict[str, Any]] = None
        self.cancelled = False
        self.error: Optional[BaseException] = None

    def cancel(self) -> None:
        """The step 3 hook of the close machine (spec §5.1.4)."""

        self._cancel_event.set()

    def should_stop(self) -> bool:
        return self._cancel_event.is_set()

    def run(self) -> None:  # noqa: D102 - QThread entry point
        try:
            payload = self._read()
        except InterruptedError:
            self.cancelled = True
            return
        except Exception as exc:  # noqa: BLE001 - rendered by the page
            self.error = exc
            self.failed.emit(exc)
            return
        self.payload = payload
        self.summary_loaded.emit(payload)

    def _read(self) -> Dict[str, Any]:
        if self.should_stop():
            raise InterruptedError("读取 summary.json 已取消")
        response = self._open_stream()
        chunks: List[bytes] = []
        received = 0
        with response:
            for chunk in response.iter_chunks():
                if self.should_stop():
                    raise InterruptedError("读取 summary.json 已取消")
                received += len(chunk)
                if received > self._limit:
                    raise api.ContractViolationError(
                        "summary.json 超过 {0} 字节上限，已停止读取"
                        .format(self._limit)
                    )
                chunks.append(chunk)
        # ContractViolationError, not MalformedResponseError: the latter
        # is a TransportError, which client_error_view() renders as the
        # "lost contact" red bar - wrong for a body that arrived fine
        # and simply is not the JSON object route 14 promised.
        try:
            payload = json.loads(b"".join(chunks).decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise api.ContractViolationError(
                "summary.json 不是合法 JSON：{0}".format(exc)
            ) from exc
        if not isinstance(payload, dict):
            raise api.ContractViolationError(
                "summary.json 不是 JSON 对象"
            )
        return payload


class ManifestWorker(QtCore.QThread):
    """Read one artifact manifest for the results page (spec §5.6.2).

    The polling tick only pulls route 13 while the job is not terminal
    (spec §5.5.2): the manifest of a finished job is frozen, so the
    routine tick never asks for it again.  A job that is *already*
    terminal when the user opens the results page would then never get
    a single row.  This worker is the one explicit read that covers
    that case; the body is parsed by poller.files_from_payload, the one
    parser the tick uses too, so both render the same table.

    `manifest_loaded` and not `loaded`: the close machine walks a
    fixed channel list by name (spec §5.1.4 step 2), so a generic name
    would be picked up by any future worker object that happens to
    expose a `loaded` attribute.
    """

    manifest_loaded = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(object)

    def __init__(
        self,
        open_files: Callable[[], Any],
        parent: Optional[QtCore.QObject] = None,
        *,
        job_id: str = "",
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        super().__init__(parent)
        self._open_files = open_files
        self._cancel_event = cancel_event or threading.Event()
        self.job_id = str(job_id)
        self.files: Optional[List[Any]] = None
        self.cancelled = False
        self.error: Optional[BaseException] = None

    def cancel(self) -> None:
        """The step 3 hook of the close machine (spec §5.1.4)."""

        self._cancel_event.set()

    def should_stop(self) -> bool:
        return self._cancel_event.is_set()

    def run(self) -> None:  # noqa: D102 - QThread entry point
        if self.should_stop():
            self.cancelled = True
            return
        try:
            data = self._open_files()
        except Exception as exc:  # noqa: BLE001 - rendered by the page
            self.error = exc
            self.failed.emit(exc)
            return
        if self.should_stop():
            # The read landed, but the window is going away: emitting
            # now could touch a widget of a torn down page.
            self.cancelled = True
            return
        self.files = poll_mod.files_from_payload(data)
        self.manifest_loaded.emit(self.files)


@dataclass
class JobMonitor:
    """The window side state of one polling worker (paper trail only).

    The dialog owns the workers themselves (spec §5.1.4 wants them in
    self.workers); this object keeps what the four pages need to render
    an outcome without a second, competing state machine.
    """

    page: str = poll_mod.PAGE_JOBS
    job_id: str = ""
    last_outcome: Optional[PollOutcome] = None
    ticks: int = 0
    intervals: List[float] = field(default_factory=list)
    stop_lines: List[Any] = field(default_factory=list)
    logs: List[str] = field(default_factory=list)

    def note(self, outcome: PollOutcome) -> PollOutcome:
        """Record one tick and return it unchanged."""

        self.last_outcome = outcome
        self.ticks += 1
        self.intervals.append(float(outcome.interval))
        return outcome

    def add_log(self, text: str) -> None:
        if text:
            self.logs.append(str(text))

SUBMIT_ABORTED_TEXT = "已取消本次上传/提交"
SUBMIT_REJECTED_TEXT = "服务端按条目级拒绝了部分条目，已停止（未剔除、未上传）"


@dataclass
class SubmitOutcomeView:
    """The submit chain result the dialog renders (spec §5.4.5)."""

    ok: bool = False
    job_id: str = ""
    client_job_name: str = ""
    error: Optional[BaseException] = None
    error_message: str = ""
    error_code: str = ""
    error_details: Dict[str, Any] = field(default_factory=dict)
    view: Optional[Any] = None
    warnings: List[str] = field(default_factory=list)
    resumed_after_429: bool = False
    wait_seconds: float = 0.0
    cancelled: bool = False


class ReconcileWorker(QtCore.QThread):
    """One reconciliation pass of the B4 state machine (spec §5.4.1)."""

    reconciled = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(object)

    def __init__(
        self,
        store: Store,
        parent: Optional[QtCore.QObject] = None,
        *,
        uploader: Optional[Uploader] = None,
        client_for: Optional[Callable[[str], Any]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        super().__init__(parent)
        self._bound_store = store
        self._uploader = uploader
        self._client_for = client_for
        self._cancel_event = cancel_event or threading.Event()
        self.report: Optional[EntriesReport] = None
        self.error: Optional[BaseException] = None

    def cancel(self) -> None:
        self._cancel_event.set()

    def should_stop(self) -> bool:
        return self._cancel_event.is_set()

    def run(self) -> None:  # noqa: D102 - QThread entry point
        try:
            uploader = self._uploader or Uploader(store=self._bound_store)
            self.report = uploader.reconcile(
                self._client_for, cancel=self._cancel_event
            )
        except Exception as exc:  # noqa: BLE001 - reported to the UI
            self.error = exc
            self.failed.emit(exc)
            return
        if self.should_stop():
            # Cancelled by step 3: no banner, no ledger write (spec §5.1.4).
            return
        self.reconciled.emit(self.report)


class SubmitWorker(QtCore.QThread):
    """The plan -> pack -> upload -> submit chain (spec §5.4).

    One worker owns the whole chain so the GUI thread never issues a
    request.  The 429 ``UploadSchedule`` of spec §5.4.4 exception ③ is
    consumed here: the worker waits out the server given delay with the
    same cancellable Event every other wait uses, then replays the very
    same upload token and body.
    """

    progress = QtCore.pyqtSignal(int, int, str)
    submitted = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(object)

    def __init__(
        self,
        pipeline: Any,
        run: Any,
        body: Mapping[str, Any],
        parent: Optional[QtCore.QObject] = None,
        *,
        store: Store,
        client: Any,
        uploader: Optional[Uploader] = None,
        server_url: str = "",
        cancel_event: Optional[threading.Event] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        super().__init__(parent)
        self._pipeline = pipeline
        self._run = run
        self._body = dict(body)
        self._bound_store = store
        self._client = client
        self._uploader = uploader
        self._server_url = str(server_url or "")
        self._cancel_event = cancel_event or threading.Event()
        self._clock = clock
        self.outcome = SubmitOutcomeView()
        self.upload_schedule: Optional[Any] = None
        self.plan_outcome: Optional[PlanOutcome] = None
        self.upload_outcome: Optional[UploadOutcome] = None

    # -- close machine hooks -----------------------------------------

    def cancel(self) -> None:
        self._cancel_event.set()

    def should_stop(self) -> bool:
        return self._cancel_event.is_set()

    def now(self) -> float:
        return float(self._clock()) if self._clock else time.time()

    def wait_for(self, duration: float) -> bool:
        """The cancellable wait used by the 429 schedule (spec §5.4.4)."""

        remaining = max(0.0, float(duration))
        while remaining > 0:
            slice_ = min(0.25, remaining)
            if self._cancel_event.wait(slice_):
                return False
            remaining -= slice_
        return not self.should_stop()

    # -- chain --------------------------------------------------------

    def run(self) -> None:  # noqa: D102 - QThread entry point
        try:
            self._run_chain()
        except Exception as exc:  # noqa: BLE001 - reported to the UI
            self.outcome.error = exc
            self.failed.emit(exc)
            return
        if self.should_stop() or self.outcome.cancelled:
            # Step 3 cancelled this worker: nothing may reach the UI or
            # the ledger any more (spec §5.1.4 step 2).
            return
        self.submitted.emit(self.outcome)

    def _run_chain(self) -> None:
        uploader = self._uploader or Uploader(store=self._bound_store)
        labels = getattr(self._pipeline, "labels", None)
        if not labels:
            self.outcome.error_message = "标签尚未冻结，请先完成本地预检"
            return
        manifest = self._run.manifest(labels)
        params = dict(self._body.get("params") or {})
        plan = uploader.plan(
            manifest,
            client=self._client,
            run=self._run,
            server_url=self._server_url,
            dataset_dir=str(getattr(self._run, "dataset_dir", "") or ""),
            classes_file=str(getattr(self._run, "classes_file", "") or ""),
            task=str(getattr(self._run, "task", "") or ""),
            val_ratio=getattr(self._run, "val_ratio", None),
            seed=getattr(self._run, "seed", None),
            params=params,
            staging_dir=getattr(self._run, "staging_dir", None),
            labels=labels,
        )
        self.plan_outcome = plan
        if not plan.ok:
            self.outcome.error_message = plan.error_message
            self.outcome.error_code = plan.error_code or plan.local_block
            self.outcome.error_details = dict(plan.error_details or {})
            return
        entry = plan.entry
        if entry is None:
            self.outcome.error_message = "预检成功但没有可上传的待提交条目"
            return
        attempt = 0
        while True:
            if self.should_stop():
                self.outcome.cancelled = True
                self.outcome.error_message = SUBMIT_ABORTED_TEXT
                return
            upload = uploader.upload(
                self._client,
                entry,
                on_progress=self._on_upload_progress,
                cancel=self._cancel_event,
                should_cancel=self.should_stop,
            )
            self.upload_outcome = upload
            if upload.ok:
                break
            if upload.cancelled or self.should_stop():
                self.outcome.cancelled = True
                self.outcome.error_message = SUBMIT_ABORTED_TEXT
                return
            if upload.action == "retry_after" and attempt < 2:
                # Spec §5.4.4 exception ③: the server told us when to come
                # back, so wait that long and replay the same token + body.
                # The cap of two waits (three sends) is a pragmatic limit
                # of this UI worker, not a spec rule: the row stays
                # "uploading" and reconciliation keeps replaying the same
                # token, so reaching the cap loses nothing.
                wait = float(upload.retry_after or 0.0)
                self.outcome.resumed_after_429 = True
                self.outcome.wait_seconds = wait
                self.progress.emit(0, 0, "服务端要求 {0:.0f} 秒后重试同一凭证".format(wait))
                if not self.wait_for(wait):
                    self.outcome.cancelled = True
                    self.outcome.error_message = SUBMIT_ABORTED_TEXT
                    return
                attempt += 1
                continue
            self.outcome.error_message = upload.error_message
            self.outcome.error_code = upload.error_code
            self.outcome.error_details = dict(upload.error_details or {})
            self.outcome.error = None
            return
        dataset_id = upload.dataset_id or entry.dataset_id
        submission_body = dict(self._body)
        submission_body["dataset_id"] = dataset_id
        submission = uploader.submit(self._client, entry, submission_body)
        if not submission.ok:
            self.outcome.error_message = submission.error_message
            self.outcome.error_code = submission.error_code
            self.outcome.error_details = dict(
                getattr(submission, "error_details", None) or {}
            )
            return
        row = uploader.complete_submission(
            self._submission_row(uploader, submission),
            str(submission.job_id or ""),
            response=submission.response,
        )
        self.outcome.ok = True
        self.outcome.job_id = str(submission.job_id or "")
        self.outcome.client_job_name = str(
            row.client_job_name or self._body.get("client_job_name") or ""
        )
        self.outcome.warnings = [
            self._warning_text(item) for item in (submission.warnings or ())
        ]

    def _warning_text(self, item: Any) -> str:
        if isinstance(item, Mapping):
            return str(item.get("message") or item.get("code") or item)
        return str(item)

    def _submission_row(self, uploader: Uploader, outcome: Any) -> Any:
        """The persisted submission row behind one SubmitOutcome."""

        uploader.reload()
        found = uploader.ledger.submission(
            str(getattr(outcome, "client_submission_id", "") or "")
        )
        if found is None:
            raise api.ContractViolationError(
                "the submitted row is missing from the ledger"
            )
        return found

    def _on_upload_progress(self, done: int, total: int) -> None:
        self.progress.emit(int(done), int(total), "上传压缩包")

class DatasetPacker(Packer):
    """The concrete pack / restore collaborator of the B4 state machine.

    `pack` is the forward path: it validates the plan response against the
    local run (spec §5.2.9 review matrix) and writes the archive whose
    manifest.json bytes are the frozen plan body, byte for byte.

    `restore` only serves the crash path (spec §5.4.1 recovery action 1):
    it rebuilds the run from `pending_dir/manifest.json` and the dataset
    directory - reusing the scanner and the converter, never a second
    pipeline - so the replay uploads the same dataset under the same
    token.
    """

    def pack(
        self,
        run: Any,
        missing_images: Sequence[Mapping[str, Any]],
        manifest_json: bytes = b"",
        labels: Optional[Mapping[str, Any]] = None,
    ) -> PackOutcome:
        if not labels or not manifest_json:
            raise api.MissingDependencyError(
                "打包前缺少冻结的标签字节或 plan 请求体"
            )
        plan = {"missing_images": [dict(item) for item in missing_images]}
        review = packer_mod.review_plan(run, labels, plan)
        if review.blocked:
            raise api.ContractViolationError(
                "; ".join(issue.text() for issue in review.blocking_issues)
            )
        work_dir = str(getattr(run, "staging_dir", "") or "")
        if not work_dir:
            raise api.MissingDependencyError(
                "缺少 staging 目录，无法打包（未在工作目录里落任何文件）"
            )
        archive_path = packer_mod.pack_archive(
            run,
            labels,
            review.missing_images,
            manifest_json,
            work_dir,
        )
        return PackOutcome(
            manifest=json.loads(manifest_json.decode("utf-8")),
            missing_images=[dict(item) for item in review.missing_images],
            archive_path=archive_path,
            manifest_bytes=manifest_json,
            messages=[issue.text() for issue in review.info_issues()],
        )

    def restore(self, entry: Any) -> PackOutcome:
        manifest_path = entry.manifest_path or ""
        if not manifest_path or not osp.isfile(manifest_path):
            raise api.MissingDependencyError(
                "pending 目录里没有 manifest.json，无法重建压缩包"
            )
        manifest = store_mod.read_json_file(manifest_path)
        manifest_json = store_mod.canonical_json_bytes(manifest)
        dataset_dir = str(entry.dataset_dir or "")
        if not dataset_dir or not osp.isdir(dataset_dir):
            raise api.MissingDependencyError(
                "数据集目录不可用，无法重建压缩包（重新预检后上传）"
            )
        task = str(entry.task or "detect")
        scan = scan_dataset(
            dataset_dir, classes_file=entry.classes_file or None, task=task
        )
        if scan.blocked:
            raise api.ContractViolationError(
                "; ".join(scan.blocking_messages())
            )
        staging = self._restore_staging(entry)
        conversion = convert_labels(
            scan,
            staging,
            classes_file=entry.classes_file or None,
            task=task,
        )
        labels = dict(conversion.by_stem)
        classes = list(manifest.get("classes") or scan.classes)
        run = packer_mod.PackerRun(
            dataset_dir=osp.abspath(dataset_dir),
            task=task,
            classes=classes,
            classes_file=str(entry.classes_file or ""),
            val_ratio=float(entry.val_ratio or 0.0),
            seed=int(entry.seed or 0),
            split_result=self._split_from_manifest(manifest, classes),
            files=[item["name"] for item in manifest.get("images") or ()],
            image_paths={
                pair.name: pair.image_path for pair in scan.pairs
            },
            image_size={
                item["name"]: int(item.get("size") or 0)
                for item in manifest.get("images") or ()
            },
            image_sha256={
                item["name"]: str(item.get("sha256") or "")
                for item in manifest.get("images") or ()
            },
            staging_dir=staging,
        )
        missing = [
            {
                "name": item["name"],
                "split": item["split"],
                "sha256": item["sha256"],
                "size": int(item.get("size") or 0),
            }
            for item in entry.missing_images or ()
            if isinstance(item, Mapping)
        ]
        archive_path = packer_mod.pack_archive(
            run,
            labels,
            missing,
            manifest_json,
            staging,
            archive_path=osp.join(
                str(entry.pending_dir or staging), ARCHIVE_FILENAME
            ),
        )
        return PackOutcome(
            manifest=dict(manifest),
            missing_images=missing,
            archive_path=archive_path,
            manifest_bytes=manifest_json,
        )

    def _split_from_manifest(
        self, manifest: Mapping[str, Any], classes: Sequence[str]
    ) -> SplitResult:
        """The split views of one archived manifest (no re-splitting)."""

        assignments = {
            str(item["name"]): str(item.get("split") or "train")
            for item in manifest.get("images") or ()
            if isinstance(item, Mapping) and item.get("name")
        }
        image_classes = {
            name: [] for name in assignments
        }
        declared = manifest.get("split_stats")
        stats = {}
        if isinstance(declared, Mapping):
            for name, row in declared.items():
                if isinstance(row, Mapping):
                    stats[str(name)] = {
                        "train": int(row.get("train") or 0),
                        "val": int(row.get("val") or 0),
                    }
        if not stats:
            stats = stats_from_assignments(
                assignments, image_classes, list(classes)
            )
        return SplitResult(
            assignments=assignments,
            split_stats=stats,
            order=sorted(assignments),
        )

    def _restore_staging(self, entry: Any) -> str:
        staging = str(entry.staging_dir or "")
        if staging and osp.isdir(staging):
            return staging
        return tempfile.mkdtemp(prefix=store_mod.STAGING_PREFIX)
