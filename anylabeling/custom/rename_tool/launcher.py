"""Lazy launcher for the rename sub window.

The dialog and its Qt imports happen inside the function body so that
importing the package during application start-up stays cheap.
"""

from __future__ import annotations

from typing import Any

__all__ = ["launch_rename_tool"]


def launch_rename_tool(parent: Any = None):
    """Open the rename window, reusing the existing instance."""

    from .dialog import RenameDialog

    dialog = getattr(parent, "_rename_tool_dialog", None)
    if dialog is None:
        dialog = RenameDialog(parent)
        if parent is not None:
            parent._rename_tool_dialog = dialog

        def _forget(_dialog=None, parent=parent):
            if parent is not None:
                parent._rename_tool_dialog = None

        dialog.destroyed.connect(_forget)
    dialog.showNormal()
    dialog.raise_()
    dialog.activateWindow()
    return dialog
