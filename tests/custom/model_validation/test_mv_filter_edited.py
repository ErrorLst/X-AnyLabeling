"""The 修改 filter: every record a user really touched.

The combo of the list offers four entries - 全部, OK, NG and 修改 - and
修改 is the union of the four ways a record can leave its own run: a
corrected ROI (records.edited), a corrected label (records.edited as
well), the soft delete mark of an original and the export mark of an
augmented copy (records.deleted / records.include_in_export). The
filter reads those flags themselves, through results_page.record_modified,
and never keeps a list of its own.

The verdicts a run also produces - PENDING, SKIPPED, NOT_JUDGED - have
no entry of their own any more: they stay part of 全部 and keep the
status of their own row.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PyQt6 import QtWidgets

from anylabeling.custom.model_validation import records as records_module
from anylabeling.custom.model_validation.ui.results_page import (
    COLUMN_VERDICT,
    DEFAULT_FILTER,
    FILTER_EDITED,
    ResultsPage,
    edited_records,
    record_modified,
)

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


def show_edited(page) -> None:
    "Select the 修改 entry of the filter combo."

    page.filter_combo.setCurrentIndex(
        page.filter_combo.findData(FILTER_EDITED)
    )
    QtWidgets.QApplication.processEvents()


def test_the_combo_offers_four_entries_and_opens_on_ng(qt_app):
    "The four entries, in order, with NG selected out of the box."

    page = ResultsPage()
    try:
        combo = page.filter_combo
        assert combo.count() == 4
        assert [
            (combo.itemText(index), combo.itemData(index))
            for index in range(combo.count())
        ] == list(EXPECTED_ITEMS)
        assert DEFAULT_FILTER == records_module.NG
        assert combo.currentData() == records_module.NG
        # the dropped verdicts have no entry of their own any more
        for verdict in (
            records_module.PENDING,
            records_module.SKIPPED,
            records_module.NOT_JUDGED,
        ):
            assert combo.findData(verdict) == -1, verdict
    finally:
        page.close()


def test_the_edited_filter_is_the_union_of_the_four_touches(qt_app):
    "A corrected ROI, a corrected label and the two marks are one view."

    untouched = record(
        records_module.KIND_ORIGINAL, "a.png", records_module.PENDING
    )
    roi = record(records_module.KIND_ORIGINAL, "b.png", records_module.PENDING)
    roi.edited = True  # a moved or resized box
    label = record(records_module.KIND_ORIGINAL, "c.png", records_module.NG)
    label.edited = True  # a renamed label
    deleted = record(records_module.KIND_ORIGINAL, "d.png", records_module.OK)
    deleted.deleted = True
    child = record(
        records_module.KIND_AUGMENTED,
        "a_aug1.png",
        records_module.SKIPPED,
        untouched.record_id,
    )
    child.include_in_export = True
    untouched_child = record(
        records_module.KIND_AUGMENTED,
        "a_aug2.png",
        records_module.PENDING,
        untouched.record_id,
    )
    records = [untouched, roi, label, deleted, child, untouched_child]

    assert [record_modified(row) for row in records] == [
        False,
        True,
        True,
        True,
        True,
        False,
    ]
    assert edited_records(records) == [roi, label, deleted, child]

    page = ResultsPage()
    try:
        page.set_records(records)
        show_edited(page)

        assert shown_ids(page) == [
            roi.record_id,
            label.record_id,
            deleted.record_id,
            child.record_id,
        ]
        # the filter is a display rule: every record is still there
        assert page.records == records
    finally:
        page.close()


def test_a_touch_moves_a_row_in_and_out_of_the_edited_view(qt_app):
    "The view follows the flags themselves, not a snapshot of a touch."

    plain = record(records_module.KIND_ORIGINAL, "a.png", records_module.NG)
    child = record(
        records_module.KIND_AUGMENTED,
        "a_aug1.png",
        records_module.NG,
        plain.record_id,
    )
    page = ResultsPage()
    try:
        page.set_records([plain, child])
        show_edited(page)
        assert page.table.rowCount() == 0

        # a corrected label of the record shows exactly that record
        plain.edited = True
        page.refresh()
        assert shown_ids(page) == [plain.record_id]

        # and so does the export mark of the copy
        child.include_in_export = True
        page.refresh()
        assert shown_ids(page) == [plain.record_id, child.record_id]

        # a record that is marked but never edited is shown as well
        plain.edited = False
        plain.deleted = True
        page.refresh()
        assert shown_ids(page) == [plain.record_id, child.record_id]

        # and clearing every flag empties the view again
        plain.deleted = False
        child.include_in_export = False
        page.refresh()
        assert page.table.rowCount() == 0
        assert record_modified(plain) is False
    finally:
        page.close()


def test_a_verdict_without_an_entry_stays_part_of_the_whole_list(qt_app):
    "PENDING, SKIPPED and NOT_JUDGED are shown under 全部 alone."

    rows = [
        record(records_module.KIND_ORIGINAL, "a.png", records_module.PENDING),
        record(records_module.KIND_ORIGINAL, "b.png", records_module.SKIPPED),
        record(
            records_module.KIND_ORIGINAL, "c.png", records_module.NOT_JUDGED
        ),
        record(records_module.KIND_ORIGINAL, "d.png", records_module.OK),
    ]
    page = ResultsPage()
    try:
        page.set_records(rows)
        # the page opens on NG, which none of these rows carries
        assert page.table.rowCount() == 0

        page.filter_combo.setCurrentIndex(0)
        QtWidgets.QApplication.processEvents()

        assert shown_ids(page) == [row.record_id for row in rows]
        # every one of them keeps the status of its own row
        assert [
            page.table.item(row, COLUMN_VERDICT).text()
            for row in range(len(rows))
        ] == [
            records_module.PENDING,
            records_module.SKIPPED,
            records_module.NOT_JUDGED,
            records_module.OK,
        ]
    finally:
        page.close()


def test_the_edited_view_shows_the_note_of_an_edited_row(qt_app):
    "A changed row says so, whatever filter shows it."

    row = record(records_module.KIND_ORIGINAL, "a.png", records_module.NG)
    row.edited = True
    page = ResultsPage()
    try:
        page.set_records([row])
        show_edited(page)

        assert shown_ids(page) == [row.record_id]
        assert page.table.item(0, COLUMN_VERDICT).text() == "NG (edited)"
    finally:
        page.close()
