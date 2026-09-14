"""session: marker lifecycle and abnormal exit detection."""

from __future__ import annotations

import datetime
import os
import os.path as osp
import signal

import pytest

from anylabeling.custom import crash_log
from anylabeling.custom.crash_log import session

INSTALL_SCRIPT = (
    "import anylabeling.custom.crash_log as crash_log\n"
    "crash_log.install_crash_log()\n"
)

KILL_SCRIPT = (
    "import os\n"
    "import signal\n"
    "import anylabeling.custom.crash_log as crash_log\n"
    "crash_log.install_crash_log()\n"
    "os.kill(os.getpid(), signal.SIGKILL)\n"
)


def test_marker_format(log_dir):
    crash_log.install_crash_log()
    path = session.marker_path(log_dir)
    assert osp.isfile(path)
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    assert lines[0] == "pid=%s" % os.getpid()
    assert lines[1].startswith("started=")
    datetime.datetime.fromisoformat(lines[1].split("=", 1)[1])


def test_stale_marker_is_reported_and_rewritten(
    log_dir, read_log, capsys
):
    session.write_marker(
        log_dir, pid=424242, started="2020-01-01T00:00:00"
    )
    crash_log.install_crash_log()
    text = read_log(log_dir)
    assert "PREVIOUS SESSION DID NOT EXIT CLEANLY" in text
    assert "未正常结束" in text
    assert "可能是崩溃" in text
    assert "424242" in text
    assert "===== END PREVIOUS SESSION =====" in text
    assert read_log(log_dir).count("PREVIOUS SESSION") == 2
    fields = session.read_marker(log_dir)
    assert fields["pid"] == str(os.getpid())
    assert "未正常结束" in capsys.readouterr().err


def test_first_run_has_no_previous_session_warning(log_dir, read_log):
    crash_log.install_crash_log()
    assert "PREVIOUS SESSION" not in read_log(log_dir)


def test_marker_of_a_running_parent_is_left_alone(log_dir, read_log):
    """A worker must not report or take over its parent's marker.

    This is the frozen-build half of the protection: the child re-runs
    the executable before multiprocessing sets the parent link, so the
    only thing that identifies the marker as a live session is that it
    belongs to our own parent process.
    """

    parent = os.getppid()
    if parent <= 0:
        pytest.skip("no usable parent pid")
    session.write_marker(
        log_dir, pid=parent, started="2020-01-01T00:00:00"
    )
    crash_log.install_crash_log()
    assert "PREVIOUS SESSION" not in read_log(log_dir)
    assert session.read_marker(log_dir)["pid"] == str(parent)


def test_mark_clean_exit_only_drops_our_own_marker(log_dir):
    session.write_marker(log_dir, pid=999999)
    assert session.mark_clean_exit(log_dir) is False
    assert osp.isfile(session.marker_path(log_dir))
    session.write_marker(log_dir)
    assert session.mark_clean_exit(log_dir) is True
    assert not osp.exists(session.marker_path(log_dir))
    assert session.mark_clean_exit(log_dir) is False


def test_clean_child_exit_removes_the_marker(log_dir, child):
    result = child(INSTALL_SCRIPT, log_dir)
    assert result.returncode == 0, result.stderr
    assert not osp.exists(session.marker_path(log_dir))


def test_killed_child_leaves_the_marker(log_dir, child):
    result = child(KILL_SCRIPT, log_dir)
    assert result.returncode == -signal.SIGKILL, result.stderr
    path = session.marker_path(log_dir)
    assert osp.isfile(path)
    fields = session.read_marker(log_dir)
    assert fields["pid"] == str(result.pid)


def test_killed_child_is_reported_by_the_next_start(
    log_dir, child, read_log
):
    killed = child(KILL_SCRIPT, log_dir)
    assert killed.returncode == -signal.SIGKILL
    again = child(INSTALL_SCRIPT, log_dir)
    assert again.returncode == 0, again.stderr
    text = read_log(log_dir)
    assert "PREVIOUS SESSION DID NOT EXIT CLEANLY" in text
    assert "pid=%s" % killed.pid in text
    assert "未正常结束" in again.stderr
    assert not osp.exists(session.marker_path(log_dir))
