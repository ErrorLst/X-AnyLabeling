"""Tests of the rename dialog: layout, drag and drop, one click export."""

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


def _rows(dialog):
    """Return the table as a list of text rows."""

    table = dialog.table
    return [
        [table.item(row, col).text() for col in range(table.columnCount())]
        for row in range(table.rowCount())
    ]


def _row_map(dialog):
    """Return the table as a {first cell: row} mapping."""

    return {row[0]: row for row in _rows(dialog)}


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
    """The window carries the result table and one main button."""

    def test_title(self, qapp):
        dialog = rt_dialog.RenameDialog()
        assert dialog.windowTitle() == "重命名"

    def test_headers(self, qapp):
        dialog = rt_dialog.RenameDialog()
        headers = [
            dialog.table.horizontalHeaderItem(col).text()
            for col in range(dialog.table.columnCount())
        ]
        assert headers == ["原文件名", "主标签", "新文件名", "状态"]

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

    def test_stats_label_starts_empty(self, qapp):
        dialog = rt_dialog.RenameDialog()
        assert dialog.stats_label.text() == rt_dialog.NOT_RUN_TEXT

    def test_blocker_label_is_wrapped(self, qapp):
        dialog = rt_dialog.RenameDialog()
        assert dialog.blocker_label.wordWrap() is True
        assert dialog.blocker_label.text() == rt_dialog.NO_BLOCKER_TEXT

    def test_drops_are_accepted(self, qapp):
        dialog = rt_dialog.RenameDialog()
        assert dialog.acceptDrops() is True


class TestDefaults:
    """Choosing the source derives the export folder, nothing else."""

    def test_export_dir_is_the_source_parent(self, rt_parent):
        _parent, data = rt_parent
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


class TestBlockers:
    """A blocked folder warns and exports nothing at all."""

    def test_blocked_click_warns_and_writes_nothing(
        self, rt_parent, monkeypatch
    ):
        parent, data = rt_parent
        write_image(data, "a.jpg")
        seen = _capture(monkeypatch, "warning")
        dialog = _dialog(data)
        before = snapshot(data)
        before_parent = snapshot(parent)
        assert dialog.execute() is None
        assert seen
        assert "缺少同名 json" in str(seen[0][2])
        assert snapshot(parent) == before_parent
        assert snapshot(data) == before
        assert dialog.execute_button.isEnabled() is True

    def test_blocked_table_lists_every_file(
        self, rt_parent, monkeypatch
    ):
        _parent, data = rt_parent
        write_image(data, "a.jpg")
        write_image(data, "a.txt", b"text")
        write_image(data, ".hidden", b"x")
        _capture(monkeypatch, "warning")
        dialog = _dialog(data)
        assert dialog.execute() is None
        rows = _row_map(dialog)
        assert set(rows) == {"a.jpg", "a.txt", ".hidden"}
        assert rows["a.jpg"][2] == rt_dialog.EMPTY_TEXT
        assert rows["a.jpg"][3] == rt_dialog.STATUS_BLOCKED
        assert rows["a.txt"][3] == rt_dialog.STATUS_BLOCKED
        assert rows[".hidden"][3] == rt_dialog.STATUS_BLOCKED
        assert "缺少同名 json" in dialog.blocker_label.text()
        assert "待改名" in dialog.stats_label.text()

    def test_orphan_json_row_is_listed(self, rt_parent, monkeypatch):
        _parent, data = rt_parent
        make_pair(data, "a", "person")
        write_json(data, "c.json", shapes=[])
        _capture(monkeypatch, "warning")
        dialog = _dialog(data)
        assert dialog.execute() is None
        rows = _row_map(dialog)
        assert rows["c.json"][3] == rt_dialog.STATUS_BLOCKED
        assert "没有同名图片" in dialog.blocker_label.text()

    def test_second_image_of_a_collision_is_listed(
        self, rt_parent, monkeypatch
    ):
        parent, data = rt_parent
        make_pair(data, "a", "person", ext=".jpg")
        make_pair(data, "a", "person", ext=".png")
        _capture(monkeypatch, "warning")
        dialog = _dialog(data)
        assert dialog.execute() is None
        rows = _row_map(dialog)
        assert set(rows) == {"a.jpg", "a.json", "a.png"}
        assert rows["a.png"][3] == rt_dialog.STATUS_BLOCKED
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
        assert "条目名非法" in dialog.blocker_label.text()

    def test_unreadable_folder_warns(self, rt_parent, monkeypatch):
        parent, data = rt_parent
        seen = _capture(monkeypatch, "warning")
        dialog = _dialog(os.path.join(data, "gone"))
        assert dialog.execute() is None
        assert seen
        assert os.listdir(parent) == ["data"]

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
        dialog = _dialog(data)
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
        dialog = _dialog("/")
        assert dialog.execute() is None
        assert seen
        assert rt_dialog.NO_PARENT_TEXT in str(seen[0][2])
        assert snapshot(parent) == before_parent
        assert dialog.table.rowCount() == 0

    def test_blockers_are_capped(self, qapp):
        dialog = rt_dialog.RenameDialog()
        blockers = ["问题 %d" % index for index in range(10)]
        text = dialog.show_blockers(blockers)
        assert "问题 0" in text and "问题 7" in text
        assert "问题 8" not in text
        assert "共 10 条" in text


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
            "b_aug1.jpg",
            "b_aug1.json",
            "classes.txt",
            "person_1.jpg",
            "person_1.json",
            "person_2.jpg",
            "person_2.json",
        ]
        assert zip_doc(path, "person_2.json")["imagePath"] == "person_2.jpg"
        assert entries["person_1.jpg"] == b"image:person_1.jpg"
        assert entries["person_1.json"] == _read(
            os.path.join(data, "person_1.json")
        )
        assert entries["b_aug1.json"] == _read(
            os.path.join(data, "b_aug1.json")
        )
        assert entries["classes.txt"] == b"person\n"
        assert seen and path in str(seen[0][2])
        assert snapshot(data) == before

    def test_result_table_and_stats(self, rt_parent, monkeypatch):
        _parent, data = rt_parent
        _dataset(data)
        _capture(monkeypatch, "information")
        dialog = _dialog(data)
        dialog.execute()
        rows = _row_map(dialog)
        assert rows["a.jpg"][2] == "person_2.jpg"
        assert rows["a.jpg"][3] == rt_dialog.STATUS_RENAMED
        assert rows["person_1.jpg"][3] == rt_dialog.STATUS_ALREADY
        assert rows["b_aug1.jpg"][3] == rt_dialog.STATUS_ORPHAN
        assert rows["classes.txt"][3] == rt_dialog.STATUS_MIRRORED
        text = dialog.stats_label.text()
        assert "共 7 个文件" in text
        assert "已改名 1" in text
        assert "符合规范 1" in text
        assert "孤儿增强 1" in text
        assert "原样镜像 1" in text
        assert dialog.blocker_label.text() == rt_dialog.NO_BLOCKER_TEXT
        assert dialog.status_bar.currentMessage()

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

    def test_failure_reports_the_part_file(
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
        assert "重命名失败" in dialog.status_bar.currentMessage()
        rows = _row_map(dialog)
        assert set(rows) == {"a.jpg", "a.json"}
        assert rows["a.jpg"][2] == "person_1.jpg"
        assert rows["a.jpg"][3] == rt_dialog.STATUS_NOT_EXPORTED
        assert rows["a.json"][3] == rt_dialog.STATUS_NOT_EXPORTED
        assert dialog.stats_label.text() == rt_dialog.NOT_RUN_TEXT
        assert "导出失败" in dialog.blocker_label.text()

    def test_failure_keeps_no_success_look(
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
        statuses = [row[3] for row in _rows(dialog)]
        assert statuses
        assert set(statuses) == {rt_dialog.STATUS_NOT_EXPORTED}
        assert rt_dialog.STATUS_RENAMED not in statuses
        assert dialog.stats_label.text() == rt_dialog.NOT_RUN_TEXT
        assert dialog.blocker_label.text() == "导出失败：坏了"

    def test_scan_failure_drops_the_success_look(
        self, rt_parent, monkeypatch
    ):
        _parent, data = rt_parent
        _dataset(data)
        _capture(monkeypatch, "information")
        dialog = _dialog(data)
        assert dialog.execute() is not None
        assert rt_dialog.STATUS_RENAMED in [row[3] for row in _rows(dialog)]

        def _boom(_directory, progress=None):
            raise core.RenameError("目录读不了")

        seen = _capture(monkeypatch, "warning")
        monkeypatch.setattr(
            rt_dialog.rename_core, "plan_directory", _boom
        )
        assert dialog.execute() is None
        assert seen and "目录读不了" in str(seen[0][2])
        assert "重命名失败" in dialog.status_bar.currentMessage()
        rows = _row_map(dialog)
        assert rows["a.jpg"][2] == "person_2.jpg"
        statuses = [row[3] for row in _rows(dialog)]
        assert statuses
        assert set(statuses) == {rt_dialog.STATUS_NOT_EXPORTED}
        assert dialog.stats_label.text() == rt_dialog.NOT_RUN_TEXT
        assert "导出失败" in dialog.blocker_label.text()
