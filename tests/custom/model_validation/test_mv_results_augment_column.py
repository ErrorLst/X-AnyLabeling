"""The augmentation column of the results list.

Every augmented copy is one draw of the augmentation parameters, and the
fifth column of the list says which fields that draw really selected -
in the order the record stored them - while an original shows the dash.
The cell tooltip carries the seeds that reproduce the very copy. These
tests pin the five columns down, the text of every kind of row, the
fallback of a field without a label of its own and the way a point
update rewrites the cell without replacing it.
"""

import os
import os.path as osp

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import cv2
import numpy as np
import pytest

from PyQt6 import QtCore, QtWidgets

from anylabeling.custom.model_validation import dataset
from anylabeling.custom.model_validation import records as records_module
from anylabeling.custom.model_validation.app_config import (
    AugmentParams,
    selectable_fields,
)
from anylabeling.custom.model_validation.ui.results_page import (
    AUGMENT_FIELD_LABELS,
    AUGMENT_SELECTION_NONE,
    COLUMN_AUGMENT,
    COLUMN_AUGMENT_TOOLTIP,
    COLUMN_EXPORT,
    COLUMN_KIND,
    COLUMN_MARK,
    COLUMN_RELPATH,
    COLUMN_VERDICT,
    HEADERS,
    ResultsPage,
)

CLASSES = ["a0_dian"]
SIZE = (32, 24)
# the keys a real pipeline writes into aug_detail, all present
SEED_KEYS = {
    "copy_index": 1,
    "seed": 7,
    # a real record really holds a draw: the try that produced it is
    # attempts - 1, never an attempt of a budget still ahead
    "attempt": 2,
    "attempts": 3,
    "attempt_seed": 20240101,
}


@pytest.fixture(scope="module")
def qt_app():
    "Create the offscreen application the page needs."

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


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


def label_payload(relpath: str, size) -> dict:
    "Return the xlabel payload of one staged picture."

    return {
        "version": "3.0.0",
        "flags": {},
        "checked": False,
        "shapes": [],
        "imagePath": osp.basename(relpath),
        "imageData": None,
        "imageHeight": size[1],
        "imageWidth": size[0],
    }


def staged(kind: str, staging: str, relpath: str, parent_record_id=""):
    "Stage one picture and return the record that points at it."

    paths = dataset.staging_paths(staging, kind, relpath)
    write_image(paths["image"], SIZE, 30)
    dataset.write_json(paths["label"], label_payload(relpath, SIZE))
    return records_module.make_record(
        kind,
        relpath,
        paths["image"],
        paths["label"],
        parent_record_id=parent_record_id or None,
    )


@pytest.fixture
def page_records(qt_app, tmp_path):
    "A shown page with one original and four copies of different draws."

    staging = staging_layout(str(tmp_path), "augment_column")
    parent = staged(records_module.KIND_ORIGINAL, staging, "a.png")
    parent.verdict = records_module.NG
    parent.judged = True
    copies = {}
    for name, selected in (
        ("three", ["contrast", "degrees", "fliplr"]),
        ("reordered", ["fliplr", "contrast"]),
        ("unknown", ["contrast", "brightness"]),
    ):
        child = staged(
            records_module.KIND_AUGMENTED,
            staging,
            "a_" + name + ".png",
            parent.record_id,
        )
        child.verdict = records_module.OK
        child.judged = True
        child.aug_detail = dict(SEED_KEYS, selected=list(selected))
        copies[name] = child
    # a copy whose record carries no draw at all
    no_draw = staged(
        records_module.KIND_AUGMENTED,
        staging,
        "a_no_draw.png",
        parent.record_id,
    )
    no_draw.verdict = records_module.OK
    no_draw.judged = True
    copies["no_draw"] = no_draw

    page = ResultsPage()
    page.resize(1024, 640)
    page.set_context(list(CLASSES), staging)
    page.set_records([parent, *copies.values()])
    # the page opens on NG: this file looks at the whole list
    page.filter_combo.setCurrentIndex(0)
    page.show()
    QtWidgets.QApplication.processEvents()
    try:
        yield page, parent, copies
    finally:
        page.close()


def row_of(page, record) -> int:
    "Return the shown row of one record."

    for row in range(page.table.rowCount()):
        current = page.record_at(row)
        if current is not None and current.record_id == record.record_id:
            return row
    raise AssertionError("record not shown: " + record.record_id)


def cell(page, record, column) -> str:
    "Return the text of one cell of one record."

    row = row_of(page, record)
    item = page.table.item(row, column)
    assert item is not None
    return item.text(column)


def tooltip_of(page, record, column) -> str:
    "Return the tooltip of one cell of one record."

    row = row_of(page, record)
    item = page.table.item(row, column)
    assert item is not None
    return item.toolTip(column)


# ------------------------------------------------------------- the columns
def test_the_list_shows_the_augmentation_column(page_records):
    "标记 / 状态 / relpath / kind / 增强, the fifth column at index 4."

    page, _parent, _copies = page_records
    table = page.table

    assert HEADERS == ("标记", "状态", "relpath", "kind", "增强")
    assert COLUMN_AUGMENT == 4
    assert (
        COLUMN_MARK,
        COLUMN_VERDICT,
        COLUMN_RELPATH,
        COLUMN_KIND,
    ) == (0, 1, 2, 3)
    # the mark column is still the export column of the previous layout
    assert COLUMN_EXPORT == COLUMN_MARK
    assert table.columnCount() == len(HEADERS) == 5
    for column, title in enumerate(HEADERS):
        assert table.horizontalHeaderItem(column).text(column) == title
    header = table.horizontalHeaderItem(COLUMN_AUGMENT)
    assert header.toolTip(COLUMN_AUGMENT) == COLUMN_AUGMENT_TOOLTIP
    assert table.header().sectionResizeMode(COLUMN_AUGMENT) == (
        QtWidgets.QHeaderView.ResizeMode.ResizeToContents
    )


def test_an_augmented_row_names_the_fields_it_really_got(page_records):
    "The cell writes the selected fields in the order they were stored."

    page, _parent, copies = page_records

    assert cell(page, copies["three"], COLUMN_AUGMENT) == (
        "对比度+旋转+水平翻转"
    )
    # the cell never sorts: the recorded order is the order on screen
    assert cell(page, copies["reordered"], COLUMN_AUGMENT) == (
        "水平翻转+对比度"
    )


def test_an_original_row_has_no_augmentation_content(page_records):
    "An original, and a copy without a recorded draw, show the dash."

    page, parent, copies = page_records

    assert cell(page, parent, COLUMN_AUGMENT) == AUGMENT_SELECTION_NONE
    assert cell(page, copies["no_draw"], COLUMN_AUGMENT) == (
        AUGMENT_SELECTION_NONE
    )
    # the dash is the em dash, not an ASCII hyphen
    assert AUGMENT_SELECTION_NONE == "\u2014"


def test_an_unknown_field_name_is_shown_as_it_is(page_records):
    "A field without a label of its own is never swallowed."

    page, _parent, copies = page_records

    assert cell(page, copies["unknown"], COLUMN_AUGMENT) == (
        "对比度+brightness"
    )


def test_every_selectable_field_has_a_chinese_label(qt_app):
    "The label table covers exactly the candidates of the draw."

    assert set(AUGMENT_FIELD_LABELS) == set(
        selectable_fields(AugmentParams())
    )


def test_the_cell_survives_a_point_update(page_records):
    "A point update rewrites the text and replaces no item."

    page, _parent, copies = page_records
    record = copies["three"]
    row = row_of(page, record)
    verdict_item = page.table.item(row, COLUMN_VERDICT)
    top_item = page.table.topLevelItem(row)
    mark_box = page.table.cellWidget(row, COLUMN_MARK)
    row_count = page.table.rowCount()

    assert page.refresh_rows([record.record_id]) == [row]

    assert cell(page, record, COLUMN_AUGMENT) == "对比度+旋转+水平翻转"
    # the row itself is the very item it was, and the record it stands
    # for is still the one its verdict cell carries (see current_record)
    assert page.table.topLevelItem(row) is top_item
    assert page.table.item(row, COLUMN_VERDICT) is verdict_item
    carried = verdict_item.data(QtCore.Qt.ItemDataRole.UserRole)
    assert carried == record.record_id
    # the checkbox of the mark column is not replaced either, and no
    # row was added or dropped by the point update
    assert page.table.cellWidget(row, COLUMN_MARK) is mark_box
    assert page.table.rowCount() == row_count


def test_the_column_tooltip_names_the_copy_and_its_seed(page_records):
    "The tooltip of a copy carries the numbers that replay it."

    page, parent, copies = page_records
    tooltip = tooltip_of(page, copies["three"], COLUMN_AUGMENT)

    assert "副本 #1" in tooltip
    assert "基种子 7" in tooltip
    assert "第 3 次尝试" in tooltip
    assert "尝试种子 20240101" in tooltip
    # an original never carries one
    assert tooltip_of(page, parent, COLUMN_AUGMENT) == ""
