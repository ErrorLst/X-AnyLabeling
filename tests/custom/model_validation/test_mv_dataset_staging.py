"""Dataset scanning and staging tests."""

import json
import os
import os.path as osp

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from anylabeling.custom.model_validation import dataset

SANDBOX_SKIP_REASON = (
    "the sandbox forbids file IO inside a freshly created staging folder"
)


def make_staging_root(parent=None):
    """Create a staging root, skipping when the sandbox denies it."""

    try:
        return dataset.create_staging_root(parent)
    except PermissionError:
        pytest.skip(SANDBOX_SKIP_REASON)


def write_image(path: str, width: int = 8, height: int = 6) -> None:
    import cv2

    os.makedirs(osp.dirname(path), exist_ok=True)
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:, :, 1] = 200
    ok, buffer = cv2.imencode(".png", image)
    assert ok
    buffer.tofile(path)


def write_label(path: str, name: str = "a.png") -> None:
    os.makedirs(osp.dirname(path), exist_ok=True)
    payload = {
        "version": "3.0.0",
        "flags": {},
        "checked": False,
        "shapes": [],
        "imagePath": name,
        "imageData": None,
        "imageHeight": 6,
        "imageWidth": 8,
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


def build_source(tmp_path) -> str:
    root = tmp_path / "source"
    write_image(str(root / "b.png"))
    write_label(str(root / "b.json"), "b.png")
    write_image(str(root / "a.png"))
    write_label(str(root / "a.json"), "a.png")
    write_image(str(root / "sub" / "c.png"))
    write_label(str(root / "sub" / "c.json"), "c.png")
    write_image(str(root / "no_label.png"))
    write_label(str(root / "orphan.json"), "orphan.png")
    return str(root)


def test_collect_pairs_counts(tmp_path):
    source = build_source(tmp_path)
    scan = dataset.collect_pairs(source)
    assert [pair.relpath for pair in scan.pairs] == [
        "a.png",
        "b.png",
        "sub/c.png",
    ]
    assert scan.image_without_label == ["no_label.png"]
    assert scan.label_without_image == ["orphan.json"]
    assert scan.unreadable_label_pairs == []


def test_collect_pairs_natural_sort(tmp_path):
    source = tmp_path / "natsort"
    for name in ("img10", "img2", "img1"):
        write_image(str(source / (name + ".png")))
        write_label(str(source / (name + ".json")), name + ".png")
    scan = dataset.collect_pairs(str(source))
    assert [pair.relpath for pair in scan.pairs] == [
        "img1.png",
        "img2.png",
        "img10.png",
    ]


def test_stage_dataset_mirrors_relpaths_and_contents(tmp_path, mv_scratch):
    source = build_source(tmp_path)
    staging = make_staging_root(mv_scratch)
    assert osp.basename(staging).startswith(dataset.STAGING_PREFIX)
    meta = dataset.stage_dataset(source, staging)
    assert meta["counts"]["original"] == 3
    assert meta["counts"]["skipped_no_label"] == 1
    assert meta["counts"]["orphan_labels"] == 1
    for relpath in ("a.png", "b.png", "sub/c.png"):
        paths = dataset.staging_paths(staging, "original", relpath)
        assert osp.isfile(paths["image"])
        assert osp.isfile(paths["label"])
        assert (
            osp.relpath(paths["image"], staging)
            .replace(os.sep, "/")
            .startswith("original/images/")
        )
        source_image = osp.join(source, relpath.replace("/", os.sep))
        with open(source_image, "rb") as handle:
            expected = handle.read()
        with open(paths["image"], "rb") as handle:
            assert handle.read() == expected
    meta_path = osp.join(staging, dataset.META_FILENAME)
    assert osp.isfile(meta_path)
    written = dataset.read_json(meta_path)
    assert written["staging_root"] == staging
    assert written["counts"]["original"] == 3
    assert written["counts"]["skipped_no_label"] == 1
    assert [item["relpath"] for item in written["originals"]] == [
        "a.png",
        "b.png",
        "sub/c.png",
    ]


def test_unreadable_label_pair_is_excluded(tmp_path):
    source = tmp_path / "broken"
    write_image(str(source / "b.png"))
    write_label(str(source / "b.json"), "b.png")
    write_image(str(source / "broken.png"))
    (source / "broken.json").write_text("{ not json", encoding="utf-8")
    scan = dataset.collect_pairs(str(source))
    assert [pair.relpath for pair in scan.pairs] == ["b.png"]
    assert scan.unreadable_label_pairs == ["broken.png"]
    assert scan.image_without_label == []
    assert scan.label_without_image == []


def test_snapshot_directory_detects_changes(tmp_path):
    source = build_source(tmp_path)
    first = dataset.snapshot_directory(source)
    second = dataset.snapshot_directory(source)
    assert first == second
    target = osp.join(source, "a.png")
    os.utime(target, (1, 1))
    third = dataset.snapshot_directory(source)
    assert third["a.png"]["mtime_ns"] != first["a.png"]["mtime_ns"]


def test_create_staging_root_is_unique(mv_scratch):
    first = make_staging_root(mv_scratch)
    second = make_staging_root(mv_scratch)
    assert first != second
    assert osp.isdir(first) and osp.isdir(second)
