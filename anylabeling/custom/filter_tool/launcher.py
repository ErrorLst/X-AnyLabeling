"""Lazy launcher for the filter sub window.

The dialog and its Qt imports happen inside the function body so that
importing the package during application start-up stays cheap.
"""

from __future__ import annotations

from typing import Any

__all__ = ["launch_filter_tool"]


def launch_filter_tool(parent: Any = None):
    """Open the filter window, reusing the instance.

    A hidden window is reloaded first: the user may have confirmed or
    cancelled an earlier edit, and the main window may show another
    image by now. A window that is still visible is only raised, so the
    unsaved edits of an open editor are never thrown away.
    """

    from .dialog import FilterDialog

    dialog = getattr(parent, "_filter_tool_dialog", None)
    if dialog is None:
        dialog = FilterDialog(parent)
        if parent is not None:
            parent._filter_tool_dialog = dialog

        def _forget(_dialog=None, parent=parent):
            if parent is not None:
                parent._filter_tool_dialog = None

        dialog.destroyed.connect(_forget)
    elif not dialog.isVisible():
        dialog.reload()
    if dialog.isMaximized():
        dialog.showMaximized()
    elif dialog.isMinimized():
        dialog.showNormal()
    else:
        dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    return dialog
