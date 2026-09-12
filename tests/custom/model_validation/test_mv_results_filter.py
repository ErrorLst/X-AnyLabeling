"""The filter of the results list: four entries, NG by default.

The page opens on NG: a run is looked at to find its defects, so the
list starts there while 全部 stays the first entry of the combo and one
click away. The other two entries are OK and 修改 - the records a user
really touched, see results_page.record_modified. The verdicts a run
also produces (PENDING, SKIPPED, NOT_JUDGED) have no entry of their own
any more; they stay part of 全部 and keep the status of their own row.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PyQt6 import QtWidgets

from anylabeling.custom.model_validation import records as records_module
from anylabeling.custom.model_validation.ui.results_page import (
    DEFAULT_FILTER,
    FILTER_EDITED,
    ResultsPage,
    edited_records,
    marked_record,
    record_modified,
)

# the four entries of the combo, in the order the page builds them
EXPECTED_ITEMS = (
    ("全部", ""),
    ("OK", records_module.OK),
    ("NG", records_module.NG),
    ("修改", FILTER_EDITED),
)


@pytest.fixture(scope="module")
def qt_app():
    "Create the offscreen application the filter tests need."

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def record(kind, relpath, verdict, parent_id=""):
    "Build one record of the list without touching the disk."

    row = records_module.make_record(
        kind,
        relpath,
        "/staging/" + relpath,
        "",
        parent_record_id=parent_id,
    )
    row.verdict = verdict
    return row


def shown_ids(page) -> list:
    "Return the record ids of the shown rows, in order."

    return [
        page.record_at(row).record_id for row in range(page.table.rowCount())
    ]


def widen(page) -> None:
    "Show every verdict again (the page opens on NG)."

    page.filter_combo.setCurrentIndex(0)
    QtWidgets.QApplication.processEvents()


def test_the_page_opens_on_the_ng_filter(qt_app):
    "The combo starts on NG and 全部 stays the first entry of the list."

    page = ResultsPage()
    try:
        assert DEFAULT_FILTER == records_module.NG
        assert page.filter_combo.itemData(0) == ""
        assert page.filter_combo.currentData() == records_module.NG

        records = [
            record(records_module.KIND_ORIGINAL, "a.png", records_module.NG),
            record(records_module.KIND_ORIGINAL, "b.png", records_module.OK),
            record(
                records_module.KIND_ORIGINAL, "c.png", records_module.PENDING
            ),
        ]
        page.set_records(records)

        # the list the page opens on is the NG list, not the whole run
        assert page.table.rowCount() == 1
        assert shown_ids(page) == [records[0].record_id]
        assert page.current_record() is records[0]

        widen(page)
        assert shown_ids(page) == [row.record_id for row in records]
    finally:
        page.close()


def test_the_combo_offers_the_four_entries(qt_app):
    "All / OK / NG / 修改, in that order and nothing else."

    page = ResultsPage()
    try:
        combo = page.filter_combo
        assert combo.count() == len(EXPECTED_ITEMS)
        assert [
            (combo.itemText(index), combo.itemData(index))
            for index in range(combo.count())
        ] == list(EXPECTED_ITEMS)
        # the verdicts a run also produces are shown under 全部 alone
        for verdict in (
            records_module.PENDING,
            records_module.SKIPPED,
            records_module.NOT_JUDGED,
        ):
            assert combo.findData(verdict) == -1, verdict
    finally:
        page.close()


def test_the_edited_filter_is_the_union_of_the_two_marks(qt_app):
    "A marked record is a changed record, whatever its verdict is."

    original = record(records_module.KIND_ORIGINAL, "a.png", records_module.OK)
    deleted = record(
        records_module.KIND_ORIGINAL, "b.png", records_module.PENDING
    )
    deleted.deleted = True
    child = record(
        records_module.KIND_AUGMENTED,
        "a_aug1.png",
        records_module.OK,
        original.record_id,
    )
    child.include_in_export = True
    plain = record(
        records_module.KIND_AUGMENTED,
        "a_aug2.png",
        records_module.PENDING,
        original.record_id,
    )
    records = [original, deleted, child, plain]

    assert edited_records(records) == [deleted, child]
    assert [record_modified(row) for row in records] == [
        False,
        True,
        True,
        False,
    ]
    assert [marked_record(row) for row in records] == [
        False,
        True,
        True,
        False,
    ]

    page = ResultsPage()
    try:
        page.set_records(records)
        page.filter_combo.setCurrentIndex(
            page.filter_combo.findData(FILTER_EDITED)
        )
        QtWidgets.QApplication.processEvents()

        # the verdict plays no part: the OK original with the delete mark
        # and the OK copy with the export mark are the two shown rows
        assert shown_ids(page) == [deleted.record_id, child.record_id]
        # the filter is a display rule: every record is still there
        assert page.records == records
        assert [row.deleted for row in records] == [False, True, False, False]
        assert [row.include_in_export for row in records] == [
            False,
            False,
            True,
            False,
        ]
    finally:
        page.close()


def test_a_mark_moves_a_row_in_and_out_of_the_edited_view(qt_app):
    "The view follows the flags themselves, not a snapshot of a mark."

    original = record(records_module.KIND_ORIGINAL, "a.png", records_module.NG)
    child = record(
        records_module.KIND_AUGMENTED,
        "a_aug1.png",
        records_module.NG,
        original.record_id,
    )
    page = ResultsPage()
    try:
        page.set_records([original, child])
        page.filter_combo.setCurrentIndex(
            page.filter_combo.findData(FILTER_EDITED)
        )
        QtWidgets.QApplication.processEvents()
        assert page.table.rowCount() == 0

        # the export mark of the copy shows exactly that copy
        child.include_in_export = True
        page.refresh()
        assert shown_ids(page) == [child.record_id]

        # the delete mark of the original shows it next to its copy
        original.deleted = True
        page.refresh()
        assert shown_ids(page) == [original.record_id, child.record_id]

        # and clearing both marks empties the view again
        original.deleted = False
        child.include_in_export = False
        page.refresh()
        assert page.table.rowCount() == 0
    finally:
        page.close()
