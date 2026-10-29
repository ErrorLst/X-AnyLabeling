"""The two read-only image panes of the filter window.

One pane shows exactly one image. It fits the image into the viewport
by default, the wheel zooms around the pointer, the left button pans
and a double click fits the image again. The empty state is a label in
the viewport, so it stays centred without touching the scene.
"""

from __future__ import annotations

from PyQt6 import QtCore, QtGui, QtWidgets

__all__ = [
    "EMPTY_TEXT",
    "MAX_ZOOM",
    "ZOOM_STEP",
    "ImageView",
]

EMPTY_TEXT = "主窗口没有打开图片"
MAX_ZOOM = 20.0
ZOOM_STEP = 1.15


class ImageView(QtWidgets.QGraphicsView):
    """One image with fit, wheel zoom, pan and double click reset."""

    def __init__(self, placeholder=EMPTY_TEXT, parent=None):
        super().__init__(parent)
        self._scene = QtWidgets.QGraphicsScene(self)
        self.setScene(self._scene)
        self._pixmap_item = None
        self._zoom = 1.0
        self._panning = False
        self._sync_peer = None
        self._syncing = False
        self._pan_origin = QtCore.QPoint(0, 0)
        self._placeholder = QtWidgets.QLabel(str(placeholder or ""), self)
        self._placeholder.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setWordWrap(True)
        self._placeholder.setAttribute(
            QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )
        self.setTransformationAnchor(
            QtWidgets.QGraphicsView.ViewportAnchor.AnchorUnderMouse
        )
        self.setResizeAnchor(
            QtWidgets.QGraphicsView.ViewportAnchor.AnchorUnderMouse
        )
        self.setDragMode(QtWidgets.QGraphicsView.DragMode.NoDrag)
        self.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform)
        self.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.setVerticalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._placeholder.setGeometry(self.viewport().rect())

    # ------------------------------------------------------------- api

    def set_pixmap(self, pixmap):
        """Show one pixmap; a same size image keeps zoom and scroll."""

        if pixmap is None or pixmap.isNull():
            self.clear()
            return False
        previous = None
        if self._pixmap_item is not None:
            previous = self._pixmap_item.pixmap().size()
        if self._pixmap_item is None:
            self._pixmap_item = QtWidgets.QGraphicsPixmapItem()
            self._scene.addItem(self._pixmap_item)
        self._pixmap_item.setPixmap(pixmap)
        rect = self._pixmap_item.boundingRect()
        self.setSceneRect(rect)
        self._placeholder.hide()
        if previous is None or previous != pixmap.size():
            self.reset_view()
        return True

    def set_image(self, image):
        """Show one QImage, returning False for a null image."""

        if image is None or image.isNull():
            self.clear()
            return False
        return self.set_pixmap(QtGui.QPixmap.fromImage(image))

    def clear(self):
        """Drop the image and show the empty state again."""

        if self._pixmap_item is not None:
            self._scene.removeItem(self._pixmap_item)
            self._pixmap_item = None
        self._zoom = 1.0
        self.resetTransform()
        self.setSceneRect(QtCore.QRectF())
        self._placeholder.setGeometry(self.viewport().rect())
        self._placeholder.show()
        self._placeholder.raise_()

    def pixmap(self):
        """Return the pixmap on screen, or None."""

        if self._pixmap_item is None:
            return None
        return self._pixmap_item.pixmap()

    def has_image(self):
        """Return True when an image is on screen."""

        return self._pixmap_item is not None

    def reset_view(self):
        """Fit the image inside the viewport and centre it."""

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
                if scale > 0:
                    self.scale(scale, scale)
        self.setSceneRect(rect)
        self.centerOn(self._pixmap_item)
        self._sync_to_peer()

    def zoom_level(self):
        """Return the zoom relative to the fitted view."""

        return float(self._zoom)

    def sync_with(self, peer):
        """Link this pane with another one for zoom and pan.

        Both directions are linked, so the user may zoom or drag either
        pane. The peer is aligned immediately when both already show an
        image.
        """

        if peer is None or peer is self:
            return
        self._sync_peer = peer
        peer._sync_peer = self
        if self.has_image() and peer.has_image():
            peer.sync_from(self)

    def sync_from(self, other):
        """Adopt the transform and scene centre of another pane."""

        if (
            self._syncing
            or other is self
            or self._pixmap_item is None
            or other._pixmap_item is None
        ):
            return
        own = self._pixmap_item.boundingRect().size()
        theirs = other._pixmap_item.boundingRect().size()
        if own != theirs:
            return
        self._syncing = True
        try:
            self.setTransform(other.transform())
            self._zoom = other.zoom_level()
            if self.viewport().size() == other.viewport().size():
                self.horizontalScrollBar().setValue(
                    other.horizontalScrollBar().value()
                )
                self.verticalScrollBar().setValue(
                    other.verticalScrollBar().value()
                )
            else:
                centre = other.mapToScene(other.viewport().rect().center())
                self.centerOn(centre)
        finally:
            self._syncing = False

    def _sync_to_peer(self):
        if self._sync_peer is None or self._syncing:
            return
        self._sync_peer.sync_from(self)

    def apply_zoom(self, factor, anchor=None):
        """Multiply the zoom, keeping the anchor point in place."""

        try:
            factor = float(factor)
        except (TypeError, ValueError):
            return self._zoom
        if factor <= 0.0 or self._pixmap_item is None:
            return self._zoom
        scene_point = None
        if anchor is not None:
            scene_point = self.mapToScene(QtCore.QPoint(anchor))
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
        if scene_point is not None:
            self._keep_anchor(scene_point, anchor)
        self._sync_to_peer()
        return self._zoom

    # ---------------------------------------------------------- events

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._placeholder.setGeometry(self.viewport().rect())
        if self._zoom <= 1.01:
            self.reset_view()

    def showEvent(self, event):
        super().showEvent(event)
        self._placeholder.setGeometry(self.viewport().rect())
        if self._zoom <= 1.01:
            self.reset_view()

    def wheelEvent(self, event):
        if self._pixmap_item is None:
            event.ignore()
            return
        delta = event.angleDelta().y()
        if not delta:
            event.ignore()
            return
        factor = ZOOM_STEP if delta > 0 else 1.0 / ZOOM_STEP
        self.apply_zoom(factor, event.position().toPoint())
        event.accept()

    def mousePressEvent(self, event):
        if (
            event.button() == QtCore.Qt.MouseButton.LeftButton
            and self._pixmap_item is not None
        ):
            self._panning = True
            self._pan_origin = event.pos()
            self.setCursor(QtCore.Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._panning:
            delta = event.pos() - self._pan_origin
            self._pan_origin = event.pos()
            horizontal = self.horizontalScrollBar()
            vertical = self.verticalScrollBar()
            horizontal.setValue(horizontal.value() - delta.x())
            vertical.setValue(vertical.value() - delta.y())
            self._sync_to_peer()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if (
            event.button() == QtCore.Qt.MouseButton.LeftButton
            and self._panning
        ):
            self._panning = False
            self.unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if (
            event.button() == QtCore.Qt.MouseButton.LeftButton
            and self._pixmap_item is not None
        ):
            self.reset_view()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    # --------------------------------------------------------- private

    def _keep_anchor(self, scene_point, anchor):
        point = QtCore.QPoint(anchor)
        mapped = self.mapFromScene(scene_point)
        horizontal = self.horizontalScrollBar()
        vertical = self.verticalScrollBar()
        horizontal.setValue(horizontal.value() + mapped.x() - point.x())
        vertical.setValue(vertical.value() + mapped.y() - point.y())
