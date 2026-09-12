"""Export switch matrix and report tests."""

import json
import os
import os.path as osp
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

from anylabeling.custom.model_validation import dataset
from anylabeling.custom.model_validation import records as records_module
from anylabeling.custom.model_validation.exporter import export_zip
from anylabeling.custom.model_validation.report import build_report


def write_image(path: str) -> None:
    import cv2

    os.makedirs(osp.dirname(path), exist_ok=True)
    image = np.zeros((10, 12, 3), dtype=np.uint8)
    image[:, :, 2] = 120
    ok, buffer = cv2.imencode(".png", image)
    assert ok
    buffer.tofile(path)


def write_label(path: str, image_name: str) -> None:
    payload = {
        "version": "3.0.0",
        "flags": {},
        "checked": False,
        "shapes": [
            {
                "label": "car",
                "shape_type": "rectangle",
                "points": [[1, 1], [5, 1], [5, 5], [1, 5]],
                "group_id": None,
                "description": "",
                "flags": {},
                "direction": 0.0,
            }
        ],
        "imagePath": image_name,
        "imageData": None,
        "imageHeight": 10,
        "imageWidth": 12,
    }
    dataset.write_json(path, payload)


def build_records(tmp_path):
    staging = str(tmp_path / "staging")
    originals = []
    for name in ("a", "b"):
        paths = dataset.staging_paths(staging, "original", name + ".png")
        write_image(paths["image"])
        write_label(paths["label"], name + ".png")
        originals.append(
            records_module.make_record(
                "original",
                name + ".png",
                paths["image"],
                paths["label"],
                source_display="/source",
            )
        )
    children = []
    for index, parent in enumerate(originals, start=1):
        relpath = f"{osp.splitext(parent.relpath)[0]}_aug1.png"
        paths = dataset.staging_paths(staging, "augmented", relpath)
        write_image(paths["image"])
        write_label(paths["label"], relpath)
        child = records_module.make_record(
            "augmented",
            relpath,
            paths["image"],
            paths["label"],
            source_display="/source",
            parent_record_id=parent.record_id,
        )
        child.aug_detail = {"clipped": [], "copy_index": 1}
        children.append(child)
    return staging, originals, children


def expected_namelist(include_originals, include_augmented):
    "The names of one export: classes.txt and one pair per record."

    names = {"classes.txt"}
    for relpath in list(include_originals) + list(include_augmented):
        stem = osp.splitext(relpath)[0]
        names.add("images/" + relpath)
        names.add("images/" + stem + ".json")
    return names


def run_matrix(tmp_path, deleted, selected):
    staging, originals, children = build_records(tmp_path)
    records = originals + children
    for record in originals:
        record.deleted = record.relpath.split(".")[0] in deleted
    for record in children:
        record.include_in_export = record.relpath.split("_aug")[0] in selected
    report = build_report(
        staging,
        records,
        model_info={"task": "detect"},
        classes=["car"],
        config={"augment_enabled": True},
    )
    zip_path = str(tmp_path / "export.zip")
    summary = export_zip(records, staging, zip_path, ["car"], report)
    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
        classes_bytes = archive.read("classes.txt")
        image_bytes = archive.read("images/b.png")
    return summary, names, classes_bytes, image_bytes, records


def test_export_matrix(tmp_path):
    summary, names, classes_bytes, _image, _records = run_matrix(
        tmp_path, deleted=set(), selected=set()
    )
    assert names == expected_namelist(["a.png", "b.png"], [])
    assert classes_bytes == b"car\n"
    # one picture and one json per exported record, nothing else
    assert summary["zip_entries"] == 2 * 2
    assert summary["renamed_entries"] == 0
    assert summary["originals"] == 2
    assert summary["augmented"] == 0
    assert summary["excluded_unselected_augmented"] == 2


def test_export_matrix_parent_deleted_and_child_selected(tmp_path):
    summary, names, _classes, _image, _records = run_matrix(
        tmp_path, deleted={"a"}, selected={"a"}
    )
    assert names == expected_namelist(["b.png"], [])
    assert summary["originals"] == 1
    assert summary["augmented"] == 0
    assert summary["excluded_deleted_parent_augmented"] == 1


def test_export_matrix_child_selected_parent_alive(tmp_path):
    summary, names, _classes, _image, _records = run_matrix(
        tmp_path, deleted=set(), selected={"a"}
    )
    assert names == expected_namelist(["a.png", "b.png"], ["a_aug1.png"])
    assert summary["augmented"] == 1
    assert summary["excluded_deleted_parent_augmented"] == 0
    assert summary["excluded_unselected_augmented"] == 1


def test_zip_bytes_match_staging(tmp_path):
    "Every entry of the archive is the byte for byte staging file."

    staging, originals, children = build_records(tmp_path)
    records = originals + children
    for record in children:
        record.include_in_export = True
    report = build_report(staging, records, classes=["car"])
    zip_path = str(tmp_path / "export.zip")
    export_zip(records, staging, zip_path, ["car"], report)
    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
        for record in records:
            image_name = "images/" + record.relpath
            assert image_name in names
            with open(record.staging_image_path, "rb") as handle:
                assert archive.read(image_name) == handle.read()
            label_name = "images/" + osp.splitext(record.relpath)[0] + ".json"
            assert label_name in names
            with open(record.staging_label_path, "rb") as handle:
                assert archive.read(label_name) == handle.read()
        # the one json of a picture is the xlabel document itself
        payload = json.loads(archive.read("images/a.json").decode("utf-8"))
        assert payload["imageData"] is None
        assert payload["shapes"][0]["shape_type"] == "rectangle"
        assert payload["shapes"][0]["points"] == [
            [1, 1],
            [5, 1],
            [5, 5],
            [1, 5],
        ]
        assert payload["imagePath"] == "a.png"


def test_report_statistics_match_records(tmp_path):
    staging, originals, children = build_records(tmp_path)
    records = originals + children
    originals[0].deleted = True
    children[1].include_in_export = True
    report = build_report(staging, records, classes=["car"])
    summary = report["summary"]
    assert summary["originals"] == 1
    assert summary["augmented"] == 1
    assert summary["excluded_deleted_originals"] == 1
    assert summary["excluded_unselected_augmented"] == 0
    assert summary["excluded_deleted_parent_augmented"] == 1
    assert summary["total_records"] == 4
    assert summary["verdicts"] == {"PENDING": 4}
    assert report["export_path"] is None
    record_ids = {entry["record_id"] for entry in report["records"]}
    assert record_ids == {record.record_id for record in records}
    assert report["labelme_known_limitations"]


def test_report_model_section_records_both_name_lists(tmp_path):
    "The report keeps the embedded names and the class table side by side."

    staging, originals, children = build_records(tmp_path)
    report = build_report(
        staging,
        originals + children,
        model_info={
            "task": "detect",
            "names": ["class_0", "class_1"],
            "names_model": ["class_0", "class_1"],
            "classes": ["car", "van"],
            "classes_count_match": True,
            "classes_name_diff": ["0: model='class_0' vs txt='car'"],
        },
        classes=["car", "van"],
    )
    model = report["model"]
    assert model["names_model"] == ["class_0", "class_1"]
    assert model["names"] == ["class_0", "class_1"]
    assert model["classes"] == ["car", "van"]
    assert model["classes_count_match"] is True
    assert model["classes_name_diff"] == ["0: model='class_0' vs txt='car'"]
    # the effective table is exported as well
    assert report["classes"] == ["car", "van"]


def test_report_model_section_falls_back_to_the_class_table(tmp_path):
    "A plain model_info still carries the effective class table."

    staging, originals, _children = build_records(tmp_path)
    report = build_report(
        staging,
        originals,
        model_info={"task": "detect"},
        classes=["car"],
    )
    model = report["model"]
    assert model["task"] == "detect"
    assert model["classes"] == ["car"]
    assert model["names_model"] == []
    assert model["classes_name_diff"] == []


def test_report_truncates_a_long_name_diff(tmp_path):
    "The stored diff never grows with the number of classes."

    staging, originals, _children = build_records(tmp_path)
    diff = [
        f"{index}: model='c{index}' vs txt='r{index}'" for index in range(9)
    ]
    report = build_report(
        staging,
        originals,
        model_info={"classes_name_diff": list(diff)},
        classes=["car"],
    )
    stored = report["model"]["classes_name_diff"]
    assert len(stored) == 6
    assert stored[:5] == diff[:5]
    assert stored[-1] == "…"


def test_report_lists_the_excluded_unreadable_pairs(tmp_path):
    staging, originals, children = build_records(tmp_path)
    report = build_report(
        staging,
        originals + children,
        classes=["car"],
        staging_meta={"unreadable_label_pairs": ["broken.png"]},
    )
    excluded = report["excluded_pairs"]
    assert excluded["unreadable_label_pairs"] == ["broken.png"]
    assert "excluded" in excluded["note"]
    assert (
        build_report(staging, [])["excluded_pairs"]["unreadable_label_pairs"]
        == []
    )


def test_skipped_no_label_record_is_not_exported(tmp_path):
    staging, originals, children = build_records(tmp_path)
    skipped_paths = dataset.staging_paths(staging, "original", "c.png")
    write_image(skipped_paths["image"])
    skipped = records_module.make_record(
        "original", "c.png", skipped_paths["image"], ""
    )
    skipped.verdict = records_module.SKIPPED
    skipped.reasons = ["NO_LABEL"]
    records = originals + children + [skipped]
    report = build_report(staging, records, classes=["car"])
    zip_path = str(tmp_path / "export.zip")
    summary = export_zip(records, staging, zip_path, ["car"], report)
    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
    assert names == expected_namelist(["a.png", "b.png"], [])
    assert not any("c.png" in name for name in names)
    assert summary["originals"] == 2
    assert summary["skipped_no_label"] == 1
    assert summary["excluded_missing_staging_files"] == 0
    assert report["summary"]["originals"] == 2
    assert report["summary"]["skipped_no_label"] == 1
    assert report["export_selection"]["originals"] == [
        original.record_id for original in originals
    ]
    payloads = {entry["record_id"]: entry for entry in report["records"]}
    assert payloads[skipped.record_id]["in_export"] is False


def test_include_in_export_defaults_to_false(tmp_path):
    _staging, _originals, children = build_records(tmp_path)
    for child in children:
        assert child.include_in_export is False


def test_the_report_stays_out_of_the_archive_and_of_the_staging(tmp_path):
    """An export writes the archive, nothing else.

    The validation report is not part of the delivery any more: it is
    neither written into the zip nor onto the staging folder. The
    document the caller handed over is left exactly as it was found -
    the report layer itself (report.build_report, dataset.REPORT_FILENAME)
    is untouched, it is only the export that stopped using it.
    """

    staging, originals, children = build_records(tmp_path)
    records = originals + children
    report = build_report(staging, records, classes=["car"])
    before = json.dumps(report, sort_keys=True, default=str)
    zip_path = str(tmp_path / "export.zip")
    export_zip(records, staging, zip_path, ["car"], report)

    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
    assert "validation_report.json" not in names
    assert not any("report" in name for name in names)
    # the staging folder keeps no report either
    assert not osp.isfile(osp.join(staging, dataset.REPORT_FILENAME))
    # and the document of the caller is not rewritten on the way out:
    # build_report leaves export_path at its own None and the export
    # never fills it in any more
    assert json.dumps(report, sort_keys=True, default=str) == before
    assert report["export_path"] is None
