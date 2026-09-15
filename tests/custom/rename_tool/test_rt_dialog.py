"""Tests of the rename dialog: text display, drops, one click export."""

import os
from types import SimpleNamespace

from PyQt6 import QtCore, QtWidgets

from anylabeling.custom.rename_tool import dialog as rt_dialog
from anylabeling.custom.rename_tool import rename_core as core

from conftest import (
    make_pair,
    read_zip,
    snapshot,
    write_image,
    write_json,
    zip_doc,
)


def _dialog(source=None):
    """Build a dialog pointed at a source folder."""

    dialog = rt_dialog.RenameDialog()
    if source is not None:
        dialog.set_source_dir(source)
    return dialog


def _lines(dialog):
    """Return the text display as a list of lines."""

    return dialog.log_lines()


def _mappings(lines):
    """Return the display lines that carry a source -> target name."""

    return [line for line in lines if "->" in line]


def _renames(lines):
    """Return the planned rename lines, progress lines excluded."""

    return [line for line in _mappings(lines) if not _progress(line)]


def _progress_count(dialog):
    """Return the number of progress lines printed by the last write."""

    return len([line for line in _lines(dialog) if _progress(line)])


def _progress(line):
    """Return the i/N progress suffix of a line, or the empty string."""

    head, _sep, tail = line.rpartition("    ")
    if head and "/" in tail:
        left, _slash, right = tail.partition("/")
        if left.isdigit() and right.isdigit():
            return tail
    return ""


def _capture(monkeypatch, name):
    """Collect the calls of a QMessageBox static method."""

    seen = []

    def _slot(*args, **kwargs):
        seen.append(args)
        return QtWidgets.QMessageBox.StandardButton.Ok

    monkeypatch.setattr(
        QtWidgets.QMessageBox, name, staticmethod(_slot)
    )
    return seen


def _drop_event(*paths):
    """Build a stand in drop event carrying the given local paths."""

    mime = QtCore.QMimeData()
    mime.setUrls([QtCore.QUrl.fromLocalFile(path) for path in paths])
    state = {"accepted": False, "ignored": False}
    event = SimpleNamespace(
        mimeData=lambda: mime,
        acceptProposedAction=lambda: state.update(accepted=True),
        ignore=lambda: state.update(ignored=True),
    )
    return event, state


def _drag_event(*paths):
    """Build a stand in drag move event over the given local paths.

    The mime object is a real QMimeData, so it answers hasUrls() as
    well as urls(); both are what the dialog relies on.
    """

    event, state = _drop_event(*paths)
    assert event.mimeData().hasUrls() is True
    return event, state


def _read(path):
    """Return the bytes of a file."""

    with open(path, "rb") as handle:
        return handle.read()


def _zip_name(source):
    """Return the default archive name of a source folder."""

    base = os.path.basename(os.path.normpath(source))
    return base + core.ZIP_DEFAULT_SUFFIX + ".zip"


def _dataset(root):
    """Create a small folder covering every result status."""

    make_pair(root, "a", "person")
    make_pair(root, "person_1", "person")
    make_pair(root, "b_aug1", "person")
    write_image(root, "classes.txt", b"person\n")


class TestLayout:
    """The window carries a read only text display and one button."""

    def test_title(self, qapp):
        dialog = rt_dialog.RenameDialog()
        assert dialog.windowTitle() == "重命名"

    def test_display_is_read_only_text(self, qapp):
        dialog = rt_dialog.RenameDialog()
        assert isinstance(dialog.log, QtWidgets.QPlainTextEdit)
        assert dialog.log.isReadOnly() is True
        assert dialog.log.objectName() == rt_dialog.LOG_NAME
        assert dialog.log.lineWrapMode() == (
            QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap
        )
        assert dialog.log.maximumBlockCount() == (
            rt_dialog.DISPLAY_MAX_BLOCKS
        )
        assert dialog.log.toPlainText() == ""
        assert dialog.plain_text() == ""

    def test_display_placeholder(self, qapp):
        dialog = rt_dialog.RenameDialog()
        assert dialog.log.placeholderText() == rt_dialog.LOG_PLACEHOLDER

    def test_no_table(self, qapp):
        dialog = rt_dialog.RenameDialog()
        assert not hasattr(dialog, "table")
        assert dialog.findChildren(QtWidgets.QTableWidget) == []

    def test_no_labels_of_the_old_layout(self, qapp):
        dialog = rt_dialog.RenameDialog()
        assert not hasattr(dialog, "stats_label")
        assert not hasattr(dialog, "blocker_label")

    def test_no_progress_dialog_class_use(self, qapp):
        dialog = rt_dialog.RenameDialog()
        assert dialog.findChildren(QtWidgets.QProgressDialog) == []

    def test_primary_button(self, qapp):
        dialog = rt_dialog.RenameDialog()
        assert dialog.execute_button.text() == "重命名"
        assert dialog.execute_button.objectName() == "primary"
        assert dialog.execute_button.isEnabled() is False

    def test_no_preview_button(self, qapp):
        dialog = rt_dialog.RenameDialog()
        assert not hasattr(dialog, "preview_button")
        assert not hasattr(dialog, "preview")

    def test_no_name_editor(self, qapp):
        dialog = rt_dialog.RenameDialog()
        assert not hasattr(dialog, "name_edit")
        assert dialog.findChildren(QtWidgets.QLineEdit) == []

    def test_no_output_controls(self, qapp):
        """The output folder is derived, nothing selects it."""

        dialog = rt_dialog.RenameDialog()
        assert not hasattr(dialog, "output_button")
        assert not hasattr(dialog, "output_label")
        assert not hasattr(dialog, "set_output_dir")
        assert not hasattr(dialog, "pick_output_dir")
        assert not hasattr(dialog, "output_dir")
        assert "输出目录" not in dialog.hint_label.text()
        assert not any(
            "输出目录" in label.text()
            for label in dialog.findChildren(QtWidgets.QLabel)
        )

    def test_hint_names_the_export_location(self, qapp):
        dialog = rt_dialog.RenameDialog()
        text = dialog.hint_label.text()
        assert "数据集同级目录" in text
        assert core.ZIP_DEFAULT_SUFFIX + ".zip" in text

    def test_progress_is_hidden(self, qapp):
        dialog = rt_dialog.RenameDialog()
        assert dialog.progress.isHidden() is True

    def test_drops_are_accepted(self, qapp):
        dialog = rt_dialog.RenameDialog()
        assert dialog.acceptDrops() is True


class TestDefaults:
    """Choosing the source derives the export folder and parses it."""

    def test_export_dir_is_the_source_parent(self, rt_parent):
        _parent, data = rt_parent
        make_pair(data, "a", "person")
        dialog = _dialog(data)
        assert dialog.export_dir(data) == os.path.dirname(data)
        assert dialog.execute_button.isEnabled() is True

    def test_empty_directory_is_ignored(self):
        dialog = rt_dialog.RenameDialog()
        dialog.set_source_dir("   ")
        assert dialog.source_dir() == ""
        assert dialog.execute_button.isEnabled() is False

    def test_export_dir_of_a_root_is_empty(self):
        dialog = rt_dialog.RenameDialog()
        assert dialog.export_dir("/") == ""

    def test_pick_source_dir(self, rt_dataset, monkeypatch):
        make_pair(rt_dataset, "a", "person")
        dialog = rt_dialog.RenameDialog()
        monkeypatch.setattr(
            QtWidgets.QFileDialog,
            "getExistingDirectory",
            staticmethod(lambda *args, **kwargs: rt_dataset),
        )
        dialog.pick_source_dir()
        assert dialog.source_dir() == rt_dataset
        assert dialog.execute_button.isEnabled() is True

    def test_pick_source_dir_picks_no_output(
        self, rt_parent, monkeypatch
    ):
        """The picker only exists for the source folder."""

        _parent, data = rt_parent
        make_pair(data, "a", "person")
        dialog = rt_dialog.RenameDialog()
        monkeypatch.setattr(
            QtWidgets.QFileDialog,
            "getExistingDirectory",
            staticmethod(lambda *args, **kwargs: data),
        )
        dialog.pick_source_dir()
        assert dialog.source_dir() == data
        assert not hasattr(dialog, "output_dir")

    def test_cancelled_pick_keeps_the_source(self, rt_dataset, monkeypatch):
        dialog = _dialog(rt_dataset)
        monkeypatch.setattr(
            QtWidgets.QFileDialog,
            "getExistingDirectory",
            staticmethod(lambda *args, **kwargs: ""),
        )
        dialog.pick_source_dir()
        assert dialog.source_dir() == rt_dataset

    def test_choosing_a_folder_clears_the_old_display(self, rt_make):
        first = rt_make()
        make_pair(first, "a", "person")
        second = rt_make()
        make_pair(second, "b", "person")
        dialog = _dialog(first)
        assert "a.jpg -> person_1.jpg" in _lines(dialog)
        dialog.set_source_dir(second)
        lines = _lines(dialog)
        assert "b.jpg -> person_1.jpg" in lines
        assert "a.jpg -> person_1.jpg" not in lines


class TestDragAndDrop:
    """Dropping a folder opens it; dropping nothing else exports."""

    def test_drop_folder_sets_the_source(self, rt_parent):
        _parent, data = rt_parent
        make_pair(data, "a", "person")
        dialog = rt_dialog.RenameDialog()
        event, state = _drop_event(data)
        dialog.dropEvent(event)
        assert dialog.source_dir() == os.path.abspath(data)
        assert dialog.source_label.text() == os.path.abspath(data)
        assert dialog.execute_button.isEnabled() is True
        assert state["accepted"] is True

    def test_drop_file_keeps_the_source(self, rt_dataset):
        path = write_image(rt_dataset, "a.jpg")
        dialog = rt_dialog.RenameDialog()
        event, state = _drop_event(path)
        dialog.dropEvent(event)
        assert dialog.source_dir() == ""
        assert "仅支持文件夹" in dialog.status_bar.currentMessage()
        assert dialog.execute_button.isEnabled() is False
        assert state["ignored"] is True

    def test_drop_file_beside_a_folder_is_reported(self, rt_dataset):
        path = write_image(rt_dataset, "a.txt", b"x")
        dialog = rt_dialog.RenameDialog()
        event, _state = _drop_event(rt_dataset, path)
        dialog.dropEvent(event)
        assert dialog.source_dir() == os.path.abspath(rt_dataset)
        message = dialog.status_bar.currentMessage()
        assert message == "已忽略: a.txt（仅支持文件夹）"

    def test_drop_extra_folder_is_reported(self, rt_parent):
        parent, data = rt_parent
        dialog = rt_dialog.RenameDialog()
        event, _state = _drop_event(data, parent)
        dialog.dropEvent(event)
        assert dialog.source_dir() == os.path.abspath(data)
        message = dialog.status_bar.currentMessage()
        assert "已忽略" in message
        assert "仅取第一个文件夹" in message

    def test_drop_never_exports(self, rt_parent):
        parent, data = rt_parent
        make_pair(data, "a", "person")
        dialog = rt_dialog.RenameDialog()
        event, _state = _drop_event(data)
        dialog.dropEvent(event)
        assert dialog.source_dir() == data
        assert os.listdir(parent) == ["data"]

    def test_drop_then_click_exports_next_to_the_source(
        self, rt_parent, monkeypatch
    ):
        parent, data = rt_parent
        make_pair(data, "a", "person")
        _capture(monkeypatch, "information")
        dialog = rt_dialog.RenameDialog()
        event, _state = _drop_event(data)
        dialog.dropEvent(event)
        result = dialog.execute()
        path = os.path.join(parent, _zip_name(data))
        assert result["zip_path"] == path
        assert os.path.isfile(path)
        assert sorted(os.listdir(data)) == ["a.jpg", "a.json"]

    def test_drag_enter_accepts_a_folder(self, rt_dataset):
        dialog = rt_dialog.RenameDialog()
        event, state = _drop_event(rt_dataset)
        dialog.dragEnterEvent(event)
        assert state["accepted"] is True
        assert state["ignored"] is False

    def test_drag_enter_rejects_a_file(self, rt_dataset):
        path = write_image(rt_dataset, "a.jpg")
        dialog = rt_dialog.RenameDialog()
        event, state = _drop_event(path)
        dialog.dragEnterEvent(event)
        assert state["ignored"] is True
        assert state["accepted"] is False

    def test_drag_move_accepts_a_folder(self, rt_dataset):
        dialog = rt_dialog.RenameDialog()
        event, state = _drag_event(rt_dataset)
        dialog.dragMoveEvent(event)
        assert state["accepted"] is True
        assert state["ignored"] is False

    def test_drag_move_rejects_a_file(self, rt_dataset):
        path = write_image(rt_dataset, "a.jpg")
        dialog = rt_dialog.RenameDialog()
        event, state = _drag_event(path)
        dialog.dragMoveEvent(event)
        assert state["ignored"] is True
        assert state["accepted"] is False

    def test_drop_entries_of_an_empty_mime(self, qapp):
        dialog = rt_dialog.RenameDialog()
        assert dialog.drop_entries(None) == ([], [])
        assert dialog.drop_entries(QtCore.QMimeData()) == ([], [])


class TestParseOutput:
    """The display shows the summary, the ignored count and the plan."""

    def test_summary_line(self, rt_parent):
        _parent, data = rt_parent
        make_pair(data, "a", "person")
        write_image(data, "b.png")
        write_json(data, "c.json", shapes=[])
        write_image(data, "classes.txt", b"person\n")
        dialog = _dialog(data)
        lines = _lines(dialog)
        assert lines[0] == (
            "共 5 个文件：图片 2、成对 json 1、其他 1、忽略 1（原样镜像 1）"
        )

    def test_ignored_line_reports_the_count_only(self, rt_parent):
        _parent, data = rt_parent
        make_pair(data, "a", "person")
        write_json(data, "c.json", shapes=[])
        dialog = _dialog(data)
        lines = _lines(dialog)
        assert "忽略 1 个无同名图片的 json（不写入 zip）" in lines
        assert not any(line.startswith("忽略") and "c.json" in line
                       for line in lines)

    def test_blocked_display(self, rt_parent):
        _parent, data = rt_parent
        make_pair(data, "a", "person")
        write_image(data, "b.png")
        write_json(data, "c.json", shapes=[])
        make_pair(data, "d", "person")
        with open(os.path.join(data, "d.json"), "w",
                  encoding="utf-8") as handle:
            handle.write("{oops")
        os.makedirs(os.path.join(data, "sub"))
        dialog = _dialog(data)
        lines = _lines(dialog)
        assert "发现 3 处阻塞，不能重命名，未写入任何文件（含 .part）：" in (
            lines
        )
        assert "图片缺少同名 json（1 张）：" in lines
        assert lines[lines.index("图片缺少同名 json（1 张）：") + 1] == (
            "b.png"
        )
        assert "其他问题：" in lines
        assert any("无法解析" in line for line in lines)
        assert any("子目录" in line for line in lines)

    def test_blocked_button_is_disabled(self, rt_parent):
        _parent, data = rt_parent
        make_pair(data, "a", "person")
        write_image(data, "b.png")
        dialog = _dialog(data)
        assert dialog._plan is not None
        assert dialog._plan.blocked() is True
        assert dialog.execute_button.isEnabled() is False

    def test_no_blocker_line_for_a_healthy_folder(self, rt_parent):
        _parent, data = rt_parent
        make_pair(data, "a", "person")
        dialog = _dialog(data)
        lines = _lines(dialog)
        assert "待改名 2 个：" in lines
        assert "a.jpg -> person_1.jpg" in lines
        assert "a.json -> person_1.json" in lines
        assert not any("阻塞" in line for line in lines)
        assert dialog.execute_button.isEnabled() is True

    def test_pending_none_prints_a_notice(self, rt_parent):
        _parent, data = rt_parent
        make_pair(data, "person_1", "person")
        dialog = _dialog(data)
        lines = _lines(dialog)
        assert "没有需要改名的文件" in lines
        assert _renames(lines) == []
        assert dialog.execute_button.isEnabled() is True

    def test_empty_folder_prints_its_notice(self, rt_parent):
        _parent, data = rt_parent
        dialog = _dialog(data)
        lines = _lines(dialog)
        assert "目录为空：可以重命名（会得到一个 0 条目的 zip）" in lines
        assert dialog.execute_button.isEnabled() is True

    def test_unreadable_folder_is_reported(self, rt_parent):
        _parent, data = rt_parent
        dialog = _dialog(os.path.join(data, "gone"))
        lines = _lines(dialog)
        assert len(lines) == 1
        assert lines[0].startswith("无法解析目录：")
        assert dialog.execute_button.isEnabled() is False

    def test_unchanged_files_are_not_listed(self, rt_parent):
        _parent, data = rt_parent
        _dataset(data)
        dialog = _dialog(data)
        lines = _lines(dialog)
        for line in _renames(lines):
            for name in ("person_1.jpg", "person_1.json"):
                assert not line.startswith(name)
        assert "classes.txt" not in dialog.plain_text()

    def test_progress_helpers(self):
        assert _progress("a.jpg -> person_1.jpg    12/45") == "12/45"
        assert _progress("a.jpg -> person_1.jpg") == ""


class TestExecute:
    """One click writes the archive next to the source folder."""

    def test_click_writes_the_archive(self, rt_parent, monkeypatch):
        parent, data = rt_parent
        _dataset(data)
        seen = _capture(monkeypatch, "information")
        dialog = _dialog(data)
        before = snapshot(data)
        result = dialog.execute()
        path = os.path.join(parent, _zip_name(data))
        assert result["zip_path"] == path
        assert os.path.isfile(path)
        assert sorted(os.listdir(data)) == sorted(snapshot(data))
        assert sorted(os.listdir(parent)) == ["data", _zip_name(data)]
        entries = read_zip(path)
        assert sorted(entries) == [
            "classes.txt",
            "person_1.jpg",
            "person_1.json",
            "person_2.jpg",
            "person_2.json",
            "person_3.jpg",
            "person_3.json",
        ]
        assert zip_doc(path, "person_2.json")["imagePath"] == "person_2.jpg"
        assert entries["person_1.jpg"] == b"image:person_1.jpg"
        assert entries["person_1.json"] == _read(
            os.path.join(data, "person_1.json")
        )
        assert zip_doc(path, "person_3.json")["imagePath"] == "person_3.jpg"
        assert entries["classes.txt"] == b"person\n"
        assert seen and path in str(seen[0][2])
        assert snapshot(data) == before

    def test_write_log_lists_only_renamed_entries(
        self, rt_parent, monkeypatch
    ):
        _parent, data = rt_parent
        _dataset(data)
        _capture(monkeypatch, "information")
        dialog = _dialog(data)
        result = dialog.execute()
        lines = _lines(dialog)
        assert result["renamed"] == 4
        assert "a.jpg -> person_2.jpg" in _renames(lines)
        assert "a.json -> person_2.json" in _renames(lines)
        assert "b_aug1.jpg -> person_3.jpg" in _renames(lines)
        assert "b_aug1.json -> person_3.json" in _renames(lines)
        assert len(_renames(lines)) == 4
        assert "person_1.jpg" not in "\n".join(_renames(lines))

    def test_write_progress_counts_the_renames(
        self, rt_parent, monkeypatch
    ):
        _parent, data = rt_parent
        _dataset(data)
        _capture(monkeypatch, "information")
        dialog = _dialog(data)
        dialog.execute()
        progress = [
            (line, _progress(line)) for line in _lines(dialog)
            if _progress(line)
        ]
        assert len(progress) == _progress_count(dialog) == 4
        assert progress[0] == ("a.jpg -> person_2.jpg    1/4", "1/4")
        assert progress[1] == ("a.json -> person_2.json    2/4", "2/4")
        assert progress[2] == ("b_aug1.jpg -> person_3.jpg    3/4", "3/4")
        assert progress[3] == ("b_aug1.json -> person_3.json    4/4", "4/4")

    def test_write_summary_line(self, rt_parent, monkeypatch):
        _parent, data = rt_parent
        _dataset(data)
        _capture(monkeypatch, "information")
        dialog = _dialog(data)
        result = dialog.execute()
        lines = _lines(dialog)
        assert "已导出：%s" % result["zip_path"] in lines
        assert (
            "共 7 个文件：改名 4、保持原名 2、原样镜像 1、忽略 0" in lines
        )

    def test_summary_keeps_the_kept_name_despite_ignored(
        self, rt_parent, monkeypatch
    ):
        """The four buckets of the closing line add up to its total."""

        _parent, data = rt_parent
        make_pair(data, "person_1", "person")
        make_pair(data, "b", "person")
        write_json(data, "c.json", shapes=[])
        _capture(monkeypatch, "information")
        dialog = _dialog(data)
        result = dialog.execute()
        lines = _lines(dialog)
        assert result["files"] == 4
        assert result["renamed"] == 2
        assert result["ignored"] == 1
        closing = [
            line for line in lines if line.startswith("共 ")
            and "改名" in line
        ]
        assert closing == [
            "共 5 个文件：改名 2、保持原名 2、原样镜像 0、忽略 1"
        ]
        assert result["files"] + result["ignored"] == 5

    def test_write_bar_advances_on_renames_only(
        self, rt_parent, monkeypatch
    ):
        """The bar follows the renamed files, not the archive entries."""

        _parent, data = rt_parent
        make_pair(data, "person_1", "person")
        make_pair(data, "b", "person")
        _capture(monkeypatch, "information")
        dialog = _dialog(data)
        seen = []

        def _write(_plan, path, progress=None):
            dialog.progress.setRange(
                0, len(dialog._rename_pairs(_plan))
            )
            progress(1, 4, "person_1.jpg", "person_1.jpg", False)
            seen.append(dialog.progress.value())
            progress(2, 4, "person_1.json", "person_1.json", False)
            seen.append(dialog.progress.value())
            progress(3, 4, "person_2.jpg", "b.jpg", True)
            seen.append(dialog.progress.value())
            progress(4, 4, "person_2.json", "b.json", True)
            seen.append(dialog.progress.value())
            with open(path, "wb") as handle:
                handle.write(b"zip")
            return {
                "zip_path": path,
                "files": 4,
                "entries": [],
                "renamed": 2,
                "ignored": 0,
                "blockers": [],
                "source_untouched": True,
            }

        monkeypatch.setattr(rt_dialog.rename_core, "write_zip", _write)
        assert dialog.execute() is not None
        assert dialog.progress.maximum() == 2
        assert seen == [0, 0, 1, 2]
        assert dialog.progress.value() == 2

    def test_result_lines_survive_the_parse_lines(
        self, rt_parent, monkeypatch
    ):
        _parent, data = rt_parent
        _dataset(data)
        _capture(monkeypatch, "information")
        dialog = _dialog(data)
        dialog.execute()
        lines = _lines(dialog)
        assert "待改名 4 个：" in lines
        assert lines.count("a.jpg -> person_2.jpg") == 1

    def test_existing_archive_is_not_overwritten(
        self, rt_parent, monkeypatch
    ):
        parent, data = rt_parent
        _dataset(data)
        _capture(monkeypatch, "information")
        old = os.path.join(parent, _zip_name(data))
        with open(old, "wb") as handle:
            handle.write(b"old")
        before = snapshot(data)
        dialog = _dialog(data)
        result = dialog.execute()
        stem = os.path.splitext(_zip_name(data))[0]
        assert result["zip_path"] == os.path.join(parent, stem + "_2.zip")
        assert _read(old) == b"old"
        assert snapshot(data) == before

    def test_second_click_makes_a_second_archive(
        self, rt_parent, monkeypatch
    ):
        parent, data = rt_parent
        _dataset(data)
        _capture(monkeypatch, "information")
        dialog = _dialog(data)
        first = dialog.execute()
        second = dialog.execute()
        assert first["zip_path"] != second["zip_path"]
        assert second["zip_path"].endswith("_2.zip")
        assert os.path.isfile(first["zip_path"])
        assert os.path.isfile(second["zip_path"])
        assert os.path.dirname(first["zip_path"]) == parent
        assert os.path.dirname(second["zip_path"]) == parent

    def test_no_confirmation_question(self, rt_parent, monkeypatch):
        _parent, data = rt_parent
        _dataset(data)
        _capture(monkeypatch, "information")
        asked = []
        monkeypatch.setattr(
            QtWidgets.QMessageBox,
            "question",
            staticmethod(lambda *args, **kwargs: asked.append(args)),
        )
        dialog = _dialog(data)
        assert dialog.execute() is not None
        assert asked == []

    def test_write_failure_keeps_the_progress_and_reports(
        self, rt_parent, monkeypatch
    ):
        _parent, data = rt_parent
        make_pair(data, "a", "person")
        seen = _capture(monkeypatch, "warning")
        dialog = _dialog(data)

        def _boom(_plan, path, progress=None):
            part = path + core.PART_SUFFIX
            raise core.RenameError("坏了（半成品已保留：%s）" % part)

        monkeypatch.setattr(rt_dialog.rename_core, "write_zip", _boom)
        assert dialog.execute() is None
        assert seen and ".part" in str(seen[0][2])
        lines = _lines(dialog)
        assert not any(line.startswith("已导出：") for line in lines)
        assert any(line.startswith("重命名失败：") for line in lines)
        assert any(line.startswith("半成品已保留：") for line in lines)
        assert "重命名失败" in dialog.status_bar.currentMessage()

    def test_write_failure_reports_a_plain_error(
        self, rt_parent, monkeypatch
    ):
        _parent, data = rt_parent
        _dataset(data)
        _capture(monkeypatch, "warning")
        dialog = _dialog(data)

        def _boom(_plan, path, progress=None):
            raise core.RenameError("坏了")

        monkeypatch.setattr(rt_dialog.rename_core, "write_zip", _boom)
        assert dialog.execute() is None
        lines = _lines(dialog)
        assert "重命名失败：坏了" in lines
        assert not any(line.startswith("已导出：") for line in lines)

    def test_write_failure_keeps_the_progress_lines(
        self, rt_parent, monkeypatch
    ):
        _parent, data = rt_parent
        make_pair(data, "a", "person")
        make_pair(data, "b", "person")
        _capture(monkeypatch, "warning")
        dialog = _dialog(data)

        def _boom(plan, path, progress=None):
            if progress is not None:
                progress(1, 2, "person_1.jpg", "a.jpg", True)
            raise core.RenameError("坏了")

        monkeypatch.setattr(rt_dialog.rename_core, "write_zip", _boom)
        assert dialog.execute() is None
        lines = _lines(dialog)
        assert "a.jpg -> person_1.jpg    1/4" in lines
        assert any(line.startswith("重命名失败：") for line in lines)

    def test_scan_failure_is_reported(
        self, rt_parent, monkeypatch
    ):
        _parent, data = rt_parent
        _dataset(data)
        seen = _capture(monkeypatch, "warning")
        dialog = _dialog(data)

        def _boom(_directory, progress=None):
            raise core.RenameError("目录读不了")

        monkeypatch.setattr(
            rt_dialog.rename_core, "plan_directory", _boom
        )
        dialog.set_source_dir(data)
        lines = _lines(dialog)
        assert len(lines) == 1
        assert lines[0] == "无法解析目录：目录读不了"
        assert dialog.execute_button.isEnabled() is False
        assert dialog.execute() is None
        assert seen


class TestBlockedFolder:
    """A blocked folder warns, writes nothing and cannot be executed."""

    def test_missing_json_blocks_the_button(self, rt_parent, monkeypatch):
        parent, data = rt_parent
        write_image(data, "a.jpg")
        seen = _capture(monkeypatch, "warning")
        dialog = _dialog(data)
        before = snapshot(data)
        before_parent = snapshot(parent)
        assert dialog.execute_button.isEnabled() is False
        assert dialog.execute() is None
        assert seen == []
        assert snapshot(parent) == before_parent
        assert snapshot(data) == before
        assert os.listdir(parent) == ["data"]

    def test_illegal_entry_name_writes_nothing(
        self, rt_parent, monkeypatch
    ):
        parent, data = rt_parent
        make_pair(data, "a", "person")
        write_image(data, "v1..2.txt", b"x")
        _capture(monkeypatch, "warning")
        dialog = _dialog(data)
        assert dialog.execute() is None
        assert os.listdir(parent) == ["data"]
        assert any("条目名非法" in line for line in _lines(dialog))

    def test_without_source_warns(self, monkeypatch):
        seen = _capture(monkeypatch, "warning")
        dialog = rt_dialog.RenameDialog()
        assert dialog.execute() is None
        assert seen

    def test_without_parent_warns(self, rt_parent, monkeypatch):
        """A source without a parent folder cannot be exported."""

        parent, data = rt_parent
        make_pair(data, "a", "person")
        before_parent = snapshot(parent)
        before_data = snapshot(data)
        seen = _capture(monkeypatch, "warning")
        monkeypatch.setattr(rt_dialog.osp, "dirname", lambda _path: "")
        dialog = rt_dialog.RenameDialog()
        dialog.source_label.setText(data)
        dialog.parse_source()
        assert dialog.execute() is None
        assert seen
        assert rt_dialog.NO_PARENT_TEXT in str(seen[0][2])
        assert snapshot(parent) == before_parent
        assert snapshot(data) == before_data

    def test_root_source_warns_and_writes_nothing(
        self, rt_parent, monkeypatch
    ):
        """A filesystem root has no parent: warn, write nothing."""

        parent, _data = rt_parent
        before_parent = snapshot(parent)
        seen = _capture(monkeypatch, "warning")
        dialog = rt_dialog.RenameDialog()
        dialog.source_label.setText("/")
        assert dialog.execute() is None
        assert seen
        assert rt_dialog.NO_PARENT_TEXT in str(seen[0][2])
        assert snapshot(parent) == before_parent
        assert dialog.plain_text() == ""

    def test_status_bar_matches_the_displayed_blocker_count(
        self, rt_parent, monkeypatch
    ):
        """B1 counts once per image in both places."""

        _parent, data = rt_parent
        write_image(data, "a.jpg")
        write_image(data, "b.jpg")
        _capture(monkeypatch, "warning")
        dialog = _dialog(data)
        assert "发现 2 处阻塞" in dialog.plain_text()
        assert dialog.execute_button.isEnabled() is False
        assert dialog.execute() is None
        assert "发现 2 处阻塞" in dialog.status_bar.currentMessage()

    def test_ignored_json_does_not_block(self, rt_parent, monkeypatch):
        _parent, data = rt_parent
        make_pair(data, "a", "person")
        write_json(data, "c.json", shapes=[])
        _capture(monkeypatch, "information")
        dialog = _dialog(data)
        assert dialog.execute_button.isEnabled() is True
        result = dialog.execute()
        assert result is not None
        assert "c.json" not in read_zip(result["zip_path"])


class TestB1B2Example:
    """The acceptance folder: a.jpg+a.json, b.png, c.json, classes.txt."""

    def _folder(self, data):
        make_pair(data, "a", "person")
        write_image(data, "b.png")
        write_json(data, "c.json", shapes=[])
        write_image(data, "classes.txt", b"person\n")

    def test_plan_contract(self, rt_parent):
        _parent, data = rt_parent
        self._folder(data)
        plan = core.resolve_targets(core.plan_directory(data))
        assert plan.blockers == ["1 张图片缺少同名 json：b.png"]
        assert plan.ignored == ["c.json"]
        assert plan.mirrored == ["classes.txt"]
        assert core.check_entry_names(plan) == []
        # entries() also names the image of a blocked item: the plan
        # mirrors every existing top level file. The dialog never
        # writes such a plan (the button stays disabled).
        assert plan.entries() == [
            ("classes.txt", "classes.txt"),
            ("a.jpg", "person_1.jpg"),
            ("a.json", "person_1.json"),
            ("b.png", "b.png"),
        ]
        assert plan.total_files() == 4
        assert plan.stats() == {
            "total": 5,
            "images": 2,
            "json": 1,
            "ignored": 1,
            "other": 1,
        }

    def test_blocked_end_to_end(self, rt_parent, monkeypatch, rt_out):
        parent, data = rt_parent
        self._folder(data)
        seen = _capture(monkeypatch, "warning")
        dialog = _dialog(data)
        text = dialog.plain_text()
        assert (
            "共 5 个文件：图片 2、成对 json 1、其他 1、忽略 1（原样镜像 1）"
            in text
        )
        assert "忽略 1 个无同名图片的 json（不写入 zip）" in text
        assert "发现 1 处阻塞" in text
        assert "图片缺少同名 json（1 张）：" in text
        assert "b.png" in text
        assert dialog.execute_button.isEnabled() is False
        assert dialog.execute() is None
        assert seen == []
        assert os.listdir(parent) == ["data"]
        assert os.listdir(rt_out) == []

    def test_fixed_folder_end_to_end(self, rt_parent, monkeypatch):
        parent, data = rt_parent
        self._folder(data)
        write_json(data, "b.json", shapes=[{"label": "person"}],
                   image_path="b.png")
        _capture(monkeypatch, "information")
        dialog = _dialog(data)
        lines = _lines(dialog)
        assert dialog.execute_button.isEnabled() is True
        assert "待改名 4 个：" in lines
        assert "a.jpg -> person_1.jpg" in lines
        assert "b.png -> person_2.png" in lines
        assert "b.json -> person_2.json" in lines
        result = dialog.execute()
        assert result is not None
        entries = read_zip(result["zip_path"])
        assert "c.json" not in entries
        assert zip_doc(result["zip_path"], "person_2.json")["imagePath"] == (
            "person_2.png"
        )
        progress = [line for line in _lines(dialog) if _progress(line)]
        assert len(progress) == 4
        assert progress[-1].endswith("4/4")
        assert sorted(os.listdir(parent)) == sorted(["data", _zip_name(data)])
