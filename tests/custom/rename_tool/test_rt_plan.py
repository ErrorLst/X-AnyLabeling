"""Tests of the naming rules: numbering, compliance and _aug chains."""

import os

import pytest

from anylabeling.custom.rename_tool import rename_core as core

from conftest import make_pair, write_json, write_image


def _plan(root):
    """Scan root and resolve the targets."""

    return core.resolve_targets(core.plan_directory(root))


def _item(root, name):
    """Return the item whose image is name."""

    plan = _plan(root)
    for item in plan.items:
        if item.image_name == name:
            return item
    raise AssertionError("%s not planned: %r" % (name, plan.items))


def _summary(root):
    """Return {stem: target stem} of a folder."""

    plan = _plan(root)
    return {item.stem: item.target_stem for item in plan.items}


class TestNumbering:
    """The smallest free number of the label wins."""

    def test_e1_plain_pair(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        item = _item(rt_dataset, "a.jpg")
        assert item.action == core.ACTION_RENAME
        assert item.target_stem == "person_1"
        assert item.target_image_name() == "person_1.jpg"
        assert item.target_json_name() == "person_1.json"

    def test_e2_compliant_pair(self, rt_dataset):
        make_pair(rt_dataset, "person_1", "person")
        item = _item(rt_dataset, "person_1.jpg")
        assert item.action == core.ACTION_ALREADY
        assert item.target_stem == "person_1"

    def test_e3_reserved_number(self, rt_dataset):
        make_pair(rt_dataset, "person_1", "person")
        make_pair(rt_dataset, "IMG_2", "person")
        assert _item(rt_dataset, "IMG_2.jpg").target_stem == "person_2"
        assert _item(
            rt_dataset, "person_1.jpg"
        ).action == core.ACTION_ALREADY

    def test_e4_free_number_first(self, rt_dataset):
        make_pair(rt_dataset, "person_2", "person")
        make_pair(rt_dataset, "a", "person")
        assert _item(rt_dataset, "a.jpg").target_stem == "person_1"

    def test_smallest_free_hole(self, rt_dataset):
        make_pair(rt_dataset, "person_1", "person")
        make_pair(rt_dataset, "person_3", "person")
        make_pair(rt_dataset, "x", "person")
        assert _item(rt_dataset, "x.jpg").target_stem == "person_2"

    def test_numbering_follows_natural_order(self, rt_dataset):
        make_pair(rt_dataset, "10", "person")
        make_pair(rt_dataset, "2", "person")
        assert _item(rt_dataset, "2.jpg").target_stem == "person_1"
        assert _item(rt_dataset, "10.jpg").target_stem == "person_2"

    def test_labels_number_independently(self, rt_dataset):
        make_pair(rt_dataset, "a", "cat")
        make_pair(rt_dataset, "b", "dog")
        assert _item(rt_dataset, "a.jpg").target_stem == "cat_1"
        assert _item(rt_dataset, "b.jpg").target_stem == "dog_1"

    def test_background(self, rt_dataset):
        write_image(rt_dataset, "a.jpg")
        write_json(rt_dataset, "a.json", shapes=[])
        assert _item(rt_dataset, "a.jpg").target_stem == "background_1"

    def test_label_sanitized(self, rt_dataset):
        make_pair(rt_dataset, "a", "cat/dog")
        assert _item(rt_dataset, "a.jpg").target_stem == "cat_dog_1"


class TestCompliance:
    """Only <label>_<n> with the same label counts as compliant."""

    @pytest.mark.parametrize(
        "stem",
        ["person_0", "person_01", "person_1_extra", "person", "person_1a",
         "person_-1", "person_1.5"],
    )
    def test_not_compliant(self, rt_dataset, stem):
        make_pair(rt_dataset, stem, "person")
        item = _item(rt_dataset, stem + ".jpg")
        assert item.action == core.ACTION_RENAME
        assert item.target_stem == "person_1"

    def test_compliant_needs_matching_label(self, rt_dataset):
        make_pair(rt_dataset, "person_1", "dog")
        item = _item(rt_dataset, "person_1.jpg")
        assert item.action == core.ACTION_RENAME
        assert item.target_stem == "dog_1"

    def test_compliant_with_digits_in_label(self, rt_dataset):
        make_pair(rt_dataset, "car_2", "car")
        make_pair(rt_dataset, "x", "car")
        assert _item(rt_dataset, "car_2.jpg").action == core.ACTION_ALREADY
        assert _item(rt_dataset, "x.jpg").target_stem == "car_1"

    def test_compliant_large_number(self, rt_dataset):
        make_pair(rt_dataset, "person_12", "person")
        assert _item(
            rt_dataset, "person_12.jpg"
        ).action == core.ACTION_ALREADY

    def test_both_files_must_match(self, rt_dataset):
        make_pair(rt_dataset, "person_1", "person")
        plan = _plan(rt_dataset)
        assert plan.valid_items() == []
        assert len(plan.unchanged_items()) == 1


class TestAugChain:
    """An _aug item follows its parent and keeps its suffix."""

    def test_e6_chain(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        make_pair(rt_dataset, "a_aug1", "person")
        make_pair(rt_dataset, "a_aug2", "person")
        assert _summary(rt_dataset) == {
            "a": "person_1",
            "a_aug1": "person_1_aug1",
            "a_aug2": "person_1_aug2",
        }
        plan = _plan(rt_dataset)
        assert len(plan.valid_items()) == 3

    def test_e7_already_chain(self, rt_dataset):
        make_pair(rt_dataset, "person_1", "person")
        make_pair(rt_dataset, "person_1_aug1", "person")
        plan = _plan(rt_dataset)
        assert plan.valid_items() == []
        assert _item(
            rt_dataset, "person_1_aug1.jpg"
        ).target_stem == "person_1_aug1"

    def test_e8_bare_suffix(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        make_pair(rt_dataset, "a_aug", "person")
        assert _summary(rt_dataset) == {
            "a": "person_1",
            "a_aug": "person_1_aug",
        }

    def test_e8_second_run_is_already(self, rt_make):
        root = rt_make()
        make_pair(root, "person_1", "person")
        make_pair(root, "person_1_aug", "person")
        plan = _plan(root)
        assert plan.valid_items() == []
        assert plan.total_files() == 4

    def test_nested_chain(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        make_pair(rt_dataset, "a_aug1_aug2", "person")
        item = _item(rt_dataset, "a_aug1_aug2.jpg")
        assert item.pure_stem == "a"
        assert item.suffixes == ("_aug1", "_aug2")
        assert item.target_stem == "person_1_aug1_aug2"

    def test_aug_does_not_take_a_number(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        make_pair(rt_dataset, "a_aug1", "person")
        make_pair(rt_dataset, "b", "person")
        assert _item(rt_dataset, "b.jpg").target_stem == "person_2"

    def test_aug_different_label_follows_parent(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        make_pair(rt_dataset, "a_aug1", "dog")
        assert _item(rt_dataset, "a_aug1.jpg").target_stem == (
            "person_1_aug1"
        )


class TestOrphan:
    """An _aug item without a parent is mirrored under its name."""

    def test_e9_orphan(self, rt_dataset):
        make_pair(rt_dataset, "a_aug1", "person")
        item = _item(rt_dataset, "a_aug1.jpg")
        assert item.action == core.ACTION_ORPHAN
        assert item.target_stem == "a_aug1"
        assert item.target_image_name() == "a_aug1.jpg"

    def test_orphan_does_not_reserve(self, rt_dataset):
        make_pair(rt_dataset, "a_aug1", "person")
        make_pair(rt_dataset, "b", "person")
        assert _item(rt_dataset, "b.jpg").target_stem == "person_1"

    def test_orphan_keeps_name_in_entries(self, rt_dataset):
        make_pair(rt_dataset, "a_aug1", "person")
        plan = _plan(rt_dataset)
        assert ("a_aug1.jpg", "a_aug1.jpg") in plan.entries()
        assert ("a_aug1.json", "a_aug1.json") in plan.entries()

    def test_orphan_plan_is_not_blocked(self, rt_dataset):
        make_pair(rt_dataset, "a_aug1", "person")
        plan = _plan(rt_dataset)
        assert plan.blocked() is False
        assert plan.valid_items() == []


class TestPlanSurface:
    """The plan object exposes what the dialog and the writer need."""

    def test_idempotent_after_rename(self, rt_make):
        first = rt_make()
        make_pair(first, "a", "person")
        plan = _plan(first)
        assert [item.target_stem for item in plan.valid_items()] == (
            ["person_1"]
        )
        second = rt_make()
        make_pair(second, "person_1", "person")
        again = _plan(second)
        assert again.valid_items() == []
        assert len(again.unchanged_items()) == 1

    def test_mirrored_files_are_kept(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        write_image(rt_dataset, "classes.txt", b"person\ndog")
        write_image(rt_dataset, ".hidden", b"x")
        plan = _plan(rt_dataset)
        assert "classes.txt" in plan.mirrored
        assert ".hidden" in plan.mirrored
        assert ("classes.txt", "classes.txt") in plan.entries()

    def test_total_files_counts_every_entry(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        write_image(rt_dataset, "notes.txt", b"n")
        plan = _plan(rt_dataset)
        assert plan.total_files() == 3
        assert len(plan.entries()) == 3

    def test_mirrored_holds_every_unclaimed_file(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        write_image(rt_dataset, "a.txt", b"plain")
        write_image(rt_dataset, ".hidden", b"x")
        plan = _plan(rt_dataset)
        assert sorted(plan.mirrored) == [".hidden", "a.txt"]
        assert plan.total_files() == len(os.listdir(rt_dataset))

    def test_item_lookup(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        plan = _plan(rt_dataset)
        assert plan.item_by_filename("a.jpg") is plan.items[0]
        assert plan.item_by_filename("a.json") is plan.items[0]
        assert plan.item_by_filename("nope") is None

    def test_unmatched_json_stays_mirrored(self, rt_dataset):
        make_pair(rt_dataset, "a_aug1", "person")
        plan = _plan(rt_dataset)
        assert "a_aug1.json" not in plan.mirrored
        assert ("a_aug1.json", "a_aug1.json") in plan.entries()

    def test_scan_progress(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        make_pair(rt_dataset, "b", "person")
        seen = []
        core.plan_directory(
            rt_dataset, progress=lambda *args: seen.append(args)
        )
        assert seen[-1] == (2, 2, "Done")
        assert [entry[:2] for entry in seen[:-1]] == [(1, 2), (2, 2)]


class TestPlanStats:
    """stats() splits every top level file into one of its buckets."""

    def test_exact_counts(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        write_image(rt_dataset, "b.png")
        write_json(rt_dataset, "c.json", shapes=[])
        write_image(rt_dataset, "classes.txt", b"person\n")
        plan = _plan(rt_dataset)
        assert plan.blocked() is True
        assert plan.ignored == ["c.json"]
        assert plan.mirrored == ["classes.txt"]
        assert plan.stats() == {
            "total": 5,
            "images": 2,
            "json": 1,
            "ignored": 1,
            "other": 1,
        }

    def test_adds_up_to_the_listing(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        make_pair(rt_dataset, "a_aug1", "person")
        write_json(rt_dataset, "gone.json", shapes=[])
        write_image(rt_dataset, "blob.bin", b"\x00\x01")
        plan = _plan(rt_dataset)
        stats = plan.stats()
        assert stats["total"] == len(os.listdir(rt_dataset))
        assert (
            stats["images"]
            + stats["json"]
            + stats["ignored"]
            + stats["other"]
            == stats["total"]
        )
        assert stats["total"] - stats["ignored"] == plan.total_files()

    def test_ignored_json_with_kept_files(self, rt_dataset):
        """R7b: ignored json never cost a kept file its bucket."""

        make_pair(rt_dataset, "person_1", "person")
        make_pair(rt_dataset, "b", "person")
        write_json(rt_dataset, "c.json", shapes=[])
        plan = _plan(rt_dataset)
        assert plan.ignored == ["c.json"]
        assert sorted(
            item.image_name for item in plan.unchanged_items()
        ) == ["person_1.jpg"]
        stats = plan.stats()
        assert stats == {
            "total": 5,
            "images": 2,
            "json": 2,
            "ignored": 1,
            "other": 0,
        }
        assert (
            stats["images"]
            + stats["json"]
            + stats["ignored"]
            + stats["other"]
            == stats["total"]
        )

    def test_case_variant_json_without_image_stays_classified(
        self, rt_dataset
    ):
        """a.json + a.JSON without an image: no bucket loses a file."""

        write_json(rt_dataset, "a.json", shapes=[])
        write_json(rt_dataset, "a.JSON", shapes=[])
        write_image(rt_dataset, "blob.bin", b"\x00\x01")
        plan = _plan(rt_dataset)
        assert plan.blocked() is True
        assert "组同名冲突" in "".join(plan.blockers)
        stats = plan.stats()
        assert stats["total"] == 3
        assert (
            stats["images"]
            + stats["json"]
            + stats["ignored"]
            + stats["other"]
            == stats["total"]
        )

    def test_empty_folder_is_all_zero(self, rt_dataset):
        assert _plan(rt_dataset).stats() == {
            "total": 0,
            "images": 0,
            "json": 0,
            "ignored": 0,
            "other": 0,
        }

    def test_unreadable_folder_is_all_zero(self, rt_make):
        missing = os.path.join(rt_make(), "gone")
        plan = core.RenamePlan(directory=missing)
        assert plan.stats() == {
            "total": 0,
            "images": 0,
            "json": 0,
            "ignored": 0,
            "other": 0,
        }
