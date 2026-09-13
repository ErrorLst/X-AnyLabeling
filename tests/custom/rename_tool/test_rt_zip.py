"""Tests of the archive writer: mirror, names, bytes and progress."""

import json
import os
import zipfile

import pytest

from anylabeling.custom.rename_tool import rename_core as core

from conftest import (
    make_pair,
    read_zip,
    snapshot,
    write_image,
    write_json,
    zip_doc,
)


def _run(root, out, name="result.zip"):
    """Plan, write the archive and return (plan, path, summary)."""

    plan = core.resolve_targets(core.plan_directory(root))
    path = os.path.join(out, name)
    summary = core.write_zip(plan, path)
    return plan, path, summary


def _info(path):
    """Return {entry name: ZipInfo} of an archive."""

    with zipfile.ZipFile(path) as archive:
        return {info.filename: info for info in archive.infolist()}


class TestSourceUntouched:
    """The source folder keeps its bytes, its mtimes and its listing."""

    def _dataset(self, root):
        make_pair(root, "a", "person")
        make_pair(root, "a_aug1", "person")
        write_image(root, "classes.txt", b"person\ndog\n")
        write_image(root, "说明.txt", "非 ASCII 文本".encode("utf-8"))
        write_image(root, "blob.bin", bytes(range(256)))

    def test_bytes_and_mtimes(self, rt_dataset, rt_out):
        self._dataset(rt_dataset)
        before = snapshot(rt_dataset)
        _run(rt_dataset, rt_out)
        assert snapshot(rt_dataset) == before

    def test_listing_unchanged(self, rt_dataset, rt_out):
        self._dataset(rt_dataset)
        before = sorted(os.listdir(rt_dataset))
        _run(rt_dataset, rt_out)
        assert sorted(os.listdir(rt_dataset)) == before
        assert not [n for n in os.listdir(rt_dataset) if ".part" in n]

    def test_reading_the_folder_writes_nothing(self, rt_dataset):
        self._dataset(rt_dataset)
        before = snapshot(rt_dataset)
        plan = core.plan_directory(rt_dataset)
        core.resolve_targets(plan)
        assert snapshot(rt_dataset) == before

    def test_summary_flags_the_source(self, rt_dataset, rt_out):
        self._dataset(rt_dataset)
        _plan, _path, summary = _run(rt_dataset, rt_out)
        assert summary["source_untouched"] is True
        assert summary["blockers"] == []
        assert summary["files"] == 7
        assert summary["renamed"] == 4


class TestEntries:
    """Entry names are exactly the computed target names."""

    def test_names_match_the_plan(self, rt_dataset, rt_out):
        make_pair(rt_dataset, "a", "person")
        make_pair(rt_dataset, "a_aug1", "person")
        write_image(rt_dataset, "notes.txt", b"n")
        plan, path, _summary = _run(rt_dataset, rt_out)
        assert sorted(read_zip(path)) == sorted(
            entry for _source, entry in plan.entries()
        )

    def test_target_names(self, rt_dataset, rt_out):
        make_pair(rt_dataset, "a", "person")
        _plan, path, _summary = _run(rt_dataset, rt_out)
        assert sorted(read_zip(path)) == [
            "person_1.jpg",
            "person_1.json",
        ]

    def test_no_directory_entries(self, rt_dataset, rt_out):
        make_pair(rt_dataset, "a", "person")
        _plan, path, _summary = _run(rt_dataset, rt_out)
        for name in read_zip(path):
            assert name
            assert not name.endswith("/")
            assert "/" not in name and chr(92) not in name
            assert ".." not in name
            assert not os.path.isabs(name)

    def test_entry_count(self, rt_dataset, rt_out):
        make_pair(rt_dataset, "a", "person")
        make_pair(rt_dataset, "b", "person")
        write_image(rt_dataset, "classes.txt", b"person\n")
        plan, path, summary = _run(rt_dataset, rt_out)
        assert len(read_zip(path)) == plan.total_files()
        assert summary["files"] == plan.total_files()

    def test_already_items_keep_their_bytes(self, rt_dataset, rt_out):
        make_pair(rt_dataset, "person_1", "person")
        make_pair(rt_dataset, "a", "person")
        payload = os.path.join(rt_dataset, "person_1.jpg")
        with open(payload, "rb") as handle:
            raw = handle.read()
        _plan, path, _summary = _run(rt_dataset, rt_out)
        assert read_zip(path)["person_1.jpg"] == raw

    def test_binary_and_non_ascii_names(self, rt_dataset, rt_out):
        make_pair(rt_dataset, "a", "person")
        blob = bytes(range(256))
        write_image(rt_dataset, "blob.bin", blob)
        write_image(rt_dataset, "说明.txt", "文本".encode("utf-8"))
        _plan, path, _summary = _run(rt_dataset, rt_out)
        entries = read_zip(path)
        assert entries["blob.bin"] == blob
        assert entries["说明.txt"] == "文本".encode("utf-8")

    def test_orphan_aug_is_mirrored(self, rt_dataset, rt_out):
        make_pair(rt_dataset, "a_aug1", "person")
        _plan, path, _summary = _run(rt_dataset, rt_out)
        assert sorted(read_zip(path)) == [
            "a_aug1.jpg",
            "a_aug1.json",
        ]


class TestFullMirror:
    """Every top level file lands in the archive exactly once (R9)."""

    def test_unclaimed_files_are_mirrored_verbatim(self, rt_dataset, rt_out):
        make_pair(rt_dataset, "a", "person")
        write_image(rt_dataset, "a.txt", b"companion text")
        write_image(rt_dataset, ".hidden", b"\x00\xffbinary")
        plan = core.plan_directory(rt_dataset)
        assert sorted(plan.mirrored) == [".hidden", "a.txt"]
        assert plan.total_files() == len(os.listdir(rt_dataset))
        _plan, path, _summary = _run(rt_dataset, rt_out)
        entries = read_zip(path)
        assert set(entries) == {
            "person_1.jpg",
            "person_1.json",
            "a.txt",
            ".hidden",
        }
        assert entries["a.txt"] == b"companion text"
        assert entries[".hidden"] == b"\x00\xffbinary"

    def test_png_pair_plus_text(self, rt_dataset, rt_out):
        make_pair(rt_dataset, "a", "person", ext=".png")
        write_image(rt_dataset, "a.txt", b"text")
        plan = core.plan_directory(rt_dataset)
        assert plan.mirrored == ["a.txt"]
        assert plan.total_files() == len(os.listdir(rt_dataset))
        _plan, path, _summary = _run(rt_dataset, rt_out)
        assert set(read_zip(path)) == {
            "person_1.png",
            "person_1.json",
            "a.txt",
        }

    def test_entry_set_matches_the_listing(self, rt_dataset, rt_out):
        make_pair(rt_dataset, "a", "person")
        make_pair(rt_dataset, "a_aug1", "person")
        write_image(rt_dataset, "classes.txt", b"person\n")
        plan = core.resolve_targets(core.plan_directory(rt_dataset))
        _plan, path, _summary = _run(rt_dataset, rt_out)
        mapped = {entry for _source, entry in plan.entries()}
        assert set(read_zip(path)) == mapped
        assert len(mapped) == len(os.listdir(rt_dataset))


class TestImagePath:
    """A renamed json carries the new image name."""

    def test_plain(self, rt_dataset, rt_out):
        make_pair(rt_dataset, "a", "person")
        _plan, path, _summary = _run(rt_dataset, rt_out)
        assert zip_doc(path, "person_1.json")["imagePath"] == (
            "person_1.jpg"
        )

    def test_with_directory_prefix(self, rt_dataset, rt_out):
        write_image(rt_dataset, "a.jpg")
        write_json(
            rt_dataset,
            "a.json",
            shapes=[{"label": "person"}],
            image_path="images/a.jpg",
        )
        _plan, path, _summary = _run(rt_dataset, rt_out)
        doc = zip_doc(path, "person_1.json")
        assert doc["imagePath"] == "images/person_1.jpg"

    def test_aug_chain(self, rt_dataset, rt_out):
        make_pair(rt_dataset, "a", "person")
        make_pair(rt_dataset, "a_aug1", "person")
        _plan, path, _summary = _run(rt_dataset, rt_out)
        assert zip_doc(path, "person_1_aug1.json")["imagePath"] == (
            "person_1_aug1.jpg"
        )

    def test_unchanged_json_is_verbatim(self, rt_dataset, rt_out):
        make_pair(rt_dataset, "person_1", "person")
        with open(os.path.join(rt_dataset, "person_1.json"), "rb") as fh:
            raw = fh.read()
        _plan, path, _summary = _run(rt_dataset, rt_out)
        assert read_zip(path)["person_1.json"] == raw

    def test_json_stays_parseable(self, rt_dataset, rt_out):
        make_pair(rt_dataset, "a", "person")
        _plan, path, _summary = _run(rt_dataset, rt_out)
        doc = zip_doc(path, "person_1.json")
        assert [shape["label"] for shape in doc["shapes"]] == ["person"]
        assert doc["imageData"] is None
        assert doc["flags"] == {}


class TestCompression:
    """Images are stored, everything else is deflated."""

    def test_methods(self, rt_dataset, rt_out):
        make_pair(rt_dataset, "a", "person")
        write_image(rt_dataset, "notes.txt", b"n" * 400)
        _plan, path, _summary = _run(rt_dataset, rt_out)
        infos = _info(path)
        assert infos["person_1.jpg"].compress_type == zipfile.ZIP_STORED
        assert infos["person_1.json"].compress_type == (
            zipfile.ZIP_DEFLATED
        )
        assert infos["notes.txt"].compress_type == zipfile.ZIP_DEFLATED

    def test_png_is_stored(self, rt_dataset, rt_out):
        make_pair(rt_dataset, "a", "person", ext=".png")
        _plan, path, _summary = _run(rt_dataset, rt_out)
        assert _info(path)["person_1.png"].compress_type == (
            zipfile.ZIP_STORED
        )

    def test_archive_allows_zip64(self, rt_dataset, rt_out, monkeypatch):
        make_pair(rt_dataset, "a", "person")
        seen = []
        original = zipfile.ZipFile

        def _spy(*args, **kwargs):
            seen.append(kwargs.get("allowZip64"))
            return original(*args, **kwargs)

        monkeypatch.setattr(zipfile, "ZipFile", _spy)
        _plan, path, _summary = _run(rt_dataset, rt_out)
        assert seen == [True]
        assert os.path.isfile(path)


class TestProgress:
    """The progress callback counts every entry once."""

    def test_monotonic_and_done(self, rt_dataset, rt_out):
        make_pair(rt_dataset, "a", "person")
        make_pair(rt_dataset, "b", "person")
        write_image(rt_dataset, "classes.txt", b"person\n")
        plan = core.resolve_targets(core.plan_directory(rt_dataset))
        seen = []
        core.write_zip(
            plan,
            os.path.join(rt_out, "result.zip"),
            progress=lambda *args: seen.append(args),
        )
        assert seen[-1] == (plan.total_files(), plan.total_files(), "Done")
        done = [entry[0] for entry in seen]
        assert done == sorted(done)
        assert done[-1] == plan.total_files()

    def test_reports_the_entry_name(self, rt_dataset, rt_out):
        make_pair(rt_dataset, "a", "person")
        plan = core.resolve_targets(core.plan_directory(rt_dataset))
        seen = []
        core.write_zip(
            plan,
            os.path.join(rt_out, "result.zip"),
            progress=lambda *args: seen.append(args),
        )
        names = [entry[2] for entry in seen[:-1]]
        assert names
        assert all(name in read_zip(os.path.join(rt_out, "result.zip"))
                   for name in names)
