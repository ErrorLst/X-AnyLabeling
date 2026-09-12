"""Lazy launcher for the model validation sub window.

The heavy imports (onnxruntime, albumentations and the Qt dialogs)
happen inside the function body so that importing the package during
application start-up stays cheap.
"""

from __future__ import annotations

from typing import Any

__all__ = ["launch_model_validation"]


def launch_model_validation(parent: Any = None):
    """Open the model validation window, reusing the existing instance."""

    from .ui.dialog import ModelValidationDialog

    dialog = getattr(parent, "_model_validation_dialog", None)
    if dialog is None:
        dialog = ModelValidationDialog(parent)
        if parent is not None:
            parent._model_validation_dialog = dialog

        def _forget(_dialog=None, parent=parent):
            if parent is not None:
                parent._model_validation_dialog = None

        dialog.destroyed.connect(_forget)
    dialog.showNormal()
    dialog.raise_()
    dialog.activateWindow()
    return dialog
