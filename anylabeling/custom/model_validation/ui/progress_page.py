"""Progress page of the model validation sub window."""

from __future__ import annotations

from typing import Any, Optional

from PyQt6 import QtCore, QtWidgets

STAGE_LABELS = {
    "staging": "① 拷贝到临时目录 / Staging",
    "augment": "② 生成增强副本 / Augmenting",
    "infer": "③ ONNX 推理与判定 / Inference",
}

# A cancel is honoured at the boundary of a file: a stage blocked in
# something it cannot interrupt (building the ONNX sessions, for
# instance) keeps running for a moment, and the page says so.
CANCEL_MESSAGE = "正在结束…"


class ProgressPage(QtWidgets.QWidget):
    """Show the three pipeline stages with progress and a log."""

    cancel_requested = QtCore.pyqtSignal()

    def __init__(self, parent: Optional[Any] = None) -> None:
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        self.stage_labels = {}
        for key in ("staging", "augment", "infer"):
            label = QtWidgets.QLabel(self.tr(STAGE_LABELS[key]))
            self.stage_labels[key] = label
            layout.addWidget(label)

        self.bar = QtWidgets.QProgressBar()
        self.bar.setRange(0, 1)
        layout.addWidget(self.bar)

        self.message_label = QtWidgets.QLabel("")
        layout.addWidget(self.message_label)

        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log, 1)

        # the one and only control of this page: a cancel ends the run
        # right here, drops it and puts the form back on screen. There is
        # deliberately no second button next to it - leaving the page
        # without cancelling would keep the worker running in the back
        # ground, which is exactly what a cancel avoids.
        buttons = QtWidgets.QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QtWidgets.QPushButton(self.tr("取消"))
        self.cancel_button.clicked.connect(self.cancel_requested.emit)
        buttons.addWidget(self.cancel_button)
        layout.addLayout(buttons)

    def reset(self) -> None:
        """Reset the progress display for a new run.

        The bar, the stage lines and the log go back to the state of a
        fresh run, so a cancelled run leaves nothing behind: the next one
        starts from an empty page and can be cancelled again.
        """

        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self.log.clear()
        self.message_label.setText("")
        self.cancel_button.setEnabled(True)
        for key, label in self.stage_labels.items():
            label.setText(self.tr(STAGE_LABELS[key]))

    def show_ending(self) -> None:
        """Announce that a cancelled stage is still winding down."""

        self.message_label.setText(self.tr(CANCEL_MESSAGE))
        self.append_line(self.tr(CANCEL_MESSAGE))
        self.cancel_button.setEnabled(False)

    def set_progress(
        self, stage: str, done: int, total: int, message: str
    ) -> None:
        """Update the progress bar for one stage."""

        self.bar.setRange(0, max(int(total), 1))
        self.bar.setValue(int(done))
        self.message_label.setText(message)
        if message:
            self.append_line(f"[{stage}] {message}")

    def mark_stage_done(self, stage: str, count: int) -> None:
        """Mark a finished stage in the stage list."""

        label = self.stage_labels.get(stage)
        if label is not None:
            base = self.tr(STAGE_LABELS.get(stage, stage))
            label.setText(f"✓ {base} ({count})")
        self.append_line(f"✓ {stage}: {count}")

    def append_line(self, text: str) -> None:
        """Append one line to the log view."""

        self.log.appendPlainText(text)


__all__ = ["CANCEL_MESSAGE", "ProgressPage", "STAGE_LABELS"]
