"""Remote training sub window (spec §5.1.2, §5.1.3, §5.1.4).

Four pages in one `QStackedWidget` - configuration, jobs, job detail and
results - reachable from the Tools menu through the lazy launcher, plus
the one and only close state machine of the feature.

Three properties of this window are load bearing:

- `WA_DeleteOnClose` is set here (and nowhere else), so `close()` really
  destroys the object, `destroyed` fires and the launcher's reuse slot
  is cleared (spec §5.1.2);
- `Esc` and `reject()` both divert to `self.close()`, because the default
  `QDialog` paths skip `QCloseEvent` entirely and would destroy the window
  together with its child `QThread`s (spec §5.1.4);
- every close path - `Esc`, the close button, `reject()`, the guard of
  `close_guard.py` and the application exit - runs the same steps 0 to 6.

The monitoring step (spec §5.5) plugs into those seams: `start_polling`
enqueues one `PollWorker` per visible page through `register_worker`, and
the cancel / resume buttons run one `CommandWorker` each.  `start_polling`
reuses the poller's tick through `Scheduler`, so the three pages share the
one terminal formula, the one interval table and the one failure ladder.
The result step (spec §5.6) plugs into the same seams: the artifact
tree of the results page asks for one download at a time, and this
window owns the save path (user chosen), the streaming worker and the
ledger write back of `download_path`.
"""

from __future__ import annotations

import logging
import os.path as osp
import shutil
import time
from typing import Any, Callable, Dict, List, Optional

from PyQt6 import QtCore, QtGui, QtWidgets

from ..api_client import SCHEMA_VERSION
from ..api_client import RemoteTrainingClient, RemoteTrainingError
from ..api_client import RetryPolicy
from ..pipeline import Pipeline, PipelineError
from ..poller import (
    ORPHAN_NOTE,
    PAGE_DETAIL,
    PAGE_JOBS,
    PAGE_RESULTS,
    PollJob,
    PollOutcome,
    cancel_message,
    client_error_view,
    is_terminal,
    resume_mode,
    status_of,
)
from ..scanner import protocol_task
from ..store import (
    RECORD_STATUS_ORPHANED,
    ServerConfig,
    Store,
    TaskRecord,
)
from ..uploader import Uploader
from ..worker import (
    OPERATION_CANCEL,
    OPERATION_RESUME,
    CommandWorker,
    DownloadWorker,
    JobMonitor,
    ManifestWorker,
    DatasetPacker,
    PollWorker,
    ReconcileWorker,
    SUBMIT_ABORTED_TEXT,
    SubmitWorker,
    SummaryWorker,
)
from .close_guard import install_close_guard, workers_finished
from .config_page import CONFIG_FILTER, ConfigPage
from .detail_page import DetailPage
from .jobs_page import JobsPage
from .results_page import (
    DOWNLOAD_CANCELLED_TEXT,
    DOWNLOAD_START_TEXT,
    ResultsPage,
)
from .widgets import (
    CONNECT_AUTH_FAILED_TEXT,
    PrecheckWorker,
    CONNECT_DISABLED_TEXT,
    CONNECT_NOT_READY_TEXT,
    CONNECT_OK_TEXT,
    CONNECT_WRONG_ADDRESS_TEXT,
    STATUS_INFO,
    STATUS_RED,
    STATUS_YELLOW,
    ConnectionTestWorker,
)

__all__ = [
    "CLOSE_WAIT_MS",
    "REMOTE_TRAINING_PAGES",
    "STOP_WARNING_SECONDS",
    "WORKER_WARNING_SECONDS",
    "RemoteTrainingDialog",
]

_LOGGER = logging.getLogger(__name__)

WINDOW_TITLE = "远程训练"
WINDOW_SIZE = (1024, 640)
MINIMUM_WIDTH = 1024
MINIMUM_HEIGHT = 600

REMOTE_TRAINING_PAGES = ("config", "jobs", "detail", "results")

#: Fixed poll interval of the finish-wait of step 4 (spec §5.1.4).
CLOSE_WAIT_MS = 200
#: Hint only: exceeding it logs one warning per worker, never a kill.
WORKER_WARNING_SECONDS = 5.0
STOP_WARNING_SECONDS = WORKER_WARNING_SECONDS

STOPPING_TEXT = "正在停止（等待 {0} 结束）…"

#: What the confirmation of step 0 counts (spec §5.1.4).
#: The five enqueue points of spec §5.1.4 plus the generic tag used by a
#: worker that has not been classified yet.
GENERIC_OPERATION = "operation"
#: The five enqueue points of spec §5.1.4, in the order step 0 lists them,
#: plus the connection probe and the generic fallback tag.
OPERATION_ORDER = (
    "upload",
    "polling",
    "download",
    "scan",
    "convert",
    "pack",
    "connection",
)
OPERATION_LABELS = {
    "upload": "上传",
    "polling": "轮询",
    "download": "下载",
    "scan": "扫描",
    "convert": "转换",
    "pack": "打包",
    "connection": "连接测试",
    GENERIC_OPERATION: "操作",
}

CLOSE_CONFIRM_TEMPLATE = (
    "仍有 {0} 个操作在进行（{1}），关闭窗口会取消它们。"
    "已上传的图片会被服务端缓存复用，下次可继续。确定关闭？"
)
CLOSE_CONFIRM_TITLE = "关闭远程训练"

CLOSE_CHANNELS = (
    "progress",
    "metrics",
    "log",
    "state",
    "finished",
    "failed",
    "finished_with",
    # The monitoring step (spec §5.5) and the reconcile / submit workers
    # speak through these; step 2 disconnects them like every other
    # business signal, so no worker can touch a widget or the ledger on
    # its way out.
    "polled",
    "polled_job",
    "command_done",
    "reconciled",
    "submitted",
    # The result step (spec §5.6.3): the download stream, the
    # summary.json read and the explicit manifest read each report on
    # their own channel.  The two read channels are named
    # `summary_loaded` / `manifest_loaded`, not `loaded`, so no future
    # worker that happens to expose a `loaded` attribute is harvested
    # here.
    "download_done",
    "summary_loaded",
    # The explicit manifest read of a terminal job: the poller never
    # pulls route 13 for it (spec §5.5.2), so the results page reads it
    # through its own worker instead.
    "manifest_loaded",
)

#: Cancel / resume wording (spec §5.5.7); one place for both dialogs.
CANCEL_CONFIRM_TEMPLATE = "确定取消任务 {0}？\n\n{1}"
RESUME_CONFIRM_TEMPLATE = (
    "确定恢复任务 {0}？\n\n"
    "当前 attempt={1}，resume_cycles={2}（本轮重试预算将重置）。\n{3}"
)
CANCEL_CONFIRM_TITLE = "取消任务"
RESUME_CONFIRM_TITLE = "恢复任务"
RESUME_MODE_HINTS = {
    "resume": "将从上次进度继续训练（run/train/weights/last.pt）",
    "restart": "未找到可用检查点，将从零重训（服务端当前允许从零重训）",
}
RESUME_EXPIRED_HINT = "检查点或数据集已过期，无法恢复"
RESUME_NO_MODE_HINT = "服务端未声明可恢复模式，恢复按钮不可用"
CANCEL_CANCELLED_NOTE = (
    "该任务已中止，仅保留部分结果（weights/last.pt 与已产出的日志 / 曲线）"
)
POLL_WORKER_GENERIC_FAILURE = "轮询失败：{0}"
STATE_LINE_TERMINAL = "该任务已结束（{0}），停止高频轮询，保留兜底轮询"
STATE_LINE_FALLBACK = "任务处于终态，按 60 秒兜底轮询检查外部恢复"
STATE_LINE_RESUMED = "检测到任务被重新激活，恢复高频轮询"


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """Read one job object field from a mapping or a plain object."""

    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def stack_minimum_size(stack: QtWidgets.QStackedWidget) -> QtCore.QSize:
    """The size every page of the stack needs."""

    minimum = stack.minimumSizeHint()
    return QtCore.QSize(
        max(int(minimum.width()), MINIMUM_WIDTH),
        max(int(minimum.height()), MINIMUM_HEIGHT),
    )


class RemoteTrainingDialog(QtWidgets.QDialog):
    """Non modal window driving the whole remote training workflow."""

    def __init__(
        self,
        parent: Optional[Any] = None,
        *,
        store: Optional[Store] = None,
        client_factory: Optional[Callable[[], Any]] = None,
        pipeline_factory: Optional[Callable[..., Pipeline]] = None,
        confirm_close: Optional[Callable[[Any], bool]] = None,
        staging_temp_root: Optional[str] = None,
    ) -> None:
        super().__init__(parent)
        # Required by the launcher's reuse semantics (spec §5.1.2): without
        # it close() only hides the window and destroyed never fires.
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowTitle(self.tr(WINDOW_TITLE))
        self.setWindowFlags(
            QtCore.Qt.WindowType.Window
            | QtCore.Qt.WindowType.WindowMinimizeButtonHint
            | QtCore.Qt.WindowType.WindowMaximizeButtonHint
            | QtCore.Qt.WindowType.WindowCloseButtonHint
        )
        self.resize(*WINDOW_SIZE)
        self._initial_size_applied = False

        # --- the mandatory initialisations of spec §5.1.4 --------------
        self._closing = False
        self._closing_in_progress = False
        self._generation = 0
        self._pending_quit = False
        self.workers: List[Any] = []

        self._confirm_close = confirm_close
        self._pending_operations: Dict[str, str] = {}
        self._warning_logged: set = set()
        self._stop_started_at: Optional[float] = None
        self._stopping = False
        self._closed_once = False

        self.store = store if store is not None else Store()
        #: The temporary root the start-up staging scan looks at; None
        #: means the system temp directory (the production setting).
        self._staging_temp_root = staging_temp_root
        self.server_config = ServerConfig()
        self.client: Any = None
        self._client_factory = client_factory
        self._pipeline_factory = pipeline_factory or Pipeline
        self.pipeline: Optional[Pipeline] = None
        self.connection_test: Any = None
        self.capabilities: Any = None
        self.health_payload: Optional[Dict[str, Any]] = None
        self._connection_worker: Optional[Any] = None

        # --- monitoring state (spec §5.5) -------------------------------
        self.poll_worker: Optional[Any] = None
        self.monitors: Dict[str, JobMonitor] = {
            PAGE_JOBS: JobMonitor(page=PAGE_JOBS),
            PAGE_DETAIL: JobMonitor(page=PAGE_DETAIL),
            PAGE_RESULTS: JobMonitor(page=PAGE_RESULTS),
        }
        self.poll_errors: List[Any] = []
        self._uploader: Optional[Any] = None
        self.reconcile_worker: Optional[Any] = None
        #: The reconciliation banner lines of the jobs page (kept until the
        #: next pass, so a polling tick cannot wipe it).
        self.reconcile_lines: List[Any] = []
        #: The report of the start-up staging scan (spec §5.1.5), kept for
        #: the debug area and for the tests.
        self.staging_reclaim: Optional[Any] = None
        self.submit_worker: Optional[Any] = None
        self.entries_report: Optional[Any] = None
        #: The download worker of the result step (spec §5.6.3).
        self.download_worker: Optional[Any] = None
        #: The summary.json read of the results page (spec §5.6.3) plus
        #: the (job_id, file_id) pair it was fetched for, so the same
        #: artifact is never requested twice per session.
        self.summary_worker: Optional[Any] = None
        self._summary_key: Optional[Any] = None
        self._summary_pending: Optional[Any] = None
        #: The one explicit manifest read of a terminal job (the poller
        #: skips route 13 once the job is terminal, spec §5.5.2), the
        #: job whose manifest was already read (latch) and the job of a
        #: read still in flight.
        self.manifest_worker: Optional[Any] = None
        self._manifest_loaded: Optional[Any] = None
        self._manifest_pending: Optional[Any] = None
        # The frozen result of the last successful pre-check (R1): the
        # submit reads THIS, never an attribute of the pipeline, because
        # Pipeline.assemble() only returns the run and stores nothing.
        self.prepared_run: Optional[Any] = None
        self.prepared_pipeline: Optional[Any] = None
        self.prepared_fingerprint: Optional[Any] = None
        #: The chain a live SubmitWorker is running: (run, pipeline,
        #: staging_dir).  Latched by start_submit(), so a callback always
        #: settles the staging of the chain it belongs to and never the
        #: one a concurrent pre-check may have frozen in the meantime.
        self._submitting: Optional[Any] = None
        #: The form the running pre-check was built from (S1 latch).
        self._precheck_form: Optional[Any] = None

        self.stack = QtWidgets.QStackedWidget()
        self.config_page = ConfigPage()
        self.jobs_page = JobsPage()
        self.detail_page = DetailPage()
        self.results_page = ResultsPage()
        self.stack.addWidget(self.config_page)
        self.stack.addWidget(self.jobs_page)
        self.stack.addWidget(self.detail_page)
        self.stack.addWidget(self.results_page)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.stack)
        self.setMinimumSize(stack_minimum_size(self.stack))

        self._connect_pages()
        self._load_server_config()
        self.results_page.append_debug_line(
            "远程训练窗口已打开（pages: {0}）".format(
                ", ".join(REMOTE_TRAINING_PAGES)
            )
        )
        # Spec §5.1.5: the crash leftovers of earlier runs are scanned
        # once, on this start-up path, before any new work begins.
        self.reclaim_staging_leftovers()

        self.install_close_guard()
        # Spec §5.4.1: reconciliation runs at start-up as well as on every
        # entry into the jobs page; it is a worker, so opening the window
        # never blocks on the network.
        self.start_reconcile()

    # ------------------------------------------------------------ wiring

    def _connect_pages(self) -> None:
        self.config_page.connection_requested.connect(self.test_connection)
        self.config_page.import_requested.connect(self.import_config)
        self.config_page.export_requested.connect(self.export_config)
        self.config_page.precheck_requested.connect(self.start_precheck)
        self.config_page.submit_requested.connect(self.start_submit)
        self.config_page.dataset_changed.connect(self._on_dataset_changed)
        self.jobs_page.details_requested.connect(self.show_detail)
        self.jobs_page.refresh_requested.connect(
            lambda: self.wake_polling(PAGE_JOBS)
        )
        self.jobs_page.cancel_requested.connect(self.cancel_jobs)
        self.jobs_page.resume_requested.connect(self.resume_jobs)
        self.detail_page.back_requested.connect(self.show_jobs)
        self.detail_page.results_requested.connect(self.show_results)
        self.detail_page.cancel_requested.connect(self.cancel_job)
        self.detail_page.resume_requested.connect(self.resume_job)
        self.results_page.back_requested.connect(self.show_detail)
        # The download step (spec §5.6.3): the list page, the detail
        # page and the results page each open the zip; a double click
        # in the artifact tree opens one file by its opaque file_id.
        self.jobs_page.download_requested.connect(self.download_results)
        self.detail_page.download_requested.connect(self.download_results)
        self.results_page.download_requested.connect(self.download_results)
        self.results_page.file_download_requested.connect(
            self.download_job_file
        )
        self.results_page.open_directory_requested.connect(
            self.open_directory
        )
        # The detail page button of spec §5.1.3: `download_path` exists
        # exactly so that this one can open the folder of the last
        # download of that job (spec §5.3.2).
        self.detail_page.open_artifacts_requested.connect(
            self.open_artifacts_directory
        )

    def install_close_guard(self) -> Any:
        """Install the application close guard exactly once (spec §5.1.4).

        The guard is a process wide singleton and therefore looks the
        dialog up through the owner on every request
        (`getattr(owner, "_remote_training_dialog")`, the attribute the
        launcher maintains and clears on destroyed).  Passing a captured
        `lambda: self` would leave the guard of a second window pointing
        at the first, destroyed one (spec §5.1.2).
        """

        self.close_guard = install_close_guard(
            self.parent(),
            app=QtWidgets.QApplication.instance(),
        )
        return self.close_guard

    # ------------------------------------------------------------- pages

    def show_config(self) -> None:
        self.stack.setCurrentWidget(self.config_page)
        self.stop_polling()

    def show_jobs(self, *_args: Any) -> None:
        """Enter the task list page: reconcile once, then poll (§5.4.1)."""

        self.stack.setCurrentWidget(self.jobs_page)
        self.start_reconcile()
        self.start_polling(PAGE_JOBS)
        return None

    def show_detail(self, job_id: str = "") -> None:
        """Enter the detail page: poll this job at its 3 s tier (§5.5.1)."""

        if job_id:
            self.detail_page.job_id = str(job_id)
            self.detail_page.job_label.setText(str(job_id))
            self.detail_page.clear_log()
        self.stack.setCurrentWidget(self.detail_page)
        current = str(self.detail_page.job_id or "")
        if current:
            self.start_polling(PAGE_DETAIL, current)

    def show_results(self, job_id: str = "") -> None:
        """Enter the results page: poll this job at its 15 s tier.

        Rows rendered for another job are dropped here: the summary area
        of spec §5.6.3 is filled out of that table, and an artifact list
        of job A must not answer a lookup for job B (nor produce a
        failure line for a file_id B never had).  The next tick refills
        the table from B's own manifest.
        """

        if job_id:
            incoming = str(job_id)
            if str(self.results_page.files_job_id) != incoming:
                self.results_page.set_files([], job_id=incoming)
                self.results_page.set_summary(None)
                self._summary_key = None
                # The next terminal tick reads this job's manifest once,
                # explicitly (see load_manifest).
                self._manifest_loaded = None
            self.results_page.job_id = incoming
            self.results_page.job_label.setText(
                "产物：{0}".format(incoming)
            )
        self.stack.setCurrentWidget(self.results_page)
        current = str(self.results_page.job_id or "")
        if current:
            self.start_polling(PAGE_RESULTS, current)
        # The summary area of spec §5.6.3 is filled from the manifest of
        # this job; entering the page is one of the two triggers, the
        # other is a polling tick that brings the manifest in.
        self.load_summary(current)

    def current_page_name(self) -> str:
        widget = self.stack.currentWidget()
        for name, page in (
            ("config", self.config_page),
            ("jobs", self.jobs_page),
            ("detail", self.detail_page),
            ("results", self.results_page),
        ):
            if page is widget:
                return name
        return ""

    # --------------------------------------------------------- monitoring

    def describe_job(self, job_id: str) -> str:
        """The display name of one ledger row (name, else the id)."""

        try:
            record = self.store.load_ledger().record(job_id)
        except Exception:  # pragma: no cover - defensive
            record = None
        name = str(getattr(record, "client_job_name", "") or "")
        return name or str(job_id)

    def start_polling(
        self,
        page: str = PAGE_JOBS,
        job_id: str = "",
        *,
        worker: Optional[Any] = None,
        one_shot: bool = False,
    ) -> Any:
        """Enqueue the polling worker of the current page (spec §5.1.4).

        One worker at a time: the page owns it, and a second call stops
        the previous one first, so self.workers never holds two polling
        threads for the same window.  The callers are the page entry
        slots (show_jobs / show_detail / show_results), the manual
        refresh button and every "refresh now" of the failure exit.
        """

        self.stop_polling()
        if worker is None:
            worker = self.new_poll_worker(page, job_id)
        if one_shot:
            self._limit_worker(worker)
        self.wire_poll_worker(worker)
        worker.finished.connect(self._on_poll_finished)
        self.poll_worker = worker
        self.register_worker(worker, "polling")
        worker.start()
        return worker

    def new_poll_worker(self, page: str, job_id: str = "") -> Any:
        """One polling worker for one page / job (not started yet)."""

        return PollWorker(
            store=self.store,
            client=self.ensure_client(),
            page=page,
            job_id=job_id,
            active_ids=self.active_job_ids,
            visible=self.monitoring_visible,
            policy=self.retry_policy(),
        )

    def wire_poll_worker(self, worker: Any) -> Any:
        """Connect one polling worker to the rendering slots.

        An explicit page entry / refresh publishes with generation None:
        the page the user is looking at must never be silenced by a stale
        generation (the close machine still cancels the worker in step 3
        and step 2 still drops every already queued signal).
        """

        worker.polled.connect(
            lambda outcome, g=None: self._on_polled(outcome, g)
        )
        worker.polled_job.connect(
            lambda outcome, result, g=None: self._on_polled_job(
                outcome, result, g
            )
        )
        worker.failed.connect(
            lambda error, g=None: self._on_poll_failure(error, g)
        )
        return worker

    def _limit_worker(self, worker: Any, ticks: int = 1) -> Any:
        limit = getattr(worker, "one_shot", None)
        if callable(limit):
            limit(ticks)
        return worker

    def stop_polling(self) -> bool:
        """Cancel the current polling worker; step 3 does the waking."""

        worker = self.poll_worker
        if worker is None:
            return False
        self.poll_worker = None
        cancel = getattr(worker, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except Exception:  # pragma: no cover - defensive
                pass
        return True

    def wake_polling(self, page: str = "") -> bool:
        """Pull once right now (manual refresh / page re entry)."""

        worker = self.poll_worker
        if worker is None or (page and page != self.current_page_name()):
            return False
        wake = getattr(worker, "wake", None)
        if callable(wake):
            wake()
            return True
        return False

    def monitoring_visible(self) -> bool:
        """False while the window is minimised or hidden (spec §5.5.1)."""

        try:
            if self.isMinimized():
                return False
            return bool(self.isVisible())
        except RuntimeError:  # pragma: no cover - destroyed window
            return False

    def retry_policy(self) -> RetryPolicy:
        """The one ladder of the api client (spec §5.5.6).

        One instance per window: the failure counter and the tier live in
        the request loop, this object only carries the schedule.
        """

        policy = getattr(self, "_retry_policy", None)
        if policy is None:
            policy = RetryPolicy()
            self._retry_policy = policy
        return policy

    def active_job_ids(self) -> List[str]:
        """Every ledger row that is not orphaned."""

        try:
            ledger = self.store.load_ledger()
        except Exception:  # pragma: no cover - defensive
            return []
        return [
            record.job_id
            for record in ledger.records
            if record.job_id and record.status != RECORD_STATUS_ORPHANED
        ]

    def job_ids_in_state(self, *statuses: str) -> List[str]:
        """Rows whose last known status is one of the given ones."""

        wanted = {str(item) for item in statuses}
        try:
            ledger = self.store.load_ledger()
        except Exception:  # pragma: no cover - defensive
            return []
        return [
            record.job_id
            for record in ledger.records
            if record.job_id and record.status in wanted
        ]

    def refresh_job(
        self, job_id: str, page: str = PAGE_DETAIL
    ) -> Optional[Any]:
        """Pull one job once, in a worker (never on the GUI thread).

        Every "refresh now" path of the monitoring step - the failure
        exit of a cancel / resume, the resume write back, the 404 of an
        artifacts lookup - lands here, so no GUI slot ever issues a
        synchronous request: the window keeps running its close state
        machine even while the server is stalled (spec §5.1.4, §5.5.1).

        Two shapes, and the difference matters (R2):

        - the continuous poller already watches exactly this page and
          job ⇒ it is woken, so the 3 s / 15 s cadence continues and
          the resume path keeps its "immediately back to high frequency
          polling" promise (spec §5.5.2, §5.5.7 step 5);
        - otherwise a temporary one shot worker is started WITHOUT
          touching self.poll_worker and WITHOUT stop_polling(), so a
          refresh of another page can never kill the page the user is
          actually looking at.
        """

        job_id = str(job_id or "")
        if not job_id:
            return None
        current = self.poll_worker
        if self.poll_watches(current, page, job_id):
            wake = getattr(current, "wake", None)
            if callable(wake):
                wake()
            return current
        worker = self.new_poll_worker(page, job_id)
        self._limit_worker(worker)
        self.wire_poll_worker(worker)
        self.register_worker(worker, "polling")
        worker.start()
        return worker

    @staticmethod
    def poll_watches(worker: Any, page: str, job_id: str) -> bool:
        """True when that live worker polls exactly that page and job."""

        if worker is None:
            return False
        try:
            if str(getattr(worker, "_page", "")) != str(page):
                return False
            if str(getattr(worker, "_job_id", "")) != str(job_id):
                return False
            is_finished = getattr(worker, "isFinished", None)
            return not (callable(is_finished) and is_finished())
        except RuntimeError:  # pragma: no cover - destroyed wrapper
            return False

    # ------------------------------------------------- polling rendering

    def _on_polled(
        self, outcome: Optional[PollOutcome], generation: Any
    ) -> None:
        """Render one tick on the GUI thread (spec §5.5)."""

        if outcome is None or self._is_stale(generation):
            return
        monitor = self.monitors.setdefault(
            outcome.page, JobMonitor(page=outcome.page)
        )
        monitor.note(outcome)
        if outcome.error_view is not None:
            # The same failure exit as the command path: the lines land
            # on this page own status row and every action flag fires
            # (highlight the settings, refresh the detail, orphan the
            # row) - all of it through workers (N2).
            self.poll_errors.append(outcome.error_view)
            self._apply_error_view(
                outcome.error_view, self._outcome_job_id(outcome)
            )
        else:
            self._render_status(outcome)
        self._render_debug(outcome)
        if outcome.unauthorized and self.poll_worker is not None:
            self.stop_polling()

    def composed_status_lines(
        self, page: str, lines: List[Any]
    ) -> List[Any]:
        """The lines of one page: its reconciliation banner first.

        The banner is the persistent line of the jobs page (spec §5.4.1)
        while a successful polling tick has no status lines of its own,
        so composing them keeps the banner from being wiped every 10 s.
        """

        if page != PAGE_JOBS or not self.reconcile_lines:
            return list(lines)
        return list(self.reconcile_lines) + list(lines)

    def _render_status(self, outcome: PollOutcome) -> None:
        """Status row of the current page (spec §5.5.6, §5.6.4)."""

        rows = self._status_rows(outcome.page)
        if not rows:
            return
        lines = self.composed_status_lines(
            outcome.page, list(outcome.status_lines())
        )
        for row in rows:
            row.set_lines(lines)

    def _status_rows(self, page: str) -> List[Any]:
        """The status row of one page (each page has its own)."""

        if page == PAGE_DETAIL:
            return [self.detail_page.status_row]
        if page == PAGE_RESULTS:
            return [self.results_page.status_row]
        if page == PAGE_JOBS:
            return [self.jobs_page.status_row]
        return []

    def _outcome_job_id(self, outcome: PollOutcome) -> str:
        """The job one tick was about (results, else the current page)."""

        for result in outcome.results:
            if result.job_id:
                return result.job_id
        if outcome.page == PAGE_DETAIL:
            return str(self.detail_page.job_id or "")
        if outcome.page == PAGE_RESULTS:
            return str(self.results_page.job_id or "")
        return ""

    def _render_debug(self, outcome: PollOutcome) -> None:
        """Append only lifecycle summary of the debug area (spec §5.1.5)."""

        if outcome.pauses:
            return
        if outcome.stopped:
            self.log_debug("轮询已停止（401：Token 无效或已过期）")
            return
        if outcome.results and outcome.results[0].page == PAGE_JOBS:
            summary = (
                "列表页轮询：{0} 批 / {1} 个任务（下次 {2:.0f} 秒）"
            ).format(outcome.batches, len(outcome.results), outcome.interval)
            if outcome.idle:
                summary += "，台账中已无活动任务，进入 60 秒空转档"
            self.log_debug(summary)

    def _on_polled_job(
        self,
        outcome: Optional[PollOutcome],
        result: Optional[PollJob],
        generation: Any,
    ) -> None:
        if result is None or self._is_stale(generation):
            return
        if result.page == PAGE_JOBS:
            self._render_jobs(outcome)
        elif result.page == PAGE_DETAIL:
            self._render_detail(result)
        elif result.page == PAGE_RESULTS:
            # A failed tick carries no manifest either; the explicit read
            # of a terminal job is skipped there (see _render_results).
            self._render_results(
                result,
                failed=(
                    outcome is None
                    or outcome.error_view is not None
                    or bool(getattr(outcome, "unauthorized", False))
                ),
            )

    def _render_jobs(self, outcome: Optional[PollOutcome]) -> None:
        if outcome is None or not outcome.results:
            return
        jobs = [result.job for result in outcome.results if result.job]
        self.jobs_page.set_jobs(jobs, now=time.time())
        summary = "共 {0} 个任务（{1} 批，下次 {2:.0f} 秒）".format(
            len(jobs), outcome.batches, outcome.interval
        )
        # The list summary is one more line of the same tick, not an
        # independent writer of the status row: it is composed with the
        # reconciliation banner, otherwise this very set_lines would wipe
        # the banner of _render_status on every tick that returns a job.
        self.jobs_page.status_label.setText(summary)
        self.jobs_page.status_row.set_lines(
            self.composed_status_lines(PAGE_JOBS, [(STATUS_INFO, summary)])
        )

    def _render_detail(self, result: PollJob) -> None:
        job = result.job
        if job is None:
            return
        first_time = self.detail_page.job_id != result.job_id
        if first_time:
            self.detail_page.clear_log()
        self.detail_page.set_job(job)
        self.detail_page.job_id = result.job_id
        if result.events:
            highest = self.detail_page.append_events(result.events)
            self._write_last_seq(result.job_id, highest)
        for etype in result.unknown_types:
            self.log_debug(
                "未知事件类型 {0} 已降级为日志行（前向兼容）".format(etype)
            )
        if result.manual_resume_event:
            self._after_manual_resume(result)
        if result.done_event:
            self.log_debug("收到 done 事件，刷新终态与产物")
        if result.transitioned:
            self.detail_page.append_line(STATE_LINE_RESUMED)
            self.log_debug(
                "任务 {0} 的 is_terminal 回到 false，恢复高频轮询".format(
                    result.job_id
                )
            )
        elif is_terminal(job):
            self.detail_page.append_line(
                STATE_LINE_TERMINAL.format(status_of(job))
            )

    def _render_results(
        self, result: PollJob, *, failed: bool = False
    ) -> None:
        job = result.job
        if job is None:
            return
        files = result.files
        if files is None:
            files = self.results_page.files
        else:
            # Rows arrived from the tick itself, so the table is fed by
            # the poller again: release the latch and let the next
            # terminal edge read the manifest explicitly once more.
            self._manifest_loaded = None
        self.results_page.set_job(job, files)
        # A terminal job needs the one explicit read: the tick never
        # pulls route 13 for it (see load_manifest).  Not on a failed
        # tick - result.files is None there too, and re-issuing the very
        # same route inside one tick would bypass the failure ladder of
        # spec §5.5.6.
        if is_terminal(job) and not failed:
            self.load_manifest(result.job_id)
        # A tick may be the first one that carries the artifact list, so
        # the summary read is retried here (it is a no-op once loaded).
        self.load_summary(result.job_id)
        if result.transitioned:
            self.log_debug(
                "结果页发现外部恢复，本次 tick 补发一次产物清单"
            )

    def _write_last_seq(self, job_id: str, last_seq: int) -> None:
        """Persist the event cursor (spec §5.5.3)."""

        if not job_id or last_seq <= 0:
            return
        try:
            ledger = self.store.load_ledger()
            record = ledger.record(job_id)
            if record is None:
                return
            record.last_seq = int(last_seq)
            ledger.upsert_record(record)
            self.store.save_ledger(ledger)
        except Exception as exc:  # pragma: no cover - defensive
            _LOGGER.warning("could not persist last_seq: %s", exc)

    def _after_manual_resume(self, result: PollJob) -> None:
        """Write the resume event back and say so (spec §5.5.3)."""

        payload = dict(result.resume or {})
        self.detail_page.append_line(
            "第 {0} 次人工恢复（mode={1}），本轮重试预算已重置".format(
                payload.get("resume_cycles"), payload.get("mode")
            )
        )
        self.log_debug(
            "任务 {0} 出现 manual_resume 事件（attempt={1}, "
            "resume_cycles={2}），已写回台账".format(
                result.job_id,
                payload.get("attempt"),
                payload.get("resume_cycles"),
            )
        )

    def _on_poll_failure(self, error: Any, generation: Any) -> None:
        if self._is_stale(generation):
            return
        try:
            view = client_error_view(error)
        except Exception:  # pragma: no cover - defensive
            view = None
        if view is not None:
            self.poll_errors.append(view)
            self._apply_error_view(
                view, self._current_monitored_job(), refresh=False
            )
        else:
            self.log_debug(POLL_WORKER_GENERIC_FAILURE.format(error))

    def _current_monitored_job(self) -> str:
        """The job of the running polling worker (may be empty)."""

        return str(getattr(self.poll_worker, "_job_id", "") or "")

    def _on_poll_finished(self) -> None:
        """The worker ended; a page re entry may start it again."""

        worker = self.poll_worker
        if worker is None:
            return
        is_finished = getattr(worker, "isFinished", None)
        try:
            if callable(is_finished) and is_finished():
                self.poll_worker = None
        except RuntimeError:  # pragma: no cover - destroyed wrapper
            self.poll_worker = None

    # ------------------------------------------------------ cancel / resume

    def cancel_jobs(self, job_ids: Any = None) -> List[Any]:
        """Bulk cancel of the checked rows (spec §5.5.7)."""

        wanted = [str(item) for item in (job_ids or [])]
        if not wanted:
            wanted = self.job_ids_in_state(
                "queued", "preparing", "running"
            )
            if not wanted:
                self._set_jobs_status(
                    "没有可取消的任务（仅 queued / preparing / running）"
                )
                return []
        return [self.cancel_job(job_id) for job_id in wanted]

    def resume_jobs(self, job_ids: Any = None) -> List[Any]:
        """Bulk resume of the checked rows (spec §5.5.7)."""

        wanted = [str(item) for item in (job_ids or [])]
        if not wanted:
            wanted = self.job_ids_in_state(
                "failed", "interrupted", "cancelled"
            )
            if not wanted:
                self._set_jobs_status(
                    "没有可恢复的任务（仅 failed / interrupted / cancelled）"
                )
                return []
        return [self.resume_job(job_id) for job_id in wanted]

    def cancel_job(self, job_id: str) -> Any:
        """Confirm and POST /jobs/{id}/cancel through CommandWorker."""

        job_id = str(job_id)
        if not job_id:
            return None
        if not self._confirm_cancel(job_id):
            return None
        self.detail_page.status_row.set_lines(
            [(STATUS_INFO, cancel_message(self.capabilities))]
        )
        return self.start_command(job_id, OPERATION_CANCEL)

    def resume_job(self, job_id: str, mode: str = "") -> Any:
        """Confirm and POST /jobs/{id}/resume through CommandWorker."""

        job_id = str(job_id)
        if not job_id:
            return None
        record = self._ledger_record(job_id)
        job = self._rendered_job(job_id)
        # The mode is a job object field (spec §3.4.4): the list page
        # never renders the detail page, so this is the only source that
        # works for the bulk action of spec §5.5.7.
        chosen = str(mode or "") or resume_mode(
            _field(job, "resume_mode_available")
        )
        if not chosen and str(self.detail_page.job_id or "") == job_id:
            chosen = resume_mode(
                getattr(self.detail_page, "resume_modes", None)
            )
        if not chosen and record is not None:
            chosen = resume_mode(
                (record.extra or {}).get("resume_mode_available")
            )
        if not chosen:
            # The hint belongs to the page the user is looking at; the
            # detail row of a job that is not on screen would hide it.
            self._set_visible_status([(STATUS_YELLOW, RESUME_NO_MODE_HINT)])
            self.detail_page.resume_button.setEnabled(False)
            return None
        if not self._confirm_resume(job_id, chosen, record, job):
            return None
        return self.start_command(job_id, OPERATION_RESUME, mode=chosen)

    def _rendered_job(self, job_id: str) -> Any:
        """The job object the pages last rendered for this id (if any).

        resume_mode_available and max_attempts are job object fields
        (spec §3.4.4) that the ledger deliberately does not mirror, so
        they are read back from what the polling rendered (spec §5.3.2).
        """

        wanted = str(job_id)
        for job in getattr(self.jobs_page, "jobs", None) or ():
            if str(_field(job, "job_id") or "") == wanted:
                return job
        detail = getattr(self.detail_page, "job", None)
        if detail is not None and (
            str(_field(detail, "job_id") or "") == wanted
        ):
            return detail
        return None

    def _visible_status_rows(self) -> List[Any]:
        """The status rows of the page on screen (never a hidden page)."""

        rows = self._status_rows(self.current_page_name())
        if not rows:
            rows = [self.detail_page.status_row]
        return rows

    def _set_visible_status(self, lines: List[Any]) -> None:
        """Write status lines on the visible page, banner included."""

        page = self.current_page_name()
        composed = self.composed_status_lines(page, list(lines))
        for row in self._visible_status_rows():
            row.set_lines(list(composed))

    def _set_jobs_status(self, text: str) -> None:
        """The list summary / local hint of the jobs page, banner first."""

        self.jobs_page.status_label.setText(text)
        self.jobs_page.status_row.set_lines(
            self.composed_status_lines(PAGE_JOBS, [(STATUS_INFO, text)])
        )

    def start_command(
        self, job_id: str, action: str, *, mode: str = ""
    ) -> Any:
        """Run one cancel / resume POST in a worker (spec §5.1.4 point ⑤)."""

        worker = CommandWorker(
            action,
            job_id,
            self,
            client=self.ensure_client(),
            mode=mode,
        )
        generation = self._generation
        worker.command_done.connect(
            lambda result, g=generation: self._on_command_done(result, g)
        )
        operation = (
            OPERATION_CANCEL if action == OPERATION_CANCEL
            else OPERATION_RESUME
        )
        self.register_worker(worker, operation)
        worker.start()
        return worker

    def _on_command_done(self, result: Any, generation: Any) -> None:
        """Render one cancel / resume outcome (spec §5.5.7, §5.6.4)."""

        if result is None or self._is_stale(generation):
            return
        job_id = str(getattr(result, "job_id", "") or "")
        if getattr(result, "ok", False):
            if str(getattr(result, "action", "") or "") == OPERATION_RESUME:
                self._write_resume(job_id, result)
            text = result.message()
            self.detail_page.status_row.set_lines([(STATUS_INFO, text)])
            self.detail_page.append_line(text)
            self.refresh_job(job_id)
            return
        view = getattr(result, "view", None)
        if view is None:
            return
        self._apply_error_view(view, job_id)

    def _apply_error_view(
        self,
        view: Any,
        job_id: str = "",
        *,
        refresh: bool = True,
        wake: bool = True,
    ) -> None:
        """The failure exit of spec §5.6.4, in one place.

        Every action it triggers is a worker: the GUI thread never
        issues a request here (spec §5.1.4, §5.5.1).  The polling
        failure slot passes refresh=False: that worker is already the
        one which produced the failure, so waking it again is enough.

        wake=False renders the failure (rows, details, the debug line)
        but runs none of the follow-up actions.  The summary read needs
        it: its 404 carries the "refresh the manifest" flag, and the
        manifest that lists the unreadable file_id is already on
        screen, so resetting the table and waking the results poller
        would re-read the very same file_id as fast as the round trip
        allows.  For a *terminal* job resetting the table is not
        permanent any more: reset_results_files() releases the manifest
        latch and the next terminal tick reads the manifest once more
        through ManifestWorker (the poller itself still skips the files
        route once the job is terminal).
        """

        page = self.current_page_name()
        lines = self.composed_status_lines(page, list(view.lines()))
        rows = self._status_rows(page)
        if not rows:
            rows = [self.detail_page.status_row]
        for row in rows:
            row.set_lines(list(lines))
        # The entry by entry details belong where the user can see them:
        # on the visible page row, or in the detail log when that page is
        # the one on screen.  A hidden page is left clean (N7).
        details = list(view.detail_lines())
        detail_row = self.detail_page.status_row
        if detail_row not in rows:
            detail_row.set_lines([])
        if details and self.current_page_name() == PAGE_DETAIL:
            for line in details:
                self.detail_page.append_line(line)
        elif details and rows[0] is not detail_row:
            for line in details:
                rows[0].append_line((STATUS_INFO, line))
        if view.highlight_settings:
            self.show_config()
        if view.highlight_field:
            self.config_page.status_row.append_line(
                (
                    STATUS_RED,
                    "请检查对应参数输入框：{0}".format(
                        str(view.details.get("field") or "")
                    ),
                )
            )
        if view.refresh_plan:
            self.log_debug("需要重新预检并上传（二次 plan）")
        if not refresh:
            return
        if wake:
            if view.refresh_files:
                self.reset_results_files(job_id)
                if job_id:
                    self.refresh_job(job_id, PAGE_RESULTS)
            if view.refresh_detail and job_id:
                self.refresh_job(job_id, PAGE_DETAIL)
            if view.refresh_capabilities:
                # A capabilities fetch is a network call: it runs in
                # the connection worker, never on the GUI thread.
                self.test_connection()
            if view.mark_orphaned and job_id:
                self.mark_orphaned(job_id)
        self.log_debug(
            "错误码 {0}（HTTP {1}）：{2}".format(
                view.code or "-", view.http_status, view.text
            )
        )

    def _write_resume(self, job_id: str, result: Any) -> None:
        """Write the resume response back (spec §5.5.7 step 2)."""

        if not job_id:
            return
        try:
            ledger = self.store.load_ledger()
            record = ledger.record(job_id)
            if record is None:
                return
            record.mark_manual_resume(
                result.attempt, result.resume_cycles, result.mode
            )
            record.needs_attention = False
            record.needs_attention_reason = None
            # artifact_suspect stays: it is a property of the artifacts.
            ledger.upsert_record(record)
            self.store.save_ledger(ledger)
        except Exception as exc:  # pragma: no cover - defensive
            _LOGGER.warning("could not write the resume back: %s", exc)
            return
        self.detail_page.set_attention(False)
        self.detail_page.resume_modes = []
        self.detail_page.resume_button.setEnabled(False)
        self.log_debug(
            "恢复响应写回台账：attempt={0}, resume_cycles={1}".format(
                result.attempt, result.resume_cycles
            )
        )

    def mark_orphaned(self, job_id: str) -> None:
        """Mark one row as gone from the server (spec §5.5.5)."""

        try:
            ledger = self.store.load_ledger()
            record = ledger.record(job_id)
            if record is None:
                return
            record.status = RECORD_STATUS_ORPHANED
            record.notes = ORPHAN_NOTE
            ledger.upsert_record(record)
            self.store.save_ledger(ledger)
        except Exception as exc:  # pragma: no cover - defensive
            _LOGGER.warning("could not mark %s orphaned: %s", job_id, exc)

    def reset_results_files(self, job_id: str) -> None:
        """Clear the artifact list so the next tick rebuilds it."""

        if job_id and job_id == self.results_page.job_id:
            self.results_page.set_files([])
            # The cleared table must be refillable: the next terminal
            # tick reads the manifest once more (see load_manifest).
            self._manifest_loaded = None

    def _ledger_record(self, job_id: str) -> Optional[TaskRecord]:
        try:
            return self.store.load_ledger().record(job_id)
        except Exception:  # pragma: no cover - defensive
            return None

    def _record_status_of(self, job_id: str) -> str:
        record = self._ledger_record(job_id)
        status = str(getattr(record, "status", "") or "")
        if status:
            return status
        return status_of(getattr(self.detail_page, "job", None))

    def _confirm_cancel(self, job_id: str) -> bool:
        """The cancel confirmation of spec §5.5.7 (per status)."""

        status = self._record_status_of(job_id)
        if status == "queued":
            hint = "任务还在排队，取消后不会开始训练，也不会产出任何产物"
        elif status == "preparing":
            hint = "任务正在准备，取消会终止准备过程"
        else:
            hint = (
                "取消会终止本轮训练；若训练已产出检查点，服务端会把已产出的"
                "部分结果落到 partial/（是否真的有 weights/last.pt 取决于"
                "取消发生在训练哪个时刻，以产物清单为准）。"
                "「部分结果可用」只由服务端的 partial_available 决定"
            )
        return self._ask(
            CANCEL_CONFIRM_TITLE,
            CANCEL_CONFIRM_TEMPLATE.format(self.describe_job(job_id), hint),
        )

    def _confirm_resume(
        self,
        job_id: str,
        mode: str,
        record: Optional[TaskRecord],
        job: Any = None,
    ) -> bool:
        """The resume confirmation: attempt, cycles and the real action."""

        attempt = getattr(record, "attempt", None)
        cycles = getattr(record, "resume_cycles", None)
        if attempt is None:
            attempt = _field(job, "attempt")
        if cycles is None:
            cycles = _field(job, "resume_cycles")
        # max_attempts is a job object field (spec §3.4.4), not a ledger
        # column: the rendered job is the authoritative source, the extra
        # map is only the historical fallback.
        maximum = _field(job, "max_attempts")
        if maximum is None:
            maximum = (getattr(record, "extra", {}) or {}).get(
                "max_attempts"
            )
        hint = RESUME_MODE_HINTS.get(mode, "")
        if maximum is not None:
            # Spec §5.5.7 step 1: the reset semantics, with both numbers
            # (max_attempts includes the first attempt, so a manual resume
            # can still auto retry max_attempts - 1 times).
            hint += (
                "；恢复后本轮重试预算重置，attempt 重置为 1 后本轮内仍可"
                "自动重试 {0} 次，即手动恢复后最多再自动重试 {1} 次"
            ).format(int(maximum), max(int(maximum) - 1, 0))
        return self._ask(
            RESUME_CONFIRM_TITLE,
            RESUME_CONFIRM_TEMPLATE.format(
                self.describe_job(job_id), attempt, cycles, hint
            ),
        )

    def _ask(self, title: str, text: str) -> bool:
        asker = getattr(self, "_confirm_action", None)
        if asker is not None:
            return bool(asker(title, text))
        answer = QtWidgets.QMessageBox.question(
            self,
            self.tr(title),
            text,
            QtWidgets.QMessageBox.StandardButton.Yes
            | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        return answer == QtWidgets.QMessageBox.StandardButton.Yes

    # ------------------------------------------- reconcile / submit (B4)

    def uploader(self) -> Uploader:
        """The B4 state machine bound to this window ledger and client.

        One instance per window is enough: the object is stateless between
        calls except for its ledger, and every call re-reads tasks.json, so
        the reclamation exemption set is always live (spec §5.1.5).
        """

        existing = getattr(self, "_uploader", None)
        if existing is None:
            existing = Uploader(
                store=self.store,
                packer=DatasetPacker(),
                clock=time.time,
            )
            self._uploader = existing
        return existing

    def reconcile_client(self, _server_url: str = "") -> Any:
        """The client every replay uses: the one in the URL box."""

        return self.ensure_client()

    def start_reconcile(self) -> Any:
        """Reconcile the pending entries once (spec §5.4.1).

        Called at start-up and on every entry into the jobs page.  The
        banner of the unfinished entries and the dropped count are shown
        on the jobs page status row (spec §5.4.1 第 8 行).
        """

        previous = list(self.reconcile_lines)
        self.reconcile_lines = []
        if not self.reconciliation_needed():
            # Nothing to reconcile this pass: the banner of the previous
            # pass must not linger on the row until the next tick.
            self._clear_stale_reconcile_lines(previous)
            return None
        worker = ReconcileWorker(
            self.store,
            uploader=self.uploader(),
            client_for=self.reconcile_client,
        )
        self.reconcile_worker = worker
        worker.reconciled.connect(
            lambda report, g=None: self._on_reconciled(report, g)
        )
        worker.failed.connect(
            lambda error, g=None: self._on_reconcile_failed(error, g)
        )
        self.register_worker(worker, "upload")
        worker.start()
        return worker

    def _clear_stale_reconcile_lines(self, previous: List[Any]) -> None:
        """Drop the previous banner when the row shows nothing else."""

        if not previous:
            return
        expected = [str(text) for _severity, text in previous]
        for row in self._status_rows(PAGE_JOBS):
            visible = row.lines()
            if visible and all(text in expected for text in visible):
                row.set_lines([])

    def reconciliation_needed(self) -> bool:
        """True while any pending entry is unfinished (spec §5.4.1).

        A cheap ledger read: it decides whether a reconciliation worker
        is worth starting at all, and it is what makes the jobs page
        banner appear only when there is something to reconcile.
        """

        try:
            ledger = self.store.load_ledger()
        except Exception:  # pragma: no cover - defensive
            return False
        for entry in ledger.pending_uploads:
            if entry.unfinished:
                return True
        for entry in ledger.pending_submissions:
            if entry.unfinished:
                return True
        return False

    def _on_reconciled(self, report: Any, generation: Any = None) -> None:
        """Show the reconciliation banner and the count of dropped rows."""

        if report is None or self._is_stale(generation):
            return
        self.entries_report = report
        banner = report.status_message()
        dropped = list(report.dropped or [])
        lines: List[Any] = []
        if banner:
            lines.append((STATUS_YELLOW, banner))
        if dropped:
            lines.append(
                (
                    STATUS_INFO,
                    "已清除的条目：{0} 条（已作废或已提交，明细见调试信息）"
                    .format(len(dropped)),
                )
            )
        # The banner is stored, not painted once: the jobs page polls every
        # 10 s and a successful tick has no status lines of its own, so a
        # one-shot paint would be wiped by the very next tick.
        self.reconcile_lines = list(lines)
        for row in self._status_rows(PAGE_JOBS):
            row.set_lines(self.composed_status_lines(PAGE_JOBS, []))
        if not lines:
            self._set_jobs_status("对账完成：没有未完成的上传 / 提交")
        for message in list(report.messages or [])[:20]:
            self.log_debug(str(message))
        self.log_debug(
            "对账完成：未完成 {0} 条，已清除 {1} 条".format(
                report.unfinished_count, len(dropped)
            )
        )

    def _on_reconcile_failed(
        self, error: Any, generation: Any = None
    ) -> None:
        if self._is_stale(generation):
            return
        self.log_debug("对账失败：{0}".format(error))
        self.reconcile_lines = [
            (STATUS_YELLOW, "对账失败：{0}".format(error))
        ]
        for row in self._status_rows(PAGE_JOBS):
            row.set_lines(self.composed_status_lines(PAGE_JOBS, []))

    def job_request_body(self) -> Dict[str, Any]:
        """The POST /jobs body built from the form (spec §3.2.2 #7).

        Only the parameters the user actually set are sent (spec §3.8.2).
        The dataset id and the client submission id are added by the
        uploader from the latched ledger row.
        """

        form = self.config_page.values()
        return {
            "schema_version": SCHEMA_VERSION,
            "task": protocol_task(form.task),
            "model": str(form.model or ""),
            "model_family": str(form.model_family or ""),
            "params": dict(form.params or {}),
        }

    def start_submit(self) -> Any:
        """Run plan -> pack -> upload -> submit in a worker (spec §5.4).

        The worker owns the whole chain, so the GUI thread never blocks;
        it also consumes the 429 UploadSchedule by waiting out the
        server given delay before replaying the same upload token.
        """

        pipeline = self.pipeline
        run = self.prepared_run
        if run is None or pipeline is None \
                or self.prepared_pipeline is not pipeline:
            self.config_page.set_status_lines(
                [(STATUS_RED, "请先在配置页完成本地预检，再提交任务")]
            )
            return None
        if self.prepared_fingerprint != self.form_fingerprint():
            self.invalidate_prepared()
            self.config_page.set_status_lines(
                [(STATUS_RED, "配置已变更，请重新预检后再提交任务")]
            )
            return None
        try:
            body = self.job_request_body()
        except ValueError as exc:
            self.config_page.set_status_lines(
                [(STATUS_RED, "配置无效：{0}".format(exc))]
            )
            return None
        worker = SubmitWorker(
            pipeline,
            run,
            body,
            store=self.store,
            client=self.ensure_client(),
            uploader=self.uploader(),
            server_url=self.server_config.server_url,
        )
        self.submit_worker = worker
        # Identity, not a later re-read of self.prepared_run: a pre-check
        # started while this chain runs would move those attributes to
        # another run (spec §5.1.5 / §5.6.4).
        identity = (
            run,
            pipeline,
            str(
                getattr(run, "staging_dir", "")
                or getattr(pipeline, "staging_dir", "")
                or ""
            ),
        )
        self._submitting = identity
        worker.progress.connect(
            lambda done, total, name, g=None: self._on_submit_progress(
                done, total, name, g
            )
        )
        worker.submitted.connect(
            lambda outcome, g=None, ident=identity: self._on_submitted(
                outcome, g, ident
            )
        )
        worker.failed.connect(
            lambda error, g=None, ident=identity: self._on_submit_failed(
                error, g, ident
            )
        )
        self.register_worker(worker, "upload")
        self.config_page.set_status_lines(
            [(STATUS_INFO, "正在上传并提交任务…")]
        )
        worker.start()
        return worker

    def _release_submitting(self, identity: Any = None) -> None:
        """Settle the staging of one finished submit chain (spec §5.1.5).

        Branch ② is published right here; branch ① is deferred to
        _retire_abandoned_staging, because the frozen run stays
        submittable and the manual retry of spec §5.6.4 must still be
        able to pack its labels out of this staging area.
        """

        chain = identity if identity is not None else self._submitting
        if self._submitting is chain:
            self._submitting = None
        if chain is None:
            self._retire_abandoned_staging()
            return
        self._publish_kept_staging(chain)
        self._retire_abandoned_staging(chain[1])

    def _publish_kept_staging(self, chain: Any) -> None:
        """Branch ② of spec §5.1.5 for one finished chain."""

        pipeline = chain[1]
        if pipeline is None or getattr(pipeline, "staging_finished", False):
            return
        if not self.keep_staging():
            # Branch ① is deferred (see _retire_abandoned_staging): the
            # frozen run may still be submitted again.
            return
        path = str(chain[2] or getattr(pipeline, "staging_dir", "") or "")
        self.finish_staging(path)
        pipeline.staging_finished = True

    def _retire_abandoned_staging(self, *extra: Any) -> None:
        """Branch ① of spec §5.1.5, once a run cannot be submitted again.

        A staging area is kept for as long as its pipeline is the frozen
        prepared run or the chain of a live SubmitWorker: spec §5.6.4
        keeps the frozen run submittable so a failed POST can be retried
        by hand, and that retry re-packs the labels out of this very
        directory.  Everything else - a run replaced by a newer
        pre-check, a refused pre-check, a chain whose window moved on -
        is retired here; the crash leftovers of a killed process are the
        start-up seven day scan of the same spec section instead.
        """

        live = [self.prepared_pipeline]
        if self._submitting is not None:
            live.append(self._submitting[1])
        for pipeline in extra:
            if pipeline is None:
                continue
            if any(pipeline is item for item in live):
                continue
            self._finish_pipeline_staging(pipeline)

    def _finish_pipeline_staging(self, pipeline: Any) -> None:
        """Take the staging branch of one pipeline exactly once.

        A staging directory an unfinished pending entry still points at is
        exempt (spec §5.1.5): DatasetPacker.restore re-reads
        entry.staging_dir to repack the labels of a replay, so deleting it
        while a reconciliation runs would break that replay (the next pass
        silently degrades to a fresh mkdtemp).  The exemption is the store
        predicate the TTL scan uses, evaluated live, and the pipeline is
        deliberately NOT latched here: once the entry settles, the next
        retire pass reclaims the directory.
        """

        if pipeline is None or getattr(pipeline, "staging_finished", False):
            return
        path = str(getattr(pipeline, "staging_dir", "") or "")
        if path and self.staging_is_referenced(path):
            _LOGGER.info(
                "staging is still referenced by an unfinished pending "
                "entry (or the ledger is degraded), keeping it: %s",
                path,
            )
            self.log_debug(
                "staging 仍被未完成的待提交条目引用（或台账处于降级状态），"
                "暂不回收：{0}".format(path)
            )
            return
        self.finish_staging(path)
        pipeline.staging_finished = True

    def staging_is_referenced(self, path: str) -> bool:
        """True while an unfinished pending entry points at that path.

        One live ledger read (spec §5.1.5: the exemption set is evaluated
        per directory, never from a scan snapshot).  A degraded ledger
        also counts as "still referenced": spec §5.3.4 makes
        load_ledger() total, so a damaged tasks.json silently yields an
        empty ledger instead of raising - without this the exemption
        would read "nothing references the directory" from a ledger
        that lost the very entry which did.  Keeping a directory is
        recoverable (the fixed TTL still reclaims it), deleting one a
        replay still needs is not.
        """

        target = str(path or "")
        if not target:
            return False
        try:
            if self.store.is_staging_referenced(target):
                return True
            degraded = bool(self.store.ledger_is_degraded())
        except Exception as exc:  # pragma: no cover - defensive
            _LOGGER.warning(
                "could not evaluate the staging exemption for %s: %s",
                target,
                exc,
            )
            return True
        if degraded:
            _LOGGER.warning(
                "the ledger is degraded (a quarantined copy exists); "
                "keeping staging %s until it is trustworthy again",
                target,
            )
            return True
        return False

    def _on_submit_progress(
        self, done: Any, total: Any, name: Any, generation: Any = None
    ) -> None:
        if self._is_stale(generation):
            return
        if not total:
            text = str(name or "上传中…")
        else:
            text = "上传进度 {0}/{1}（{2}）".format(done, total, name or "")
        self.config_page.set_status_lines([(STATUS_INFO, text)])

    def _on_submitted(
        self, outcome: Any, generation: Any = None, identity: Any = None
    ) -> None:
        """Render the submit result and open the new job (spec §5.4.5)."""

        if outcome is None or self._is_stale(generation):
            return
        if self._closing:
            # Step 3 is already cancelling; opening the detail page would
            # start a new polling thread during the close (spec §5.1.4).
            self._release_submitting(identity)
            self.log_debug("窗口正在关闭，提交结果只记日志，不再打开任务页")
            self.log_debug(
                "已提交任务（job_id={0}）".format(
                    getattr(outcome, "job_id", "") or "-"
                )
            )
            return
        if not getattr(outcome, "ok", False):
            return self._on_submit_failed(outcome, generation, identity)
        # The submit chain ended (spec §5.1.5): branch ② publishes the
        # kept path here, branch ① waits until the run cannot be
        # submitted again (a manual retry of §5.6.4 must still pack).
        self._release_submitting(identity)
        lines: List[Any] = [
            (
                STATUS_INFO,
                "已提交任务（job_id={0}）".format(
                    getattr(outcome, "job_id", "") or "-"
                ),
            ),
        ]
        if getattr(outcome, "resumed_after_429", False):
            lines.append(
                (
                    STATUS_INFO,
                    "服务端保留区已满，已在 {0:.0f} 秒后用同一凭证重试成功"
                    .format(float(getattr(outcome, "wait_seconds", 0.0))),
                )
            )
        for warning in getattr(outcome, "warnings", None) or ():
            lines.append((STATUS_YELLOW, str(warning)))
        self.config_page.set_status_lines(lines)
        job_id = str(getattr(outcome, "job_id", "") or "")
        if job_id:
            self.show_detail(job_id)

    def _on_submit_failed(
        self, outcome: Any, generation: Any = None, identity: Any = None
    ) -> None:
        """The one failure exit of the submit chain (spec §5.6.4)."""

        if outcome is None or self._is_stale(generation):
            return
        self._release_submitting(identity)
        if isinstance(outcome, BaseException):
            self._apply_error_view(client_error_view(outcome))
            return
        error = getattr(outcome, "error", None)
        if error is not None:
            self._apply_error_view(client_error_view(error))
            return
        lines: List[Any] = []
        message = str(getattr(outcome, "error_message", "") or "")
        code = str(getattr(outcome, "error_code", "") or "")
        if message:
            lines.append((STATUS_RED, message))
        if code:
            lines.append((STATUS_INFO, "错误码：{0}".format(code)))
        if getattr(outcome, "cancelled", False):
            lines = [(STATUS_YELLOW, SUBMIT_ABORTED_TEXT)]
        if not lines:
            lines = [(STATUS_RED, "提交未完成")]
        self.config_page.set_status_lines(lines)
        self.log_debug(
            "提交失败：{0}（code={1}）".format(message or "-", code or "-")
        )

    # ------------------------------------------------------------ server

    def _load_server_config(self) -> None:
        try:
            self.server_config = self.store.load_server()
        except Exception as exc:  # pragma: no cover - defensive
            _LOGGER.warning("server.json unreadable: %s", exc)
            self.server_config = ServerConfig()
        values = {
            "server_url": self.server_config.server_url,
            "api_key": self.server_config.api_key,
        }
        self.config_page.set_values(values)

    def save_server_config(self) -> ServerConfig:
        """Persist URL and Token; the Token never leaves the ledger.

        The form is user input (a hand typed seed included), so a
        `ValueError` from it is reported like any other local problem
        instead of escaping this slot.
        """

        try:
            form = self.config_page.values()
        except ValueError as exc:
            self.config_page.set_status_lines(
                [(STATUS_RED, "配置无效：{0}".format(exc))]
            )
            return self.server_config
        config = ServerConfig(
            server_url=form.server_url,
            api_key=form.api_key,
            updated_at=self.server_config.updated_at,
        )
        try:
            self.server_config = self.store.save_server(config)
        except Exception as exc:  # pragma: no cover - defensive
            _LOGGER.warning("server.json not writable: %s", exc)
            self.server_config = config
        return self.server_config

    def ensure_client(self) -> Any:
        """The API client of this window, created on first use."""

        if self.client is None:
            if self._client_factory is not None:
                self.client = self._client_factory()
            else:
                config = self.save_server_config()
                self.client = RemoteTrainingClient(
                    config.server_url, config.api_key
                )
        return self.client

    # ---------------------------------------------------- connection test

    def test_connection(self) -> Any:
        """Probe the training health route in the background (§5.1.3)."""

        client = self.ensure_client()
        # A wrong address / missing route answers 404 while the server is
        # reachable: that must stop the retry loop instead of looking like
        # "not ready" (spec §5.1.3).
        try:
            worker = ConnectionTestWorker(client, self)
        except RemoteTrainingError as exc:  # pragma: no cover - defensive
            self.config_page.set_status_lines([(STATUS_RED, str(exc))])
            return None
        generation = self._generation
        worker.finished_with.connect(
            lambda outcome, g=generation: self._on_connection_tested(
                outcome, g
            )
        )
        self.register_worker(worker, "connection")
        self._connection_worker = worker
        worker.start()
        return worker

    def _on_connection_tested(
        self, outcome: Any, generation: Any = None
    ) -> None:
        if self._is_stale(generation):
            return
        self.connection_test = outcome
        probe = getattr(outcome, "probe", None)
        health = getattr(probe, "health", None)
        payload = getattr(health, "payload", None)
        self.health_payload = payload if isinstance(payload, dict) else None
        capabilities = getattr(outcome, "capabilities", None)
        if capabilities is not None:
            # The parameter surface and the vram table of the pre-check
            # (spec §5.1.3) come with the successful probe; the wording of
            # the four connection states stays untouched by this.
            self.capabilities = capabilities
            self.config_page.set_capabilities(capabilities)
        lines = self._connection_lines(probe, getattr(outcome, "error", None))
        self.config_page.set_status_lines(lines)
        self.log_debug(
            "连接测试：{0}".format(
                "; ".join(text for _severity, text in lines) or "无结果"
            )
        )

    def load_capabilities(self) -> Any:
        """Refresh `capabilities` through the connection worker (route 1).

        N5: nothing calls this any more (the 422 OPTIMIZER_UNSUPPORTED
        branch of the failure exit asks for the same thing), and the old
        body was a synchronous GET on the GUI thread.  It now just runs
        the same background probe the connection test uses, so keeping
        the name cannot reintroduce a frozen window.
        """

        return self.test_connection()

    def _connection_lines(
        self, probe: Any, error: Optional[BaseException] = None
    ) -> List[Any]:
        """Map one probe onto the four connection states (spec §5.1.3)."""

        if error is not None:
            # A reachable server that answers 404 has a wrong address or
            # prefix, which stops the retry loop; anything else is "not
            # ready yet" (spec §5.1.3).
            status = getattr(error, "http_status", 0)
            if status == 404:
                return [(STATUS_RED, CONNECT_WRONG_ADDRESS_TEXT)]
            return [(STATUS_RED, CONNECT_NOT_READY_TEXT)]
        if probe is None:
            return [(STATUS_RED, CONNECT_NOT_READY_TEXT)]
        state = getattr(probe, "state", "")
        if state == "unauthorized":
            return [(STATUS_RED, CONNECT_AUTH_FAILED_TEXT)]
        if state == "unreachable":
            detail = getattr(probe, "error", None)
            lines: List[Any] = [(STATUS_RED, CONNECT_NOT_READY_TEXT)]
            if detail is not None:
                lines.append((STATUS_INFO, str(detail)))
            return lines
        if state == "training_disabled":
            return [(STATUS_RED, CONNECT_DISABLED_TEXT)]
        health = getattr(probe, "health", None)
        return [(STATUS_INFO, CONNECT_OK_TEXT), *self.health_lines(health)]

    def health_lines(self, health: Any) -> List[Any]:
        """The health fields of spec §5.1.3; none of them blocks."""

        if health is None:
            return []
        payload = getattr(health, "payload", None)
        if not isinstance(payload, dict):
            return []
        lines: List[Any] = []
        if not payload.get("devices"):
            lines.append(
                (STATUS_YELLOW, "服务端当前没有可用 GPU")
            )
        work_dir = payload.get("work_dir")
        if isinstance(work_dir, dict) and work_dir.get("free_gb") is not None:
            lines.append(
                (
                    STATUS_INFO,
                    "服务端工作目录剩余 {0} GB".format(
                        work_dir.get("free_gb")
                    ),
                )
            )
        queue = payload.get("queue")
        if isinstance(queue, dict) and any(
            queue.get(key) is not None
            for key in ("queued", "running", "max_concurrent_jobs")
        ):
            lines.append(
                (
                    STATUS_INFO,
                    "服务端队列：排队 {0} / 运行 {1}（并发上限 {2}）".format(
                        queue.get("queued", 0),
                        queue.get("running", 0),
                        queue.get("max_concurrent_jobs", "-"),
                    ),
                )
            )
        weights = payload.get("weights")
        if isinstance(weights, dict):
            lines.append(
                (
                    STATUS_INFO,
                    "权重摘要：已缓存 {0} / 缺失 {1}".format(
                        weights.get("cached", "-"), weights.get("missing", "-")
                    ),
                )
            )
        blobs = payload.get("blobs")
        if isinstance(blobs, dict):
            lines.append(
                (
                    STATUS_INFO,
                    "缓存摘要：{0} 个 blob（物化方式 {1}）".format(
                        blobs.get("count", "-"),
                        blobs.get("materialize") or "-",
                    ),
                )
            )
        calibration = payload.get("calibration")
        if isinstance(calibration, dict):
            if calibration.get("deferred"):
                text = (
                    "服务端本次未做本机显存标定（有训练任务在跑），"
                    "正在用兜底层数值，估算偏保守"
                )
                if calibration.get("deferred_ready"):
                    text += "；服务端当前已空闲，重启服务即可完成补标定"
                lines.append((STATUS_INFO, text))
            elif not calibration.get("required") or not calibration.get(
                "auto_loaded"
            ):
                lines.append(
                    (
                        STATUS_INFO,
                        "服务端未使用本机实测显存基线（退回手工基线 / "
                        "内置默认），显存估算可能偏保守",
                    )
                )
        for warning in payload.get("warnings") or []:
            code = ""
            message = ""
            if isinstance(warning, dict):
                code = str(warning.get("code") or "")
                message = str(warning.get("message") or "")
            else:
                message = str(warning)
            lines.append((STATUS_INFO, "{0}：{1}".format(code, message)))
        return lines

    # ------------------------------------------------------------- config

    def export_config(self) -> str:
        """Write the form (never the Token) to a chosen file (§5.2.8).

        The document is built by the pipeline, which is the single source
        of truth for the export key set: it validates `val_ratio` and
        requires a real `seed`, so the seed box is filled in first.  Both
        the seed box (a hand typed value) and the file dialog are user
        input, hence the explicit `ValueError` branch: an unhandled one
        would abort the process inside a Qt slot.
        """

        path, _selected = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "导出配置",
            self.default_config_name(),
            CONFIG_FILTER,
        )
        if not path:
            return ""
        if not path.lower().endswith(".json"):
            path += ".json"
        return path if self.write_config_document(path) else ""

    def write_config_document(self, path: str) -> bool:
        """Build the export document and write it to `path`.

        Split out of the file dialog so that the document itself can be
        exercised without a native dialog.

        N-b (accepted side effect): the document is built through
        `build_pipeline()`, which invalidates the frozen result of the
        last pre-check.  Exporting after a pre-check therefore asks for
        a new pre-check before the submit.  That is conservative and
        safe (the frozen bytes and the exported document can never
        drift apart); it only costs one extra pre-check, which is why
        it is documented instead of worked around.
        """

        try:
            pipeline = self.build_pipeline()
            form = self.config_page.values()
            values = form.export_values()
            values["seed"] = pipeline.effective_seed()
            document = pipeline.export_config(values)
        except (PipelineError, ValueError) as exc:
            self.config_page.set_status_lines(
                [(STATUS_RED, "导出失败：{0}".format(exc))]
            )
            return False
        text = self.config_page.config_json(document)
        try:
            from ..store import atomic_write_text

            atomic_write_text(path, text)
        except OSError as exc:
            self.config_page.set_status_lines(
                [(STATUS_RED, "导出失败：{0}".format(exc))]
            )
            return False
        self.log_debug("已导出配置到 {0}".format(path))
        return True

    def default_config_name(self) -> str:
        """A sane default file name for the export dialog (§5.2.8)."""

        return "remote_training_config.json"

    def import_config(self, path: str = "") -> Dict[str, Any]:
        """Read a configuration and backfill the form key by key (§5.2.8)."""

        if not path:
            path, _selected = QtWidgets.QFileDialog.getOpenFileName(
                self, "导入配置", "", CONFIG_FILTER
            )
        if not path:
            return {}
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            self.config_page.set_status_lines(
                [(STATUS_RED, "导入失败：{0}".format(exc))]
            )
            return {}
        try:
            pipeline = self._new_pipeline()
            values = pipeline.import_config(text, apply=False)
        except PipelineError as exc:
            self.show_pipeline_error(exc)
            return {}
        except ValueError as exc:
            # `ConfigImportError` of the splitter (a missing or illegal
            # val_ratio / seed) reaches this slot as a ValueError; letting
            # it through would abort the process (spec §5.2.8).
            self.config_page.set_status_lines(
                [(STATUS_RED, "导入失败：{0}".format(exc))]
            )
            self.log_debug("导入失败：{0}".format(exc))
            return {}
        values.pop("api_key", None)
        self.config_page.set_values(values)
        self.pipeline = pipeline
        # A replaced pipeline is the end of the frozen run of the last
        # pre-check: it can no longer be submitted (the submit compares
        # object identity with self.prepared_pipeline), so its staging
        # area must not wait for the window close or the seven day TTL
        # (spec §5.2.8 / §5.1.5 branch ①).  invalidate_prepared() reads
        # the stale run before it clears the attributes.
        self.invalidate_prepared()
        self.log_debug("已导入配置 {0}".format(path))
        return values

    # ------------------------------------------------------------ pipeline

    def _new_pipeline(self, **kwargs: Any) -> Pipeline:
        form = self.config_page.values()
        pipeline = self._pipeline_factory(
            config=form.pipeline_config(), **kwargs
        )
        return pipeline

    def invalidate_prepared(self) -> None:
        """Forget the frozen run of the last pre-check (spec §5.2.1).

        Any change of the dataset, the classes file or the form makes the
        frozen labels / split stale, so the submit must ask for a new
        pre-check instead of uploading the old bytes.  A run that can no
        longer be submitted also loses its staging area here (branch ①
        of spec §5.1.5), unless a live submit chain still owns it.
        """

        stale = self.prepared_pipeline
        self.prepared_run = None
        self.prepared_pipeline = None
        self.prepared_fingerprint = None
        self._retire_abandoned_staging(stale)

    def form_fingerprint(self) -> Any:
        """A cheap snapshot of the form a frozen run belongs to."""

        try:
            form = self.config_page.values()
        except (ValueError, TypeError):  # pragma: no cover - defensive
            return None
        try:
            ratio = float(form.val_ratio)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            ratio = None
        params = tuple(
            sorted(
                (str(name), repr(value))
                for name, value in (form.params or {}).items()
            )
        )
        return (
            str(form.dataset_dir),
            str(form.classes_file),
            str(form.task),
            str(form.model_family),
            str(form.model),
            ratio,
            form.seed,
            params,
        )

    def build_pipeline(self) -> Pipeline:
        """Create the pipeline of the current form and backfill the seed.

        The seed box may be empty; the pipeline then generates one and the
        window writes it back into the box (spec §5.2.8), so the exported
        configuration carries the value the split actually used.
        """

        self.pipeline = self._new_pipeline()
        seed = self.pipeline.effective_seed()
        if not self.config_page.seed_text().strip():
            self.config_page.set_seed(seed)
        # A fresh pipeline invalidates whatever the last pre-check froze
        # (identity is also checked at submit time, so a stale object can
        # never be uploaded).
        self.invalidate_prepared()
        return self.pipeline

    def start_precheck(self, *, worker: Optional[Any] = None) -> Any:
        """Start the local pre-check in a worker (spec §5.1.3, §5.1.4).

        The pre-check needs the whole step 1 to 7 pipeline - scanning,
        conversion, the split and the per image hashes - so it runs in a
        worker: the window keeps repainting, can be closed while it runs,
        and the close machine's step 3 stops it through
        `PrecheckWorker.cancel()` (the flag the pipeline polls as
        `should_stop`).  Only the cheap validation stays inline.

        Two locally possible results: (1) the estimate of the chosen
        `(model, task)` straight from `capabilities.vram_table` - its
        `max_batch` and its `source` - and (2) the local validation
        conclusion (the N1 matrix of spec §5.2.5 and the split assertions
        of spec §5.2.7).  The authoritative estimate (`vram_estimate_mb`,
        `batch_assumed`, `resolved_params`, `warnings[]`) only arrives
        with the submit response, so nothing here blocks the submit.
        """

        try:
            pipeline = self.build_pipeline()
            pipeline.validate_config()
        except (PipelineError, ValueError) as exc:
            return self.report_precheck_error(exc)
        # S1: the pipeline was built from THIS form.  The fingerprint is
        # latched before the worker starts, so a form edit during the run
        # (the longest window of the whole step) cannot whitewash the
        # frozen result: the finishing slot compares the latched value
        # with the form as it is then and refuses to freeze on a change.
        self._precheck_form = self.form_fingerprint()
        if worker is None:
            worker = PrecheckWorker(pipeline, self)
        generation = self._generation
        worker.finished_with.connect(
            lambda pipe, run, error, g=generation: (
                self._on_precheck_finished(pipe, run, error, g)
            )
        )
        self.register_worker(worker, "scan")
        worker.start()
        return worker

    def _on_precheck_finished(
        self,
        pipeline: Any,
        run: Any,
        error: Any,
        generation: Any = None,
    ) -> List[Any]:
        """Render the worker's outcome on the GUI thread (§5.1.4 step 1)."""

        if self._is_stale(generation):
            return []
        if error is not None:
            return self.report_precheck_error(error)
        lines: List[Any] = []
        check = run.split_check()
        if check.blocked:
            lines.append((STATUS_RED, check.reason()))
        # R1 + S1: the submit reads this frozen result, and it is frozen
        # only when the form is still the one the pipeline was built
        # from.  A blocked split freezes nothing either, so a refused or
        # already stale pre-check can never be uploaded.
        latched = getattr(self, "_precheck_form", None)
        current = self.form_fingerprint()
        if check.blocked or latched != current:
            self.invalidate_prepared()
            # The just finished run is refused: it can never be
            # submitted, so its staging area is retired here.
            self._retire_abandoned_staging(pipeline)
            if not check.blocked:
                lines.append(
                    (
                        STATUS_YELLOW,
                        "预检期间配置已变更，请重新预检后再提交任务",
                    )
                )
        else:
            self.prepared_run = run
            self.prepared_pipeline = pipeline
            self.prepared_fingerprint = latched
        preview = pipeline.split_preview
        if preview is not None:
            self.config_page.set_preview(preview)
            lines.extend(self.preview_status_lines(preview))
        lines.extend(self.vram_summary_lines())
        expected = sum(int(size) for size in run.image_size.values())
        warning = self.space_warning(expected)
        if warning is not None:
            lines.append(warning)
        if not lines:
            lines.append((STATUS_INFO, "本地预检通过"))
        self.config_page.set_status_lines(lines)
        return lines

    def report_precheck_error(self, error: Any) -> List[Any]:
        """Report a failed pre-check without losing the estimate line.

        The `(model, task)` estimate of spec §5.1.3 does not depend on the
        dataset, so it is shown even when the dataset itself blocked the
        run (the space warning is skipped: there is no size to compare).
        """

        if isinstance(error, InterruptedError) or "已取消" in str(error):
            lines: List[Any] = [(STATUS_INFO, "本地预检已取消，未改动预览")]
        elif isinstance(error, PipelineError):
            lines = list(self.show_pipeline_error(error))
        else:
            lines = [(STATUS_RED, str(error))]
            self.config_page.set_status_lines(lines)
        lines.extend(self.vram_summary_lines())
        self.config_page.set_status_lines(lines)
        return lines

    def preview_status_lines(self, preview: Any) -> List[Any]:
        """The red blocking line of the preview (spec §5.2.8).

        The yellow warning lines live in the preview block itself
        (`ConfigPage.set_preview`), so this returns the blocking red line
        only - returning both would render every warning twice.
        """

        unresolved = list(getattr(preview, "unresolved", ()) or ())
        if not unresolved:
            return []
        return [
            (
                STATUS_RED,
                "两侧代表无法保证的类别：{0}".format(", ".join(unresolved)),
            )
        ]

    def vram_summary_lines(self) -> List[Any]:
        """The (model, task) estimate line of spec §5.1.3.

        The table is keyed the same way the weights list is; a server that
        keys it by the bare model stem (as the spec §3.6 example does) is
        covered by trying both spellings.
        """

        capabilities = self.capabilities
        if capabilities is None:
            return [
                (STATUS_INFO, "尚未取得服务端能力协商，无法显示显存估算上限")
            ]
        form = self.config_page.values()
        task = protocol_task(form.task)
        entry = self._vram_lookup(capabilities, form.model, task)
        if entry is None:
            reason = self._unschedulable_reason(
                capabilities, form.model, task
            )
            if reason:
                return [
                    (
                        STATUS_YELLOW,
                        "服务端把该 (模型, 任务) 标记为不可调度：{0}".format(
                            reason
                        ),
                    )
                ]
            return [
                (
                    STATUS_INFO,
                    "服务端显存表未覆盖该 (模型, 任务) 组合，提交时以服务端"
                    "估算为准",
                )
            ]
        max_batch = entry.get("max_batch")
        source = entry.get("source") or "-"
        if max_batch is None:
            return [
                (
                    STATUS_YELLOW,
                    "该 (模型, 任务) 组合尚无实测上限（source={0}）".format(
                        source
                    ),
                )
            ]
        return [
            (
                STATUS_INFO,
                "该 (模型, 任务) 的估算上限 max_batch={0}（source={1}）".format(
                    max_batch, source
                ),
            )
        ]

    def _vram_lookup(
        self, capabilities: Any, model: str, task: str
    ) -> Any:
        """`vram_table.entries` row for one model spelling."""

        for spelling in self._model_spellings(model):
            entry = capabilities.vram_entry(spelling, task)
            if entry is not None:
                return entry
        return None

    def _unschedulable_reason(
        self, capabilities: Any, model: str, task: str
    ) -> Optional[str]:
        for spelling in self._model_spellings(model):
            reason = capabilities.unschedulable_reason(spelling, task)
            if reason:
                return reason
        return None

    @staticmethod
    def _model_spellings(model: str) -> List[str]:
        """The weights file name and its bare stem (spec §3.6 keys)."""

        name = str(model or "")
        spellings = [name]
        stem = osp.splitext(name)[0]
        if stem and stem != name:
            spellings.append(stem)
        return spellings

    def space_warning(self, expected_bytes: int) -> Optional[Any]:
        """Yellow line when the expected upload may not fit (§5.1.3).

        Only issued when a connection test already reported
        `work_dir.free_gb`; a missing value never blocks or warns.
        """

        if not expected_bytes:
            return None
        payload = self.health_payload
        if not isinstance(payload, dict):
            return None
        work_dir = payload.get("work_dir")
        if not isinstance(work_dir, dict):
            return None
        free_gb = work_dir.get("free_gb")
        if not isinstance(free_gb, (int, float)) or isinstance(free_gb, bool):
            return None
        needed_gb = expected_bytes / float(1000**3)
        if needed_gb <= float(free_gb):
            return None
        return (
            STATUS_YELLOW,
            "本次预计上传 {0:.2f} GB，服务端工作目录仅剩 {1} GB，"
            "空间可能不足；服务端仍以 413 兜底".format(needed_gb, free_gb),
        )

    def show_pipeline_error(self, error: PipelineError) -> List[Any]:
        """Render one blocking local condition (spec §5.2.5, §5.2.8)."""

        lines: List[Any] = [(STATUS_RED, str(error))]
        for issue in getattr(error, "issues", ()) or ():
            text = getattr(issue, "text", None)
            lines.append(
                (STATUS_RED, str(text()) if callable(text) else str(issue))
            )
        self.config_page.set_status_lines(lines)
        self.log_debug("本地预检阻断：{0}".format(error.status_text()))
        return lines

    def show_server_receipt(self, warnings: Any) -> List[Any]:
        """Informational lines from a server receipt (spec §3.9 channel B)."""

        from ..packer import warning_info_issues

        lines = [
            (
                STATUS_YELLOW if not issue.blocking else STATUS_RED,
                issue.text(),
            )
            for issue in warning_info_issues(warnings)
        ]
        if lines:
            self.config_page.status_row.append_line(
                (STATUS_INFO, "服务端回执：")
            )
            for line in lines:
                self.config_page.status_row.append_line(line)
        return lines

    def _on_dataset_changed(self, _path: str) -> None:
        self.config_page.clear_preview()
        self.invalidate_prepared()

    # ------------------------------------------------------------ workers

    def register_worker(self, worker: Any, operation: str = "") -> Any:
        """Enqueue one worker (spec §5.1.4: five enqueue points)."""

        self.workers.append(worker)
        if operation:
            # Step 0 reads the operation off the worker, not off a side
            # table: the label it shows must be the one really running.
            self.set_worker_operation(worker, operation)
        generation = self._generation

        def _on_finished(w: Any = worker) -> None:
            self.release_worker(w)

        finished = getattr(worker, "finished", None)
        if finished is not None and hasattr(finished, "connect"):
            finished.connect(_on_finished)
        setattr(worker, "generation", generation)
        return worker

    def release_worker(self, worker: Any) -> None:
        """Dequeue first, destroy later (spec §5.1.4 step 5 order)."""

        for operation, label in list(self._pending_operations.items()):
            if label and not self.workers_with_operation(operation):
                self._pending_operations.pop(operation, None)
        try:
            self.workers.remove(worker)
        except ValueError:
            return

    def workers_with_operation(self, operation: str) -> List[Any]:
        return [
            worker
            for worker in self.workers
            if getattr(worker, "operation", None) == operation
        ]

    def set_worker_operation(self, worker: Any, operation: str) -> Any:
        """Tag one worker with its operation label (step 0 wording)."""

        setattr(worker, "operation", operation)
        if operation:
            self._pending_operations[operation] = OPERATION_LABELS.get(
                operation, operation
            )
        return worker

    def pending_operations(self) -> List[str]:
        """The operation labels of step 0, in a stable order.

        The labels are derived from the live workers first, so a worker
        that was never tagged still produces a confirmation.  A worker
        without a tag counts as the generic "operation" rather than being
        silently skipped: skipping it would let the window close without
        asking while work is in progress.
        """

        labels: List[str] = []
        for operation in OPERATION_ORDER:
            if self.workers_with_operation(operation):
                labels.append(OPERATION_LABELS[operation])
        known = sum(
            len(self.workers_with_operation(operation))
            for operation in OPERATION_ORDER
        )
        if len(self.workers) > known:
            labels.append(OPERATION_LABELS[GENERIC_OPERATION])
        return labels

    def active_workers(self) -> List[Any]:
        """The workers that are not finished yet."""

        alive: List[Any] = []
        for worker in list(self.workers):
            is_finished = getattr(worker, "isFinished", None)
            try:
                done = bool(is_finished()) if callable(is_finished) else False
            except RuntimeError:  # pragma: no cover - deleted wrapper
                continue
            if not done:
                alive.append(worker)
        return alive

    def _is_stale(self, generation: Any) -> bool:
        """Drop callbacks of a superseded run (spec §5.1.4 step 1)."""

        if generation is None:
            return False
        return int(generation) != self._generation

    # ------------------------------------------------------- close machine

    def confirm_close(self) -> bool:
        """Step 0: the secondary confirmation, once per close."""

        labels = self.pending_operations()
        if not labels:
            return True
        if self._confirm_close is not None:
            return bool(self._confirm_close(self))
        answer = QtWidgets.QMessageBox.question(
            self,
            self.tr(CLOSE_CONFIRM_TITLE),
            self.tr(
                CLOSE_CONFIRM_TEMPLATE.format(len(labels), " / ".join(labels))
            ),
            QtWidgets.QMessageBox.StandardButton.Yes
            | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        return answer == QtWidgets.QMessageBox.StandardButton.Yes

    def start_close_wait(self) -> QtCore.QTimer:
        """Step 4's own timer; the slot re-evaluates the criterion."""

        if getattr(self, "_close_timer", None) is None:
            self._close_timer = QtCore.QTimer(self)
            self._close_timer.setInterval(CLOSE_WAIT_MS)
            self._close_timer.timeout.connect(self._recheck_close)
        return self._close_timer

    def _recheck_close(self) -> None:
        if workers_finished(self):
            QtCore.QTimer.singleShot(0, self.close)
            return
        if self._stop_started_at is None:
            self._stop_started_at = _now()
        elapsed = _now() - self._stop_started_at
        for worker in self.active_workers():
            if elapsed < WORKER_WARNING_SECONDS:
                break
            if id(worker) in self._warning_logged:
                continue
            self._warning_logged.add(id(worker))
            _LOGGER.warning(
                "remote training worker %s is still running after %ss; "
                "waiting without killing it (spec §5.1.4 step 4)",
                type(worker).__name__,
                WORKER_WARNING_SECONDS,
            )

    def disconnect_business_signals(self) -> int:
        """Step 2: drop every UI / ledger updating connection."""

        dropped = 0
        for worker in list(self.workers):
            for channel in CLOSE_CHANNELS:
                signal = getattr(worker, channel, None)
                if signal is None or not hasattr(signal, "disconnect"):
                    continue
                try:
                    signal.disconnect()
                    dropped += 1
                except TypeError:
                    continue
        return dropped

    def cancel_workers(self) -> int:
        """Step 3: set the flag and wake every waiting worker."""

        woken = 0
        for worker in self.active_workers():
            for name in ("cancel", "stop", "requestInterruption"):
                hook = getattr(worker, name, None)
                if callable(hook):
                    try:
                        hook()
                        woken += 1
                    except Exception:  # pragma: no cover - defensive
                        pass
                    break
            event = getattr(worker, "_cancel_event", None)
            if event is not None and hasattr(event, "set"):
                event.set()
        return woken

    def _set_stopping(self) -> None:
        """Say what the window is waiting for and disable the close button."""

        names = ", ".join(
            type(worker).__name__ for worker in self.active_workers()
        )
        lines = [(STATUS_YELLOW, STOPPING_TEXT.format(names or "worker"))]
        # Every page that can show a status line says what is happening,
        # each one on its own row (no page shares another page widget).
        self.config_page.set_status_lines(lines)
        for page in (PAGE_JOBS, PAGE_DETAIL, PAGE_RESULTS):
            for row in self._status_rows(page):
                row.set_lines(lines)
        self.setWindowFlag(
            QtCore.Qt.WindowType.WindowCloseButtonHint, False
        )
        self.show()

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802
        """The only close path of the feature (spec §5.1.4 steps 0 to 6)."""

        if self._closing_in_progress:
            event.ignore()
            return
        self._closing_in_progress = True
        try:
            if not self._closing:
                if self._pending_quit:
                    # The guard asked the very same question for this exit
                    # request before it called close(); asking again would
                    # be a second, word for word identical dialog
                    # (spec §5.1.4).
                    self._closing = True
                elif not self.confirm_close():
                    event.ignore()
                    return
                else:
                    self._closing = True
            # step 1: invalidate every in flight response
            self._generation += 1
            # step 2: no business signal may reach a dying widget
            self.disconnect_business_signals()
            # step 3: wake the cancellation of every worker
            self.cancel_workers()
            # step 4: the single criterion, evaluated without blocking
            if not workers_finished(self):
                event.ignore()
                # The stopping flag is the observable "step 4 vetoed
                # this close" marker for the callers and the tests.
                self._stopping = True
                self._set_stopping()
                self.start_close_wait()
                if not self._close_timer.isActive():
                    self._close_timer.start()
                return
            # step 5: dequeue first, destroy afterwards
            self.release_finished_workers()
            self._stop_wait()
            # step 6: the staging cleanup of the data pipeline
            self.cleanup()
            event.accept()
        finally:
            self._closing_in_progress = False

    def release_finished_workers(self) -> List[Any]:
        """Remove and schedule the finished workers (order is frozen)."""

        finished: List[Any] = []
        for worker in list(self.workers):
            is_finished = getattr(worker, "isFinished", None)
            try:
                done = bool(is_finished()) if callable(is_finished) else False
            except RuntimeError:  # pragma: no cover - deleted wrapper
                continue
            if done:
                self.workers.remove(worker)
                finished.append(worker)
        for worker in finished:
            delete = getattr(worker, "deleteLater", None)
            if callable(delete):
                delete()
        return finished

    def _stop_wait(self) -> None:
        timer = getattr(self, "_close_timer", None)
        if timer is not None and timer.isActive():
            timer.stop()

    def cleanup(self) -> None:
        """Step 6: settle every staging area the window still holds.

        Step 4 has already proven that no worker is running, so the
        liveness guards of _retire_abandoned_staging do not apply here:
        a frozen run that is still submittable is retired too, because
        the window itself is going away.
        """

        pipelines = [self.pipeline, self.prepared_pipeline]
        if self._submitting is not None:
            pipelines.append(self._submitting[1])
        self._submitting = None
        self.prepared_run = None
        self.prepared_pipeline = None
        self.prepared_fingerprint = None
        for pipeline in pipelines:
            self._finish_pipeline_staging(pipeline)

    # ----------------------------------------------------------- download

    def ask_save_path(self, default_name: str, filter_text: str = "") -> str:
        """The one save dialog of the result step (spec §5.6.3).

        The path is always the user's: the feature never picks a target
        itself, so it can never overwrite a path the user did not
        confirm, and the zip is never unpacked anywhere else.
        """

        start = osp.join(osp.expanduser("~"), str(default_name or ""))
        chosen, _selected = QtWidgets.QFileDialog.getSaveFileName(
            self,
            self.tr("保存结果"),
            start,
            filter_text or "所有文件 (*)",
        )
        return str(chosen or "")

    def download_results(self, job_id: str, path: str = "") -> Any:
        """Stream GET /jobs/{id}/download into a chosen file (§5.6.3).

        `path` is the parameterised input of the tests; the production
        caller (the 下载结果 button of the three pages) leaves it empty
        and gets the save dialog with the default name <job_id>.zip.
        """

        job_id = str(job_id or "")
        if not job_id:
            return None
        if not path:
            path = self.ask_save_path("{0}.zip".format(job_id))
        if not path:
            return None
        return self.start_download(job_id, "", str(path), archive=True)

    def download_job_file(
        self, job_id: str, file_id: str, path: str = ""
    ) -> Any:
        """Stream one files[] entry by its opaque file_id (§3.10).

        The route parameter is the identifier the manifest carried; the
        displayed path is never turned into a URL (spec §3.10.1).
        """

        job_id = str(job_id or "")
        file_id = str(file_id or "")
        if not job_id or not file_id:
            return None
        if not path:
            default = self.results_page.entry_save_name(job_id, file_id)
            path = self.ask_save_path(default)
        if not path:
            return None
        return self.start_download(job_id, file_id, str(path), archive=False)

    def start_download(
        self, job_id: str, file_id: str, path: str, *, archive: bool
    ) -> Any:
        """Run one download in a worker (spec §5.6.3, §5.1.4).

        The worker owns the socket and the file, so the GUI thread never
        blocks and step 3 of the close machine can cancel the transfer
        inside the chunk loop.
        """

        client = self.ensure_client()

        def open_stream() -> Any:
            if archive:
                return client.open_job_download(job_id)
            return client.open_job_file(job_id, file_id)

        worker = DownloadWorker(
            open_stream,
            path,
            self,
            job_id=job_id,
            file_id=file_id,
            archive=archive,
        )
        generation = self._generation
        self.download_worker = worker
        worker.progress.connect(
            lambda received, total, g=generation: self._on_download_progress(
                received, total, g
            )
        )
        worker.download_done.connect(
            lambda outcome, g=generation: self._on_download_done(outcome, g)
        )
        worker.failed.connect(
            lambda error, g=generation: self._on_download_failed(
                error, job_id, g
            )
        )
        self.register_worker(worker, "download")
        self.results_page.set_status(DOWNLOAD_START_TEXT)
        self.log_debug(
            "开始下载 {0}".format(
                "结果压缩包" if archive else "file_id={0}".format(file_id)
            )
        )
        worker.start()
        return worker

    def _on_download_progress(
        self, received: Any, total: Any, generation: Any = None
    ) -> str:
        if self._is_stale(generation):
            return ""
        return self.results_page.set_download_progress(received, total)

    def _on_download_done(
        self, outcome: Any, generation: Any = None
    ) -> None:
        """Save the result: path, ledger and debug area (spec §5.6.3)."""

        if outcome is None or self._is_stale(generation):
            return
        if getattr(outcome, "cancelled", False):
            self.results_page.clear_download_progress()
            self.results_page.set_status(DOWNLOAD_CANCELLED_TEXT)
            self.log_debug("下载已取消：{0}".format(outcome.path))
            return
        self.results_page.set_saved_path(outcome.path)
        self.write_download_path(
            getattr(outcome, "job_id", ""), outcome.path
        )
        self.log_debug(
            "已保存到 {0}（{1} 字节）".format(
                outcome.path, outcome.bytes_written
            )
        )
        if outcome.content_length is None:
            self.log_debug(
                "该响应没有 Content-Length，进度按已下载量显示"
            )
        elif not outcome.complete:
            self.log_debug(
                "注意：收到的字节数与 Content-Length 不一致（{0}/{1}）"
                .format(outcome.bytes_written, outcome.content_length)
            )

    def _on_download_failed(
        self, error: Any, job_id: str = "", generation: Any = None
    ) -> None:
        """The spec §5.6.4 exit for a failed download.

        Only this exit clears the progress bar: a failed *summary* read
        must not wipe the progress line of a download that is still
        streaming (the next chunk re-renders it, but the gap is real).
        """

        self._render_artifact_error(
            error,
            job_id,
            generation,
            prefix="下载失败",
            clear_download=True,
        )

    def _render_artifact_error(
        self,
        error: Any,
        job_id: str = "",
        generation: Any = None,
        *,
        prefix: str = "下载失败",
        clear_download: bool = False,
        wake: bool = True,
    ) -> None:
        """The one failure exit of an artifact request (spec §5.6.4).

        The download and the summary read share it: a mapped code goes
        through the same rendering as every other failure (text, the
        "refresh the manifest" flag of 404 ARTIFACT_NOT_FOUND, the
        settings highlight of 401), anything unmapped becomes a plain
        line plus a debug entry.  `clear_download` is set by the
        download exit only, so a summary failure leaves a running
        transfer's bar alone.  `wake` is forwarded to the view: the
        download exit keeps the manifest refresh of ARTIFACT_NOT_FOUND,
        the summary exit does not (see _on_summary_failed).
        """

        if self._is_stale(generation):
            return
        if clear_download:
            self.results_page.clear_download_progress()
        try:
            view = client_error_view(error)
        except Exception:  # pragma: no cover - defensive
            view = None
        if view is None:
            self.results_page.set_status("{0}：{1}".format(prefix, error))
            self.log_debug("{0}：{1}".format(prefix, error))
            return
        self.poll_errors.append(view)
        self._apply_error_view(view, str(job_id or ""), wake=wake)

    def open_artifacts_directory(self, job_id: str = "") -> bool:
        """Open the folder of this job's last download (spec §5.1.3).

        `download_path` is the ledger field that exists for exactly this
        button (spec §5.3.2); without it the readable hint of
        open_directory() is shown instead of a silent no-op.
        """

        job_id = str(job_id or "") or str(self.detail_page.job_id or "")
        path = ""
        record = self._ledger_record(job_id) if job_id else None
        if record is not None:
            path = str(getattr(record, "download_path", "") or "")
        return self.open_directory(path)

    def load_manifest(self, job_id: str = "") -> Any:
        """Read the manifest of a terminal job once (spec §5.6.2).

        The polling tick skips route 13 once the job is terminal (spec
        §5.5.2): the manifest of a finished job is frozen, so the
        routine tick never asks again.  A job that is ALREADY terminal
        when the results page is opened would then never get a single
        row, and the summary - which is read out of that very table -
        would stay empty as well.  This is the one explicit read that
        covers the case, and it runs in a worker (a network read never
        happens on the GUI thread).

        The latch is per job: once the manifest was read for `job_id`
        the routine 60 s tick of a terminal job does not ask again.  A
        tick that carries rows of its own (a running job) releases the
        latch, so the next terminal edge reads it exactly once more.
        """

        job_id = str(job_id or "")
        if not job_id or str(self.results_page.job_id) != job_id:
            return None
        if self._manifest_loaded == job_id:
            return None
        if self._manifest_pending == job_id:
            return None
        client = self.ensure_client()
        worker = ManifestWorker(
            lambda: client.list_job_files(job_id, include_partial=True),
            self,
            job_id=job_id,
        )
        generation = self._generation
        self.manifest_worker = worker
        self._manifest_pending = job_id
        worker.manifest_loaded.connect(
            lambda files, g=generation, k=job_id: self._on_manifest_loaded(
                files, k, g
            )
        )
        worker.failed.connect(
            lambda error, g=generation, k=job_id: self._on_manifest_failed(
                error, k, g
            )
        )
        self.register_worker(worker, "download")
        worker.finished.connect(
            lambda k=job_id: self._clear_manifest_pending(k)
        )
        # A cancelled read emits nothing, so the pending job is also
        # released when the worker ends: the next entry into the page is
        # allowed to retry it.
        worker.start()
        return worker

    def _clear_manifest_pending(self, key: Any = None) -> None:
        if self._manifest_pending == key:
            self._manifest_pending = None

    def _on_manifest_loaded(
        self, files: Any, job_id: str = "", generation: Any = None
    ) -> None:
        """Render the explicit manifest read (spec §5.6.2)."""

        self._clear_manifest_pending(job_id)
        if self._is_stale(generation):
            return
        if str(self.results_page.job_id) != str(job_id):
            # The page moved on while the read was in flight: the rows
            # belong to another job and must not answer a lookup for
            # this one.
            return
        self._manifest_loaded = job_id
        rows = list(files or [])
        job = getattr(self.results_page, "job", None)
        if job is not None:
            self.results_page.set_job(job, rows)
        else:  # pragma: no cover - the tick renders before the read
            self.results_page.set_files(rows, job_id=job_id)
        self.log_debug(
            "已加载产物清单（{0} 条，job_id={1}）".format(
                len(rows), job_id
            )
        )
        self.load_summary(job_id)

    def _on_manifest_failed(
        self, error: Any, job_id: str = "", generation: Any = None
    ) -> None:
        """An unreadable manifest is reported, never fatal.

        The latch is NOT set: the next 60 s tick of the terminal job
        reads the manifest once more, which is the self healing path of
        a transient failure.  wake=False (see _on_summary_failed) keeps
        the "refresh the manifest" reset of a 404 away from a table
        that this very read could not fill.
        """

        self._clear_manifest_pending(job_id)
        self._render_artifact_error(
            error,
            job_id,
            generation,
            prefix="读取产物清单失败",
            wake=False,
        )

    def load_summary(self, job_id: str = "") -> Any:
        """Fetch `summary.json` in memory for the results page (§5.6.3).

        The artifact is addressed by its manifest `file_id` (route 14)
        and parsed from the response body: nothing is written to disk,
        because the feature never unpacks or copies a result.  It runs
        in a worker (a network read never happens on the GUI thread).

        The latch is per WINDOW SESSION, not per job: the dialog lives
        until the window is closed (spec §5.1.2), so the key carries the
        manifest row content (`sha256`) next to the identity.  A manual
        resume rewrites `jobs/<job_id>/summary.json` under the SAME path
        and therefore the same file_id (spec §3.10.1), so an identity
        only key would keep showing the metrics of the previous attempt
        forever.  The manifest row also has to belong to `job_id`: rows
        rendered for another job must never answer this lookup, and a
        job whose manifest has no summary row shows an empty area.
        """

        job_id = str(job_id or "")
        if not job_id or str(self.results_page.files_job_id) != job_id:
            return None
        entry = self.results_page.summary_entry()
        if entry is None:
            self.results_page.set_summary(None)
            self._summary_key = None
            return None
        file_id = str(_field(entry, "file_id") or "")
        if not file_id:
            return None
        # Spec §3.2.2 #13 declares sha256 *and* mtime on every manifest
        # row, so the fallback never invents a field: it keeps a content
        # / provenance component even for a row that violates the
        # contract by carrying no sha256 (item 2 of the B7 review).
        content = _field(entry, "sha256") or _field(entry, "mtime") or ""
        key = (job_id, file_id, str(content))
        if self._summary_key == key or self._summary_pending == key:
            return None
        client = self.ensure_client()
        worker = SummaryWorker(
            lambda: client.open_job_file(job_id, file_id),
            self,
            job_id=job_id,
            file_id=file_id,
        )
        generation = self._generation
        self.summary_worker = worker
        self._summary_pending = key
        worker.summary_loaded.connect(
            lambda payload, g=generation, k=key: self._on_summary_loaded(
                payload, k, g
            )
        )
        worker.failed.connect(
            lambda error, g=generation: self._on_summary_failed(
                error, job_id, g
            )
        )
        self.register_worker(worker, "download")
        worker.finished.connect(lambda k=key: self._clear_summary_pending(k))
        # A cancelled read emits nothing, so the pending key is also
        # released when the worker ends: the next entry into the results
        # page is allowed to retry it.
        worker.start()
        return worker

    def _clear_summary_pending(self, key: Any = None) -> None:
        if self._summary_pending == key:
            self._summary_pending = None

    def _on_summary_loaded(
        self, payload: Any, key: Any = None, generation: Any = None
    ) -> None:
        """Render the parsed summary (spec §5.6.3)."""

        self._summary_pending = None
        if self._is_stale(generation):
            return
        self._summary_key = key
        self.results_page.set_summary(payload)
        fields = len(payload) if isinstance(payload, dict) else 0
        parts = tuple(key or ()) + ("", "", "")
        self.log_debug(
            "已加载 summary.json（{0} 个字段，file_id={1}，sha256={2}）"
            .format(fields, parts[1], parts[2][:8])
        )

    def _on_summary_failed(
        self, error: Any, job_id: str = "", generation: Any = None
    ) -> None:
        """A missing / unreadable summary is reported, never fatal.

        The exit renders the §5.6.4 wording but never self-refreshes
        (wake=False).  The manifest that lists this file_id is already
        on screen, so the ARTIFACT_NOT_FOUND reset would clear the
        artifact table and wake the results poller at once, whose tick
        re-reads the very same file_id: a refresh loop bounded only by
        the round trip.  For a terminal job the manifest comes from
        ManifestWorker instead, and a cleared table is refilled by the
        next terminal tick (reset_results_files releases the latch);
        the routine tick retries the read at its own cadence.
        """

        self._summary_pending = None
        self._render_artifact_error(
            error,
            job_id,
            generation,
            prefix="读取 summary.json 失败",
            wake=False,
        )

    def write_download_path(self, job_id: str, path: str) -> bool:
        """Write the landing point back into the ledger (§5.6.3)."""

        job_id = str(job_id or "")
        if not job_id or not path:
            return False
        try:
            ledger = self.store.load_ledger()
            record = ledger.record(job_id)
            if record is None:
                return False
            record.download_path = str(path)
            ledger.upsert_record(record)
            self.store.save_ledger(ledger)
            return True
        except Exception as exc:  # pragma: no cover - defensive
            _LOGGER.warning("could not persist download_path: %s", exc)
            return False

    def open_directory(self, path: str = "") -> bool:
        """Open the folder of a saved result (spec §5.6.3)."""

        target = str(path or "") or str(self.results_page.last_saved_path)
        if not target:
            self.results_page.set_status("还没有已保存的结果")
            return False
        folder = target if osp.isdir(target) else osp.dirname(target)
        if not folder or not osp.isdir(folder):
            self.results_page.set_status(
                "目录不存在：{0}".format(folder or target)
            )
            return False
        self.log_debug("打开所在目录：{0}".format(folder))
        return bool(
            QtGui.QDesktopServices.openUrl(
                QtCore.QUrl.fromLocalFile(folder)
            )
        )

    # ----------------------------------------------------------- staging

    def reclaim_staging_leftovers(self) -> Optional[Any]:
        """Spec §5.1.5: scan the temp dir once and log the count.

        Runs on the start-up path of the window, so a crash leftover is
        reclaimed the next time the feature is opened instead of piling
        up until the operating system decides to sweep its own temp dir.
        It is synchronous on the GUI thread by design (the scan is one
        listdir plus one ledger read per candidate directory, and a large
        reclamation is bounded by the TTL, never by in-app work).
        """

        try:
            report = self.store.reclaim_staging_leftovers(
                temp_root=self._staging_temp_root
            )
        except Exception as exc:  # pragma: no cover - defensive
            _LOGGER.warning("could not scan the staging leftovers: %s", exc)
            return None
        self.staging_reclaim = report
        text = "启动扫描 staging 遗留：回收 {0} 个，释放 {1:.1f} MB".format(
            report.reclaimed_count, report.freed_mb
        )
        _LOGGER.info(
            "staging scan at start-up: reclaimed %d director(ies), "
            "freed %.1f MB",
            report.reclaimed_count,
            report.freed_mb,
        )
        self.log_debug(text)
        # Spec §5.3.4 tells the application to make a recovered ledger
        # visible, and spec §5.1.5 gives the debug area exactly that job.
        # The predicate is sticky (the quarantine copy is renamed aside,
        # never deleted), so this one line explains why staging areas are
        # kept for the rest of the session and only the fixed seven day
        # TTL scan reclaims them.
        try:
            degraded = bool(self.store.ledger_is_degraded())
        except Exception:  # pragma: no cover - defensive
            degraded = False
        if degraded:
            self.log_debug(
                "台账曾损坏并被隔离（corrupt 副本保留在台账目录）："
                "本次会话内 staging 一律按「仍被引用」保留，"
                "仅固定 7 天 TTL 扫描会回收"
            )
        return report

    def keep_staging(self) -> bool:
        """The one production reader of settings.keep_staging (§5.1.5)."""

        try:
            return bool(self.store.load_settings().keep_staging)
        except Exception:  # pragma: no cover - defensive
            return False

    def finish_staging(self, staging_dir: Any) -> Optional[str]:
        """The two mutually exclusive branches of spec §5.1.5.

        Called wherever one task ends (the submit chain and step 6 of
        the close machine).  Returns the kept absolute path when
        keep_staging is on, otherwise None after removing this run's
        staging directory.  Idempotent: an already gone directory is a
        no-op.  The persistent pending/<id>/ reclamation data is never
        touched here.
        """

        path = str(staging_dir or "")
        if not path:
            return None
        absolute = osp.abspath(path)
        if not osp.isdir(absolute):
            return None
        if self.keep_staging():
            # Branch ②: keep this run as a debugging artifact and make
            # the full absolute path visible in the log and in the debug
            # area (selectable / copyable, spec §5.1.5).
            _LOGGER.info("staging kept (keep_staging: true): %s", absolute)
            self.log_debug(
                "本次 staging 已保留（keep_staging: true）：{0}".format(
                    absolute
                )
            )
            return absolute
        # Spec §5.1.5, hard exemption: a directory an unfinished pending
        # entry still points at (or one the degraded-ledger safety net
        # covers) is never freed here either - a replay may be reading it
        # right now (DatasetPacker.restore reuses entry.staging_dir).
        # The next retire pass reclaims it once the entry is submitted or
        # voided; the fixed seven day TTL scan is the last resort.
        if self.staging_is_referenced(absolute):
            _LOGGER.info(
                "staging is still referenced by an unfinished pending "
                "entry (or the ledger is degraded), keeping it: %s",
                absolute,
            )
            self.log_debug(
                "staging 仍被未完成的待提交条目引用（或台账处于降级状态），"
                "暂不回收：{0}".format(absolute)
            )
            return None
        # Branch ①: drop this run's staging area (the replayable archive
        # already lives in pending/<id>/).  ignore_errors keeps a failure
        # from turning a finished task into an error.
        _LOGGER.info("staging removed (keep_staging: false): %s", absolute)
        shutil.rmtree(absolute, ignore_errors=True)
        return None

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:  # noqa: N802
        """Esc takes the same path as the close button (spec §5.1.4)."""

        if event.key() == QtCore.Qt.Key.Key_Escape:
            self.close()
            event.accept()
            return
        super().keyPressEvent(event)

    def reject(self) -> None:
        """Divert to close(); the QDialog default skips the machine."""

        self.close()

    # -------------------------------------------------------------- misc

    def log_debug(self, text: str) -> None:
        """One line into the append only debug area (spec §5.1.5)."""

        self.results_page.append_debug_line(text)

    def _available_height(self) -> int:
        screen = self.screen() or QtGui.QGuiApplication.primaryScreen()
        if screen is None:
            return 0
        return int(screen.availableGeometry().height())

    def apply_initial_size(self) -> QtCore.QSize:
        """Size the window to the page it opens on, once."""

        if self._initial_size_applied:
            return self.size()
        self._initial_size_applied = True
        content = self.stack.currentWidget()
        hint = content.sizeHint() if content is not None else self.sizeHint()
        height = max(int(hint.height()), MINIMUM_HEIGHT)
        available = self._available_height()
        if available > 0:
            height = min(height, available)
        self.resize(max(self.minimumWidth(), int(hint.width())), height)
        return self.size()

    def showEvent(self, event: QtGui.QShowEvent) -> None:  # noqa: N802
        super().showEvent(event)
        self.apply_initial_size()


def _now() -> float:
    """Monotonic seconds; only used to bound the step 4 warning."""

    return time.monotonic()
