"""QSettings backed parameters of the crop tool.

Every value lives under the "custom/crop_tool/" prefix of the store,
so the tool can never collide with an upstream key. The wrapper never
raises on a read: a missing, empty or malformed value falls back to
the default of its key and is clamped to the range of the spin box it
feeds.

The output directory is the only value with a computed default: it is
the current working directory, fetched on every read and never
written back. The input directory has no default at all and stays
empty until the user picks a folder.

The store is handed to the constructor, so a test can give the
wrapper an INI file of its own instead of the store of the user.
"""

from __future__ import annotations

from PyQt6 import QtCore

from anylabeling.custom.crop_tool import crop_core

__all__ = ["DEFAULTS", "KEY_PREFIX", "CropSettings"]

#: Prefix of every key this tool owns.
KEY_PREFIX = "custom/crop_tool/"

#: Defaults of the spin boxes, in the order they are shown.
DEFAULTS = {
    "width": 640,
    "height": 640,
    "pad_width": 0,
    "pad_height": 0,
}


class CropSettings:
    """Parameters of the crop tool, read from and written to a store."""

    def __init__(self, settings=None):
        """Wrap a store; None means the store of the application."""

        if settings is None:
            settings = QtCore.QSettings("anylabeling", "anylabeling")
        self._settings = settings

    def _read_int(
        self, key: str, default: int, low: int, high: int
    ) -> int:
        """Return one integer parameter, never raising.

        A store hands strings back as a rule, so int() is attempted
        first and a value it rejects only falls back to the default.
        The result is always clamped into low..high.
        """

        raw = self._settings.value(KEY_PREFIX + key, default)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = int(default)
        return max(low, min(high, value))

    def _write_int(self, key: str, value) -> None:
        """Store one integer parameter under its prefixed key."""

        self._settings.setValue(KEY_PREFIX + key, int(value))

    def _read_text(self, key: str) -> str:
        """Return one text parameter, empty when it is not stored."""

        raw = self._settings.value(KEY_PREFIX + key, "")
        if raw is None:
            return ""
        return str(raw)

    def width(self) -> int:
        """Return the crop width in pixels."""

        return self._read_int(
            "width", DEFAULTS["width"], 1, crop_core.SIZE_MAX
        )

    def height(self) -> int:
        """Return the crop height in pixels."""

        return self._read_int(
            "height", DEFAULTS["height"], 1, crop_core.SIZE_MAX
        )

    def pad_width(self) -> int:
        """Return the horizontal padding width in pixels."""

        return self._read_int(
            "pad_width", DEFAULTS["pad_width"], 0, crop_core.SIZE_MAX
        )

    def pad_height(self) -> int:
        """Return the vertical padding width in pixels."""

        return self._read_int(
            "pad_height", DEFAULTS["pad_height"], 0, crop_core.SIZE_MAX
        )

    def output_dir(self) -> str:
        """Return the output directory, the cwd when none is stored."""

        stored = self._read_text("output_dir")
        if stored:
            return stored
        return crop_core.default_output_dir()

    def input_dir(self) -> str:
        """Return the source directory, empty when none is stored."""

        return self._read_text("input_dir")

    def set_width(self, value) -> None:
        """Store the crop width in pixels."""

        self._write_int("width", value)

    def set_height(self, value) -> None:
        """Store the crop height in pixels."""

        self._write_int("height", value)

    def set_pad_width(self, value) -> None:
        """Store the horizontal padding width in pixels."""

        self._write_int("pad_width", value)

    def set_pad_height(self, value) -> None:
        """Store the vertical padding width in pixels."""

        self._write_int("pad_height", value)

    def set_output_dir(self, value) -> None:
        """Store the output directory."""

        self._settings.setValue(KEY_PREFIX + "output_dir", str(value))

    def set_input_dir(self, value) -> None:
        """Store the source directory."""

        self._settings.setValue(KEY_PREFIX + "input_dir", str(value))
