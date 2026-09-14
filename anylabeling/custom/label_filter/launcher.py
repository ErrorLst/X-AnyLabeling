"""Lazy launcher for the label filter sub window.

The dialog and its Qt imports happen inside the function body so that
importing the package during application start-up stays cheap.
"""

from __future__ import annotations

from typing import Any

__all__ = ["launch_label_filter"]


def launch_label_filter(parent: Any = None):
    """Open the label filter window, reusing the instance.

    The window of a widget lives for the whole session, so the same
    instance is shown again. Its list is rebuilt first: another folder
    may have been opened since it was shown last, and the window has
    to describe the folder the widget holds now. The very first
    opening scans in the constructor, which is why a reused window is
    the only one that scans here - a first opening never scans twice.
    """

    from .dialog import LabelFilterDialog

    dialog = getattr(parent, "_label_filter_dialog", None)
    if dialog is None:
        dialog = LabelFilterDialog(parent)
        if parent is not None:
            parent._label_filter_dialog = dialog

        def _forget(_dialog=None, parent=parent):
            if parent is not None:
                parent._label_filter_dialog = None

        dialog.destroyed.connect(_forget)
    else:
        dialog.rescan()
    dialog.showNormal()
    dialog.raise_()
    dialog.activateWindow()
    return dialog
