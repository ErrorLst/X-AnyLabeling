"""Behaviour of state.json: writing, reading and the failure paths.

The state file is the only thing a run keeps besides its images, so
these tests pin its place, its shape, its atomic write and the way a
damaged or a foreign file is read back.
"""

import json
import os
import os.path as osp

import pytest

from anylabeling.custom.model_validation import dataset
from anylabeling.custom.model_validation import history
from anylabeling.custom.model_validation import records

PREFIX = dataset.STAGING_PREFIX


def make_staging(parent, name):
    root = osp.join(parent, PREFIX + name)
    for folder in (dataset.ORIGINAL_DIRNAME, dataset.AUGMENTED_DIRNAME):
        for sub in (dataset.IMAGES_DIRNAME, dataset.LABELS_DIRNAME):
            os.makedirs(osp.join(root, folder, sub), exist_ok=True)
    return root


def touch(path, payload=b"\x89PNG\r\n\x1a\n"):
    os.makedirs(osp.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(payload)


def write_label(path):
    os.makedirs(osp.dirname(path), exist_ok=True)
    dataset.write_json(path, {"shapes": []})


def original_entry(root, relpath):
    paths = dataset.staging_paths(root, records.KIND_ORIGINAL, relpath)
    return {
        "relpath": relpath,
        "staging_image_path": paths["image"],
        "staging_label_path": paths["label"],
    }


def write_meta(root, originals, source_display="/data/src"):
    dataset.write_json(
        osp.join(root, dataset.META_FILENAME),
        {
            "staging_root": root,
            "source_display": source_display,
            "counts": {"original": len(originals)},
            "originals": originals,
            "skipped": [],
        },
    )


def staged_original(root, relpath="a.png"):
    entry = original_entry(root, relpath)
    touch(entry["staging_image_path"])
    write_label(entry["staging_label_path"])
    return entry


def rich_record():
    record = records.make_record(
        records.KIND_AUGMENTED,
        "sub/a_aug1.png",
        "/staging/images/a_aug1.png",
        "/staging/labels/a_aug1.png",
        source_display="/data/src",
        parent_record_id="original::sub/a.png",
    )
    record.verdict = records.NG
    record.reasons = ["miss", "low_score"]
    record.detail = {
        "predictions": [
            {"label": "car", "score": 0.25, "points": [[1.0, 2.0]]}
        ],
        "classes": ["car"],
        "skipped": False,
    }
    record.deleted = True
    record.include_in_export = True
    record.edited = True
    record.judged = True
    record.aug_detail = {
        "parent_relpath": "sub/a.png",
        "copy_index": 1,
        "seed": 7,
        "clipped": [],
    }
    return record


def read_state_file(root):
    with open(
        osp.join(root, history.STATE_FILENAME), encoding="utf-8"
    ) as handle:
        return handle.read()


def test_state_file_lands_next_to_the_meta_with_every_field(tmp_path):
    root = str(tmp_path)
    record = rich_record()
    payload = history.save_restore_state(
        root,
        [record],
        classes=["car", "bus"],
        source_display="/data/src",
        shown_record_id=record.record_id,
    )
    path = osp.join(root, history.STATE_FILENAME)
    assert osp.isfile(path)
    written = json.loads(read_state_file(root))
    assert set(written) == {
        "schema_version",
        "saved_at",
        "staging_root",
        "source_display",
        "classes",
        "record_count",
        "shown_record_id",
        "summary",
        "records",
    }
    assert written["schema_version"] == history.STATE_VERSION
    assert written["staging_root"] == root
    assert written["source_display"] == "/data/src"
    assert written["classes"] == ["car", "bus"]
    assert written["record_count"] == 1
    assert written["shown_record_id"] == record.record_id
    assert written["records"] == [record.to_dict()]
    assert written["summary"] == {
        "judged": 1,
        "skipped": 0,
        "marked": 1,
        "verdicts": {records.NG: 1},
    }
    assert payload["record_count"] == 1


def test_state_file_is_compact_and_leaves_no_temporary(tmp_path):
    root = str(tmp_path)
    history.save_restore_state(root, [rich_record()])
    with open(
        osp.join(root, history.STATE_FILENAME), "rb"
    ) as handle:
        raw = handle.read()
    assert b"\n" not in raw
    assert b'": ' not in raw
    assert b", " not in raw
    assert not osp.isfile(
        osp.join(root, history.STATE_FILENAME + ".tmp")
    )
    assert json.loads(raw.decode("utf-8"))["record_count"] == 1


def test_round_trip_restores_every_field(tmp_path):
    root = str(tmp_path)
    record = rich_record()
    history.save_restore_state(root, [record], classes=["car"])
    state = history.read_restore_state(root)
    assert state["readable"] is True
    assert state["classes"] == ["car"]
    assert state["record_count"] == 1
    assert state["note"] == ""
    restored = records.ValidationRecord.from_dict(state["records"][0])
    assert restored.to_dict() == record.to_dict()
    assert state["summary"] == {
        "judged": 1,
        "skipped": 0,
        "marked": 1,
        "verdicts": {records.NG: 1},
    }


def test_state_file_is_rewritten_in_place(tmp_path):
    root = str(tmp_path)
    history.save_restore_state(root, [rich_record()])
    second = records.make_record(
        records.KIND_ORIGINAL, "b.png", "/staging/b.png", "/staging/b.json"
    )
    history.save_restore_state(root, [second], classes=["bus"])
    state = history.read_restore_state(root)
    assert state["record_count"] == 1
    assert state["classes"] == ["bus"]
    assert state["records"][0]["relpath"] == "b.png"
    assert not osp.isfile(
        osp.join(root, history.STATE_FILENAME + ".tmp")
    )


def test_missing_state_file_reads_as_unreadable(tmp_path):
    root = str(tmp_path)
    state = history.read_restore_state(root)
    assert state["readable"] is False
    assert state["records"] == []
    assert state["summary"]["judged"] == 0
    assert history.NO_STATE_NOTE in state["note"]


def test_broken_state_file_reads_as_unreadable(tmp_path):
    root = str(tmp_path)
    touch(osp.join(root, history.STATE_FILENAME), b"{not json")
    state = history.read_restore_state(root)
    assert state["readable"] is False
    assert state["records"] == []


def test_foreign_staging_root_is_ignored_and_noted(tmp_path):
    root = str(tmp_path)
    payload = history.save_restore_state(root, [rich_record()])
    payload["staging_root"] = "/somewhere/else/xal_validation_other"
    dataset.write_json(osp.join(root, history.STATE_FILENAME), payload)
    state = history.read_restore_state(root)
    assert state["readable"] is True
    assert state["staging_root"] == root
    assert state["record_count"] == 1
    assert history.MISMATCH_NOTE in state["note"]


def test_newer_schema_is_read_back_field_by_field(tmp_path):
    root = str(tmp_path)
    payload = history.save_restore_state(root, [rich_record()])
    payload["schema_version"] = 99
    dataset.write_json(osp.join(root, history.STATE_FILENAME), payload)
    state = history.read_restore_state(root)
    assert state["readable"] is True
    assert state["schema_version"] == 99
    assert state["records"][0]["verdict"] == records.NG
    assert state["records"][0]["detail"]["predictions"][0]["score"] == 0.25
    assert history.NEWER_SCHEMA_NOTE in state["note"]


def test_record_with_missing_image_is_dropped_and_counted(tmp_path):
    temp_root = str(tmp_path)
    root = make_staging(temp_root, "drop")
    entry = staged_original(root)
    write_meta(root, [entry])
    alive = records.make_record(
        records.KIND_ORIGINAL,
        "a.png",
        entry["staging_image_path"],
        entry["staging_label_path"],
    )
    alive.verdict = records.OK
    alive.judged = True
    ghost = records.make_record(
        records.KIND_AUGMENTED,
        "a_aug1.png",
        osp.join(root, "augmented", "images", "a_aug1.png"),
        osp.join(root, "augmented", "labels", "a_aug1.png"),
    )
    ghost.judged = True
    history.save_restore_state(root, [alive, ghost])
    run = history.list_runs(temp_root)[0]
    restored, report = history.restore_records(run, ["car"])
    ids = [record.record_id for record in restored]
    assert "original::a.png" in ids
    assert "augmented::a_aug1.png" not in ids
    assert report["dropped_missing_images"] == 1
    assert report["judged"] == 1
    assert report["state_readable"] is True


def test_failed_replace_raises_and_keeps_the_previous_file(
    tmp_path, monkeypatch
):
    root = str(tmp_path)
    history.save_restore_state(root, [rich_record()])
    before = read_state_file(root)

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    second = records.make_record(
        records.KIND_ORIGINAL, "b.png", "/staging/b.png", "/staging/b.json"
    )
    with pytest.raises(OSError):
        history.save_restore_state(root, [second])
    after = read_state_file(root)
    assert after == before
    assert json.loads(after)["records"][0]["relpath"] == "sub/a_aug1.png"


def test_failed_write_raises_oserror(tmp_path, monkeypatch):
    root = str(tmp_path)
    real_open = open

    def guarded(file, mode="r", *args, **kwargs):
        if str(file).endswith(history.STATE_FILENAME + ".tmp"):
            raise OSError("read only")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", guarded)
    with pytest.raises(OSError):
        history.save_restore_state(root, [rich_record()])
    assert not osp.isfile(osp.join(root, history.STATE_FILENAME))
