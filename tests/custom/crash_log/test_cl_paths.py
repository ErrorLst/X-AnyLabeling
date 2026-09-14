"""paths: folder resolution, file naming, rotation and pruning."""

from __future__ import annotations

import datetime
import logging
import os
import os.path as osp

from anylabeling.custom import crash_log
from anylabeling.custom.crash_log import handlers, paths


def write_file(path, text):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def test_default_dir_is_home_dot_xanylabeling():
    assert paths.DEFAULT_DIR == "~/.xanylabeling/logs"


def test_env_dir_wins(tmp_path, monkeypatch):
    target = tmp_path / "custom-logs"
    monkeypatch.setenv(paths.ENV_DIR, str(target))
    assert paths.resolve_log_directory() == str(target)
    assert osp.isdir(str(target))


def test_home_default_used_when_env_is_absent(tmp_path, monkeypatch):
    monkeypatch.delenv(paths.ENV_DIR, raising=False)
    home = tmp_path / "home"
    monkeypatch.setattr(
        paths, "DEFAULT_DIR", str(home / ".xanylabeling" / "logs")
    )
    resolved = paths.resolve_log_directory()
    assert resolved == str(home / ".xanylabeling" / "logs")
    assert osp.isdir(resolved)


def test_unwritable_home_falls_back_to_temp(tmp_path, monkeypatch):
    monkeypatch.delenv(paths.ENV_DIR, raising=False)
    blocker = tmp_path / "blocker"
    write_file(str(blocker), "not a directory")
    monkeypatch.setattr(
        paths, "DEFAULT_DIR", str(blocker / ".xanylabeling" / "logs")
    )
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    monkeypatch.setattr(
        paths.tempfile, "gettempdir", lambda: str(temp_root)
    )
    resolved = paths.resolve_log_directory()
    assert resolved == str(temp_root / paths.TEMP_DIRNAME)
    assert osp.isdir(resolved)


def test_unwritable_env_dir_falls_back_to_temp(tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    write_file(str(blocker), "not a directory")
    monkeypatch.setenv(paths.ENV_DIR, str(blocker / "logs"))
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    monkeypatch.setattr(
        paths.tempfile, "gettempdir", lambda: str(temp_root)
    )
    assert paths.resolve_log_directory() == (
        str(temp_root / paths.TEMP_DIRNAME)
    )


def test_no_writable_place_at_all_returns_none(tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    write_file(str(blocker), "not a directory")
    monkeypatch.setenv(paths.ENV_DIR, str(blocker / "logs"))
    monkeypatch.setattr(
        paths.tempfile, "gettempdir", lambda: str(blocker / "temp")
    )
    assert paths.resolve_log_directory() is None


def test_file_name_and_own_pattern():
    date = datetime.date(2026, 9, 14)
    assert paths.log_file_name(date) == "xany-20260914.log"
    assert paths.log_file_name("20260914") == "xany-20260914.log"
    assert paths.log_file_path("/logs", date) == (
        "/logs/xany-20260914.log"
    )
    assert paths.is_own_log_name("xany-20260914.log")
    assert not paths.is_own_log_name("xany-2026091.log")
    assert not paths.is_own_log_name("xany-20260914.log.1")
    assert not paths.is_own_log_name("other.log")
    assert not paths.is_own_log_name("xany-notes.txt")


def test_open_streams_appends_across_reopen(tmp_path):
    folder = str(tmp_path)
    first, first_fault = paths.open_streams(folder)
    try:
        first.write("one\n")
        first.flush()
        assert osp.getsize(paths.log_file_path(folder)) > 0
        assert first_fault.fileno() != first.fileno()
        assert osp.samefile(
            first.name, paths.log_file_path(folder)
        )
    finally:
        first.close()
        first_fault.close()
    second, second_fault = paths.open_streams(folder)
    try:
        second.write("two\n")
        second.flush()
    finally:
        second.close()
        second_fault.close()
    with open(paths.log_file_path(folder), encoding="utf-8") as handle:
        assert handle.read() == "one\ntwo\n"


def test_cross_day_rotation(log_dir, read_log, monkeypatch):
    today = datetime.date(2026, 9, 14)
    monkeypatch.setattr(handlers, "_today", lambda: today)
    crash_log.install_crash_log()
    first = paths.log_file_path(log_dir, today)
    assert "SESSION START" in read_log(log_dir, today)
    handlers._write("TEST", "first day")
    tomorrow = today + datetime.timedelta(days=1)
    monkeypatch.setattr(handlers, "_today", lambda: tomorrow)
    handlers._write("TEST", "second day")
    second = paths.log_file_path(log_dir, tomorrow)
    assert "first day" in read_log(log_dir, today)
    assert "second day" not in read_log(log_dir, today)
    assert "second day" in read_log(log_dir, tomorrow)
    assert "SESSION START" not in read_log(log_dir, tomorrow)
    assert osp.getsize(first) > 0 and osp.getsize(second) > 0


def test_cross_day_rotation_rebinds_the_app_logger(
    log_dir, read_log, monkeypatch, capsys
):
    """Records of the application logger must follow the new file.

    The handler is bound to one stream object; if the rotation does not
    rebind it, every later record is written into the closed stream and
    silently dropped by logging's error handling.
    """

    today = datetime.date(2026, 9, 14)
    tomorrow = today + datetime.timedelta(days=1)
    monkeypatch.setattr(handlers, "_today", lambda: today)
    crash_log.install_crash_log()
    logger = logging.getLogger(handlers.APP_LOGGER_NAME)
    level = logger.level
    logger.setLevel(logging.INFO)
    try:
        logger.info("app line before rotation")
        monkeypatch.setattr(handlers, "_today", lambda: tomorrow)
        handlers._write("TEST", "trigger rotation")
        assert handlers._app_handler.stream is handlers._log_stream
        logger.info("app line after rotation")
    finally:
        logger.setLevel(level)

    assert "app line before rotation" in read_log(log_dir, today)
    assert "app line after rotation" in read_log(log_dir, tomorrow)
    assert "app line after rotation" not in read_log(log_dir, today)
    assert "Logging error" not in capsys.readouterr().err


def test_prune_keeps_newest_14_and_never_touches_foreign(
    log_dir, monkeypatch
):
    for day in range(1, 21):
        write_file(
            osp.join(log_dir, "xany-202601%02d.log" % day), "day"
        )
    write_file(osp.join(log_dir, "other.log"), "foreign")
    write_file(osp.join(log_dir, "xany-notes.txt"), "foreign")
    removed = paths.prune_old_logs(log_dir)
    assert sorted(removed) == [
        "xany-202601%02d.log" % day for day in range(1, 7)
    ]
    names = sorted(os.listdir(log_dir))
    own = [name for name in names if paths.is_own_log_name(name)]
    assert own == [
        "xany-202601%02d.log" % day for day in range(7, 21)
    ]
    assert "other.log" in names
    assert "xany-notes.txt" in names
    assert os.path.getsize(osp.join(log_dir, "other.log")) == 7
