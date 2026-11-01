"""Tests of the window: inputs, summary, drop, convert and guards."""

import os.path as osp
from types import SimpleNamespace

from PyQt6 import QtCore, QtWidgets

from anylabeling.custom.yolo_to_labelme import dialog as y2l_dialog
from anylabeling.custom.yolo_to_labelme import yolo_core

from conftest import (
    write_classes,
    write_image,
    write_label,
    write_text,
)

_ALIVE = []


def _keep(*objects):
    _ALIVE.extend(objects)


def _dialog():
    dialog = y2l_dialog.YoloToLabelmeDialog()
    _keep(dialog)
    return dialog


def _drop_event(*paths):
    mime = QtCore.QMimeData()
    mime.setUrls([QtCore.QUrl.fromLocalFile(path) for path in paths])
    state = {"accepted": False, "ignored": False}
    event = SimpleNamespace(
        mimeData=lambda: mime,
        acceptProposedAction=lambda: state.update(accepted=True),
        ignore=lambda: state.update(ignored=True),
    )
    return event, state


class TestLayout:
    def test_title(self, qapp):
        dialog = _dialog()
        assert dialog.windowTitle() == y2l_dialog.WINDOW_TITLE

    def test_summary_is_read_only(self, qapp):
        dialog = _dialog()
        assert isinstance(dialog.summary, QtWidgets.QPlainTextEdit)
        assert dialog.summary.isReadOnly() is True
        assert dialog.summary.toPlainText() == ""

    def test_mode_items(self, qapp):
        dialog = _dialog()
        assert dialog.mode_combo.count() == 4
        assert dialog.mode_combo.currentData() == yolo_core.MODE_AUTO

    def test_initial_state(self, qapp):
        dialog = _dialog()
        assert dialog.convert_button.isEnabled() is False
        assert dialog.status_text() == y2l_dialog.INITIAL_STATUS


class TestScan:
    def test_source_without_classes(self, qapp, scratch):
        dialog = _dialog()
        dialog.set_source_dir(scratch)
        assert y2l_dialog.NO_CLASSES in dialog.summary_text()
        assert dialog.convert_button.isEnabled() is False

    def test_scan_enables_convert(self, qapp, scratch):
        write_image(scratch, "a.jpg")
        write_label(scratch, "a.txt", ["0 0.5 0.5 0.5 0.5"])
        classes = write_classes(scratch, ["a0"])
        dialog = _dialog()
        dialog.set_source_dir(scratch)
        dialog.set_class_file(classes)
        text = dialog.summary_text()
        assert "已配对：1 组" in text
        assert "检测框 1" in text
        assert dialog.convert_button.isEnabled() is True
        assert "已扫描" in dialog.status_text()

    def test_invalid_labels_are_reported(self, qapp, scratch):
        write_image(scratch, "a.jpg")
        write_label(scratch, "a.txt", ["1 0.5 0.5 0.5 0.5"])
        classes = write_classes(scratch, ["a0"])
        dialog = _dialog()
        dialog.set_source_dir(scratch)
        dialog.set_class_file(classes)
        assert "无效标注：1 个" in dialog.summary_text()
        assert dialog.convert_button.isEnabled() is False

    def test_collision_disables_convert(self, qapp, scratch):
        write_image(scratch, "a.jpg")
        write_image(scratch, "a.png")
        write_label(scratch, "a.txt", ["0 0.5 0.5 0.5 0.5"])
        classes = write_classes(scratch, ["a0"])
        dialog = _dialog()
        dialog.set_source_dir(scratch)
        dialog.set_class_file(classes)
        assert "输出冲突" in dialog.summary_text()
        assert dialog.convert_button.isEnabled() is False

    def test_mode_change_rescans(self, qapp, scratch):
        write_image(scratch, "a.jpg")
        write_label(
            scratch, "a.txt", ["0 0.1 0.1 0.2 0.2 0.3 0.3"]
        )
        classes = write_classes(scratch, ["a0"])
        dialog = _dialog()
        dialog.set_source_dir(scratch)
        dialog.set_class_file(classes)
        assert dialog.convert_button.isEnabled() is True
        index = dialog.mode_combo.findData(yolo_core.MODE_RECTANGLE)
        dialog.mode_combo.setCurrentIndex(index)
        assert dialog.convert_button.isEnabled() is False
        index = dialog.mode_combo.findData(yolo_core.MODE_AUTO)
        dialog.mode_combo.setCurrentIndex(index)
        assert dialog.convert_button.isEnabled() is True

    def test_coco_hint(self, qapp, scratch):
        write_text(scratch, "coco_detection.json", "{}")
        classes = write_classes(scratch, ["a0"])
        dialog = _dialog()
        dialog.set_source_dir(scratch)
        dialog.set_class_file(classes)
        assert "COCO" in dialog.summary_text()


class TestConvert:
    def test_convert_writes_json(self, qapp, scratch):
        write_image(scratch, "a.jpg")
        write_label(scratch, "a.txt", ["0 0.5 0.5 0.5 0.5"])
        classes = write_classes(scratch, ["a0"])
        dialog = _dialog()
        dialog.set_source_dir(scratch)
        dialog.set_class_file(classes)
        dialog.convert()
        assert osp.isfile(osp.join(scratch, "a.json"))
        assert "转换完成" in dialog.summary_text()
        assert "转换完成" in dialog.status_text()
        assert dialog.isEnabled() is True
        assert dialog.convert_button.isEnabled() is False

    def test_existing_json_needs_overwrite(self, qapp, scratch):
        write_image(scratch, "a.jpg")
        write_label(scratch, "a.txt", ["0 0.5 0.5 0.5 0.5"])
        write_text(scratch, "a.json", '{"sentinel": true}\n')
        classes = write_classes(scratch, ["a0"])
        dialog = _dialog()
        dialog.set_source_dir(scratch)
        dialog.set_class_file(classes)
        assert dialog.convert_button.isEnabled() is False
        assert "覆盖" in dialog.convert_button.toolTip()
        dialog.overwrite_check.setChecked(True)
        assert dialog.convert_button.isEnabled() is True
        dialog.convert()
        path = osp.join(scratch, "a.json")
        with open(path, "r", encoding="utf-8") as handle:
            assert '"sentinel"' not in handle.read()

    def test_no_modal_popup(self, qapp, scratch, monkeypatch):
        write_image(scratch, "a.jpg")
        write_label(scratch, "a.txt", ["0 0.5 0.5 0.5 0.5"])
        classes = write_classes(scratch, ["a0"])

        def _forbidden(*_args, **_kwargs):
            raise AssertionError("modal dialog opened")

        monkeypatch.setattr(
            QtWidgets.QMessageBox, "exec", staticmethod(_forbidden)
        )
        dialog = _dialog()
        dialog.set_source_dir(scratch)
        dialog.set_class_file(classes)
        dialog.convert()
        assert osp.isfile(osp.join(scratch, "a.json"))


class TestDropAndClose:
    def test_drop_routes_folder_and_classes(self, qapp, scratch):
        write_image(scratch, "a.jpg")
        write_label(scratch, "a.txt", ["0 0.5 0.5 0.5 0.5"])
        classes = write_classes(scratch, ["a0"])
        dialog = _dialog()
        event, state = _drop_event(scratch, classes)
        dialog.dropEvent(event)
        assert state["accepted"] is True
        assert dialog.source_edit.text() == osp.abspath(scratch)
        assert dialog.class_edit.text() == osp.abspath(classes)
        assert dialog.convert_button.isEnabled() is True

    def test_drop_ignores_other_files(self, qapp, scratch):
        other = write_text(scratch, "note.md", "x\n")
        dialog = _dialog()
        event, state = _drop_event(other)
        dialog.dropEvent(event)
        assert state["ignored"] is True
        assert dialog.source_edit.text() == ""

    def test_close_refused_while_busy(self, qapp):
        dialog = _dialog()
        dialog._busy = True
        dialog.close()
        assert dialog.status_text() == y2l_dialog.CLOSE_REFUSED
