"""Unit tests of the pure pick layer of the preview tool.

This module imports no GUI class on purpose: the copy and the move
rules have to stay usable from a plain script.
"""

import ast
import os
import tempfile
from types import SimpleNamespace

from anylabeling.custom.preview_tool import core
from anylabeling.custom.preview_tool import pick_core as pick

from conftest import make_shape, trash_listing, write_image, write_json


def _entry(path, name=None, rel_dir=""):
    """Return an ImageEntry of a file path, at the root by default."""

    return core.ImageEntry(
        path=path,
        name=name or os.path.basename(path),
        rel_dir=rel_dir,
    )


def _names(root):
    """Return the sorted names inside a directory."""

    return sorted(os.listdir(root))


def _read(path):
    """Return the bytes of a file."""

    with open(path, "rb") as handle:
        return handle.read()


def _snapshot(root):
    """Return the names and the bytes of every file of a directory."""

    return {
        name: _read(os.path.join(root, name))
        for name in _names(root)
        if os.path.isfile(os.path.join(root, name))
    }


class TestTrashDir:
    """The trash folder sits in the temp folder and is not created."""

    def test_location(self):
        assert pick.trash_dir() == os.path.join(
            tempfile.gettempdir(), "dsh-trash"
        )
        assert pick.PICK_SUBDIR == "picked"
        assert pick.PICK_SEQ_START == 1
        assert ".jpg" in pick.PICK_IMAGE_EXTS
        assert ".json" not in pick.PICK_IMAGE_EXTS


class TestUniqueTarget:
    """A taken name only moves the sequence suffix."""

    def test_plain_name_wins(self):
        target = pick.unique_target("/data", "a.jpg", lambda path: False)
        assert target == os.path.join("/data", "a.jpg")

    def test_taken_name_moves_the_suffix(self):
        taken = {
            os.path.join("/data", "a.jpg"),
            os.path.join("/data", "a_1.jpg"),
        }
        target = pick.unique_target(
            "/data", "a.jpg", lambda path: path in taken
        )
        assert target == os.path.join("/data", "a_2.jpg")

    def test_empty_name(self):
        assert pick.unique_target("/data", "", lambda path: False) == ""

    def test_extension_is_kept(self):
        target = pick.unique_target(
            "/data", "a.tar.gz", lambda path: path.endswith("a.tar.gz")
        )
        assert os.path.basename(target) == "a.tar_1.gz"


class TestPickState:
    """Only an image of the same stem family counts as picked."""

    def test_missing_folder(self, pt_dir, pt_out):
        image = write_image(pt_dir, "a.jpg")
        assert pick.pick_state(_entry(image), pt_out) is False

    def test_side_car_alone_is_not_picked(self, pt_dir, pt_out):
        write_image(pt_dir, "a.jpg")
        picked = os.path.join(pt_out, "picked")
        os.makedirs(picked)
        write_json(picked, "a.json")
        assert pick.pick_state(_entry("a.jpg"), pt_out) is False

    def test_after_a_copy(self, pt_dir, pt_out):
        image = write_image(pt_dir, "a.jpg")
        assert pick.pick_one(image, pt_out).status == "ok"
        assert pick.pick_state(_entry(image), pt_out) is True

    def test_a_sequence_copy_counts(self, pt_dir, pt_out):
        image = write_image(pt_dir, "a.jpg")
        picked = os.path.join(pt_out, "picked")
        os.makedirs(picked)
        write_image(picked, "a_1.jpg")
        assert pick.pick_state(_entry(image), pt_out) is True

    def test_another_stem_does_not_count(self, pt_dir, pt_out):
        image = write_image(pt_dir, "a.jpg")
        picked = os.path.join(pt_out, "picked")
        os.makedirs(picked)
        write_image(picked, "ab.jpg")
        write_image(picked, "a_1x.jpg")
        assert pick.pick_state(_entry(image), pt_out) is False

    def test_entry_with_only_a_name(self, pt_dir, pt_out):
        image = write_image(pt_dir, "a.jpg")
        pick.pick_one(image, pt_out)
        assert pick.pick_state(SimpleNamespace(name="a.jpg"), pt_out) is True

    def test_entry_with_only_a_path(self, pt_dir, pt_out):
        image = write_image(pt_dir, "a.jpg")
        pick.pick_one(image, pt_out)
        assert pick.pick_state(SimpleNamespace(path=image), pt_out) is True

    def test_plain_string_entry(self, pt_dir, pt_out):
        image = write_image(pt_dir, "a.jpg")
        pick.pick_one(image, pt_out)
        assert pick.pick_state(image, pt_out) is True

    def test_empty_entry(self, pt_out):
        assert pick.pick_state(object(), pt_out) is False


class TestPickOne:
    """The copy never overwrites and never leaves a placeholder."""

    def test_copies_image_and_side_car(self, pt_dir, pt_out):
        image = write_image(pt_dir, "a.jpg")
        side = write_json(pt_dir, "a.json", {"shapes": [make_shape()]})
        before = _read(image)
        outcome = pick.pick_one(image, pt_out)
        assert outcome.status == "ok"
        assert outcome.source == image
        assert outcome.error is None
        assert outcome.target == os.path.join(pt_out, "picked", "a.jpg")
        assert outcome.sidecar == os.path.join(pt_out, "picked", "a.json")
        assert _read(outcome.target) == before
        assert _read(outcome.sidecar) == _read(side)

    def test_source_directory_is_untouched(self, pt_dir, pt_out):
        write_image(pt_dir, "a.jpg")
        write_json(pt_dir, "a.json")
        before = _snapshot(pt_dir)
        pick.pick_one(os.path.join(pt_dir, "a.jpg"), pt_out)
        assert _snapshot(pt_dir) == before

    def test_without_a_side_car(self, pt_dir, pt_out):
        image = write_image(pt_dir, "a.jpg")
        outcome = pick.pick_one(image, pt_out)
        assert outcome.status == "ok"
        assert outcome.sidecar == ""
        assert _names(os.path.join(pt_out, "picked")) == ["a.jpg"]

    def test_duplicate_gets_a_sequence_suffix(self, pt_dir, pt_out):
        image = write_image(pt_dir, "a.jpg")
        write_json(pt_dir, "a.json")
        first = pick.pick_one(image, pt_out)
        second = pick.pick_one(image, pt_out)
        assert first.target != second.target
        assert os.path.basename(second.target) == "a_1.jpg"
        assert os.path.basename(second.sidecar) == "a_1.json"
        assert _names(os.path.join(pt_out, "picked")) == [
            "a.jpg",
            "a.json",
            "a_1.jpg",
            "a_1.json",
        ]

    def test_three_copies_are_kept(self, pt_dir, pt_out):
        image = write_image(pt_dir, "a.jpg")
        for _round in range(3):
            assert pick.pick_one(image, pt_out).status == "ok"
        assert _names(os.path.join(pt_out, "picked")) == [
            "a.jpg",
            "a_1.jpg",
            "a_2.jpg",
        ]

    def test_picked_folder_is_created(self, pt_dir, pt_out):
        picked = os.path.join(pt_out, "picked")
        assert not os.path.exists(picked)
        image = write_image(pt_dir, "a.jpg")
        assert pick.pick_one(image, pt_out).status == "ok"
        assert os.path.isdir(picked)

    def test_missing_source(self, pt_out):
        outcome = pick.pick_one(os.path.join(pt_out, "gone.jpg"), pt_out)
        assert outcome.status == "missing"
        assert outcome.error is None

    def test_broken_destination_is_reported(self, pt_dir, pt_out):
        image = write_image(pt_dir, "a.jpg")
        with open(os.path.join(pt_out, "picked"), "w") as handle:
            handle.write("x")
        outcome = pick.pick_one(image, pt_out)
        assert outcome.status == "error"
        assert outcome.error

    def test_orphan_side_car_is_not_overwritten(self, pt_dir, pt_out):
        image = write_image(pt_dir, "a.jpg")
        write_json(pt_dir, "a.json", {"shapes": [make_shape()]})
        picked = os.path.join(pt_out, "picked")
        os.makedirs(picked)
        orphan = write_json(picked, "a.json", {"shapes": []})
        outcome = pick.pick_one(image, pt_out)
        assert outcome.status == "ok"
        assert outcome.sidecar == ""
        assert _read(orphan) == b'{"shapes": []}'


class TestUnpickOne:
    """One picked file and its side car move into the temp trash."""

    def test_moves_file_and_side_car(self, pt_dir, pt_out):
        image = write_image(pt_dir, "a.jpg")
        write_json(pt_dir, "a.json")
        pick.pick_one(image, pt_out)
        outcome = pick.unpick_one(pt_out, "a.jpg")
        assert outcome.status == "ok"
        assert outcome.target.startswith(pick.trash_dir())
        assert outcome.sidecar.startswith(pick.trash_dir())
        assert os.path.isfile(outcome.target)
        assert os.path.isfile(outcome.sidecar)
        assert os.path.basename(outcome.target) in trash_listing()
        assert _names(os.path.join(pt_out, "picked")) == []

    def test_invalid_names(self, pt_out):
        sep = chr(92)
        for name in ("../a.jpg", "a/b.jpg", "", ".", "..", "a" + sep + "b"):
            assert pick.unpick_one(pt_out, name).status == "invalid"

    def test_missing_file(self, pt_dir, pt_out):
        image = write_image(pt_dir, "a.jpg")
        pick.pick_one(image, pt_out)
        outcome = pick.unpick_one(pt_out, "b.jpg")
        assert outcome.status == "missing"


class TestUnpickStem:
    """Every copy of a stem leaves the picked folder together."""

    def test_moves_every_copy_and_side_car(self, pt_dir, pt_out):
        write_image(pt_dir, "a.jpg")
        write_json(pt_dir, "a.json")
        write_image(pt_dir, "a_2.jpg")
        write_json(pt_dir, "a_2.json")
        pick.pick_one(os.path.join(pt_dir, "a.jpg"), pt_out)
        pick.pick_one(os.path.join(pt_dir, "a_2.jpg"), pt_out)
        assert _names(os.path.join(pt_out, "picked")) == [
            "a.jpg",
            "a.json",
            "a_2.jpg",
            "a_2.json",
        ]
        result = pick.unpick_stem(pt_out, "a")
        assert result.status == "ok"
        assert result.failed == ()
        assert len(result.moved) == 4
        for item in result.moved:
            assert os.path.isfile(item.target)
            assert item.target.startswith(pick.trash_dir())
            assert not os.path.exists(item.source)
        assert _names(os.path.join(pt_out, "picked")) == []
        assert "已移除 4 个文件" in result.message
        assert result.message.count("→") == 4

    def test_other_stem_is_untouched(self, pt_dir, pt_out):
        a = write_image(pt_dir, "a.jpg")
        b = write_image(pt_dir, "b.jpg")
        pick.pick_one(a, pt_out)
        pick.pick_one(b, pt_out)
        assert pick.unpick_stem(pt_out, "a").status == "ok"
        assert _names(os.path.join(pt_out, "picked")) == ["b.jpg"]

    def test_side_car_alone_is_missing(self, pt_dir, pt_out):
        picked = os.path.join(pt_out, "picked")
        os.makedirs(picked)
        write_json(picked, "a.json")
        result = pick.unpick_stem(pt_out, "a")
        assert result.status == "missing"
        assert result.moved == ()
        assert _names(picked) == ["a.json"]

    def test_missing_directory(self, pt_out):
        result = pick.unpick_stem(pt_out, "a")
        assert result.status == "missing"
        assert result.moved == ()

    def test_invalid_stem(self, pt_out):
        for stem in ("a/b", "", ".", "..", "a" + chr(92) + "b"):
            assert pick.unpick_stem(pt_out, stem).status == "invalid"

    def test_foreign_file_with_the_same_stem_is_moved(self, pt_dir, pt_out):
        picked = os.path.join(pt_out, "picked")
        os.makedirs(picked)
        foreign = write_image(picked, "a.png")
        result = pick.unpick_stem(pt_out, "a")
        assert result.status == "ok"
        assert len(result.moved) == 1
        assert not os.path.exists(foreign)
        assert os.path.isfile(result.moved[0].target)

    def test_every_trash_name_is_distinct(self, pt_dir, pt_out):
        write_image(pt_dir, "a.jpg")
        write_json(pt_dir, "a.json")
        pick.pick_one(os.path.join(pt_dir, "a.jpg"), pt_out)
        result = pick.unpick_stem(pt_out, "a")
        targets = [item.target for item in result.moved]
        assert len(targets) == len(set(targets))


class TestReversibleSwitch:
    """A copy can be taken back and made again, in any order."""

    def test_copy_remove_copy(self, pt_dir, pt_out):
        image = write_image(pt_dir, "a.jpg")
        write_json(pt_dir, "a.json")
        entry = _entry(image)
        assert pick.pick_state(entry, pt_out) is False
        assert pick.pick_one(image, pt_out).status == "ok"
        assert pick.pick_state(entry, pt_out) is True
        assert pick.unpick_stem(pt_out, "a").status == "ok"
        assert pick.pick_state(entry, pt_out) is False
        assert pick.pick_one(image, pt_out).status == "ok"
        assert pick.pick_state(entry, pt_out) is True
        assert os.path.isfile(image)
        assert _names(os.path.join(pt_out, "picked")) == ["a.jpg", "a.json"]

    def test_remove_remove_is_harmless(self, pt_dir, pt_out):
        image = write_image(pt_dir, "a.jpg")
        pick.pick_one(image, pt_out)
        assert pick.unpick_stem(pt_out, "a").status == "ok"
        assert pick.unpick_stem(pt_out, "a").status == "missing"


class TestPickAll:
    """A batch skips what is picked already and can be cancelled."""

    def test_copies_and_skips(self, pt_dir, pt_out):
        a = write_image(pt_dir, "a.jpg")
        b = write_image(pt_dir, "b.jpg")
        pick.pick_one(a, pt_out)
        seen = []
        copied, skipped, stems = pick.pick_all(
            [_entry(a), _entry(b)],
            pt_out,
            progress_cb=lambda done, total: seen.append((done, total)),
        )
        assert (copied, skipped) == (1, 1)
        assert stems == (("", "b"),)
        assert seen == [(1, 2), (2, 2)]

    def test_progress_covers_skipped_entries(self, pt_dir, pt_out):
        a = write_image(pt_dir, "a.jpg")
        pick.pick_one(a, pt_out)
        seen = []
        pick.pick_all(
            [_entry(a)], pt_out, progress_cb=lambda d, t: seen.append((d, t))
        )
        assert seen == [(1, 1)]

    def test_cancel_stops_after_the_first_entry(self, pt_dir, pt_out):
        a = write_image(pt_dir, "a.jpg")
        b = write_image(pt_dir, "b.jpg")
        calls = []

        def should_stop():
            calls.append(True)
            return len(calls) > 1

        copied, skipped, stems = pick.pick_all(
            [_entry(a), _entry(b)], pt_out, should_stop=should_stop
        )
        assert (copied, skipped, stems) == (1, 0, (("", "a"),))
        assert not os.path.exists(os.path.join(pt_out, "picked", "b.jpg"))

    def test_cancel_before_the_first_entry(self, pt_dir, pt_out):
        a = write_image(pt_dir, "a.jpg")
        copied, skipped, stems = pick.pick_all(
            [_entry(a)], pt_out, should_stop=lambda: True
        )
        assert (copied, skipped, stems) == (0, 0, ())
        assert not os.path.exists(os.path.join(pt_out, "picked"))

    def test_missing_source_is_not_counted(self, pt_dir, pt_out):
        a = write_image(pt_dir, "a.jpg")
        gone = os.path.join(pt_dir, "gone.jpg")
        copied, skipped, stems = pick.pick_all(
            [_entry(gone), _entry(a)], pt_out
        )
        assert (copied, skipped, stems) == (1, 0, (("", "a"),))

    def test_entries_may_be_plain_objects(self, pt_dir, pt_out):
        a = write_image(pt_dir, "a.jpg")
        copied, _skipped, stems = pick.pick_all(
            [SimpleNamespace(path=a, name="a.jpg")], pt_out
        )
        assert (copied, stems) == (1, (("", "a"),))


class TestDetectPicked:
    """The probe answers with the stems that are picked already."""

    def test_mixed_entries(self, pt_dir, pt_out):
        a = write_image(pt_dir, "a.jpg")
        b = write_image(pt_dir, "b.jpg")
        pick.pick_one(a, pt_out)
        assert pick.detect_picked([_entry(a), _entry(b)], pt_out) == (
            frozenset({("", "a")})
        )

    def test_missing_folder(self, pt_dir, pt_out):
        a = write_image(pt_dir, "a.jpg")
        assert pick.detect_picked([_entry(a)], pt_out) == frozenset()

    def test_empty_input(self, pt_out):
        assert pick.detect_picked([], pt_out) == frozenset()


class TestRelativePickedDirs:
    """picked/ keeps the relative directory of every image."""

    def test_copy_lands_in_its_subdirectory(self, pt_dir, pt_out):
        source = write_image(pt_dir, "a.jpg")
        side = write_json(pt_dir, "a.json")
        outcome = pick.pick_one(source, pt_out, "sub/deep")
        target = os.path.join(
            pt_out, "picked", "sub", "deep", "a.jpg"
        )
        assert outcome.status == "ok"
        assert outcome.target == target
        assert outcome.sidecar == os.path.splitext(target)[0] + ".json"
        assert _read(target) == _read(source)
        assert _read(outcome.sidecar) == _read(side)

    def test_same_name_in_two_directories_does_not_collide(
        self, pt_dir, pt_out
    ):
        first = write_image(pt_dir, os.path.join("one", "a.jpg"))
        second = write_image(pt_dir, os.path.join("two", "a.jpg"))
        one = pick.pick_one(first, pt_out, "one")
        two = pick.pick_one(second, pt_out, "two")
        assert one.target == os.path.join(
            pt_out, "picked", "one", "a.jpg"
        )
        assert two.target == os.path.join(
            pt_out, "picked", "two", "a.jpg"
        )

    def test_pick_state_is_scoped_to_the_subdirectory(self, pt_dir, pt_out):
        first = write_image(pt_dir, os.path.join("one", "a.jpg"))
        pick.pick_one(first, pt_out, "one")
        assert pick.pick_state(_entry(first, rel_dir="one"), pt_out) is True
        assert pick.pick_state(_entry(first, rel_dir="two"), pt_out) is False
        assert pick.pick_state(_entry(first), pt_out) is False

    def test_duplicate_in_the_same_subdirectory_gets_a_suffix(
        self, pt_dir, pt_out
    ):
        source = write_image(pt_dir, os.path.join("one", "a.jpg"))
        write_json(pt_dir, os.path.join("one", "a.json"))
        first = pick.pick_one(source, pt_out, "one")
        second = pick.pick_one(source, pt_out, "one")
        assert os.path.basename(first.target) == "a.jpg"
        assert os.path.basename(second.target) == "a_1.jpg"
        assert os.path.basename(second.sidecar) == "a_1.json"
        assert os.path.dirname(second.target) == os.path.join(
            pt_out, "picked", "one"
        )

    def test_root_and_subdirectory_names_are_independent(
        self, pt_dir, pt_out
    ):
        pick.pick_one(write_image(pt_dir, "a.jpg"), pt_out)
        nested = write_image(pt_dir, os.path.join("one", "a.jpg"))
        outcome = pick.pick_one(nested, pt_out, "one")
        assert outcome.status == "ok"
        assert os.path.basename(outcome.target) == "a.jpg"

    def test_remove_touches_only_its_relative_directory(self, pt_dir, pt_out):
        first = write_image(pt_dir, os.path.join("one", "a.jpg"))
        second = write_image(pt_dir, os.path.join("two", "a.jpg"))
        pick.pick_one(first, pt_out, "one")
        pick.pick_one(second, pt_out, "two")
        result = pick.unpick_stem(pt_out, "a", "one")
        assert result.status == "ok"
        assert len(result.moved) == 1
        assert os.path.isfile(
            os.path.join(pt_out, "picked", "two", "a.jpg")
        )
        assert not os.path.exists(
            os.path.join(pt_out, "picked", "one", "a.jpg")
        )

    def test_unpick_one_moves_only_its_relative_directory(
        self, pt_dir, pt_out
    ):
        first = write_image(pt_dir, os.path.join("one", "a.jpg"))
        second = write_image(pt_dir, os.path.join("two", "a.jpg"))
        pick.pick_one(first, pt_out, "one")
        pick.pick_one(second, pt_out, "two")
        outcome = pick.unpick_one(pt_out, "a.jpg", "one")
        assert outcome.status == "ok"
        assert outcome.target.startswith(pick.trash_dir())
        assert os.path.isfile(
            os.path.join(pt_out, "picked", "two", "a.jpg")
        )

    def test_batch_keeps_every_relative_directory(self, pt_dir, pt_out):
        first = write_image(pt_dir, os.path.join("one", "a.jpg"))
        second = write_image(pt_dir, os.path.join("two", "a.jpg"))
        entries = [
            _entry(first, rel_dir="one"),
            _entry(second, rel_dir="two"),
        ]
        copied, skipped, keys = pick.pick_all(entries, pt_out)
        assert (copied, skipped) == (2, 0)
        assert keys == (("one", "a"), ("two", "a"))
        assert os.path.isfile(
            os.path.join(pt_out, "picked", "one", "a.jpg")
        )
        assert os.path.isfile(
            os.path.join(pt_out, "picked", "two", "a.jpg")
        )

    def test_detect_reports_the_relative_directory(self, pt_dir, pt_out):
        first = write_image(pt_dir, os.path.join("one", "a.jpg"))
        pick.pick_one(first, pt_out, "one")
        entries = [
            _entry(first, rel_dir="one"),
            _entry(first, rel_dir="two"),
        ]
        assert pick.detect_picked(entries, pt_out) == frozenset(
            {("one", "a")}
        )

    def test_unsafe_relative_directories_are_refused(self, pt_dir, pt_out):
        source = write_image(pt_dir, "a.jpg")
        unsafe = ("../escape", os.path.abspath(pt_out), "a/../../b")
        for rel_dir in unsafe:
            assert pick.pick_one(source, pt_out, rel_dir).status == (
                "invalid"
            )
            assert not pick.pick_state(
                _entry(source, rel_dir=rel_dir), pt_out
            )
            assert pick.unpick_stem(pt_out, "a", rel_dir).status == (
                "invalid"
            )

    def test_normalize_rel_dir(self):
        assert pick.normalize_rel_dir("") == ""
        assert pick.normalize_rel_dir(".") == ""
        assert pick.normalize_rel_dir(None) == ""
        assert pick.normalize_rel_dir("a/b") == os.path.join("a", "b")
        assert pick.normalize_rel_dir("../x") == ""
        assert pick.normalize_rel_dir(os.path.sep + "abs") == ""

    def test_entry_key_shapes(self):
        entry = _entry("/tmp/a.jpg", rel_dir="sub")
        assert pick.entry_key(entry) == ("sub", "a")
        assert pick.entry_key("a.jpg") == ("", "a")
        assert pick.entry_key(("sub", "a")) == ("sub", "a")
        assert pick.entry_rel_dir(entry) == "sub"


class TestPickedDir:
    """The picked folder path of one relative directory."""

    def test_root(self, pt_out):
        assert pick.picked_dir(pt_out) == os.path.join(pt_out, "picked")

    def test_subdirectory(self, pt_out):
        assert pick.picked_dir(pt_out, os.path.join("a", "b")) == (
            os.path.join(pt_out, "picked", "a", "b")
        )


class TestOutputSkipDirs:
    """The scan leaves the output subtree of the input alone."""

    def test_output_below_the_input_is_skipped(self, pt_dir):
        out = os.path.join(pt_dir, "out")
        os.makedirs(out)
        assert pick.output_skip_dirs(pt_dir, out) == (out,)

    def test_output_equal_to_the_input_skips_the_picked_folder(self, pt_dir):
        assert pick.output_skip_dirs(pt_dir, pt_dir) == (
            os.path.join(pt_dir, "picked"),
        )

    def test_output_outside_the_input_needs_no_skip(self, pt_dir, pt_out):
        assert pick.output_skip_dirs(pt_dir, pt_out) == ()

    def test_missing_values_need_no_skip(self, pt_dir):
        assert pick.output_skip_dirs(pt_dir, "") == ()
        assert pick.output_skip_dirs("", pt_dir) == ()

    def test_parent_of_the_input_is_not_inside_it(self, pt_dir):
        parent = os.path.dirname(pt_dir)
        assert pick.output_skip_dirs(pt_dir, parent) == ()


BANNED_ATTRS = ("unlink", "rmtree")
BANNED_OS_ATTRS = ("remove", "unlink", "rmtree", "rmdir")
BANNED_NAMES = ("remove", "unlink", "rmtree", "rmdir")


def _is_module(node, names):
    """Return True when a call target is a module of the given names."""

    if isinstance(node, ast.Name):
        return node.id in names
    if isinstance(node, ast.Attribute):
        return node.attr in names
    return False


def _banned_calls(tree):
    """Return the deleting calls of one parsed module."""

    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            if func.attr in BANNED_ATTRS:
                found.append(func.attr)
            elif func.attr in BANNED_OS_ATTRS and _is_module(
                func.value, ("os", "shutil")
            ):
                found.append("os." + func.attr)
        elif isinstance(func, ast.Name) and func.id in BANNED_NAMES:
            found.append(func.id)
    return found


class TestNoDeleteCall:
    """No module of the package may ever delete a file."""

    def test_package_sources(self):
        package = os.path.dirname(core.__file__)
        offenders = {}
        for name in sorted(os.listdir(package)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(package, name), encoding="utf-8") as f:
                text = f.read()
            try:
                tree = ast.parse(text)
            except SyntaxError:
                continue
            found = _banned_calls(tree)
            if found:
                offenders[name] = found
        assert offenders == {}

    def test_pick_core_has_no_gui_import(self):
        with open(pick.__file__, encoding="utf-8") as handle:
            text = handle.read()
        assert "PyQt6" not in text
        assert "QtCore" not in text
        assert "QtWidgets" not in text
