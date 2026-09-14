"""Rename window: set a folder, read the log, get one zip.

The dialog is deliberately thin: rename_core owns every rule, this
module only drives the core and reports the result. Nothing here
touches the source folder either - the scan only reads it, and the
execution calls write_zip, whose only output is the archive the user
asked for. The window is opened from the Tool menu by
install_rename_tool, which is idempotent so a repeated mount call
cannot leave a second menu entry behind.

The source folder comes from the file dialog or from a drag and drop
of one folder; the archive name and its folder are derived from that
folder when the main button is clicked, so there is neither a file
name editor nor an output folder to pick: the zip lands next to the
dataset folder. Picking or dropping a folder parses it right away and
writes the whole result into a read only text display, one line per
fact: the summary counters, the number of ignored json files, the
blockers with the name of every image that misses its json, or the
mapping of every file that has to be renamed. Nothing else is
printed: a file that keeps its name is only counted in the closing
summary, never listed by name.

A blocked folder disables the main button instead of opening a popup,
and the reason stays visible in the display and the status bar. One
click then runs the packaging and streams its progress into the same
display; the progress counter of the write phase counts the entries
that are really renamed, not the entries of the archive.

No thread is used: the scan and the archiving run on the main thread
with QCoreApplication.processEvents driving the progress bar, exactly
like the export progress of the model validation dialog. The window is
disabled while that happens, so no reentry is possible, and there is
no cancel button: a cancelled archive would leave a .part file whose
state the user cannot see.
"""

from __future__ import annotations

import os.path as osp
import re

from PyQt6 import QtCore, QtGui, QtWidgets

from anylabeling.views.labeling.utils import qt as qt_utils

from . import rename_core

__all__ = [
    "DISPLAY_MAX_BLOCKS",
    "LOG_NAME",
    "PRIMARY_BUTTON_NAME",
    "PRIMARY_BUTTON_TEXT",
    "RenameDialog",
    "WINDOW_SIZE",
    "WINDOW_TITLE",
    "install_rename_tool",
]

WINDOW_TITLE = "重命名"
WINDOW_SIZE = (960, 620)
PRIMARY_BUTTON_NAME = "primary"
PRIMARY_BUTTON_TEXT = "重命名"
LOG_NAME = "rename_log"
DISPLAY_MAX_BLOCKS = 5000

LOG_PLACEHOLDER = "选择或拖入一个数据集目录后开始解析…"
SOURCE_NEEDED_TEXT = "请先选择源目录。"
PLAN_MISSING_TEXT = "目录尚未解析成功，无法重命名。"
NO_PARENT_TEXT = "源目录没有上级目录，无法导出"

SUMMARY_FORMAT = (
    "共 %d 个文件：图片 %d、成对 json %d、其他 %d、忽略 %d（原样镜像 %d）"
)
IGNORED_FORMAT = "忽略 %d 个无同名图片的 json（不写入 zip）"
BLOCKED_FORMAT = "发现 %d 处阻塞，不能重命名，未写入任何文件（含 .part）："
BLOCKED_B1_FORMAT = "图片缺少同名 json（%d 张）："
BLOCKED_OTHER_TEXT = "其他问题："
PENDING_FORMAT = "待改名 %d 个："
PENDING_NONE_TEXT = "没有需要改名的文件"
EMPTY_PLAN_TEXT = "目录为空：可以重命名（会得到一个 0 条目的 zip）"
PARSE_FAIL_FORMAT = "无法解析目录：%s"
RENAME_LINE_FORMAT = "%s -> %s"
PROGRESS_LINE_FORMAT = "%s -> %s    %d/%d"
DONE_FORMAT = "已导出：%s"
DONE_SUMMARY_FORMAT = (
    "共 %d 个文件：改名 %d、保持原名 %d、原样镜像 %d、忽略 %d"
)
FAIL_FORMAT = "重命名失败：%s"
FAIL_PART_FORMAT = "半成品已保留：%s"
FAIL_PART_PATTERN = re.compile(r"半成品已保留：(.+?)[）)]")
FAIL_TITLE = "重命名失败"

HINT_TEXT = "导出到数据集同级目录：<源目录名>%s.zip（重名自动加 _2、_3…）"
NAME_HINT_TEXT = HINT_TEXT % rename_core.ZIP_DEFAULT_SUFFIX
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
    """One click rename window: set a folder, get one zip next to it.

    The text display is the whole report. It is read only and capped
    at DISPLAY_MAX_BLOCKS lines; because Qt clears the widget memory
    under that cap, the lines are kept in ``self._lines`` as well and
    read back through plain_text() / log_lines().
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._plan = None
        self._busy = False
        self._lines = []
        self._rename_total = 0
        self._rename_done = 0
        self.setWindowTitle(WINDOW_TITLE)
        self.resize(*WINDOW_SIZE)
        self.setAcceptDrops(True)
        self._build_ui()
        self._refresh_actions()

    # ---------------------------------------------------------------- UI

    def _build_ui(self):
        """Build the source row, the display, the bar and the status."""

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

        self.log = QtWidgets.QPlainTextEdit()
        self.log.setObjectName(LOG_NAME)
        self.log.setReadOnly(True)
        self.log.setLineWrapMode(
            QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap
        )
        self.log.setMaximumBlockCount(DISPLAY_MAX_BLOCKS)
        self.log.setPlaceholderText(LOG_PLACEHOLDER)
        self.log.setFont(
            QtGui.QFontDatabase.systemFont(
                QtGui.QFontDatabase.SystemFont.FixedFont
            )
        )
        root.addWidget(self.log, stretch=1)

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
        never exports anything, it only parses the folder; the user
        still clicks the main button.
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
        """Ask for the source folder and parse it right away."""

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
        """Remember the source folder, clear the display and parse it.

        This is the single entry of both the picker and the drop: the
        display is emptied, the folder is remembered and the folder is
        parsed immediately, so the user sees the summary and the
        blockers without a second click.
        """

        directory = (directory or "").strip()
        if not directory:
            return
        self._clear_display()
        self.source_label.setText(directory)
        self.parse_source()
        self._refresh_actions()

    def _clear_display(self):
        """Drop the previous plan, the text display and the counters."""

        self._plan = None
        self._lines = []
        self._rename_total = 0
        self._rename_done = 0
        self.log.clear()
        self.progress.hide()
        self.status_bar.clearMessage()

    def _refresh_actions(self):
        """Enable the main button exactly when a run is possible.

        The four conditions are: a source folder, an export folder
        derived from it, a parsed plan that is not blocked, and no
        running scan or write.
        """

        source = self.source_dir()
        ready = bool(source) and bool(self.export_dir(source))
        ready = ready and self._plan is not None
        ready = ready and not self._plan.blocked()
        self.execute_button.setEnabled(bool(ready) and not self._busy)

    # ------------------------------------------------------------ 解析

    def parse_source(self):
        """Parse the current source folder into a plan and render it.

        The scan runs on the main thread while the window is disabled
        and the progress bar reports it. A folder that cannot be read
        is reported in the display instead of raising; in both cases
        the button is refreshed from the new plan.
        """

        directory = self.source_dir()
        if not directory:
            return None
        self._busy = True
        self._refresh_actions()
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
            self._fail_parse(error)
            return None
        finally:
            self.progress.hide()
            self._busy = False
            self.setEnabled(True)
            self._refresh_actions()
        self._apply_plan(plan)
        return plan

    def _fail_parse(self, error):
        """Report a folder that cannot be parsed at all."""

        self._plan = None
        self._append_line(PARSE_FAIL_FORMAT % error)
        self.status_bar.showMessage(PARSE_FAIL_FORMAT % error)
        return self._lines

    def _apply_plan(self, plan):
        """Store a parsed plan and render its lines."""

        self._plan = plan
        lines = self._render_plan(plan)
        self._refresh_actions()
        return lines

    def _scan_progress(self, done: int, total: int, message: str):
        """Drive the busy bar from a plan_directory callback."""

        self.progress.show()
        self.progress.setRange(0, max(int(total), 1))
        self.progress.setValue(int(done))
        if message and message != "Done":
            self.status_bar.showMessage("正在解析：%s" % message)
        QtCore.QCoreApplication.processEvents()

    def _render_plan(self, plan):
        """Print every line of one parsed plan into the display."""

        for line in self._plan_lines(plan):
            self._append_line(line)
        if not self._lines:
            self.status_bar.showMessage(LOG_PLACEHOLDER)
        else:
            self.status_bar.showMessage(self._lines[0])
        return list(self._lines)

    def _plan_lines(self, plan):
        """Return the display lines of a parsed plan.

        The order is fixed: the summary counters, the ignored json
        count, then either the blockers or the pending renames. Only
        the files that really change their name are listed by name, an
        ignored json and a mirrored file are just counted.
        """

        stats = plan.stats()
        mirrored = len(plan.mirrored)
        other = (
            stats["total"]
            - stats["images"]
            - stats["json"]
            - stats["ignored"]
        )
        lines = [
            SUMMARY_FORMAT % (
                stats["total"],
                stats["images"],
                stats["json"],
                other,
                stats["ignored"],
                mirrored,
            ),
            IGNORED_FORMAT % stats["ignored"],
        ]
        if plan.blocked():
            lines.append(BLOCKED_FORMAT % self._blocker_count(plan))
            b1 = self._b1_names(plan)
            if b1:
                lines.append(BLOCKED_B1_FORMAT % len(b1))
                lines.extend(b1)
            rest = [
                text for text in plan.blockers
                if "图片缺少同名 json" not in text
            ]
            if rest:
                lines.append(BLOCKED_OTHER_TEXT)
                lines.extend(rest)
            return lines
        pairs = self._rename_pairs(plan)
        if not pairs:
            if not plan.entries():
                lines.append(EMPTY_PLAN_TEXT)
            else:
                lines.append(PENDING_NONE_TEXT)
            return lines
        lines.append(PENDING_FORMAT % len(pairs))
        for source, target in pairs:
            lines.append(RENAME_LINE_FORMAT % (source, target))
        return lines

    def _b1_names(self, plan):
        """Return the file names of the images that miss their json."""

        names = []
        for item in plan.items:
            if item.error != "缺少同名 json":
                continue
            names.append(item.image_name)
        return names

    def _blocker_count(self, plan):
        """Return the number of problems shown in the blocker block.

        One blocker text may name several files: the B1 blocker counts
        once per missing image, every other blocker counts once. An
        image listed twice - two images sharing a stem - is counted
        once, because the B5 blocker already covers the double name.
        """

        count = len(self._b1_names(plan))
        for text in plan.blockers:
            if "图片缺少同名 json" not in text:
                count += 1
        return count

    def _rename_pairs(self, plan):
        """Return the (source name, target name) pairs that change name.

        One pair per file that really gets a new name: the image of a
        renamed item plus its json when that json exists. The json of
        an item whose json is missing (the B1 blocker) is never part of
        the archive and never part of this list.
        """

        pairs = []
        for item in plan.valid_items():
            pairs.append((item.image_name, item.target_image_name()))
            json_name = item.json_name
            if osp.isfile(osp.join(plan.directory, json_name)):
                pairs.append((json_name, item.target_json_name()))
        return pairs

    # ------------------------------------------------------------ 执行

    def execute(self):
        """Export the archive of the current plan.

        The plan comes from the parse that ran when the source folder
        was set; a blocked plan keeps the button disabled, and this
        method refuses such a plan defensively as well.

        Returns:
            The write_zip result dictionary, or None when nothing was
            exported (no source, no parent folder, a blocked or
            missing plan, or an error).
        """

        directory = self.source_dir()
        if not directory:
            self._warn(SOURCE_NEEDED_TEXT)
            return None
        out_dir = self.export_dir(directory)
        if not out_dir:
            self._warn(NO_PARENT_TEXT)
            return None
        if self._plan is None:
            self._warn(PLAN_MISSING_TEXT)
            return None
        plan = self._plan
        if plan.blocked():
            self._report_blocked(plan)
            return None
        name = osp.basename(osp.normpath(directory))
        target = rename_core.resolve_output_path(
            out_dir, ZIP_NAME_FORMAT % (name, rename_core.ZIP_DEFAULT_SUFFIX)
        )
        return self._write(plan, target)

    def _write(self, plan, path: str):
        """Write the archive of plan into path, streaming its progress.

        The display gets one line per renamed entry and two closing
        lines; a failure appends its reason instead of a success line
        and keeps the progress lines that were already printed.
        """

        self._begin_write_log(len(self._rename_pairs(plan)))
        self._busy = True
        self._refresh_actions()
        self.setEnabled(False)
        try:
            result = rename_core.write_zip(
                plan,
                path,
                progress=self._write_progress,
            )
        except Exception as error:  # noqa: BLE001
            self._finish_write_log(plan, error)
            self._report_failure(error)
            return None
        finally:
            self.progress.hide()
            self._busy = False
            self.setEnabled(True)
            self._refresh_actions()
        self._finish_write_log(plan, None, result)
        self._report_success(result)
        return result

    def _begin_write_log(self, total: int):
        """Reset the progress counter of the write phase."""

        self._rename_total = max(int(total), 0)
        self._rename_done = 0
        self.progress.show()
        self.progress.setRange(0, max(self._rename_total, 1))
        self.progress.setValue(0)
        QtCore.QCoreApplication.processEvents()
        return self._rename_total

    def _write_progress(
        self,
        done: int,
        total: int,
        entry_name: str,
        source_name: str,
        changed: bool,
    ):
        """Print one line per renamed entry of the archive.

        The write_zip callback counts the entries of the archive, but
        the display only reports the entries that really change their
        name: i is "the i-th renamed file" and N was set by
        _begin_write_log to the number of files the plan renames. An
        entry that keeps its name leaves the bar alone, so the value
        follows the renamed files only and never walks backwards.
        """

        if entry_name == "Done":
            return None
        if not changed:
            QtCore.QCoreApplication.processEvents()
            return None
        source = source_name or entry_name
        self._rename_done += 1
        line = PROGRESS_LINE_FORMAT % (
            source,
            entry_name,
            self._rename_done,
            self._rename_total,
        )
        self._append_line(line)
        self.progress.setValue(self._rename_done)
        self.status_bar.showMessage("正在打包：%s" % entry_name)
        QtCore.QCoreApplication.processEvents()
        return line

    def _finish_write_log(self, plan, error=None, result=None):
        """Print the closing lines of the write phase."""

        if error is not None:
            text = FAIL_FORMAT % error
            self._append_line(text)
            match = FAIL_PART_PATTERN.search(str(error))
            if match is not None:
                self._append_line(FAIL_PART_FORMAT % match.group(1))
            self.status_bar.showMessage(text)
            return list(self._lines)
        result = result or {}
        zip_path = result.get("zip_path", "")
        files = result.get("files", 0)
        renamed = result.get("renamed", 0)
        ignored = result.get("ignored", len(plan.ignored))
        mirrored = len(plan.mirrored)
        # files is the entry count of the archive, so it leaves out the
        # ignored json: the closing line counts the whole source folder,
        # like the parse summary does. The four buckets print there are
        # mutually exclusive and add up to that total.
        source_total = files + ignored
        kept = files - renamed - mirrored
        self._append_line(DONE_FORMAT % zip_path)
        summary = DONE_SUMMARY_FORMAT % (
            source_total,
            renamed,
            kept,
            mirrored,
            ignored,
        )
        self._append_line(summary)
        self.status_bar.showMessage(DONE_FORMAT % zip_path)
        return list(self._lines)

    # ------------------------------------------------------------ 报告

    def _report_blocked(self, plan):
        """Keep the folder untouched and report the blockers again."""

        text = BLOCKED_FORMAT % self._blocker_count(plan)
        self.status_bar.showMessage(text)
        return text

    def _report_success(self, result):
        """Tell the user the full path of the archive."""

        text = DONE_FORMAT % result["zip_path"]
        self.status_bar.showMessage(text)
        QtWidgets.QMessageBox.information(self, WINDOW_TITLE, text)
        return text

    def _report_failure(self, error):
        """Show the failure, half finished archive path included."""

        text = FAIL_FORMAT % error
        self.status_bar.showMessage(text)
        QtWidgets.QMessageBox.warning(self, FAIL_TITLE, text)
        return text

    def _warn(self, text: str):
        """Show one warning popup and mirror it in the status bar."""

        self.status_bar.showMessage(text)
        QtWidgets.QMessageBox.warning(self, WINDOW_TITLE, text)
        return text

    # ------------------------------------------------------------ 显示器

    def _append_line(self, text: str):
        """Append one line to the display and to the in memory log.

        The widget is capped at DISPLAY_MAX_BLOCKS lines, so its own
        text may be dropped; the log list keeps the whole run.
        """

        text = str(text)
        self.log.appendPlainText(text)
        self._lines.append(text)
        return text

    def plain_text(self) -> str:
        """Return the whole display as one newline joined string."""

        return "\n".join(self._lines)

    def log_lines(self):
        """Return the display lines as a list of strings."""

        return list(self._lines)
