"""The right button previews the original of an augmented record.

An augmented copy is judged on its own augmented picture, but the
question the user asks while looking at it is what the *original* did:
holding the right button on either canvas replaces the copy by its
parent - the parent picture, the parent ground truth, the parent
predictions and the parent judgement - and releasing the button brings
the copy back. Every assertion below drives the real Qt event path of a
press and of a release, and the colours are read from the rendered pixels
of the real canvases: the box the parent never matched is the miss
colour while it is the plain matched colour on the copy.
"""

import os
import os.path as osp

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import cv2
import numpy as np
import pytest

from PyQt6 import QtCore, QtGui, QtWidgets

from anylabeling.custom.model_validation import dataset
from anylabeling.custom.model_validation import judge as judge_module
from anylabeling.custom.model_validation import records as records_module
from anylabeling.custom.model_validation.ui.image_view import (
    GT_COLOR,
    MISS_COLOR,
    PRED_COLOR,
)
from anylabeling.custom.model_validation.ui.results_page import (
    GT_CANVAS_TITLE,
    PRED_CANVAS_TITLE,
    PREVIEW_HINT_MISSING,
    PREVIEW_HINT_ORIGINAL,
    PREVIEW_NOTE_NOT_JUDGED,
    PREVIEW_TITLE_PREFIX,
    PREVIEW_TOOLTIP,
    ResultsPage,
)

CLASSES = ["a0_dian"]
# the parent picture and the smaller, augmented copy of the fixture
PARENT_SIZE = (64, 48)
CHILD_SIZE = (32, 24)
# the one box both records describe: the parent never matches it (a miss,
# orange red) while the copy matches it with a prediction of its own (a
# matched pair, therefore the plain colours of the two canvases)
BOX = ((4, 4), (20, 20))
# the two strokes of a corner meet inside this many pixels of the edge
PROBE_TOLERANCE = 2


@pytest.fixture(scope="module")
def qt_app():
    "Create the offscreen application the preview tests need."

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def rect(start, end, label="a0_dian") -> dict:
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
    }


def judgment(ground_truth, predictions):
    "Return the real judgement of one pair of shape lists."

    result = judge_module.judge_record(
        ground_truth, predictions, list(CLASSES), ng_iou_threshold=0.5
    )
    detail = dict(result.detail)
    # the payload the pipeline stores next to the judgement
    detail["predictions"] = list(predictions)
    return result, detail


def staging_layout(scratch: str, suffix: str) -> str:
    "Create the staging folder layout the tool expects."

    staging = osp.join(scratch, dataset.STAGING_PREFIX + suffix)
    for folder in (dataset.ORIGINAL_DIRNAME, dataset.AUGMENTED_DIRNAME):
        for sub in (dataset.IMAGES_DIRNAME, dataset.LABELS_DIRNAME):
            os.makedirs(osp.join(staging, folder, sub), exist_ok=True)
    return staging


def write_image(path: str, size, value: int) -> None:
    "Write a tiny flat png of a given size and grey value."

    os.makedirs(osp.dirname(path), exist_ok=True)
    image = np.full((size[1], size[0], 3), int(value), dtype=np.uint8)
    ok, buffer = cv2.imencode(".png", image)
    assert ok
    buffer.tofile(path)


def label_payload(relpath: str, shapes, size) -> dict:
    "Return the xlabel payload of a staged pair."

    return {
        "version": "3.0.0",
        "flags": {},
        "checked": False,
        "shapes": shapes,
        "imagePath": osp.basename(relpath),
        "imageData": None,
        "imageHeight": size[1],
        "imageWidth": size[0],
    }


def staged_original(staging: str):
    "Create the staged original of the fixture: one GT, no prediction."

    paths = dataset.staging_paths(
        staging, records_module.KIND_ORIGINAL, "a.png"
    )
    ground_truth = [rect(*BOX)]
    write_image(paths["image"], PARENT_SIZE, 30)
    dataset.write_json(
        paths["label"], label_payload("a.png", ground_truth, PARENT_SIZE)
    )
    record = records_module.make_record(
        records_module.KIND_ORIGINAL, "a.png", paths["image"], paths["label"]
    )
    set_judgment(record, ground_truth, [])
    return record


def staged_child(staging: str, relpath: str, parent_record_id: str):
    "Create one staged augmented copy: the very box, matched by itself."

    paths = dataset.staging_paths(
        staging, records_module.KIND_AUGMENTED, relpath
    )
    ground_truth = [rect(*BOX)]
    predictions = [rect(*BOX)]
    write_image(paths["image"], CHILD_SIZE, 200)
    dataset.write_json(
        paths["label"],
        label_payload(relpath, ground_truth, CHILD_SIZE),
    )
    record = records_module.make_record(
        records_module.KIND_AUGMENTED,
        relpath,
        paths["image"],
        paths["label"],
        parent_record_id=parent_record_id,
    )
    set_judgment(record, ground_truth, predictions)
    return record


def set_judgment(record, ground_truth, predictions) -> None:
    "Attach the judgement of one pair of shape lists to a record."

    result, detail = judgment(ground_truth, predictions)
    record.detail = detail
    record.verdict = result.verdict
    record.reasons = list(result.reasons)
    record.judged = True


def row_of(page, record_id: str) -> int:
    "Return the shown row of one record."

    for row in range(page.table.rowCount()):
        record = page.record_at(row)
        if record is not None and record.record_id == record_id:
            return row
    raise AssertionError("record not shown: " + record_id)


def show_page(staging: str, records, current_id: str = ""):
    "Return a shown page holding the given records."

    page = ResultsPage()
    page.resize(1024, 640)
    page.set_context(list(CLASSES), staging)
    page.set_records(records)
    # the page opens on NG: this file selects the record it looks at
    page.filter_combo.setCurrentIndex(0)
    page.show()
    QtWidgets.QApplication.processEvents()
    if current_id:
        page.table.selectRow(row_of(page, current_id))
        QtWidgets.QApplication.processEvents()
    return page


@pytest.fixture
def page_pair(qt_app, tmp_path):
    "A shown page with one NG original and its augmented copy."

    staging = staging_layout(str(tmp_path), "preview_pair")
    parent = staged_original(staging)
    child = staged_child(staging, "a_aug1.png", parent.record_id)
    page = show_page(staging, [parent, child], child.record_id)
    try:
        yield page, parent, child
    finally:
        page.close()


def send_mouse(canvas, event_type, button) -> None:
    "Send one real mouse event of Qt to a canvas."

    pressed = event_type == QtCore.QEvent.Type.MouseButtonPress
    event = QtGui.QMouseEvent(
        event_type,
        QtCore.QPointF(10.0, 10.0),
        button,
        button if pressed else QtCore.Qt.MouseButton.NoButton,
        QtCore.Qt.KeyboardModifier.NoModifier,
    )
    QtWidgets.QApplication.sendEvent(canvas, event)
    QtWidgets.QApplication.processEvents()


def press_right(canvas) -> None:
    "Press the right button on one canvas."

    send_mouse(
        canvas,
        QtCore.QEvent.Type.MouseButtonPress,
        QtCore.Qt.MouseButton.RightButton,
    )


def release_right(canvas) -> None:
    "Release the right button on one canvas."

    send_mouse(
        canvas,
        QtCore.QEvent.Type.MouseButtonRelease,
        QtCore.Qt.MouseButton.RightButton,
    )


def render_rgba(canvas) -> np.ndarray:
    "Render a canvas offscreen and return its pixels as an RGBA array."

    result = QtGui.QImage(canvas.size(), QtGui.QImage.Format.Format_RGBA8888)
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
    return np.ascontiguousarray(pixels)


def canvas_stroke(canvas, color, box=BOX) -> int:
    "Return the count of the stroke pixels of one box of a canvas."

    rendered = render_rgba(canvas)
    rgb = np.array(
        [color.red(), color.green(), color.blue(), color.alpha()], np.int16
    )
    painted = np.all(np.abs(rendered.astype(np.int16) - rgb) == 0, axis=2)
    near = np.zeros(painted.shape, bool)
    for point in (
        (box[0][0], box[0][1]),
        (box[1][0], box[0][1]),
        (box[1][0], box[1][1]),
        (box[0][0], box[1][1]),
    ):
        corner = canvas.image_to_widget(
            QtCore.QPointF(float(point[0]), float(point[1]))
        )
        column = int(round(corner.x()))
        row = int(round(corner.y()))
        near[
            max(row - PROBE_TOLERANCE, 0) : row + PROBE_TOLERANCE + 1,
            max(column - PROBE_TOLERANCE, 0) : column + PROBE_TOLERANCE + 1,
        ] = True
    return int(np.count_nonzero(painted & near))


def test_the_fixture_parent_is_ng_and_the_copy_is_ok(page_pair):
    "The two records of the fixture carry the judgements under test."

    _page, parent, child = page_pair

    assert parent.verdict == records_module.NG
    assert child.verdict == records_module.OK
    assert child.parent_record_id == parent.record_id
    # the parent missed its only box, the copy matched it
    assert parent.detail["predictions"] == []
    assert len(child.detail["predictions"]) == 1


def test_the_right_button_shows_the_original_result(page_pair):
    "Holding the right button replaces the copy by its parent result."

    page, parent, child = page_pair
    canvas = page.gt_canvas
    assert page.displayed_record() is child
    assert canvas.image_size().width() == CHILD_SIZE[0]
    # the copy matched its box: the plain GT colour is on the canvas, on
    # the picture of the copy
    assert canvas_stroke(canvas, GT_COLOR) > 0
    assert canvas_stroke(canvas, MISS_COLOR) == 0
    assert canvas_stroke(page.pred_canvas, PRED_COLOR) > 0

    press_right(canvas)

    assert page.preview_active() is True
    assert page.displayed_record() is parent
    # both canvases show the parent picture and say so
    for side in (page.gt_canvas, page.pred_canvas):
        assert side.image_size().width() == PARENT_SIZE[0]
        assert side.image_size().height() == PARENT_SIZE[1]
        assert side.title.startswith(PREVIEW_TITLE_PREFIX)
    assert page.gt_canvas.title == (
        PREVIEW_TITLE_PREFIX + " · " + GT_CANVAS_TITLE
    )
    assert page.pred_canvas.title == (
        PREVIEW_TITLE_PREFIX + " · " + PRED_CANVAS_TITLE
    )
    # the status line names the parent, its verdict and the way back
    note = page.preview_note.text()
    assert page.preview_note.isVisibleTo(page) is True
    assert parent.relpath in note
    assert "状态 NG" in note
    assert "松开右键" in note
    # the parent judgement colours the boxes: the box the parent never
    # matched is the miss colour and the green of the copy is gone
    assert canvas_stroke(canvas, MISS_COLOR) > 0
    assert canvas_stroke(canvas, GT_COLOR) == 0
    # the parent had no prediction at all, so the Pred canvas is empty
    # where the copy carried its matched prediction
    assert canvas_stroke(page.pred_canvas, PRED_COLOR) == 0
    # one zoom, one anchor and one pan for the two pictures, still
    assert page.gt_canvas.view_state() == pytest.approx(
        page.pred_canvas.view_state(), abs=1e-9
    )


def test_the_release_brings_the_augmented_copy_back(page_pair):
    "Releasing the right button restores the record of the current row."

    page, parent, child = page_pair
    press_right(page.pred_canvas)
    assert page.preview_active() is True
    assert page.displayed_record() is parent

    release_right(page.pred_canvas)

    assert page.preview_active() is False
    assert page.displayed_record() is child
    for side in (page.gt_canvas, page.pred_canvas):
        assert side.image_size().width() == CHILD_SIZE[0]
        assert side.image_size().height() == CHILD_SIZE[1]
    assert page.gt_canvas.title == GT_CANVAS_TITLE
    assert page.pred_canvas.title == PRED_CANVAS_TITLE
    assert page.preview_note.text() == ""
    assert page.preview_note.isHidden() is True
    assert canvas_stroke(page.gt_canvas, GT_COLOR) > 0
    assert canvas_stroke(page.gt_canvas, MISS_COLOR) == 0
    assert canvas_stroke(page.pred_canvas, PRED_COLOR) > 0


def test_a_left_press_and_the_next_row_leave_the_preview(page_pair):
    "A left press and a move to another record both end the preview."

    page, parent, child = page_pair
    press_right(page.gt_canvas)
    assert page.preview_active() is True

    # a left press is the second way out: it ends the preview and starts
    # the pan of the picture on screen
    send_mouse(
        page.gt_canvas,
        QtCore.QEvent.Type.MouseButtonPress,
        QtCore.Qt.MouseButton.LeftButton,
    )
    send_mouse(
        page.gt_canvas,
        QtCore.QEvent.Type.MouseButtonRelease,
        QtCore.Qt.MouseButton.LeftButton,
    )
    assert page.preview_active() is False
    assert page.displayed_record() is child

    # and the preview never survives the move to the next record
    press_right(page.gt_canvas)
    assert page.preview_active() is True
    page.table.selectRow(row_of(page, parent.record_id))
    QtWidgets.QApplication.processEvents()
    assert page.preview_active() is False
    assert page.current_record() is parent
    assert page.gt_canvas.title == GT_CANVAS_TITLE
    assert page.gt_canvas.image_size().width() == PARENT_SIZE[0]


def test_a_plain_original_never_enters_the_preview(page_pair):
    "The right button of a record without a parent answers with one line."

    page, parent, _child = page_pair
    page.table.selectRow(row_of(page, parent.record_id))
    QtWidgets.QApplication.processEvents()

    press_right(page.gt_canvas)

    assert page.preview_active() is False
    assert page.displayed_record() is parent
    assert page.gt_canvas.image_size().width() == PARENT_SIZE[0]
    assert page.preview_note.text() == PREVIEW_HINT_ORIGINAL
    assert page.gt_canvas.title == GT_CANVAS_TITLE
    # the line belongs to the press and goes away with the release
    release_right(page.gt_canvas)
    assert page.preview_note.text() == ""
    assert page.preview_note.isHidden() is True


def test_an_augmented_record_without_its_parent_gets_one_status_line(
    qt_app, tmp_path
):
    "A copy whose original is not in the list is answered, never crashed."

    staging = staging_layout(str(tmp_path), "preview_orphan")
    orphan = staged_child(staging, "a_aug1.png", "original::missing.png")
    page = show_page(staging, [orphan], orphan.record_id)
    try:
        press_right(page.gt_canvas)

        assert page.preview_active() is False
        assert page.displayed_record() is orphan
        assert page.gt_canvas.image_size().width() == CHILD_SIZE[0]
        assert page.preview_note.text() == PREVIEW_HINT_MISSING
    finally:
        page.close()


def test_a_parent_without_a_judgement_is_previewed_with_a_note(
    qt_app, tmp_path
):
    "An unjudged original keeps its plain colours and says so in the line."

    staging = staging_layout(str(tmp_path), "preview_unjudged")
    parent = staged_original(staging)
    parent.detail = {}
    parent.verdict = records_module.PENDING
    parent.reasons = []
    parent.judged = False
    child = staged_child(staging, "a_aug1.png", parent.record_id)
    page = show_page(staging, [parent, child], child.record_id)
    try:
        press_right(page.gt_canvas)

        assert page.preview_active() is True
        assert page.displayed_record() is parent
        assert page.gt_canvas.image_size().width() == PARENT_SIZE[0]
        assert "状态 PENDING" in page.preview_note.text()
        assert PREVIEW_NOTE_NOT_JUDGED in page.preview_note.text()
        # a record without a judgement colours no box
        assert canvas_stroke(page.gt_canvas, GT_COLOR) > 0
        assert canvas_stroke(page.gt_canvas, MISS_COLOR) == 0
        assert canvas_stroke(page.gt_canvas, PRED_COLOR) == 0
    finally:
        page.close()


def test_the_canvases_own_no_menu_and_document_the_gesture(page_pair):
    "No context menu fights the press, and the two tooltips name it."

    page, _parent, _child = page_pair

    for canvas in (page.gt_canvas, page.pred_canvas):
        assert canvas.contextMenuPolicy() == (
            QtCore.Qt.ContextMenuPolicy.NoContextMenu
        )
        assert canvas.toolTip() == PREVIEW_TOOLTIP
