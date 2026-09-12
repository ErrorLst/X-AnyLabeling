"""The label chooser of the ROI edit mode.

Renaming a box is a *choice*, never a typing job: the labels of a run
are the class table the model was configured with, and a name that is
not part of it would produce an export nobody can train with. The one
control of this dialog is therefore a combo box with setEditable(False)
- the very call that closes every free text path of a combo box, the
line edit behind it is never created and typing into the popup filters
nothing. The user picks one of the entries or cancels; there is no third
way in.

The class table of a run may be empty (a label file without classes.txt)
and the dialog says so instead of offering a list of nothing: the
confirm button is refused and the reason is written above the box.

A label the shape carries but the class table does not know is the one
case a preselection cannot honour: the name is written into the line
above the box - so the user reads what is being renamed - and the box
itself stays on the entries of the table, because a name that is not
part of it is exactly what this dialog must never hand back.

The chosen name is only ever read back through choose_label, which is
the single entry point the results page calls - a cancel and an empty
class table both answer None.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from PyQt6 import QtWidgets

DIALOG_TITLE = "重命名标签"
DIALOG_LABEL = "标签"
DIALOG_HINT = "标签只能从分类表中选择，不能自行输入。"
DIALOG_HINT_EMPTY = "分类表为空：本次运行没有可选的标签，无法重命名。"
DIALOG_HINT_STALE = "当前标签：{current}（不在分类表中，请从下表选择）"
CONFIRM_TEXT = "确定"
CANCEL_TEXT = "取消"


class LabelChoiceDialog(QtWidgets.QDialog):
    """Pick one label out of the class table of the run.

    The widget never invents a label: the combo box is not editable and
    holds exactly the entries it was handed, so its current text is
    always one of them and chosen_label() can never answer a name that
    is not part of the class table of the run. A label the shape
    already carries but the table does not know - an older run, a hand
    edited json - is *said* above the box and is never added as an
    entry: picking such a name would produce an export nobody can train
    with, which is the one thing this dialog exists to prevent. The
    confirm button is refused while the table is empty, and the dialog
    answers that very table through chosen_label().
    """

    def __init__(
        self,
        classes: Sequence[str],
        current: str = "",
        parent: Optional[Any] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr(DIALOG_TITLE))
        self.classes = [str(name) for name in classes]
        layout = QtWidgets.QVBoxLayout(self)

        hint = self.tr(DIALOG_HINT if self.classes else DIALOG_HINT_EMPTY)
        self.hint_label = QtWidgets.QLabel(hint)
        self.hint_label.setWordWrap(True)
        layout.addWidget(self.hint_label)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel(self.tr(DIALOG_LABEL)))
        self.combo = QtWidgets.QComboBox()
        # the whole point of this dialog: a combo box that cannot be
        # typed into. The call removes the line edit itself, so no key
        # stroke can ever reach it.
        self.combo.setEditable(False)
        self.combo.addItems(self.classes)
        self.combo.setToolTip(self.tr(DIALOG_HINT))
        if self.classes:
            self.set_current_label(current)
        row.addWidget(self.combo, 1)
        layout.addLayout(row)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        self.button_box = buttons
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText(
            self.tr(CONFIRM_TEXT)
        )
        buttons.button(
            QtWidgets.QDialogButtonBox.StandardButton.Cancel
        ).setText(self.tr(CANCEL_TEXT))
        self.confirm_button = buttons.button(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
        )
        self.cancel_button = buttons.button(
            QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        # an empty table has nothing to confirm: the refusal is written
        # into the button instead of being answered after the click
        self.confirm_button.setEnabled(bool(self.classes))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def set_current_label(self, label: str) -> None:
        """Preselect one label of the table, the first one otherwise.

        A label outside the table is preselected by nobody: the box
        stays on its first entry and the line above it names the label
        the shape carries and says that it is not part of the table, so
        the user still reads what is being renamed while the control
        keeps offering the entries of the table and nothing else.
        """

        text = str(label or "")
        index = self.combo.findText(text)
        if text and index < 0:
            # the rule of the dialog stays readable, the name the shape
            # carries is said right under it
            self.hint_label.setText(
                self.tr(DIALOG_HINT)
                + "\n"
                + self.tr(DIALOG_HINT_STALE).format(current=text)
            )
            index = -1
        self.combo.setCurrentIndex(max(index, 0))

    def chosen_label(self) -> str:
        """Return the label the user picked."""

        return str(self.combo.currentText())


def choose_label(
    parent: Optional[Any],
    classes: Sequence[str],
    current: str = "",
) -> Optional[str]:
    """Ask for one label of the class table, None when nothing was chosen.

    The empty table is answered before a dialog is built at all: a run
    without a class table has no label to offer and a modal window
    saying so would cost one click for nothing. A cancel answers None as
    well, which is what keeps the caller from writing the label that was
    already there.
    """

    names = [str(name) for name in classes]
    if not names:
        return None
    dialog = LabelChoiceDialog(names, current, parent)
    try:
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return None
        return dialog.chosen_label()
    finally:
        dialog.deleteLater()


__all__ = [
    "CANCEL_TEXT",
    "CONFIRM_TEXT",
    "DIALOG_HINT",
    "DIALOG_HINT_EMPTY",
    "DIALOG_HINT_STALE",
    "DIALOG_LABEL",
    "DIALOG_TITLE",
    "LabelChoiceDialog",
    "choose_label",
]
