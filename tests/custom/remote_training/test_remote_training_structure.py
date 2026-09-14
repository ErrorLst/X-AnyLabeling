"""Structure and blocking matrix drills (spec §5.2.3 to §5.2.5).

CT25 lives here: an empty `points` rectangle, a single point rectangle
and a two point polygon are illegal, each with a readable reason, and
nothing is uploaded.  CT27 covers the root scan and the N1 blocking
matrix row by row - every finding carries its reason and the dataset
directory is never written to (it is strictly read only, spec §5.3.1).

The two point polygon is the one place where the plan and the spec
disagree: spec §5.2.4 would skip it ("polygon 点数 < 3 ⇒ 跳过"), the
plan CT25 asks for "非法 + 可读原因 + 不上传".  This step implements
the plan (registered as a pending item) and keeps the other skip rows
exactly as the spec declares them.
"""

from __future__ import annotations

import hashlib
import json
import os
import os.path as osp
from typing import Any, Dict, List, Tuple

import pytest

from anylabeling.custom.remote_training.converter import FrozenLabel
from anylabeling.custom.remote_training.packer import (
    PackerRun,
    label_line_cardinality_issues,
    review_plan,
)
from anylabeling.custom.remote_training.pipeline import (
    Pipeline,
    PipelineConfig,
    PipelineError,
)
from anylabeling.custom.remote_training.scanner import (
    scan_dataset,
    validate_label_schema,
)

CLASSES = ("person", "car")
SHA = "0" * 64


def write_classes(root: str, classes: Any = CLASSES) -> str:
    path = osp.join(root, "classes.txt")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(classes) + "\n")
    return path


def write_image(root: str, name: str) -> str:
    path = osp.join(root, name)
    with open(path, "wb") as handle:
        handle.write(b"image-bytes")
    return path


def write_label(
    root: str,
    stem: str,
    shapes: List[Dict[str, Any]],
    *,
    width: Any = 10,
    height: Any = 10,
    extra: Any = None,
) -> str:
    payload: Dict[str, Any] = {"version": "1.0", "shapes": shapes}
    if width is not None:
        payload["imageWidth"] = width
    if height is not None:
        payload["imageHeight"] = height
    if extra:
        payload.update(extra)
    path = osp.join(root, stem + ".json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return path


def shape(label: str, shape_type: str, points: Any) -> Dict[str, Any]:
    return {"label": label, "shape_type": shape_type, "points": points}


def tree_fingerprint(root: str) -> List[Tuple[str, int, int, str]]:
    """(relative path, size, mtime, sha256) of every file below root."""

    entries: List[Tuple[str, int, int, str]] = []
    for current, dirs, names in os.walk(root):
        dirs.sort()
        for name in sorted(names):
            path = osp.join(current, name)
            stat = os.lstat(path)
            with open(path, "rb") as handle:
                digest = hashlib.sha256(handle.read()).hexdigest()
            entries.append(
                (
                    osp.relpath(path, root),
                    stat.st_size,
                    stat.st_mtime_ns,
                    digest,
                )
            )
    return entries


def reasons(error: Any) -> str:
    """The summary plus every per file reason of one PipelineError."""

    lines = [str(error)]
    for issue in getattr(error, "issues", ()) or ():
        text = getattr(issue, "text", None)
        lines.append(str(text()) if callable(text) else str(issue))
    return "\n".join(lines)


def label_file(tmp_path: Any, stem: str, text: str) -> FrozenLabel:
    folder = tmp_path / "labels"
    folder.mkdir(exist_ok=True)
    path = folder / (stem + ".txt")
    path.write_text(text, encoding="utf-8")
    return FrozenLabel(
        stem=stem,
        stage_path=str(path),
        sha256=SHA,
        size=len(text.encode("utf-8")),
        label_name=stem + ".txt",
    )


# ------------------------------------------------------------- CT25


def test_ct25_an_empty_rectangle_blocks():
    """CT25: a rectangle with an empty points list is illegal."""

    schema = validate_label_schema(
        {
            "imageWidth": 10,
            "imageHeight": 10,
            "shapes": [shape("person", "rectangle", [])],
        },
        "hbb",
        list(CLASSES),
    )
    assert schema.ok is False
    assert schema.blocking is True
    assert "rectangle" in schema.reason and "2" in schema.reason
    assert "{2, 4}" in schema.reason or "2, 4" in schema.reason


def test_ct25_a_single_point_rectangle_blocks():
    """CT25: a one point rectangle is illegal (the converter indexes 2)."""

    schema = validate_label_schema(
        {
            "imageWidth": 10,
            "imageHeight": 10,
            "shapes": [shape("person", "rectangle", [[1, 2]])],
        },
        "hbb",
        list(CLASSES),
    )
    assert schema.ok is False
    assert schema.blocking is True
    assert "实际 1" in schema.reason


def test_ct25_a_two_point_polygon_blocks():
    """CT25: a two point polygon is illegal (spec §5.2.4 says skip)."""

    schema = validate_label_schema(
        {
            "imageWidth": 10,
            "imageHeight": 10,
            "shapes": [shape("person", "polygon", [[1, 2], [3, 4]])],
        },
        "seg",
        list(CLASSES),
    )
    assert schema.ok is False
    assert schema.blocking is True
    assert "polygon" in schema.reason
    assert "实际 2" in schema.reason and "3" in schema.reason


def test_ct25_a_three_point_polygon_is_legal_and_one_point_skips():
    """The rest of the cardinality table is unchanged (spec §5.2.4)."""

    legal = validate_label_schema(
        {
            "imageWidth": 10,
            "imageHeight": 10,
            "shapes": [shape("person", "polygon", [[1, 2], [3, 4], [5, 6]])],
        },
        "seg",
        list(CLASSES),
    )
    assert legal.ok is True and legal.detected == 1

    skipped = validate_label_schema(
        {
            "imageWidth": 10,
            "imageHeight": 10,
            "shapes": [
                shape("person", "polygon", [[1, 2]]),
                shape("person", "rectangle", [[1, 2], [3, 4]]),
            ],
        },
        "seg",
        list(CLASSES),
    )
    assert skipped.ok is True
    assert skipped.skipped == 2


def test_ct25_a_two_point_rectangle_still_converts(tmp_path):
    """A 2 point rectangle is legal (the deprecated diagonal mode)."""

    schema = validate_label_schema(
        {
            "imageWidth": 10,
            "imageHeight": 10,
            "shapes": [shape("person", "rectangle", [[1, 2], [3, 4]])],
        },
        "hbb",
        list(CLASSES),
    )
    assert schema.ok is True
    assert schema.detected == 1
    assert schema.downgraded_rectangles == 1


@pytest.mark.parametrize(
    "points,reason",
    [
        ([], "rectangle 的点数不在 {2, 4} 内（实际 0）"),
        ([[1, 2]], "rectangle 的点数不在 {2, 4} 内（实际 1）"),
    ],
)
def test_ct25_the_pipeline_blocks_before_any_request(
    tmp_path, points, reason
):
    """CT25: the blocked dataset never reaches the upload path."""

    root = str(tmp_path / "dataset")
    os.makedirs(root)
    classes = write_classes(root)
    write_image(root, "0001.jpg")
    write_label(root, "0001", [shape("person", "rectangle", points)])
    write_image(root, "0002.jpg")
    write_label(
        root,
        "0002",
        [shape("person", "rectangle", [[0, 0], [1, 1], [1, 0], [0, 1]])],
    )
    staging = tmp_path / "staging"
    before = tree_fingerprint(root)

    pipeline = Pipeline(
        config=PipelineConfig(
            dataset_dir=root,
            classes_file=classes,
            task="Detect",
            val_ratio=0.5,
            seed=7,
        ),
        staging_parent=str(staging),
    )
    with pytest.raises(PipelineError) as info:
        pipeline.prepare()
    text = reasons(info.value)
    assert "0001.json" in text
    assert reason in text
    assert info.value.issues, "a blocked run must carry its reasons"
    # No staging work area was created and the dataset is byte identical.
    assert staging.exists() is False
    assert tree_fingerprint(root) == before


def test_ct25_a_two_point_polygon_blocks_through_the_pipeline(tmp_path):
    """CT25: the same block on the Segment task, with its reason."""

    root = str(tmp_path / "dataset")
    os.makedirs(root)
    classes = write_classes(root)
    write_image(root, "0001.jpg")
    write_label(root, "0001", [shape("person", "polygon", [[1, 2], [3, 4]])])
    write_image(root, "0002.jpg")
    write_label(
        root, "0002", [shape("person", "polygon", [[1, 2], [3, 4], [5, 6]])]
    )
    staging = tmp_path / "staging"
    before = tree_fingerprint(root)

    pipeline = Pipeline(
        config=PipelineConfig(
            dataset_dir=root,
            classes_file=classes,
            task="Segment",
            val_ratio=0.5,
            seed=7,
        ),
        staging_parent=str(staging),
    )
    with pytest.raises(PipelineError) as info:
        pipeline.prepare()
    text = reasons(info.value)
    assert "polygon" in text and "实际 2" in text
    assert staging.exists() is False
    assert tree_fingerprint(root) == before


# ------------------------------------------------------------- CT27


def test_ct27_the_root_scan_matrix_reports_every_row(tmp_path):
    """CT27: root only, one reason per row, the dataset is not written."""

    root = str(tmp_path / "dataset")
    os.makedirs(root)
    classes = write_classes(root)
    # A valid pair, and the four rows of the matrix that can coexist.
    write_image(root, "a.jpg")
    write_label(root, "a", [shape("person", "rectangle", [[0, 0], [1, 1]])])
    write_image(root, "b.jpg")
    write_image(root, "c.jpg")
    write_image(root, "c.png")
    write_label(root, "c", [shape("person", "rectangle", [[0, 0], [1, 1]])])
    os.makedirs(osp.join(root, "sub"))
    write_image(osp.join(root, "sub"), "d.jpg")
    with open(osp.join(root, "notes.txt"), "w", encoding="utf-8") as handle:
        handle.write("not an image, not a label\n")
    before = tree_fingerprint(root)

    scan = scan_dataset(root, classes_file=classes, task="Detect")
    assert scan.blocked is True
    messages = "\n".join(scan.blocking_messages())
    codes = [issue.code for issue in scan.blocking()]
    assert "IMAGE_WITHOUT_LABEL" in codes
    assert "- b.jpg" in messages
    assert "STEM_COLLISION" in codes
    assert "- c.jpg / c.png" in messages
    assert "SUBDIRECTORY" in codes
    assert "- sub/d.jpg" in messages
    # A root file outside the whitelist is ignored, not reported.
    assert "notes.txt" not in messages
    assert [pair.name for pair in scan.pairs] == ["a.jpg"]
    # Nothing was written into the dataset directory.
    assert tree_fingerprint(root) == before


def test_ct27_the_classes_and_empty_dataset_rows_block(tmp_path):
    """CT27: the classes row and the empty dataset row, with reasons."""

    empty = str(tmp_path / "empty")
    os.makedirs(empty)
    scan = scan_dataset(
        empty,
        classes_file=str(tmp_path / "missing_classes.txt"),
        task="Detect",
    )
    messages = "\n".join(scan.blocking_messages())
    assert "类别表 classes.txt 为空或不可读" in messages
    assert "所选目录中没有找到图片" in messages

    blank = str(tmp_path / "blank")
    os.makedirs(blank)
    classes = write_classes(blank, classes=("",))
    write_image(blank, "a.jpg")
    write_label(blank, "a", [shape("person", "rectangle", [[0, 0], [1, 1]])])
    scan = scan_dataset(blank, classes_file=classes, task="Detect")
    messages = "\n".join(scan.blocking_messages())
    assert "类别表 classes.txt 为空或不可读" in messages


def test_ct27_a_corrupt_label_reports_its_own_reason(tmp_path):
    """CT27: the corrupt row lists the file with a readable reason."""

    root = str(tmp_path / "dataset")
    os.makedirs(root)
    classes = write_classes(root)
    write_image(root, "a.jpg")
    with open(osp.join(root, "a.json"), "w", encoding="utf-8") as handle:
        handle.write("{not json")
    write_image(root, "b.jpg")
    write_label(root, "b", [shape("person", "rectangle", [[0, 0], [1, 1]])])
    before = tree_fingerprint(root)

    scan = scan_dataset(root, classes_file=classes, task="Detect")
    messages = "\n".join(scan.blocking_messages())
    assert "CORRUPT_LABEL" in [issue.code for issue in scan.blocking()]
    assert "- a.json（JSON 解析失败" in messages
    assert tree_fingerprint(root) == before


def test_ct27_a_clean_dataset_passes_the_matrix(tmp_path):
    """The positive control: no issue, and the split reaches the run."""

    root = str(tmp_path / "dataset")
    os.makedirs(root)
    classes = write_classes(root)
    for name in ("0001", "0002"):
        write_image(root, name + ".jpg")
        write_label(
            root,
            name,
            [shape("person", "rectangle", [[0, 0], [1, 1]])],
        )
    before = tree_fingerprint(root)
    staging = tmp_path / "staging"

    pipeline = Pipeline(
        config=PipelineConfig(
            dataset_dir=root,
            classes_file=classes,
            task="Detect",
            val_ratio=0.5,
            seed=7,
        ),
        staging_parent=str(staging),
    )
    run = pipeline.prepare()
    assert run.files == ["0001.jpg", "0002.jpg"]
    assert run.split_check().blocked is False
    assert tree_fingerprint(root) == before
    # The staging work area lives outside the dataset (spec §5.3.1).
    assert osp.isdir(str(run.staging_dir)) is True
    assert not str(run.staging_dir).startswith(root)


def test_ct27_the_frozen_label_cardinality_assertion_blocks_the_pack(
    tmp_path,
):
    """The packer assertion on the bytes that really enter the zip."""

    two_points = label_file(tmp_path, "0001", "0 0.1 0.2 0.3 0.4")
    three_points = label_file(
        tmp_path, "0002", "0 0.1 0.2 0.3 0.4 0.5 0.6"
    )
    seg_run = PackerRun(task="segment", classes=["person"])
    bad = label_line_cardinality_issues(seg_run, {"0001": two_points})
    assert len(bad) == 1
    assert bad[0].code == "LABEL_POINTS_CARDINALITY"
    assert bad[0].blocking is True
    assert "- 0001:1" in bad[0].details[0]
    assert "2 点" in bad[0].details[0]
    assert label_line_cardinality_issues(
        seg_run, {"0002": three_points}
    ) == []

    short_row = label_file(tmp_path, "0003", "0 0.1 0.2 0.3")
    hbb_run = PackerRun(task="detect", classes=["person"])
    bad = label_line_cardinality_issues(hbb_run, {"0003": short_row})
    assert len(bad) == 1
    assert "rectangle 行应为 5 字段，实际 4" in bad[0].details[0]
    hbb_ok = label_file(tmp_path, "0004", "0 0.5 0.5 0.2 0.2")
    assert label_line_cardinality_issues(
        hbb_run, {"0004": hbb_ok}
    ) == []

    review = review_plan(
        seg_run,
        {"0001": two_points},
        {"missing_images": [], "rejected": []},
    )
    assert review.blocked is True
    assert "LABEL_POINTS_CARDINALITY" in [
        issue.code for issue in review.issues
    ]


def test_ct27_the_scan_reads_a_label_once(tmp_path):
    """The root scan only reads: the label bytes are unchanged."""

    root = str(tmp_path / "dataset")
    os.makedirs(root)
    classes = write_classes(root)
    write_image(root, "0001.jpg")
    label = write_label(
        root, "0001", [shape("person", "rectangle", [[0, 0], [1, 1]])]
    )
    with open(label, "rb") as handle:
        before = handle.read()
    scan_dataset(root, classes_file=classes, task="Detect")
    with open(label, "rb") as handle:
        assert handle.read() == before
