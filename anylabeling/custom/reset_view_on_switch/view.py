"""Reset the canvas view of the widget after a switch to another image.

The module holds one wrapper around load_file. Before the upstream load
it drops the per file memory entries of the file that is about to be
loaded, so the restore branches of upstream cannot flash the old view,
and after a successful switch it resets the view to the state a fresh
widget shows for its first image. A load of the same file again (a
reload after an edit, for example) is left alone, and a failed load
leaves the widget and its memory as they were.

Nothing here imports the labeling widget or its canvas: the widget is
passed in and every step is guarded, so a widget without the expected
attributes is reported and never turns a load into an error.
"""

import logging

LOGGER = logging.getLogger(__name__)


def install_reset_view_on_switch(widget):
    """Wrap the load_file of the widget to reset the view after a switch.

    The installation is idempotent: the wrapper is created once and
    reused, so a repeated call cannot wrap the wrapper and make the
    original load_file run twice per load. The wrapping happens on the
    instance, which leaves the upstream class untouched. A widget
    without a callable load_file gets no wrapper and no flag, so a later
    call can still install the wrapper.
    """
    if getattr(widget, "_reset_view_on_switch_installed", False):
        return getattr(widget, "_reset_view_on_switch_wrapper", None)
    original = getattr(widget, "load_file", None)
    if not callable(original):
        return None
    wrapper = _load_file_wrapper(widget, original)
    widget.load_file = wrapper
    widget._reset_view_on_switch_wrapper = wrapper
    widget._reset_view_on_switch_installed = True
    return wrapper


def _load_file_wrapper(widget, original):
    """Return the load_file replacement around the original method."""

    def load_file(filename=None):
        previous = getattr(widget, "filename", None)
        target = _resolved_target(widget, filename)
        saved = _drop_entries(widget, target, previous)
        try:
            loaded = original(filename)
        except BaseException:
            _restore_entries(widget, saved)
            raise
        if loaded is True:
            loaded_name = getattr(widget, "filename", None)
            if (
                isinstance(loaded_name, str)
                and loaded_name != previous
            ):
                reset_view_on_switch(widget, loaded_name)
            # The reset belongs to a load that happened: only now may the
            # entries of the target be dropped again, so the upstream
            # cannot restore what the wrapper removed and the reset
            # cannot leave behind the entry the upstream just wrote.
            _drop_entries(widget, target, previous)
        else:
            _restore_entries(widget, saved)
        return loaded

    return load_file


def _resolved_target(widget, filename):
    """Return the file the wrapper may expect to be loaded, or None.

    The name is resolved the way the upstream load_file resolves it
    (an argument of None falls back to the filename of the settings)
    without asking whether the file exists, so the per file memory of
    the file that is about to be loaded can be dropped beforehand. The
    comparison with a resolved name is enough: a target that differs
    from the loaded one makes the guard of the caller a no-op.
    """
    if filename is None:
        settings = getattr(widget, "settings", None)
        filename = getattr(settings, "value", None)
        if not callable(filename):
            return None
        try:
            filename = filename("filename", "")
        except Exception as error:  # noqa: BLE001
            LOGGER.warning("Could not read the target file: %s", error)
            return None
    if filename is None:
        return None
    try:
        return str(filename)
    except Exception:  # noqa: BLE001
        return None


def _drop_entries(widget, target, previous):
    """Forget the memory the widget holds for one file.

    The zoom entry and the scroll entries of the file are removed, not
    rewritten: the restore branches of the upstream load_file then find
    nothing, so the load starts from the state of an image that was
    never shown. The removed entries and the name they belong to are
    returned, so _restore_entries can put them back when the load does
    not happen. A target that is already the file the widget shows is
    left alone, which is what keeps a reload of the same file
    untouched. Every failure is logged and treated as a memory that was
    never dropped.
    """
    saved = {}
    if target is None or target == previous:
        return saved
    saved["target"] = target
    zoom_values = getattr(widget, "zoom_values", None)
    if isinstance(zoom_values, dict):
        try:
            if target in zoom_values:
                saved["zoom"] = zoom_values.pop(target)
        except (KeyError, TypeError) as error:  # noqa: BLE001
            LOGGER.warning("Could not drop the zoom of %s: %s", target, error)
    scroll_values = getattr(widget, "scroll_values", None)
    if isinstance(scroll_values, dict):
        entries = {}
        for orientation, values in scroll_values.items():
            if not isinstance(values, dict):
                continue
            try:
                if target in values:
                    entries[orientation] = values.pop(target)
            except (KeyError, TypeError) as error:  # noqa: BLE001
                LOGGER.warning(
                    "Could not drop the scroll of %s: %s", target, error
                )
        if entries:
            saved["scroll"] = entries
    return saved


def _restore_entries(widget, saved):
    """Put back the memory entries _drop_entries removed.

    A failed load leaves the per file memory as it was found. Every
    failure is logged and swallowed, because a load that is already
    failing must not be replaced by another error.
    """
    if not saved:
        return
    saved_target = saved.get("target")
    if saved_target is None:
        return
    zoom_values = getattr(widget, "zoom_values", None)
    if isinstance(zoom_values, dict) and "zoom" in saved:
        try:
            zoom_values[saved_target] = saved["zoom"]
        except Exception as error:  # noqa: BLE001
            LOGGER.warning("Could not restore the zoom: %s", error)
    scroll_values = getattr(widget, "scroll_values", None)
    if not isinstance(scroll_values, dict):
        return
    for orientation, value in saved.get("scroll", {}).items():
        values = scroll_values.get(orientation)
        if not isinstance(values, dict):
            continue
        try:
            values[saved_target] = value
        except Exception as error:  # noqa: BLE001
            LOGGER.warning("Could not restore the scroll: %s", error)


def reset_view_on_switch(widget, filename):
    """Reset the view of the widget to the state of its first image.

    The upstream load_file restores the memory of the file it loads
    (the remembered zoom, the remembered scroll) and can keep the scale
    of the previous image, so the view it leaves behind is not the
    default one. Upstream adjust_scale only writes the zoom widget, the
    zoom memory and the navigator, so the zoom_mode and the checked
    state of the fit actions have to be set here as well; the first
    image of a session gets that state in the constructor of the
    widget.

    The keep_prev_scale setting of the configuration is deliberately
    ignored: the view has to be the default one for every image, and
    respecting the setting would make the outcome depend on how an
    image was reached. The fallback is one guard on that setting in
    this function.

    The reset never raises: every step is guarded and reported, so a
    widget that does not offer the expected attributes still loads, and
    a failure of the reset cannot turn a successful load into an error.
    Returns True, also when a step had to be skipped.
    """
    _reset_zoom(widget)
    _reset_fit_actions(widget)
    _reset_scroll(widget)
    _drop_entries(widget, filename, None)
    return True


def _reset_zoom(widget):
    """Reset the zoom widget and the zoom memory to fit window."""
    adjust_scale = getattr(widget, "adjust_scale", None)
    if callable(adjust_scale):
        try:
            # A zoom that is not fit window would keep the mode of the
            # previous image, so the reset always asks for the initial
            # scale of the upstream.
            adjust_scale(initial=True)
        except Exception as error:  # noqa: BLE001
            LOGGER.warning("Could not reset the zoom: %s", error)
    else:
        LOGGER.warning("The widget offers no adjust_scale to reset the zoom")
    try:
        widget.zoom_mode = getattr(widget, "FIT_WINDOW", 0)
    except Exception as error:  # noqa: BLE001
        LOGGER.warning("Could not reset the zoom mode: %s", error)
    zoom_values = getattr(widget, "zoom_values", None)
    if isinstance(zoom_values, dict):
        try:
            zoom_values.pop(None, None)
        except Exception as error:  # noqa: BLE001
            LOGGER.warning("Could not clean the zoom memory: %s", error)


def _reset_fit_actions(widget):
    """Check the fit window action and uncheck the fit width action.

    QAction.setChecked emits toggled but never triggered, so the
    handlers of the actions do not run and cannot change the zoom
    widget the reset just set.
    """
    actions = getattr(widget, "actions", None)
    for name, checked in (("fit_window", True), ("fit_width", False)):
        action = getattr(actions, name, None)
        set_checked = getattr(action, "setChecked", None)
        if action is None or not callable(set_checked):
            LOGGER.warning("The widget offers no %s action to reset", name)
            continue
        try:
            set_checked(checked)
        except Exception as error:  # noqa: BLE001
            LOGGER.warning("Could not reset the %s action: %s", name, error)


def _reset_scroll(widget):
    """Move both scroll bars of the loaded image back to the origin.

    The scroll of the image is an upstream state the reset has to
    address through the setter, which also refreshes the navigator
    viewport; the memory entry the setter writes is removed by the
    caller.
    """
    set_scroll = getattr(widget, "set_scroll", None)
    scroll_values = getattr(widget, "scroll_values", None)
    if not callable(set_scroll):
        LOGGER.warning("The widget offers no set_scroll to reset the scroll")
        return
    if isinstance(scroll_values, dict):
        orientations = list(scroll_values)
    else:
        orientations = _scroll_orientations(widget)
    if not orientations:
        LOGGER.warning("The widget offers no scroll bars to reset")
    for orientation in orientations:
        try:
            set_scroll(orientation, 0.0)
        except Exception as error:  # noqa: BLE001
            LOGGER.warning("Could not reset the scroll: %s", error)


def _scroll_orientations(widget):
    """Return the orientations of the scroll bars of the widget."""
    scroll_bars = getattr(widget, "scroll_bars", None)
    if not isinstance(scroll_bars, dict):
        return []
    return list(scroll_bars)
