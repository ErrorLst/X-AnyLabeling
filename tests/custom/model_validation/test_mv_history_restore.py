"""Behaviour of restore_records and of the augmented bookkeeping.

A restored run has to show exactly what the finished run showed, and
when there is no judgement data it has to fall back to the staged
images and labels without losing a record or inventing a parent.
"""

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


def staged_paths(root, kind, relpath):
    return dataset.staging_paths(root, kind, relpath)


def stage_original(root, relpath, with_label=True):
    paths = staged_paths(root, records.KIND_ORIGINAL, relpath)
    touch(paths["image"])
    if with_label:
        write_label(paths["label"])
    return {
        "relpath": relpath,
        "staging_image_path": paths["image"],
        "staging_label_path": paths["label"] if with_label else "",
    }


def stage_augmented(
    root, relpath, with_image=True, with_label=True
):
    paths = staged_paths(root, records.KIND_AUGMENTED, relpath)
    if with_image:
        touch(paths["image"])
    if with_label:
        write_label(paths["label"])
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


def record_for(root, kind, relpath):
    paths = staged_paths(root, kind, relpath)
    return records.make_record(
        kind, relpath, paths["image"], paths["label"]
    )


def plain_original(relpath):
    return records.make_record(records.KIND_ORIGINAL, relpath, "img", "lbl")


def plain_augmented(relpath, detail=None):
    record = records.make_record(
        records.KIND_AUGMENTED, relpath, "img", "lbl"
    )
    if detail is not None:
        record.aug_detail = detail
    return record


def test_legacy_folder_restores_pending_originals(tmp_path):
    temp_root = str(tmp_path)
    root = make_staging(temp_root, "legacy")
    first = stage_original(root, "a.png")
    second = stage_original(root, "b.png")
    write_meta(root, [first, second])
    run = history.list_runs(temp_root)[0]
    restored, report = history.restore_records(run, ["car"])
    assert report["state_readable"] is False
    assert "没有判定数据" in report["note"]
    assert report["record_count"] == 2
    assert report["classes"] == ["car"]
    assert report["source_display"] == "/data/src"
    assert [record.relpath for record in restored] == ["a.png", "b.png"]
    for record in restored:
        assert record.verdict == records.PENDING
        assert record.judged is False
        assert record.parent_record_id is None
        assert record.detail["classes"] == ["car"]
        assert record.image_exists is True


def test_unique_augment_match_links_the_parent(tmp_path):
    temp_root = str(tmp_path)
    root = make_staging(temp_root, "unique")
    parent = stage_original(root, "a.png")
    stage_augmented(root, "a_aug1.png")
    write_meta(root, [parent])
    run = history.list_runs(temp_root)[0]
    restored, report = history.restore_records(run, ["car"])
    children = {
        record.record_id: record
        for record in restored
        if record.kind == records.KIND_AUGMENTED
    }
    child = children["augmented::a_aug1.png"]
    assert child.parent_record_id == "original::a.png"
    assert child.verdict == records.PENDING
    assert child.aug_detail == {}
    assert report["unknown_parents"] == []


def test_ambiguous_parent_is_never_linked(tmp_path):
    temp_root = str(tmp_path)
    root = make_staging(temp_root, "ambiguous")
    entry = stage_original(root, "dup.png")
    write_meta(root, [entry, dict(entry)])
    stage_augmented(root, "dup_aug1.png")
    run = history.list_runs(temp_root)[0]
    restored, report = history.restore_records(run, ["car"])
    children = [
        record
        for record in restored
        if record.kind == records.KIND_AUGMENTED
    ]
    assert len(children) == 1
    assert children[0].parent_record_id is None
    assert report["unknown_parents"] == ["augmented::dup_aug1.png"]


def test_augmented_without_image_or_label_is_kept(tmp_path):
    temp_root = str(tmp_path)
    root = make_staging(temp_root, "degraded")
    parent = stage_original(root, "a.png")
    write_meta(root, [parent])
    stage_augmented(root, "only_label_aug1.png", with_image=False)
    stage_augmented(root, "only_image_aug1.png", with_label=False)
    run = history.list_runs(temp_root)[0]
    restored, report = history.restore_records(run, ["car"])
    by_id = {record.record_id: record for record in restored}
    label_only = by_id["augmented::only_label_aug1.png"]
    image_only = by_id["augmented::only_image_aug1.png"]
    assert label_only.image_exists is False
    assert label_only.label_exists is True
    assert image_only.image_exists is True
    assert image_only.label_exists is False
    assert report["dropped_missing_images"] == 0
    assert report["record_count"] == 3


def test_document_stray_in_the_images_tree_is_not_a_copy(tmp_path):
    temp_root = str(tmp_path)
    root = make_staging(temp_root, "stray")
    parent = stage_original(root, "a.png")
    write_meta(root, [parent])
    touch(osp.join(root, "augmented", "images", "a_aug1.json"))
    touch(osp.join(root, "original", "images", "a.png.json"))
    run = history.list_runs(temp_root)[0]
    restored, report = history.restore_records(run, ["car"])
    assert [record.record_id for record in restored] == ["original::a.png"]
    assert report["record_count"] == 1
    current = [record_for(root, records.KIND_ORIGINAL, "a.png")]
    refreshed = history.refresh_from_roots(root, current)
    assert refreshed["missing_labels"] == []
    assert refreshed["extra_augmented"] == []
    assert refreshed["renamed"] == []


@pytest.mark.parametrize(
    "name,expected",
    [
        ("a_aug1.jpg", "a.jpg"),
        ("a (1)_aug12.png", "a (1).png"),
        ("plain.png", "plain.png"),
        ("a_aug.png", "a_aug.png"),
        ("a_aug1x.png", "a_aug1x.png"),
        ("sub/a_aug2.jpg", "sub/a.jpg"),
        ("sub/_aug1.png", "sub/_aug1.png"),
        ("_aug1.png", "_aug1.png"),
    ],
)
def test_augment_suffix_stem_table(name, expected):
    assert history.augment_suffix_stem(name) == expected


def test_augment_parents_table():
    parent = plain_original("a.png")
    by_detail = plain_augmented(
        "x_aug1.png", {"parent_relpath": "a.png"}
    )
    by_suffix = plain_augmented("a_aug1.png")
    loose = plain_augmented("loose.png")
    duplicate = plain_original("dup.png")
    duplicate_twin = plain_original("dup.png")
    ambiguous = plain_augmented("dup_aug1.png")
    parents = history.augment_parents(
        [
            parent,
            by_detail,
            by_suffix,
            loose,
            duplicate,
            duplicate_twin,
            ambiguous,
        ]
    )
    assert parents == {
        by_detail.record_id: parent.record_id,
        by_suffix.record_id: parent.record_id,
    }


def test_existing_parent_link_wins_over_the_suffix():
    first = plain_original("a.png")
    second = plain_original("b.png")
    child = plain_augmented("a_aug1.png")
    child.parent_record_id = second.record_id
    parents = history.augment_parents([first, second, child])
    assert parents == {child.record_id: second.record_id}


def test_clear_augment_parents_drops_unconfirmed_links():
    parent = plain_original("a.png")
    stale = plain_augmented("a_aug1.png")
    stale.parent_record_id = "original::ghost.png"
    kept = plain_augmented("b_aug1.png")
    kept.parent_record_id = parent.record_id
    empty = plain_augmented("c_aug1.png")
    cleared = history.clear_augment_parents(
        [parent, stale, kept, empty], [stale.record_id, empty.relpath]
    )
    assert cleared == [stale.record_id]
    assert stale.parent_record_id is None
    assert kept.parent_record_id == parent.record_id
    assert empty.parent_record_id is None


def test_refresh_reports_missing_labels_and_new_copies(tmp_path):
    temp_root = str(tmp_path)
    root = make_staging(temp_root, "refresh")
    first = stage_original(root, "a.png")
    second = stage_original(root, "b.png")
    write_meta(root, [first, second])
    stage_augmented(root, "a_aug1.png")

    kept_original = record_for(root, records.KIND_ORIGINAL, "a.png")
    kept_original.verdict = records.OK
    kept_original.judged = True
    kept_augmented = record_for(root, records.KIND_AUGMENTED, "a_aug1.png")
    kept_augmented.include_in_export = True
    ghost = record_for(root, records.KIND_AUGMENTED, "ghost_aug2.png")
    current = [kept_original, kept_augmented, ghost]

    trash = osp.join(temp_root, "trash")
    os.makedirs(trash, exist_ok=True)
    os.rename(second["staging_label_path"], osp.join(trash, "b.png"))
    stage_augmented(root, "b_aug1.png")

    report = history.refresh_from_roots(root, current)
    assert report["missing_labels"] == ["original::b.png"]
    assert report["missing_images"] == []
    assert report["extra_augmented"] == ["b_aug1.png"]
    assert report["removed"] == ["ghost_aug2.png"]
    assert report["renamed"] == []
    relpaths = [record.relpath for record in current]
    assert relpaths == ["a.png", "a_aug1.png", "b_aug1.png"]
    assert kept_original.verdict == records.OK
    assert kept_original.judged is True
    assert kept_augmented.include_in_export is True
    added = current[-1]
    assert added.kind == records.KIND_AUGMENTED
    assert added.verdict == records.PENDING
    assert added.parent_record_id is None


def test_refresh_follows_a_rename_and_keeps_the_verdict(tmp_path):
    temp_root = str(tmp_path)
    root = make_staging(temp_root, "rename")
    parent = stage_original(root, "a.png")
    write_meta(root, [parent])
    old = stage_augmented(root, "a_aug1.png")
    record = record_for(root, records.KIND_AUGMENTED, "a_aug1.png")
    record.verdict = records.NG
    record.include_in_export = True
    record.judged = True
    current = [record_for(root, records.KIND_ORIGINAL, "a.png"), record]

    os.rename(
        old["staging_image_path"],
        osp.join(osp.dirname(old["staging_image_path"]), "renamed_aug1.png"),
    )
    os.rename(
        old["staging_label_path"],
        osp.join(osp.dirname(old["staging_label_path"]), "renamed_aug1.png"),
    )

    report = history.refresh_from_roots(root, current)
    assert report["extra_augmented"] == []
    assert report["removed"] == []
    assert len(report["renamed"]) == 1
    assert report["renamed"][0]["old_relpath"] == "a_aug1.png"
    assert report["renamed"][0]["new_relpath"] == "renamed_aug1.png"
    assert record.relpath == "renamed_aug1.png"
    assert record.record_id == "augmented::renamed_aug1.png"
    assert record.verdict == records.NG
    assert record.include_in_export is True
    assert record.image_exists is True
    assert [item.relpath for item in current] == ["a.png", "renamed_aug1.png"]
