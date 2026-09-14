"""Tests of the Qt layer: button, event filter, overlay and undo."""

import os
from types import SimpleNamespace

import numpy as np
import PIL.Image
import pytest
from PyQt6 import QtCore, QtGui, QtWidgets

from anylabeling.custom.smudge_tool import operations
from anylabeling.custom.smudge_tool import smudge_filter
from anylabeling.custom.smudge_tool import texture_fill

from conftest import (
    drag,
    image_bytes,
    move,
    press,
    release,
    send_key,
    send_mouse,
)


def _bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


def _same_image(pixmap, path):
    "Return True when a pixmap shows exactly the file on disk."

    with open(path, "rb") as handle:
        shown = QtGui.QPixmap()
        assert shown.loadFromData(handle.read())
    return pixmap.toImage() == shown.toImage()


def _fill(canvas, source, start, end):
    "Right click a source point, then drag a region and release it."

    press(canvas, source, QtCore.Qt.MouseButton.RightButton)
    drag(canvas, start, end)


def _is_mark(color):
    "Return True for a pixel painted with the green of the source mark."

    green = color.green()
    if green <= 120:
        return False
    return green > 2 * color.red() and green > 2 * color.blue()


def _mark_points(widget):
    "Return the pixels of a rendered widget that carry the source mark."

    image = widget.grab().toImage()
    found = []
    for y in range(image.height()):
        for x in range(image.width()):
            if _is_mark(image.pixelColor(x, y)):
                found.append((x, y))
    return found


def _override_shape():
    "Return the shape of the application override cursor, or None."

    cursor = QtWidgets.QApplication.overrideCursor()
    return None if cursor is None else cursor.shape()


def _drop_override_cursors():
    "Drop whatever a previous test left on the override cursor stack."

    while QtWidgets.QApplication.overrideCursor() is not None:
        QtWidgets.QApplication.restoreOverrideCursor()


def _third_png(st_scratch, st_image, name="other.png"):
    path = os.path.join(st_scratch, name)
    PIL.Image.fromarray(st_image[::-1], "RGB").save(path)
    return path


def test_install_adds_a_checkable_chinese_button(st_tool):
    widget = st_tool.widget
    action = st_tool.controller._action
    assert action is not None
    assert action.text() == "涂抹工具"
    assert action.isCheckable()
    assert "右键" in action.toolTip()
    assert action in widget.tools.actions()


def test_install_is_idempotent(make_widget):
    widget = make_widget()
    first = smudge_filter.install_smudge_tool(widget)
    second = smudge_filter.install_smudge_tool(widget)
    assert first is second
    assert widget.tools.actions().count(first._action) == 1


def test_install_without_a_canvas_returns_none():
    class Bare:
        pass

    assert smudge_filter.install_smudge_tool(Bare()) is None


def test_the_button_survives_a_toolbar_rebuild(st_tool):
    controller = st_tool.controller
    widget = st_tool.widget
    widget.tools.clear()
    assert controller._action not in widget.tools.actions()
    widget.populate_mode_actions()
    assert controller._action in widget.tools.actions()


def test_triggering_the_button_toggles_the_mode(st_tool):
    controller = st_tool.controller
    controller._action.trigger()
    assert controller._is_active() is True
    assert controller._cursor_overridden is True
    controller._action.trigger()
    assert controller._is_active() is False
    assert controller._cursor_overridden is False


def test_escaping_leaves_the_mode(st_tool):
    controller = st_tool.controller
    canvas = st_tool.widget.canvas
    controller._action.trigger()
    event = send_key(canvas, QtCore.Qt.Key.Key_Escape)
    assert event.isAccepted()
    assert controller._is_active() is False
    assert controller._action.isChecked() is False


def test_ctrl_z_is_taken_by_the_mode(st_tool):
    controller = st_tool.controller
    canvas = st_tool.widget.canvas
    controller._action.trigger()
    override = QtGui.QKeyEvent(
        QtCore.QEvent.Type.ShortcutOverride,
        QtCore.Qt.Key.Key_Z,
        QtCore.Qt.KeyboardModifier.ControlModifier,
    )
    QtWidgets.QApplication.sendEvent(canvas, override)
    assert override.isAccepted()
    event = send_key(
        canvas, QtCore.Qt.Key.Key_Z, QtCore.Qt.KeyboardModifier.ControlModifier
    )
    assert event.isAccepted()


def test_a_right_click_marks_the_source_point(st_tool):
    controller = st_tool.controller
    canvas = st_tool.widget.canvas
    controller._action.trigger()
    event = press(canvas, (20.0, 30.0), QtCore.Qt.MouseButton.RightButton)
    assert event.isAccepted()
    assert controller._source == (20.0, 30.0)
    assert controller._overlay._source == (20.0, 30.0)
    assert controller._overlay._source_box is None
    mark = controller._overlay._source
    # The context menu of the canvas opens on the release, so the
    # release of the right button has to be taken as well, and it
    # must not move the mark that the press just set.
    released = release(
        canvas, (20.0, 30.0), QtCore.Qt.MouseButton.RightButton
    )
    assert released.isAccepted()
    assert controller._source == mark


def test_a_right_click_outside_the_image_is_ignored(st_tool):
    controller = st_tool.controller
    canvas = st_tool.widget.canvas
    controller._action.trigger()
    press(canvas, (150.0, 150.0), QtCore.Qt.MouseButton.RightButton)
    assert controller._source is None
    assert any("图像范围" in message for message in st_tool.widget.messages)


def test_a_small_drag_only_explains_the_minimum(st_tool):
    controller = st_tool.controller
    canvas = st_tool.widget.canvas
    widget = st_tool.widget
    before = _bytes(widget.image_path)
    controller._action.trigger()
    drag(canvas, (20.0, 40.0), (29.0, 41.0))
    assert any("6 像素" in message for message in widget.messages)
    assert _bytes(widget.image_path) == before
    assert widget.dirty is False
    assert canvas.shapes == []
    assert controller._history == {}


def test_a_drag_without_a_source_point_asks_for_one(st_tool):
    controller = st_tool.controller
    canvas = st_tool.widget.canvas
    widget = st_tool.widget
    before = _bytes(widget.image_path)
    controller._action.trigger()
    drag(canvas, (10.0, 10.0), (50.0, 50.0))
    assert any("背景源点" in message for message in widget.messages)
    assert _bytes(widget.image_path) == before


def test_a_drag_fills_the_region_and_writes_the_file(st_tool):
    controller = st_tool.controller
    canvas = st_tool.widget.canvas
    widget = st_tool.widget
    assert widget.dirty is False
    was = np.array(PIL.Image.open(widget.image_path))
    controller._action.trigger()
    press(canvas, (60.0, 60.0), QtCore.Qt.MouseButton.RightButton)
    drag(canvas, (10.0, 10.0), (50.0, 40.0))
    is_now = np.array(PIL.Image.open(widget.image_path))
    roi = (10, 10, 50, 40)
    outside = np.ones(is_now.shape[:2], bool)
    outside[roi[1] : roi[3], roi[0] : roi[2]] = False
    # Only the region may move: a wrong array, or a whole image
    # written back, shows up here at the pixel level.
    assert np.array_equal(is_now[outside], was[outside])
    assert not np.array_equal(
        is_now[roi[1] : roi[3], roi[0] : roi[2]],
        was[roi[1] : roi[3], roi[0] : roi[2]],
    )
    assert widget.dirty is False
    assert canvas.shapes == []
    assert controller._history[widget.image_path]
    expected = operations.source_window(
        (60.0, 60.0), (40, 30), (canvas.pixmap.height(), canvas.pixmap.width())
    )
    assert controller._overlay._source_box == expected
    assert os.path.isdir(controller._backup_dir)
    assert os.path.isfile(controller._backups[widget.image_path])
    assert any("已完成涂抹修复" in message for message in widget.messages)
    # The canvas has to show the pixels of the file, not the ones
    # cached by the load that happened before the write.
    assert _same_image(canvas.pixmap, widget.image_path)


def test_a_reverse_drag_fills_the_same_region(st_tool):
    controller = st_tool.controller
    canvas = st_tool.widget.canvas
    widget = st_tool.widget
    was = np.array(PIL.Image.open(widget.image_path))
    controller._action.trigger()
    press(canvas, (60.0, 60.0), QtCore.Qt.MouseButton.RightButton)
    # The drag runs from the bottom right corner to the top left one, so
    # the region has to be built from the image point of the press on
    # every move: a box grown from its own top left corner would collapse
    # to a single pixel on the release and fill nothing.
    calls = []
    real_fill = texture_fill.fill_roi

    def spy(data, roi, window):
        calls.append(roi)
        return real_fill(data, roi, window)

    texture_fill.fill_roi = spy
    try:
        press(canvas, (50.0, 40.0))
        move(canvas, (10.0, 10.0))
        assert controller._roi == (10, 10, 50, 40)
        release(canvas, (10.0, 10.0))
    finally:
        texture_fill.fill_roi = real_fill
    assert calls == [(10, 10, 50, 40)]
    is_now = np.array(PIL.Image.open(widget.image_path))
    roi = (10, 10, 50, 40)
    outside = np.ones(is_now.shape[:2], bool)
    outside[roi[1] : roi[3], roi[0] : roi[2]] = False
    # Only the region may move, whatever the direction of the drag was.
    assert np.array_equal(is_now[outside], was[outside])
    assert not np.array_equal(
        is_now[roi[1] : roi[3], roi[0] : roi[2]],
        was[roi[1] : roi[3], roi[0] : roi[2]],
    )
    assert controller._history[widget.image_path]
    assert not any("6 像素" in message for message in widget.messages)


def _visible_to_its_owner(label):
    "Return True while the label is not hidden from the widget owning it."

    owner = label.parentWidget()
    if owner is None:
        return not label.isHidden()
    return label.isVisibleTo(owner)


def test_the_history_label_counts_the_steps(st_tool):
    controller = st_tool.controller
    canvas = st_tool.widget.canvas
    widget = st_tool.widget
    controller._action.trigger()
    label = controller._status_label
    assert label is not None
    # The label is a permanent widget of the status bar: hidden while
    # there is nothing to undo, visible afterwards, and the text counts
    # the steps of the image that is displayed.
    assert _visible_to_its_owner(label) is False
    _fill(canvas, (60.0, 60.0), (10.0, 10.0), (50.0, 40.0))
    assert label.text() == "已涂抹修复 · 可撤销 1 次"
    assert _visible_to_its_owner(label) is True
    _fill(canvas, (60.0, 60.0), (10.0, 40.0), (50.0, 70.0))
    assert len(controller._history[widget.image_path]) == 2
    assert label.text() == "已涂抹修复 · 可撤销 2 次"
    controller.set_mode(False)
    assert _visible_to_its_owner(label) is False


def test_ctrl_z_undoes_the_last_operation(st_tool):
    controller = st_tool.controller
    canvas = st_tool.widget.canvas
    widget = st_tool.widget
    before = _bytes(widget.image_path)
    controller._action.trigger()
    press(canvas, (85.0, 70.0), QtCore.Qt.MouseButton.RightButton)
    drag(canvas, (20.0, 20.0), (60.0, 60.0))
    assert controller._history[widget.image_path]
    send_key(
        canvas, QtCore.Qt.Key.Key_Z, QtCore.Qt.KeyboardModifier.ControlModifier
    )
    assert _bytes(widget.image_path) == before
    assert controller._history == {}
    assert any("已撤销一步" in message for message in widget.messages)


def test_the_history_survives_a_walk_through_the_folder(
    st_tool, st_scratch, st_image
):
    controller = st_tool.controller
    canvas = st_tool.widget.canvas
    widget = st_tool.widget
    first = widget.image_path
    other = _third_png(st_scratch, st_image)
    controller._action.trigger()
    press(canvas, (60.0, 60.0), QtCore.Qt.MouseButton.RightButton)
    drag(canvas, (10.0, 10.0), (50.0, 40.0))
    assert list(controller._history) == [first]
    # A real switch of the displayed file, through the canvas, the way
    # the labeling widget does it. The history of the folder survives.
    _switch_to(widget, controller, other)
    assert controller._source is None
    assert list(controller._history) == [first]
    _switch_to(widget, controller, first)
    assert list(controller._history) == [first]


def test_opening_a_folder_resets_the_history(st_tool):
    controller = st_tool.controller
    canvas = st_tool.widget.canvas
    widget = st_tool.widget
    controller._action.trigger()
    press(canvas, (60.0, 60.0), QtCore.Qt.MouseButton.RightButton)
    drag(canvas, (10.0, 10.0), (50.0, 40.0))
    assert controller._history
    widget.import_image_folder("C:/somewhere/else")
    assert controller._history == {}
    assert widget.folder_calls == [1]


def test_importing_the_folder_of_the_history_again_keeps_it(st_tool):
    """A walk away and back keeps the stack of the image.

    The model validation follow opens one staging file at a time, and
    the file list of the main window imports the folder of that file on
    every switch: importing the folder the history belongs to again is
    not a change of folder, and the steps of the image have to be there
    when the user comes back to it.
    """

    controller = st_tool.controller
    canvas = st_tool.widget.canvas
    widget = st_tool.widget
    folder = os.path.dirname(widget.image_path)
    controller._action.trigger()
    _fill(canvas, (85.0, 70.0), (10.0, 10.0), (50.0, 40.0))
    assert list(controller._history) == [widget.image_path]
    # The follow hands the folder of the file it just opened in; the
    # path is spelled the way a folder dialog would spell it, which is
    # not the way the first import handed it in.
    widget.import_image_folder(os.path.join(folder, "."))
    assert list(controller._history) == [widget.image_path]
    # The import asks the widget to load a file of that folder, so the
    # tool forgets which file it was showing; the history stays, and
    # the next event adopts the file on screen again -- another file of
    # the run, whose own bucket is empty while this one is kept.
    controller._adopt_file()
    assert controller._current_file == widget.image_path
    assert controller._can_undo() is True


def test_a_walk_to_another_image_and_back_keeps_the_undo(
    st_scratch, st_image, make_widget
):
    """The reported workflow: smudge, switch image, switch back, undo.

    The follow switches the file of the main window and the file list
    imports the folder of that file on the way; the steps of the image
    the user returns to have to be there, on screen and on disk.
    """

    first = os.path.join(st_scratch, "first.png")
    second = os.path.join(st_scratch, "second.png")
    PIL.Image.fromarray(st_image, "RGB").save(first)
    PIL.Image.fromarray(st_image[::-1], "RGB").save(second)
    widget = make_widget(image_path=first, image_data=_bytes(first))
    imported = []

    def import_image_folder(dirpath, pattern=None, load=True):
        imported.append(str(dirpath))
        return None

    # The wrapper has to take the upstream method over, so the stub is
    # in place before the tool is installed.
    widget.import_image_folder = import_image_folder
    controller = smudge_filter.install_smudge_tool(widget)
    canvas = widget.canvas
    before = _bytes(first)
    assert controller.set_mode(True) is True
    _fill(canvas, (85.0, 70.0), (10.0, 10.0), (50.0, 40.0))
    assert widget.messages[-2:] == [
        "已选择背景源点",
        "已完成涂抹修复，Ctrl+Z 可撤销",
    ]
    assert controller._history[first]
    # The follow opens the next record of the run: the main window
    # imports its folder again and shows that file.
    widget.import_image_folder(st_scratch)
    _switch_to(widget, controller, second)
    assert list(controller._history) == [first]
    assert controller._current_file == second
    # And back to the image that was repaired.
    widget.import_image_folder(st_scratch)
    _switch_to(widget, controller, first)
    assert controller._current_file == first
    assert controller._can_undo() is True
    send_key(
        canvas,
        QtCore.Qt.Key.Key_Z,
        QtCore.Qt.KeyboardModifier.ControlModifier,
    )
    assert _bytes(first) == before
    assert controller._history == {}
    assert imported == [st_scratch, st_scratch]


def test_another_folder_resets_the_history(st_tool, st_scratch, st_image):
    controller = st_tool.controller
    canvas = st_tool.widget.canvas
    widget = st_tool.widget
    deeper = os.path.join(st_scratch, "deeper")
    os.makedirs(deeper)
    other = os.path.join(deeper, "elsewhere.png")
    PIL.Image.fromarray(st_image, "RGB").save(other)
    controller._action.trigger()
    press(canvas, (60.0, 60.0), QtCore.Qt.MouseButton.RightButton)
    drag(canvas, (10.0, 10.0), (50.0, 40.0))
    assert controller._history
    _switch_to(widget, controller, other)
    assert controller._current_file == other
    assert controller._history == {}


def test_the_memory_budget_drops_the_oldest_image(st_tool):
    controller = st_tool.controller
    roi = (0, 0, 4, 4)
    small = np.zeros((4, 4, 3), np.uint8)
    huge = np.zeros((8192, 8192, 3), np.uint8)
    controller._remember("old.png", roi, small)
    controller._remember("new.png", roi, small)
    assert list(controller._history) == ["old.png", "new.png"]
    assert controller._history_bytes() == 4 * 4 * 3 * 2
    # a step that does not fit the budget at all empties that image
    controller._history["new.png"][0] = ("roi", huge, "new.png", huge.nbytes)
    assert controller._history_bytes() > smudge_filter.MAX_HISTORY_BYTES
    controller._trim_history()
    assert controller._history_bytes() <= smudge_filter.MAX_HISTORY_BYTES
    assert "new.png" not in controller._history
    assert controller._recent_files == []
    controller._remember("new.png", roi, small)
    assert list(controller._history) == ["new.png"]


def test_the_step_cap_is_twenty(st_tool):
    controller = st_tool.controller
    small = np.zeros((2, 2, 3), np.uint8)
    for _step in range(smudge_filter.MAX_FILE_STEPS + 5):
        controller._remember("one.png", (0, 0, 2, 2), small)
    assert len(controller._history["one.png"]) == smudge_filter.MAX_FILE_STEPS


def test_the_rubber_band_previews_the_drag(st_tool):
    controller = st_tool.controller
    canvas = st_tool.widget.canvas
    controller._action.trigger()
    press(canvas, (10.0, 10.0))
    move(canvas, (40.0, 30.0))
    # QRubberBand lives on a QRect, so a 30 pixel drag covers 31 columns
    assert controller._rubber.geometry().width() in (30, 31)
    assert controller._rubber.geometry().height() in (20, 21)
    release(canvas, (40.0, 30.0))
    assert controller._rubber.isHidden() is True
    assert controller._drag_start is None


def test_the_overlay_follows_the_canvas(st_tool, st_image):
    controller = st_tool.controller
    canvas = st_tool.widget.canvas
    controller._action.trigger()
    press(canvas, (20.0, 30.0), QtCore.Qt.MouseButton.RightButton)
    canvas.scale = 2.0
    offset = canvas.offset_to_center()
    rect = smudge_filter._source_window_rect(
        canvas, (10, 20, 30, 40), canvas.scale
    )
    assert rect.left() == pytest.approx(
        (10 + offset.x()) * canvas.scale, abs=1e-6
    )
    assert rect.width() == pytest.approx(40.0, abs=1e-6)
    controller._overlay.sync()
    assert controller._overlay.geometry() == canvas.geometry()


def test_events_are_left_alone_while_the_mode_is_off(st_tool):
    controller = st_tool.controller
    canvas = st_tool.widget.canvas
    assert (
        controller.eventFilter(canvas, QtGui.QPaintEvent(QtCore.QRect()))
        is False
    )
    assert controller.eventFilter(canvas, QtGui.QKeyEvent(
        QtCore.QEvent.Type.KeyPress,
        QtCore.Qt.Key.Key_Z,
        QtCore.Qt.KeyboardModifier.ControlModifier,
    )) is False
    assert controller.eventFilter(canvas, QtGui.QMouseEvent(
        QtCore.QEvent.Type.MouseButtonPress,
        QtCore.QPointF(10.0, 10.0),
        QtCore.QPointF(10.0, 10.0),
        QtCore.Qt.MouseButton.RightButton,
        QtCore.Qt.MouseButton.RightButton,
        QtCore.Qt.KeyboardModifier.NoModifier,
    )) is False


def test_an_unsupported_image_is_refused(st_scratch, st_image, make_widget):
    path = os.path.join(st_scratch, "palette.png")
    PIL.Image.fromarray(st_image, "RGB").convert("P").save(path)
    widget = make_widget(image_path=path, image_data=_bytes(path))
    controller = smudge_filter.install_smudge_tool(widget)
    assert controller.set_mode(True) is False
    assert controller._is_active() is False
    assert widget.errors
    assert "暂不支持" in widget.errors[0][1]
    assert controller._action.isChecked() is False


def test_a_missing_file_is_refused(st_scratch, st_image, make_widget):
    "No disk file means nothing to repair: the mode is refused."

    path = os.path.join(st_scratch, "gone.png")
    widget = make_widget(
        image_path=path,
        image_data=image_bytes(PIL.Image.fromarray(st_image, "RGB")),
    )
    assert not os.path.isfile(path)
    controller = smudge_filter.install_smudge_tool(widget)
    assert controller.set_mode(True) is False
    assert controller._is_active() is False
    assert any("没有磁盘文件" in message for message in widget.messages)
    assert widget.errors == []


def test_a_failure_does_not_write_the_history(st_tool, monkeypatch):
    controller = st_tool.controller
    canvas = st_tool.widget.canvas
    widget = st_tool.widget
    before = _bytes(widget.image_path)
    controller._action.trigger()
    press(canvas, (60.0, 60.0), QtCore.Qt.MouseButton.RightButton)

    def boom(*_args, **_kwargs):
        raise operations.SmudgeError("写入图像文件失败：模拟")

    monkeypatch.setattr(operations, "write_image", boom)
    drag(canvas, (10.0, 10.0), (50.0, 40.0))
    assert controller._history == {}
    assert controller._history_bytes() == 0
    assert _bytes(widget.image_path) == before
    assert widget.errors
    assert "模拟" in widget.errors[-1][1]


def test_a_source_window_over_the_region_reports_it(st_tool):
    controller = st_tool.controller
    canvas = st_tool.widget.canvas
    widget = st_tool.widget
    before = _bytes(widget.image_path)
    controller._action.trigger()
    # The source point is the middle of the box, so the window is the
    # region itself and every candidate is refused.
    _fill(canvas, (40.0, 40.0), (20.0, 20.0), (60.0, 60.0))
    assert any("未发生变化" in message for message in widget.messages)
    assert not any("已完成涂抹修复" in message for message in widget.messages)
    assert _bytes(widget.image_path) == before
    assert controller._history == {}
    assert controller._backups == {}


def test_undo_shows_the_pixels_again(st_tool):
    controller = st_tool.controller
    canvas = st_tool.widget.canvas
    widget = st_tool.widget
    was = np.array(PIL.Image.open(widget.image_path))
    controller._action.trigger()
    _fill(canvas, (85.0, 70.0), (20.0, 20.0), (60.0, 60.0))
    assert controller._history
    send_key(
        canvas, QtCore.Qt.Key.Key_Z, QtCore.Qt.KeyboardModifier.ControlModifier
    )
    assert np.array_equal(np.array(PIL.Image.open(widget.image_path)), was)
    assert _same_image(canvas.pixmap, widget.image_path)
    assert controller._history == {}


def _switch_to(widget, controller, path):
    "Open another file the way the labeling widget does: paint events."

    widget.image_path = path
    widget.filename = path
    widget.image_data = _bytes(path)
    canvas = widget.canvas
    QtWidgets.QApplication.sendEvent(canvas, QtGui.QPaintEvent(canvas.rect()))


def test_switching_image_forgets_the_marks_and_the_count(
    st_tool, st_scratch, st_image
):
    controller = st_tool.controller
    canvas = st_tool.widget.canvas
    widget = st_tool.widget
    first = widget.image_path
    other = _third_png(st_scratch, st_image)
    controller._action.trigger()
    _fill(canvas, (85.0, 70.0), (10.0, 10.0), (50.0, 40.0))
    assert list(controller._history) == [first]
    press(canvas, (60.0, 60.0), QtCore.Qt.MouseButton.RightButton)
    assert controller._source is not None
    _switch_to(widget, controller, other)
    assert controller._current_file == other
    assert controller._source is None
    assert controller._source_box is None
    assert controller._overlay._source is None
    assert list(controller._history) == [first]
    assert controller._status_label.isVisibleTo(
        controller._status_label.parentWidget()
    ) is False
    _switch_to(widget, controller, first)
    assert controller._current_file == first
    assert list(controller._history) == [first]


def test_switching_image_cancels_a_running_drag(
    st_tool, st_scratch, st_image
):
    controller = st_tool.controller
    canvas = st_tool.widget.canvas
    widget = st_tool.widget
    other = _third_png(st_scratch, st_image)
    controller._action.trigger()
    press(canvas, (10.0, 10.0))
    move(canvas, (40.0, 30.0))
    assert controller._drag_start is not None
    _switch_to(widget, controller, other)
    assert controller._drag_start is None
    assert controller._drag_now is None
    assert controller._roi is None
    assert controller._rubber.isHidden() is True


def test_a_paint_event_without_a_file_is_harmless(st_tool):
    controller = st_tool.controller
    canvas = st_tool.widget.canvas
    widget = st_tool.widget
    controller._action.trigger()
    # The mode is entered with a real file, so the controller holds the
    # file it adopted: the event below happens while that file is gone
    # from the widget, the way the labeling widget does it between two
    # images.
    assert controller._current_file == widget.image_path
    adopted = controller._current_file
    widget.image_path = None
    widget.filename = None
    event = QtGui.QPaintEvent(canvas.rect())
    # The filter has to pass the event on and survive the missing file:
    # nothing is adopted, and no exception escapes from the filter.
    assert controller.eventFilter(canvas, event) is False
    assert controller._current_file == adopted


def test_the_drag_preview_is_a_shown_widget(st_tool):
    controller = st_tool.controller
    canvas = st_tool.widget.canvas
    controller._action.trigger()
    assert controller._rubber.isHidden() is True
    press(canvas, (10.0, 10.0))
    move(canvas, (40.0, 30.0))
    # A widget that is hidden explicitly, on a shown parent, is a
    # visible widget: this fails as soon as the preview is not shown.
    assert controller._rubber.isHidden() is False
    assert controller._rubber.isVisibleTo(controller._rubber.parentWidget())
    release(canvas, (40.0, 30.0))
    assert controller._rubber.isHidden() is True


def test_a_right_click_paints_the_source_mark_at_once(st_tool):
    controller = st_tool.controller
    canvas = st_tool.widget.canvas
    overlay = controller._overlay
    controller._action.trigger()
    assert overlay.isVisibleTo(overlay.parentWidget()) is True
    # The overlay has no source yet: it paints nothing at all.
    assert _mark_points(overlay) == []
    # The overlay is a child of the canvas here, so a grab of the canvas
    # is the composite the user looks at. The fixture is one image pixel
    # per widget pixel, so the cross has to sit on the point clicked.
    before = len(_mark_points(canvas))
    press(canvas, (20.0, 30.0), QtCore.Qt.MouseButton.RightButton)
    assert controller._source == (20.0, 30.0)
    assert controller._source_box is None
    assert overlay._source_box is None
    points = _mark_points(overlay)
    assert points
    origin = canvas.geometry().topLeft()
    assert canvas.offset_to_center().isNull()
    mean_x = sum(x for x, _y in points) / len(points)
    mean_y = sum(y for _x, y in points) / len(points)
    assert mean_x == pytest.approx(20.0 - origin.x(), abs=3.0)
    assert mean_y == pytest.approx(30.0 - origin.y(), abs=3.0)
    composite = _mark_points(canvas)
    assert len(composite) > before
    assert (20, 30) in composite


def test_the_source_window_is_painted_once_it_is_known(st_tool):
    controller = st_tool.controller
    canvas = st_tool.widget.canvas
    overlay = controller._overlay
    controller._action.trigger()
    press(canvas, (60.0, 60.0), QtCore.Qt.MouseButton.RightButton)
    cross_only = len(_mark_points(overlay))
    assert cross_only > 0
    drag(canvas, (10.0, 10.0), (50.0, 40.0))
    assert controller._source_box is not None
    assert overlay._source_box == controller._source_box
    assert len(_mark_points(overlay)) > cross_only


def test_a_small_drag_fills_the_region_and_writes_the_file(st_tool):
    controller = st_tool.controller
    canvas = st_tool.widget.canvas
    widget = st_tool.widget
    was = np.array(PIL.Image.open(widget.image_path))
    controller._action.trigger()
    press(canvas, (85.0, 70.0), QtCore.Qt.MouseButton.RightButton)
    drag(canvas, (10.0, 10.0), (20.0, 20.0))
    is_now = np.array(PIL.Image.open(widget.image_path))
    roi = (10, 10, 20, 20)
    outside = np.ones(is_now.shape[:2], bool)
    outside[roi[1] : roi[3], roi[0] : roi[2]] = False
    assert np.array_equal(is_now[outside], was[outside])
    assert not np.array_equal(
        is_now[roi[1] : roi[3], roi[0] : roi[2]],
        was[roi[1] : roi[3], roi[0] : roi[2]],
    )
    assert not any("未发生变化" in message for message in widget.messages)
    assert any("已完成涂抹修复" in message for message in widget.messages)
    assert controller._history[widget.image_path]
    assert _same_image(canvas.pixmap, widget.image_path)
    shape = (canvas.pixmap.height(), canvas.pixmap.width())
    window = operations.source_window((85.0, 70.0), (10, 10), shape)
    assert window == (80, 65, 90, 75)
    # The window is as large as the region, so the aligned patch is the
    # window itself: the region holds the texture of the source point.
    assert np.array_equal(is_now[10:20, 10:20], was[65:75, 80:90])


def test_a_small_box_over_the_source_window_reports_it(st_tool):
    controller = st_tool.controller
    canvas = st_tool.widget.canvas
    widget = st_tool.widget
    before = _bytes(widget.image_path)
    controller._action.trigger()
    # The source point is the middle of the box, so the window is the box
    # itself: the aligned patch would be the region, which is refused.
    _fill(canvas, (15.0, 15.0), (10.0, 10.0), (20.0, 20.0))
    assert any("未发生变化" in message for message in widget.messages)
    assert _bytes(widget.image_path) == before
    assert controller._history == {}
    assert controller._backups == {}

def test_a_plain_move_keeps_the_cross_of_the_mode(st_tool):
    controller = st_tool.controller
    canvas = st_tool.widget.canvas
    _drop_override_cursors()
    controller._action.trigger()
    assert _override_shape() == QtCore.Qt.CursorShape.CrossCursor
    move(canvas, (50.0, 40.0))
    # The upstream move asks for the default cursor as soon as no shape
    # is under the pointer: the cross of the mode has to survive it.
    assert _override_shape() == QtCore.Qt.CursorShape.CrossCursor


def test_leaving_the_mode_gives_the_cursor_back(st_tool):
    controller = st_tool.controller
    canvas = st_tool.widget.canvas
    _drop_override_cursors()
    controller._action.trigger()
    move(canvas, (50.0, 40.0))
    assert _override_shape() == QtCore.Qt.CursorShape.CrossCursor
    assert controller.set_mode(False) is True
    assert _override_shape() is None


def test_a_create_mode_takes_the_mode_down(st_tool):
    controller = st_tool.controller
    canvas = st_tool.widget.canvas
    overlay = controller._overlay
    _drop_override_cursors()
    controller._action.trigger()
    press(canvas, (60.0, 60.0), QtCore.Qt.MouseButton.RightButton)
    press(canvas, (10.0, 10.0))
    move(canvas, (40.0, 30.0))
    assert controller._source is not None
    assert controller._drag_start is not None
    # Picking one of the nine create actions goes through the canvas.
    canvas.set_editing(False)
    assert controller._is_active() is False
    assert controller._action.isChecked() is False
    assert controller._source is None
    assert controller._source_box is None
    assert controller._drag_start is None
    assert controller._rubber.isHidden() is True
    assert overlay._source is None
    assert overlay.isVisibleTo(overlay.parentWidget()) is False
    # The cursor stays on the stack when the exit makes room for a
    # drawing mode: upstream installs its own on the next move of the
    # canvas and replaces this one, so popping it here would only risk
    # taking back an entry that is not the one the tool pushed.
    assert _override_shape() == QtCore.Qt.CursorShape.CrossCursor
    _drop_override_cursors()


def test_going_back_to_editing_takes_the_mode_down(st_tool):
    controller = st_tool.controller
    canvas = st_tool.widget.canvas
    widget = st_tool.widget
    _drop_override_cursors()
    controller._action.trigger()
    _fill(canvas, (60.0, 60.0), (10.0, 10.0), (50.0, 40.0))
    label = controller._status_label
    assert _visible_to_its_owner(label) is True
    # The edit action of the toolbar calls the canvas the same way.
    canvas.set_editing(True)
    assert canvas.editing() is True
    assert controller._is_active() is False
    assert controller._action.isChecked() is False
    assert _override_shape() is None
    assert _visible_to_its_owner(label) is False
    assert controller._history[widget.image_path]


def test_one_install_wraps_the_mode_switch_once(make_widget):
    widget = make_widget()
    controller = smudge_filter.install_smudge_tool(widget)
    assert smudge_filter.install_smudge_tool(widget) is controller
    calls = []
    controller._leave_for_canvas_mode = lambda: calls.append(1)
    widget.canvas.set_editing(False)
    widget.canvas.set_editing(True)
    # A second wrapper would walk the exit twice.
    assert calls == [1, 1]


def test_a_create_mode_refuses_the_entry(st_tool):
    controller = st_tool.controller
    canvas = st_tool.widget.canvas
    overlay = controller._overlay
    _drop_override_cursors()
    canvas.set_editing(False)
    canvas.create_mode = "rectangle"
    controller._action.trigger()
    assert controller._is_active() is False
    assert controller._action.isChecked() is False
    assert any("绘制模式" in message for message in st_tool.widget.messages)
    assert _override_shape() is None
    assert overlay.isVisibleTo(overlay.parentWidget()) is False


def test_a_drawing_mode_is_left_before_the_entry(st_tool):
    controller = st_tool.controller
    canvas = st_tool.widget.canvas
    widget = st_tool.widget
    _drop_override_cursors()
    calls = []

    class _Edit:
        "The upstream editing action, as far as the tool uses it."

        def isEnabled(self):
            return True

        def trigger(self):
            calls.append(1)
            canvas.set_editing(True)

    widget.actions = SimpleNamespace(edit_mode=_Edit())
    canvas.set_editing(False)
    canvas.create_mode = "rectangle"
    assert canvas.drawing() is True
    controller._action.trigger()
    assert calls == [1]
    # The canvas draws again, but for the smudge mode this time: the
    # entry switches it to its own create mode right after the
    # rectangle mode was left, so the tool owns the gestures again.
    assert canvas.drawing() is True
    assert controller._mode_switched is True
    assert controller._is_active() is True
    assert controller._action.isChecked() is True
    assert _override_shape() == QtCore.Qt.CursorShape.CrossCursor


def test_a_half_drawn_shape_refuses_the_entry(st_tool):
    controller = st_tool.controller
    canvas = st_tool.widget.canvas
    widget = st_tool.widget
    _drop_override_cursors()
    calls = []

    class _Edit:
        def isEnabled(self):
            return True

        def trigger(self):
            calls.append(1)

    widget.actions = SimpleNamespace(edit_mode=_Edit())
    canvas.set_editing(False)
    canvas.create_mode = "rectangle"
    canvas.current = "shape in progress"
    controller._action.trigger()
    assert calls == []
    assert controller._is_active() is False
    assert controller._action.isChecked() is False
    assert any("绘制模式" in message for message in widget.messages)


def test_an_auto_labeling_session_refuses_the_entry(st_tool):
    controller = st_tool.controller
    canvas = st_tool.widget.canvas
    widget = st_tool.widget
    _drop_override_cursors()
    calls = []

    class _Edit:
        def isEnabled(self):
            return True

        def trigger(self):
            calls.append(1)
            canvas.set_editing(True)

    widget.actions = SimpleNamespace(edit_mode=_Edit())
    canvas.set_editing(False)
    canvas.create_mode = "rectangle"
    canvas.is_auto_labeling = True
    controller._action.trigger()
    assert calls == []
    assert controller._is_active() is False
    assert controller._action.isChecked() is False
    assert any("绘制模式" in message for message in widget.messages)


def test_a_drawing_mode_gives_the_gestures_back(st_tool):
    controller = st_tool.controller
    canvas = st_tool.widget.canvas
    controller._action.trigger()
    # Leaving the mode has to free the event filter as well: a left
    # press inside the image starts the rectangle of the create mode.
    canvas.set_editing(False)
    canvas.create_mode = "rectangle"
    press(canvas, (10.0, 10.0))
    assert canvas.current is not None
    assert canvas.current.shape_type == "rectangle"


def test_a_brush_or_magic_wand_mode_refuses_the_entry(st_tool):
    controller = st_tool.controller
    canvas = st_tool.widget.canvas
    widget = st_tool.widget
    canvas.is_brush_mode = True
    controller._action.trigger()
    assert controller._is_active() is False
    assert any("画笔" in message for message in widget.messages)
    canvas.is_brush_mode = False
    canvas.set_magic_wand_mode(True)
    controller._action.trigger()
    assert controller._is_active() is False
    assert controller._action.isChecked() is False
    assert any("魔法棒" in message for message in widget.messages)


def test_a_move_of_the_canvas_keeps_the_overlay_on_it(st_scrolled_tool):
    controller = st_scrolled_tool.controller
    canvas = st_scrolled_tool.canvas
    overlay = controller._overlay
    controller._action.trigger()
    press(canvas, (20.0, 30.0), QtCore.Qt.MouseButton.RightButton)
    assert controller._source is not None
    canvas.move(-20, -15)
    # The move is sent explicitly: a scroll area delivers one when it
    # scrolls the canvas, and the overlay has to follow it even when no
    # paint event follows.
    QtWidgets.QApplication.sendEvent(
        canvas, QtGui.QMoveEvent(canvas.pos(), QtCore.QPoint(0, 0))
    )
    assert overlay.geometry() == canvas.geometry()
    assert overlay.pos() == canvas.pos()


def test_the_source_mark_lands_on_the_click_after_scrolling(
    st_scrolled_tool, qapp
):
    controller = st_scrolled_tool.controller
    canvas = st_scrolled_tool.canvas
    scroll = st_scrolled_tool.scroll
    overlay = controller._overlay
    controller._action.trigger()
    # The overlay of this layout is a sibling of the canvas, built
    # inside the viewport of the scroll area, exactly like the one the
    # labeling widget builds at mount time.
    assert overlay.parentWidget() is canvas.parentWidget()
    horizontal = scroll.horizontalScrollBar()
    vertical = scroll.verticalScrollBar()
    assert horizontal.maximum() > 0 and vertical.maximum() > 0
    horizontal.setValue(horizontal.maximum() * 3 // 5)
    vertical.setValue(vertical.maximum() * 2 // 5)
    qapp.processEvents()
    assert canvas.pos().x() < 0 and canvas.pos().y() < 0
    assert overlay.geometry() == canvas.geometry()
    press(canvas, (100.0, 80.0), QtCore.Qt.MouseButton.RightButton)
    assert controller._source == (50.0, 40.0)
    points = _mark_points(overlay)
    assert points
    mean_x = sum(x for x, _y in points) / len(points)
    mean_y = sum(y for _x, y in points) / len(points)
    mark = overlay.mapToGlobal(QtCore.QPoint(round(mean_x), round(mean_y)))
    clicked = canvas.mapToGlobal(QtCore.QPoint(100, 80))
    assert mark.x() == pytest.approx(clicked.x(), abs=3.0)
    assert mark.y() == pytest.approx(clicked.y(), abs=3.0)

