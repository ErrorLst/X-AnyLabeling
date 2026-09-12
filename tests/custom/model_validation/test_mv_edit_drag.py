"""Dragging a box: picking, moving and resizing a region shape.

The geometry of the edit mode is tested twice: the pure functions
(box_handles, hit_test, resize_points) on their own, and the real canvas -
a real Qt mouse event travels from the press through the move to the
release, and the point set the gesture produced is read back from the
shape the canvas was handed.
"""

import math
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PyQt6 import QtCore, QtGui, QtWidgets

from anylabeling.custom.model_validation.ui.image_view import (
    EDIT_CURSOR,
    HANDLE_COLOR,
    HANDLE_CURSORS,
    HANDLE_NAMES,
    HIT_TOLERANCE,
    MIN_BOX_SIZE,
    ImageCanvas,
    box_handles,
    hit_test,
    nearest_point,
    resize_points,
)

# The picture of the canvas fixture, in image pixels.
IMAGE_SIZE = (320, 240)
WIDGET_SIZE = (640, 480)
# Two boxes that overlap on purpose: the smaller one sits inside the
# bigger one, which is what makes the "smallest wins" rule observable.
BIG = ((20, 20), (220, 180))
SMALL = ((60, 60), (140, 120))


@pytest.fixture(scope="module")
def qt_app():
    "Create the offscreen application the drag tests need."

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def rect(start, end, label: str = "car") -> dict:
    "Return one rectangle of the picture coordinate system."

    return {
        "label": label,
        "shape_type": "rectangle",
        "points": [
            [start[0], start[1]],
            [end[0], start[1]],
            [end[0], end[1]],
            [start[0], end[1]],
        ],
    }


def ring(points, label: str = "car", shape_type: str = "polygon") -> dict:
    "Return one shape of an explicit point ring."

    return {
        "label": label,
        "shape_type": shape_type,
        "points": [list(point) for point in points],
    }


def points_of(shape) -> list:
    "Return the point list of a shape as a list of pairs."

    return [(float(point[0]), float(point[1])) for point in shape["points"]]


@pytest.fixture
def canvas(qt_app):
    "A shown canvas holding one editable box and one refused point shape."

    widget = ImageCanvas("GT")
    widget.resize(*WIDGET_SIZE)
    pixmap = QtGui.QPixmap(*IMAGE_SIZE)
    pixmap.fill(QtGui.QColor(30, 30, 30))
    widget.set_image(pixmap)
    shapes = [
        rect(*BIG),
        rect(*SMALL),
        {"label": "x", "shape_type": "point", "points": [[5, 5]]},
    ]
    widget.set_shapes(shapes, [], (), ())
    widget.set_editable_shapes("rec", shapes, [True, True, False])
    widget.show()
    widget.set_edit_mode(True)
    QtWidgets.QApplication.processEvents()
    try:
        yield widget
    finally:
        widget.close()


def image_point(canvas, x: float, y: float) -> QtCore.QPointF:
    "Return the widget position of one image pixel."

    return canvas.image_to_widget(QtCore.QPointF(float(x), float(y)))


def send(
    widget,
    event_type,
    position: QtCore.QPointF,
    button=QtCore.Qt.MouseButton.NoButton,
    buttons=QtCore.Qt.MouseButton.NoButton,
) -> None:
    "Send one real Qt mouse event through the whole event chain."

    event = QtGui.QMouseEvent(
        event_type,
        position,
        button,
        buttons,
        QtCore.Qt.KeyboardModifier.NoModifier,
    )
    QtWidgets.QApplication.sendEvent(widget, event)
    QtWidgets.QApplication.processEvents()


def drag(canvas, start, end) -> list:
    "Drag the left button and return the point sets the gesture published."

    published = []
    canvas.shape_move_finished.connect(
        lambda _rid, _index, points: published.append(list(points))
    )
    send(
        canvas,
        QtCore.QEvent.Type.MouseButtonPress,
        start,
        QtCore.Qt.MouseButton.LeftButton,
        QtCore.Qt.MouseButton.LeftButton,
    )
    send(
        canvas,
        QtCore.QEvent.Type.MouseMove,
        end,
        QtCore.Qt.MouseButton.NoButton,
        QtCore.Qt.MouseButton.LeftButton,
    )
    send(
        canvas,
        QtCore.QEvent.Type.MouseButtonRelease,
        end,
        QtCore.Qt.MouseButton.LeftButton,
    )
    return published


def drag_image(canvas, start, end) -> list:
    "Drag between two positions given in image pixels."

    return drag(canvas, image_point(canvas, *start), image_point(canvas, *end))


# ---------------------------------------------------------- the pure maths
def test_the_eight_handles_sit_on_the_corners_and_the_edge_midpoints():
    "Four corners and four midpoints, in the documented order."

    points = [[10, 20], [30, 20], [30, 60], [10, 60]]
    handles = box_handles(points)

    assert len(handles) == 8 == len(HANDLE_NAMES)
    assert handles == [
        (10.0, 20.0),
        (20.0, 20.0),
        (30.0, 20.0),
        (30.0, 40.0),
        (30.0, 60.0),
        (20.0, 60.0),
        (10.0, 60.0),
        (10.0, 40.0),
    ]
    # an empty shape has no handle at all
    assert box_handles([]) == []


def test_the_hit_test_prefers_a_handle_over_a_box_and_the_smallest_box():
    "A handle wins, then the box the user sees on top of the others."

    big = (rect(*BIG), True)
    small = (rect(*SMALL), True)
    refused = (
        {"label": "x", "shape_type": "point", "points": [[5, 5]]},
        False,
    )

    # the corner of the small box is a handle of it
    assert hit_test([big, small], (60.0, 60.0), 1.0) == (1, "lt")
    # the middle of the overlap belongs to the smaller box
    assert hit_test([big, small], (100.0, 90.0), 1.0) == (1, "")
    # and it belongs to the smaller box whichever order the two are
    # handed over in: the last one of the list alone would answer 1 for
    # the big box here, so only the span comparison can answer 0
    assert hit_test([small, big], (100.0, 90.0), 1.0) == (0, "")
    # outside the small box it is the big one
    assert hit_test([big, small], (200.0, 160.0), 1.0) == (0, "")
    # empty space picks nothing
    assert hit_test([big, small], (300.0, 220.0), 1.0) == (-1, "")
    # a shape the caller refused is never picked, not even on its handles
    assert hit_test([big, refused], (5.0, 5.0), 1.0) == (-1, "")


def test_the_hit_tolerance_is_a_screen_size():
    "Zooming in shrinks the tolerance in image pixels, not the feel."

    big = (rect(*BIG), True)
    # a point HIT_TOLERANCE away from the corner is a handle at 1x; it
    # is taken *outside* the box, where only the handle can reach it
    near = (20.0 - HIT_TOLERANCE + 1.0, 20.0 - HIT_TOLERANCE + 1.0)
    assert hit_test([big], near, 1.0) == (0, "lt")
    # at 10x the same widget distance is a tenth of an image pixel, so
    # the very same point is empty space
    assert hit_test([big], near, 10.0) == (-1, "")
    assert hit_test([big], (20.5, 20.4), 10.0) == (0, "lt")


def test_the_nearest_vertex_is_the_one_a_polygon_handle_moves():
    "The vertex the handle grabbed is the vertex closest to the press."

    points = [[0, 0], [100, 0], [100, 100], [0, 100]]
    assert nearest_point(points, (98.0, 2.0)) == 1
    assert nearest_point(points, (2.0, 98.0)) == 3
    assert nearest_point(points, (50.0, 50.0)) == 0


def test_a_resize_rewrites_a_rectangle_in_the_xyxy_order():
    "Every rectangle keeps the four point (left, top) convention."

    start = [[10, 10], [110, 10], [110, 60], [10, 60]]
    assert resize_points("rectangle", start, "lt", (-4.0, -6.0)) == [
        (6.0, 4.0),
        (110.0, 4.0),
        (110.0, 60.0),
        (6.0, 60.0),
    ]
    # an edge handle moves that edge alone
    assert resize_points("rectangle", start, "r", (10.0, 7.0)) == [
        (10.0, 10.0),
        (120.0, 10.0),
        (120.0, 60.0),
        (10.0, 60.0),
    ]
    assert resize_points("rectangle", start, "b", (5.0, 20.0)) == [
        (10.0, 10.0),
        (110.0, 10.0),
        (110.0, 80.0),
        (10.0, 80.0),
    ]


def test_a_resize_never_collapses_a_box():
    "The two sides stop at MIN_BOX_SIZE instead of turning inside out."

    start = [[10, 10], [110, 10], [110, 60], [10, 60]]
    collapsed = resize_points("rectangle", start, "lt", (400.0, 400.0))
    left, top = collapsed[0]
    right, bottom = collapsed[2]
    assert right - left == pytest.approx(MIN_BOX_SIZE)
    assert bottom - top == pytest.approx(MIN_BOX_SIZE)

    other = resize_points("rectangle", start, "rb", (-400.0, -400.0))
    assert other[2][0] - other[0][0] == pytest.approx(MIN_BOX_SIZE)
    assert other[2][1] - other[0][1] == pytest.approx(MIN_BOX_SIZE)


def test_a_polygon_only_moves_the_vertex_the_handle_grabbed():
    "Every other vertex of a hand drawn outline stays where it was."

    points = [[0, 0], [100, 0], [100, 100], [0, 100]]
    moved = resize_points(
        "polygon", points, "rb", (20.0, 30.0), (100.0, 100.0)
    )
    assert moved == [(0, 0), (100, 0), (120, 130), (0, 100)]
    # the very same handle dragged the other way moves the very same
    # vertex back, and still moves nobody else
    assert resize_points(
        "polygon", points, "rb", (-20.0, -30.0), (100.0, 100.0)
    ) == [(0, 0), (100, 0), (80, 70), (0, 100)]
    # the vertex the handle grabs is the one nearest to the press, so a
    # handle that sits between two vertices never moves the wrong one
    assert resize_points("polygon", points, "lt", (5.0, 5.0), (2.0, 3.0)) == [
        (5, 5),
        (100, 0),
        (100, 100),
        (0, 100),
    ]


def test_a_shape_the_branch_of_its_type_cannot_serve_is_handed_back():
    "A two point polygon and a three point ring are never rewritten."

    # A polygon needs three vertices to be one and a rotation box or a
    # quadrilateral needs exactly four corners: a shape of another count
    # is answered with its very own points, so the caller sees a point
    # set that did not change and publishes nothing. Rewriting such a
    # shape instead would turn it into an axis aligned rectangle and
    # change both the number of its points and the meaning of the
    # annotation.
    line = [[10, 20], [30, 40]]
    assert resize_points(
        "polygon", line, "rb", (50.0, 60.0), (30.0, 40.0)
    ) == [
        (10, 20),
        (30, 40),
    ]
    assert resize_points(
        "polygon", line, "lt", (-5.0, -5.0), (10.0, 20.0)
    ) == [
        (10, 20),
        (30, 40),
    ]
    # an empty polygon has no vertex to move either
    assert resize_points("polygon", [], "lt", (5.0, 5.0), (0.0, 0.0)) == []

    triangle = [[0, 0], [40, 0], [20, 30]]
    for shape_type in ("rotation", "quadrilateral"):
        for name in HANDLE_NAMES:
            assert resize_points(
                shape_type, triangle, name, (30.0, 30.0), (20.0, 30.0)
            ) == [(0, 0), (40, 0), (20, 30)], (shape_type, name)
    five = [[0, 0], [40, 0], [50, 30], [20, 50], [-10, 30]]
    assert resize_points("rotation", five, "rb", (10.0, 10.0)) == [
        (0, 0),
        (40, 0),
        (50, 30),
        (20, 50),
        (-10, 30),
    ]


def angle_of(points, first: int = 0, second: int = 1) -> float:
    "Return the angle of one side of a point ring, in degrees."

    return math.degrees(
        math.atan2(
            points[second][1] - points[first][1],
            points[second][0] - points[first][0],
        )
    )


def inner_angle(points, first: int, corner: int, second: int) -> float:
    "Return the angle of a ring at one of its corners, in degrees."

    left = (
        points[first][0] - points[corner][0],
        points[first][1] - points[corner][1],
    )
    right = (
        points[second][0] - points[corner][0],
        points[second][1] - points[corner][1],
    )
    cosine = (left[0] * right[0] + left[1] * right[1]) / (
        math.hypot(*left) * math.hypot(*right)
    )
    return math.degrees(math.acos(min(max(cosine, -1.0), 1.0)))


def ring_area(points) -> float:
    "Return the area of a four point ring, by the shoelace formula."

    doubled = 0.0
    for index in range(len(points)):
        following = points[(index + 1) % len(points)]
        doubled += (
            points[index][0] * following[1] - following[0] * points[index][1]
        )
    return abs(doubled) / 2.0


def test_a_rotated_ring_keeps_its_own_angle():
    "A rotated box is scaled along its own sides, never along the screen."

    # the 26.57 degree box the review measured: four sides of 55.902
    # units, an area of 2500 and two inner angles of 126.87 degrees
    ring_points = [[60.0, 10.0], [110.0, 35.0], [60.0, 60.0], [10.0, 35.0]]
    start_angle = angle_of(ring_points)
    assert start_angle == pytest.approx(26.56505117707799)
    assert ring_area(ring_points) == pytest.approx(2500.0)
    assert inner_angle(ring_points, 3, 0, 1) == pytest.approx(
        126.86989764584402
    )

    # whichever corner the handle names and whichever way it is
    # dragged, the corner opposite it holds still and the box keeps its
    # angle to the last decimal - the previous revision scaled the two
    # screen axes by their own amplitude and let a rotated box drift by
    # eight to twelve degrees
    for name, anchor, delta in (
        ("rb", 0, (10.0, 0.0)),
        ("rb", 0, (0.0, 10.0)),
        ("rb", 0, (-20.0, 0.0)),
        ("rb", 0, (0.0, -20.0)),
        ("rb", 0, (30.0, 30.0)),
        ("lt", 2, (15.0, -5.0)),
        ("rt", 3, (10.0, 10.0)),
        ("lb", 1, (-8.0, 4.0)),
    ):
        scaled = resize_points("rotation", ring_points, name, delta)
        assert scaled[anchor] == (
            ring_points[anchor][0],
            ring_points[anchor][1],
        ), (name, delta)
        assert angle_of(scaled) == pytest.approx(start_angle, abs=1e-9), (
            name,
            delta,
        )
        # the shape is the one the user drew: every side keeps its own
        # direction and every inner angle of the ring is untouched -
        # the four sides only ever change length
        for first, corner, second in (
            (3, 0, 1),
            (0, 1, 2),
            (1, 2, 3),
            (2, 3, 0),
        ):
            assert inner_angle(scaled, first, corner, second) == (
                pytest.approx(
                    inner_angle(ring_points, first, corner, second), abs=1e-9
                )
            ), (name, delta, corner)

    # and the sign of the factor is the sign of the drag: the very same
    # handle dragged towards its anchor shrinks the shape - the branch
    # the previous revision could not reach at all - while a drag away
    # from it grows it
    assert (
        ring_area(resize_points("rotation", ring_points, "rb", (-20.0, 0.0)))
        < 2500.0
    )
    assert (
        ring_area(resize_points("rotation", ring_points, "rb", (0.0, -20.0)))
        < 2500.0
    )
    assert (
        ring_area(resize_points("rotation", ring_points, "rb", (10.0, 10.0)))
        > 2500.0
    )


def test_a_rotated_ring_shrinks_and_grows_with_the_pointer():
    "The factor of a resize is signed: drag it back and the box shrinks."

    # an axis aligned square makes the arithmetic readable: its two
    # sides are the horizontal one and the vertical one, and each of
    # them is exactly one hundred pixels long
    ring_points = [[20.0, 20.0], [120.0, 20.0], [120.0, 120.0], [20.0, 120.0]]

    def sized(points):
        "Return the (width, height) of the bounding box of a ring."

        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        return max(xs) - min(xs), max(ys) - min(ys)

    # a drag of the bottom right corner to the right grows the width by
    # the whole displacement and leaves the height alone: the side the
    # handle names is the one that follows the pointer
    grown = resize_points("rotation", ring_points, "rb", (10.0, 0.0))
    assert sized(grown) == (110.0, 100.0)
    assert grown[2] == (130.0, 120.0)

    # a drag to the left shrinks it: this is the branch the previous
    # revision could never reach, because its factor was built from the
    # amplitude of the delta and was therefore never below one
    shrunk = resize_points("rotation", ring_points, "rb", (-40.0, 0.0))
    assert sized(shrunk) == (60.0, 100.0)
    assert shrunk[2] == (80.0, 120.0)

    # the very same rule on the vertical side
    assert sized(
        resize_points("rotation", ring_points, "rb", (0.0, 25.0))
    ) == (
        100.0,
        125.0,
    )
    assert sized(
        resize_points("rotation", ring_points, "rb", (0.0, -25.0))
    ) == (100.0, 75.0)

    # a diagonal drag of a corner moves both of its sides
    assert sized(
        resize_points("rotation", ring_points, "rb", (10.0, 10.0))
    ) == (
        110.0,
        110.0,
    )

    # a side never goes through the anchor: the stop of MIN_BOX_SIZE
    # holds the ring at one pixel instead of turning it inside out
    collapsed = resize_points("rotation", ring_points, "rb", (-500.0, -500.0))
    assert sized(collapsed) == (MIN_BOX_SIZE, MIN_BOX_SIZE)
    assert collapsed[0] == (20.0, 20.0)


def test_an_edge_handle_moves_its_own_edge_and_nothing_else():
    "A middle handle of a rotated box is a one sided resize, signed too."

    # a rotated rectangle of 100 by 50, axis aligned so that the two
    # sides of the ring are the horizontal one and the vertical one
    rect = [[10.0, 10.0], [110.0, 10.0], [110.0, 60.0], [10.0, 60.0]]

    def sized(points):
        "Return the (width, height) of the bounding box of a ring."

        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        return max(xs) - min(xs), max(ys) - min(ys)

    # the right edge follows the pointer: ten pixels to the right on a
    # side of a hundred is ten pixels of that side, and the two corners
    # of the opposite edge do not move at all
    grown = resize_points("rotation", rect, "r", (10.0, 0.0))
    wide, high = sized(grown)
    assert wide == pytest.approx(110.0)
    assert high == pytest.approx(50.0)
    assert grown[0] == (10.0, 10.0)
    assert grown[3] == (10.0, 60.0)

    # the very same handle pulled to the left shortens it - the branch
    # the previous revision could not reach - and a pull far enough
    # stops at the one pixel floor instead of crossing the anchor
    shrunk = resize_points("rotation", rect, "r", (-20.0, 0.0))
    assert sized(shrunk) == pytest.approx((80.0, 50.0))
    assert sized(resize_points("rotation", rect, "r", (-500.0, 0.0))) == (
        pytest.approx(MIN_BOX_SIZE),
        pytest.approx(50.0),
    )

    # and the three other edges behave the same way, each on its own
    # side: "l" moves the left one, "t" the top one, "b" the bottom one
    assert sized(resize_points("rotation", rect, "l", (-10.0, 0.0))) == (
        pytest.approx(110.0),
        pytest.approx(50.0),
    )
    assert sized(resize_points("rotation", rect, "t", (0.0, -25.0))) == (
        pytest.approx(100.0),
        pytest.approx(75.0),
    )
    assert sized(resize_points("rotation", rect, "b", (0.0, 25.0))) == (
        pytest.approx(100.0),
        pytest.approx(75.0),
    )
    assert sized(resize_points("rotation", rect, "b", (0.0, -30.0))) == (
        pytest.approx(100.0),
        pytest.approx(20.0),
    )

    # an edge handle keeps the edge opposite the one it names still, so
    # the corner the two of them start from never moves either
    rotated = [[60.0, 10.0], [110.0, 35.0], [60.0, 60.0], [10.0, 35.0]]
    for name, delta, fixed in (
        ("r", (10.0, 0.0), 0),
        ("l", (-10.0, 0.0), 1),
        ("t", (0.0, 10.0), 3),
        ("b", (0.0, -10.0), 0),
    ):
        scaled = resize_points("rotation", rotated, name, delta)
        assert scaled[fixed] == (
            rotated[fixed][0],
            rotated[fixed][1],
        ), (name, delta)
        # and the shape is still the same shape: the face the handle
        # names followed the pointer, the other three sides kept their
        # own directions
        assert angle_of(scaled) == pytest.approx(
            angle_of(rotated), abs=1e-9
        ), (name, delta)


def test_a_quadrilateral_resizes_along_its_own_sides():
    "The fourth shape type of the mode is scaled like a ring, never boxed."

    # a hand drawn quadrilateral: none of its corners is a right angle
    # and its two sides at the anchor are 80 and 63.25 units long
    quad = [[20.0, 20.0], [100.0, 20.0], [80.0, 100.0], [0.0, 80.0]]
    start_anchor = inner_angle(quad, 3, 0, 1)
    start_corner = inner_angle(quad, 1, 2, 3)
    assert start_anchor == pytest.approx(108.43494882292201)

    # the anchor is the corner opposite the dragged one and it never
    # moves, whatever the drag does
    for name, anchor, delta in (
        ("rb", 0, (20.0, 0.0)),
        ("rb", 0, (-20.0, 0.0)),
        ("lt", 2, (20.0, 0.0)),
        ("lt", 2, (-20.0, 0.0)),
    ):
        scaled = resize_points("quadrilateral", quad, name, delta)
        assert scaled[anchor] == (quad[anchor][0], quad[anchor][1]), (
            name,
            delta,
        )
    # the two neighbours of the anchor keep their own direction: the
    # sides of a quadrilateral are only ever made longer or shorter,
    # never turned, and the two inner angles are preserved
    scaled = resize_points("quadrilateral", quad, "rb", (20.0, 0.0))
    for index in (1, 3):
        direction = (
            scaled[index][0] - scaled[0][0],
            scaled[index][1] - scaled[0][1],
        )
        original = (quad[index][0] - quad[0][0], quad[index][1] - quad[0][1])
        cross = direction[0] * original[1] - direction[1] * original[0]
        assert cross == pytest.approx(0.0, abs=1e-9), index
    assert inner_angle(scaled, 3, 0, 1) == pytest.approx(
        start_anchor, abs=1e-9
    )
    # and so does the angle of the dragged corner, to within the two
    # degrees a non uniform scaling of a shape that is not a rectangle
    # can spend - never the twelve degrees the previous revision lost
    assert abs(inner_angle(scaled, 1, 2, 3) - start_corner) < 2.0

    # a drag towards the anchor shortens the side the pointer is on...
    shrunk = resize_points("quadrilateral", quad, "rb", (-20.0, 0.0))
    assert math.dist(shrunk[1], shrunk[0]) < math.dist(quad[1], quad[0])
    assert shrunk[2] == pytest.approx((58.333333333333336, 100.0))
    # ...and a drag away from it makes it longer, while the other side
    # of the corner does not move at all: the pointer stayed on this
    # one axis
    grown = resize_points("quadrilateral", quad, "rb", (20.0, 0.0))
    assert math.dist(grown[1], grown[0]) > math.dist(quad[1], quad[0])
    assert grown[2] == pytest.approx((101.66666666666667, 100.0))
    assert math.dist(grown[3], grown[0]) == pytest.approx(
        math.dist(quad[3], quad[0])
    )


# ------------------------------------------------------------- the canvas
def test_a_click_inside_a_box_selects_the_smallest_one(canvas):
    "The click picks the box the user sees, and only selects it."

    published = drag_image(canvas, (100.0, 90.0), (100.0, 90.0))

    assert canvas.selected_index() == 1
    # a click that moved nothing publishes nothing and writes no file
    assert published == []
    assert points_of(canvas.editable_shapes()[1]) == [
        (60.0, 60.0),
        (140.0, 60.0),
        (140.0, 120.0),
        (60.0, 120.0),
    ]


def test_a_click_on_empty_space_clears_the_selection(canvas):
    "Nothing under the pointer means nothing is selected."

    drag_image(canvas, (100.0, 90.0), (100.0, 90.0))
    assert canvas.selected_index() == 1

    drag_image(canvas, (300.0, 220.0), (300.0, 220.0))

    assert canvas.selected_index() == -1


def test_the_point_shape_of_the_canvas_is_never_picked(canvas):
    "A shape type the mode refuses is drawn but not editable."

    drag_image(canvas, (5.0, 5.0), (5.0, 5.0))

    assert canvas.selected_index() == -1
    assert canvas.editable_flags() == [True, True, False]


def test_a_drag_moves_the_whole_box_by_the_pointer_delta(canvas):
    "The picture follows the cursor: image pixels, not widget pixels."

    shape = canvas.editable_shapes()[1]
    published = drag_image(canvas, (100.0, 90.0), (130.0, 105.0))

    assert len(published) == 1
    assert points_of(shape) == [
        (90.0, 75.0),
        (170.0, 75.0),
        (170.0, 135.0),
        (90.0, 135.0),
    ]
    assert [(float(x), float(y)) for x, y in published[0]] == points_of(shape)


def test_every_handle_resizes_the_side_it_belongs_to(canvas):
    "One drag per handle, and each one moves the edges it names."

    # The eight point sets below are written out by hand, corner by
    # corner: a test that asked resize_points what to expect would only
    # prove the canvas is wired to it, never that the geometry is right
    # - a handle that moved the wrong edge would move oracle and
    # subject together and stay green.
    start = [[60, 60], [140, 60], [140, 120], [60, 120]]
    # one drag per handle: the pointer ends 10 image pixels to the left
    # or right and 10 up or down of where the handle sits
    steps = (
        (
            "lt",
            -10.0,
            -10.0,
            [(50, 50), (140, 50), (140, 120), (50, 120)],
        ),
        (
            "rt",
            10.0,
            -10.0,
            [(60, 50), (150, 50), (150, 120), (60, 120)],
        ),
        (
            "rb",
            10.0,
            10.0,
            [(60, 60), (150, 60), (150, 130), (60, 130)],
        ),
        (
            "lb",
            -10.0,
            10.0,
            [(50, 60), (140, 60), (140, 130), (50, 130)],
        ),
        ("l", -10.0, 0.0, [(50, 60), (140, 60), (140, 120), (50, 120)]),
        ("r", 10.0, 0.0, [(60, 60), (150, 60), (150, 120), (60, 120)]),
        ("t", 0.0, -10.0, [(60, 50), (140, 50), (140, 120), (60, 120)]),
        ("b", 0.0, 10.0, [(60, 60), (140, 60), (140, 130), (60, 130)]),
    )
    for name, delta_x, delta_y, expected in steps:
        handle = box_handles(start)[HANDLE_NAMES.index(name)]
        shape = canvas.editable_shapes()[1]
        shape["points"] = [list(point) for point in start]
        published = drag_image(
            canvas,
            handle,
            (handle[0] + delta_x, handle[1] + delta_y),
        )
        assert len(published) == 1, name
        assert points_of(shape) == expected, name
        # and the published set is the clamped, rounded one of the canvas
        assert [
            (round(float(x), 2), round(float(y), 2)) for x, y in published[0]
        ] == expected, name
        # the edges the handle does not name never move at all
        for axis in (0, 1):
            moved = {
                min(p[axis] for p in expected),
                max(p[axis] for p in expected),
            }
            kept = {min(p[axis] for p in start), max(p[axis] for p in start)}
            if (axis == 0 and delta_x) or (axis == 1 and delta_y):
                assert moved != kept, (name, axis)
            else:
                assert moved == kept, (name, axis)


def test_a_handle_drag_accumulates_over_the_whole_gesture(canvas):
    "A real drag is many moves: the box follows the pointer, not the step."

    shape = canvas.editable_shapes()[1]
    canvas.shape_move_finished.connect(lambda *args: None)
    start = points_of(shape)
    handle = box_handles(start)[HANDLE_NAMES.index("lt")]
    # ten moves of one image pixel each are one move of ten, which is
    # what a hand on a mouse really produces; a step relative delta
    # would leave the box one pixel from where it started
    press = image_point(canvas, *handle)
    send(
        canvas,
        QtCore.QEvent.Type.MouseButtonPress,
        press,
        QtCore.Qt.MouseButton.LeftButton,
        QtCore.Qt.MouseButton.LeftButton,
    )
    for step in range(1, 11):
        send(
            canvas,
            QtCore.QEvent.Type.MouseMove,
            image_point(canvas, handle[0] - step, handle[1] - step),
            QtCore.Qt.MouseButton.NoButton,
            QtCore.Qt.MouseButton.LeftButton,
        )
    send(
        canvas,
        QtCore.QEvent.Type.MouseButtonRelease,
        image_point(canvas, handle[0] - 10.0, handle[1] - 10.0),
        QtCore.Qt.MouseButton.LeftButton,
    )

    assert points_of(shape) == [
        (50.0, 50.0),
        (140.0, 50.0),
        (140.0, 120.0),
        (50.0, 120.0),
    ]


def test_a_multi_step_move_accumulates_as_well(canvas):
    "The same rule for a box dragged as a whole: ten steps are ten."

    shape = canvas.editable_shapes()[1]
    start = (100.0, 90.0)
    press = image_point(canvas, *start)
    send(
        canvas,
        QtCore.QEvent.Type.MouseButtonPress,
        press,
        QtCore.Qt.MouseButton.LeftButton,
        QtCore.Qt.MouseButton.LeftButton,
    )
    for step in range(1, 11):
        send(
            canvas,
            QtCore.QEvent.Type.MouseMove,
            image_point(canvas, start[0] + step, start[1]),
            QtCore.Qt.MouseButton.NoButton,
            QtCore.Qt.MouseButton.LeftButton,
        )
    send(
        canvas,
        QtCore.QEvent.Type.MouseButtonRelease,
        image_point(canvas, start[0] + 10.0, start[1]),
        QtCore.Qt.MouseButton.LeftButton,
    )

    assert points_of(shape) == [
        (70.0, 60.0),
        (150.0, 60.0),
        (150.0, 120.0),
        (70.0, 120.0),
    ]


def test_a_handle_click_that_changes_nothing_writes_nothing(canvas):
    "A press and a release on a handle is a click, not an edit."

    shape = canvas.editable_shapes()[1]
    published = []
    canvas.shape_move_finished.connect(
        lambda _rid, _index, points: published.append(list(points))
    )
    before = points_of(shape)

    # the top left corner of the small box is one of its handles
    drag_image(canvas, (60.0, 60.0), (60.0, 60.0))

    assert canvas.selected_index() == 1
    assert published == []
    assert points_of(shape) == before


def test_a_handle_dragged_out_and_back_writes_nothing(canvas):
    "The box that comes home is the box that was there: no file, no mark."

    shape = canvas.editable_shapes()[1]
    published = []
    canvas.shape_move_finished.connect(
        lambda _rid, _index, points: published.append(list(points))
    )
    before = points_of(shape)
    handle = box_handles(before)[HANDLE_NAMES.index("lt")]
    press = image_point(canvas, *handle)

    send(
        canvas,
        QtCore.QEvent.Type.MouseButtonPress,
        press,
        QtCore.Qt.MouseButton.LeftButton,
        QtCore.Qt.MouseButton.LeftButton,
    )
    # the pointer leaves the picture entirely and comes back onto the
    # corner it started from: the clamped geometry is the one of the
    # start again, and no write follows a gesture that changed nothing
    send(
        canvas,
        QtCore.QEvent.Type.MouseMove,
        image_point(canvas, -400.0, -400.0),
        QtCore.Qt.MouseButton.NoButton,
        QtCore.Qt.MouseButton.LeftButton,
    )
    send(
        canvas,
        QtCore.QEvent.Type.MouseMove,
        press,
        QtCore.Qt.MouseButton.NoButton,
        QtCore.Qt.MouseButton.LeftButton,
    )
    send(
        canvas,
        QtCore.QEvent.Type.MouseButtonRelease,
        press,
        QtCore.Qt.MouseButton.LeftButton,
    )

    assert published == []
    assert points_of(shape) == before


def test_a_drag_is_clamped_to_the_picture(canvas):
    "A box dragged over the border is stored inside the picture."

    shape = canvas.editable_shapes()[1]
    published = []
    canvas.shape_move_finished.connect(
        lambda _rid, index, points: published.append((index, list(points)))
    )
    # The two drags below are the sequence the second review measured, and
    # every point set they produce is written out by hand: the shape the
    # assertions read is named by the index the gesture published, so a
    # drag that quietly picked *another* box can never keep the test green
    # (a clamp that collapsed this box onto (0, 0) would leave the second
    # press outside it, and the gesture would move the box behind it).
    before = points_of(shape)
    drag_image(canvas, (100.0, 90.0), (-40.0, -30.0))

    # the small box is the shape under the pointer and the one that moved
    assert [index for index, _points in published] == [1]
    # a drag that leaves the picture by (-140, -120) is stopped there: the
    # box is pinned to the top left corner and is still a box of its own
    # 80x60, never four points folded onto (0, 0)
    assert points_of(shape) == [
        (0.0, 0.0),
        (80.0, 0.0),
        (80.0, 60.0),
        (0.0, 60.0),
    ]
    assert ring_area(points_of(shape)) == pytest.approx(ring_area(before))

    drag_image(canvas, (40.0, 15.0), (9999.0, 9999.0))

    # the press sits on the geometry the first drag stored, so this
    # gesture really moves the very same box again - and its exact point
    # set is the one the translation clamp of the canvas owes it: the
    # box slides to the bottom right corner and keeps its own size.
    assert [index for index, _points in published[1:]] == [1]
    assert points_of(shape) == [
        (240.0, 180.0),
        (320.0, 180.0),
        (320.0, 240.0),
        (240.0, 240.0),
    ]
    assert [(float(x), float(y)) for x, y in published[1][1]] == points_of(
        shape
    )
    assert ring_area(points_of(shape)) == pytest.approx(ring_area(before))

    # and the coordinates are inside the picture, at every corner
    for x, y in points_of(shape):
        assert 0.0 <= x <= IMAGE_SIZE[0]
        assert 0.0 <= y <= IMAGE_SIZE[1]


def test_a_move_keeps_the_size_of_the_box(canvas):
    "A move translates: every drag of it leaves width, height and area."

    shape = canvas.editable_shapes()[1]
    before = points_of(shape)
    width = max(x for x, _y in before) - min(x for x, _y in before)
    height = max(y for _x, y in before) - min(y for _x, y in before)
    assert (width, height) == (80.0, 60.0)
    # the four corners of the picture, far outside it on one side each
    for end in (
        (-500.0, -500.0),
        (9999.0, -500.0),
        (9999.0, 9999.0),
        (-500.0, 9999.0),
    ):
        shape["points"] = [list(point) for point in before]
        drag_image(canvas, (100.0, 90.0), end)
        moved = points_of(shape)
        assert max(x for x, _y in moved) - min(
            x for x, _y in moved
        ) == pytest.approx(width), end
        assert max(y for _x, y in moved) - min(
            y for _x, y in moved
        ) == pytest.approx(height), end
        assert ring_area(moved) == pytest.approx(ring_area(before)), end


def test_a_move_never_nudges_the_axis_it_does_not_use(canvas, monkeypatch):
    "A pure horizontal drag leaves every y of the shape exactly as it was."

    shape = canvas.editable_shapes()[1]
    before = points_of(shape)
    # the translation the canvas really applied: the move is clamped as
    # one delta, so the axis the drag does not use has to arrive as 0.0
    seen = []
    original = ImageCanvas._clamped_delta

    def spy(self, start_points, delta):
        seen.append((tuple(start_points), tuple(delta)))
        return original(self, start_points, delta)

    monkeypatch.setattr(ImageCanvas, "_clamped_delta", spy)

    drag_image(canvas, (100.0, 90.0), (-5000.0, 90.0))

    assert seen, "the move never asked for the clamped translation"
    # inside the picture every y of the drag is an integer, so a clamp
    # that really keeps the axis untouched hands over plain integers: a
    # clamp of the points alone would have nudged the y of each of them
    assert seen[-1][1][1] == 0.0
    assert points_of(shape) == [
        (0.0, 60.0),
        (80.0, 60.0),
        (80.0, 120.0),
        (0.0, 120.0),
    ]
    assert [y for _x, y in points_of(shape)] == [y for _x, y in before]


def test_a_resize_stops_at_one_pixel_instead_of_collapsing(canvas):
    "Pulling a corner far past its neighbour leaves a one pixel box."

    shape = canvas.editable_shapes()[1]
    handle = box_handles(points_of(shape))[HANDLE_NAMES.index("lt")]
    drag_image(canvas, handle, (9999.0, 9999.0))

    xs = [x for x, _y in points_of(shape)]
    ys = [y for _x, y in points_of(shape)]
    assert max(xs) - min(xs) == pytest.approx(MIN_BOX_SIZE)
    assert max(ys) - min(ys) == pytest.approx(MIN_BOX_SIZE)


def test_a_polygon_drag_moves_only_the_vertex_under_the_pointer():
    "The other vertices of a polygon stay exactly where the user drew them."

    widget = ImageCanvas("GT")
    widget.resize(*WIDGET_SIZE)
    pixmap = QtGui.QPixmap(*IMAGE_SIZE)
    pixmap.fill(QtGui.QColor(30, 30, 30))
    widget.set_image(pixmap)
    shape = ring([[20, 20], [200, 20], [200, 200], [20, 200]])
    widget.set_shapes([shape], [], (), ())
    widget.set_editable_shapes("rec", [shape], [True])
    widget.show()
    widget.set_edit_mode(True)
    QtWidgets.QApplication.processEvents()
    try:
        # the bottom right handle of the ring sits on its own vertex, so
        # that one vertex follows the pointer and nothing else moves
        handle = box_handles(points_of(shape))[HANDLE_NAMES.index("rb")]
        assert handle == (200.0, 200.0)
        published = drag_image(widget, handle, (170.0, 220.0))

        assert len(published) == 1
        assert points_of(shape) == [
            (20.0, 20.0),
            (200.0, 20.0),
            (170.0, 220.0),
            (20.0, 200.0),
        ]
    finally:
        widget.close()


def test_a_shape_the_resize_cannot_serve_publishes_nothing(qt_app):
    "A two point polygon and a three point ring are never rewritten."

    # The pure function above pins the point set it hands back; this one
    # pins what the canvas does with it: the drag is armed, the handle
    # really is grabbed, and the gesture still publishes nothing,
    # because the point set it produced is the one the gesture started
    # from (see _finish_drag). Without that second wall a drag of such a
    # shape would write a file and mark the record edited for nothing.
    widget = ImageCanvas("GT")
    widget.resize(*WIDGET_SIZE)
    pixmap = QtGui.QPixmap(*IMAGE_SIZE)
    pixmap.fill(QtGui.QColor(30, 30, 30))
    widget.set_image(pixmap)
    line = ring([[40, 40], [120, 100]])
    triangle = ring([[60, 140], [140, 140], [100, 200]], shape_type="rotation")
    shapes = [line, triangle]
    widget.set_shapes(shapes, [], (), ())
    widget.set_editable_shapes("rec", shapes, [True, True])
    widget.show()
    widget.set_edit_mode(True)
    QtWidgets.QApplication.processEvents()
    try:
        published = []
        widget.shape_move_finished.connect(
            lambda _rid, index, points: published.append((index, list(points)))
        )
        before = [points_of(shape) for shape in shapes]

        # The bounding box of the two point line offers eight handles
        # like every other shape, so the drag really is a resize: the
        # handle of the bottom right corner is dragged as far as it goes.
        handle = box_handles(before[0])[HANDLE_NAMES.index("rb")]
        assert handle == (120.0, 100.0)
        drag_image(widget, handle, (200.0, 180.0))

        assert widget.selected_index() == 0
        # the triangle is a three point ring: every handle of its box
        # names a corner the ring resize cannot serve either
        tri_handle = box_handles(before[1])[HANDLE_NAMES.index("rb")]
        assert tri_handle == (140.0, 200.0)
        drag_image(widget, tri_handle, (220.0, 260.0))

        assert widget.selected_index() == 1
        assert published == []
        assert [points_of(shape) for shape in shapes] == before
    finally:
        widget.close()


def test_the_middle_button_of_a_bare_canvas_arms_the_mode(canvas):
    "The canvas owns the switch as well, for a page that never filters it."

    assert canvas.editable is True

    send(
        canvas,
        QtCore.QEvent.Type.MouseButtonPress,
        image_point(canvas, 100.0, 90.0),
        QtCore.Qt.MouseButton.MiddleButton,
        QtCore.Qt.MouseButton.MiddleButton,
    )

    assert canvas.editable is False
    # and the same press arms it again
    send(
        canvas,
        QtCore.QEvent.Type.MouseButtonPress,
        image_point(canvas, 100.0, 90.0),
        QtCore.Qt.MouseButton.MiddleButton,
        QtCore.Qt.MouseButton.MiddleButton,
    )
    assert canvas.editable is True


def test_the_middle_button_of_a_bare_canvas_ignores_the_letterbox(canvas):
    "A blank canvas hands the button on instead of arming a mode."

    canvas.set_edit_mode(False)
    outside = QtCore.QPointF(1.0, 1.0)
    assert canvas._target_rect().contains(outside) is False

    send(
        canvas,
        QtCore.QEvent.Type.MouseButtonPress,
        outside,
        QtCore.Qt.MouseButton.MiddleButton,
        QtCore.Qt.MouseButton.MiddleButton,
    )

    assert canvas.editable is False
    canvas.set_edit_mode(True)


def test_a_bare_canvas_without_a_shape_never_arms_the_mode(qt_app):
    "A canvas that edits nothing keeps the behaviour of the plain viewer."

    widget = ImageCanvas("GT")
    widget.resize(*WIDGET_SIZE)
    pixmap = QtGui.QPixmap(*IMAGE_SIZE)
    pixmap.fill(QtGui.QColor(30, 30, 30))
    widget.set_image(pixmap)
    widget.show()
    QtWidgets.QApplication.processEvents()
    try:
        send(
            widget,
            QtCore.QEvent.Type.MouseButtonPress,
            image_point(widget, 10.0, 10.0),
            QtCore.Qt.MouseButton.MiddleButton,
            QtCore.Qt.MouseButton.MiddleButton,
        )
        assert widget.editable is False
    finally:
        widget.close()


def test_a_hover_names_what_a_click_would_do(canvas):
    "The cursor of a handle, of a box and of the state are three."

    def hover(x: float, y: float):
        send(
            canvas,
            QtCore.QEvent.Type.MouseMove,
            image_point(canvas, x, y),
            QtCore.Qt.MouseButton.NoButton,
            QtCore.Qt.MouseButton.NoButton,
        )
        return canvas.cursor().shape()

    # the top left handle of the small box is a resize
    assert hover(60.0, 60.0) == QtCore.Qt.CursorShape.SizeFDiagCursor
    # the right edge midpoint of it is a horizontal resize
    assert hover(140.0, 90.0) == QtCore.Qt.CursorShape.SizeHorCursor
    # the inside of the box is a move
    assert hover(100.0, 90.0) == QtCore.Qt.CursorShape.OpenHandCursor
    # and the empty picture carries the cross hair of the edit state: it
    # is the one hint a user gets that the canvas left its viewer state
    assert hover(300.0, 20.0) == EDIT_CURSOR
    assert hover(300.0, 20.0) == QtCore.Qt.CursorShape.CrossCursor

    # The eight cursors are pinned one by one, in the order of the handle
    # names: a table that named the wrong cursor for "lb" or "t" - the
    # four handles the two probes above never touch - would still answer
    # every probe of this test and stay green.
    assert HANDLE_CURSORS == {
        "lt": QtCore.Qt.CursorShape.SizeFDiagCursor,
        "t": QtCore.Qt.CursorShape.SizeVerCursor,
        "rt": QtCore.Qt.CursorShape.SizeBDiagCursor,
        "r": QtCore.Qt.CursorShape.SizeHorCursor,
        "rb": QtCore.Qt.CursorShape.SizeFDiagCursor,
        "b": QtCore.Qt.CursorShape.SizeVerCursor,
        "lb": QtCore.Qt.CursorShape.SizeBDiagCursor,
        "l": QtCore.Qt.CursorShape.SizeHorCursor,
    }
    assert set(HANDLE_CURSORS) == set(HANDLE_NAMES)
    # and the canvas really answers each of them, corner by corner
    for name, handle in zip(
        HANDLE_NAMES, box_handles(points_of(canvas.editable_shapes()[1]))
    ):
        assert hover(handle[0], handle[1]) == HANDLE_CURSORS[name], name


def test_the_cross_hair_comes_back_after_a_drag(canvas):
    "A finished gesture gives the cursor of the state back, not the arrow."

    def hover(x: float, y: float):
        send(
            canvas,
            QtCore.QEvent.Type.MouseMove,
            image_point(canvas, x, y),
            QtCore.Qt.MouseButton.NoButton,
            QtCore.Qt.MouseButton.NoButton,
        )
        return canvas.cursor().shape()

    # a drag that really moved the box: the pointer sits on the shape it
    # just let go, and the answer is the open hand of a fresh hover
    assert drag_image(
        canvas, (100.0, 90.0), (120.0, 90.0)
    ), "the fixture drag has to move the box"
    assert canvas.editable is True
    assert canvas.cursor().shape() == EDIT_CURSOR
    assert hover(120.0, 90.0) == QtCore.Qt.CursorShape.OpenHandCursor

    # a press on empty space drops the selection: the mode is still on,
    # so the pointer keeps the cross hair of the state
    send(
        canvas,
        QtCore.QEvent.Type.MouseButtonPress,
        image_point(canvas, 300.0, 20.0),
        QtCore.Qt.MouseButton.LeftButton,
        QtCore.Qt.MouseButton.LeftButton,
    )
    assert canvas.selected_index() == -1
    assert canvas.editable is True
    assert canvas.cursor().shape() == EDIT_CURSOR

    # leaving the mode is what gives the cursor of the application back
    canvas.set_edit_mode(False)
    assert canvas.cursor().shape() == QtCore.Qt.CursorShape.ArrowCursor
    assert hover(300.0, 20.0) == QtCore.Qt.CursorShape.ArrowCursor
    canvas.set_edit_mode(True)
    assert canvas.cursor().shape() == EDIT_CURSOR


def test_the_cursor_of_a_mode_that_is_off_stays_untouched(canvas):
    "The plain viewer of the previous revision keeps its own cursor."

    canvas.set_edit_mode(False)
    send(
        canvas,
        QtCore.QEvent.Type.MouseMove,
        image_point(canvas, 60.0, 60.0),
        QtCore.Qt.MouseButton.NoButton,
        QtCore.Qt.MouseButton.NoButton,
    )

    assert canvas.cursor().shape() == QtCore.Qt.CursorShape.ArrowCursor
    canvas.set_edit_mode(True)


def test_a_polygon_of_the_canvas_is_moved_as_a_whole(qt_app):
    "A ring dragged from its inside translates, it never resizes."

    widget = ImageCanvas("GT")
    widget.resize(*WIDGET_SIZE)
    pixmap = QtGui.QPixmap(*IMAGE_SIZE)
    pixmap.fill(QtGui.QColor(30, 30, 30))
    widget.set_image(pixmap)
    shape = ring([[20, 20], [200, 20], [200, 200], [20, 200]])
    widget.set_shapes([shape], [], (), ())
    widget.set_editable_shapes("rec", [shape], [True])
    widget.show()
    widget.set_edit_mode(True)
    QtWidgets.QApplication.processEvents()
    try:
        # the press sits well inside the ring, far from its handles
        published = drag_image(widget, (100.0, 100.0), (130.0, 120.0))

        assert len(published) == 1
        assert points_of(shape) == [
            (50.0, 40.0),
            (230.0, 40.0),
            (230.0, 220.0),
            (50.0, 220.0),
        ]
        # every edge kept its length: a move is no resize
        assert points_of(shape)[1][0] - points_of(shape)[0][0] == 180.0
        assert points_of(shape)[2][1] - points_of(shape)[1][1] == 180.0
    finally:
        widget.close()


def test_the_handles_are_painted_over_the_selected_box(canvas):
    "The selection is visible: an outline and eight filled squares."

    canvas.select_shape(1)
    QtWidgets.QApplication.processEvents()
    rendered = render(canvas)
    handle_color = HANDLE_COLOR

    assert painted(rendered, handle_color) > 0
    # the handles are eight small squares around the box of the selection
    box = points_of(canvas.editable_shapes()[1])
    xs = [point[0] for point in box]
    ys = [point[1] for point in box]
    for corner in ((min(xs), min(ys)), (max(xs), max(ys))):
        widget_point = canvas.image_to_widget(
            QtCore.QPointF(corner[0], corner[1])
        )
        assert near_color(
            rendered, handle_color, widget_point, radius=6
        ), corner

    # nothing of the overlay is painted while the mode is off
    canvas.set_edit_mode(False)
    QtWidgets.QApplication.processEvents()
    assert painted(render(canvas), handle_color) == 0


def render(widget):
    "Render a widget offscreen and return its pixel rows."

    image = QtGui.QImage(widget.size(), QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor(0, 0, 0))
    painter = QtGui.QPainter(image)
    widget.render(
        painter,
        QtCore.QPoint(0, 0),
        QtGui.QRegion(widget.rect()),
        QtWidgets.QWidget.RenderFlag.DrawWindowBackground,
    )
    painter.end()
    pointer = image.constBits()
    pointer.setsize(image.sizeInBytes())
    pixels = bytes(pointer)
    height, width = image.height(), image.width()
    rows = []
    for row in range(height):
        start = row * width * 4
        rows.append(pixels[start : start + width * 4])
    return rows


def painted(rows, color) -> int:
    "Return the count of the pixels of one colour."

    wanted = bytes((color.blue(), color.green(), color.red(), 255))
    return sum(row.count(wanted) for row in rows)


def near_color(rows, color, point, radius: int) -> bool:
    "Return True when the colour sits within a radius of a widget point."

    wanted = bytes((color.blue(), color.green(), color.red(), 255))
    column = int(round(point.x()))
    row = int(round(point.y()))
    for y in range(max(row - radius, 0), min(row + radius + 1, len(rows))):
        line = rows[y]
        start = max(column - radius, 0) * 4
        window = line[start : (column + radius + 1) * 4]
        if wanted in [
            window[index : index + 4] for index in range(0, len(window), 4)
        ]:
            return True
    return False
