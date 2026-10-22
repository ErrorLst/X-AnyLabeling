"""Tests of the preview dialog: filter, picking, keyboard, closing.

The window is driven through its own methods and through synthetic key
events; a directory scan runs in its real thread, so the few tests that
open a folder wait for the entries with a timeout. Every dialog is
closed at the end of its test, which also stops the background jobs.
"""

import os
import time
from types import SimpleNamespace

import pytest

from PyQt6 import QtCore, QtGui, QtWidgets

from anylabeling.custom.preview_tool import core, list_panel
from anylabeling.custom.preview_tool import dialog as pt_dialog
from anylabeling.custom.preview_tool.worker import SourceSnapshot

from conftest import (
    flush_settings,
    make_settings,
    make_shape,
    snapshot,
    trash_listing,
    write_image,
    write_json,
)


def _key(key, kind=QtCore.QEvent.Type.KeyPress):
    """Build a key event with no modifier."""

    return QtGui.QKeyEvent(
        kind, key, QtCore.Qt.KeyboardModifier.NoModifier
    )


def _drop_event(*paths):
    """Build a stand in drop event carrying local paths."""

    mime = QtCore.QMimeData()
    mime.setUrls([QtCore.QUrl.fromLocalFile(path) for path in paths])
    state = {"accepted": False, "ignored": False}
    event = SimpleNamespace(
        mimeData=lambda: mime,
        acceptProposedAction=lambda: state.update(accepted=True),
        ignore=lambda: state.update(ignored=True),
    )
    return event, state


def _entry(directory, name, shapes=(), rel_dir=""):
    """Build one core.ImageEntry of a directory tree, without a disk."""

    shapes = tuple(shapes)
    if rel_dir:
        path = os.path.join(directory, rel_dir, name)
    else:
        path = os.path.join(directory, name)
    return core.ImageEntry(
        path=path,
        name=name,
        json_path=None,
        labels=frozenset(shape.label for shape in shapes),
        shapes=shapes,
        rel_dir=rel_dir,
    )


def _rectangle(label="cat", score=0.9, size=40.0):
    """Build one rectangle big enough for the default thresholds."""

    return core.ShapeInfo(
        label=label,
        score=score,
        shape_type="rectangle",
        points=((0.0, 0.0), (size, size)),
        width=size,
        height=size,
    )


def _wait_entries(dialog, timeout=10.0):
    """Process events until the scan of the dialog delivered entries."""

    app = QtWidgets.QApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if app is not None:
            app.processEvents()
        if dialog._entries:
            return True
        time.sleep(0.005)
    return bool(dialog._entries)


def _wait_picked(dialog, keys, timeout=10.0):
    """Process events until the picked key set of the dialog is keys."""

    expected = frozenset(keys)
    app = QtWidgets.QApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if app is not None:
            app.processEvents()
        if dialog._picked == expected and not dialog._pick_busy():
            return True
        time.sleep(0.005)
    return dialog._picked == expected


@pytest.fixture
def opened():
    """Factory of dialogs, all closed and released at the end."""

    dialogs = []

    def _make(settings=None, explorer=None):
        dialog = pt_dialog.PreviewDialog(None, explorer, settings)
        dialog.resize(900, 600)
        dialogs.append(dialog)
        return dialog

    yield _make
    for dialog in dialogs:
        try:
            dialog.close()
        except Exception:
            pass
        dialog.deleteLater()


def _open_entries(dialog, directory, names, shapes=()):
    """Feed a dialog with synthetic entries of one directory."""

    entries = [_entry(directory, name, shapes) for name in names]
    dialog._directory = directory
    dialog._on_scanned(
        SourceSnapshot(directory, entries, len(entries), False)
    )
    return entries


# ---------------------------------------------------------------- filter


def test_build_filter_follows_the_master_switch(opened, pt_store):
    settings, _ini = pt_store
    dialog = opened(settings)
    dialog._filter_enabled.setChecked(False)
    assert dialog._build_filter() is None
    dialog._filter_enabled.setChecked(True)
    config = dialog._build_filter()
    assert config is not None
    assert config.score_threshold == 0.45
    assert config.width_threshold == 10.0
    assert config.height_threshold == 10.0
    assert config.size_mode is core.SizeFilterMode.ANY_ABOVE


def test_build_filter_reads_every_widget(opened, pt_store):
    settings, _ini = pt_store
    dialog = opened(settings)
    dialog._filter_enabled.setChecked(True)
    dialog._score_box.setValue(0.8)
    dialog._size_mode.setCurrentIndex(
        dialog._size_mode.findData("both_below")
    )
    dialog._width_box.setValue(30.0)
    dialog._height_box.setValue(40.0)
    config = dialog._build_filter()
    assert config.score_threshold == 0.8
    assert config.size_mode is core.SizeFilterMode.BOTH_BELOW
    assert config.width_threshold == 30.0
    assert config.height_threshold == 40.0
    assert dialog._width_label.text() == pt_dialog.WIDTH_BELOW_TEXT
    assert dialog._height_label.text() == pt_dialog.HEIGHT_BELOW_TEXT


def test_the_window_follows_the_layout_of_the_other_tools(
    opened, pt_store
):
    """The list sits on the left, the status is a standard bar."""

    settings, _ini = pt_store
    dialog = opened(settings)
    splitter = dialog._splitter
    assert splitter.widget(0) is dialog._list
    assert splitter.widget(1) is dialog._view
    assert splitter.childrenCollapsible() is False
    assert splitter.handleWidth() == 1
    assert dialog._list.minimumWidth() == pt_dialog.LIST_MIN_WIDTH
    assert dialog._list.focusPolicy() == QtCore.Qt.FocusPolicy.NoFocus
    assert dialog.styleSheet() == ""
    assert isinstance(dialog.status_bar, QtWidgets.QStatusBar)
    for label in (dialog._status, dialog._hint, dialog._coords):
        assert label.parent() is dialog.status_bar


def test_the_window_can_be_minimized_and_maximized(opened, pt_store):
    """The window frame carries the minimize and maximize buttons."""

    settings, _ini = pt_store
    dialog = opened(settings)
    flags = dialog.windowFlags()
    hints = QtCore.Qt.WindowType
    assert flags & hints.WindowMinimizeButtonHint
    assert flags & hints.WindowMaximizeButtonHint
    assert flags & hints.WindowCloseButtonHint


def test_filter_controls_disable_with_the_switch(opened, pt_store):
    settings, _ini = pt_store
    dialog = opened(settings)
    dialog._filter_enabled.setChecked(False)
    assert dialog._score_box.isEnabled() is False
    assert dialog._width_box.isEnabled() is False
    assert dialog._categories.isEnabled() is False
    dialog._filter_enabled.setChecked(True)
    assert dialog._score_box.isEnabled() is True
    assert dialog._categories.isEnabled() is True


def test_the_score_threshold_is_stored(opened, pt_store):
    settings, ini = pt_store
    dialog = opened(settings)
    dialog._score_box.setValue(0.75)
    flush_settings(settings)
    assert make_settings(ini).score_threshold() == 0.75


# ------------------------------------------------------------- directory


def test_set_directory_scans_and_filters(opened, pt_store, pt_dir):
    settings, _ini = pt_store
    write_image(pt_dir, "img_10.jpg", size=(20, 20))
    write_image(pt_dir, "img_2.jpg", size=(20, 20))
    write_json(pt_dir, "img_2.json", shapes=[make_shape(score=0.9)])

    dialog = opened(settings)
    dialog.set_directory(pt_dir)
    assert _wait_entries(dialog)
    assert [entry.name for entry in dialog._entries] == [
        "img_2.jpg",
        "img_10.jpg",
    ]
    assert dialog._list.row_count() == 2
    assert dialog._status_text().startswith("共 2 张")

    dialog._filter_enabled.setChecked(True)
    dialog._score_box.setValue(0.5)
    dialog._width_box.setValue(0.0)
    dialog._height_box.setValue(0.0)
    dialog._categories.set_categories(["cat"], {"cat"})
    dialog._apply_filter()
    assert [entry.name for entry in dialog._kept] == ["img_2.jpg"]
    assert dialog._list.row_count() == 1
    assert dialog._index_text() == (
        "1/1" + pt_dialog.FILTER_SUFFIX_FORMAT % (1, 2)
    )


def test_set_directory_twice_with_the_same_folder_is_a_no_op(
    opened, pt_store, pt_dir
):
    settings, _ini = pt_store
    write_image(pt_dir, "a.jpg", size=(8, 8))
    dialog = opened(settings)
    dialog.set_directory(pt_dir)
    assert _wait_entries(dialog)
    worker = dialog._scan_worker
    dialog.set_directory(pt_dir)
    assert dialog._scan_worker is worker


def test_size_mode_and_thresholds_are_stored(opened, pt_store):
    settings, ini = pt_store
    dialog = opened(settings)
    dialog._size_mode.setCurrentIndex(
        dialog._size_mode.findData("both_above")
    )
    dialog._width_box.setValue(12.0)
    dialog._height_box.setValue(13.0)
    flush_settings(settings)
    again = make_settings(ini)
    assert again.size_mode() is core.SizeFilterMode.BOTH_ABOVE
    assert again.width_threshold() == 12.0
    assert again.height_threshold() == 13.0


def test_a_dropped_directory_opens_the_folder(opened, pt_store, pt_dir):
    settings, _ini = pt_store
    write_image(pt_dir, "a.jpg", size=(8, 8))
    dialog = opened(settings)
    event, state = _drop_event(pt_dir)
    dialog.dropEvent(event)
    assert state["accepted"] is True
    assert _wait_entries(dialog)
    assert dialog._directory == pt_dir


def test_a_dropped_file_is_refused(opened, pt_store, pt_dir):
    settings, _ini = pt_store
    path = write_image(pt_dir, "a.jpg", size=(8, 8))
    dialog = opened(settings)
    event, state = _drop_event(path)
    dialog.dropEvent(event)
    assert state == {"accepted": False, "ignored": True}
    assert dialog._directory == ""


# ------------------------------------------------------------ background


def test_unchecking_the_background_hides_the_unlabelled_images(
    opened, pt_store, pt_dir
):
    settings, _ini = pt_store
    dialog = opened(settings)
    entries = [
        _entry(pt_dir, "a.png", (_rectangle(),)),
        _entry(pt_dir, "b.png"),
    ]
    dialog._directory = pt_dir
    dialog._on_scanned(SourceSnapshot(pt_dir, entries, 2, False))
    assert dialog._list.row_count() == 2

    dialog._filter_enabled.setChecked(True)
    dialog._score_box.setValue(0.5)
    dialog._width_box.setValue(0.0)
    dialog._height_box.setValue(0.0)
    dialog._categories.set_categories(["cat"], {"cat"})
    dialog._apply_filter()
    assert [entry.name for entry in dialog._kept] == ["a.png"]

    dialog._categories.set_categories(
        ["cat"], {"cat", core.BACKGROUND_LABEL}
    )
    dialog._apply_filter()
    assert [entry.name for entry in dialog._kept] == ["a.png", "b.png"]


# --------------------------------------------------------- pick target


def test_pick_target_keeps_the_current_image(opened, pt_store, pt_dir):
    settings, _ini = pt_store
    dialog = opened(settings)
    entries = [_entry(pt_dir, "a.png"), _entry(pt_dir, "b.png")]
    assert dialog._pick_target(entries, entries[1].path) == 1
    assert dialog._pick_target(
        entries, os.path.join(pt_dir, "gone.png")
    ) == 0
    assert dialog._pick_target([], "anything") == -1


# ------------------------------------------------------------ the toggle


def test_toggle_button_follows_the_picked_state(opened, pt_store, pt_dir):
    settings, _ini = pt_store
    dialog = opened(settings)
    _open_entries(dialog, pt_dir, ["a.png"])
    assert dialog._toggle_btn.text() == pt_dialog.TOGGLE_PICK_TEXT
    assert dialog._toggle_icon_name == pt_dialog.TOGGLE_PICK_ICON
    dialog._picked = frozenset({("", "a")})
    dialog._refresh_pick_button()
    assert dialog._toggle_btn.text() == pt_dialog.TOGGLE_REMOVE_TEXT
    assert dialog._toggle_icon_name == pt_dialog.TOGGLE_REMOVE_ICON
    dialog._picked = frozenset()
    dialog._refresh_pick_button()
    assert dialog._toggle_btn.text() == pt_dialog.TOGGLE_PICK_TEXT
    assert dialog._toggle_icon_name == pt_dialog.TOGGLE_PICK_ICON


def test_space_switches_between_copy_and_remove(opened, pt_store, pt_dir):
    settings, _ini = pt_store
    dialog = opened(settings)
    _open_entries(dialog, pt_dir, ["a.png"])
    calls = []
    dialog._start_pick = lambda kind, **kwargs: calls.append(
        (kind, kwargs)
    )
    dialog._on_toggle_current()
    kind, kwargs = calls[0]
    assert kind == "pick"
    assert kwargs["targets"] == (dialog._current_entry(),)
    assert kwargs["advance"] == ("", "a")
    dialog._picked = frozenset({("", "a")})
    dialog._refresh_pick_button()
    dialog._on_toggle_current()
    kind, kwargs = calls[-1]
    assert kind == "remove"
    assert kwargs["targets"] == (("", "a"),)
    assert kwargs["advance"] == ("", "a")


def test_delete_on_an_unpicked_image_touches_nothing(
    opened, pt_store, pt_dir, pt_out
):
    settings, _ini = pt_store
    settings.set_output_dir(pt_out)
    dialog = opened(settings)
    # The temp trash folder is shared with every other test run of the
    # machine, so the check is scoped to the stem of this scratch
    # directory instead of the whole folder.
    name = "a_%s.png" % os.path.basename(pt_dir).replace("-", "")
    stem = os.path.splitext(name)[0]
    _open_entries(dialog, pt_dir, [name])
    before = (snapshot(pt_dir), snapshot(pt_out),
              trash_listing(prefix=stem))
    calls = []
    dialog._start_pick = lambda kind, **kwargs: calls.append(kind)
    dialog._on_remove_current()
    assert calls == []
    assert dialog._status.text() == pt_dialog.NOT_PICKED_TEXT
    after = (snapshot(pt_dir), snapshot(pt_out),
             trash_listing(prefix=stem))
    assert after == before
    assert after[2] == []


def test_delete_on_a_picked_image_removes_it(opened, pt_store, pt_dir):
    settings, _ini = pt_store
    dialog = opened(settings)
    _open_entries(dialog, pt_dir, ["a.png"])
    dialog._picked = frozenset({("", "a")})
    calls = []
    dialog._start_pick = lambda kind, **kwargs: calls.append(
        (kind, kwargs)
    )
    dialog._on_remove_current()
    kind, kwargs = calls[0]
    assert kind == "remove"
    assert kwargs["targets"] == (("", "a"),)


def test_pick_all_asks_before_copying(opened, pt_store, pt_dir, monkeypatch):
    settings, _ini = pt_store
    dialog = opened(settings)
    _open_entries(dialog, pt_dir, ["a.png", "b.png"])
    calls = []
    dialog._start_pick = lambda kind, **kwargs: calls.append(kind)

    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "question",
        staticmethod(
            lambda *args, **kwargs: (
                QtWidgets.QMessageBox.StandardButton.No
            )
        ),
    )
    dialog._on_pick_all()
    assert calls == []

    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "question",
        staticmethod(
            lambda *args, **kwargs: (
                QtWidgets.QMessageBox.StandardButton.Yes
            )
        ),
    )
    dialog._on_pick_all()
    assert calls == ["all"]

    dialog._picked = frozenset({("", "a"), ("", "b")})
    dialog._mark_picked()
    dialog._on_pick_all()
    assert dialog._status.text() == pt_dialog.PICK_ALL_EMPTY_TEXT


def test_the_toggle_copies_and_removes_for_real(
    opened, pt_store, pt_dir, pt_out
):
    settings, _ini = pt_store
    settings.set_output_dir(pt_out)
    write_image(pt_dir, "a.jpg", size=(8, 8))
    write_json(pt_dir, "a.json", shapes=[make_shape()])

    dialog = opened(settings)
    dialog.set_directory(pt_dir)
    assert _wait_entries(dialog)
    assert dialog._current_stem() == "a"

    dialog._on_toggle_current()
    assert _wait_picked(dialog, {("", "a")})
    copied = os.path.join(pt_out, "a.jpg")
    assert os.path.isfile(copied)
    assert os.path.isfile(os.path.join(pt_out, "a.json"))
    assert dialog._toggle_btn.text() == pt_dialog.TOGGLE_REMOVE_TEXT
    assert dialog._list.row_text(0).startswith(list_panel.PICK_MARK)

    dialog._on_remove_current()
    assert _wait_picked(dialog, set())
    assert not os.path.exists(copied)
    assert dialog._toggle_btn.text() == pt_dialog.TOGGLE_PICK_TEXT
    assert dialog._list.row_text(0) == "a.jpg"
    assert os.path.isfile(os.path.join(pt_dir, "a.jpg"))


def test_pick_all_copies_everything_that_is_left(
    opened, pt_store, pt_dir, pt_out, monkeypatch
):
    settings, _ini = pt_store
    settings.set_output_dir(pt_out)
    write_image(pt_dir, "a.jpg", size=(8, 8))
    write_image(pt_dir, "b.jpg", size=(8, 8))
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "question",
        staticmethod(
            lambda *args, **kwargs: (
                QtWidgets.QMessageBox.StandardButton.Yes
            )
        ),
    )
    dialog = opened(settings)
    dialog.set_directory(pt_dir)
    assert _wait_entries(dialog)
    dialog._on_pick_all()
    assert _wait_picked(dialog, {("", "a"), ("", "b")})
    assert sorted(os.listdir(pt_out)) == ["a.jpg", "b.jpg"]


# ---------------------------------------------------------------- output


def test_output_button_writes_the_injected_ini(
    opened, pt_store, pt_out, monkeypatch
):
    settings, ini = pt_store
    dialog = opened(settings)
    monkeypatch.setattr(
        QtWidgets.QFileDialog,
        "getExistingDirectory",
        staticmethod(lambda *args, **kwargs: pt_out),
    )
    assert dialog._choose_output_dir() == pt_out
    flush_settings(settings)
    assert make_settings(ini).output_dir() == pt_out
    assert dialog._output_btn.toolTip() == (
        f"{pt_dialog.OUTPUT_TIP}\n{pt_out}"
    )


def test_expand_spinbox_reaches_the_view_and_the_store(opened, pt_store):
    settings, ini = pt_store
    dialog = opened(settings)
    dialog._expand_box.setValue(12)
    assert dialog._view.expand_px() == 12.0
    flush_settings(settings)
    assert make_settings(ini).expand_px() == 12


# -------------------------------------------------------------- keyboard


def test_arrow_keys_move_one_step_and_home_end_jump(
    opened, pt_store, pt_dir
):
    settings, _ini = pt_store
    dialog = opened(settings)
    entries = _open_entries(dialog, pt_dir, ["a.png", "b.png", "c.png"])
    assert dialog._current_path == entries[0].path

    dialog._on_key_press(_key(QtCore.Qt.Key.Key_Right))
    assert dialog._current_path == entries[1].path
    dialog._on_key_press(_key(QtCore.Qt.Key.Key_D))
    assert dialog._current_path == entries[2].path
    dialog._on_key_press(_key(QtCore.Qt.Key.Key_D))
    assert dialog._current_path == entries[2].path
    dialog._on_key_press(_key(QtCore.Qt.Key.Key_Left))
    assert dialog._current_path == entries[1].path
    dialog._on_key_press(_key(QtCore.Qt.Key.Key_A))
    assert dialog._current_path == entries[0].path
    dialog._on_key_press(_key(QtCore.Qt.Key.Key_End))
    assert dialog._current_path == entries[2].path
    dialog._on_key_press(_key(QtCore.Qt.Key.Key_Home))
    assert dialog._current_path == entries[0].path

    dialog._on_key_press(_key(QtCore.Qt.Key.Key_Right))
    assert dialog._repeat_step == 1
    dialog._on_key_release(
        _key(QtCore.Qt.Key.Key_Right, QtCore.QEvent.Type.KeyRelease)
    )
    assert dialog._repeat_step == 0


def test_speed_controls_the_repeat_interval(opened, pt_store):
    settings, _ini = pt_store
    dialog = opened(settings)
    dialog._speed_box.setValue(10)
    assert dialog._repeat_interval() == 100
    assert dialog._repeat_timer.interval() == 100
    dialog._speed_box.setValue(30)
    assert dialog._repeat_interval() == 33


def test_event_filter_takes_the_keys_of_the_active_window(
    opened, pt_store, pt_dir, monkeypatch
):
    settings, _ini = pt_store
    dialog = opened(settings)
    _open_entries(dialog, pt_dir, ["a.png"])
    calls = []
    dialog._on_toggle_current = lambda: calls.append(1)

    monkeypatch.setattr(dialog, "isActiveWindow", lambda: True)
    assert (
        dialog.eventFilter(dialog, _key(QtCore.Qt.Key.Key_Space)) is True
    )
    assert calls == [1]

    event = _key(QtCore.Qt.Key.Key_Space)
    assert dialog.eventFilter(dialog._score_box, event) is False
    assert calls == [1]

    monkeypatch.setattr(dialog, "isActiveWindow", lambda: False)
    event = _key(QtCore.Qt.Key.Key_Space)
    assert dialog.eventFilter(dialog, event) is False
    assert calls == [1]


def test_escape_closes_the_window(opened, pt_store):
    settings, _ini = pt_store
    dialog = opened(settings)
    dialog.show()
    assert dialog._on_key_press(_key(QtCore.Qt.Key.Key_Escape)) is True
    assert dialog.isVisible() is False


# --------------------------------------------------------------- closing


def test_close_event_stops_the_jobs_and_the_event_filter(opened, pt_store):
    settings, _ini = pt_store
    dialog = opened(settings)
    calls = []
    job = __import__("types").SimpleNamespace(
        kind="pick",
        isRunning=lambda: False,
        requestInterruption=lambda: calls.append("job-interrupt"),
        cancel=lambda: calls.append("job-cancel"),
        wait=lambda milliseconds: calls.append(
            ("job-wait", milliseconds)
        ),
    )
    worker = __import__("types").SimpleNamespace(
        requestInterruption=lambda: calls.append("scan-interrupt"),
        wait=lambda milliseconds: calls.append(
            ("scan-wait", milliseconds)
        ),
    )
    dialog._pick_job = job
    dialog._scan_worker = worker
    dialog.show()
    dialog.close()

    assert "job-interrupt" in calls
    assert "job-cancel" in calls
    assert ("job-wait", 2000) in calls
    assert "scan-interrupt" in calls
    assert ("scan-wait", 2000) in calls
    assert dialog._filter_installed is False
    assert dialog._pick_job is None
    assert dialog._scan_worker is None
    assert dialog._preloader.cached_paths() == ()


# ------------------------------------------------------------ performance


def test_a_thousand_images_are_filtered_in_time(opened, pt_store, pt_dir):
    settings, _ini = pt_store
    dialog = opened(settings)
    dialog._directory = pt_dir
    entries = []
    for index in range(1000):
        shapes = (_rectangle(),) if index % 2 == 0 else ()
        entries.append(
            _entry(pt_dir, "img_%04d.png" % index, shapes)
        )
    started = time.monotonic()
    dialog._on_scanned(
        SourceSnapshot(pt_dir, entries, len(entries), False)
    )
    elapsed = time.monotonic() - started
    assert dialog._list.row_count() == 1000
    assert len(dialog._entries) == 1000
    assert elapsed < 2.0

# -------------------------------------------------------------- the tree


def _quiet_probe(dialog):
    """Stop the scheduled picked probe of a test dialog."""

    dialog._schedule_probe = lambda: None
    dialog._pending_probe = False
    try:
        dialog._debounce_timer.stop()
    except Exception:
        pass


def _wait_until(predicate, timeout=10.0):
    """Process events until a predicate is true."""

    app = QtWidgets.QApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if app is not None:
            app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


def _wait_idle(dialog, timeout=10.0):
    """Process events until the running pick job is gone."""

    return _wait_until(
        lambda: dialog._pick_job is None and not dialog._pick_busy(),
        timeout,
    )


def test_the_tree_shows_the_opened_folder(opened, pt_store, pt_dir):
    settings, _ini = pt_store
    write_image(pt_dir, "root.png", size=(8, 8))
    write_image(pt_dir, os.path.join("sub", "inside.png"), size=(8, 8))
    dialog = opened(settings)
    dialog.set_directory(pt_dir)
    assert _wait_entries(dialog)
    _quiet_probe(dialog)
    tree = dialog._list
    assert tree.root_text() == os.path.basename(pt_dir)
    assert tree.row_count() == 2
    assert [tree.name_at(row) for row in range(2)] == [
        "root.png",
        "inside.png",
    ]
    assert tree.directory_item("sub") is not None
    assert tree.current_row() == 0


def test_selecting_a_directory_node_keeps_the_image(
    opened, pt_store, pt_dir
):
    settings, _ini = pt_store
    write_image(pt_dir, "root.png", size=(8, 8))
    write_image(pt_dir, os.path.join("sub", "inside.png"), size=(8, 8))
    dialog = opened(settings)
    dialog.set_directory(pt_dir)
    assert _wait_entries(dialog)
    _quiet_probe(dialog)
    before = dialog._current_path
    dialog._list.setCurrentItem(dialog._list.directory_item("sub"))
    assert dialog._current_path == before
    assert dialog._index == 0


def test_the_filter_prunes_directories_without_visible_images(
    opened, pt_store, pt_dir
):
    settings, _ini = pt_store
    dialog = opened(settings)
    entries = [
        _entry(pt_dir, "a.png", (_rectangle(),), rel_dir="keep"),
        _entry(pt_dir, "b.png", (), rel_dir="drop"),
    ]
    dialog._directory = pt_dir
    dialog._on_scanned(SourceSnapshot(pt_dir, entries, 2, False))
    _quiet_probe(dialog)
    tree = dialog._list
    assert tree.row_count() == 2
    assert tree.directory_item("keep") is not None
    assert tree.directory_item("drop") is not None

    dialog._filter_enabled.setChecked(True)
    dialog._score_box.setValue(0.5)
    dialog._width_box.setValue(0.0)
    dialog._height_box.setValue(0.0)
    dialog._categories.set_categories(["cat"], {"cat"})
    dialog._apply_filter()

    assert tree.row_count() == 1
    assert tree.directory_item("keep") is not None
    assert tree.directory_item("drop") is None


def test_a_filter_rebuild_keeps_the_current_image(
    opened, pt_store, pt_dir
):
    settings, _ini = pt_store
    dialog = opened(settings)
    entries = [
        _entry(pt_dir, "a.png", (_rectangle(),)),
        _entry(pt_dir, "b.png", (_rectangle(),), rel_dir="sub"),
    ]
    dialog._directory = pt_dir
    dialog._on_scanned(SourceSnapshot(pt_dir, entries, 2, False))
    _quiet_probe(dialog)
    dialog._set_current(1)
    current = dialog._current_path

    dialog._filter_enabled.setChecked(True)
    dialog._score_box.setValue(0.5)
    dialog._width_box.setValue(0.0)
    dialog._height_box.setValue(0.0)
    dialog._categories.set_categories(["cat"], {"cat"})
    dialog._apply_filter()

    assert dialog._current_path == current
    assert dialog._list.current_row() == 1
    dialog._list.setCurrentItem(dialog._list.file_item(0))
    assert dialog._index == 0
    assert dialog._current_path == entries[0].path


# ------------------------------------------------------------ auto jump


def test_a_copy_advances_across_directory_borders(
    opened, pt_store, pt_dir, pt_out
):
    settings, _ini = pt_store
    settings.set_output_dir(pt_out)
    write_image(pt_dir, "01_root.png", size=(8, 8))
    write_image(pt_dir, os.path.join("a", "02_a.png"), size=(8, 8))
    write_image(pt_dir, os.path.join("b", "03_b.png"), size=(8, 8))
    write_image(pt_dir, os.path.join("b", "04_b.png"), size=(8, 8))
    dialog = opened(settings)
    dialog.set_directory(pt_dir)
    assert _wait_entries(dialog)
    _quiet_probe(dialog)
    assert [entry.name for entry in dialog._kept] == [
        "01_root.png",
        "02_a.png",
        "03_b.png",
        "04_b.png",
    ]
    assert dialog._index == 0

    dialog._on_toggle_current()
    assert _wait_idle(dialog)
    assert dialog._list.picked() == frozenset({("", "01_root")})
    assert dialog._index == 1
    assert os.path.basename(dialog._current_path) == "02_a.png"
    assert dialog._list.row_text(0).startswith(list_panel.PICK_MARK)

    dialog._on_toggle_current()
    assert _wait_idle(dialog)
    assert dialog._index == 2
    assert dialog._list.picked() == frozenset(
        {("", "01_root"), ("a", "02_a")}
    )
    assert os.path.basename(dialog._current_path) == "03_b.png"
    assert os.path.isfile(os.path.join(pt_out, "02_a.png"))


def test_the_advance_stops_at_the_end_of_the_flat_order(
    opened, pt_store, pt_dir, pt_out
):
    settings, _ini = pt_store
    settings.set_output_dir(pt_out)
    write_image(pt_dir, "01_a.png", size=(8, 8))
    write_image(pt_dir, os.path.join("s", "02_b.png"), size=(8, 8))
    dialog = opened(settings)
    dialog.set_directory(pt_dir)
    assert _wait_entries(dialog)
    _quiet_probe(dialog)

    dialog._on_toggle_current()
    assert _wait_idle(dialog)
    assert dialog._index == 1
    dialog._on_toggle_current()
    assert _wait_idle(dialog)
    assert dialog._index == 1
    assert dialog._list.picked() == frozenset(
        {("", "01_a"), ("s", "02_b")}
    )


def test_a_removal_advances_to_the_next_image(
    opened, pt_store, pt_dir, pt_out
):
    settings, _ini = pt_store
    settings.set_output_dir(pt_out)
    for name in ("01_a.png", "02_b.png", "03_c.png"):
        write_image(pt_dir, name, size=(8, 8))
    dialog = opened(settings)
    dialog.set_directory(pt_dir)
    assert _wait_entries(dialog)
    _quiet_probe(dialog)

    dialog._on_toggle_current()
    assert _wait_idle(dialog)
    dialog._on_toggle_current()
    assert _wait_idle(dialog)
    assert dialog._index == 2

    dialog._on_key_press(_key(QtCore.Qt.Key.Key_Home))
    dialog._on_key_press(_key(QtCore.Qt.Key.Key_Right))
    assert dialog._index == 1
    dialog._on_remove_current()
    assert _wait_idle(dialog)

    assert dialog._index == 2
    assert os.path.basename(dialog._current_path) == "03_c.png"
    assert dialog._list.picked() == frozenset({("", "01_a")})


def test_a_failed_copy_does_not_advance(
    opened, pt_store, pt_dir, pt_out
):
    settings, _ini = pt_store
    settings.set_output_dir(pt_out)
    dialog = opened(settings)
    entries = [
        _entry(pt_dir, "missing.png"),
        _entry(pt_dir, "second.png"),
    ]
    dialog._directory = pt_dir
    dialog._on_scanned(SourceSnapshot(pt_dir, entries, 2, False))
    _quiet_probe(dialog)
    assert dialog._index == 0
    dialog._on_toggle_current()
    assert _wait_idle(dialog)
    assert dialog._index == 0
    assert dialog._list.picked() == frozenset()


def test_a_partially_failed_job_does_not_advance(
    opened, pt_store, pt_dir
):
    settings, _ini = pt_store
    dialog = opened(settings)
    _open_entries(dialog, pt_dir, ["a.png", "b.png"])
    dialog._advance_key = ("", "a")
    dialog._advance_failed = False
    dialog._on_pick_failed(("", "a"), "boom")
    dialog._on_pick_done("remove", 1, 2)
    assert dialog._index == 0


def test_a_cancelled_job_does_not_advance(opened, pt_store, pt_dir):
    settings, _ini = pt_store
    dialog = opened(settings)
    _open_entries(dialog, pt_dir, ["a.png", "b.png"])
    dialog._advance_key = ("", "a")
    dialog._on_pick_done("pick", 0, 1)
    assert dialog._index == 0
    dialog._advance_key = ("", "a")
    dialog._on_pick_done("remove", 0, 2)
    assert dialog._index == 0


def test_pick_all_does_not_advance(
    opened, pt_store, pt_dir, pt_out, monkeypatch
):
    settings, _ini = pt_store
    settings.set_output_dir(pt_out)
    write_image(pt_dir, "a.png", size=(8, 8))
    write_image(pt_dir, "b.png", size=(8, 8))
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "question",
        staticmethod(
            lambda *args, **kwargs: (
                QtWidgets.QMessageBox.StandardButton.Yes
            )
        ),
    )
    dialog = opened(settings)
    dialog.set_directory(pt_dir)
    assert _wait_entries(dialog)
    _quiet_probe(dialog)
    dialog._on_pick_all()
    assert _wait_idle(dialog)
    assert dialog._index == 0
    assert dialog._list.picked() == frozenset(
        {("", "a"), ("", "b")}
    )


# -------------------------------------------------- output below input


def test_the_output_folder_below_the_input_is_skipped(
    opened, pt_store, pt_dir
):
    settings, _ini = pt_store
    output = os.path.join(pt_dir, "out")
    settings.set_output_dir(output)
    write_image(pt_dir, "keep.png", size=(8, 8))
    write_image(output, "product.png", size=(8, 8))
    dialog = opened(settings)
    dialog.set_directory(pt_dir)
    assert _wait_entries(dialog)
    assert [entry.name for entry in dialog._entries] == ["keep.png"]


def test_the_output_equal_to_the_input_is_refused_as_a_setting(
    opened, pt_store, pt_dir, pt_out
):
    settings, ini = pt_store
    settings.set_output_dir(pt_out)
    write_image(pt_dir, "a.jpg", size=(8, 8))
    dialog = opened(settings)
    dialog.set_directory(pt_dir)
    assert _wait_entries(dialog)
    assert _wait_until(lambda: dialog._scan_worker is None)
    _quiet_probe(dialog)

    dialog._on_output_dir_changed(pt_dir)

    assert dialog._status.text() == pt_dialog.OUTPUT_EQUALS_INPUT_TEXT
    flush_settings(settings)
    assert make_settings(ini).output_dir() == pt_out
    assert dialog._scan_worker is None


def test_picking_actions_refuse_when_the_output_is_the_input(
    opened, pt_store, pt_dir, monkeypatch
):
    settings, _ini = pt_store
    settings.set_output_dir(pt_dir)
    dialog = opened(settings)
    _open_entries(dialog, pt_dir, ["a.png", "b.png"])
    dialog._picked = frozenset({("", "a")})
    asked = []
    calls = []
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "question",
        staticmethod(
            lambda *args, **kwargs: asked.append(True)
            or QtWidgets.QMessageBox.StandardButton.No
        ),
    )
    dialog._start_pick = lambda kind, **kwargs: calls.append(kind)

    dialog._on_toggle_current()
    assert calls == []
    assert dialog._status.text() == pt_dialog.OUTPUT_EQUALS_INPUT_TEXT

    dialog._on_remove_current()
    assert calls == []
    assert dialog._status.text() == pt_dialog.OUTPUT_EQUALS_INPUT_TEXT

    dialog._on_pick_all()
    assert calls == []
    assert asked == []
    assert dialog._status.text() == pt_dialog.OUTPUT_EQUALS_INPUT_TEXT


def test_the_scan_lists_the_products_when_output_is_the_input(
    opened, pt_store, pt_dir
):
    """The guard refuses the actions; the scan never hides the input."""

    settings, _ini = pt_store
    settings.set_output_dir(pt_dir)
    write_image(pt_dir, "keep.png", size=(8, 8))
    write_image(
        pt_dir, os.path.join("picked", "product.png"), size=(8, 8)
    )
    dialog = opened(settings)
    dialog.set_directory(pt_dir)
    assert _wait_entries(dialog)
    _quiet_probe(dialog)
    assert [entry.name for entry in dialog._entries] == [
        "keep.png",
        "product.png",
    ]
    assert dialog._output_dir_is_the_input() is True


def test_moving_the_output_into_the_tree_rescans(
    opened, pt_store, pt_dir, pt_out
):
    settings, _ini = pt_store
    settings.set_output_dir(pt_out)
    write_image(pt_dir, "keep.png", size=(8, 8))
    nested = os.path.join(pt_dir, "out")
    write_image(nested, "product.png", size=(8, 8))
    dialog = opened(settings)
    dialog.set_directory(pt_dir)
    assert _wait_entries(dialog)
    assert [entry.name for entry in dialog._entries] == [
        "keep.png",
        "product.png",
    ]

    dialog._on_output_dir_changed(nested)
    assert _wait_until(
        lambda: [entry.name for entry in dialog._entries] == ["keep.png"]
    )
