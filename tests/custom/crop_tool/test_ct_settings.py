"""Unit tests of the QSettings wrapper of the crop tool."""

import os
import tempfile

from PyQt6 import QtCore

from anylabeling.custom.crop_tool import crop_core as core

from anylabeling.custom.crop_tool.settings import (  # noqa: E402
    DEFAULTS,
    KEY_PREFIX,
)

from conftest import flush_settings, make_settings


def _prepare(ct_make, key, value):
    """Store a raw value and return a wrapper over the same INI file."""

    ini = os.path.join(ct_make(), "crop_tool.ini")
    raw = QtCore.QSettings(ini, QtCore.QSettings.Format.IniFormat)
    raw.setValue(KEY_PREFIX + key, value)
    raw.sync()
    return make_settings(ini)


class TestDefaults:
    """A store without a value answers with the default of the key."""

    def test_spin_box_defaults(self, ct_store):
        settings, _ini = ct_store
        assert settings.width() == 640
        assert settings.height() == 640
        assert settings.pad_width() == 0
        assert settings.pad_height() == 0
        assert DEFAULTS == {
            "width": 640,
            "height": 640,
            "pad_width": 0,
            "pad_height": 0,
        }

class TestRoundTrip:
    """Every parameter survives a write and a read."""

    def test_integers(self, ct_store):
        settings, _ini = ct_store
        settings.set_width(320)
        settings.set_height(321)
        settings.set_pad_width(7)
        settings.set_pad_height(8)
        assert settings.width() == 320
        assert settings.height() == 321
        assert settings.pad_width() == 7
        assert settings.pad_height() == 8

    def test_directories(self, ct_store):
        settings, _ini = ct_store
        settings.set_output_dir("/data/out")
        assert settings.output_dir() == "/data/out"

    def test_keys_carry_the_prefix(self, ct_store):
        settings, ini = ct_store
        settings.set_width(320)
        flush_settings(settings)
        raw = QtCore.QSettings(ini, QtCore.QSettings.Format.IniFormat)
        assert int(raw.value(KEY_PREFIX + "width")) == 320

    def test_the_store_stays_in_the_scratch_directory(self, ct_store):
        settings, ini = ct_store
        settings.set_width(320)
        settings.set_output_dir("/data/out")
        flush_settings(settings)
        assert os.path.exists(ini)
        root = os.path.realpath(os.path.dirname(ini))
        temp = os.path.realpath(tempfile.gettempdir())
        assert os.path.commonpath([root, temp]) == temp


class TestOutputDefault:
    """The default output directory is cwd/crop, fetched every time."""

    def test_cwd_is_fetched_on_every_read(self, ct_store, monkeypatch):
        settings, _ini = ct_store
        calls = []

        def fake_getcwd():
            calls.append(True)
            return "/ct-cwd/{}".format(len(calls))

        monkeypatch.setattr(os, "getcwd", fake_getcwd)
        assert settings.output_dir() == "/ct-cwd/1/crop"
        assert settings.output_dir() == "/ct-cwd/2/crop"

    def test_empty_value_falls_back_to_the_cwd(self, ct_store, monkeypatch):
        settings, _ini = ct_store
        settings.set_output_dir("")
        monkeypatch.setattr(os, "getcwd", lambda: "/ct/cwd")
        assert settings.output_dir() == "/ct/cwd/crop"


class TestClamping:
    """A malformed stored value falls back and is clamped."""

    def test_text_width_falls_back(self, ct_make):
        settings = _prepare(ct_make, "width", "abc")
        assert settings.width() == DEFAULTS["width"]

    def test_float_text_falls_back(self, ct_make):
        settings = _prepare(ct_make, "pad_height", "3.5")
        assert settings.pad_height() == DEFAULTS["pad_height"]

    def test_low_value_is_clamped(self, ct_make):
        settings = _prepare(ct_make, "width", -5)
        assert settings.width() == 1

    def test_high_value_is_clamped(self, ct_make):
        settings = _prepare(ct_make, "height", 100000)
        assert settings.height() == core.SIZE_MAX

    def test_high_padding_is_clamped(self, ct_make):
        settings = _prepare(ct_make, "pad_width", 100000)
        assert settings.pad_width() == core.SIZE_MAX
