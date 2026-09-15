"""The annotation layer drawn on top of the previewed image.

One shape becomes one outline plus one text: the outline is the minimum
bounding box of its points, which keeps every LabelMe type readable
without a per type painter, and the text is "label score WxH". A
missing score is printed as a dash and never as 0.00, because the
client labels carry no score at all.

The only shape that grows is the rectangle: the expand parameter of the
dialog is applied to a rectangle alone, a rotation is never enlarged.
Every point of the shapes of core is a plain tuple, so this class is the
only place where a QPointF is built.
"""

from __future__ import annotations

import math
from typing import Iterable, List, Optional, Sequence, Tuple

from PyQt6 import QtCore, QtGui, QtWidgets

from anylabeling.views.labeling.utils.theme import get_mode

__all__ = [
    "LINE_WIDTH",
    "TEXT_PIXEL_SIZE",
    "AnnotationLayer",
    "expand_rect",
    "outline_corners",
    "palette",
    "shape_text",
]

LINE_WIDTH = 2.0
TEXT_PIXEL_SIZE = 11
TEXT_GAP = 2.0

_PALETTE_LIGHT = (
    "#C94F4F",
    "#C07A2A",
    "#9A7B12",
    "#3E7E5B",
    "#3A72B0",
    "#8E4FA8",
)

_PALETTE_DARK = (
    "#E06C75",
    "#D19A66",
    "#E5C07B",
    "#7EB896",
    "#61AFEF",
    "#C678DD",
)


def palette() -> Tuple[str, ...]:
    """Return the outline palette of the active theme."""

    return _PALETTE_DARK if get_mode() == "dark" else _PALETTE_LIGHT


def _coordinate(point, index: int, default: float = 0.0) -> float:
    """Return one coordinate of a point, tuple or QPointF alike."""

    try:
        if isinstance(point, (tuple, list)):
            return float(point[index])
        if index == 0:
            return float(point.x())
        return float(point.y())
    except (TypeError, ValueError, IndexError, AttributeError):
        return default


def expand_rect(points: Sequence, pixels: float) -> List[QtCore.QPointF]:
    """Return the four corners of a rectangle grown on every side.

    Args:
        points: The points of the rectangle, tuples or QPointF.
        pixels: How far each side moves outwards; negative values are
            read as zero.

    Returns:
        Four corners in clockwise order, empty when there is no point.
    """

    xs = [_coordinate(point, 0) for point in points or ()]
    ys = [_coordinate(point, 1) for point in points or ()]
    if not xs or not ys:
        return []
    try:
        pad = max(0.0, float(pixels))
    except (TypeError, ValueError):
        pad = 0.0
    x0, x1 = min(xs) - pad, max(xs) + pad
    y0, y1 = min(ys) - pad, max(ys) + pad
    return [
        QtCore.QPointF(x0, y0),
        QtCore.QPointF(x1, y0),
        QtCore.QPointF(x1, y1),
        QtCore.QPointF(x0, y1),
    ]


def _box_corners(points: Sequence) -> List[QtCore.QPointF]:
    """Return the four corners of the bounding box of some points."""

    xs = [_coordinate(point, 0) for point in points or ()]
    ys = [_coordinate(point, 1) for point in points or ()]
    if not xs or not ys:
        return []
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    if x0 == x1:
        x0 -= 1.0
        x1 += 1.0
    if y0 == y1:
        y0 -= 1.0
        y1 += 1.0
    return [
        QtCore.QPointF(x0, y0),
        QtCore.QPointF(x1, y0),
        QtCore.QPointF(x1, y1),
        QtCore.QPointF(x0, y1),
    ]


def _circle_corners(points: Sequence) -> List[QtCore.QPointF]:
    """Return the box of a circle, built from its centre and radius."""

    if len(points) < 2:
        return _box_corners(points)
    cx = _coordinate(points[0], 0)
    cy = _coordinate(points[0], 1)
    dx = _coordinate(points[1], 0) - cx
    dy = _coordinate(points[1], 1) - cy
    radius = math.hypot(dx, dy)
    if radius <= 0.0:
        return _box_corners(points)
    return [
        QtCore.QPointF(cx - radius, cy - radius),
        QtCore.QPointF(cx + radius, cy - radius),
        QtCore.QPointF(cx + radius, cy + radius),
        QtCore.QPointF(cx - radius, cy + radius),
    ]


def outline_corners(shape, expand_px: float = 0.0) -> List[QtCore.QPointF]:
    """Return the corners of the outline of one shape.

    Only a rectangle honours expand_px; every other type keeps its own
    bounding box.
    """

    points = tuple(getattr(shape, "points", ()) or ())
    shape_type = str(getattr(shape, "shape_type", "") or "")
    if not points:
        return []
    if shape_type == "rectangle" and expand_px:
        return expand_rect(points, expand_px)
    if shape_type == "circle":
        return _circle_corners(points)
    return _box_corners(points)


def _number(value, default: float = 0.0) -> float:
    """Return one size value as a float."""

    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def shape_text(shape) -> str:
    """Return the label text of one shape, '-' when it has no score."""

    label = str(getattr(shape, "label", "") or "")
    score = getattr(shape, "score", None)
    try:
        score_text = "-" if score is None else f"{float(score):.2f}"
    except (TypeError, ValueError):
        score_text = "-"
    width = _number(getattr(shape, "width", 0.0))
    height = _number(getattr(shape, "height", 0.0))
    return f"{label} {score_text} {width:.0f}×{height:.0f}px"


class AnnotationLayer(QtWidgets.QGraphicsItemGroup):
    """Every outline and every label of the image on screen."""

    def __init__(self, parent=None):
        """Build an empty layer."""

        super().__init__(parent)
        self._shapes: Tuple = ()
        self._expand_px = 0.0
        self._colors = {}
        self._items: List[QtWidgets.QGraphicsItem] = []
        self.setZValue(10)

    # -------------------------------------------------------------- api

    def build(self, shapes: Iterable, expand_px: Optional[float] = None):
        """Replace the layer with the outlines of some shapes.

        Args:
            shapes: The core.ShapeInfo sequence of one image.
            expand_px: New expand value; None keeps the current one.

        Returns:
            The shapes that were drawn.
        """

        if expand_px is not None:
            self._expand_px = self._clamp_px(expand_px)
        self.clear()
        self._shapes = tuple(shapes or ())
        for shape in self._shapes:
            self._add_shape(shape)
        return self._shapes

    def set_expand_px(self, pixels) -> None:
        """Set the expand value and rebuild the layer."""

        value = self._clamp_px(pixels)
        if value == self._expand_px:
            return
        self._expand_px = value
        shapes = self._shapes
        self.build(shapes, value)

    def set_visible(self, flag: bool) -> None:
        """Show or hide the whole layer."""

        self.setVisible(bool(flag))

    def clear(self) -> None:
        """Remove every child of the layer."""

        for item in self._items:
            if item.scene() is not None:
                self.removeFromGroup(item)
                item.scene().removeItem(item)
            else:
                self.removeFromGroup(item)
        self._items = []
        self._shapes = ()

    def shapes(self) -> Tuple:
        """Return the shapes currently drawn."""

        return self._shapes

    def expand_px(self) -> float:
        """Return the current expand value."""

        return float(self._expand_px)

    def texts(self) -> List[str]:
        """Return the label texts of the layer, in order."""

        texts = []
        for item in self._items:
            if isinstance(item, QtWidgets.QGraphicsSimpleTextItem):
                texts.append(item.text())
        return texts

    def rects(self) -> List[QtCore.QRectF]:
        """Return the bounding rects of the outlines, in order."""

        rects = []
        for item in self._items:
            if isinstance(item, QtWidgets.QGraphicsPolygonItem):
                rects.append(item.polygon().boundingRect())
        return rects

    def color_for_label(self, label: str) -> QtGui.QColor:
        """Return the colour of one label, assigned on first use."""

        key = str(label or "")
        color = self._colors.get(key)
        if color is not None:
            return color
        colors = palette()
        color = QtGui.QColor(colors[len(self._colors) % len(colors)])
        self._colors[key] = color
        return color

    def reset_colors(self) -> None:
        """Forget the colours, a new theme assigns them again."""

        self._colors = {}

    # ---------------------------------------------------------- private

    @staticmethod
    def _clamp_px(pixels) -> float:
        """Return a sane, non negative expand value."""

        try:
            value = float(pixels)
        except (TypeError, ValueError):
            return 0.0
        if value < 0.0:
            return 0.0
        return value

    def _add_shape(self, shape) -> None:
        """Add the outline and the text of one shape."""

        corners = outline_corners(shape, self._expand_px)
        if not corners:
            return
        color = self.color_for_label(getattr(shape, "label", ""))
        polygon = QtWidgets.QGraphicsPolygonItem()
        polygon.setPolygon(QtGui.QPolygonF(corners))
        pen = QtGui.QPen(color, LINE_WIDTH)
        pen.setCosmetic(True)
        polygon.setPen(pen)
        polygon.setBrush(QtGui.QColor(0, 0, 0, 0))
        self.addToGroup(polygon)
        self._items.append(polygon)

        text = QtWidgets.QGraphicsSimpleTextItem(shape_text(shape))
        font = QtGui.QFont(text.font())
        font.setPixelSize(TEXT_PIXEL_SIZE)
        text.setFont(font)
        text.setBrush(color)
        left = min(point.x() for point in corners)
        top = min(point.y() for point in corners)
        position = top - TEXT_PIXEL_SIZE - TEXT_GAP
        if position < 0.0:
            position = top + TEXT_GAP
        text.setPos(left, position)
        self.addToGroup(text)
        self._items.append(text)
