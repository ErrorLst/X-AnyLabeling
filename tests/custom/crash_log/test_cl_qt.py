"""Qt message handler behaviour, offscreen."""

from __future__ import annotations

import pytest
from PyQt6 import QtCore

from anylabeling.custom import crash_log
from anylabeling.custom.crash_log import handlers


@pytest.fixture(autouse=True)
def no_logging_rules(monkeypatch):
    """Keep the Qt logging rules of the host out of the way."""

    monkeypatch.delenv("QT_LOGGING_RULES", raising=False)


def test_qwarning_reaches_file_and_stderr(log_dir, read_log, capsys):
    crash_log.install_crash_log()
    assert handlers._qt_installed is True
    QtCore.qWarning("cl-qt-warning-probe")
    text = read_log(log_dir)
    assert "cl-qt-warning-probe" in text
    assert "QTWARNINGMSG" in text
    assert "cl-qt-warning-probe" in capsys.readouterr().err


def test_qcritical_reaches_file(log_dir, read_log):
    crash_log.install_crash_log()
    QtCore.qCritical("cl-qt-critical-probe")
    text = read_log(log_dir)
    assert "cl-qt-critical-probe" in text
    assert "QTCRITICALMSG" in text


def test_qdebug_is_only_mirrored(log_dir, read_log, capsys):
    crash_log.install_crash_log()
    QtCore.qDebug("cl-qt-debug-probe")
    assert "cl-qt-debug-probe" not in read_log(log_dir)
    assert "cl-qt-debug-probe" in capsys.readouterr().err


def test_previous_qt_handler_is_chained(log_dir):
    seen = []

    def previous(msg_type, context, message):
        seen.append(message)

    restore = QtCore.qInstallMessageHandler(previous)
    try:
        crash_log.install_crash_log()
        assert handlers._qt_previous is previous
        QtCore.qWarning("cl-qt-chained-probe")
        assert seen == ["cl-qt-chained-probe"]
    finally:
        crash_log.uninstall_crash_log()
        QtCore.qInstallMessageHandler(restore)


def test_qt_handler_is_removed_on_uninstall(log_dir):
    crash_log.install_crash_log()
    assert handlers._qt_installed is True
    crash_log.uninstall_crash_log()
    assert handlers._qt_installed is False
    assert handlers._qt_previous is None
