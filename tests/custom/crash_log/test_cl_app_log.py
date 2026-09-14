"""The real application logger must end up in the log file."""

from __future__ import annotations

import logging

from anylabeling.custom import crash_log
from anylabeling.custom.crash_log import handlers


def _app_logger():
    from anylabeling.views.labeling.logger import logger as app_logger

    return app_logger


def test_logger_name_matches_upstream():
    assert handlers.APP_LOGGER_NAME == "X-AnyLabeling"
    assert _app_logger().logger.name == handlers.APP_LOGGER_NAME


def test_real_app_logger_info_lands_in_the_file(log_dir, read_log):
    crash_log.install_crash_log()
    app_logger = _app_logger()
    target = logging.getLogger(handlers.APP_LOGGER_NAME)
    previous = target.level
    target.setLevel(logging.INFO)
    try:
        app_logger.info("cl-app-logger-probe")
    finally:
        target.setLevel(previous)
    text = read_log(log_dir)
    assert "cl-app-logger-probe" in text
    assert "INFO" in text
    assert "\x1b" not in text


def test_upstream_stderr_handler_is_kept(log_dir):
    """The application keeps its own console handler next to ours."""

    _app_logger()
    target = logging.getLogger(handlers.APP_LOGGER_NAME)
    before = list(target.handlers)
    crash_log.install_crash_log()
    after = list(target.handlers)
    assert after[: len(before)] == before
    assert len(after) == len(before) + 1


def test_propagate_is_left_alone(log_dir):
    target = logging.getLogger(handlers.APP_LOGGER_NAME)
    before = target.propagate
    crash_log.install_crash_log()
    assert target.propagate is before
