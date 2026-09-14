"""Job detail page of the remote training sub window (spec §5.1.3).

One job in full: the status badge, the progress bar, the metric cards,
the queue reason, the parameter snapshot the server actually used
(`resolved_params`, spec §3.8.5), the read only event log fed by
`GET /jobs/{job_id}/events?after=<seq>` (route 10) and the four actions.

The page only renders; the polling, the cancellation and the download
belong to the monitoring and result steps.  The resume button is enabled
from the server's own `resume_mode_available` list (spec §3.4.4), never
from a local guess.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional

from PyQt6 import QtCore, QtWidgets

from .widgets import StatusRow

__all__ = [
    "STATUS_BADGE_COLORS",
    "DetailPage",
    "event_line",
    "format_metrics",
    "job_metrics",
    "job_progress",
    "resolved_param_lines",
]

#: Badge colour per status of spec §3.4.1.
STATUS_BADGE_COLORS = {
    "queued": "#8a6d00",
    "preparing": "#8a6d00",
    "running": "#1f6feb",
    "completed": "#1a7f37",
    "failed": "#b00020",
    "interrupted": "#b00020",
    "cancelled": "#b00020",
}

_QUEUED_REASON_LABELS = {
    "WAITING_DEVICE_VRAM": "等待显存释放",
    "WAITING_PREVIOUS_JOBS": "等待同卡上的任务",
    "WAITING_CONCURRENCY_SLOT": "并发位已满",
}


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def job_progress(job: Any) -> Dict[str, Any]:
    """`{epoch, total_epochs, percent}` of spec §3.4.4."""

    progress = _get(job, "progress") or {}
    return {
        "epoch": _get(progress, "epoch", 0) or 0,
        "total_epochs": _get(progress, "total_epochs", 0) or 0,
        "percent": _get(progress, "percent", 0.0) or 0.0,
    }


def job_metrics(job: Any) -> Dict[str, Any]:
    """The last `metrics` event of the job (spec §3.4.4)."""

    metrics = _get(job, "metrics")
    return dict(metrics) if isinstance(metrics, dict) else {}


def format_metrics(metrics: Any) -> List[str]:
    """One "name: value" line per metric, in the payload order."""

    lines: List[str] = []
    if not isinstance(metrics, dict):
        return lines
    for name, value in metrics.items():
        if isinstance(value, float):
            lines.append("{0}: {1:.4g}".format(name, value))
        else:
            lines.append("{0}: {1}".format(name, value))
    return lines


def resolved_param_lines(params: Any) -> List[str]:
    """The eight authoritative fields of `resolved_params` (§3.8.5)."""

    if not isinstance(params, dict):
        return []
    order = (
        "optimizer",
        "optimizer_preset",
        "optimizer_source",
        "batch",
        "requested_batch",
        "batch_assumed",
        "device",
        "oom_retry_max",
    )
    lines = []
    for name in order:
        if name in params:
            lines.append("{0}: {1}".format(name, params[name]))
    for name, value in params.items():
        if name not in order:
            lines.append("{0}: {1}".format(name, value))
    return lines


def event_line(event: Any) -> str:
    """One log line for an event object of spec §3.5."""

    seq = _get(event, "seq")
    etype = str(_get(event, "type") or "")
    ts = str(_get(event, "ts") or "")
    data = _get(event, "data")
    detail = ""
    if isinstance(data, dict):
        if data.get("message"):
            detail = str(data["message"])
        elif data:
            detail = json.dumps(data, ensure_ascii=False)
    elif data:
        detail = str(data)
    head = "#{0} {1} {2}".format(seq, ts, etype).strip()
    return "{0} {1}".format(head, detail).strip()


class DetailPage(QtWidgets.QWidget):
    """Show one job and its four actions."""

    back_requested = QtCore.pyqtSignal()
    results_requested = QtCore.pyqtSignal(str)
    cancel_requested = QtCore.pyqtSignal(str)
    resume_requested = QtCore.pyqtSignal(str)
    download_requested = QtCore.pyqtSignal(str)
    open_artifacts_requested = QtCore.pyqtSignal(str)

    def __init__(self, parent: Optional[Any] = None) -> None:
        super().__init__(parent)
        self.job_id = ""
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        header = QtWidgets.QHBoxLayout()
        self.job_label = QtWidgets.QLabel("")
        self.job_label.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.badge = QtWidgets.QLabel("")
        self.badge.setObjectName("statusBadge")
        header.addWidget(self.job_label, 1)
        header.addWidget(self.badge)
        layout.addLayout(header)

        # The status row of the monitoring step (spec §5.5.6 red bar,
        # §5.6.4 error wording, §5.5.7 stopping line): one multi line
        # widget, so a red bar and an information line can coexist
        # instead of replacing each other.
        self.status_row = StatusRow()
        self.status_row.setObjectName("detailStatusRow")
        layout.addWidget(self.status_row)

        self.bar = QtWidgets.QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        layout.addWidget(self.bar)

        self.progress_label = QtWidgets.QLabel("")
        layout.addWidget(self.progress_label)

        self.queued_reason_label = QtWidgets.QLabel("")
        self.queued_reason_label.setWordWrap(True)
        layout.addWidget(self.queued_reason_label)

        # `queued_reason` and `needs_attention` are independent fields
        # (spec §3.4.4) and can both be set, so they never share a label.
        self.attention_label = QtWidgets.QLabel("")
        self.attention_label.setWordWrap(True)
        self.attention_label.setStyleSheet("color: #b00020;")
        layout.addWidget(self.attention_label)

        self.metrics_label = QtWidgets.QLabel("")
        self.metrics_label.setWordWrap(True)
        self.metrics_label.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.metrics_label)

        self.params_label = QtWidgets.QLabel("")
        self.params_label.setWordWrap(True)
        self.params_label.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.params_label)

        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setObjectName("eventLog")
        layout.addWidget(self.log, 1)

        buttons = QtWidgets.QHBoxLayout()
        self.back_button = QtWidgets.QPushButton("返回")
        self.results_button = QtWidgets.QPushButton("查看结果")
        self.cancel_button = QtWidgets.QPushButton("取消")
        self.resume_button = QtWidgets.QPushButton("恢复")
        self.download_button = QtWidgets.QPushButton("下载结果")
        self.artifacts_button = QtWidgets.QPushButton("打开产物目录")
        self.back_button.clicked.connect(self.back_requested.emit)
        self.results_button.clicked.connect(
            lambda: self.results_requested.emit(self.job_id)
        )
        self.cancel_button.clicked.connect(
            lambda: self.cancel_requested.emit(self.job_id)
        )
        self.resume_button.clicked.connect(
            lambda: self.resume_requested.emit(self.job_id)
        )
        self.download_button.clicked.connect(
            lambda: self.download_requested.emit(self.job_id)
        )
        self.artifacts_button.clicked.connect(
            lambda: self.open_artifacts_requested.emit(self.job_id)
        )
        for button in (
            self.back_button,
            self.results_button,
            self.cancel_button,
            self.resume_button,
            self.download_button,
            self.artifacts_button,
        ):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

    # ------------------------------------------------------------- render

    def set_job(self, job: Any) -> None:
        """Render one job object (spec §3.4.4)."""

        self.job = job
        self.job_id = str(_get(job, "job_id") or "")
        status = str(_get(job, "status") or "")
        self.job_label.setText(
            "{0}（{1}）".format(self.job_id, status or "-")
        )
        self._set_badge(status)
        progress = job_progress(job)
        self.set_progress(
            progress["percent"], progress["epoch"], progress["total_epochs"]
        )
        self.set_metrics(job_metrics(job))
        self.set_queued_reason(_get(job, "queued_reason"))
        self.set_resolved_params(_get(job, "resolved_params"))
        self.set_resume_modes(_get(job, "resume_mode_available"))
        self.attention_label.setText("")
        self.set_attention(
            bool(_get(job, "needs_attention")),
            _get(job, "needs_attention_reason"),
            _get(job, "error_summary"),
        )
        if not _get(job, "needs_attention"):
            self.attention_label.setVisible(False)

    def _set_badge(self, status: str) -> None:
        color = STATUS_BADGE_COLORS.get(status, "#4a4a4a")
        self.badge.setText(_status_label(status))
        self.badge.setStyleSheet(
            "color: white; background: {0}; padding: 2px 8px;"
            " border-radius: 6px;".format(color)
        )

    def set_progress(
        self, percent: Any, epoch: Any = 0, total_epochs: Any = 0
    ) -> None:
        try:
            value = float(percent)
        except (TypeError, ValueError):
            value = 0.0
        self.bar.setValue(int(round(value)))
        self.progress_label.setText(
            "进度 {0:.1f}%（{1} / {2} epochs）".format(
                value, epoch, total_epochs
            )
        )

    def set_metrics(self, metrics: Any) -> None:
        lines = format_metrics(metrics)
        self.metrics_label.setText("\n".join(lines))

    def set_queued_reason(self, reason: Any) -> None:
        if not reason:
            self.queued_reason_label.setText("")
            self.queued_reason_label.setVisible(False)
            return
        text = str(reason)
        label = _QUEUED_REASON_LABELS.get(text.split(" ")[0])
        if label:
            text = "{0}（{1}）".format(label, text)
        self.queued_reason_label.setText("排队原因：" + text)
        self.queued_reason_label.setVisible(True)

    def set_resolved_params(self, params: Any) -> None:
        lines = resolved_param_lines(params)
        self.params_label.setText(
            "服务端实际使用参数：\n" + "\n".join(lines) if lines else ""
        )

    def set_resume_modes(self, modes: Any) -> None:
        """Enable resume from the server's own list (spec §3.4.4)."""

        available = list(modes or [])
        self.resume_modes = available
        self.resume_button.setEnabled(bool(available))
        self.resume_button.setToolTip(
            "可恢复模式：" + " / ".join(available)
            if available
            else "服务端未声明可恢复模式"
        )

    def set_attention(
        self,
        needs_attention: bool,
        reason: Any = None,
        error_summary: Any = None,
    ) -> None:
        self.needs_attention = bool(needs_attention)
        self.attention_reason = reason
        if not needs_attention:
            self.attention_label.setText("")
            self.attention_label.setVisible(False)
            return
        text = "需关注：{0}".format(reason or "")
        if error_summary:
            text += "\n" + str(error_summary)
        self.attention_label.setText(text)
        self.attention_label.setVisible(True)

    # ---------------------------------------------------------------- log

    def clear_log(self) -> None:
        self.log.clear()
        self.seen_seqs = set()

    def append_events(self, events: Iterable[Any]) -> int:
        """Append the new events, skipping the ones already shown.

        Returns the highest `seq` seen, which is what the next
        `?after=` of route 10 needs (spec §5.5.3).
        """

        seen = getattr(self, "seen_seqs", set())
        highest = max(seen) if seen else 0
        for event in events or []:
            seq = _get(event, "seq")
            if isinstance(seq, int):
                if seq in seen:
                    continue
                seen.add(seq)
                highest = max(highest, seq)
            self.log.appendPlainText(event_line(event))
        self.seen_seqs = seen
        return highest

    def append_line(self, text: str) -> None:
        self.log.appendPlainText(text)


def _status_label(status: str) -> str:
    labels = {
        "queued": "排队中",
        "preparing": "准备中",
        "running": "训练中",
        "completed": "已完成",
        "failed": "失败",
        "interrupted": "已中断",
        "cancelled": "已取消",
    }
    return labels.get(status, status or "-")
