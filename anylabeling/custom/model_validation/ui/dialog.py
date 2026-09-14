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
from ..exporter import CANCELLED_MESSAGE, ExportCancelled, export_zip
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
        self.results_page.edit_requested.connect(self.on_edit_shape)
        self.results_page.shape_moved.connect(self.on_shape_moved)

        self._show_deferred_warnings()

    # ----------------------------------------------------------------- setup
    def _show_deferred_warnings(self) -> None:
        """Report the multi image augmentations that are not implemented."""

        from ..app_config import MULTI_IMAGE_AUGMENTATIONS

        self.config_page.set_status(
            self.tr("多图增强不实现：") + ", ".join(MULTI_IMAGE_AUGMENTATIONS),
        )

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
        """Return the pair count of the source dataset directory.

        The source directory is enumerated at most once per selection:
        the result is cached until another directory is chosen.
        """

        if directory != self.source_dataset_dir:
            self.source_dataset_dir = directory
            self.source_pair_count = 0
            if directory and osp.isdir(directory):
                try:
                    scan = dataset.collect_pairs(directory)
                except Exception:  # noqa: BLE001 - a preview count
                    # A directory that cannot be enumerated counts 0: this
                    # runs inside a Qt slot and an uncaught exception there
                    # aborts the whole application instead of the preview.
                    scan = None
                if scan is not None:
                    self.source_pair_count = len(scan.pairs)
        return self.source_pair_count

    def _on_dataset_changed(self, *_args: Any) -> None:
        """Cache the pair count of the newly selected source directory."""

        self._count_source_pairs(self.config_page.dataset_edit.text().strip())
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

    def on_edit_shape(
        self, record_id: str, index: int, label: Any, shape_type: Any
    ) -> None:
        """Write an edited label back into the staging json (no re judging).

        A rename only rewrites the one row of the record: the whole list
        is never rebuilt, so the scroll position, the selection and every
        other row stay exactly where the user left them - the very
        contract of the mark toggles (see on_toggle_deleted). The verdict
        of the record is deliberately left alone: a corrected label does
        not re-run the matching of the run.

        The display is refreshed as well (see ResultsPage.reload_record):
        the two canvases paint the shapes the page loaded, so rewriting
        the row alone would leave the old label on the box until another
        record is visited - the very defect the point drag never had,
        because apply_edit_result repaints the canvases after its write.
        """

        lookup = records_module.record_lookup(self.records)
        record = lookup.get(str(record_id))
        if record is None:
            return
        if records_module.update_shape(
            record, int(index), labels=label, shape_type=shape_type
        ):
            self.results_page.reload_record(record.record_id)

    def on_shape_moved(self, record_id: str, index: int, points: Any) -> None:
        """Store the point set a drag produced and refresh the row.

        The whole write is owned by the records layer and by the page
        (see ResultsPage.apply_edit_result); this slot is the one place
        of the window that knows both, exactly like on_edit_shape. The
        judgement of the record is not recomputed: a corrected box does
        not re-run inference, the run keeps the verdict it produced.
        """

        self.results_page.apply_edit_result(str(record_id), int(index), points)

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

    def export_zip(self, path: str) -> Dict[str, Any]:
        """Run the export and report the result on the results page.

        The archive is the only thing this method produces: the class
        table and the images/ folder of the selected pairs (see
        exporter.write_zip). No report document is assembled for it and
        none is written into the staging folder - the report of a run is
        what its own reader asks for (see build_report_document), while
        an export stays a read only pass over the staging folder.
        """

        progress = QtWidgets.QProgressDialog(
            self.tr("正在导出…"), self.tr("取消"), 0, 100, self
        )
        progress.setWindowModality(QtCore.Qt.WindowModality.WindowModal)
        progress.setAutoClose(False)
        progress.setStyleSheet(EXPORT_PROGRESS_STYLE)
        progress.show()

        def on_progress(done: int, total: int, message: str) -> None:
            progress.setMaximum(max(int(total), 1))
            progress.setValue(int(done))
            progress.setLabelText(message)
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
            self.results_page.set_summary(
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
            self.results_page.set_summary(message + path + detail)
        finally:
            progress.close()
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
        """Stop every worker before closing the window."""

        for worker in self._running_workers():
            worker.cancel()
        for worker in self._running_workers():
            worker.wait(3000)
        super().closeEvent(event)


__all__ = [
    "CANCELLED_STATUS",
    "EXPORT_PROGRESS_STYLE",
    "MINIMUM_HEIGHT",
    "MINIMUM_WIDTH",
    "ModelValidationDialog",
    "WINDOW_SIZE",
    "WINDOW_TITLE",
    "initial_window_height",
    "stack_minimum_size",
]
