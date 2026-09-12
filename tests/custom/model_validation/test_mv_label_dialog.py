"""The label chooser: a choice out of the class table, never typing.

The dialog is built around one promise - a label can only be *picked* -
and the tests below read that promise off the real widget: the combo box
is not editable, its entries are exactly the class table of the run, the
current label is preselected while the table knows it, a label outside
the table is named above the box and never becomes an entry, the confirm
button refuses an empty table and the one entry point the results page
uses answers None whenever nothing was chosen.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PyQt6 import QtCore, QtTest, QtWidgets

from anylabeling.custom.model_validation.ui.label_dialog import (
    CANCEL_TEXT,
    CONFIRM_TEXT,
    DIALOG_HINT,
    DIALOG_HINT_EMPTY,
    DIALOG_HINT_STALE,
    DIALOG_TITLE,
    LabelChoiceDialog,
    choose_label,
)

CLASSES = ["a0_dian", "a1_xian", "a2_mian"]


@pytest.fixture(scope="module")
def qt_app():
    "Create the offscreen application the dialog tests need."

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


@pytest.fixture
def dialog(qt_app):
    "A dialog of the fixture class table, closed afterwards."

    widget = LabelChoiceDialog(list(CLASSES), CLASSES[1])
    try:
        yield widget
    finally:
        widget.close()


def labels_of(combo) -> list:
    "Return every entry of a combo box."

    return [combo.itemText(index) for index in range(combo.count())]


def test_the_combo_box_can_never_be_typed_into(dialog):
    "The one control of the dialog is a pure choice."

    combo = dialog.combo
    assert combo.isEditable() is False
    # a non editable combo box owns no line edit at all: there is no
    # widget left that could receive a keystroke
    assert combo.lineEdit() is None
    assert combo.findChildren(QtWidgets.QLineEdit) == []
    # and the whole dialog holds no text field either
    assert dialog.findChildren(QtWidgets.QLineEdit) == []


def test_the_entries_are_the_class_table_of_the_run(dialog):
    "Nothing outside the effective class table can be chosen."

    assert labels_of(dialog.combo) == CLASSES
    assert dialog.classes == CLASSES
    # the current label is the one the shape already carries
    assert dialog.chosen_label() == CLASSES[1]
    assert dialog.combo.currentIndex() == 1


def test_the_confirm_and_the_cancel_are_chinese_and_named(dialog):
    "The user reads two Chinese buttons and the dialog says what it does."

    assert dialog.windowTitle() == DIALOG_TITLE
    assert dialog.confirm_button.text() == CONFIRM_TEXT
    assert dialog.cancel_button.text() == CANCEL_TEXT
    texts = [label.text() for label in dialog.findChildren(QtWidgets.QLabel)]
    assert DIALOG_HINT in texts


def test_choosing_a_label_and_confirming_returns_it(dialog):
    "The picked entry travels back through chosen_label."

    dialog.combo.setCurrentIndex(2)
    QtTest.QTest.mouseClick(
        dialog.confirm_button,
        QtCore.Qt.MouseButton.LeftButton,
        QtCore.Qt.KeyboardModifier.NoModifier,
    )
    QtWidgets.QApplication.processEvents()

    assert dialog.result() == QtWidgets.QDialog.DialogCode.Accepted
    assert dialog.chosen_label() == CLASSES[2]


def test_a_label_outside_the_table_is_named_but_never_offered(qt_app):
    "A stale label of an older run is written down, never added as an entry."

    widget = LabelChoiceDialog(list(CLASSES), "old_name")
    try:
        # the entries are the class table and nothing else: the choice
        # can never hand back a name the run cannot export
        assert labels_of(widget.combo) == CLASSES
        assert widget.chosen_label() == CLASSES[0]
        assert widget.combo.currentIndex() == 0
        # the name the shape carries is still read by the user, above
        # the box, with the reason why it is not preselected
        hint = widget.hint_label.text()
        assert DIALOG_HINT in hint
        assert DIALOG_HINT_STALE.format(current="old_name") in hint
        assert widget.combo.isEditable() is False
        assert widget.combo.findChildren(QtWidgets.QLineEdit) == []
    finally:
        widget.close()


def test_a_stale_label_is_never_added_whatever_it_is_called(qt_app):
    "The rule holds for every name outside the table, not for one sample."

    for stale in ("old_name", "", "  ", CLASSES[0] + "_x", "bus"):
        if stale in CLASSES:
            continue
        widget = LabelChoiceDialog(list(CLASSES), stale)
        try:
            assert labels_of(widget.combo) == CLASSES, stale
            assert widget.chosen_label() in CLASSES, stale
        finally:
            widget.close()


def test_the_current_label_of_the_table_is_still_preselected(qt_app):
    "A label the table knows is preselected, and the hint stays the plain one."

    widget = LabelChoiceDialog(list(CLASSES), CLASSES[2])
    try:
        assert widget.combo.currentIndex() == 2
        assert widget.chosen_label() == CLASSES[2]
        texts = [
            label.text() for label in widget.findChildren(QtWidgets.QLabel)
        ]
        assert DIALOG_HINT in texts
        assert not any(text.startswith("当前标签") for text in texts)
    finally:
        widget.close()


def test_an_empty_class_table_refuses_the_confirm(qt_app):
    "A run without a class table says so and closes on cancel alone."

    widget = LabelChoiceDialog([], "")
    try:
        assert labels_of(widget.combo) == []
        assert widget.confirm_button.isEnabled() is False
        assert widget.cancel_button.isEnabled() is True
        texts = [
            label.text() for label in widget.findChildren(QtWidgets.QLabel)
        ]
        assert DIALOG_HINT_EMPTY in texts
        assert DIALOG_HINT not in texts
        assert widget.chosen_label() == ""
    finally:
        widget.close()


def test_choose_label_of_an_empty_table_never_opens_a_dialog(qt_app):
    "The empty table is answered before a modal window is built at all."

    assert choose_label(None, [], "car") is None
    assert choose_label(None, (), "") is None


def test_choose_label_answers_none_when_the_dialog_is_cancelled(
    qt_app, monkeypatch
):
    "A cancel is None, which is what keeps the caller from writing."

    def cancelled(self) -> int:
        return QtWidgets.QDialog.DialogCode.Rejected

    monkeypatch.setattr(LabelChoiceDialog, "exec", cancelled)

    assert choose_label(None, list(CLASSES), CLASSES[0]) is None


def test_choose_label_returns_the_picked_entry(qt_app, monkeypatch):
    "A confirmed choice is the text of the combo box."

    def accepted(self) -> int:
        self.combo.setCurrentIndex(2)
        return QtWidgets.QDialog.DialogCode.Accepted

    monkeypatch.setattr(LabelChoiceDialog, "exec", accepted)

    assert choose_label(None, list(CLASSES), CLASSES[0]) == CLASSES[2]


def test_the_real_modal_loop_hands_back_the_entry_the_user_clicked(qt_app):
    "The whole chain of choose_label: a real exec(), a real click."

    # The two tests above stub the modal loop out, so nothing they do
    # proves that choose_label really reaches a dialog a user can click.
    # This one leaves exec() alone: the dialog is entered for real, a
    # timer callback - it runs inside the event loop of that very modal
    # loop - picks up the window Qt made active and clicks its confirm
    # button.
    seen = []

    def pick() -> None:
        window = QtWidgets.QApplication.activeModalWidget()
        assert isinstance(window, LabelChoiceDialog), window
        seen.append(window)
        window.combo.setCurrentIndex(2)
        assert window.combo.currentText() == CLASSES[2]
        QtTest.QTest.mouseClick(
            window.confirm_button,
            QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.KeyboardModifier.NoModifier,
        )

    QtCore.QTimer.singleShot(0, pick)

    assert choose_label(None, list(CLASSES), CLASSES[0]) == CLASSES[2]
    assert len(seen) == 1, "the modal loop never opened a dialog"
    # the dialog was really entered and really confirmed
    assert seen[0].result() == QtWidgets.QDialog.DialogCode.Accepted
    assert seen[0].chosen_label() == CLASSES[2]
    assert seen[0].combo.isEditable() is False


def test_the_real_modal_loop_answers_none_when_cancel_is_clicked(qt_app):
    "The way out of the real dialog is None, never the preselected label."

    seen = []

    def cancel() -> None:
        window = QtWidgets.QApplication.activeModalWidget()
        assert isinstance(window, LabelChoiceDialog), window
        seen.append(window)
        QtTest.QTest.mouseClick(
            window.cancel_button,
            QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.KeyboardModifier.NoModifier,
        )

    QtCore.QTimer.singleShot(0, cancel)

    # the combo box sits on the current label of the shape, so a cancel
    # that leaked the preselection would hand "a1_xian" back
    assert choose_label(None, list(CLASSES), CLASSES[1]) is None
    assert len(seen) == 1, "the modal loop never opened a dialog"
    assert seen[0].result() == QtWidgets.QDialog.DialogCode.Rejected
