"""The smudge tool: erase a defect with texture taken from elsewhere.

Everything the tool does lives here and in its sibling modules; the
labeling widget is only touched by two mount lines, one import and one
call to :func:`install_smudge_tool`. The controller is attached to the
instance, never to the class, and the upstream method bodies stay
untouched: the button is added to the tools toolbar and restored after
every rebuild of that toolbar, and the history is reset from a wrapper
around ``import_image_folder``.

Interaction, as approved in the plan:

* a right click on the image sets the background source point, shown as a
  green cross with the source window drawn around it;
* a left drag paints the region, previewed with a rubber band, and
  releasing the button fills the region right away;
* the result is written over the original file, which is copied into
  ``%TEMP%/dsh-smudge/<timestamp>-<pid>`` before the very first write;
* ``Ctrl+Z`` undoes one operation at a time, on screen and on disk, and
  ``Esc`` cancels a drag, or leaves the mode when nothing is dragged;
* entering any drawing mode leaves the mode and gives the canvas back;
  the tool leaves that mode itself when it can, and refuses otherwise.

While the mode is off the event filter returns ``False`` for every event,
so the behaviour of the window is exactly the one it had before.
"""

import os
import os.path as osp

import numpy as np
from PyQt6 import QtCore, QtGui, QtWidgets

from . import operations, texture_fill

#: Text of the tool button.
ACTION_TEXT = "涂抹工具"

#: Tooltip of the tool button.
ACTION_TIP = (
    "涂抹工具：右键选背景源点，左键拖出矩形后松开；"
    "用源点附近同尺寸纹理填充框内内容，结果直接覆盖原图。"
    "涂抹模式下 Ctrl+Z 撤销，Esc 取消。"
)

#: Tooltip of the tool button while the image has undo steps.
ACTION_UNDO_TIP = (
    "涂抹工具：右键选背景源点，左键拖出矩形后松开；"
    "涂抹模式下 Ctrl+Z 撤销（可撤销 {} 次），Esc 取消。"
)

#: Icon of the tool button, taken from the compiled resources.
ICON_NAME = "brush"

#: Color of the source point and of the source window.
SOURCE_COLOR = QtGui.QColor(0, 200, 0)

#: Radius of the source point cross, in widget pixels.
CROSS_RADIUS = 9.0

#: Cursor of the mode: the cross of the rectangle drawing mode.
MODE_CURSOR = QtCore.Qt.CursorShape.CrossCursor

#: Undo steps kept for one image.
MAX_FILE_STEPS = 20

#: Memory budget of the whole history, in bytes (64 MB).
MAX_HISTORY_BYTES = 64 * 1024 * 1024


def _source_window_rect(canvas, box, scale):
    """Return the widget rectangle of an image box, in canvas pixels.

    ``box`` is half open, like every region of this package, so the far
    corner is the end of the last included pixel and not its centre.
    """
    offset = canvas.offset_to_center()
    x0, y0, x1, y1 = box
    return QtCore.QRectF(
        (x0 + offset.x()) * scale,
        (y0 + offset.y()) * scale,
        (x1 - x0) * scale,
        (y1 - y0) * scale,
    )


class SmudgeOverlay(QtWidgets.QWidget):
    """Transparent overlay drawing the source point and the source window.

    A child widget is used on purpose: an event filter runs before
    ``Canvas.paintEvent``, so anything painted there would be wiped by
    the canvas repaint that follows. Mouse clicks pass through, the
    widget only paints, and it follows the canvas geometry so that zooming
    and panning keep the marks in place.

    The marks themselves are given in canvas pixels: :meth:`paintEvent`
    translates the painter by :meth:`_canvas_origin`, the origin of the
    canvas client area in the coordinates of this widget. Going through
    the screen is the one formula that works for both layouts of the
    overlay, a sibling of the canvas (the labeling widget builds it
    inside a scroll area) and a child of it (a canvas without parent, as
    in the unit tests), and it also stays right while the geometry is
    stale, between a move of the canvas and the next :meth:`sync`.
    """

    def __init__(self, canvas):
        """Create the overlay on top of ``canvas``."""
        parent = canvas.parentWidget() or canvas
        super().__init__(parent)
        self._canvas = canvas
        self._source = None
        self._source_box = None
        self.setAttribute(
            QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.setGeometry(canvas.geometry())
        self.hide()

    def set_source(self, source, source_box):
        """Remember the source point and its window, then repaint.

        Args:
            source: ``(x, y)`` in image coordinates, or ``None`` to clear
                the marks.
            source_box: ``(x0, y0, x1, y1)`` of the window around the
                source point, in image coordinates, or ``None`` when
                the size of the window is not known yet.
        """
        self._source = source
        self._source_box = source_box
        if source is not None:
            # The mark of a right click has to be on screen at once, on
            # top of the canvas: the canvas can have been raised above
            # this overlay by the upstream paint path, so the overlay
            # raises itself here instead of waiting for a repaint it does
            # not control.
            self.raise_()
        self.update()

    def sync(self):
        """Follow the canvas while it is zoomed, panned or resized."""
        canvas = self._canvas
        geometry = canvas.geometry()
        if self.geometry() != geometry:
            self.setGeometry(geometry)
        if self._source is None:
            return
        if canvas.isHidden() or getattr(canvas, "pixmap", None) is None:
            return
        self.raise_()
        self.update()

    def _canvas_origin(self):
        """Return the canvas origin, in the coordinates of this widget.

        Going through the screen is what makes the two layouts of the
        overlay work with a single formula: a sibling of the canvas
        (the labeling widget builds it inside a scroll area) and a
        child of it (a canvas without parent, as in the unit tests).
        Reading the position now instead of the one of the last sync
        also keeps a mark right while the geometry is stale, between
        a move of the canvas and the next sync.
        """
        canvas = self._canvas
        try:
            corner = canvas.mapToGlobal(QtCore.QPoint(0, 0))
            return QtCore.QPointF(self.mapFromGlobal(corner))
        except (AttributeError, RuntimeError, TypeError):
            # The canvas can be gone while this overlay still paints.
            return QtCore.QPointF()

    def paintEvent(self, event):
        """Paint the green cross, and the source window when it is known."""
        del event
        source = self._source
        if source is None:
            return
        source_box = self._source_box
        canvas = self._canvas
        scale = float(getattr(canvas, "scale", 1.0) or 1.0)
        offset = canvas.offset_to_center()
        center = QtCore.QPointF(
            (source[0] + offset.x()) * scale,
            (source[1] + offset.y()) * scale,
        )
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(QtGui.QPen(SOURCE_COLOR, 1.5))
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        # The marks below are given in canvas pixels, so the painter is
        # the only place that has to know where the canvas is.
        painter.translate(self._canvas_origin())
        if source_box is not None:
            # The size of the window is only known while a region is
            # dragged or after a fill: a plain right click has none, and
            # the cross alone is its mark.
            painter.drawRect(_source_window_rect(canvas, source_box, scale))
        painter.drawLine(
            QtCore.QPointF(center.x() - CROSS_RADIUS, center.y()),
            QtCore.QPointF(center.x() + CROSS_RADIUS, center.y()),
        )
        painter.drawLine(
            QtCore.QPointF(center.x(), center.y() - CROSS_RADIUS),
            QtCore.QPointF(center.x(), center.y() + CROSS_RADIUS),
        )
        painter.end()


class SmudgeController(QtCore.QObject):
    """Drive the smudge mode of one labeling widget."""

    def __init__(self, widget):
        """Attach a controller to ``widget``, without installing it.

        A Qt parent is only used when the widget really is a QObject; the
        tool also has to work with a plain object that merely carries the
        canvas, which is what the unit tests use.
        """
        parent = widget if isinstance(widget, QtCore.QObject) else None
        super().__init__(parent)
        self._widget = widget
        canvas = getattr(widget, "canvas", None)
        self._canvas = canvas
        self._action = None
        self._mode = False
        self._busy = False
        self._source = None
        self._source_box = None
        self._drag_start = None
        self._drag_now = None
        self._drag_origin = None
        self._roi = None
        self._current_file = None
        self._top_dir = None
        self._backup_dir = None
        self._backups = {}
        self._history = {}
        self._recent_files = []
        self._work = None
        self._work_file = None
        self._work_format = None
        self._work_info = None
        self._work_signature = None
        self._status_label = None
        self._cursor_overridden = False
        self._populate_wrapper = None
        self._overlay = None
        self._rubber = None
        if canvas is not None:
            parent = canvas.parentWidget() or canvas
            self._overlay = SmudgeOverlay(canvas)
            self._rubber = QtWidgets.QRubberBand(
                QtWidgets.QRubberBand.Shape.Rectangle, parent
            )
            self._rubber.hide()

    # ----------------------------------------------------------- the mode

    def _is_active(self):
        """Return ``True`` while the smudge mode is on."""
        return self._mode

    def _can_draw(self):
        """Return ``True`` when the canvas can accept a gesture."""
        canvas = self._canvas
        if canvas is None or not self._mode:
            return False
        if not canvas.isEnabled() or getattr(canvas, "is_loading", False):
            return False
        if getattr(canvas, "pixmap", None) is None:
            return False
        return bool(getattr(self._widget, "filename", None))

    def _can_execute(self):
        """Return ``True`` when a region may be filled right now."""
        canvas = self._canvas
        return bool(
            not self._busy
            and canvas is not None
            and canvas.isEnabled()
            and getattr(self._widget, "filename", None),
        )

    def set_mode(self, enabled):
        """Enter or leave the smudge mode.

        Returns ``True`` when the mode ended up in the requested state, so
        that a checkable button can be put back in sync when the mode
        cannot be entered.
        """
        enabled = bool(enabled)
        if self._canvas is None:
            return False
        if enabled == self._mode:
            return True
        if enabled:
            return self._enter_mode()
        self._exit_mode()
        return True

    def _leave_drawing_mode(self):
        """Return the canvas to editing mode, or explain why it cannot.

        A drawing mode and this mode cannot share the canvas: the filter
        takes the left and the right button of every gesture, so a
        drawing mode that stayed on would draw nothing at all, without
        a word. The toolbar action of the editing mode is the safe way
        back: it is the one the upstream widget offers, it keeps the
        canvas and the toolbar bookkeeping in step, and it is disabled
        while a shape is being drawn, which is exactly when the tool
        must refuse instead of forcing. Upstream does not disable it
        during an auto labeling session, and the action does clear the
        marks of that session (``set_edit_mode`` calls
        ``clear_auto_labeling_marks``), so that case is refused by the
        explicit test on ``canvas.is_auto_labeling`` above instead.

        Returns:
            True when the canvas now edits instead of drawing.
        """
        canvas = self._canvas
        widget = self._widget
        if getattr(canvas, "current", None) is not None:
            return False
        if getattr(canvas, "is_auto_labeling", False):
            return False
        actions = getattr(widget, "actions", None)
        action = None if actions is None else getattr(
            actions, "edit_mode", None
        )
        if action is None or not action.isEnabled():
            return False
        action.trigger()
        drawing = getattr(canvas, "drawing", None)
        return not (callable(drawing) and drawing())

    def _enter_mode(self):
        """Switch the mode on, or explain why it cannot be entered."""
        canvas = self._canvas
        if getattr(canvas, "is_brush_mode", False) or getattr(
            canvas, "is_magic_wand_mode", False
        ):
            self._notify("请先退出画笔或魔法棒模式，再使用涂抹工具")
            return False
        drawing = getattr(canvas, "drawing", None)
        if callable(drawing) and drawing():
            if not self._leave_drawing_mode():
                self._notify("请先退出绘制模式，再使用涂抹工具")
                return False
        target = self._target_file()
        if not target or not osp.isfile(target):
            self._notify("当前图像没有磁盘文件，无法涂抹修复")
            return False
        try:
            operations.read_image(target)
        except operations.SmudgeError as error:
            self._error("涂抹工具", str(error))
            return False
        self._mode = True
        self._container_file(target)
        canvas.override_cursor(MODE_CURSOR)
        self._cursor_overridden = True
        canvas.setFocus()
        canvas.update()
        if self._overlay is not None:
            self._overlay.sync()
            self._overlay.show()
        self._refresh_status()
        return True

    def _exit_mode(self):
        """Switch the mode off and clean up every mark of it."""
        self._mode = False
        self._cancel_drag()
        self._source = None
        self._source_box = None
        self._roi = None
        self._release_work()
        if self._overlay is not None:
            self._overlay.set_source(None, None)
            self._overlay.hide()
        if self._cursor_overridden and self._canvas is not None:
            self._canvas.restore_cursor()
            self._cursor_overridden = False
        if self._rubber is not None:
            self._rubber.hide()
        self._refresh_status()
        if self._canvas is not None:
            self._canvas.update()

    def _cancel_drag(self):
        """Forget the running drag and hide its preview."""
        self._drag_start = None
        self._drag_now = None
        self._drag_origin = None
        self._roi = None
        if self._rubber is not None:
            self._rubber.hide()

    # ------------------------------------------------------------ history

    def _target_file(self):
        """Return the file the displayed image is stored in, or ``None``.

        ``__init__`` has not run yet when the controller is torn down,
        so the attributes are read with ``getattr``: Qt may still hand an
        event to a filter whose controller is gone.
        """
        widget = getattr(self, "_widget", None)
        if widget is None:
            return None
        target = getattr(widget, "image_path", None) or getattr(
            widget, "filename", None
        )
        return None if target is None else str(target)

    def _can_undo(self):
        """Return ``True`` when the current image has a step to undo."""
        return bool(self._history.get(self._target_file()))

    def _container_file(self, target):
        """Remember the displayed file and reset the marks when it changed.

        The undo history survives a walk through the images of one folder
        and is dropped when the folder changes, or when a single image of
        another folder is opened: both are paths that cannot share the
        history of the previous folder.
        """
        if target == self._current_file:
            return
        folder = osp.dirname(target) if target else None
        if folder != self._top_dir:
            self._reset_history()
            self._top_dir = folder
        self._current_file = target
        self._source = None
        self._source_box = None
        self._cancel_drag()
        self._release_work()
        if self._overlay is not None:
            self._overlay.set_source(None, None)
        self._refresh_status()

    def _adopt_file(self):
        """Notice a newly displayed image, keeping one history per file."""
        target = self._target_file()
        if target is not None:
            self._container_file(target)

    def _reset_history(self):
        """Drop every undo step of every image of the previous folder."""
        self._history.clear()
        self._recent_files = []
        self._refresh_status()

    def _remember(self, file_path, roi, before):
        """Push one undo step, keeping the history inside its budget.

        A step holds the pixels of the region as they were before the
        operation and the file they belong to. The number of steps of one
        image is capped, and an overall budget drops the history of the
        least recently used image, oldest step first, until it fits.
        Backups on disk are never dropped.
        """
        steps = self._history.setdefault(file_path, [])
        steps.append((roi, before, file_path, int(before.nbytes)))
        if len(steps) > MAX_FILE_STEPS:
            del steps[: len(steps) - MAX_FILE_STEPS]
        if file_path in self._recent_files:
            self._recent_files.remove(file_path)
        self._recent_files.append(file_path)
        self._trim_history()

    def _trim_history(self):
        """Drop the oldest steps until the whole history fits in memory."""
        while self._history_bytes() > MAX_HISTORY_BYTES:
            victim = self._oldest_step()
            if victim is None:
                return
            file_path, index = victim
            del self._history[file_path][index]
            if not self._history[file_path]:
                del self._history[file_path]
            if file_path not in self._history:
                if file_path in self._recent_files:
                    self._recent_files.remove(file_path)

    def _history_bytes(self):
        """Return the memory held by the pixels of every undo step."""
        return sum(
            step[3] for steps in self._history.values() for step in steps
        )

    def _oldest_step(self):
        """Return ``(file, index)`` of the oldest step, or ``None``."""
        order = [
            file_path
            for file_path in self._recent_files
            if self._history.get(file_path)
        ]
        for file_path in self._history:
            if file_path not in order:
                order.insert(0, file_path)
        for file_path in order:
            if self._history[file_path]:
                return file_path, 0
        return None

    # ------------------------------------------------------------- action

    def _install_action(self):
        """Create the tool button once and add it to the tools toolbar."""
        if self._action is None:
            action = QtGui.QAction(ACTION_TEXT, _qt_parent(self._widget))
            action.setIcon(
                QtGui.QIcon(":/images/images/{}.png".format(ICON_NAME))
            )
            action.setCheckable(True)
            action.setToolTip(ACTION_TIP)
            action.setStatusTip(ACTION_TIP)
            action.triggered.connect(self._on_action_triggered)
            self._action = action
        tools = getattr(self._widget, "tools", None)
        if tools is None:
            return
        if self._action not in tools.actions():
            tools.addAction(self._action)
        tools.setMinimumHeight(tools.sizeHint().height())

    def _on_action_triggered(self, checked):
        """Enter or leave the mode, keeping the button state truthful."""
        wanted = self._action.isChecked() if self._action else bool(checked)
        entered = self.set_mode(wanted)
        if self._action is not None:
            self._action.setChecked(bool(entered))
        self._update_action_tip()

    def _update_action_tip(self):
        """Show the number of undo steps of the image in the tooltip."""
        if self._action is None:
            return
        steps = len(self._history.get(self._target_file(), ()))
        if steps:
            self._action.setToolTip(ACTION_UNDO_TIP.format(steps))
        else:
            self._action.setToolTip(ACTION_TIP)

    def _wrap_populate_mode_actions(self):
        """Restore the button after the tools toolbar was rebuilt.

        ``populate_mode_actions`` clears the toolbar before it adds the
        upstream actions, so the wrapper is the place to add the button
        again. The upstream method keeps its whole body and stays the only
        builder of the toolbar.
        """
        if self._populate_wrapper is not None:
            return
        original = getattr(self._widget, "populate_mode_actions", None)
        if not callable(original):
            return

        def populate_mode_actions():
            original()
            self._install_action()

        self._populate_wrapper = populate_mode_actions
        self._widget.populate_mode_actions = populate_mode_actions

    def _wrap_import_image_folder(self):
        """Reset the whole history when a folder is opened or reopened.

        The upstream method keeps its whole body; the history is dropped
        before it runs, so even a folder that fails to open starts from an
        empty history, which is what the plan asks for.
        """
        original = getattr(self._widget, "import_image_folder", None)
        if not callable(original):
            return
        if getattr(self._widget, "_smudge_import_wrapped", False):
            return

        def import_image_folder(dirpath, pattern=None, load=True):
            self._reset_history()
            self._top_dir = str(dirpath) if dirpath else None
            self._current_file = None
            return original(dirpath, pattern=pattern, load=load)

        self._widget.import_image_folder = import_image_folder
        self._widget._smudge_import_wrapped = True

    def _wrap_canvas_mouse_move(self):
        """Keep the cross cursor of the mode on the canvas.

        ``Canvas.mouseMoveEvent`` asks for the default cursor as soon as
        no shape is under the pointer, so the cross set when the mode is
        entered would live for a single move. The upstream method keeps
        its whole body and it runs first: only a wrapper can put the
        cross back after it, an event filter runs before it. Nothing is
        touched while the mode is off, nor the wait cursor of a running
        fill, nor the cursor of a canvas the mode cannot draw on.
        """
        canvas = self._canvas
        if canvas is None or getattr(
            canvas, "_smudge_cursor_wrapped", False
        ):
            return
        original = getattr(canvas, "mouseMoveEvent", None)
        if not callable(original):
            return

        def mouse_move_event(event):
            original(event)
            if self._busy or not self._can_draw():
                return
            canvas.override_cursor(MODE_CURSOR)

        canvas.mouseMoveEvent = mouse_move_event
        canvas._smudge_cursor_wrapped = True

    def _wrap_canvas_set_editing(self):
        """Leave the mode whenever the canvas switches mode.

        Every way into another mode goes through ``Canvas.set_editing``:
        ``LabelingWidget.toggle_draw_mode`` calls it once, without a
        condition, for the nine create actions, the digit shortcuts, the
        brush polygon mode, the magic wand, the edit action and the
        brush edit action, and the canvas calls it itself when an auto
        labeling mode or the brush mode is entered. The signal
        ``mode_changed`` is not usable here: it is the request of the
        settings for the automatic switch back to editing mode, while
        ``set_editing`` changes ``mode`` without emitting anything.

        The upstream body stays whole and runs after the mode is left,
        so the cursor of the tool is off the stack before upstream
        installs its own, and the marks are gone when the new mode
        paints. :meth:``_exit_mode`` clears ``_mode`` first, so a switch
        taken from a slot of this exit is a no op.
        """
        canvas = self._canvas
        if canvas is None or getattr(
            canvas, "_smudge_set_editing_wrapped", False
        ):
            return
        original = getattr(canvas, "set_editing", None)
        if not callable(original):
            return

        def set_editing(value=True):
            self._leave_for_canvas_mode()
            return original(value)

        canvas.set_editing = set_editing
        canvas._smudge_set_editing_wrapped = True

    def _leave_for_canvas_mode(self):
        """Leave the mode, button and marks included.

        A click on the button and a mode switch of the canvas have to
        end in the same state, so both go through :meth:``set_mode``; the
        button is unchecked first, the way the escape key does it.
        """
        if not self._mode or self._busy:
            return
        if self._action is not None:
            self._action.setChecked(False)
        self.set_mode(False)

    # ----------------------------------------------------------- gestures

    def _on_source_point(self, position):
        """Set the background source point from a right click."""
        point = self._to_image(position)
        if point is None:
            self._status("源点不在图像范围内，请重新选择")
            return
        self._source = (float(point.x()), float(point.y()))
        self._source_box = None
        if self._roi is not None and not operations.too_small(self._roi):
            size = (self._roi[2] - self._roi[0], self._roi[3] - self._roi[1])
            self._source_box = operations.source_window(
                self._source, size, self._image_shape()
            )
        if self._overlay is not None:
            self._overlay.set_source(self._source, self._source_box)
        self._status("已选择背景源点")

    def _on_left_press(self, position):
        """Start a region drag at a canvas position."""
        point = self._to_image(position)
        if point is None:
            self._status("请从图像内部开始框选")
            return
        self._drag_start = QtCore.QPointF(position)
        self._drag_now = QtCore.QPointF(position)
        self._drag_origin = point
        self._roi = (
            int(round(point.x())),
            int(round(point.y())),
            int(round(point.x())),
            int(round(point.y())),
        )
        self._show_rubber_band()

    def _on_left_move(self, position):
        """Update the region preview while the left button is held."""
        if self._drag_start is None:
            return
        self._drag_now = QtCore.QPointF(position)
        self._show_rubber_band()
        point = self._to_image(position)
        if point is None:
            return
        # The box grows from the image point of the press: ``self._roi``
        # is normalised, so reusing its corner as a new origin would
        # widen the filled area as soon as the pointer moves back past
        # the press, and the result would not match the preview.
        self._roi = operations.roi_box(
            (self._drag_origin.x(), self._drag_origin.y()),
            (point.x(), point.y()),
        )

    def _on_left_release(self, position):
        """Fill the dragged region, or explain why it cannot be filled."""
        if self._drag_start is None:
            return
        self._on_left_move(position)
        roi = self._roi
        self._cancel_drag()
        if roi is None or operations.too_small(roi):
            self._status("请拖出至少 6 像素的矩形区域")
            return
        if self._source is None:
            self._status("请先右键选择背景源点")
            return
        self._roi = roi
        self._execute()

    def _show_rubber_band(self):
        """Place the rubber band over the region being dragged."""
        rubber = self._rubber
        if rubber is None or self._drag_start is None:
            return
        parent = rubber.parentWidget()
        start = self._drag_start
        now = self._drag_now or start
        geometry = QtCore.QRect(
            self._canvas.mapTo(parent, start.toPoint()),
            self._canvas.mapTo(parent, now.toPoint()),
        ).normalized()
        rubber.setGeometry(geometry)
        rubber.show()
        rubber.raise_()

    # ---------------------------------------------------------- execution

    def _execute(self):
        """Fill the region of interest and write the result back."""
        if not self._can_execute():
            return
        roi = self._roi
        if roi is None or operations.too_small(roi):
            self._status("请拖出至少 6 像素的矩形区域")
            return
        if self._source is None:
            self._status("请先右键选择背景源点")
            return
        target = self._target_file()
        if not target or not osp.isfile(target):
            self._error("涂抹工具", "当前图像没有磁盘文件，无法写回")
            return
        self._busy = True
        QtWidgets.QApplication.setOverrideCursor(
            QtCore.Qt.CursorShape.WaitCursor
        )
        try:
            work, image_format, work_info = self._read_work(target)
            operations.validate_roi(roi, work.shape)
            before = np.array(
                work[roi[1] : roi[3], roi[0] : roi[2]], copy=True
            )
            window = self._source_window_for(roi, work.shape)
            result = texture_fill.fill_roi(work, roi, window)
            filled = result[roi[1] : roi[3], roi[0] : roi[2]]
            if np.array_equal(filled, before):
                # Nothing moved inside the region: either the source
                # window covered it and every candidate was refused, or
                # the two textures are the same. Both leave the file
                # untouched, without an undo step and without a success
                # message. The wording names both causes, as the check
                # above cannot tell them apart.
                self._status(
                    "框内像素未发生变化，已跳过（源区域可能与框选区域"
                    "重叠，或两者纹理相同）"
                )
                return
            self._write(result, target, image_format, work_info)
            self._work = result
            self._work_file = target
            self._work_format = image_format
            self._work_signature = self._file_signature(target)
            self._remember(target, roi, before)
            self._refresh_view()
            self._status("已完成涂抹修复，Ctrl+Z 可撤销")
        except Exception as error:  # noqa: BLE001
            self._fail(str(error))
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
            self._busy = False
            self._roi = None
            self._refresh_status()

    def _undo(self):
        """Put the pixels of the last step back, on screen and on disk."""
        target = self._target_file()
        steps = self._history.get(target) if target else None
        if not steps:
            return False
        roi, before, path = steps[-1][0], steps[-1][1], steps[-1][2]
        if not path or not osp.isfile(path):
            self._error("涂抹工具", "原图文件已不存在，无法撤销")
            return True
        self._busy = True
        QtWidgets.QApplication.setOverrideCursor(
            QtCore.Qt.CursorShape.WaitCursor
        )
        try:
            work, image_format, work_info = self._read_work(path)
            height, width = before.shape[:2]
            if roi[3] - roi[1] != height or roi[2] - roi[0] != width:
                raise operations.SmudgeError("撤销记录与当前图像不一致")
            work[roi[1] : roi[3], roi[0] : roi[2]] = before
            self._write(work, path, image_format, work_info)
            self._work = work
            self._work_file = path
            self._work_format = image_format
            self._work_signature = self._file_signature(path)
            steps.pop()
            if not steps:
                self._history.pop(path, None)
                if path in self._recent_files:
                    self._recent_files.remove(path)
            self._refresh_view()
            self._status("已撤销一步涂抹修复")
        except Exception as error:  # noqa: BLE001
            self._fail(str(error))
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
            self._busy = False
            self._refresh_status()
        return True

    def _source_window_for(self, roi, shape):
        """Return the source window of the region about to be filled."""
        window = None
        if self._source is not None:
            size = (roi[2] - roi[0], roi[3] - roi[1])
            window = operations.source_window(self._source, size, shape)
        self._source_box = window
        if self._overlay is not None:
            self._overlay.set_source(self._source, window)
        return window

    def _write(self, array, path, image_format, info):
        """Write an array back over the file, after backing it up once.

        ``info`` carries the encoder parameters of the original file, so
        a JPEG is not re-encoded at the Pillow defaults on every write.
        """
        if path not in self._backups:
            if self._backup_dir is None:
                self._backup_dir = operations.default_backup_dir()
            self._backups[path] = operations.backup_original(
                path, self._backup_dir
            )
        operations.write_image(array, path, image_format, info)

    def _fail(self, message):
        """Roll the screen back to the file on disk and report the error."""
        target = self._target_file()
        self._release_work()
        if target and osp.isfile(target):
            try:
                self._refresh_view()
            except Exception:  # noqa: BLE001
                pass
        self._error("涂抹工具", message)

    # ------------------------------------------------------------ display

    def _refresh_view(self):
        """Show the pixels on disk again, through the upstream pipeline.

        The file is read back every time: a write has just happened, so
        the bytes cached by ``load_file`` are stale, and decoding them
        again would put the old pixels back on the canvas.
        """
        widget = self._widget
        target = self._target_file()
        if not target:
            return
        try:
            with open(target, "rb") as handle:
                widget.image_data = handle.read()
        except OSError:
            return
        if widget.image_data is None:
            return
        processor = getattr(widget, "brightness_contrast_processor", None)
        if processor is None:
            return
        processor.update_image(_image_data_to_pil(widget.image_data))
        values = getattr(widget, "brightness_contrast_values", None) or {}
        stored = values.get(getattr(widget, "filename", None)) or (50, 50)
        brightness, contrast = stored[0], stored[1]
        qimage = processor.adjust(
            brightness if brightness is not None else 50,
            contrast if contrast is not None else 50,
        )
        widget.canvas.load_pixmap(
            QtGui.QPixmap.fromImage(qimage), clear_shapes=False
        )
        widget.canvas.update()
        if self._overlay is not None:
            self._overlay.sync()

    def _refresh_status(self):
        """Show how many steps the current image can undo, if any."""
        label = self._ensure_status_label()
        if label is None:
            return
        current = self._target_file() if self._mode else None
        steps = len(self._history.get(current, ())) if current else 0
        if steps > 0:
            label.setText("已涂抹修复 · 可撤销 {} 次".format(steps))
            label.setVisible(True)
        else:
            label.setVisible(False)
        self._update_action_tip()

    def _ensure_status_label(self):
        """Create the permanent status bar label once and return it."""
        if self._status_label is not None:
            return self._status_label
        status_bar = None
        try:
            status_bar = self._widget.statusBar()
        except Exception:  # noqa: BLE001
            status_bar = None
        if status_bar is None:
            return None
        label = QtWidgets.QLabel("")
        label.hide()
        status_bar.addPermanentWidget(label)
        self._status_label = label
        return label

    # ------------------------------------------------------------- canvas

    def _image_shape(self):
        """Return the shape of the displayed image, or ``None``."""
        pixmap = getattr(self._canvas, "pixmap", None)
        if pixmap is None:
            return None
        return (pixmap.height(), pixmap.width())

    def _to_image(self, position):
        """Map a canvas position to image coordinates, or ``None``."""
        canvas = self._canvas
        if canvas is None:
            return None
        try:
            point = canvas.transform_pos(position)
        except (AttributeError, TypeError, ZeroDivisionError):
            return None
        if canvas.out_off_pixmap(point):
            return None
        return point

    def _sync_overlay(self):
        """Keep the marks and the rubber band aligned with the canvas."""
        if self._overlay is not None:
            self._overlay.sync()
        if self._rubber is not None and self._drag_start is not None:
            self._show_rubber_band()

    def _status(self, message):
        """Show a transient message in the status bar."""
        method = getattr(self._widget, "status", None)
        if callable(method):
            method(message)

    def _notify(self, message):
        """Show a message that the user has to read."""
        self._status(message)

    def _error(self, title, message):
        """Report a failure through the upstream dialog, never raise."""
        method = getattr(self._widget, "error_message", None)
        if callable(method):
            method(title, message)
        else:
            self._status(message)

    # ------------------------------------------------------------- events

    def eventFilter(self, obj, event):
        """Handle the gestures of the mode, pass every other event on.

        Qt keeps the installed filter alive on the canvas, so a canvas
        that outlives a torn down controller still delivers events here:
        every attribute is read defensively, and a controller whose
        ``__init__`` never finished returns ``False`` right away.
        """
        if (
            obj is not getattr(self, "_canvas", None)
            or not getattr(self, "_mode", False)
            or getattr(self, "_busy", False)
        ):
            return False
        # Past the guard the controller is live: the displayed file is
        # adopted on every event, so switching image, or leaving and
        # re-entering the mode, refreshes the status line and drops the
        # marks of the file that is gone.
        self._adopt_file()
        kind = event.type()
        if kind == QtCore.QEvent.Type.ShortcutOverride:
            return self._filter_shortcut(event)
        if kind == QtCore.QEvent.Type.KeyPress:
            return self._filter_key(event)
        # A Move event is what a scroll area sends when it scrolls the
        # canvas, and a canvas that moved is not always painted again.
        if kind in (
            QtCore.QEvent.Type.Paint,
            QtCore.QEvent.Type.Resize,
            QtCore.QEvent.Type.Move,
            QtCore.QEvent.Type.Wheel,
            QtCore.QEvent.Type.Scroll,
        ):
            self._sync_overlay()
            return False
        if kind == QtCore.QEvent.Type.MouseButtonPress:
            return self._filter_press(event)
        if kind == QtCore.QEvent.Type.MouseMove:
            return self._filter_move(event)
        if kind == QtCore.QEvent.Type.MouseButtonRelease:
            return self._filter_release(event)
        return False

    def _filter_shortcut(self, event):
        """Keep Ctrl+Z for the mode, leave Ctrl+Shift+Z to upstream."""
        if not _is_undo(event):
            return False
        event.accept()
        return True

    def _filter_key(self, event):
        """Handle Ctrl+Z and Esc while the mode is on."""
        if event.key() == QtCore.Qt.Key.Key_Escape:
            if self._drag_start is not None:
                self._cancel_drag()
                self._status("已取消当前框选")
            else:
                if self._action is not None:
                    self._action.setChecked(False)
                self.set_mode(False)
            event.accept()
            return True
        if _is_undo(event):
            if self._can_undo():
                self._undo()
            event.accept()
            return True
        return False

    def _filter_press(self, event):
        """Take the right click for the source point and the left drag."""
        if not self._can_draw():
            return False
        button = event.button()
        if button == QtCore.Qt.MouseButton.RightButton:
            self._on_source_point(event.position())
            event.accept()
            return True
        if button == QtCore.Qt.MouseButton.LeftButton:
            self._on_left_press(event.position())
            event.accept()
            return True
        return False

    def _filter_move(self, event):
        """Track the drag for the preview, let an idle move pass on."""
        if self._drag_start is None:
            return False
        self._on_left_move(event.position())
        self._canvas.update()
        event.accept()
        return True

    def _filter_release(self, event):
        """Execute on a left release and swallow the right one."""
        if not self._can_draw():
            return False
        button = event.button()
        if button == QtCore.Qt.MouseButton.RightButton:
            event.accept()
            return True
        if button == QtCore.Qt.MouseButton.LeftButton:
            if self._drag_start is not None:
                self._on_left_release(event.position())
            event.accept()
            return True
        return False

    # ------------------------------------------------------------- memory

    def _read_work(self, path):
        """Return the working array of a file, read from disk when needed.

        The array is cached, together with the size and the modification
        time of the file, so a run of operations on one image reads it
        once while an image changed by another program is read again.
        The encoder parameters of the file are cached with it and travel
        back to the disk on every write.
        """
        signature = self._file_signature(path)
        if (
            self._work is not None
            and self._work_file == path
            and self._work_signature == signature
        ):
            return self._work, self._work_format, self._work_info
        array, image_format, _mode, info = operations.read_image(path)
        self._work = array
        self._work_file = path
        self._work_format = image_format
        self._work_info = info
        self._work_signature = signature
        return array, image_format, info

    def _file_signature(self, path):
        """Return a cheap identity of a file, or ``None`` when it is gone."""
        try:
            info = os.stat(path)
        except OSError:
            return None
        return (info.st_size, info.st_mtime_ns)

    def _release_work(self):
        """Forget the cached working array of the current image."""
        self._work = None
        self._work_file = None
        self._work_format = None
        self._work_info = None
        self._work_signature = None


def _qt_parent(widget):
    """Return a QObject to parent a child of an action, or ``None``.

    The tool also has to work with a plain object that merely carries the
    canvas, which is what the unit tests use; Qt needs a real parent or
    none at all.
    """
    if isinstance(widget, QtCore.QObject):
        return widget
    canvas = getattr(widget, "canvas", None)
    if isinstance(canvas, QtCore.QObject):
        return canvas.parentWidget() or canvas
    return None


def _is_undo(event):
    """Return ``True`` for Ctrl+Z without Shift, the undo of the mode."""
    modifiers = event.modifiers()
    if not modifiers & QtCore.Qt.KeyboardModifier.ControlModifier:
        return False
    if modifiers & QtCore.Qt.KeyboardModifier.ShiftModifier:
        return False
    return event.key() == QtCore.Qt.Key.Key_Z


def _image_data_to_pil(image_data):
    """Decode image bytes the way the labeling widget does."""
    from anylabeling.views.labeling.utils.image import img_data_to_pil

    return img_data_to_pil(image_data)


def install_smudge_tool(widget):
    """Attach the smudge tool to a labeling widget.

    Installing twice is a no op, so the mount point can be called again
    without leaving a second controller, a second event filter or a second
    button behind.

    Args:
        widget: The labeling widget to extend. It has to expose ``canvas``.

    Returns:
        The installed :class:`SmudgeController`, or ``None`` when the
        widget has no canvas.
    """
    canvas = getattr(widget, "canvas", None)
    if canvas is None:
        return None
    existing = getattr(widget, "_smudge_controller", None)
    if isinstance(existing, SmudgeController) and existing._canvas is canvas:
        return existing
    controller = SmudgeController(widget)
    canvas.installEventFilter(controller)
    controller._wrap_populate_mode_actions()
    controller._wrap_import_image_folder()
    controller._wrap_canvas_mouse_move()
    controller._wrap_canvas_set_editing()
    controller._install_action()
    widget._smudge_controller = controller
    return controller
