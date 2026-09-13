"""Tests of the blocking conditions that disable execution."""

import os

import pytest

from anylabeling.custom.rename_tool import rename_core as core

from conftest import make_pair, write_image, write_json


def _plan(root):
    """Scan root and resolve the targets when the plan is healthy."""

    plan = core.plan_directory(root)
    if not plan.blocked():
        core.resolve_targets(plan)
    return plan


def _blocked_text(root):
    """Return the blocker text of a folder, joined."""

    return " | ".join(_plan(root).blockers)


def _target(plan, name):
    """Return the target stem of the item owning name."""

    item = plan.item_by_filename(name)
    assert item is not None, name
    return item.target_stem


class TestB1MissingJson:
    """An image without its json blocks the whole folder."""

    def test_reports_every_image(self, rt_dataset):
        write_image(rt_dataset, "a.jpg")
        write_image(rt_dataset, "b.png")
        write_image(rt_dataset, "c.webp")
        plan = _plan(rt_dataset)
        assert plan.blocked() is True
        text = " | ".join(plan.blockers)
        assert "3 张图片缺少同名 json" in text
        for name in ("a.jpg", "b.png", "c.webp"):
            assert name in text

    def test_no_targets_are_produced(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        write_image(rt_dataset, "b.png")
        plan = _plan(rt_dataset)
        assert plan.valid_items() == []
        assert [item.target_stem for item in plan.items] == ["", ""]

    def test_healthy_sibling_is_reported_too(self, rt_dataset):
        make_pair(rt_dataset, "ok", "person")
        write_image(rt_dataset, "bad.jpg")
        plan = _plan(rt_dataset)
        assert plan.total_files() == 3
        assert len(plan.items) == 2
        assert ("ok.json", "ok.json") in plan.entries()
        assert all(source != "bad.json" for source, _ in plan.entries())


class TestB2OrphanJson:
    """A json without an image blocks the whole folder."""

    def test_reports_the_json(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        write_json(rt_dataset, "c.json", shapes=[])
        plan = _plan(rt_dataset)
        assert plan.blocked() is True
        text = " | ".join(plan.blockers)
        assert "1 个 json 没有同名图片" in text
        assert "c.json" in text

    def test_orphan_json_is_mirrored_for_the_preview(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        write_json(rt_dataset, "c.json", shapes=[])
        plan = _plan(rt_dataset)
        assert "c.json" in plan.mirrored
        assert ("c.json", "c.json") in plan.entries()


class TestB3BrokenJson:
    """An unparsable json blocks the whole folder."""

    def _break(self, root, stem):
        with open(os.path.join(root, stem + ".json"), "w",
                  encoding="utf-8") as handle:
            handle.write("{not json")

    def test_reports_every_json(self, rt_dataset):
        make_pair(rt_dataset, "d", "person")
        make_pair(rt_dataset, "e", "person")
        self._break(rt_dataset, "d")
        self._break(rt_dataset, "e")
        plan = _plan(rt_dataset)
        assert plan.blocked() is True
        text = " | ".join(plan.blockers)
        assert "2 个 json 无法解析" in text
        assert "d.json" in text and "e.json" in text

    def test_top_level_list_counts_too(self, rt_dataset):
        make_pair(rt_dataset, "d", "person")
        with open(os.path.join(rt_dataset, "d.json"), "w",
                  encoding="utf-8") as handle:
            handle.write("[1, 2]")
        text = _blocked_text(rt_dataset)
        assert "1 个 json 无法解析" in text

    def test_error_is_kept_on_the_item(self, rt_dataset):
        make_pair(rt_dataset, "d", "person")
        self._break(rt_dataset, "d")
        plan = _plan(rt_dataset)
        assert "解析失败" in plan.items[0].error


class TestB4Subdirectory:
    """Only top level files are handled."""

    def test_reports_every_directory(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        os.makedirs(os.path.join(rt_dataset, "sub"))
        os.makedirs(os.path.join(rt_dataset, "imgs2"))
        plan = _plan(rt_dataset)
        assert plan.blocked() is True
        text = " | ".join(plan.blockers)
        assert "2 个子目录不支持" in text
        assert "sub" in text and "imgs2" in text

    def test_subdirectory_files_are_ignored(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        os.makedirs(os.path.join(rt_dataset, "sub"))
        write_image(rt_dataset, os.path.join("sub", "x.jpg"))
        plan = _plan(rt_dataset)
        assert plan.total_files() == 2


class TestB5StemCollision:
    """One stem carrying two files blocks the whole folder."""

    def test_two_images(self, rt_dataset):
        make_pair(rt_dataset, "a", "person", ext=".jpg")
        make_pair(rt_dataset, "a", "person", ext=".png")
        plan = _plan(rt_dataset)
        assert plan.blocked() is True
        text = " | ".join(plan.blockers)
        assert "1 组同名冲突" in text
        assert "a.jpg" in text and "a.png" in text

    def test_two_json(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        write_json(rt_dataset, "a.JSON", shapes=[])
        text = " | ".join(core.plan_directory(rt_dataset).blockers)
        assert "同名冲突" in text

    def test_healthy_folder_has_no_conflict(self, rt_dataset):
        make_pair(rt_dataset, "a", "person", ext=".jpg")
        make_pair(rt_dataset, "b", "person", ext=".png")
        assert _plan(rt_dataset).blocked() is False


class TestCombined:
    """Several problems are all reported at once."""

    def test_all_kinds_together(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        write_image(rt_dataset, "b.png")
        write_json(rt_dataset, "c.json", shapes=[])
        make_pair(rt_dataset, "d", "person")
        with open(os.path.join(rt_dataset, "d.json"), "w",
                  encoding="utf-8") as handle:
            handle.write("{oops")
        os.makedirs(os.path.join(rt_dataset, "sub"))
        plan = _plan(rt_dataset)
        text = " | ".join(plan.blockers)
        assert len(plan.blockers) == 4
        assert "缺少同名 json" in text
        assert "没有同名图片" in text
        assert "无法解析" in text
        assert "子目录" in text

    def test_blockers_are_chinese_sentences(self, rt_dataset):
        write_image(rt_dataset, "a.jpg")
        plan = _plan(rt_dataset)
        assert plan.blockers and plan.blockers[0].endswith("a.jpg")


class TestCollisionWithKeptName:
    """R8 entry name check: a target may not shadow a kept name.

    plan.mirrored alone cannot trigger it in a resolvable folder (see
    FEATURES.md): an image shaped name only lands in mirrored when its
    stem is already taken by another image, which is the B5 blocker,
    and a .json name only when it is an orphan or a B5 duplicate. The
    already / orphan names of plan.items can collide though, and
    test_target_collides_with_orphan_name_in_a_real_folder pins that
    with a plain three pair folder.
    """

    def test_target_collides_with_mirrored_name(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        plan = core.plan_directory(rt_dataset)
        plan.mirrored.append("person_1.jpg")
        core.resolve_targets(plan)
        assert plan.blocked() is True
        text = " | ".join(plan.blockers)
        assert "person_1.jpg" in text and "同名" in text

    def test_target_collides_with_orphan_name(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        plan = core.plan_directory(rt_dataset)
        plan.mirrored.append("person_1.json")
        core.resolve_targets(plan)
        assert plan.blocked() is True

    def test_target_collides_with_orphan_name_in_a_real_folder(
        self, rt_dataset
    ):
        make_pair(rt_dataset, "b", "person")
        make_pair(rt_dataset, "b_aug1", "person")
        make_pair(rt_dataset, "person_1_aug1", "person")
        plan = _plan(rt_dataset)
        assert plan.blocked() is True
        assert _target(plan, "b.jpg") == "person_1"
        assert _target(plan, "person_1_aug1.jpg") == "person_1_aug1"
        text = " | ".join(plan.blockers)
        assert "person_1_aug1.jpg" in text
        assert "同名" in text

    def test_folder_variant_of_e10(self, rt_dataset):
        make_pair(rt_dataset, "a", "a", ext=".png")
        make_pair(rt_dataset, "a_aug", "a", ext=".png")
        plan = _plan(rt_dataset)
        assert plan.blocked() is False
        assert _target(plan, "a.png") == "a_1"
        assert _target(plan, "a_aug.png") == "a_1_aug"

    def test_entry_names_are_unique(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        make_pair(rt_dataset, "a_aug1", "person")
        make_pair(rt_dataset, "person_1", "person")
        plan = _plan(rt_dataset)
        names = [entry for _source, entry in plan.entries()]
        assert len(names) == len(set(names))
        assert core.check_entry_names(plan) == []


class TestEntryNameBlocking:
    """check_entry_names blocks bad names and an incomplete plan."""

    def test_illegal_entry_name_is_reported(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        write_image(rt_dataset, "v1..2.txt", b"x")
        plan = _plan(rt_dataset)
        problems = core.check_entry_names(plan)
        assert any("条目名非法" in item for item in problems)
        assert any("v1..2.txt" in item for item in problems)

    def test_a_dropped_mirror_is_reported(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        write_image(rt_dataset, "a.txt", b"text")
        plan = _plan(rt_dataset)
        plan.mirrored = []
        problems = core.check_entry_names(plan)
        assert any("没有被计划镜像" in item for item in problems)
        assert any("a.txt" in item for item in problems)

    def test_a_duplicated_source_is_reported(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        plan = _plan(rt_dataset)
        plan.mirrored.append("a.jpg")
        problems = core.check_entry_names(plan)
        assert "源文件在计划里出现 2 次：a.jpg" in problems

    def test_a_ghost_entry_is_reported(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        plan = _plan(rt_dataset)
        plan.items[0].image_name = "ghost.jpg"
        problems = core.check_entry_names(plan)
        assert "计划里的文件不在源目录：ghost.jpg" in problems

    def test_a_clean_plan_has_no_problem(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        write_image(rt_dataset, "a.txt", b"text")
        assert core.check_entry_names(_plan(rt_dataset)) == []


class TestNonDecimalDigits:
    """A superscript digit must not raise anywhere in the scan.

    U+00B2 satisfies str.isdigit() but int() rejects it, so the old
    natural_key raised ValueError on a name like a².jpg; the same
    scan also feeds check_entry_names. Both run inside a Qt slot, and
    an uncaught exception there makes PyQt abort the application.
    """

    def test_folder_with_superscript_plans_and_checks(self, rt_dataset):
        make_pair(rt_dataset, "a²", "person")
        make_pair(rt_dataset, "b", "person")
        plan = _plan(rt_dataset)
        assert plan.blocked() is False
        assert [item.stem for item in plan.items] == ["a²", "b"]
        assert [item.target_stem for item in plan.items] == [
            "person_1", "person_2"
        ]
        assert core.check_entry_names(plan) == []

    def test_mirrored_superscript_is_checked(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        write_image(rt_dataset, "a².txt", b"text")
        plan = _plan(rt_dataset)
        assert plan.mirrored == ["a².txt"]
        assert core.check_entry_names(plan) == []


class TestHealthyControl:
    """A folder that only looks suspicious is not blocked."""

    def test_compliant_sibling_takes_the_number(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        make_pair(rt_dataset, "person_1", "person")
        plan = _plan(rt_dataset)
        assert plan.blocked() is False
        assert _target(plan, "a.jpg") == "person_2"

    def test_non_compliant_sibling_is_renamed(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        make_pair(rt_dataset, "person_01", "person")
        plan = _plan(rt_dataset)
        assert plan.blocked() is False
        assert _target(plan, "a.jpg") == "person_1"
        assert _target(plan, "person_01.jpg") == "person_2"

    def test_orphan_aug_is_not_a_blocker(self, rt_dataset):
        make_pair(rt_dataset, "a_aug1", "person")
        plan = _plan(rt_dataset)
        assert plan.blocked() is False

    def test_missing_directory_raises(self, rt_make):
        path = rt_make()
        with pytest.raises(core.RenameError):
            core.plan_directory(os.path.join(path, "nope"))
