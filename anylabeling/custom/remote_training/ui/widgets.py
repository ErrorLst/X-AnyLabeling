"""Shared widgets of the remote training sub window (spec §5.1.3).

The building blocks of the four pages live here so that the page modules
only describe layout and signals:

- the read only path box with its browse button (the three piece combo
  copied from the model validation configuration page);
- the severity aware status line (red = blocking, yellow = hint, plain =
  information) the configuration page uses for the local pre-check, the
  split preview warnings and the connection test;
- the connection test worker: one background probe of the training
  health route so that a slow or dead server never freezes the window.

Status row contract (spec §5.2.5, §5.2.8): the texts are always the ones
the data pipeline already produced (``status_text()`` / ``issues`` /
``format_lines()``), never a second wording written here.
"""

from __future__ import annotations

import threading
from typing import Any, List, Optional, Sequence

from PyQt6 import QtCore, QtWidgets

__all__ = [
    "CONNECT_AUTH_FAILED_TEXT",
    "CONNECT_NOT_READY_TEXT",
    "CONNECT_OK_TEXT",
    "CONNECT_WRONG_ADDRESS_TEXT",
    "ConnectionTest",
    "ConnectionTestWorker",
    "PrecheckWorker",
    "STATUS_INFO",
    "STATUS_RED",
    "STATUS_YELLOW",
    "StatusRow",
    "browse_directory",
    "browse_file",
    "readonly_path",
    "set_line",
    "status_color",
]

#: Severity tags of one status line.
STATUS_INFO = "info"
STATUS_YELLOW = "yellow"
STATUS_RED = "red"

_INFO_COLOR = "#1a1a1a"
_YELLOW_COLOR = "#8a6d00"
_RED_COLOR = "#b00020"

#: Connection test wording of spec §5.1.3 (used verbatim).
CONNECT_OK_TEXT = "连接成功"
CONNECT_AUTH_FAILED_TEXT = "Token 无效或已过期"
CONNECT_WRONG_ADDRESS_TEXT = "地址不正确：未找到训练接口"
CONNECT_NOT_READY_TEXT = (
    "服务端正在启动（首次启动可能需要 10–15 分钟做显存基线标定），"
    "将自动重试；若长时间（超过 20 分钟）仍然连接失败，请让管理员检查"
    "服务端日志：可能因未配置鉴权 key 而拒绝启动"
    "（security.api_key_enabled / XANYLABELING_API_KEY）"
)
CONNECT_DISABLED_TEXT = "服务端未启用远程训练"


def status_color(severity: str) -> str:
    """Return the text colour of one status line."""

    if severity == STATUS_RED:
        return _RED_COLOR
    if severity == STATUS_YELLOW:
        return _YELLOW_COLOR
    return _INFO_COLOR


def set_line(label: QtWidgets.QLabel, text: str, severity: str) -> None:
    """Write one severity tagged line into a label."""

    label.setText(text)
    label.setStyleSheet("color: {0};".format(status_color(severity)))
    label.setVisible(bool(text))


class StatusRow(QtWidgets.QWidget):
    """A multi line, selectable status row with per line severity.

    Every line is its own label so that one blocking red line and one
    informational line can coexist (spec §5.2.5: the red status row is a
    top-of-page element that never swallows the other channels).
    """

    def __init__(self, parent: Optional[Any] = None) -> None:
        super().__init__(parent)
        self._labels: List[QtWidgets.QLabel] = []
        self._lines: List[Any] = []
        self.layout = QtWidgets.QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(2)

    def clear(self) -> None:
        """Remove every line."""

        self._lines = []
        while self._labels:
            label = self._labels.pop()
            self.layout.removeWidget(label)
            label.setParent(None)
            label.deleteLater()

    def set_lines(self, lines: Sequence[Any]) -> None:
        """Replace the content with (severity, text) pairs or plain texts."""

        self.clear()
        for entry in lines:
            self.append_line(entry)

    def append_line(self, entry: Any) -> None:
        """Append one line; a (severity, text) pair or a plain string."""

        severity, text = _split_entry(entry)
        if not text:
            return
        label = QtWidgets.QLabel(text)
        label.setWordWrap(True)
        label.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
            | QtCore.Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        label.setStyleSheet("color: {0};".format(status_color(severity)))
        self._labels.append(label)
        self._lines.append((severity, text))
        self.layout.addWidget(label)

    def lines(self) -> List[str]:
        """The visible lines, in order."""

        return [text for _severity, text in self._lines]

    def severities(self) -> List[str]:
        """The severity of every visible line, in order."""

        return [severity for severity, _text in self._lines]

    def is_empty(self) -> bool:
        return not self._lines

    def has_red(self) -> bool:
        """True when at least one blocking line is shown."""

        return STATUS_RED in self.severities()

    def has_yellow(self) -> bool:
        return STATUS_YELLOW in self.severities()


def _split_entry(entry: Any) -> Any:
    if isinstance(entry, (tuple, list)) and len(entry) == 2:
        return str(entry[0]), str(entry[1])
    return STATUS_INFO, str(entry)


def readonly_path(
    placeholder: str, tooltip: str = ""
) -> QtWidgets.QLineEdit:
    """One read only line edit (spec §5.1.3).

    Directory and classes boxes are read only: the dataset directory is
    strictly read only (spec §5.1.3), so it is never typed into either.
    """

    line = QtWidgets.QLineEdit()
    line.setReadOnly(True)
    line.setPlaceholderText(placeholder)
    if tooltip:
        line.setToolTip(tooltip)
    return line


def wrap_with_button(
    line_edit: QtWidgets.QLineEdit,
    text: str,
    slot: Any,
    tooltip: str = "",
) -> QtWidgets.QWidget:
    """Wrap a line edit with its browse button (model validation shape)."""

    container = QtWidgets.QWidget()
    layout = QtWidgets.QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(line_edit, 1)
    button = QtWidgets.QPushButton(text)
    if tooltip:
        button.setToolTip(tooltip)
    button.clicked.connect(slot)
    layout.addWidget(button)
    return container


def browse_directory(
    parent: QtWidgets.QWidget, title: str, current: str = ""
) -> str:
    """Pick a directory; an empty string when the user cancels."""

    chosen = QtWidgets.QFileDialog.getExistingDirectory(
        parent, title, current or ""
    )
    return str(chosen or "")


def browse_file(
    parent: QtWidgets.QWidget,
    title: str,
    name_filter: str,
    current: str = "",
) -> str:
    """Pick a file; an empty string when the user cancels."""

    path, _selected = QtWidgets.QFileDialog.getOpenFileName(
        parent, title, current or "", name_filter
    )
    return str(path or "")


class ConnectionTest:
    """Outcome of one connection test (state + displayable lines)."""

    def __init__(
        self,
        state: str = "",
        lines: Optional[Sequence[Any]] = None,
        probe: Any = None,
        error: Optional[BaseException] = None,
        capabilities: Any = None,
    ) -> None:
        self.state = state
        self.lines: List[Any] = list(lines or [])
        self.probe = probe
        self.error = error
        self.capabilities = capabilities

    def texts(self) -> List[str]:
        return [text for _severity, text in
                (_split_entry(entry) for entry in self.lines)]

    def severities(self) -> List[str]:
        return [severity for severity, _text in
                (_split_entry(entry) for entry in self.lines)]

    def ok(self) -> bool:
        return not self.error and bool(self.probe) and not self.probe.error


class ConnectionTestWorker(QtCore.QThread):
    """One background probe of the training health route (spec §5.1.3).

    The probe uses the client of the window, which serves a 10 s connect
    timeout; running it here keeps the event loop responsive and lets the
    close state machine wait for it like any other worker.
    """

    finished_with = QtCore.pyqtSignal(object)

    def __init__(
        self,
        client: Any,
        parent: Optional[QtCore.QObject] = None,
        *,
        should_stop: Optional[Any] = None,
    ) -> None:
        super().__init__(parent)
        self._client = client
        self._should_stop = should_stop

    def run(self) -> None:  # noqa: D102 - QThread entry point
        probe = None
        capabilities = None
        error = None
        if not (self._should_stop is not None and self._should_stop()):
            try:
                probe = self._client.probe_health()
            except Exception as exc:  # pragma: no cover - defensive
                error = exc
            # The parameter surface and the vram table of the pre-check
            # come from capabilities (spec §5.1.3): fetch them in the same
            # worker so the window never waits on the network.  A failed
            # negotiation must not change the four probe states.
            health = getattr(probe, "health", None)
            if health is not None:
                try:
                    capabilities = self._client.get_capabilities()
                except Exception:  # pragma: no cover - defensive
                    capabilities = None
        self.finished_with.emit(
            ConnectionTest(
                probe=probe,
                capabilities=capabilities,
                error=error,
            )
        )


class PrecheckWorker(QtCore.QThread):
    """The local data pipeline, off the GUI thread (spec §5.1.4 step 3).

    `run()` hands the cancellation callback of the close machine straight
    to `Pipeline.assemble(should_stop=...)`, so the step 3 cancellation of
    spec §5.1.4 stops this worker at the next image boundary (the converter
    checks between images, the packing loop before every image) instead of
    freezing the window, and step 4 waits for it without killing the
    thread.  The outcome is emitted as `(pipeline, run, error)`.
    """

    finished_with = QtCore.pyqtSignal(object, object, object)

    def __init__(
        self,
        pipeline: Any,
        parent: Optional[QtCore.QObject] = None,
        *,
        cancel_event: Optional[Any] = None,
    ) -> None:
        super().__init__(parent)
        self._pipeline = pipeline
        self._cancel_event = cancel_event or threading.Event()
        self.run_result: Any = None
        self.error: Optional[BaseException] = None

    def cancel(self) -> None:
        """The step 3 hook of the close state machine."""

        self._cancel_event.set()

    def should_stop(self) -> bool:
        return self._cancel_event.is_set()

    def run(self) -> None:  # noqa: D102 - QThread entry point
        try:
            self.run_result = self._pipeline.assemble(
                should_stop=self.should_stop
            )
        except BaseException as exc:  # noqa: BLE001 - reported to the UI
            self.error = exc
        self.finished_with.emit(
            self._pipeline, self.run_result, self.error
        )
