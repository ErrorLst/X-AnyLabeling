"""Unit tests of the QSettings wrapper of the preview tool."""

import os
import tempfile

from PyQt6 import QtCore

from anylabeling.custom.preview_tool import core

from anylabeling.custom.preview_tool.settings import (  # noqa: E402
    CATEGORY_DIRS_MAX,
    DEFAULTS,
    KEY_PREFIX,
    RANGES,
    PreviewSettings,
    escape_key,
    mode_from_name,
    mode_name,
)

from conftest import flush_settings, make_settings


def _prepare(pt_make, key, value):
    """Store a raw value and return a wrapper over the same INI file."""

    ini = os.path.join(pt_make(), "preview_tool.ini")
    raw = QtCore.QSettings(ini, QtCore.QSettings.Format.IniFormat)
    raw.setValue(KEY_PREFIX + key, value)
    raw.sync()
    return make_settings(ini)


class BoomStore:
    """Store that refuses every operation, to prove nothing raises."""

    def value(self, key, default=None):
        raise TypeError("boom")

    def setValue(self, key, value):
        raise OSError("boom")

    def remove(self, key):
        raise OSError("boom")


class TestDefaults:
    """A store without a value answers with the default of the key."""

    def test_frozen_tables(self):
        assert KEY_PREFIX == "custom/preview_tool/"
        assert CATEGORY_DIRS_MAX == 32
        assert DEFAULTS == {
            "filter_enabled": False,
            "score_threshold": 0.45,
            "size_mode": "any_above",
            "width_threshold": 10.0,
            "height_threshold": 10.0,
            "expand_px": 5,
            "speed": 5,
            "output_dir": "",
        }
        assert RANGES == {
            "score_threshold": (0.0, 1.0),
            "width_threshold": (0.0, 9999.0),
            "height_threshold": (0.0, 9999.0),
            "expand_px": (0, 100),
            "speed": (1, 30),
        }

    def test_empty_store(self, pt_store):
        settings, _ini = pt_store
        assert settings.filter_enabled() is False
        assert settings.score_threshold() == 0.45
        assert settings.size_mode() == "any_above"
        assert settings.size_mode() is core.SizeFilterMode.ANY_ABOVE
        assert settings.width_threshold() == 10.0
        assert settings.height_threshold() == 10.0
        assert settings.expand_px() == 5
        assert settings.speed() == 5
        assert settings.output_dir() == os.getcwd()
        assert settings.categories_for("/nowhere") == []

    def test_there_is_no_step_parameter(self, pt_store):
        settings, _ini = pt_store
        assert not hasattr(settings, "step")
        assert "step" not in DEFAULTS


class TestRoundTrip:
    """Every parameter survives a write and a read."""

    def test_values(self, pt_store):
        settings, _ini = pt_store
        settings.set_filter_enabled(True)
        settings.set_score_threshold(0.8)
        settings.set_size_mode(core.SizeFilterMode.BOTH_BELOW)
        settings.set_width_threshold(20.0)
        settings.set_height_threshold(30.0)
        settings.set_expand_px(12)
        settings.set_speed(15)
        settings.set_output_dir("/data/out")
        assert settings.filter_enabled() is True
        assert settings.score_threshold() == 0.8
        assert settings.size_mode() is core.SizeFilterMode.BOTH_BELOW
        assert settings.width_threshold() == 20.0
        assert settings.height_threshold() == 30.0
        assert settings.expand_px() == 12
        assert settings.speed() == 15
        assert settings.output_dir() == "/data/out"

    def test_values_survive_a_new_wrapper(self, pt_store):
        settings, ini = pt_store
        settings.set_speed(21)
        settings.set_size_mode("any_below")
        flush_settings(settings)
        again = make_settings(ini)
        assert again.speed() == 21
        assert again.size_mode() == "any_below"

    def test_keys_carry_the_prefix(self, pt_store):
        settings, ini = pt_store
        settings.set_speed(9)
        flush_settings(settings)
        raw = QtCore.QSettings(ini, QtCore.QSettings.Format.IniFormat)
        assert int(raw.value(KEY_PREFIX + "speed")) == 9

    def test_the_store_stays_in_the_scratch_directory(self, pt_store):
        settings, ini = pt_store
        settings.set_speed(9)
        flush_settings(settings)
        assert os.path.exists(ini)
        root = os.path.realpath(os.path.dirname(ini))
        temp = os.path.realpath(tempfile.gettempdir())
        assert os.path.commonpath([root, temp]) == temp


class TestClamping:
    """A malformed stored value falls back and is clamped."""

    def test_score_is_clamped(self, pt_make):
        high = _prepare(pt_make, "score_threshold", 2.5)
        low = _prepare(pt_make, "score_threshold", -1)
        assert high.score_threshold() == 1.0
        assert low.score_threshold() == 0.0

    def test_width_is_clamped(self, pt_make):
        assert (
            _prepare(pt_make, "width_threshold", 100000).width_threshold()
            == 9999.0
        )

    def test_expand_is_clamped(self, pt_make):
        assert _prepare(pt_make, "expand_px", 999).expand_px() == 100
        assert _prepare(pt_make, "expand_px", -3).expand_px() == 0

    def test_speed_is_clamped(self, pt_make):
        assert _prepare(pt_make, "speed", 0).speed() == 1
        assert _prepare(pt_make, "speed", 99).speed() == 30

    def test_non_numeric_falls_back(self, pt_make):
        assert _prepare(pt_make, "speed", "abc").speed() == 5
        assert _prepare(pt_make, "speed", "3.5").speed() == 5
        broken = _prepare(pt_make, "score_threshold", "x")
        assert broken.score_threshold() == 0.45

    def test_writes_are_clamped_too(self, pt_store):
        settings, _ini = pt_store
        settings.set_speed(99)
        settings.set_expand_px(-4)
        settings.set_score_threshold(7)
        assert settings.speed() == 30
        assert settings.expand_px() == 0
        assert settings.score_threshold() == 1.0

    def test_broken_bool_falls_back(self, pt_make):
        maybe = _prepare(pt_make, "filter_enabled", "maybe")
        yes = _prepare(pt_make, "filter_enabled", "true")
        assert maybe.filter_enabled() is False
        assert yes.filter_enabled() is True

    def test_nan_falls_back(self, pt_make):
        settings = _prepare(pt_make, "score_threshold", float("nan"))
        assert settings.score_threshold() == 0.45


class TestModes:
    """The size mode round trips by name and by enum member."""

    def test_mode_from_name(self):
        assert (
            mode_from_name("both_below") is core.SizeFilterMode.BOTH_BELOW
        )
        assert (
            mode_from_name(core.SizeFilterMode.ANY_BELOW)
            is core.SizeFilterMode.ANY_BELOW
        )
        assert mode_from_name("nope") is core.SizeFilterMode.ANY_ABOVE
        assert mode_from_name(None) is core.SizeFilterMode.ANY_ABOVE

    def test_mode_name(self):
        assert mode_name(core.SizeFilterMode.BOTH_ABOVE) == "both_above"
        assert mode_name("any_below") == "any_below"
        assert mode_name("nope") == "any_above"

    def test_static_access(self, pt_store):
        settings, _ini = pt_store
        assert (
            settings.mode_from_name("any_below")
            is core.SizeFilterMode.ANY_BELOW
        )
        assert settings.mode_name("any_below") == "any_below"

    def test_broken_stored_mode_falls_back(self, pt_make):
        settings = _prepare(pt_make, "size_mode", "sideways")
        assert settings.size_mode() is core.SizeFilterMode.ANY_ABOVE


class TestOutputDir:
    """The output directory default is the cwd, fetched every time."""

    def test_cwd_is_fetched_on_every_read(self, pt_store, monkeypatch):
        settings, _ini = pt_store
        calls = []

        def fake_getcwd():
            calls.append(True)
            return "/pt-cwd/{}".format(len(calls))

        monkeypatch.setattr(os, "getcwd", fake_getcwd)
        assert settings.output_dir() == "/pt-cwd/1"
        assert settings.output_dir() == "/pt-cwd/2"

    def test_empty_value_falls_back_to_the_cwd(self, pt_store, monkeypatch):
        settings, _ini = pt_store
        settings.set_output_dir("")
        monkeypatch.setattr(os, "getcwd", lambda: "/pt/cwd")
        assert settings.output_dir() == "/pt/cwd"


class TestCategories:
    """The category selection is stored per directory."""

    def test_round_trip(self, pt_store):
        settings, _ini = pt_store
        settings.set_categories("/data/imgs", ["cat", "dog"])
        assert settings.categories_for("/data/imgs") == ["cat", "dog"]
        assert settings.categories_for("/data/other") == []

    def test_round_trip_after_reopen(self, pt_store):
        settings, ini = pt_store
        settings.set_categories("/data/imgs", ["cat"])
        flush_settings(settings)
        again = make_settings(ini)
        assert again.categories_for("/data/imgs") == ["cat"]

    def test_relative_path_is_made_absolute(self, pt_store):
        settings, _ini = pt_store
        settings.set_categories("rel/dir", ["cat"])
        assert settings.categories_for("rel/dir") == ["cat"]
        assert settings.categories_for(os.path.abspath("rel/dir")) == ["cat"]

    def test_duplicates_are_dropped(self, pt_store):
        settings, _ini = pt_store
        settings.set_categories("/data/imgs", ["cat", "cat", ""])
        assert settings.categories_for("/data/imgs") == ["cat"]

    def test_clearing_stores_an_empty_selection(self, pt_store):
        settings, _ini = pt_store
        settings.set_categories("/data/imgs", ["cat"])
        settings.set_categories("/data/imgs", [])
        assert settings.categories_for("/data/imgs") == []

    def test_key_escaping(self):
        assert escape_key("/a/b:c") == "%2Fa%2Fb%3Ac"
        assert escape_key("C" + chr(92) + "data") == "C%5Cdata"

    def test_key_escaping_is_injective(self):
        # The percent sign is escaped too, so two different paths can
        # never collapse into one key.
        assert escape_key("/data/a/b") == "%2Fdata%2Fa%2Fb"
        assert escape_key("/data/a%2Fb") == "%2Fdata%2Fa%252Fb"
        assert escape_key("/data/a/b") != escape_key("/data/a%2Fb")
        assert escape_key("/a,b") == "%2Fa%2Cb"

    def test_paths_with_a_percent_do_not_share_a_key(self, pt_store):
        settings, _ini = pt_store
        plain = os.path.join("/data", "a", "b")
        tricky = "/data/a%2Fb"
        assert settings._category_key(plain) != settings._category_key(tricky)
        settings.set_categories(plain, ["cat"])
        settings.set_categories(tricky, ["dog"])
        assert settings.categories_for(plain) == ["cat"]
        assert settings.categories_for(tricky) == ["dog"]

    def test_a_comma_in_a_name_round_trips(self, pt_store):
        settings, ini = pt_store
        names = ["a,b", "c"]
        settings.set_categories("/data/imgs", names)
        assert settings.categories_for("/data/imgs") == names
        flush_settings(settings)
        assert make_settings(ini).categories_for("/data/imgs") == names

    def test_percent_and_comma_are_reversible(self, pt_store):
        settings, ini = pt_store
        names = ["a%2Cb", "100%", ",", "a,b"]
        settings.set_categories("/data/imgs", names)
        assert settings.categories_for("/data/imgs") == names
        flush_settings(settings)
        assert make_settings(ini).categories_for("/data/imgs") == names

    def test_a_comma_in_the_directory_path(self, pt_store):
        settings, _ini = pt_store
        first = os.path.join(tempfile.gettempdir(), "a,b")
        second = os.path.join(tempfile.gettempdir(), "c")
        settings.set_categories(first, ["cat"])
        settings.set_categories(second, ["dog"])
        order = settings._value(KEY_PREFIX + "category_dirs", [])
        assert [str(item) for item in order] == [
            settings._category_key(first),
            settings._category_key(second),
        ]

    def test_the_key_is_one_segment(self, pt_store):
        settings, _ini = pt_store
        directory = os.path.join(tempfile.gettempdir(), "a b", "c")
        settings.set_categories(directory, ["cat"])
        flush_settings(settings)
        prefix = KEY_PREFIX + "categories/"
        for key in settings._settings.allKeys():
            if not key.startswith(prefix):
                continue
            assert "/" not in key[len(prefix):]

    def test_only_32_directories_are_kept(self, pt_make):
        ini = os.path.join(pt_make(), "preview_tool.ini")
        settings = make_settings(ini)
        dirs = [pt_make() for _ in range(CATEGORY_DIRS_MAX + 8)]
        for index, directory in enumerate(dirs):
            settings.set_categories(directory, ["cat{}".format(index)])
        flush_settings(settings)
        raw = QtCore.QSettings(ini, QtCore.QSettings.Format.IniFormat)
        raw.sync()
        order = raw.value(KEY_PREFIX + "category_dirs")
        names = [str(item) for item in order]
        assert len(names) == CATEGORY_DIRS_MAX
        assert names[0] == settings._category_key(dirs[8])
        assert names[-1] == settings._category_key(dirs[-1])
        assert settings.categories_for(dirs[0]) == []
        assert settings.categories_for(dirs[-1]) == ["cat39"]
        assert raw.value(settings._category_key(dirs[0])) is None

    def test_touching_a_directory_moves_it_to_the_end(self, pt_make):
        ini = os.path.join(pt_make(), "preview_tool.ini")
        settings = make_settings(ini)
        first = pt_make()
        second = pt_make()
        settings.set_categories(first, ["cat"])
        settings.set_categories(second, ["dog"])
        settings.set_categories(first, ["cat", "dog"])
        order = settings._value(KEY_PREFIX + "category_dirs", [])
        names = [str(item) for item in order]
        assert names == [
            settings._category_key(second),
            settings._category_key(first),
        ]

    def test_empty_stored_value(self, pt_make):
        settings = _prepare(pt_make, "categories/%2Fdata", "")
        assert settings.categories_for("/data") == []


class TestNeverRaises:
    """A hostile store yields the defaults instead of an exception."""

    def test_reads(self):
        settings = PreviewSettings(BoomStore())
        assert settings.filter_enabled() is False
        assert settings.score_threshold() == 0.45
        assert settings.size_mode() is core.SizeFilterMode.ANY_ABOVE
        assert settings.expand_px() == 5
        assert settings.speed() == 5
        assert settings.output_dir() == os.getcwd()
        assert settings.categories_for("/data") == []

    def test_writes(self):
        settings = PreviewSettings(BoomStore())
        settings.set_speed(9)
        settings.set_categories("/data", ["cat"])
        settings.set_categories(None, ["cat"])
