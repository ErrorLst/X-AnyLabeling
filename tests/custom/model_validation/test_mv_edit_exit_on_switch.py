"""Switching the record leaves the ROI edit mode.

The mode is armed on the record the user sees. Selecting another row -
by the list, by the A / D shortcuts or by a filter that drops the row -
is the way out of it: the gestures of the mode would otherwise travel
to the next picture without a word. A repaint of the *same* record - a
finished drag, a renamed label, a display switch, a filter that keeps
the row - keeps the mode, and the tests below pin both directions,
because a page that leaves the mode on every selection signal would
throw the user out of the mode after every finished drag.

The mode the page leaves behind is the whole state of the mode: the
flag, the editable set of the left canvas, the title suffix, the hint
line and the selection of the shape the user was working on.
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
from anylabeling.custom.model_validation.ui.results_page import (
    EDIT_TITLE_SUFFIX,
    GT_CANVAS_TITLE,
    ResultsPage,
)

CLASSES = ["car", "bus"]
IMAGE_SIZE = (64, 48)
WIDGET_SIZE = (1024, 640)
BOX = ((8, 8), (48, 36))


@pytest.fixture(scope="module")
def qt_app():
    "Create the offscreen application these tests need."

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


def staged_original(staging: str, relpath: str, shapes) -> object:
    "Create one staged original record with its files."

    paths = dataset.staging_paths(
        staging, records_module.KIND_ORIGINAL, relpath
    )
    write_image(paths["image"])
    dataset.write_json(paths["label"], label_payload(relpath, shapes))
    return records_module.make_record(
        records_module.KIND_ORIGINAL, relpath, paths["image"], paths["label"]
    )


def build_page(staging: str, records) -> ResultsPage:
    "Return a shown page holding the given records."

    page = ResultsPage()
    page.resize(*WIDGET_SIZE)
    page.set_context(list(CLASSES), staging)
    page.set_records(records)
    page.filter_combo.setCurrentIndex(0)
    page.show()
    QtWidgets.QApplication.processEvents()
    return page


def image_point(page, x: float, y: float) -> QtCore.QPointF:
    "Return the widget position of one image pixel of the left canvas."

    return page.gt_canvas.image_to_widget(QtCore.QPointF(float(x), float(y)))


def send(widget, event_type, position, button, buttons) -> None:
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


def toggle(widget, position) -> None:
    "Press and release the middle button at one position."

    for event_type in (
        QtCore.QEvent.Type.MouseButtonPress,
        QtCore.QEvent.Type.MouseButtonRelease,
    ):
        send(
            widget,
            event_type,
            position,
            QtCore.Qt.MouseButton.MiddleButton,
            QtCore.Qt.MouseButton.MiddleButton,
        )


def drag(widget, start, end) -> None:
    "Drag the left button from one widget position to another."

    send(
        widget,
        QtCore.QEvent.Type.MouseButtonPress,
        start,
        QtCore.Qt.MouseButton.LeftButton,
        QtCore.Qt.MouseButton.LeftButton,
    )
    send(
        widget,
        QtCore.QEvent.Type.MouseMove,
        end,
        QtCore.Qt.MouseButton.NoButton,
        QtCore.Qt.MouseButton.LeftButton,
    )
    send(
        widget,
        QtCore.QEvent.Type.MouseButtonRelease,
        end,
        QtCore.Qt.MouseButton.LeftButton,
        QtCore.Qt.MouseButton.NoButton,
    )


def assert_preview_state(page) -> None:
    "Assert the page is back to the plain preview of its record."

    assert page.edit_mode is False
    assert page.gt_canvas.editable is False
    assert page.gt_canvas.editable_shapes() == []
    assert page.gt_canvas.editable_flags() == []
    assert page.gt_canvas.edit_record_id() == ""
    assert page.gt_canvas.selected_index() == -1
    assert page.gt_canvas.title == GT_CANVAS_TITLE
    assert page.edit_hint.text() == ""
    assert page.edit_hint.isVisibleTo(page) is False
    # the predictions of the right canvas were never editable
    assert page.pred_canvas.editable is False


@pytest.fixture
def two_records(qt_app, tmp_path):
    "A shown page with two originals, each carrying one rectangle."

    staging = staging_layout(str(tmp_path), "edit_exit")
    first = staged_original(staging, "a.png", [rect(*BOX)])
    second = staged_original(
        staging, "b.png", [rect((2, 2), (20, 16)), rect((24, 24), (40, 40))]
    )
    widget = build_page(staging, [first, second])
    try:
        yield widget, first, second
    finally:
        widget.close()


def test_a_click_on_another_row_leaves_the_mode(two_records):
    "The row the user picks is the way out of the mode."

    page, first, second = two_records
    toggle(page.gt_canvas, image_point(page, 28.0, 22.0))
    assert page.edit_mode is True
    assert page.gt_canvas.title == GT_CANVAS_TITLE + EDIT_TITLE_SUFFIX

    page.table.selectRow(1)
    QtWidgets.QApplication.processEvents()

    assert page.current_record() is second
    assert_preview_state(page)


def test_the_d_key_leaves_the_mode_on_the_next_picture(two_records):
    "The keyboard navigation is a switch of the record like any other."

    page, first, second = two_records
    toggle(page.gt_canvas, image_point(page, 28.0, 22.0))
    assert page.edit_mode is True

    QtTest.QTest.keyClick(page.gt_canvas, QtCore.Qt.Key.Key_D)
    QtWidgets.QApplication.processEvents()

    assert page.current_record() is second
    assert_preview_state(page)

    # and the mode is armed again on the record that is on screen now
    toggle(page.gt_canvas, image_point(page, 10.0, 8.0))
    assert page.edit_mode is True
    assert page.gt_canvas.editable_flags() == [True, True]


def test_the_a_key_leaves_the_mode_as_well(two_records):
    "Walking backwards is the very same switch."

    page, first, second = two_records
    page.table.selectRow(1)
    QtWidgets.QApplication.processEvents()
    toggle(page.gt_canvas, image_point(page, 10.0, 8.0))
    assert page.edit_mode is True

    QtTest.QTest.keyClick(page.gt_canvas, QtCore.Qt.Key.Key_A)
    QtWidgets.QApplication.processEvents()

    assert page.current_record() is first
    assert_preview_state(page)


def test_a_repaint_of_the_same_record_keeps_the_mode(two_records):
    "A drag, a rename or a display switch never drops the mode."

    page, first, second = two_records
    canvas = page.gt_canvas
    center = image_point(page, 28.0, 22.0)
    toggle(canvas, center)
    assert page.edit_mode is True

    # a display switch repaints the two canvases through
    # _refresh_canvases, which the selection signal reaches as well
    page.overlay_check.setChecked(False)
    QtWidgets.QApplication.processEvents()
    assert page.edit_mode is True
    assert canvas.editable is True

    # a finished drag rewrites the row of the record in place and
    # repaints the two canvases: the mode stays where the user left it
    before = [list(point) for point in canvas.editable_shapes()[0]["points"]]
    drag(canvas, center, QtCore.QPointF(center.x() + 12.0, center.y()))
    assert canvas.editable_shapes()[0]["points"] != before
    assert page.edit_mode is True
    assert canvas.editable is True
    assert canvas.editable_shapes()
    assert canvas.title == GT_CANVAS_TITLE + EDIT_TITLE_SUFFIX

    # a rebuild of the list selects the very same record again
    page.refresh()
    QtWidgets.QApplication.processEvents()
    assert page.current_record() is first
    assert page.edit_mode is True
    assert canvas.editable is True
    # the box the user is working on is still the picked one: the
    # rebuild reaches the canvas as the record it already shows
    assert canvas.selected_index() == 0


def test_the_mode_leaves_before_an_emptied_list_shows_nothing(two_records):
    "A page without a record has no record to edit either."

    page, _first, _second = two_records
    toggle(page.gt_canvas, image_point(page, 28.0, 22.0))
    assert page.edit_mode is True

    page.set_records([])
    QtWidgets.QApplication.processEvents()

    assert page.current_record() is None
    assert_preview_state(page)


def test_a_filter_that_drops_the_row_leaves_the_mode(two_records):
    "The row the user was editing may not stay armed off screen."

    page, first, second = two_records
    first.verdict = records_module.NG
    second.verdict = records_module.OK
    page.refresh()
    QtWidgets.QApplication.processEvents()
    assert page.current_record() is first

    toggle(page.gt_canvas, image_point(page, 28.0, 22.0))
    assert page.edit_mode is True

    # the NG filter shows the first record alone, so the list stays on
    # it; the OK filter drops it and the next record is selected instead
    page.filter_combo.setCurrentIndex(page.filter_combo.findData("OK"))
    QtWidgets.QApplication.processEvents()

    assert page.current_record() is second
    assert_preview_state(page)


def test_a_filter_that_keeps_the_row_keeps_the_mode(two_records):
    "A rebuild that lands on the same record never leaves the mode."

    page, first, second = two_records
    first.verdict = records_module.NG
    second.verdict = records_module.OK
    page.refresh()
    QtWidgets.QApplication.processEvents()
    assert page.current_record() is first

    canvas = page.gt_canvas
    center = image_point(page, 28.0, 22.0)
    toggle(canvas, center)
    assert page.edit_mode is True
    # a click on the box picks the shape the user is working on: the
    # selection is part of the state a rebuild may not touch either
    drag(canvas, center, center)
    assert canvas.selected_index() == 0
    before = [list(point) for point in canvas.editable_shapes()[0]["points"]]

    # the NG filter still holds the record the mode was armed on, so the
    # rebuild selects the very same row again; the empty selection the
    # table reports while it is cleared may not drop the mode on the way
    page.filter_combo.setCurrentIndex(page.filter_combo.findData("NG"))
    QtWidgets.QApplication.processEvents()

    assert page.current_record() is first
    assert page.edit_mode is True
    assert canvas.editable is True
    assert canvas.title == GT_CANVAS_TITLE + EDIT_TITLE_SUFFIX
    assert page.edit_hint.text() != ""
    assert canvas.edit_record_id() == str(first.record_id)
    assert canvas.editable_shapes()
    assert canvas.selected_index() == 0
    assert [
        list(point) for point in canvas.editable_shapes()[0]["points"]
    ] == before


def test_the_empty_selection_of_a_running_rebuild_is_no_switch(two_records):
    "The state a rebuild passes through may not drop the mode."

    page, _first, _second = two_records
    canvas = page.gt_canvas
    toggle(canvas, image_point(page, 28.0, 22.0))
    assert page.edit_mode is True

    # refresh clears the table while the flag of its rebuild is up, so a
    # selection signal raised in between resolves to no record at all
    # (see _on_selection_changed). The state below is that moment, asked
    # directly: the mode of a record that never left the screen has to
    # survive a signal of a table that is only half built.
    page.table.clear()
    page._loading = True
    try:
        page._on_selection_changed()
    finally:
        page._loading = False

    assert page.edit_mode is True
    assert canvas.editable is True
    assert canvas.title == GT_CANVAS_TITLE + EDIT_TITLE_SUFFIX
    assert canvas.editable_shapes()


def test_the_mode_can_be_left_by_hand_and_is_not_left_twice(two_records):
    "leave_edit_mode is idempotent, and the middle button still works."

    page, _first, _second = two_records
    toggle(page.gt_canvas, image_point(page, 28.0, 22.0))
    assert page.leave_edit_mode() is True
    assert_preview_state(page)
    # a mode that is already off costs nothing
    assert page.leave_edit_mode() is False
    assert_preview_state(page)

    # and it is not a one way door
    toggle(page.gt_canvas, image_point(page, 28.0, 22.0))
    assert page.edit_mode is True
    toggle(page.gt_canvas, image_point(page, 28.0, 22.0))
    assert page.edit_mode is False
    assert page.gt_canvas.editable is False
