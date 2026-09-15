"""Lazy launcher for the preview tool sub window.

The dialog and its Qt imports happen inside the function body so that
importing the package during application start-up stays cheap.
"""

from __future__ import annotations

from typing import Any

__all__ = ["launch_preview_tool"]


def launch_preview_tool(parent: Any = None):
    """Open the preview window of a widget, reusing the instance.

    The window of a widget lives until it is closed: closing destroys
    it, and the destroyed signal clears the attribute, so the next call
    builds a fresh window on the folder the widget holds then. While an
    instance is alive the call only brings it up again, after asking it
    to read its folder once more - the files may have changed while the
    window was in the background.

    The widget is handed over twice: as the parent of the window and as
    the explorer it takes its initial folder from. Nothing else of the
    widget is read, by this module or by the window.
    """

    from .dialog import PreviewDialog

    dialog = getattr(parent, "_preview_tool_dialog", None)
    if dialog is None:
        dialog = PreviewDialog(parent, explorer=parent)
        if parent is not None:
            parent._preview_tool_dialog = dialog

        def _forget(_dialog=None, parent=parent):
            if parent is not None:
                parent._preview_tool_dialog = None

        dialog.destroyed.connect(_forget)
    else:
        dialog.rescan()
    dialog.showNormal()
    dialog.raise_()
    dialog.activateWindow()
    return dialog
