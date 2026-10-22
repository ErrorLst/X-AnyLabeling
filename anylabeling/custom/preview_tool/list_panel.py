"""The directory tree beside the image view of the preview tool.

The tree shows the scanned input folder: one root node on top, one
child node per subdirectory that still holds a visible image and one
leaf per image. A directory node can be expanded and folded and it
never selects an image; only a leaf does. A picked image carries the
marker of PICK_MARK in front of its name, so the row marker and the
toggle button of the toolbar always tell the same story. The tree only
accepts a dropped directory: a single image or any other file is
refused, because a drop always opens a folder.

The flat row order of the leaves is the visual order of the tree: one
directory contributes its files first and its subdirectories after
them, both in natural order, depth first. The dialog navigates with
this flat order, so the arrow keys, Home/End and the automatic jump
after a copy or a removal walk exactly the order the tree shows.
"""

from __future__ import annotations

import os
from typing import Dict, Iterable, List

from PyQt6 import QtCore, QtWidgets

from . import pick_core
from .viewer import first_directory

__all__ = ["PICK_MARK", "PreviewFileTree"]

#: Prefix of a row whose image already exists in the output folder.
PICK_MARK = "✓  "

MIN_WIDTH = 200

#: Item data role of the flat row of a file leaf.
FILE_ROLE = int(QtCore.Qt.ItemDataRole.UserRole)

#: Item data role of the relative directory of a directory node.
DIR_ROLE = FILE_ROLE + 1


def _normalize_rel_dir(value) -> str:
    """Return the normalized relative directory of a value."""

    try:
        return pick_core.normalize_rel_dir(value)
    except Exception:
        return ""


def _as_key(value):
    """Return a (rel_dir, stem) key of a picked value, None when empty."""

    try:
        key = pick_core.entry_key(value)
    except Exception:
        return None
    if not key or not key[1]:
        return None
    return key


class PreviewFileTree(QtWidgets.QTreeWidget):
    """One leaf per visible image, one node per directory, a root on top.

    Signals:
        selection_changed(int) - the flat row of the leaf the user
            selected; a directory node emits nothing at all, so
            selecting one never changes the image on screen.
        directory_dropped(str) - a directory was dropped on the tree.
    """

    selection_changed = QtCore.pyqtSignal(int)
    directory_dropped = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        """Build an empty tree with its root node."""

        super().__init__(parent)
        self._entries: List = []
        self._items: List[QtWidgets.QTreeWidgetItem] = []
        self._dir_items: Dict[str, QtWidgets.QTreeWidgetItem] = {}
        self._picked = frozenset()
        self._collapsed = set()
        self._root_path = ""
        self._ignore_drops = False
        self._root_item = QtWidgets.QTreeWidgetItem(self)
        self.setMinimumWidth(MIN_WIDTH)
        self.setHeaderHidden(True)
        self.setUniformRowHeights(True)
        self.setRootIsDecorated(True)
        self.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.SingleSelection
        )
        self.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.setDragDropMode(
            QtWidgets.QAbstractItemView.DragDropMode.DropOnly
        )
        self.setDefaultDropAction(QtCore.Qt.DropAction.CopyAction)
        self.setAcceptDrops(True)
        self.currentItemChanged.connect(self._on_current_changed)
        self.itemCollapsed.connect(self._on_item_collapsed)
        self.itemExpanded.connect(self._on_item_expanded)

    # ------------------------------------------------------------ build

    def set_entries(self, entries: Iterable, root="") -> None:
        """Rebuild the whole tree from the visible entries.

        The entries are the already filtered core.ImageEntry list in
        the flat scan order; the parents of every entry are rebuilt from
        its rel_dir, so a directory without a visible image disappears
        and the chain above it stays. The folded state of the directory
        nodes is kept across a rebuild, every new node starts expanded.
        The tree is rebuilt without emitting selection_changed.
        """

        self._entries = list(entries or ())
        if root is not None:
            self._root_path = str(root or "")
        collapsed = set(self._collapsed)
        blocked = self.blockSignals(True)
        try:
            self.clear()
            self._items = []
            self._dir_items = {}
            self._root_item = QtWidgets.QTreeWidgetItem(self)
            self._root_item.setText(0, self._root_text())
            self._root_item.setToolTip(0, self._root_path)
            self._root_item.setData(0, DIR_ROLE, "")
            for index, entry in enumerate(self._entries):
                item = self._make_file_item(entry, index)
                if item is not None:
                    self._items.append(item)
            for rel_dir, item in self._dir_items.items():
                item.setExpanded(rel_dir not in collapsed)
            self._root_item.setExpanded("" not in collapsed)
            self._refresh_texts()
            self.setCurrentItem(None)
        finally:
            self.blockSignals(blocked)

    def set_root(self, directory) -> None:
        """Show one root directory, keeping the rows in place."""

        self._root_path = str(directory or "")
        try:
            self._root_item.setText(0, self._root_text())
            self._root_item.setToolTip(0, self._root_path)
        except RuntimeError:
            return

    def _root_text(self) -> str:
        """Return the label of the root node."""

        path = self._root_path.rstrip("/" + os.sep)
        name = os.path.basename(path)
        return name or self._root_path

    def _make_file_item(self, entry, index: int):
        """Create the leaf of one entry and return it."""

        rel_dir = _normalize_rel_dir(getattr(entry, "rel_dir", ""))
        parent = self._dir_item(rel_dir)
        if parent is None:
            return None
        item = QtWidgets.QTreeWidgetItem(parent)
        item.setData(0, FILE_ROLE, int(index))
        item.setToolTip(0, str(getattr(entry, "path", "") or ""))
        return item

    def _dir_item(self, rel_dir: str):
        """Return the node of one relative directory, creating parents."""

        rel = _normalize_rel_dir(rel_dir)
        if not rel:
            return self._root_item
        item = self._dir_items.get(rel)
        if item is not None:
            return item
        parent = self._dir_item(os.path.dirname(rel))
        if parent is None:
            return None
        item = QtWidgets.QTreeWidgetItem(parent)
        item.setText(0, os.path.basename(rel))
        item.setData(0, DIR_ROLE, rel)
        if self._root_path:
            item.setToolTip(0, os.path.join(self._root_path, rel))
        else:
            item.setToolTip(0, rel)
        self._dir_items[rel] = item
        return item

    # -------------------------------------------------------------- api

    def entries(self) -> List:
        """Return the entries of the leaves, in flat order."""

        return list(self._entries)

    def row_count(self) -> int:
        """Return the number of leaves."""

        return len(self._entries)

    def entry_at(self, row: int):
        """Return the entry of one flat row, None when out of range."""

        try:
            row = int(row)
        except (TypeError, ValueError):
            return None
        if 0 <= row < len(self._entries):
            return self._entries[row]
        return None

    def name_at(self, row: int) -> str:
        """Return the file name of one flat row, '' when out of range."""

        entry = self.entry_at(row)
        if entry is None:
            return ""
        return str(getattr(entry, "name", "") or "")

    def row_text(self, row: int) -> str:
        """Return the text of one flat row, '' when out of range."""

        item = self.file_item(row)
        if item is None:
            return ""
        return item.text(0)

    def file_item(self, row: int):
        """Return the leaf item of one flat row, None when out of range."""

        try:
            row = int(row)
        except (TypeError, ValueError):
            return None
        if 0 <= row < len(self._items):
            return self._items[row]
        return None

    def directory_item(self, rel_dir):
        """Return the node of one relative directory, None when absent."""

        rel = _normalize_rel_dir(rel_dir)
        if not rel:
            return self._root_item
        return self._dir_items.get(rel)

    def root_item(self):
        """Return the root node of the tree."""

        return self._root_item

    def root_text(self) -> str:
        """Return the label of the root node."""

        try:
            return self._root_item.text(0)
        except RuntimeError:
            return ""

    def set_picked(self, picked: Iterable) -> None:
        """Mark the leaves whose (rel_dir, stem) key is picked."""

        keys = set()
        for value in picked or ():
            key = _as_key(value)
            if key is not None:
                keys.add(key)
        self._picked = frozenset(keys)
        self._refresh_texts()

    def picked(self) -> frozenset:
        """Return the marked (rel_dir, stem) keys."""

        return self._picked

    def set_current_row(self, row: int) -> None:
        """Select one leaf without emitting selection_changed."""

        try:
            row = int(row)
        except (TypeError, ValueError):
            return
        blocked = self.blockSignals(True)
        try:
            if 0 <= row < len(self._items):
                item = self._items[row]
                self.setCurrentItem(item)
                self.scrollToItem(item)
            else:
                self.setCurrentItem(None)
        finally:
            self.blockSignals(blocked)

    def current_row(self) -> int:
        """Return the flat row of the selected leaf, -1 otherwise."""

        item = self.currentItem()
        if item is None:
            return -1
        index = item.data(0, FILE_ROLE)
        if index is None:
            return -1
        try:
            return int(index)
        except (TypeError, ValueError):
            return -1

    def ignore_drops(self, flag: bool) -> None:
        """Refuse every drop while flag is true."""

        self._ignore_drops = bool(flag)

    # ----------------------------------------------------------- private

    def _refresh_texts(self) -> None:
        """Write the picked marker on every leaf."""

        for index, item in enumerate(self._items):
            if item is None:
                continue
            try:
                item.setText(0, self._file_text(index))
            except RuntimeError:
                return

    def _file_text(self, index: int) -> str:
        """Return the text of one leaf, with its picked marker."""

        entry = self._entries[index] if index < len(self._entries) else None
        name = str(getattr(entry, "name", "") or "")
        if not name:
            path = str(getattr(entry, "path", "") or "")
            name = os.path.basename(path)
        key = _as_key(entry)
        if key is not None and key in self._picked:
            return PICK_MARK + name
        return name

    def _on_current_changed(self, current, _previous) -> None:
        """Forward the flat row of a selected leaf to the dialog."""

        if current is None:
            return
        index = current.data(0, FILE_ROLE)
        if index is None:
            return
        try:
            self.selection_changed.emit(int(index))
        except (TypeError, ValueError):
            return

    def _on_item_collapsed(self, item) -> None:
        """Remember one folded directory across a rebuild."""

        rel = item.data(0, DIR_ROLE)
        if rel is not None:
            self._collapsed.add(str(rel))

    def _on_item_expanded(self, item) -> None:
        """Forget the folded state of an opened directory."""

        rel = item.data(0, DIR_ROLE)
        if rel is not None:
            self._collapsed.discard(str(rel))

    # ----------------------------------------------------------- events

    def dragEnterEvent(self, event) -> None:
        """Accept a dropped directory, refuse anything else."""

        if self._ignore_drops or not first_directory(event.mimeData()):
            event.ignore()
            return
        event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:
        """Keep the drag alive over the tree."""

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
