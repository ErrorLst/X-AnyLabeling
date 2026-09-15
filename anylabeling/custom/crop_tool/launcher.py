"""Lazy launcher for the crop sub window.

The dialog and its Qt imports happen inside the function body so that
importing the package during application start-up stays cheap.
"""

from __future__ import annotations

from typing import Any

__all__ = ["launch_crop_tool"]


def launch_crop_tool(parent: Any = None):
    """Open the crop window, reusing the existing instance.

    A second call returns the window that is already open instead of
    rescanning the folders: the window keeps its own source folder,
    output folder and image list.
    """

    from .dialog import CropDialog

    dialog = getattr(parent, "_crop_tool_dialog", None)
    if dialog is None:
        dialog = CropDialog(parent)
        if parent is not None:
            parent._crop_tool_dialog = dialog

        def _forget(_dialog=None, parent=parent):
            if parent is not None:
                parent._crop_tool_dialog = None

        dialog.destroyed.connect(_forget)
    dialog.showNormal()
    dialog.raise_()
    dialog.activateWindow()
    return dialog
