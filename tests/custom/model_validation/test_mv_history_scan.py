"""Behaviour of the run history scanner (history.list_runs).

Every scratch folder is created inside the tmp_path given by the
shared conftest, so the system temporary directory itself is never
scanned and a real validation run is never touched.
"""

import os
import os.path as osp

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


def staged_run(parent, name, source_display="/data/src"):
    """Create one staging folder holding a single labelled original."""

    root = make_staging(parent, name)
    entry = original_entry(root, "a.png")
    touch(entry["staging_image_path"])
    write_label(entry["staging_label_path"])
    write_meta(root, [entry], source_display)
    return root


def test_only_staging_folders_are_listed(tmp_path):
    temp_root = str(tmp_path)
    keep = staged_run(temp_root, "keep")
    other = staged_run(temp_root, "other")
    os.makedirs(osp.join(temp_root, "unrelated"), exist_ok=True)
    touch(osp.join(temp_root, PREFIX + "plain_file"))
    found = {run.staging_root for run in history.list_runs(temp_root)}
    assert found == {keep, other}


def test_newest_run_comes_first(tmp_path):
    temp_root = str(tmp_path)
    oldest = staged_run(temp_root, "oldest")
    middle = staged_run(temp_root, "middle")
    newest = staged_run(temp_root, "newest")
    os.utime(oldest, (1000, 1000))
    os.utime(middle, (2000, 2000))
    os.utime(newest, (3000, 3000))
    runs = history.list_runs(temp_root)
    assert [run.staging_root for run in runs] == [newest, middle, oldest]


def test_missing_meta_is_reported(tmp_path):
    temp_root = str(tmp_path)
    root = make_staging(temp_root, "nometa")
    write_label(
        osp.join(
            root, dataset.ORIGINAL_DIRNAME, dataset.LABELS_DIRNAME, "a.png"
        )
    )
    run = history.list_runs(temp_root)[0]
    assert run.reason == history.REASON_MISSING_META
    assert run.meta_readable is False
    assert run.restorable is False


def test_empty_meta_is_reported_as_missing(tmp_path):
    temp_root = str(tmp_path)
    root = make_staging(temp_root, "emptymeta")
    write_label(
        osp.join(
            root, dataset.ORIGINAL_DIRNAME, dataset.LABELS_DIRNAME, "a.png"
        )
    )
    touch(osp.join(root, dataset.META_FILENAME), b"")
    run = history.list_runs(temp_root)[0]
    assert run.reason == history.REASON_MISSING_META


def test_broken_meta_is_reported(tmp_path):
    temp_root = str(tmp_path)
    root = make_staging(temp_root, "broken")
    write_label(
        osp.join(
            root, dataset.ORIGINAL_DIRNAME, dataset.LABELS_DIRNAME, "a.png"
        )
    )
    touch(osp.join(root, dataset.META_FILENAME), b'{"counts": ')
    run = history.list_runs(temp_root)[0]
    assert run.reason == history.REASON_BAD_META
    assert run.meta_readable is False


def test_empty_labels_folder_is_reported(tmp_path):
    temp_root = str(tmp_path)
    root = make_staging(temp_root, "empty")
    write_meta(root, [])
    run = history.list_runs(temp_root)[0]
    assert run.reason == history.REASON_NO_LABELS
    assert run.label_count == 0
    assert run.restorable is False


def test_vanished_folder_is_reported_as_missing(tmp_path, monkeypatch):
    temp_root = str(tmp_path)
    target = staged_run(temp_root, "gone")
    staged_run(temp_root, "kept")
    real_summarize = history._summarize

    def vanished(path, mtime, truncated):
        if path == target:
            os.rename(target, target + "_moved")
        return real_summarize(path, mtime, truncated)

    monkeypatch.setattr(history, "_summarize", vanished)
    runs = {run.staging_root: run for run in history.list_runs(temp_root)}
    assert runs[target].reason == history.REASON_MISSING
    assert runs[target].label_count == 0
    assert runs[target].restorable is False


def test_folder_vanishing_mid_scan_does_not_raise(tmp_path, monkeypatch):
    temp_root = str(tmp_path)
    target = staged_run(temp_root, "vanish")
    staged_run(temp_root, "survivor")
    real_summarize = history._summarize

    def broken(path, mtime, truncated):
        if path == target:
            raise FileNotFoundError(path)
        return real_summarize(path, mtime, truncated)

    monkeypatch.setattr(history, "_summarize", broken)
    runs = history.list_runs(temp_root)
    assert {osp.basename(run.staging_root) for run in runs} == {
        PREFIX + "survivor"
    }


def test_state_counters_match_the_written_values(tmp_path):
    temp_root = str(tmp_path)
    root = staged_run(temp_root, "state")
    entry = original_entry(root, "a.png")
    judged = records.make_record(
        records.KIND_ORIGINAL,
        "a.png",
        entry["staging_image_path"],
        entry["staging_label_path"],
    )
    judged.verdict = records.OK
    judged.judged = True
    marked = records.make_record(
        records.KIND_AUGMENTED, "a_aug1.png", "image", "label"
    )
    marked.include_in_export = True
    skipped = records.make_record(
        records.KIND_ORIGINAL, "b.png", "image", ""
    )
    skipped.verdict = records.SKIPPED
    history.save_restore_state(
        root, [judged, marked, skipped], classes=["car"]
    )
    run = history.list_runs(temp_root)[0]
    assert run.state_readable is True
    assert run.judged == 1
    assert run.marked == 1
    assert run.skipped == 1
    assert run.verdict_counts == {
        records.OK: 1,
        records.PENDING: 1,
        records.SKIPPED: 1,
    }


def test_run_without_state_is_still_restorable(tmp_path):
    temp_root = str(tmp_path)
    staged_run(temp_root, "nostate")
    run = history.list_runs(temp_root)[0]
    assert run.state_readable is False
    assert run.restorable is True
    assert run.judged == 0
    assert run.marked == 0
    assert run.verdict_counts == {}


def test_scan_limit_marks_the_shown_head_as_truncated(tmp_path):
    temp_root = str(tmp_path)
    staged_run(temp_root, "one")
    staged_run(temp_root, "two")
    limited = history.list_runs(temp_root, scan_limit=1)
    assert len(limited) == 1
    assert limited[0].truncated is True
    full = history.list_runs(temp_root, scan_limit=10)
    assert len(full) == 2
    assert [run.truncated for run in full] == [False, False]


def test_large_meta_head_still_gives_the_counts(tmp_path):
    temp_root = str(tmp_path)
    root = make_staging(temp_root, "big")
    entry = original_entry(root, "a.png")
    write_label(entry["staging_label_path"])
    filler = [
        {
            "relpath": f"image_{index:05d}.png",
            "staging_image_path": "/tmp/staging/images/" + "x" * 60,
            "staging_label_path": "/tmp/staging/labels/" + "y" * 60,
        }
        for index in range(2000)
    ]
    meta_path = osp.join(root, dataset.META_FILENAME)
    dataset.write_json(
        meta_path,
        {
            "staging_root": root,
            "source_display": "/data/big",
            "counts": {"original": 1714},
            "originals": filler,
        },
    )
    assert osp.getsize(meta_path) > history.META_HEAD_BYTES
    run = history.list_runs(temp_root)[0]
    assert run.meta_readable is True
    assert run.staged_originals == 1714
    assert run.source_display == "/data/big"
    assert run.label_count == 1
    assert run.reason == ""
