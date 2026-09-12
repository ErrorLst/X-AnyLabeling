"""Create an empty label file for an image that does not have one yet.

The labeling window is attached to a wrapper around load_file: upstream
performs the whole load, and only a successful load (True) can be
followed by the creation of the missing label file. Every failure inside
the extra step is swallowed, so a problem with the new file can never
break an otherwise successful load.

The file itself is written by the upstream save path, save_labels, so
what a label file holds and how it reaches the disk stays an upstream
matter. The price of that reuse is that the save replaces its target: a
label file another process creates between the existence check and the
save is overwritten, a narrow window that is accepted.

The wrapper is installed on the instance, never on the class, and the
upstream load_file stays the only implementation of the load.
"""

import logging
import os.path as osp

LOGGER = logging.getLogger(__name__)


def install_ensure_label_file(widget):
    """Wrap the load_file of the widget to create a missing label file.

    The installation is idempotent: the wrapper is created once and
    reused, so a repeated call cannot wrap the wrapper and make the
    original load_file run twice per load. The wrapping happens on the
    instance, which leaves the upstream class untouched. A widget
    without a callable load_file gets no wrapper and no flag, so a later
    call can still install the wrapper.
    """
    if getattr(widget, "_ensure_label_file_installed", False):
        return getattr(widget, "_ensure_label_file_wrapper", None)
    original = getattr(widget, "load_file", None)
    if not callable(original):
        return None
    wrapper = _load_file_wrapper(widget, original)
    widget.load_file = wrapper
    widget._ensure_label_file_wrapper = wrapper
    widget._ensure_label_file_installed = True
    return wrapper


def _load_file_wrapper(widget, original):
    """Return the load_file replacement around the original method."""

    def load_file(filename=None):
        loaded = original(filename)
        if loaded is True:
            ensure_label_file(widget)
        return loaded

    return load_file


def ensure_label_file(widget):
    """Create the missing label file of the image the widget loaded.

    The creation is delegated to the save path of the widget, so the
    file holds what a save would have written and the widget comes out
    in the state of an image with a label file: save_labels sets the
    label file instance and marks the entry of the file list. Only the
    delete command is completed here; see _sync_delete_action.

    Returns the path of the created label file, or None when there is
    nothing to create or the save failed. A failure is logged and never
    propagated.
    """
    try:
        target = _missing_label_file(widget)
        if target is None:
            return None
        if not widget.save_labels(target):
            LOGGER.warning("Could not create the empty label file %s", target)
            return None
        _sync_delete_action(widget)
        LOGGER.info("Created the empty label file %s", target)
        return target
    except Exception as error:  # noqa: BLE001
        LOGGER.warning("Could not create the empty label file: %s", error)
        return None


def _missing_label_file(widget):
    """Return the label file to create, or None when there is none.

    The guards mirror the state a successful load_file leaves behind, so
    only a really loaded image without annotation and without label file
    reaches the save:

    * a file was loaded and it is not a label file itself;
    * label_file is None, because upstream loads a LabelFile whenever a
      label file exists;
    * the widget is not dirty, because keep_prev with auto_save off
      leaves the shapes of the previous image on the canvas;
    * the canvas holds no shape and the image is loaded, so the written
      file matches what the canvas shows and has a size;
    * the label list is empty, so the file that is written is empty;
    * the resolved target does not exist yet, so an image that already
      has a json keeps it. That check is a plain path check: a file
      another process creates afterwards is overwritten by the save.

    Returns the path of the label file to create, or None.
    """
    filename = getattr(widget, "filename", None)
    if not filename or str(filename).lower().endswith(".json"):
        return None
    if getattr(widget, "label_file", None) is not None:
        return None
    if getattr(widget, "dirty", False):
        return None
    canvas = getattr(widget, "canvas", None)
    if canvas is not None and getattr(canvas, "shapes", None):
        return None
    image = getattr(widget, "image", None)
    if image is None or not hasattr(image, "isNull") or image.isNull():
        return None
    label_list = getattr(widget, "label_list", None)
    if label_list is None or len(list(label_list)) > 0:
        return None
    get_label_file = getattr(widget, "get_label_file", None)
    if not callable(get_label_file):
        return None
    target = get_label_file()
    if not target or osp.lexists(target):
        return None
    return target


def _sync_delete_action(widget):
    """Enable the delete command now that the label file exists.

    Upstream disables it while the loaded image has no label file
    (set_clean, label_widget.py:3042-3045), a decision taken before the
    file existed. Everything else is left to the save path, which
    already set the label file instance and the file list entry; a
    widget without the action or without has_label_file is skipped.
    """
    actions = getattr(widget, "actions", None)
    delete_file = getattr(actions, "delete_file", None)
    has_label_file = getattr(widget, "has_label_file", None)
    if delete_file is None or not callable(has_label_file):
        return
    delete_file.setEnabled(has_label_file())
