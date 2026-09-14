"""handlers: the Python hooks, chaining and idempotency."""

from __future__ import annotations

import faulthandler
import logging
import sys
import threading
from types import SimpleNamespace

from anylabeling.custom import crash_log
from anylabeling.custom.crash_log import handlers


def test_excepthook_writes_traceback_and_chains(log_dir, read_log):
    original = sys.excepthook
    calls = []
    sys.excepthook = lambda *args: calls.append(args)
    try:
        crash_log.install_crash_log()
        assert sys.excepthook is handlers._excepthook
        try:
            raise ValueError("boom-in-main")
        except ValueError:
            handlers._excepthook(*sys.exc_info())
        assert len(calls) == 1
        assert calls[0][0] is ValueError
        text = read_log(log_dir)
        assert "===== UNCAUGHT EXCEPTION (thread=MainThread) =====" in text
        assert "ValueError: boom-in-main" in text
        assert "===== END UNCAUGHT EXCEPTION =====" in text
    finally:
        crash_log.uninstall_crash_log()
        sys.excepthook = original
    assert sys.excepthook is original


def test_threading_excepthook_names_the_thread(log_dir, read_log):
    original = threading.excepthook
    calls = []
    threading.excepthook = lambda args: calls.append(args)
    try:
        crash_log.install_crash_log()
        assert threading.excepthook is handlers._threading_excepthook

        def boom():
            raise RuntimeError("thread boom")

        thread = threading.Thread(target=boom, name="cl-boom-thread")
        thread.start()
        thread.join()
        assert len(calls) == 1
        text = read_log(log_dir)
        assert "(thread=cl-boom-thread)" in text
        assert "RuntimeError: thread boom" in text
    finally:
        crash_log.uninstall_crash_log()
        threading.excepthook = original
    assert threading.excepthook is original


def test_unraisablehook_writes_and_truncates(log_dir, read_log):
    original = sys.unraisablehook
    calls = []
    sys.unraisablehook = lambda unraisable: calls.append(unraisable)
    try:
        crash_log.install_crash_log()

        class Big:
            def __repr__(self):
                return "x" * 900

        fake = SimpleNamespace(
            exc_type=ValueError,
            exc_value=ValueError("unraisable boom"),
            exc_traceback=None,
            err_msg="Exception ignored in: probe",
            object=Big(),
        )
        handlers._unraisablehook(fake)
        assert calls == [fake]
        text = read_log(log_dir)
        assert "===== UNRAISABLE =====" in text
        assert "err_msg=Exception ignored in: probe" in text
        assert "ValueError: unraisable boom" in text
        assert "x" * handlers.REPR_LIMIT in text
        assert "x" * (handlers.REPR_LIMIT + 1) not in text
    finally:
        crash_log.uninstall_crash_log()
        sys.unraisablehook = original


def test_unraisablehook_catches_a_real_finalizer_error(
    log_dir, read_log
):
    original = sys.unraisablehook
    calls = []
    sys.unraisablehook = lambda unraisable: calls.append(unraisable)
    try:
        crash_log.install_crash_log()

        class Finalizer:
            def __del__(self):
                raise RuntimeError("finalizer boom")

        Finalizer()
        assert len(calls) == 1
        text = read_log(log_dir)
        assert "UNRAISABLE" in text
        assert "RuntimeError: finalizer boom" in text
    finally:
        crash_log.uninstall_crash_log()
        sys.unraisablehook = original


def test_previous_hook_raising_is_swallowed(log_dir, read_log):
    original = sys.excepthook
    calls = []

    def picky(*args):
        calls.append(args)
        raise RuntimeError("previous hook is broken")

    sys.excepthook = picky
    try:
        crash_log.install_crash_log()
        try:
            raise ValueError("boom-with-broken-prev")
        except ValueError:
            handlers._excepthook(*sys.exc_info())
        assert len(calls) == 1
        assert "ValueError: boom-with-broken-prev" in read_log(log_dir)
    finally:
        crash_log.uninstall_crash_log()
        sys.excepthook = original


def test_reentrant_hook_is_ignored(log_dir, read_log):
    original = sys.excepthook
    calls = []

    def reentrant(*args):
        calls.append(args)
        sys.excepthook(*args)

    sys.excepthook = reentrant
    try:
        crash_log.install_crash_log()
        try:
            raise ValueError("boom-reentrant")
        except ValueError:
            handlers._excepthook(*sys.exc_info())
        assert len(calls) == 1
        assert "ValueError: boom-reentrant" in read_log(log_dir)
    finally:
        crash_log.uninstall_crash_log()
        sys.excepthook = original


def test_broken_stream_only_stops_file_writes(
    log_dir, read_log, monkeypatch
):
    original = sys.excepthook
    calls = []
    sys.excepthook = lambda *args: calls.append(args)
    try:
        crash_log.install_crash_log()

        class Broken:
            def write(self, text):
                raise OSError("the disk went away")

            def flush(self):
                raise OSError("the disk went away")

        monkeypatch.setattr(handlers, "_log_stream", Broken())
        try:
            raise RuntimeError("after the break")
        except RuntimeError:
            handlers._excepthook(*sys.exc_info())
        assert handlers._broken is True
        assert len(calls) == 1
        assert sys.excepthook is handlers._excepthook
        assert handlers._fault_stream is not None
        assert "SESSION START" in read_log(log_dir)
    finally:
        crash_log.uninstall_crash_log()
        sys.excepthook = original


def test_install_is_idempotent(log_dir):
    crash_log.install_crash_log()
    hook = sys.excepthook
    handler = handlers._app_handler
    thread_hook = threading.excepthook
    crash_log.install_crash_log()
    assert sys.excepthook is hook
    assert threading.excepthook is thread_hook
    assert handlers._app_handler is handler
    logger = logging.getLogger(handlers.APP_LOGGER_NAME)
    assert sum(1 for item in logger.handlers if item is handler) == 1
    assert crash_log.get_log_directory() == log_dir


def test_uninstall_restores_every_global(log_dir):
    original_excepthook = sys.excepthook
    original_threading = threading.excepthook
    original_unraisable = sys.unraisablehook
    logger = logging.getLogger(handlers.APP_LOGGER_NAME)
    before = list(logger.handlers)
    crash_log.install_crash_log()
    assert sys.excepthook is handlers._excepthook
    assert crash_log.get_log_directory() == log_dir
    crash_log.uninstall_crash_log()
    assert sys.excepthook is original_excepthook
    assert threading.excepthook is original_threading
    assert sys.unraisablehook is original_unraisable
    assert logger.handlers == before
    assert crash_log.get_log_directory() is None
    assert handlers._log_stream is None
    assert handlers._fault_stream is None
    assert faulthandler.is_enabled() is False
    crash_log.uninstall_crash_log()


def test_install_survives_a_missing_pyqt6(log_dir, read_log, monkeypatch):
    monkeypatch.setitem(sys.modules, "PyQt6", None)
    monkeypatch.setitem(sys.modules, "PyQt6.QtCore", None)
    crash_log.install_crash_log()
    assert sys.excepthook is handlers._excepthook
    assert handlers._qt_installed is False
    assert handlers._qt_previous is None
    assert "SESSION START" in read_log(log_dir)


def test_faulthandler_is_enabled_with_a_stream(log_dir):
    crash_log.install_crash_log()
    assert faulthandler.is_enabled() is True
    assert handlers._faulthandler_on is True
    assert handlers._fault_stream is not None
