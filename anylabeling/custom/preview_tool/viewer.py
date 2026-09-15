"""The image view of the preview tool.

The view shows one image at a time, keeps the zoom level and the
relative position of the viewport when the image changes, and carries
the annotation layer of overlay.py. The mouse wheel zooms up to
MAX_ZOOM, the left button pans, a double click fits the image again and
a dropped directory is reported to the dialog.

The empty state is a label in the viewport instead of a scene item, so
it stays centred without touching the scene transform.
"""

from __future__ import annotations

import os
from typing import Optional, Sequence

from PyQt6 import QtCore, QtGui, QtWidgets

from anylabeling.views.labeling.utils.theme import get_mode, get_theme

from .overlay import AnnotationLayer

__all__ = [
    "EMPTY_TEXT",
    "MAX_ZOOM",
    "ZOOM_STEP",
    "PreviewView",
    "first_directory",
]

MAX_ZOOM = 20.0
ZOOM_STEP = 1.15

EMPTY_TEXT = "把图片目录拖到这里，或按 Ctrl+O 选择目录"
ERROR_FORMAT = "图片无法显示：%s"
BACKGROUND_TEXT = "已切换视图背景"
BACKGROUND_DARK = "#101010"
BACKGROUND_LIGHT = "#f0f0f0"


def first_directory(mime) -> str:
    """Return the first local directory of a mime payload, else ''.

    Only a directory is accepted: a dropped image is refused, because a
    drop always opens a folder.
    """

    if mime is None:
        return ""
    try:
        if not mime.hasUrls():
            return ""
        urls = mime.urls()
    except Exception:
        return ""
    for url in urls:
        try:
            if not url.isLocalFile():
                continue
            path = url.toLocalFile()
        except Exception:
            continue
        if path and os.path.isdir(path):
            return path
    return ""


def _background_style(color: str) -> str:
    """Return the QSS that paints the viewport with one colour."""

    return f"QGraphicsView {{ background-color: {color}; border: none; }}"


class PreviewView(QtWidgets.QGraphicsView):
    """One image, its annotations, its zoom and its pan.

    Signals:
        pixel_moved(int, int) - pointer position in image pixels,
            (-1, -1) outside the image.
        directory_dropped(str) - a directory was dropped on the view.
    """

    pixel_moved = QtCore.pyqtSignal(int, int)
    directory_dropped = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        """Build an empty view."""

        super().__init__(parent)
        self._scene = QtWidgets.QGraphicsScene(self)
        self.setScene(self._scene)
        self._layer = AnnotationLayer()
        self._scene.addItem(self._layer)
        self._pixmap_item: Optional[QtWidgets.QGraphicsPixmapItem] = None
        self._has_image = False
        self._zoom = 1.0
        self._expand_px = 0.0
        self._panning = False
        self._pan_origin = QtCore.QPoint(0, 0)
        self._error = ""
        self._plain_background = False
        self.setTransformationAnchor(
            QtWidgets.QGraphicsView.ViewportAnchor.AnchorUnderMouse
        )
        self.setResizeAnchor(
            QtWidgets.QGraphicsView.ViewportAnchor.AnchorUnderMouse
        )
        self.setDragMode(QtWidgets.QGraphicsView.DragMode.NoDrag)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform)
        self.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.setVerticalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._hint = QtWidgets.QLabel(EMPTY_TEXT, self.viewport())
        self._hint.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self._hint.setWordWrap(True)
        self._hint.setAttribute(
            QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )
        self._apply_theme()
        self.clear()

    # -------------------------------------------------------------- api

    def show_pixmap(self, pixmap, shapes: Optional[Sequence] = None) -> bool:
        """Show one pixmap, keeping the zoom and the relative position."""

        if pixmap is None or pixmap.isNull():
            return False
        self._apply_pixmap(pixmap, shapes)
        return True

    def show_image(self, image, shapes: Optional[Sequence] = None) -> bool:
        """Show one QImage, the main thread is the only caller."""

        if image is None or image.isNull():
            return False
        return self.show_pixmap(QtGui.QPixmap.fromImage(image), shapes)

    def show_error(self, message: str) -> None:
        """Remember a load failure and show it when nothing is on screen."""

        self._error = str(message or "")
        if not self._has_image:
            self._show_hint(ERROR_FORMAT % self._error)

    def clear(self) -> None:
        """Drop the image and its annotations, show the empty state."""

        self._scene.removeItem(self._layer)
        self._scene.clear()
        self._scene.addItem(self._layer)
        self._layer.setZValue(10)
        self._layer.clear()
        self._pixmap_item = None
        self._has_image = False
        self._zoom = 1.0
        self._error = ""
        self.resetTransform()
        self.setSceneRect(QtCore.QRectF())
        self._show_hint(EMPTY_TEXT)

    def set_expand_px(self, pixels) -> None:
        """Set the expand value of the annotation layer."""

        self._expand_px = max(0.0, _to_float(pixels))
        self._layer.set_expand_px(self._expand_px)

    def expand_px(self) -> float:
        """Return the expand value of the annotation layer."""

        return float(self._expand_px)

    def set_annotations(self, shapes) -> None:
        """Replace the annotations of the image on screen."""

        self._layer.build(shapes, self._expand_px)

    def set_annotations_visible(self, flag: bool) -> None:
        """Show or hide the annotation layer."""

        self._layer.set_visible(flag)

    def annotations_visible(self) -> bool:
        """Return True when the annotation layer is visible."""

        return bool(self._layer.isVisible())

    def annotation_layer(self) -> AnnotationLayer:
        """Return the layer, mostly for tests and for the theme."""

        return self._layer

    def has_image(self) -> bool:
        """Return True when an image is on screen."""

        return bool(self._has_image)

    def last_error(self) -> str:
        """Return the reason of the last failed load, else ''."""

        return self._error

    def image_size(self):
        """Return the size of the image on screen, (0, 0) when empty."""

        if not self._has_image or self._pixmap_item is None:
            return (0, 0)
        rect = self._pixmap_item.boundingRect()
        return (int(rect.width()), int(rect.height()))

    def zoom_level(self) -> float:
        """Return the current zoom, 1.0 when the image just fits."""

        return float(self._zoom)

    def reset_view(self) -> None:
        """Fit the image inside the viewport and centre it."""

        if not self._has_image or self._pixmap_item is None:
            return
        self._zoom = 1.0
        self.resetTransform()
        rect = self._pixmap_item.boundingRect()
        viewport = self.viewport().rect()
        if rect.width() > 0 and rect.height() > 0:
            if viewport.width() > 0 and viewport.height() > 0:
                scale = min(
                    viewport.width() / rect.width(),
                    viewport.height() / rect.height(),
                )
                self.scale(scale, scale)
        self.setSceneRect(rect)
        self.centerOn(self._pixmap_item)

    def apply_zoom(self, factor) -> float:
        """Multiply the zoom by a factor and return the new level."""

        factor = _to_float(factor, 0.0)
        if factor <= 0.0 or not self._has_image:
            return self._zoom
        target = self._zoom * factor
        if target < 1.0:
            self.reset_view()
            return self._zoom
        if target > MAX_ZOOM:
            factor = MAX_ZOOM / self._zoom
            target = MAX_ZOOM
            if factor <= 1.0:
                return self._zoom
        self._zoom = target
        self.scale(factor, factor)
        return self._zoom

    def toggle_background(self) -> bool:
        """Switch the viewport between the theme and a plain colour.

        Returns:
            True when the plain background is now on.
        """

        self._plain_background = not self._plain_background
        self._apply_theme()
        return self._plain_background

    def plain_background(self) -> bool:
        """Return True when the plain background is on."""

        return bool(self._plain_background)

    # ---------------------------------------------------------- painting

    def _apply_theme(self) -> None:
        """Paint the viewport with the colour of the current mode."""

        if self._plain_background:
            color = BACKGROUND_DARK if _is_dark() else BACKGROUND_LIGHT
        else:
            color = get_theme().get("background", "#ffffff")
        self.setStyleSheet(_background_style(color))
        self._hint.setStyleSheet(
            f"color: {get_theme().get('text_secondary', '#86868b')};"
        )

    def _show_hint(self, text: str) -> None:
        """Show one line of text in the middle of the viewport."""

        self._hint.setText(str(text or ""))
        self._hint.setGeometry(self.viewport().rect())
        self._hint.show()
        self._hint.raise_()

    def _hide_hint(self) -> None:
        """Hide the text of the empty state."""

        self._hint.hide()

    def _apply_pixmap(self, pixmap, shapes: Optional[Sequence]) -> None:
        """Show one pixmap and keep the framing of the previous image."""

        previous = None
        prop_x = 0.5
        prop_y = 0.5
        if self._has_image and self._pixmap_item is not None:
            previous = self._zoom
            rect = self._pixmap_item.boundingRect()
            centre = self.mapToScene(self.viewport().rect().center())
            if rect.width() > 0 and rect.height() > 0:
                prop_x = _clamp(centre.x() / rect.width(), 0.0, 1.0)
                prop_y = _clamp(centre.y() / rect.height(), 0.0, 1.0)
        self._scene.removeItem(self._layer)
        self._scene.clear()
        self._scene.addItem(self._layer)
        self._layer.setZValue(10)
        self._pixmap_item = QtWidgets.QGraphicsPixmapItem(pixmap)
        self._scene.addItem(self._pixmap_item)
        self._has_image = True
        self._error = ""
        self._hide_hint()
        if shapes is not None:
            self._layer.build(shapes, self._expand_px)
        else:
            self._layer.clear()
        self.setSceneRect(self._pixmap_item.boundingRect())
        if previous is None:
            self.reset_view()
        else:
            self._restore_view(previous, prop_x, prop_y)

    def _restore_view(self, zoom: float, prop_x: float, prop_y: float):
        """Apply the zoom and the relative centre of the last image."""

        if self._pixmap_item is None:
            return
        self._zoom = 1.0
        self.resetTransform()
        rect = self._pixmap_item.boundingRect()
        viewport = self.viewport().rect()
        if rect.width() > 0 and rect.height() > 0:
            if viewport.width() > 0 and viewport.height() > 0:
                scale = min(
                    viewport.width() / rect.width(),
                    viewport.height() / rect.height(),
                )
                self.scale(scale, scale)
        self.setSceneRect(rect)
        if zoom > 1.0:
            self._zoom = min(zoom, MAX_ZOOM)
            self.scale(self._zoom, self._zoom)
        self.centerOn(prop_x * rect.width(), prop_y * rect.height())

    # ------------------------------------------------------------ events

    def resizeEvent(self, event) -> None:
        """Keep the empty state centred."""

        super().resizeEvent(event)
        self._hint.setGeometry(self.viewport().rect())

    def wheelEvent(self, event) -> None:
        """Zoom with the wheel, up to MAX_ZOOM."""

        if not self._has_image:
            event.ignore()
            return
        delta = event.angleDelta().y()
        if not delta:
            event.ignore()
            return
        factor = ZOOM_STEP if delta > 0 else 1.0 / ZOOM_STEP
        self.apply_zoom(factor)
        event.accept()

    def mousePressEvent(self, event) -> None:
        """Start a pan with the left button."""

        if (
            event.button() == QtCore.Qt.MouseButton.LeftButton
            and self._has_image
        ):
            self._panning = True
            self._pan_origin = event.pos()
            self.setCursor(QtCore.Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        """Pan and publish the pointer position in image pixels."""

        self._emit_pixel(event.pos())
        if self._panning:
            delta = event.pos() - self._pan_origin
            self._pan_origin = event.pos()
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - delta.x()
            )
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - delta.y()
            )
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        """End the pan."""

        if (
            event.button() == QtCore.Qt.MouseButton.LeftButton
            and self._panning
        ):
            self._panning = False
            self.unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        """Fit the image again on a double click."""

        if (
            event.button() == QtCore.Qt.MouseButton.LeftButton
            and self._has_image
        ):
            self.reset_view()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def leaveEvent(self, event) -> None:
        """Forget the pointer position."""

        self.pixel_moved.emit(-1, -1)
        super().leaveEvent(event)

    def dragEnterEvent(self, event) -> None:
        """Accept a dropped directory, refuse anything else."""

        if first_directory(event.mimeData()):
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event) -> None:
        """Keep the drag alive over the view."""

        if first_directory(event.mimeData()):
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event) -> None:
        """Report the dropped directory to the dialog."""

        path = first_directory(event.mimeData())
        if not path:
            event.ignore()
            return
        event.acceptProposedAction()
        self.directory_dropped.emit(path)

    # ----------------------------------------------------------- private

    def _emit_pixel(self, position) -> None:
        """Publish one pointer position in image pixels."""

        if not self._has_image or self._pixmap_item is None:
            self.pixel_moved.emit(-1, -1)
            return
        scene_pos = self.mapToScene(position)
        rect = self._pixmap_item.boundingRect()
        if rect.contains(scene_pos):
            self.pixel_moved.emit(int(scene_pos.x()), int(scene_pos.y()))
            return
        self.pixel_moved.emit(-1, -1)


def _to_float(value, default: float = 0.0) -> float:
    """Return one value as a float, or the default."""

    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    """Clamp one value between two bounds."""

    return max(low, min(high, value))


def _is_dark() -> bool:
    """Return True when the active theme is the dark one."""

    return get_mode() == "dark"


PreviewView.MAX_ZOOM = MAX_ZOOM
PreviewView.ZOOM_STEP = ZOOM_STEP
