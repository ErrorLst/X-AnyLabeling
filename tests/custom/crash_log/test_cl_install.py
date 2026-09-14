"""install_crash_log: the disable switch and worker protection."""

from __future__ import annotations

import faulthandler
import json
import logging
import os
import os.path as osp
import sys
import threading

from anylabeling.custom import crash_log
from anylabeling.custom.crash_log import handlers, paths, session

# The outer process plays the application: it installs the crash log at
# module level, exactly like app.py does. The spawn child re-executes
# this very file as __mp_main__ (running the same module level install)
# before it runs the target of cl_spawn_worker.
SPAWN_MAIN = '''
import json
import os

from anylabeling.custom.crash_log import install_crash_log, session

LOG_DIR = os.environ["XANY_LOG_DIR"]

install_crash_log()
print("STARTED_PID", os.getpid(), flush=True)

if __name__ == "__main__":
    import multiprocessing

    import cl_spawn_worker

    context = multiprocessing.get_context("spawn")
    process = context.Process(target=cl_spawn_worker.work)
    process.start()
    process.join()
    fields = session.read_marker(LOG_DIR) or {}
    print("MARKER", json.dumps(fields), flush=True)
    print(
        "MARKER_EXISTS",
        os.path.exists(session.marker_path(LOG_DIR)),
        flush=True,
    )
    print("CHILD_EXIT", process.exitcode, flush=True)
'''

SPAWN_WORKER = '''"""Target of the spawn regression test."""

import os


def work():
    from anylabeling.custom.crash_log import install_crash_log

    install_crash_log()
    print("WORKER_PID", os.getpid(), flush=True)
'''


def _fields(stdout):
    found = {}
    for line in stdout.splitlines():
        if line.startswith(("MARKER ", "MARKER_EXISTS ", "CHILD_EXIT ")):
            key, value = line.split(" ", 1)
            found[key] = value
    return found


def test_disable_env_leaves_no_trace(tmp_path, monkeypatch):
    target = tmp_path / "never-created"
    monkeypatch.setenv(paths.ENV_DIR, str(target))
    monkeypatch.setenv(paths.ENV_DISABLE, "1")

    logger = logging.getLogger(handlers.APP_LOGGER_NAME)
    handlers_before = list(logger.handlers)
    excepthook = sys.excepthook
    thread_hook = threading.excepthook
    unraisable = sys.unraisablehook
    enabled_before = faulthandler.is_enabled()

    crash_log.install_crash_log()

    assert sys.excepthook is excepthook
    assert threading.excepthook is thread_hook
    assert sys.unraisablehook is unraisable
    assert faulthandler.is_enabled() is enabled_before
    assert logger.handlers == handlers_before
    assert crash_log.get_log_directory() is None
    assert handlers._log_stream is None
    assert handlers._fault_stream is None
    assert not target.exists()
    assert not osp.exists(osp.join(str(target), session.MARKER_NAME))


def test_disable_env_requires_the_value_one(log_dir, monkeypatch):
    monkeypatch.setenv(paths.ENV_DISABLE, "true")
    crash_log.install_crash_log()
    assert sys.excepthook is handlers._excepthook
    assert crash_log.get_log_directory() == log_dir


def test_spawn_worker_keeps_the_marker_of_its_parent(
    log_dir, read_log, tmp_path, child, repo_root
):
    helper = tmp_path / "probe"
    helper.mkdir()
    main_path = helper / "cl_spawn_main.py"
    main_path.write_text(SPAWN_MAIN, encoding="utf-8")
    (helper / "cl_spawn_worker.py").write_text(
        SPAWN_WORKER, encoding="utf-8"
    )
    python_path = os.pathsep.join(
        [repo_root, os.environ.get("PYTHONPATH", "")]
    )

    result = child(
        log_dir=log_dir,
        script_path=main_path,
        extra_env={"PYTHONPATH": python_path},
    )
    assert result.returncode == 0, result.stderr

    started = [
        line.split()[1]
        for line in result.stdout.splitlines()
        if line.startswith("STARTED_PID ")
    ]
    # The application module ran twice: in the process that owns the
    # marker and, re-executed by spawn as __mp_main__, in the worker.
    assert len(started) == 2, result.stdout
    parent_pid = started[0]

    fields = _fields(result.stdout)
    assert fields["CHILD_EXIT"] == "0", result.stderr
    assert fields["MARKER_EXISTS"] == "True"
    assert json.loads(fields["MARKER"])["pid"] == parent_pid

    text = read_log(log_dir)
    assert "PREVIOUS SESSION DID NOT EXIT CLEANLY" not in text
    assert "未正常结束" not in result.stderr
    # The worker still logs for itself, it just never touches the
    # marker: one session header for the parent, one for the worker.
    assert text.count("SESSION START") == 2
    # The parent exited cleanly, so its own marker is gone by now.
    assert not osp.exists(session.marker_path(log_dir))
