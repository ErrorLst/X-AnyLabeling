"""Image view of the crop tool: zoom, pan, crop box and marks.

The view owns exactly one image and one fixed size crop box on top of
it. The box follows the pointer, is clamped inside the image and is
the only thing the right button crops: there is no other crop trigger,
no drag to draw a region and no keyboard shortcut. The left button
pans, the wheel zooms around the pointer and a double click fits the
image back into the window.

The view also paints the crop regions that already exist in the output
directory as green outlines, so a crop made in an earlier session stays
visible. Those rectangles come from the file names alone and are
pushed in by the dialog through set_marks.

Coordinates are image pixels throughout: the scene rectangle is the
image rectangle, so a scene point and an image point are the same
thing. The zoom of the view is relative to the fitted state, 1.0 being
'the whole image fits the window'.

Escaping keys: Left, Right, Delete and Escape are deliberately ignored
instead of handled, so they travel up the parent chain to the dialog,
which owns the single keyboard entry point. A view that consumed them
would swallow the shortcut as soon as it holds the focus.
"""

from __future__ import annotations

from typing import List, Tuple

from PyQt6 import QtCore, QtGui, QtWidgets

from . import crop_core

__all__ = [
    "BOX_Z",
    "CROP_PEN_COLOR",
    "MARK_PEN_COLOR",
    "MAX_ZOOM",
    "MIN_ZOOM",
    "PAD_FILL",
    "PAD_Z",
    "MARK_Z",
    "ZOOM_STEP",
    "CropView",
]

#: Factor of one wheel notch, relative to the fitted view.
ZOOM_STEP = 1.15

#: Lowest accepted zoom: 1.0 is the fitted view.
MIN_ZOOM = 1.0

#: Highest accepted zoom.
MAX_ZOOM = 20.0

#: Colour of the crop box outline.
CROP_PEN_COLOR = "#0a84ff"

#: Colour of the outline of a crop that already exists.
MARK_PEN_COLOR = "#34c759"

#: Fill of the padding bands, with alpha.
PAD_FILL = (10, 132, 255, 60)

#: Z values of the three overlays, drawn from back to front.
BOX_Z = 100.0
PAD_Z = 98.0
MARK_Z = 50.0

#: Text of the placeholder that covers an empty view.
EMPTY_TEXT = '把图片目录拖到这里，或点「打开目录」'

#: Width of the overlay outlines, in device independent pixels.
PEN_WIDTH = 1.0

#: Reason reported when the file cannot be decoded.
ERROR_DECODE_FORMAT = '图片无法解码：%s'

#: Reason reported when the reader knows the size but rejects the file.
ERROR_DECODE_FAILED = '图片无法读取：%s'

#: Reason reported when the image is larger than the pixel budget.
ERROR_TOO_LARGE = '图片过大：%sx%s 超过上限 %s 像素'


def _color(value) -> QtGui.QColor:
    """Return a colour given as name, QColor or RGB(A) tuple."""

    if isinstance(value, QtGui.QColor):
        return QtGui.QColor(value)
    if isinstance(value, str):
        return QtGui.QColor(value)
    return QtGui.QColor(*value)


class CropView(QtWidgets.QGraphicsView):
    """One image, one crop box, one mark layer.

    The public API is small on purpose: the dialog loads an image, sets
    the size of the box, asks where the box is, crops, and pushes the
    rectangles of the existing crops. Everything else is a Qt event.
    """

    #: Top left corner of the crop box, in image pixels. Only the right
    #: button makes the view emit it.
    crop_requested = QtCore.pyqtSignal(int, int)

    #: Pointer position in image pixels, (-1, -1) outside the image.
    cursor_moved = QtCore.pyqtSignal(int, int)

    def __init__(self, parent=None, max_pixels=None):
        """Build an empty view.

        Args:
            parent: The parent widget, usually the dialog.
            max_pixels: Pixel budget of a source image. None reads
                crop_core.MAX_IMAGE_PIXELS at call time, so a test can
                lower the module constant and be honoured.
        """

        super().__init__(parent)
        self.max_pixels = max_pixels
        self._pixmap = None
        self._size = (0, 0)
        self._error = ''
        self._crop_size = [1, 1]
        self._crop_pos = [0, 0]
        self._pad = (0, 0)
        self._marks: List[Tuple[int, int, int, int]] = []
        self._zoom = MIN_ZOOM
        self._base_scale = 1.0
        self._panning = False
        self._pan_origin = QtCore.QPoint(0, 0)
        self.setTransformationAnchor(
            QtWidgets.QGraphicsView.ViewportAnchor.AnchorUnderMouse
        )
        self.setResizeAnchor(
            QtWidgets.QGraphicsView.ViewportAnchor.AnchorUnderMouse
        )
        self.setMouseTracking(True)
        self.setDragMode(QtWidgets.QGraphicsView.DragMode.NoDrag)
        self.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform)
        self.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.setVerticalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.setScene(QtWidgets.QGraphicsScene(self))
        self.setSceneRect(QtCore.QRectF())
        self._empty_label = QtWidgets.QLabel(EMPTY_TEXT, self.viewport())
        self._empty_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setAttribute(
            QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )
        self._empty_label.setGeometry(self.viewport().rect())

    # ------------------------------------------------------------- 加载

    def load_image(self, path) -> bool:
        """Show one image, or keep the view as it is and return False.

        A failed load leaves the image that is already shown in place
        and only reports the reason through last_error, so a broken file
        of a folder cannot blank the view. The size guard runs before
        the pixels are decoded, so a huge or a truncated file is refused
        without allocating its buffer. The reader runs with the
        automatic EXIF transform switched off: the view shows the same
        pixels as the main canvas, which does not transform them
        either.
        """

        self._error = ''
        reader = QtGui.QImageReader(path)
        reader.setAutoTransform(False)
        size = reader.size()
        if not size.isValid():
            self._error = ERROR_DECODE_FORMAT % path
            return False
        if size.width() * size.height() > self._limit():
            self._error = ERROR_TOO_LARGE % (
                size.width(),
                size.height(),
                self._limit(),
            )
            return False
        image = reader.read()
        if image.isNull():
            self._error = ERROR_DECODE_FAILED % path
            return False
        self._pixmap = QtGui.QPixmap.fromImage(image)
        self._size = (image.width(), image.height())
        self._crop_pos = [0, 0]
        self._marks = []
        self._error = ''
        self._empty_label.hide()
        self._sync_scene()
        self.reset_view()
        self.center_crop()
        self.viewport().update()
        return True

    def _limit(self) -> int:
        """Return the pixel budget of a source image."""

        if self.max_pixels is None:
            return crop_core.MAX_IMAGE_PIXELS
        return int(self.max_pixels)

    def clear_image(self) -> None:
        """Drop the image, its box and its marks."""

        self._pixmap = None
        self._size = (0, 0)
        self._crop_pos = [0, 0]
        self._marks = []
        self._zoom = MIN_ZOOM
        self._base_scale = 1.0
        self.setSceneRect(QtCore.QRectF())
        self.resetTransform()
        self._empty_label.show()
        self.viewport().update()

    def has_image(self) -> bool:
        """Return True when an image is loaded."""

        return self._pixmap is not None

    def image_size(self) -> Tuple[int, int]:
        """Return the size of the loaded image, (0, 0) when empty."""

        return self._size

    def last_error(self) -> str:
        """Return the reason of the last failed load, else ''."""

        return self._error

    # ----------------------------------------------------------- 裁切框

    def set_crop_size(self, width, height) -> None:
        """Set the size of the crop box and clamp its corner.

        The top left corner is kept when it still fits and pinned to
        (0, 0) when the box is larger than the image.
        """

        try:
            new_width = max(1, int(width))
            new_height = max(1, int(height))
        except (TypeError, ValueError):
            return
        self._crop_size = [new_width, new_height]
        self._crop_pos = list(
            self._clamp(self._crop_pos[0], self._crop_pos[1])
        )
        self.viewport().update()

    def crop_size(self) -> Tuple[int, int]:
        """Return the size of the crop box."""

        return (self._crop_size[0], self._crop_size[1])

    def set_pad(self, pad_x, pad_y) -> None:
        """Set the width of the padding bands drawn inside the box."""

        try:
            self._pad = (max(0, int(pad_x)), max(0, int(pad_y)))
        except (TypeError, ValueError):
            self._pad = (0, 0)
        self.viewport().update()

    def pad_size(self) -> Tuple[int, int]:
        """Return the width of the padding bands drawn in the box."""

        return self._pad

    def _clamp(self, x, y) -> Tuple[int, int]:
        """Return a box corner that stays inside the image."""

        if self._pixmap is None:
            return (0, 0)
        max_x = self._size[0] - self._crop_size[0]
        max_y = self._size[1] - self._crop_size[1]
        return (
            max(0, min(int(round(x)), max(0, max_x))),
            max(0, min(int(round(y)), max(0, max_y))),
        )

    def move_crop_to(self, x, y) -> Tuple[int, int]:
        """Move the crop box to a point, clamped inside the image."""

        self._crop_pos = list(self._clamp(x, y))
        self.viewport().update()
        return self.crop_pos()

    def crop_pos(self) -> Tuple[int, int]:
        """Return the top left corner of the crop box."""

        return (self._crop_pos[0], self._crop_pos[1])

    def center_crop(self) -> Tuple[int, int]:
        """Center the crop box inside the image and return its corner."""

        if self._pixmap is None:
            return self.crop_pos()
        x = (self._size[0] - self._crop_size[0]) // 2
        y = (self._size[1] - self._crop_size[1]) // 2
        return self.move_crop_to(x, y)

    def center_crop_on(self, x, y) -> Tuple[int, int]:
        """Put the center of the crop box on one image point.

        The corner that center implies is clamped by move_crop_to, so
        a point near an edge slides the box along that edge and a box
        larger than the image stays pinned at (0, 0). Without an
        image the box does not move at all.
        """

        if self._pixmap is None:
            return self.crop_pos()
        return self.move_crop_to(
            int(x) - self._crop_size[0] // 2,
            int(y) - self._crop_size[1] // 2,
        )

    def _crop_rect(self) -> QtCore.QRectF:
        """Return the crop box as a scene rectangle."""

        return QtCore.QRectF(
            float(self._crop_pos[0]),
            float(self._crop_pos[1]),
            float(self._crop_size[0]),
            float(self._crop_size[1]),
        )

    def _pad_rects(self) -> List[QtCore.QRectF]:
        """Return the four padding bands of the current box."""

        pad_x, pad_y = self._pad
        left = min(pad_x, self._crop_size[0] // 2)
        top = min(pad_y, self._crop_size[1] // 2)
        if left <= 0 and top <= 0:
            return []
        band = self._crop_rect()
        rects = []
        if left > 0:
            rects.append(
                QtCore.QRectF(
                    band.left(), band.top(), float(left), band.height()
                )
            )
            rects.append(
                QtCore.QRectF(
                    band.right() - left,
                    band.top(),
                    float(left),
                    band.height(),
                )
            )
        if top > 0:
            rects.append(
                QtCore.QRectF(
                    band.left(), band.top(), band.width(), float(top)
                )
            )
            rects.append(
                QtCore.QRectF(
                    band.left(),
                    band.bottom() - top,
                    band.width(),
                    float(top),
                )
            )
        return rects

    # ------------------------------------------------------------- 标记

    def set_marks(self, rects) -> None:
        """Replace the mark layer with (x, y, w, h) image rectangles."""

        marks = []
        for rect in rects or []:
            try:
                x, y, width, height = (int(value) for value in rect)
            except (TypeError, ValueError):
                continue
            if width > 0 and height > 0:
                marks.append((x, y, width, height))
        self._marks = marks
        self.viewport().update()

    def clear_marks(self) -> None:
        """Drop every mark."""

        self._marks = []
        self.viewport().update()

    def mark_count(self) -> int:
        """Return how many marks are drawn."""

        return len(self._marks)

    # ------------------------------------------------------------- 缩放

    def reset_view(self) -> None:
        """Fit the whole image into the window and center it."""

        self._zoom = MIN_ZOOM
        self._refit()
        self.centerOn(self._center_point())

    def _refit(self) -> None:
        """Apply the fitted scale and then the current zoom on top."""

        if not self._can_fit():
            self._base_scale = 1.0
            self.resetTransform()
            return
        self.fitInView(
            self.sceneRect(), QtCore.Qt.AspectRatioMode.KeepAspectRatio
        )
        self._base_scale = max(1e-6, self.transform().m11())
        if abs(self._zoom - MIN_ZOOM) > 1e-9:
            self.scale(self._zoom, self._zoom)

    def _can_fit(self) -> bool:
        """Return True when the view and its image can be fitted."""

        return (
            self._pixmap is not None
            and self.width() > 2
            and self.height() > 2
        )

    def _center_point(self) -> QtCore.QPointF:
        """Return the center of the image in scene coordinates."""

        rect = self.sceneRect()
        if rect.isNull():
            return QtCore.QPointF(0.0, 0.0)
        return rect.center()

    def zoom_level(self) -> float:
        """Return the zoom, 1.0 meaning the image fits the window."""

        return self._zoom

    def apply_zoom(self, factor) -> float:
        """Multiply the zoom by factor and clamp it.

        A zoom that would fall below MIN_ZOOM goes back to the fitted
        view instead of a scale of its own, so the wheel can always
        come home.
        """

        try:
            step = float(factor)
        except (TypeError, ValueError):
            return self._zoom
        target = self._zoom * step
        if target <= MIN_ZOOM:
            self.reset_view()
            return self._zoom
        self._zoom = min(MAX_ZOOM, target)
        self._refit()
        return self._zoom

    def wheelEvent(self, event) -> None:
        """Zoom around the pointer."""

        delta = event.angleDelta().y()
        if delta and self.has_image():
            self.apply_zoom(ZOOM_STEP if delta > 0 else 1.0 / ZOOM_STEP)
        super().wheelEvent(event)

    def _apply_pan(self, delta) -> None:
        """Scroll the view by a pixel delta."""

        self.horizontalScrollBar().setValue(
            self.horizontalScrollBar().value() - delta.x()
        )
        self.verticalScrollBar().setValue(
            self.verticalScrollBar().value() - delta.y()
        )

    # ------------------------------------------------------------- 裁切

    def trigger_crop(self) -> bool:
        """Ask for a crop of the current box; False without an image."""

        if not self.has_image():
            return False
        x, y = self.crop_pos()
        self.crop_requested.emit(int(x), int(y))
        return True

    # ----------------------------------------------------------- Qt 事件

    def mousePressEvent(self, event) -> None:
        """Start a pan on the left button."""

        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._panning = True
            self._pan_origin = event.position().toPoint()
            self.setCursor(QtCore.Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        """Pan while the left button is held, and report the pointer."""

        position = event.position().toPoint()
        if self._panning:
            self._apply_pan(position - self._pan_origin)
            self._pan_origin = position
        self._emit_cursor(position)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        """End the pan started by the left button."""

        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._panning = False
            self.setCursor(QtCore.Qt.CursorShape.ArrowCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        """Fit the image back into the window."""

        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.reset_view()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event) -> None:
        """Crop the current box: the only crop entry point of the tool.

        No context menu is built and no menu object is kept: the right
        button means 'crop' and nothing else.
        """

        if self.trigger_crop():
            event.accept()
            return
        event.ignore()

    def keyPressEvent(self, event) -> None:
        """Let the navigation and deletion keys travel to the dialog.

        Left, Right, Delete and Escape are ignored without calling the
        base implementation, so Qt hands them to the parent widget
        chain; the dialog is the one place that turns them into an
        action. Every other key keeps its default behaviour.
        """

        if event.key() in (
            QtCore.Qt.Key.Key_Left,
            QtCore.Qt.Key.Key_Right,
            QtCore.Qt.Key.Key_Delete,
            QtCore.Qt.Key.Key_Escape,
        ):
            event.ignore()
            return
        super().keyPressEvent(event)

    def leaveEvent(self, event) -> None:
        """Report that the pointer left the image."""

        self.cursor_moved.emit(-1, -1)
        super().leaveEvent(event)

    def resizeEvent(self, event) -> None:
        """Keep the placeholder and the fitted view in step."""

        super().resizeEvent(event)
        self._empty_label.setGeometry(self.viewport().rect())
        if abs(self._zoom - MIN_ZOOM) <= 1e-9 and self._can_fit():
            self._refit()
            self.centerOn(self._center_point())

    # ------------------------------------------------------- 覆盖层绘制

    def _emit_cursor(self, position) -> None:
        """Emit the pointer position in image pixels."""

        if self._pixmap is None:
            self.cursor_moved.emit(-1, -1)
            return
        point = self.mapToScene(position)
        width, height = self._size
        if 0 <= point.x() < width and 0 <= point.y() < height:
            self.cursor_moved.emit(int(point.x()), int(point.y()))
        else:
            self.cursor_moved.emit(-1, -1)

    def _sync_scene(self) -> None:
        """Give the scene the rectangle of the image."""

        width, height = self._size
        self.setSceneRect(QtCore.QRectF(0.0, 0.0, width, height))

    @staticmethod
    def _pen(color, z_value) -> QtGui.QPen:
        """Return a cosmetic pen of one overlay layer."""

        pen = QtGui.QPen(_color(color))
        pen.setCosmetic(True)
        pen.setWidthF(PEN_WIDTH)
        pen.setJoinStyle(QtCore.Qt.PenJoinStyle.MiterJoin)
        return pen

    def drawBackground(self, painter, rect) -> None:
        """Paint the image itself, below the overlay layers.

        The scene is one unit per image pixel and its rectangle is
        exactly the image, so the pixmap is drawn at the scene
        origin; the crop box, the padding bands and the marks are
        painted later, by drawForeground, on top of it.
        """

        super().drawBackground(painter, rect)
        if self._pixmap is None:
            return
        painter.drawPixmap(QtCore.QPointF(0.0, 0.0), self._pixmap)

    def drawForeground(self, painter, rect) -> None:
        """Paint the marks, the padding bands and the crop box."""

        super().drawForeground(painter, rect)
        if self._pixmap is None:
            return
        painter.save()
        mark_pen = self._pen(MARK_PEN_COLOR, MARK_Z)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        for x, y, width, height in self._marks:
            painter.setPen(mark_pen)
            painter.drawRect(
                QtCore.QRectF(
                    float(x), float(y), float(width), float(height)
                )
            )
        pad_fill = QtGui.QColor(*PAD_FILL)
        pad_pen = self._pen(pad_fill, PAD_Z)
        for band in self._pad_rects():
            painter.setPen(pad_pen)
            painter.setBrush(pad_fill)
            painter.drawRect(band)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.setPen(self._pen(CROP_PEN_COLOR, BOX_Z))
        painter.drawRect(self._crop_rect())
        painter.restore()
