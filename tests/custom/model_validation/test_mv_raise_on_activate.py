"""The validation window lifts itself above the main window.

Editing in the labeling window is what hides the validation window
behind it, so the window watches the main window for its own
activations and raises itself once the activation is over. The feature
is pure window behaviour: it must never take the keyboard from the
canvas (no activateWindow), never touch a hidden or minimized window,
and leave a standalone start (no main window) exactly as it was.

The window manager is never asked for anything here: the activation
event is delivered to the stub main window by hand and the raise is
counted on the window itself, offscreen.
"""

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PyQt6 import QtCore, QtWidgets

from anylabeling.custom.model_validation.ui.dialog import (
    ModelValidationDialog,
)

# The widgets a test closes are kept referenced until the session ends:
# a window whose Python wrapper is collected while a deferred delete of
# a child is still queued crashes the Qt event loop of the next test.
_ALIVE: list = []


def keep_alive(*widgets) -> None:
    "Hold references to closed widgets for the rest of the session."

    _ALIVE.extend(widgets)


@pytest.fixture(scope="module")
def qt_app():
    "Create the offscreen application the events need."

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


class StubMainWindow(QtWidgets.QWidget):
    "The labeling window the tool is opened from."

    def __init__(self) -> None:
        super().__init__()
        self.loaded = []
        self.activated = 0

    def load_file(self, path: str) -> bool:
        self.loaded.append(str(path))
        return True

    def activateWindow(self) -> None:  # noqa: N802
        self.activated += 1


def make_pair() -> tuple:
    "Return a stub main window and a window whose parent it is."

    window = StubMainWindow()
    dialog = ModelValidationDialog(window)
    return window, dialog


def raise_spy(dialog, monkeypatch) -> list:
    "Count every raise the window performs, wherever it starts."

    calls = []
    monkeypatch.setattr(
        dialog, "raise_", lambda: calls.append("raise")
    )
    monkeypatch.setattr(
        dialog, "activateWindow", lambda: calls.append("activate")
    )
    return calls


def activate(stub) -> bool:
    "Deliver one WindowActivate to the widget, like the platform does."

    event = QtCore.QEvent(QtCore.QEvent.Type.WindowActivate)
    return bool(QtWidgets.QApplication.sendEvent(stub, event))


def flush() -> None:
    "Run the deferred raise the event queued."

    QtWidgets.QApplication.processEvents()


# --------------------------------------------------------- the lift
def test_an_activated_main_window_lifts_the_window(qt_app, monkeypatch):
    "The activation of the main window raises the visible window."

    window, dialog = make_pair()
    dialog.show()
    flush()
    calls = raise_spy(dialog, monkeypatch)
    try:
        # nothing is raised before the activation: the spy was placed
        # after the window became visible, so the counter starts empty
        assert calls == []

        activate(window)

        # the raise is deferred by one turn of the event loop: it has to
        # wait for the window manager to settle the activation
        assert calls == []
        flush()
        assert calls == ["raise"]
        # and the keyboard stays in the labeling window
        assert window.activated == 0
    finally:
        dialog.close()
        window.close()
        keep_alive(dialog, window)


def test_the_window_never_activates_itself(qt_app, monkeypatch):
    "A lift must not steal the keyboard from the canvas."

    window, dialog = make_pair()
    dialog.show()
    flush()
    calls = raise_spy(dialog, monkeypatch)
    try:
        for _ in range(3):
            activate(window)
            flush()

        assert calls == ["raise", "raise", "raise"]
        assert "activate" not in calls
    finally:
        dialog.close()
        window.close()
        keep_alive(dialog, window)


def test_an_activation_reaches_a_lazily_parented_window(
    qt_app, monkeypatch
):
    "A window that is only parented later is still watched."

    window = StubMainWindow()
    window.show()
    dialog = ModelValidationDialog()
    calls = raise_spy(dialog, monkeypatch)
    try:
        # no parent, therefore no filter: the activation changes nothing
        activate(window)
        flush()
        assert calls == []
        assert dialog._activate_filter_target is None

        # the very same window is handed in after the fact, and the
        # filter is taken from the show event
        dialog.setParent(window)
        dialog.show()
        flush()
        assert dialog.isVisible() is True

        assert dialog._activate_filter_target is window
        activate(window)
        flush()
        assert calls == ["raise"]
    finally:
        dialog.close()
        window.close()
        keep_alive(dialog, window)


def test_the_filter_is_installed_once_for_the_same_window(
    qt_app, monkeypatch
):
    "A second show does not stack a second filter on the main window."

    window, dialog = make_pair()
    dialog.show()
    flush()
    calls = raise_spy(dialog, monkeypatch)
    try:
        dialog._install_activate_filter()
        dialog._install_activate_filter()

        assert dialog._activate_filter_target is window
        activate(window)
        flush()
        # one filter, one raise: a filter installed twice would raise
        # the window twice for one single activation
        assert calls == ["raise"]
    finally:
        dialog.close()
        window.close()
        keep_alive(dialog, window)


# ------------------------------------------------------- user's state
def test_a_hidden_window_is_not_lifted(qt_app, monkeypatch):
    "A window that was never shown stays hidden."

    window, dialog = make_pair()
    calls = raise_spy(dialog, monkeypatch)
    try:
        assert dialog.isVisible() is False

        activate(window)
        flush()

        assert calls == []
    finally:
        dialog.close()
        window.close()
        keep_alive(dialog, window)


def test_a_minimized_window_is_not_lifted(qt_app, monkeypatch):
    "A window sent to the task bar is left there."

    window, dialog = make_pair()
    dialog.show()
    flush()
    calls = raise_spy(dialog, monkeypatch)
    try:
        dialog.showMinimized()
        flush()
        assert dialog.isMinimized() is True

        activate(window)
        flush()

        assert calls == []
    finally:
        dialog.close()
        window.close()
        keep_alive(dialog, window)


def test_another_event_never_lifts_the_window(qt_app, monkeypatch):
    "Only an activation of the main window is a reason to raise."

    window, dialog = make_pair()
    dialog.show()
    flush()
    calls = raise_spy(dialog, monkeypatch)
    try:
        for kind in (
            QtCore.QEvent.Type.WindowDeactivate,
            QtCore.QEvent.Type.Hide,
            QtCore.QEvent.Type.Close,
        ):
            QtWidgets.QApplication.sendEvent(window, QtCore.QEvent(kind))
        flush()

        assert calls == []
    finally:
        dialog.close()
        window.close()
        keep_alive(dialog, window)


# ------------------------------------------------- a start of its own
def test_a_standalone_start_watches_nothing(qt_app, monkeypatch):
    "Without a main window there is no filter and no exception."

    dialog = ModelValidationDialog()
    calls = raise_spy(dialog, monkeypatch)
    try:
        assert dialog._activate_target() is None
        dialog._install_activate_filter()
        assert dialog._activate_filter_target is None

        dialog.show()
        flush()

        # its own activation is not a reason to raise anything either
        activate(dialog)
        flush()

        assert calls == []
    finally:
        dialog.close()
        keep_alive(dialog)


def test_a_parent_that_is_no_labeling_window_is_ignored(
    qt_app, monkeypatch
):
    "A parent without load_file is not the window to watch."

    parent = QtWidgets.QWidget()
    dialog = ModelValidationDialog(parent)
    calls = raise_spy(dialog, monkeypatch)
    try:
        assert dialog._main_window() is None
        dialog.show()
        flush()

        assert dialog._activate_filter_target is None
        activate(parent)
        flush()
        assert calls == []
    finally:
        dialog.close()
        parent.close()
        keep_alive(dialog, parent)


# ----------------------------------------------------------- teardown
def test_the_filter_is_gone_after_the_close(qt_app, monkeypatch):
    "A closed window is not raised by a later activation."

    window, dialog = make_pair()
    dialog.show()
    flush()
    calls = raise_spy(dialog, monkeypatch)
    try:
        activate(window)
        flush()
        assert calls == ["raise"]
        assert dialog._activate_filter_target is window

        dialog.close()
        assert dialog._activate_filter_target is None

        activate(window)
        flush()
        assert calls == ["raise"]
    finally:
        window.close()
        keep_alive(dialog, window)


def test_a_top_level_parent_needs_no_window_call(qt_app, monkeypatch):
    "A parent that answers itself is watched directly."

    window = StubMainWindow()
    dialog = ModelValidationDialog(window)
    calls = raise_spy(dialog, monkeypatch)
    try:
        # the stub is its own top level window: window() answers itself
        assert dialog._activate_target() is window
        monkeypatch.setattr(
            window, "window", SimpleNamespace(), raising=False
        )
        assert dialog._activate_target() is window
    finally:
        dialog.close()
        window.close()
        keep_alive(dialog, window)


def test_the_deferred_raise_survives_a_destroyed_window(
    qt_app, monkeypatch
):
    "A raise queued while the window is destroyed is dropped silently."

    window, dialog = make_pair()
    dialog.show()
    flush()
    calls = raise_spy(dialog, monkeypatch)

    def explode() -> None:
        raise RuntimeError("wrapped C/C++ object deleted")

    monkeypatch.setattr(dialog, "raise_", explode)
    try:
        # the ordinary end of a window: the queued call must not raise
        # out of the event loop of the application
        dialog._raise_deferred()
    finally:
        monkeypatch.undo()
        dialog.close()
        window.close()
        keep_alive(dialog, window)
    assert calls == []
