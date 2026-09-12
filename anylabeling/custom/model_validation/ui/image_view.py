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

The canvas is still a viewer, but it owns one *edit* state as well (see
set_editable_shapes): switched on by the middle button it selects, moves
and resizes the region shapes it was handed, and it hands the new point
set back as a signal instead of writing it anywhere. The records layer is
what stores the drag (results_page.apply_edit_result ->
records.update_shape_points), so a canvas that is not part of the results
page never edits anything. Every unrecognised shape type stays grey and
dashed while the edit state is on and is never picked, and the whole
overlay is skipped unless the state is on: the plain viewer of the
previous revision is exactly what an inactive canvas keeps painting.
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
# orange and a pair whose IoU stayed under the threshold is dark yellow.
# A box whose state is unknown - a matched pair, a record nobody judged -
# keeps the plain GT / Pred colour above.
MISS_COLOR = QtGui.QColor(231, 76, 60)
FALSE_POSITIVE_COLOR = QtGui.QColor(155, 89, 182)
CLASS_MISMATCH_COLOR = QtGui.QColor(230, 126, 34)
IOU_BELOW_COLOR = QtGui.QColor(241, 196, 15)
# The states a box can carry. Two of them are the states a matched pair
# of the judge detail is written with; the other two are the states of a
# shape no match points at - a GT no prediction met and a prediction no
# GT met - which the reader of the detail derives from its missed and
# false positive indexes. The state of a matched pair that is fine keeps
# the plain colour of its canvas and so does a pair of two labels that
# are not even known (see results_page.matched_pair_state).
MATCH_COLOR = GT_COLOR
STATE_OK_PAIR = "OK_PAIR"
STATE_MISS = "MISS"
STATE_FALSE_POSITIVE = "FALSE_POSITIVE"
STATE_CLASS_MISMATCH = "CLASS_MISMATCH"
STATE_IOU_BELOW = "IOU_BELOW"

# The colour of the four judgement states. The map is keyed by the state
# of a shape; a state it does not carry - OK_PAIR, an empty state, a
# state of a later revision - has no colour of its own and keeps the
# plain colour of the canvas it is drawn on. The states of a canvas are
# a display side copy: the record itself is never written to.
STATE_TO_COLOR = {
    STATE_MISS: MISS_COLOR,
    STATE_FALSE_POSITIVE: FALSE_POSITIVE_COLOR,
    STATE_CLASS_MISMATCH: CLASS_MISMATCH_COLOR,
    STATE_IOU_BELOW: IOU_BELOW_COLOR,
}
TEXT_COLOR = QtGui.QColor(255, 255, 255)
BACKGROUND_COLOR = QtGui.QColor(24, 26, 30)
POINT_RADIUS = 3.0
OVERLAY_PEN_WIDTH = 2.0

# The label of a box is written with one font size in widget pixels, so
# it keeps the same readable size whatever the zoom. It is measured and
# placed in that very widget space: the anchor is the upper left corner
# of the box, the text sits LABEL_GAP pixels above it and is only moved
# below the anchor when the room above is missing.
LABEL_POINT_SIZE = 8.0
LABEL_PADDING = 3.0
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


# The ROI edit mode. A user enters it with the middle button, picks a
# region shape with the left button and drags either the box itself (it
# moves) or one of its eight handles (it resizes). The handles are
# measured in *image* pixels and divided by the view scale before they
# are painted, so they keep one screen size at every zoom exactly like
# the point markers do, and the hit tolerance is divided the very same
# way. A shape type this tool cannot edit is drawn grey and dashed and
# is never hit tested.
EDIT_MODE_TITLE_SUFFIX = " · 编辑模式"
# The cursor of the edit state: a cross hair says "the left button draws
# here" before a shape or a handle is even under the pointer, which is
# the one hint a user gets that the canvas left its viewer state. It is
# the *fall back* of the state and never beats a more precise answer:
# a handle keeps its resize cursor and a box under the pointer keeps the
# open hand (see _update_hover_cursor).
EDIT_CURSOR = QtCore.Qt.CursorShape.CrossCursor
HANDLE_SIZE = 4.0
HANDLE_COLOR = QtGui.QColor(0, 188, 212)
HANDLE_FILL_COLOR = QtGui.QColor(255, 255, 255)
SELECTED_PEN_WIDTH = 3.0
UNEDITABLE_PEN_WIDTH = 2.0
UNEDITABLE_COLOR = QtGui.QColor(150, 150, 150)
HIT_TOLERANCE = 6.0
# A box never shrinks below one image pixel: a resize that would collapse
# a side is stopped at this width / height instead of producing a
# degenerate box the judge could not match any more.
MIN_BOX_SIZE = 1.0
# The eight handles of a box, in the order box_handles() returns them.
# The name says where the handle sits, and the resize reads it as the two
# edges it drags: "l"/"r" the horizontal ones, "t"/"b" the vertical ones.
HANDLE_NAMES = ("lt", "t", "rt", "r", "rb", "b", "lb", "l")
# One cursor per handle, so the picture answers what a drag would do
# before the button is even pressed.
HANDLE_CURSORS: Dict[str, QtCore.Qt.CursorShape] = {
    "lt": QtCore.Qt.CursorShape.SizeFDiagCursor,
    "rt": QtCore.Qt.CursorShape.SizeBDiagCursor,
    "lb": QtCore.Qt.CursorShape.SizeBDiagCursor,
    "rb": QtCore.Qt.CursorShape.SizeFDiagCursor,
    "l": QtCore.Qt.CursorShape.SizeHorCursor,
    "r": QtCore.Qt.CursorShape.SizeHorCursor,
    "t": QtCore.Qt.CursorShape.SizeVerCursor,
    "b": QtCore.Qt.CursorShape.SizeVerCursor,
}


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


def shape_label_text(shape: Any) -> str:
    """Return the text drawn at the upper left corner of one box."""

    label = shape_label(shape).strip()
    score = shape_score(shape)
    if score is None:
        return label
    text = SCORE_FORMAT.format(score)
    return label + " " + text if label else text


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


def _point_box(points: Sequence[Tuple[float, float]]):
    """Return the (left, top, right, bottom) box of a point set.

    None is answered for an empty point set: a shape without points has
    no handle, no hit area and nothing to resize.
    """

    if not points:
        return None
    xs = [float(point[0]) for point in points]
    ys = [float(point[1]) for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _widest_span(points: Sequence[Tuple[float, float]]) -> float:
    """Return the longer side of the box of a point set, 0.0 without one."""

    box = _point_box(points)
    if box is None:
        return 0.0
    return max(float(box[2] - box[0]), float(box[3] - box[1]))


def box_handles(points: Sequence[Tuple[float, float]]):
    """Return the eight handles of the bounding box of a point set.

    Four corners and the four midpoints of the edges, in the order of
    HANDLE_NAMES: this is the one layout every box of the edit mode is
    driven by, whatever its shape type is.
    """

    box = _point_box(points)
    if box is None:
        return []
    left, top, right, bottom = box
    middle_x = (left + right) / 2.0
    middle_y = (top + bottom) / 2.0
    return [
        (left, top),
        (middle_x, top),
        (right, top),
        (right, middle_y),
        (right, bottom),
        (middle_x, bottom),
        (left, bottom),
        (left, middle_y),
    ]


def handle_cursor(handle: Any) -> Optional[QtCore.Qt.CursorShape]:
    """Return the cursor of one handle, None for a plain box drag."""

    return HANDLE_CURSORS.get(str(handle or ""))


def contains_point(
    points: Sequence[Tuple[float, float]], point: Sequence[float]
) -> bool:
    """Return True when a point sits inside the bounding box of a shape."""

    box = _point_box(points)
    if box is None:
        return False
    return bool(box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3])


def hit_test(editable, point, scale: float = 1.0):
    """Return (index, handle) of the shape a point picks, (-1, "") else.

    A handle wins over a box, so a corner sitting on the border of
    another box stays grabbable, and the tolerance of both tests is
    divided by the view scale first: HIT_TOLERANCE and the handles are
    screen sizes, so a hit stays as easy after zooming in as it was on
    the fitted picture (the same division the point markers use).

    The boxes are examined from the last one to the first, which is the
    paint order backwards: the box the user sees on top is the box the
    click picks when two of them overlap, and the smallest one is picked
    when one sits completely inside another.

    Every entry of the editable list is (shape, can_edit) and a shape
    the caller refused - an unrecognised type - is skipped entirely: it
    is drawn grey and dashed and can neither be selected nor moved.
    """

    entries = list(editable or [])
    tolerance = HIT_TOLERANCE / max(float(scale), 1e-9)
    for index in range(len(entries) - 1, -1, -1):
        shape, can_edit = entries[index]
        if not can_edit:
            continue
        handles = box_handles(shape_points(shape))
        for name, handle in zip(HANDLE_NAMES, handles):
            if (
                abs(float(point[0]) - handle[0]) <= tolerance
                and abs(float(point[1]) - handle[1]) <= tolerance
            ):
                return index, name
    best_index = -1
    best_span = 0.0
    for index in range(len(entries) - 1, -1, -1):
        shape, can_edit = entries[index]
        if not can_edit:
            continue
        points = shape_points(shape)
        if contains_point(points, point):
            span = _widest_span(points)
            if best_index < 0 or span <= best_span:
                best_index, best_span = index, span
    return best_index, ""


def nearest_point(
    points: Sequence[Tuple[float, float]], target: Sequence[float]
) -> int:
    """Return the index of the vertex closest to a target position.

    A polygon has no handles of its own - its eight handles are the ones
    of its bounding box - so a resize of one moves the vertex the handle
    sits on: the one nearest to the point the drag started from.
    """

    best_index = 0
    best_distance = None
    for index, point in enumerate(points):
        distance = (float(point[0]) - float(target[0])) ** 2 + (
            float(point[1]) - float(target[1])
        ) ** 2
        if best_distance is None or distance < best_distance:
            best_index, best_distance = index, distance
    return best_index


# The two corners the anchor of a ring resize holds, per handle, in the
# index order of HANDLE_NAMES. The anchor is the corner opposite the
# dragged one - corner 2 for "lt", 3 for "rt", 0 for "rb", 1 for "lb" -
# and the two numbers are the indices of its neighbours, which are the
# two sides the whole ring is scaled along. A ring resize is read off
# these three corners alone, never off the axis aligned box of the
# screen.
RING_CORNERS = {
    "lt": (1, 3),
    "rt": (0, 2),
    "rb": (1, 3),
    "lb": (0, 2),
}
# The corner opposite every handle of a ring: the one that holds still
# whatever the pointer does.
RING_ANCHORS = {"lt": 2, "rt": 3, "rb": 0, "lb": 1}
# An edge handle keeps the opposite *edge* still, which is the same
# thing: the corner that edge starts from, and the two corners it ends
# on, of which the first one is the side the handle names. The corner
# of an edge handle is the diagonal opposite one, the corner no drag of
# that edge touches.
EDGE_ANCHORS = {
    "l": (1, 0, 2),
    "r": (0, 1, 3),
    "t": (3, 0, 2),
    "b": (0, 3, 1),
}


def _ring_anchors(points, name: str):
    """Return the (anchor, neighbours) a ring resize is read from.

    A ring of four free corners has no axis aligned box to rewrite: the
    handle names still say which corner the user grabbed and which one
    is therefore nailed - the corner opposite the handle, the one every
    drag of a box editor leaves in place. The two neighbours of that
    anchor are the two corners its own sides end on, and their
    directions are the two axes the shape is scaled along, so the
    rotation of the shape is read off the shape itself and never
    assumed to be the one of the screen.
    """

    if len(points) != 4:
        return None
    key = str(name or "")
    corners = RING_CORNERS.get(key)
    anchor_index = RING_ANCHORS.get(key)
    if corners is None or anchor_index is None:
        edge = EDGE_ANCHORS.get(key)
        if edge is None:
            return None
        # An edge handle of the bounding box of a rotated shape: it
        # names one side of the two the diagonal opposite corner holds,
        # and that one is handed over first, so _ring_factors scales it
        # and leaves the other one alone.
        anchor_index, first, second = edge
        if key in ("t", "b"):
            # the side "t" and "b" name is the second one of that
            # corner: the two are swapped to keep the order of the
            # handle names - "l" / "r" scale the first, "t" / "b" the
            # second
            first, second = second, first
    else:
        first, second = corners
    return points[anchor_index], (points[first], points[second])


def _ring_factors(points, name: str, delta) -> Optional[Tuple[float, float]]:
    """Return the two signed scale factors of one ring resize step.

    The pointer displacement is written in the basis of the two sides
    the anchor holds - the axes of the shape itself, never the ones of
    the screen - and every coordinate it gets there is read as the
    ratio it is: a displacement of a whole side doubles that side, a
    displacement of half a side shrinks it to a half, and a
    displacement *against* the side shrinks it instead of growing it.
    That sign is the whole point: the previous revision took the
    amplitude of the delta, so its factor could never be smaller than
    one and a shape could only ever grow.

    Writing the displacement in that basis is what makes the dragged
    corner follow the pointer: the corner sits at the sum of the two
    sides, so scaling each side by one plus its own share of the
    displacement is exactly what moves that corner onto the pointer,
    and a square whose sides are perpendicular keeps its angle (both
    factors follow the pointer by the same amount). A ratio below one
    is a shrink, a ratio of exactly zero is a side of no length at all,
    which is where MIN_BOX_SIZE takes over (see _rewrite_ring).

    Only the axes the handle names are scaled, which is what keeps an
    edge handle a one sided resize while a corner scales both.
    """

    anchors = _ring_anchors(points, name)
    if anchors is None:
        return None
    anchor, (first, second) = anchors
    u = (
        float(first[0]) - float(anchor[0]),
        float(first[1]) - float(anchor[1]),
    )
    v = (
        float(second[0]) - float(anchor[0]),
        float(second[1]) - float(anchor[1]),
    )
    length_u = math.hypot(u[0], u[1])
    length_v = math.hypot(v[0], v[1])
    if length_u <= 0.0 or length_v <= 0.0:
        return None
    step_x, step_y = float(delta[0]), float(delta[1])
    factors = [1.0, 1.0]
    horizontal = "l" in str(name) or "r" in str(name)
    vertical = "t" in str(name) or "b" in str(name)
    if horizontal and vertical:
        # Both axes move: the displacement is written in the basis of
        # the two sides, and the number it gets there is already the
        # length the side gains - a whole side of it doubles that side,
        # a negative one shortens it - so the factor is one plus that
        # length over the length of the side.
        determinant = u[0] * v[1] - u[1] * v[0]
        if abs(determinant) <= 1e-9:
            # the four corners are on one line: there is no ring to scale
            return None
        weight_u = (step_x * v[1] - step_y * v[0]) / determinant
        weight_v = (u[0] * step_y - u[1] * step_x) / determinant
        # The two weights are the ratios themselves: a displacement
        # that lands on a side with a weight of one is a displacement
        # of a whole side, and the ratio of the side is therefore one
        # plus that weight. Expanded in the basis of the sides, the map
        # sends the dragged corner exactly onto the pointer - the
        # anchor plus the displacement - which is why the corner of a
        # box follows the hand and not an amplitude of it.
        factors[0] = 1.0 + weight_u
        factors[1] = 1.0 + weight_v
        return factors[0], factors[1]
    if horizontal:
        # One axis alone: the side the handle names follows the pointer
        # and the opposite one holds still, which is the plain "move
        # this edge, keep the other one" rule of every box editor - a
        # drag of ten pixels on a side of a hundred moves that side by
        # ten. The sign of the ratio is the sign of the drag, so an
        # edge pulled towards its anchor shortens the box instead of
        # growing it.
        factors[0] = 1.0 + (step_x * u[0] + step_y * u[1]) / (
            length_u * length_u
        )
    elif vertical:
        factors[1] = 1.0 + (step_x * v[0] + step_y * v[1]) / (
            length_v * length_v
        )
    return factors[0], factors[1]


def _rewrite_ring(points, name: str, delta):
    """Scale a point ring about the corner opposite the dragged handle.

    A rotation box and a quadrilateral are four free corners: moving one
    of them with the pointer instead of re-deriving an axis aligned box
    keeps the rotation of the shape, which an axis aligned rewrite would
    silently destroy. The corner the handle is *not* dragging stays
    nailed; the whole ring is scaled along the two sides that anchor
    holds (see _ring_factors) - in the basis of those sides, never along
    the axes of the screen - so a rotated box keeps its angle, both
    inner angles of the ring are preserved and a drag *towards* the
    anchor shrinks the shape instead of growing it. A square whose
    sides are perpendicular and whose factors came out equal is scaled
    by one single number: its own angle survives the resize to the last
    decimal.

    The point the pointer holds is the corner opposite the anchor, and
    the scaling above is built to land that corner exactly on it: a
    displacement written in the basis of the two sides scales each side
    by its own share of it, which is the very definition of the scaling
    being applied (see the round trip note of results_page tests).

    A side never collapses: a scale that would shorten a side below
    MIN_BOX_SIZE is stopped there, the very guard the axis aligned
    branch of resize_points applies to its own two sides.
    """

    anchor_points = _ring_anchors(points, name)
    if anchor_points is None:
        return list(points)
    fixed, (first, second) = anchor_points
    factors = _ring_factors(points, name, delta)
    if factors is None:
        return list(points)
    sides = _ring_sides(fixed, first, second)
    limits = tuple(
        _ring_factor_limit(factors[axis], sides[axis]) for axis in (0, 1)
    )
    u = (
        float(first[0]) - float(fixed[0]),
        float(first[1]) - float(fixed[1]),
    )
    v = (
        float(second[0]) - float(fixed[0]),
        float(second[1]) - float(fixed[1]),
    )
    determinant = u[0] * v[1] - u[1] * v[0]
    if abs(determinant) <= 1e-9:
        return list(points)
    result = []
    for point in points:
        offset = (
            float(point[0]) - float(fixed[0]),
            float(point[1]) - float(fixed[1]),
        )
        weight_u = (offset[0] * v[1] - offset[1] * v[0]) / determinant
        weight_v = (u[0] * offset[1] - u[1] * offset[0]) / determinant
        result.append(
            (
                float(fixed[0])
                + weight_u * limits[0] * u[0]
                + weight_v * limits[1] * v[0],
                float(fixed[1])
                + weight_u * limits[0] * u[1]
                + weight_v * limits[1] * v[1],
            )
        )
    return result


def _ring_sides(fixed, first, second) -> Tuple[float, float]:
    """Return the two side lengths the anchor of a ring holds."""

    return (
        math.hypot(
            float(first[0]) - float(fixed[0]),
            float(first[1]) - float(fixed[1]),
        ),
        math.hypot(
            float(second[0]) - float(fixed[0]),
            float(second[1]) - float(fixed[1]),
        ),
    )


def _ring_factor_limit(factor: float, side: float) -> float:
    """Stop a signed scale factor where its own side reaches the minimum."""

    if side <= 0.0:
        return 1.0
    return max(float(factor), MIN_BOX_SIZE / side)


def resize_points(
    shape_type: str,
    points: Sequence[Tuple[float, float]],
    handle: str,
    delta: Sequence[float],
    origin: Optional[Sequence[float]] = None,
) -> List[Tuple[float, float]]:
    """Return the point set of one resize step of a shape.

    The box of the shape is re-derived from the edges the handle
    carries ("l" / "r" the horizontal ones, "t" / "b" the vertical
    ones) and the result is written back in the shape convention of the
    repository: a rectangle becomes the four point axis aligned box in
    the (left, top) order, a rotation box and a quadrilateral are
    scaled about the corner opposite the dragged handle, along the two
    axes the shape itself holds (see _rewrite_ring) - a drag towards
    the anchor shrinks it, an edge handle keeps the opposite edge still
    and a corner handle keeps the opposite corner still. A polygon only
    moves the single
    vertex the handle grabbed - the one nearest to `origin`, the image
    point the drag started from - so every other vertex of a hand drawn
    outline stays exactly where the user drew it.

    The two sides never collapse: a resize that would push an edge past
    the opposite one is stopped at MIN_BOX_SIZE, one image pixel, which
    keeps every box matchable by the judge instead of degenerate.

    A shape the branch of its own type cannot serve is answered with
    its very own points: a polygon of fewer than three vertices and a
    rotation box or a quadrilateral that does not carry four corners
    are handed back untouched instead of being silently rewritten as an
    axis aligned rectangle, which would change both the number of the
    points and the meaning of the annotation. The caller sees a point
    set that did not change and publishes nothing.
    """

    box = _point_box(points)
    if box is None:
        return list(points)
    name = str(handle or "")
    step_x, step_y = float(delta[0]), float(delta[1])
    if shape_type == "polygon":
        if len(points) < 3:
            return [(float(x), float(y)) for x, y in points]
        result = [(float(x), float(y)) for x, y in points]
        index = nearest_point(points, origin or _point_box(points)[:2])
        result[index] = (result[index][0] + step_x, result[index][1] + step_y)
        return result
    if shape_type in ("rotation", "quadrilateral"):
        if len(points) != 4:
            return [(float(x), float(y)) for x, y in points]
        return _rewrite_ring(points, name, (step_x, step_y))
    left, top, right, bottom = box
    if "l" in name:
        left = min(left + step_x, right - MIN_BOX_SIZE)
    if "r" in name:
        right = max(right + step_x, left + MIN_BOX_SIZE)
    if "t" in name:
        top = min(top + step_y, bottom - MIN_BOX_SIZE)
    if "b" in name:
        bottom = max(bottom + step_y, top + MIN_BOX_SIZE)
    return [
        (left, top),
        (right, top),
        (right, bottom),
        (left, bottom),
    ]


def clamp_zoom(value: float) -> float:
    """Clamp a zoom factor into the supported [MIN_ZOOM, MAX_ZOOM] range."""

    return float(min(max(float(value), MIN_ZOOM), MAX_ZOOM))


class ImageCanvas(QtWidgets.QWidget):
    """Draw one image with its overlays and share one zoom / pan state."""

    # (view scale, image x at the widget centre, image y at the centre)
    view_changed = QtCore.pyqtSignal(float, float, float)
    # The four signals of the edit state. A canvas never writes a file:
    # it publishes the gesture and the point set it produced, and the
    # page decides what is stored (see results_page.apply_edit_result).
    edit_mode_toggled = QtCore.pyqtSignal(bool)
    shape_selected = QtCore.pyqtSignal(str, int)
    shape_move_finished = QtCore.pyqtSignal(str, int, object)
    shape_rename_requested = QtCore.pyqtSignal(str, int)

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
        # the colours of the last ground truth pass; the edit overlay
        # re-strokes a selection in its own colour instead of computing
        # the whole colour list a second time (see paintEvent)
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
        # the edit state: off until the user asks for it, and it only
        # ever holds shapes set_editable_shapes handed over - the canvas
        # looked at every other picture exactly the way it always did
        self.editable = False
        self._edit_record_id = ""
        self._edit_shapes: List[Tuple[Any, bool]] = []
        self._selected = -1
        self._active_handle: Optional[str] = None
        self._drag_anchor: Optional[Tuple[float, float]] = None
        self._drag_start_points: List[Tuple[float, float]] = []
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
        only in paintEvent: the edit overlay re-strokes the selected box
        with the colour of its own state, and a canvas that was handed
        the shapes of another record must never keep the colours of the
        previous one (see _gt_outline_colors).
        """

        self._ground_truth = list(ground_truth)
        self._predictions = list(predictions)
        self._gt_statuses = list(ground_truth_statuses or [])
        self._pred_statuses = list(prediction_statuses or [])
        self._gt_colors = self._gt_outline_colors()
        self.update()

    def _gt_outline_colors(self) -> List[QtGui.QColor]:
        """Return the outline colour of every ground truth box.

        The list and the shapes it belongs to are built together, so the
        edit overlay of a selected box can never read a colour that was
        left behind by another record: a canvas showing no ground truth
        (the checkbox is off, the record has no shape) answers the
        empty list and the overlay falls back to the plain ground truth
        colour.
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
        # an emptied canvas has no shape left to select or to drag
        self._edit_record_id = ""
        self._edit_shapes = []
        self._selected = -1
        self._active_handle = None
        self._stop_zoom_animation()
        self.update()

    # ------------------------------------------------------------ edit ROI
    def set_edit_mode(self, enabled: bool) -> None:
        """Switch the ROI edit state of this canvas on or off.

        Switching off forgets the selection and every finished drag
        state: the canvas is the plain viewer of the previous revision
        again and paints nothing of the edit overlay. Switching on
        re-reads the shapes the page already handed over, so a mode that
        is toggled does not wait for the next picture.
        """

        wanted = bool(enabled)
        if self.editable == wanted:
            return
        self.editable = wanted
        if not wanted:
            self._selected = -1
            self._active_handle = None
            self._drag_anchor = None
            self._drag_start_points = []
        # the pointer is told about the state right away, and it is told
        # again by every path that ends a gesture (see _restore_edit_cursor)
        self._restore_edit_cursor()
        self.update()
        self.edit_mode_toggled.emit(wanted)

    def set_editable_shapes(
        self,
        record_id: str,
        shapes: Sequence[Any],
        editable_flags: Sequence[bool],
    ) -> None:
        """Set the editable shapes of one record (record id + shapes).

        The canvas keeps its own copy of the pairs (shape, can_edit) and
        never reads the record again: the page hands the very shapes it
        paints, so a drag moves the picture the user is looking at. The
        selection survives a refresh of the *same* record - the page
        repaints the canvases after every drag - and is dropped when
        another record arrives or the index it pointed at is gone.
        """

        self._edit_record_id = str(record_id or "")
        if not str(record_id or ""):
            # no record on screen is no record to edit: a page that
            # previews another picture hands an empty record id over,
            # and the overlay of this canvas has to follow the shapes
            # the two overlays show instead of decorating the previous
            # record with them (see results_page._refresh_canvases)
            self._edit_shapes = []
            self._selected = -1
            self.update()
            return
        entries = list(zip(shapes, editable_flags))
        self._edit_shapes = [(shape, bool(flag)) for shape, flag in entries]
        if not 0 <= self._selected < len(self._edit_shapes):
            self._selected = -1
        self.update()

    def editable_shapes(self) -> List[Any]:
        """Return the shapes the edit overlay is driven by."""

        return [shape for shape, _flag in self._edit_shapes]

    def editable_flags(self) -> List[bool]:
        """Return the can_edit flag that belongs to each editable shape."""

        return [bool(flag) for _shape, flag in self._edit_shapes]

    def edit_record_id(self) -> str:
        """Return the record id the editable shapes belong to."""

        return str(self._edit_record_id)

    def selected_index(self) -> int:
        """Return the selected shape index, -1 when nothing is selected."""

        return int(self._selected)

    def select_shape(self, index: int) -> bool:
        """Select one shape by index; -1 clears the selection."""

        position = int(index)
        if position < 0 or position >= len(self._edit_shapes):
            position = -1
        if position == self._selected:
            return False
        self._selected = position
        self.update()
        if position >= 0:
            self.shape_selected.emit(self._edit_record_id, position)
        return True

    def clear_selection(self) -> None:
        """Drop the selection and the handle a drag would start from."""

        self._active_handle = None
        self.select_shape(-1)
        self._restore_edit_cursor()

    def _restore_edit_cursor(self) -> None:
        """Write the cursor the edit state owns, or give the default back.

        This is the one place the cursor of a *finished* gesture is read
        from: the cross hair of the state while the canvas still edits,
        the cursor of the application otherwise. It is deliberately not
        the answer of a hover - a move of the pointer over a handle or a
        box is what _update_hover_cursor answers - but the way back to
        the state after a press, a drag or a dropped selection, so a
        canvas never keeps the closed hand of a pan it never started.
        """

        if self.editable:
            self.setCursor(EDIT_CURSOR)
        else:
            self.unsetCursor()

    def _edit_shape(self, index: int) -> Optional[Any]:
        """Return one editable shape by index, None when there is none."""

        if not 0 <= int(index) < len(self._edit_shapes):
            return None
        return self._edit_shapes[int(index)][0]

    def _can_edit(self, index: int) -> bool:
        """Return True when the shape at an index may be edited."""

        if not 0 <= int(index) < len(self._edit_shapes):
            return False
        return bool(self._edit_shapes[int(index)][1])

    def _inside_image(self, widget_point: QtCore.QPointF) -> bool:
        """Return True when a widget position sits on the picture itself.

        The letterbox around a fitted picture is no place to start an
        edit: a middle click there is swallowed instead of switching the
        mode, so a stray click in the black border never arms the ROI
        editing of the record on screen.
        """

        if self._pixmap is None or self._pixmap.isNull():
            return False
        return bool(self._target_rect().contains(widget_point))

    def _edit_at(self, widget_point: QtCore.QPointF):
        """Return the (index, handle) a widget position picks."""

        if not self._inside_image(widget_point):
            return -1, ""
        point = self.widget_to_image(widget_point)
        index, handle = hit_test(
            self._edit_shapes, (point.x(), point.y()), self.view_scale()
        )
        if index < 0 or not self._can_edit(index):
            return -1, ""
        return index, handle

    def _update_hover_cursor(self, position: QtCore.QPointF) -> None:
        """Answer a hover with the cursor of what a click would do.

        The order is the precision of the answer: a handle names the
        resize it would start, a box under the pointer names the move it
        would start, and the cross hair of the state is what is left -
        the empty picture *and* every position this canvas still edits.
        A canvas that does not edit keeps the cursor of the application.
        """

        if not self.editable:
            self.unsetCursor()
            return
        index, handle = self._edit_at(position)
        if handle:
            self.setCursor(handle_cursor(handle))
        elif index >= 0:
            self.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
        else:
            self.setCursor(EDIT_CURSOR)

    def _begin_drag(self, widget_point: QtCore.QPointF) -> bool:
        """Arm a move or a resize for the shape under a position."""

        index, handle = self._edit_at(widget_point)
        if index < 0:
            return False
        point = self.widget_to_image(widget_point)
        self._selected = index
        self._active_handle = str(handle) if handle else None
        self._drag_anchor = (float(point.x()), float(point.y()))
        self._drag_start_points = list(
            shape_points(self._edit_shapes[index][0])
        )
        self.update()
        self.shape_selected.emit(self._edit_record_id, index)
        return True

    def _update_drag(self, widget_point: QtCore.QPointF) -> bool:
        """Apply one step of a running drag and repaint the result.

        The displacement a step applies is always read from the *press*
        point (see _begin_drag) and never from the position of the
        previous move: a real drag is a whole series of mouse moves and
        the shape has to follow the pointer, not the last step of it,
        which would leave a box of a long drag almost where it started.
        A pointer that comes back onto the press point therefore answers
        the geometry of the start instead of a stale offset.
        """

        anchor = self._drag_anchor
        if anchor is None:
            return False
        index = self._selected
        if index < 0 or index >= len(self._edit_shapes):
            return False
        point = self.widget_to_image(widget_point)
        delta = (
            float(point.x()) - float(anchor[0]),
            float(point.y()) - float(anchor[1]),
        )
        shape = self._edit_shapes[index][0]
        if self._active_handle is None:
            # A move translates the whole box: the translation itself is
            # clamped (see _clamped_delta) so the box slides along the
            # border of the picture instead of being folded onto it.
            delta = self._clamped_delta(self._drag_start_points, delta)
        points = self._dragged_points(shape, delta)
        if points == shape_points(shape):
            return False
        self.apply_edit_points(index, self._clamped_points(points))
        return True

    def _dragged_points(self, shape: Any, delta):
        """Return the point set of one drag step of a shape.

        A shape without a handle is moved as a whole; a shape with one is
        resized. The point set is derived from the points the drag
        started with and from the displacement between the press point
        and the pointer, never from the previous step: a drag that runs
        into the border of the picture and is pulled back returns to the
        geometry of the start instead of accumulating error, and the
        anchor of a resize stays the point the button went down on.
        """

        if self._active_handle is None:
            return [
                (x + delta[0], y + delta[1])
                for x, y in self._drag_start_points
            ]
        return resize_points(
            shape_type_of(shape),
            self._drag_start_points,
            str(self._active_handle),
            delta,
            self._drag_anchor,
        )

    def _clamped_delta(self, start_points, delta):
        """Return the translation that keeps a whole box in the picture.

        A move is clamped as one *translation*, never coordinate by
        coordinate: the delta is shortened until the bounding box of the
        translated points sits in [0, width] x [0, height] again, so a
        box dragged far over the border slides along it and keeps its
        width, its height and its area. Clamping the points one by one
        would fold every point that left the picture onto the border
        instead - a box of 40x30 pulled to the upper left corner would
        come out as four points on (0, 0), an annotation of no area at
        all, which is exactly the degenerate shape a resize is protected
        from by MIN_BOX_SIZE.

        The clamp of the *points* still runs afterwards (see
        _clamped_points, the very rule records.update_shape_points writes
        with), and it is the clamp that keeps the live picture and the
        stored json in agreement: this one only makes sure the picture it
        is applied to is still a box.

        An axis the drag does not move is answered with exactly 0.0: the
        coordinates of that axis stay bit for bit the ones of the press
        point, so a purely horizontal drag can never nudge a y of the
        shape (and the same holds the other way round), and a drag that
        comes home lands on the geometry it started from to the last
        decimal.

        A box the picture cannot hold at all - one wider or taller than
        the image itself, which the drag of a ring over the border can
        produce - is answered with no translation: it is stopped where it
        is instead of being pushed around by a clamp that has no room to
        hand out.

        An axis whose picture size is unknown is answered the very same
        way: a canvas without a picture reports two sizes of 0.0 (see
        image_size), so no border of that axis can be clamped against
        and no translation can be judged for it.
        """

        step_x, step_y = float(delta[0]), float(delta[1])
        if step_x == 0.0 and step_y == 0.0:
            return (step_x, step_y)
        box = _point_box(start_points)
        if box is None:
            return (step_x, step_y)
        left, top, right, bottom = box
        size = self.image_size()
        limits = (
            (0.0, size.width()) if size.width() > 0.0 else None,
            (0.0, size.height()) if size.height() > 0.0 else None,
        )
        sides = (
            (float(left), float(right) - float(left)),
            (float(top), float(bottom) - float(top)),
        )
        moved = (step_x, step_y)
        result = []
        for axis in (0, 1):
            if limits[axis] is None:
                # the picture has no size on this axis: there is no
                # border to clamp against, so the axis is answered with
                # the 0.0 of a drag that does not move it instead of
                # unpacking a limit that is None
                result.append(0.0)
                continue
            low, high = limits[axis]
            start, span = sides[axis]
            if moved[axis] == 0.0:
                result.append(0.0)
                continue
            if span > high - low:
                # the picture cannot hold this box on that axis: it is
                # left where it is instead of being pushed around by a
                # clamp with no room to hand out
                result.append(0.0)
                continue
            result.append(
                min(max(moved[axis], low - start), high - start - span)
            )
        return (result[0], result[1])

    def apply_edit_points(self, index: int, points) -> bool:
        """Replace the points of one editable shape and repaint it.

        The canvas owns no file: this is the live picture of a drag. The
        page stores what the drag produced when the button comes up (see
        shape_move_finished), so a drag that is still moving never
        touches the staging json.
        """

        shape = self._edit_shape(index)
        if shape is None:
            return False
        folder = [[float(x), float(y)] for x, y in points]
        if isinstance(shape, dict):
            shape["points"] = folder
        else:
            shape.points = folder
        self.update()
        return True

    def _clamped_points(self, points):
        """Clamp a point set into the picture, rounded to 2 decimals.

        The clamp is the very same rule records.update_shape_points
        writes with, so the live picture of a drag and the json stored on
        release agree to the last decimal: the box never jumps when the
        button comes up.
        """

        size = self.image_size()
        width = size.width() if size.width() > 0 else None
        height = size.height() if size.height() > 0 else None
        result = []
        for x, y in points:
            value_x = float(x)
            value_y = float(y)
            if width is not None:
                value_x = min(max(value_x, 0.0), width)
            if height is not None:
                value_y = min(max(value_y, 0.0), height)
            result.append((round(value_x, 2), round(value_y, 2)))
        return result

    def _finish_drag(self) -> bool:
        """End a drag and publish the new point set when it moved.

        A plain click only selects, and so does a click that happens to
        land on one of the eight handles, a drag that was pulled back
        onto the press point, and a resize that ran into its own
        MIN_BOX_SIZE stop: the point set is compared with the one the
        gesture started from - both of them read through the very clamp
        the writer applies - and a geometry that did not change is never
        published, so no click of the user writes a file or marks a
        record edited. The comparison also covers the shape type whose
        resize branch has nothing to resize (see resize_points), which
        hands its own points back.
        """

        self._active_handle = None
        self._drag_anchor = None
        if self._selected < 0:
            self._drag_start_points = []
            self._restore_edit_cursor()
            return False
        start = self._drag_start_points
        self._drag_start_points = []
        shape = self._edit_shape(self._selected)
        if shape is None:
            self._restore_edit_cursor()
            return False
        points = shape_points(shape)
        if self._clamped_points(points) == self._clamped_points(start):
            self._restore_edit_cursor()
            return False
        self._restore_edit_cursor()
        self.shape_move_finished.emit(
            self._edit_record_id, self._selected, points
        )
        return True

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
        """Start a pan with the left button, or an edit with the middle one.

        The middle button is the switch of the edit state: it is only
        honoured while the canvas really edits something - a canvas that
        was handed no editable shape keeps its previous behaviour and
        ignores the button - and it is only honoured on the picture
        itself, so a click in the letterbox around a fitted image starts
        nothing. With the edit state on, the left button selects and
        drags a shape instead of panning: the two gestures would fight
        over the same button, and the wheel is right there for the view.
        """

        button = event.button()
        if button == QtCore.Qt.MouseButton.MiddleButton:
            if (self.editable or self._edit_shapes) and self._inside_image(
                QtCore.QPointF(event.position())
            ):
                self.set_edit_mode(not self.editable)
                event.accept()
                return
            super().mousePressEvent(event)
            return
        if button != QtCore.Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        if self.editable:
            position = QtCore.QPointF(event.position())
            if not self._begin_drag(position):
                self.clear_selection()
            event.accept()
            return
        self._panning = True
        self._pan_origin = QtCore.QPointF(event.position())
        self.setCursor(QtCore.Qt.CursorShape.ClosedHandCursor)
        event.accept()

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802
        """Drag a shape in the edit state, pan the picture otherwise."""

        left_held = bool(event.buttons() & QtCore.Qt.MouseButton.LeftButton)
        position = QtCore.QPointF(event.position())
        if self.editable and left_held and self._drag_anchor is not None:
            self._update_drag(position)
            event.accept()
            return
        if self.editable:
            self._update_hover_cursor(position)
            super().mouseMoveEvent(event)
            return
        if not self._panning or not left_held:
            super().mouseMoveEvent(event)
            return
        delta = position - self._pan_origin
        self._pan_origin = position
        self.pan_by(delta)
        event.accept()

    def mouseReleaseEvent(  # noqa: N802
        self, event: QtGui.QMouseEvent
    ) -> None:
        """End the pan, or publish the shape a drag just produced."""

        if event.button() != QtCore.Qt.MouseButton.LeftButton:
            super().mouseReleaseEvent(event)
            return
        if self.editable and self._drag_anchor is not None:
            self._finish_drag()
            event.accept()
            return
        if self._panning:
            self._panning = False
            self._restore_edit_cursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(  # noqa: N802
        self, event: QtGui.QMouseEvent
    ) -> None:
        """Rename a box in the edit state, fit the image otherwise.

        A double click inside a box asks for the label dialog; a double
        click on empty space keeps the gesture of the previous revision
        and fits the whole picture again, so the way back to the first
        screen is never lost - not even in the edit state.
        """

        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            if self.editable:
                index, _handle = self._edit_at(
                    QtCore.QPointF(event.position())
                )
                if index >= 0:
                    self.select_shape(index)
                    self.shape_rename_requested.emit(
                        self._edit_record_id, index
                    )
                    event.accept()
                    return
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
                # the edit overlay needs the colour of every box it
                # re-strokes, so the ground truth pass hands its own
                # list over instead of letting it be computed twice;
                # the very same builder runs in set_shapes, so the
                # overlay never reads a colour of another record
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
            self._draw_edit_overlay(painter)
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

        The colours may be handed over by the caller; they are a pure
        function of the shapes and of their states, so the ground truth
        pass of the edit mode computes them once and shares them with the
        overlay instead of walking both lists twice per repaint.
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

    def _draw_edit_overlay(self, painter: QtGui.QPainter) -> None:
        """Stroke the selected box and its eight handles.

        The pass runs under the image transform of the caller, exactly
        like the shapes it decorates, and it paints three things: the
        outline of every shape the edit mode refuses, in grey and
        dashed, so the user sees at a glance what may be picked; the
        outline of the selected box again, wider and in the handle
        colour; and the eight handles themselves, one small white
        square with a coloured border per corner and per edge midpoint.

        A box is still never filled - the picture under it stays visible
        - while a handle is a solid little square: it is a widget the
        user aims at, not a region of the annotation. Nothing at all is
        painted while the edit state is off, which is what keeps the
        plain viewer of the previous revision untouched.

        The colour of the selection is the one of its own judgement
        state; a canvas whose ground truth is hidden or empty carries no
        colour list at all and the box is stroked with the plain ground
        truth colour, never with a colour of the record shown before.
        """

        if not self.editable:
            return
        entries = self._edit_shapes
        if not entries:
            return
        scale = self.view_scale()
        size = HANDLE_SIZE / scale if scale > 0 else HANDLE_SIZE
        half = size / 2.0
        for index, (shape, can_edit) in enumerate(entries):
            points = shape_points(shape)
            if not points:
                continue
            if not can_edit:
                pen = QtGui.QPen(UNEDITABLE_COLOR, UNEDITABLE_PEN_WIDTH)
                pen.setCosmetic(True)
                pen.setStyle(QtCore.Qt.PenStyle.DashLine)
                painter.setPen(pen)
                painter.setBrush(QtGui.QBrush())
                painter.drawPolygon(
                    QtGui.QPolygonF([QtCore.QPointF(x, y) for x, y in points])
                )
                continue
            if index != self._selected:
                continue
            color = (
                self._gt_colors[index]
                if index < len(self._gt_colors)
                else GT_COLOR
            )
            box = _point_box(points)
            selected = QtGui.QPen(color, SELECTED_PEN_WIDTH)
            selected.setCosmetic(True)
            painter.setPen(selected)
            painter.setBrush(QtGui.QBrush())
            painter.drawRect(
                QtCore.QRectF(
                    QtCore.QPointF(box[0], box[1]),
                    QtCore.QPointF(box[2], box[3]),
                )
            )
            painter.setPen(QtGui.QPen(HANDLE_COLOR, 1.0))
            painter.setBrush(QtGui.QBrush(HANDLE_FILL_COLOR))
            for handle in box_handles(points):
                painter.drawRect(
                    QtCore.QRectF(
                        handle[0] - half, handle[1] - half, size, size
                    )
                )

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
        view has no room for a label anywhere.
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
        """

        glyphs = self._label_glyphs(font, text)
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
        # one text origin, so the plate and the text never drift apart:
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
    "EDIT_CURSOR",
    "EDIT_MODE_TITLE_SUFFIX",
    "FALSE_POSITIVE_COLOR",
    "FIT_PADDING",
    "GT_COLOR",
    "HANDLE_COLOR",
    "HANDLE_CURSORS",
    "HANDLE_NAMES",
    "HANDLE_SIZE",
    "HIT_TOLERANCE",
    "ImageCanvas",
    "IOU_BELOW_COLOR",
    "MIN_BOX_SIZE",
    "LABEL_GAP",
    "LABEL_MARGIN",
    "LABEL_OUTLINE",
    "LABEL_OUTLINE_WIDTH",
    "LABEL_PADDING",
    "LABEL_POINT_SIZE",
    "LABEL_VERTICAL_MARGIN",
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
    "STATE_MISS",
    "STATE_OK_PAIR",
    "STATE_TO_COLOR",
    "WHEEL_DELTA",
    "SELECTED_PEN_WIDTH",
    "UNEDITABLE_COLOR",
    "WHEEL_ZOOM_STEP",
    "ZOOM_ANIM_FRACTION",
    "ZOOM_ANIM_INTERVAL_MS",
    "box_handles",
    "clamp_zoom",
    "contains_point",
    "handle_cursor",
    "hit_test",
    "load_pixmap",
    "nearest_point",
    "resize_points",
    "shape_color",
    "shape_colors",
    "shape_label",
    "shape_label_text",
    "shape_points",
    "shape_score",
    "shape_type_of",
]
