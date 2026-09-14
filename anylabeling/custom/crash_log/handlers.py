"""The crash hooks themselves.

Everything here is standard library; PyQt6 is imported lazily inside
install_qt_handler so that a broken Qt installation can never take
this module down. Every hook body is defensive on purpose: a hook that
raises turns a recoverable failure into a fatal one.
"""

from __future__ import annotations

import datetime
import faulthandler
import logging
import os
import sys
import threading
import traceback

from . import paths

SESSION_TAG = "SESSION"
EXC_TAG = "EXC"
THREAD_TAG = "THREAD"
UNRAISABLE_TAG = "UNRAISABLE"
QT_TAG = "QT"
APP_LOGGER_NAME = "X-AnyLabeling"
APP_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(message)s"
REPR_LIMIT = 500

# Sentinel: "no hook captured yet", so a legitimately None previous
# hook can be told apart from a missing one.
_UNSET = object()

# Message levels that are worth a line in the file; debug and info are
# only mirrored to stderr.
_QT_FILE_LEVELS = frozenset(("WARNING", "CRITICAL", "FATAL"))

_lock = threading.RLock()
_local = threading.local()
_broken = False

_installed_dir = None
_current_date = None
_log_stream = None
_fault_stream = None
_faulthandler_on = False

_prev_excepthook = _UNSET
_prev_threading_excepthook = _UNSET
_prev_unraisablehook = _UNSET

_qt_previous = None
_qt_installed = False

_app_logger = None
_app_handler = None


def _today():
    """Indirection kept patchable for the cross-day rotation test."""
    return datetime.date.today()


def _enter_hook():
    """True when the calling thread may run a hook body.

    The guard is per thread on purpose: a crash in one thread must not
    silence the hooks of another thread that is running at the same
    moment.
    """
    if getattr(_local, "active", False):
        return False
    _local.active = True
    return True


def _leave_hook():
    _local.active = False


def _write(tag, text):
    """Append one tagged block to the log file. Never raises."""
    global _broken
    if _broken:
        return
    with _lock:
        try:
            _rotate_if_needed()
        except Exception:
            pass
        stream = _log_stream
        if stream is None:
            return
        try:
            stamp = datetime.datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            prefix = "%s | %-10s | " % (stamp, tag)
            body = text if text.endswith("\n") else text + "\n"
            lines = body.splitlines() or [""]
            stream.write(
                "".join(prefix + line + "\n" for line in lines)
            )
            stream.flush()
        except Exception:
            # Broken writer: stop hammering the file, keep the hooks.
            _broken = True


def _safe_call(previous, *args, **kwargs):
    """Call a previous hook, swallowing everything it may raise."""
    if not callable(previous):
        return
    try:
        previous(*args, **kwargs)
    except Exception:
        pass


def _exception_block(title, thread_name, lines):
    """Build the block written for one uncaught exception."""
    body = "".join(lines)
    text = "===== %s (thread=%s) =====\n" % (title, thread_name)
    text += body if body.endswith("\n") else body + "\n"
    text += "===== END %s =====" % title
    return text


def _excepthook(exc_type, exc_value, exc_tb):
    """sys.excepthook replacement: log first, then chain."""
    if not _enter_hook():
        return
    try:
        try:
            block = _exception_block(
                "UNCAUGHT EXCEPTION",
                "MainThread",
                traceback.format_exception(exc_type, exc_value, exc_tb),
            )
            _write(EXC_TAG, block)
        except Exception:
            pass
        _safe_call(_prev_excepthook, exc_type, exc_value, exc_tb)
    finally:
        _leave_hook()


def _threading_excepthook(args):
    """threading.excepthook replacement: log first, then chain."""
    if not _enter_hook():
        return
    try:
        thread_name = "unknown"
        try:
            thread_name = getattr(args.thread, "name", None) or "unknown"
        except Exception:
            pass
        try:
            block = _exception_block(
                "UNCAUGHT EXCEPTION",
                thread_name,
                traceback.format_exception(
                    args.exc_type,
                    args.exc_value,
                    args.exc_traceback,
                ),
            )
            _write(THREAD_TAG, block)
        except Exception:
            pass
        _safe_call(_prev_threading_excepthook, args)
    finally:
        _leave_hook()


def _unraisablehook(unraisable):
    """sys.unraisablehook replacement: log first, then chain."""
    if not _enter_hook():
        return
    try:
        try:
            block = _unraisable_block(unraisable)
            _write(UNRAISABLE_TAG, block)
        except Exception:
            pass
        _safe_call(_prev_unraisablehook, unraisable)
    finally:
        _leave_hook()


def _unraisable_block(unraisable):
    text = "===== UNRAISABLE =====\n"
    try:
        err_msg = getattr(unraisable, "err_msg", None)
    except Exception:
        err_msg = None
    if err_msg:
        text += "err_msg=%s\n" % err_msg
    try:
        target = getattr(unraisable, "object", None)
        rendered = "None" if target is None else repr(target)
    except Exception:
        rendered = "<unrepresentable>"
    text += "object=%s\n" % rendered[:REPR_LIMIT]
    text += "".join(
        traceback.format_exception(
            unraisable.exc_type,
            unraisable.exc_value,
            unraisable.exc_traceback,
        )
    )
    text += "===== END UNRAISABLE ====="
    return text


def _qt_level_name(msg_type):
    """Normalise QtWarningMsg / Warning into WARNING."""
    name = getattr(msg_type, "name", None) or str(msg_type)
    name = name.upper()
    if name.startswith("QT"):
        name = name[2:]
    if name.endswith("MSG"):
        name = name[:-3]
    return name


def _qt_message_handler(msg_type, context, message):
    """Qt message handler: file for warnings and worse, mirror stderr.

    The whole body is guarded: an exception raised from a Qt message
    handler makes Qt call qFatal and abort the process.
    """
    if not _enter_hook():
        return
    try:
        try:
            level = getattr(msg_type, "name", None) or str(msg_type)
            level = level.upper()
            if _qt_level_name(msg_type) in _QT_FILE_LEVELS:
                _write(QT_TAG, "%-14s %s" % (level, message))
        except Exception:
            pass
        try:
            if callable(_qt_previous):
                _qt_previous(msg_type, context, message)
            elif sys.stderr is not None:
                sys.stderr.write("%s\n" % message)
                sys.stderr.flush()
        except Exception:
            pass
    finally:
        _leave_hook()


def _close_streams():
    """Close both handles, ignoring every failure."""
    global _log_stream, _fault_stream
    for stream in (_log_stream, _fault_stream):
        if stream is None:
            continue
        try:
            stream.close()
        except Exception:
            pass
    _log_stream = None
    _fault_stream = None


def _reopen(date):
    """Switch both handles to the file of date."""
    global _current_date, _log_stream, _fault_stream
    _close_streams()
    log_stream, fault_stream = paths.open_streams(
        _installed_dir, date
    )
    _log_stream = log_stream
    _fault_stream = fault_stream
    _current_date = date
    _rebind_app_handler()


def _rebind_app_handler():
    """Keep the application logger on the stream that is alive now.

    Without this the handler keeps writing into the stream the rotation
    just closed: every later record raises inside emit() and is dropped
    by logging's error handling, so the application log silently
    disappears until the next restart. The assignment is direct on
    purpose; Handler.setStream() flushes the old stream first, and that
    is the very closed object that must not be touched.
    """
    if _app_handler is None or _log_stream is None:
        return
    try:
        _app_handler.stream = _log_stream
    except Exception:
        pass


def _rotate_if_needed():
    """Lazily move to the file of the new day, pruning on the way."""
    if _installed_dir is None:
        return
    try:
        today = _today()
    except Exception:
        return
    if today == _current_date:
        return
    with _lock:
        if today == _current_date:
            return
        _reopen(today)
        try:
            paths.prune_old_logs(_installed_dir)
        except Exception:
            pass
        # faulthandler holds a duplicate of the old descriptor; move it
        # to the new file instead of letting it write into a stale one.
        _restore_faulthandler()


def _session_header(log_dir):
    frozen = bool(getattr(sys, "frozen", False))
    lines = [
        "===== SESSION START =====",
        "pid=%s" % os.getpid(),
        "python=%s" % sys.version.replace("\n", " "),
        "frozen=%s" % frozen,
        "log_dir=%s" % log_dir,
    ]
    try:
        lines.append("executable=%s" % sys.executable)
    except Exception:
        pass
    try:
        lines.append("argv=%s" % (sys.argv,))
    except Exception:
        pass
    return "\n".join(lines)


def enable_faulthandler():
    """Enable faulthandler on the fault stream. First hook installed."""
    global _faulthandler_on
    if _fault_stream is None:
        return False
    try:
        faulthandler.enable(file=_fault_stream, all_threads=True)
    except Exception:
        return False
    _faulthandler_on = True
    return True


def disable_faulthandler():
    global _faulthandler_on
    if not _faulthandler_on:
        return
    try:
        faulthandler.disable()
    except Exception:
        pass
    _faulthandler_on = False


def _restore_faulthandler():
    if not _faulthandler_on or _fault_stream is None:
        return
    try:
        faulthandler.disable()
        faulthandler.enable(file=_fault_stream, all_threads=True)
    except Exception:
        pass


def install_python_hooks():
    """Install the three Python level hooks, chaining the old ones."""
    global _prev_excepthook, _prev_threading_excepthook
    global _prev_unraisablehook
    if _prev_excepthook is _UNSET:
        _prev_excepthook = sys.excepthook
        sys.excepthook = _excepthook
    if _prev_threading_excepthook is _UNSET:
        _prev_threading_excepthook = getattr(
            threading, "excepthook", None
        )
        threading.excepthook = _threading_excepthook
    if _prev_unraisablehook is _UNSET:
        _prev_unraisablehook = getattr(sys, "unraisablehook", None)
        sys.unraisablehook = _unraisablehook


def remove_python_hooks():
    """Restore exactly what was captured, idempotently."""
    global _prev_excepthook, _prev_threading_excepthook
    global _prev_unraisablehook
    if _prev_excepthook is not _UNSET:
        sys.excepthook = _prev_excepthook
        _prev_excepthook = _UNSET
    if _prev_threading_excepthook is not _UNSET:
        threading.excepthook = _prev_threading_excepthook
        _prev_threading_excepthook = _UNSET
    if _prev_unraisablehook is not _UNSET:
        if _prev_unraisablehook is None:
            try:
                del sys.unraisablehook
            except AttributeError:
                pass
        else:
            sys.unraisablehook = _prev_unraisablehook
        _prev_unraisablehook = _UNSET


def attach_app_logger(name=APP_LOGGER_NAME):
    """Mirror the application logger into the log file.

    propagate is left alone and the root logger is never touched.
    """
    global _app_logger, _app_handler
    stream = _log_stream
    if stream is None:
        return False
    if _app_handler is not None:
        return True
    try:
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter(APP_LOG_FORMAT))
        logger = logging.getLogger(name)
        logger.addHandler(handler)
    except Exception:
        return False
    _app_logger = logger
    _app_handler = handler
    return True


def detach_app_logger():
    global _app_logger, _app_handler
    handler = _app_handler
    logger = _app_logger
    _app_handler = None
    _app_logger = None
    if handler is None or logger is None:
        return
    try:
        logger.removeHandler(handler)
    except Exception:
        pass


def install_qt_handler():
    """Install the Qt message handler. Missing PyQt6 is not an error."""
    global _qt_previous, _qt_installed
    if _qt_installed:
        return False
    try:
        from PyQt6 import QtCore
    except ImportError:
        return False
    except Exception:
        return False
    try:
        previous = QtCore.qInstallMessageHandler(_qt_message_handler)
    except Exception:
        return False
    _qt_previous = previous
    _qt_installed = True
    return True


def uninstall_qt_handler():
    global _qt_previous, _qt_installed
    if not _qt_installed:
        return
    previous = _qt_previous
    _qt_previous = None
    _qt_installed = False
    try:
        from PyQt6 import QtCore
        QtCore.qInstallMessageHandler(previous)
    except Exception:
        pass


def start_session(log_dir):
    """Open the day file, prune, and write the session header."""
    global _installed_dir, _broken, _current_date
    global _log_stream, _fault_stream
    with _lock:
        disable_faulthandler()
        _installed_dir = log_dir
        _broken = False
        _current_date = None
        _close_streams()
        if log_dir is None:
            return
        try:
            _reopen(_today())
        except Exception:
            _log_stream = None
            _fault_stream = None
        try:
            paths.prune_old_logs(log_dir)
        except Exception:
            pass
        _write(SESSION_TAG, _session_header(log_dir))


def shutdown_session():
    """Disable faulthandler first, then close the streams."""
    global _installed_dir, _current_date
    with _lock:
        disable_faulthandler()
        detach_app_logger()
        _close_streams()
        _installed_dir = None
        _current_date = None
