"""End to end tests of the crop dialog, without touching a screen."""

import os
from types import SimpleNamespace

import pytest

from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtTest import QTest

from anylabeling.custom.crop_tool import crop_core as core
from anylabeling.custom.crop_tool import dialog as ct_dialog

from conftest import crop_names, snapshot, write_pil

SIZE = (80, 60)
BOX = (12, 9)


def _make_dialog(settings, source=None):
    """Build a dialog on a private parameter store."""

    dialog = ct_dialog.CropDialog(None, settings)
    dialog.resize(900, 600)
    if source is not None:
        dialog.set_input_dir(source)
    return dialog


def _question(monkeypatch, answer):
    """Stub QMessageBox.question and collect its calls."""

    seen = []

    def _slot(*args, **kwargs):
        seen.append(args)
        return answer

    monkeypatch.setattr(
        QtWidgets.QMessageBox, "question", staticmethod(_slot)
    )
    return seen


def _warning(monkeypatch):
    """Stub QMessageBox.warning and collect its calls."""

    seen = []

    def _slot(*args, **kwargs):
        seen.append(args)
        return QtWidgets.QMessageBox.StandardButton.Ok

    monkeypatch.setattr(
        QtWidgets.QMessageBox, "warning", staticmethod(_slot)
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


def _key_event(key):
    """Build a key press event with no modifier."""

    return QtGui.QKeyEvent(
        QtCore.QEvent.Type.KeyPress, key, QtCore.Qt.KeyboardModifier.NoModifier
    )


def _show(dialog):
    """Show a dialog and let Qt settle the focus of its widgets."""

    dialog.show()
    dialog.activateWindow()
    QtWidgets.QApplication.processEvents()
    return dialog


def _press(widget, key):
    """Give a widget the real focus and deliver one real key to it.

    The key is handed to the very widget Qt gave the focus to, so the
    shortcut travels through the event delivery of the application
    instead of a direct call of a handler of the dialog. A widget that
    did not take the focus would make the check meaningless, so the
    focus is asserted first.
    """

    widget.setFocus()
    QtWidgets.QApplication.processEvents()
    assert QtWidgets.QApplication.focusWidget() is widget
    QTest.keyClick(widget, key)
    QtWidgets.QApplication.processEvents()


@pytest.fixture
def ct_dialog_factory(ct_store):
    """Return a factory of dialogs sharing one scratch store."""

    settings, _ini = ct_store

    def _factory(source=None):
        return _make_dialog(settings, source)

    return _factory


class TestLayout:
    """The window is built the way the plan describes it."""

    def test_title_and_size(self, ct_store):
        settings, _ini = ct_store
        dialog = _make_dialog(settings)
        assert dialog.windowTitle() == ct_dialog.WINDOW_TITLE
        assert ct_dialog.WINDOW_SIZE == (1100, 720)
        assert ct_dialog.MIN_WINDOW_SIZE == (760, 520)
        assert dialog.minimumWidth() == ct_dialog.MIN_WINDOW_SIZE[0]

    def test_splitter_holds_the_list_then_the_view(self, ct_store):
        settings, _ini = ct_store
        dialog = _make_dialog(settings)
        assert dialog.splitter.count() == 2
        assert dialog.splitter.widget(0) is dialog.list
        assert dialog.splitter.widget(1) is dialog.viewer
        assert dialog.splitter.sizes()[0] == ct_dialog.LIST_MIN_WIDTH
        assert dialog.splitter.childrenCollapsible() is False
        assert dialog.splitter.handleWidth() == 1
        assert dialog.list.minimumWidth() == ct_dialog.LIST_MIN_WIDTH

    def test_view_and_list_belong_to_the_dialog(self, ct_store):
        settings, _ini = ct_store
        dialog = _make_dialog(settings)
        assert dialog.viewer.parentWidget() is dialog.splitter
        assert dialog.list.parentWidget() is dialog.splitter
        assert dialog.splitter.parentWidget() is dialog

    def test_drops_reach_the_dialog_only(self, ct_store):
        settings, _ini = ct_store
        dialog = _make_dialog(settings)
        assert dialog.acceptDrops() is True
        assert dialog.viewer.acceptDrops() is False
        assert dialog.list.acceptDrops() is False

    def test_list_never_takes_the_keyboard(self, ct_store):
        settings, _ini = ct_store
        dialog = _make_dialog(settings)
        assert dialog.list.focusPolicy() == QtCore.Qt.FocusPolicy.NoFocus

    def test_hint_is_permanent(self, ct_store):
        settings, _ini = ct_store
        dialog = _make_dialog(settings)
        assert dialog.hint_label.text() == ct_dialog.HINT_TEXT

    def test_hint_names_the_two_shortcuts(self):
        assert "A/D" in ct_dialog.HINT_TEXT
        assert "右键" in ct_dialog.HINT_TEXT
        assert "A/D" in ct_dialog.KEYS_TEXT
        assert "右键" in ct_dialog.KEYS_TEXT

    def test_no_return_binding_in_the_sources(self):
        for module in (ct_dialog,):
            with open(module.__file__, encoding="utf-8") as handle:
                text = handle.read()
            assert "Key_Return" not in text
            assert "Key_Enter" not in text

    def test_no_colour_control(self, ct_store):
        settings, _ini = ct_store
        dialog = _make_dialog(settings)
        assert not hasattr(dialog, "colour_box")
        assert not hasattr(dialog, "fill_box")
        for name in ("颜色", "填充"):
            assert name not in dialog.hint_label.text()

    def test_status_bar_carries_the_shortcuts(self, ct_store):
        settings, _ini = ct_store
        dialog = _make_dialog(settings)
        assert dialog.keys_label.text() == ct_dialog.KEYS_TEXT
        assert dialog.coord_label.text() == ct_dialog.COORD_FORMAT % (0, 0)
        assert dialog.count_label.text() == ct_dialog.COUNT_FORMAT % 0
        assert dialog.box_label.text() == ct_dialog.BOX_FORMAT % (640, 640)

    def test_status_bar_shows_both_folders(self, ct_dir, ct_store):
        settings, _ini = ct_store
        dialog = _make_dialog(settings, ct_dir)
        assert dialog.input_path_label.toolTip() == ct_dir
        assert dialog.input_path_label.text() != ""
        assert dialog.output_path_label.toolTip() == dialog.output_dir()
        assert dialog.input_label.text() == ct_dir

    def test_buttons_never_become_the_default(self, ct_store):
        settings, _ini = ct_store
        dialog = _make_dialog(settings)
        for button in dialog.findChildren(QtWidgets.QPushButton):
            assert button.autoDefault() is False


class TestDrop:
    """A folder opens the dialog, anything else is reported."""

    def test_drop_folder(self, ct_dir, ct_store):
        write_pil(ct_dir, "a10.png", (20, 20))
        write_pil(ct_dir, "a2.png", (20, 20))
        settings, _ini = ct_store
        dialog = _make_dialog(settings)
        event, state = _drop_event(ct_dir)
        dialog.dropEvent(event)
        assert dialog.input_dir() == os.path.abspath(ct_dir)
        assert dialog.input_label.text() == os.path.abspath(ct_dir)
        assert state["accepted"] is True
        assert dialog.image_count() == 2
        assert [entry.name for entry in dialog._entries] == [
            "a2.png",
            "a10.png",
        ]
        assert dialog.current_index() == 0
        assert dialog.current_path() == os.path.join(ct_dir, "a2.png")

    def test_drop_file_keeps_the_current_folder(
        self, ct_dir, ct_out, ct_store
    ):
        write_pil(ct_dir, "a.png", (20, 20))
        path = write_pil(ct_out, "loose.png", (20, 20))
        settings, _ini = ct_store
        dialog = _make_dialog(settings, ct_dir)
        dialog.set_output_dir(ct_out)
        event, state = _drop_event(path)
        dialog.dropEvent(event)
        assert dialog.input_dir() == ct_dir
        assert dialog.current_path() == os.path.join(ct_dir, "a.png")
        assert dialog.status_bar.currentMessage() == (
            ct_dialog.DROP_ONLY_FOLDERS
        )
        assert state["ignored"] is True

    def test_drop_extra_folders_are_reported(self, ct_dir, ct_make, ct_store):
        other = ct_make()
        write_pil(ct_dir, "a.png", (20, 20))
        write_pil(other, "b.png", (20, 20))
        settings, _ini = ct_store
        dialog = _make_dialog(settings)
        event, _state = _drop_event(ct_dir, other)
        dialog.dropEvent(event)
        assert dialog.input_dir() == os.path.abspath(ct_dir)
        message = dialog.status_bar.currentMessage()
        assert message.startswith(ct_dialog.DROP_IGNORED_PREFIX)
        assert ct_dialog.DROP_EXTRA_FOLDER_REASON in message

    def test_drop_of_an_empty_folder(self, ct_dir, ct_store):
        settings, _ini = ct_store
        dialog = _make_dialog(settings)
        event, _state = _drop_event(ct_dir)
        dialog.dropEvent(event)
        assert dialog.image_count() == 0
        assert dialog.status_bar.currentMessage() == (
            ct_dialog.NO_IMAGES_TEXT
        )

    def test_drag_enter_and_move(self, ct_dir, ct_store):
        write_pil(ct_dir, "a.png", (20, 20))
        settings, _ini = ct_store
        dialog = _make_dialog(settings)
        event, state = _drop_event(ct_dir)
        dialog.dragEnterEvent(event)
        assert state["accepted"] is True
        event, state = _drop_event(ct_dir)
        dialog.dragMoveEvent(event)
        assert state["accepted"] is True
        event, state = _drop_event(os.path.join(ct_dir, "a.png"))
        dialog.dragEnterEvent(event)
        assert state["ignored"] is True

    def test_drop_entries_without_urls(self, ct_store):
        settings, _ini = ct_store
        dialog = _make_dialog(settings)
        assert dialog.drop_entries(None) == ([], [])
        assert dialog.drop_entries(QtCore.QMimeData()) == ([], [])

    def test_ignored_text(self, ct_store):
        settings, _ini = ct_store
        dialog = _make_dialog(settings)
        text = dialog.ignored_text([("a.txt", ct_dialog.DROP_FILE_REASON)])
        assert text == "已忽略: a.txt（仅支持文件夹）"


class TestPick:
    """Both pickers only open what the user chose."""

    def test_pick_input_dir(self, ct_dir, ct_store, monkeypatch):
        write_pil(ct_dir, "a.png", (20, 20))
        settings, _ini = ct_store
        dialog = _make_dialog(settings)
        monkeypatch.setattr(
            QtWidgets.QFileDialog,
            "getExistingDirectory",
            staticmethod(lambda *args, **kwargs: ct_dir),
        )
        dialog.pick_input_dir()
        assert dialog.input_dir() == ct_dir
        assert dialog.image_count() == 1

    def test_cancelled_pick_keeps_the_folder(
        self, ct_dir, ct_store, monkeypatch
    ):
        write_pil(ct_dir, "a.png", (20, 20))
        settings, _ini = ct_store
        dialog = _make_dialog(settings, ct_dir)
        monkeypatch.setattr(
            QtWidgets.QFileDialog,
            "getExistingDirectory",
            staticmethod(lambda *args, **kwargs: ""),
        )
        dialog.pick_input_dir()
        assert dialog.input_dir() == ct_dir

    def test_pick_output_dir(self, ct_dir, ct_out, ct_store, monkeypatch):
        write_pil(ct_dir, "a.png", (20, 20))
        settings, _ini = ct_store
        dialog = _make_dialog(settings, ct_dir)
        monkeypatch.setattr(
            QtWidgets.QFileDialog,
            "getExistingDirectory",
            staticmethod(lambda *args, **kwargs: ct_out),
        )
        dialog.pick_output_dir()
        assert dialog.output_dir() == ct_out
        assert settings.output_dir() == ct_out
        assert dialog.output_label.text() == ct_out


class TestParameters:
    """The spin boxes drive the view and the store at once."""

    def test_stored_values_are_loaded(self, ct_store):
        settings, _ini = ct_store
        settings.set_width(320)
        settings.set_height(240)
        settings.set_pad_width(7)
        settings.set_pad_height(8)
        dialog = _make_dialog(settings)
        assert dialog.width_box.value() == 320
        assert dialog.height_box.value() == 240
        assert dialog.viewer.crop_size() == (320, 240)
        assert dialog.viewer.pad_size() == (7, 8)

    def test_defaults(self, ct_store):
        settings, _ini = ct_store
        dialog = _make_dialog(settings)
        assert dialog.width_box.value() == 640
        assert dialog.height_box.value() == 640
        assert dialog.box_label.text() == ct_dialog.BOX_FORMAT % (640, 640)

    def test_size_boxes_write_back(self, ct_dir, ct_store):
        write_pil(ct_dir, "a.png", SIZE)
        settings, _ini = ct_store
        dialog = _make_dialog(settings, ct_dir)
        dialog.width_box.setValue(BOX[0])
        dialog.height_box.setValue(BOX[1])
        assert dialog.viewer.crop_size() == BOX
        assert settings.width() == BOX[0]
        assert settings.height() == BOX[1]
        assert dialog.box_label.text() == ct_dialog.BOX_FORMAT % BOX

    def test_pad_boxes_write_back(self, ct_dir, ct_store):
        write_pil(ct_dir, "a.png", SIZE)
        settings, _ini = ct_store
        dialog = _make_dialog(settings, ct_dir)
        dialog.pad_x_box.setValue(5)
        dialog.pad_y_box.setValue(6)
        assert dialog.viewer.pad_size() == (5, 6)
        assert settings.pad_width() == 5
        assert settings.pad_height() == 6

    def test_ranges(self, ct_store):
        settings, _ini = ct_store
        dialog = _make_dialog(settings)
        assert dialog.width_box.minimum() == 1
        assert dialog.width_box.maximum() == core.SIZE_MAX
        assert dialog.pad_x_box.minimum() == 0
        assert dialog.pad_y_box.maximum() == core.SIZE_MAX

    def test_small_image_warning_text(self):
        assert ct_dialog.SMALL_IMAGE_TEXT == "图片小于裁切框，右侧/下侧填 0"


class TestCrop:
    """The right button path writes exactly one crop."""

    def test_crop_end_to_end(self, ct_dir, ct_out, ct_store):
        source = write_pil(ct_dir, "img.png", SIZE)
        settings, _ini = ct_store
        dialog = _make_dialog(settings, ct_dir)
        dialog.set_output_dir(ct_out)
        dialog.width_box.setValue(BOX[0])
        dialog.height_box.setValue(BOX[1])
        dialog.pad_x_box.setValue(2)
        assert dialog.current_index() == 0
        path = dialog.crop_current()
        assert path is not None
        assert os.path.dirname(path) == ct_out
        names = crop_names(ct_out)
        assert names == [os.path.basename(path)]
        record = core.parse_crop_name(names[0], ct_out)
        expected_x, expected_y = dialog.viewer.crop_pos()
        assert (record.x, record.y) == (expected_x, expected_y)
        assert (record.w, record.h) == BOX
        assert (record.pad_x, record.pad_y) == (2, 0)
        assert record.source_stem == "img"
        assert dialog.marks_of_current() == [
            (record.x, record.y, record.w, record.h)
        ]
        assert dialog.viewer.mark_count() == 1
        assert "✓1" in dialog.list.item(0).text()
        assert dialog.count_label.text() == ct_dialog.COUNT_FORMAT % 1
        assert dialog.current_index() == 0
        assert dialog.status_bar.currentMessage() == (
            ct_dialog.SAVED_FORMAT % names[0]
        )
        assert snapshot(ct_dir)[os.path.basename(source)]

    def test_second_crop_of_the_same_region_counts_up(
        self, ct_dir, ct_out, ct_store
    ):
        write_pil(ct_dir, "img.png", SIZE)
        settings, _ini = ct_store
        dialog = _make_dialog(settings, ct_dir)
        dialog.set_output_dir(ct_out)
        dialog.width_box.setValue(BOX[0])
        dialog.height_box.setValue(BOX[1])
        first = dialog.crop_current()
        second = dialog.crop_current()
        assert second != first
        assert os.path.basename(second).endswith("__2.png")
        assert crop_names(ct_out) == [
            os.path.basename(first),
            os.path.basename(second),
        ]
        assert "✓2" in dialog.list.item(0).text()
        assert dialog.marks_of_current() == [
            dialog.marks_of_current()[0],
            dialog.marks_of_current()[0],
        ]

    def test_explicit_coordinates_are_used(self, ct_dir, ct_out, ct_store):
        write_pil(ct_dir, "img.png", SIZE)
        settings, _ini = ct_store
        dialog = _make_dialog(settings, ct_dir)
        dialog.set_output_dir(ct_out)
        dialog.width_box.setValue(BOX[0])
        dialog.height_box.setValue(BOX[1])
        path = dialog.crop_current(3, 4)
        record = core.parse_crop_name(os.path.basename(path))
        assert (record.x, record.y) == (3, 4)

    def test_crop_without_an_image(self, ct_store):
        settings, _ini = ct_store
        dialog = _make_dialog(settings)
        assert dialog.crop_current() is None
        assert dialog.status_bar.currentMessage() == (
            ct_dialog.NO_IMAGE_TEXT
        )

    def test_crop_failure_warns_and_writes_nothing(
        self, ct_dir, ct_out, ct_store, monkeypatch
    ):
        write_pil(ct_dir, "img.png", SIZE)
        settings, _ini = ct_store
        dialog = _make_dialog(settings, ct_dir)
        dialog.set_output_dir(ct_out)
        seen = _warning(monkeypatch)
        monkeypatch.setattr(core, "MAX_CROP_PIXELS", 4)
        assert dialog.crop_current() is None
        assert len(seen) == 1
        assert seen[0][1] == ct_dialog.CROP_FAIL_TITLE
        assert "裁切尺寸过大" in seen[0][2]
        assert os.listdir(ct_out) == []
        assert dialog.marks_of_current() == []

    def test_view_signal_drives_the_crop(self, ct_dir, ct_out, ct_store):
        write_pil(ct_dir, "img.png", SIZE)
        settings, _ini = ct_store
        dialog = _make_dialog(settings, ct_dir)
        dialog.set_output_dir(ct_out)
        dialog.width_box.setValue(BOX[0])
        dialog.height_box.setValue(BOX[1])
        dialog.viewer.trigger_crop()
        assert len(crop_names(ct_out)) == 1


class TestMarks:
    """The marks come from the output folder, old crops included."""

    def test_old_crops_are_visible(self, ct_dir, ct_out, ct_store):
        write_pil(ct_dir, "img.png", SIZE)
        for index in (1, 2):
            name = core.build_crop_name(
                "img", index, index, 5, 5, 0, 0, ".png"
            )
            path = os.path.join(ct_out, name)
            with open(path, "w", encoding="utf-8") as out:
                out.write("x")
        settings, _ini = ct_store
        dialog = _make_dialog(settings, ct_dir)
        dialog.set_output_dir(ct_out)
        assert dialog.marks_of_current() == [(1, 1, 5, 5), (2, 2, 5, 5)]
        assert dialog.viewer.mark_count() == 2
        assert "✓2" in dialog.list.item(0).text()
        assert dialog.count_label.text() == ct_dialog.COUNT_FORMAT % 2

    def test_marks_follow_the_current_image(self, ct_dir, ct_out, ct_store):
        write_pil(ct_dir, "a.png", SIZE)
        write_pil(ct_dir, "b.png", SIZE)
        name = core.build_crop_name("b", 1, 1, 5, 5, 0, 0, ".png")
        with open(os.path.join(ct_out, name), "w", encoding="utf-8") as out:
            out.write("x")
        settings, _ini = ct_store
        dialog = _make_dialog(settings, ct_dir)
        dialog.set_output_dir(ct_out)
        assert dialog.marks_of_current() == []
        dialog.next_image()
        assert dialog.marks_of_current() == [(1, 1, 5, 5)]
        assert dialog.viewer.mark_count() == 1

    def test_foreign_files_are_not_marked(self, ct_dir, ct_out, ct_store):
        write_pil(ct_dir, "img.png", SIZE)
        with open(os.path.join(ct_out, "notes.txt"), "w") as out:
            out.write("x")
        settings, _ini = ct_store
        dialog = _make_dialog(settings, ct_dir)
        dialog.set_output_dir(ct_out)
        assert dialog.marks_of_current() == []
        assert dialog.viewer.mark_count() == 0


class TestOutputDir:
    """Changing the output folder rebuilds everything from the new one."""

    def test_badges_follow_the_new_folder(self, ct_dir, ct_make, ct_store):
        write_pil(ct_dir, "img.png", SIZE)
        write_pil(ct_dir, "other.png", SIZE)
        first_out = ct_make()
        second_out = ct_make()
        for index in (1, 3):
            name = core.build_crop_name(
                "img", index, index, 5, 5, 0, 0, ".png"
            )
            with open(os.path.join(first_out, name), "w") as out:
                out.write("x")
        name = core.build_crop_name("img", 9, 9, 5, 5, 0, 0, ".png")
        with open(os.path.join(second_out, name), "w") as out:
            out.write("x")
        settings, _ini = ct_store
        dialog = _make_dialog(settings, ct_dir)
        dialog.set_output_dir(first_out)
        assert "✓2" in dialog.list.item(0).text()
        assert "✓" not in dialog.list.item(1).text()
        assert dialog.marks_of_current() == [(1, 1, 5, 5), (3, 3, 5, 5)]
        dialog.set_output_dir(second_out)
        assert "✓1" in dialog.list.item(0).text()
        assert dialog.marks_of_current() == [(9, 9, 5, 5)]
        assert dialog.output_path_label.toolTip() == second_out

    def test_current_image_is_kept_by_path(self, ct_dir, ct_out, ct_store):
        write_pil(ct_dir, "a.png", SIZE)
        write_pil(ct_dir, "b.png", SIZE)
        write_pil(ct_dir, "c.png", SIZE)
        settings, _ini = ct_store
        dialog = _make_dialog(settings, ct_dir)
        assert dialog.show_image(2) is True
        current = dialog.current_path()
        dialog.set_output_dir(ct_out)
        assert dialog.current_path() == current
        assert dialog.current_index() == 2

    def test_rescan_without_a_folder(self, ct_dir, ct_store):
        write_pil(ct_dir, "a.png", SIZE)
        settings, _ini = ct_store
        dialog = _make_dialog(settings, ct_dir)
        settings.set_input_dir("")
        assert dialog.rescan() == []
        assert dialog.current_index() == -1

    def test_rescan_reports_an_empty_folder(self, ct_make, ct_store):
        empty = ct_make()
        settings, _ini = ct_store
        dialog = _make_dialog(settings, empty)
        assert dialog.rescan() == []
        assert dialog.status_bar.currentMessage() == (
            ct_dialog.NO_IMAGES_TEXT
        )


class TestNavigation:
    """A and D switch images and stop at both ends."""

    def test_next_and_prev(self, ct_dir, ct_store):
        write_pil(ct_dir, "a.png", SIZE)
        write_pil(ct_dir, "b.png", SIZE)
        write_pil(ct_dir, "c.png", SIZE)
        settings, _ini = ct_store
        dialog = _make_dialog(settings, ct_dir)
        assert dialog.current_index() == 0
        assert dialog.next_image() is True
        assert dialog.current_index() == 1
        assert dialog.next_image() is True
        assert dialog.current_index() == 2
        assert dialog.next_image() is False
        assert dialog.current_index() == 2
        assert dialog.prev_image() is True
        assert dialog.current_index() == 1
        assert dialog.prev_image() is True
        assert dialog.current_index() == 0
        assert dialog.prev_image() is False

    def test_index_label_tracks_the_image(self, ct_dir, ct_store):
        write_pil(ct_dir, "a.png", SIZE)
        write_pil(ct_dir, "b.png", SIZE)
        settings, _ini = ct_store
        dialog = _make_dialog(settings, ct_dir)
        assert dialog.index_label.text() == ct_dialog.INDEX_FORMAT % (1, 2)
        dialog.next_image()
        assert dialog.index_label.text() == ct_dialog.INDEX_FORMAT % (2, 2)

    def test_clicking_a_row_shows_that_image(self, ct_dir, ct_store):
        write_pil(ct_dir, "a.png", SIZE)
        write_pil(ct_dir, "b.png", SIZE)
        settings, _ini = ct_store
        dialog = _make_dialog(settings, ct_dir)
        dialog.list.setCurrentRow(1)
        assert dialog.current_index() == 1
        assert os.path.basename(dialog.current_path()) == "b.png"

    def test_cursor_label_is_updated(self, ct_dir, ct_store):
        write_pil(ct_dir, "a.png", SIZE)
        settings, _ini = ct_store
        dialog = _make_dialog(settings, ct_dir)
        dialog.viewer.cursor_moved.emit(5, 7)
        assert dialog.coord_label.text() == ct_dialog.COORD_FORMAT % (5, 7)


class TestDelete:
    """Delete asks first and then removes only its own crops."""

    def _two_crops(self, ct_dir, ct_out, ct_store):
        write_pil(ct_dir, "img.png", SIZE)
        settings, _ini = ct_store
        dialog = _make_dialog(settings, ct_dir)
        dialog.set_output_dir(ct_out)
        dialog.width_box.setValue(BOX[0])
        dialog.height_box.setValue(BOX[1])
        dialog.crop_current()
        dialog.crop_current()
        return dialog, sorted(os.listdir(ct_out))

    def test_no_crops(self, ct_dir, ct_out, ct_store, monkeypatch):
        write_pil(ct_dir, "img.png", SIZE)
        settings, _ini = ct_store
        dialog = _make_dialog(settings, ct_dir)
        dialog.set_output_dir(ct_out)
        seen = _question(monkeypatch, QtWidgets.QMessageBox.StandardButton.No)
        assert dialog.delete_marks() is None
        assert seen == []
        assert dialog.status_bar.currentMessage() == (
            ct_dialog.NO_CROPS_TEXT
        )

    def test_no_answer_changes_nothing(
        self, ct_dir, ct_out, ct_store, monkeypatch
    ):
        dialog, names = self._two_crops(ct_dir, ct_out, ct_store)
        seen = _question(monkeypatch, QtWidgets.QMessageBox.StandardButton.No)
        assert dialog.delete_marks() is None
        assert sorted(os.listdir(ct_out)) == names
        assert dialog.viewer.mark_count() == 2
        assert "✓2" in dialog.list.item(0).text()
        assert len(seen) == 1

    def test_confirmation_lists_every_crop(
        self, ct_dir, ct_out, ct_store, monkeypatch
    ):
        dialog, names = self._two_crops(ct_dir, ct_out, ct_store)
        seen = _question(monkeypatch, QtWidgets.QMessageBox.StandardButton.No)
        dialog.delete_marks()
        title, text = seen[0][1], seen[0][2]
        assert title == ct_dialog.DELETE_TITLE
        for name in names:
            assert os.path.join(ct_out, name) in text
        assert seen[0][4] == QtWidgets.QMessageBox.StandardButton.No

    def test_yes_answer_removes_them(
        self, ct_dir, ct_out, ct_store, monkeypatch
    ):
        dialog, names = self._two_crops(ct_dir, ct_out, ct_store)
        _question(monkeypatch, QtWidgets.QMessageBox.StandardButton.Yes)
        deleted, skipped = dialog.delete_marks()
        assert skipped == []
        assert sorted(os.path.basename(path) for path in deleted) == names
        assert os.listdir(ct_out) == []
        assert dialog.marks_of_current() == []
        assert dialog.viewer.mark_count() == 0
        assert dialog.list.item(0).text() == "img.png"
        assert dialog.count_label.text() == ct_dialog.COUNT_FORMAT % 0
        assert dialog.status_bar.currentMessage() == (
            ct_dialog.DELETED_FORMAT % 2
        )

    def test_file_outside_the_output_folder_is_skipped(
        self, ct_dir, ct_out, ct_make, ct_store, monkeypatch
    ):
        dialog, _names = self._two_crops(ct_dir, ct_out, ct_store)
        outside = ct_make()
        name = core.build_crop_name("img", 0, 0, 5, 5, 0, 0, ".png")
        path = os.path.join(outside, name)
        with open(path, "w", encoding="utf-8") as out:
            out.write("x")
        dialog._crops["img"].append(core.parse_crop_name(name, outside))
        _question(monkeypatch, QtWidgets.QMessageBox.StandardButton.Yes)
        deleted, skipped = dialog.delete_marks()
        assert len(deleted) == 2
        assert len(skipped) == 1
        assert skipped[0][0] == path
        assert os.path.exists(path) is True
        message = dialog.status_bar.currentMessage()
        assert message == ct_dialog.DELETED_SKIPPED_FORMAT % (
            2,
            1,
            skipped[0][1],
        )
        assert "跳过" in message


class TestKeyboard:
    """One key entry point drives the whole window."""

    def _three(self, ct_dir, ct_store):
        write_pil(ct_dir, "a.png", SIZE)
        write_pil(ct_dir, "b.png", SIZE)
        write_pil(ct_dir, "c.png", SIZE)
        settings, _ini = ct_store
        return _make_dialog(settings, ct_dir)

    def test_d_and_right_go_forward(self, ct_dir, ct_store):
        dialog = self._three(ct_dir, ct_store)
        for key in (QtCore.Qt.Key.Key_D, QtCore.Qt.Key.Key_Right):
            before = dialog.current_index()
            dialog.keyPressEvent(_key_event(key))
            assert dialog.current_index() == before + 1
        dialog.keyPressEvent(_key_event(QtCore.Qt.Key.Key_D))
        assert dialog.current_index() == 2
        dialog.keyPressEvent(_key_event(QtCore.Qt.Key.Key_D))
        assert dialog.current_index() == 2

    def test_a_and_left_go_back(self, ct_dir, ct_store):
        dialog = self._three(ct_dir, ct_store)
        dialog.show_image(2)
        for key in (QtCore.Qt.Key.Key_A, QtCore.Qt.Key.Key_Left):
            before = dialog.current_index()
            dialog.keyPressEvent(_key_event(key))
            assert dialog.current_index() == before - 1

    def test_return_and_enter_never_crop(self, ct_dir, ct_out, ct_store):
        write_pil(ct_dir, "img.png", SIZE)
        settings, _ini = ct_store
        dialog = _make_dialog(settings, ct_dir)
        dialog.set_output_dir(ct_out)
        dialog.width_box.setValue(BOX[0])
        dialog.height_box.setValue(BOX[1])
        before = snapshot(ct_out)
        for key in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter):
            dialog.keyPressEvent(_key_event(key))
        assert snapshot(ct_out) == before
        assert os.listdir(ct_out) == []

    def test_delete_key_runs_the_deletion(
        self, ct_dir, ct_out, ct_store, monkeypatch
    ):
        write_pil(ct_dir, "img.png", SIZE)
        settings, _ini = ct_store
        dialog = _make_dialog(settings, ct_dir)
        dialog.set_output_dir(ct_out)
        dialog.width_box.setValue(BOX[0])
        dialog.height_box.setValue(BOX[1])
        dialog.crop_current()
        _question(monkeypatch, QtWidgets.QMessageBox.StandardButton.Yes)
        dialog.keyPressEvent(_key_event(QtCore.Qt.Key.Key_Delete))
        assert os.listdir(ct_out) == []
        assert dialog.marks_of_current() == []

    def test_escape_closes(self, ct_dir, ct_store):
        dialog = self._three(ct_dir, ct_store)
        closed = []
        dialog.close = lambda: closed.append(True)
        dialog.keyPressEvent(_key_event(QtCore.Qt.Key.Key_Escape))
        assert closed == [True]

    def test_focus_on_a_spin_box_keeps_the_shortcuts(
        self, ct_dir, ct_store
    ):
        dialog = _show(self._three(ct_dir, ct_store))
        for box in (
            dialog.width_box,
            dialog.height_box,
            dialog.pad_x_box,
            dialog.pad_y_box,
        ):
            for key, start, expected in (
                (QtCore.Qt.Key.Key_D, 1, 2),
                (QtCore.Qt.Key.Key_Right, 1, 2),
                (QtCore.Qt.Key.Key_A, 1, 0),
                (QtCore.Qt.Key.Key_Left, 1, 0),
            ):
                dialog.show_image(start)
                _press(box, key)
                assert dialog.current_index() == expected

    def test_focus_on_the_view_keeps_the_shortcuts(self, ct_dir, ct_store):
        dialog = _show(self._three(ct_dir, ct_store))
        _press(dialog.viewer, QtCore.Qt.Key.Key_D)
        assert dialog.current_index() == 1
        _press(dialog.viewer, QtCore.Qt.Key.Key_A)
        assert dialog.current_index() == 0

    def test_focus_on_a_spin_box_still_takes_digits(
        self, ct_dir, ct_store
    ):
        dialog = _show(self._three(ct_dir, ct_store))
        editor = dialog.width_box.findChild(QtWidgets.QLineEdit)
        editor.selectAll()
        _press(dialog.width_box, QtCore.Qt.Key.Key_7)
        assert editor.text() == "7"
        assert dialog.current_index() == 0

    def test_focus_on_a_spin_box_still_commits_on_enter(
        self, ct_dir, ct_store
    ):
        dialog = _show(self._three(ct_dir, ct_store))
        editor = dialog.width_box.findChild(QtWidgets.QLineEdit)
        editor.selectAll()
        _press(dialog.width_box, QtCore.Qt.Key.Key_7)
        _press(dialog.width_box, QtCore.Qt.Key.Key_Return)
        assert dialog.width_box.value() == 7
        assert dialog.current_index() == 0

    def test_focus_on_a_spin_box_keeps_delete(
        self, ct_dir, ct_out, ct_store, monkeypatch
    ):
        write_pil(ct_dir, "img.png", SIZE)
        settings, _ini = ct_store
        dialog = _show(_make_dialog(settings, ct_dir))
        dialog.set_output_dir(ct_out)
        dialog.width_box.setValue(BOX[0])
        dialog.height_box.setValue(BOX[1])
        dialog.crop_current()
        assert crop_names(ct_out) != []
        seen = _question(monkeypatch, QtWidgets.QMessageBox.StandardButton.Yes)
        _press(dialog.pad_x_box, QtCore.Qt.Key.Key_Delete)
        assert len(seen) == 1
        assert os.listdir(ct_out) == []
        assert dialog.marks_of_current() == []

    def test_focus_on_a_spin_box_never_crops_on_enter(
        self, ct_dir, ct_out, ct_store
    ):
        write_pil(ct_dir, "img.png", SIZE)
        settings, _ini = ct_store
        dialog = _show(_make_dialog(settings, ct_dir))
        dialog.set_output_dir(ct_out)
        dialog.width_box.setValue(BOX[0])
        dialog.height_box.setValue(BOX[1])
        before = snapshot(ct_out)
        for box in (dialog.width_box, dialog.pad_y_box):
            for key in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter):
                _press(box, key)
        assert snapshot(ct_out) == before
        assert os.listdir(ct_out) == []

    def test_a_second_dialog_keeps_its_own_keys(self, ct_dir, ct_store):
        first = _show(self._three(ct_dir, ct_store))
        settings, _ini = ct_store
        second = _show(_make_dialog(settings, ct_dir))
        _press(second.height_box, QtCore.Qt.Key.Key_D)
        assert second.current_index() == 1
        assert first.current_index() == 0


class TestSourceStaysReadOnly:
    """Neither a crop nor a deletion ever touches the source folder."""

    def test_snapshot_is_identical(
        self, ct_dir, ct_out, ct_store, monkeypatch
    ):
        write_pil(ct_dir, "a.png", SIZE)
        write_pil(ct_dir, "b.png", SIZE)
        with open(os.path.join(ct_dir, "notes.txt"), "w") as out:
            out.write("keep me")
        before = snapshot(ct_dir)
        settings, _ini = ct_store
        dialog = _make_dialog(settings, ct_dir)
        dialog.set_output_dir(ct_out)
        dialog.width_box.setValue(BOX[0])
        dialog.height_box.setValue(BOX[1])
        dialog.crop_current()
        assert snapshot(ct_dir) == before
        _question(monkeypatch, QtWidgets.QMessageBox.StandardButton.Yes)
        dialog.delete_marks()
        assert snapshot(ct_dir) == before
        assert sorted(os.listdir(ct_dir)) == [
            "a.png",
            "b.png",
            "notes.txt",
        ]


class TestRestore:
    """The stored source folder reopens with the window."""

    def test_stored_folder_is_restored(self, ct_dir, ct_store):
        write_pil(ct_dir, "a.png", SIZE)
        settings, _ini = ct_store
        settings.set_input_dir(ct_dir)
        dialog = _make_dialog(settings)
        assert dialog.image_count() == 1
        assert dialog.current_index() == 0

    def test_missing_folder_shows_the_hint(self, ct_dir, ct_store):
        settings, _ini = ct_store
        settings.set_input_dir(os.path.join(ct_dir, "gone"))
        dialog = _make_dialog(settings)
        assert dialog.image_count() == 0
        assert dialog.status_bar.currentMessage() == (
            ct_dialog.BAD_DIR_TEXT % os.path.join(ct_dir, "gone")
        )

    def test_no_folder_shows_the_hint(self, ct_store):
        settings, _ini = ct_store
        dialog = _make_dialog(settings)
        assert dialog.image_count() == 0
        assert dialog.status_bar.currentMessage() == ct_dialog.EMPTY_HINT
        assert dialog.current_index() == -1
        assert dialog.crop_current() is None
