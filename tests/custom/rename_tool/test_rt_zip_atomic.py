"""Tests of the atomic archive write: .part, collisions, failures."""

import os

import pytest

from anylabeling.custom.rename_tool import rename_core as core

from conftest import make_pair, snapshot, write_image, write_json


def _plan(root):
    """Return a resolved plan of a healthy folder."""

    return core.resolve_targets(core.plan_directory(root))


def _corrupt_json(root, stem):
    """Replace a label file with bytes that cannot be parsed."""

    with open(os.path.join(root, stem + ".json"), "w",
              encoding="utf-8") as handle:
        handle.write("{not json")


class TestAtomicWrite:
    """The archive shows up under its final name, or not at all."""

    def test_no_part_file_after_success(self, rt_dataset, rt_out):
        make_pair(rt_dataset, "a", "person")
        path = os.path.join(rt_out, "result.zip")
        core.write_zip(_plan(rt_dataset), path)
        assert os.path.isfile(path)
        assert not os.path.exists(path + core.PART_SUFFIX)

    def test_part_name_is_the_final_name_plus_suffix(self, rt_dataset, rt_out):
        make_pair(rt_dataset, "a", "person")
        path = os.path.join(rt_out, "result.zip")
        core.write_zip(_plan(rt_dataset), path)
        assert sorted(os.listdir(rt_out)) == ["result.zip"]

    def test_existing_archive_is_not_overwritten(self, rt_dataset, rt_out):
        make_pair(rt_dataset, "a", "person")
        first = os.path.join(rt_out, "result.zip")
        with open(first, "wb") as handle:
            handle.write(b"OLD")
        path = core.resolve_output_path(rt_out, "result.zip")
        assert os.path.basename(path) == "result_2.zip"
        core.write_zip(_plan(rt_dataset), path)
        assert os.path.isfile(os.path.join(rt_out, "result_2.zip"))
        with open(first, "rb") as handle:
            assert handle.read() == b"OLD"

    def test_third_name(self, rt_dataset, rt_out):
        for name in ("result.zip", "result_2.zip"):
            open(os.path.join(rt_out, name), "wb").close()
        path = core.resolve_output_path(rt_out, "result.zip")
        assert os.path.basename(path) == "result_3.zip"

    def test_leftover_part_is_refused(self, rt_dataset, rt_out):
        make_pair(rt_dataset, "a", "person")
        path = os.path.join(rt_out, "result.zip")
        part = path + core.PART_SUFFIX
        with open(part, "wb") as handle:
            handle.write(b"LEFT")
        with pytest.raises(core.RenameError) as info:
            core.write_zip(_plan(rt_dataset), path)
        assert "拒绝覆盖" in str(info.value)
        with open(part, "rb") as handle:
            assert handle.read() == b"LEFT"
        assert not os.path.exists(path)


class TestFailure:
    """A failure keeps the half finished archive and says where it is."""

    def test_part_kept_and_final_missing(self, rt_dataset, rt_out):
        make_pair(rt_dataset, "a", "person")
        plan = _plan(rt_dataset)
        _corrupt_json(rt_dataset, "a")
        path = os.path.join(rt_out, "result.zip")
        with pytest.raises(core.RenameError) as info:
            core.write_zip(plan, path)
        part = path + core.PART_SUFFIX
        assert part in str(info.value)
        assert os.path.isfile(part)
        assert not os.path.exists(path)

    def test_failure_does_not_touch_the_source(self, rt_dataset, rt_out):
        make_pair(rt_dataset, "a", "person")
        make_pair(rt_dataset, "b", "person")
        plan = _plan(rt_dataset)
        before = snapshot(rt_dataset)
        _corrupt_json(rt_dataset, "b")
        damaged = snapshot(rt_dataset)
        with pytest.raises(core.RenameError):
            core.write_zip(plan, os.path.join(rt_out, "result.zip"))
        assert snapshot(rt_dataset) == damaged
        assert before["a.jpg"] == damaged["a.jpg"]

    def test_message_carries_the_reason(self, rt_dataset, rt_out):
        make_pair(rt_dataset, "a", "person")
        plan = _plan(rt_dataset)
        _corrupt_json(rt_dataset, "a")
        with pytest.raises(core.RenameError) as info:
            core.write_zip(plan, os.path.join(rt_out, "result.zip"))
        assert "解析失败" in str(info.value)

    def test_retry_after_fixing(self, rt_dataset, rt_out):
        make_pair(rt_dataset, "a", "person")
        plan = _plan(rt_dataset)
        _corrupt_json(rt_dataset, "a")
        path = os.path.join(rt_out, "result.zip")
        with pytest.raises(core.RenameError):
            core.write_zip(plan, path)
        write_json(
            rt_dataset,
            "a.json",
            shapes=[{"label": "person"}],
            image_path="a.jpg",
        )
        with pytest.raises(core.RenameError) as info:
            core.write_zip(plan, path)
        assert "拒绝覆盖" in str(info.value)


class TestOutputLocation:
    """The archive never lands inside the folder it mirrors."""

    def test_inside_source(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        path = os.path.join(rt_dataset, "result.zip")
        with pytest.raises(core.RenameError) as info:
            core.write_zip(_plan(rt_dataset), path)
        assert "源目录" in str(info.value)
        assert not os.path.exists(path)

    def test_inside_source_subpath(self, rt_dataset):
        make_pair(rt_dataset, "a", "person")
        nested = os.path.join(rt_dataset, "out", "result.zip")
        with pytest.raises(core.RenameError) as info:
            core.write_zip(_plan(rt_dataset), nested)
        assert "源目录" in str(info.value)
        assert not os.path.exists(nested)

    def test_create_missing_output_directory(self, rt_dataset, rt_out):
        make_pair(rt_dataset, "a", "person")
        nested = os.path.join(rt_out, "sub", "result.zip")
        core.write_zip(_plan(rt_dataset), nested)
        assert os.path.isfile(nested)

    def test_sibling_directory_is_allowed(self, rt_make):
        source = rt_make()
        out = rt_make()
        make_pair(source, "a", "person")
        path = os.path.join(out, "result.zip")
        core.write_zip(_plan(source), path)
        assert os.path.isfile(path)

    def test_blocked_plan_is_refused(self, rt_dataset, rt_out):
        write_image(rt_dataset, "a.jpg")
        plan = _plan(rt_dataset)
        assert plan.blocked() is True
        assert plan.valid_items() == []
        path = os.path.join(rt_out, "result.zip")
        with pytest.raises(core.RenameError) as info:
            core.write_zip(plan, path)
        assert "阻塞" in str(info.value)
        assert not os.path.exists(path)
        assert os.listdir(rt_out) == []

    def test_existing_final_name_is_refused(self, rt_dataset, rt_out):
        make_pair(rt_dataset, "a", "person")
        path = os.path.join(rt_out, "result.zip")
        with open(path, "wb") as handle:
            handle.write(b"OLD")
        with pytest.raises(core.RenameError) as info:
            core.write_zip(_plan(rt_dataset), path)
        assert "拒绝覆盖" in str(info.value)
        assert sorted(os.listdir(rt_out)) == ["result.zip"]
        with open(path, "rb") as handle:
            assert handle.read() == b"OLD"
