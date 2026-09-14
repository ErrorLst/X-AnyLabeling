"""Reset the canvas view to its default state when another image loads.

Upstream load_file remembers the zoom and the scroll of every image it
has shown, so a later visit restores the old view (zoom_values), keeps
the zoom_mode of the previous image, and with keep_prev_scale on it
carries the scale of the previous image over. This project specific
feature gives every image the view of the first image of a session
instead: the switch to another file resets the zoom to fit window and
forgets the per file memory entries of that file.

The behaviour is attached to the labeling widget by wrapping its
load_file method from a single mount point in the constructor of
LabelingWidget; no upstream method body is touched. The instance level
wrapper is the outermost one on purpose: it is installed after the
wrapper of ensure_label_file, so the pre and post steps below see the
final value of widget.filename, whatever the other wrappers do. A later
feature that wraps load_file as well must append its mount point after
the call of install_reset_view_on_switch to stay inside that window.

Neither module imports `label_widget`, the widget is passed in and used
at runtime, which keeps the mount point in that module acyclic and the
module unit testable.

The reset deliberately ignores the keep_prev_scale setting of the
configuration; see reset_view_on_switch below and the known pitfalls of
docs/custom/FEATURES.md.
"""

from .view import install_reset_view_on_switch, reset_view_on_switch

__all__ = [
    "install_reset_view_on_switch",
    "reset_view_on_switch",
]
