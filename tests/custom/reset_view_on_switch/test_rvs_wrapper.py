"""Tests of the load_file wrapper of reset_view_on_switch.

The widget is a fake object carrying the state names the upstream widget
exposes, so the wrapper sees exactly what it sees in the application:
the zoom widget, the zoom mode, the per file memory and the two fit
actions. The expected fit value is derived from the scaler of the fake
widget, never from a literal measured on the offscreen layout.

The upstream load_file is modelled with its two interesting branches
(the remembered entry of the file, otherwise the fit window scale), so
the tests can show that the wrapper drops a stale entry before the load
without running the real, heavy widget.
"""

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from PyQt6 import QtCore, QtWidgets

from anylabeling.custom.reset_view_on_switch import (
    install_reset_view_on_switch,
    reset_view_on_switch,
)

FIT_WINDOW = 0
MANUAL_ZOOM = 2
#: Scale the fake widget reports for a fit window, in percent.
FIT_PERCENT = 199
#: Scroll value a test may plant for a file the widget never showed.
PLANTED_SCROLL = 17.5


class _Navigator:
    "Record the zoom value the widget pushes to the navigator."

    def __init__(self):
        self.zoom = None

    def set_zoom_value(self, value):
        self.zoom = value


def _expect_fit(widget):
    "Return the zoom value a fit window of the fake widget produces."

    return int(100 * widget.scale_fit_window())


def _apply_fit(widget, initial=True):
    "Stand in for the upstream adjust_scale."

    value = int(100 * widget.scale_fit_window())
    # The upstream keeps the mode of the previous image when it is not
    # asked for the initial scale, which is what keep_prev_scale does.
    mode = widget.FIT_WINDOW if initial else widget.zoom_mode
    widget.zoom_widget.setValue(value)
    widget.zoom_values[widget.filename] = (mode, value)
    widget.navigator_dialog.set_zoom_value(value)


def _apply_scroll(widget, orientation, value):
    "Stand in for the upstream set_scroll."

    widget.scroll_bars[orientation].setValue(round(value))
    widget.scroll_values[orientation][widget.filename] = value


def _manual_zoom(widget, value):
    "Switch the fake widget to a manual zoom, as the upstream one does."

    widget.actions.fit_width.setChecked(False)
    widget.actions.fit_window.setChecked(False)
    widget.zoom_mode = MANUAL_ZOOM
    widget.zoom_widget.setValue(value)
    widget.zoom_values[widget.filename] = (MANUAL_ZOOM, value)
    widget.navigator_dialog.set_zoom_value(value)


def _base_widget(filename=None):
    "Return a fake widget offering every state the wrapper touches."

    widget = SimpleNamespace(
        filename=filename,
        settings=SimpleNamespace(value=lambda key, default=None: default),
        zoom_widget=QtWidgets.QSpinBox(),
        zoom_values={},
        scroll_values={
            QtCore.Qt.Orientation.Horizontal: {},
            QtCore.Qt.Orientation.Vertical: {},
        },
        scroll_bars={
            QtCore.Qt.Orientation.Horizontal: QtWidgets.QScrollBar(),
            QtCore.Qt.Orientation.Vertical: QtWidgets.QScrollBar(),
        },
        # The wrapper only calls setChecked(True/False); a real QAction
        # is avoided so the test does not depend on the Qt signal order.
        actions=SimpleNamespace(
            fit_window=MagicMock(),
            fit_width=MagicMock(),
        ),
        navigator_dialog=_Navigator(),
        FIT_WINDOW=FIT_WINDOW,
    )
    widget.zoom_widget.setRange(1, 1000)
    widget.zoom_widget.setValue(100)
    widget._fit_scale = FIT_PERCENT / 100.0
    widget.scale_fit_window = lambda: widget._fit_scale
    widget.actions.fit_window.isChecked.return_value = True
    widget.actions.fit_width.isChecked.return_value = False
    widget.adjust_scale = lambda initial=True: _apply_fit(widget, initial)
    widget.set_scroll = lambda orientation, value: _apply_scroll(
        widget, orientation, value
    )
    widget.set_zoom = lambda value: _manual_zoom(widget, value)
    return widget


def _keep_prev_scale(widget):
    "Return the upstream keep_prev_scale switch of the widget."

    config = getattr(widget, "_config", None) or {}
    if not isinstance(config, dict):
        return False
    return bool(config.get("keep_prev_scale"))


def _upstream_style_load(widget, filename):
    "Model the restore branches of the upstream load_file."

    widget.filename = filename
    if filename in widget.zoom_values:
        widget.zoom_mode, remembered = widget.zoom_values[filename]
        widget.zoom_widget.setValue(remembered)
        widget.navigator_dialog.set_zoom_value(remembered)
    # The upstream fits an unknown file, and keeps the scale of the
    # previous image instead when the configuration asks it to.
    elif _keep_prev_scale(widget) and widget.zoom_values:
        _apply_fit(widget, initial=False)
    else:
        _apply_fit(widget, initial=True)
    for orientation in widget.scroll_values:
        values = widget.scroll_values[orientation]
        if filename in values:
            _apply_scroll(widget, orientation, values[filename])
    return True


def make_widget(filename=None, behaviour=None):
    "Return a fake widget wrapped by the installation under test."

    widget = _base_widget(filename)
    state = {"calls": 0}
    widget.load_state = state
    if behaviour is None:

        def behaviour(name):
            return _upstream_style_load(widget, name)

    widget.load_behaviour = behaviour

    def load_file(name):
        state["calls"] += 1
        return widget.load_behaviour(name)

    widget.load_file = load_file
    install_reset_view_on_switch(widget)
    return widget


def load_first_image(widget, filename):
    "Load the first image the way the queue of the widget does."

    widget.filename = filename
    widget.zoom_values.pop(filename, None)
    _apply_fit(widget, initial=True)
    widget.zoom_mode = widget.FIT_WINDOW
    widget.actions.fit_window.setChecked(True)
    widget.actions.fit_width.setChecked(False)
    return widget


def view_state(widget):
    "Return every part of the view the reset has to leave behind."

    return {
        "zoom": widget.zoom_widget.value(),
        "mode": widget.zoom_mode,
        "fit_window": widget.actions.fit_window.isChecked(),
        "fit_width": widget.actions.fit_width.isChecked(),
        "scroll": (
            widget.scroll_bars[QtCore.Qt.Orientation.Horizontal].value(),
            widget.scroll_bars[QtCore.Qt.Orientation.Vertical].value(),
        ),
        "zoom_values": dict(widget.zoom_values),
        "scroll_values": {
            orientation: dict(values)
            for orientation, values in widget.scroll_values.items()
        },
    }


def assert_default_view(widget, target):
    "Assert the widget shows the default view of a freshly loaded image."

    assert widget.filename == target
    assert widget.zoom_mode == widget.FIT_WINDOW
    assert widget.actions.fit_window.isChecked() is True
    assert widget.actions.fit_width.isChecked() is False
    assert widget.zoom_widget.value() == _expect_fit(widget)
    assert widget.navigator_dialog.zoom == widget.zoom_widget.value()
    for bar in widget.scroll_bars.values():
        assert bar.value() == 0
    # The contract is "no entry", not "an entry that happens to hold the
    # default": the dropped entries are what the upstream restore reads.
    assert target not in widget.zoom_values
    for values in widget.scroll_values.values():
        assert target not in values


def test_first_load_leaves_fit_state(rvs_images):
    "The wrapper leaves the first image in the state a session shows."

    first, second = rvs_images[0], rvs_images[1]
    widget = make_widget(None)
    widget.load_state["calls"] = 0

    assert widget.load_file(first) is True

    assert widget.load_state["calls"] == 1
    assert_default_view(widget, first)
    assert second not in widget.zoom_values
    for values in widget.scroll_values.values():
        assert second not in values


def test_switch_resets_manual_zoom(rvs_images):
    "A manual zoom is dropped when the widget switches to another file."

    first, second = rvs_images[0], rvs_images[1]
    widget = make_widget(None)
    load_first_image(widget, first)
    widget.set_zoom(300)

    assert widget.load_file(second) is True

    assert_default_view(widget, second)
    # The file that was left keeps the zoom it was left at, while the
    # newly loaded file only carries the entry the reset wrote.
    assert widget.zoom_values[first] == (MANUAL_ZOOM, 300)
    entry = widget.zoom_values.get(second)
    assert entry is None or entry == (FIT_WINDOW, _expect_fit(widget))


def test_switch_back_does_not_restore_old_zoom(rvs_images):
    "Coming back to a file does not restore the zoom remembered for it."

    first, second = rvs_images[0], rvs_images[1]
    widget = make_widget(None)
    load_first_image(widget, first)
    widget.set_zoom(300)
    widget.load_file(second)
    widget.set_zoom(250)
    widget.load_file(first)
    widget.set_zoom(300)

    assert widget.load_file(second) is True

    value = widget.zoom_widget.value()
    assert value == _expect_fit(widget)
    assert value != 300
    entry = widget.zoom_values.get(second)
    assert entry is None or entry == (FIT_WINDOW, value)


def test_same_file_reload_keeps_view(rvs_images):
    "A reload of the same file leaves the view and the memory alone."

    first = rvs_images[0]
    widget = make_widget(None)
    load_first_image(widget, first)
    widget.set_zoom(300)
    widget.load_file(first)
    widget.set_zoom(300)
    # The remembered scroll of the file takes part in "keeps the view":
    # a reload must leave the entry alone as well.
    for orientation in widget.scroll_values:
        widget.set_scroll(orientation, 42.0)
    before = view_state(widget)

    assert widget.load_file(widget.filename) is True

    after = view_state(widget)
    assert after["zoom_values"] == before["zoom_values"]
    assert after["scroll_values"] == before["scroll_values"]
    assert after["scroll"] == before["scroll"]
    assert after["zoom"] == before["zoom"] == 300
    assert after["mode"] == before["mode"] == MANUAL_ZOOM
    assert after["fit_window"] == before["fit_window"]
    assert after["fit_width"] == before["fit_width"]


def test_failed_load_keeps_entries_and_view(rvs_images):
    "A load that fails puts the dropped memory back and keeps the view."

    first, second = rvs_images[0], rvs_images[1]
    widget = make_widget(None)
    load_first_image(widget, first)
    widget.zoom_values[second] = (MANUAL_ZOOM, 123)
    widget.scroll_values[QtCore.Qt.Orientation.Vertical][second] = (
        PLANTED_SCROLL
    )
    before = view_state(widget)
    widget.load_behaviour = lambda name: False

    assert widget.load_file(second) is False

    after = view_state(widget)
    # The memory of the target is put back exactly as it was dropped; a
    # load that never happened must not forget what the file showed.
    assert after["zoom_values"] == before["zoom_values"]
    assert after["scroll_values"][QtCore.Qt.Orientation.Vertical][second] == (
        PLANTED_SCROLL
    )
    assert after["scroll_values"][QtCore.Qt.Orientation.Horizontal] == {}
    assert after["zoom"] == before["zoom"]
    assert after["mode"] == before["mode"]
    assert widget.load_state["calls"] == 1


def test_failed_load_is_reported_without_error(rvs_images):
    "A load that raises keeps the memory and lets the error through."

    first, second = rvs_images[0], rvs_images[1]
    widget = make_widget(None)
    load_first_image(widget, first)
    widget.zoom_values[second] = (MANUAL_ZOOM, 123)
    widget.scroll_values[QtCore.Qt.Orientation.Vertical][second] = (
        PLANTED_SCROLL
    )
    before = view_state(widget)

    def raising(name):
        raise RuntimeError("upstream load failed")

    widget.load_behaviour = raising

    with pytest.raises(RuntimeError):
        widget.load_file(second)

    after = view_state(widget)
    assert after["zoom_values"] == before["zoom_values"]
    assert after["scroll_values"] == before["scroll_values"]
    assert after["zoom"] == before["zoom"]


def test_missing_helpers_degrade_without_error(rvs_images, caplog):
    "A widget without the expected helpers still loads, with warnings."

    first, second = rvs_images[0], rvs_images[1]
    widget = make_widget(None)
    load_first_image(widget, first)
    del widget.adjust_scale
    del widget.set_scroll
    del widget.actions

    with caplog.at_level(logging.WARNING, logger="anylabeling"):
        assert reset_view_on_switch(widget, first) is True
        assert widget.load_file(second) is True

    assert widget.filename == second
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ]
    # Each missing helper is reported once, so a silent skip of the
    # reset cannot pass as a degraded reset.
    assert any("adjust_scale" in message for message in messages)
    assert any("set_scroll" in message for message in messages)
    assert any("fit_window" in message for message in messages)


def test_keep_prev_scale_true_is_overridden(rvs_images):
    "The reset ignores the keep_prev_scale setting of the configuration."

    first, second = rvs_images[0], rvs_images[1]
    widget = make_widget(None)
    widget._config = {"keep_prev_scale": True}
    load_first_image(widget, first)
    widget.set_zoom(250)
    assert widget.zoom_values[first] == (MANUAL_ZOOM, 250)

    assert widget.load_file(second) is True

    value = widget.zoom_widget.value()
    assert value == _expect_fit(widget)
    assert value != 250
    assert_default_view(widget, second)


def test_filename_none_resolves_the_target_through_settings(rvs_images):
    "A load without argument follows the settings, like the upstream."

    first, second = rvs_images[0], rvs_images[1]
    widget = make_widget(None)
    load_first_image(widget, first)
    widget.zoom_values[second] = (MANUAL_ZOOM, 123)
    widget.settings.value = lambda key, default=None: second

    assert widget.load_file(None) is True

    # The settings name the file, so the wrapper dropped its entry
    # before the upstream restore could read it: the stale manual zoom
    # of the file never reaches the view. The fake upstream takes None
    # as the name it loads, so the reset is not what removes the entry.
    assert widget.zoom_widget.value() == _expect_fit(widget)
    assert second not in widget.zoom_values
    assert widget.zoom_values[first] == (FIT_WINDOW, _expect_fit(widget))


def test_filename_none_without_a_target_keeps_the_view(rvs_images):
    "No argument and an empty settings name leave the memory alone."

    first = rvs_images[0]
    widget = make_widget(None)
    load_first_image(widget, first)
    widget.set_zoom(300)

    assert widget.load_file(None) is True

    # The settings of the fake widget offer no name, so the wrapper has
    # no target: it drops nothing and resets nothing. Only the memory of
    # the current file shows that, because the fake upstream takes None
    # as the name of the file it loaded and touches the view itself.
    assert widget.zoom_values.get(first) == (MANUAL_ZOOM, 300)


def test_install_is_idempotent_and_needs_callable():
    "Installing twice keeps one wrapper, a widget without one gets none."

    widget = _base_widget(None)

    def load_file(name):
        return True

    widget.load_file = load_file
    first = install_reset_view_on_switch(widget)
    second = install_reset_view_on_switch(widget)

    assert first is second
    assert widget.load_file is first
    assert widget._reset_view_on_switch_installed is True
    assert getattr(widget, "_reset_view_on_switch_wrapper") is first

    bare = SimpleNamespace()
    assert install_reset_view_on_switch(bare) is None
    assert getattr(bare, "_reset_view_on_switch_installed", False) is False
