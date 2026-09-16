"""Invariant I-1: the source dataset is never touched after staging."""

import builtins
import json
import os
import os.path as osp
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from anylabeling.custom.model_validation import dataset
from anylabeling.custom.model_validation import history
from anylabeling.custom.model_validation import inference as inference_module
from anylabeling.custom.model_validation import records as records_module
from anylabeling.custom.model_validation.app_config import (
    AugmentParams,
    ValidationConfig,
)
from anylabeling.custom.model_validation.exporter import export_zip
from anylabeling.custom.model_validation.pipeline import ValidationWorker
from anylabeling.custom.model_validation.report import build_report

SANDBOX_SKIP_REASON = (
    "the sandbox forbids file IO inside a freshly created staging folder"
)


def make_staging_root(parent=None):
    """Create a staging root, skipping when the sandbox denies it."""

    from anylabeling.custom.model_validation import dataset as _dataset

    try:
        return _dataset.create_staging_root(parent)
    except PermissionError:
        pytest.skip(SANDBOX_SKIP_REASON)


PACKAGE_DIR = osp.dirname(osp.abspath(inference_module.__file__))
TESTS_DIR = osp.dirname(osp.abspath(__file__))
TEXT_SUFFIXES = (".py", ".toml", ".cfg", ".txt")


def write_pair(root: str, name: str) -> None:
    import cv2

    image_path = osp.join(root, name + ".png")
    label_path = osp.join(root, name + ".json")
    image = np.zeros((32, 40, 3), dtype=np.uint8)
    image[:, :, 1] = 180
    os.makedirs(root, exist_ok=True)
    ok, buffer = cv2.imencode(".png", image)
    assert ok
    buffer.tofile(image_path)
    payload = {
        "version": "3.0.0",
        "flags": {},
        "checked": False,
        "shapes": [
            {
                "label": "car",
                "shape_type": "rectangle",
                "points": [[4, 4], [20, 4], [20, 20], [4, 20]],
                "group_id": None,
                "description": "",
                "flags": {},
            }
        ],
        "imagePath": name + ".png",
        "imageData": None,
        "imageHeight": 32,
        "imageWidth": 40,
    }
    dataset.write_json(label_path, payload)


class FakeRunner:
    """Stand-in for ModelRunner that returns the ground truth shapes."""

    warnings = []

    def __init__(self, model_path, classes, **kwargs):
        self.model_path = model_path
        self.classes = list(classes)
        self.calls = []

    def predict(self, image_path):
        self.calls.append(image_path)
        return [
            {
                "label": "car",
                "shape_type": "rectangle",
                "points": [[4, 4], [20, 4], [20, 20], [4, 20]],
                "score": 0.9,
            }
        ]

    def model_info(self):
        return {"path": self.model_path, "task": "detect"}


def build_config(
    source: str, model_path: str, augment_enabled: bool
) -> ValidationConfig:
    return ValidationConfig(
        dataset_dir=source,
        model_path=model_path,
        augment_enabled=augment_enabled,
        augment_mode="count",
        total_count=3,
        judge_augmented=True,
        augment_params=AugmentParams(
            degrees=10.0,
            translate=0.1,
            scale_min=0.9,
            scale_max=1.1,
            fliplr=True,
            # a probability of one keeps generated equal to planned: no
            # try of this module is wasted by an empty draw
            select_prob=1.0,
            seed=1234,
        ),
    )


def test_source_directory_is_untouched_after_staging(
    tmp_path, mv_scratch, monkeypatch
):
    pytest.importorskip("albumentations")
    source = str(tmp_path / "source")
    write_pair(source, "a")
    write_pair(source, "b")
    model_path = str(tmp_path / "model.onnx")
    with open(model_path, "wb") as handle:
        handle.write(b"not-a-real-model")

    before = dataset.snapshot_directory(source)
    staging = make_staging_root(mv_scratch)
    worker = ValidationWorker(
        build_config(source, model_path, True), ["car"], staging
    )
    worker._stage_staging()

    hidden = source + "__hidden"
    os.rename(source, hidden)

    real_open = builtins.open
    source_prefix = osp.abspath(source).lower()

    def guarded_open(file, *args, **kwargs):
        target = osp.abspath(str(file)).lower()
        if target.startswith(source_prefix):
            raise AssertionError("source dataset accessed: " + str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(inference_module, "ModelRunner", FakeRunner)
    try:
        worker._stage_augment()
        worker._stage_infer()
        records = worker.records
        assert len(records) == 5
        report = build_report(
            staging,
            records,
            model_info=worker.model_info,
            classes=["car"],
            config=worker.config.to_dict(),
            augment_summary=worker.augment_summary,
        )
        for record in records:
            if record.kind == records_module.KIND_AUGMENTED:
                record.include_in_export = True
        zip_path = str(tmp_path / "export.zip")
        summary = export_zip(records, staging, zip_path, ["car"], report)
        assert summary["originals"] == 2
        assert summary["augmented"] == 3
        assert osp.isfile(zip_path)
    finally:
        monkeypatch.undo()
        os.rename(hidden, source)

    after = dataset.snapshot_directory(source)
    assert after == before


def test_static_module_scanning_rules():
    """Neither the package nor the tests may delete a file."""

    import re

    offenders = []
    forbidden_call = re.compile(r"(shutil\.rmtree|os\.remove|os\.unlink)\s*\(")
    access_terms = ("dataset_dir", "source_dir")
    allowed_access = {
        osp.join(PACKAGE_DIR, "dataset.py"),
        osp.join(PACKAGE_DIR, "report.py"),
        osp.join(PACKAGE_DIR, "app_config.py"),
        osp.join(PACKAGE_DIR, "pipeline.py"),
        osp.join(PACKAGE_DIR, "ui", "config_page.py"),
        osp.join(PACKAGE_DIR, "ui", "dialog.py"),
    }

    def python_files(root):
        for current, _dirs, names in os.walk(root):
            if "__pycache__" in current:
                continue
            for name in sorted(names):
                if name.endswith(".py"):
                    yield osp.join(current, name)

    def read(path):
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    for path in python_files(PACKAGE_DIR):
        text = read(path)
        if forbidden_call.search(text):
            offenders.append("destructive call in " + path)
        if osp.basename(path) == "exporter.py":
            for term in access_terms:
                if term in text:
                    offenders.append("exporter references " + term)
        if path in allowed_access:
            continue
        for term in access_terms:
            hit = any(
                term in line
                and term + ": str" not in line
                and term + "=" not in line
                for line in text.split(chr(10))
            )
            if hit:
                offenders.append(path + " references " + term)

    # the test suite must not delete either: its scratch folders are
    # moved into the temp trash folder instead.
    for path in python_files(TESTS_DIR):
        if forbidden_call.search(read(path)):
            offenders.append("destructive call in " + path)
    assert offenders == []


def test_package_import_does_not_load_albumentations():
    import subprocess

    script = ";".join(
        [
            "import sys",
            "sys.modules[" + repr("albumentations") + "] = None",
            "sys.modules[" + repr("onnxruntime") + "] = None",
            "import anylabeling.custom.model_validation as m",
            "from anylabeling.custom.model_validation.ui import dialog",
            "print(" + repr("IMPORT-OK") + ")",
        ]
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=os.getcwd(),
    )
    assert completed.returncode == 0, completed.stderr
    assert "IMPORT-OK" in completed.stdout


def staging_layout(scratch: str) -> str:
    """Create the staging folder layout the tool expects.

    dataset.create_staging_root relies on tempfile.mkdtemp and the
    sandbox of this workspace denies every write inside such a folder,
    therefore the layout is created with os.makedirs here.
    """

    staging = osp.join(scratch, dataset.STAGING_PREFIX + "manual")
    for folder in (dataset.ORIGINAL_DIRNAME, dataset.AUGMENTED_DIRNAME):
        for sub in (dataset.IMAGES_DIRNAME, dataset.LABELS_DIRNAME):
            os.makedirs(osp.join(staging, folder, sub), exist_ok=True)
    return staging


def staged_original(staging: str, relpath: str, with_label: bool = True):
    """Create one staged original record with or without a label."""

    import cv2

    paths = dataset.staging_paths(
        staging, records_module.KIND_ORIGINAL, relpath
    )
    os.makedirs(osp.dirname(paths["image"]), exist_ok=True)
    ok, buffer = cv2.imencode(".png", np.zeros((32, 40, 3), dtype=np.uint8))
    assert ok
    buffer.tofile(paths["image"])
    if not with_label:
        record = records_module.make_record(
            records_module.KIND_ORIGINAL, relpath, paths["image"], ""
        )
        record.verdict = records_module.SKIPPED
        record.reasons = ["NO_LABEL"]
        record.detail = {"skipped": True}
        return record
    dataset.write_json(
        paths["label"],
        {
            "version": "3.0.0",
            "flags": {},
            "checked": False,
            "shapes": [
                {
                    "label": "car",
                    "shape_type": "rectangle",
                    "points": [[4, 4], [20, 4], [20, 20], [4, 20]],
                    "group_id": None,
                    "description": "",
                    "flags": {},
                }
            ],
            "imagePath": osp.basename(relpath),
            "imageData": None,
            "imageHeight": 32,
            "imageWidth": 40,
        },
    )
    return records_module.make_record(
        records_module.KIND_ORIGINAL, relpath, paths["image"], paths["label"]
    )


class RecordingRunner:
    """Stand-in for ModelRunner that records every predicted image."""

    warnings = []
    calls = []

    def __init__(self, model_path, classes, **kwargs):
        self.model_path = model_path
        self.classes = list(classes)

    def predict(self, image_path):
        RecordingRunner.calls.append(image_path)
        return [
            {
                "label": "car",
                "shape_type": "rectangle",
                "points": [[4, 4], [20, 4], [20, 20], [4, 20]],
                "score": 0.9,
            }
        ]

    def model_info(self):
        return {"path": self.model_path, "task": "detect"}


def test_no_label_image_is_never_judged(tmp_path, monkeypatch):
    """A SKIPPED record keeps its verdict, the model never sees it."""

    staging = staging_layout(str(tmp_path))
    labelled = staged_original(staging, "a.png")
    skipped = staged_original(staging, "b.png", with_label=False)
    worker = ValidationWorker(
        build_config("/source", "/model.onnx", False), ["car"], staging
    )
    worker.records = [labelled, skipped]
    RecordingRunner.calls = []
    monkeypatch.setattr(inference_module, "ModelRunner", RecordingRunner)

    worker._stage_infer()

    assert RecordingRunner.calls == [labelled.staging_image_path]
    assert skipped.verdict == records_module.SKIPPED
    assert skipped.reasons == ["NO_LABEL"]
    assert skipped.judged is False
    assert labelled.judged is True
    assert labelled.verdict == records_module.OK


def test_no_label_image_is_not_augmented(tmp_path):
    """The augment plan only covers the images that carry a label."""

    staging = staging_layout(str(tmp_path))
    labelled = staged_original(staging, "a.png")
    skipped = staged_original(staging, "b.png", with_label=False)
    worker = ValidationWorker(
        build_config("/source", "/model.onnx", True), ["car"], staging
    )
    worker.records = [labelled, skipped]

    sources = worker._augment_sources()
    assert sources == [labelled]
    assert worker._augment_plan(sources) == [3]
    assert worker._augment_plan(worker._originals()) == [2, 1]
    assert worker._augment_plan([]) == []


def test_ratio_zero_plans_no_augmented_copy(tmp_path):
    """r = 0.0 stays an explicit value, it is never replaced by 1.0."""

    staging = staging_layout(str(tmp_path))
    labelled = staged_original(staging, "a.png")
    config = build_config("/source", "/model.onnx", True)
    config.augment_mode = "ratio"
    config.ratio = 0.0
    worker = ValidationWorker(config, ["car"], staging)
    worker.records = [labelled]

    assert worker._augment_plan(worker._augment_sources()) == [0]

    worker._stage_augment()
    assert worker.augment_summary["planned"] == 0
    assert worker.augment_summary["generated"] == 0
    assert len(worker.records) == 1
    assert worker.records[0] is labelled


def test_augment_summary_reports_the_skipped_images(tmp_path):
    """The summary distinguishes augmentable and skipped originals."""

    staging = staging_layout(str(tmp_path))
    labelled = staged_original(staging, "a.png")
    skipped = staged_original(staging, "b.png", with_label=False)
    worker = ValidationWorker(
        build_config("/source", "/model.onnx", False), ["car"], staging
    )
    worker.records = [labelled, skipped]

    worker._stage_augment()

    summary = worker.augment_summary
    assert summary["enabled"] is False
    assert summary["planned"] == 0
    assert summary["augmentable_originals"] == 1
    assert summary["skipped_no_label"] == 1


def test_saving_the_run_state_never_touches_the_source_dataset(tmp_path):
    """Invariant I-1 for the history: the state file stays in staging.

    The verdicts and the marks of a run are written into the staging
    folder of that very run, so the snapshot of the source dataset is
    the same before and after the write.
    """

    source = str(tmp_path / "source")
    write_pair(source, "a")
    write_pair(source, "b")
    before = dataset.snapshot_directory(source)

    staging = staging_layout(str(tmp_path))
    record = staged_original(staging, "a.png")
    record.verdict = records_module.OK
    record.judged = True
    history.save_restore_state(
        staging, [record], classes=["car"], source_display=source
    )

    assert osp.isfile(osp.join(staging, history.STATE_FILENAME))
    after = dataset.snapshot_directory(source)
    assert after == before


def test_listing_the_history_creates_no_file(tmp_path):
    """A scan lists the temp folder, it never writes into it.

    The mirror of the same invariant: history.list_runs is a read only
    pass over the candidates, so an empty scratch directory stays empty
    and answers an empty list.
    """

    scratch = osp.join(str(tmp_path), "scratch")
    os.makedirs(scratch, exist_ok=True)

    assert history.list_runs(temp_root=scratch) == []
    assert os.listdir(scratch) == []
