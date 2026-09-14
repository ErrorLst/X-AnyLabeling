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
from .. import records as records_module
from ..app_config import (
    RATIO_MODE as DEFAULT_AUGMENT_MODE,
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

        self.stack = QtWidgets.QStackedWidget()
        self.config_page = ConfigPage()
        self.progress_page = ProgressPage()
        self.results_page = ResultsPage()
        self.stack.addWidget(self.config_page)
        self.stack.addWidget(self.progress_page)
        self.stack.addWidget(self.results_page)
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
        self.config_page.mode_combo.currentIndexChanged.connect(
            self._refresh_preview
        )
        self.config_page.multiplier_spin.valueChanged.connect(
            self._refresh_preview
        )
        self.config_page.ratio_spin.valueChanged.connect(self._refresh_preview)
        self.config_page.count_spin.valueChanged.connect(self._refresh_preview)
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
        self.bridge.open_record(record)

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
                    config.augment_mode or DEFAULT_AUGMENT_MODE,
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

    def on_toggle_export(self, record_ids: list, include: bool) -> None:
        """Toggle the export flag of the given augmented records."""

        changed = records_module.set_include_in_export(
            self.records, list(record_ids), include
        )
        self.results_page.refresh_rows(
            [record.record_id for record in changed]
        )
        self.refresh_export_summary()

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

        self.results_page.set_records(self.records)
        self.stack.setCurrentWidget(self.config_page)
        self._refresh_preview()

    def show_results(self) -> None:
        """Switch to the results page."""

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
        # a follow that is still waiting must not reach into the close
        self.follow_timer.stop()
        self.scan_scheduler.shutdown(1000)
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
    "MINIMUM_HEIGHT",
    "MINIMUM_WIDTH",
    "ModelValidationDialog",
    "WINDOW_SIZE",
    "WINDOW_TITLE",
    "initial_window_height",
    "stack_minimum_size",
]
