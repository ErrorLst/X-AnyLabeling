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
        assert settings.save_width() == 0
        assert settings.save_height() == 0
        assert DEFAULTS == {
            "width": 640,
            "height": 640,
            "pad_width": 0,
            "pad_height": 0,
            "save_width": 0,
            "save_height": 0,
        }

class TestRoundTrip:
    """Every parameter survives a write and a read."""

    def test_integers(self, ct_store):
        settings, _ini = ct_store
        settings.set_width(320)
        settings.set_height(321)
        settings.set_pad_width(7)
        settings.set_pad_height(8)
        settings.set_save_width(160)
        settings.set_save_height(240)
        assert settings.width() == 320
        assert settings.height() == 321
        assert settings.pad_width() == 7
        assert settings.pad_height() == 8
        assert settings.save_width() == 160
        assert settings.save_height() == 240

    def test_directories_stay_in_memory(self, ct_store, monkeypatch):
        settings, ini = ct_store
        monkeypatch.setattr(core, "program_dir", lambda: "/ct/program")
        settings.set_output_dir("/data/out")
        assert settings.output_dir() == "/data/out"
        flush_settings(settings)
        raw = QtCore.QSettings(ini, QtCore.QSettings.Format.IniFormat)
        assert raw.value(KEY_PREFIX + "output_dir") is None
        assert make_settings(ini).output_dir() == core.default_output_dir()

    def test_keys_carry_the_prefix(self, ct_store):
        settings, ini = ct_store
        settings.set_width(320)
        flush_settings(settings)
        raw = QtCore.QSettings(ini, QtCore.QSettings.Format.IniFormat)
        assert int(raw.value(KEY_PREFIX + "width")) == 320

    def test_the_saved_size_keys_carry_the_prefix(self, ct_store):
        settings, ini = ct_store
        settings.set_save_width(320)
        settings.set_save_height(240)
        flush_settings(settings)
        raw = QtCore.QSettings(ini, QtCore.QSettings.Format.IniFormat)
        assert int(raw.value(KEY_PREFIX + "save_width")) == 320
        assert int(raw.value(KEY_PREFIX + "save_height")) == 240

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
    """The default output directory is program/crop, fetched every time."""

    def test_program_dir_is_fetched_on_every_read(
        self, ct_store, monkeypatch
    ):
        settings, _ini = ct_store
        calls = []

        def fake_program_dir():
            calls.append(True)
            return os.path.join("/ct-program", str(len(calls)))

        monkeypatch.setattr(core, "program_dir", fake_program_dir)
        assert settings.output_dir() == os.path.join(
            "/ct-program", "1", core.OUTPUT_SUBDIR
        )
        assert settings.output_dir() == os.path.join(
            "/ct-program", "2", core.OUTPUT_SUBDIR
        )

    def test_empty_value_falls_back_to_the_default(
        self, ct_store, monkeypatch
    ):
        settings, _ini = ct_store
        settings.set_output_dir("")
        monkeypatch.setattr(core, "program_dir", lambda: "/ct/program")
        assert settings.output_dir() == os.path.join(
            "/ct/program", core.OUTPUT_SUBDIR
        )

    def test_a_stored_value_is_ignored(self, ct_make, monkeypatch):
        settings = _prepare(ct_make, "output_dir", "/old/out")
        monkeypatch.setattr(core, "program_dir", lambda: "/ct/program")
        assert settings.output_dir() == os.path.join(
            "/ct/program", core.OUTPUT_SUBDIR
        )


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

    def test_a_negative_saved_width_is_clamped_to_zero(self, ct_make):
        settings = _prepare(ct_make, "save_width", -5)
        assert settings.save_width() == 0

    def test_a_high_saved_height_is_clamped(self, ct_make):
        settings = _prepare(ct_make, "save_height", 100000)
        assert settings.save_height() == core.SIZE_MAX

    def test_a_text_saved_width_falls_back(self, ct_make):
        settings = _prepare(ct_make, "save_width", "abc")
        assert settings.save_width() == DEFAULTS["save_width"]
