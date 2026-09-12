"""The export never crashes on a callback or a foreign object.

Regression of the crashing delivery path: the progress callback of the
results page used to be read as if it were the report document, and
json.dumps raised "Object of type function is not JSON serializable"
before the archive was written.
"""

import dataclasses
import json
import os
import os.path as osp
import pathlib
import threading
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from anylabeling.custom.model_validation import dataset
from anylabeling.custom.model_validation import records as records_module
from anylabeling.custom.model_validation import report as report_module
from anylabeling.custom.model_validation.exporter import (
    CANCELLED_MESSAGE,
    ExportCancelled,
    cancellation_requested,
    export_zip,
    resolve_export_arguments,
    write_zip,
)
from anylabeling.custom.model_validation.json_safe import (
    as_list,
    as_mapping,
    json_default,
    sanitize,
)
from anylabeling.custom.model_validation.report import build_report


def write_image(path: str) -> None:
    import cv2

    os.makedirs(osp.dirname(path), exist_ok=True)
    image = np.zeros((10, 12, 3), dtype=np.uint8)
    ok, buffer = cv2.imencode(".png", image)
    assert ok
    buffer.tofile(path)


def write_label(path: str, image_name: str) -> None:
    os.makedirs(osp.dirname(path), exist_ok=True)
    dataset.write_json(
        path,
        {
            "version": "3.0.0",
            "flags": {},
            "shapes": [
                {
                    "label": "car",
                    "shape_type": "rectangle",
                    "points": [[1, 1], [5, 1], [5, 5], [1, 5]],
                }
            ],
            "imagePath": image_name,
            "imageHeight": 10,
            "imageWidth": 12,
        },
    )


def build_records(tmp_path):
    """Create one staged original and one selected augmented child."""

    staging = str(tmp_path / "staging")
    parent_paths = dataset.staging_paths(staging, "original", "a.png")
    write_image(parent_paths["image"])
    write_label(parent_paths["label"], "a.png")
    parent = records_module.make_record(
        "original",
        "a.png",
        parent_paths["image"],
        parent_paths["label"],
        source_display="/source",
    )
    child_paths = dataset.staging_paths(staging, "augmented", "a_aug1.png")
    write_image(child_paths["image"])
    write_label(child_paths["label"], "a_aug1.png")
    child = records_module.make_record(
        "augmented",
        "a_aug1.png",
        child_paths["image"],
        child_paths["label"],
        source_display="/source",
        parent_record_id=parent.record_id,
    )
    child.include_in_export = True
    return staging, parent, child


def write_classes_file(staging: str) -> str:
    path = osp.join(staging, "classes.txt")
    os.makedirs(staging, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("car\nvan\n")
    return path


def zip_payload(zip_path: str):
    "Return (names, documents, classes) of one archive."

    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
        documents = {
            name: json.loads(archive.read(name).decode("utf-8"))
            for name in sorted(names)
            if name.endswith(".json")
        }
        classes = archive.read("classes.txt").decode("utf-8")
    return names, documents, classes


def test_the_documented_call_shape_with_callbacks_exports(tmp_path):
    """export_zip(records, staging, classes_file, zip, cb, flag) works."""

    staging, parent, child = build_records(tmp_path)
    classes_file = write_classes_file(staging)
    zip_path = str(tmp_path / "export.zip")
    seen = []
    cancel_flag = threading.Event()

    def on_progress(done, total, message):
        seen.append((done, total, message))

    summary = export_zip(
        [parent, child],
        staging,
        classes_file,
        zip_path,
        on_progress,
        cancel_flag.is_set,
    )

    names, documents, classes = zip_payload(zip_path)
    assert names == {
        "classes.txt",
        "images/a.png",
        "images/a.json",
        "images/a_aug1.png",
        "images/a_aug1.json",
    }
    assert classes == "car\nvan\n"
    # the one json of a picture is the xlabel document of its record
    assert documents["images/a.json"]["shapes"][0]["label"] == "car"
    assert documents["images/a_aug1.json"]["shapes"][0]["label"] == "car"
    assert summary["originals"] == 1
    assert summary["augmented"] == 1
    assert summary["zip_entries"] == 4
    assert summary["renamed_entries"] == 0
    assert summary["zip_path"] == zip_path
    assert seen[-1] == (2, 2, "Done")


def test_a_staged_report_is_not_reused_any_more(tmp_path):
    """An export writes no report into the archive, staged or built.

    A report already written into the staging folder by the report
    layer is left exactly where it is: the archive of this revision is
    the class table plus the images/ folder, so the documents it holds
    are the annotations of the exported pictures and nothing else.
    """

    staging, parent, child = build_records(tmp_path)
    classes_file = write_classes_file(staging)
    staged = build_report(
        staging,
        [parent, child],
        model_info={"task": "detect", "sha256": "abc"},
        classes=["car", "van"],
    )
    report_module.write_report(staging, staged)
    zip_path = str(tmp_path / "export.zip")

    export_zip(
        [parent, child], staging, classes_file, zip_path, lambda *args: None
    )

    names, documents, _classes = zip_payload(zip_path)
    assert names == {
        "classes.txt",
        "images/a.png",
        "images/a.json",
        "images/a_aug1.png",
        "images/a_aug1.json",
    }
    # the documents of the archive are the annotations, never the report
    assert set(documents) == {"images/a.json", "images/a_aug1.json"}
    assert "validation_report.json" not in names
    # the staged report itself is untouched, and no second copy of it is
    # written next to it by the export
    staged_path = osp.join(staging, report_module.STAGING_REPORT_FILENAME)
    assert dataset.read_json(staged_path)["model"]["sha256"] == "abc"
    assert not osp.isfile(osp.join(staging, dataset.REPORT_FILENAME))


def test_a_callback_inside_the_report_never_breaks_the_export(tmp_path):
    """A callback, a Path or a numpy value in the report degrades to text."""

    staging, parent, child = build_records(tmp_path)
    zip_path = str(tmp_path / "export.zip")

    def on_progress(done, total, message):
        return None

    dirty = {
        "version": 1,
        "staging_root": tmp_path / "staging",
        "progress": on_progress,
        "cancel": threading.Event().is_set,
        "classes": np.array(["car"]),
        "score": np.float32(0.25),
        "size": np.int64(3),
        "flags": {"b", "a"},
        "rows": (1, 2),
        "raw": b"bytes",
        "records": [{"detail": {"callback": lambda: None}}],
    }
    summary = export_zip([parent, child], staging, zip_path, ["car"], dirty)

    names, documents, classes = zip_payload(zip_path)
    assert classes == "car\n"
    assert names == {
        "classes.txt",
        "images/a.png",
        "images/a.json",
        "images/a_aug1.png",
        "images/a_aug1.json",
    }
    # none of the foreign objects can reach the archive any more: the
    # documents it holds are the two annotations of the two records
    assert set(documents) == {"images/a.json", "images/a_aug1.json"}
    assert all("progress" not in document for document in documents.values())
    # the document of the caller is neither sanitised nor written
    assert "export_path" not in dirty
    assert dirty["progress"] is on_progress
    assert not osp.isfile(osp.join(staging, dataset.REPORT_FILENAME))
    assert summary["zip_entries"] == 4


def test_write_zip_writes_the_two_files_of_an_entry(tmp_path):
    """write_zip writes the names it is given, and nothing else.

    The two names of an entry travel in the entry itself (see
    exporter.build_export_entries), so the writer never derives a path
    and never builds a second document: one picture, one annotation, and
    the class table at the root. A report handed over positionally is
    accepted and ignored.
    """

    staging, parent, _child = build_records(tmp_path)
    zip_path = str(tmp_path / "labels.zip")

    written = write_zip(
        [
            {
                "record": parent,
                "image_name": "images/a.png",
                "json_name": "images/a.json",
            }
        ],
        zip_path,
        ["car"],
        {"progress": lambda: None, "note": tmp_path},
    )

    assert written == {
        "entries": 1,
        "zip_entries": 2,
        "renames": [],
        "renamed_entries": 0,
    }
    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
        stored = json.loads(archive.read("images/a.json").decode("utf-8"))
        with open(parent.staging_image_path, "rb") as handle:
            image = handle.read()
        assert archive.read("images/a.png") == image
    assert names == {"classes.txt", "images/a.png", "images/a.json"}
    # the annotation is the staging xlabel document, byte for byte
    payload = dataset.read_json(parent.staging_label_path)
    assert stored == payload


def test_build_report_only_holds_json_primitives(tmp_path):
    """Every section of the report is rebuilt with JSON values only."""

    staging, parent, child = build_records(tmp_path)

    def callback():
        return None

    report = build_report(
        staging,
        [parent, child],
        model_info={
            "task": "detect",
            "names": np.array(["car"]),
            "callback": callback,
        },
        classes="car",
        config={"path": tmp_path, "callback": callback},
        augment_summary={
            "params": {"scale": (0.5, 1.5)},
            "seed": np.int64(0),
        },
        staging_meta={"unreadable_label_pairs": "broken.png"},
        warnings=callback,
        previous_staging_roots=tmp_path,
        export_path=tmp_path / "x.zip",
    )

    text = json.dumps(report, ensure_ascii=False)
    assert "callback" in text
    # a lone class name stays one class instead of becoming characters
    assert report["classes"] == ["car"]
    assert report["model"]["names_model"] == ["car"]
    assert report["model"]["callback"] == "<callable callback>"
    assert report["config"]["path"] == str(tmp_path)
    assert report["config"]["callback"] == "<callable callback>"
    assert report["augment"]["params"]["scale"] == [0.5, 1.5]
    assert report["augment"]["seed"] == 0
    assert report["disabled_multi_image_augmentations"] == []
    assert report["excluded_pairs"]["unreadable_label_pairs"] == ["broken.png"]
    assert report["warnings"] == ["<callable callback>"]
    assert report["previous_staging_roots"] == [str(tmp_path)]
    assert report["export_path"] == str(tmp_path / "x.zip")


@dataclasses.dataclass
class Payload:
    """Stand in for a record or a parameter snapshot."""

    name: str
    size: int


def test_json_safe_degrades_every_foreign_value():
    """The sanitizer converts the types the report may receive."""

    def callback(one, two):
        return None

    sanitized = sanitize(
        {
            "path": pathlib.Path("tmp") / "x.zip",
            "count": np.int64(3),
            "score": np.float32(0.5),
            "array": np.array([1, 2]),
            "matrix": np.zeros((2, 2)),
            "callback": callback,
            "generator": (item for item in [1]),
            "flags": {"b", "a"},
            "pair": ("x", "y"),
            "raw": b"abc",
            "none": None,
            "flag": True,
            "payload": Payload("a", 1),
            3: "int key",
            (1, 2): "tuple key",
        }
    )

    assert sanitized["path"] == os.path.join("tmp", "x.zip")
    assert sanitized["count"] == 3
    assert sanitized["score"] == 0.5
    assert sanitized["array"] == [1, 2]
    assert sanitized["matrix"] == [[0.0, 0.0], [0.0, 0.0]]
    assert sanitized["callback"] == "<callable callback>"
    assert sanitized["generator"] == "<generator>"
    assert sanitized["flags"] == ["a", "b"]
    assert sanitized["pair"] == ["x", "y"]
    assert sanitized["raw"] == "abc"
    assert sanitized["none"] is None
    assert sanitized["flag"] is True
    assert sanitized["payload"] == {"name": "a", "size": 1}
    assert sanitized[3] == "int key"
    assert sanitized["(1, 2)"] == "tuple key"
    assert json.dumps(sanitized) == json.dumps(sanitize(sanitized))
    assert as_list("car") == ["car"]
    assert as_list(None) == []
    assert as_mapping("car") == {}
    assert as_mapping({"a": 1}) == {"a": 1}
    assert json_default(callback) == "<callable callback>"
    assert json.dumps({"cb": callback}, default=json_default) == (
        '{"cb": "<callable callback>"}'
    )


def test_the_argument_resolution_tells_the_two_shapes_apart():
    """The report position decides which call shape was used."""

    def on_progress(done, total, message):
        return None

    flag = threading.Event()
    explicit = resolve_export_arguments(
        "out.zip", ["car"], {"version": 1}, on_progress, flag.is_set
    )
    assert explicit["zip_path"] == "out.zip"
    assert explicit["classes"] == ["car"]
    assert explicit["report"] == {"version": 1}
    assert explicit["progress"] is on_progress
    assert explicit["is_cancelled"] == flag.is_set

    shifted = resolve_export_arguments(
        "classes.txt", "out.zip", on_progress, flag.is_set, None
    )
    assert shifted["zip_path"] == "out.zip"
    assert shifted["classes_file"] == "classes.txt"
    assert shifted["report"] is None
    assert shifted["progress"] is on_progress
    assert shifted["is_cancelled"] == flag.is_set


def test_cancellation_requested_reads_callables_and_flags(tmp_path):
    """A cancel callback or a flag object both stop an export."""

    assert cancellation_requested(None) is False
    assert cancellation_requested(lambda: False) is False
    assert cancellation_requested(lambda: True) is True
    assert cancellation_requested(False) is False
    event = threading.Event()
    assert cancellation_requested(event) is False
    event.set()
    assert cancellation_requested(event) is True
    assert cancellation_requested(True) is True

    staging, parent, child = build_records(tmp_path)
    zip_path = str(tmp_path / "cancelled.zip")
    with pytest.raises(ExportCancelled):
        export_zip(
            [parent, child],
            staging,
            zip_path,
            ["car"],
            {"version": 1},
            is_cancelled=event,
        )


def test_a_cancelled_export_carries_the_chinese_message(tmp_path):
    """The cancel error is the one user visible message of the export.

    The dialog prints it in the state line, therefore it is Chinese like
    the rest of the model validation user interface. Only the message is
    translated: the type keeps its English name, so every caller still
    tells a cancel from a failure with isinstance(error, ExportCancelled)
    and the progress lines of write_zip stay English as well.
    """

    staging, parent, child = build_records(tmp_path)
    zip_path = str(tmp_path / "cancel_message.zip")
    with pytest.raises(ExportCancelled) as info:
        export_zip(
            [parent, child],
            staging,
            zip_path,
            ["car"],
            {"version": 1},
            is_cancelled=lambda: True,
        )

    assert isinstance(info.value, ExportCancelled)
    assert str(info.value) == CANCELLED_MESSAGE == "用户已取消导出"
    assert "export cancelled" not in str(info.value)
