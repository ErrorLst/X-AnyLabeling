"""History page of the model validation sub window.

Every run stages its pictures into a fresh folder of the system
temporary directory, and those folders outlive the window that wrote
them: a closed run, a restarted machine or a second window opened on
the same temp folder all leave an ``xal_validation_*`` directory
behind. This page is the one place that lists them and hands one of
them back - a double click on a restorable row, or the "恢复选中"
button, asks the window to restore that run on the results page.

The page scans nothing. The walk of the temporary folder belongs to
the window that holds this page (see dialog); the window passes a list
of run summaries in and answers the two requests of the page, the
refresh and the restore. Only the attributes of those summaries are
read here (a duck typed RunSummary is enough) and the module that
produced them is never imported, so the listing and the page can be
revised independently.

Every row is read only and no command touches a run: a history entry
is shown, opened or restored, and the one irreversible operation of
the folder - removing a run - is deliberately not offered. The columns
are what a user needs to pick the right entry: when the folder was
written, which dataset it came from, how many originals and augmented
copies it holds, the verdicts of its state file and how many records
the user marked.

The room the page asks for stays under the floor the window sets for
every page: the tree is the only growing widget, so the minimum height
hint of the page stays far below the height of the results page and
never pushes the window (see dialog.stack_minimum_size).
"""

from __future__ import annotations

import datetime
import tempfile
from typing import Any, Iterable, List, Optional

from PyQt6 import QtCore, QtGui, QtWidgets

from .. import records as records_module

COLUMN_TIME = 0
COLUMN_SOURCE = 1
COLUMN_ORIGINALS = 2
COLUMN_AUGMENTED = 3
COLUMN_VERDICT = 4
COLUMN_MARKED = 5
COLUMN_STATE = 6

HEADERS = ("时间", "数据来源", "原图", "增强", "判定", "标记", "状态")

# The run summary of a row travels in the item data, so selected_run()
# answers the very object the window handed in and the page keeps no
# second index of its own listing.
RUN_ROLE = QtCore.Qt.ItemDataRole.UserRole + 1

TITLE_TEMPLATE = "历史记录（临时目录：{tempdir}）"
# U+2014 em dash, the one mark of "nothing here".
EMPTY_TEXT = "—"
NO_SELECTION_NOTE = "未选择任何运行目录"
OPEN_FAILED_NOTE = "无法打开目录：{path}"
SCANNING_NOTE = "正在扫描临时目录…"
TRUNCATED_NOTE = "已显示前 {count} 条（历史较多被截断）"

STATE_CURRENT = "当前"
STATE_RESTORABLE = "可恢复"
STATE_UNRESTORABLE = "不可恢复"
STATE_NO_JUDGEMENT = "无判定数据"

# The frozen reason enum of a run summary and the status line each value
# gets. A value of a later revision is shown as the plain "不可恢复"
# instead of an empty cell, so an unknown reason still says something.
REASON_STATES = {
    "missing": "临时文件已失效",
    "missing_meta": "缺少 meta.json",
    "bad_meta": "meta 损坏",
    "no_labels": "没有已拷贝的标签",
}

# The one floor of this page: a comfortable list, and far under the
# height the window keeps for every page (see dialog.MINIMUM_HEIGHT).
MINIMUM_HEIGHT = 320


def title_text() -> str:
    """Return the title line naming the temp folder that is listed."""

    return TITLE_TEMPLATE.format(tempdir=tempfile.gettempdir())


def run_root(run: Any) -> str:
    """Return the staging root one run summary points at."""

    if run is None:
        return ""
    return str(getattr(run, "staging_root", "") or "")


def run_restorable(run: Any) -> bool:
    """Return True when the window can restore one run summary."""

    if run is None:
        return False
    return bool(getattr(run, "restorable", False))


def _int_text(value: Any) -> str:
    """Return a count as text, "0" for anything that is no count."""

    try:
        return str(int(value))
    except (TypeError, ValueError):
        return "0"


def format_time(mtime: Any) -> str:
    """Return the minute a run folder was written, as local time.

    A missing or unreadable timestamp is shown as the em dash of "no
    value": the row stays listed, because a folder whose mtime cannot
    be read is still a folder the user may want to restore.
    """

    try:
        moment = datetime.datetime.fromtimestamp(float(mtime))
    except (TypeError, ValueError, OSError, OverflowError):
        return EMPTY_TEXT
    return moment.strftime("%Y-%m-%d %H:%M")


def verdict_text(run: Any) -> str:
    """Return the verdict counters of one run, in the frozen order.

    The counts are spelled in the order of records.VERDICT_ORDER, the
    order the records layer itself reads them in, and a verdict the
    state file does not mention is left out. A run without a readable
    state file - and one whose state file counted nothing - is answered
    with 无判定数据, the same line the status column uses.
    """

    if not bool(getattr(run, "state_readable", False)):
        return STATE_NO_JUDGEMENT
    try:
        counts = dict(getattr(run, "verdict_counts", None) or {})
    except (TypeError, ValueError):
        counts = {}
    parts = []
    for verdict in records_module.VERDICT_ORDER:
        try:
            count = int(counts.get(verdict) or 0)
        except (TypeError, ValueError):
            continue
        if count > 0:
            parts.append("{name} {count}".format(name=verdict, count=count))
    if not parts:
        return STATE_NO_JUDGEMENT
    return " / ".join(parts)


def marked_text(run: Any) -> str:
    """Return the amount of marked records, an em dash when none is."""

    try:
        marked = int(getattr(run, "marked", 0) or 0)
    except (TypeError, ValueError):
        marked = 0
    return str(marked) if marked > 0 else EMPTY_TEXT


def state_text(run: Any, current_root: str = "") -> str:
    """Return the status column of one run, the one derivation rule.

    The cases are read in a fixed priority: the run that is on screen
    right now wins over everything, then the reason a run cannot be
    restored, then the run that is fine but carries no judgement yet,
    and the plain restorable row last. The reason enum is frozen (see
    the task contract): an unknown value falls back to 不可恢复 instead
    of leaving the cell empty.
    """

    root = run_root(run)
    if current_root and root == str(current_root):
        return STATE_CURRENT
    if not run_restorable(run):
        reason = str(getattr(run, "reason", "") or "")
        return REASON_STATES.get(reason, STATE_UNRESTORABLE)
    if not bool(getattr(run, "state_readable", False)):
        return STATE_NO_JUDGEMENT
    return STATE_RESTORABLE


def open_local_path(path: str) -> bool:
    """Ask the desktop to open one folder, False when it refused."""

    return bool(
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path)))
    )


class HistoryPage(QtWidgets.QWidget):
    """List the runs left in the temp folder and ask for one of them."""

    # the staging root to restore; "" is never emitted
    restore_requested = QtCore.pyqtSignal(str)
    refresh_requested = QtCore.pyqtSignal()
    close_requested = QtCore.pyqtSignal()

    def __init__(self, parent: Optional[Any] = None) -> None:
        super().__init__(parent)
        self.runs: List[Any] = []
        self.current_root = ""
        # True while the window walks the temp folder: the list on
        # screen is then known to be incomplete, so no restore may
        # start from it (see set_busy).
        self._busy = False
        self._build_ui()

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        """Create every widget of the page."""

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self.title_label = QtWidgets.QLabel(self.tr(title_text()))
        layout.addWidget(self.title_label)

        toolbar = QtWidgets.QHBoxLayout()
        self.refresh_button = QtWidgets.QPushButton(self.tr("刷新"))
        self.refresh_button.setToolTip(
            self.tr("重新扫描系统临时目录里的运行目录。")
        )
        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        self.open_button = QtWidgets.QPushButton(self.tr("打开所选目录"))
        self.open_button.setToolTip(
            self.tr("用文件管理器打开所选运行的目录。")
        )
        self.open_button.clicked.connect(self.open_selected)
        self.restore_button = QtWidgets.QPushButton(self.tr("恢复选中"))
        self.restore_button.setToolTip(
            self.tr("把所选运行恢复到结果页；不可恢复的记录不能选中。")
        )
        self.restore_button.clicked.connect(self.restore_selected)
        self.close_button = QtWidgets.QPushButton(self.tr("关闭"))
        self.close_button.clicked.connect(self.close_requested.emit)
        toolbar.addWidget(self.refresh_button)
        toolbar.addWidget(self.open_button)
        toolbar.addWidget(self.restore_button)
        toolbar.addStretch(1)
        toolbar.addWidget(self.close_button)
        layout.addLayout(toolbar)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setColumnCount(len(HEADERS))
        self.tree.setHeaderLabels([self.tr(header) for header in HEADERS])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setItemsExpandable(False)
        # The list is a read only report of the temp folder: no cell may
        # be typed over and no header click may reorder it.
        self.tree.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.tree.setSortingEnabled(False)
        self.tree.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.tree.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.SingleSelection
        )
        header = self.tree.header()
        header.setSectionsClickable(False)
        header.setSortIndicatorShown(False)
        header.setStretchLastSection(False)
        header.setSectionResizeMode(
            COLUMN_SOURCE, QtWidgets.QHeaderView.ResizeMode.Stretch
        )
        for column in (
            COLUMN_TIME,
            COLUMN_ORIGINALS,
            COLUMN_AUGMENTED,
            COLUMN_VERDICT,
            COLUMN_MARKED,
            COLUMN_STATE,
        ):
            header.setSectionResizeMode(
                column, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
            )
        self.tree.setToolTip(
            self.tr("双击一条可恢复的记录即把该次运行恢复到结果页。")
        )
        self.tree.setContextMenuPolicy(
            QtCore.Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.tree.customContextMenuRequested.connect(self._show_context_menu)
        self.tree.itemActivated.connect(self._on_item_activated)
        self.tree.itemSelectionChanged.connect(self._sync_buttons)
        layout.addWidget(self.tree, 1)

        self.status_label = QtWidgets.QLabel("")
        layout.addWidget(self.status_label)

        self.setMinimumHeight(MINIMUM_HEIGHT)
        self._sync_buttons()

    # ------------------------------------------------------------------
    # the listing
    # ------------------------------------------------------------------
    def show_runs(
        self,
        runs: Iterable[Any],
        *,
        current_root: str = "",
        scanning: bool = False,
    ) -> None:
        """Fill the list with the run summaries the window scanned.

        The order is the one handed in (the window sorts by mtime,
        newest first), the list is cleared first and no summary is read
        again here. While scanning is True the status line says that
        the walk of the temporary folder is still running, so the list
        on screen is known to be incomplete.

        A truncated listing is announced with one extra row at the end
        of the list (the alternative of a status line was deliberately
        not taken): that row carries no run at all. It is the non
        restorable note line of the listing, its status column spells
        the truncation text of TRUNCATED_NOTE, no other column carries
        a value of a run, selected_run() answers None for it and
        neither a double click nor the restore button can act on it, so
        the note can never be mistaken for a run that is merely broken.
        """

        self.runs = list(runs or [])
        self.current_root = str(current_root or "")
        self.tree.clear()
        for run in self.runs:
            self._append_run(run)
        if any(bool(getattr(run, "truncated", False)) for run in self.runs):
            self._append_truncated(len(self.runs))
        self.set_note(self.tr(SCANNING_NOTE) if scanning else "")
        self._sync_buttons()

    def _append_run(self, run: Any) -> QtWidgets.QTreeWidgetItem:
        """Append the row that shows one run summary."""

        item = QtWidgets.QTreeWidgetItem(self.tree)
        item.setText(COLUMN_TIME, format_time(getattr(run, "mtime", 0)))
        source = str(getattr(run, "source_display", "") or "")
        item.setText(COLUMN_SOURCE, source or EMPTY_TEXT)
        item.setToolTip(COLUMN_SOURCE, source)
        item.setText(
            COLUMN_ORIGINALS, _int_text(getattr(run, "staged_originals", 0))
        )
        item.setText(
            COLUMN_AUGMENTED, _int_text(getattr(run, "augmented_count", 0))
        )
        item.setText(COLUMN_VERDICT, self.tr(verdict_text(run)))
        item.setText(COLUMN_MARKED, marked_text(run))
        item.setText(COLUMN_STATE, self.tr(state_text(run, self.current_root)))
        item.setData(COLUMN_TIME, RUN_ROLE, run)
        # a row is a report, never an input field: the item flags keep
        # the cells read only whatever the widget triggers say
        item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
        return item

    def _append_truncated(self, shown: int) -> QtWidgets.QTreeWidgetItem:
        """Append the non restorable line of a truncated listing."""

        item = QtWidgets.QTreeWidgetItem(self.tree)
        note = self.tr(TRUNCATED_NOTE).format(count=max(int(shown), 0))
        item.setText(COLUMN_VERDICT, EMPTY_TEXT)
        item.setText(COLUMN_STATE, note)
        item.setToolTip(COLUMN_STATE, note)
        item.setData(COLUMN_TIME, RUN_ROLE, None)
        item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
        return item

    # ------------------------------------------------------------------
    # the selection and the two commands that read it
    # ------------------------------------------------------------------
    def selected_run(self) -> Optional[Any]:
        """Return the run of the selected row, None without a selection.

        The extra row of a truncated listing carries no run, so a
        selected note answers None exactly like an empty list does.
        """

        return self._run_of_item(self.tree.currentItem())

    def _run_of_item(
        self, item: Optional[QtWidgets.QTreeWidgetItem]
    ) -> Optional[Any]:
        """Return the run summary a row carries, None for the note row."""

        if item is None:
            return None
        data = item.data(COLUMN_TIME, RUN_ROLE)
        return data if data is not None else None

    def restore_selected(self) -> bool:
        """Ask the window to restore the selected run, True once asked."""

        return self.restore_run(self.selected_run())

    def restore_run(self, run: Any) -> bool:
        """Ask the window to restore one run summary.

        A missing, non restorable or empty summary is answered with
        False and emits nothing, so a double click on a broken row never
        turns into a request. While the page is busy (set_busy) every
        request is refused as well: the scan that is about to replace
        the list must not race a restore started from the old one.
        """

        if self._busy or not run_restorable(run):
            return False
        root = run_root(run)
        if not root:
            return False
        self.restore_requested.emit(root)
        return True

    def open_selected(self) -> bool:
        """Open the folder of the selected run in the file manager."""

        return self.open_root(run_root(self.selected_run()))

    def open_root(self, root: str) -> bool:
        """Open one staging folder, False and a status line otherwise.

        The desktop is what opens the folder here as well; a refusal of
        the desktop is written on the status line instead of being
        swallowed, because the user clicked and nothing happened.
        """

        if not root:
            self.set_note(self.tr(NO_SELECTION_NOTE))
            return False
        if not open_local_path(root):
            self.set_note(self.tr(OPEN_FAILED_NOTE).format(path=root))
            return False
        return True

    def copy_selected(self) -> bool:
        """Copy the path of the selected run to the clipboard."""

        root = run_root(self.selected_run())
        if not root:
            self.set_note(self.tr(NO_SELECTION_NOTE))
            return False
        clipboard = QtGui.QGuiApplication.clipboard()
        if clipboard is None:
            return False
        clipboard.setText(root)
        return True

    # ------------------------------------------------------------------
    # the two small pieces of state the window drives
    # ------------------------------------------------------------------
    def set_note(self, text: str) -> None:
        """Write the bottom status line of the page."""

        self.status_label.setText(str(text or ""))

    def set_busy(self, busy: bool) -> None:
        """Disable the restore control while the folder is scanned."""

        self._busy = bool(busy)
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        """Keep the two selection controls in step with the selection."""

        run = self.selected_run()
        selected = run is not None
        self.open_button.setEnabled(selected)
        self.restore_button.setEnabled(
            selected and not self._busy and run_restorable(run)
        )

    # ------------------------------------------------------------------
    # the events of the tree
    # ------------------------------------------------------------------
    def _on_item_activated(
        self, item: QtWidgets.QTreeWidgetItem, _column: int = 0
    ) -> None:
        """Restore the run of an activated row; the note row does nothing.

        Qt raises itemActivated for a double click and for the Return key
        of the current row. A busy page and a row without a restorable
        run are both answered by doing nothing at all: the page never
        restores a folder it cannot vouch for.
        """

        self.restore_run(self._run_of_item(item))

    def _show_context_menu(self, position: QtCore.QPoint) -> None:
        """Offer the two read only commands of a row.

        There is deliberately no delete and no hide entry: this page
        lists what the temp folder holds and never changes it.
        """

        menu = QtWidgets.QMenu(self)
        menu.addAction(self.tr("打开所在目录"), self.open_selected)
        menu.addAction(self.tr("复制路径"), self.copy_selected)
        menu.exec(self.tree.viewport().mapToGlobal(position))


__all__ = [
    "COLUMN_AUGMENTED",
    "COLUMN_MARKED",
    "COLUMN_ORIGINALS",
    "COLUMN_SOURCE",
    "COLUMN_STATE",
    "COLUMN_TIME",
    "COLUMN_VERDICT",
    "EMPTY_TEXT",
    "HEADERS",
    "HistoryPage",
    "MINIMUM_HEIGHT",
    "NO_SELECTION_NOTE",
    "OPEN_FAILED_NOTE",
    "REASON_STATES",
    "RUN_ROLE",
    "SCANNING_NOTE",
    "STATE_CURRENT",
    "STATE_NO_JUDGEMENT",
    "STATE_RESTORABLE",
    "STATE_UNRESTORABLE",
    "TITLE_TEMPLATE",
    "TRUNCATED_NOTE",
    "format_time",
    "marked_text",
    "open_local_path",
    "run_restorable",
    "run_root",
    "state_text",
    "title_text",
    "verdict_text",
]
