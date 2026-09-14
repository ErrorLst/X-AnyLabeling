"""Record bookkeeping and LabelMe conversion tests."""

import json
import os
import os.path as osp

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from anylabeling.custom.model_validation import dataset
from anylabeling.custom.model_validation import records as records_module
from anylabeling.custom.model_validation.labelme_io import (
    LABELME_VERSION,
    SHAPE_TYPE_MAP,
    shape_to_labelme,
    to_labelme_dict,
)
from anylabeling.custom.model_validation.report import in_export


def test_shape_type_map_covers_every_supported_shape():
    for shape_type in (
        "rectangle",
        "rotation",
        "polygon",
        "quadrilateral",
        "point",
        "line",
        "linestrip",
        "circle",
        "cuboid",
    ):
        assert shape_type in SHAPE_TYPE_MAP


def test_rectangle_four_points_become_two():
    shape = {
        "label": "car",
        "shape_type": "rectangle",
        "points": [[10, 20], [30, 20], [30, 40], [10, 40]],
        "group_id": 3,
        "description": "note",
        "flags": {"a": True},
    }
    converted = shape_to_labelme(shape)
    assert converted["shape_type"] == "rectangle"
    assert converted["points"] == [[10.0, 20.0], [30.0, 40.0]]
    assert converted["group_id"] == 3
    assert converted["description"] == "note"
    assert converted["flags"] == {"a": True}
    assert converted["mask"] is None


def test_every_shape_type_is_mapped():
    cases = {
        "rotation": "polygon",
        "polygon": "polygon",
        "quadrilateral": "polygon",
        "point": "point",
        "line": "line",
        "linestrip": "linestrip",
        "circle": "circle",
        "cuboid": "polygon",
    }
    for shape_type, expected in cases.items():
        shape = {
            "label": "x",
            "shape_type": shape_type,
            "points": [[1, 2], [3, 4]],
        }
        assert shape_to_labelme(shape)["shape_type"] == expected


def test_to_labelme_dict_fields():
    label = {
        "version": "3.0.0",
        "flags": {"f": 1},
        "checked": True,
        "shapes": [
            {
                "label": "car",
                "shape_type": "rectangle",
                "points": [[0, 0], [4, 0], [4, 4], [0, 4]],
            }
        ],
        "imagePath": "old.png",
        "imageData": "base64",
        "imageHeight": 6,
        "imageWidth": 8,
        "custom": {"keep": 1},
    }
    document = to_labelme_dict(label, "/staging/a.png")
    assert document["version"] == LABELME_VERSION
    assert document["flags"] == {"f": 1}
    assert document["imagePath"] == "a.png"
    assert document["imageData"] is None
    assert document["imageHeight"] == 6
    assert document["imageWidth"] == 8
    assert len(document["shapes"]) == 1
    assert "custom" not in document


def test_to_labelme_dict_uses_provided_dimensions():
    label = {"shapes": [], "imageHeight": -1, "imageWidth": -1}
    document = to_labelme_dict(label, "/x/b.png", 20, 30)
    assert document["imageHeight"] == 20
    assert document["imageWidth"] == 30


def make_record(tmp_path, kind: str = "original", relpath: str = "a.png"):
    staging = str(tmp_path)
    paths = dataset.staging_paths(staging, kind, relpath)
    os.makedirs(osp.dirname(paths["image"]), exist_ok=True)
    os.makedirs(osp.dirname(paths["label"]), exist_ok=True)
    with open(paths["image"], "wb") as handle:
        handle.write(b"png")
    payload = {
        "version": "3.0.0",
        "flags": {},
        "checked": False,
        "shapes": [
            {
                "label": "car",
                "shape_type": "rectangle",
                "points": [[0, 0], [1, 1]],
                "custom": 5,
            }
        ],
        "imagePath": "a.png",
        "imageData": None,
        "imageHeight": 6,
        "imageWidth": 8,
        "extra_key": "keep-me",
    }
    dataset.write_json(paths["label"], payload)
    return records_module.make_record(
        kind, relpath, paths["image"], paths["label"]
    )


def test_read_and_write_staging_label_roundtrip(tmp_path):
    """Read, change, write, read again: no field of the label is lost.

    records.update_shape / records.update_shape_points are gone, so the
    staging json is changed the way the UI does it now: the label of a
    record is read (records.read_staging_label), one of its shapes is
    changed in memory and the whole document is written back
    (records.write_staging_label). The shape that was changed carries
    the new value and every other key - the shape keys this revision
    does not know (custom), the document keys of the label (imagePath,
    imageHeight, imageWidth) and a key another tool added (extra_key) -
    comes back untouched.
    """

    record = make_record(tmp_path)
    data = records_module.read_staging_label(record)
    assert data["extra_key"] == "keep-me"
    assert data["imagePath"] == "a.png"
    assert data["imageHeight"] == 6
    assert data["imageWidth"] == 8
    data["shapes"][0]["label"] = "bus"
    data["shapes"][0]["shape_type"] = "polygon"
    records_module.write_staging_label(record, data)
    refreshed = records_module.read_staging_label(record)
    assert refreshed["shapes"][0]["label"] == "bus"
    assert refreshed["shapes"][0]["shape_type"] == "polygon"
    assert refreshed["shapes"][0]["points"] == [[0, 0], [1, 1]]
    assert refreshed["shapes"][0]["custom"] == 5
    assert refreshed["imagePath"] == "a.png"
    assert refreshed["imageHeight"] == 6
    assert refreshed["imageWidth"] == 8
    assert refreshed["extra_key"] == "keep-me"


def test_staging_label_io_stays_in_the_staging_folder(tmp_path):
    """Both functions target the staging label of the record alone."""

    record = make_record(tmp_path)
    paths = dataset.staging_paths(str(tmp_path), record.kind, record.relpath)
    assert record.staging_label_path == paths["label"]
    records_module.write_staging_label(record, {"shapes": []})
    assert records_module.read_staging_label(record)["shapes"] == []
    # a record without a staging label answers None: the reader never
    # falls back to a source dataset path
    record.staging_label_path = ""
    assert records_module.read_staging_label(record) is None


def test_export_formula_and_defaults(tmp_path):
    original = make_record(tmp_path, "original", "a.png")
    parent_deleted = make_record(tmp_path, "original", "b.png")
    child = records_module.make_record(
        "augmented",
        "a_aug1.png",
        "/x/img",
        "/x/label",
        parent_record_id=original.record_id,
    )
    orphan = records_module.make_record(
        "augmented",
        "b_aug1.png",
        "/x/img",
        "/x/label",
        parent_record_id=parent_deleted.record_id,
    )
    records = [original, parent_deleted, child, orphan]
    assert child.include_in_export is False
    assert original.deleted is False

    selection = records_module.export_selection(records)
    assert selection["originals"] == [original, parent_deleted]
    assert selection["augmented"] == []

    records_module.set_include_in_export(records, [child.record_id], True)
    selection = records_module.export_selection(records)
    assert selection["augmented"] == [child]

    records_module.set_deleted(records, [original.record_id], True)
    selection = records_module.export_selection(records)
    assert selection["originals"] == [parent_deleted]
    assert selection["augmented"] == []
    assert selection["excluded_deleted_parent_augmented"] == [child]

    summary = records_module.export_summary(records)
    assert summary["originals"] == 1
    assert summary["augmented"] == 0
    assert summary["excluded_deleted_originals"] == 1
    assert summary["excluded_deleted_parent_augmented"] == 1
    assert summary["excluded_unselected_augmented"] == 1


def test_skipped_record_is_excluded_from_every_export_view(tmp_path):
    original = make_record(tmp_path, "original", "a.png")
    skipped = records_module.make_record(
        "original", "b.png", "/staging/b.png", ""
    )
    skipped.verdict = records_module.SKIPPED
    skipped.reasons = ["NO_LABEL"]
    records = [original, skipped]

    selection = records_module.export_selection(records)
    assert selection["originals"] == [original]
    assert selection["excluded_skipped_originals"] == [skipped]
    summary = records_module.export_summary(records)
    assert summary["originals"] == 1
    assert summary["augmented"] == 0
    assert summary["skipped_no_label"] == 1
    assert in_export(original, records) is True
    assert in_export(skipped, records) is False


def test_set_deleted_only_affects_originals(tmp_path):
    child = records_module.make_record("augmented", "a", "/i", "/l")
    assert records_module.set_deleted([child], [child.record_id], True) == []


def test_records_from_staging_reads_meta(tmp_path):
    staging = str(tmp_path / "staging")
    meta = {
        "staging_root": staging,
        "source_display": "/source",
        "counts": {},
        "originals": [
            {
                "relpath": "a.png",
                "staging_image_path": "/s/a.png",
                "staging_label_path": "/s/a.json",
            }
        ],
        "skipped": [
            {
                "relpath": "b.png",
                "staging_image_path": "/s/b.png",
                "reason": "NO_LABEL",
            }
        ],
    }
    dataset.write_json(osp.join(staging, dataset.META_FILENAME), meta)
    records = records_module.records_from_staging(staging, ["car"])
    assert len(records) == 2
    assert records[0].record_id == "original::a.png"
    assert records[0].source_display == "/source"
    assert records[1].verdict == records_module.SKIPPED
    assert records[1].reasons == ["NO_LABEL"]
    assert (
        records_module.record_lookup(records)[records[0].record_id]
        is records[0]
    )
