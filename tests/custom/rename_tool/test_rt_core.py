"""Unit tests of the pure helpers of rename_core."""

import json
import os

import pytest

from anylabeling.custom.rename_tool import rename_core as core

from conftest import make_pair, write_json


def _doc(shapes, **extra):
    """Return a minimal labelme document."""

    data = {"version": "1.0", "flags": {}, "shapes": shapes}
    data.update(extra)
    return data


def _write(root, name, payload):
    """Write raw bytes or a document and return the path."""

    path = os.path.join(root, name)
    with open(path, "w", encoding="utf-8") as handle:
        if isinstance(payload, str):
            handle.write(payload)
        else:
            json.dump(payload, handle, ensure_ascii=False)
    return path


class TestNaturalKey:
    """The sort key keeps numeric runs in numeric order.

    A name carrying a superscript digit (a²) must not raise: U+00B2
    is a digit for str.isdigit() but not for int(), and the scan runs
    inside a Qt slot where an uncaught exception aborts the whole
    application.
    """

    def test_numeric_order(self):
        names = ["10.jpg", "2.jpg", "1.jpg", "20.jpg"]
        assert sorted(names, key=core.natural_key) == [
            "1.jpg", "2.jpg", "10.jpg", "20.jpg"
        ]

    def test_mixed_text(self):
        names = ["a10", "a2", "b1", "A1"]
        assert sorted(names, key=core.natural_key) == [
            "A1", "a2", "a10", "b1"
        ]

    def test_case_folds(self):
        assert core.natural_key("ABC") == core.natural_key("abc")

    def test_used_for_pairs(self):
        order = sorted(["a_aug10", "a_aug9"], key=core.natural_key)
        assert order == ["a_aug9", "a_aug10"]

    def test_non_decimal_digit_is_a_character(self):
        assert core.natural_key("a².jpg") == [
            (1, "a"), (1, "²"), (1, "."), (1, "j"), (1, "p"), (1, "g")
        ]

    def test_chunks_are_kind_tagged(self):
        assert core.natural_key("a10") == [(1, "a"), (0, 10)]

    def test_mixed_names_sort(self):
        names = ["b.jpg", "10.jpg", "a2.png", "1.jpg", "正面.png"]
        assert sorted(names, key=core.natural_key) == [
            "1.jpg", "10.jpg", "a2.png", "b.jpg", "正面.png"
        ]

    def test_number_and_text_prefix_do_not_raise(self):
        assert sorted(["a.jpg", "a2.png"], key=core.natural_key) == [
            "a2.png", "a.jpg"
        ]

    def test_extension_and_number_do_not_raise(self):
        assert sorted(
            ["img.jpg", "img2.jpg", "img10.jpg"], key=core.natural_key
        ) == ["img2.jpg", "img10.jpg", "img.jpg"]

    def test_mixed_folder_plans(self, rt_dataset):
        for stem in ("a", "a2", "b", "10", "1"):
            make_pair(rt_dataset, stem, "person")
        plan = core.plan_directory(rt_dataset)
        assert plan.blocked() is False
        assert len(plan.items) == 5

    def test_superscript_names_sort_stably(self):
        names = ["a².jpg", "b.jpg", "a.jpg"]
        assert sorted(names, key=core.natural_key) == [
            "a.jpg", "a².jpg", "b.jpg"
        ]


class TestSanitizeLabel:
    """Illegal file name characters collapse into one underscore."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("person", "person"),
            ("a/b", "a_b"),
            ("a//b", "a_b"),
            ("a\\\\b", "a_b"),
            ("a:b", "a_b"),
            ("a*b?c", "a_b_c"),
            ("a<<b>>c", "a_b_c"),
            ("a|b", "a_b"),
            ("a__b", "a_b"),
            ("a  b", "a  b"),
            ("_a_", "a"),
            (".a.", "a"),
            (" ", "unnamed"),
            ("", "unnamed"),
            ("...", "unnamed"),
            ("/", "unnamed"),
        ],
    )
    def test_cases(self, raw, expected):
        assert core.sanitize_label(raw) == expected

    def test_non_string(self):
        assert core.sanitize_label(None) == "None"


class TestDominantLabel:
    """The most frequent label wins, sanitized before counting."""

    def test_mode(self, rt_dataset):
        path = _write(rt_dataset, "a.json", _doc([
            {"label": "cat"}, {"label": "dog"}, {"label": "cat"},
        ]))
        assert core.dominant_label(path) == ("cat", "")

    def test_tie_takes_first(self, rt_dataset):
        path = _write(rt_dataset, "a.json", _doc([
            {"label": "dog"}, {"label": "cat"},
        ]))
        assert core.dominant_label(path) == ("dog", "")

    def test_labels_trimmed(self, rt_dataset):
        path = _write(rt_dataset, "a.json", _doc([
            {"label": "  cat  "}, {"label": "cat be"},
        ]))
        assert core.dominant_label(path) == ("cat", "")

    def test_counts_after_sanitizing(self, rt_dataset):
        path = _write(rt_dataset, "a.json", _doc([
            {"label": "a/b"}, {"label": "a_b"}, {"label": "c"},
        ]))
        assert core.dominant_label(path) == ("a_b", "")

    def test_empty_shapes(self, rt_dataset):
        path = _write(rt_dataset, "a.json", _doc([]))
        assert core.dominant_label(path) == ("background", "")

    def test_missing_shapes_key(self, rt_dataset):
        path = _write(rt_dataset, "a.json", {"version": "1.0"})
        assert core.dominant_label(path) == ("background", "")

    def test_shapes_not_a_list(self, rt_dataset):
        path = _write(rt_dataset, "a.json", {"shapes": "nope"})
        assert core.dominant_label(path) == ("background", "")

    def test_blank_labels(self, rt_dataset):
        path = _write(rt_dataset, "a.json", _doc([
            {"label": ""}, {"label": "   "}, {}
        ]))
        assert core.dominant_label(path) == ("background", "")

    def test_broken_json(self, rt_dataset):
        path = _write(rt_dataset, "a.json", "{not json")
        label, error = core.dominant_label(path)
        assert label == "" and "解析失败" in error

    def test_top_level_list(self, rt_dataset):
        path = _write(rt_dataset, "a.json", [{"label": "cat"}])
        label, error = core.dominant_label(path)
        assert label == "" and "顶层不是对象" in error

    def test_missing_file(self, rt_dataset):
        label, error = core.dominant_label(
            os.path.join(rt_dataset, "nope.json")
        )
        assert label == "" and error

    def test_non_dict_shape_skipped(self, rt_dataset):
        path = _write(rt_dataset, "a.json", _doc([
            "cat", {"label": "dog"}
        ]))
        assert core.dominant_label(path) == ("dog", "")


class TestResolveOutputPath:
    """The archive name never overwrites an existing file."""

    def test_free_name(self, rt_out):
        path = core.resolve_output_path(rt_out, "data_renamed.zip")
        assert path == os.path.join(rt_out, "data_renamed.zip")

    def test_second(self, rt_out):
        first = core.resolve_output_path(rt_out, "data_renamed.zip")
        open(first, "wb").close()
        assert core.resolve_output_path(
            rt_out, "data_renamed.zip"
        ) == os.path.join(rt_out, "data_renamed_2.zip")

    def test_third(self, rt_out):
        for name in ("data_renamed.zip", "data_renamed_2.zip"):
            open(os.path.join(rt_out, name), "wb").close()
        assert core.resolve_output_path(
            rt_out, "data_renamed.zip"
        ) == os.path.join(rt_out, "data_renamed_3.zip")

    def test_extension_less(self, rt_out):
        open(os.path.join(rt_out, "plain"), "wb").close()
        assert core.resolve_output_path(rt_out, "plain") == os.path.join(
            rt_out, "plain_2.zip"
        )

    def test_empty_name(self, rt_out):
        assert core.resolve_output_path(
            rt_out, "  "
        ).endswith("dataset_renamed.zip")

    def test_chinese_name(self, rt_out):
        path = core.resolve_output_path(rt_out, "数据集_renamed.zip")
        assert os.path.basename(path) == "数据集_renamed.zip"


class TestEntryNameOk:
    """Entry names are checked before any archive is opened."""

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("a.jpg", True),
            ("数据集.json", True),
            ("", False),
            ("a/b.jpg", False),
            ("a\\\\b.jpg", False),
            ("..", False),
            ("../a.jpg", False),
            ("a..b.jpg", False),
            ("/abs/a.jpg", False),
            (".", False),
        ],
    )
    def test_cases(self, name, expected):
        assert core.entry_name_ok(name) is expected


class TestJsonWithImagePath:
    """Only imagePath is rewritten, and only in memory."""

    def test_plain_value(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        payload = core.json_with_image_path(
            os.path.join(rt_dataset, "a.json"), "person_1.jpg"
        )
        assert json.loads(payload.decode("utf-8"))["imagePath"] == (
            "person_1.jpg"
        )

    def test_source_json_untouched(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        path = os.path.join(rt_dataset, "a.json")
        before = os.stat(path).st_mtime_ns
        core.json_with_image_path(path, "person_1.jpg")
        assert os.stat(path).st_mtime_ns == before

    def test_keeps_directory_prefix(self, rt_dataset):
        write_json(rt_dataset, "a.json", shapes=[], image_path="images/a.jpg")
        payload = core.json_with_image_path(
            os.path.join(rt_dataset, "a.json"), "person_1.jpg"
        )
        assert json.loads(payload.decode("utf-8"))["imagePath"] == (
            "images/person_1.jpg"
        )

    def test_windows_prefix(self, rt_dataset):
        win_path = "imgs" + chr(92) + "a.jpg"
        write_json(rt_dataset, "a.json", shapes=[], image_path=win_path)
        payload = core.json_with_image_path(
            os.path.join(rt_dataset, "a.json"), "person_1.jpg"
        )
        assert json.loads(payload.decode("utf-8"))["imagePath"] == (
            "imgs/person_1.jpg"
        )

    def test_broken_json_raises(self, rt_dataset):
        path = _write(rt_dataset, "a.json", "{not json")
        with pytest.raises(core.RenameError):
            core.json_with_image_path(path, "person_1.jpg")

    def test_top_level_list_raises(self, rt_dataset):
        path = _write(rt_dataset, "a.json", [1, 2])
        with pytest.raises(core.RenameError):
            core.json_with_image_path(path, "person_1.jpg")
