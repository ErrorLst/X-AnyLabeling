"""Shared fixtures for the crash log tests.

Nothing here ever touches the real ~/.xanylabeling/logs: every case
works inside a tmp_path folder announced through XANY_LOG_DIR, and the
module level globals of the package are restored before and after each
case.

Child processes skip the heavy anylabeling package initialisation by
stubbing the two parent packages with modules that only carry a
__path__; the crash log package itself is then the real one.
"""

from __future__ import annotations

import os
import os.path as osp
import subprocess
import sys
import types

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from anylabeling.custom import crash_log  # noqa: E402
from anylabeling.custom.crash_log import paths  # noqa: E402

REPO_ROOT = osp.abspath(
    osp.join(osp.dirname(__file__), "..", "..", "..")
)

#: Replaces anylabeling and anylabeling.custom with empty packages that
#: point at the real source tree, so importing the crash log package
#: never drags in cv2 / numpy / PyQt6 / the whole application.
BOOTSTRAP = '''
import os
import sys
import types

ROOT = {root!r}
for _name, _relative in (
    ("anylabeling", "anylabeling"),
    ("anylabeling.custom", os.path.join("anylabeling", "custom")),
):
    _module = types.ModuleType(_name)
    _module.__path__ = [os.path.join(ROOT, _relative)]
    _module.__package__ = _name
    sys.modules[_name] = _module
'''


class ChildResult(types.SimpleNamespace):
    """Outcome of one child process run."""

    def __init__(self, returncode, stdout, stderr, pid):
        super().__init__(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            pid=pid,
        )


@pytest.fixture(autouse=True)
def crash_log_reset():
    """Every case starts and ends with the globals fully restored."""

    crash_log.uninstall_crash_log()
    yield
    crash_log.uninstall_crash_log()


@pytest.fixture(scope="session")
def repo_root():
    """Absolute path of the repository root."""

    return REPO_ROOT


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    """Isolated log folder, announced through XANY_LOG_DIR."""

    path = tmp_path / "logs"
    path.mkdir()
    monkeypatch.setenv(paths.ENV_DIR, str(path))
    return str(path)


@pytest.fixture
def read_log():
    """Read back the log file of a folder (today by default)."""

    def _read(folder, date=None):
        path = paths.log_file_path(str(folder), date)
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    return _read


@pytest.fixture
def child():
    """Run code in a fresh interpreter.

    ``script`` runs through -c on top of BOOTSTRAP. ``script_path``
    runs that file as __main__ instead, which is what a real
    multiprocessing spawn child re-executes.
    """

    def _run(script=None, log_dir=None, extra_env=None, timeout=180,
             script_path=None):
        env = dict(os.environ)
        env["QT_QPA_PLATFORM"] = "offscreen"
        env.pop(paths.ENV_DIR, None)
        env.pop(paths.ENV_DISABLE, None)
        if log_dir is not None:
            env[paths.ENV_DIR] = str(log_dir)
        if extra_env:
            env.update(extra_env)
        if script_path is not None:
            command = [sys.executable, str(script_path)]
        else:
            command = [
                sys.executable,
                "-c",
                BOOTSTRAP.format(root=REPO_ROOT) + (script or ""),
            ]
        process = subprocess.Popen(
            command,
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = process.communicate(timeout=timeout)
        return ChildResult(process.returncode, stdout, stderr, process.pid)

    return _run


def marker_of(folder):
    """Return the marker path of a log folder."""

    return osp.join(str(folder), "xany-session.marker")
