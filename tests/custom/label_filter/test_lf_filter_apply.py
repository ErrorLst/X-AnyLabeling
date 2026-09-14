"""Tests of the mount point, the state and the file list wrapper.

The fake widget of the conftest carries the real import_image_folder
of LabelingWidget, so every test here runs the upstream method first
and checks what the wrapper does around it: which rows survive, how
the index map is rebuilt, and which folder call resets the filter.
"""

from __future__ import annotations

import os.path as osp
import time
from types import SimpleNamespace
from unittest.mock import Mock, call

from PyQt6 import QtCore, QtWidgets

from conftest import make_image, shown_message, write_image, write_json

from anylabeling.custom.label_filter import core
from anylabeling.custom.label_filter.core import BACKGROUND_LABEL
from anylabeling.custom.label_filter.installer import (
    ACTIVE_TIP_FORMAT,
    ACTION_NAME,
    ACTION_TIP,
    CLEAR_TEXT,
    RESET_TEXT,
    install_label_filter,
)


def text_set(widget):
    """Return the rows of the file list as a set of names."""

    items = widget.file_list_widget
    return {items.item(row).text() for row in range(items.count())}


def cancel_the_question(widget):
    """Answer the unsaved-changes question the way a cancel does.

    The real folder import runs unchanged here and asks the question
    itself: this only makes that question return False. The method then
    returns on the spot - before the folder, the list or the current
    file is touched - which is exactly the state the wrapper has to
    notice.
    """

    widget.may_continue = Mock(return_value=False)
    return widget


def _bind_open_next_image(widget):
    """Put the real open_next_image back on a fake widget."""

    from types import MethodType

    import anylabeling.views.labeling.label_widget as label_widget

    method = label_widget.LabelingWidget.open_next_image
    widget.open_next_image = MethodType(method, widget)
    return widget


def bind_selection_change(widget):
    """Wire the real selection handler the way the widget does.

    The stand-in of the conftest carries a real file list but does not
    connect itemSelectionChanged on its own, so the tests that ask "did
    moving the current row load the file again?" have to wire the
    upstream handler exactly like LabelingWidget.__init__ does.
    """

    from types import MethodType

    import anylabeling.views.labeling.label_widget as label_widget

    method = label_widget.LabelingWidget.file_selection_changed
    widget.file_selection_changed = MethodType(method, widget)
    # The slot is reached through a plain callable: PyQt6 cannot take
    # a bound method of a stand-in that is not a QObject as a slot.
    widget.file_list_widget.itemSelectionChanged.connect(
        lambda: widget.file_selection_changed()
    )
    # The real handler rebuilds the attribute panel when the widget
    # carries attributes; the stand-in has none, so None keeps that
    # branch out of the way.
    widget.attributes = None
    return widget


def open_the_folder(widget, root):
    """Open a folder the way the application does, on its first file.

    The navigation and the selection handler are the real upstream
    ones, so the state a test starts from - list built, first file on
    the canvas - is the one the wrapper really meets.
    """

    _bind_open_next_image(widget)
    bind_selection_change(widget)
    widget.import_image_folder(root)
    return widget


def build_folder(root):
    """Write the standard five images of the wrapper tests."""

    make_image(root, "img1", ["person"])
    make_image(root, "img2", [BACKGROUND_LABEL])
    make_image(root, "img3", ["car"])
    make_image(root, "img4", None)
    make_image(root, "img5", ["person", "car"])
    return [osp.join(root, "img%d.jpg" % index) for index in range(1, 6)]


def fake_menu():
    """Return a real Tool menu, so the action is a real QAction."""

    menu = QtWidgets.QMenu("工具")
    return menu


def attach_menu(widget, menu):
    widget.menus = SimpleNamespace(tool=menu)
    return widget


class RecordingListWidget(QtWidgets.QListWidget):
    """A file list that records how the post filter edits it.

    The timing of one pass depends on the machine, the edits it makes
    to the list do not: both counters start empty and are cleared once
    the fixture has built its rows.
    """

    def __init__(self):
        super().__init__()
        self.taken_rows = []
        self.updates_enabled = []

    def takeItem(self, row):
        self.taken_rows.append(row)
        return super().takeItem(row)

    def setUpdatesEnabled(self, enabled):
        self.updates_enabled.append(bool(enabled))
        return super().setUpdatesEnabled(enabled)


def test_install_is_idempotent(lf_widget):
    menu = fake_menu()
    widget = attach_menu(lf_widget(), menu)

    first = install_label_filter(widget)
    second = install_label_filter(widget)

    assert first is second
    assert menu.actions() == [first]
    assert first.text() == ACTION_NAME
    assert first.toolTip() == ACTION_TIP
    assert widget._label_filter_import_wrapped is True
    assert widget._label_filter_controller is not None


def test_install_without_a_tool_menu_does_nothing(lf_widget):
    widget = lf_widget()
    assert install_label_filter(widget) is None
    assert not hasattr(widget, "_label_filter_import_wrapped")


def test_the_wrapper_is_a_pure_passthrough_while_inactive(
    lf_scratch, lf_widget
):
    files = build_folder(lf_scratch)
    menu = fake_menu()
    widget = attach_menu(lf_widget(), menu)
    install_label_filter(widget)
    original = widget.import_image_folder
    calls = []

    def spy(dirpath, pattern=None, load=True):
        calls.append((dirpath, pattern, load))
        return original(dirpath, pattern=pattern, load=load)

    # The wrapper reads the original from the instance, so a spy put
    # in front of it sees exactly what the wrapper forwards. While no
    # filter is active the wrapper forwards the call untouched.
    widget.import_image_folder = spy
    widget.import_image_folder(lf_scratch)
    assert calls == [(lf_scratch, None, True)]
    assert text_set(widget) == set(files)
    assert widget.last_open_dir == lf_scratch


def test_an_active_filter_drops_the_rows_that_miss(lf_scratch, lf_widget):
    files = build_folder(lf_scratch)
    widget = attach_menu(lf_widget(last_open_dir=lf_scratch), fake_menu())
    install_label_filter(widget)
    controller = widget._label_filter_controller

    assert controller.apply({BACKGROUND_LABEL})
    assert text_set(widget) == {files[1], files[3]}
    assert len(widget.fn_to_index) == 2
    # The index map has to match the shortened list, not the folder.
    assert widget.fn_to_index[files[1]] == 0
    assert widget.fn_to_index[files[3]] == 1
    # No file was on screen before the filter, so the navigation of
    # the widget is asked for the first surviving row, with the load
    # the caller passed: the method is stubbed here, the tests below
    # cover the real one and the canvas itself.
    assert widget.file_list_widget.item(0).text() == files[1]
    widget.open_next_image.assert_called_with(load=True)
    assert "显示 2/5 张" in shown_message(widget)


def test_an_empty_canvas_opens_the_first_survivor(
    lf_scratch, lf_widget
):
    """Without a shown file the apply has to load a hit."""

    files = build_folder(lf_scratch)
    widget = attach_menu(lf_widget(last_open_dir=lf_scratch), fake_menu())
    install_label_filter(widget)
    controller = widget._label_filter_controller
    # The real method, so that the wrapper is checked against what the
    # widget really does with the shortened list.
    _bind_open_next_image(widget)

    # Nothing was on screen before the filter (the fixture never
    # opened a file), so the canvas has to end on the first surviving
    # row - and that one is loaded, because the filter is what the
    # user asked to see.
    assert controller.apply({BACKGROUND_LABEL})
    assert widget.filename == files[1]
    widget.load_file.assert_called_once_with(files[1])

    # The navigation of the widget walks the shortened list for real:
    # from the first survivor it opens the next one.
    widget.open_next_image(load=True)
    assert widget.filename == files[3]
    assert widget.load_file.call_args_list == [
        call(files[1]),
        call(files[3]),
    ]


def test_a_filtered_out_image_is_replaced_by_a_hit(
    lf_scratch, lf_widget
):
    """The canvas may not stay on a file the filter dropped."""

    files = build_folder(lf_scratch)
    widget = attach_menu(lf_widget(last_open_dir=lf_scratch), fake_menu())
    install_label_filter(widget)
    controller = widget._label_filter_controller
    open_the_folder(widget, lf_scratch)
    assert widget.filename == files[0]
    widget.load_file.reset_mock()

    # img1 carries "person" only, while the selection keeps the
    # background images: the shown file is dropped, so the canvas has
    # to move to the first hit, and that move loads it.
    assert controller.apply({BACKGROUND_LABEL})
    assert text_set(widget) == {files[1], files[3]}
    assert widget.filename == files[1]
    widget.load_file.assert_called_once_with(files[1])


def test_a_kept_image_is_not_reloaded(lf_scratch, lf_widget):
    """A file the selection keeps stays on screen, without a reload."""

    files = build_folder(lf_scratch)
    widget = attach_menu(lf_widget(last_open_dir=lf_scratch), fake_menu())
    install_label_filter(widget)
    controller = widget._label_filter_controller
    open_the_folder(widget, lf_scratch)
    assert widget.filename == files[0]
    widget.load_file.reset_mock()

    # img1 carries "person", so the selection keeps the very file the
    # canvas shows. The real selection handler of the list is wired
    # here: moving the current row without blocking its signals would
    # turn into a load of the same file right away.
    assert controller.apply({"person"})
    assert text_set(widget) == {files[0], files[4]}
    widget.load_file.assert_not_called()
    assert widget.filename == files[0]
    row = widget.fn_to_index[files[0]]
    assert widget.file_list_widget.currentRow() == row


def test_a_load_false_reimport_leaves_the_row_to_the_caller(
    lf_scratch, lf_widget
):
    """A caller that passed load=False owns the current row."""

    files = build_folder(lf_scratch)
    widget = attach_menu(lf_widget(last_open_dir=lf_scratch), fake_menu())
    install_label_filter(widget)
    controller = widget._label_filter_controller
    open_the_folder(widget, lf_scratch)
    controller.apply({"person"})
    widget.open_next_image(load=True)
    assert widget.filename == files[4]
    widget.load_file.reset_mock()

    # change_output_dir_dialog re-imports with load=False and then
    # sets the current row itself, which is what makes the widget
    # load the annotations of the new folder. The pass may not take
    # that row over first, so it leaves the upstream behaviour -
    # first surviving row as filename, no row, no load - in place.
    widget.import_image_folder(lf_scratch, load=False)

    assert widget.filename == files[0]
    assert widget.file_list_widget.currentRow() == -1
    widget.load_file.assert_not_called()


def test_a_filter_without_a_hit_loads_nothing(lf_scratch, lf_widget):
    """An empty result may neither raise nor load a file."""

    build_folder(lf_scratch)
    widget = attach_menu(lf_widget(last_open_dir=lf_scratch), fake_menu())
    install_label_filter(widget)
    controller = widget._label_filter_controller
    open_the_folder(widget, lf_scratch)
    widget.load_file.reset_mock()

    # No category matches, so the list is empty and the canvas keeps
    # what it shows - the way upstream behaves on an empty folder -
    # and nothing is loaded on the way.
    assert controller.apply({"nothing"})
    assert widget.file_list_widget.count() == 0
    assert widget.fn_to_index == {}
    assert widget.filename is None
    widget.load_file.assert_not_called()


def test_the_selection_is_an_or_of_categories(lf_scratch, lf_widget):
    files = build_folder(lf_scratch)
    widget = attach_menu(lf_widget(last_open_dir=lf_scratch), fake_menu())
    install_label_filter(widget)
    controller = widget._label_filter_controller

    assert controller.apply({"car", "person"})
    assert text_set(widget) == {files[0], files[2], files[4]}


def test_a_filter_that_matches_nothing_leaves_an_empty_list(
    lf_scratch, lf_widget
):
    build_folder(lf_scratch)
    widget = attach_menu(lf_widget(last_open_dir=lf_scratch), fake_menu())
    install_label_filter(widget)
    controller = widget._label_filter_controller

    assert controller.apply({"nothing"})
    assert text_set(widget) == set()
    assert widget.file_list_widget.count() == 0
    assert widget.fn_to_index == {}
    assert widget.filename is None
    widget.open_next_image.assert_called_with(load=True)


def test_applying_reloads_the_list_and_filters_it(lf_scratch, lf_widget):
    build_folder(lf_scratch)
    widget = attach_menu(lf_widget(last_open_dir=lf_scratch), fake_menu())
    install_label_filter(widget)
    controller = widget._label_filter_controller

    assert widget.file_list_widget.count() == 0
    assert controller.apply({BACKGROUND_LABEL})
    assert widget.file_list_widget.count() == 2
    assert controller.state() == {
        "dir": osp.normpath(osp.abspath(lf_scratch)),
        "selected": frozenset({BACKGROUND_LABEL}),
    }


def test_opening_another_folder_resets_the_filter(
    lf_scratch, lf_make_scratch, lf_widget
):
    other = lf_make_scratch()
    write_image(other, "else.jpg")
    write_json(other, "else.json", shapes=[{"label": "cat"}])
    build_folder(lf_scratch)
    menu = fake_menu()
    widget = attach_menu(lf_widget(last_open_dir=lf_scratch), menu)
    install_label_filter(widget)
    controller = widget._label_filter_controller
    controller.apply({BACKGROUND_LABEL})
    assert widget.file_list_widget.count() == 2

    widget.import_image_folder(other)

    assert controller.state() is None
    assert text_set(widget) == {osp.join(other, "else.jpg")}
    assert shown_message(widget) == RESET_TEXT
    assert widget._label_filter_action.toolTip() == ACTION_TIP


def test_a_cancelled_folder_switch_keeps_the_filter(
    lf_scratch, lf_make_scratch, lf_widget
):
    other = lf_make_scratch()
    write_image(other, "else.jpg")
    write_json(other, "else.json", shapes=[{"label": "cat"}])
    build_folder(lf_scratch)
    widget = attach_menu(lf_widget(last_open_dir=lf_scratch), fake_menu())
    install_label_filter(widget)
    controller = widget._label_filter_controller
    controller.apply({BACKGROUND_LABEL})
    before = controller.state()
    tip_before = widget._label_filter_action.toolTip()
    message_before = shown_message(widget)

    # The question of the folder import is refused, so upstream
    # returns on the spot: the file list still holds the filtered rows
    # of the old folder and the filter may not be dropped - nor may
    # the tooltip or the status bar claim a reset that never ran.
    cancel_the_question(widget)
    widget.import_image_folder(other)

    assert controller.state() == before
    assert widget.file_list_widget.count() == 2
    assert widget.last_open_dir == lf_scratch
    assert widget._label_filter_action.toolTip() == tip_before
    assert shown_message(widget) == message_before
    assert shown_message(widget) != RESET_TEXT


def test_clearing_the_filter_shows_every_file_again(lf_scratch, lf_widget):
    files = build_folder(lf_scratch)
    widget = attach_menu(lf_widget(last_open_dir=lf_scratch), fake_menu())
    install_label_filter(widget)
    controller = widget._label_filter_controller
    controller.apply({BACKGROUND_LABEL})
    assert widget.file_list_widget.count() == 2

    assert controller.reset()
    assert controller.state() is None
    assert text_set(widget) == set(files)
    assert shown_message(widget) == CLEAR_TEXT


def test_clearing_the_filter_keeps_the_shown_image(
    lf_scratch, lf_widget
):
    """Clearing the filter leaves the canvas on the file it shows."""

    files = build_folder(lf_scratch)
    widget = attach_menu(lf_widget(last_open_dir=lf_scratch), fake_menu())
    install_label_filter(widget)
    controller = widget._label_filter_controller
    open_the_folder(widget, lf_scratch)
    controller.apply({BACKGROUND_LABEL})
    assert widget.filename == files[1]
    widget.load_file.reset_mock()

    # The shown file is part of the full list again, so the clear only
    # has to make it the current row: loading it again would flicker,
    # and leaving the list on another row would make the navigation
    # start from the wrong image.
    assert controller.reset()
    assert text_set(widget) == set(files)
    assert widget.filename == files[1]
    widget.load_file.assert_not_called()
    row = widget.fn_to_index[files[1]]
    assert widget.file_list_widget.currentRow() == row


def test_a_cancelled_clear_keeps_the_filter(lf_scratch, lf_widget):
    build_folder(lf_scratch)
    widget = attach_menu(lf_widget(last_open_dir=lf_scratch), fake_menu())
    install_label_filter(widget)
    controller = widget._label_filter_controller
    controller.apply({BACKGROUND_LABEL})
    before = controller.state()
    tip_before = widget._label_filter_action.toolTip()
    message_before = shown_message(widget)

    # reset() rebuilds the folder through the upstream method it took
    # over. The refused question stops that rebuild on the spot, so
    # the old filtered list stays and nothing may be reported as
    # cleared: the state and the tooltip are put back untouched.
    cancel_the_question(widget)

    assert controller.reset() is False
    assert controller.state() == before
    assert widget.file_list_widget.count() == 2
    assert widget._label_filter_action.toolTip() == tip_before
    assert shown_message(widget) == message_before
    assert shown_message(widget) != CLEAR_TEXT


def test_applying_an_empty_selection_clears_the_filter(
    lf_scratch, lf_widget
):
    build_folder(lf_scratch)
    widget = attach_menu(lf_widget(last_open_dir=lf_scratch), fake_menu())
    install_label_filter(widget)
    controller = widget._label_filter_controller
    controller.apply({BACKGROUND_LABEL})

    assert controller.apply(set())
    assert controller.state() is None
    assert widget.file_list_widget.count() == 5
    assert shown_message(widget) == CLEAR_TEXT


def test_apply_without_an_open_folder_reports_and_returns_false(
    lf_widget
):
    widget = attach_menu(lf_widget(), fake_menu())
    install_label_filter(widget)
    controller = widget._label_filter_controller

    assert controller.apply({"person"}) is False
    assert controller.state() is None
    assert "请先打开一个图片目录" == shown_message(widget)


def test_a_cancelled_folder_call_rolls_the_selection_back(
    lf_scratch, lf_widget
):
    widget = attach_menu(
        lf_widget(last_open_dir=lf_scratch), fake_menu()
    )
    install_label_filter(widget)
    controller = widget._label_filter_controller
    widget.file_list_widget.addItem("kept.jpg")
    widget.filename = "kept.jpg"

    # The real method runs and asks may_continue() before it touches
    # anything; the user refuses, so the method returns on the spot and
    # the wrapper has to report the failure instead of filtering a
    # stale list.
    cancel_the_question(widget)
    assert controller.apply({"person"}) is False
    assert controller.state() is None
    assert widget.file_list_widget.item(0).text() == "kept.jpg"
    assert widget.filename == "kept.jpg"
    assert "操作已取消" in shown_message(widget)


def test_a_rollback_keeps_the_previous_filter(lf_scratch, lf_widget):
    build_folder(lf_scratch)
    widget = attach_menu(lf_widget(last_open_dir=lf_scratch), fake_menu())
    install_label_filter(widget)
    controller = widget._label_filter_controller
    controller.apply({BACKGROUND_LABEL})
    before = controller.state()
    # The next folder call is refused by the same question: the
    # wrapper has to roll the new selection back and leave the list
    # and the remembered folder as the previous filter left them.
    cancel_the_question(widget)

    assert controller.apply({"car"}) is False
    assert controller.state() == before
    assert widget.file_list_widget.count() == 2
    assert widget._label_filter_apply_failed is False


def test_the_user_search_pattern_is_kept(lf_scratch, lf_widget):
    build_folder(lf_scratch)
    widget = attach_menu(lf_widget(last_open_dir=lf_scratch), fake_menu())
    install_label_filter(widget)
    controller = widget._label_filter_controller
    controller.apply({"person"})
    assert widget.file_list_widget.count() == 2

    # The search bar of the widget calls the folder import with its
    # own pattern; the wrapper has to combine both, it may not drop
    # the pattern of the user.
    widget.import_image_folder(lf_scratch, pattern="img1", load=False)

    assert text_set(widget) == {osp.join(lf_scratch, "img1.jpg")}
    assert widget.file_list_widget.count() == 1
    widget.open_next_image.assert_called_with(load=False)


def test_a_pattern_that_matches_no_file_is_safe(lf_scratch, lf_widget):
    build_folder(lf_scratch)
    widget = attach_menu(lf_widget(last_open_dir=lf_scratch), fake_menu())
    install_label_filter(widget)
    controller = widget._label_filter_controller
    controller.apply({"person"})

    widget.import_image_folder(
        lf_scratch, pattern="no-such-image", load=False
    )
    assert widget.file_list_widget.count() == 0
    assert widget.filename is None


def test_the_wrapper_stacks_with_another_one(lf_scratch, lf_widget):
    build_folder(lf_scratch)
    widget = attach_menu(lf_widget(last_open_dir=lf_scratch), fake_menu())
    lower_calls = []
    original = widget.import_image_folder

    def lower(dirpath, pattern=None, load=True):
        lower_calls.append(dirpath)
        return original(dirpath, pattern=pattern, load=load)

    widget.import_image_folder = lower
    widget._lower_wrapped = True
    install_label_filter(widget)
    controller = widget._label_filter_controller

    assert controller.apply({"car"})
    assert lower_calls == [lf_scratch]
    assert text_set(widget) == {
        osp.join(lf_scratch, "img3.jpg"),
        osp.join(lf_scratch, "img5.jpg"),
    }


def test_the_tooltip_reports_the_active_filter(lf_scratch, lf_widget):
    build_folder(lf_scratch)
    widget = attach_menu(lf_widget(last_open_dir=lf_scratch), fake_menu())
    install_label_filter(widget)
    controller = widget._label_filter_controller

    controller.apply({BACKGROUND_LABEL})
    assert widget._label_filter_action.toolTip() == ACTIVE_TIP_FORMAT % (
        ACTION_TIP,
        1,
        2,
    )
    controller.reset()
    assert widget._label_filter_action.toolTip() == ACTION_TIP


def test_the_post_filter_stays_fast_on_ten_thousand_rows(
    lf_make_scratch, lf_widget, monkeypatch
):
    """The pass has to stay linear on a big folder.

    Ten thousand rows are built on the file list, one image out of
    every three carrying a label the selection does not keep. The
    pass is judged on what it does to the list, not on the clock:
    every row is classified exactly once and exactly the rows that
    miss are taken out, which is the shape of a linear pass and
    cannot turn red just because the machine is busy. The wall clock
    stays only as a very loose smoke guard, never as a benchmark.
    """

    items = 10000
    keep_every = 3
    root = lf_make_scratch()
    names = []
    kept = []
    dropped = 0
    for index in range(items):
        labels = ["person"] if index % keep_every else ["car"]
        image, _label = make_image(root, "img%d" % index, labels)
        names.append(image)
        if index % keep_every:
            kept.append(image)
        else:
            dropped += 1
    file_list = RecordingListWidget()
    file_list.setUpdatesEnabled(False)
    try:
        for name in names:
            item = QtWidgets.QListWidgetItem(name)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, False)
            file_list.addItem(item)
    finally:
        file_list.setUpdatesEnabled(True)
    # The rows built here are the fixture of the test, not part of
    # what the pass does to the list.
    file_list.taken_rows.clear()
    file_list.updates_enabled.clear()
    widget = lf_widget(last_open_dir=root)
    widget.file_list_widget = file_list
    widget.fn_to_index = {}
    # The widget starts on an image the selection keeps, which is
    # the case the pass has to leave alone: the file stays on the
    # canvas and only the row moves.
    widget.filename = kept[0]
    install_label_filter(attach_menu(widget, fake_menu()))
    controller = widget._label_filter_controller

    classified = []
    real_is_hit = core.is_hit

    def counting_is_hit(image_path, output_dir, selected, cache):
        classified.append(image_path)
        return real_is_hit(image_path, output_dir, selected, cache)

    monkeypatch.setattr(core, "is_hit", counting_is_hit)

    started = time.monotonic()
    assert controller.apply({"person"})
    elapsed = time.monotonic() - started

    # Linear classification: one look at every row, none of them
    # twice, whatever the machine is doing.
    assert len(classified) == items
    assert len(set(classified)) == items
    # Linear deletion: one takeItem per dropped row, not per row, and
    # the repaints are batched around the whole pass.
    assert len(file_list.taken_rows) == dropped
    assert file_list.updates_enabled == [False, True]
    assert widget.file_list_widget.count() == items - dropped
    assert set(widget.fn_to_index) == set(kept)
    # Smoke guard only: a busy machine can add seconds to the pass,
    # so the limit is far above the interactive range on purpose.
    assert elapsed < 30.0
