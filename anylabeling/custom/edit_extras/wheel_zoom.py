"""Zoom the canvas with the plain mouse wheel, like Ctrl+wheel does.

``Canvas`` stays untouched: a ``WheelZoomFilter`` is installed on it and
consumes the plain wheel before ``Canvas.wheelEvent`` can turn it into a
scroll request. The two non zooming wheel gestures the canvas owns keep
working, because the filter forwards them untouched:

* ``Alt`` plus wheel adjusts the selected rectangle, and
* ``Shift`` plus wheel moves the compare view split line.

The conditions of ``_canvas_owns_wheel`` mirror the branches of
``Canvas.wheelEvent``; upstream has to update both together.
"""

from PyQt6 import QtCore


def _flag(canvas, name):
    """Return a boolean canvas attribute, ``False`` when it is missing."""
    return bool(getattr(canvas, name, False))


def _is_editing(canvas):
    """Return ``True`` when the canvas is in edit mode."""
    editing = getattr(canvas, "editing", None)
    return bool(editing()) if callable(editing) else False


def _selected_shapes(canvas):
    """Return the shapes currently selected on the canvas."""
    return getattr(canvas, "selected_shapes", None) or []


def _canvas_owns_wheel(canvas, event):
    """Return ``True`` when ``Canvas.wheelEvent`` must handle the event.

    The conditions mirror the branches of ``Canvas.wheelEvent``: ``Alt``
    with a single unlocked rectangle selected belongs to the rectangle
    micro adjustment, and ``Shift`` with a compare pixmap to the compare
    split.

    Args:
        canvas: The canvas the wheel event is delivered to.
        event: The wheel event about to be filtered.

    Returns:
        ``True`` when the canvas keeps ownership of the event.
    """
    mods = event.modifiers()
    shapes = _selected_shapes(canvas)
    rectangle_selected = (
        len(shapes) == 1
        and getattr(shapes[0], "shape_type", None) == "rectangle"
        and not getattr(shapes[0], "locked", False)
    )
    if (
        mods & QtCore.Qt.KeyboardModifier.AltModifier
        and not (mods & QtCore.Qt.KeyboardModifier.ControlModifier)
        and _flag(canvas, "enable_wheel_rectangle_editing")
        and _is_editing(canvas)
        and not _flag(canvas, "auto_highlight_shape")
        and rectangle_selected
    ):
        return True
    compare_pixmap = getattr(canvas, "compare_pixmap", None)
    if (
        mods == QtCore.Qt.KeyboardModifier.ShiftModifier
        and compare_pixmap is not None
        and not compare_pixmap.isNull()
    ):
        return True
    return False


class WheelZoomFilter(QtCore.QObject):
    """Make the plain wheel behave exactly like Ctrl+wheel (cursor zoom)."""

    def __init__(self, canvas):
        super().__init__(canvas)
        self._canvas = canvas

    def eventFilter(self, obj, event):
        """Zoom on a plain wheel, leave every other event alone."""
        if event.type() != QtCore.QEvent.Type.Wheel or obj is not self._canvas:
            return False
        canvas = self._canvas
        if getattr(canvas, "is_brush_mode", False):
            # Brush edit mode keeps the wheel for the brush radius.
            return False
        delta = event.angleDelta()
        if delta.y() == 0:
            # A purely horizontal wheel never acts, like Ctrl+wheel. This
            # has to be decided before the canvas owned gestures: the
            # rectangle micro adjustment and the compare split read the
            # vertical delta only, so on such an event they would treat it
            # as a downward wheel and shrink the selected rectangle or
            # move the split line.
            event.accept()
            return True
        if _canvas_owns_wheel(canvas, event):
            return False
        canvas.zoom_request.emit(delta.y(), event.position().toPoint())
        event.accept()
        return True


def _installed_filter(widget, canvas):
    """Return the filter already installed on ``canvas``, if any.

    Args:
        widget: The object the filter was installed on.
        canvas: The canvas the filter has to be attached to.

    Returns:
        The installed ``WheelZoomFilter``, or ``None`` when there is
        none for this canvas.
    """
    wheel_filter = getattr(widget, "_wheel_zoom_filter", None)
    if isinstance(wheel_filter, WheelZoomFilter):
        if wheel_filter.parent() is canvas:
            return wheel_filter
    return None


def install_wheel_zoom(widget):
    """Install the plain wheel zoom filter on ``widget.canvas``.

    Installing twice is a no op: the filter of a widget is created once
    and reused, so a repeated call cannot leave a second filter behind
    that keeps receiving the wheel of the same canvas.

    Args:
        widget: The object exposing the canvas to extend.

    Returns:
        The installed ``WheelZoomFilter``, or ``None`` when there is no
        canvas.
    """
    canvas = getattr(widget, "canvas", None)
    if canvas is None:
        return None
    existing = _installed_filter(widget, canvas)
    if existing is not None:
        return existing
    wheel_filter = WheelZoomFilter(canvas)
    canvas.installEventFilter(wheel_filter)
    widget._wheel_zoom_filter = wheel_filter
    return wheel_filter
