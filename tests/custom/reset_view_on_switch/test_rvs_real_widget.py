"""Tests of the reset view on switch mount point on a real widget.

The widget is the real offscreen LabelingWidget built on the shipped
configuration, so these tests cover the two mount point lines of
label_widget together with the wrapper. The offscreen layout is not the
layout of a shown window, so the expected zoom value is read from the
widget at run time (int(100 * widget.scale_fit_window())), never from a
literal.
"""

from PyQt6 import QtCore

from anylabeling.custom.reset_view_on_switch import (
    install_reset_view_on_switch,
)


def _expect_fit(widget):
    "Return the zoom value the fit window scale of the widget yields."

    return int(100 * widget.scale_fit_window())


def _load_first_image(real):
    "Run the first load the constructor queued and return the widget."

    widget = real.widget
    assert len(real.queued) == 1
    assert real.queued[0]() is True
    return widget


def _view(widget):
    "Return the view state the reset has to leave behind."

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
    }


def _assert_default_view(widget, target):
    "Assert the widget shows the default view of a freshly loaded image."

    assert widget.filename == target
    assert widget.zoom_mode == widget.FIT_WINDOW
    assert widget.actions.fit_window.isChecked() is True
    assert widget.actions.fit_width.isChecked() is False
    assert widget.zoom_widget.value() == _expect_fit(widget)
    assert widget.scroll_bars[QtCore.Qt.Orientation.Horizontal].value() == 0
    assert widget.scroll_bars[QtCore.Qt.Orientation.Vertical].value() == 0


def test_real_widget_mount_and_switch(rvs_images, make_real_widget):
    "The mounted wrapper resets the zoom when the widget switches file."

    first, second = rvs_images[0], rvs_images[1]
    real = make_real_widget(first)
    widget = _load_first_image(real)

    assert getattr(widget, "_reset_view_on_switch_installed", False) is True
    assert callable(widget.load_file)
    _assert_default_view(widget, first)

    widget.set_zoom(300)
    assert widget.zoom_widget.value() == 300
    assert widget.zoom_mode == widget.MANUAL_ZOOM

    assert widget.load_file(second) is True

    _assert_default_view(widget, second)
    # The stale manual entry of the first file stays remembered for it,
    # but nothing the switch wrote may still hold the manual zoom, and
    # the entry of the newly loaded file has to describe the reset view.
    entry = widget.zoom_values.get(second)
    assert entry is None or entry == (
        widget.FIT_WINDOW,
        widget.zoom_widget.value(),
    )
    assert widget.zoom_mode != widget.MANUAL_ZOOM
    # The file that was left keeps its own memory: the wrapper only drops
    # what belongs to the file that is about to be loaded.
    assert widget.zoom_values[first] == (widget.MANUAL_ZOOM, 300)


def test_real_widget_navigator_synced(rvs_images, make_real_widget):
    "The navigator of the widget shows the zoom the reset set."

    first, second = rvs_images[0], rvs_images[1]
    real = make_real_widget(first)
    widget = _load_first_image(real)
    widget.set_zoom(300)

    assert widget.load_file(second) is True

    navigator = widget.navigator_dialog
    assert navigator.current_zoom == widget.zoom_widget.value()
    assert navigator.zoom_input.text() == str(widget.zoom_widget.value())
    assert navigator.zoom_slider.value() == widget.zoom_widget.value()


def test_real_widget_switch_back_and_reload(rvs_images, make_real_widget):
    "Coming back to a file and reloading one leave them at the default."

    first, second = rvs_images[0], rvs_images[1]
    real = make_real_widget(first)
    widget = _load_first_image(real)

    widget.set_zoom(300)
    assert widget.load_file(second) is True
    widget.set_zoom(250)
    assert widget.load_file(first) is True

    assert widget.zoom_widget.value() == _expect_fit(widget)
    assert widget.zoom_widget.value() != 300
    _assert_default_view(widget, first)

    before = _view(widget)
    assert widget.load_file(widget.filename) is True
    after = _view(widget)

    assert after["zoom"] == before["zoom"]
    assert after["mode"] == before["mode"]
    assert after["fit_window"] == before["fit_window"]
    assert after["fit_width"] == before["fit_width"]
    assert after["scroll"] == before["scroll"]
    for name, entry in before["zoom_values"].items():
        if name == widget.filename:
            continue
        assert after["zoom_values"][name] == entry
