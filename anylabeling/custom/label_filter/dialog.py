"""Label filter window: pick the categories the file list should show.

The dialog is a thin reader of the scanner of the controller: it lists
the categories of the open folder - one checkable row per category,
with the number of images behind it - lets the user search inside that
list, and hands the checked names back to the controller. Nothing is
written anywhere and no configuration key is touched, so the window
leaves no trace once it is closed.

Categories are combined with "or", and a category name matches a label
exactly. Selecting no category at all is not a filter that shows
nothing: the window treats it as "clear the filter" and the file list
shows the folder in full again, which is the only outcome the user can
read as a filter that is off.

The scan runs on the main thread while a progress dialog reports it and
QCoreApplication.processEvents keeps the window alive, the way the
other custom windows of this fork do it; there is no worker thread. A
cancelled scan changes nothing either: the window stays open with an
empty list and says so (SCAN_CANCEL_TEXT), so the user can close it.
"""

from __future__ import annotations

from PyQt6 import QtCore, QtWidgets

__all__ = [
    "BUTTON_CLEAR",
    "BUTTON_CONFIRM",
    "BUTTON_INVERT",
    "BUTTON_NONE",
    "BUTTON_SELECT_ALL",
    "HINT_TEXT",
    "LabelFilterDialog",
    "ROWTIP_ROLE",
    "WINDOW_SIZE",
    "WINDOW_TITLE",
]

WINDOW_TITLE = "标签过滤"
WINDOW_SIZE = (560, 620)
PRIMARY_BUTTON_NAME = "primary"

#: Role that keeps the [name, count, checked] row behind an item.
ROWTIP_ROLE = QtCore.Qt.ItemDataRole.UserRole + 1

SEARCH_PLACEHOLDER = "搜索分类…"
HINT_TEXT = "未勾选任何分类 = 不启用过滤（显示全部）"

BUTTON_SELECT_ALL = "全选"
BUTTON_INVERT = "反选"
BUTTON_NONE = "清空"
BUTTON_CONFIRM = "确认过滤"
BUTTON_CLEAR = "清除过滤"

TITLE_TEXT = "分类列表（勾选后只显示命中的图片）："
ROW_FORMAT = "%s（%d 张）"
SEARCH_EMPTY_TEXT = "没有匹配的分类"

IDLE_TEXT = "请先打开一个图片目录"
SCAN_RUNNING = "正在扫描标注分类…"
SCAN_TITLE = "正在扫描标注分类…"
SCAN_DONE_FORMAT = "共 %d 张图片，%d 个分类"
SCAN_EMPTY_TEXT = "当前目录没有图片文件"
SCAN_CANCEL_TEXT = "已取消扫描，未改动过滤"
APPLIED_FORMAT = "已应用标签过滤：%d 个分类"
CLEARED_TEXT = "已清除标签过滤"
NO_SELECTION_TEXT = "标签过滤未生效，文件列表未改动"

#: How often the progress dialog is pumped while the scan runs.
PUMP_EVERY = 64


class LabelFilterDialog(QtWidgets.QDialog):
    """Category picker of one labeling widget.

    The dialog owns its list and the search field; the file list itself
    belongs to the widget, and the controller is the only thing that
    touches it, through apply() and reset(). The scan is done when the
    window opens and again whenever rescan() is called - which is what
    the launcher does for the window it shows a second time - so the
    categories always describe the folder the widget holds now.
    """

    def __init__(self, parent=None, explorer=None):
        """Build the window that works on one labeling widget.

        Args:
            parent: The Qt parent of the window, or None. A real
                QDialog only accepts a QWidget here, so the tests of
                the window hand the widget over separately.
            explorer: The labeling widget the window works on. It
                defaults to the parent, which is the widget in the
                application.
        """

        super().__init__(parent)
        self._explorer = explorer if explorer is not None else parent
        self._rows_all = []
        self._result = None
        self._cancelled = False
        self._updating = False
        self.setWindowTitle(WINDOW_TITLE)
        self.resize(*WINDOW_SIZE)
        self._build_ui()
        self._scan()
        self._refresh(echo=True)
        self._refresh_buttons()

    # ---------------------------------------------------------------- UI

    def _build_ui(self):
        """Build the search field, the category list, the buttons."""

        root = QtWidgets.QVBoxLayout(self)
        root.setSpacing(6)

        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText(SEARCH_PLACEHOLDER)
        self.search.textChanged.connect(self._on_search_changed)
        root.addWidget(self.search)

        self.title = QtWidgets.QLabel(TITLE_TEXT)
        root.addWidget(self.title)

        self.list_widget = QtWidgets.QListWidget()
        self.list_widget.setObjectName("label_filter_list")
        # A click on a checkbox has to reach the row behind the item:
        # the list is rebuilt whenever the search changes, and the rows
        # are what that rebuild reads the check states from.
        self.list_widget.itemChanged.connect(self._on_item_changed)
        root.addWidget(self.list_widget, stretch=1)

        button_row = QtWidgets.QHBoxLayout()
        self.select_all_button = QtWidgets.QPushButton(BUTTON_SELECT_ALL)
        self.select_all_button.clicked.connect(self.select_all)
        button_row.addWidget(self.select_all_button)
        self.invert_button = QtWidgets.QPushButton(BUTTON_INVERT)
        self.invert_button.clicked.connect(self.invert)
        button_row.addWidget(self.invert_button)
        self.clear_button = QtWidgets.QPushButton(BUTTON_NONE)
        self.clear_button.clicked.connect(self.clear_checks)
        button_row.addWidget(self.clear_button)
        button_row.addStretch(1)
        self.confirm_button = QtWidgets.QPushButton(BUTTON_CONFIRM)
        self.confirm_button.setObjectName(PRIMARY_BUTTON_NAME)
        self.confirm_button.setDefault(True)
        self.confirm_button.clicked.connect(self.confirm)
        button_row.addWidget(self.confirm_button)
        self.reset_button = QtWidgets.QPushButton(BUTTON_CLEAR)
        self.reset_button.clicked.connect(self.clear_filter)
        button_row.addWidget(self.reset_button)
        self.cancel_button = QtWidgets.QPushButton("取消")
        self.cancel_button.clicked.connect(self.reject)
        button_row.addWidget(self.cancel_button)
        root.addLayout(button_row)

        self.hint_label = QtWidgets.QLabel(HINT_TEXT)
        root.addWidget(self.hint_label)
        self.status_label = QtWidgets.QLabel()
        self.status_label.setObjectName("label_filter_status")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

    # ------------------------------------------------------------- scan

    def _run_scan(self):
        """Run the scan of the folder, with a cancellable box."""

        progress = QtWidgets.QProgressDialog(
            SCAN_TITLE, "取消", 0, 100, self
        )
        progress.setWindowModality(
            QtCore.Qt.WindowModality.WindowModal
        )
        progress.setMinimumDuration(0)
        progress.setLabelText(SCAN_RUNNING)

        def on_cancel():
            self._cancelled = True

        def on_progress(done, total):
            progress.setMaximum(max(int(total), 1))
            progress.setValue(int(done))
            if done % PUMP_EVERY == 0:
                QtCore.QCoreApplication.processEvents()

        controller = getattr(
            self._explorer, "_label_filter_controller", None
        )
        progress.canceled.connect(on_cancel)
        progress.show()
        QtCore.QCoreApplication.processEvents()
        result = None
        try:
            scan = getattr(controller, "scan", None)
            if not callable(scan):
                scan = getattr(self._explorer, "scan", None)
            if callable(scan):
                result = scan(
                    progress_cb=on_progress,
                    should_stop=lambda: bool(self._cancelled),
                )
        finally:
            progress.close()
        return result

    def _scan(self):
        """Scan the open folder and keep the result for the list.

        The result of a previous scan is dropped first: a scan that is
        cancelled, or that has no folder to read, leaves the window
        without any category - exactly like a window that never
        scanned - so the primary button can never act on the
        categories of a folder the window no longer shows.
        """

        self._result = None
        if not getattr(self._explorer, "last_open_dir", None):
            self._rows_all = []
            self.status_label.setText(IDLE_TEXT)
            return None
        result = self._run_scan()
        if result is None:
            self._rows_all = []
            self.status_label.setText(
                SCAN_CANCEL_TEXT if self._cancelled else SCAN_RUNNING
            )
            return None
        self._result = result
        self._rows_all = [
            [name, int(result.counts.get(name, 0)), False]
            for name in result.categories()
        ]
        if not result.total:
            self.status_label.setText(SCAN_EMPTY_TEXT)
        else:
            self.status_label.setText(
                SCAN_DONE_FORMAT
                % (result.total, len(result.categories()))
            )
        return result

    def rescan(self):
        """Show the categories of the folder the widget holds now.

        The launcher keeps one window per widget for the whole session
        and shows that same instance again, so the rows it built are
        stale as soon as another folder was opened: the scan is redone
        and the whole list - rows, counts, status line and the echo of
        the active filter - is built from the new result. Only this
        method rescans, never the search field of the user, so the
        echo still happens once per scan and a category the user
        unchecked is not checked again by typing.

        A scan the user cancels keeps the contract of _scan(): the
        window shows the empty list and SCAN_CANCEL_TEXT, and no
        filter state is touched. The active filter therefore stays in
        place and cannot be cleared by a confirmation, because the
        primary button is disabled while there is no category to act
        on.
        """

        self._cancelled = False
        self._scan()
        self._refresh(echo=True)
        self._refresh_buttons()

    # ------------------------------------------------------- list state

    def _active_selection(self):
        """Return the names the active filter shows, if any."""

        controller = getattr(
            self._explorer, "_label_filter_controller", None
        )
        if controller is None:
            return None
        return set(controller.selected())

    def _on_search_changed(self, _text):
        """Filter the visible rows when the search field changes."""

        self._refresh()

    def _on_item_changed(self, item):
        """Keep the row of an item in step with its checkbox.

        Qt signals a change both for a click of the user and for the
        programmatic writes of _refresh() and _set_visible_checks();
        the rows already hold the wanted state in those two cases, so
        the rebuild is skipped there.
        """

        if self._updating:
            return
        self._write_checked(
            item, item.checkState() == QtCore.Qt.CheckState.Checked
        )

    def _write_checked(self, item, checked):
        """Store the check state of an item in its row.

        The index of the row is kept on the item, not the row itself:
        Qt hands a stored Python list back as a copy, so a mutation of
        what data() returns would never reach the list of this window.
        """

        index = item.data(ROWTIP_ROLE)
        if not isinstance(index, int):
            return
        if 0 <= index < len(self._rows_all):
            self._rows_all[index][2] = bool(checked)

    def _refresh(self, echo=False):
        """Rebuild the visible rows, keeping the check states.

        The rows keep the check state the user gave them, which is what
        makes the rebuild a search triggers harmless. Only the build
        that follows a scan - the one where echo is set - copies the
        active filter into the rows, so a window that is opened, or
        shown again, while a filter runs shows what the file list
        shows now. A later build never re-checks a category the user
        just unchecked: the
        state of the hidden rows is kept as well, and an invisible row
        simply comes back with the check it had.
        """

        selected = self._active_selection() if echo else None
        needle = self.search.text().strip().lower()
        self._updating = True
        self.list_widget.clear()
        visible = 0
        for index, row in enumerate(self._rows_all):
            name, count, checked = row
            if needle and needle not in name.lower():
                continue
            if selected is not None and not checked:
                checked = name in selected
                row[2] = checked
            item = QtWidgets.QListWidgetItem(ROW_FORMAT % (name, count))
            item.setData(QtCore.Qt.ItemDataRole.UserRole, name)
            item.setFlags(
                item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable
            )
            item.setCheckState(
                QtCore.Qt.CheckState.Checked
                if checked
                else QtCore.Qt.CheckState.Unchecked
            )
            item.setData(ROWTIP_ROLE, index)
            self.list_widget.addItem(item)
            visible += 1
        self._updating = False
        if not visible and needle and self._rows_all:
            self.status_label.setText(SEARCH_EMPTY_TEXT)
        self._refresh_buttons()

    def rows(self):
        """Return [(name, count, checked)] of every category."""

        return [(row[0], row[1], row[2]) for row in self._rows_all]

    def visible_rows(self):
        """Return [(name, count)] of the rows the list shows."""

        result = []
        for index in range(self.list_widget.count()):
            item = self.list_widget.item(index)
            row = self._rows_all[item.data(ROWTIP_ROLE)]
            result.append(
                (item.data(QtCore.Qt.ItemDataRole.UserRole), row[1])
            )
        return result

    def checked_names(self):
        """Return the names of the checked rows of the list."""

        names = []
        for index in range(self.list_widget.count()):
            item = self.list_widget.item(index)
            if item.checkState() == QtCore.Qt.CheckState.Checked:
                names.append(item.data(QtCore.Qt.ItemDataRole.UserRole))
        return names

    def _set_visible_checks(self, checked=True, invert=False):
        """Check or uncheck every visible row, invert optionally."""

        self._updating = True
        for index in range(self.list_widget.count()):
            item = self.list_widget.item(index)
            if invert:
                state = item.checkState() == QtCore.Qt.CheckState.Checked
                wanted = not state
            else:
                wanted = checked
            item.setCheckState(
                QtCore.Qt.CheckState.Checked
                if wanted
                else QtCore.Qt.CheckState.Unchecked
            )
            self._write_checked(item, wanted)
        self._updating = False
        self._refresh_buttons()

    def select_all(self):
        """Check every visible row."""

        self._set_visible_checks(True)

    def invert(self):
        """Invert the check state of every visible row."""

        self._set_visible_checks(invert=True)

    def clear_checks(self):
        """Uncheck every visible row."""

        self._set_visible_checks(False)

    def _has_work(self):
        """Return whether the folder holds images to classify."""

        if self._result is None:
            return False
        return bool(self._result.total)

    def _refresh_buttons(self):
        """Enable the primary action only when it can do work."""

        self.confirm_button.setEnabled(self._has_work())
        self.reset_button.setEnabled(True)

    # ----------------------------------------------------------- actions

    def confirm(self):
        """Apply the checked selection and close on success.

        Nothing checked is not a filter of zero categories: it is the
        same as clearing the filter, so that path goes through
        clear_filter() and reports a cleared filter instead of
        "0 categories".
        """

        selected = set(self.checked_names())
        controller = getattr(
            self._explorer, "_label_filter_controller", None
        )
        if controller is None or not self._has_work():
            self.status_label.setText(NO_SELECTION_TEXT)
            return False
        if not selected:
            return self.clear_filter()
        if not controller.apply(selected):
            self.status_label.setText(NO_SELECTION_TEXT)
            return False
        self.status_label.setText(APPLIED_FORMAT % len(selected))
        self.accept()
        return True

    def clear_filter(self):
        """Clear the active filter and close.

        The controller refuses when the user cancelled the
        unsaved-annotations question of the widget: the filter is then
        still in place, so the window stays open and says that nothing
        was changed.
        """

        controller = getattr(
            self._explorer, "_label_filter_controller", None
        )
        if controller is not None and not controller.reset():
            self.status_label.setText(NO_SELECTION_TEXT)
            return False
        self.status_label.setText(CLEARED_TEXT)
        self.accept()
        return True
