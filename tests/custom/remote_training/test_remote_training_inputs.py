"""Offscreen net for the input presentation rule (spec §5.1.3).

The configuration form is keyboard / click only: a number never shows
the up / down step buttons, and no wheel turn changes a value.  The
wheel keeps propagating (the event stays *unaccepted*), so a stray turn
over the form scrolls the parameter page - it never repaints a
parameter.  The controls stay `QComboBox` / `QSpinBox` /
`QDoubleSpinBox` subclasses, so the readers, the explicit bookkeeping
and the request body are untouched (spec §5.2.2).

Only the presentation is asserted here: the form semantics have their
own nets (`test_remote_training_ui.py`) and this file must not weaken
them.
"""

from __future__ import annotations

import pytest
from PyQt6 import QtCore, QtGui, QtWidgets

from anylabeling.custom.remote_training import launcher
from anylabeling.custom.remote_training.ui.config_page import ConfigPage

CAPABILITIES = {
    "param_schema": {
        "epochs": {"type": "int", "min": 1, "max": 1000},
        "optimizer": {"type": "preset",
                      "values": ["yolo11-sgd", "yolo11-adamw", "auto"]},
    },
    "optimizer_presets": {
        "yolo11-sgd": {"optimizer": "SGD"},
        "yolo11-adamw": {"optimizer": "AdamW"},
    },
    "model_families": {
        "yolo11": {"weights": {"detect": ["yolo11n.pt", "yolo11s.pt"],
                               "segment": ["yolo11n-seg.pt"]},
                   "presets": ["yolo11-sgd", "yolo11-adamw", "auto"]},
    },
    "preset_policy": {"default_preset": {"yolo11": "yolo11-sgd"}},
    "tasks": ["detect", "segment"],
}

#: The untouched form keeps sending its two value defaults.
ALWAYS_SENT = {"batch": 16, "optimizer": "auto"}

NO_BUTTONS = QtWidgets.QAbstractSpinBox.ButtonSymbols.NoButtons


class Parent(QtWidgets.QMainWindow):
    """Stand in for the labeling main window."""


def wheel_event(delta: int = -120) -> QtGui.QWheelEvent:
    """One wheel turn over a control (the whole input of these tests)."""

    return QtGui.QWheelEvent(
        QtCore.QPointF(5, 5), QtCore.QPointF(5, 5),
        QtCore.QPoint(0, 0), QtCore.QPoint(0, delta),
        QtCore.Qt.MouseButton.NoButton,
        QtCore.Qt.KeyboardModifier.NoModifier,
        QtCore.Qt.ScrollPhase.NoScrollPhase, False,
    )


def turn(widget: QtWidgets.QWidget, delta: int = -120) -> bool:
    """Send one wheel turn and report whether the control accepted it."""

    event = wheel_event(delta)
    QtWidgets.QApplication.sendEvent(widget, event)
    return event.isAccepted()


def number_widgets(page) -> list:
    """Every numeric control of the page: the 41 form boxes plus
    `val_ratio`."""

    found = [
        (name, page._params[name].widget)
        for name in sorted(page._params)
        if isinstance(page._params[name].widget,
                      (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox))
    ]
    return found + [("val_ratio", page.val_ratio_spin)]


def drop_down_widgets(page) -> list:
    """Every drop down of the page: the form ones and the three pickers."""

    found = [
        (name, page._params[name].widget)
        for name in sorted(page._params)
        if isinstance(page._params[name].widget, QtWidgets.QComboBox)
    ]
    return found + [
        ("task", page.task_combo),
        ("model_family", page.model_family_combo),
        ("model", page.model_combo),
    ]


@pytest.fixture
def page(qapp):
    """One built configuration page on the frozen capabilities."""

    widget = ConfigPage()
    widget.set_capabilities(dict(CAPABILITIES))
    widget.resize(1200, 900)
    widget.show()
    qapp.processEvents()
    yield widget
    widget.close()
    widget.deleteLater()
    qapp.processEvents()


@pytest.fixture
def dialog(qapp, pump):
    """One real window, torn down like the other UI nets tear theirs."""

    opened = []

    def _open():
        window = launcher.launch_remote_training(Parent())
        opened.append(window)
        window.show()
        pump(30)
        return window

    yield _open
    for window in opened:
        try:
            window._confirm_close = lambda _dialog: True
            window.cancel_workers()
            window.reject()
        except RuntimeError:  # pragma: no cover - already destroyed
            pass
    pump(200)


# ------------------------------------------- no step buttons on a number


def test_every_number_box_hides_its_step_buttons(page):
    """No `UpDownArrows`: the value is typed, never stepped."""

    assert len(page._params) == 41
    numbers = number_widgets(page)
    kinds = set()
    for _name, widget in numbers:
        if isinstance(widget, QtWidgets.QDoubleSpinBox):
            kinds.add("double")
        elif isinstance(widget, QtWidgets.QSpinBox):
            kinds.add("int")
    assert kinds == {"int", "double"}
    for name, widget in numbers:
        assert widget.buttonSymbols() == NO_BUTTONS, name


# ------------------------------------------------- the wheel never sets


def test_the_wheel_never_changes_a_number(page):
    """Both directions are ignored and left for the scroll area."""

    for name, widget in number_widgets(page):
        before = widget.value()
        assert turn(widget, -120) is False, name
        assert turn(widget, 120) is False, name
        assert widget.value() == before, name


def test_the_wheel_never_changes_a_drop_down(page):
    """The four form combos and the three pickers keep their entry."""

    combos = drop_down_widgets(page)
    assert len(combos) >= 7
    for name, widget in combos:
        before = (widget.currentIndex(), widget.currentText())
        assert turn(widget, -120) is False, name
        assert turn(widget, 120) is False, name
        assert (widget.currentIndex(), widget.currentText()) == before, name


def test_an_editable_drop_down_is_typed_and_never_turned(page):
    """The editable path of a drop down follows the same rule."""

    batch = page._params["batch"].widget
    batch.setEditable(True)
    try:
        batch.setFocus()
        edit = batch.lineEdit()
        edit.clear()
        for char in "32":
            QtWidgets.QApplication.sendEvent(edit, QtGui.QKeyEvent(
                QtCore.QEvent.Type.KeyPress,
                getattr(QtCore.Qt.Key, "Key_" + char),
                QtCore.Qt.KeyboardModifier.NoModifier, char,
            ))
        assert edit.text() == "32"
        assert batch.currentText() == "32"
        before = (batch.currentIndex(), batch.currentText())
        assert turn(batch) is False
        assert (batch.currentIndex(), batch.currentText()) == before
    finally:
        batch.setEditable(False)


# ---------------------------------------------- the keyboard still works


def test_the_keyboard_still_enters_a_number(page):
    """Typing sets the value; `setRange` still rules an out of range one."""

    epochs = page._params["epochs"].widget
    epochs.setFocus()
    epochs.lineEdit().setText("7")
    epochs.interpretText()
    assert epochs.value() == 7
    assert page._params["epochs"].explicit is True

    doubles = [widget for _name, widget in number_widgets(page)
               if isinstance(widget, QtWidgets.QDoubleSpinBox)
               and widget.minimum() <= 0.5 <= widget.maximum()]
    assert doubles
    target = doubles[0]
    target.setFocus()
    target.lineEdit().setText("0.5")
    target.interpretText()
    assert target.value() == pytest.approx(0.5)

    lr0 = page._params["lr0"].widget
    lr0.setFocus()
    lr0.lineEdit().setText("0.25")  # above the 0.05 ceiling
    lr0.interpretText()
    assert lr0.minimum() <= lr0.value() <= lr0.maximum()


def test_the_keyboard_still_moves_a_drop_down(page):
    """Arrow keys and `setCurrentIndex` keep switching the entry."""

    imgsz = page._params["imgsz"].widget
    imgsz.setFocus()
    QtWidgets.QApplication.processEvents()
    before = imgsz.currentIndex()
    QtWidgets.QApplication.sendEvent(imgsz, QtGui.QKeyEvent(
        QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_Down,
        QtCore.Qt.KeyboardModifier.NoModifier,
    ))
    assert imgsz.currentIndex() == before + 1
    imgsz.setCurrentIndex(2)
    assert imgsz.currentIndex() == 2


# ------------------------------------------------------ regression anchor


def test_the_untouched_form_still_sends_its_two_defaults(dialog):
    """The presentation rule changes no request body (§5.2.2)."""

    window = dialog()
    page = window.config_page
    page.set_capabilities(dict(CAPABILITIES))
    assert page.values().params == ALWAYS_SENT
    assert window.job_request_body()["params"] == ALWAYS_SENT
