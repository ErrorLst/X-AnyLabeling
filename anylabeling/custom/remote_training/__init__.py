"""Remote training sub-window.

Only the launcher is imported eagerly so that loading the package
never pulls in the Qt dialogs or the HTTP stack during start-up.
"""

from .launcher import launch_remote_training

__all__ = ["launch_remote_training"]
