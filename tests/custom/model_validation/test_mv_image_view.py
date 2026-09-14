"""Results page viewers: read only zoom, drag pan, GT / Pred sync.

The right hand side of the results page is a viewer: the left canvas
draws the ground truth, the right one the predictions, both in the pixel
coordinate system of the original image (origin at the top left) and
both driven by one view state. The wheel zoom is animated (a notch
moves a target, small steps walk towards it) and the left button drags
the picture. The canvas owns no edit state at all: the box editor of
the previous revision lived here and is gone (see
test_the_canvas_owns_no_edit_gesture). A box the matching merged may
carry several classes: its label is then one block of one row per
class, stacked one line height of the label font apart - that height
times LABEL_LINE_SPACING - so the rows never overlap (see the label
block tests at the bottom of this file).
"""

import os
import os.path as osp

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import cv2
import numpy as np
import pytest

from PyQt6 import QtCore, QtGui, QtWidgets

from anylabeling.custom.model_validation import dataset
from anylabeling.custom.model_validation import records as records_module
from anylabeling.custom.model_validation.ui import (
    image_view as image_view_module,
)
from anylabeling.custom.model_validation.ui.dialog import ModelValidationDialog
from anylabeling.custom.model_validation.ui.image_view import (
    LABEL_LINE_SPACING,
    LABEL_OUTLINE_WIDTH,
    LABEL_PADDING,
    MAX_ZOOM,
    MIN_ZOOM,
    SCORE_FORMAT,
    WHEEL_DELTA,
    WHEEL_ZOOM_STEP,
    ImageCanvas,
    shape_label_rows,
    shape_label_text,
    shape_score,
)

CLASSES = ["a0_dian", "a1_xian"]

IMAGE_SIZE = (640, 480)
# the canvas refuses a height below its own minimum (320 px)
WIDGET_SIZE = (400, 320)


@pytest.fixture(scope="module")
def qt_app():
    "Create the offscreen application shared by the viewer tests."

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


@pytest.fixture
def dialog(qt_app):
    "Create a model validation window and close it afterwards."

    window = ModelValidationDialog()
    try:
        yield window
    finally:
        window.close()


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


class FakeMouse:
    "Minimal stand-in for the QMouseEvent Qt hands to a canvas."

    def __init__(self, x, y, button=None, held=False):
        self._position = QtCore.QPointF(float(x), float(y))
        self._button = (
            button if button is not None else QtCore.Qt.MouseButton.NoButton
        )
        self._buttons = (
            QtCore.Qt.MouseButton.LeftButton
            if held
            else QtCore.Qt.MouseButton.NoButton
        )
        self.accepted = 0
        self.ignored = 0

    def position(self):
        "Return the cursor position inside the widget."

        return self._position

    def button(self):
        "Return the button that changed state."

        return self._button

    def buttons(self):
        "Return the buttons held while the event happened."

        return self._buttons

    def accept(self):
        "Record that the canvas accepted the event."

        self.accepted += 1

    def ignore(self):
        "Record that the canvas refused the event."

        self.ignored += 1


def make_pixmap(width: int = 640, height: int = 480) -> QtGui.QPixmap:
    "Return a deterministic pixmap of the given size."

    pixmap = QtGui.QPixmap(width, height)
    pixmap.fill(QtGui.QColor(32, 64, 96))
    return pixmap


def make_canvas(width: int = 640, height: int = 480) -> ImageCanvas:
    "Return a canvas of WIDGET_SIZE showing an image of the given size."

    canvas = ImageCanvas("GT")
    canvas.resize(*WIDGET_SIZE)
    canvas.set_image(make_pixmap(width, height))
    return canvas


def render_canvas(canvas) -> np.ndarray:
    "Render a canvas offscreen and return its pixels as an RGB array."

    result = QtGui.QImage(canvas.size(), QtGui.QImage.Format.Format_RGB32)
    result.fill(QtGui.QColor(0, 0, 0))
    painter = QtGui.QPainter(result)
    canvas.render(
        painter,
        QtCore.QPoint(0, 0),
        QtGui.QRegion(canvas.rect()),
        QtWidgets.QWidget.RenderFlag.DrawWindowBackground,
    )
    painter.end()
    pointer = result.constBits()
    pointer.setsize(result.sizeInBytes())
    pixels = np.frombuffer(
        bytes(pointer), np.uint8, count=result.sizeInBytes()
    )
    pixels = pixels.reshape(result.height(), result.width(), 4)
    return np.ascontiguousarray(pixels[:, :, :3])


def drawn_region(reference, rendered):
    "Return (rect, pixel count) of what rendered adds over reference."

    mask = np.any(
        rendered.astype(np.int16) != reference.astype(np.int16), axis=2
    )
    if not mask.any():
        return None, 0
    rows, columns = np.nonzero(mask)
    rect = (
        int(columns.min()),
        int(rows.min()),
        int(columns.max()),
        int(rows.max()),
    )
    return rect, int(mask.sum())


def white_region(image):
    "Return the rect and the count of the pure white pixels."

    mask = np.all(image >= 255, axis=2)
    if not mask.any():
        return None, 0
    rows, columns = np.nonzero(mask)
    rect = (
        int(columns.min()),
        int(rows.min()),
        int(columns.max()),
        int(rows.max()),
    )
    return rect, int(mask.sum())


def score_box(start, end) -> dict:
    "Return one predicted rectangle with a label and a score."

    return {
        "label": "a0_dian",
        "shape_type": "rectangle",
        "points": [start, [end[0], start[1]], end, [start[0], end[1]]],
        "score": LABEL_SCORE,
    }


def drag(canvas, start, end) -> None:
    "Drag the picture with the left button from start to end."

    canvas.mousePressEvent(
        FakeMouse(start[0], start[1], button=QtCore.Qt.MouseButton.LeftButton)
    )
    canvas.mouseMoveEvent(FakeMouse(end[0], end[1], held=True))
    canvas.mouseReleaseEvent(
        FakeMouse(end[0], end[1], button=QtCore.Qt.MouseButton.LeftButton)
    )


def staging_layout(scratch: str, suffix: str) -> str:
    "Create the staging folder layout the tool expects."

    staging = osp.join(scratch, dataset.STAGING_PREFIX + suffix)
    for folder in (dataset.ORIGINAL_DIRNAME, dataset.AUGMENTED_DIRNAME):
        for sub in (dataset.IMAGES_DIRNAME, dataset.LABELS_DIRNAME):
            os.makedirs(osp.join(staging, folder, sub), exist_ok=True)
    return staging


def write_image(path: str, width: int = 640, height: int = 640) -> None:
    "Write a png holding one rectangle of gray value 128."

    os.makedirs(osp.dirname(path), exist_ok=True)
    image = np.full((height, width, 3), 128, dtype=np.uint8)
    ok, buffer = cv2.imencode(".png", image)
    assert ok
    buffer.tofile(path)


def label_payload(relpath: str, width: int, height: int) -> dict:
    "Return the xlabel payload of a staged pair."

    return {
        "version": "3.0.0",
        "flags": {},
        "checked": False,
        "shapes": [
            {
                "label": "a0_dian",
                "shape_type": "rectangle",
                "points": [[10, 10], [90, 10], [90, 90], [10, 90]],
                "group_id": None,
                "description": "",
                "flags": {},
            }
        ],
        "imagePath": osp.basename(relpath),
        "imageData": None,
        "imageHeight": height,
        "imageWidth": width,
    }


def staged_original(
    staging: str, relpath: str, width: int = 640, height: int = 640
):
    "Create one staged original record with its files."

    paths = dataset.staging_paths(
        staging, records_module.KIND_ORIGINAL, relpath
    )
    write_image(paths["image"], width, height)
    dataset.write_json(paths["label"], label_payload(relpath, width, height))
    return records_module.make_record(
        records_module.KIND_ORIGINAL, relpath, paths["image"], paths["label"]
    )


def show_record(dialog, staging: str, records) -> None:
    "Put the given records on the results page and select the first one."

    page = dialog.results_page
    dialog.records = list(records)
    page.set_context(list(CLASSES), staging)
    page.set_records(dialog.records)
    # the page opens on NG and this test looks at the whole list; the
    # switch only asks for the rebuild, so the events are driven once
    page.filter_combo.setCurrentIndex(0)
    QtWidgets.QApplication.processEvents()
    page.table.selectRow(0)
    page.gt_canvas.resize(*WIDGET_SIZE)
    page.pred_canvas.resize(*WIDGET_SIZE)


def same_state(left, right) -> bool:
    "Compare two view states within a pixel of tolerance."

    return left == pytest.approx(right, abs=1e-6)


# ------------------------------------------------------------- the canvas
def test_the_first_screen_shows_the_whole_image(qt_app):
    "A fresh picture is fitted and centred without any user input."

    canvas = make_canvas(*IMAGE_SIZE)
    assert canvas.zoom_factor() == pytest.approx(1.0)
    assert canvas.view_scale() == pytest.approx(canvas.fit_scale())
    rect = canvas._target_rect()
    assert rect.width() <= WIDGET_SIZE[0]
    assert rect.height() <= WIDGET_SIZE[1]
    assert rect.center().x() == pytest.approx(WIDGET_SIZE[0] / 2.0, abs=1e-6)
    assert rect.center().y() == pytest.approx(WIDGET_SIZE[1] / 2.0, abs=1e-6)
    # the whole image sits inside the widget
    top_left = canvas.widget_to_image(QtCore.QPointF(0.0, 0.0))
    bottom_right = canvas.widget_to_image(QtCore.QPointF(*WIDGET_SIZE))
    assert top_left.x() <= 0 and top_left.y() <= 0
    assert bottom_right.x() >= IMAGE_SIZE[0]
    assert bottom_right.y() >= IMAGE_SIZE[1]


def test_the_image_rect_is_drawn_in_image_pixels(qt_app):
    "The mapping is the plain pixel grid of the original image."

    canvas = make_canvas(*IMAGE_SIZE)
    transform, rect = canvas._transform()
    for point in ((0.0, 0.0), (10.0, 20.0), (640.0, 480.0)):
        mapped = transform.map(QtCore.QPointF(*point))
        expected = canvas.image_to_widget(QtCore.QPointF(*point))
        assert mapped.x() == pytest.approx(expected.x(), abs=1e-6)
        assert mapped.y() == pytest.approx(expected.y(), abs=1e-6)
    # origin at the top left corner: x grows right, y grows down
    origin = canvas.image_to_widget(QtCore.QPointF(0.0, 0.0))
    right = canvas.image_to_widget(QtCore.QPointF(100.0, 0.0))
    below = canvas.image_to_widget(QtCore.QPointF(0.0, 100.0))
    assert right.x() > origin.x() and right.y() == pytest.approx(origin.y())
    assert below.y() > origin.y() and below.x() == pytest.approx(origin.x())
    assert rect.topLeft().x() == pytest.approx(origin.x(), abs=1e-6)


def test_the_wheel_zooms_smoothly_around_the_mouse(qt_app):
    "One notch moves the target and small steps walk the zoom to it."

    canvas = make_canvas(*IMAGE_SIZE)
    anchor = QtCore.QPointF(120.0, 90.0)
    anchored_image = canvas.widget_to_image(anchor)
    event = FakeWheel(anchor.x(), anchor.y(), notches=1.0)

    canvas.wheelEvent(event)

    assert event.accepted == 1
    assert canvas.target_zoom() == pytest.approx(WHEEL_ZOOM_STEP)
    # the picture did not jump: the applied zoom is still the fit one
    assert canvas.zoom_factor() == pytest.approx(1.0)
    steps = []
    for _ in range(240):
        if not canvas.advance_zoom_animation():
            break
        steps.append(canvas.zoom_factor())
    assert steps, "the animation never moved the zoom"
    assert steps == sorted(steps)
    assert all(1.0 < value <= WHEEL_ZOOM_STEP + 1e-9 for value in steps)
    # the first tick is a fraction of the whole notch, not the notch
    assert (steps[0] - 1.0) < (WHEEL_ZOOM_STEP - 1.0) * 0.5
    assert canvas.zoom_factor() == pytest.approx(WHEEL_ZOOM_STEP)
    # the anchored image pixel stayed under the mouse
    after = canvas.widget_to_image(anchor)
    assert after.x() == pytest.approx(anchored_image.x(), abs=1e-6)
    assert after.y() == pytest.approx(anchored_image.y(), abs=1e-6)


def test_the_wheel_keeps_the_zoom_inside_its_range(qt_app):
    "Zooming never leaves the supported 0.1x - 10x range."

    canvas = make_canvas(*IMAGE_SIZE)
    middle = QtCore.QPointF(WIDGET_SIZE[0] / 2.0, WIDGET_SIZE[1] / 2.0)
    for _ in range(60):
        canvas.zoom_steps_at(middle, 1.0)
        canvas.finish_zoom()
    assert canvas.zoom_factor() == pytest.approx(MAX_ZOOM)
    top_left = canvas.widget_to_image(QtCore.QPointF(0.0, 0.0))
    assert top_left.x() > 0 or top_left.y() > 0

    for _ in range(120):
        canvas.zoom_steps_at(middle, -1.0)
        canvas.finish_zoom()
    assert canvas.zoom_factor() == pytest.approx(MIN_ZOOM)
    assert canvas.view_scale() == pytest.approx(MIN_ZOOM * canvas.fit_scale())


def test_the_left_drag_pans_the_picture(qt_app):
    "The left button moves the picture and never changes the zoom."

    canvas = make_canvas(*IMAGE_SIZE)
    scale = canvas.view_scale()
    center = canvas.view_center()
    under_press = canvas.widget_to_image(QtCore.QPointF(100.0, 100.0))

    drag(canvas, (100.0, 100.0), (140.0, 130.0))

    assert canvas.view_center().x() == pytest.approx(center.x() - 40.0 / scale)
    assert canvas.view_center().y() == pytest.approx(center.y() - 30.0 / scale)
    assert canvas.zoom_factor() == pytest.approx(1.0)
    # the picture follows the cursor: the pixel under the released
    # position is the one that was under the pressed position
    released = canvas.widget_to_image(QtCore.QPointF(140.0, 130.0))
    assert released.x() == pytest.approx(under_press.x(), abs=1e-6)
    assert released.y() == pytest.approx(under_press.y(), abs=1e-6)


def test_a_double_click_fits_the_image_again(qt_app):
    "The way back to the first screen is one double click."

    canvas = make_canvas(*IMAGE_SIZE)
    canvas.zoom_steps_at(QtCore.QPointF(50.0, 50.0), 3.0)
    canvas.finish_zoom()
    canvas.pan_by(QtCore.QPointF(60.0, 40.0))
    assert canvas.zoom_factor() > 1.0

    canvas.mouseDoubleClickEvent(
        FakeMouse(10.0, 10.0, button=QtCore.Qt.MouseButton.LeftButton)
    )

    assert canvas.zoom_factor() == pytest.approx(1.0)
    assert canvas.view_center().x() == pytest.approx(IMAGE_SIZE[0] / 2.0)
    assert canvas.view_center().y() == pytest.approx(IMAGE_SIZE[1] / 2.0)


def test_the_canvas_owns_no_edit_gesture(qt_app):
    "The canvas is a viewer: no edit state, no handle, no edit signal."

    canvas = make_canvas(*IMAGE_SIZE)
    # the canvas of the previous revision carried an edit state and a
    # selection; neither has a place in a viewer
    for removed in (
        "editable",
        "set_edit_mode",
        "set_editable_shapes",
        "editable_shapes",
        "editable_flags",
        "edit_record_id",
        "selected_index",
        "select_shape",
        "clear_selection",
        "apply_edit_points",
        "_draw_edit_overlay",
        "_begin_drag",
        "_update_drag",
        "_finish_drag",
        "_edit_at",
    ):
        assert not hasattr(canvas, removed), removed
    for signal in (
        "edit_mode_toggled",
        "shape_selected",
        "shape_move_finished",
        "shape_rename_requested",
    ):
        assert not hasattr(ImageCanvas, signal), signal
    # the module no longer carries the geometry of a box editor either
    for helper in (
        "EDIT_CURSOR",
        "EDIT_MODE_TITLE_SUFFIX",
        "HANDLE_COLOR",
        "HANDLE_CURSORS",
        "HANDLE_NAMES",
        "HIT_TOLERANCE",
        "MIN_BOX_SIZE",
        "SELECTED_PEN_WIDTH",
        "UNEDITABLE_COLOR",
        "box_handles",
        "contains_point",
        "handle_cursor",
        "hit_test",
        "nearest_point",
        "resize_points",
    ):
        assert not hasattr(image_view_module, helper), helper
    # the one signal the canvas still owns
    assert hasattr(ImageCanvas, "view_changed")


# -------------------------------------------------------------- the pair
def test_both_canvases_share_one_zoom_one_anchor_and_one_pan(dialog, tmp_path):
    "An action on either side shows the very same image region."

    staging = staging_layout(str(tmp_path), "view_sync")
    record = staged_original(staging, "a.png")
    show_record(dialog, staging, [record])
    page = dialog.results_page
    gt, pred = page.gt_canvas, page.pred_canvas

    assert same_state(gt.view_state(), pred.view_state())

    anchor = QtCore.QPointF(90.0, 70.0)
    anchored = gt.widget_to_image(anchor)
    event = FakeWheel(anchor.x(), anchor.y(), notches=1.0)
    gt.wheelEvent(event)
    gt.finish_zoom()

    assert same_state(gt.view_state(), pred.view_state())
    assert gt.zoom_factor() == pytest.approx(WHEEL_ZOOM_STEP)
    assert pred.zoom_factor() == pytest.approx(WHEEL_ZOOM_STEP)
    # the anchor is the same *image pixel*, at the same widget position
    for canvas in (gt, pred):
        mapped = canvas.widget_to_image(anchor)
        assert mapped.x() == pytest.approx(anchored.x(), abs=1e-6)
        assert mapped.y() == pytest.approx(anchored.y(), abs=1e-6)

    # a drag on the other picture moves both by the same image offset
    before = gt.view_center()
    drag(pred, (60.0, 60.0), (110.0, 95.0))
    assert same_state(gt.view_state(), pred.view_state())
    assert gt.view_center().x() != pytest.approx(before.x())
    drag(gt, (60.0, 60.0), (110.0, 95.0))
    assert same_state(gt.view_state(), pred.view_state())


def test_a_display_toggle_never_resets_the_view(dialog, tmp_path):
    "Showing or hiding an overlay repaints, it never refits."

    staging = staging_layout(str(tmp_path), "view_toggle")
    records = [
        staged_original(staging, "a.png"),
        staged_original(staging, "b.png", 480, 320),
    ]
    show_record(dialog, staging, records)
    page = dialog.results_page
    gt, pred = page.gt_canvas, page.pred_canvas

    gt.zoom_steps_at(QtCore.QPointF(70.0, 50.0), 2.0)
    gt.finish_zoom()
    state = gt.view_state()
    assert gt.zoom_factor() > 1.0

    page.overlay_check.setChecked(False)
    page.gt_check.setChecked(False)
    page.pred_check.setChecked(False)
    page.overlay_check.setChecked(True)

    assert same_state(gt.view_state(), state)
    assert same_state(pred.view_state(), state)

    # a new record does open on its own first screen again
    page.table.selectRow(1)
    assert gt.zoom_factor() == pytest.approx(1.0)
    assert same_state(gt.view_state(), pred.view_state())
    assert gt.fit_scale() == pytest.approx(pred.fit_scale())


def test_the_side_is_laid_out_ground_truth_then_prediction(dialog):
    "GT on the left, Pred on the right, nothing else on that side."

    page = dialog.results_page
    dialog.show()
    dialog.show_results()
    QtWidgets.QApplication.processEvents()
    gt_x = page.gt_canvas.mapTo(page, QtCore.QPoint(0, 0)).x()
    pred_x = page.pred_canvas.mapTo(page, QtCore.QPoint(0, 0)).x()
    assert gt_x < pred_x
    assert page.gt_canvas.width() > 0 and page.pred_canvas.width() > 0
    dialog.hide()


# ---------------------------------------------------------- box labels
# The canvas refuses a width below its own minimum, so the label tests
# pin the size they laid their expectations out for.
LABEL_WIDGET = (400, 320)
LABEL_IMAGE = (200, 260)
# The box every label test hangs its assertions on.
LABEL_START = (20, 30)
LABEL_END = (90, 80)
LABEL_SCORE = 0.8642
# The picture is pure black, so every pixel at or above this level is a
# glyph of a label and nothing else.
LABEL_THRESHOLD = 150


def make_label_canvas() -> ImageCanvas:
    "Return a fitted canvas showing a black picture."

    canvas = ImageCanvas("")
    canvas.resize(*LABEL_WIDGET)
    # rows first, columns second: a LABEL_IMAGE of (200, 260) is a 260
    # pixels wide and 200 pixels high picture
    image = np.zeros((LABEL_IMAGE[1], LABEL_IMAGE[0], 3), dtype=np.uint8)
    image = np.ascontiguousarray(image.transpose(1, 0, 2))
    ok, buffer = cv2.imencode(".png", image)
    assert ok
    pixmap = QtGui.QPixmap()
    assert pixmap.loadFromData(buffer.tobytes())
    canvas.set_image(pixmap)
    return canvas


def label_pixels(image, threshold: int = LABEL_THRESHOLD):
    "Return the rect and the count of the label pixels of a render."

    mask = np.all(image >= threshold, axis=2)
    if not mask.any():
        return None, 0
    rows, columns = np.nonzero(mask)
    rect = (
        int(columns.min()),
        int(rows.min()),
        int(columns.max()),
        int(rows.max()),
    )
    return rect, int(mask.sum())


def rect_box(start, end, label: str = "a0_dian", score=None) -> dict:
    "Return one rectangle of the picture coordinate system."

    box = {
        "label": label,
        "shape_type": "rectangle",
        "points": [start, [end[0], start[1]], end, [start[0], end[1]]],
    }
    if score is not None:
        box["score"] = score
    return box


def test_a_prediction_carries_its_score_and_a_ground_truth_does_not(
    qt_app,
):
    "label score for a prediction, the label alone for a GT shape."

    predicted = score_box(list(LABEL_START), list(LABEL_END))
    assert shape_score(predicted) == pytest.approx(LABEL_SCORE)
    assert shape_label_text(predicted) == "a0_dian 0.86"
    assert shape_label_text(predicted) == "a0_dian " + SCORE_FORMAT.format(
        LABEL_SCORE
    )
    ground_truth = dict(predicted)
    ground_truth.pop("score")
    assert shape_score(ground_truth) is None
    assert shape_label_text(ground_truth) == "a0_dian"
    # a broken or missing score never becomes a label of its own
    for broken in (None, "", "abc", float("nan"), float("inf"), True):
        assert shape_score({"label": "x", "score": broken}) is None
    assert shape_label_text({"label": "", "score": 0.5}) == "0.50"
    assert shape_label_text({"label": "", "score": None}) == ""


def test_the_prediction_label_is_painted_at_the_corner_of_its_box(qt_app):
    "The text sits at the upper left corner of its own box."

    canvas = make_label_canvas()
    plain = render_canvas(canvas)
    corner = canvas.image_to_widget(QtCore.QPointF(*LABEL_START))
    canvas.set_shapes([], [score_box(list(LABEL_START), list(LABEL_END))])
    rendered = render_canvas(canvas)

    rect, count = label_pixels(rendered)
    assert rect is not None and count > 40
    # nothing at all is painted without a box
    assert label_pixels(plain) == (None, 0)
    # the text sits inside a plate that hangs just above the corner of
    # the box and starts at its left edge
    assert abs(drawn_region(plain, rendered)[0][0] - corner.x()) <= 3
    plate = drawn_region(
        plain[: int(corner.y())], rendered[: int(corner.y())]
    )[0]
    assert plate is not None
    assert abs(plate[0] - corner.x()) <= 3
    assert 0 <= corner.y() - plate[3] <= 4
    assert rect[3] < plate[3]
    # the text and its score: as wide as a label needs to be
    assert rect[2] - rect[0] > 100
    # and it fits inside the widget instead of being clipped
    assert rect[2] < LABEL_WIDGET[0] and rect[3] < LABEL_WIDGET[1]


def test_the_ground_truth_label_has_no_score(qt_app):
    "The GT canvas writes the label of the box, never a score."

    canvas = make_label_canvas()
    predicted = score_box(list(LABEL_START), list(LABEL_END))
    ground_truth = dict(predicted)
    ground_truth.pop("score")
    canvas.set_shapes([], [predicted])
    prediction_only = label_pixels(render_canvas(canvas))
    canvas.set_shapes([ground_truth], [])
    ground_truth_only = label_pixels(render_canvas(canvas))
    canvas.set_shapes([ground_truth], [predicted])
    both = label_pixels(render_canvas(canvas))

    assert ground_truth_only[0] is not None
    assert prediction_only[0] is not None and both[0] is not None
    # the GT text is the shorter one, so it is the narrower label
    assert ground_truth_only[0][2] < prediction_only[0][2]
    # and the shorter text is painted with fewer pixels
    assert ground_truth_only[1] < prediction_only[1]
    # with both overlays the predicted label wins the corner
    assert both[0][2] == prediction_only[0][2]


def test_a_label_never_shrinks_or_grows_with_the_zoom(qt_app):
    "The text is painted in widget space, so its size stays on screen."

    canvas = make_label_canvas()
    predicted = score_box([100, 100], [150, 140])
    canvas.set_shapes([], [predicted])
    fitted = label_pixels(render_canvas(canvas))[0]
    for _ in range(2):
        canvas.zoom_steps_at(
            QtCore.QPointF(LABEL_WIDGET[0] / 2.0, LABEL_WIDGET[1] / 2.0),
            1.0,
        )
        canvas.finish_zoom()
    assert canvas.zoom_factor() > 1.2
    # the corner stayed inside the widget, so the label is drawn there
    corner = canvas.image_to_widget(QtCore.QPointF(100.0, 100.0))
    assert corner.x() > 60 and corner.y() > 60
    zoomed = label_pixels(render_canvas(canvas))[0]
    assert zoomed is not None and fitted is not None
    # the glyph band keeps the very same height at both zooms: the text
    # did not grow with the picture (antialiasing moves a pixel at most)
    assert abs((zoomed[3] - zoomed[1]) - (fitted[3] - fitted[1])) <= 6
    assert zoomed[3] - zoomed[1] < 20


def test_a_label_of_an_offscreen_box_is_kept_inside_the_view(qt_app):
    "A box the pan pushed out keeps a readable, fully visible label."

    canvas = make_label_canvas()
    # the box starts outside the picture, so its upper left corner - the
    # anchor of its label - sits outside the widget as well
    predicted = score_box([-120, -60], [40, 40])
    canvas.set_shapes([], [predicted])
    corner = canvas.image_to_widget(QtCore.QPointF(-120.0, -60.0))
    assert corner.x() < 0 and corner.y() < 0

    rect, count = label_pixels(render_canvas(canvas))
    assert rect is not None and count > 40
    assert rect[0] >= 0 and rect[1] >= 0
    assert rect[2] < LABEL_WIDGET[0] and rect[3] < LABEL_WIDGET[1]
    # the label is clamped to the border of the widget, never dropped:
    # it starts at the very left of the view and is still the full text
    # (its outlined band starts one LABEL_PADDING below the top border)
    assert rect[0] <= 12 and rect[1] <= int(LABEL_PADDING) + 2
    assert rect[2] - rect[0] > 100


def test_a_long_label_stays_inside_the_widget(qt_app):
    "A long label is bounded by the widget, never painted outside it."

    canvas = make_label_canvas()
    short = rect_box([10, 10], [250, 190], label="a" * 10, score=LABEL_SCORE)
    canvas.set_shapes([], [short])
    fitted = label_pixels(render_canvas(canvas))
    assert fitted[0] is not None and fitted[1] > 40
    assert fitted[0][2] < LABEL_WIDGET[0]

    # a label wider than the widget would be cut in half: it is refused
    # instead, so nothing is ever painted outside the view
    canvas.set_shapes(
        [],
        [rect_box([10, 10], [250, 190], label="a" * 40, score=LABEL_SCORE)],
    )
    assert label_pixels(render_canvas(canvas)) == (None, 0)


# ------------------------------------------------------ label blocks
# A box the matching merged carries one label row per class: the rows are
# stacked into the one text of the box and placed as a single block.
MULTI_LABEL = "a0_dian"
MULTI_OTHER = "a2_henxian_super_long"


def multi_label_box(start, end, rows) -> dict:
    "Return a rectangle carrying one label row per (label, score) pair."

    box = rect_box(start, end, label=rows[0][0], score=rows[0][1])
    box["labels"] = [label for label, _score in rows]
    box["scores"] = [score for _label, score in rows]
    return box


def two_line_rows():
    "Return the rows of the two line box under test."

    return [(MULTI_LABEL, LABEL_SCORE), (MULTI_OTHER, 0.4321)]


def test_the_label_rows_of_a_multi_class_box_are_one_row_per_class(qt_app):
    "One merged box carries its classes on parallel lists, in given order."

    rows = two_line_rows()
    box = multi_label_box(list(LABEL_START), list(LABEL_END), rows)
    assert shape_label_rows(box) == rows
    assert shape_label_text(box) == (
        MULTI_LABEL + " " + SCORE_FORMAT.format(LABEL_SCORE)
        + "\n"
        + MULTI_OTHER + " " + SCORE_FORMAT.format(0.4321)
    )
    # one row is not a block: a single class box keeps its one line
    single = rect_box(list(LABEL_START), list(LABEL_END), score=LABEL_SCORE)
    single["labels"] = [MULTI_LABEL]
    single["scores"] = [LABEL_SCORE]
    assert shape_label_rows(single) == [(MULTI_LABEL, LABEL_SCORE)]
    assert shape_label_text(single) == shape_label_text(
        score_box(list(LABEL_START), list(LABEL_END))
    )
    assert "\n" not in shape_label_text(single)


def test_a_single_class_box_is_the_row_it_always_was(qt_app):
    "A shape without the two keys falls back to its own label and score."

    predicted = score_box(list(LABEL_START), list(LABEL_END))
    assert shape_label_rows(predicted) == [(MULTI_LABEL, LABEL_SCORE)]
    ground_truth = dict(predicted)
    ground_truth.pop("score")
    assert shape_label_rows(ground_truth) == [("a0_dian", None)]
    assert shape_label_text(ground_truth) == "a0_dian"
    # the two lists are only read while they are parallel and longer than
    # one row: a broken pair keeps the plain label of the shape
    for labels, scores in (
        (["a", "b"], [0.5]),
        (["a", "b"], 0.5),
        ("ab", "ab"),
        ([], []),
    ):
        broken = dict(predicted)
        broken["labels"] = labels
        broken["scores"] = scores
        assert shape_label_rows(broken) == [(MULTI_LABEL, LABEL_SCORE)]
    # a row without a score is written without one, exactly like the row
    # of a ground truth box
    half = dict(predicted)
    half["labels"] = ["a", "b"]
    half["scores"] = [None, 0.5]
    assert shape_label_text(half) == "a\nb 0.50"


def test_the_rows_of_a_multi_line_block_are_stacked_with_one_step(qt_app):
    "The block is one font line height taller per row of the label."

    font = ImageCanvas._label_font()
    rows = two_line_rows()
    texts = [
        label + " " + SCORE_FORMAT.format(score) for label, score in rows
    ]
    boxes = [
        ImageCanvas._label_glyphs(font, text).boundingRect()
        for text in texts
    ]
    # the rows are stacked from baseline to baseline, one line height of
    # the label font per row: the block is the union of the first ink box
    # and of every later one moved down by that step, which is what the
    # stacking computes
    step = float(QtGui.QFontMetricsF(font).height()) * LABEL_LINE_SPACING
    assert step > 0
    tops = [float(boxes[0].top())]
    bottoms = [float(boxes[0].bottom())]
    for index, box in enumerate(boxes[1:], start=1):
        shift = float(index) * step
        tops.append(float(box.top()) + shift)
        bottoms.append(float(box.bottom()) + shift)
    block = ImageCanvas._stacked_glyphs(
        font, shape_label_text(multi_label_box([0, 0], [1, 1], rows))
    ).boundingRect()
    assert block.top() == pytest.approx(min(tops), abs=1e-6)
    assert block.bottom() == pytest.approx(max(bottoms), abs=1e-6)
    # the step really spreads the rows instead of stacking them on top
    # of each other: the block is taller than the taller of its rows
    heights = [box.height() for box in boxes]
    assert block.height() > max(heights)
    # the bound carries no font metric assumption beyond the one step:
    # the block is at least one step taller than the shorter row and at
    # most one step taller than the taller one - the two ends meet on the
    # one number whenever the rows measure the same ink height, which is
    # what this platform does. The exact geometry of the step is already
    # pinned, above, by the top and the bottom of the block: a row that
    # was NOT moved by the step would be caught there, not by this height.
    assert block.height() >= min(heights) + step - 1e-6
    assert block.height() <= max(heights) + step + 1e-6
    # Readability guard, the one the one pixel step of the previous
    # revision fails: the ink of two neighbours must not overlap, and the
    # dark outline of the upper row must not reach into the glyphs of the
    # row under it. The outline is centred on the border of the ink, so
    # it grows that ink by half of its width, and half an outline width
    # is therefore the clearance to keep - asking for a whole outline
    # width would ask the line step to swallow a whole descent as well,
    # which the line height of a font never carries. Both rows under test
    # do descend: the underscore of a0_dian sits 2.8px under the
    # baseline, and one line height still leaves 2.9px of clearance.
    for index in range(len(boxes) - 1):
        upper_bottom = float(boxes[index].bottom()) + index * step
        lower_top = float(boxes[index + 1].top()) + (index + 1) * step
        assert upper_bottom < lower_top
        assert upper_bottom + LABEL_OUTLINE_WIDTH / 2.0 < lower_top
    # the rows are stacked in the given order: the wider row sits under
    # the narrower one, so the block is wider than its first row alone
    assert boxes[0].width() < block.width()
    assert block.width() == pytest.approx(boxes[1].width(), abs=1e-6)


def test_a_single_row_label_is_the_very_path_it_always_was(qt_app):
    "One row skips the stacking: its path is the plain glyph path."

    font = ImageCanvas._label_font()
    text = MULTI_LABEL + " " + SCORE_FORMAT.format(LABEL_SCORE)
    glyphs = ImageCanvas._label_glyphs(font, text)
    stacked = ImageCanvas._stacked_glyphs(font, text)
    # the two paths carry the very same elements, in the same order, so
    # the rendering pixels of a single row label cannot move with the
    # line step: the one row of a label is never routed through the
    # stacking at all (regression guard for the changed spacing)
    assert stacked.boundingRect() == glyphs.boundingRect()
    assert stacked.elementCount() == glyphs.elementCount()
    for index in range(glyphs.elementCount()):
        one = glyphs.elementAt(index)
        other = stacked.elementAt(index)
        assert (other.x, other.y, other.type) == (one.x, one.y, one.type)


def test_the_block_of_a_multi_line_label_hangs_at_the_box_corner(qt_app):
    "Pixel evidence: the block sits above the corner as a whole."

    canvas = make_label_canvas()
    rows = two_line_rows()
    corner = canvas.image_to_widget(QtCore.QPointF(*LABEL_START))
    single = rect_box(list(LABEL_START), list(LABEL_END), score=LABEL_SCORE)
    canvas.set_shapes([], [single])
    one_line = label_pixels(render_canvas(canvas))
    canvas.set_shapes(
        [], [multi_label_box(list(LABEL_START), list(LABEL_END), rows)]
    )
    rendered = render_canvas(canvas)
    block = label_pixels(rendered)

    assert one_line[0] is not None and block[0] is not None
    # the block starts at the very left of its box and its last row ends
    # at the very spot the one line of the plain box ends: the rows are
    # stacked upwards, away from the anchor, and never drift off it
    assert abs(block[0][0] - corner.x()) <= 3
    assert abs(block[0][3] - one_line[0][3]) <= 2
    assert block[0][3] < corner.y()
    # the block is taller than the one line of the very same box: the
    # second class really is a second row
    assert (block[0][3] - block[0][1]) > (one_line[0][3] - one_line[0][1])
    # the block is as wide as its wider row - the long second class - so
    # both rows were shaped and both were left aligned on the anchor
    width = max(
        ImageCanvas._label_glyphs(
            ImageCanvas._label_font(), label + " " + SCORE_FORMAT.format(score)
        ).boundingRect().width()
        for label, score in rows
    )
    assert abs((block[0][2] - block[0][0]) - width) <= 4
    assert abs(block[0][0] - corner.x()) <= 3


def test_a_block_that_does_not_fit_the_widget_is_dropped_whole(qt_app):
    "A block wider or taller than the view is refused, never cut in half."

    canvas = make_label_canvas()
    # a label wider than the widget would be cut in half: it is refused
    wide = multi_label_box(
        [10, 10],
        [250, 190],
        [(MULTI_OTHER, 0.9), (MULTI_OTHER * 4, 0.8)],
    )
    canvas.set_shapes([], [wide])
    assert label_pixels(render_canvas(canvas)) == (None, 0)

    # and so is a block taller than the widget, whatever its rows hold:
    # every row sits one font line height below the one before it, so a
    # block of hundreds of rows is far taller than the 320 pixel view
    many = multi_label_box(
        [10, 10],
        [250, 190],
        [(MULTI_OTHER, 0.9)] * 400,
    )
    canvas.set_shapes([], [many])
    assert label_pixels(render_canvas(canvas)) == (None, 0)

    # the very same rows fit the view while there are only a few of them,
    # which is what proves the block and not a row was measured
    few = multi_label_box([10, 10], [250, 190], [(MULTI_LABEL, 0.9)] * 3)
    canvas.set_shapes([], [few])
    rect, count = label_pixels(render_canvas(canvas))
    assert rect is not None and count > 40
    assert rect[2] < LABEL_WIDGET[0] and rect[3] < LABEL_WIDGET[1]


def test_the_viewer_owns_no_edit_control(dialog):
    "The right hand side of this revision only looks at the data."

    page = dialog.results_page
    assert page.findChildren(QtWidgets.QComboBox) == [page.filter_combo]
    labels = [
        button.text() for button in page.findChildren(QtWidgets.QPushButton)
    ]
    assert "应用" not in labels
    assert "Apply" not in labels
    for removed in (
        "shape_combo",
        "label_combo",
        "type_combo",
        "apply_button",
    ):
        assert not hasattr(page, removed), removed
    # the two mark toggles of the record layer stay declared, while the
    # edit hook of the previous revision - the one the removed gesture
    # emitted - is gone
    assert hasattr(page, "toggle_deleted")
    assert hasattr(page, "toggle_export")
    assert not hasattr(page, "edit_requested")
    # and the two canvases are the only image widgets
    assert len(page.findChildren(ImageCanvas)) == 2
