"""Whole image, class agnostic NMS and the per class row expansion.

The postprocessing changed: every row produced for one image runs a
single, class agnostic greedy NMS, so two overlapping boxes of two
different classes collapse into the one box the canvas draws - with one
label line per class - and the judge reads one independent row per class
("one box per class"). These tests pin both halves of that contract:
what the runner answers, and what the pipeline hands the judge, while
the canvas payload and the record of a single label prediction stay
exactly what they were.

The ONNX session, the YOLO wrapper and the image decode are replaced by
the small stand-ins this directory already uses, therefore no ONNX file
and no real model is needed.
"""

import os
import os.path as osp

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6 import QtWidgets

from anylabeling.custom.model_validation import dataset
from anylabeling.custom.model_validation import inference
from anylabeling.custom.model_validation import multilabel
from anylabeling.custom.model_validation import records as records_module
from anylabeling.custom.model_validation.app_config import ValidationConfig
from anylabeling.custom.model_validation.judge import (
    judge_record,
    shape_label,
    shape_type_of,
)
from anylabeling.custom.model_validation.pipeline import (
    ValidationWorker,
    _InferJob,
)

CLASSES = ["a0_dian", "a1_xian", "a2_kong"]
# the fixed inference cut of the tool, the floor of every row the
# engine answers (the score rule of the judge uses it as well)
SCORES = [0.9, 0.6, 0.3]


def metadata_for(task: str = "detect") -> dict:
    """Return ONNX metadata embedding the class table of these tests."""

    entries = ", ".join(
        f"{index}: {name!r}" for index, name in enumerate(CLASSES)
    )
    return {
        "task": task,
        "imgsz": "[640, 640]",
        "names": "{" + entries + "}",
    }


def result_of(shapes) -> object:
    """Return an object shaped like the result of the YOLO wrapper."""

    return type("Result", (), {"shapes": list(shapes)})()


def box_points(x: float = 0.0, y: float = 0.0, height: float = 10.0):
    """Return the two point rectangle of a 10 wide box at (x, y)."""

    return [(x, y), (x + 10.0, y + height)]


def row(label, score, points=None, shape_type: str = "rectangle") -> dict:
    """Return one predicted row in the payload dict shape."""

    return {
        "label": label,
        "score": score,
        "shape_type": shape_type,
        "points": list(box_points() if points is None else points),
    }


class FakeShape:
    """Predicted shape carrying the label given by the YOLO wrapper."""

    def __init__(
        self,
        label: str,
        score: float = 0.9,
        points=None,
        shape_type: str = "rectangle",
    ) -> None:
        self.label = label
        self.score = float(score)
        self.shape_type = shape_type
        self.points = list(box_points() if points is None else points)


def merged_shape(labels, scores, points=None) -> FakeShape:
    """Return a merged prediction shape with its per class rows."""

    shape = FakeShape(str(labels[0]), float(scores[0]), points)
    shape.labels = list(labels)
    shape.scores = list(scores)
    return shape


class FakeSession:
    """Stand-in for the ONNX session of the auto labeling engine."""

    def __init__(self, model_path: str, device: str) -> None:
        self.model_path = model_path
        self.device = device

    def get_input_shape(self):
        """Return a static NCHW input shape."""

        return [1, 3, 640, 640]


class RowsYOLO:
    """Stand-in answering exactly the rows a test pinned.

    The rows are a class attribute on purpose: a test pins them before
    building the runner, which is the only moment the wrapper is built.
    """

    rows: list = []

    def __init__(self, config, on_message=None) -> None:
        self.config = dict(config)
        self.on_message = on_message
        self.classes = list(self.config.get("classes") or [])
        self.net = FakeSession(self.config.get("model_path", ""), "cpu")

    def predict_shapes(self, image, image_path=None):
        """Answer the pinned rows as shapes of the wrapper."""

        return result_of(
            FakeShape(
                str(item["label"]),
                score=item["score"],
                points=item.get("points"),
                shape_type=item.get("shape_type", "rectangle"),
            )
            for item in self.rows
        )


def write_png(directory, name: str, size=(64, 96)) -> str:
    """Write a plain RGB PNG and return its path."""

    import cv2
    import numpy as np

    height, width = size
    image = np.full((height, width, 3), 127, dtype=np.uint8)
    ok, buffer = cv2.imencode(".png", image)
    assert ok
    path = os.path.join(str(directory), name)
    buffer.tofile(path)
    return path


def install_model(monkeypatch, tmp_path) -> str:
    """Patch the ONNX session, the YOLO wrapper and the metadata read."""

    import anylabeling.services.auto_labeling.__base__.yolo as yolo_base
    import anylabeling.services.auto_labeling.engines as engines

    model_path = os.path.join(str(tmp_path), "model.onnx")
    with open(model_path, "wb") as handle:
        handle.write(b"onnx-stand-in")
    monkeypatch.setattr(engines, "OnnxBaseModel", FakeSession)
    monkeypatch.setattr(yolo_base, "YOLO", RowsYOLO)
    monkeypatch.setattr(
        inference, "read_onnx_metadata", lambda _path: metadata_for()
    )
    return model_path


@pytest.fixture
def runner_env(monkeypatch, tmp_path):
    """Patch the session and the wrapper, letting a test pick metadata."""

    model_path = install_model(monkeypatch, tmp_path)
    # the merge only needs coordinates and scores: the decoder stays out
    monkeypatch.setattr(
        inference, "load_image_rgb", lambda _path: [[0, 0, 0]]
    )

    def install(metadata: dict) -> str:
        monkeypatch.setattr(
            inference,
            "read_onnx_metadata",
            lambda _path: dict(metadata),
        )
        return model_path

    return install


@pytest.fixture
def pinned_rows(monkeypatch):
    """Pin the rows every runner of one test answers."""

    def install(rows):
        monkeypatch.setattr(RowsYOLO, "rows", [dict(item) for item in rows])
        return rows

    return install


@pytest.fixture
def image_file(tmp_path):
    """A readable, three channel PNG."""

    return write_png(tmp_path, "sample.png")


# ------------------------------------------------- the whole image NMS
def test_two_overlapping_classes_of_one_box_become_one_box_with_two_rows(
    runner_env, image_file, pinned_rows
):
    """Criterion 1: two classes above the cut reach the canvas as one box."""

    # the two rows describe the same object with a one pixel shift, so
    # their IoU is 81 / 119 = 0.68, above the configured 0.45
    pinned_rows(
        [
            row("a1_xian", 0.6, box_points(1.0, 1.0)),
            row("a0_dian", 0.9),
        ]
    )
    runner = inference.ModelRunner(runner_env(metadata_for()), CLASSES)

    shapes = runner.predict(image_file)

    assert len(shapes) == 1
    merged = shapes[0]
    # the highest scoring row is the primary one and it gives the box
    assert merged.label == "a0_dian"
    assert merged.score == 0.9
    assert [tuple(point) for point in merged.points] == box_points()
    # one label line per class, parallel and score descending
    assert merged.labels == ["a0_dian", "a1_xian"]
    assert merged.scores == [0.9, 0.6]
    assert len(merged.labels) == len(merged.scores)
    assert merged.scores == sorted(merged.scores, reverse=True)


def test_two_boxes_below_the_iou_threshold_stay_two_predictions(
    runner_env, image_file, pinned_rows
):
    """Criterion 2: an IoU at or below the cut keeps both boxes."""

    # (0, 0)-(10, 10) against (8, 0)-(18, 10): IoU = 20 / 180 = 0.11
    pinned_rows(
        [
            row("a0_dian", 0.9),
            row("a1_xian", 0.8, box_points(8.0, 0.0)),
        ]
    )
    runner = inference.ModelRunner(runner_env(metadata_for()), CLASSES)

    shapes = runner.predict(image_file)

    assert [shape.label for shape in shapes] == ["a0_dian", "a1_xian"]
    assert [tuple(shape.points[0]) for shape in shapes] == [
        (0.0, 0.0),
        (8.0, 0.0),
    ]
    assert all(not hasattr(shape, "labels") for shape in shapes)


def test_a_row_joins_a_cluster_only_above_the_threshold():
    """The comparison is strict, exactly like the engine NMS."""

    # a 10x10 box against a 10x5 box at the same corner: IoU = 0.5
    outer = row("a0_dian", 0.9)
    inner = row("a1_xian", 0.5, box_points(height=5.0))

    assert (
        len(multilabel.merge_overlapping_predictions([outer, inner], 0.5))
        == 2
    )
    merged = multilabel.merge_overlapping_predictions([outer, inner], 0.4)
    assert len(merged) == 1
    assert merged[0]["labels"] == ["a0_dian", "a1_xian"]


def test_the_obb_task_keeps_the_legacy_coordinate_grouping(
    runner_env, image_file, pinned_rows
):
    """obb and pose never run the whole image NMS."""

    pinned_rows(
        [
            row("a0_dian", 0.9, shape_type="rotation"),
            row(
                "a1_xian",
                0.6,
                box_points(1.0, 1.0),
                "rotation",
            ),
        ]
    )
    runner = inference.ModelRunner(runner_env(metadata_for("obb")), CLASSES)

    shapes = runner.predict(image_file)

    assert [shape.label for shape in shapes] == ["a0_dian", "a1_xian"]
    assert all(not hasattr(shape, "labels") for shape in shapes)


# ------------------------------------------------------- the IoU helper
def test_shape_iou_of_two_polygons_uses_the_true_overlap():
    """A rotated region is never judged by its bounding box."""

    left = [(0.0, 5.0), (5.0, 0.0), (10.0, 5.0), (5.0, 10.0)]
    right = [(5.0, 5.0), (10.0, 0.0), (15.0, 5.0), (10.0, 10.0)]

    # 12.5 / 87.5, while the two bounding boxes would answer 0.333
    assert multilabel.shape_iou(left, right) == pytest.approx(12.5 / 87.5)
    assert multilabel.shape_iou(right, left) == pytest.approx(12.5 / 87.5)
    assert multilabel.shape_iou(left, []) == 0.0


def test_shape_iou_of_two_rectangles_is_the_intersection_over_union():
    """The two point rectangle of the engine keeps the AABB rule."""

    assert multilabel.shape_iou(box_points(), box_points()) == 1.0
    assert multilabel.shape_iou(
        box_points(), box_points(5.0, 0.0)
    ) == pytest.approx(50.0 / 150.0)


def test_shape_iou_of_a_degenerate_shape_is_zero():
    """A point list without an area never overlaps anything."""

    assert multilabel.shape_iou([(0.0, 0.0)], box_points()) == 0.0
    assert (
        multilabel.shape_iou(
            [(0.0, 0.0), (0.0, 0.0)], box_points()
        )
        == 0.0
    )
    assert multilabel.shape_iou(box_points(), box_points(50.0, 0.0)) == 0.0


# --------------------------------------------------- the cluster merge
def test_two_classes_of_one_cluster_are_kept_as_two_rows():
    """One cluster, one label line per class, best score first."""

    rows = [
        row("a1_xian", 0.6, box_points(1.0, 1.0)),
        row("a0_dian", 0.9),
        row("a2_kong", 0.3),
    ]

    merged = multilabel.merge_overlapping_predictions(rows, 0.45)

    assert len(merged) == 1
    assert merged[0]["label"] == "a0_dian"
    assert merged[0]["labels"] == ["a0_dian", "a1_xian", "a2_kong"]
    assert merged[0]["scores"] == [0.9, 0.6, 0.3]


def test_one_class_can_never_count_twice_inside_a_cluster():
    """Two anchors of one class fold into the best row of that class."""

    rows = [
        row("a0_dian", 0.9),
        row("a0_dian", 0.4, box_points(1.0, 1.0)),
    ]

    merged = multilabel.merge_overlapping_predictions(rows, 0.45)

    assert len(merged) == 1
    assert merged[0] is rows[0]
    assert "labels" not in merged[0]


def test_a_row_without_a_score_yields_to_the_scored_row_of_its_class():
    """The duplicate rule keeps the row the judge can rank."""

    unscored = row("a0_dian", None)
    scored = row("a0_dian", 0.7, box_points(1.0, 1.0))

    merged = multilabel.merge_overlapping_predictions(
        [unscored, scored], 0.45
    )

    assert merged[0] is scored


def test_the_clusters_come_out_by_descending_score():
    """The output order is the score order of the kept rows."""

    rows = [
        row("a0_dian", 0.4, box_points(50.0, 0.0)),
        row("a1_xian", 0.9),
        row("a2_kong", 0.5, box_points(50.0, 0.0)),
        row("a0_dian", 0.7, box_points(1.0, 1.0)),
    ]

    merged = multilabel.merge_overlapping_predictions(rows, 0.45)

    assert [item["label"] for item in merged] == ["a1_xian", "a2_kong"]
    assert merged[0]["labels"] == ["a1_xian", "a0_dian"]
    assert merged[1]["labels"] == ["a2_kong", "a0_dian"]


def test_without_a_threshold_only_identical_coordinates_group():
    """The obb and pose path is the legacy grouping, untouched."""

    rows = [
        row("a0_dian", 0.9, shape_type="rotation"),
        row("a1_xian", 0.6, box_points(1.0, 1.0), "rotation"),
        row("a2_kong", 0.3, shape_type="rotation"),
    ]

    merged = multilabel.merge_overlapping_predictions(rows, None)

    assert [item["label"] for item in merged] == ["a0_dian", "a1_xian"]
    assert merged[0]["labels"] == ["a0_dian", "a2_kong"]


def test_a_non_mergeable_shape_keeps_the_legacy_grouping():
    """A rotation row never joins the whole image NMS."""

    rows = [
        row("a0_dian", 0.9, shape_type="rotation"),
        row("a1_xian", 0.6, box_points(1.0, 1.0), "rotation"),
        row("a2_kong", 0.5),
    ]

    merged = multilabel.merge_overlapping_predictions(rows, 0.45)

    # only the rectangle row is a candidate, the two rotations stay two
    # boxes even though they overlap by 0.68
    assert [item["label"] for item in merged] == [
        "a2_kong",
        "a0_dian",
        "a1_xian",
    ]


# ------------------------------------------------- the row expansion
def test_a_merged_box_expands_into_one_row_per_class():
    """The judge reads one row per class, all carrying the same box."""

    merged = row("a0_dian", 0.9)
    merged["labels"] = ["a0_dian", "a1_xian"]
    merged["scores"] = [0.9, 0.6]
    single = row("a2_kong", 0.5, box_points(50.0, 0.0))

    rows, boxes = multilabel.expand_multilabel_rows([merged, single])

    assert [item["label"] for item in rows] == [
        "a0_dian",
        "a1_xian",
        "a2_kong",
    ]
    assert [item["score"] for item in rows] == [0.9, 0.6, 0.5]
    assert [item["shape_type"] for item in rows] == ["rectangle"] * 3
    assert boxes == [0, 0, 1]
    assert rows[0]["points"] == [[0.0, 0.0], [10.0, 10.0]]
    # the rows of one box share the very point list
    assert rows[0]["points"] is rows[1]["points"]


def test_a_single_label_shape_expands_into_its_one_row():
    """A box without label rows keeps its own label and score."""

    shape = FakeShape("a0_dian", 0.9)

    rows, boxes = multilabel.expand_multilabel_rows([shape])

    assert rows == [
        {
            "label": "a0_dian",
            "score": 0.9,
            "shape_type": "rectangle",
            "points": [[0.0, 0.0], [10.0, 10.0]],
        }
    ]
    assert boxes == [0]


def test_a_broken_label_pair_falls_back_to_the_main_row():
    """Two lists that do not line up are never half read."""

    broken = row("a0_dian", 0.9)
    broken["labels"] = ["a0_dian", "a1_xian"]
    broken["scores"] = [0.9]

    rows, boxes = multilabel.expand_multilabel_rows([broken])

    assert [item["label"] for item in rows] == ["a0_dian"]
    assert boxes == [0]


def test_the_expansion_reads_shape_objects_too():
    """A Shape object of the runner expands like a payload dict."""

    shape = merged_shape(["a0_dian", "a1_xian"], [0.9, 0.6])

    rows, boxes = multilabel.expand_multilabel_rows([shape])

    assert [item["label"] for item in rows] == ["a0_dian", "a1_xian"]
    assert [item["score"] for item in rows] == [0.9, 0.6]
    assert boxes == [0, 0]


# --------------------------------------------------- the pipeline wiring
@pytest.fixture(scope="module")
def qt_app():
    """Create the offscreen application the worker is built in."""

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


class StubRunner:
    """Answer the merged payload a test pinned."""

    def __init__(self, shapes) -> None:
        self.shapes = list(shapes)

    def predict(self, image_path):
        """Return the pinned payload whatever the image is."""

        return list(self.shapes)


def gt_box(label: str, x: float = 0.0, y: float = 0.0) -> dict:
    """Return one ground truth rectangle shape."""

    return {
        "label": label,
        "shape_type": "rectangle",
        "points": [list(point) for point in box_points(x, y)],
    }


def staging_record(tmp_path, gt_shapes):
    """Write one staged image and label pair, return its record."""

    image_path = osp.join(str(tmp_path), "sample.png")
    with open(image_path, "wb") as handle:
        handle.write(b"staged-image")
    label_path = osp.join(str(tmp_path), "sample.json")
    dataset.write_json(
        label_path,
        {
            "version": "3.0.0",
            "flags": {},
            "shapes": list(gt_shapes),
            "imagePath": "sample.png",
            "imageData": None,
            "imageHeight": 32,
            "imageWidth": 40,
        },
    )
    return records_module.make_record(
        records_module.KIND_ORIGINAL,
        "sample.png",
        image_path,
        label_path,
    )


def infer_one(tmp_path, predicted, gt_shapes):
    """Run the real _infer_one on one staged record of these tests."""

    config = ValidationConfig(
        dataset_dir="/source",
        model_path="/model.onnx",
        augment_enabled=False,
    )
    worker = ValidationWorker(config, CLASSES, str(tmp_path))
    job = _InferJob(
        ordinal=0, record=staging_record(tmp_path, gt_shapes)
    )
    return worker, worker._infer_one(StubRunner(predicted), job)


def test_a_merged_box_expands_into_one_row_per_class_for_the_judge(
    tmp_path, qt_app
):
    """Criterion 3: one payload box, one judge row per class."""

    predicted = [merged_shape(["a0_dian", "a1_xian"], [0.9, 0.2])]

    _worker, result = infer_one(
        tmp_path, predicted, [gt_box("a0_dian")]
    )
    detail = result.detail

    # the canvas payload stays the one box, carrying both label rows
    assert len(detail["predictions"]) == 1
    payload = detail["predictions"][0]
    assert payload["label"] == "a0_dian"
    assert payload["score"] == 0.9
    assert payload["points"] == [[0.0, 0.0], [10.0, 10.0]]
    assert payload["labels"] == ["a0_dian", "a1_xian"]
    assert payload["scores"] == [0.9, 0.2]
    assert set(payload) == {
        "label",
        "shape_type",
        "points",
        "score",
        "labels",
        "scores",
    }
    # the judge saw one independent row per class
    assert detail["predicted_labels"] == ["a0_dian", "a1_xian"]
    assert detail["predicted_types"] == ["rectangle", "rectangle"]
    assert detail["pred_total"] == 2
    assert detail["pred_valid"] == 2
    assert detail["pred_row_boxes"] == [0, 0]

    # every index of the verdict addresses a row, never the one payload
    # box: the unmatched second row is index 1 while detail
    # ["predictions"] holds the single box 0
    matched = detail["matched"]
    false_positives = detail["false_positives"]
    assert len(matched) == 1
    assert matched[0]["gt_index"] == 0
    assert len(false_positives) == 1
    assert sorted(
        [matched[0]["pred_index"], false_positives[0]["index"]]
    ) == [0, 1]
    # the score rule works in the very same row coordinate system
    assert detail["low_score"] == [
        {"index": 1, "label": "a1_xian", "score": 0.2}
    ]
    # row 1 belongs to the payload box 0, the only box the canvas draws
    assert detail["pred_row_boxes"][1] == 0


def test_a_single_label_record_keeps_its_payload_and_its_verdict(
    tmp_path, qt_app
):
    """Criterion 5: a single label box only gains pred_row_boxes."""

    gt_shapes = [gt_box("a0_dian")]
    predicted = [FakeShape("a0_dian", 0.9)]

    worker, result = infer_one(tmp_path, predicted, gt_shapes)

    # the record the tool produced before this change, rebuilt from the
    # untouched pieces (the judge, the payload builder and the two label
    # lists of the shapes themselves)
    expected = judge_record(
        gt_shapes,
        predicted,
        CLASSES,
        ng_iou_threshold=worker.config.ng_iou_threshold,
        ng_score_threshold=worker.config.conf_threshold,
    )
    expected_detail = dict(expected.detail)
    expected_detail["predicted_labels"] = [
        shape_label(shape) for shape in predicted
    ]
    expected_detail["predicted_types"] = [
        shape_type_of(shape) for shape in predicted
    ]
    expected_detail["predictions"] = (
        ValidationWorker._prediction_payload(predicted)
    )

    assert result.verdict == expected.verdict == records_module.OK
    assert result.reasons == expected.reasons == []
    assert set(result.detail) == set(expected_detail) | {"pred_row_boxes"}
    assert {
        key: value
        for key, value in result.detail.items()
        if key != "pred_row_boxes"
    } == expected_detail
    assert result.detail["pred_row_boxes"] == [0]
