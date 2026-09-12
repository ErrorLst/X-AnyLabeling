"""The plain wheel zooms the canvas exactly like Ctrl+wheel.

The filter is exercised through the full event chain
(``QApplication.sendEvent`` on a real ``Canvas``), so the assertions cover
what the canvas receives: the same ``zoom_request`` payload as Ctrl+wheel,
no ``scroll_request`` at all, and the wheel gestures the canvas owns (brush
radius, rectangle micro adjustment, compare split) left alone.
"""

from types import SimpleNamespace

import pytest
from PyQt6 import QtCore, QtGui, QtWidgets

from anylabeling.custom.edit_extras import (
    install_edit_extras,
    install_wheel_zoom,
)
from anylabeling.custom.edit_extras.wheel_zoom import WheelZoomFilter
from anylabeling.views.labeling.shape import Shape

#: One wheel notch, as reported by QWheelEvent.angleDelta.
WHEEL_NOTCH = 120

CURSOR = (50.0, 60.0)
RECTANGLE = (20.0, 20.0, 80.0, 80.0)
ZOOM_PAYLOAD = (WHEEL_NOTCH, QtCore.QPoint(50, 60))


def _install(canvas):
    "Install the wheel filter on a canvas and return its widget."

    widget = SimpleNamespace(canvas=canvas)
    install_wheel_zoom(widget)
    return widget


def _record(canvas):
    "Return the lists collecting the zoom and scroll requests."

    zoomed = []
    scrolled = []
    canvas.zoom_request.connect(lambda delta, pos: zoomed.append((delta, pos)))
    canvas.scroll_request.connect(lambda *args: scrolled.append(args))
    return zoomed, scrolled


def _rectangle(label="object"):
    "Return a selected 60x60 rectangle sitting at (20, 20)."

    left, top, right, bottom = RECTANGLE
    shape = Shape(label=label, shape_type="rectangle")
    shape.points = [
        QtCore.QPointF(left, top),
        QtCore.QPointF(right, top),
        QtCore.QPointF(right, bottom),
        QtCore.QPointF(left, bottom),
    ]
    shape.close()
    shape.selected = True
    return shape


def _polygon(points):
    "Return a selected polygon built from ``(x, y)`` pairs."

    shape = Shape(label="object", shape_type="polygon")
    shape.points = [QtCore.QPointF(x, y) for x, y in points]
    shape.close()
    shape.selected = True
    return shape


def _select(canvas, shape):
    "Select one shape on the canvas."

    canvas.shapes = [shape]
    canvas.selected_shapes = [shape]
    canvas.store_shapes()


def test_plain_wheel_zooms_like_ctrl_wheel(canvas, make_canvas, send_wheel):
    _install(canvas)
    zoomed, scrolled = _record(canvas)

    send_wheel(canvas, position=CURSOR)
    plain_payload = list(zoomed)
    zoomed.clear()
    send_wheel(
        canvas,
        modifiers=QtCore.Qt.KeyboardModifier.ControlModifier,
        position=CURSOR,
    )
    ctrl_payload = list(zoomed)

    # Reference canvas without the filter: the canvas own Ctrl+wheel.
    reference = make_canvas()
    reference_zoomed, reference_scrolled = _record(reference)
    send_wheel(
        reference,
        modifiers=QtCore.Qt.KeyboardModifier.ControlModifier,
        position=CURSOR,
    )

    assert plain_payload == ctrl_payload == reference_zoomed
    assert plain_payload == [ZOOM_PAYLOAD]
    assert scrolled == []
    assert reference_scrolled == []


def test_horizontal_wheel_does_not_zoom(canvas, send_wheel):
    _install(canvas)
    zoomed, scrolled = _record(canvas)

    send_wheel(canvas, delta=(WHEEL_NOTCH, 0))

    assert zoomed == []
    assert scrolled == []


def test_brush_mode_wheel_resizes_the_brush_only(canvas, send_wheel):
    _install(canvas)
    zoomed, scrolled = _record(canvas)
    _select(canvas, _polygon([(10, 10), (50, 10), (50, 50), (10, 50)]))
    canvas.set_brush_mode(True)
    assert canvas.is_brush_mode is True
    radius = canvas.brush_radius

    send_wheel(canvas)

    assert canvas.brush_radius == radius + 1
    assert zoomed == []
    assert scrolled == []


def test_alt_wheel_keeps_rectangle_micro_adjustments(make_canvas, send_wheel):
    canvas = make_canvas(wheel_rectangle_editing={"enable": True})
    _install(canvas)
    zoomed, _scrolled = _record(canvas)
    moved = []
    canvas.shape_moved.connect(lambda: moved.append(True))
    shape = _rectangle()
    _select(canvas, shape)
    before = [QtCore.QPointF(point) for point in shape.points]

    send_wheel(canvas, modifiers=QtCore.Qt.KeyboardModifier.AltModifier)

    assert moved == [True]
    assert shape.points != before
    assert zoomed == []


def test_plain_wheel_does_not_move_the_selected_rectangle(
    make_canvas, send_wheel
):
    canvas = make_canvas(wheel_rectangle_editing={"enable": True})
    _install(canvas)
    zoomed, _scrolled = _record(canvas)
    moved = []
    canvas.shape_moved.connect(lambda: moved.append(True))
    shape = _rectangle()
    _select(canvas, shape)
    before = [QtCore.QPointF(point) for point in shape.points]

    send_wheel(canvas, position=CURSOR)

    assert zoomed == [ZOOM_PAYLOAD]
    assert moved == []
    assert shape.points == before


def test_shift_wheel_without_compare_pixmap_zooms(canvas, send_wheel):
    # A canvas without a compare view owns no Shift+wheel gesture.
    _install(canvas)
    zoomed, scrolled = _record(canvas)

    send_wheel(
        canvas,
        modifiers=QtCore.Qt.KeyboardModifier.ShiftModifier,
        position=CURSOR,
    )

    assert zoomed == [ZOOM_PAYLOAD]
    assert scrolled == []


def test_shift_wheel_keeps_moving_the_compare_split(canvas, send_wheel):
    _install(canvas)
    zoomed, _scrolled = _record(canvas)
    canvas.compare_pixmap = QtGui.QPixmap(10, 10)
    canvas.compare_pixmap.fill(QtGui.QColor("white"))
    splits = []
    canvas.split_position_changed.connect(splits.append)

    send_wheel(canvas, modifiers=QtCore.Qt.KeyboardModifier.ShiftModifier)

    assert canvas.split_position == pytest.approx(0.52)
    assert splits == [canvas.split_position]
    assert zoomed == []


def test_alt_wheel_zooms_without_a_rectangle_guard(make_canvas, send_wheel):
    # Wheel rectangle editing is on, but nothing is selected, so the
    # rectangle micro adjustment cannot run and Alt falls back to zoom.
    canvas = make_canvas(wheel_rectangle_editing={"enable": True})
    _install(canvas)
    zoomed, scrolled = _record(canvas)
    moved = []
    canvas.shape_moved.connect(lambda: moved.append(True))
    assert canvas.selected_shapes == []

    send_wheel(
        canvas,
        modifiers=QtCore.Qt.KeyboardModifier.AltModifier,
        position=CURSOR,
    )

    assert zoomed == [ZOOM_PAYLOAD]
    assert scrolled == []
    assert moved == []


def test_ctrl_wheel_zooms_over_a_selected_rectangle(make_canvas, send_wheel):
    # The canvas own rectangle branch excludes Ctrl, so even with one
    # unlocked rectangle selected Ctrl+wheel has to reach the filter.
    canvas = make_canvas(wheel_rectangle_editing={"enable": True})
    _install(canvas)
    zoomed, scrolled = _record(canvas)
    moved = []
    canvas.shape_moved.connect(lambda: moved.append(True))
    shape = _rectangle()
    _select(canvas, shape)
    before = [QtCore.QPointF(point) for point in shape.points]

    send_wheel(
        canvas,
        modifiers=QtCore.Qt.KeyboardModifier.ControlModifier,
        position=CURSOR,
    )

    assert len(zoomed) == 1
    assert zoomed == [ZOOM_PAYLOAD]
    assert scrolled == []
    assert moved == []
    assert shape.points == before


def test_alt_horizontal_wheel_leaves_the_rectangle_alone(
    make_canvas, send_wheel
):
    # The rectangle micro adjustment reads the vertical delta only, so a
    # purely horizontal wheel must never reach it: it would shrink the
    # selected rectangle through its wheel_down branch.
    canvas = make_canvas(wheel_rectangle_editing={"enable": True})
    _install(canvas)
    zoomed, scrolled = _record(canvas)
    moved = []
    canvas.shape_moved.connect(lambda: moved.append(True))
    shape = _rectangle()
    _select(canvas, shape)
    before = [QtCore.QPointF(point) for point in shape.points]

    send_wheel(
        canvas,
        delta=(WHEEL_NOTCH, 0),
        modifiers=QtCore.Qt.KeyboardModifier.AltModifier,
    )

    assert moved == []
    assert shape.points == before
    assert zoomed == []
    assert scrolled == []


def test_shift_horizontal_wheel_leaves_the_compare_split_alone(
    canvas, send_wheel
):
    # The compare split moves by -0.02 on a non positive vertical delta,
    # so a purely horizontal wheel would drag the split line leftwards.
    _install(canvas)
    zoomed, scrolled = _record(canvas)
    canvas.compare_pixmap = QtGui.QPixmap(10, 10)
    canvas.compare_pixmap.fill(QtGui.QColor("white"))
    splits = []
    canvas.split_position_changed.connect(splits.append)
    before = canvas.split_position

    send_wheel(
        canvas,
        delta=(WHEEL_NOTCH, 0),
        modifiers=QtCore.Qt.KeyboardModifier.ShiftModifier,
    )

    assert canvas.split_position == pytest.approx(before)
    assert splits == []
    assert zoomed == []
    assert scrolled == []


def test_install_wheel_zoom_is_idempotent(canvas):
    widget = SimpleNamespace(canvas=canvas)

    first = install_wheel_zoom(widget)
    second = install_wheel_zoom(widget)

    assert isinstance(first, WheelZoomFilter)
    assert second is first
    assert widget._wheel_zoom_filter is first
    assert canvas.findChildren(WheelZoomFilter) == [first]


def test_install_edit_extras_reuses_the_installed_filter(qapp, canvas):
    widget = QtWidgets.QWidget()
    widget.canvas = canvas
    installed = install_wheel_zoom(widget)

    returned = install_edit_extras(widget)

    assert returned is installed
    assert widget._wheel_zoom_filter is installed
    assert canvas.findChildren(WheelZoomFilter) == [installed]
    widget.close()


def test_install_wheel_zoom_keeps_the_filter_alive(canvas):
    widget = _install(canvas)
    wheel_filter = widget._wheel_zoom_filter

    assert isinstance(wheel_filter, WheelZoomFilter)
    assert wheel_filter.parent() is canvas
    assert install_wheel_zoom(SimpleNamespace()) is None


def test_install_edit_extras_is_idempotent(qapp, canvas):
    widget = QtWidgets.QWidget()
    widget.canvas = canvas

    first = install_edit_extras(widget)
    second = install_edit_extras(widget)

    assert first is second
    assert widget._wheel_zoom_filter is first
    widget.close()
