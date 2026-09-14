"""Point updates of the mark column: a tick never rebuilds the list.

The mark column is the export list of a run, and ticking one checkbox
used to rebuild every row of the table: the dialog answered the signal
with set_records(), which cleared the tree and created one item and one
checkbox per record again. On a real run of more than a thousand records
that blocks the window for hundreds of milliseconds and throws the
scroll position away, so the user loses the place of the list after every
single click.

The page now rewrites the rows that really changed - the record whose
mark moved plus, for an original, its augmented children - and nothing
else: the items, the checkboxes, the scroll position and the selection of
every other row survive the click. The tests below pin the two costs
against each other on a 1500 record list, so a future change that brings
the rebuild back is caught by a number and not by a feeling.
"""

import os
import os.path as osp
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PyQt6 import QtCore, QtWidgets

from anylabeling.custom.model_validation import records as records_module
from anylabeling.custom.model_validation.ui.dialog import ModelValidationDialog
from anylabeling.custom.model_validation.ui.results_page import (
    COLUMN_KIND,
    COLUMN_MARK,
    COLUMN_RELPATH,
    COLUMN_VERDICT,
    ResultsPage,
)

# 750 originals with one augmented copy each: 1500 records, the size of
# the run the user reported the freeze on.
ORIGINALS = 750
COPIES = 1


@pytest.fixture(scope="module")
def qt_app():
    "Create the offscreen application shared by the update tests."

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def synthetic_records(count: int = ORIGINALS):
    "Return a realistic record list without touching the disk."

    records = []
    for index in range(count):
        relpath = f"a{index:04d}_dian.png"
        original = records_module.make_record(
            records_module.KIND_ORIGINAL,
            relpath,
            f"/staging/original/images/{relpath}",
        )
        original.verdict = (
            records_module.NG if index % 3 == 0 else records_module.OK
        )
        records.append(original)
        for copy_index in range(1, COPIES + 1):
            child_relpath = f"a{index:04d}_dian_aug{copy_index}.png"
            records.append(
                records_module.make_record(
                    records_module.KIND_AUGMENTED,
                    child_relpath,
                    f"/staging/augmented/images/{child_relpath}",
                    parent_record_id=original.record_id,
                )
            )
    return records


def row_of(page, record_id: str) -> int:
    "Return the shown row of one record."

    for row in range(page.table.rowCount()):
        item = page.table.item(row, COLUMN_VERDICT)
        if item is None:
            continue
        if str(item.data(QtCore.Qt.ItemDataRole.UserRole) or "") == record_id:
            return row
    raise AssertionError("record not shown: " + record_id)


def shown_ids(page):
    "Return the record ids of the shown rows, in order."

    return [
        str(
            page.table.item(row, COLUMN_VERDICT).data(
                QtCore.Qt.ItemDataRole.UserRole
            )
        )
        for row in range(page.table.rowCount())
    ]


def legacy_toggle(dialog, record_id: str, deleted: bool) -> None:
    """Rebuild the whole list for one mark, the way it used to happen.

    This is the code path of the revision before the point update:
    on_toggle_deleted moved the flag and then handed the complete record
    list back to the page, which cleared the tree and built every row
    again. It is kept here as the reference the new path is measured
    against - the cost of one click of the previous revision.
    """

    records_module.set_deleted(dialog.records, [record_id], deleted)
    dialog.results_page.set_records(dialog.records)
    dialog.refresh_export_summary()


def milliseconds(call, repeats: int = 3) -> float:
    "Return the best of a few runs of one call, in milliseconds."

    best = None
    for _ in range(repeats):
        start = time.perf_counter()
        call()
        elapsed = (time.perf_counter() - start) * 1000.0
        best = elapsed if best is None else min(best, elapsed)
    return float(best)


@pytest.fixture
def page(qt_app):
    "A results page showing a realistic 1500 record list."

    widget = ResultsPage()
    records = synthetic_records()
    widget.resize(1024, 640)
    widget.set_records(records)
    # the page opens on NG and this fixture looks at the whole list
    widget.filter_combo.setCurrentIndex(0)
    widget.show()
    QtWidgets.QApplication.processEvents()
    try:
        yield widget, records
    finally:
        widget.close()


@pytest.fixture
def dialog(qt_app):
    "A model validation window holding the same 1500 record list."

    window = ModelValidationDialog()
    records = synthetic_records()
    window.records = records
    page = window.results_page
    page.resize(1024, 640)
    page.set_records(records)
    # the page opens on NG and this fixture looks at the whole list
    page.filter_combo.setCurrentIndex(0)
    window.refresh_export_summary()
    page.show()
    QtWidgets.QApplication.processEvents()
    try:
        yield window, records
    finally:
        window.close()


# ------------------------------------------------------- the affected rows
def test_the_affected_set_is_the_record_and_its_children(qt_app):
    "One original, its two children, and nothing else of the list."

    records = synthetic_records(3)
    original, child = records[0], records[1]
    assert child.parent_record_id == original.record_id

    assert records_module.affected_record_ids(
        records, [original.record_id]
    ) == [
        original.record_id,
        child.record_id,
    ]
    # an augmented record has no children of its own
    assert records_module.affected_record_ids(records, [child.record_id]) == [
        child.record_id
    ]
    # an unknown id is carried through instead of blowing up
    assert records_module.affected_record_ids(records, ["missing"]) == [
        "missing"
    ]


def test_a_point_update_writes_the_rows_it_is_given(page):
    "The mark, the parent note and the strike out follow the flag."

    widget, records = page
    original, child = records[0], records[1]
    row = row_of(widget, original.record_id)
    child_row = row_of(widget, child.record_id)
    box = widget.table.cellWidget(row, COLUMN_MARK)
    child_box = widget.table.cellWidget(child_row, COLUMN_MARK)
    verdict_item = widget.table.item(row, COLUMN_VERDICT)
    child_item = widget.table.item(child_row, COLUMN_RELPATH)

    records_module.set_deleted(records, [original.record_id], True)
    rewritten = widget.refresh_rows(
        records_module.affected_record_ids(records, [original.record_id])
    )

    assert rewritten == [row, child_row]
    # the very same widgets and cells: no row was created again
    assert widget.table.cellWidget(row, COLUMN_MARK) is box
    assert widget.table.cellWidget(child_row, COLUMN_MARK) is child_box
    assert widget.table.item(row, COLUMN_VERDICT) is verdict_item
    assert widget.table.item(child_row, COLUMN_RELPATH) is child_item
    assert box.isChecked() is True
    assert child_box.isChecked() is False
    assert verdict_item.font(COLUMN_VERDICT).strikeOut() is True
    assert "父图已删除" in child_item.text(COLUMN_RELPATH)
    grey = child_item.foreground(COLUMN_RELPATH).color()
    assert (grey.red(), grey.green(), grey.blue()) == (140, 140, 140)

    # the way back clears both paints again
    records_module.set_deleted(records, [original.record_id], False)
    widget.refresh_rows(
        records_module.affected_record_ids(records, [original.record_id])
    )
    assert box.isChecked() is False
    assert verdict_item.font(COLUMN_VERDICT).strikeOut() is False
    assert "父图已删除" not in child_item.text(COLUMN_RELPATH)
    assert (
        child_item.foreground(COLUMN_RELPATH).style()
        == QtCore.Qt.BrushStyle.NoBrush
    )


def test_a_point_update_touches_no_other_row(page):
    "Every row but the affected one keeps its item and its checkbox."

    widget, records = page
    original = records[0]
    row = row_of(widget, original.record_id)
    before = shown_ids(widget)
    items = {
        index: widget.table.topLevelItem(index)
        for index in (0, row + 1, widget.table.rowCount() - 1)
    }
    widgets = {
        index: widget.table.cellWidget(index, COLUMN_MARK) for index in items
    }
    row_count = widget.table.rowCount()

    records_module.set_deleted(records, [original.record_id], True)
    widget.refresh_rows(
        records_module.affected_record_ids(records, [original.record_id])
    )

    assert widget.table.rowCount() == row_count
    assert shown_ids(widget) == before
    for index, item in items.items():
        assert widget.table.topLevelItem(index) is item
        assert widget.table.cellWidget(index, COLUMN_MARK) is widgets[index]


# --------------------------------------------------- the visible rebuild
def test_a_visible_rebuild_keeps_the_table_hidden(page, monkeypatch):
    """Every row is created while the table is off screen.

    A row added to a tree that is on screen is laid out and painted,
    which costs about a hundred times more than the same row added to a
    hidden one (minutes against milliseconds on this very list). Pinned
    here is the behaviour - not a timing - so the regression is caught
    on any machine: the page is on screen before and after, and not one
    of the 1500 rows is created while it is.
    """

    widget, records = page
    table = widget.table
    assert table.isVisible() is True
    seen = []
    real_fill = widget._fill_row

    def fill_row(row, record, parent_deleted=None):
        seen.append(table.isVisible())
        return real_fill(row, record, parent_deleted=parent_deleted)

    monkeypatch.setattr(widget, "_fill_row", fill_row)
    widget.refresh()

    assert len(seen) == len(records) == ORIGINALS * (COPIES + 1)
    assert set(seen) == {False}
    assert table.isVisible() is True


def test_a_visible_rebuild_is_cheap(page):
    """1500 records: a rebuild of a page on screen stays cheap.

    The bound is deliberately loose - it is a freeze detector, not a
    benchmark: rebuilding the same list row by row with the table on
    screen took minutes here (the fixture of this file alone spent
    minutes in the first processEvents()), while the rebuild that hides
    the table takes a fraction of a second.
    """

    widget, _records = page
    assert widget.table.isVisible() is True
    elapsed = milliseconds(widget.refresh, repeats=1)
    assert elapsed < 2000.0, elapsed
    assert widget.table.rowCount() == ORIGINALS * (COPIES + 1)


def test_a_visible_rebuild_keeps_the_place_and_the_keyboard(page):
    "The hidden rebuild gives the selection, the scroll and the focus back."

    widget, records = page
    table = widget.table
    bar = table.verticalScrollBar()
    target = 700
    table.setCurrentItem(table.topLevelItem(target))
    table.scrollToItem(table.topLevelItem(target))
    table.setFocus()
    QtWidgets.QApplication.processEvents()
    before = bar.value()
    assert before > 0, "the list has to be scrolled for this assertion"
    assert table.hasFocus() is True
    selected = widget.record_at(target).record_id

    widget.refresh()

    # the place of the user and the keyboard of the A / D navigation
    # survive the rebuild the filter switch asks for
    assert bar.value() == before
    assert table.currentRow() == target
    assert widget.record_at(table.currentRow()).record_id == selected
    assert table.hasFocus() is True
    assert table.isVisible() is True


def test_a_rebuild_of_a_hidden_page_shows_nothing(page):
    "A page that is not on screen is rebuilt without being shown."

    widget, records = page
    widget.hide()
    QtWidgets.QApplication.processEvents()
    assert widget.table.isVisible() is False

    widget.refresh()

    assert widget.table.isVisible() is False
    assert widget.table.rowCount() == len(records)


# --------------------------------------------------------- the user gesture
def test_the_scroll_position_survives_a_tick_in_the_middle(dialog):
    "The click of the user leaves the view where it was."

    window, records = dialog
    page = window.results_page
    table = page.table
    bar = table.verticalScrollBar()
    target = 700
    table.setCurrentItem(table.topLevelItem(target))
    table.scrollToItem(table.topLevelItem(target))
    QtWidgets.QApplication.processEvents()
    before = bar.value()
    assert before > 0, "the list has to be scrolled for this assertion"
    selected = table.currentRow()
    assert selected == target
    original = page.record_at(target)
    box = table.cellWidget(target, COLUMN_MARK)

    box.setChecked(True)
    QtWidgets.QApplication.processEvents()

    assert original.deleted is True
    assert bar.value() == before
    assert table.currentRow() == selected
    assert table.cellWidget(target, COLUMN_MARK) is box
    assert box.isChecked() is True
    # the counters followed the click
    assert "未删原图 749" in page.status_label.text()


def test_a_tick_costs_a_fraction_of_the_rebuild(dialog):
    """1500 records: the point update against the rebuild of a click.

    Both numbers are measured on the very same list: the rebuild is what
    the previous revision did for one tick (move the flag, hand the whole
    list to the page, let it clear and rebuild every row), the point
    update is what the revision does now. The exact milliseconds depend
    on the machine, the ratio does not: rewriting two rows can not come
    anywhere near rebuilding fifteen hundred.
    """

    window, records = dialog
    page = window.results_page
    table = page.table
    target = page.record_at(700).record_id
    assert table.rowCount() == len(records) == ORIGINALS * (COPIES + 1)

    flag = True
    rebuild_ms = milliseconds(lambda: legacy_toggle(window, target, flag))
    point_ms = milliseconds(lambda: window.on_toggle_deleted([target], flag))

    # the list is intact after both paths and the two costs are the two
    # orders of magnitude the user feels as "freezes" and as "instant"
    assert table.rowCount() == len(records)
    assert point_ms < rebuild_ms / 10.0, (rebuild_ms, point_ms)
    assert point_ms < 50.0, point_ms
    assert rebuild_ms > 5.0, rebuild_ms

    # and the rows that were rewritten are the affected two
    affected = records_module.affected_record_ids(records, [target])
    assert len(affected) == 1 + COPIES
    assert page.refresh_rows(affected) == sorted(
        row_of(page, record_id) for record_id in affected
    )


def test_the_export_counters_follow_the_point_update(dialog):
    "The status line is text: it stays updated on the cheap path."

    window, records = dialog
    page = window.results_page
    original = records[0]
    child = records[1]

    assert "未删原图 750" in page.status_label.text()
    window.on_toggle_deleted([original.record_id], True)
    assert "未删原图 749" in page.status_label.text()
    assert "软删除原图 1" in page.status_label.text()
    assert "因父图删除排除的增强 1" in page.status_label.text()

    window.on_toggle_export([child.record_id], True)
    assert "勾选增强 0" in page.status_label.text()

    window.on_toggle_deleted([original.record_id], False)
    window.on_toggle_export([child.record_id], True)
    assert "未删原图 750" in page.status_label.text()
    assert "勾选增强 1" in page.status_label.text()
