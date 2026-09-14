"""Tests of the category picker window.

The dialog is driven without ever entering its event loop: the
buttons and the list are exercised directly, and the scan of the
window runs the same way it runs in the application (main thread,
QCoreApplication.processEvents) on a real scratch folder.
"""

from __future__ import annotations

import os.path as osp
from types import SimpleNamespace
from unittest.mock import Mock

from PyQt6 import QtCore, QtWidgets

from conftest import make_image, shown_message

from anylabeling.custom.label_filter import dialog as lf_dialog
from anylabeling.custom.label_filter.core import BACKGROUND_LABEL
from anylabeling.custom.label_filter.dialog import (
    HINT_TEXT,
    IDLE_TEXT,
    ROW_FORMAT,
    SCAN_CANCEL_TEXT,
    SCAN_DONE_FORMAT,
    SCAN_EMPTY_TEXT,
    LabelFilterDialog,
)
from anylabeling.custom.label_filter.installer import install_label_filter


def build_folder(root):
    """Write the standard folder of the dialog tests."""

    make_image(root, "cat1", ["person"])
    make_image(root, "cat2", ["person", "car"])
    make_image(root, "cat3", ["car"])
    make_image(root, "cat4", None)
    return [osp.join(root, "cat%d.jpg" % index) for index in range(1, 5)]


def rows_of(dialog):
    """Return the visible rows as [(name, count, checked)]."""

    result = []
    for index in range(dialog.list_widget.count()):
        item = dialog.list_widget.item(index)
        result.append(
            (
                item.data(QtCore.Qt.ItemDataRole.UserRole),
                item.checkState() == QtCore.Qt.CheckState.Checked,
            )
        )
    return result


def prepared(widget):
    """Mount the filter on a stand-in widget that has a Tool menu.

    The mount point only works on a widget that exposes its Tool menu,
    which is what the two mount lines of the real widget give it.
    """

    widget.menus = SimpleNamespace(tool=QtWidgets.QMenu("工具"))
    install_label_filter(widget)
    return widget


def make_dialog(widget, qapp):
    """Build the window of an already prepared widget.

    A real QDialog only accepts a QWidget as its parent, so the window
    gets a small host and the stand-in widget of the tests is handed
    over on its own; the launcher does the same with the real widget.
    """

    host = QtWidgets.QWidget()
    dialog = LabelFilterDialog(host, explorer=widget)
    dialog._test_host = host
    qapp.processEvents()
    return dialog


def test_the_rows_carry_the_name_and_the_count(lf_scratch, lf_widget, qapp):
    build_folder(lf_scratch)
    widget = prepared(lf_widget(last_open_dir=lf_scratch))
    dialog = make_dialog(widget, qapp)
    try:
        texts = [
            dialog.list_widget.item(index).text()
            for index in range(dialog.list_widget.count())
        ]
        assert texts == [
            ROW_FORMAT % ("car", 2),
            ROW_FORMAT % ("person", 2),
            ROW_FORMAT % (BACKGROUND_LABEL, 1),
        ]
        assert [row[0] for row in rows_of(dialog)] == [
            "car",
            "person",
            BACKGROUND_LABEL,
        ]
        # Nothing is checked on the first opening.
        assert [row[1] for row in rows_of(dialog)] == [False, False, False]
        assert dialog.confirm_button.isEnabled()
        assert dialog.hint_label.text() == HINT_TEXT
        assert dialog.status_label.text() == SCAN_DONE_FORMAT % (4, 3)
    finally:
        dialog.close()


def test_the_search_field_filters_the_rows(lf_scratch, lf_widget, qapp):
    build_folder(lf_scratch)
    widget = prepared(lf_widget(last_open_dir=lf_scratch))
    dialog = make_dialog(widget, qapp)
    try:
        dialog.search.setText("PER")
        qapp.processEvents()
        assert [row[0] for row in rows_of(dialog)] == ["person"]
        dialog.search.setText("zzz")
        qapp.processEvents()
        assert rows_of(dialog) == []
    finally:
        dialog.close()


def test_a_search_does_not_check_a_row_the_user_cleared(
    lf_scratch, lf_widget, qapp
):
    build_folder(lf_scratch)
    widget = prepared(lf_widget(last_open_dir=lf_scratch))
    widget._label_filter_controller.apply({"car"})
    dialog = make_dialog(widget, qapp)
    try:
        # The active filter is echoed once, on the first build.
        assert dialog.checked_names() == ["car"]
        items = {}
        for index in range(dialog.list_widget.count()):
            item = dialog.list_widget.item(index)
            items[item.data(QtCore.Qt.ItemDataRole.UserRole)] = item
        items["person"].setCheckState(QtCore.Qt.CheckState.Checked)
        items["car"].setCheckState(QtCore.Qt.CheckState.Unchecked)
        qapp.processEvents()
        assert dialog.checked_names() == ["person"]

        # Typing in the search field rebuilds the rows; the category
        # the user just unchecked may not come back checked.
        dialog.search.setText("ca")
        qapp.processEvents()
        assert rows_of(dialog) == [("car", False)]
        assert dialog.checked_names() == []

        # A row the search hid keeps the check state it had.
        dialog.search.setText("")
        qapp.processEvents()
        assert [row[1] for row in rows_of(dialog)] == [False, True, False]
    finally:
        dialog.close()


def test_select_all_invert_and_clear(lf_scratch, lf_widget, qapp):
    build_folder(lf_scratch)
    widget = prepared(lf_widget(last_open_dir=lf_scratch))
    dialog = make_dialog(widget, qapp)
    try:
        dialog.select_all()
        assert [row[1] for row in rows_of(dialog)] == [True, True, True]
        dialog.invert()
        assert [row[1] for row in rows_of(dialog)] == [False, False, False]
        dialog.invert()
        assert [row[1] for row in rows_of(dialog)] == [True, True, True]
        dialog.clear_checks()
        assert [row[1] for row in rows_of(dialog)] == [False, False, False]
    finally:
        dialog.close()


def test_select_all_only_touches_the_visible_rows(
    lf_scratch, lf_widget, qapp
):
    build_folder(lf_scratch)
    widget = prepared(lf_widget(last_open_dir=lf_scratch))
    dialog = make_dialog(widget, qapp)
    try:
        dialog.search.setText("car")
        qapp.processEvents()
        assert [row[0] for row in rows_of(dialog)] == ["car"]
        dialog.select_all()
        assert dialog.checked_names() == ["car"]
        # The two rows the search hid were not touched by the button.
        assert [row[2] for row in dialog.rows()] == [True, False, False]

        # A click on a checkbox goes through the same bookkeeping.
        item = dialog.list_widget.item(0)
        item.setCheckState(QtCore.Qt.CheckState.Unchecked)
        qapp.processEvents()
        assert dialog.rows() == [
            ("car", 2, False),
            ("person", 2, False),
            (BACKGROUND_LABEL, 1, False),
        ]
    finally:
        dialog.close()


def test_confirming_hands_the_checked_names_to_the_controller(
    lf_scratch, lf_widget, qapp
):
    build_folder(lf_scratch)
    widget = prepared(lf_widget(last_open_dir=lf_scratch))
    dialog = make_dialog(widget, qapp)
    try:
        dialog.list_widget.item(1).setCheckState(
            QtCore.Qt.CheckState.Checked
        )
        assert dialog.confirm() is True
        state = widget._label_filter_state
        assert state["selected"] == frozenset({"person"})
        assert widget.file_list_widget.count() == 2
        assert dialog.result() == int(
            QtWidgets.QDialog.DialogCode.Accepted
        )
    finally:
        dialog.close()


def test_confirming_without_a_check_clears_the_filter(
    lf_scratch, lf_widget, qapp
):
    build_folder(lf_scratch)
    widget = prepared(lf_widget(last_open_dir=lf_scratch))
    controller = widget._label_filter_controller
    controller.apply({BACKGROUND_LABEL})
    assert widget.file_list_widget.count() == 1
    dialog = make_dialog(widget, qapp)
    try:
        # The category of the active filter is shown as checked, but
        # nothing else is: the file list still holds one image.
        assert dialog.checked_names() == [BACKGROUND_LABEL]
        assert widget.file_list_widget.count() == 1
        dialog.clear_checks()
        assert dialog.confirm() is True
        assert widget._label_filter_state is None
        assert widget.file_list_widget.count() == 4
        assert shown_message(widget) == lf_dialog.CLEARED_TEXT
    finally:
        dialog.close()


def test_an_open_filter_is_shown_again(lf_scratch, lf_widget, qapp):
    build_folder(lf_scratch)
    widget = prepared(lf_widget(last_open_dir=lf_scratch))
    widget._label_filter_controller.apply({"car"})
    dialog = make_dialog(widget, qapp)
    try:
        assert [row[1] for row in rows_of(dialog)] == [True, False, False]
    finally:
        dialog.close()


def test_the_clear_button_drops_the_filter(lf_scratch, lf_widget, qapp):
    build_folder(lf_scratch)
    widget = prepared(lf_widget(last_open_dir=lf_scratch))
    widget._label_filter_controller.apply({BACKGROUND_LABEL})
    dialog = make_dialog(widget, qapp)
    try:
        assert dialog.clear_filter() is True
        assert widget._label_filter_state is None
        assert widget.file_list_widget.count() == 4
        assert shown_message(widget) == lf_dialog.CLEARED_TEXT
    finally:
        dialog.close()


def test_an_empty_confirmation_reports_a_cleared_filter(
    lf_scratch, lf_widget, qapp
):
    build_folder(lf_scratch)
    widget = prepared(lf_widget(last_open_dir=lf_scratch))
    widget._label_filter_controller.apply({"person"})
    dialog = make_dialog(widget, qapp)
    try:
        # Confirming with nothing checked is "clear the filter", not a
        # filter of zero categories.
        dialog.clear_checks()
        assert dialog.confirm() is True
        assert dialog.status_label.text() == lf_dialog.CLEARED_TEXT
        assert "0 个分类" not in dialog.status_label.text()
        assert widget._label_filter_state is None
        assert widget.file_list_widget.count() == 4
    finally:
        dialog.close()


def test_a_cancelled_clear_keeps_the_window_open(lf_scratch, lf_widget,
                                                 qapp):
    build_folder(lf_scratch)
    widget = prepared(lf_widget(last_open_dir=lf_scratch))
    controller = widget._label_filter_controller
    controller.apply({"person"})
    dialog = make_dialog(widget, qapp)
    try:
        # The rebuild of the clear path asks the unsaved-annotations
        # question and the user refuses: the list is still the filtered
        # one, so the button may not close the window as if the filter
        # were gone.
        widget.may_continue = Mock(return_value=False)
        assert dialog.clear_filter() is False
        assert dialog.result() == int(
            QtWidgets.QDialog.DialogCode.Rejected
        )
        assert controller.state() == {
            "dir": osp.normpath(osp.abspath(lf_scratch)),
            "selected": frozenset({"person"}),
        }
        assert widget.file_list_widget.count() == 2
        assert dialog.status_label.text() == lf_dialog.NO_SELECTION_TEXT
    finally:
        dialog.close()


def test_a_cancelled_scan_changes_nothing(lf_scratch, lf_widget, qapp,
                                          monkeypatch):
    build_folder(lf_scratch)
    widget = prepared(lf_widget(last_open_dir=lf_scratch))
    controller = widget._label_filter_controller
    seen = {}

    def cancelled_scan(progress_cb=None, should_stop=None):
        seen["called"] = True
        # The window asks once and the user cancels: the scanner then
        # reports a stopped scan, which is what None means.
        seen["stopped"] = bool(should_stop and should_stop())
        return None

    monkeypatch.setattr(controller, "scan", cancelled_scan)
    dialog = make_dialog(widget, qapp)
    try:
        assert seen["called"] is True
        assert seen["stopped"] is False
        assert rows_of(dialog) == []
        assert dialog.status_label.text() == lf_dialog.SCAN_CANCEL_TEXT
        assert controller.state() is None
        assert widget.file_list_widget.count() == 0
    finally:
        dialog.close()


def test_without_a_folder_the_window_says_so(lf_widget, qapp):
    widget = prepared(lf_widget(last_open_dir=None))
    dialog = make_dialog(widget, qapp)
    try:
        assert dialog.status_label.text() == IDLE_TEXT
        assert dialog.rows() == []
        assert dialog.list_widget.count() == 0
        assert not dialog.confirm_button.isEnabled()
        assert dialog.confirm() is False
    finally:
        dialog.close()


def test_an_empty_folder_disables_the_primary_button(
    lf_scratch, lf_widget, qapp
):
    empty = osp.join(lf_scratch, "empty")
    import os
    os.makedirs(empty)
    widget = prepared(lf_widget(last_open_dir=empty))
    dialog = make_dialog(widget, qapp)
    try:
        # Even an empty folder has the one category of the tool: the
        # image that carries no annotation. There is simply nothing to
        # filter, so the primary button stays disabled.
        assert dialog.status_label.text() == SCAN_EMPTY_TEXT
        assert dialog.rows() == [(BACKGROUND_LABEL, 0, False)]
        assert not dialog.confirm_button.isEnabled()
        assert dialog.confirm() is False
        state = getattr(widget, "_label_filter_state", None)
        assert state is None
    finally:
        dialog.close()
