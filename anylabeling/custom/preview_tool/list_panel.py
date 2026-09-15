"""The file list beside the image view of the preview tool.

The list has one row per image of the current, already filtered list.
A picked image carries the marker of PICK_MARK in front of its name, so
the row marker and the toggle button of the toolbar always tell the
same story. The list only accepts a dropped directory: a single image
or any other file is refused, because a drop always opens a folder.
"""

from __future__ import annotations

import os
from typing import Iterable, List

from PyQt6 import QtCore, QtWidgets

from .viewer import first_directory

__all__ = ["PICK_MARK", "PreviewFileList"]

#: Prefix of a row whose image already exists inside picked/.
PICK_MARK = "✓  "

MIN_WIDTH = 200


class PreviewFileList(QtWidgets.QListWidget):
    """One row per image, one marker per picked image.

    Signals:
        selection_changed(int) - the row the user picked, -1 when the
            selection was cleared.
        directory_dropped(str) - a directory was dropped on the list.
    """

    selection_changed = QtCore.pyqtSignal(int)
    directory_dropped = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        """Build an empty list."""

        super().__init__(parent)
        self._names: List[str] = []
        self._picked = frozenset()
        self._ignore_drops = False
        self.setMinimumWidth(MIN_WIDTH)
        self.setUniformItemSizes(True)
        self.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.SingleSelection
        )
        self.setDragDropMode(
            QtWidgets.QAbstractItemView.DragDropMode.DropOnly
        )
        self.setDefaultDropAction(QtCore.Qt.DropAction.CopyAction)
        self.setAcceptDrops(True)
        self.currentRowChanged.connect(self._on_row_changed)

    # ------------------------------------------------------------- rows

    def set_rows(self, names: Iterable[str]) -> None:
        """Replace every row, without emitting selection_changed."""

        self._names = [str(name) for name in (names or ())]
        blocked = self.blockSignals(True)
        try:
            self.clear()
            for name in self._names:
                item = QtWidgets.QListWidgetItem(self._row_text(name))
                item.setData(
                    QtCore.Qt.ItemDataRole.UserRole, name
                )
                item.setToolTip(name)
                self.addItem(item)
        finally:
            self.blockSignals(blocked)

    def names(self) -> List[str]:
        """Return the names of the rows, in order."""

        return list(self._names)

    def row_count(self) -> int:
        """Return the number of rows."""

        return len(self._names)

    def name_at(self, row: int) -> str:
        """Return the name of one row, or an empty string."""

        try:
            row = int(row)
        except (TypeError, ValueError):
            return ""
        if 0 <= row < len(self._names):
            return self._names[row]
        return ""

    def row_text(self, row: int) -> str:
        """Return the text of one row, or an empty string."""

        item = self.item(row)
        if item is None:
            return ""
        return item.text()

    def set_picked(self, stems: Iterable[str]) -> None:
        """Mark the rows whose stem is picked, and unmark the others."""

        self._picked = frozenset(
            str(stem) for stem in (stems or ())
        )
        for row, name in enumerate(self._names):
            item = self.item(row)
            if item is not None:
                item.setText(self._row_text(name))

    def picked(self) -> frozenset:
        """Return the marked stems."""

        return self._picked

    def set_current_row(self, row: int) -> None:
        """Select one row without emitting selection_changed."""

        try:
            row = int(row)
        except (TypeError, ValueError):
            return
        blocked = self.blockSignals(True)
        try:
            self.setCurrentRow(row)
        finally:
            self.blockSignals(blocked)

    def current_row(self) -> int:
        """Return the selected row, -1 when nothing is selected."""

        return self.currentRow()

    def ignore_drops(self, flag: bool) -> None:
        """Refuse every drop while flag is true."""

        self._ignore_drops = bool(flag)

    # ----------------------------------------------------------- events

    def _row_text(self, name: str) -> str:
        """Return the text of one row, with its picked marker."""

        stem = os.path.splitext(os.path.basename(str(name)))[0]
        if stem and stem in self._picked:
            return PICK_MARK + str(name)
        return str(name)

    def _on_row_changed(self, row: int) -> None:
        """Forward the row of the list to the dialog."""

        self.selection_changed.emit(int(row))

    def dragEnterEvent(self, event) -> None:
        """Accept a dropped directory, refuse anything else."""

        if self._ignore_drops or not first_directory(event.mimeData()):
            event.ignore()
            return
        event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:
        """Keep the drag alive over the list."""

        if self._ignore_drops or not first_directory(event.mimeData()):
            event.ignore()
            return
        event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        """Open the dropped directory."""

        path = "" if self._ignore_drops else first_directory(
            event.mimeData()
        )
        if not path:
            event.ignore()
            return
        event.acceptProposedAction()
        self.directory_dropped.emit(path)
