"""Model validation sub-window.

Only the launcher is imported eagerly so that loading the package
never pulls in onnxruntime or albumentations during start-up.
"""

from .launcher import launch_model_validation

__all__ = ["launch_model_validation"]
