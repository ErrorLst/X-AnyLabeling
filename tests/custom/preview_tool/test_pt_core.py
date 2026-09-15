"""Unit tests of the pure data layer of the preview tool core.

This module imports no GUI class on purpose: the scan, the parsing and
the filter have to stay usable without a widget.
"""

import os

from anylabeling.custom.preview_tool import core

from conftest import make_shape, write_image, write_json


def _shape(
    label="cat",
    score=None,
    width=20.0,
    height=20.0,
    shape_type="rectangle",
):
    """Return a ShapeInfo with a bounding box of the given size."""

    return core.ShapeInfo(
        label=label,
        score=score,
        shape_type=shape_type,
        points=((0.0, 0.0), (width, height)),
        width=width,
        height=height,
    )


def _entry(name, shapes=(), path=None):
    """Return an ImageEntry of the given shapes."""

    return core.ImageEntry(
        path=path or os.path.join("/tmp/preview", name),
        name=name,
        shapes=tuple(shapes),
    )


def _cfg(score=0.45, width=10.0, height=10.0, mode=None):
    """Return a filter with the given thresholds."""

    return core.PreviewFilter(
        score_threshold=score,
        size_mode=mode or core.SizeFilterMode.ANY_ABOVE,
        width_threshold=width,
        height_threshold=height,
    )


class TestNaturalKey:
    """The sort key keeps numeric runs in numeric order."""

    def test_numeric_order(self):
        names = ["10.jpg", "2.jpg", "1.jpg", "20.jpg"]
        assert sorted(names, key=core.natural_key) == [
            "1.jpg",
            "2.jpg",
            "10.jpg",
            "20.jpg",
        ]

    def test_case_folds(self):
        assert core.natural_key("ABC") == core.natural_key("abc")

    def test_non_decimal_digit_is_a_character(self):
        assert core.natural_key("a" + chr(178) + ".jpg") == [
            (1, "a"),
            (1, chr(178)),
            (1, "."),
            (1, "j"),
            (1, "p"),
            (1, "g"),
        ]

    def test_mixed_names_do_not_break_the_scan(self, pt_dir):
        for name in ("a.png", "10.png", "2.png", "1.png", "b.png"):
            write_image(pt_dir, name, (4, 4))
        names = [entry.name for entry in core.scan_directory(pt_dir)]
        assert names == ["1.png", "2.png", "10.png", "a.png", "b.png"]

    def test_a_number_and_a_letter_sort(self):
        names = ["1.jpg", "a.jpg"]
        assert sorted(names, key=core.natural_key) == ["1.jpg", "a.jpg"]

    def test_digits_and_letters_mix(self):
        names = ["b2.jpg", "a10.jpg", "a2.jpg", "1.jpg"]
        assert sorted(names, key=core.natural_key) == [
            "1.jpg",
            "a2.jpg",
            "a10.jpg",
            "b2.jpg",
        ]


class TestShapeInfo:
    """The bounding box is derived from the points only."""

    def test_bbox_of_two_points(self):
        shape = core.ShapeInfo(
            label="cat",
            score=None,
            shape_type="rectangle",
            points=((3.0, 4.0), (1.0, 9.0)),
            width=2.0,
            height=5.0,
        )
        assert shape.bbox() == (1.0, 4.0, 3.0, 9.0)

    def test_bbox_without_points(self):
        shape = core.ShapeInfo("cat", None, "point", (), 0.0, 0.0)
        assert shape.bbox() == (0.0, 0.0, 0.0, 0.0)


class TestParseAnnotations:
    """A LabelMe document is parsed defensively."""

    def test_rectangle_with_score(self):
        shapes = core.parse_annotations(
            {"shapes": [make_shape(score=0.9)]}
        )
        assert len(shapes) == 1
        shape = shapes[0]
        assert shape.label == "cat"
        assert shape.score == 0.9
        assert shape.shape_type == "rectangle"
        assert shape.width == 10.0
        assert shape.height == 10.0

    def test_missing_score_is_none(self):
        shapes = core.parse_annotations({"shapes": [make_shape()]})
        assert shapes[0].score is None

    def test_non_numeric_score_is_none(self):
        data = {
            "shapes": [
                make_shape(score="0.9"),
                make_shape(score=True),
                make_shape(score=[1]),
            ]
        }
        assert [shape.score for shape in core.parse_annotations(data)] == [
            None,
            None,
            None,
        ]

    def test_integer_score_is_a_float(self):
        shapes = core.parse_annotations({"shapes": [make_shape(score=1)]})
        assert shapes[0].score == 1.0
        assert isinstance(shapes[0].score, float)

    def test_every_shape_type_is_accepted(self):
        data = {
            "shapes": [
                make_shape(shape_type=kind, points=((1, 1), (2, 2)))
                for kind in core.SHAPE_TYPES
            ]
        }
        assert [s.shape_type for s in core.parse_annotations(data)] == list(
            core.SHAPE_TYPES
        )

    def test_single_point_shape(self):
        shape = core.parse_annotations(
            {"shapes": [make_shape(shape_type="point", points=((5, 6),))]}
        )[0]
        assert shape.points == ((5.0, 6.0),)
        assert (shape.width, shape.height) == (0.0, 0.0)

    def test_broken_shapes_are_skipped(self):
        data = {
            "shapes": [
                "not a dict",
                {"shape_type": "rectangle"},
                {"shape_type": "unknown", "points": [[1, 1], [2, 2]]},
                {"shape_type": "rectangle", "points": []},
                {"shape_type": "rectangle", "points": [[1], [2, 2]]},
                {"shape_type": "rectangle", "points": [["a", "b"], [1, 1]]},
                {"shape_type": "rectangle", "points": [[1, 1], "x"]},
                make_shape(),
            ]
        }
        shapes = core.parse_annotations(data)
        assert len(shapes) == 1
        assert shapes[0].label == "cat"

    def test_bad_documents_yield_an_empty_list(self):
        for data in (None, [], "x", 5, {"shapes": None}, {"shapes": {}}):
            assert core.parse_annotations(data) == []

    def test_label_that_is_not_a_string_is_converted(self):
        shape = core.parse_annotations(
            {"shapes": [make_shape(label=7)]}
        )[0]
        assert shape.label == "7"

    def test_points_may_be_tuples(self):
        shape = core.parse_annotations(
            {"shapes": [make_shape(points=((1, 2), (3, 4)))]}
        )[0]
        assert shape.points == ((1.0, 2.0), (3.0, 4.0))


class TestScanDirectory:
    """Only top level images are listed, in natural order."""

    def test_extension_case_and_order(self, pt_dir):
        write_image(pt_dir, "b.JPG", (4, 4))
        write_image(pt_dir, "a10.png", (4, 4))
        write_image(pt_dir, "a2.png", (4, 4))
        with open(os.path.join(pt_dir, "notes.txt"), "wb") as handle:
            handle.write(b"x")
        names = [entry.name for entry in core.scan_directory(pt_dir)]
        assert names == ["a2.png", "a10.png", "b.JPG"]

    def test_top_level_only(self, pt_dir):
        sub = os.path.join(pt_dir, "sub")
        os.makedirs(sub)
        write_image(sub, "inside.png", (4, 4))
        write_image(pt_dir, "top.png", (4, 4))
        assert [entry.name for entry in core.scan_directory(pt_dir)] == [
            "top.png"
        ]

    def test_entries_carry_path_and_side_car(self, pt_dir):
        image = write_image(pt_dir, "one.png", (4, 4))
        write_json(pt_dir, "one.json")
        entry = core.scan_directory(pt_dir)[0]
        assert entry.path == image
        assert entry.name == "one.png"
        assert entry.json_path == os.path.join(pt_dir, "one.json")
        assert entry.labels == frozenset()
        assert entry.shapes == ()

    def test_image_without_side_car(self, pt_dir):
        write_image(pt_dir, "one.png", (4, 4))
        assert core.scan_directory(pt_dir)[0].json_path is None

    def test_missing_directory(self, pt_dir):
        assert core.scan_directory(os.path.join(pt_dir, "nope")) == []

    def test_path_is_not_a_directory(self, pt_dir):
        path = write_image(pt_dir, "one.png", (4, 4))
        assert core.scan_directory(path) == []

    def test_empty_directory(self, pt_dir):
        assert core.scan_directory(pt_dir) == []


class TestFindLabelFile:
    """The side car sits next to the image under the same stem."""

    def test_found(self, pt_dir):
        image = write_image(pt_dir, "one.png", (4, 4))
        side = write_json(pt_dir, "one.json")
        assert core.find_label_file(image) == side

    def test_absent(self, pt_dir):
        image = write_image(pt_dir, "one.png", (4, 4))
        assert core.find_label_file(image) is None

    def test_garbage_path(self):
        assert core.find_label_file(None) is None


class TestLocalize:
    """The label file and the shapes of one image are read together."""

    def test_relative_name(self, pt_dir):
        write_image(pt_dir, "one.png", (4, 4))
        write_json(pt_dir, "one.json", {"shapes": [make_shape(score=0.5)]})
        json_path, shapes = core.localize(pt_dir, "one.png")
        assert json_path == os.path.join(pt_dir, "one.json")
        assert len(shapes) == 1
        assert shapes[0].score == 0.5

    def test_absolute_name(self, pt_dir):
        image = write_image(pt_dir, "one.png", (4, 4))
        write_json(pt_dir, "one.json")
        json_path, shapes = core.localize("/nowhere", image)
        assert json_path == os.path.join(pt_dir, "one.json")
        assert shapes == ()

    def test_missing_side_car_is_collected(self, pt_dir):
        write_image(pt_dir, "one.png", (4, 4))
        missing = []
        assert core.localize(pt_dir, "one.png", missing) == (None, ())
        assert missing == ["one.png"]

    def test_broken_side_car_is_collected(self, pt_dir):
        write_image(pt_dir, "one.png", (4, 4))
        path = os.path.join(pt_dir, "one.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        missing = set()
        json_path, shapes = core.localize(pt_dir, "one.png", missing)
        assert json_path == path
        assert shapes == ()
        assert missing == {"one.png"}

    def test_missing_collector_is_optional(self, pt_dir):
        assert core.localize(pt_dir, "gone.png") == (None, ())

    def test_garbage_input_does_not_raise(self):
        assert core.localize(None, None) == (None, ())


class TestScoreOk:
    """A missing score counts as 0.0 and a zero threshold is off."""

    def test_missing_score(self):
        assert not core.score_ok(None, _cfg(score=0.45))
        assert core.score_ok(None, _cfg(score=0.0))

    def test_threshold_boundary(self):
        assert core.score_ok(0.45, _cfg(score=0.45))
        assert not core.score_ok(0.44, _cfg(score=0.45))

    def test_garbage_score(self):
        assert not core.score_ok("x", _cfg(score=0.45))
        assert not core.score_ok(float("nan"), _cfg(score=0.45))


class TestSizeOk:
    """The four modes compare strictly."""

    def test_strict_boundaries(self):
        equal = _shape(width=10.0, height=10.0)
        assert not core.size_ok(
            equal, _cfg(mode=core.SizeFilterMode.ANY_ABOVE)
        )
        assert not core.size_ok(
            equal, _cfg(mode=core.SizeFilterMode.BOTH_ABOVE)
        )
        assert not core.size_ok(
            equal, _cfg(mode=core.SizeFilterMode.ANY_BELOW)
        )
        assert not core.size_ok(
            equal, _cfg(mode=core.SizeFilterMode.BOTH_BELOW)
        )

    def test_any_above(self):
        shape = _shape(width=11.0, height=5.0)
        cfg = _cfg(mode=core.SizeFilterMode.ANY_ABOVE)
        assert core.size_ok(shape, cfg)

    def test_both_above(self):
        cfg = _cfg(mode=core.SizeFilterMode.BOTH_ABOVE)
        assert core.size_ok(_shape(width=11.0, height=11.0), cfg)
        assert not core.size_ok(_shape(width=11.0, height=5.0), cfg)

    def test_any_below(self):
        cfg = _cfg(mode=core.SizeFilterMode.ANY_BELOW)
        assert core.size_ok(_shape(width=9.0, height=5.0), cfg)
        assert core.size_ok(_shape(width=5.0, height=9.0), cfg)

    def test_both_below(self):
        cfg = _cfg(mode=core.SizeFilterMode.BOTH_BELOW)
        assert core.size_ok(_shape(width=9.0, height=9.0), cfg)
        assert not core.size_ok(_shape(width=9.0, height=11.0), cfg)

    def test_mode_labels_and_side(self):
        assert core.SizeFilterMode.ANY_ABOVE.above is True
        assert core.SizeFilterMode.BOTH_BELOW.above is False
        assert core.SizeFilterMode.ANY_ABOVE.value == "any_above"
        assert core.SizeFilterMode.ANY_BELOW.label
        assert (
            core.SizeFilterMode("both_below")
            is core.SizeFilterMode.BOTH_BELOW
        )


class TestFilterImages:
    """The score and the size axis are joined with "and"."""

    def test_master_switch_off_keeps_everything(self):
        entries = [_entry("a.jpg"), _entry("b.jpg", [_shape()])]
        kept, total, _categories = core.filter_images(entries, None, None)
        assert kept == entries
        assert total == 2

    def test_score_passes_and_size_fails(self):
        entry = _entry("a.jpg", [_shape(score=0.9, width=1.0, height=1.0)])
        kept, _total, _categories = core.filter_images(
            [entry], _cfg(), None
        )
        assert kept == []

    def test_size_passes_and_score_fails(self):
        entry = _entry("a.jpg", [_shape(score=0.1, width=50.0, height=50.0)])
        kept, _total, _categories = core.filter_images(
            [entry], _cfg(), None
        )
        assert kept == []

    def test_both_axes_pass_on_one_shape(self):
        entry = _entry("a.jpg", [_shape(score=0.9, width=50.0, height=50.0)])
        kept, _total, _categories = core.filter_images(
            [entry], _cfg(), None
        )
        assert kept == [entry]

    def test_the_two_axes_may_be_satisfied_by_different_shapes(self):
        entry = _entry(
            "a.jpg",
            [
                _shape(label="small", score=0.9, width=1.0, height=1.0),
                _shape(label="big", score=0.1, width=50.0, height=50.0),
            ],
        )
        kept, _total, _categories = core.filter_images(
            [entry], _cfg(), None
        )
        assert kept == [entry]

    def test_score_axis_off(self):
        entry = _entry("a.jpg", [_shape(score=None, width=50.0)])
        kept, _total, _categories = core.filter_images(
            [entry], _cfg(score=0.0), None
        )
        assert kept == [entry]

    def test_size_axis_open_at_zero(self):
        entry = _entry("a.jpg", [_shape(score=0.9, width=5.0, height=0.0)])
        kept, _total, _categories = core.filter_images(
            [entry], _cfg(width=0.0, height=0.0), None
        )
        assert kept == [entry]

    def test_size_axis_closed_by_a_high_threshold(self):
        entry = _entry("a.jpg", [_shape(score=0.9, width=5.0, height=5.0)])
        kept, _total, _categories = core.filter_images(
            [entry], _cfg(width=50.0, height=50.0), None
        )
        assert kept == []

    def test_background_selected(self):
        entry = _entry("a.jpg")
        kept, _total, _categories = core.filter_images(
            [entry], _cfg(), {core.BACKGROUND_LABEL}
        )
        assert kept == [entry]

    def test_background_not_selected(self):
        entry = _entry("a.jpg")
        kept, _total, _categories = core.filter_images(
            [entry], _cfg(), {"cat"}
        )
        assert kept == []

    def test_category_axis_off_keeps_the_background(self):
        entry = _entry("a.jpg")
        kept, _total, _categories = core.filter_images([entry], _cfg(), None)
        assert kept == [entry]

    def test_category_axis_drops_unchecked_labels(self):
        cat = _entry("a.jpg", [_shape(label="cat", score=0.9)])
        dog = _entry("b.jpg", [_shape(label="dog", score=0.9)])
        kept, total, _categories = core.filter_images(
            [cat, dog], _cfg(), {"cat"}
        )
        assert kept == [cat]
        assert total == 2

    def test_empty_selection_drops_everything(self):
        cat = _entry("a.jpg", [_shape(label="cat")])
        empty = _entry("b.jpg")
        kept, _total, _categories = core.filter_images(
            [cat, empty], _cfg(), frozenset()
        )
        assert kept == []

    def test_categories_start_with_the_background(self):
        cat = _entry("a.jpg", [_shape(label="cat", score=0.0, width=1.0)])
        dog = _entry("b.jpg", [_shape(label="dog", score=1.0, width=50.0)])
        _kept, _total, categories = core.filter_images(
            [cat, dog], _cfg(), None
        )
        assert categories == [core.BACKGROUND_LABEL, "cat", "dog"]

    def test_categories_are_collected_with_the_switch_off(self):
        cat = _entry("a.jpg", [_shape(label="cat")])
        _kept, _total, categories = core.filter_images([cat], None, None)
        assert categories == [core.BACKGROUND_LABEL, "cat"]

    def test_a_real_background_label_is_not_duplicated(self):
        entry = _entry(
            "a.jpg", [_shape(label=core.BACKGROUND_LABEL, score=0.9)]
        )
        kept, _total, categories = core.filter_images([entry], _cfg(), None)
        assert kept == [entry]
        assert categories.count(core.BACKGROUND_LABEL) == 1
        assert categories[0] == core.BACKGROUND_LABEL

    def test_the_pseudo_background_and_a_real_one_coexist(self):
        empty = _entry("a.jpg")
        real = _entry(
            "b.jpg", [_shape(label=core.BACKGROUND_LABEL, score=0.9)]
        )
        kept, _total, categories = core.filter_images(
            [empty, real], _cfg(), {core.BACKGROUND_LABEL}
        )
        assert kept == [empty, real]
        assert categories.count(core.BACKGROUND_LABEL) == 1
        assert categories[0] == core.BACKGROUND_LABEL

    def test_entry_without_shapes_attribute(self):
        class Bare:
            pass

        kept, total, categories = core.filter_images([Bare()], None, None)
        assert kept
        assert total == 1
        assert categories == [core.BACKGROUND_LABEL]

    def test_no_entries(self):
        assert core.filter_images([], _cfg(), None) == (
            [],
            0,
            [core.BACKGROUND_LABEL],
        )


class TestModuleSurface:
    """The frozen constants and the GUI free source stay as promised."""

    def test_constants(self):
        assert core.BACKGROUND_LABEL == chr(0x80CC) + chr(0x666F)
        assert core.SHAPE_TYPES == (
            "rectangle",
            "polygon",
            "circle",
            "line",
            "point",
            "rotation",
        )
        assert core.FILTER_DEFAULT_SCORE == 0.45
        assert core.FILTER_DEFAULT_SIZE == 10.0
        assert core.SCAN_WORKERS == 4

    def test_source_has_no_gui_import(self):
        with open(core.__file__, encoding="utf-8") as handle:
            text = handle.read()
        assert "PyQt6" not in text
        assert "QtCore" not in text
        assert "QtWidgets" not in text
