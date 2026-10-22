"""Tests of the in place rename: moves, json rewrite, rollback."""

import hashlib
import os

import pytest

from anylabeling.custom.rename_tool import rename_core as core

from conftest import (
    make_pair,
    read_json_file,
    snapshot,
    write_image,
    write_json,
)


def _plan(root):
    """Return the resolved plan of a healthy folder."""

    return core.resolve_targets(core.plan_directory(root))


def _listing(root):
    """Return the sorted top level listing of a folder."""

    return sorted(os.listdir(root))


def _staging(root):
    """Return the staging files a failed or finished run left behind."""

    return [
        name for name in os.listdir(root)
        if name.startswith(core.STAGING_PREFIX)
    ]


def _content(root):
    """Return {name: (size, sha256)} of every top level file.

    The rollback rewrites a json from its saved bytes, so its mtime
    moves even when its content is restored exactly; that is why the
    rollback assertions compare content, not the full snapshot.
    """

    result = {}
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as handle:
            payload = handle.read()
        result[name] = (
            len(payload),
            hashlib.sha256(payload).hexdigest(),
        )
    return result


def _fail_on_call(monkeypatch, number):
    """Make the number-th file move of a run raise an OSError."""

    original = core._rename_file
    calls = []

    def _boom(source, target):
        calls.append((source, target))
        if len(calls) == number:
            raise OSError("boom")
        return original(source, target)

    monkeypatch.setattr(core, "_rename_file", _boom)
    return calls


class TestBasicRename:
    """A renamed pair takes its new name inside the same folder."""

    def test_pair_is_renamed(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        result = core.apply_renames(_plan(rt_dataset))
        assert _listing(rt_dataset) == [
            "person_1.jpg",
            "person_1.json",
        ]
        assert result["renamed"] == 2
        assert result["moves"] == [
            ("a.jpg", "person_1.jpg"),
            ("a.json", "person_1.json"),
        ]

    def test_image_bytes_are_preserved(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        core.apply_renames(_plan(rt_dataset))
        with open(os.path.join(rt_dataset, "person_1.jpg"), "rb") as fh:
            assert fh.read() == b"image:a.jpg"

    def test_image_path_is_rewritten(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        core.apply_renames(_plan(rt_dataset))
        doc = read_json_file(rt_dataset, "person_1.json")
        assert doc["imagePath"] == "person_1.jpg"
        assert doc["version"] == "1.0"
        assert doc["imageData"] is None
        assert [shape["label"] for shape in doc["shapes"]] == ["person"]

    def test_directory_prefix_is_kept(self, rt_dataset):
        write_image(rt_dataset, "a.jpg")
        write_json(
            rt_dataset,
            "a.json",
            shapes=[{"label": "person"}],
            image_path="images/a.jpg",
        )
        core.apply_renames(_plan(rt_dataset))
        doc = read_json_file(rt_dataset, "person_1.json")
        assert doc["imagePath"] == "images/person_1.jpg"

    def test_two_pairs_follow_natural_order(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        make_pair(rt_dataset, "b", "person")
        core.apply_renames(_plan(rt_dataset))
        assert _listing(rt_dataset) == [
            "person_1.jpg",
            "person_1.json",
            "person_2.jpg",
            "person_2.json",
        ]
        with open(os.path.join(rt_dataset, "person_1.jpg"), "rb") as fh:
            assert fh.read() == b"image:a.jpg"
        with open(os.path.join(rt_dataset, "person_2.jpg"), "rb") as fh:
            assert fh.read() == b"image:b.jpg"
        assert read_json_file(
            rt_dataset, "person_2.json"
        )["imagePath"] == "person_2.jpg"

    def test_chinese_label(self, rt_dataset):
        make_pair(rt_dataset, "a", "猫")
        core.apply_renames(_plan(rt_dataset))
        assert _listing(rt_dataset) == ["猫_1.jpg", "猫_1.json"]
        doc = read_json_file(rt_dataset, "猫_1.json")
        assert doc["imagePath"] == "猫_1.jpg"

    def test_no_staging_file_is_left(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        core.apply_renames(_plan(rt_dataset))
        assert _staging(rt_dataset) == []


class TestKeptFiles:
    """Compliant, ignored and mirrored files keep name and bytes."""

    def test_compliant_item_is_kept(self, rt_dataset):
        make_pair(rt_dataset, "person_1", "person")
        before = snapshot(rt_dataset)
        make_pair(rt_dataset, "a", "person")
        result = core.apply_renames(_plan(rt_dataset))
        assert snapshot(rt_dataset)["person_1.jpg"] == before[
            "person_1.jpg"
        ]
        assert snapshot(rt_dataset)["person_1.json"] == before[
            "person_1.json"
        ]
        assert _listing(rt_dataset) == [
            "person_1.jpg",
            "person_1.json",
            "person_2.jpg",
            "person_2.json",
        ]
        assert result["kept"] == 2

    def test_ignored_json_is_untouched(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        write_json(rt_dataset, "c.json", shapes=[])
        before = snapshot(rt_dataset)["c.json"]
        result = core.apply_renames(_plan(rt_dataset))
        assert snapshot(rt_dataset)["c.json"] == before
        assert result["ignored"] == 1
        assert "c.json" in os.listdir(rt_dataset)

    def test_mirrored_file_is_untouched(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        write_image(rt_dataset, "classes.txt", b"person\n")
        before = snapshot(rt_dataset)["classes.txt"]
        result = core.apply_renames(_plan(rt_dataset))
        assert snapshot(rt_dataset)["classes.txt"] == before
        assert result["mirrored"] == 1

    def test_noop_plan_changes_nothing(self, rt_dataset):
        make_pair(rt_dataset, "person_1", "person")
        before = snapshot(rt_dataset)
        result = core.apply_renames(_plan(rt_dataset))
        assert result["renamed"] == 0
        assert result["moves"] == []
        assert snapshot(rt_dataset) == before


class TestCollision:
    """A target may equal the source name of another item."""

    def test_target_equals_another_source(self, rt_dataset):
        write_image(rt_dataset, "person_1.jpg")
        write_json(
            rt_dataset, "person_1.json",
            shapes=[{"label": "dog"}], image_path="person_1.jpg",
        )
        make_pair(rt_dataset, "a", "person")
        plan = _plan(rt_dataset)
        targets = {
            item.stem: item.target_stem for item in plan.items
        }
        assert targets == {"person_1": "dog_1", "a": "person_1"}
        core.apply_renames(plan)
        assert _listing(rt_dataset) == [
            "dog_1.jpg",
            "dog_1.json",
            "person_1.jpg",
            "person_1.json",
        ]
        with open(os.path.join(rt_dataset, "dog_1.jpg"), "rb") as fh:
            assert fh.read() == b"image:person_1.jpg"
        with open(os.path.join(rt_dataset, "person_1.jpg"), "rb") as fh:
            assert fh.read() == b"image:a.jpg"
        assert read_json_file(
            rt_dataset, "dog_1.json"
        )["imagePath"] == "dog_1.jpg"
        assert read_json_file(
            rt_dataset, "person_1.json"
        )["imagePath"] == "person_1.jpg"

    def test_swap_cycle(self, rt_dataset):
        write_image(rt_dataset, "person_1.jpg")
        write_json(
            rt_dataset, "person_1.json",
            shapes=[{"label": "dog"}], image_path="person_1.jpg",
        )
        write_image(rt_dataset, "dog_1.jpg")
        write_json(
            rt_dataset, "dog_1.json",
            shapes=[{"label": "person"}], image_path="dog_1.jpg",
        )
        plan = _plan(rt_dataset)
        result = core.apply_renames(plan)
        assert result["renamed"] == 4
        assert _listing(rt_dataset) == [
            "dog_1.jpg",
            "dog_1.json",
            "person_1.jpg",
            "person_1.json",
        ]
        with open(os.path.join(rt_dataset, "dog_1.jpg"), "rb") as fh:
            assert fh.read() == b"image:person_1.jpg"
        with open(os.path.join(rt_dataset, "person_1.jpg"), "rb") as fh:
            assert fh.read() == b"image:dog_1.jpg"
        assert read_json_file(
            rt_dataset, "dog_1.json"
        )["imagePath"] == "dog_1.jpg"
        assert read_json_file(
            rt_dataset, "person_1.json"
        )["imagePath"] == "person_1.jpg"
        assert _staging(rt_dataset) == []


class TestGuards:
    """Blocked plans and folders that changed after the scan are refused."""

    def test_blocked_plan_is_refused(self, rt_dataset):
        write_image(rt_dataset, "a.jpg")
        plan = _plan(rt_dataset)
        assert plan.blocked() is True
        before = snapshot(rt_dataset)
        with pytest.raises(core.RenameError) as info:
            core.apply_renames(plan)
        assert "阻塞" in str(info.value)
        assert snapshot(rt_dataset) == before

    def test_folder_changed_after_the_scan(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        plan = _plan(rt_dataset)
        before = snapshot(rt_dataset)
        write_image(rt_dataset, "extra.txt", b"x")
        with pytest.raises(core.RenameError) as info:
            core.apply_renames(plan)
        assert "没有被计划镜像" in str(info.value)
        assert snapshot(rt_dataset)["a.jpg"] == before["a.jpg"]
        assert snapshot(rt_dataset)["a.json"] == before["a.json"]


class TestProgress:
    """apply_renames reports every moved file once, then the close call."""

    def test_one_callback_per_file(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        make_pair(rt_dataset, "b", "person")
        seen = []
        core.apply_renames(
            _plan(rt_dataset),
            progress=lambda *args: seen.append(args),
        )
        assert len(seen) == 5
        assert seen[-1] == (4, 4, "Done", "")
        done = [entry[0] for entry in seen[:-1]]
        assert done == [1, 2, 3, 4]
        assert all(entry[1] == 4 for entry in seen[:-1])
        assert seen[0] == (1, 4, "a.jpg", "person_1.jpg")
        assert seen[1] == (2, 4, "a.json", "person_1.json")

    def test_noop_plan_reports_nothing(self, rt_dataset):
        make_pair(rt_dataset, "person_1", "person")
        seen = []
        core.apply_renames(
            _plan(rt_dataset),
            progress=lambda *args: seen.append(args),
        )
        assert seen == []


class TestRollback:
    """A failure undoes every move made so far."""

    def test_failure_during_staging_restores(self, rt_dataset, monkeypatch):
        make_pair(rt_dataset, "a", "person")
        make_pair(rt_dataset, "b", "person")
        before = snapshot(rt_dataset)
        _fail_on_call(monkeypatch, 3)
        with pytest.raises(core.RenameError) as info:
            core.apply_renames(_plan(rt_dataset))
        assert "boom" in str(info.value)
        assert "已回滚" in str(info.value)
        assert snapshot(rt_dataset) == before
        assert _staging(rt_dataset) == []

    def test_failure_after_json_rewrite_restores(
        self, rt_dataset, monkeypatch
    ):
        make_pair(rt_dataset, "a", "person")
        make_pair(rt_dataset, "b", "person")
        before = _content(rt_dataset)
        _fail_on_call(monkeypatch, 6)
        with pytest.raises(core.RenameError) as info:
            core.apply_renames(_plan(rt_dataset))
        assert "已回滚" in str(info.value)
        assert _content(rt_dataset) == before
        assert _staging(rt_dataset) == []

    def test_failure_names_the_reason(self, rt_dataset, monkeypatch):
        make_pair(rt_dataset, "a", "person")
        _fail_on_call(monkeypatch, 1)
        with pytest.raises(core.RenameError) as info:
            core.apply_renames(_plan(rt_dataset))
        assert "OSError" in str(info.value)


class TestCounters:
    """The result dictionary counts the files of the folder."""

    def test_counters(self, rt_dataset):
        make_pair(rt_dataset, "person_1", "person")
        make_pair(rt_dataset, "a", "person")
        write_image(rt_dataset, "classes.txt", b"x")
        write_json(rt_dataset, "c.json", shapes=[])
        result = core.apply_renames(_plan(rt_dataset))
        assert result["renamed"] == 2
        assert result["renamed_items"] == 1
        assert result["kept"] == 2
        assert result["mirrored"] == 1
        assert result["ignored"] == 1
        assert result["directory"] == rt_dataset
