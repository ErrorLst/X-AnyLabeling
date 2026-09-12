"""The middle button arms the ROI edit mode of the results page.

The mode is a state of the *left* canvas alone - the ground truth of the
current record is the annotation, the predictions of the right canvas are
the output of the model - so the tests below drive the real Qt event path
of the middle button on both canvases and read the page state back: which
canvas is editable, what the two titles say, which hint line is written
and what the right button does while the mode is on.
"""

import os
import os.path as osp

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import cv2
import numpy as np
import pytest

from PyQt6 import QtCore, QtGui, QtTest, QtWidgets

from anylabeling.custom.model_validation import dataset
from anylabeling.custom.model_validation import records as records_module
from anylabeling.custom.model_validation.ui.image_view import (
    EDIT_CURSOR,
    GT_COLOR,
    STATE_TO_COLOR,
    WHEEL_DELTA,
    WHEEL_ZOOM_STEP,
    shape_points,
)
from anylabeling.custom.model_validation.ui.results_page import (
    EDIT_HINT,
    EDIT_HINT_NO_PREVIEW,
    EDIT_HINT_NO_REGION,
    EDIT_TITLE_SUFFIX,
    GT_CANVAS_TITLE,
    PRED_CANVAS_TITLE,
    ResultsPage,
)

CLASSES = ["car", "bus"]
IMAGE_SIZE = (64, 48)
WIDGET_SIZE = (1024, 640)
# the box of the fixture label, in image pixels
BOX = ((8, 8), (48, 36))


@pytest.fixture(scope="module")
def qt_app():
    "Create the offscreen application the edit mode tests need."

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def staging_layout(scratch: str, suffix: str) -> str:
    "Create the staging folder layout the tool expects."

    staging = osp.join(scratch, dataset.STAGING_PREFIX + suffix)
    for folder in (dataset.ORIGINAL_DIRNAME, dataset.AUGMENTED_DIRNAME):
        for sub in (dataset.IMAGES_DIRNAME, dataset.LABELS_DIRNAME):
            os.makedirs(osp.join(staging, folder, sub), exist_ok=True)
    return staging


def write_image(path: str, value: int = 40) -> None:
    "Write a tiny, deterministic png file."

    os.makedirs(osp.dirname(path), exist_ok=True)
    image = np.full((IMAGE_SIZE[1], IMAGE_SIZE[0], 3), int(value), np.uint8)
    ok, buffer = cv2.imencode(".png", image)
    assert ok
    buffer.tofile(path)


def rect(start, end, label: str = "car") -> dict:
    "Return one rectangle of the picture coordinate system."

    return {
        "label": label,
        "shape_type": "rectangle",
        "points": [
            list(start),
            [end[0], start[1]],
            list(end),
            [start[0], end[1]],
        ],
        "group_id": None,
        "description": "",
        "flags": {},
    }


def point_shape(x: float, y: float, label: str = "car") -> dict:
    "Return one point shape, which the edit mode must refuse."

    return {
        "label": label,
        "shape_type": "point",
        "points": [[x, y]],
        "group_id": None,
        "description": "",
        "flags": {},
    }


def label_payload(relpath: str, shapes) -> dict:
    "Return the xlabel payload of a staged pair."

    return {
        "version": "3.0.0",
        "flags": {},
        "checked": False,
        "shapes": shapes,
        "imagePath": osp.basename(relpath),
        "imageData": None,
        "imageHeight": IMAGE_SIZE[1],
        "imageWidth": IMAGE_SIZE[0],
    }


def staged_original(staging: str, relpath: str, shapes):
    "Create one staged original record with its files."

    paths = dataset.staging_paths(
        staging, records_module.KIND_ORIGINAL, relpath
    )
    write_image(paths["image"])
    dataset.write_json(paths["label"], label_payload(relpath, shapes))
    return records_module.make_record(
        records_module.KIND_ORIGINAL, relpath, paths["image"], paths["label"]
    )


def build_page(staging: str, records, classes=None) -> ResultsPage:
    "Return a shown page holding the given records."

    page = ResultsPage()
    page.resize(*WIDGET_SIZE)
    page.set_context(
        list(classes if classes is not None else CLASSES), staging
    )
    page.set_records(records)
    page.filter_combo.setCurrentIndex(0)
    page.show()
    QtWidgets.QApplication.processEvents()
    return page


@pytest.fixture
def page(qt_app, tmp_path):
    "A shown page with one original carrying a rectangle and a point."

    staging = staging_layout(str(tmp_path), "edit_mode")
    record = staged_original(
        staging, "a.png", [rect(*BOX), point_shape(4, 40)]
    )
    widget = build_page(staging, [record])
    try:
        yield widget
    finally:
        widget.close()


def image_point(page, x: float, y: float) -> QtCore.QPointF:
    "Return the widget position of one image pixel of the left canvas."

    return page.gt_canvas.image_to_widget(QtCore.QPointF(float(x), float(y)))


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


def press(
    widget,
    position: QtCore.QPointF,
    button=QtCore.Qt.MouseButton.MiddleButton,
) -> None:
    "Press one button on a widget."

    send(
        widget,
        QtCore.QEvent.Type.MouseButtonPress,
        position,
        button,
        button,
    )


def release(
    widget,
    position: QtCore.QPointF,
    button=QtCore.Qt.MouseButton.MiddleButton,
) -> None:
    "Release one button on a widget."

    send(widget, QtCore.QEvent.Type.MouseButtonRelease, position, button)


def toggle(widget, position: QtCore.QPointF) -> None:
    "Press and release the middle button at one position."

    press(widget, position)
    release(widget, position)


def drag(
    widget,
    start: QtCore.QPointF,
    end: QtCore.QPointF,
    button=QtCore.Qt.MouseButton.LeftButton,
) -> None:
    "Drag the left button from one widget position to another."

    press(widget, start, button)
    send(
        widget,
        QtCore.QEvent.Type.MouseMove,
        end,
        QtCore.Qt.MouseButton.NoButton,
        button,
    )
    release(widget, end, button)


def test_the_middle_button_arms_the_edit_mode_on_the_ground_truth(page):
    "A middle click enters the mode and the left canvas is the one edited."

    assert page.edit_mode is False
    assert page.gt_canvas.editable is False
    assert page.pred_canvas.editable is False

    center = image_point(page, 28.0, 22.0)
    toggle(page.gt_canvas, center)

    assert page.edit_mode is True
    assert page.gt_canvas.editable is True
    # the predictions of the right canvas are never editable
    assert page.pred_canvas.editable is False
    assert page.pred_canvas.editable_shapes() == []
    # the left canvas holds the two shapes of the record, one of them
    # refused: the point is drawn by the plain viewer but not picked
    assert len(page.gt_canvas.editable_shapes()) == 2
    assert page.gt_canvas.editable_flags() == [True, False]
    assert page.gt_canvas.edit_record_id() == page.current_record().record_id


def test_the_middle_button_leaves_the_mode_again(page):
    "The very same gesture is the way out, however often it is used."

    center = image_point(page, 28.0, 22.0)
    toggle(page.gt_canvas, center)
    assert page.edit_mode is True

    toggle(page.gt_canvas, center)
    assert page.edit_mode is False
    assert page.gt_canvas.editable is False
    assert page.gt_canvas.editable_shapes() == []
    assert page.gt_canvas.selected_index() == -1

    # and it is not a one way door: the mode can be entered again
    toggle(page.gt_canvas, center)
    assert page.edit_mode is True


def test_the_mode_belongs_to_the_left_canvas_whichever_one_is_clicked(page):
    "A middle click on the prediction canvas arms the ground truth canvas."

    center = image_point(page, 28.0, 22.0)
    toggle(page.pred_canvas, center)

    assert page.edit_mode is True
    assert page.gt_canvas.editable is True
    assert page.pred_canvas.editable is False

    toggle(page.pred_canvas, center)
    assert page.edit_mode is False
    assert page.gt_canvas.editable is False


def test_a_middle_click_in_the_letterbox_arms_nothing(page):
    "The black border around a fitted picture is no place to start an edit."

    canvas = page.gt_canvas
    rect = canvas._target_rect()
    assert rect.top() > 2, "the fixture picture needs a border to click in"
    outside = QtCore.QPointF(rect.center().x(), 0.5)
    assert rect.contains(outside) is False

    toggle(canvas, outside)

    assert page.edit_mode is False
    assert canvas.editable is False
    # the same press on the picture itself does enter the mode
    toggle(canvas, image_point(page, 28.0, 22.0))
    assert page.edit_mode is True


def test_a_middle_click_in_the_letterbox_leaves_the_mode_again(page):
    "The border refuses the way in, never the way out."

    # The rule is deliberately one sided: entering the mode means arming
    # gestures on the annotation, which the black band around a fitted
    # picture cannot do, while leaving it costs nothing and has to work
    # wherever the pointer happens to be - a user who lost the picture
    # under a zoomed view must not be locked inside the mode.
    canvas = page.gt_canvas
    border = canvas._target_rect()
    assert border.top() > 2, "the fixture picture needs a border to click in"
    outside = QtCore.QPointF(border.center().x(), 0.5)
    assert border.contains(outside) is False

    toggle(canvas, image_point(page, 28.0, 22.0))
    assert page.edit_mode is True

    toggle(canvas, outside)

    assert page.edit_mode is False
    assert canvas.editable is False
    assert canvas.editable_shapes() == []
    assert canvas.selected_index() == -1

    # ... and the band does not arm the mode back either
    toggle(canvas, outside)
    assert page.edit_mode is False


class FakeWheel:
    "Minimal stand-in for the QWheelEvent Qt hands to a canvas."

    def __init__(self, x, y, notches=1.0):
        self._angle = QtCore.QPoint(0, int(round(WHEEL_DELTA * notches)))
        self._position = QtCore.QPointF(float(x), float(y))
        self.accepted = 0
        self.ignored = 0

    def angleDelta(self):
        "Return the wheel delta."

        return self._angle

    def position(self):
        "Return the cursor position inside the widget."

        return self._position

    def accept(self):
        "Record that the canvas accepted the wheel event."

        self.accepted += 1

    def ignore(self):
        "Record that the canvas refused the wheel event."

        self.ignored += 1


def test_the_wheel_keeps_zooming_while_the_mode_is_on(page):
    "The edit mode owns the left button alone, never the wheel."

    canvas = page.gt_canvas
    center = image_point(page, 28.0, 22.0)
    toggle(canvas, center)
    assert page.edit_mode is True

    canvas.wheelEvent(FakeWheel(center.x(), center.y(), 1.0))

    assert canvas.target_zoom() == pytest.approx(WHEEL_ZOOM_STEP)
    canvas.finish_zoom()
    # the zoom really landed on the canvas, and the mode is still on
    assert canvas.zoom_factor() == pytest.approx(WHEEL_ZOOM_STEP)
    assert page.edit_mode is True
    # a wheel notch never moved a shape of the record
    shape = canvas.editable_shapes()[0]
    assert shape["points"] == [
        [8.0, 8.0],
        [48.0, 8.0],
        [48.0, 36.0],
        [8.0, 36.0],
    ]


def test_the_a_and_d_keys_leave_the_mode_on_the_next_picture(qt_app, tmp_path):
    """A and D keep walking the list and drop the mode on the way.

    The keyboard switch of a record is the switch of the picture the
    mode was armed on (see test_mv_edit_exit_on_switch): the next record
    is shown in the plain preview, and the mode can be armed on it again
    with the very same middle click.
    """

    staging = staging_layout(str(tmp_path), "edit_keys")
    first = staged_original(staging, "a.png", [rect(*BOX)])
    second = staged_original(
        staging, "b.png", [rect((2, 2), (20, 16)), point_shape(6, 6)]
    )
    widget = build_page(staging, [first, second])
    try:
        toggle(widget.gt_canvas, image_point(widget, 28.0, 22.0))
        assert widget.edit_mode is True
        assert widget.current_record() is first
        assert widget.gt_canvas.edit_record_id() == first.record_id

        QtTest.QTest.keyClick(widget.gt_canvas, QtCore.Qt.Key.Key_D)
        QtWidgets.QApplication.processEvents()

        assert widget.current_record() is second
        assert widget.edit_mode is False
        assert widget.gt_canvas.editable is False
        assert widget.gt_canvas.edit_record_id() == ""
        assert widget.gt_canvas.editable_shapes() == []

        # armed again on the record that is on screen now, the mode holds
        # the shapes of *that* record - the point shape included, which
        # the mode draws but never picks
        toggle(widget.gt_canvas, image_point(widget, 10.0, 8.0))
        assert widget.edit_mode is True
        assert widget.gt_canvas.edit_record_id() == second.record_id
        assert widget.gt_canvas.editable_flags() == [True, False]

        # and walking back leaves it a second time
        QtTest.QTest.keyClick(widget.gt_canvas, QtCore.Qt.Key.Key_A)
        QtWidgets.QApplication.processEvents()

        assert widget.current_record() is first
        assert widget.edit_mode is False
        assert widget.gt_canvas.edit_record_id() == ""
    finally:
        widget.close()


def test_a_middle_click_in_the_letterbox_of_the_prediction_arms_nothing(page):
    "The black border of the right picture is no place to start an edit."

    canvas = page.pred_canvas
    rect = canvas._target_rect()
    assert rect.top() > 2, "the fixture picture needs a border to click in"
    outside = QtCore.QPointF(rect.center().x(), 0.5)
    assert rect.contains(outside) is False

    toggle(canvas, outside)

    assert page.edit_mode is False
    assert page.gt_canvas.editable is False
    # the same press on the picture itself enters the mode, and the
    # left canvas is the one it arms
    toggle(canvas, image_point(page, 28.0, 22.0))
    assert page.edit_mode is True
    assert page.gt_canvas.editable is True
    assert page.pred_canvas.editable is False


def test_the_cursor_names_the_mode_of_the_canvas(page):
    "The middle button changes the pointer of both canvases at once."

    def hover(canvas, x: float, y: float):
        send(
            canvas,
            QtCore.QEvent.Type.MouseMove,
            canvas.image_to_widget(QtCore.QPointF(float(x), float(y))),
            QtCore.Qt.MouseButton.NoButton,
            QtCore.Qt.MouseButton.NoButton,
        )
        return canvas.cursor().shape()

    # the plain viewer of the previous revision keeps the default cursor
    assert page.gt_canvas.cursor().shape() == QtCore.Qt.CursorShape.ArrowCursor
    assert page.pred_canvas.cursor().shape() == (
        QtCore.Qt.CursorShape.ArrowCursor
    )
    assert hover(page.gt_canvas, 28.0, 22.0) == (
        QtCore.Qt.CursorShape.ArrowCursor
    )

    toggle(page.gt_canvas, image_point(page, 28.0, 22.0))

    # entering the mode is visible before the pointer moves at all: the
    # canvas carries the cursor of the state from the very middle click
    assert page.edit_mode is True
    assert page.gt_canvas.cursor().shape() == EDIT_CURSOR
    # the right canvas keeps the predictions read only, and it says so:
    # the mode belongs to the left one alone
    assert page.pred_canvas.cursor().shape() == (
        QtCore.Qt.CursorShape.ArrowCursor
    )
    # a hover over the box is a move and beats the cross hair of the state
    assert hover(page.gt_canvas, 28.0, 22.0) == (
        QtCore.Qt.CursorShape.OpenHandCursor
    )
    # a hover over empty space falls back to the cross hair
    assert hover(page.gt_canvas, 2.0, 2.0) == EDIT_CURSOR

    # a middle click on the right canvas is the switch of the left one,
    # so the left canvas gives the cursor of the application back
    toggle(page.pred_canvas, image_point(page, 28.0, 22.0))
    assert page.edit_mode is False
    assert page.gt_canvas.cursor().shape() == QtCore.Qt.CursorShape.ArrowCursor
    assert hover(page.gt_canvas, 2.0, 2.0) == QtCore.Qt.CursorShape.ArrowCursor


def test_the_titles_and_the_hint_line_follow_the_mode(page):
    "The left title names the mode, the right one never does."

    assert page.gt_canvas.title == GT_CANVAS_TITLE
    assert page.pred_canvas.title == PRED_CANVAS_TITLE
    assert page.edit_hint.isVisibleTo(page) is False

    toggle(page.gt_canvas, image_point(page, 28.0, 22.0))

    assert page.gt_canvas.title == GT_CANVAS_TITLE + EDIT_TITLE_SUFFIX
    assert page.pred_canvas.title == PRED_CANVAS_TITLE
    assert page.edit_hint.text() == EDIT_HINT
    assert page.edit_hint.isVisibleTo(page) is True

    toggle(page.gt_canvas, image_point(page, 28.0, 22.0))

    # the plain titles come back word for word
    assert page.gt_canvas.title == GT_CANVAS_TITLE
    assert page.pred_canvas.title == PRED_CANVAS_TITLE
    assert page.edit_hint.text() == ""
    assert page.edit_hint.isVisibleTo(page) is False


def test_the_left_drag_pans_again_once_the_mode_is_left(page):
    "Outside the mode the left button keeps dragging the picture."

    canvas = page.gt_canvas
    center = image_point(page, 28.0, 22.0)
    before = canvas.view_center()

    drag(canvas, center, QtCore.QPointF(center.x() + 30.0, center.y()))

    assert abs(canvas.view_center().x() - before.x()) > 1e-6
    assert canvas.editable is False

    # inside the mode the very same gesture moves the box and leaves the
    # view alone: the two gestures share the button and never both act
    toggle(canvas, center)
    settled = canvas.view_center()
    shape = canvas.editable_shapes()[0]
    start = [list(point) for point in shape["points"]]
    drag(canvas, center, QtCore.QPointF(center.x() + 30.0, center.y()))

    assert canvas.view_center() == pytest.approx(settled, abs=1e-6)
    assert shape["points"] != start


def test_the_right_button_preview_is_refused_while_the_mode_is_on(page):
    "The edit mode owns the right button, and says so in one line."

    center = image_point(page, 28.0, 22.0)
    toggle(page.gt_canvas, center)

    press(page.gt_canvas, center, QtCore.Qt.MouseButton.RightButton)

    assert page.preview_active() is False
    assert page.displayed_record() is page.current_record()
    assert page.preview_note.text() == EDIT_HINT_NO_PREVIEW
    assert page.preview_note.isVisibleTo(page) is True

    release(page.gt_canvas, center, QtCore.Qt.MouseButton.RightButton)
    # leaving the mode gives the preview back
    toggle(page.gt_canvas, center)
    assert page.edit_mode is False
    assert page.preview_note.text() == ""


def test_a_record_without_a_region_shape_answers_with_one_line(
    qt_app, tmp_path
):
    "A record the mode could not edit refuses the gesture, it never crashes."

    staging = staging_layout(str(tmp_path), "edit_no_region")
    record = staged_original(staging, "a.png", [point_shape(10, 10)])
    widget = build_page(staging, [record])
    try:
        toggle(widget.gt_canvas, image_point(widget, 10.0, 10.0))

        assert widget.edit_mode is False
        assert widget.gt_canvas.editable is False
        assert widget.preview_note.text() == EDIT_HINT_NO_REGION
        assert widget.gt_canvas.title == GT_CANVAS_TITLE
        # the regions of the mode really are the region shape types
        assert widget.editable_regions() == []
    finally:
        widget.close()


def test_a_record_without_a_label_refuses_the_gesture_as_well(
    qt_app, tmp_path
):
    "A record without a staging json is not an editable record either."

    staging = staging_layout(str(tmp_path), "edit_no_label")
    record = staged_original(staging, "a.png", [rect(*BOX)])
    record.staging_label_path = ""
    widget = build_page(staging, [record])
    try:
        assert widget.editable_regions() == []
        toggle(widget.gt_canvas, image_point(widget, 28.0, 22.0))
        assert widget.edit_mode is False
        assert widget.preview_note.text() == EDIT_HINT_NO_REGION
    finally:
        widget.close()


def test_the_edit_overlay_follows_the_shape_it_decorates(qt_app, tmp_path):
    "The colours of the overlay belong to the shapes of the record shown."

    staging = staging_layout(str(tmp_path), "edit_gt_colors")
    first = staged_original(
        staging,
        "a.png",
        [rect(*BOX), rect((2, 2), (20, 16)), point_shape(4, 40)],
    )
    second = staged_original(staging, "b.png", [rect(*BOX)])
    widget = build_page(staging, [first, second])
    canvas = widget.gt_canvas
    try:
        # a ground truth whose states colour one box as a miss
        widget.set_records([first])
        QtWidgets.QApplication.processEvents()
        canvas.set_shapes(
            [rect(*BOX), rect((2, 2), (20, 16)), point_shape(4, 40)],
            [],
            ["MISS", "CLASS_MISMATCH", "OK_PAIR"],
            (),
        )
        assert canvas._gt_colors == [
            STATE_TO_COLOR["MISS"],
            STATE_TO_COLOR["CLASS_MISMATCH"],
            GT_COLOR,
        ]

        # the canvas is handed another record: the colours of the first
        # one may not survive it, or the selection of a box would be
        # stroked with the verdict of a record that is not on screen
        canvas.set_shapes([rect(*BOX)], [], ["CLASS_MISMATCH"], ())
        assert len(canvas._gt_colors) == 1
        assert canvas._gt_colors == [STATE_TO_COLOR["CLASS_MISMATCH"]]

        # hiding the ground truth hides its colours as well
        canvas.show_ground_truth = False
        canvas.set_shapes([rect(*BOX)], [], ["CLASS_MISMATCH"], ())
        assert canvas._gt_colors == []

        # a cleared canvas keeps nothing of the record it showed
        canvas.show_ground_truth = True
        canvas.set_shapes([rect(*BOX)], [], ["CLASS_MISMATCH"], ())
        assert canvas._gt_colors
        canvas.clear()
        assert canvas._gt_colors == []
    finally:
        widget.close()


def test_the_preview_of_a_parent_shows_nothing_to_edit(qt_app, tmp_path):
    "A held right button replaces the shapes *and* the handles on screen."

    staging = staging_layout(str(tmp_path), "edit_preview_overlay")
    original = staged_original(staging, "a.png", [rect(*BOX)])
    child = staged_original(staging, "a_aug.png", [rect((4, 4), (30, 20))])
    child.kind = records_module.KIND_AUGMENTED
    child.record_id = records_module.KIND_AUGMENTED + "::a_aug.png"
    child.parent_record_id = original.record_id
    widget = build_page(staging, [original, child], classes=CLASSES)
    try:
        assert widget.table.rowCount() == 2
        widget.table.selectRow(1)
        QtWidgets.QApplication.processEvents()
        assert widget.current_record() is child

        toggle(widget.gt_canvas, image_point(widget, 20.0, 14.0))
        assert widget.edit_mode is True
        assert widget.gt_canvas.edit_record_id() == child.record_id
        assert points_of(widget.gt_canvas.editable_shapes()[0]) == [
            (4.0, 4.0),
            (30.0, 4.0),
            (30.0, 20.0),
            (4.0, 20.0),
        ]

        # the mode owns the right button, so the preview is armed the
        # way the page itself would arm it (see begin_parent_preview)
        assert widget.begin_parent_preview() is True
        assert widget.displayed_record() is original
        # the picture is the parent's, and so is the box on it: the
        # child shapes never decorate the original they came from
        assert [
            shape_points(shape) for shape in widget.gt_canvas._ground_truth
        ] == [[(8.0, 8.0), (48.0, 8.0), (48.0, 36.0), (8.0, 36.0)]]
        assert widget.gt_canvas.editable_shapes() == []
        assert widget.gt_canvas.selected_index() == -1

        assert widget.end_parent_preview() is True
        assert widget.displayed_record() is child
        assert points_of(widget.gt_canvas.editable_shapes()[0]) == [
            (4.0, 4.0),
            (30.0, 4.0),
            (30.0, 20.0),
            (4.0, 20.0),
        ]
    finally:
        widget.close()


def points_of(shape) -> list:
    "Return the point list of a shape as a list of pairs."

    return [(float(point[0]), float(point[1])) for point in shape["points"]]


def test_moving_to_another_record_leaves_the_mode_again(qt_app, tmp_path):
    """Another row is another picture, and the mode is left with the old one.

    This is the new behaviour of the requirement "switching the picture
    returns to the preview mode": the mode used to follow the selection,
    which left the gestures armed on a record the user had never asked
    to edit. Every piece of the mode is given back with the record - the
    flag, the editable set of the left canvas, the title suffix, the
    hint line, the shapes and the selection of the shape.
    """

    staging = staging_layout(str(tmp_path), "edit_two_records")
    first = staged_original(staging, "a.png", [rect(*BOX)])
    second = staged_original(
        staging, "b.png", [rect((2, 2), (20, 16)), point_shape(6, 6)]
    )
    widget = build_page(staging, [first, second])
    try:
        toggle(widget.gt_canvas, image_point(widget, 28.0, 22.0))
        assert widget.edit_mode is True
        assert widget.gt_canvas.edit_record_id() == first.record_id
        assert widget.gt_canvas.title == GT_CANVAS_TITLE + EDIT_TITLE_SUFFIX
        assert widget.edit_hint.text() == EDIT_HINT

        widget.table.selectRow(1)
        QtWidgets.QApplication.processEvents()

        assert widget.current_record() is second
        assert widget.edit_mode is False
        assert widget.gt_canvas.editable is False
        assert widget.gt_canvas.edit_record_id() == ""
        assert widget.gt_canvas.editable_shapes() == []
        assert widget.gt_canvas.editable_flags() == []
        assert widget.gt_canvas.selected_index() == -1
        assert widget.gt_canvas.title == GT_CANVAS_TITLE
        assert widget.pred_canvas.title == PRED_CANVAS_TITLE
        assert widget.edit_hint.text() == ""
        assert widget.edit_hint.isVisibleTo(widget) is False
        # the record that is on screen now is the one the list moved to,
        # and it carries the one region shape of its own label
        regions = widget.editable_regions()
        assert len(regions) == 1
        assert regions[0]["shape_type"] == "rectangle"
        assert regions[0]["points"] == [
            [2.0, 2.0],
            [20.0, 2.0],
            [20.0, 16.0],
            [2.0, 16.0],
        ]
    finally:
        widget.close()


def test_a_new_record_list_leaves_the_mode_safely(qt_app, tmp_path):
    "Replacing the records never crashes a page that is editing."

    staging = staging_layout(str(tmp_path), "edit_relist")
    record = staged_original(staging, "a.png", [rect(*BOX)])
    widget = build_page(staging, [record])
    try:
        toggle(widget.gt_canvas, image_point(widget, 28.0, 22.0))
        assert widget.edit_mode is True

        widget.set_records([])
        QtWidgets.QApplication.processEvents()

        # an empty list has no record left to edit: the mode is dropped
        # with the record it was armed on, and the two canvases are
        # emptied without a single shape to drag
        assert widget.edit_mode is False
        assert widget.gt_canvas.editable is False
        assert widget.gt_canvas.editable_shapes() == []
        assert widget.gt_canvas.edit_record_id() == ""
        assert widget.gt_canvas.selected_index() == -1
        assert widget.gt_canvas.title == GT_CANVAS_TITLE
        assert widget.edit_hint.text() == ""

        # the same record list armed the mode again once it is back
        widget.set_records([record])
        QtWidgets.QApplication.processEvents()
        assert widget.current_record() is record
        toggle(widget.gt_canvas, image_point(widget, 28.0, 22.0))
        assert widget.edit_mode is True
        assert widget.gt_canvas.editable is True
    finally:
        widget.close()
