"""Where the crash logs live and how their files are managed.

Standard library only: this module is imported before any heavy
dependency of the application and must never become a new source of
crashes itself.
"""

from __future__ import annotations

import datetime
import os
import re
import tempfile

ENV_DIR = "XANY_LOG_DIR"
ENV_DISABLE = "XANY_LOG_DISABLE"

# Default location, picked by the user for the packaged build. The
# application config module is deliberately not imported here: at
# install time get_work_directory() is still "~", and that import would
# only add a dependency chain that may itself crash.
DEFAULT_DIR = "~/.xanylabeling/logs"
TEMP_DIRNAME = "xanylabeling-logs"
KEEP_LOGS = 14

# Only files matching this pattern belong to this feature; anything
# else in the folder is somebody else's business.
_OWN_NAME = re.compile(r"^xany-\d{8}\.log$")


def log_file_name(date):
    """Return the file name that one day writes to."""
    if isinstance(date, datetime.datetime):
        date = date.date()
    if isinstance(date, datetime.date):
        return "xany-%s.log" % date.strftime("%Y%m%d")
    return "xany-%s.log" % date


def is_own_log_name(name):
    """Tell our own rotated files from foreign files."""
    return bool(_OWN_NAME.match(name))


def _make_directory(path):
    """Create path, returning it on success and None on any failure."""
    try:
        os.makedirs(path, exist_ok=True)
        return path
    except OSError:
        return None


def resolve_log_directory():
    """Return the folder that receives the logs, or None.

    Order: XANY_LOG_DIR, ~/.xanylabeling/logs, a folder inside the
    system temporary directory. None is the degraded mode: hook output
    is only mirrored to stderr and faulthandler stays disabled.
    """
    requested = os.environ.get(ENV_DIR, "").strip()
    if requested:
        candidate = os.path.abspath(os.path.expanduser(requested))
    else:
        candidate = os.path.expanduser(DEFAULT_DIR)
    resolved = _make_directory(candidate)
    if resolved is not None:
        return resolved
    return _make_directory(
        os.path.join(tempfile.gettempdir(), TEMP_DIRNAME)
    )


def log_file_path(log_dir, date=None):
    """Return the full path of the log file of that day."""
    if date is None:
        date = datetime.date.today()
    return os.path.join(log_dir, log_file_name(date))


def prune_old_logs(log_dir, keep=KEEP_LOGS):
    """Remove our own old files, keeping the newest keep of them.

    Only names matching xany-YYYYMMDD.log are ever listed or removed,
    so a foreign file dropped into the folder is never touched. A
    failed removal is silent on purpose.
    """
    try:
        names = [
            name for name in os.listdir(log_dir)
            if is_own_log_name(name)
        ]
    except OSError:
        return []
    names.sort()
    if keep < 0:
        keep = 0
    removed = []
    for name in names[: max(0, len(names) - keep)]:
        try:
            os.remove(os.path.join(log_dir, name))
        except OSError:
            continue
        removed.append(name)
    return removed


def open_streams(log_dir, date=None):
    """Open the two append handles of one day.

    log_stream is what this package and the application logger write
    to. fault_stream is handed to faulthandler, which writes to its
    file descriptor directly, bypassing the Python buffers. Both open
    the same file with O_APPEND.
    """
    path = log_file_path(log_dir, date)
    log_stream = open(path, "a", encoding="utf-8", buffering=1)
    try:
        fault_stream = open(path, "a", encoding="utf-8", buffering=1)
    except OSError:
        log_stream.close()
        raise
    return log_stream, fault_stream
