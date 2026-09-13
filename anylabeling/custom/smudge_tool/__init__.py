"""Smudge tool: remove a defect with texture taken from elsewhere.

The tool is a project specific addition upstream does not offer, so it
lives inside ``anylabeling/custom`` and is attached to the labeling
widget from two mount lines in ``LabelingWidget.__init__``, one import
and one call to :func:`install_smudge_tool`. The package imports neither
``label_widget`` nor the canvas module; the widget is passed in, which
keeps the import graph acyclic and the modules unit testable.

Modules:

* ``texture_fill`` - the algorithm, pure numpy/OpenCV;
* ``operations`` - reading, writing, backups, guards and geometry;
* ``smudge_filter`` - the Qt layer: button, event filter, overlay, undo.

Upstream state this package leans on. An upstream upgrade has to check
every entry of this list; a rename breaks the tool at runtime, not at
import time:

widget: ``canvas``, ``tools``, ``populate_mode_actions``,
``import_image_folder``, ``menus`` (the menus carrying a drawing
action are filtered themselves, so that a click on one of their
entries leaves the mode before the entry runs),
``actions`` (the drawing actions inside it
included: eleven ``create_*`` names plus ``edit_mode`` and
``edit_brush_mode``; their ``triggered`` signal is connected while the
mode owns the canvas, so a click or a shortcut of one of them leaves
the mode and then runs the action -- the exact list is
``smudge_filter.DRAW_ACTION_NAMES``), ``set_edit_mode`` (the fallback
that takes the canvas back to editing when ``actions.edit_mode`` is
missing or disabled), ``filename``, ``image_path``, ``image_data``,
``brightness_contrast_processor``,
``brightness_contrast_values``, ``statusBar``, ``status``,
``error_message``.

canvas: ``transform_pos``, ``offset_to_center``, ``out_off_pixmap``,
``menus`` (the pair of context menus, filtered the same way),
``override_cursor``, ``restore_cursor``, ``load_pixmap``, ``pixmap``,
``scale``, ``isEnabled``, ``is_loading``, ``is_brush_mode``,
``is_magic_wand_mode``, ``drawing``, ``editing``, ``update``,
``setFocus``, ``geometry``, ``parentWidget``, ``mapToGlobal``,
``mouseMoveEvent`` (wrapped on the instance: upstream asks for the
default cursor as soon as a move hits no shape, so the cross of the
mode has to be put back after the original body), ``set_editing``
(wrapped on the instance: the one entry of every mode switch, and it
emits no signal, so the tool has to leave the mode from there; the
tool also calls it itself, to put the canvas into its create mode when
the mode is entered).

Undo history lives in memory only, one list of steps per image, and is
dropped when the folder changes, when another folder is opened, or at
the end of the process. The originals of the modified files stay in
``%TEMP%/dsh-smudge/<timestamp>-<pid>`` and are never deleted.
"""

from .smudge_filter import SmudgeController, install_smudge_tool

__all__ = [
    "SmudgeController",
    "install_smudge_tool",
]
