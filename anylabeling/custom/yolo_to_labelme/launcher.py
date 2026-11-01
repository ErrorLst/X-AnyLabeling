"""Lazy launcher for the YOLO to LabelMe window.

The window and its Qt imports happen inside the function body so that
importing the package during application start-up stays cheap.
"""

from __future__ import annotations

from typing import Any

__all__ = ["launch_yolo_to_labelme"]


def launch_yolo_to_labelme(parent: Any = None):
    """Open the window, reusing the existing instance."""

    from .dialog import YoloToLabelmeDialog

    dialog = getattr(parent, "_yolo_to_labelme_dialog", None)
    if dialog is None:
        dialog = YoloToLabelmeDialog(parent)
        if parent is not None:
            parent._yolo_to_labelme_dialog = dialog

        def _forget(_dialog=None, parent=parent):
            if parent is not None:
                parent._yolo_to_labelme_dialog = None

        dialog.destroyed.connect(_forget)
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    return dialog
