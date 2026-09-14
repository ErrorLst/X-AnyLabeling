"""Results page of the remote training sub window (spec §5.1.3).

The artifact list of one job (route 13), the summary block, the two
yellow bands that never replace each other (partial results and suspect
artifacts, spec §5.6.2) and - at the bottom, always present - the read
only debug information area of spec §5.1.5: title 「调试信息」,
`objectName` `debugInfoEdit`, selectable and append only.  The window
never removes it, whatever the result state is.

Downloads, the artifact tree grouping and the `file_id` driven single file
download are rendered here but executed by the result step; this module
only holds the widgets and the display rules.
"""

from __future__ import annotations

import os.path as osp
from typing import Any, Iterable, List, Optional

from PyQt6 import QtCore, QtWidgets

from ..poller import is_terminal
from .widgets import StatusRow

__all__ = [
    "ARTIFACT_COLUMNS",
    "DEBUG_INFO_TITLE",
    "DEBUG_OBJECT_NAME",
    "DOWNLOAD_BUSY_BAR_TEXT",
    "DOWNLOAD_BUSY_TEMPLATE",
    "DOWNLOAD_CANCELLED_TEXT",
    "DOWNLOAD_START_TEXT",
    "PARTIAL_PREFIX",
    "ResultsPage",
    "SUMMARY_PATH",
    "WAIT_AUTO_RETRY_TEXT",
    "artifact_suspect_text",
    "debug_area_is_selectable",
    "download_progress_text",
    "format_size",
    "partial_result_text",
    "summary_lines",
]

ARTIFACT_COLUMNS = ("路径", "大小", "时间", "标记", "分组")
PARTIAL_PREFIX = "partial/"
DEBUG_INFO_TITLE = "调试信息"
DEBUG_OBJECT_NAME = "debugInfoEdit"
GROUP_COMPLETE = "完整结果"
GROUP_PARTIAL = "部分结果"

SUSPECT_TEXT = (
    "best.pt 的复算指标与训练日志不一致，或恢复后 loss 异常，"
    "请人工确认后再使用"
)
NO_WEIGHT_TEXT = "没有可下载的权重文件"
NO_ARTIFACT_TEXT = "该任务没有可下载的产物"
DOWNLOAD_START_TEXT = "开始下载…"
DOWNLOAD_PROGRESS_TEMPLATE = "下载进度 {0}/{1}（{2:.0f}%）"
DOWNLOAD_BUSY_TEMPLATE = "已下载 {0}（总大小未知，进度不定）"
DOWNLOAD_CANCELLED_TEXT = "下载已取消，未保存文件"
DOWNLOAD_BUSY_BAR_TEXT = "下载中…"
#: The one artifact whose content fills the summary area (§5.6.3).
SUMMARY_PATH = "summary.json"
WAIT_AUTO_RETRY_TEXT = "正在自动重试（第 {0}/{1} 次）"
RESUME_HINT_TEXT = "；如需继续训练请点击『恢复』"
RESUME_EXPIRED_TEXT = "；检查点或数据集已过期，无法恢复"


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def format_size(size: Any) -> str:
    """Human readable size (KB / MB / GB, spec §5.6.1)."""

    try:
        value = float(size)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return "{0:.0f} B".format(value)
            return "{0:.1f} {1}".format(value, unit)
        value /= 1024
    return ""


def download_progress_text(received: Any, total: Any) -> str:
    """Progress wording whose denominator is the response length.

    Spec §5.6.3: with a Content-Length the progress is
    received / Content-Length; without one (Transfer-Encoding: chunked)
    only the received amount is shown together with a busy indicator -
    no percentage, and never the sum of files[].size, which measures the
    uncompressed bytes of a compressed stream.
    """

    try:
        done = int(received)
    except (TypeError, ValueError):
        done = 0
    shown = format_size(done)
    if total is None:
        # No Content-Length (chunked): the only honest wording.
        return DOWNLOAD_BUSY_TEMPLATE.format(shown)
    try:
        whole: Any = int(total)
    except (TypeError, ValueError):
        whole = None
    if whole is None or whole < 0:
        return DOWNLOAD_BUSY_TEMPLATE.format(shown)
    # A legal empty artifact (Content-Length: 0) is a KNOWN length: the
    # transfer is complete at EOF, so it reads as a finished 100%.
    percent = 100.0 if whole == 0 else min(100.0, 100.0 * done / whole)
    return DOWNLOAD_PROGRESS_TEMPLATE.format(
        shown, format_size(whole), percent
    )


def partial_result_text(job: Any) -> str:
    """The unique partial result wording of spec §5.6.2.

    `partial_available` false produces no line at all; when true the
    `status` picks exactly one of the five mutually exclusive rows and
    the resume suffix only applies to the cancelled / failed rows.
    """

    if not bool(_get(job, "partial_available")):
        return ""
    status = str(_get(job, "status") or "")
    if status == "queued":
        return "该任务已重新排队，将继续训练"
    if status == "interrupted":
        return "该任务已中断（等待人工恢复），仅保留部分结果"
    if status == "cancelled":
        text = (
            "该任务已中止，仅保留部分结果"
            "（weights/last.pt 与已产出的日志 / 曲线）"
        )
    elif status == "failed":
        text = "训练失败，已保留部分结果"
    else:
        text = (
            "该任务保留了部分结果（partial/ 目录下的中间产物），"
            "可按需查看或下载"
        )
    if status in ("cancelled", "failed"):
        modes = list(_get(job, "resume_mode_available") or [])
        text += RESUME_HINT_TEXT if modes else RESUME_EXPIRED_TEXT
    return text


def artifact_suspect_text(job: Any) -> str:
    """The yellow band of a suspect artifact (spec §5.6.3)."""

    if not bool(_get(job, "artifact_suspect")):
        return ""
    return SUSPECT_TEXT


def has_weight_file(files: Iterable[Any]) -> bool:
    """True when the manifest lists any weights file."""

    for entry in files or []:
        path = str(_get(entry, "path") or "")
        if path.endswith(".pt"):
            return True
    return False


def summary_lines(summary: Any) -> List[str]:
    """The key fields of `summary.json` (spec §5.6.3)."""

    if not isinstance(summary, dict):
        return []
    lines: List[str] = []
    for name in ("job_id", "status", "attempt", "resume_cycles"):
        if name in summary:
            lines.append("{0}: {1}".format(name, summary[name]))
    if summary.get("duration_seconds") is not None:
        lines.append(
            "duration_seconds: {0}".format(summary["duration_seconds"])
        )
    for name in ("final_metrics", "verified_metrics"):
        metrics = summary.get(name)
        if isinstance(metrics, dict):
            lines.append(
                "{0}: {1}".format(
                    name,
                    ", ".join(
                        "{0}={1}".format(key, value)
                        for key, value in metrics.items()
                    ),
                )
            )
    params = summary.get("resolved_params")
    if isinstance(params, dict):
        lines.append(
            "resolved_params: " + ", ".join(
                "{0}={1}".format(key, value)
                for key, value in params.items()
            )
        )
    env = summary.get("training_env")
    if isinstance(env, dict):
        lines.append(
            "training_env: " + ", ".join(
                "{0}={1}".format(key, value)
                for key, value in env.items()
            )
        )
    if summary.get("suspect_reason"):
        lines.append("suspect_reason: {0}".format(summary["suspect_reason"]))
    return lines


def debug_area_is_selectable(widget: QtWidgets.QPlainTextEdit) -> bool:
    """True when the debug area keeps the default selectable flags."""

    flags = widget.textInteractionFlags()
    return bool(
        flags & QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
    ) and bool(
        flags & QtCore.Qt.TextInteractionFlag.TextSelectableByKeyboard
    )


class ResultsPage(QtWidgets.QWidget):
    """Artifact list, summary, yellow bands and the debug area."""

    back_requested = QtCore.pyqtSignal()
    download_requested = QtCore.pyqtSignal(str)
    file_download_requested = QtCore.pyqtSignal(str, str)
    open_directory_requested = QtCore.pyqtSignal(str)

    def __init__(self, parent: Optional[Any] = None) -> None:
        super().__init__(parent)
        self.job_id = ""
        #: Whose manifest `self.files` holds (spec §5.6.3: the summary is
        #: read out of this list, so it must belong to the job asked for).
        self.files_job_id = ""
        self.files: List[Any] = []
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)

        self.job_label = QtWidgets.QLabel("")
        layout.addWidget(self.job_label)

        self.partial_banner = QtWidgets.QLabel("")
        self.partial_banner.setWordWrap(True)
        self.partial_banner.setObjectName("partialBanner")
        self.partial_banner.setStyleSheet(
            "background: #fff8e1; color: #8a6d00; padding: 4px;"
        )
        layout.addWidget(self.partial_banner)

        self.suspect_banner = QtWidgets.QLabel("")
        self.suspect_banner.setWordWrap(True)
        self.suspect_banner.setObjectName("suspectBanner")
        self.suspect_banner.setStyleSheet(
            "background: #fff8e1; color: #8a6d00; padding: 4px;"
        )
        layout.addWidget(self.suspect_banner)

        self.tree = QtWidgets.QTableWidget(0, len(ARTIFACT_COLUMNS))
        self.tree.setHorizontalHeaderLabels(list(ARTIFACT_COLUMNS))
        self.tree.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.tree.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.tree.horizontalHeader().setStretchLastSection(True)
        self.tree.doubleClicked.connect(self._on_double_click)
        layout.addWidget(self.tree, 1)

        summary_box = QtWidgets.QGroupBox("摘要")
        summary_layout = QtWidgets.QVBoxLayout(summary_box)
        self.summary_edit = QtWidgets.QPlainTextEdit()
        self.summary_edit.setReadOnly(True)
        self.summary_edit.setObjectName("summaryEdit")
        summary_layout.addWidget(self.summary_edit)
        layout.addWidget(summary_box)

        debug_box = QtWidgets.QGroupBox(DEBUG_INFO_TITLE)
        debug_layout = QtWidgets.QVBoxLayout(debug_box)
        self.debug_edit = QtWidgets.QPlainTextEdit()
        self.debug_edit.setReadOnly(True)
        # Selectable and copyable, never NoTextInteraction (spec §5.1.5).
        self.debug_edit.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
            | QtCore.Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.debug_edit.setObjectName(DEBUG_OBJECT_NAME)
        debug_layout.addWidget(self.debug_edit)
        layout.addWidget(debug_box)

        # The results page polls on its own 15 s tier (spec §5.5.1), so it
        # needs its own status row: a red bar / 5xx hint must be visible
        # on the page that is actually on screen, not on the detail page.
        self.status_row = StatusRow()
        self.status_row.setObjectName("resultsStatusRow")
        layout.addWidget(self.status_row)
        # Spec §5.6.3: the progress denominator is the response own
        # Content-Length, so a chunked body shows an indeterminate
        # (busy) bar instead of a percentage over a made up total.
        self.download_bar = QtWidgets.QProgressBar()
        self.download_bar.setObjectName("downloadProgress")
        self.download_bar.setRange(0, 100)
        self.download_bar.setValue(0)
        self.download_bar.setFormat("%p%")
        self.download_bar.setVisible(False)
        layout.addWidget(self.download_bar)
        self.status_label = QtWidgets.QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        buttons = QtWidgets.QHBoxLayout()
        self.back_button = QtWidgets.QPushButton("返回")
        self.download_button = QtWidgets.QPushButton("下载结果")
        self.open_dir_button = QtWidgets.QPushButton("打开所在目录")
        self.back_button.clicked.connect(self.back_requested.emit)
        self.download_button.clicked.connect(
            lambda: self.download_requested.emit(self.job_id)
        )
        self.open_dir_button.clicked.connect(
            lambda: self.open_directory_requested.emit(self.last_saved_path)
        )
        buttons.addWidget(self.back_button)
        buttons.addWidget(self.download_button)
        buttons.addWidget(self.open_dir_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.last_saved_path = ""
        self.set_files([])

    # ------------------------------------------------------------- render

    def set_job(self, job: Any, files: Any = None) -> None:
        """Render one job plus its `files[]` (spec §5.6.1, §5.6.2)."""

        self.job = job
        self.job_id = str(_get(job, "job_id") or "")
        self.job_label.setText("产物：{0}".format(self.job_id))
        if files is not None:
            self.set_files(files, job_id=self.job_id)
        self.set_partial_text(partial_result_text(job))
        self.set_suspect_text(artifact_suspect_text(job))
        self.set_status(self._download_hint(job, self.files))

    def _download_hint(self, job: Any, files: Any) -> str:
        entries = list(files or [])
        if entries and not has_weight_file(entries):
            return NO_WEIGHT_TEXT
        if not entries:
            return NO_ARTIFACT_TEXT
        attempt = _get(job, "attempt")
        maximum = _get(job, "max_attempts")
        # The one terminal formula of spec §5.5.4: an old server without
        # the field must still be read through finished_at, otherwise a
        # finished job would keep showing the auto retry hint.
        if attempt and maximum and not is_terminal(job):
            return WAIT_AUTO_RETRY_TEXT.format(attempt, maximum)
        return ""

    def set_files(self, files: Any, job_id: str = "") -> None:
        """Render the artifact list, grouped complete / partial.

        `job_id` records whose manifest these rows are.  The summary
        area of spec §5.6.3 is filled out of this very list, so a read
        must never be answered by the rows of another job; the dialog
        compares this value against the job it is asked about.
        """

        if job_id:
            self.files_job_id = str(job_id)
        self.files = list(files or [])
        self.tree.setRowCount(len(self.files))
        for row, entry in enumerate(self.files):
            path = str(_get(entry, "path") or "")
            partial = bool(_get(entry, "partial")) or path.startswith(
                PARTIAL_PREFIX
            )
            cells = (
                path,
                format_size(_get(entry, "size")),
                str(_get(entry, "mtime") or ""),
                "上一轮中止的产物" if partial else "",
                GROUP_PARTIAL if partial else GROUP_COMPLETE,
            )
            for column, text in enumerate(cells):
                item = QtWidgets.QTableWidgetItem(text)
                if column == 0:
                    item.setToolTip(str(_get(entry, "file_id") or ""))
                self.tree.setItem(row, column, item)
        self.tree.resizeColumnsToContents()
        self.download_button.setEnabled(
            bool(self.files) and has_weight_file(self.files)
        )

    def set_partial_text(self, text: str) -> None:
        self.partial_banner.setText(text)
        self.partial_banner.setVisible(bool(text))

    def set_suspect_text(self, text: str) -> None:
        self.suspect_banner.setText(text)
        self.suspect_banner.setVisible(bool(text))

    def set_summary(self, summary: Any) -> None:
        self.summary_edit.setPlainText("\n".join(summary_lines(summary)))

    def set_status(self, text: str) -> None:
        self.status_label.setText(text)
        if text:
            self.status_row.set_lines([("info", text)])

    # -------------------------------------------------------- debug area

    def append_debug_line(self, text: str) -> None:
        """Append one line to the debug area (append only, §5.1.5)."""

        if not text:
            return
        self.debug_edit.appendPlainText(str(text))

    def debug_text(self) -> str:
        return self.debug_edit.toPlainText()

    def debug_object_name(self) -> str:
        return self.debug_edit.objectName()

    def set_saved_path(self, path: str) -> None:
        """Remember the last written file and say where it went.

        Spec §5.6.3: the zip is never unpacked, so the path is the one
        the user chose in the save dialog; the button next to it opens
        that folder.
        """

        self.last_saved_path = str(path or "")
        self.clear_download_progress()
        if path:
            self.set_status("已保存到 {0}".format(path))

    def set_download_progress(self, received: Any, total: Any) -> str:
        """One progress line plus the bar, Content-Length driven.

        Spec §5.6.3: with a Content-Length the bar is determinate and the
        line shows the percentage; without one the bar turns into the
        busy indicator (setRange(0, 0)) and the line only reports the
        received amount - never a percentage over a made up total.
        """

        text = download_progress_text(received, total)
        self.status_label.setText(text)
        self.status_row.set_lines([("info", text)])
        self._apply_download_bar(received, total)
        return text

    def _apply_download_bar(self, received: Any, total: Any) -> None:
        """Determinate when the length is known, busy otherwise."""

        try:
            whole: Any = int(total) if total is not None else None
            done = int(received)
        except (TypeError, ValueError):
            whole = None
            done = 0
        if whole is None or whole < 0:
            if self.download_bar.maximum() != 0:
                self.download_bar.setRange(0, 0)
                self.download_bar.setFormat(DOWNLOAD_BUSY_BAR_TEXT)
        else:
            if self.download_bar.maximum() == 0:
                self.download_bar.setRange(0, 100)
                self.download_bar.setFormat("%p%")
            # Content-Length: 0 is a known length: the transfer is
            # complete, so the bar reads 100% instead of staying busy.
            percent = 100 if whole == 0 else min(100, int(100 * done / whole))
            self.download_bar.setValue(percent)
        self.download_bar.setVisible(True)

    def clear_download_progress(self) -> None:
        """Hide the bar and forget the transfer it described."""

        self.download_bar.setRange(0, 100)
        self.download_bar.setValue(0)
        self.download_bar.setFormat("%p%")
        self.download_bar.setVisible(False)

    def summary_entry(self) -> Optional[Any]:
        """The manifest row of `summary.json`, if this job has one.

        Spec §5.6.3 renders the summary area from `summary.json`; the
        manifest is what says whether it exists (a cancelled or failed
        job without a summary simply has no such row).
        """

        for entry in self.files:
            if str(_get(entry, "path") or "") == SUMMARY_PATH:
                return entry
        return None

    def artifact_entry(self, file_id: str) -> Optional[Any]:
        """The files[] entry of one opaque file_id (spec §3.10.1).

        The lookup is by the identifier the server sent, never by a path
        the client rebuilt: file_id is an opaque string (spec §3.10.1).
        """

        wanted = str(file_id or "")
        if not wanted:
            return None
        for entry in self.files:
            if str(_get(entry, "file_id") or "") == wanted:
                return entry
        return None

    def entry_save_name(self, job_id: str, file_id: str) -> str:
        """Default file name of one download (spec §5.6.3).

        A single artifact keeps the basename of its manifest path (that
        path is display only - the download is addressed by file_id);
        the archive defaults to <job_id>.zip.
        """

        if not file_id:
            return "{0}.zip".format(str(job_id or "results"))
        entry = self.artifact_entry(file_id)
        name = osp.basename(str(_get(entry, "path") or ""))
        return name or str(file_id)

    # ------------------------------------------------------------- slots

    def _on_double_click(self, index: QtCore.QModelIndex) -> None:
        row = index.row()
        if row < 0 or row >= len(self.files):
            return
        entry = self.files[row]
        file_id = str(_get(entry, "file_id") or "")
        if file_id:
            # Partial entries download like any other one: the manifest
            # is the whitelist and the page only groups them apart
            # (spec §5.6.1, §5.6.2).  The download is addressed by the
            # opaque file_id, never by the displayed path (spec §3.10).
            self.file_download_requested.emit(self.job_id, file_id)
