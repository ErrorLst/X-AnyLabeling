"""Tests of the backup writer: original names, bytes and progress."""

import json
import os
import zipfile

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
    """Plan, write the backup and return (plan, path, summary)."""

    plan = core.resolve_targets(core.plan_directory(root))
    path = os.path.join(out, name)
    summary = core.write_backup(plan, path)
    return plan, path, summary


def _info(path):
    """Return {entry name: ZipInfo} of an archive."""

    with zipfile.ZipFile(path) as archive:
        return {info.filename: info for info in archive.infolist()}


class TestSourceUntouched:
    """Writing the backup keeps bytes, mtimes and the listing."""

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

    def test_summary_flags_the_source(self, rt_dataset, rt_out):
        self._dataset(rt_dataset)
        _plan, _path, summary = _run(rt_dataset, rt_out)
        assert summary["source_untouched"] is True
        assert summary["files"] == 7
        assert summary["ignored"] == 0
        assert summary["mirrored"] == 3


class TestBackupContent:
    """The archive is the folder as it is, current names and bytes."""

    def test_names_match_the_folder(self, rt_dataset, rt_out):
        make_pair(rt_dataset, "a", "person")
        make_pair(rt_dataset, "a_aug1", "person")
        write_image(rt_dataset, "notes.txt", b"n")
        _plan, path, _summary = _run(rt_dataset, rt_out)
        assert sorted(read_zip(path)) == sorted(os.listdir(rt_dataset))

    def test_current_names_not_target_names(self, rt_dataset, rt_out):
        make_pair(rt_dataset, "a", "person")
        plan = core.resolve_targets(core.plan_directory(rt_dataset))
        assert plan.valid_items()[0].target_image_name() == (
            "person_1.jpg"
        )
        _plan, path, _summary = _run(rt_dataset, rt_out)
        assert sorted(read_zip(path)) == ["a.jpg", "a.json"]

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

    def test_kept_files_keep_their_bytes(self, rt_dataset, rt_out):
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

    def test_unclaimed_files_are_mirrored_verbatim(self, rt_dataset, rt_out):
        make_pair(rt_dataset, "a", "person")
        write_image(rt_dataset, "a.txt", b"companion text")
        write_image(rt_dataset, ".hidden", b"\x00\xffbinary")
        plan = core.plan_directory(rt_dataset)
        assert sorted(plan.mirrored) == [".hidden", "a.txt"]
        assert plan.total_files() == len(os.listdir(rt_dataset))
        _plan, path, _summary = _run(rt_dataset, rt_out)
        entries = read_zip(path)
        assert entries["a.txt"] == b"companion text"
        assert entries[".hidden"] == b"\x00\xffbinary"


class TestJsonVerbatim:
    """The backup keeps the json exactly as it is on disk."""

    def test_image_path_is_not_rewritten(self, rt_dataset, rt_out):
        make_pair(rt_dataset, "a", "person")
        _plan, path, _summary = _run(rt_dataset, rt_out)
        assert zip_doc(path, "a.json")["imagePath"] == "a.jpg"

    def test_directory_prefix_is_kept(self, rt_dataset, rt_out):
        write_image(rt_dataset, "a.jpg")
        write_json(
            rt_dataset,
            "a.json",
            shapes=[{"label": "person"}],
            image_path="images/a.jpg",
        )
        _plan, path, _summary = _run(rt_dataset, rt_out)
        doc = zip_doc(path, "a.json")
        assert doc["imagePath"] == "images/a.jpg"

    def test_bytes_match_the_file(self, rt_dataset, rt_out):
        make_pair(rt_dataset, "a", "person")
        with open(os.path.join(rt_dataset, "a.json"), "rb") as fh:
            raw = fh.read()
        _plan, path, _summary = _run(rt_dataset, rt_out)
        assert read_zip(path)["a.json"] == raw

    def test_json_stays_parseable(self, rt_dataset, rt_out):
        make_pair(rt_dataset, "a", "person")
        _plan, path, _summary = _run(rt_dataset, rt_out)
        doc = zip_doc(path, "a.json")
        assert [shape["label"] for shape in doc["shapes"]] == ["person"]
        assert doc["imageData"] is None
        assert doc["flags"] == {}


class TestIgnoredJson:
    """A json without an image still lands in the backup."""

    def test_it_is_included_and_counted(self, rt_dataset, rt_out):
        make_pair(rt_dataset, "a", "person")
        write_json(rt_dataset, "c.json", shapes=[])
        write_image(rt_dataset, "classes.txt", b"x")
        _plan, path, summary = _run(rt_dataset, rt_out)
        assert summary["ignored"] == 1
        assert "c.json" in read_zip(path)
        assert summary["files"] == 4

    def test_only_orphan_json(self, rt_dataset, rt_out):
        write_json(rt_dataset, "c.json", shapes=[])
        _plan, path, summary = _run(rt_dataset, rt_out)
        assert sorted(read_zip(path)) == ["c.json"]
        assert summary["files"] == 1
        assert summary["ignored"] == 1

    def test_empty_folder(self, rt_dataset, rt_out):
        plan = core.resolve_targets(core.plan_directory(rt_dataset))
        path = os.path.join(rt_out, "result.zip")
        summary = core.write_backup(plan, path)
        assert read_zip(path) == {}
        assert summary["files"] == 0


class TestCompression:
    """Images are stored, everything else is deflated."""

    def test_methods(self, rt_dataset, rt_out):
        make_pair(rt_dataset, "a", "person")
        write_image(rt_dataset, "notes.txt", b"n" * 400)
        _plan, path, _summary = _run(rt_dataset, rt_out)
        infos = _info(path)
        assert infos["a.jpg"].compress_type == zipfile.ZIP_STORED
        assert infos["a.json"].compress_type == zipfile.ZIP_DEFLATED
        assert infos["notes.txt"].compress_type == zipfile.ZIP_DEFLATED

    def test_png_is_stored(self, rt_dataset, rt_out):
        make_pair(rt_dataset, "a", "person", ext=".png")
        _plan, path, _summary = _run(rt_dataset, rt_out)
        assert _info(path)["a.png"].compress_type == zipfile.ZIP_STORED

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
    """write_backup reports every entry once, then the closing call."""

    def test_one_callback_per_entry(self, rt_dataset, rt_out):
        make_pair(rt_dataset, "a", "person")
        make_pair(rt_dataset, "b", "person")
        write_image(rt_dataset, "classes.txt", b"person\n")
        plan = core.resolve_targets(core.plan_directory(rt_dataset))
        seen = []
        core.write_backup(
            plan,
            os.path.join(rt_out, "result.zip"),
            progress=lambda *args: seen.append(args),
        )
        total = plan.total_files()
        assert len(seen) == total + 1
        assert seen[-1] == (total, total, "Done")

    def test_done_counts_are_strictly_increasing(self, rt_dataset, rt_out):
        make_pair(rt_dataset, "a", "person")
        make_pair(rt_dataset, "b", "person")
        write_image(rt_dataset, "classes.txt", b"person\n")
        plan = core.resolve_targets(core.plan_directory(rt_dataset))
        seen = []
        core.write_backup(
            plan,
            os.path.join(rt_out, "result.zip"),
            progress=lambda *args: seen.append(args),
        )
        done = [entry[0] for entry in seen[:-1]]
        assert done == list(range(1, plan.total_files() + 1))
        assert all(entry[1] == plan.total_files() for entry in seen[:-1])

    def test_reports_the_current_names(self, rt_dataset, rt_out):
        make_pair(rt_dataset, "a", "person")
        write_image(rt_dataset, "classes.txt", b"person\n")
        plan = core.resolve_targets(core.plan_directory(rt_dataset))
        seen = []
        core.write_backup(
            plan,
            os.path.join(rt_out, "result.zip"),
            progress=lambda *args: seen.append(args),
        )
        names = [entry[2] for entry in seen[:-1]]
        assert sorted(names) == sorted(os.listdir(rt_dataset))
