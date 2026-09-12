"""Wheel zoom extra for the labeling window.

The behaviour is a project specific addition that upstream does not
offer, so it lives inside ``anylabeling/custom`` and is attached to the
labeling widget from a single mount point in ``LabelingWidget.__init__``.
The package imports neither ``label_widget`` nor the canvas module; the
widget is passed in, which keeps the import graph acyclic and the module
unit testable.
"""

from .wheel_zoom import install_wheel_zoom

__all__ = [
    "install_edit_extras",
    "install_wheel_zoom",
]


def install_edit_extras(widget):
    """Attach the custom edit helpers to a labeling widget.

    Args:
        widget: The labeling widget to extend. It has to expose ``canvas``.

    Returns:
        The installed ``WheelZoomFilter``, or ``None`` when the widget has no
        canvas.
    """
    if getattr(widget, "_edit_extras_installed", False):
        return getattr(widget, "_wheel_zoom_filter", None)
    wheel_filter = install_wheel_zoom(widget)
    widget._edit_extras_installed = True
    return wheel_filter
