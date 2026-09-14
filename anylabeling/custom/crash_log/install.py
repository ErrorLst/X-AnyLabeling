"""Idempotent installer and full rollback of the crash log.

No step may raise: this module runs before the application is fully
alive and must never become a new source of crashes.
"""

from __future__ import annotations

import atexit
import os
import sys

from . import handlers, paths, session

_installed = False
_atexit_registered = False
_log_dir = None


def _is_worker_process():
    """True when multiprocessing started this process.

    multiprocessing.parent_process() alone is not enough. A spawn child
    re-executes the application module with runpy, as __mp_main__, and a
    frozen build re-runs the executable; both happen *before*
    BaseProcess._bootstrap() installs the parent link, so at the moment
    this module is imported in that child the parent is still unknown
    (measured on CPython 3.12.14). That re-execution is exactly what
    runs the mount point of app.py a second time, so the spawn module is
    checked as well. While the re-executed module runs, __main__ is
    still the spawn bootstrap; the module really named __mp_main__ is
    the signal. Testing mere membership is not enough:
    multiprocessing/__init__.py aliases __mp_main__ to __main__ on
    import (its own comment says the alias is reset when bootstrapping a
    child), and app.py imports multiprocessing, so membership is always
    true and would disable the marker in every normal run. A frozen
    worker never goes through runpy at all, which is what the second
    guard of session.prepare_session_marker() is for.
    """
    try:
        import multiprocessing

        if multiprocessing.parent_process() is not None:
            return True
    except Exception:
        pass
    try:
        main = sys.modules.get("__main__")
        if getattr(main, "__name__", "") == "__mp_main__":
            return True
        spawn_main = sys.modules.get("__mp_main__")
        if getattr(spawn_main, "__name__", "") == "__mp_main__":
            return True
    except Exception:
        pass
    try:
        # A frozen worker is the executable re-run as
        # "<exe> --multiprocessing-fork ..."; that is the same test the
        # PyInstaller runtime hook uses before it diverts to spawn_main.
        from multiprocessing import spawn as mp_spawn

        if mp_spawn.is_forking(sys.argv):
            return True
    except Exception:
        pass
    return False


def install_crash_log():
    """Install every hook once. Safe to call from anywhere."""
    global _installed, _atexit_registered, _log_dir
    if _installed:
        return
    if os.environ.get(paths.ENV_DISABLE, "") == "1":
        return

    # 1) Resolve the folder; None degrades to stderr mirroring only.
    log_dir = None
    try:
        log_dir = paths.resolve_log_directory()
    except Exception:
        log_dir = None

    # 2) Streams, prune, session header.
    try:
        handlers.start_session(log_dir)
    except Exception:
        pass

    # 3) faulthandler first: it must be live before anything else can
    #    take the process down.
    try:
        handlers.enable_faulthandler()
    except Exception:
        pass

    # 4) The three Python level hooks, each chained to its predecessor.
    try:
        handlers.install_python_hooks()
    except Exception:
        pass

    # 5) The application logger.
    try:
        handlers.attach_app_logger()
    except Exception:
        pass

    # 6) Previous-exit detection, then the marker of this run. A worker
    #    process must never report or steal the marker of the still
    #    running application it belongs to: spawning a worker during a
    #    model check would otherwise erase the crash evidence.
    try:
        if not _is_worker_process() and session.prepare_session_marker(
            log_dir
        ):
            if not _atexit_registered:
                atexit.register(session.mark_clean_exit, log_dir)
                _atexit_registered = True
    except Exception:
        pass

    # 7) The Qt handler, lazily and silently.
    try:
        handlers.install_qt_handler()
    except Exception:
        pass

    _log_dir = log_dir
    _installed = True


def uninstall_crash_log():
    """Restore every global touched by install_crash_log."""
    global _installed, _atexit_registered, _log_dir
    try:
        handlers.uninstall_qt_handler()
    except Exception:
        pass
    try:
        handlers.remove_python_hooks()
    except Exception:
        pass
    if _atexit_registered:
        try:
            atexit.unregister(session.mark_clean_exit)
        except Exception:
            pass
        _atexit_registered = False
    try:
        handlers.shutdown_session()
    except Exception:
        pass
    _installed = False
    _log_dir = None


def get_log_directory():
    """Return the folder resolved by this session, or None."""
    return _log_dir
