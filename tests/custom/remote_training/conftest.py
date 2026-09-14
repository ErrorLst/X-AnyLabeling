"""Offscreen Qt setup for the remote training UI tests.

The window is a PyQt6 dialog, so every test here needs the offscreen
platform (no display on the build host) and exactly one
`QApplication` for the whole session.  The dataset fixtures live in
`tmp_path`: the repository never receives test data.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore, QtWidgets  # noqa: E402  (after the env var)


@pytest.fixture(scope="session")
def qapp():
    """One offscreen QApplication for the whole session."""

    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])
    yield app


@pytest.fixture(autouse=True)
def drop_close_guard(qapp):
    """Forget the process wide close guard between the tests.

    The guard is a deliberate singleton (spec §5.1.4); a test that leaves
    one installed would otherwise filter the next test's events.
    """

    yield
    guard = getattr(qapp, "_remote_training_close_guard", None)
    if guard is not None:
        qapp.removeEventFilter(guard)
        try:
            del qapp._remote_training_close_guard
        except AttributeError:  # pragma: no cover - defensive
            pass


@pytest.fixture
def qt_messages(qapp):
    """Capture the Qt warnings of one test (QThread destruction etc.).

    The monitoring tests assert that no "Destroyed while thread is
    still running" warning is produced (spec §5.1.4 step 4).
    """

    seen = []
    previous = QtCore.qInstallMessageHandler(
        lambda mode, context, message: seen.append(message)
    )
    yield lambda: list(seen)
    if previous is not None:
        QtCore.qInstallMessageHandler(previous)


@pytest.fixture
def pump(qapp):
    """Process events for `ms` milliseconds."""

    def _pump(milliseconds: int = 200) -> None:
        timer = QtCore.QElapsedTimer()
        timer.start()
        while timer.elapsed() < milliseconds:
            qapp.processEvents()
            QtCore.QThread.msleep(10)

    return _pump
