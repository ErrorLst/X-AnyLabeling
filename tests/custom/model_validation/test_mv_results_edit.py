"""The record list refuses every edit path without crashing.

The user reached the results page, clicked a row and the window died
with "TypeError: RecordTree.edit() takes 2 positional arguments but 4
were given": the class overrode the Qt virtual
"QAbstractItemView.edit(index, trigger, event)" with a one argument
method, so every standard edit path of Qt blew up on the way in. The
override is gone; the read only promise is carried by NoEditTriggers
and by the refusal of the non virtual "editItem(item, column=0)".

The tests drive the chain the user drove - click a row, select it,
double click it, press F2 - and call the Qt entry points by hand: none
of them may raise and none of them may open an editor.
"""

import inspect
import os
import os.path as osp

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from PyQt6 import QtCore, QtTest, QtWidgets

from anylabeling.custom.model_validation import dataset
from anylabeling.custom.model_validation import records as records_module
from anylabeling.custom.model_validation.ui.results_page import (
    COLUMN_AUGMENT,
    COLUMN_KIND,
    COLUMN_RELPATH,
    COLUMN_VERDICT,
    RecordTree,
    ResultsPage,
)

CLASSES = ["car"]
ORIGINALS = ("a.jpg", "b.jpg")
COPIES = 2

EDIT_TRIGGERS = QtWidgets.QAbstractItemView.EditTrigger
NO_EDIT = EDIT_TRIGGERS.NoEditTriggers
NO_STATE = QtWidgets.QAbstractItemView.State.NoState


@pytest.fixture(scope="module")
def qt_app():
    "Create the offscreen application the results page needs."

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

    import cv2

    os.makedirs(osp.dirname(path), exist_ok=True)
    image = np.full((24, 32, 3), int(value), dtype=np.uint8)
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
                "label": "car",
                "shape_type": "rectangle",
                "points": [[4, 4], [20, 4], [20, 20], [4, 20]],
                "group_id": None,
                "description": "",
                "flags": {},
            }
        ],
        "imagePath": osp.basename(relpath),
        "imageData": None,
        "imageHeight": 24,
        "imageWidth": 32,
    }


def staged_original(staging: str, relpath: str):
    "Create one staged original record with its files."

    paths = dataset.staging_paths(
        staging, records_module.KIND_ORIGINAL, relpath
    )
    write_image(paths["image"])
    dataset.write_json(paths["label"], label_payload(relpath))
    return records_module.make_record(
        records_module.KIND_ORIGINAL, relpath, paths["image"], paths["label"]
    )


def staged_child(staging: str, relpath: str, parent_record_id: str):
    "Create one staged augmented record with its files."

    paths = dataset.staging_paths(
        staging, records_module.KIND_AUGMENTED, relpath
    )
    write_image(paths["image"], 90)
    dataset.write_json(paths["label"], label_payload(relpath))
    return records_module.make_record(
        records_module.KIND_AUGMENTED,
        relpath,
        paths["image"],
        paths["label"],
        parent_record_id=parent_record_id,
    )


def build_records(staging: str):
    "Stage two originals with two augmented copies each."

    records = []
    for relpath in ORIGINALS:
        original = staged_original(staging, relpath)
        records.append(original)
        stem = osp.splitext(relpath)[0]
        for copy_index in range(1, COPIES + 1):
            records.append(
                staged_child(
                    staging,
                    f"{stem}_aug{copy_index}.png",
                    original.record_id,
                )
            )
    return records


@pytest.fixture
def page(qt_app, tmp_path):
    "Show the six records of one staged run on a results page."

    staging = staging_layout(str(tmp_path), "edit_paths")
    records = build_records(staging)
    widget = ResultsPage()
    try:
        # a page of its own has no size until it is laid out: the click
        # tests need a viewport wide enough for a whole row
        widget.resize(1024, 640)
        widget.set_context(list(CLASSES), staging)
        widget.set_records(records)
        # the page opens on NG and this fixture looks at the whole list
        widget.filter_combo.setCurrentIndex(0)
        widget.show()
        QtWidgets.QApplication.processEvents()
        yield widget
    finally:
        widget.close()


def index_of(tree, row: int, column: int = COLUMN_VERDICT):
    "Return the cell index of one row."

    item = tree.topLevelItem(row)
    assert item is not None
    return tree.indexFromItem(item, column)


def click_point(tree, row: int) -> QtCore.QPoint:
    "Return a point inside the verdict cell of one row."

    item = tree.topLevelItem(row)
    assert item is not None
    tree.scrollToItem(item)
    rect = tree.visualRect(index_of(tree, row))
    assert rect.isValid()
    # a narrow cell may reach past the viewport of a small page: the
    # point stays inside the widget either way
    viewport = tree.viewport()
    x = min(rect.center().x(), viewport.width() - 2)
    y = min(rect.center().y(), viewport.height() - 2)
    return QtCore.QPoint(max(x, 1), max(y, 1))


def test_the_tree_keeps_only_the_safe_edit_signatures():
    """No override shadows a Qt virtual with a foreign signature.

    The crash came from a one argument edit(index) shadowing the Qt
    virtual edit(index, trigger, event). The class defines no edit at all
    any more and the single override it keeps, editItem, carries the very
    signature Qt declares for it: editItem(item, column=0).
    """

    assert "edit" not in vars(RecordTree)
    parameters = inspect.signature(RecordTree.editItem).parameters
    assert list(parameters) == ["self", "item", "column"]
    assert parameters["column"].default == 0


def test_a_click_on_a_row_selects_it_without_crashing(page):
    "The action of the user: click a row, the page stays alive."

    tree = page.table
    assert tree.rowCount() == len(ORIGINALS) * (COPIES + 1)
    seen = []
    page.selection_changed.connect(seen.append)

    # the page opens on its first row, so the click below moves the
    # selection to another one and the page reports the change
    assert tree.currentRow() == 0
    item = tree.topLevelItem(1)
    QtTest.QTest.mouseClick(
        tree.viewport(),
        QtCore.Qt.MouseButton.LeftButton,
        QtCore.Qt.KeyboardModifier.NoModifier,
        click_point(tree, 1),
    )
    QtWidgets.QApplication.processEvents()

    assert tree.currentItem() is item
    assert tree.state() == NO_STATE
    assert seen and seen[-1] == item.data(COLUMN_VERDICT)


def test_selecting_a_row_keeps_the_page_read_only(page):
    "setCurrentItem and itemSelectionChanged never open an editor."

    tree = page.table
    item = tree.topLevelItem(1)
    tree.setCurrentItem(item)
    tree.itemSelectionChanged.emit()
    QtWidgets.QApplication.processEvents()

    assert tree.currentItem() is item
    assert page.selected_record_ids() == [item.data(COLUMN_VERDICT)]
    assert tree.state() == NO_STATE


def test_the_three_argument_edit_of_qt_never_raises(page):
    "Qt calls edit(index, trigger, event): the call must survive it."

    tree = page.table
    index = index_of(tree, 0)
    for trigger in (
        NO_EDIT,
        EDIT_TRIGGERS.DoubleClicked,
        EDIT_TRIGGERS.EditKeyPressed,
        EDIT_TRIGGERS.SelectedClicked,
        EDIT_TRIGGERS.AnyKeyPressed,
    ):
        assert tree.edit(index, trigger, None) is not True, trigger
        assert tree.state() == NO_STATE, trigger
    # the public one argument slot of the base class stays usable
    assert tree.edit(index) is not True
    assert tree.state() == NO_STATE


def test_edit_item_never_opens_an_editor(page):
    "editItem keeps the Qt signature of the base class and refuses."

    tree = page.table
    item = tree.topLevelItem(0)
    assert tree.editItem(item, COLUMN_VERDICT) is None
    assert tree.editItem(item, 0) is None
    assert tree.state() == NO_STATE


def test_a_double_click_and_f2_never_open_an_editor(page):
    "The two edit gestures of Qt are refused as well."

    tree = page.table
    item = tree.topLevelItem(2)
    point = click_point(tree, 2)
    tree.setCurrentItem(item)
    QtTest.QTest.mouseDClick(
        tree.viewport(),
        QtCore.Qt.MouseButton.LeftButton,
        QtCore.Qt.KeyboardModifier.NoModifier,
        point,
    )
    QtTest.QTest.keyClick(tree, QtCore.Qt.Key.Key_F2)
    QtWidgets.QApplication.processEvents()

    assert tree.currentItem() is item
    assert tree.state() == NO_STATE
    assert tree.editTriggers() == NO_EDIT
    assert tree.editItem(item, 0) is None
    assert tree.state() == NO_STATE


def test_the_rows_still_carry_no_edit_flag(page):
    "No cell of the list is editable, whatever Qt asks the model."

    tree = page.table
    for row in range(tree.rowCount()):
        for column in (
            COLUMN_VERDICT,
            COLUMN_RELPATH,
            COLUMN_KIND,
            COLUMN_AUGMENT,
        ):
            item = tree.item(row, column)
            assert item is not None
            flags = item.flags()
            assert not (flags & QtCore.Qt.ItemFlag.ItemIsEditable)
