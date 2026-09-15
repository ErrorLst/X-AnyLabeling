"""QSettings backed parameters of the preview tool.

Every value lives under the "custom/preview_tool/" prefix of the store,
so the tool can never collide with an upstream key. The wrapper never
raises on a read: a missing, empty or malformed value falls back to the
default of its key and is clamped to the range of the widget it feeds.

The output directory is the only value with a computed default: it is
the current working directory, fetched on every read and never written
back. The categories a directory had selected last time live under one
key per directory; the key is the escaped absolute path, so an
arbitrary path still fits into a single INI segment. category_dirs
keeps the order those keys were touched in and trims the oldest one
past CATEGORY_DIRS_MAX.

The store is handed to the constructor, so a test can give the wrapper
an INI file of its own instead of the store of the user.
"""

from __future__ import annotations

import os
from typing import List, Optional

from PyQt6 import QtCore

from anylabeling.custom.preview_tool import core

__all__ = [
    "CATEGORY_DIRS_MAX",
    "DEFAULTS",
    "KEY_PREFIX",
    "RANGES",
    "PreviewSettings",
    "escape_key",
    "mode_from_name",
    "mode_name",
]

#: Prefix of every key this tool owns.
KEY_PREFIX = "custom/preview_tool/"

#: Defaults of the eight persisted parameters.
DEFAULTS = {
    "filter_enabled": False,
    "score_threshold": 0.45,
    "size_mode": "any_above",
    "width_threshold": 10.0,
    "height_threshold": 10.0,
    "expand_px": 5,
    "speed": 5,
    "output_dir": "",
}

#: Inclusive bounds every numeric parameter is clamped into.
RANGES = {
    "score_threshold": (0.0, 1.0),
    "width_threshold": (0.0, 9999.0),
    "height_threshold": (0.0, 9999.0),
    "expand_px": (0, 100),
    "speed": (1, 30),
}

#: How many directories keep their category selection.
CATEGORY_DIRS_MAX = 32

_WINDOWS_SEP = chr(92)
_ESCAPED = frozenset("/," + _WINDOWS_SEP + ':*?"<>|%')
_TRUE_WORDS = ("true", "1", "yes", "on")


def escape_key(value) -> str:
    """Return the key segment of an arbitrary absolute path.

    The separators and the characters a file name may not carry become
    a percent escape, so the whole path stays one INI key and can never
    open a group of its own. The percent sign itself is escaped too, so
    two different paths can never map to the same key, and a comma is
    escaped because the list of known directories is stored comma
    separated.
    """

    parts = []
    for char in str(value):
        if char in _ESCAPED:
            parts.append("%{:02X}".format(ord(char)))
        else:
            parts.append(char)
    return "".join(parts)


def mode_from_name(name) -> core.SizeFilterMode:
    """Return the size mode of a stored name, ANY_ABOVE when unknown."""

    if isinstance(name, core.SizeFilterMode):
        return name
    try:
        return core.SizeFilterMode(str(name))
    except ValueError:
        return core.SizeFilterMode.ANY_ABOVE


def mode_name(mode) -> str:
    """Return the stored name of a size mode, ANY_ABOVE when unknown."""

    if isinstance(mode, core.SizeFilterMode):
        return mode.value
    try:
        return core.SizeFilterMode(str(mode)).value
    except ValueError:
        return core.SizeFilterMode.ANY_ABOVE.value


def _split_items(raw) -> List[str]:
    """Return the text items of a stored list value of any shape.

    Qt joins a stored list with a comma, so a string value is split on
    it. An item never carries a bare comma of its own: a label is
    escaped before it is stored and a key escapes one as well.
    """

    if raw is None:
        return []
    if isinstance(raw, str):
        parts = raw.split(",") if "," in raw else [raw]
    elif isinstance(raw, (list, tuple, set, frozenset)):
        parts = [str(part) for part in raw]
    else:
        parts = [str(raw)]
    items: List[str] = []
    for part in parts:
        text = str(part).strip()
        if text and text not in items:
            items.append(text)
    return items


def _escape_name(name) -> str:
    """Escape the two characters a stored label list could confuse."""

    parts = []
    for char in str(name):
        if char == "%":
            parts.append("%25")
        elif char == ",":
            parts.append("%2C")
        else:
            parts.append(char)
    return "".join(parts)


def _unescape_name(text) -> str:
    """Reverse _escape_name, leaving every other percent alone."""

    value = str(text)
    parts = []
    index = 0
    length = len(value)
    while index < length:
        if value[index] == "%" and index + 2 < length:
            code = value[index + 1:index + 3]
            if code == "25":
                parts.append("%")
                index += 3
                continue
            if code == "2C":
                parts.append(",")
                index += 3
                continue
        parts.append(value[index])
        index += 1
    return "".join(parts)


def _as_labels(raw) -> List[str]:
    """Return the label list of a stored value, unescaped."""

    labels: List[str] = []
    for text in _split_items(raw):
        value = _unescape_name(text)
        if value and value not in labels:
            labels.append(value)
    return labels


class PreviewSettings:
    """Parameters of the preview tool, read from and written to a store."""

    def __init__(self, settings=None):
        """Wrap a store; None means the store of the application."""

        if settings is None:
            settings = QtCore.QSettings("anylabeling", "anylabeling")
        self._settings = settings

    def _value(self, key: str, default):
        """Return one full key, the default when the store refuses."""

        try:
            return self._settings.value(key, default)
        except (TypeError, ValueError, OSError):
            return default

    def _read(self, key: str):
        """Return one parameter under its prefixed key."""

        return self._value(KEY_PREFIX + key, DEFAULTS[key])

    def _read_float(self, key: str) -> float:
        """Return one bounded float parameter, never raising."""

        low, high = RANGES[key]
        default = float(DEFAULTS[key])
        try:
            value = float(self._read(key))
        except (TypeError, ValueError):
            value = default
        if value != value:
            value = default
        return max(low, min(high, value))

    def _read_int(self, key: str) -> int:
        """Return one bounded integer parameter, never raising."""

        low, high = RANGES[key]
        default = int(DEFAULTS[key])
        try:
            value = int(self._read(key))
        except (TypeError, ValueError):
            value = default
        return max(low, min(high, value))

    def _read_bool(self, key: str) -> bool:
        """Return one boolean parameter, never raising."""

        raw = self._read(key)
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            return raw.strip().lower() in _TRUE_WORDS
        try:
            return bool(raw)
        except (TypeError, ValueError):
            return bool(DEFAULTS[key])

    def _read_text(self, key: str) -> str:
        """Return one text parameter, empty when it is not stored."""

        raw = self._value(KEY_PREFIX + key, DEFAULTS[key])
        if raw is None:
            return ""
        return str(raw)

    def _write(self, key: str, value) -> None:
        """Store one parameter under its prefixed key."""

        try:
            self._settings.setValue(KEY_PREFIX + key, value)
        except (TypeError, ValueError, OSError):
            return

    def _clamp_float(self, key: str, value) -> float:
        """Return one float clamped into the range of its key."""

        low, high = RANGES[key]
        default = float(DEFAULTS[key])
        try:
            result = float(value)
        except (TypeError, ValueError):
            result = default
        if result != result:
            result = default
        return max(low, min(high, result))

    def _clamp_int(self, key: str, value) -> int:
        """Return one integer clamped into the range of its key."""

        low, high = RANGES[key]
        default = int(DEFAULTS[key])
        try:
            result = int(value)
        except (TypeError, ValueError):
            result = default
        return max(low, min(high, result))

    def filter_enabled(self) -> bool:
        """Return True when the master filter switch is on."""

        return self._read_bool("filter_enabled")

    def score_threshold(self) -> float:
        """Return the score threshold of the score axis."""

        return self._read_float("score_threshold")

    def size_mode(self) -> core.SizeFilterMode:
        """Return the size mode, a value that equals its stored name."""

        return mode_from_name(self._read_text("size_mode"))

    def width_threshold(self) -> float:
        """Return the width threshold of the size axis."""

        return self._read_float("width_threshold")

    def height_threshold(self) -> float:
        """Return the height threshold of the size axis."""

        return self._read_float("height_threshold")

    def expand_px(self) -> int:
        """Return how far a rectangle is expanded while drawing."""

        return self._read_int("expand_px")

    def speed(self) -> int:
        """Return how many images a second a held key advances."""

        return self._read_int("speed")

    def output_dir(self) -> str:
        """Return the output directory, the cwd when none is stored."""

        stored = self._read_text("output_dir")
        if stored:
            return stored
        return core.default_output_dir()

    def set_filter_enabled(self, value) -> None:
        """Store the master filter switch."""

        self._write("filter_enabled", bool(value))

    def set_score_threshold(self, value) -> None:
        """Store the score threshold, clamped into its range."""

        self._write(
            "score_threshold", self._clamp_float("score_threshold", value)
        )

    def set_size_mode(self, value) -> None:
        """Store the size mode, by name or by enum member."""

        self._write("size_mode", mode_name(value))

    def set_width_threshold(self, value) -> None:
        """Store the width threshold, clamped into its range."""

        self._write(
            "width_threshold", self._clamp_float("width_threshold", value)
        )

    def set_height_threshold(self, value) -> None:
        """Store the height threshold, clamped into its range."""

        self._write(
            "height_threshold", self._clamp_float("height_threshold", value)
        )

    def set_expand_px(self, value) -> None:
        """Store the drawing expansion, clamped into its range."""

        self._write("expand_px", self._clamp_int("expand_px", value))

    def set_speed(self, value) -> None:
        """Store the repeat speed, clamped into its range."""

        self._write("speed", self._clamp_int("speed", value))

    def set_output_dir(self, value) -> None:
        """Store the output directory."""

        self._write("output_dir", str(value))

    def _category_key(self, directory) -> Optional[str]:
        """Return the store key of the categories of one directory."""

        try:
            path = os.path.abspath(str(directory))
        except (TypeError, ValueError, OSError):
            return None
        return KEY_PREFIX + "categories/" + escape_key(path)

    def categories_for(self, directory) -> List[str]:
        """Return the categories stored for one directory.

        An empty list means that nothing was stored for the directory
        (or that the user unchecked everything); the window treats it
        as the default selection, which is every category.
        """

        key = self._category_key(directory)
        if key is None:
            return []
        return _as_labels(self._value(key, []))

    def set_categories(self, directory, names) -> None:
        """Store the categories of one directory and touch its order."""

        key = self._category_key(directory)
        if key is None:
            return
        cleaned = []
        for name in _split_items(list(names or ())):
            value = _escape_name(name)
            if value not in cleaned:
                cleaned.append(value)
        try:
            self._settings.setValue(key, cleaned)
        except (TypeError, ValueError, OSError):
            return
        self._remember_category_dir(key)

    def _remember_category_dir(self, key: str) -> None:
        """Keep the newest CATEGORY_DIRS_MAX directories, oldest first."""

        order = _split_items(
            self._value(KEY_PREFIX + "category_dirs", [])
        )
        if key in order:
            order.remove(key)
        order.append(key)
        while len(order) > CATEGORY_DIRS_MAX:
            self._forget_category(order.pop(0))
        self._write_full(KEY_PREFIX + "category_dirs", order)

    def _write_full(self, key: str, value) -> None:
        """Store a value under an already complete key."""

        try:
            self._settings.setValue(key, value)
        except (TypeError, ValueError, OSError):
            return

    def _forget_category(self, key: str) -> None:
        """Drop the category key of a directory that fell out of order."""

        try:
            self._settings.remove(key)
        except (TypeError, ValueError, OSError):
            return

    mode_from_name = staticmethod(mode_from_name)
    mode_name = staticmethod(mode_name)
