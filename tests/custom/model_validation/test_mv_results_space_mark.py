"""The space mark of the results page: one key, the current row.

The keyboard of the results page used to be the A / D walk alone. The
space bar is the second gesture of the list: it marks - and unmarks - the
picture of the current row, exactly like a click on the checkbox of that
row, and it never toggles the checkbox or the button that happens to hold
the focus.

The page keeps no state of its own here: toggle_current_mark picks the
target state of the flag the export formula reads and emits the very
signals the checkbox emits (toggle_deleted for an original, toggle_export
for an augmented copy), so the dialog stays the single writer of the
marks and of the counters. The tests below pin the gesture: which page
the shortcut belongs to, both kinds of mark, the empty page, the double
trigger and the sync of the row with the summary line.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PyQt6 import QtCore, QtWidgets
from PyQt6.QtTest import QTest

from anylabeling.custom.model_validation import records as records_module
from anylabeling.custom.model_validation.ui.dialog import (
    ModelValidationDialog,
)
from anylabeling.custom.model_validation.ui.results_page import (
    COLUMN_MARK,
    SHORTCUT_HINT,
    SHORTCUT_TOOLTIP,
    ResultsPage,
)


@pytest.fixture(scope="module")
def qt_app():
    "Create the offscreen application shared by the space tests."

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def synthetic_records():
    "Return one original and its augmented copy, without touching disk."

    original = records_module.make_record(
        records_module.KIND_ORIGINAL, "a.png", ""
    )
    original.verdict = records_module.OK
    child = records_module.make_record(
        records_module.KIND_AUGMENTED,
        "a_aug1.png",
        "",
        parent_record_id=original.record_id,
    )
    child.verdict = records_module.OK
    return [original, child]


def row_of(page, record_id: str) -> int:
    "Return the shown row of one record."

    for row in range(page.table.rowCount()):
        box = page.table.cellWidget(row, COLUMN_MARK)
        if box is not None and box.property("recordId") == record_id:
            return row
    raise AssertionError("record not shown: " + record_id)


def press(widget) -> None:
    "Send the space bar to one widget of the page and let Qt deliver it."

    QTest.keyClick(widget, QtCore.Qt.Key.Key_Space)
    QtWidgets.QApplication.processEvents()


@pytest.fixture
def dialog(qt_app):
    "A shown window holding one original and its augmented copy."

    window = ModelValidationDialog()
    records = synthetic_records()
    window.records = records
    page = window.results_page
    page.resize(1024, 640)
    # the page opens on NG and these tests look at the whole list
    page.filter_combo.setCurrentIndex(0)
    page.set_records(records)
    window.refresh_export_summary()
    window.show()
    window.stack.setCurrentWidget(page)
    page.table.setFocus()
    QtWidgets.QApplication.processEvents()
    try:
        yield window, records
    finally:
        window.close()


def test_the_space_shortcut_belongs_to_the_page_and_its_children(qt_app):
    "The space is a page shortcut, and the page writes it down."

    page = ResultsPage()
    try:
        assert page.mark_shortcut.key().toString() == "Space"
        assert page.mark_shortcut.parent() is page
        assert page.mark_shortcut.context() == (
            QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        # the hint names the key, and it stays the one text of the page
        assert "空格" in page.shortcut_hint.text()
        assert page.shortcut_hint.text() == SHORTCUT_HINT
        assert "空格" in page.shortcut_hint.toolTip()
        assert "空格" in SHORTCUT_TOOLTIP
    finally:
        page.close()


def test_space_marks_and_unmarks_the_current_original(qt_app, dialog):
    "One press of the key marks the original, the next one restores it."

    window, records = dialog
    page = window.results_page
    original = records[0]
    seen = []
    page.toggle_deleted.connect(
        lambda record_ids, flag: seen.append((list(record_ids), flag))
    )
    assert row_of(page, original.record_id) == 0
    assert page.current_record() is original
    assert original.deleted is False

    press(page.table)

    assert original.deleted is True
    assert seen == [([original.record_id], True)]

    press(page.table)

    assert original.deleted is False
    assert seen == [
        ([original.record_id], True),
        ([original.record_id], False),
    ]

    # the slot answers every press it was given
    assert page.toggle_current_mark() is True
    assert original.deleted is True


def test_space_uses_the_selection_mark_of_an_augmented_copy(
    qt_app, dialog
):
    "An augmented copy only ever moves include_in_export."

    window, records = dialog
    page = window.results_page
    deleted = []
    exported = []
    page.toggle_deleted.connect(
        lambda record_ids, flag: deleted.append((list(record_ids), flag))
    )
    page.toggle_export.connect(
        lambda record_ids, flag: exported.append((list(record_ids), flag))
    )
    child = records[1]
    row = row_of(page, child.record_id)
    page.table.setCurrentItem(page.table.topLevelItem(row))
    QtWidgets.QApplication.processEvents()
    assert page.current_record() is child
    assert child.kind == records_module.KIND_AUGMENTED
    assert child.deleted is False
    assert child.include_in_export is False

    press(page.table)

    assert child.include_in_export is True
    assert child.deleted is False
    assert exported == [([child.record_id], True)]
    assert deleted == []

    press(page.table)

    assert child.include_in_export is False
    assert exported == [
        ([child.record_id], True),
        ([child.record_id], False),
    ]


def test_space_on_an_empty_page_changes_nothing(qt_app):
    "A page with no current row marks nothing and never raises."

    page = ResultsPage()
    deleted = []
    exported = []
    page.toggle_deleted.connect(
        lambda record_ids, flag: deleted.append((list(record_ids), flag))
    )
    page.toggle_export.connect(
        lambda record_ids, flag: exported.append((list(record_ids), flag))
    )
    page.resize(1024, 640)
    page.show()
    page.table.setFocus()
    QtWidgets.QApplication.processEvents()
    try:
        assert page.table.rowCount() == 0
        assert page.current_record() is None
        assert page.toggle_current_mark() is False
        press(page.table)
        assert deleted == []
        assert exported == []
    finally:
        page.close()


def test_space_wins_over_a_focused_checkbox_and_button(qt_app, dialog):
    "The key marks the current row once, never the focused control."

    window, records = dialog
    page = window.results_page
    original = records[0]
    row = row_of(page, original.record_id)
    box = page.table.cellWidget(row, COLUMN_MARK)
    flips = []
    page.toggle_deleted.connect(
        lambda record_ids, flag: flips.append(flag)
    )
    # a space that leaked into the button would open the export dialog of
    # the window, so the test watches the signal instead of the dialog
    page.export_requested.disconnect()
    clicked = []
    page.export_button.clicked.connect(clicked.append)

    box.setFocus()
    QtWidgets.QApplication.processEvents()
    assert box.hasFocus() is True

    press(box)

    # the checkbox did not toggle itself: the flag moved exactly once
    assert original.deleted is True
    assert flips == [True]
    assert box.isChecked() is True

    page.export_button.setFocus()
    QtWidgets.QApplication.processEvents()
    assert page.export_button.hasFocus() is True

    press(page.export_button)

    assert original.deleted is False
    assert flips == [True, False]
    assert box.isChecked() is False
    assert clicked == []


def test_the_space_mark_keeps_the_row_and_the_summary_in_step(
    qt_app, dialog
):
    "The checkbox of the row and the export counters follow the key."

    window, records = dialog
    page = window.results_page
    original = records[0]
    row = row_of(page, original.record_id)
    box = page.table.cellWidget(row, COLUMN_MARK)

    window.refresh_export_summary()
    assert "未删原图 1" in page.status_label.text()

    press(page.table)

    assert original.deleted is True
    assert box.isChecked() is True
    window.refresh_export_summary()
    assert "未删原图 0" in page.status_label.text()
    assert "软删除原图 1" in page.status_label.text()

    press(page.table)

    assert original.deleted is False
    assert box.isChecked() is False
    window.refresh_export_summary()
    assert "未删原图 1" in page.status_label.text()
    assert "软删除原图 0" in page.status_label.text()
