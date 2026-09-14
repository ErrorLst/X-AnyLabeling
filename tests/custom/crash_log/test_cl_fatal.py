"""Real fatal failures recorded by faulthandler, in a child process."""

from __future__ import annotations

import os

import pytest

from anylabeling.custom.crash_log import paths

pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="the faulthandler signal probe is posix only",
)

# faulthandler._sigsegv() is the documented way to trigger a real
# crash; the ctypes fallback only runs if that helper disappears.
SEGV_SCRIPT = (
    "import ctypes\n"
    "import anylabeling.custom.crash_log as crash_log\n"
    "crash_log.install_crash_log()\n"
    "try:\n"
    "    import faulthandler\n"
    "    faulthandler._sigsegv()\n"
    "except AttributeError:\n"
    "    ctypes.CDLL(None).strlen(ctypes.c_char_p(0))\n"
)

ABRT_SCRIPT = (
    "import os\n"
    "import signal\n"
    "import anylabeling.custom.crash_log as crash_log\n"
    "crash_log.install_crash_log()\n"
    "os.kill(os.getpid(), signal.SIGABRT)\n"
)

EXC_SCRIPT = (
    "import anylabeling.custom.crash_log as crash_log\n"
    "crash_log.install_crash_log()\n"
    "raise SystemError('cl-fatal-probe')\n"
)


def test_sigsegv_is_recorded(log_dir, read_log, child):
    result = child(SEGV_SCRIPT, log_dir)
    assert result.returncode != 0
    text = read_log(log_dir)
    assert "Fatal Python error" in text
    assert "Segmentation fault" in text


def test_sigabrt_is_recorded(log_dir, read_log, child):
    result = child(ABRT_SCRIPT, log_dir)
    assert result.returncode != 0
    text = read_log(log_dir)
    assert "Fatal Python error" in text
    assert "Aborted" in text


def test_uncaught_exception_is_recorded(log_dir, read_log, child):
    result = child(EXC_SCRIPT, log_dir)
    assert result.returncode == 1
    text = read_log(log_dir)
    assert "UNCAUGHT EXCEPTION" in text
    assert "SystemError: cl-fatal-probe" in text
    assert "SESSION START" in text


def test_fatal_run_keeps_the_session_header(log_dir, read_log, child):
    child(SEGV_SCRIPT, log_dir)
    text = read_log(log_dir)
    assert "SESSION START" in text
    assert "frozen=False" in text
    assert "log_dir=%s" % log_dir in text
    assert "argv=" in text
