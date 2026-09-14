"""UI package of the remote training sub window (spec §5.1.3).

The package is imported lazily by `launcher.py`; only the dialog is part
of its public surface.
"""

from __future__ import annotations

__all__ = [
    "ApplicationCloseGuard",
    "ConfigPage",
    "DetailPage",
    "JobsPage",
    "RemoteTrainingDialog",
    "ResultsPage",
    "install_close_guard",
]


def __getattr__(name: str):
    """Import the heavy Qt modules only when one of them is asked for."""

    if name == "RemoteTrainingDialog":
        from .dialog import RemoteTrainingDialog

        return RemoteTrainingDialog
    if name in ("ApplicationCloseGuard", "install_close_guard"):
        from . import close_guard

        return getattr(close_guard, name)
    if name in ("ConfigPage", "JobsPage", "DetailPage", "ResultsPage"):
        from . import config_page, detail_page, jobs_page, results_page

        return {
            "ConfigPage": config_page.ConfigPage,
            "JobsPage": jobs_page.JobsPage,
            "DetailPage": detail_page.DetailPage,
            "ResultsPage": results_page.ResultsPage,
        }[name]
    raise AttributeError(name)
