"""Session marker: telling an abnormal exit from a clean one.

A packaged build that is killed (crash, force quit, power loss) never
runs any shutdown code, so the only trace it leaves is the marker file
that the next start finds. The marker belongs to one log folder and is
removed by atexit only by the very process that wrote it.
"""

from __future__ import annotations

import datetime
import os
import sys

from .handlers import _write

MARKER_NAME = "xany-session.marker"
SESSION_TAG = "SESSION"
STALE_MESSAGE = (
    "上一次会话未正常结束，可能是崩溃、被强制结束或断电。"
)


def marker_path(log_dir):
    return os.path.join(log_dir, MARKER_NAME)


def read_marker(log_dir):
    """Return the marker fields as a dict, or None when absent."""
    if log_dir is None:
        return None
    try:
        with open(marker_path(log_dir), "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return None
    fields = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            fields[key.strip()] = value.strip()
    return fields


def write_marker(log_dir, pid=None, started=None):
    """Claim the marker for this process. Failures are silent."""
    if log_dir is None:
        return False
    if pid is None:
        pid = os.getpid()
    if started is None:
        started = datetime.datetime.now().replace(
            microsecond=0
        ).isoformat()
    body = "pid=%s\nstarted=%s\n" % (pid, started)
    try:
        with open(marker_path(log_dir), "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
    except OSError:
        return False
    return True


def _owned_by_parent(stale):
    """True when the marker belongs to the process that started us.

    A worker started by the application (multiprocessing spawn re-runs
    this module, a frozen build re-runs the executable) finds the
    marker of its still running parent. That marker is neither an
    abnormal exit nor ours to take over, so it must stay untouched.

    os.getppid() is used instead of a liveness probe on purpose:
    os.kill(pid, 0) terminates a process on Windows, which would turn
    this safety check into a way to kill the application.
    """
    try:
        pid = int(stale.get("pid", ""))
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        return pid == os.getppid()
    except OSError:
        return False


def prepare_session_marker(log_dir):
    """Report a previous abnormal exit, then claim the marker.

    Returns True when this process now owns the marker.
    """
    if log_dir is None:
        return False
    stale = read_marker(log_dir)
    if stale is not None:
        if _owned_by_parent(stale):
            return False
        report_previous_abnormal_exit(stale, log_dir)
    return write_marker(log_dir)


def report_previous_abnormal_exit(stale, log_dir):
    """Write the loud warning for a marker left behind by a dead run."""
    pid = stale.get("pid", "?")
    started = stale.get("started", "?")
    _write(
        SESSION_TAG,
        "===== PREVIOUS SESSION DID NOT EXIT CLEANLY =====\n"
        "pid=%s\nstarted=%s\nlog_dir=%s\n%s\n"
        "===== END PREVIOUS SESSION ====="
        % (pid, started, log_dir, STALE_MESSAGE),
    )
    stream = getattr(sys, "stderr", None)
    if stream is None:
        return
    try:
        stream.write(
            "WARNING: %s (pid=%s, started=%s, log_dir=%s)\n"
            % (STALE_MESSAGE, pid, started, log_dir)
        )
        stream.flush()
    except Exception:
        pass


def mark_clean_exit(log_dir=None):
    """atexit callback: drop the marker, but only our own."""
    stale = read_marker(log_dir)
    if not stale:
        return False
    if stale.get("pid") != str(os.getpid()):
        return False
    try:
        os.remove(marker_path(log_dir))
    except OSError:
        return False
    return True
