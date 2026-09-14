"""Jobs page of the remote training sub window (spec §5.1.3).

A flat table over `GET /jobs?ids=` (route 8): name, status, progress,
device, elapsed time and the attention flag.  Row checkboxes drive the
bulk actions (refresh / cancel / resume / details / download) and a
double click opens the detail page.

The page renders `job` objects of spec §3.4.4 as plain mappings; the
polling that fills them belongs to the monitoring step, so nothing here
touches the network.
"""

from __future__ import annotations

import time
from typing import Any, List, Optional

from PyQt6 import QtCore, QtGui, QtWidgets

from ..poller import is_terminal
from ..store import parse_iso8601_epoch
from .widgets import StatusRow

__all__ = [
    "ATTENTION_COLUMN",
    "job_duration_seconds",
    "COLUMNS",
    "DETAIL_COLUMN",
    "DURATION_COLUMN",
    "NAME_COLUMN",
    "PROGRESS_COLUMN",
    "STATUS_COLUMN",
    "JobsPage",
    "format_duration",
    "job_name",
    "job_progress_percent",
    "job_row",
]

COLUMNS = (
    "任务名",
    "状态",
    "进度",
    "设备",
    "耗时",
    "需关注",
    "恢复次数",
)
NAME_COLUMN = 0
STATUS_COLUMN = 1
PROGRESS_COLUMN = 2
DURATION_COLUMN = 4
ATTENTION_COLUMN = 5
DETAIL_COLUMN = 6

_STATUS_LABELS = {
    "queued": "排队中",
    "preparing": "准备中",
    "running": "训练中",
    "completed": "已完成",
    "failed": "失败",
    "interrupted": "已中断",
    "cancelled": "已取消",
}


def job_name(job: Any) -> str:
    """Display name of one job.

    `client_job_name` is not a job object field (spec §3.4.4: it only
    reaches the local ledger, §5.3.2), so it is only a nicer label when the
    caller merged the ledger row in; the authoritative display value of a
    bare job object is its `job_id`.
    """

    name = str(_get(job, "client_job_name") or "")
    if name:
        return name
    return str(_get(job, "job_id") or "")

def job_duration_seconds(job: Any, now: Any = None) -> Optional[float]:
    """Elapsed seconds from the contract timestamps (spec §3.4.4).

    The job object has no duration field: it is derived from
    `started_at` (or `created_at` before a device was assigned) and
    `finished_at`.  A running job is measured against `now`.
    """

    started = parse_iso8601_epoch(_get(job, "started_at"))
    if started is None:
        started = parse_iso8601_epoch(_get(job, "created_at"))
    if started is None:
        return None
    finished = parse_iso8601_epoch(_get(job, "finished_at"))
    if finished is None:
        # The terminal judgement is the one formula of spec §5.5.4, never
        # the bare field: an old server only sends finished_at.
        if is_terminal(job):
            return None
        finished = float(now) if now is not None else time.time()
    return max(0.0, float(finished) - float(started))


def job_progress_percent(job: Any) -> float:
    """`progress.percent` of spec §3.4.4 (0.0 when absent)."""

    progress = _get(job, "progress") or {}
    value = _get(progress, "percent")
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def format_duration(seconds: Any) -> str:
    """Human readable duration; an unknown duration stays empty."""

    if seconds is None:
        return ""
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return ""
    if total < 0:
        return ""
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return "{0}h{1:02d}m".format(hours, minutes)
    return "{0}m{1:02d}s".format(minutes, secs)


def job_row(job: Any, now: Any = None) -> List[str]:
    """The seven display cells of one job object."""

    status = str(_get(job, "status") or "")
    device = _get(job, "device_index")
    attention = bool(_get(job, "needs_attention"))
    reason = _get(job, "needs_attention_reason")
    resume_cycles = _get(job, "resume_cycles")
    return [
        job_name(job),
        _STATUS_LABELS.get(status, status),
        "{0:.1f}%".format(job_progress_percent(job)),
        "" if device is None else str(device),
        format_duration(job_duration_seconds(job, now)),
        str(reason or "是") if attention else "",
        "" if resume_cycles is None else str(resume_cycles),
    ]


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


class JobsPage(QtWidgets.QWidget):
    """List every training task and offer the bulk actions."""

    refresh_requested = QtCore.pyqtSignal()
    cancel_requested = QtCore.pyqtSignal(list)
    resume_requested = QtCore.pyqtSignal(list)
    details_requested = QtCore.pyqtSignal(str)
    download_requested = QtCore.pyqtSignal(str)

    def __init__(self, parent: Optional[Any] = None) -> None:
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        self.table = QtWidgets.QTableWidget(0, len(COLUMNS) + 1)
        self.table.setHorizontalHeaderLabels(["", *COLUMNS])
        self.table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.SingleSelection
        )
        self.table.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.doubleClicked.connect(self._on_double_click)
        layout.addWidget(self.table, 1)

        # Status row of the monitoring step (spec §5.5.6 red bar); the
        # label below mirrors the list summary while the row carries the
        # composed lines (banner + summary, spec §5.4.1).
        self.status_row = StatusRow()
        self.status_row.setObjectName("jobsStatusRow")
        layout.addWidget(self.status_row)
        self.status_label = QtWidgets.QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        buttons = QtWidgets.QHBoxLayout()
        self.refresh_button = QtWidgets.QPushButton("刷新")
        self.cancel_button = QtWidgets.QPushButton("取消")
        self.resume_button = QtWidgets.QPushButton("恢复")
        self.details_button = QtWidgets.QPushButton("查看详情")
        self.download_button = QtWidgets.QPushButton("下载结果")
        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        self.cancel_button.clicked.connect(
            lambda: self.cancel_requested.emit(self.selected_ids())
        )
        self.resume_button.clicked.connect(
            lambda: self.resume_requested.emit(self.selected_ids())
        )
        self.details_button.clicked.connect(self._emit_details)
        self.download_button.clicked.connect(self._emit_download)
        for button in (
            self.refresh_button,
            self.cancel_button,
            self.resume_button,
            self.details_button,
            self.download_button,
        ):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.set_jobs([])

    # ------------------------------------------------------------- render

    def set_jobs(self, jobs: Any, now: Any = None) -> None:
        """Replace the table content with the given job objects."""

        self.now = now
        self.jobs: List[Any] = list(jobs or [])
        self.table.setRowCount(len(self.jobs))
        for row, job in enumerate(self.jobs):
            check = QtWidgets.QTableWidgetItem()
            check.setFlags(
                QtCore.Qt.ItemFlag.ItemIsUserCheckable
                | QtCore.Qt.ItemFlag.ItemIsEnabled
            )
            check.setCheckState(QtCore.Qt.CheckState.Unchecked)
            self.table.setItem(row, 0, check)
            for column, text in enumerate(
                job_row(job, self.now), start=1
            ):
                item = QtWidgets.QTableWidgetItem(text)
                if column == STATUS_COLUMN + 1 and _get(
                    job, "needs_attention"
                ):
                    item.setForeground(QtGui.QColor("#b00020"))
                self.table.setItem(row, column, item)
            self.table.item(row, 0).setData(
                QtCore.Qt.ItemDataRole.UserRole, str(_get(job, "job_id") or "")
            )
        self.table.resizeColumnsToContents()

    # The whole-row setter that used to live here (set_status) has no
    # caller left: the list summary and the reconciliation banner are
    # composed instead of replacing the row (spec §5.4.1).  It is gone on
    # purpose - one line replacing the whole row is exactly what wiped
    # the banner in the first D5 round.

    # ------------------------------------------------------------ reading

    def row_job_id(self, row: int) -> str:
        item = self.table.item(row, 0)
        if item is None:
            return ""
        return str(item.data(QtCore.Qt.ItemDataRole.UserRole) or "")

    def selected_ids(self) -> List[str]:
        """The ids of every checked row, in table order."""

        ids: List[str] = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and (
                item.checkState() == QtCore.Qt.CheckState.Checked
            ):
                ids.append(self.row_job_id(row))
        return ids

    def checked_count(self) -> int:
        return len(self.selected_ids())

    def displayed_cell(self, row: int, column: int) -> str:
        item = self.table.item(row, column)
        return "" if item is None else item.text()

    def current_job_id(self) -> str:
        row = self.table.currentRow()
        if row < 0:
            return ""
        return self.row_job_id(row)

    # ------------------------------------------------------------- slots

    def _on_double_click(self, index: QtCore.QModelIndex) -> None:
        job_id = self.row_job_id(index.row())
        if job_id:
            self.details_requested.emit(job_id)

    def _emit_details(self) -> None:
        job_id = self.current_job_id()
        if not job_id and self.jobs:
            job_id = str(_get(self.jobs[0], "job_id") or "")
        if job_id:
            self.details_requested.emit(job_id)

    def _emit_download(self) -> None:
        job_id = self.current_job_id()
        if not job_id and self.jobs:
            job_id = str(_get(self.jobs[0], "job_id") or "")
        if job_id:
            self.download_requested.emit(job_id)
