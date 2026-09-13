"""Rename window: set a folder, click once, get one zip.

The dialog is deliberately thin: rename_core owns every rule, this
module only drives the core and reports the result. Nothing here
touches the source folder either - the scan only reads it, and the
execution calls write_zip, whose only output is the archive the user
asked for. The window is opened from the Tool menu by
install_rename_tool, which is idempotent so a repeated mount call
cannot leave a second menu entry behind.

The source folder comes from the file dialog or from a drag and drop
of one folder; the archive name and its folder are derived from that
folder when the main button is clicked, so there is neither a preview
step, nor a file name editor, nor an output folder to pick: the zip
lands next to the dataset folder. One click runs the scan, the
optional blocker popup and the packaging in a row; dropping a folder
never writes anything by itself.

No thread is used: the scan and the archiving run on the main thread
with QCoreApplication.processEvents driving the progress bar, exactly
like the export progress of the model validation dialog. The window is
disabled while that happens, so no reentry is possible, and there is
no cancel button: a cancelled archive would leave a .part file whose
state the user cannot see.
"""

from __future__ import annotations

import os.path as osp

from PyQt6 import QtCore, QtWidgets

from anylabeling.views.labeling.utils import qt as qt_utils

from . import rename_core

__all__ = [
    "STATUS_ALREADY",
    "STATUS_BLOCKED",
    "STATUS_MIRRORED",
    "STATUS_NOT_EXPORTED",
    "STATUS_ORPHAN",
    "STATUS_RENAMED",
    "RenameDialog",
    "install_rename_tool",
]

WINDOW_TITLE = "重命名"
WINDOW_SIZE = (960, 620)
PRIMARY_BUTTON_NAME = "primary"
PRIMARY_BUTTON_TEXT = "重命名"

STATUS_RENAMED = "已改名"
STATUS_ALREADY = "符合规范，保持原名"
STATUS_ORPHAN = "孤儿增强文件，保持原名"
STATUS_MIRRORED = "原样镜像"
STATUS_BLOCKED = "阻塞，未导出"
STATUS_NOT_EXPORTED = "未导出"
STATUS_COLUMN = 3

ACTION_TEXT = {
    rename_core.ACTION_RENAME: STATUS_RENAMED,
    rename_core.ACTION_ALREADY: STATUS_ALREADY,
    rename_core.ACTION_ORPHAN: STATUS_ORPHAN,
}

STATS_TEXT = (
    "共 %d 个文件：%s %d、符合规范 %d、孤儿增强 %d、原样镜像 %d"
)
STATS_PENDING = "待改名"
STATS_RENAMED = "已改名"
DONE_TEXT = "已导出：%s\n共 %d 个文件，其中 %d 个已改名。"
DONE_TITLE = "重命名完成"
FAIL_TITLE = "重命名失败"
FAIL_BLOCKER_FORMAT = "导出失败：%s"
BLOCKED_TITLE = "无法重命名"

NOT_RUN_TEXT = "尚未重命名"
EMPTY_TEXT = "—"
NO_BLOCKER_TEXT = "未发现阻塞项"
MAX_BLOCKERS_SHOWN = 8
BLOCKER_STYLE = "color: #c0392b;"

SOURCE_HINT_FORMAT = (
    "导出到数据集同级目录：<源目录名>%s.zip（重名自动加 _2、_3…）"
)
NAME_HINT_TEXT = SOURCE_HINT_FORMAT % rename_core.ZIP_DEFAULT_SUFFIX
SOURCE_NEEDED_TEXT = "请先选择源目录。"
NO_PARENT_TEXT = "源目录没有上级目录，无法导出"
DROP_ONLY_FOLDERS = "仅支持文件夹，未改动源目录"
DROP_FILE_REASON = "仅支持文件夹"
DROP_EXTRA_FOLDER_REASON = "仅取第一个文件夹"
DROP_IGNORED_PREFIX = "已忽略: "
ZIP_NAME_FORMAT = "%s%s.zip"


def install_rename_tool(widget):
    """Attach the rename action to the Tool menu of a widget.

    Installing twice is a no op, so the mount point can be called
    again without adding a second entry to the menu.

    Args:
        widget: The labeling widget to extend. It has to expose
            ``menus.tool``.

    Returns:
        The installed QAction, or ``None`` when the widget has no
        Tool menu.
    """

    menus = getattr(widget, "menus", None)
    menu = getattr(menus, "tool", None)
    if menu is None:
        return None
    existing = getattr(widget, "_rename_tool_action", None)
    if existing is not None:
        return existing
    action = qt_utils.new_action(
        menu,
        "重命名",
        lambda _checked=False: launch_rename_tool(widget),
        icon="convert",
        tip="按标注主分类批量重命名图片与 json，拖入目录一键导出 zip",
    )
    menu.addAction(action)
    widget._rename_tool_action = action
    return action


def launch_rename_tool(parent=None):
    """Lazy wrapper around the launcher, keeping imports acyclic."""

    from .launcher import launch_rename_tool as _launch

    return _launch(parent)


class RenameDialog(QtWidgets.QDialog):
    """One click rename window: set a folder, get one zip next to it."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(WINDOW_TITLE)
        self.resize(*WINDOW_SIZE)
        self.setAcceptDrops(True)
        self._build_ui()
        self._refresh_actions()

    # ---------------------------------------------------------------- UI

    def _build_ui(self):
        """Build the source row, the table and the status bar."""

        root = QtWidgets.QVBoxLayout(self)
        root.setSpacing(6)

        source_row = QtWidgets.QHBoxLayout()
        source_row.addWidget(QtWidgets.QLabel("源目录："))
        self.source_button = QtWidgets.QToolButton()
        self.source_button.setText("选择源目录")
        self.source_button.clicked.connect(self.pick_source_dir)
        source_row.addWidget(self.source_button)
        self.source_label = QtWidgets.QLabel("未选择")
        source_row.addWidget(self.source_label, stretch=1)
        root.addLayout(source_row)

        self.hint_label = QtWidgets.QLabel(NAME_HINT_TEXT)
        root.addWidget(self.hint_label)

        button_row = QtWidgets.QHBoxLayout()
        self.execute_button = QtWidgets.QPushButton(PRIMARY_BUTTON_TEXT)
        self.execute_button.setObjectName(PRIMARY_BUTTON_NAME)
        self.execute_button.setEnabled(False)
        self.execute_button.clicked.connect(self.execute)
        button_row.addWidget(self.execute_button)
        button_row.addStretch(1)
        root.addLayout(button_row)

        self.stats_label = QtWidgets.QLabel(NOT_RUN_TEXT)
        root.addWidget(self.stats_label)

        self.blocker_label = QtWidgets.QLabel(NO_BLOCKER_TEXT)
        self.blocker_label.setWordWrap(True)
        self.blocker_label.setStyleSheet(BLOCKER_STYLE)
        root.addWidget(self.blocker_label)

        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["原文件名", "主标签", "新文件名", "状态"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.table.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.NoSelection
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(
            QtWidgets.QHeaderView.ResizeMode.Interactive
        )
        self.table.setColumnWidth(0, 250)
        self.table.setColumnWidth(1, 120)
        self.table.setColumnWidth(2, 250)
        self.table.setColumnWidth(3, 220)
        root.addWidget(self.table, stretch=1)

        self.progress = QtWidgets.QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.hide()
        root.addWidget(self.progress)

        self.status_bar = QtWidgets.QStatusBar()
        root.addWidget(self.status_bar)

    # ------------------------------------------------------- 拖拽目录

    def dragEnterEvent(self, event):
        """Accept a drag as soon as it carries at least one folder."""

        self.accept_folders(event)

    def dragMoveEvent(self, event):
        """Keep the same answer while the drag moves over the window.

        Qt only delivers the drop once the move event is accepted, so
        this repeats the folder test of ``dragEnterEvent`` through the
        same helper instead of leaving the event to the default
        handler, which would ignore it and cancel the drop.

        Args:
            event: The drag move event delivered by Qt.
        """

        self.accept_folders(event)

    def accept_folders(self, event):
        """Accept a drag event exactly when it carries a folder.

        Args:
            event: A drag enter/move event exposing ``mimeData`` plus
                the two answer methods of the Qt event API.
        """

        folders, _ignored = self.drop_entries(event.mimeData())
        if folders:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        """Take the first dropped folder as the source directory.

        Only folders are accepted: a dropped file is reported in the
        status bar and the current source directory is kept. Dropping
        never exports anything, the user still clicks the main button.
        """

        folders, ignored = self.drop_entries(event.mimeData())
        if not folders:
            self.status_bar.showMessage(DROP_ONLY_FOLDERS)
            event.ignore()
            return
        self.set_source_dir(folders[0])
        if ignored:
            self.status_bar.showMessage(self.ignored_text(ignored))
        event.acceptProposedAction()

    def drop_entries(self, mime):
        """Split a drop into its source folders and its ignored items.

        Args:
            mime: The QMimeData of the drop, or None.

        Returns:
            A (folders, ignored) pair. folders holds the absolute
            paths of the dropped folders in drop order; ignored holds
            one (name, reason) pair per entry that was not used, with
            the reasons already localized.
        """

        if mime is None or not mime.hasUrls():
            return [], []
        urls = list(mime.urls())
        folders = []
        ignored = []
        for url in urls:
            path = url.toLocalFile()
            if not path:
                continue
            if not osp.isdir(path):
                ignored.append((osp.basename(path), DROP_FILE_REASON))
                continue
            full = osp.abspath(path)
            if folders:
                ignored.append(
                    (osp.basename(full), DROP_EXTRA_FOLDER_REASON)
                )
            else:
                folders.append(full)
        return folders, ignored

    def ignored_text(self, ignored):
        """Return the status line that lists the ignored drop items."""

        parts = ["%s（%s）" % (name, reason) for name, reason in ignored]
        return DROP_IGNORED_PREFIX + "、".join(parts)

    # ------------------------------------------------------------ 选择

    def pick_source_dir(self):
        """Ask for the source folder and reset the previous result."""

        chosen = QtWidgets.QFileDialog.getExistingDirectory(
            self, "选择源目录", self.source_dir() or ""
        )
        if not chosen:
            return
        self.set_source_dir(chosen)

    def source_dir(self) -> str:
        """Return the source folder currently held by the label."""

        text = self.source_label.text()
        return "" if text in ("", "未选择") else text

    def export_dir(self, directory: str) -> str:
        """Return the folder the zip of directory is exported into.

        The zip always lands next to the dataset folder, that is in
        the parent folder of the source directory. A source without a
        usable parent - a filesystem root - has no such folder and
        yields an empty string, which aborts the export.

        Args:
            directory: The source folder, absolute or not.

        Returns:
            The parent folder of directory, or "" when directory is a
            root or has no parent at all.
        """

        source = osp.normpath(osp.abspath(directory))
        parent = osp.dirname(source)
        if not parent or parent == source:
            return ""
        return parent

    def set_source_dir(self, directory: str):
        """Remember the source folder and reset the previous result."""

        directory = (directory or "").strip()
        if not directory:
            return
        self.source_label.setText(directory)
        self._reset_result()
        self._refresh_actions()

    def _reset_result(self):
        """Drop the previous table and counters."""

        self.table.setRowCount(0)
        self.stats_label.setText(NOT_RUN_TEXT)
        self.show_blockers([])

    def _refresh_actions(self):
        """Enable the main button exactly when a source folder is set."""

        self.execute_button.setEnabled(bool(self.source_dir()))

    # ------------------------------------------------------------ 执行

    def execute(self):
        """Scan the source folder and export the archive in one click.

        The scan, the blocker check and the packaging run in a row.
        A blocked folder shows the blockers, fills the table with the
        blocked status and writes nothing at all: no zip and no .part
        file.

        Returns:
            The write_zip result dictionary, or None when nothing was
            exported (no source, no parent folder, a blocked plan or
            an error).
        """

        directory = self.source_dir()
        if not directory:
            self._warn(SOURCE_NEEDED_TEXT)
            return None
        out_dir = self.export_dir(directory)
        if not out_dir:
            self._warn(NO_PARENT_TEXT)
            return None
        plan = self._scan(directory)
        if plan is None:
            return None
        if plan.blocked():
            self._report_blocked(plan)
            return None
        name = osp.basename(osp.normpath(directory))
        target = rename_core.resolve_output_path(
            out_dir, ZIP_NAME_FORMAT % (name, rename_core.ZIP_DEFAULT_SUFFIX)
        )
        result = self._write(plan, target)
        if result is None:
            return None
        self.fill_table(plan)
        self.update_stats(plan)
        self.show_blockers([])
        self._report_success(result)
        return result

    def _scan(self, directory: str):
        """Scan the folder and merge the entry name check into it."""

        self.setEnabled(False)
        try:
            plan = rename_core.plan_directory(
                directory, progress=self._scan_progress
            )
            if not plan.blocked():
                rename_core.resolve_targets(plan)
            problems = rename_core.check_entry_names(plan)
            if problems:
                plan.blockers.extend(problems)
        except rename_core.RenameError as error:
            self._discard_result()
            self._report_failure(error)
            return None
        finally:
            self.progress.hide()
            self.setEnabled(True)
            self._refresh_actions()
        self.fill_table(plan)
        self.update_stats(plan)
        return plan

    def _scan_progress(self, done: int, total: int, message: str):
        """Drive the busy bar from a plan_directory callback."""

        self.progress.show()
        self.progress.setRange(0, max(int(total), 1))
        self.progress.setValue(int(done))
        if message and message != "Done":
            self.status_bar.showMessage("正在扫描：%s" % message)
        QtCore.QCoreApplication.processEvents()

    def _write(self, plan, path: str):
        """Write the archive of plan into path with a busy dialog."""

        progress = QtWidgets.QProgressDialog(
            "正在打包…", None, 0, 100, self
        )
        progress.setWindowTitle("正在打包…")
        progress.setWindowModality(
            QtCore.Qt.WindowModality.WindowModal
        )
        progress.setAutoClose(False)
        progress.setCancelButton(None)
        progress.show()
        self.setEnabled(False)
        try:
            return rename_core.write_zip(
                plan,
                path,
                progress=lambda done, total, message: self._zip_progress(
                    progress, done, total, message
                ),
            )
        except Exception as error:  # noqa: BLE001
            self._discard_result()
            self._report_failure(error)
            return None
        finally:
            progress.close()
            self.setEnabled(True)
            self._refresh_actions()

    def _zip_progress(self, dialog, done: int, total: int, message: str):
        """Drive the packaging dialog from a write_zip callback."""

        dialog.setMaximum(max(int(total), 1))
        dialog.setValue(int(done))
        dialog.setLabelText(message)
        QtWidgets.QApplication.processEvents()

    # ------------------------------------------------------------ 结果

    def fill_table(self, plan):
        """Render the plan: one row per top level file, natural order."""

        rows = self.table_rows(plan)
        self.table.setRowCount(len(rows))
        for index, cells in enumerate(rows):
            for column, text in enumerate(cells):
                self.table.setItem(
                    index, column, QtWidgets.QTableWidgetItem(text)
                )
        return rows

    def table_rows(self, plan):
        """Return the (name, label, target, status) rows of a plan."""

        rows = []
        for item in plan.items:
            for name in (item.image_name, item.json_name):
                if not osp.isfile(osp.join(plan.directory, name)):
                    continue
                rows.append((
                    name,
                    item.label or EMPTY_TEXT,
                    self._target_text(plan, item, name),
                    self._status_text(plan, item),
                ))
        for name, flag in self.mirrored_rows(plan):
            rows.append((name, EMPTY_TEXT, name, flag))
        return rows

    def mirrored_rows(self, plan):
        """Return (name, status) of the files that are mirrored as is."""

        flag = STATUS_BLOCKED if plan.blocked() else STATUS_MIRRORED
        return [(name, flag) for name in plan.mirrored]

    def _target_text(self, plan, item, name: str) -> str:
        """Return the new name of one row, or the placeholder."""

        if plan.blocked() or not item.target_stem:
            return EMPTY_TEXT
        if name == item.json_name:
            return item.target_json_name()
        if osp.splitext(name)[1].lower() in rename_core.IMAGE_EXTS:
            return item.target_image_name()
        return EMPTY_TEXT

    def _status_text(self, plan, item) -> str:
        """Return the Chinese status of one item."""

        if plan.blocked():
            return STATUS_BLOCKED
        return ACTION_TEXT.get(item.action, EMPTY_TEXT)

    def update_stats(self, plan):
        """Show the file counters of the plan."""

        renamed = len(plan.valid_items())
        already = sum(
            1 for item in plan.items
            if item.action == rename_core.ACTION_ALREADY
        )
        orphans = sum(
            1 for item in plan.items
            if item.action == rename_core.ACTION_ORPHAN
        )
        text = STATS_TEXT % (
            plan.total_files(),
            STATS_PENDING if plan.blocked() else STATS_RENAMED,
            renamed,
            already,
            orphans,
            len(plan.mirrored),
        )
        self.stats_label.setText(text)
        return text

    def _discard_result(self):
        """Take the success look back after a failed export.

        The table and the counters are rendered by ``_scan``,
        that is before the archive is written, so a failed ``_scan``
        or ``_write`` has to undo that preview: no row may keep a
        "renamed" status and the counters go back to their initial
        text.
        """

        for row in range(self.table.rowCount()):
            item = self.table.item(row, STATUS_COLUMN)
            if item is not None:
                item.setText(STATUS_NOT_EXPORTED)
        self.stats_label.setText(NOT_RUN_TEXT)

    def show_blockers(self, blockers):
        """Show the first blockers plus the total count."""

        items = list(blockers or [])
        if not items:
            self.blocker_label.setText(NO_BLOCKER_TEXT)
            return NO_BLOCKER_TEXT
        text = "；".join(items[:MAX_BLOCKERS_SHOWN])
        if len(items) > MAX_BLOCKERS_SHOWN:
            text += "；…共 %d 条" % len(items)
        self.blocker_label.setText(text)
        return text

    # ------------------------------------------------------------ 报告

    def _report_blocked(self, plan):
        """Show the blockers; the folder is left exactly as it was."""

        text = self.show_blockers(plan.blockers)
        self.status_bar.showMessage(text)
        QtWidgets.QMessageBox.warning(self, BLOCKED_TITLE, text)
        return text

    def _report_success(self, result):
        """Tell the user the full path of the archive."""

        text = DONE_TEXT % (
            result["zip_path"],
            result["files"],
            result["renamed"],
        )
        self.status_bar.showMessage(text)
        QtWidgets.QMessageBox.information(self, DONE_TITLE, text)
        return text

    def _report_failure(self, error):
        """Show the failure, half finished archive path included."""

        text = "重命名失败：%s" % error
        self.blocker_label.setText(FAIL_BLOCKER_FORMAT % error)
        self.status_bar.showMessage(text)
        QtWidgets.QMessageBox.warning(self, FAIL_TITLE, text)
        return text

    def _warn(self, text: str):
        """Show one warning popup and mirror it in the status bar."""

        self.status_bar.showMessage(text)
        QtWidgets.QMessageBox.warning(self, WINDOW_TITLE, text)
        return text
