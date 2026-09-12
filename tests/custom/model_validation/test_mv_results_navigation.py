"""Results page navigation: A / D, the first row and the file name order.

Three behaviours of the results page are pinned here.

* The page opens on its first row: the run just finished, the picture and
  its annotations are on screen at once, and only an empty list leaves
  the right hand side blank.
* A / D walk the list one record at a time, scroll the new row into view
  and let both canvases open the new picture on its own first screen; the
  ends of the list stop the walk instead of wrapping around.
* The rows follow the file name of a record in natural order (a2 before
  a10), the kind only decides which block comes first: every original,
  then the augmented copies. The verdict is no longer part of the order -
  the filter combo is what groups by verdict.
"""

import os
import os.path as osp

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import cv2
import numpy as np
import pytest

from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtTest import QTest

from anylabeling.custom.model_validation import dataset
from anylabeling.custom.model_validation import records as records_module
from anylabeling.custom.model_validation.ui.results_page import (
    COLUMN_KIND,
    COLUMN_VERDICT,
    SHORTCUT_HINT,
    ResultsPage,
    record_sort_key,
)

CLASSES = ["a0_dian"]

WIDGET_SIZE = (400, 320)


@pytest.fixture(scope="module")
def qt_app():
    "Create the offscreen application shared by the navigation tests."

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def staging_layout(scratch: str, suffix: str) -> str:
    "Create the staging folder layout the tool expects."

    staging = osp.join(scratch, dataset.STAGING_PREFIX + suffix)
    for folder in (dataset.ORIGINAL_DIRNAME, dataset.AUGMENTED_DIRNAME):
        for sub in (dataset.IMAGES_DIRNAME, dataset.LABELS_DIRNAME):
            os.makedirs(osp.join(staging, folder, sub), exist_ok=True)
    return staging


def write_image(path: str, width: int = 64, height: int = 48) -> None:
    "Write a tiny flat png of a given size."

    os.makedirs(osp.dirname(path), exist_ok=True)
    image = np.full((height, width, 3), 128, dtype=np.uint8)
    ok, buffer = cv2.imencode(".png", image)
    assert ok
    buffer.tofile(path)


def label_payload(relpath: str) -> dict:
    "Return the xlabel payload of a staged pair."

    return {
        "version": "3.0.0",
        "flags": {},
        "checked": False,
        "shapes": [
            {
                "label": "a0_dian",
                "shape_type": "rectangle",
                "points": [[4, 4], [20, 4], [20, 20], [4, 20]],
                "group_id": None,
                "description": "",
                "flags": {},
            }
        ],
        "imagePath": osp.basename(relpath),
        "imageData": None,
        "imageHeight": 48,
        "imageWidth": 64,
    }


def staged_original(staging: str, relpath: str, size=(64, 48)):
    "Create one staged original record with its files."

    paths = dataset.staging_paths(
        staging, records_module.KIND_ORIGINAL, relpath
    )
    write_image(paths["image"], size[0], size[1])
    dataset.write_json(paths["label"], label_payload(relpath))
    return records_module.make_record(
        records_module.KIND_ORIGINAL, relpath, paths["image"], paths["label"]
    )


def staged_child(staging: str, relpath: str, parent_record_id: str):
    "Create one staged augmented record with its files."

    paths = dataset.staging_paths(
        staging, records_module.KIND_AUGMENTED, relpath
    )
    write_image(paths["image"], 32, 24)
    dataset.write_json(paths["label"], label_payload(relpath))
    return records_module.make_record(
        records_module.KIND_AUGMENTED,
        relpath,
        paths["image"],
        paths["label"],
        parent_record_id=parent_record_id,
    )


def show_page(staging: str, records):
    "Return a shown, focused results page holding the given records."

    page = ResultsPage()
    page.resize(1024, 640)
    page.set_context(list(CLASSES), staging)
    page.set_records(records)
    # the page opens on NG and these tests look at the whole list
    page.filter_combo.setCurrentIndex(0)
    page.show()
    page.table.setFocus()
    QtWidgets.QApplication.processEvents()
    return page


def shown_relpaths(page):
    "Return the relpaths of the shown rows, in order."

    return [
        page.table.item(row, COLUMN_KIND).text(COLUMN_KIND)
        and str(page.table.item(row, COLUMN_VERDICT).text(COLUMN_VERDICT))
        for row in range(page.table.rowCount())
    ]


def shown_records(page):
    "Return the records of the shown rows, in order."

    return [page.record_at(row) for row in range(page.table.rowCount())]


def relpaths(page):
    "Return the relpaths of the shown rows, in order."

    return [record.relpath for record in shown_records(page)]


def press(page, key, target=None):
    "Send one key to the list of the page and let Qt deliver it."

    QTest.keyClick(target if target is not None else page.table, key)
    QtWidgets.QApplication.processEvents()


# ------------------------------------------------------------ the first row
def test_the_page_opens_on_its_first_row(qt_app, tmp_path):
    "A finished run shows a picture at once, never a blank area."

    staging = staging_layout(str(tmp_path), "nav_first")
    records = [
        staged_original(staging, "a.png"),
        staged_original(staging, "b.png", (128, 96)),
    ]
    page = show_page(staging, records)
    try:
        assert page.table.rowCount() == 2
        assert page.table.currentRow() == 0
        assert page.current_record() is records[0]
        # the right hand side was filled with the first record
        assert page.gt_canvas.image_size().width() == 64
        assert page.pred_canvas.image_size().width() == 64
        assert page.gt_canvas.zoom_factor() == pytest.approx(1.0)
        assert page.gt_canvas.view_state() == pytest.approx(
            page.pred_canvas.view_state(), abs=1e-9
        )
    finally:
        page.close()


def test_an_empty_list_is_the_only_blank_view(qt_app):
    "No record at all leaves the canvases empty and the selection clear."

    page = ResultsPage()
    page.resize(1024, 640)
    page.set_records([])
    try:
        assert page.table.rowCount() == 0
        assert page.table.currentRow() == -1
        assert page.current_record() is None
        assert page.gt_canvas.image_size().width() == 0
        assert page.pred_canvas.image_size().width() == 0
    finally:
        page.close()


def test_an_existing_selection_survives_a_point_update_and_a_rebuild(
    qt_app, tmp_path
):
    "The record the user looks at is kept, it never jumps back to row one."

    staging = staging_layout(str(tmp_path), "nav_keep")
    records = [
        staged_original(staging, name) for name in ("a.png", "b.png", "c.png")
    ]
    page = show_page(staging, records)
    try:
        page.table.setCurrentItem(page.table.topLevelItem(2))
        QtWidgets.QApplication.processEvents()
        assert page.current_record() is records[2]

        page.refresh_rows([records[2].record_id])
        assert page.table.currentRow() == 2
        assert page.current_record() is records[2]

        # a rebuild (the filter, a new record list) restores by id
        page.set_records(records)
        QtWidgets.QApplication.processEvents()
        assert page.table.currentRow() == 2
        assert page.current_record() is records[2]
    finally:
        page.close()


# ------------------------------------------------------------------ A / D
def test_the_d_and_a_keys_walk_the_list_and_fit_the_new_picture(
    qt_app, tmp_path
):
    "D moves one record forward, A one back, and both ends stop."

    staging = staging_layout(str(tmp_path), "nav_keys")
    records = [
        staged_original(staging, "a.png", (64, 48)),
        staged_original(staging, "b.png", (128, 96)),
        staged_original(staging, "c.png", (32, 24)),
    ]
    page = show_page(staging, records)
    try:
        assert page.current_record() is records[0]
        first_width = page.gt_canvas.image_size().width()
        # the user zoomed the first picture by hand
        page.gt_canvas.zoom_steps_at(QtCore.QPointF(60.0, 60.0), 1.0)
        page.gt_canvas.finish_zoom()
        assert page.gt_canvas.zoom_factor() > 1.0

        press(page, QtCore.Qt.Key.Key_D)
        assert page.table.currentRow() == 1
        assert page.current_record() is records[1]
        # the new picture opens on its own first screen and both canvases
        # show it together
        assert page.gt_canvas.image_size().width() != first_width
        assert page.gt_canvas.image_size().width() == 128
        assert page.gt_canvas.zoom_factor() == pytest.approx(1.0)
        assert page.pred_canvas.image_size().width() == 128
        assert page.gt_canvas.view_state() == pytest.approx(
            page.pred_canvas.view_state(), abs=1e-9
        )

        press(page, QtCore.Qt.Key.Key_D)
        assert page.table.currentRow() == 2
        assert page.current_record() is records[2]
        # the last row is the end of the list: D stays on it
        press(page, QtCore.Qt.Key.Key_D)
        assert page.table.currentRow() == 2
        assert page.current_record() is records[2]

        press(page, QtCore.Qt.Key.Key_A)
        assert page.table.currentRow() == 1
        press(page, QtCore.Qt.Key.Key_A)
        assert page.table.currentRow() == 0
        # and so is the first row: A stays on it
        press(page, QtCore.Qt.Key.Key_A)
        assert page.table.currentRow() == 0
        assert page.current_record() is records[0]
    finally:
        page.close()


def test_the_keys_scroll_the_new_row_into_view(qt_app):
    "The walk keeps the selected row visible in a long list."

    page = ResultsPage()
    page.resize(1024, 300)
    records = [
        records_module.make_record(
            records_module.KIND_ORIGINAL, f"a{index:04d}.png", ""
        )
        for index in range(400)
    ]
    page.set_records(records)
    # the page opens on NG and this test looks at the whole list
    page.filter_combo.setCurrentIndex(0)
    page.show()
    page.table.setFocus()
    QtWidgets.QApplication.processEvents()
    try:
        assert page.table.rowCount() == 400
        viewport = page.table.viewport()
        for _ in range(150):
            press(page, QtCore.Qt.Key.Key_D)
        row = page.table.currentRow()
        assert row == 150
        # the walk scrolled the new row into view instead of leaving the
        # view at the top of the list
        rect = page.table.visualItemRect(page.table.topLevelItem(row))
        assert rect.top() >= 0
        assert rect.bottom() <= viewport.height()
        # the whole way down: the last row is the end of the list and the
        # view followed it to the bottom
        bar = page.table.verticalScrollBar()
        for _ in range(300):
            press(page, QtCore.Qt.Key.Key_D)
        assert page.table.currentRow() == page.table.rowCount() - 1
        rect = page.table.visualItemRect(
            page.table.topLevelItem(page.table.currentRow())
        )
        assert rect.bottom() <= viewport.height()
        assert bar.value() == bar.maximum()
        # and every row the key landed on scrolled back into view
        for _ in range(400):
            press(page, QtCore.Qt.Key.Key_A)
        assert page.table.currentRow() == 0
        rect = page.table.visualItemRect(page.table.topLevelItem(0))
        assert rect.top() >= 0 and rect.bottom() <= viewport.height()
        assert bar.value() == bar.minimum()
    finally:
        page.close()


def test_the_shortcuts_belong_to_the_page_and_its_children(qt_app):
    "A and D are page shortcuts, and the page writes them down."

    page = ResultsPage()
    try:
        assert page.next_shortcut.key().toString() == "D"
        assert page.previous_shortcut.key().toString() == "A"
        for shortcut in (page.next_shortcut, page.previous_shortcut):
            assert shortcut.parent() is page
            assert shortcut.context() == (
                QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut
            )
        # the list keeps them while it holds the focus, and the page says
        # so in its two hints
        assert "A" in page.shortcut_hint.text()
        assert "D" in page.shortcut_hint.text()
        assert page.shortcut_hint.text() == SHORTCUT_HINT
        assert "A" in page.table.toolTip() and "D" in page.table.toolTip()
        assert page.shortcut_hint.toolTip()
    finally:
        page.close()


def test_a_hidden_page_never_swallows_a_key(qt_app):
    "The shortcuts are page local: nothing fires while it is hidden."

    page = ResultsPage()
    records = [
        records_module.make_record(
            records_module.KIND_ORIGINAL, f"a{index}.png", ""
        )
        for index in range(4)
    ]
    page.resize(1024, 640)
    page.set_records(records)
    # the page opens on NG and this test looks at the whole list
    page.filter_combo.setCurrentIndex(0)
    try:
        assert page.table.currentRow() == 0
        page.hide()
        QtWidgets.QApplication.processEvents()
        press(page, QtCore.Qt.Key.Key_D)
        assert page.table.currentRow() == 0
    finally:
        page.close()


# --------------------------------------------------------------- the order
def test_the_rows_follow_the_file_name_in_natural_order(qt_app, tmp_path):
    "a2 before a10, originals first, and the verdict orders nothing."

    staging = staging_layout(str(tmp_path), "nav_order")
    records = []
    for name in ("a10.png", "a2.png", "a1.png", "a100.png"):
        original = staged_original(staging, name)
        records.append(original)
        if name == "a2.png":
            for copy_index in (1, 2, 10):
                records.append(
                    staged_child(
                        staging,
                        f"a2_aug{copy_index}.png",
                        original.record_id,
                    )
                )
    # a NG verdict does not move a record to the top any more
    records[0].verdict = records_module.NG
    for record in records:
        if record.verdict == records_module.PENDING:
            record.verdict = records_module.OK

    page = show_page(staging, records)
    try:
        assert relpaths(page) == [
            "a1.png",
            "a2.png",
            "a10.png",
            "a100.png",
            "a2_aug1.png",
            "a2_aug2.png",
            "a2_aug10.png",
        ]
        # every original comes before the augmented copies
        kinds = [
            page.table.item(row, COLUMN_KIND).text(COLUMN_KIND)
            for row in range(page.table.rowCount())
        ]
        assert (
            kinds
            == [records_module.KIND_ORIGINAL] * 4
            + [records_module.KIND_AUGMENTED] * 3
        )
        # and the shown order is exactly the order of that key
        assert relpaths(page) == [
            record.relpath for record in sorted(records, key=record_sort_key)
        ]
    finally:
        page.close()


def test_the_verdict_filter_keeps_the_file_name_order(qt_app, tmp_path):
    "The filter groups the rows by verdict, the order inside stays."

    staging = staging_layout(str(tmp_path), "nav_filter")
    records = []
    for name, verdict in (
        ("a10.png", records_module.NG),
        ("a2.png", records_module.OK),
        ("a1.png", records_module.NG),
    ):
        record = staged_original(staging, name)
        record.verdict = verdict
        records.append(record)
    page = show_page(staging, records)
    try:
        index = page.filter_combo.findData(records_module.NG)
        page.filter_combo.setCurrentIndex(index)
        QtWidgets.QApplication.processEvents()
        assert relpaths(page) == ["a1.png", "a10.png"]
        assert page.table.currentRow() == 0
        page.filter_combo.setCurrentIndex(0)
        QtWidgets.QApplication.processEvents()
        assert relpaths(page) == ["a1.png", "a2.png", "a10.png"]
    finally:
        page.close()


def test_the_page_keeps_the_canvases_and_the_list_in_step(qt_app, tmp_path):
    "Walking the list only ever shows the picture of the current row."

    staging = staging_layout(str(tmp_path), "nav_step")
    records = [
        staged_original(staging, "a.png", (64, 48)),
        staged_original(staging, "b.png", (128, 96)),
    ]
    page = show_page(staging, records)
    try:
        assert page.gt_canvas.image_size().width() == 64
        press(page, QtCore.Qt.Key.Key_D)
        assert page.gt_canvas.image_size().width() == 128
        press(page, QtCore.Qt.Key.Key_A)
        assert page.gt_canvas.image_size().width() == 64
        # the canvases are sized like the widget, never like the picture
        for canvas in (page.gt_canvas, page.pred_canvas):
            assert canvas.width() > 0
    finally:
        page.close()


def test_the_top_level_widget_keeps_the_focus_path(qt_app):
    "A key sent to the page itself walks the list as well."

    page = ResultsPage()
    records = [
        records_module.make_record(
            records_module.KIND_ORIGINAL, f"a{index}.png", ""
        )
        for index in range(3)
    ]
    page.resize(1024, 640)
    page.set_records(records)
    # the page opens on NG and this test looks at the whole list
    page.filter_combo.setCurrentIndex(0)
    page.show()
    page.setFocus()
    QtWidgets.QApplication.processEvents()
    try:
        assert page.table.currentRow() == 0
        QTest.keyClick(page, QtCore.Qt.Key.Key_D)
        QtWidgets.QApplication.processEvents()
        assert page.table.currentRow() == 1
    finally:
        page.close()
