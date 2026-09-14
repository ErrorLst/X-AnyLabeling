"""Lazy launcher for the remote training sub window."""

from __future__ import annotations

from typing import Any

__all__ = ["launch_remote_training"]


def launch_remote_training(parent: Any = None):
    """Open the remote training window, reusing the existing instance."""

    from .ui.dialog import RemoteTrainingDialog

    dialog = getattr(parent, "_remote_training_dialog", None)
    if dialog is None:
        dialog = RemoteTrainingDialog(parent)
        if parent is not None:
            parent._remote_training_dialog = dialog

        def _forget(_dialog=None, parent=parent):
            if parent is not None:
                parent._remote_training_dialog = None

        dialog.destroyed.connect(_forget)
    dialog.showNormal()
    dialog.raise_()
    dialog.activateWindow()
    return dialog
