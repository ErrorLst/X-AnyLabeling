"""Crash logging for builds that have no console.

The packaged application is built with console=False, so an uncaught
exception (PyQt6 turns one escaping a slot into abort()) or a fatal
signal leaves no trace at all. This package mirrors every such failure
into ~/.xanylabeling/logs/xany-YYYYMMDD.log.

Only the standard library is imported at package import time; PyQt6 is
imported lazily inside the Qt handler installer.

Public surface:

    install_crash_log()    idempotent installer
    uninstall_crash_log()  full rollback (tests)
    get_log_directory()    folder resolved by this session, or None
"""

from .install import (
    get_log_directory,
    install_crash_log,
    uninstall_crash_log,
)

__all__ = (
    "install_crash_log",
    "uninstall_crash_log",
    "get_log_directory",
)
