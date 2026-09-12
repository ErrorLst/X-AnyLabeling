"""An edit really lands in the staging json of the record.

The gesture of the canvas is only half of the feature: the point set it
publishes travels through the results page into the dialog
(ModelValidationDialog.on_shape_moved) and from there into the staging
file (records.update_shape_points). The tests below drive that whole
chain - a real drag on a real page of a real dialog - and read the bytes
back off the disk: the points of the staging json, the "(edited)" note of
the row, the label of a rename and the refusals of the writer.
"""

import json
import os
import os.path as osp

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import cv2
import numpy as np
import pytest

from PyQt6 import QtCore, QtGui, QtWidgets

from anylabeling.custom.model_validation import dataset
from anylabeling.custom.model_validation import records as records_module
from anylabeling.custom.model_validation.ui import label_dialog
from anylabeling.custom.model_validation.ui.dialog import ModelValidationDialog

CLASSES = ["car", "bus", "truck"]
IMAGE_SIZE = (64, 48)
WIDGET_SIZE = (960, 640)
BOX = ((8, 8), (48, 36))


@pytest.fixture(scope="module")
def qt_app():
    "Create the offscreen application the persistence tests need."

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
            [start[0], start[1]],
            [end[0], start[1]],
            [end[0], end[1]],
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


def source_pair(directory: str, relpath: str = "a.png", label: str = "car"):
    "Write one real image/label pair of a user dataset, bytes included."

    image_path = osp.join(directory, relpath)
    label_path = osp.splitext(image_path)[0] + ".json"
    write_image(image_path)
    dataset.write_json(label_path, label_payload(relpath, [rect(*BOX, label)]))
    return image_path, label_path


def shown_label(record) -> dict:
    "Read the staging json of a record back off the disk."

    with open(record.staging_label_path, encoding="utf-8") as handle:
        return json.load(handle)


def rect_labels(record):
    "Return the (label, points) of every shape of a staging json."

    data = shown_label(record)
    return [
        (shape["label"], [tuple(point) for point in shape["points"]])
        for shape in data["shapes"]
    ]


def verdict_cell_text(page, record) -> str:
    "Return the status cell of one record of the list."

    for row in range(page.table.rowCount()):
        if page.record_at(row) is record:
            return page.table.item(row, 1).text()
    raise AssertionError("record not shown: " + record.record_id)


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


def drag(page, start, end) -> None:
    "Drag the selected box of the page between two image positions."

    canvas = page.gt_canvas
    send(
        canvas,
        QtCore.QEvent.Type.MouseButtonPress,
        image_point(page, *start),
        QtCore.Qt.MouseButton.LeftButton,
        QtCore.Qt.MouseButton.LeftButton,
    )
    send(
        canvas,
        QtCore.QEvent.Type.MouseMove,
        image_point(page, *end),
        QtCore.Qt.MouseButton.NoButton,
        QtCore.Qt.MouseButton.LeftButton,
    )
    send(
        canvas,
        QtCore.QEvent.Type.MouseButtonRelease,
        image_point(page, *end),
        QtCore.Qt.MouseButton.LeftButton,
    )


@pytest.fixture
def dialog(qt_app, tmp_path):
    "A shown window holding one staged original with one box."

    staging = staging_layout(str(tmp_path), "edit_persist")
    record = staged_original(staging, "a.png", [rect(*BOX)])
    window = ModelValidationDialog()
    try:
        window.resize(*WIDGET_SIZE)
        window.records = [record]
        page = window.results_page
        page.set_context(list(CLASSES), staging)
        page.set_records(window.records)
        page.filter_combo.setCurrentIndex(0)
        page.table.selectRow(0)
        window.show()
        QtWidgets.QApplication.processEvents()
        yield window, record
    finally:
        window.close()


def enter_edit_mode(page) -> None:
    "Arm the edit mode with the real middle button of the left canvas."

    center = image_point(page, 28.0, 22.0)
    send(
        page.gt_canvas,
        QtCore.QEvent.Type.MouseButtonPress,
        center,
        QtCore.Qt.MouseButton.MiddleButton,
        QtCore.Qt.MouseButton.MiddleButton,
    )
    QtWidgets.QApplication.processEvents()
    assert page.edit_mode is True


# ------------------------------------------------------------ the writer
def test_a_moved_box_is_written_into_the_staging_json(dialog):
    "The chain a drag really drives: canvas, page, dialog, staging json."

    window, record = dialog
    page = window.results_page
    enter_edit_mode(page)

    drag(page, (28.0, 22.0), (38.0, 30.0))

    assert rect_labels(record) == [
        ("car", [(18.0, 16.0), (58.0, 16.0), (58.0, 44.0), (18.0, 44.0)])
    ]
    assert record.edited is True
    assert verdict_cell_text(page, record).endswith("(edited)")


def test_the_source_dataset_is_never_touched(qt_app, tmp_path, monkeypatch):
    "A real run of a real dataset: the user files never change a byte."

    # a real source dataset, like the one the configuration page points
    # at, and a real staging folder built from it
    scratch = str(tmp_path)
    source = osp.join(scratch, "source")
    image_path, label_path = source_pair(source)
    staging = staging_layout(scratch, "edit_source_invariant")
    dataset.stage_dataset(source, staging)
    records = records_module.records_from_staging(staging, list(CLASSES))
    assert len(records) == 1
    assert records[0].source_display == osp.abspath(source)

    before = dataset.snapshot_directory(source)
    assert set(before) == {"a.png", "a.json"}
    staged_before = snapshot_file(records[0].staging_label_path)

    window = ModelValidationDialog()
    try:
        window.resize(*WIDGET_SIZE)
        window.records = records
        page = window.results_page
        page.set_context(list(CLASSES), staging)
        page.set_records(window.records)
        page.filter_combo.setCurrentIndex(0)
        page.table.selectRow(0)
        window.show()
        QtWidgets.QApplication.processEvents()

        # a real drag and then a real rename, not a description of one
        enter_edit_mode(page)
        drag(page, (28.0, 22.0), (38.0, 30.0))
        monkeypatch.setattr(
            label_dialog, "choose_label", lambda *args, **kwargs: "bus"
        )
        double_click(page, 28.0, 22.0)

        # the staging copy really is the one that changed
        assert records[0].edited is True
        assert rect_labels(records[0]) == [
            ("bus", [(18.0, 16.0), (58.0, 16.0), (58.0, 44.0), (18.0, 44.0)])
        ]
        assert snapshot_file(records[0].staging_label_path) != staged_before

        # and the two files of the user are the very same bytes, with
        # the very same size and the very same modification time
        assert dataset.snapshot_directory(source) == before
        assert snapshot_file(image_path) == before["a.png"]
        assert snapshot_file(label_path) == before["a.json"]
        # both paths of the record live inside the staging folder of the
        # run, under the two names the dataset layer gave them
        for path in (
            records[0].staging_label_path,
            records[0].staging_image_path,
        ):
            parts = osp.normpath(path).split(osp.sep)
            assert dataset.STAGING_PREFIX in osp.normpath(path)
            assert dataset.ORIGINAL_DIRNAME in parts
            assert osp.abspath(path).startswith(osp.abspath(staging))
        assert dataset.IMAGES_DIRNAME in osp.normpath(
            records[0].staging_image_path
        ).split(osp.sep)
        assert dataset.LABELS_DIRNAME in osp.normpath(
            records[0].staging_label_path
        ).split(osp.sep)
    finally:
        window.close()


def snapshot_file(path: str) -> dict:
    "Return the (sha256, mtime_ns, size) of one file."

    return dataset.snapshot_directory(osp.dirname(path))[osp.basename(path)]


def test_a_click_that_moved_nothing_writes_no_file(dialog):
    "Selecting a box is not an edit: no file, no (edited) note."

    window, record = dialog
    page = window.results_page
    enter_edit_mode(page)
    before = shown_label(record)

    drag(page, (28.0, 22.0), (28.0, 22.0))

    assert page.gt_canvas.selected_index() == 0
    assert shown_label(record) == before
    assert record.edited is False
    assert not verdict_cell_text(page, record).endswith("(edited)")


def test_a_click_on_a_handle_writes_no_file(dialog):
    "A click is a click wherever it lands: the corner is a handle too."

    window, record = dialog
    page = window.results_page
    enter_edit_mode(page)
    before = shown_label(record)

    # (8, 8) is the top left corner of the box, which is one of the
    # eight handles of the edit mode; a press and a release with no
    # move at all is still no edit
    drag(page, (8.0, 8.0), (8.0, 8.0))

    assert page.gt_canvas.selected_index() == 0
    assert shown_label(record) == before
    assert record.edited is False
    assert not verdict_cell_text(page, record).endswith("(edited)")


def test_a_handle_dragged_out_of_the_picture_and_back_writes_no_file(dialog):
    "A gesture that ends where it started is not an edit, however far it went."

    window, record = dialog
    page = window.results_page
    enter_edit_mode(page)
    before = shown_label(record)
    canvas = page.gt_canvas
    handle = (8.0, 8.0)

    send(
        canvas,
        QtCore.QEvent.Type.MouseButtonPress,
        image_point(page, *handle),
        QtCore.Qt.MouseButton.LeftButton,
        QtCore.Qt.MouseButton.LeftButton,
    )
    # far outside the picture, where the clamp pins the corner to (0, 0)
    send(
        canvas,
        QtCore.QEvent.Type.MouseMove,
        image_point(page, -400.0, -400.0),
        QtCore.Qt.MouseButton.NoButton,
        QtCore.Qt.MouseButton.LeftButton,
    )
    # and back onto the corner it started from
    send(
        canvas,
        QtCore.QEvent.Type.MouseMove,
        image_point(page, *handle),
        QtCore.Qt.MouseButton.NoButton,
        QtCore.Qt.MouseButton.LeftButton,
    )
    send(
        canvas,
        QtCore.QEvent.Type.MouseButtonRelease,
        image_point(page, *handle),
        QtCore.Qt.MouseButton.LeftButton,
    )

    assert shown_label(record) == before
    assert record.edited is False
    assert not verdict_cell_text(page, record).endswith("(edited)")


def test_a_drag_refreshes_one_row_and_never_rebuilds_the_list(dialog):
    "The list keeps its scroll position: the point update is the cheap path."

    window, record = dialog
    page = window.results_page
    enter_edit_mode(page)
    item = page.table.topLevelItem(0)
    cells = [page.table.item(0, column) for column in (0, 1, 2, 3)]
    rebuilt = []
    original = page.set_records
    page.set_records = lambda records: rebuilt.append(records)

    drag(page, (28.0, 22.0), (34.0, 26.0))

    assert rebuilt == []
    assert page.table.topLevelItem(0) is item
    assert [page.table.item(0, column) for column in (0, 1, 2, 3)] == cells
    assert record.edited is True
    page.set_records = original


def test_the_verdict_of_the_record_is_not_recomputed(dialog):
    "Correcting a box never re-runs the matching of the run."

    window, record = dialog
    page = window.results_page
    record.verdict = records_module.NG
    record.reasons = ["MISS_FP"]
    enter_edit_mode(page)

    drag(page, (28.0, 22.0), (38.0, 30.0))

    assert record.verdict == records_module.NG
    assert record.reasons == ["MISS_FP"]


# ------------------------------------------------------------ the rename
def test_a_rename_of_the_dialog_writes_the_new_label(dialog, monkeypatch):
    "A confirmed choice lands in the staging json, the box stays a box."

    window, record = dialog
    page = window.results_page
    monkeypatch.setattr(
        label_dialog, "choose_label", lambda *args, **kwargs: "bus"
    )
    enter_edit_mode(page)

    double_click(page, 28.0, 22.0)

    label, points = rect_labels(record)[0]
    assert label == "bus"
    assert points == [(8.0, 8.0), (48.0, 8.0), (48.0, 36.0), (8.0, 36.0)]
    assert record.edited is True
    assert verdict_cell_text(page, record).endswith("(edited)")


def test_a_rename_repaints_the_canvas_with_the_new_label(dialog, monkeypatch):
    "The box on the picture carries the new name as soon as it is chosen."

    window, record = dialog
    page = window.results_page
    monkeypatch.setattr(
        label_dialog, "choose_label", lambda *args, **kwargs: "bus"
    )
    enter_edit_mode(page)
    # the two canvases draw the very shapes the page handed them, so
    # the label of the box on screen is the one of those shapes
    ground_truth, _predictions, _detail = page._load_shapes(record)
    assert ground_truth[0]["label"] == "car"

    double_click(page, 28.0, 22.0)

    # the json and the row say "bus" - and so does the shape the left
    # canvas paints, without another record being visited first
    assert rect_labels(record)[0][0] == "bus"
    assert verdict_cell_text(page, record).endswith("(edited)")
    shown, _predictions, _detail = page._load_shapes(record)
    assert shown[0]["label"] == "bus"
    assert page.gt_canvas.editable_shapes()[0]["label"] == "bus"
    painted_shape = page.gt_canvas._ground_truth[0]
    assert painted_shape["label"] == "bus"
    assert page.gt_canvas.editable_shapes()[0] is painted_shape


def test_a_rename_goes_through_the_public_reload_of_the_page(
    dialog, monkeypatch
):
    "The dialog asks the page to reload the record it just wrote."

    window, record = dialog
    page = window.results_page
    enter_edit_mode(page)
    reloaded = []
    original = page.reload_record

    def spy(record_id: str) -> bool:
        reloaded.append(str(record_id))
        return original(record_id)

    # a cancel never reaches the writer, so it never reloads anything
    monkeypatch.setattr(
        label_dialog, "choose_label", lambda *args, **kwargs: None
    )
    double_click(page, 28.0, 22.0)
    assert reloaded == []

    monkeypatch.setattr(label_dialog, "choose_label", spy_label)
    page.reload_record = spy
    try:
        double_click(page, 28.0, 22.0)
    finally:
        page.reload_record = original

    assert reloaded == [record.record_id]
    assert record.edited is True
    assert rect_labels(record)[0][0] == "bus"


def spy_label(*_args, **_kwargs) -> str:
    "Answer the new label of a rename."

    return "bus"


def test_a_cancelled_rename_never_writes_anything(dialog, monkeypatch):
    "A cancel answers None and the staging json is left alone."

    window, record = dialog
    page = window.results_page
    monkeypatch.setattr(
        label_dialog, "choose_label", lambda *args, **kwargs: None
    )
    enter_edit_mode(page)
    before = shown_label(record)

    double_click(page, 28.0, 22.0)

    assert shown_label(record) == before
    assert record.edited is False


def test_choosing_the_label_the_shape_already_has_writes_nothing(
    dialog, monkeypatch
):
    "Confirming the current name is no edit at all."

    window, record = dialog
    page = window.results_page
    monkeypatch.setattr(
        label_dialog, "choose_label", lambda *args, **kwargs: "car"
    )
    enter_edit_mode(page)
    emitted = []
    page.edit_requested.connect(lambda *args: emitted.append(args))

    double_click(page, 28.0, 22.0)

    assert emitted == []
    assert record.edited is False


def test_the_rename_offers_the_class_table_of_the_run(dialog, monkeypatch):
    "The dialog is handed the classes of the page and the current label."

    window, record = dialog
    page = window.results_page
    seen = {}

    def spy(parent, classes, current=""):
        seen["classes"] = list(classes)
        seen["current"] = current
        return None

    monkeypatch.setattr(label_dialog, "choose_label", spy)
    enter_edit_mode(page)

    double_click(page, 28.0, 22.0)

    assert seen["classes"] == CLASSES
    assert seen["current"] == "car"


def test_a_double_click_on_empty_space_still_fits_the_picture(dialog):
    "The gesture of the previous revision survives the edit mode."

    window, _record = dialog
    page = window.results_page
    enter_edit_mode(page)
    canvas = page.gt_canvas
    canvas.zoom_steps_at(QtCore.QPointF(50.0, 50.0), 2.0)
    canvas.finish_zoom()
    assert canvas.zoom_factor() > 1.0

    double_click(page, 60.0, 44.0)

    assert canvas.zoom_factor() == pytest.approx(1.0)


def test_an_empty_class_table_refuses_the_rename(dialog, monkeypatch):
    "A run without a class table cannot rename, and says so."

    window, record = dialog
    page = window.results_page
    page.set_context([], page.staging_edit.text())
    enter_edit_mode(page)
    before = shown_label(record)

    double_click(page, 28.0, 22.0)

    assert shown_label(record) == before
    assert "分类表" in page.preview_note.text()


def double_click(page, x: float, y: float) -> None:
    "Send the double click gesture of Qt to the left canvas."

    canvas = page.gt_canvas
    point = image_point(page, x, y)
    for kind in (
        QtCore.QEvent.Type.MouseButtonPress,
        QtCore.QEvent.Type.MouseButtonRelease,
    ):
        send(
            canvas,
            kind,
            point,
            QtCore.Qt.MouseButton.LeftButton,
            (
                QtCore.Qt.MouseButton.LeftButton
                if kind == QtCore.QEvent.Type.MouseButtonPress
                else QtCore.Qt.MouseButton.NoButton
            ),
        )
    send(
        canvas,
        QtCore.QEvent.Type.MouseButtonDblClick,
        point,
        QtCore.Qt.MouseButton.LeftButton,
        QtCore.Qt.MouseButton.LeftButton,
    )


# ------------------------------------------------------ the writer itself
def test_the_writer_clamps_and_rounds_every_coordinate(qt_app, tmp_path):
    "A point outside the picture is stored on its border, at 2 decimals."

    staging = staging_layout(str(tmp_path), "edit_writer_clamp")
    record = staged_original(staging, "a.png", [rect(*BOX)])

    assert records_module.update_shape_points(
        record, 0, [(-40.0, 12.34567), (999.0, -1.0), (12.0, 99.0)]
    )

    assert rect_labels(record) == [
        ("car", [(0.0, 12.35), (64.0, 0.0), (12.0, 48.0)])
    ]
    assert record.edited is True


def test_the_writer_answers_false_for_every_input_it_cannot_use(
    qt_app, tmp_path
):
    "The same contract as records.update_shape: a refusal, never a raise."

    staging = staging_layout(str(tmp_path), "edit_writer_refusals")
    record = staged_original(
        staging,
        "a.png",
        [
            rect(*BOX),
            {"label": "p", "shape_type": "point", "points": [[3, 3]]},
            "not a shape",
        ],
    )
    before = shown_label(record)

    # an index outside the shape list
    assert records_module.update_shape_points(record, 2, [[1, 1]]) is False
    assert records_module.update_shape_points(record, -1, [[1, 1]]) is False
    assert records_module.update_shape_points(record, "x", [[1, 1]]) is False
    # an empty or unusable point set
    assert records_module.update_shape_points(record, 0, []) is False
    assert records_module.update_shape_points(record, 0, None) is False
    assert records_module.update_shape_points(record, 0, [None, "x"]) is False
    assert (
        records_module.update_shape_points(
            record, 0, [(float("nan"), 1.0), (1.0, float("inf"))]
        )
        is False
    )
    # a shape type the mode does not edit
    assert records_module.update_shape_points(record, 1, [[5, 5]]) is False
    # an entry of the shape list that is not the dict the writer needs
    assert records_module.update_shape_points(record, 2, [[5, 5]]) is False
    # a bool is an int in python, but no index of a shape list: True
    # would silently be read as 1 and rewrite the point shape
    assert records_module.update_shape_points(record, True, [[5, 5]]) is False
    assert records_module.update_shape_points(record, False, [[5, 5]]) is False
    # a record without a readable label
    silent = staged_original(staging, "b.png", [rect(*BOX)])
    silent.staging_label_path = ""
    assert records_module.update_shape_points(silent, 0, [[1, 1]]) is False
    assert silent.edited is False

    assert shown_label(record) == before
    assert record.edited is False


def test_the_writer_of_a_point_set_refuses_a_point_set_that_is_already_there(
    qt_app, tmp_path
):
    "A drag that reads back the very points of the shape writes nothing."

    staging = staging_layout(str(tmp_path), "edit_writer_noop")
    record = staged_original(staging, "a.png", [rect(*BOX)])
    before = snapshot_file(record.staging_label_path)
    stored = [[8, 8], [48, 8], [48, 36], [8, 36]]

    # the very points of the shape, and the same set written as the
    # floats a canvas reads it back with
    assert records_module.update_shape_points(record, 0, stored) is False
    assert (
        records_module.update_shape_points(
            record, 0, [(float(x), float(y)) for x, y in stored]
        )
        is False
    )
    # the very same set written with the rounding of the canvas: the
    # compare runs on the numbers the writer would store, not on the
    # objects the caller handed over
    assert (
        records_module.update_shape_points(
            record,
            0,
            [
                (8.001, 8.001),
                (48.002, 8.002),
                (48.0, 36.0),
                (8.0, 36.0),
            ],
        )
        is False
    )

    assert snapshot_file(record.staging_label_path) == before
    assert record.edited is False
    # a set that really differs still lands, and only then is the record
    # marked edited and the row rewritten
    assert (
        records_module.update_shape_points(
            record, 0, [[9, 9], [49, 9], [49, 37], [9, 37]]
        )
        is True
    )
    assert record.edited is True
    assert rect_labels(record) == [
        ("car", [(9.0, 9.0), (49.0, 9.0), (49.0, 37.0), (9.0, 37.0)])
    ]


def test_the_writer_of_a_rename_refuses_a_rename_that_changes_nothing(
    qt_app, tmp_path
):
    "The very same contract for the writer of a label."

    staging = staging_layout(str(tmp_path), "edit_writer_rename_noop")
    record = staged_original(staging, "a.png", [rect(*BOX)])
    before = snapshot_file(record.staging_label_path)

    # the label the shape already carries, and a shape type that is not
    # asked for at all: neither is a write
    assert records_module.update_shape(record, 0, labels="car") is False
    assert (
        records_module.update_shape(record, 0, shape_type="rectangle") is False
    )
    assert snapshot_file(record.staging_label_path) == before
    assert record.edited is False
    # a label that really differs still lands, and only then is the
    # record marked edited and its row rewritten
    assert records_module.update_shape(record, 0, labels="bus") is True
    assert rect_labels(record)[0][0] == "bus"
    assert record.edited is True


def test_the_writer_of_a_rename_refuses_an_index_and_a_shape_it_cannot_use(
    qt_app, tmp_path
):
    "A bool index and a shape that is no dict are refused, never raised."

    # The very contract the writer of a point set has had all along: the
    # rename is reached from the double click of the canvas as well, and
    # a bool index would rewrite the wrong shape of the label while a
    # shape entry of another revision would raise an AttributeError out
    # of a Qt event handler.
    staging = staging_layout(str(tmp_path), "edit_writer_rename_refusal")
    record = staged_original(
        staging,
        "a.png",
        [
            rect(*BOX),
            {"label": "p", "shape_type": "point", "points": [[3, 3]]},
            "not a shape",
        ],
    )
    before = shown_label(record)

    # True is the integer 1: without the guard the *point* shape would be
    # the one that gains the label of the rename
    assert records_module.update_shape(record, True, labels="bus") is False
    assert records_module.update_shape(record, False, labels="bus") is False
    # an index that is no index at all
    assert records_module.update_shape(record, "x", labels="bus") is False
    # an entry of the shape list that is not a dict
    assert records_module.update_shape(record, 2, labels="bus") is False

    assert shown_label(record) == before
    assert record.edited is False


def test_a_write_that_the_file_system_refuses_never_raises(
    qt_app, tmp_path, monkeypatch
):
    "A full disk or a missing folder is answered False, never a crash."

    staging = staging_layout(str(tmp_path), "edit_writer_os_error")
    record = staged_original(staging, "a.png", [rect(*BOX)])
    before = snapshot_file(record.staging_label_path)

    def refused(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(records_module.dataset, "write_json", refused)

    # this is reached from the release of a drag inside a Qt event
    # handler, where an escaping exception aborts the whole window
    assert (
        records_module.update_shape_points(
            record, 0, [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
        )
        is False
    )
    assert records_module.update_shape(record, 0, labels="bus") is False
    assert record.edited is False
    assert snapshot_file(record.staging_label_path) == before


def test_the_writer_survives_a_label_without_an_image_size(qt_app, tmp_path):
    "An old label without the two size keys is written unclamped, not crashed."

    staging = staging_layout(str(tmp_path), "edit_writer_nosize")
    record = staged_original(staging, "a.png", [rect(*BOX)])
    data = shown_label(record)
    data.pop("imageWidth")
    data.pop("imageHeight")
    dataset.write_json(record.staging_label_path, data)

    assert (
        records_module.update_shape_points(record, 0, [(-5.0, -5.0)]) is True
    )

    assert rect_labels(record) == [("car", [(-5.0, -5.0)])]
    assert record.edited is True
