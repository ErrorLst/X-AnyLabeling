"""The multi select category drop down of the preview tool.

The button lists every label of the directory plus the pseudo category
"背景" of core, which stands for the images that carry no annotation at
all. 背景 is always the first row and it is checked by default; a new
category is checked by default as well, an existing one keeps the state
the user gave it.

The label of the button tells the state at a glance: every category
checked shows 全部 and reports None to the caller, which means "no
category filter at all"; nothing checked shows 无.
"""

from __future__ import annotations

from typing import Dict, FrozenSet, Iterable, List, Optional

from PyQt6 import QtCore, QtWidgets

from .core import BACKGROUND_LABEL

__all__ = ["ALL_TEXT", "NONE_TEXT", "CategoryFilterButton"]

ALL_TEXT = "全部"
NONE_TEXT = "无"
SELECT_ALL_TEXT = "全选"
CLEAR_ALL_TEXT = "清空"
MIN_MENU_WIDTH = 160


class CategoryFilterButton(QtWidgets.QToolButton):
    """A drop down of check boxes, one per category.

    Signals:
        changed() - the user changed the selection.
    """

    changed = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        """Build an empty drop down."""

        super().__init__(parent)
        self._categories: List[str] = []
        self._checked: Dict[str, bool] = {}
        self._boxes: Dict[str, QtWidgets.QCheckBox] = {}
        self._rebuilding = False
        self.setPopupMode(
            QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self._menu = QtWidgets.QMenu(self)
        self._menu.setMinimumWidth(MIN_MENU_WIDTH)
        self.setMenu(self._menu)
        self._rebuild_menu()
        self._update_label()

    # -------------------------------------------------------------- api

    def set_categories(self, names: Iterable[str],
                       checked: Optional[Iterable[str]] = None) -> None:
        """Rebuild the rows; '背景' always comes first.

        Args:
            names: Every category of the directory.
            checked: The categories to check. None keeps the state of
                the categories that are already known and checks a new
                one, which is the default of a fresh directory. A saved
                selection that is empty counts as "no selection yet",
                so the default of a fresh directory wins.
        """

        wanted = [BACKGROUND_LABEL]
        for name in (names or ()):
            text = str(name)
            if text and text not in wanted:
                wanted.append(text)
        if checked is None:
            state = {name: self._checked.get(name, True)
                     for name in wanted}
        else:
            selected = {str(name) for name in checked}
            if not selected:
                state = {name: True for name in wanted}
            else:
                state = {name: name in selected for name in wanted}
        state[BACKGROUND_LABEL] = state.get(BACKGROUND_LABEL, True)
        self._categories = wanted
        self._checked = state
        self._rebuild_menu()
        self._update_label()

    def reset(self) -> None:
        """Forget every category and every check mark."""

        self._categories = []
        self._checked = {}
        self._rebuild_menu()
        self._update_label()

    def categories(self) -> List[str]:
        """Return the categories, '背景' first."""

        return list(self._categories)

    def checked(self) -> Optional[FrozenSet[str]]:
        """Return the checked categories, None when every one is checked.

        None means "no category filter at all", which is also the answer
        for an empty list.
        """

        if not self._categories:
            return None
        selected = frozenset(
            name for name in self._categories
            if self._checked.get(name, True)
        )
        if len(selected) == len(self._categories):
            return None
        return selected

    def is_checked(self, name: str) -> bool:
        """Return True when one category is checked."""

        return bool(self._checked.get(str(name), True))

    def checked_count(self) -> int:
        """Return the number of checked categories."""

        return sum(
            1 for name in self._categories
            if self._checked.get(name, True)
        )

    # ----------------------------------------------------------- private

    def _rebuild_menu(self) -> None:
        """Fill the menu with the default widget of every category."""

        self._rebuilding = True
        try:
            self._menu.clear()
            self._boxes = {}
            select_all = self._menu.addAction(SELECT_ALL_TEXT)
            select_all.triggered.connect(self._select_all)
            clear_all = self._menu.addAction(CLEAR_ALL_TEXT)
            clear_all.triggered.connect(self._clear_all)
            self._menu.addSeparator()
            for name in self._categories:
                box = QtWidgets.QCheckBox(name)
                box.setChecked(self._checked.get(name, True))
                box.setSizePolicy(
                    QtWidgets.QSizePolicy.Policy.Expanding,
                    QtWidgets.QSizePolicy.Policy.Preferred,
                )
                box.toggled.connect(
                    lambda _on, key=name: self._on_box_toggled(key)
                )
                action = QtWidgets.QWidgetAction(self._menu)
                action.setDefaultWidget(box)
                self._menu.addAction(action)
                self._boxes[name] = box
        finally:
            self._rebuilding = False

    def _on_box_toggled(self, name: str) -> None:
        """Remember one check box and tell the dialog."""

        if self._rebuilding:
            return
        box = self._boxes.get(name)
        if box is None:
            return
        self._checked[name] = bool(box.isChecked())
        self._update_label()
        self.changed.emit()

    def _select_all(self) -> None:
        """Check every category."""

        for name in self._categories:
            self._checked[name] = True
        self._rebuild_menu()
        self._update_label()
        self.changed.emit()

    def _clear_all(self) -> None:
        """Uncheck every category."""

        for name in self._categories:
            self._checked[name] = False
        self._rebuild_menu()
        self._update_label()
        self.changed.emit()

    def _update_label(self) -> None:
        """Write 全部, 无 or n/m on the button."""

        total = len(self._categories)
        if total <= 0:
            self.setText(ALL_TEXT)
            return
        checked = self.checked_count()
        if checked == total:
            self.setText(ALL_TEXT)
        elif checked == 0:
            self.setText(NONE_TEXT)
        else:
            self.setText(f"{checked}/{total}")
