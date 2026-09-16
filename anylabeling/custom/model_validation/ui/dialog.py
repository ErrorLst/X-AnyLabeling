"""Model validation sub window.

A non modal, standalone window reachable from the main window Tool
menu. It never touches the main window layout, business state or the
file list of the labeling session.
"""

from __future__ import annotations

import datetime
import os.path as osp
from typing import Any, Dict, List, Optional

from PyQt6 import QtCore, QtGui, QtWidgets

from .. import dataset
from .. import history
from .. import records as records_module
from ..app_config import (
    RATIO_MODE,
    AugmentParams,
    ValidationConfig,
    ValidationConfigError,
    load_classes_file,
    plan_aug_counts,
    validate_augment_params,
)
from ..async_scan import DirectoryScanScheduler
from ..exporter import CANCELLED_MESSAGE, ExportCancelled, export_zip
from ..main_window_bridge import MainWindowBridge
from ..pipeline import STAGE_STAGING, ValidationWorker
from ..report import build_report
from .config_page import ConfigPage
from .history_page import HistoryPage
from .progress_page import ProgressPage
from .results_page import ResultsPage

WINDOW_TITLE = "模型验证"
WINDOW_SIZE = (1024, 600)

# The three column augment grid and the two side by side views of the
# results page need the room; everything below that is wasted space on
# the configuration page, so the width stays fixed and only the height
# follows the content.
MINIMUM_WIDTH = 1024
# Lower bound of the window height: the results page needs a tall area
# (its own minimum height hint is ~592px), while the configuration page
# only needs ~544px plus the button row and is therefore never padded up
# to a full blown empty area again.
MINIMUM_HEIGHT = 600

EXPORT_PROGRESS_STYLE = "QProgressDialog { min-width: 360px; }"

# The line the configuration page shows after a cancel: the run is gone,
# nothing of it was saved and the next one starts from scratch.
CANCELLED_STATUS = "已取消，未保存任何结果"

# What the window answers a close click that arrives while an export
# runs: the archive is written in this thread and pumps the event loop
# (see export_zip), so closing right now would delete the very widgets
# the export is still writing to.
EXPORT_GUARD_STATUS = "导出进行中，请稍候再关闭"

# The debounce of the source directory scan: a path typed by hand asks
# for one walk of the folder once the typing paused, never one per
# keystroke.
SCAN_DEBOUNCE_MS = 400

# The debounce of the automatic follow: the results page announces every
# record it moves to, and a run of A / D presses or a quick series of
# clicks has to cost one jump - the last one - instead of one per step.
FOLLOW_DEBOUNCE_MS = 200

# What the preview line says while the scheduler walks the source
# directory: the pair count of that folder is not known yet.
SCAN_PENDING_TEXT = "扫描中…"

# The debounce of the state file write: a burst of marks or of record
# switches costs one write of the state.json of the run once the user
# stopped, never one per click.
STATE_SAVE_DEBOUNCE_MS = 800

# What the window says about a history command it refuses because the
# run on screen still owns the staging folder.
HISTORY_BUSY_STATUS = "运行或导出进行中，历史记录暂不可用"
# What the window says about a start pressed while the history page is
# the entry point on screen.
HISTORY_OPEN_STATUS = "历史记录已打开，请先关闭历史记录再开始验证"
# What the window says about a history entry it cannot restore.
HISTORY_MISSING_STATUS = "找不到该运行目录，无法恢复"
# What the history page says while its scan thread walks the folder.
HISTORY_SCAN_FAILED_TEMPLATE = "扫描历史失败：{message}"

# The one bounded wait a history scan thread gets before the window
# stops owning it. A walk of a busy temporary directory can take longer
# than this, and the close must not freeze on it: what is left running
# is detached from the window and finishes on its own (see
# _detach_history_scan).
HISTORY_SCAN_WAIT_MS = 3000


def stack_minimum_size(stack: QtWidgets.QStackedWidget) -> QtCore.QSize:
    """Return the size every page of a stacked widget needs.

    QStackedWidget reports the maximum of all its pages, so the window
    is never too small for the results page even while the compact
    configuration page is the one on screen.
    """

    minimum = stack.minimumSizeHint()
    return QtCore.QSize(
        max(int(minimum.width()), MINIMUM_WIDTH),
        max(int(minimum.height()), MINIMUM_HEIGHT),
    )


def initial_window_height(content_height: int, available_height: int) -> int:
    """Return the height a freshly opened window should start with.

    The height hugs the content (the configuration page padding the
    empty area the hard coded minimum used to add) and never exceeds
    the screen the window is about to be placed on.
    """

    wanted = max(int(content_height), MINIMUM_HEIGHT)
    if available_height > 0:
        wanted = min(wanted, int(available_height))
    return max(wanted, MINIMUM_HEIGHT)


# Every scan thread that was detached from its window while it was
# still walking the temporary directory. The set is the one owner of
# those threads: Qt destroys the QThread of a window with its parented
# children, and destroying a live one aborts the process, so a worker
# that outlived its window is unparented, kept here - which is what
# keeps its Python and C++ side alive - and asks to be released on its
# own finished signal.
_ORPHAN_SCANS: set = set()


def _orphan_scan_finished(worker: Any) -> None:
    """Release a detached scan thread once it left its run().

    The worker is handed in by the connection that made it an orphan:
    QtCore.QObject.sender() is not usable from a plain Python callable.
    Only the reference the registry holds is dropped here - the worker
    is never deleted from inside its own emission; a finished QThread
    is collected by itself once nothing points at it.
    """

    _ORPHAN_SCANS.discard(worker)
    waiter = getattr(worker, "wait", None)
    if callable(waiter):
        try:
            waiter(0)
        except TypeError:
            pass


class _HistoryScanWorker(QtCore.QThread):
    """Walk the temporary directory for the staging folders, off the UI.

    The walk belongs to a thread of its own because a temporary
    directory full of old runs costs one readdir plus one stat per
    candidate, and the window must keep painting while that happens.
    Nothing but the scan runs here and no exception may leave run():
    an error is reported on failed instead of aborting the process.
    """

    ready = QtCore.pyqtSignal(list)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, temp_root: Optional[str] = None, parent=None) -> None:
        super().__init__(parent)
        self.temp_root = temp_root

    def run(self) -> None:
        """List the runs of the temporary directory, never raising."""

        try:
            runs = history.list_runs(self.temp_root)
        except Exception as error:  # noqa: BLE001
            self.failed.emit(str(error))
            return
        self.ready.emit(list(runs))


class ModelValidationDialog(QtWidgets.QDialog):
    """Non modal window driving the whole model validation workflow."""

    def __init__(self, parent: Optional[Any] = None) -> None:
        super().__init__(parent)
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

        self.staging_root: str = ""
        self.previous_staging_roots: List[str] = []
        self.records: List[records_module.ValidationRecord] = []
        self.source_dataset_dir: str = ""
        self.source_pair_count: int = 0
        self.classes: List[str] = []
        self.meta: Dict[str, Any] = {}
        self.model_info: Dict[str, Any] = {}
        self.augment_summary: Dict[str, Any] = {}
        self.warnings: List[str] = []
        self.worker: Optional[ValidationWorker] = None
        # workers whose run was dropped by a cancel: they keep winding
        # down in the background and the window only waits for them when
        # it is closed
        self._detached_workers: List[ValidationWorker] = []
        self.last_export_path: str = ""
        # the token of the newest scan request, and whether its answer is
        # still waited for (see _on_dataset_changed)
        self._scan_token: int = 0
        self._scan_pending: bool = False
        # True while export_zip runs: the progress dialog is not modal
        # any more, so this flag is what refuses a second export
        self._exporting: bool = False
        # the window whose activation lifts this one, and that holds the
        # event filter of this dialog (see _install_activate_filter)
        self._activate_filter_target: Optional[Any] = None
        # the history page and the scan thread that fills it: the thread
        # is held here so a walk still running while the next one is
        # asked for is waited for instead of being destroyed alive
        self._history_scan: Optional[_HistoryScanWorker] = None
        # the summaries of the last finished scan: a restore looks its
        # run up here, and only a restorable entry is ever accepted
        self._history_runs: List[history.RunSummary] = []
        # scan threads that finished their run but were not waited for
        # yet: they are kept alive until the window closes, because
        # destroying a live QThread aborts the process
        self._detached_history_scans: List[_HistoryScanWorker] = []
        # True while the history page is the entry point on screen: it
        # blocks the start of a run, and both exits - a restore and the
        # close of the page - clear it again
        self._history_mode: bool = False
        # the debounced write of the state.json of the current run: a
        # burst of marks costs one write (see _schedule_state_save)
        self._state_dirty: bool = False
        self._state_timer = QtCore.QTimer(self)
        self._state_timer.setSingleShot(True)
        self._state_timer.setInterval(STATE_SAVE_DEBOUNCE_MS)
        self._state_timer.timeout.connect(self._save_state_now)

        self.stack = QtWidgets.QStackedWidget()
        self.config_page = ConfigPage()
        self.progress_page = ProgressPage()
        self.results_page = ResultsPage()
        # the history is the fourth page: the configuration, the progress
        # and the results page keep the indexes the window and its tests
        # have always used
        self.history_page = HistoryPage()
        self.stack.addWidget(self.config_page)
        self.stack.addWidget(self.progress_page)
        self.stack.addWidget(self.results_page)
        self.stack.addWidget(self.history_page)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.stack)
        # derived from the pages instead of a hard coded 1024 x 680: the
        # configuration page stays free of the empty area the fixed
        # minimum height used to add below the button row.
        self.setMinimumSize(stack_minimum_size(self.stack))

        self.config_page.start_requested.connect(self.start_validation)
        self.config_page.dataset_edit.textChanged.connect(
            self._on_dataset_changed
        )
        self.config_page.augment_check.toggled.connect(self._refresh_preview)
        self.config_page.ratio_spin.valueChanged.connect(self._refresh_preview)
        self.config_page.judge_augmented_check.toggled.connect(
            self._refresh_preview
        )

        # the progress page carries a single control, the cancel: it
        # ends the run, drops it and returns to the form by itself
        self.progress_page.cancel_requested.connect(self.cancel_validation)

        self.results_page.toggle_deleted.connect(self.on_toggle_deleted)
        self.results_page.toggle_export.connect(self.on_toggle_export)
        self.results_page.export_requested.connect(self.export_zip_dialog)
        # the follow of the record on screen: the results page says which
        # record it shows, this window opens it in the main labeling
        # window once the switching stopped (see _follow_current_record)
        self.results_page.current_record_changed.connect(
            self._on_current_record_changed
        )

        # the history page asks for the three things it cannot do itself:
        # the scan of the temporary directory, a restore and the way back
        # to the form
        self.config_page.history_requested.connect(self.show_history)
        self.history_page.refresh_requested.connect(self.refresh_history)
        self.history_page.restore_requested.connect(self.restore_run)
        self.history_page.close_requested.connect(self._close_history)

        # the enumeration of the source folder runs behind a worker
        # thread and a debounce: the slot that watches the path line edit
        # only ever reads a cache (see _on_dataset_changed)
        self.scan_scheduler = DirectoryScanScheduler(self)
        self.scan_scheduler.scan_ready.connect(self._on_scan_ready)
        self.scan_scheduler.scan_failed.connect(self._on_scan_failed)

        # the bridge owns every jump to the main window: the window this
        # tool was opened from is the parent, and a standalone start
        # passes None, which degrades every jump to a line of status
        self.bridge = MainWindowBridge(self._main_window(), parent=self)
        self.bridge.status_message.connect(self._show_status_message)
        self.bridge.record_changed.connect(self._on_record_changed)

        # the record the results page settled on is opened 200 ms after
        # the last switch: the id waiting for that timer, the id that was
        # opened last (never opened twice) and the one line a standalone
        # start is allowed to say about a follow it cannot do
        self._pending_record_id = ""
        self._followed_record_id = ""
        self._follow_refusal_shown = False
        self.follow_timer = QtCore.QTimer(self)
        self.follow_timer.setSingleShot(True)
        self.follow_timer.setInterval(FOLLOW_DEBOUNCE_MS)
        self.follow_timer.timeout.connect(self._follow_current_record)

        self._show_deferred_warnings()

    # ----------------------------------------------------------------- setup
    def _show_deferred_warnings(self) -> None:
        """Report the multi image augmentations that are not implemented."""

        from ..app_config import MULTI_IMAGE_AUGMENTATIONS

        self.config_page.set_status(
            self.tr("多图增强不实现：") + ", ".join(MULTI_IMAGE_AUGMENTATIONS),
        )

    # --------------------------------------------------------- main window
    def _main_window(self) -> Optional[Any]:
        """Return the main labeling window this tool was opened from.

        The launcher hands the labeling window in as the parent of this
        one; a start of its own (python -m ...) passes None, and every
        jump then degrades to a line of status instead of an exception. A
        parent that cannot load a file is not a labeling window either and
        is treated the same way, so the bridge never calls into an
        unrelated widget.
        """

        parent = self.parent()
        if parent is None or not callable(
            getattr(parent, "load_file", None)
        ):
            return None
        return parent

    # ------------------------------------------------------ raise on activate
    def eventFilter(  # noqa: N802
        self, watched: QtCore.QObject, event: QtCore.QEvent
    ) -> bool:
        """Lift this window when the main window is activated.

        This is the filter the main window carries (see
        _install_activate_filter), and the only hook of the feature: an
        activation of the main window is exactly the moment this window
        would end up behind it.

        Only activations are of interest here. An event of any other
        kind is passed on untouched, and no event is ever swallowed -
        the main window handles every one of its own exactly as it did
        before the filter was installed. Nothing is raised from the
        activation of this window itself either: the window is on top
        when that happens, and a second raise from there would only
        double the work of the window manager.
        """

        if event.type() == QtCore.QEvent.Type.WindowActivate:
            self._raise_above_main_window()
        return super().eventFilter(watched, event)

    def _activate_target(self) -> Optional[Any]:
        """Return the window whose activation lifts this one.

        An activation is delivered to the top level window, and the
        labeling widget handed in as the parent is not one of those (it
        is a plain widget inside the main window): the widget window()
        answers is the one to watch. A parent that is already top level
        - a standalone start of this window, a test stub - answers
        itself. A parent without that API is watched directly instead.
        """

        main_window = self._main_window()
        if main_window is None:
            return None
        getter = getattr(main_window, "window", None)
        if not callable(getter):
            return main_window
        try:
            return getter() or main_window
        except RuntimeError:
            # the main window was destroyed under the filter
            return None

    def _install_activate_filter(self) -> None:
        """Watch the main window for its own activations, once.

        The call is idempotent and repeated on every show: a Launcher
        hands the main window in as the constructor parent, but this
        method does not depend on that being true already. Installing
        the filter again on the very same object is expensive and
        pointless, so the target is compared first.
        """

        target = self._activate_target()
        if target is None:
            return
        if (
            self._activate_filter_target is not None
            and self._activate_filter_target is not target
        ):
            self._remove_activate_filter()
        if self._activate_filter_target is not None:
            return
        try:
            target.installEventFilter(self)
        except RuntimeError:
            # the main window is on its way out: no filter, no raise
            return
        self._activate_filter_target = target

    def _remove_activate_filter(self) -> None:
        """Drop the filter installed on the main window, if any.

        The filter is held by another widget, so it is removed on close:
        a stale dialog would otherwise keep watching the main window and
        raising itself from the grave.
        """

        target = self._activate_filter_target
        self._activate_filter_target = None
        if target is None:
            return
        try:
            target.removeEventFilter(self)
        except RuntimeError:
            # the main window is already gone, so is the filter
            pass

    def _raise_above_main_window(self) -> None:
        """Queue one raise for the next turn of the event loop.

        The activation of the main window is still being delivered while
        this runs: the window manager is the one that would put this
        window back behind the main window, and it settles the stacking
        order after the activation is over. One turn of the event loop
        is therefore what makes the raise stick instead of being the
        losing half of a race with the window manager.

        The user is the one in charge of this window: a hidden or
        minimized one is left exactly as it is. Nothing here activates
        anything either - the raise must not take the keyboard away from
        the canvas the user is editing in.
        """

        if not self.isVisible() or self.isMinimized():
            return
        QtCore.QTimer.singleShot(0, self._raise_deferred)

    def _raise_deferred(self) -> None:
        """Run the raise the activation filter queued.

        The queued call may arrive after the window was closed and its
        C++ side deleted: a RuntimeError there is the ordinary end of a
        window, not a failure.
        """

        try:
            self.raise_()
        except RuntimeError:
            pass

    def _show_status_message(self, message: str) -> None:
        """Write one runtime note on both status lines of the window.

        Every note of the bridge is written twice: the form and the
        results page each carry a status line, and the note can arrive
        while either page is the visible one. The refusals of the
        automatic follow and of the jump that follows a run are the loud
        case, they can only happen while the results page is on screen -
        written on the hidden form alone they would look like a follow
        that does nothing.
        No third status display is created for them: the two existing
        lines are mirrored, and the results line hands its room back to
        the summary on the next export (see set_summary).
        """

        text = str(message)
        self.config_page.set_status(text)
        self.results_page.set_summary(text)

    def _open_current_in_main_window(self) -> None:
        """Hand the record on screen to the main labeling window.

        This is the one entry of the automatic follow. Every refusal is
        the business of the bridge, which explains it on status_message:
        nothing here opens a message box, and nothing here touches the
        validation result.
        """

        record = self.results_page.displayed_record()
        if record is None:
            self._show_status_message(self.tr("没有可打开的记录"))
            return
        if not self.bridge.open_record(record):
            # a refused jump never loaded a file, so there is no focus
            # to take back and the refusal stays the only effect
            return
        # The upstream load_file ends on canvas.setFocus(), which hands
        # the active window to the main window; the keyboard belongs to
        # this window, so it is asked back one turn later. The call is
        # deferred because load_file is still running: the focus set
        # here would be the one the upstream line overwrites.
        QtCore.QTimer.singleShot(0, self._restore_validation_focus)

    def _restore_validation_focus(self) -> None:
        """Give the keyboard back to the validation window.

        Called one turn after a successful follow. The jump switches
        the file of the main window alone and this window is never
        lifted by it, so what is restored here is the focus and not
        the stacking: the page that carries the A / D navigation gets
        the focus back and the user can keep browsing.

        A window the user hid or minimized is left exactly as it is,
        and every call is guarded: the queued turn may arrive after
        the window was closed and its C++ side deleted, which is the
        ordinary end of a window, not a failure.
        """

        try:
            if not self.isVisible() or self.isMinimized():
                return
            self.activateWindow()
            self.results_page.focus_results()
        except RuntimeError:
            pass

    def _on_current_record_changed(self, record_id: str) -> None:
        """Restart the debounce that follows the record on screen.

        A held A / D key and a quick series of clicks move the page
        through several records within a moment: every switch restarts
        the timer, so only the record the user settled on is opened.
        """

        self._pending_record_id = str(record_id or "")
        self.follow_timer.start()

    def _follow_current_record(self) -> None:
        """Open the record the results page settled on in the main window.

        This is what the E key used to be, without the key: the record
        the page shows is carried over as soon as the switching stopped.
        Two conditions guard the follow - there has to be a main window
        to follow into, and the results page has to be the page on
        screen (a hidden window and a run that is still on its setup or
        progress page follow nothing), and one record is opened once:
        the very record that was opened last is never opened again, so a
        filter that shows it once more and a save the watcher carried
        back cost no second jump and no second may_continue question.
        """

        record_id = self._pending_record_id
        if not record_id or not self._follows_now():
            return
        if self.bridge.main_window is None:
            # a start of its own has no window to follow: the refusal is
            # said once, never again on every switch of the record
            if not self._follow_refusal_shown:
                self._follow_refusal_shown = True
                self._show_status_message(
                    self.tr("未连接主窗口，当前记录不会自动打开")
                )
            return
        if record_id == self._followed_record_id:
            return
        self._followed_record_id = record_id
        self._open_current_in_main_window()

    def _follows_now(self) -> bool:
        """Return True while the results page is the page on screen."""

        return bool(
            self.isVisible()
            and self.stack.currentWidget() is self.results_page
        )

    def _on_record_changed(self, record_id: str) -> None:
        """Repaint one record after the main window saved its label.

        StagingSync has already written the sibling json back into the
        canonical label, so this slot only reads it again: the row and the
        two canvases are refreshed in place, the list is never rebuilt and
        no jump is started from here - that would reenter the bridge the
        signal just came from.
        """

        if not record_id:
            return
        self.results_page.reload_record(str(record_id))
        self.refresh_export_summary()
        self._schedule_state_save()

    # -------------------------------------------------------------- history
    def _history_guard(self, require_mode: bool = False) -> bool:
        """Return True while a history command may run.

        The history is refused while an export owns the event loop or
        while a worker of a run is still alive: both keep writing the
        very staging folder the history would replace, and the restore
        would detach the watcher of a run that is not finished. The
        restore is confined to the history page as well, so a stale
        signal can never swap the run under the user's feet.
        """

        if self._exporting or not self._history_idle():
            self._show_status_message(self.tr(HISTORY_BUSY_STATUS))
            return False
        if require_mode and not self._history_mode:
            return False
        return True

    def _history_idle(self) -> bool:
        """Return True while no worker of this window is still running."""

        return self.worker is None and not self._detached_workers

    def show_history(self) -> None:
        """Open the history page and scan the temporary directory.

        The list is filled behind a thread of its own: the window walks
        thousands of folders on a busy temporary directory and would
        freeze for the whole walk if the scan ran in this slot.
        """

        if not self._history_guard():
            return
        self._history_mode = True
        self.history_page.show_runs(
            [], current_root=self.staging_root, scanning=True
        )
        self.history_page.set_busy(True)
        self.stack.setCurrentWidget(self.history_page)
        self._start_history_scan()

    def refresh_history(self) -> None:
        """Scan the temporary directory again for the page on screen.

        A refresh is the same path as the first scan, minus the page
        switch: the user is already looking at the list, and switching
        away and back would only blink.
        """

        if not self._history_guard():
            return
        self._history_mode = True
        self.history_page.show_runs(
            self._history_runs,
            current_root=self.staging_root,
            scanning=True,
        )
        self.history_page.set_busy(True)
        self._start_history_scan()

    def _start_history_scan(self) -> None:
        """Start the thread that lists the runs of the temp folder."""

        self._retire_history_scan()
        worker = _HistoryScanWorker(parent=self)
        worker.ready.connect(self._on_history_ready)
        worker.failed.connect(self._on_history_failed)
        worker.finished.connect(self._on_history_scan_finished)
        self._history_scan = worker
        worker.start()

    def _retire_history_scan(
        self, timeout_ms: int = HISTORY_SCAN_WAIT_MS
    ) -> None:
        """Cut a running scan off and wait for it to leave run().

        The signals of the retired thread are dropped first: its answer
        describes the folder of a request the window no longer waits
        for. The wait is polling, because a QThread that overrides run()
        has no event loop to quit and the only way it ends is finishing
        the walk - an unbounded wait would freeze the window for as long
        as the walk takes.

        A wait that timed out never loses the thread: the worker is
        detached from this window and handed to the module level set
        that owns it until it finishes (see _detach_history_scan), so
        the window can be destroyed while the walk is still running.
        """

        worker = self._history_scan
        self._history_scan = None
        if worker is not None:
            for signal in (worker.ready, worker.failed, worker.finished):
                try:
                    signal.disconnect()
                except TypeError:
                    # a worker whose signals were never connected
                    pass
            self._wait_thread(worker, timeout_ms)
            if self._thread_running(worker):
                self._detach_history_scan(worker)
        for retired in list(self._detached_history_scans):
            self._wait_thread(retired, timeout_ms)
            self._forget_detached(retired)

    def _wait_thread(self, worker: Any, timeout_ms: int) -> bool:
        """Wait for one thread, answering whether it left its run().

        A worker without a usable wait() - a stub of a test, a platform
        that refuses the call - counts as finished: there is nothing
        left to wait for, and the window must never be frozen by it.
        """

        waiter = getattr(worker, "wait", None)
        if not callable(waiter):
            return True
        try:
            return bool(waiter(int(timeout_ms)))
        except TypeError:
            # a wait() that takes no timeout
            try:
                return bool(waiter())
            except TypeError:
                return True

    def _thread_running(self, worker: Any) -> bool:
        """Return True while a thread is still inside its run()."""

        probe = getattr(worker, "isRunning", None)
        if not callable(probe):
            return False
        try:
            return bool(probe())
        except RuntimeError:
            # the C++ side is already gone: nothing runs any more
            return False

    def _detach_history_scan(self, worker: Any) -> None:
        """Hand a timed out scan thread over to the module level set.

        This is what keeps the promise of the close handler: a window
        that is destroyed with a walk still running would delete the
        QThread underneath it, and Qt aborts the whole application for
        that. The thread is unparented - so the window owns no running
        thread any more - remembered in _ORPHAN_SCANS, and released from
        there on its own finished signal (see _orphan_scan_finished).
        """

        try:
            worker.setParent(None)
        except (AttributeError, RuntimeError):
            pass
        try:
            worker.finished.disconnect()
        except (AttributeError, TypeError):
            pass
        try:
            # the argument is bound here: a plain Python slot cannot ask
            # Qt for the sender of the signal
            worker.finished.connect(
                lambda: _orphan_scan_finished(worker)
            )
        except (AttributeError, RuntimeError):
            pass
        _ORPHAN_SCANS.add(worker)

    def _forget_detached(self, worker: Any) -> None:
        """Let go of one retired thread, once it really stopped.

        The thread is dropped from the list while it is known to have
        finished. One that is still running after the second wait is
        detached like a fresh timeout: this window may not keep it.
        """

        running = self._thread_running(worker)
        if running:
            # the wait above timed out again: the thread keeps walking
            # and stays owned by the module level set
            self._detach_history_scan(worker)
            return
        try:
            self._detached_history_scans.remove(worker)
        except ValueError:
            pass

    def _on_history_ready(self, runs: list) -> None:
        """Fill the history page with the finished scan."""

        if self._history_scan is None:
            return
        self._history_runs = list(runs)
        # a run the folder no longer holds is not restorable any more,
        # so the current root is only highlighted while it is listed
        current = (
            self.staging_root
            if any(
                run.staging_root == self.staging_root
                for run in self._history_runs
            )
            else ""
        )
        self.history_page.show_runs(
            self._history_runs, current_root=current, scanning=False
        )
        self.history_page.set_busy(False)
        self._show_status_message(
            self.tr("找到 {count} 个历史运行目录").format(
                count=len(self._history_runs)
            )
        )

    def _on_history_failed(self, message: str) -> None:
        """Report a scan that could not list the temp folder."""

        if self._history_scan is None:
            return
        self._history_runs = []
        self.history_page.show_runs(
            [], current_root=self.staging_root, scanning=False
        )
        self.history_page.set_busy(False)
        self.history_page.set_note(
            self.tr(HISTORY_SCAN_FAILED_TEMPLATE).format(message=message)
        )
        self._show_status_message(
            self.tr(HISTORY_SCAN_FAILED_TEMPLATE).format(message=message)
        )

    def _on_history_scan_finished(self) -> None:
        """Release the scan thread once it left its run().

        The Python side of a finished QThread is dropped without a
        blocking wait: the walk is over by the time this slot runs, so
        the thread is reclaimed here and waited for in closeEvent.
        """

        worker = self._history_scan
        if worker is None:
            return
        self._history_scan = None
        try:
            worker.finished.disconnect()
        except TypeError:
            pass
        self._detached_history_scans.append(worker)

    def _running_history_scans(self) -> list:
        """Return every scan thread this window still owns and runs.

        Both owners are read: the scan that is in flight and the threads
        that finished their work but were not waited for yet. A thread
        that outlasted the bounded wait is deliberately not one of them:
        it was unparented by _detach_history_scan and belongs to the
        module level set from then on, so this window owns no running
        thread any more - which is exactly what the close needs to hold.
        """

        found = [
            worker
            for worker in self._detached_history_scans
            if self._thread_running(worker)
        ]
        current = self._history_scan
        if current is not None and self._thread_running(current):
            found.append(current)
        return found

    def _close_history(self) -> None:
        """Leave the history page and go back to the form.

        The flag that blocks a start has to be cleared here as well as
        after a restore: a page the user closed is not the entry point
        on screen any more, and keeping the flag would refuse every
        later run of the window.
        """

        self._history_mode = False
        self.show_config()

    def restore_run(self, staging_root: str) -> bool:
        """Rebuild one finished run on the results page.

        The staging folder of that run becomes the one this window
        works on: the records, the class table and the exports all
        follow it, and the state it carried - the verdicts and the
        marks - is what the page shows. The parameters of the form are
        deliberately not restored: a restore brings a result back, it
        never rewrites the configuration of the next run.
        """

        if not self._history_guard(require_mode=True):
            return False
        root = str(staging_root or "")
        run = next(
            (
                entry
                for entry in self._history_runs
                if entry.staging_root == root
            ),
            None,
        )
        if run is None or not run.restorable:
            self._show_status_message(self.tr(HISTORY_MISSING_STATUS))
            return False
        # the class table of the run on disk wins: a restore brings the
        # run back as it was validated, and the form of the next run is
        # not its business. Only a run that recorded no table at all
        # falls back to the classes the window holds right now.
        classes = self._run_classes(run.staging_root) or list(self.classes)
        records, report = history.restore_records(run, classes)
        restored = list(report["classes"]) or list(self.classes)
        self.staging_root = str(report["staging_root"] or root)
        self.records = list(records)
        self.classes = list(restored)
        self.meta = self._read_run_meta(self.staging_root)
        self.model_info = {}
        self.augment_summary = {}
        # a restored run is a new run for everything that follows it:
        # the warnings of the previous one are not its warnings
        self.warnings = []
        # the follow is re-armed exactly like a new run arms it (see
        # start_validation): ResultsPage.set_records announces its first
        # record again, and the same record id as the previous run - the
        # typical restore of the same dataset - must not be answered
        # with "already followed", which would leave the main window on
        # the picture of the old staging folder while the edits travel
        # to the restored one
        self.follow_timer.stop()
        self._pending_record_id = ""
        self._followed_record_id = ""
        self.results_page.set_context(list(restored), self.staging_root)
        self.results_page.set_model_note({})
        # the page opens on NG, and a run whose verdicts are not written
        # yet would show an empty table under it: the filter is widened
        # to 全部 *before* the records are handed over, so the one build
        # the restore needs is also the one that selects the first record
        # of the restored run (and not a left over row of the old one)
        self.results_page.filter_combo.setCurrentIndex(0)
        self.results_page.set_records(self.records)
        # the note of the restore goes to the form and to the history
        # page alone: the summary line of the results page belongs to
        # the export formula, which the next call writes there (see
        # refresh_export_summary)
        text = self._restore_status_text(report)
        self.config_page.set_status(text)
        self.history_page.set_note(text)
        self.refresh_export_summary()
        self.stack.setCurrentWidget(self.results_page)
        self.bridge.attach(self.staging_root, self.records)
        # the history page is left behind by this switch: a later start
        # has to be allowed again
        self._history_mode = False
        self._schedule_state_save()
        return True

    def _run_classes(self, staging_root: str) -> List[str]:
        """Return the class table the state file of a run recorded.

        The table is what the run was validated with; a folder without
        a usable state file answers an empty list and the caller falls
        back to the classes the window holds itself.
        """

        state = history.read_restore_state(staging_root)
        if not state.get("readable"):
            return []
        return [str(item) for item in state.get("classes", []) or []]

    def _read_run_meta(self, staging_root: str) -> Dict[str, Any]:
        """Read the meta.json of a run folder, {} when there is none.

        The meta of a restored run is only used by the paths that still
        describe the run - a report, a follow, an export - and a folder
        that lost its meta.json is a normal, half cleaned run: it is
        answered with an empty mapping instead of raising.
        """

        path = osp.join(staging_root, dataset.META_FILENAME)
        try:
            meta = dataset.read_json(path)
        except (OSError, ValueError):
            return {}
        return dict(meta) if isinstance(meta, dict) else {}

    def _restore_status_text(self, report: Dict[str, Any]) -> str:
        """Return the status line of a finished restore.

        The note of the report is written first and in full: it is the
        one line that explains a run restored without judgement data.
        """

        root = osp.basename(str(report.get("staging_root", "")))
        parts = [self.tr("已恢复历史运行：{root}").format(root=root)]
        note = str(report.get("note", "") or "")
        if note:
            parts.append(note)
        parts.append(
            self.tr("记录 {count}（判定 {judged} / 跳过 {skipped}）").format(
                count=int(report.get("record_count", 0)),
                judged=int(report.get("judged", 0)),
                skipped=int(report.get("skipped", 0)),
            )
        )
        return "；".join(parts)

    # ------------------------------------------------------- state on disk
    def _schedule_state_save(self) -> None:
        """Ask for the debounced write of the state of the current run."""

        if not self.staging_root or not self.records:
            return
        self._state_dirty = True
        self._state_timer.start()

    def _save_state_now(self) -> None:
        """Write the state.json of the current run, never raising.

        A staging folder that was removed under the running window is a
        normal end of a run, not a failure of the window: the OSError of
        the write is reported on the status line and the run keeps going
        with everything it holds in memory.
        """

        self._state_timer.stop()
        dirty = self._state_dirty
        self._state_dirty = False
        if not dirty or not self.staging_root:
            return
        record = self.results_page.displayed_record()
        shown = str(record.record_id) if record is not None else ""
        try:
            history.save_restore_state(
                self.staging_root,
                self.records,
                classes=self.classes,
                source_display=self._state_source_display(),
                shown_record_id=shown,
            )
        except OSError as error:
            self._show_status_message(
                self.tr("保存运行状态失败：{message}").format(
                    message=str(error)
                )
            )

    def _state_source_display(self) -> str:
        """Return the source shown in the state file of the current run."""

        text = str(self.meta.get("source_display", "") or "")
        if text:
            return text
        for record in self.records:
            if record.source_display:
                return str(record.source_display)
        return ""

    # ------------------------------------------------------------ validation
    def validate_config(self, config: ValidationConfig) -> List[str]:
        """Return the blocking problems of the current configuration."""

        problems: List[str] = []
        if not config.dataset_dir or not osp.isdir(config.dataset_dir):
            problems.append(self.tr("请选择有效的数据来源目录"))
        if not config.model_path or not osp.isfile(config.model_path):
            problems.append(self.tr("请选择有效的 ONNX 模型文件"))
        if not config.classes_file or not osp.isfile(config.classes_file):
            problems.append(self.tr("请选择有效的 classes.txt"))
        if problems:
            return problems
        try:
            load_classes_file(config.classes_file)
        except ValidationConfigError as error:
            problems.append(str(error))
        if config.augment_enabled:
            try:
                validate_augment_params(config.augment_params)
            except ValidationConfigError as error:
                problems.append(str(error))
        return problems

    # --------------------------------------------------------------- preview
    def _count_source_pairs(self, directory: str) -> int:
        """Return the cached pair count of the source dataset directory.

        The enumeration itself belongs to the scan scheduler (see
        _on_dataset_changed): the slot that watches the path line edit
        only reads the cache, so a large dataset never freezes the window
        and typing a path does not walk the folder once per keystroke. A
        directory whose scan has not answered yet counts 0.
        """

        if directory != self.source_dataset_dir:
            return 0
        return self.source_pair_count

    def _on_dataset_changed(self, *_args: Any) -> None:
        """Ask the scheduler for a debounced scan of the new directory.

        The pending flag is set before the request leaves this slot: a
        path that is not a folder is answered inside schedule() itself
        (see DirectoryScanScheduler), and that synchronous answer has to
        find - and clear - the pending state already in place.
        """

        self._scan_token += 1
        directory = self.config_page.dataset_edit.text().strip()
        self._scan_pending = True
        self._refresh_preview()
        self.scan_scheduler.schedule(
            directory, self._scan_token, SCAN_DEBOUNCE_MS
        )

    def _on_scan_ready(self, token: int, directory: str, count: int) -> None:
        """Cache the pair count of a finished scan of the source folder.

        A scan the window no longer waits for - a newer keystroke moved
        the token on, or the path of the form is another one - is dropped:
        its count describes a directory the user already left.
        """

        if int(token) != self._scan_token:
            return
        if directory != self.config_page.dataset_edit.text().strip():
            return
        self.source_dataset_dir = directory
        self.source_pair_count = int(count)
        self._scan_pending = False
        self._refresh_preview()

    def _on_scan_failed(
        self, token: int, directory: str, message: str
    ) -> None:
        """Report a scan the scheduler could not finish.

        The directory counts 0 - the preview is an estimate, not a result
        - and the reason goes to the status line: a scan runs behind the
        form the user is still filling in, and an exception escaping its
        slot would abort the whole application.
        """

        if int(token) != self._scan_token:
            return
        if directory != self.config_page.dataset_edit.text().strip():
            return
        self.source_dataset_dir = directory
        self.source_pair_count = 0
        self._scan_pending = False
        self._show_status_message(str(message))
        self._refresh_preview()

    def preview_counts(
        self, sample_count: int, config: ValidationConfig
    ) -> Dict[str, int]:
        """Compute the live preview numbers shown on the configuration page."""

        augmented = 0
        if config.augment_enabled and sample_count > 0:
            try:
                plan = plan_aug_counts(
                    sample_count,
                    config.augment_mode or RATIO_MODE,
                    multiplier=config.multiplier,
                    ratio=config.ratio,
                    total=config.total_count,
                )
                augmented = int(sum(plan))
            except ValidationConfigError:
                augmented = 0
        judged = sample_count + (augmented if config.judge_augmented else 0)
        if self.records:
            # the staged records define the export estimate, so the line
            # stays consistent with the export formula and the report.
            summary = records_module.export_summary(self.records)
            exported = int(summary["originals"]) + int(summary["augmented"])
        else:
            # nothing is staged yet and the augmented copies are not
            # selected for the export by default.
            exported = int(sample_count)
        return {
            "originals": sample_count,
            "augmented": augmented,
            "judged": judged,
            "exported": exported,
        }

    def _refresh_preview(self, *_args: Any) -> None:
        """Refresh the live preview line of the configuration page."""

        config = self.config_page.collect_config()
        sample_count = len(
            [
                record
                for record in self.records
                if record.kind == records_module.KIND_ORIGINAL
                and record.verdict != records_module.SKIPPED
            ]
        )
        if not self.records:
            if self._scan_pending:
                # the folder is still being walked: the line says so
                # instead of showing a zero nobody has counted yet
                self.config_page.set_preview(self.tr(SCAN_PENDING_TEXT))
                return
            sample_count = self._count_source_pairs(config.dataset_dir)
        counts = self.preview_counts(sample_count, config)
        self.config_page.set_preview(
            self.tr(
                "有效原图 {n} / 将生成增强 {m} / 验证总数 {t} / 预计导出 {e}"
            ).format(
                n=counts["originals"],
                m=counts["augmented"],
                t=counts["judged"],
                e=counts["exported"],
            )
        )

    # ------------------------------------------------------------------- run
    def start_validation(self) -> None:
        """Validate the form and start the worker thread."""

        if self._history_mode:
            # the history page is the entry point on screen: a start
            # from here would leave the list the user is browsing and
            # overwrite the run the page is about to restore
            self._show_status_message(self.tr(HISTORY_OPEN_STATUS))
            return
        config = self.config_page.collect_config()
        problems = self.validate_config(config)
        if problems:
            self.config_page.set_status("\n".join(problems), error=True)
            return
        classes = self.config_page.load_classes(config.classes_file)
        if classes is None:
            # the page already reports why the file could not be loaded
            return
        self.classes = classes

        # a new run replaces the records of the previous one: the watcher
        # must stop carrying an edit of the old staging folder into the
        # new list (see on_worker_finished for the attach)
        self.bridge.detach()
        # and it starts its follow from scratch: the first record of the
        # run is opened again even when it carries the record id the
        # previous run was left on (the same dataset validated twice)
        self.follow_timer.stop()
        self._pending_record_id = ""
        self._followed_record_id = ""

        if self.staging_root:
            self.previous_staging_roots.append(self.staging_root)
        self.staging_root = dataset.create_staging_root()
        self.records: List[records_module.ValidationRecord] = []
        self.model_info = {}
        self.augment_summary = {}
        self.warnings = []

        self.progress_page.reset()
        self.progress_page.append_line(f"staging: {self.staging_root}")
        for warning in self.warnings:
            self.progress_page.append_line(warning)
        self.stack.setCurrentWidget(self.progress_page)

        self.worker = ValidationWorker(
            config, self.classes, self.staging_root, self
        )
        self.worker.progress.connect(self.progress_page.set_progress)
        self.worker.stage_finished.connect(self.progress_page.mark_stage_done)
        self.worker.warnings_ready.connect(self.on_worker_warnings)
        self.worker.dataset_ready.connect(self.on_dataset_ready)
        self.worker.failed.connect(self.on_worker_failed)
        self.worker.cancelled.connect(self.on_worker_cancelled)
        self.worker.finished_ok.connect(self.on_worker_finished)
        self.worker.start()

    def on_dataset_ready(
        self, staging_root: str, records: list, meta: dict
    ) -> None:
        """Store the staged records once the first stage finished."""

        self.staging_root = staging_root
        self.meta = dict(meta or {})
        classes = self.classes
        # Keep the very list the worker keeps filling: _stage_augment
        # appends the augmented children to it after this signal, a copy
        # would hide them from the results page.
        self.records = records if isinstance(records, list) else list(records)
        for record in self.records:
            record.detail.setdefault("classes", list(classes))
        counts = dict(self.meta.get("counts", {}))
        self.progress_page.append_line(
            f"staged pairs: {counts.get('original', 0)}"
        )

    def on_worker_warnings(self, warnings: list) -> None:
        """Append the non blocking notes of the run to the log."""

        for warning in warnings or []:
            text = str(warning)
            if text and text not in self.warnings:
                self.warnings.append(text)
            self.progress_page.append_line(text)

    def cancel_validation(self) -> None:
        """Stop the run right away, drop it and go back to the form.

        A cancel is not a failure: the running worker is asked to stop
        and its signals are cut off at once, so nothing it still produces
        - a dataset snapshot, a warning, a progress line, a result - can
        reach this window any more. Every piece of run state is thrown
        away with it (the records, the counters, the model snapshot and
        the warnings), which is what makes the next run start from
        scratch: the preview falls back to the estimate of the source
        directory, because there is no staged record left to count. The
        staging folder itself is kept, like every other staging folder of
        this tool - nothing is ever deleted here.
        """

        worker = self.worker
        self.worker = None
        if worker is not None:
            worker.cancel()
            self._detach_worker(worker)
        self._drop_run()

    def _drop_run(self) -> None:
        """Forget everything a cancelled run produced and show the form.

        The records, the counters, the model snapshot, the warnings and
        the export line of the dropped run all go away with it, and the
        progress page is reset before the window returns to the form: the
        next run starts from an empty page instead of showing what the
        cancelled one had reached.
        """

        self.meta = {}
        self.model_info = {}
        self.augment_summary = {}
        self.warnings = []
        self.records = []
        # the page goes back to the state of a fresh run, then says that
        # the stage it is leaving behind may still take a moment: a
        # stage blocked in the ONNX runtime can not be interrupted
        self.progress_page.reset()
        self.progress_page.show_ending()
        self.show_config()
        # the export line of a dropped run is a leftover like its records
        self.results_page.set_summary("")
        self.config_page.set_status(self.tr(CANCELLED_STATUS))

    def _detach_worker(self, worker: ValidationWorker) -> None:
        """Cut every signal of a worker whose run was dropped.

        The thread keeps running until it reaches its next cancel check
        (a stage blocked in the ONNX runtime can not be stopped from the
        outside), so nothing of it may reach this window in the meantime.
        The worker is remembered afterwards, which is what lets the close
        handler wait for it.
        """

        for signal in (
            worker.progress,
            worker.stage_finished,
            worker.warnings_ready,
            worker.dataset_ready,
            worker.failed,
            worker.cancelled,
            worker.finished_ok,
        ):
            try:
                signal.disconnect()
            except TypeError:
                # a worker whose signals were never connected
                pass
        self._detached_workers = [
            item for item in self._detached_workers if item.isRunning()
        ]
        self._detached_workers.append(worker)

    def on_worker_cancelled(self) -> None:
        """Drop the run when the worker reports its own cancellation.

        This path is deliberately independent of on_worker_failed: a
        cancelled run is an outcome of its own and never an error, so it
        gets no error message and no error colour. A cancel clicked in
        the window already dropped its worker and returns right here,
        because that worker is no longer the current one.
        """

        if self.worker is None:
            return
        self.worker = None
        self._drop_run()

    def on_worker_failed(self, title: str, message: str) -> None:
        """Show a failure and return to the configuration page."""

        self.progress_page.append_line(f"{title}: {message}")
        self.config_page.set_status(f"{title}: {message}", error=True)
        self.worker = None
        self.show_config()

    def on_worker_finished(self) -> None:
        """Populate the results page after a successful run."""

        worker = self.worker
        self.worker = None
        if worker is not None:
            # the worker list is final once the last stage finished and
            # it already holds the augmented children.
            self.records = worker.records
        self.results_page.set_context(self.classes, self.staging_root)
        self.results_page.set_model_note(self.model_info)
        self.results_page.set_records(self.records)
        self.refresh_export_summary()
        self.stack.setCurrentWidget(self.results_page)
        # the edits made in the main window travel back through the
        # watcher of this folder: it has to watch the records of this very
        # run
        self.bridge.attach(self.staging_root, self.records)
        self._schedule_state_save()
        # the jump to the first record of the run is the debounced follow
        # of the results page itself (see _follow_current_record): one
        # path, not a second call that would jump twice

    # --------------------------------------------------------------- results
    def on_toggle_deleted(self, record_ids: list, deleted: bool) -> None:
        """Soft delete or restore the given original records.

        The data moves first, then only the rows that really changed are
        rewritten: the toggled originals plus their augmented children,
        whose parent note and export verdict follow the mark. The list is
        never rebuilt, so the scroll position and the selection of a long
        list survive the click; the export counters are text and stay
        updated on every path.
        """

        changed = records_module.set_deleted(
            self.records, list(record_ids), deleted
        )
        self.results_page.refresh_rows(
            records_module.affected_record_ids(
                self.records, [record.record_id for record in changed]
            )
        )
        self.refresh_export_summary()
        self._schedule_state_save()

    def on_toggle_export(self, record_ids: list, include: bool) -> None:
        """Toggle the export flag of the given augmented records."""

        changed = records_module.set_include_in_export(
            self.records, list(record_ids), include
        )
        self.results_page.refresh_rows(
            [record.record_id for record in changed]
        )
        self.refresh_export_summary()
        self._schedule_state_save()

    def refresh_export_summary(self) -> None:
        """Update the export formula counters of the results page.

        The archive keeps no original / augmented folder any more: both
        kinds land side by side in images/, told apart by the file name
        of the copy, so the line names the two counters as the *pairs*
        of the folder it writes and states where they go.
        """

        summary = records_module.export_summary(self.records)
        self.results_page.set_summary(
            self.tr(
                "导出 = 未删原图 {o} + 勾选增强 {a} = 共 {t} 组，"
                "全部写入 zip 的 images/（每组图片与其 json 同目录同名）"
                "（另排除：软删除原图 {d}、未勾选增强 {u}、"
                "因父图删除排除的增强 {p}、无标签跳过 {s}）"
            ).format(
                o=summary["originals"],
                a=summary["augmented"],
                t=summary["originals"] + summary["augmented"],
                d=summary["excluded_deleted_originals"],
                u=summary["excluded_unselected_augmented"],
                p=summary["excluded_deleted_parent_augmented"],
                s=summary["skipped_no_label"],
            )
        )

    def default_export_name(self) -> str:
        """Return the default zip file name."""

        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"model_validation_{stamp}.zip"

    def build_report_document(self, export_path: str = "") -> Dict[str, Any]:
        """Assemble the validation report for the current records.

        Kept as the documented report entry point of the window (the
        report layer itself is untouched by this revision), but no
        export calls it any more: an export writes the archive and
        nothing else, it neither builds nor stores a report document.
        """

        config = self.config_page.collect_config()
        return build_report(
            self.staging_root,
            self.records,
            model_info=self.model_info,
            classes=self.classes,
            config=config.to_dict(),
            augment_summary=self.augment_summary,
            staging_meta=self.meta,
            warnings=self.warnings,
            previous_staging_roots=self.previous_staging_roots,
            export_path=export_path,
        )

    def export_zip_dialog(self) -> None:
        """Ask for the destination and export the selected staging files."""

        if not self.staging_root:
            return
        path, _filter = QtWidgets.QFileDialog.getSaveFileName(
            self,
            self.tr("导出 Zip"),
            self.default_export_name(),
            self.tr("Zip 文件 (*.zip)"),
        )
        if not path:
            return
        if not path.lower().endswith(".zip"):
            path += ".zip"
        self.export_zip(path)

    def _write_export_result(self, text: str) -> None:
        """Report the outcome of an export on the results page.

        The export runs with the event loop pumping (see export_zip), so
        the page may be gone by the time the line is written: a widget a
        closing window already destroyed is dropped instead of raised,
        because a report that can no longer be shown must never abort the
        application.
        """

        try:
            self.results_page.set_summary(text)
        except RuntimeError:
            pass

    def export_zip(self, path: str) -> Dict[str, Any]:
        """Run the export and report the result on the results page.

        The archive is the only thing this method produces: the class
        table and the images/ folder of the selected pairs (see
        exporter.write_zip). No report document is assembled for it and
        none is written into the staging folder - the report of a run is
        what its own reader asks for (see build_report_document), while
        an export stays a read only pass over the staging folder.

        The progress dialog is not modal: the main window stays usable
        while the archive is written. The export itself is refused twice
        over, by a disabled button and by the _exporting flag, and both
        are restored on every path, including a failure.
        """

        if self._exporting:
            return {}
        self._exporting = True
        export_button = self.results_page.export_button
        export_was_enabled = True
        try:
            export_was_enabled = export_button.isEnabled()
            export_button.setEnabled(False)
        except RuntimeError:
            # the page is already gone: the archive is still what the
            # user asked for, and there is no button left to disable
            pass

        progress = QtWidgets.QProgressDialog(
            self.tr("正在导出…"), self.tr("取消"), 0, 100, self
        )
        progress.setWindowModality(QtCore.Qt.WindowModality.NonModal)
        progress.setAutoClose(False)
        progress.setStyleSheet(EXPORT_PROGRESS_STYLE)
        progress.show()

        def on_progress(done: int, total: int, message: str) -> None:
            # every write lands in a try: processEvents() below is what
            # delivers the close click of the user, and a widget deleted
            # on the way would otherwise raise out of the export and
            # abort the whole application
            try:
                progress.setMaximum(max(int(total), 1))
                progress.setValue(int(done))
                progress.setLabelText(message)
            except RuntimeError:
                return
            QtWidgets.QApplication.processEvents()

        summary: Dict[str, Any] = {}
        try:
            summary = export_zip(
                self.records,
                self.staging_root,
                path,
                self.classes,
                None,
                progress=on_progress,
                is_cancelled=progress.wasCanceled,
            )
            self.last_export_path = path
            self._write_export_result(
                self.tr(
                    "导出完成：{path}（原图 {originals} 张，增强 {augmented} 张）"
                ).format(
                    path=path,
                    originals=summary.get("originals", 0),
                    augmented=summary.get("augmented", 0),
                )
            )
        except Exception as error:  # noqa: BLE001
            message = self.tr("导出失败或已取消（半成品 zip 已保留）：")
            if isinstance(error, ExportCancelled):
                # The user asked for this stop: it is told in Chinese and
                # in the full width parentheses of the finished line.
                detail = f"（{CANCELLED_MESSAGE}）"
            else:
                # A real failure keeps its own message verbatim: the
                # diagnostic often comes from a library and is never
                # translated away.
                detail = f" ({error})"
            self._write_export_result(message + path + detail)
        finally:
            # the teardown of the export touches the widgets the closing
            # window may already have destroyed: a report that cannot be
            # written any more is dropped, and the export flag is always
            # cleared so the window stays usable
            try:
                progress.close()
            except RuntimeError:
                pass
            try:
                export_button.setEnabled(export_was_enabled)
            except RuntimeError:
                pass
            self._exporting = False
        return summary

    # ----------------------------------------------------------------- pages
    def show_config(self) -> None:
        """Switch back to the configuration page."""

        # leaving the history page - wherever the call comes from - is
        # the entry point of the next run: a flag left standing would
        # refuse every start of this window (see start_validation)
        if self.stack.currentWidget() is self.history_page:
            self._history_mode = False
        self.results_page.set_records(self.records)
        self.stack.setCurrentWidget(self.config_page)
        self._refresh_preview()

    def show_results(self) -> None:
        """Switch to the results page."""

        if self.stack.currentWidget() is self.history_page:
            self._history_mode = False
        self.stack.setCurrentWidget(self.results_page)

    # ------------------------------------------------------------------ size
    def _available_height(self) -> int:
        """Return the usable height of the screen hosting this window."""

        screen = self.screen() or QtGui.QGuiApplication.primaryScreen()
        if screen is None:
            return 0
        return int(screen.availableGeometry().height())

    def apply_initial_size(self) -> QtCore.QSize:
        """Resize the window to the content of the current page, once.

        The configuration page is the page the window opens on: it only
        needs its own size hint, so the empty area a taller hard coded
        minimum produced is gone. Later page switches leave the size
        alone: the user keeps whatever size was chosen in the meantime.
        """

        if self._initial_size_applied:
            return self.size()
        self._initial_size_applied = True
        content = self.stack.currentWidget()
        hint = content.sizeHint() if content is not None else self.sizeHint()
        height = initial_window_height(hint.height(), self._available_height())
        self.resize(max(self.minimumWidth(), int(hint.width())), height)
        return self.size()

    def showEvent(self, event: QtGui.QShowEvent) -> None:  # noqa: N802
        """Size the window to its content before it becomes visible."""

        super().showEvent(event)
        self.apply_initial_size()
        # the main window is watched from the first show on: a parent
        # handed in later still gets its filter here, and _main_window()
        # answering None leaves this a no-op
        self._install_activate_filter()

    def _running_workers(self) -> List[ValidationWorker]:
        """Return the workers of this window that are still alive."""

        workers = list(self._detached_workers)
        if self.worker is not None:
            workers.append(self.worker)
        return workers

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802
        """Stop every worker and watcher before closing the window.

        An export owns this thread while it runs and pumps the event loop
        (see export_zip), so the very click that arrives here is delivered
        inside the export: letting the close through would destroy the
        widgets the export is still writing to. The click is refused with
        a line of status instead, and the window stays open until the
        archive is written.
        """

        if self._exporting:
            self._show_status_message(self.tr(EXPORT_GUARD_STATUS))
            event.ignore()
            return
        self.bridge.detach()
        # a state write that is still waiting is flushed before the
        # window goes: the verdicts and the marks of the run are what the
        # history restores later, and a folder that cannot be written any
        # more must not abort the close
        self._state_timer.stop()
        self._save_state_now()
        # the filter lives on the main window: the close has to take it
        # off, a stale dialog must never raise itself again
        self._remove_activate_filter()
        # a follow that is still waiting must not reach into the close
        self.follow_timer.stop()
        self.scan_scheduler.shutdown(1000)
        # a walk of the temporary directory is waited for with the one
        # bounded budget, and a walk that outlasts it is detached from
        # this window: destroying a live QThread aborts the process,
        # while the detached one finishes on its own
        self._retire_history_scan(HISTORY_SCAN_WAIT_MS)
        for worker in self._running_workers():
            worker.cancel()
        for worker in self._running_workers():
            worker.wait(3000)
        super().closeEvent(event)


__all__ = [
    "CANCELLED_STATUS",
    "EXPORT_GUARD_STATUS",
    "EXPORT_PROGRESS_STYLE",
    "FOLLOW_DEBOUNCE_MS",
    "HISTORY_BUSY_STATUS",
    "HISTORY_MISSING_STATUS",
    "HISTORY_OPEN_STATUS",
    "HISTORY_SCAN_FAILED_TEMPLATE",
    "HISTORY_SCAN_WAIT_MS",
    "MINIMUM_HEIGHT",
    "MINIMUM_WIDTH",
    "ModelValidationDialog",
    "STATE_SAVE_DEBOUNCE_MS",
    "WINDOW_SIZE",
    "WINDOW_TITLE",
    "initial_window_height",
    "stack_minimum_size",
]
