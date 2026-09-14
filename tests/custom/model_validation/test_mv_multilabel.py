"""Multi label merge tests for the model validation inference layer.

A detector whose NMS runs with multi_label=True answers one row per
class whose score passed the fixed inference confidence cut, and every
row of a box carries the very same coordinates. The merge folds those
rows into the single box the canvas draws, with one label line per row,
while a single label prediction keeps exactly the payload it always
had. The ONNX session and the YOLO wrapper are replaced by the small
stand-ins this directory already uses, so no ONNX file is needed.
"""

import inspect
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from anylabeling.custom.model_validation import inference
from anylabeling.custom.model_validation.app_config import (
    INFERENCE_CONF_THRESHOLD,
)

CLASSES = ["a0_dian", "a1_xian", "a2_kong"]
SCORES = [0.9, 0.7, 0.5]


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


def box_points(x: float = 0.0, y: float = 0.0):
    """Return the two point rectangle of a box at (x, y)."""

    return [(x, y), (x + 10.0, y + 10.0)]


def row(label: str, score, x: float = 0.0, y: float = 0.0) -> dict:
    """Return one predicted row in the dict shape of the pipeline."""

    return {
        "label": label,
        "score": score,
        "shape_type": "rectangle",
        "points": box_points(x, y),
    }


class FakeShape:
    """Predicted shape carrying the label given by the YOLO wrapper."""

    def __init__(
        self, label: str, score: float = 0.9, origin=(0.0, 0.0)
    ) -> None:
        self.label = label
        self.score = float(score)
        self.shape_type = "rectangle"
        self.points = box_points(origin[0], origin[1])


class FakeSession:
    """Stand-in for the ONNX session of the auto labeling engine."""

    def __init__(self, model_path: str, device: str) -> None:
        self.model_path = model_path
        self.device = device

    def get_input_shape(self):
        """Return a static NCHW input shape."""

        return [1, 3, 640, 640]


class MultiLabelYOLO:
    """Stand-in answering one scored row per class for the same box."""

    def __init__(self, config, on_message=None) -> None:
        self.config = dict(config)
        self.on_message = on_message
        self.classes = list(self.config.get("classes") or [])
        self.net = FakeSession(self.config.get("model_path", ""), "cpu")

    def predict_shapes(self, image, image_path=None):
        """Return one differently scored row per class, one single box."""

        return result_of(
            FakeShape(str(name), score=SCORES[index])
            for index, name in enumerate(self.classes)
        )


class SpreadYOLO(MultiLabelYOLO):
    """Stand-in answering one class row per class, one box per row."""

    def predict_shapes(self, image, image_path=None):
        """Return one differently scored row per class, one box each."""

        return result_of(
            FakeShape(
                str(name),
                score=SCORES[index],
                origin=(50.0 * index, 0.0),
            )
            for index, name in enumerate(self.classes)
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


def install_model(
    monkeypatch, tmp_path, metadata, yolo_class=MultiLabelYOLO
) -> str:
    """Patch the ONNX session, the YOLO wrapper and the metadata read."""

    import anylabeling.services.auto_labeling.__base__.yolo as yolo_base
    import anylabeling.services.auto_labeling.engines as engines

    model_path = os.path.join(str(tmp_path), "model.onnx")
    with open(model_path, "wb") as handle:
        handle.write(b"onnx-stand-in")
    monkeypatch.setattr(engines, "OnnxBaseModel", FakeSession)
    monkeypatch.setattr(yolo_base, "YOLO", yolo_class)
    monkeypatch.setattr(
        inference, "read_onnx_metadata", lambda _path: dict(metadata)
    )
    return model_path


@pytest.fixture
def runner_env(monkeypatch, tmp_path):
    """Patch the session and the wrapper, letting a test pick metadata."""

    model_path = install_model(monkeypatch, tmp_path, metadata_for())
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
def spread_env(monkeypatch, tmp_path):
    """Patch a wrapper answering one box per class, not per row."""

    model_path = install_model(
        monkeypatch, tmp_path, metadata_for(), SpreadYOLO
    )
    monkeypatch.setattr(
        inference, "load_image_rgb", lambda _path: [[0, 0, 0]]
    )
    return model_path


@pytest.fixture
def image_file(tmp_path):
    """A readable, three channel PNG."""

    return write_png(tmp_path, "sample.png")


def test_the_rows_of_one_box_merge_into_a_single_shape():
    """Three classes above the cut are one box with three label rows."""

    rows = [
        row("a0_dian", 0.9),
        row("a1_xian", 0.6),
        row("a2_kong", 0.3),
    ]
    merged = inference.merge_multilabel_predictions(rows)
    assert len(merged) == 1
    assert merged[0]["label"] == "a0_dian"
    assert merged[0]["score"] == 0.9
    assert merged[0]["labels"] == ["a0_dian", "a1_xian", "a2_kong"]
    assert merged[0]["scores"] == [0.9, 0.6, 0.3]
    # the box itself is untouched by the merge
    assert merged[0]["points"] == box_points()
    assert merged[0]["shape_type"] == "rectangle"


def test_the_primary_row_is_the_highest_score_of_the_group():
    """The label/score the judge reads is the top row, not the last."""

    rows = [
        row("a2_kong", 0.31),
        row("a0_dian", 0.88),
        row("a1_xian", 0.55),
    ]
    merged = inference.merge_multilabel_predictions(rows)[0]
    assert merged["label"] == "a0_dian"
    assert merged["score"] == 0.88
    assert merged["labels"][0] == merged["label"]
    assert merged["scores"][0] == merged["score"]


def test_labels_and_scores_are_parallel_and_score_descending():
    """The two lists line up and the scores never grow downwards."""

    rows = [
        row("a2_kong", 0.31),
        row("a0_dian", 0.88),
        row("a1_xian", 0.55),
    ]
    merged = inference.merge_multilabel_predictions(rows)[0]
    assert len(merged["labels"]) == len(merged["scores"]) == 3
    assert merged["scores"] == sorted(merged["scores"], reverse=True)
    assert merged["labels"] == ["a0_dian", "a1_xian", "a2_kong"]


def test_boxes_at_different_coordinates_do_not_merge():
    """Two really different boxes stay two predictions."""

    rows = [row("a0_dian", 0.9), row("a1_xian", 0.8, x=40.0)]
    merged = inference.merge_multilabel_predictions(rows)
    assert [item["label"] for item in merged] == ["a0_dian", "a1_xian"]
    assert all("labels" not in item for item in merged)


def test_the_shape_type_separates_rows_at_the_same_coordinates():
    """The same points under two shape types are two different keys."""

    rows = [row("a0_dian", 0.9), row("a1_xian", 0.8)]
    rows[1]["shape_type"] = "polygon"
    merged = inference.merge_multilabel_predictions(rows)
    assert len(merged) == 2
    assert [item["shape_type"] for item in merged] == [
        "rectangle",
        "polygon",
    ]


def test_the_groups_keep_the_first_seen_order():
    """The box order is the score descending order of the NMS output."""

    rows = [
        row("a0_dian", 0.9, x=40.0),
        row("a1_xian", 0.8),
        row("a2_kong", 0.2, x=40.0),
    ]
    merged = inference.merge_multilabel_predictions(rows)
    assert [item["label"] for item in merged] == ["a0_dian", "a1_xian"]
    assert merged[0]["labels"] == ["a0_dian", "a2_kong"]
    assert "labels" not in merged[1]


def test_a_single_row_gains_no_key_at_all():
    """The single label payload is returned byte for byte."""

    original = row("a0_dian", 0.9)
    before = dict(original)
    merged = inference.merge_multilabel_predictions([original])
    assert merged == [original]
    assert merged[0] is original
    assert set(original) == set(before)
    assert "labels" not in original
    assert "scores" not in original


def test_a_merged_dict_is_a_copy_of_the_top_row():
    """The merge never writes the extra keys into the caller rows."""

    first = row("a0_dian", 0.9)
    second = row("a1_xian", 0.5)
    merged = inference.merge_multilabel_predictions([first, second])
    assert merged[0] is not first
    assert "labels" not in first
    assert "labels" not in second
    assert first["score"] == 0.9


def test_a_single_shape_object_gains_no_dynamic_attribute():
    """A Shape object of one row keeps its attributes untouched."""

    shape = FakeShape("a0_dian", 0.7)
    merged = inference.merge_multilabel_predictions([shape])
    assert merged == [shape]
    assert not hasattr(shape, "labels")
    assert not hasattr(shape, "scores")


def test_shape_objects_merge_and_the_top_row_is_reused():
    """The Shape object of the highest score carries the two lists."""

    top = FakeShape("a1_xian", 0.8)
    other = FakeShape("a0_dian", 0.4)
    merged = inference.merge_multilabel_predictions([other, top])
    assert len(merged) == 1
    assert merged[0] is top
    assert top.label == "a1_xian"
    assert top.score == 0.8
    assert top.labels == ["a1_xian", "a0_dian"]
    assert top.scores == [0.8, 0.4]
    assert not hasattr(other, "labels")


def test_equal_scores_break_the_tie_on_the_label():
    """A tie resolves deterministically, never by the input order."""

    rows = [
        row("a2_kong", 0.5),
        row("a0_dian", 0.5),
        row("a1_xian", 0.5),
    ]
    merged = inference.merge_multilabel_predictions(rows)[0]
    assert merged["label"] == "a0_dian"
    assert merged["labels"] == ["a0_dian", "a1_xian", "a2_kong"]


def test_a_row_without_a_score_sorts_after_the_scored_rows():
    """A missing score can never become the primary label."""

    rows = [row("a0_dian", None), row("a1_xian", 0.3)]
    merged = inference.merge_multilabel_predictions(rows)[0]
    assert merged["label"] == "a1_xian"
    assert merged["labels"] == ["a1_xian", "a0_dian"]
    assert merged["scores"] == [0.3, None]


def test_fractional_coordinates_are_compared_at_six_decimals():
    """The float noise of the tensor arithmetic is absorbed."""

    rows = [
        row("a0_dian", 0.9, x=1.0000001),
        row("a1_xian", 0.4, x=1.0000002),
    ]
    merged = inference.merge_multilabel_predictions(rows)
    assert len(merged) == 1
    assert merged[0]["labels"] == ["a0_dian", "a1_xian"]


def test_rows_without_points_group_on_the_empty_point_list():
    """The grouping key of a row without points is its empty tuple."""

    rows = [
        {"label": "a0_dian", "score": 0.9, "shape_type": "rectangle"},
        {"label": "a1_xian", "score": 0.4, "shape_type": "rectangle"},
        {"label": "a2_kong", "score": 0.3, "shape_type": "polygon"},
    ]
    merged = inference.merge_multilabel_predictions(rows)
    assert [item["label"] for item in merged] == ["a0_dian", "a2_kong"]
    assert merged[0]["labels"] == ["a0_dian", "a1_xian"]


def test_predict_merges_the_rows_of_one_box(runner_env, image_file):
    """The runner answers one box with the label lines of every row."""

    model_path = runner_env(metadata_for())
    runner = inference.ModelRunner(model_path, CLASSES)
    shapes = runner.predict(image_file)
    assert len(shapes) == 1
    assert shapes[0].label == CLASSES[0]
    assert shapes[0].score == SCORES[0]
    assert shapes[0].labels == CLASSES
    assert shapes[0].scores == SCORES


def test_predict_keeps_different_boxes_apart(spread_env, image_file):
    """Two boxes at two coordinates stay two single label shapes."""

    runner = inference.ModelRunner(spread_env, CLASSES)
    shapes = runner.predict(image_file)
    assert [shape.label for shape in shapes] == CLASSES
    assert all(not hasattr(shape, "labels") for shape in shapes)


def test_the_runner_signature_has_no_confidence_threshold():
    """The fixed cut is not an argument of the runner any more."""

    parameters = inspect.signature(
        inference.ModelRunner.__init__
    ).parameters
    assert "conf_threshold" not in parameters
    assert "iou_threshold" in parameters


def test_the_pool_signature_keeps_only_the_iou_threshold():
    """build_runner_pool lost conf_threshold and kept iou_threshold."""

    parameters = inspect.signature(
        inference.build_runner_pool
    ).parameters
    assert "conf_threshold" not in parameters
    assert "iou_threshold" in parameters


def test_the_pool_refuses_a_confidence_threshold(runner_env):
    """Handing the removed keyword over is an explicit error."""

    model_path = runner_env(metadata_for())
    with pytest.raises(TypeError):
        inference.build_runner_pool(
            model_path, CLASSES, 1, conf_threshold=0.3
        )


def test_the_runner_pins_the_fixed_cut_and_enables_multilabel(runner_env):
    """detect runs with the frozen cut and the multi label NMS on."""

    model_path = runner_env(metadata_for())
    runner = inference.ModelRunner(model_path, CLASSES)
    config = runner.model.config
    assert config["conf_threshold"] == INFERENCE_CONF_THRESHOLD == 0.25
    assert config["multi_label"] is True


@pytest.mark.parametrize(
    "task, expected",
    [
        ("detect", True),
        ("segment", True),
        ("obb", False),
        ("pose", False),
    ],
)
def test_multilabel_is_on_for_the_box_tasks_only(
    runner_env, task, expected
):
    """obb and pose keep the single label path."""

    model_path = runner_env(metadata_for(task))
    runner = inference.ModelRunner(model_path, CLASSES)
    assert runner.model.config["multi_label"] is expected


def test_the_pool_builds_every_runner_with_the_fixed_cut(runner_env):
    """The pool forwards the IoU value and nothing else."""

    model_path = runner_env(metadata_for())
    runners = inference.build_runner_pool(
        model_path, CLASSES, 2, iou_threshold=0.4
    )
    assert len(runners) == 2
    assert [
        runner.model.config["conf_threshold"] for runner in runners
    ] == [0.25, 0.25]
    assert [runner.model.config["iou_threshold"] for runner in runners] == [
        0.4,
        0.4,
    ]
    assert all(
        runner.model.config["multi_label"] is True for runner in runners
    )


def test_the_merge_is_exported_by_the_module():
    """The contract name is public."""

    assert "merge_multilabel_predictions" in inference.__all__
    assert callable(inference.merge_multilabel_predictions)


def test_the_fixed_threshold_is_the_frozen_value():
    """The one source of the cut is app_config."""

    from anylabeling.custom.model_validation import app_config

    assert INFERENCE_CONF_THRESHOLD == 0.25
    assert app_config.INFERENCE_CONF_THRESHOLD == 0.25
    assert "INFERENCE_CONF_THRESHOLD" in app_config.__all__
