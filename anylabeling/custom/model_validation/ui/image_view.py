"""QPainter based image view with GT / prediction overlays.

The canvas is a viewer, never an editor: it draws one image in its own
pixel coordinate system (origin at the top left corner) with an optional
ground truth and an optional prediction overlay, and it owns exactly one
view state - a zoom factor and the image point shown at the centre of
the widget. Every other view question (where does a pixel land, which
image pixel sits under the mouse) is derived from those two numbers, so
two canvases showing the same image can share one state.

The wheel zoom is animated: one notch moves a *target* zoom and a timer
walks the current zoom towards it in small steps, therefore the picture
never jumps from one scale to the next.

Every box is labelled in place: the label of the shape, plus the score
of a prediction, is written just above the upper left corner of its own
box. The text is painted in widget space and therefore keeps one readable
screen size at every zoom level, it never grows with the picture and it
is never shrunk into an unreadable speck. That is why the label pass
resets the painter to the widget coordinate system before it measures or
writes anything: the image transform of the shapes would scale the text
and displace it away from its own box.

One switch is owned by the canvas: show_labels hides every label text.
A detection box is only ever stroked - the inside of a box is never
filled, so the picture under it stays visible - and a label itself NEVER
carries a background plate: it is always written as white glyphs with a
thin dark outline, which stays readable on a flat as well as on a busy
picture.

The colour of an outline is the judgement of the box it belongs to. A
canvas is handed the verdict of the matching that produced its shapes
(see shape_colors) and paints every box with the colour of its own
state: a matched GT stays GT_COLOR and a matched prediction stays
PRED_COLOR, a GT no prediction met - a miss, the false negative of the
run - turns MISS_COLOR, a prediction no GT met - a false positive -
turns FALSE_POSITIVE_COLOR, a matched pair of two different classes
turns CLASS_MISMATCH_COLOR and a pair whose IoU stayed under the
threshold turns IOU_BELOW_COLOR. Both canvases read those states from
the one matching of the record, so a defect keeps its colour on both
sides. A record without a judgement - an augmented copy that was never
judged, a skipped record - carries no state at all and keeps the plain
GT / Pred colour of every box.

The canvas is a viewer and nothing else: it owns no edit state, no
selection and no handle, and it never writes an annotation. A box is
picked, moved and renamed in the main window, never here, so every
gesture of this widget - the wheel, the left button drag and the double
click - only changes the view and the way the two overlays are painted.
"""

from __future__ import annotations

import math
import os.path as osp
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PyQt6 import QtCore, QtGui, QtWidgets

GT_COLOR = QtGui.QColor(46, 204, 113)
PRED_COLOR = QtGui.QColor(52, 152, 219)
BOX_COLOR = QtGui.QColor(241, 196, 15)
# The judgement colours. A box the matching left on its own outline is
# punched out of the crowd: a GT no prediction met (a miss, the false
# negative of the run) is orange red, a prediction no GT met (a false
# positive) is magenta, while a matched pair of two different classes is
# orange, a pair whose IoU stayed under the threshold is dark yellow and
# a matched prediction whose score stayed under the NG score threshold of
# the run is teal. A box whose state is unknown - a matched pair, a
# record nobody judged - keeps the plain GT / Pred colour above.
MISS_COLOR = QtGui.QColor(231, 76, 60)
FALSE_POSITIVE_COLOR = QtGui.QColor(155, 89, 182)
CLASS_MISMATCH_COLOR = QtGui.QColor(230, 126, 34)
IOU_BELOW_COLOR = QtGui.QColor(241, 196, 15)
# The colour of a low score prediction: the box of a matched pair whose
# prediction score stayed under the NG score threshold of the run. It is
# a teal green, chosen to sit beside the other six colours above without
# being mistaken for the plain GT green, the blue of a prediction or the
# dark yellow of a pair under the IoU threshold.
LOW_SCORE_COLOR = QtGui.QColor(26, 188, 156)
# The states a box can carry. Three of them are the states a matched
# pair of the judge detail is written with (a class mismatch, an IoU
# under the threshold, a score under the NG score threshold); the other
# two are the states of a shape no match points at - a GT no prediction
# met and a prediction no GT met - which the reader of the detail derives
# from its missed and false positive indexes. The state of a matched
# pair that is fine keeps the plain colour of its canvas, and so does a
# pair of two labels that are not even known (see
# results_page.matched_pair_state).
MATCH_COLOR = GT_COLOR
STATE_OK_PAIR = "OK_PAIR"
STATE_MISS = "MISS"
STATE_FALSE_POSITIVE = "FALSE_POSITIVE"
STATE_CLASS_MISMATCH = "CLASS_MISMATCH"
STATE_IOU_BELOW = "IOU_BELOW"
STATE_LOW_SCORE = "LOW_SCORE"

# The colour of the six judgement states. The map is keyed by the state
# of a shape; a state it does not carry - OK_PAIR, an empty state, a
# state of a later revision - has no colour of its own and keeps the
# plain colour of the canvas it is drawn on. The states of a canvas are
# a display side copy: the record itself is never written to.
STATE_TO_COLOR = {
    STATE_MISS: MISS_COLOR,
    STATE_FALSE_POSITIVE: FALSE_POSITIVE_COLOR,
    STATE_CLASS_MISMATCH: CLASS_MISMATCH_COLOR,
    STATE_IOU_BELOW: IOU_BELOW_COLOR,
    STATE_LOW_SCORE: LOW_SCORE_COLOR,
}
TEXT_COLOR = QtGui.QColor(255, 255, 255)
BACKGROUND_COLOR = QtGui.QColor(24, 26, 30)
POINT_RADIUS = 3.0
OVERLAY_PEN_WIDTH = 2.0

# The label of a box is written with one font size in widget pixels, so
# it keeps the same readable size whatever the zoom. It is measured and
# placed in that very widget space: the anchor is the upper left corner
# of the box, the text sits LABEL_GAP pixels above it and is only moved
# below the anchor when the room above is missing. A box two classes met
# on carries one such block with a row per class: the block is measured
# and placed as a whole, never row by row.
LABEL_POINT_SIZE = 8.0
LABEL_PADDING = 3.0
# The distance between two rows of a multi line label, in widget
# pixels: the label block of a multi class box moves every row its own
# step below the one before it, so the rows never drift with the zoom
# either. It is measured on the ink of two rows (see _stacked_glyphs).
LABEL_LINE_SPACING = 1.0
LABEL_GAP = 2.0
LABEL_MARGIN = 1.0
# A label may touch the top and the bottom border of the widget - that
# is what lets it flip right under a corner sitting on the top edge - but
# it always keeps LABEL_MARGIN away from the left and the right border.
LABEL_VERTICAL_MARGIN = 0.0
# A label never carries a background plate: the white glyphs are stroked
# with this dark outline and filled on top of it, which is what keeps
# them readable on a bright as well as on a dark picture. The outline is
# centred on the border of the glyphs and is painted wide enough that the
# pale fill drawn on top of it leaves a visible dark edge behind: a
# hairline outline is covered by the fill of its own text.
LABEL_OUTLINE = QtGui.QColor(0, 0, 0, 200)
LABEL_OUTLINE_WIDTH = 3.0

# A detection box is drawn with its outline alone: the inside of a box is
# never filled, neither with a plate nor with a tint, so the picture under
# every box stays visible on both canvases. Only the colour of the outline
# carries the two things a box says: whether it is a GT (green) or a Pred
# (blue) box, and what the matching of the record did to it (see
# STATE_TO_COLOR below).

# a prediction is shown with its score, and the score is rounded to two
# decimals: "a0_dian 0.86"
SCORE_FORMAT = "{:.2f}"

# The zoom factor is relative to the fit view (1.0 shows the whole image
# inside the widget), so 0.1x and 10x mean the same thing whatever the
# size of the picture or of the canvas.
MIN_ZOOM = 0.1
MAX_ZOOM = 10.0
FIT_PADDING = 0.99

# one wheel notch (120 units) multiplies the target zoom by this factor
WHEEL_DELTA = 120.0
WHEEL_ZOOM_STEP = 1.2
# the animation walks a fixed fraction of the remaining distance (in log
# space) every tick: every step is small, monotone and never overshoots
ZOOM_ANIM_INTERVAL_MS = 16
ZOOM_ANIM_FRACTION = 0.35
ZOOM_ANIM_EPSILON = 1e-3


def load_pixmap(path: str) -> Optional[QtGui.QPixmap]:
    """Load an image from a staging path, tolerating non ASCII paths."""

    if not path or not osp.isfile(path):
        return None
    data = QtCore.QByteArray()
    with open(path, "rb") as handle:
        data.append(handle.read())
    pixmap = QtGui.QPixmap()
    if not pixmap.loadFromData(data):
        return None
    return pixmap


def shape_points(shape: Any) -> List[Tuple[float, float]]:
    """Return the float point list of a shape dict or Shape object."""

    if isinstance(shape, dict):
        raw = shape.get("points") or []
    else:
        raw = getattr(shape, "points", []) or []
    points: List[Tuple[float, float]] = []
    for item in raw:
        if hasattr(item, "x") and hasattr(item, "y"):
            points.append((float(item.x()), float(item.y())))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            points.append((float(item[0]), float(item[1])))
    return points


def shape_type_of(shape: Any) -> str:
    """Return the shape type of a shape dict or Shape object."""

    if isinstance(shape, dict):
        return str(shape.get("shape_type") or "polygon")
    return str(getattr(shape, "shape_type", "polygon") or "polygon")


def shape_label(shape: Any) -> str:
    """Return the label of a shape dict or Shape object."""

    if isinstance(shape, dict):
        return str(shape.get("label", ""))
    return str(getattr(shape, "label", ""))


def shape_score(shape: Any) -> Optional[float]:
    """Return the score of a shape, None when it carries none.

    Only a prediction carries a score: a ground truth shape of the
    staging labels has no score key (or holds None) and is therefore
    drawn with its label alone.
    """

    if isinstance(shape, dict):
        value = shape.get("score")
    else:
        value = getattr(shape, "score", None)
    if value is None or isinstance(value, bool):
        return None
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(score) or math.isinf(score):
        return None
    return score


def shape_label_rows(shape: Any) -> List[Tuple[str, Optional[float]]]:
    """Return the label rows of a box, one entry per class it carries.

    The predictions of one box the matching merged may carry several
    classes: the merge writes the parallel lists `labels` and `scores`
    on the shape - in score descending order, the main `label` and
    `score` staying the highest scoring pair - and the box has to show
    every one of them. The rows are read from those two lists, in the
    very order they were given.

    A shape without them - a ground truth box, a prediction the merge
    left alone, a record of an older revision - falls back to the one
    row of its own label and score, which is exactly what a single
    label box showed before. The two lists are only read while they are
    parallel and longer than one entry: a shape carrying a single row,
    or two lists that do not line up, keeps the plain fallback and is
    therefore never mislabelled with half of a pair.
    """

    if isinstance(shape, dict):
        labels = shape.get("labels")
        scores = shape.get("scores")
    else:
        labels = getattr(shape, "labels", None)
        scores = getattr(shape, "scores", None)
    if (
        isinstance(labels, (list, tuple))
        and isinstance(scores, (list, tuple))
        and len(labels) == len(scores)
        and len(labels) > 1
    ):
        return [
            (str(label), _as_score(score))
            for label, score in zip(labels, scores)
        ]
    return [(shape_label(shape), shape_score(shape))]


def _as_score(value: Any) -> Optional[float]:
    """Return one entry of a score list, None when it carries none.

    A row is written without a score when its number is missing or is
    not a finite number, which is the same rule the single score of a
    shape follows (see shape_score).
    """

    if value is None or isinstance(value, bool):
        return None
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(score) or math.isinf(score):
        return None
    return score


def shape_label_text(shape: Any) -> str:
    """Return the text drawn at the upper left corner of one box.

    One row per class the box carries, joined by a newline: the single
    class box of a ground truth is still the one row it always was, and
    the multi class box of a merge writes its classes one under the
    other (see shape_label_rows). Every row is formatted like the one
    label of the previous revision: "label 0.86" for a row with a score
    and only the label for a row without one.
    """

    rows: List[str] = []
    for label, score in shape_label_rows(shape):
        text = label.strip()
        if score is not None:
            written = SCORE_FORMAT.format(score)
            text = text + " " + written if text else written
        rows.append(text)
    return "\n".join(rows)


def shape_color(status: Any) -> Optional[QtGui.QColor]:
    """Return the colour of a judgement status, None when it has none.

    A status that is not one of the known states - an empty string, a
    state a later revision added - has no colour of its own and is drawn
    with the plain colour of its canvas instead of being mislabelled as
    a miss or a false positive.
    """

    if status is None:
        return None
    return STATE_TO_COLOR.get(str(status))


def shape_colors(
    shapes: Sequence[Any], default: QtGui.QColor, statuses: Any = None
) -> List[QtGui.QColor]:
    """Return one outline colour per shape of a canvas.

    The statuses are the states of the very same shapes, entry by entry:
    the state of a shape is the one its own index points at. A status
    list that does not line up with the shapes - a record whose
    judgement came from another revision, a caller that handed the
    states of another list - is refused as a whole, because a colour
    read from a shifted index would flag the wrong box. Every shape then
    keeps the plain colour of its canvas.

    The result is a new list: the canvas copies the states it paints and
    never writes one back into the record it was handed.
    """

    aligned = isinstance(statuses, (list, tuple)) and len(statuses) == len(
        shapes
    )
    colors: List[QtGui.QColor] = []
    for index, _shape in enumerate(shapes):
        color = shape_color(statuses[index]) if aligned else None
        colors.append(color if color is not None else default)
    return colors


def clamp_zoom(value: float) -> float:
    """Clamp a zoom factor into the supported [MIN_ZOOM, MAX_ZOOM] range."""

    return float(min(max(float(value), MIN_ZOOM), MAX_ZOOM))


class ImageCanvas(QtWidgets.QWidget):
    """Draw one image with its overlays and share one zoom / pan state."""

    # (view scale, image x at the widget centre, image y at the centre)
    view_changed = QtCore.pyqtSignal(float, float, float)

    def __init__(self, title: str = "", parent: Optional[Any] = None) -> None:
        super().__init__(parent)
        self.title = title
        self._pixmap: Optional[QtGui.QPixmap] = None
        self._ground_truth: List[Any] = []
        self._predictions: List[Any] = []
        # the judgement state of every entry of the two overlays (see
        # set_shapes): entry by entry, so the state of a shape is the one
        # its own index points at
        self._gt_statuses: List[Any] = []
        self._pred_statuses: List[Any] = []
        # the colours of the last ground truth pass, built by the one
        # builder of set_shapes and reused by the repaint instead of
        # walking the list twice (see paintEvent)
        self._gt_colors: List[QtGui.QColor] = []
        self.matches: List[Dict[str, Any]] = []
        self.show_ground_truth = True
        self.show_predictions = True
        self.show_overlay = True
        # the one display switch of the overlays: hiding the labels skips
        # the whole text pass, the shapes themselves are always painted
        # the very same way
        self.show_labels = True
        # view state: the zoom is relative to the fit view and the
        # centre is the image point drawn at the middle of the widget
        self._fit_scale = 1.0
        self._zoom = 1.0
        self._target_zoom = 1.0
        self._center = QtCore.QPointF(0.0, 0.0)
        self._anchor_image = QtCore.QPointF(0.0, 0.0)
        self._anchor_widget = QtCore.QPointF(0.0, 0.0)
        self._user_adjusted = False
        self._panning = False
        self._pan_origin = QtCore.QPointF(0.0, 0.0)
        self._zoom_timer = QtCore.QTimer(self)
        self._zoom_timer.setInterval(ZOOM_ANIM_INTERVAL_MS)
        self._zoom_timer.timeout.connect(self.advance_zoom_animation)
        self.setMinimumSize(320, 320)
        self.setMouseTracking(True)

    # ------------------------------------------------------------ content
    def set_image(self, pixmap: Optional[QtGui.QPixmap]) -> None:
        """Set the displayed image and fit a fresh one to the widget."""

        fresh = self._pixmap is None or pixmap is None
        if not fresh and pixmap is not None and self._pixmap is not None:
            fresh = pixmap.cacheKey() != self._pixmap.cacheKey()
        self._pixmap = pixmap
        if fresh:
            self.fit_view()
        else:
            self.update()

    def set_shapes(
        self,
        ground_truth: Sequence[Any],
        predictions: Sequence[Any],
        ground_truth_statuses: Sequence[Any] = (),
        prediction_statuses: Sequence[Any] = (),
    ) -> None:
        """Set the overlays and the judgement state of every shape.

        The two status lists are optional and are the states of the
        shapes in the very same order: the state of a shape is the one
        its own index points at, and a list that does not line up with
        its shapes is refused as a whole (see shape_colors), so a box is
        never coloured by the state of another box. Only the two lists
        are kept, never the record that produced them.

        The colours of the ground truth pass are derived here and not
        only in paintEvent, so a canvas that was handed the shapes of
        another record never keeps a colour of the previous one (see
        _gt_outline_colors).
        """

        self._ground_truth = list(ground_truth)
        self._predictions = list(predictions)
        self._gt_statuses = list(ground_truth_statuses or [])
        self._pred_statuses = list(prediction_statuses or [])
        self._gt_colors = self._gt_outline_colors()
        self.update()

    def _gt_outline_colors(self) -> List[QtGui.QColor]:
        """Return the outline colour of every ground truth box.

        The list and the shapes it belongs to are built together, so a
        colour can never be left behind by another record: a canvas
        showing no ground truth (the checkbox is off, the record has no
        shape) answers the empty list and every box keeps the plain
        ground truth colour.
        """

        if not (self.show_overlay and self.show_ground_truth):
            return []
        return shape_colors(self._ground_truth, GT_COLOR, self._gt_statuses)

    def clear(self) -> None:
        """Reset the view."""

        self._pixmap = None
        self._ground_truth = []
        self._predictions = []
        self._gt_statuses = []
        self._pred_statuses = []
        # an emptied canvas has no ground truth left to colour either
        self._gt_colors = []
        self.matches = []
        self._fit_scale = 1.0
        self._zoom = 1.0
        self._target_zoom = 1.0
        self._center = QtCore.QPointF(0.0, 0.0)
        self._user_adjusted = False
        self._stop_zoom_animation()
        self.update()

    # ---------------------------------------------------------- view state
    def image_size(self) -> QtCore.QSizeF:
        """Return the size of the displayed image in image pixels."""

        if self._pixmap is None or self._pixmap.isNull():
            return QtCore.QSizeF(0.0, 0.0)
        size = self._pixmap.size()
        return QtCore.QSizeF(float(size.width()), float(size.height()))

    def _widget_center(self) -> QtCore.QPointF:
        """Return the middle of the widget."""

        return QtCore.QPointF(self.width() / 2.0, self.height() / 2.0)

    def _compute_fit_scale(self) -> float:
        """Return the widget pixels per image pixel of a full view."""

        size = self.image_size()
        if size.width() <= 0 or size.height() <= 0:
            return 1.0
        width = max(float(self.width()), 1.0)
        height = max(float(self.height()), 1.0)
        return FIT_PADDING * min(width / size.width(), height / size.height())

    def fit_scale(self) -> float:
        """Return the scale that shows the whole image."""

        return float(self._fit_scale)

    def zoom_factor(self) -> float:
        """Return the zoom relative to the fit view (1.0 = full image)."""

        return float(self._zoom)

    def target_zoom(self) -> float:
        """Return the zoom the animation is walking towards."""

        return float(self._target_zoom)

    def view_scale(self) -> float:
        """Return the current widget pixels per image pixel."""

        return float(self._fit_scale * self._zoom)

    def view_center(self) -> QtCore.QPointF:
        """Return a copy of the image point drawn at the widget centre."""

        return QtCore.QPointF(self._center)

    def view_state(self) -> Tuple[float, float, float]:
        """Return the (scale, centre x, centre y) of this canvas."""

        center = self._center
        return self.view_scale(), float(center.x()), float(center.y())

    def user_adjusted(self) -> bool:
        """Return True when the user moved the view by hand."""

        return bool(self._user_adjusted)

    def set_user_adjusted(self, adjusted: bool) -> None:
        """Record whether the view was moved by the user."""

        self._user_adjusted = bool(adjusted)

    def fit_view(self, emit: bool = True) -> None:
        """Show the whole image, centred: the first screen of a record."""

        self._fit_scale = self._compute_fit_scale()
        self._zoom = 1.0
        self._target_zoom = 1.0
        self._stop_zoom_animation()
        size = self.image_size()
        self._center = QtCore.QPointF(size.width() / 2.0, size.height() / 2.0)
        self._anchor_image = QtCore.QPointF(self._center)
        self._anchor_widget = self._widget_center()
        self._user_adjusted = False
        self.update()
        if emit:
            self._emit_view()

    def apply_view_state(
        self, scale: float, center_x: float, center_y: float
    ) -> None:
        """Adopt the view state of the other canvas (no signal back).

        The absolute scale travels between the two canvases, therefore
        both pictures show the very same image pixel at the very same
        size: one zoom, one anchor and one pan for the pair.
        """

        fit = self._fit_scale if self._fit_scale > 0 else 1.0
        self._zoom = clamp_zoom(float(scale) / fit)
        self._target_zoom = self._zoom
        self._center = QtCore.QPointF(float(center_x), float(center_y))
        self._anchor_image = QtCore.QPointF(self._center)
        self._anchor_widget = self._widget_center()
        self._stop_zoom_animation()
        self.update()

    # ------------------------------------------------------------ geometry
    def widget_to_image(self, point: QtCore.QPointF) -> QtCore.QPointF:
        """Return the image pixel drawn under a widget position."""

        scale = self.view_scale()
        if scale <= 0:
            return QtCore.QPointF(self._center)
        middle = self._widget_center()
        return QtCore.QPointF(
            self._center.x() + (float(point.x()) - middle.x()) / scale,
            self._center.y() + (float(point.y()) - middle.y()) / scale,
        )

    def image_to_widget(self, point: QtCore.QPointF) -> QtCore.QPointF:
        """Return the widget position of an image pixel."""

        scale = self.view_scale()
        middle = self._widget_center()
        return QtCore.QPointF(
            middle.x() + (float(point.x()) - self._center.x()) * scale,
            middle.y() + (float(point.y()) - self._center.y()) * scale,
        )

    def _transform(self):
        """Return the pixel to widget transform and the image rect."""

        transform = QtGui.QTransform()
        middle = self._widget_center()
        transform.translate(middle.x(), middle.y())
        transform.scale(self.view_scale(), self.view_scale())
        transform.translate(-self._center.x(), -self._center.y())
        return transform, self._target_rect()

    def _target_rect(self) -> QtCore.QRectF:
        """Return the rect the image occupies in widget coordinates."""

        rect = QtCore.QRectF(self.rect())
        size = self.image_size()
        if size.width() <= 0 or size.height() <= 0:
            return rect
        top_left = self.image_to_widget(QtCore.QPointF(0.0, 0.0))
        bottom_right = self.image_to_widget(
            QtCore.QPointF(size.width(), size.height())
        )
        return QtCore.QRectF(top_left, bottom_right)

    # ------------------------------------------------------- zoom and pan
    def _emit_view(self) -> None:
        """Publish the current view state to the paired canvas."""

        scale, center_x, center_y = self.view_state()
        self.view_changed.emit(scale, center_x, center_y)

    def _stop_zoom_animation(self) -> None:
        """Stop the running zoom animation, if any."""

        if self._zoom_timer.isActive():
            self._zoom_timer.stop()

    def _hold_anchor(self) -> None:
        """Keep the anchored image pixel under its widget position."""

        scale = self.view_scale()
        if scale <= 0:
            return
        middle = self._widget_center()
        self._center = QtCore.QPointF(
            self._anchor_image.x()
            - (self._anchor_widget.x() - middle.x()) / scale,
            self._anchor_image.y()
            - (self._anchor_widget.y() - middle.y()) / scale,
        )

    def zoom_steps_at(
        self, widget_point: QtCore.QPointF, steps: float
    ) -> bool:
        """Zoom around a widget position; returns True when it started.

        One positive step zooms in by WHEEL_ZOOM_STEP, one negative step
        zooms out by the same factor. The zoom is applied to the target
        only: advance_zoom_animation walks the current zoom towards it,
        which is what makes the wheel feel continuous instead of jumpy.
        """

        if self._pixmap is None or self._pixmap.isNull() or not steps:
            return False
        target = clamp_zoom(self._target_zoom * (WHEEL_ZOOM_STEP**steps))
        if target == self._target_zoom and target == self._zoom:
            return False
        self._target_zoom = target
        self._anchor_image = self.widget_to_image(widget_point)
        self._anchor_widget = QtCore.QPointF(widget_point)
        self._user_adjusted = True
        if abs(target - self._zoom) <= ZOOM_ANIM_EPSILON * max(
            abs(self._zoom), 1e-9
        ):
            self._zoom = target
            self._hold_anchor()
            self._stop_zoom_animation()
            self.update()
            self._emit_view()
            return True
        self._zoom_timer.start()
        return True

    def advance_zoom_animation(self) -> bool:
        """Move the zoom one small step towards its target.

        Returns True while the animation still has work to do. The step
        is a fixed fraction of the remaining distance in log space, so
        every tick is a small move and the series converges without ever
        overshooting the target.
        """

        if self._target_zoom == self._zoom:
            self._stop_zoom_animation()
            return False
        remaining = abs(self._target_zoom - self._zoom)
        if remaining <= ZOOM_ANIM_EPSILON * max(abs(self._zoom), 1e-9):
            self._zoom = self._target_zoom
            self._hold_anchor()
            self._stop_zoom_animation()
            self.update()
            self._emit_view()
            return False
        self._zoom = self._zoom * (
            (self._target_zoom / self._zoom) ** ZOOM_ANIM_FRACTION
        )
        self._hold_anchor()
        self.update()
        self._emit_view()
        return True

    def finish_zoom(self) -> None:
        """Apply the target zoom right away (no animation left)."""

        if self._target_zoom != self._zoom:
            self._zoom = self._target_zoom
        self._hold_anchor()
        self._stop_zoom_animation()
        self.update()
        self._emit_view()

    def pan_by(self, delta: QtCore.QPointF) -> None:
        """Move the picture by a widget space delta."""

        scale = self.view_scale()
        if scale <= 0 or self._pixmap is None or self._pixmap.isNull():
            return
        self._center = QtCore.QPointF(
            self._center.x() - float(delta.x()) / scale,
            self._center.y() - float(delta.y()) / scale,
        )
        self._user_adjusted = True
        self.update()
        self._emit_view()

    def _zoom_animation_running(self) -> bool:
        """Return True while the timer still moves the zoom."""

        return bool(self._zoom_timer.isActive())

    # -------------------------------------------------------------- events
    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:  # noqa: N802
        """Zoom smoothly around the mouse position."""

        delta = event.angleDelta().y() or event.angleDelta().x()
        if not delta:
            event.ignore()
            return
        if self.zoom_steps_at(
            QtCore.QPointF(event.position()), float(delta) / WHEEL_DELTA
        ):
            event.accept()
        else:
            event.ignore()

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802
        """Grab the picture with the left button.

        The left button starts the pan and nothing else: this canvas owns
        one gesture. Every other button - the middle one of the previous
        revision, the right one the page previews with - is handed over
        to Qt untouched.
        """

        if event.button() != QtCore.Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        self._panning = True
        self._pan_origin = QtCore.QPointF(event.position())
        self.setCursor(QtCore.Qt.CursorShape.ClosedHandCursor)
        event.accept()

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802
        """Pan the picture while the left button is held."""

        left_held = bool(event.buttons() & QtCore.Qt.MouseButton.LeftButton)
        if not self._panning or not left_held:
            super().mouseMoveEvent(event)
            return
        position = QtCore.QPointF(event.position())
        delta = position - self._pan_origin
        self._pan_origin = position
        self.pan_by(delta)
        event.accept()

    def mouseReleaseEvent(  # noqa: N802
        self, event: QtGui.QMouseEvent
    ) -> None:
        """End the pan and give the idle cursor back."""

        if event.button() != QtCore.Qt.MouseButton.LeftButton:
            super().mouseReleaseEvent(event)
            return
        if self._panning:
            self._panning = False
            self.unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(  # noqa: N802
        self, event: QtGui.QMouseEvent
    ) -> None:
        """Fit the whole picture again on a left double click.

        The way back to the first screen stays one double click, wherever
        the pointer sits.
        """

        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.fit_view()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:  # noqa: N802
        """Refit an untouched view, keep a hand made one."""

        super().resizeEvent(event)
        self._fit_scale = self._compute_fit_scale()
        if not self._user_adjusted:
            self.fit_view(emit=False)
        else:
            self.update()

    # ------------------------------------------------------------ painting
    def paintEvent(self, event: QtGui.QPaintEvent) -> None:  # noqa: N802
        """Paint the image and both overlays in image coordinates."""

        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), BACKGROUND_COLOR)
        pixmap = self._pixmap
        if pixmap is not None and not pixmap.isNull():
            size = self.image_size()
            painter.save()
            painter.setRenderHint(
                QtGui.QPainter.RenderHint.SmoothPixmapTransform, True
            )
            transform, _rect = self._transform()
            painter.setTransform(transform)
            painter.drawPixmap(
                QtCore.QRectF(0.0, 0.0, size.width(), size.height()),
                pixmap,
                QtCore.QRectF(pixmap.rect()),
            )
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
            if self.show_overlay and self.show_ground_truth:
                # the colour of every box is built once and handed to
                # the pass below; the very same builder runs in
                # set_shapes, so no colour of another record survives a
                # repaint
                self._gt_colors = self._gt_outline_colors()
                self._draw_shapes(
                    painter,
                    self._ground_truth,
                    GT_COLOR,
                    draw_labels=True,
                    statuses=self._gt_statuses,
                    colors=self._gt_colors,
                )
            if self.show_overlay and self.show_predictions:
                self._draw_shapes(
                    painter,
                    self._predictions,
                    PRED_COLOR,
                    draw_labels=True,
                    statuses=self._pred_statuses,
                )
            painter.restore()
        painter.end()
        if self.title:
            painter = QtGui.QPainter(self)
            painter.setPen(TEXT_COLOR)
            painter.drawText(8, 18, self.title)
            painter.end()

    def _draw_shapes(
        self,
        painter: QtGui.QPainter,
        shapes: Sequence[Any],
        color: QtGui.QColor,
        draw_labels: bool = False,
        statuses: Optional[Sequence[Any]] = None,
        colors: Optional[Sequence[QtGui.QColor]] = None,
    ) -> None:
        """Draw a list of shapes, each in the colour of its own state.

        The colour of a box is the one its status maps to, the canvas
        colour for a shape without a state: a matched GT therefore stays
        green, a missed one turns orange red (see shape_colors). A
        single pen is not enough any more, because a miss may sit right
        next to a match on the very same canvas.

        The pens are still cosmetic, so a shape keeps its two pixel
        outline whatever the zoom, and the point markers are divided by
        the view scale for the same reason. Every shape is still stroked
        only: the brush stays empty, so the inside of a closed box is
        never filled - a judgement colour changes the outline of a box
        and nothing else - and the picture under it stays visible. The
        labels are collected first and painted afterwards in widget
        space; a canvas that hides its labels collects nothing at all, so
        no text of a hidden label is ever measured or shaped.

        The text of a label is read as a whole: a box the merge gave
        several classes carries one text of several rows, and the newline
        it holds is stacked by the label pass (see _draw_label).

        The colours may be handed over by the caller; they are a pure
        function of the shapes and of their states, so the ground truth
        pass computes them once instead of walking the same list a
        second time in one repaint.
        """

        if colors is None:
            colors = shape_colors(shapes, color, statuses)
        # no shape is ever filled: with an empty brush a closed box is
        # only outlined and the picture inside it stays visible
        painter.setBrush(QtGui.QBrush())
        scale = self.view_scale()
        radius = POINT_RADIUS / scale if scale > 0 else POINT_RADIUS
        labels: List[Tuple[str, QtCore.QPointF]] = []
        collect = bool(draw_labels and self.show_labels)
        for index, shape in enumerate(shapes):
            points = shape_points(shape)
            if not points:
                continue
            pen = QtGui.QPen(colors[index], OVERLAY_PEN_WIDTH)
            pen.setCosmetic(True)
            painter.setPen(pen)
            if collect:
                text = shape_label_text(shape).strip()
                if text:
                    labels.append((text, self._label_anchor(shape)))
            shape_type = shape_type_of(shape)
            if shape_type in (
                "rectangle",
                "rotation",
                "quadrilateral",
                "polygon",
            ):
                painter.drawPolygon(
                    QtGui.QPolygonF([QtCore.QPointF(x, y) for x, y in points])
                )
            elif shape_type == "circle" and len(points) >= 2:
                circle_radius = (
                    (points[1][0] - points[0][0]) ** 2
                    + (points[1][1] - points[0][1]) ** 2
                ) ** 0.5
                painter.drawEllipse(
                    QtCore.QPointF(points[0][0], points[0][1]),
                    circle_radius,
                    circle_radius,
                )
            else:
                if len(points) > 1:
                    painter.drawPolyline(
                        QtGui.QPolygonF(
                            [QtCore.QPointF(x, y) for x, y in points]
                        )
                    )
                for x, y in points:
                    painter.drawEllipse(QtCore.QPointF(x, y), radius, radius)
        if labels:
            self._draw_labels(painter, labels)

    def _label_anchor(self, shape: Any) -> QtCore.QPointF:
        """Return the image point the label of one box hangs from.

        The anchor is the upper left corner of the bounding box of the
        shape, which is the corner the annotation conventions of the
        repository use for a box label.
        """

        points = shape_points(shape)
        if not points:
            return QtCore.QPointF(0.0, 0.0)
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        return QtCore.QPointF(min(xs), min(ys))

    @staticmethod
    def _label_font() -> QtGui.QFont:
        """Return the fixed screen font of the box labels.

        The application font is only re-sized, never replaced, so the
        text stays readable on a high dpi screen as well, and the point
        size is one widget space number: the label keeps the same size at
        every zoom because it is measured and painted in widget pixels.
        """

        font = QtGui.QFont(QtWidgets.QApplication.font())
        font.setPointSizeF(LABEL_POINT_SIZE)
        return font

    def _draw_labels(
        self,
        painter: QtGui.QPainter,
        labels: Sequence[Tuple[str, QtCore.QPointF]],
    ) -> None:
        """Paint the collected box labels in widget space.

        The pass starts by resetting the painter to the widget coordinate
        system, therefore the text is never scaled with the picture: a
        zoomed in box keeps the very same label size as a fitted one.
        Without that reset the image transform of the shapes would scale
        the font by the view scale and push the text away from the corner
        it belongs to: the labels of the shapes are collected in image
        coordinates and converted here.

        A label hangs just above the upper left corner of its own box, so
        one glance tells which box it describes. The room above the
        corner is used while the text fits and the label flips below the
        corner when it does not; horizontally it is only pulled back
        inside the widget when it would leave the view. A label that does
        not fit the widget at all is dropped: a box filling the complete
        view has no room for a label anywhere. A label of several rows -
        the classes of one merged box - is one block like any other: the
        rows are stacked inside it and the block as a whole is placed.
        """

        if not labels:
            return
        font = self._label_font()
        painter.save()
        # the shapes were painted under the image transform: the labels
        # belong to the widget grid and are only correct without it
        painter.resetTransform()
        painter.setFont(font)
        width = max(float(self.width()), 1.0)
        height = max(float(self.height()), 1.0)
        try:
            for text, anchor in labels:
                if anchor is None:
                    continue
                corner = self.image_to_widget(anchor)
                self._draw_label(painter, text, corner, font, width, height)
        finally:
            painter.restore()

    @staticmethod
    def _label_glyphs(font: QtGui.QFont, text: str) -> QtGui.QPainterPath:
        """Return the glyphs of one label as a path.

        A label is drawn from this path instead of drawText: stroking a
        text with a pen outlines the glyphs but never fills them, which
        turns a readable label into a hollow skeleton. The path can be
        stroked for the outline and filled for the letters, and the very
        same path carries the ink box the placement is measured on - the
        metrics of a font report the line box, which is much taller than
        the letters actually painted, so a label placed on that box would
        float far above its own box. The path is built at the origin:
        its left and top edges are the (negative) offsets the placement
        and the painter translation must carry, and its own box is the
        label padding box of the placement.
        """

        path = QtGui.QPainterPath()
        path.addText(0.0, 0.0, font, text)
        if path.boundingRect().isEmpty():
            # a font the platform cannot shape: fall back to the metrics
            metrics = QtGui.QFontMetricsF(font)
            path.addRect(
                QtCore.QRectF(
                    0.0,
                    -metrics.ascent(),
                    max(metrics.horizontalAdvance(text), 1.0),
                    max(metrics.ascent(), 1.0),
                )
            )
        return path

    @classmethod
    def _stacked_glyphs(
        cls, font: QtGui.QFont, text: str
    ) -> QtGui.QPainterPath:
        """Return the glyphs of a possibly multi line label as one path.

        QPainterPath.addText cannot lay out a newline: a text carrying
        one would be shaped as a row of replacement boxes. The lines are
        therefore split here and stacked by hand, one after the other,
        with one LABEL_LINE_SPACING of widget pixels between their
        baselines. One path comes back for the whole block - a single
        line included - so the placement and the two paints of a label
        stay the one pass they always were (see _draw_label).
        """

        lines = str(text).split("\n")
        if len(lines) == 1:
            return cls._label_glyphs(font, lines[0])
        path = QtGui.QPainterPath()
        first_bottom: Optional[float] = None
        for index, line in enumerate(lines):
            glyphs = cls._label_glyphs(font, line)
            box = glyphs.boundingRect()
            if first_bottom is None:
                first_bottom = float(box.bottom())
            offset = float(index) * LABEL_LINE_SPACING - (
                float(box.bottom()) - first_bottom
            )
            stacked = QtGui.QPainterPath(glyphs)
            stacked.translate(0.0, offset)
            path.addPath(stacked)
        return path

    def _draw_label(
        self,
        painter: QtGui.QPainter,
        text: str,
        corner: QtCore.QPointF,
        font: QtGui.QFont,
        width: float,
        height: float,
    ) -> None:
        """Place and paint one label next to its own anchor.

        The anchor is the upper left corner of the box in widget
        coordinates. The label starts above that corner, with its lower
        edge LABEL_GAP pixels above it, and flips below the corner when
        the room above is missing; it is only pulled back horizontally
        and only when it would leave the widget.

        The block of a multi class box is placed as a whole: the lines
        are stacked into one path (see _stacked_glyphs), its union box
        is what the room is measured on and what the anchor carries.
        Therefore a block that does not fit the widget is dropped whole
        instead of being half written - row by row placement could leave
        the lines of one box scattered over two corners - and the block
        only ever moves as a whole: its union box is the anchor, exactly
        the way the single line of the previous revision was anchored on
        its own ink. The lines of the block keep the left edge of their
        first row and the LABEL_LINE_SPACING of their own stacking.
        """

        glyphs = self._stacked_glyphs(font, text)
        ink = glyphs.boundingRect()
        text_width = float(ink.width())
        if text_width <= 0:
            return
        # The outline of a label is centred on the border of the glyphs,
        # so only half of it grows them: the room the label needs is
        # measured on that half, exactly as the glyphs are measured.
        outline = LABEL_OUTLINE_WIDTH / 2.0
        ink_left = float(ink.left()) - outline
        ink_top = float(ink.top()) - outline
        ink_right = float(ink.right()) + outline
        ink_bottom = float(ink.bottom()) + outline
        ink_width = ink_right - ink_left
        ink_height = max(ink_bottom - ink_top, 1.0)
        # the band a label occupies is the measured glyphs plus the label
        # padding; it is what has to fit the widget
        band_width = ink_width + 2.0 * LABEL_PADDING
        band_height = ink_height + 2.0 * LABEL_PADDING
        if band_width > width or band_height > height:
            return
        # one text origin, so the block and the lines never drift apart:
        # the glyphs are painted at (origin + ink.left, origin + ink.top)
        origin_x = corner.x() - ink_left
        if origin_x + ink_right > width - LABEL_MARGIN:
            origin_x = width - LABEL_MARGIN - ink_right
        origin_x = max(origin_x, LABEL_MARGIN - ink_left)
        if origin_x + ink_right > width:
            return
        # above the corner by default; when the room above is missing the
        # label flips below the corner instead of drifting away from it
        if corner.y() - LABEL_GAP - ink_bottom >= LABEL_VERTICAL_MARGIN:
            origin_y = corner.y() - LABEL_GAP - ink_bottom
        else:
            # no room above: flip below, unless that would leave the
            # widget as well - the label of a box the pan pushed off the
            # view is clamped inside instead of being dropped
            under = corner.y() + LABEL_GAP - ink_top
            if under + ink_bottom > height - LABEL_VERTICAL_MARGIN:
                above = corner.y() - LABEL_GAP - ink_bottom
                if above >= LABEL_VERTICAL_MARGIN:
                    under = above
            origin_y = under
        band_top = origin_y + ink_top - LABEL_PADDING
        band_top = min(
            max(band_top, LABEL_VERTICAL_MARGIN),
            height - LABEL_VERTICAL_MARGIN - band_height,
        )
        origin_y = band_top + LABEL_PADDING - ink_top
        self._draw_label_at(painter, glyphs, origin_x, origin_y)

    def _draw_label_at(
        self,
        painter: QtGui.QPainter,
        glyphs: QtGui.QPainterPath,
        left: float,
        top: float,
    ) -> None:
        """Write one label at a widget space position.

        A label never carries a background plate: the white glyphs are
        stroked with the dark outline first and filled on top of it, so
        the text stays readable on a bright as well as on a dark picture
        and the picture around it is left untouched.
        """

        painter.save()
        painter.translate(left, top)
        pen = QtGui.QPen(LABEL_OUTLINE)
        pen.setWidthF(LABEL_OUTLINE_WIDTH)
        painter.setPen(pen)
        painter.setBrush(QtGui.QBrush())
        painter.drawPath(glyphs)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QBrush(TEXT_COLOR))
        painter.drawPath(glyphs)
        painter.restore()


__all__ = [
    "BOX_COLOR",
    "CLASS_MISMATCH_COLOR",
    "FALSE_POSITIVE_COLOR",
    "FIT_PADDING",
    "GT_COLOR",
    "ImageCanvas",
    "IOU_BELOW_COLOR",
    "LABEL_GAP",
    "LABEL_LINE_SPACING",
    "LABEL_MARGIN",
    "LABEL_OUTLINE",
    "LABEL_OUTLINE_WIDTH",
    "LABEL_PADDING",
    "LABEL_POINT_SIZE",
    "LABEL_VERTICAL_MARGIN",
    "LOW_SCORE_COLOR",
    "MATCH_COLOR",
    "MAX_ZOOM",
    "MIN_ZOOM",
    "MISS_COLOR",
    "OVERLAY_PEN_WIDTH",
    "PRED_COLOR",
    "SCORE_FORMAT",
    "STATE_CLASS_MISMATCH",
    "STATE_FALSE_POSITIVE",
    "STATE_IOU_BELOW",
    "STATE_LOW_SCORE",
    "STATE_MISS",
    "STATE_OK_PAIR",
    "STATE_TO_COLOR",
    "WHEEL_DELTA",
    "WHEEL_ZOOM_STEP",
    "ZOOM_ANIM_FRACTION",
    "ZOOM_ANIM_INTERVAL_MS",
    "clamp_zoom",
    "load_pixmap",
    "shape_color",
    "shape_colors",
    "shape_label",
    "shape_label_rows",
    "shape_label_text",
    "shape_points",
    "shape_score",
    "shape_type_of",
]
