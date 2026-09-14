"""Tests of a window that is opened a second time.

The launcher keeps one window per widget for the whole session, so the
same instance is shown again after another folder was opened. These
tests drive the launcher - the entry the Tool menu uses - and check
that a reused window describes the folder the widget holds now, that
its first opening still scans exactly once, and that a scan the user
cancels on a second opening leaves the active filter alone.
"""

from __future__ import annotations

import os.path as osp

from PyQt6 import QtCore, QtWidgets

from conftest import make_image

from anylabeling.custom.label_filter import dialog as lf_dialog
from anylabeling.custom.label_filter.core import BACKGROUND_LABEL
from anylabeling.custom.label_filter.installer import (
    LabelFilterController,
)
from anylabeling.custom.label_filter.launcher import launch_label_filter


def build_folder(root, entries):
    """Write one image (and its json) per (stem, labels) entry."""

    for stem, labels in entries:
        make_image(root, stem, labels)


def categories_of(dialog):
    """Return the category names the list shows, in order."""

    names = []
    for index in range(dialog.list_widget.count()):
        item = dialog.list_widget.item(index)
        names.append(item.data(QtCore.Qt.ItemDataRole.UserRole))
    return names


def checked_of(dialog):
    """Return the category names the list shows as checked."""

    names = []
    for index in range(dialog.list_widget.count()):
        item = dialog.list_widget.item(index)
        if item.checkState() == QtCore.Qt.CheckState.Checked:
            names.append(item.data(QtCore.Qt.ItemDataRole.UserRole))
    return names


def make_host(last_open_dir, output_dir=None, selected=None):
    """Return a real QWidget the launcher can own a window for.

    The launcher hands the widget over as the Qt parent of the window
    and the window reads the folder and the active filter of that
    widget, so the stand-in has to be a real QWidget carrying a real
    controller. Only the folder and the filter state are set here:
    these tests never apply a filter through the file list.
    """

    host = QtWidgets.QWidget()
    host.last_open_dir = last_open_dir
    host.output_dir = output_dir
    host._label_filter_controller = LabelFilterController(host)
    if selected is not None:
        host._label_filter_state = {
            "dir": osp.normpath(osp.abspath(last_open_dir)),
            "selected": frozenset(selected),
        }
    return host


def test_reopening_the_window_shows_the_new_folder(
    lf_make_scratch, qapp
):
    first = lf_make_scratch()
    second = lf_make_scratch()
    build_folder(first, [("cat1", ["person"])])
    build_folder(
        second, [("cat2", ["car"]), ("cat3", ["car"]), ("cat4", None)]
    )
    host = make_host(first)
    dialog = launch_label_filter(host)
    try:
        assert categories_of(dialog) == ["person", BACKGROUND_LABEL]
        dialog.close()
        qapp.processEvents()

        # The session switched to another folder in between: the
        # launcher reuses the window, whose rows have to follow.
        host.last_open_dir = second
        again = launch_label_filter(host)

        assert again is dialog
        assert categories_of(dialog) == ["car", BACKGROUND_LABEL]
        assert dialog.rows() == [
            ("car", 2, False),
            (BACKGROUND_LABEL, 1, False),
        ]
        assert dialog.status_label.text() == lf_dialog.SCAN_DONE_FORMAT % (
            3,
            2,
        )
    finally:
        dialog.close()


def test_reopening_the_window_echoes_the_active_filter(
    lf_make_scratch, qapp
):
    root = lf_make_scratch()
    build_folder(root, [("cat1", ["person"]), ("cat2", ["car"])])
    host = make_host(root)
    dialog = launch_label_filter(host)
    try:
        assert checked_of(dialog) == []
        dialog.close()
        qapp.processEvents()

        # A filter was applied while the window was closed: the reused
        # window echoes it on the categories of the folder it scans.
        host._label_filter_state = {
            "dir": osp.normpath(osp.abspath(root)),
            "selected": frozenset({"person"}),
        }
        launch_label_filter(host)

        assert checked_of(dialog) == ["person"]
        assert categories_of(dialog) == [
            "car",
            "person",
            BACKGROUND_LABEL,
        ]
    finally:
        dialog.close()


def test_the_first_opening_scans_the_folder_once(
    lf_make_scratch, monkeypatch
):
    root = lf_make_scratch()
    build_folder(root, [("cat1", ["person"])])
    host = make_host(root)
    controller = host._label_filter_controller
    real_scan = controller.scan
    calls = []

    def counting_scan(*args, **kwargs):
        calls.append(True)
        return real_scan(*args, **kwargs)

    monkeypatch.setattr(controller, "scan", counting_scan)
    dialog = launch_label_filter(host)
    try:
        assert len(calls) == 1
        assert categories_of(dialog) == ["person", BACKGROUND_LABEL]
    finally:
        dialog.close()


def test_a_cancelled_rescan_keeps_the_filter_alone(
    lf_make_scratch, qapp, monkeypatch
):
    root = lf_make_scratch()
    build_folder(root, [("cat1", ["person"]), ("cat2", ["car"])])
    host = make_host(root, selected={"car"})
    dialog = launch_label_filter(host)
    try:
        assert checked_of(dialog) == ["car"]
        dialog.close()
        qapp.processEvents()

        controller = host._label_filter_controller

        def cancelled_scan(progress_cb=None, should_stop=None):
            return None

        monkeypatch.setattr(controller, "scan", cancelled_scan)
        launch_label_filter(host)

        # A cancelled scan changes nothing, and the window ends up in
        # the state a window that never scanned is in: no category and
        # no primary action - in particular the active filter may not
        # be cleared by a confirmation that has nothing to act on.
        assert dialog.rows() == []
        assert dialog.list_widget.count() == 0
        assert dialog.status_label.text() == lf_dialog.SCAN_CANCEL_TEXT
        assert not dialog.confirm_button.isEnabled()
        assert dialog.confirm() is False
        assert controller.state() == {
            "dir": osp.normpath(osp.abspath(root)),
            "selected": frozenset({"car"}),
        }
    finally:
        dialog.close()
