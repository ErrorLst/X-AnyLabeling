"""Results page marks: one checkbox column, two independent switches.

The left table is the export list of the run. An original carries a
*delete mark* (checked = the original, its json and its augmented
children are excluded) and an augmented copy carries a *selection mark*
(checked = the copy joins the export). Both marks start unchecked and
they are the very flags the export formula reads, so the table, the
status line, the report and the zip can never disagree.
"""

import os
import os.path as osp
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from PyQt6 import QtCore, QtWidgets

from anylabeling.custom.model_validation import dataset
from anylabeling.custom.model_validation import records as records_module
from anylabeling.custom.model_validation.exporter import export_zip
from anylabeling.custom.model_validation.ui import dialog as dialog_module
from anylabeling.custom.model_validation.ui.dialog import ModelValidationDialog
from anylabeling.custom.model_validation.ui.results_page import (
    COLUMN_AUGMENT,
    COLUMN_EXPORT,
    COLUMN_KIND,
    COLUMN_MARK,
    COLUMN_MARK_TOOLTIP,
    COLUMN_RELPATH,
    COLUMN_VERDICT,
    HEADERS,
    MARK_TOOLTIP_AUGMENTED,
    MARK_TOOLTIP_ORIGINAL,
)

CLASSES = ["a0_dian", "a1_xian"]

ORIGINALS = ("a.jpg", "b.jpg", "c.jpg")
COPIES = 2


@pytest.fixture(scope="module")
def qt_app():
    "Create the offscreen application shared by the marking tests."

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
    "Stage three originals with two augmented copies each."

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


def show_all(page) -> None:
    """Show every verdict again (the page opens on NG).

    A filter switch only asks for a rebuild: the table is rebuilt on
    the next turn of the event loop, so the events have to be driven
    once before the rows of the new filter exist.
    """

    page.filter_combo.setCurrentIndex(0)
    QtWidgets.QApplication.processEvents()


def row_of(page, record_id: str) -> int:
    "Return the table row showing one record."

    for row in range(page.table.rowCount()):
        item = page.table.item(row, COLUMN_VERDICT)
        if item is None:
            continue
        value = item.data(QtCore.Qt.ItemDataRole.UserRole)
        if str(value or "") == record_id:
            return row
    raise AssertionError("record not shown: " + record_id)


def mark_box(page, record_id: str) -> QtWidgets.QCheckBox:
    "Return the mark checkbox of one record."

    box = page.table.cellWidget(row_of(page, record_id), COLUMN_MARK)
    assert isinstance(box, QtWidgets.QCheckBox)
    return box


def find_record(records, record_id: str):
    "Return one record by id."

    for record in records:
        if record.record_id == record_id:
            return record
    raise AssertionError("unknown record: " + record_id)


def summary_of(records) -> dict:
    "Return the export counters of a record list."

    return records_module.export_summary(list(records))


# ------------------------------------------------------------- the columns
def test_the_list_shows_the_five_columns_without_a_reason_column(dialog):
    "标记 / 状态 / relpath / kind / 增强: primary_reason left the table."

    page = dialog.results_page
    assert HEADERS == ("标记", "状态", "relpath", "kind", "增强")
    assert "primary_reason" not in HEADERS
    assert "reason" not in HEADERS
    assert (
        COLUMN_MARK,
        COLUMN_VERDICT,
        COLUMN_RELPATH,
        COLUMN_KIND,
        COLUMN_AUGMENT,
    ) == (0, 1, 2, 3, 4)
    # the mark column is the export column of the previous layout
    assert COLUMN_EXPORT == COLUMN_MARK
    assert page.table.columnCount() == len(HEADERS)
    titles = [
        page.table.horizontalHeaderItem(column).text(column)
        for column in range(page.table.columnCount())
    ]
    assert titles == list(HEADERS)
    header = page.table.horizontalHeaderItem(COLUMN_MARK)
    assert header.toolTip(COLUMN_MARK) == COLUMN_MARK_TOOLTIP


def test_both_kinds_start_unmarked_and_the_flags_are_untouched(
    dialog, tmp_path
):
    "A fresh window shows every checkbox clear and moves no flag."

    staging = staging_layout(str(tmp_path), "marks_default")
    records = build_records(staging)
    dialog.records = records
    dialog.results_page.set_context(list(CLASSES), staging)
    dialog.results_page.set_records(records)
    # the results page opens on NG: this test looks at the whole list
    show_all(dialog.results_page)

    assert dialog.results_page.table.rowCount() == len(records) == 9
    for record in records:
        box = mark_box(dialog.results_page, record.record_id)
        assert box.isChecked() is False, record.record_id
        assert box.toolTip() == (
            MARK_TOOLTIP_AUGMENTED
            if record.kind == records_module.KIND_AUGMENTED
            else MARK_TOOLTIP_ORIGINAL
        )
    assert all(record.deleted is False for record in records)
    assert all(record.include_in_export is False for record in records)


def test_the_original_box_emits_the_delete_mark(dialog, tmp_path):
    "Checking an original forwards records.deleted, never the export flag."

    staging = staging_layout(str(tmp_path), "marks_original")
    records = build_records(staging)
    page = dialog.results_page
    dialog.records = records
    page.set_records(records)
    # the results page opens on NG: this test looks at the whole list
    show_all(page)
    seen = []
    page.toggle_deleted.connect(lambda ids, flag: seen.append((ids, flag)))
    page.toggle_export.connect(lambda ids, flag: seen.append((ids, flag)))

    original = find_record(records, records_module.KIND_ORIGINAL + "::a.jpg")
    mark_box(page, original.record_id).setChecked(True)

    assert seen == [([original.record_id], True)]
    assert original.deleted is True
    assert original.include_in_export is False


def test_the_augmented_box_emits_the_selection_mark(dialog, tmp_path):
    "Checking an augmented copy forwards include_in_export only."

    staging = staging_layout(str(tmp_path), "marks_augmented")
    records = build_records(staging)
    page = dialog.results_page
    dialog.records = records
    page.set_records(records)
    # the results page opens on NG: this test looks at the whole list
    show_all(page)
    seen = []
    page.toggle_deleted.connect(lambda ids, flag: seen.append((ids, flag)))
    page.toggle_export.connect(lambda ids, flag: seen.append((ids, flag)))

    child = find_record(
        records, records_module.KIND_AUGMENTED + "::a_aug1.png"
    )
    mark_box(page, child.record_id).setChecked(True)

    assert seen == [([child.record_id], True)]
    assert child.include_in_export is True
    assert child.deleted is False


# ---------------------------------------------------------- export formula
def test_the_export_list_follows_the_three_paths(dialog, tmp_path):
    """Initial, delete mark and selection mark, and the way back.

    The three paths are driven through the very checkbox the user
    clicks, so the table, the dialog wiring and the export formula are
    asserted together.
    """

    staging = staging_layout(str(tmp_path), "marks_paths")
    records = build_records(staging)
    page = dialog.results_page
    dialog.records = records
    page.set_context(list(CLASSES), staging)
    page.set_records(records)
    # the results page opens on NG: this test looks at the whole list
    show_all(page)

    # 1) initial: every original is exported, no augmented copy is
    summary = summary_of(dialog.records)
    assert summary["originals"] == 3
    assert summary["augmented"] == 0
    assert summary["excluded_unselected_augmented"] == 6

    # 2) a delete mark on a.jpg removes it and its two children
    original_a = find_record(records, records_module.KIND_ORIGINAL + "::a.jpg")
    mark_box(page, original_a.record_id).setChecked(True)
    summary = summary_of(dialog.records)
    assert summary["originals"] == 2
    assert summary["augmented"] == 0
    assert summary["excluded_deleted_originals"] == 1
    assert summary["excluded_deleted_parent_augmented"] == 2
    text = page.status_label.text()
    assert "未删原图 2" in text
    assert "勾选增强 0" in text

    # 3) a selection mark on b_aug1.png adds that copy alone
    child_b = find_record(
        records, records_module.KIND_AUGMENTED + "::b_aug1.png"
    )
    mark_box(page, child_b.record_id).setChecked(True)
    summary = summary_of(dialog.records)
    assert summary["originals"] == 2
    assert summary["augmented"] == 1
    assert "勾选增强 1" in page.status_label.text()

    # the state survives the table rebuild the dialog performs
    dialog.results_page.set_records(dialog.records)
    assert mark_box(page, original_a.record_id).isChecked() is True
    assert mark_box(page, child_b.record_id).isChecked() is True

    # 4) unchecking both restores the initial list, and the child of a
    #    restored original stays excluded until it is selected itself
    mark_box(page, original_a.record_id).setChecked(False)
    mark_box(page, child_b.record_id).setChecked(False)
    summary = summary_of(dialog.records)
    assert summary["originals"] == 3
    assert summary["augmented"] == 0
    assert summary["excluded_deleted_originals"] == 0
    assert summary["excluded_deleted_parent_augmented"] == 0
    assert summary["excluded_unselected_augmented"] == 6


def test_the_selection_only_lists_what_the_marks_ask_for(dialog, tmp_path):
    "The export selection itself, not only the counters."

    staging = staging_layout(str(tmp_path), "marks_selection")
    records = build_records(staging)
    page = dialog.results_page
    dialog.records = records
    page.set_records(records)
    # the results page opens on NG: this test looks at the whole list
    show_all(page)

    original_a = find_record(records, records_module.KIND_ORIGINAL + "::a.jpg")
    child_b = find_record(
        records, records_module.KIND_AUGMENTED + "::b_aug1.png"
    )
    mark_box(page, original_a.record_id).setChecked(True)
    mark_box(page, child_b.record_id).setChecked(True)

    selection = records_module.export_selection(dialog.records)
    assert [r.relpath for r in selection["originals"]] == ["b.jpg", "c.jpg"]
    assert [r.relpath for r in selection["augmented"]] == ["b_aug1.png"]
    assert [
        r.relpath for r in selection["excluded_deleted_parent_augmented"]
    ] == ["a_aug1.png", "a_aug2.png"]


def test_the_export_zip_holds_exactly_the_marked_records(dialog, tmp_path):
    "The archive content and the counters agree on a marked list."

    staging = staging_layout(str(tmp_path), "marks_zip")
    records = build_records(staging)
    page = dialog.results_page
    dialog.records = records
    page.set_context(list(CLASSES), staging)
    page.set_records(records)
    # the results page opens on NG: this test looks at the whole list
    show_all(page)

    original_a = find_record(records, records_module.KIND_ORIGINAL + "::a.jpg")
    child_b = find_record(
        records, records_module.KIND_AUGMENTED + "::b_aug1.png"
    )
    mark_box(page, original_a.record_id).setChecked(True)
    mark_box(page, child_b.record_id).setChecked(True)

    zip_path = str(tmp_path / "export.zip")
    report = dialog.build_report_document(zip_path)
    summary = export_zip(
        dialog.records, staging, zip_path, list(CLASSES), report
    )

    assert summary["originals"] == 2
    assert summary["augmented"] == 1
    # two entries per exported record: the picture and its one json, both
    # of them under images/ (the archive keeps no original / augmented
    # split and no LabelMe document any more)
    assert summary["zip_entries"] == 2 * 3
    assert summary["renamed_entries"] == 0
    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
    assert len(names) == 2 * 3 + 1
    assert "classes.txt" in names
    assert "validation_report.json" not in names
    for relpath in ("b.jpg", "c.jpg", "b_aug1.png"):
        stem = osp.splitext(relpath)[0]
        assert f"images/{relpath}" in names
        assert f"images/{stem}.json" in names
    assert not any("a.jpg" in name for name in names)
    assert not any("a_aug" in name for name in names)
    assert not any("b_aug2" in name for name in names)
    assert all(
        name.startswith("images/") for name in names if name != "classes.txt"
    )


def test_the_export_line_names_the_zip_and_both_counters(dialog, tmp_path):
    """The status line of a finished export is Chinese and complete.

    The line the user reads after an export carries the three things it
    always carried - the wording, the path the archive was written to and
    the two counters of the export formula - and it now spells the
    wording in Chinese: the English counter names ("originals=" /
    "augmented=") never reach the page again, they stay the keys of the
    summary and of the report.
    """

    staging = staging_layout(str(tmp_path), "marks_line")
    records = build_records(staging)
    page = dialog.results_page
    dialog.records = records
    dialog.classes = list(CLASSES)
    dialog.staging_root = staging
    page.set_context(list(CLASSES), staging)
    page.set_records(records)
    # the results page opens on NG: this test looks at the whole list
    show_all(page)

    original_a = find_record(records, records_module.KIND_ORIGINAL + "::a.jpg")
    child_b = find_record(
        records, records_module.KIND_AUGMENTED + "::b_aug1.png"
    )
    mark_box(page, original_a.record_id).setChecked(True)
    mark_box(page, child_b.record_id).setChecked(True)

    zip_path = str(tmp_path / "export.zip")
    summary = dialog.export_zip(zip_path)

    text = page.status_label.text()
    # one single line, the Chinese wording, the path and both numbers
    assert "\n" not in text
    assert text == f"导出完成：{zip_path}（原图 2 张，增强 1 张）"
    assert "originals=" not in text
    assert "augmented=" not in text
    assert summary["originals"] == 2
    assert summary["augmented"] == 1
    assert dialog.last_export_path == zip_path


class _CancelledProgress:
    """Stand in for the progress dialog the user cancels.

    The window builds its QProgressDialog inside export_zip, so a test
    that wants the real cancel path swaps the class: every call the
    export makes on the dialog is accepted and wasCanceled() reports the
    click of the user. The exporter then takes its own cancel path and
    raises its own ExportCancelled.
    """

    def __init__(self, *args, **kwargs):
        "Accept the arguments of the real dialog."

    def setWindowModality(self, *args):
        "Ignore the modality flag."

    def setAutoClose(self, *args):
        "Ignore the auto close flag."

    def setStyleSheet(self, *args):
        "Ignore the style sheet."

    def show(self):
        "Show nothing: the test runs offscreen."

    def setMaximum(self, *args):
        "Ignore the new maximum."

    def setValue(self, *args):
        "Ignore the new value."

    def setLabelText(self, text):
        "Ignore the new progress line."

    def wasCanceled(self):
        "Report the cancel click of the user."

        return True

    def close(self):
        "Close nothing: the test runs offscreen."


def test_the_export_line_names_a_cancelled_export_in_chinese(
    dialog, tmp_path, monkeypatch
):
    """A cancelled export is announced in Chinese, the half zip stays.

    The line keeps the wording and the path of the failure branch and
    replaces the English message of the cancel error with its Chinese
    one, inside the full width parentheses of the finished line. The
    half written archive is kept on purpose, the line says so.
    """

    staging = staging_layout(str(tmp_path), "marks_cancel")
    records = build_records(staging)
    page = dialog.results_page
    dialog.records = records
    dialog.classes = list(CLASSES)
    dialog.staging_root = staging
    page.set_context(list(CLASSES), staging)
    page.set_records(records)
    # the results page opens on NG: this test looks at the whole list
    show_all(page)

    child_b = find_record(
        records, records_module.KIND_AUGMENTED + "::b_aug1.png"
    )
    mark_box(page, child_b.record_id).setChecked(True)

    monkeypatch.setattr(
        dialog_module.QtWidgets, "QProgressDialog", _CancelledProgress
    )
    zip_path = str(tmp_path / "cancelled.zip")

    assert dialog.export_zip(zip_path) == {}

    text = page.status_label.text()
    assert text == (
        f"导出失败或已取消（半成品 zip 已保留）：{zip_path}"
        "（用户已取消导出）"
    )
    assert "export cancelled by the user" not in text
    # a cancelled export is not a finished one
    assert dialog.last_export_path == ""
    # the half written archive stays where the export left it
    assert osp.isfile(zip_path)


def test_the_export_line_keeps_the_raw_diagnostic_of_a_failure(
    dialog, tmp_path
):
    """A real failure keeps its own message, cancel stays Chinese.

    Only the cancel branch is translated: an error raised by the file
    system (here: a destination folder that does not exist) or by a
    library keeps its original message inside the half width
    parentheses, so the diagnostic is never translated away.
    """

    staging = staging_layout(str(tmp_path), "marks_failure")
    records = build_records(staging)
    page = dialog.results_page
    dialog.records = records
    dialog.classes = list(CLASSES)
    dialog.staging_root = staging
    page.set_context(list(CLASSES), staging)
    page.set_records(records)
    # the results page opens on NG: this test looks at the whole list
    show_all(page)

    child_b = find_record(
        records, records_module.KIND_AUGMENTED + "::b_aug1.png"
    )
    mark_box(page, child_b.record_id).setChecked(True)

    # the folder that should hold the archive does not exist, so the
    # export fails the way an unwritable destination fails
    zip_path = osp.join(str(tmp_path), "missing_folder", "export.zip")
    try:
        zipfile.ZipFile(zip_path, "w")
    except OSError as error:
        expected = str(error)
    else:  # pragma: no cover - the folder is never created here
        pytest.skip("the destination folder was created")

    # the raw diagnostic is the one of the file system, errno included:
    # nothing of it is rewritten for the page
    assert expected.startswith("[Errno")

    assert dialog.export_zip(zip_path) == {}

    text = page.status_label.text()
    assert text == (
        "导出失败或已取消（半成品 zip 已保留）：" + zip_path + f" ({expected})"
    )
    assert "用户已取消导出" not in text


# --------------------------------------------------------- batch commands
# The batch commands own no button of the page any more: they are the
# entries of the context menu of the list. The menu is built and opened
# by _show_context_menu, whose blocking exec() is replaced below by a
# recorder, so a test can read the entries and trigger one of them.
MENU_TEXTS = (
    "删除/恢复选中原图（original）",
    "加入导出（选中增强图）",
    "取消导出（选中增强图）",
    "增强图全选加入",
    "全部取消",
)


class MenuSpy:
    "Keep the menu the page builds instead of opening it."

    def __init__(self) -> None:
        self.menu = None

    def record(self, menu):
        "Remember one built menu."

        self.menu = menu


def context_menu(page, monkeypatch) -> MenuSpy:
    "Build the context menu of the list without opening it."

    spy = MenuSpy()
    monkeypatch.setattr(
        QtWidgets.QMenu, "exec", lambda menu, *_args: spy.record(menu)
    )
    page._show_context_menu(QtCore.QPoint(0, 0))
    assert spy.menu is not None
    return spy


def trigger_menu(page, monkeypatch, text: str) -> None:
    "Trigger one entry of the context menu of the list."

    spy = context_menu(page, monkeypatch)
    for action in spy.menu.actions():
        if action.text() == text:
            action.trigger()
            return
    raise AssertionError("context menu entry missing: " + text)


def test_the_batch_commands_live_on_the_context_menu(
    dialog, tmp_path, monkeypatch
):
    "The context menu drives the two marks, the page keeps no button."

    staging = staging_layout(str(tmp_path), "marks_batch")
    records = build_records(staging)
    page = dialog.results_page
    dialog.records = records
    page.set_records(records)
    # the results page opens on NG: this test looks at the whole list
    show_all(page)

    # the three batch buttons of the previous revision are gone and the
    # export is the only push button the page owns
    for name in ("select_all_button", "clear_all_button", "delete_button"):
        assert not hasattr(page, name)
    assert [
        button.text() for button in page.findChildren(QtWidgets.QPushButton)
    ] == ["导出 Zip..."]

    spy = context_menu(page, monkeypatch)
    assert (
        tuple(action.text() for action in spy.menu.actions() if action.text())
        == MENU_TEXTS
    )

    augmented = [
        r for r in dialog.records if r.kind == records_module.KIND_AUGMENTED
    ]
    originals = [
        r for r in dialog.records if r.kind == records_module.KIND_ORIGINAL
    ]
    trigger_menu(page, monkeypatch, "增强图全选加入")
    assert all(r.include_in_export for r in augmented)
    assert all(r.deleted is False for r in originals)
    assert summary_of(dialog.records)["augmented"] == 6

    trigger_menu(page, monkeypatch, "全部取消")
    assert all(not r.include_in_export for r in augmented)
    assert summary_of(dialog.records)["augmented"] == 0

    page.table.selectRow(row_of(page, originals[0].record_id))
    trigger_menu(page, monkeypatch, "删除/恢复选中原图（original）")
    assert originals[0].deleted is True
    assert summary_of(dialog.records)["originals"] == 2
    # the same entry restores what it deleted
    page.table.selectRow(row_of(page, originals[0].record_id))
    trigger_menu(page, monkeypatch, "删除/恢复选中原图（original）")
    assert originals[0].deleted is False
    assert summary_of(dialog.records)["originals"] == 3

    # a batch command never replaces the per row checkbox: the mark of a
    # single record is still set by the box of its own row
    mark_box(page, originals[1].record_id).setChecked(True)
    assert originals[1].deleted is True
    assert mark_box(page, originals[1].record_id).isChecked() is True
    assert summary_of(dialog.records)["originals"] == 2


def test_the_mark_column_survives_the_verdict_filter(dialog, tmp_path):
    "Filtering the rows never changes a mark."

    staging = staging_layout(str(tmp_path), "marks_filter")
    records = build_records(staging)
    page = dialog.results_page
    dialog.records = records
    page.set_records(records)
    # the results page opens on NG: this test looks at the whole list
    show_all(page)

    original = find_record(records, records_module.KIND_ORIGINAL + "::a.jpg")
    mark_box(page, original.record_id).setChecked(True)

    page.filter_combo.setCurrentIndex(
        page.filter_combo.findData(records_module.OK)
    )
    QtWidgets.QApplication.processEvents()
    assert original.deleted is True
    show_all(page)
    assert original.deleted is True
    assert mark_box(page, original.record_id).isChecked() is True
    assert summary_of(dialog.records)["excluded_deleted_originals"] == 1
