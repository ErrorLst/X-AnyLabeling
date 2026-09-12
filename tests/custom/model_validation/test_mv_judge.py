"""OK / NG verdict and matching tests."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from anylabeling.custom.model_validation.judge import (
    CLASS_MISMATCH,
    COUNT_MISMATCH,
    GT_DEGENERATE,
    GT_UNKNOWN_CLASS,
    INFER_ERROR,
    IOU_BELOW,
    MISS_FP,
    PRED_UNKNOWN_CLASS,
    UNSUPPORTED_GT_SHAPE,
    hungarian_match,
    iou_matrix,
    judge_record,
)
from anylabeling.custom.model_validation.records import NG, OK

CLASSES = ["person", "car"]


def rect(
    label: str = "person", offset: float = 0.0, size: float = 10.0
) -> dict:
    return {
        "label": label,
        "shape_type": "rectangle",
        "points": [
            [offset, offset],
            [offset + size, offset],
            [offset + size, offset + size],
            [offset, offset + size],
        ],
    }


def test_dual_empty_is_ok():
    result = judge_record([], [], CLASSES)
    assert result.verdict == OK
    assert result.reasons == []
    assert result.detail["dual_empty"] is True


def test_infer_error_has_highest_priority():
    result = judge_record([], [], CLASSES, infer_error="boom")
    assert result.verdict == NG
    assert result.reasons == [INFER_ERROR]
    assert result.detail["error"] == "boom"


def test_gt_degenerate():
    degenerate = {
        "label": "person",
        "shape_type": "polygon",
        "points": [[3, 3], [3, 3]],
    }
    result = judge_record([degenerate], [], CLASSES)
    assert result.verdict == NG
    assert result.reasons[0] == GT_DEGENERATE


def test_two_point_legacy_rectangle_is_not_degenerate():
    legacy = {
        "label": "person",
        "shape_type": "rectangle",
        "points": [[0, 0], [10, 10]],
    }
    result = judge_record([legacy], [legacy], CLASSES)
    assert result.verdict == OK
    assert GT_DEGENERATE not in result.reasons
    assert result.detail["matched"][0]["iou"] == 1.0


def test_zero_area_rectangle_is_degenerate():
    degenerate = {
        "label": "person",
        "shape_type": "rectangle",
        "points": [[4, 4], [4, 20]],
    }
    result = judge_record([degenerate], [], CLASSES)
    assert GT_DEGENERATE in result.reasons


def test_gt_unknown_class():
    result = judge_record([rect("ghost")], [rect("ghost")], CLASSES)
    assert result.verdict == NG
    assert GT_UNKNOWN_CLASS in result.reasons
    assert PRED_UNKNOWN_CLASS in result.reasons
    assert result.reasons[0] == GT_UNKNOWN_CLASS


def test_unknown_class_prediction_consumes_the_match():
    result = judge_record([rect("ghost")], [rect("ghost")], CLASSES)
    assert result.detail["false_positives"] == []
    assert result.detail["missed_gt"] == []
    assert MISS_FP not in result.reasons


def test_unsupported_gt_shape():
    point = {"label": "person", "shape_type": "point", "points": [[1, 1]]}
    result = judge_record([point], [], CLASSES)
    assert result.verdict == NG
    assert result.reasons[0] == UNSUPPORTED_GT_SHAPE


def test_count_mismatch():
    result = judge_record([rect(), rect(offset=20.0)], [rect()], CLASSES)
    assert result.verdict == NG
    assert result.reasons[0] == COUNT_MISMATCH


def test_class_mismatch_and_iou_below_and_miss_fp():
    result = judge_record([rect("person")], [rect("car")], CLASSES)
    assert result.reasons[0] == CLASS_MISMATCH
    assert IOU_BELOW not in result.reasons
    assert MISS_FP not in result.reasons


def test_iou_below_threshold():
    shifted = {
        "label": "person",
        "shape_type": "rectangle",
        "points": [[7.0, 0.0], [17.0, 0.0], [17.0, 10.0], [7.0, 10.0]],
    }
    result = judge_record([rect()], [shifted], CLASSES, ng_iou_threshold=0.9)
    assert result.verdict == NG
    assert IOU_BELOW in result.reasons
    assert result.detail["matched"][0]["iou"] < 0.9


def test_miss_and_false_positive():
    result = judge_record([rect()], [rect(offset=40.0)], CLASSES)
    assert result.reasons[0] == MISS_FP
    assert len(result.detail["missed_gt"]) == 1
    assert len(result.detail["false_positives"]) == 1


def test_pose_point_predictions_are_ignored():
    keypoint = {
        "label": "person",
        "shape_type": "point",
        "points": [[1.0, 1.0]],
        "score": 0.9,
    }
    result = judge_record([rect()], [rect(), keypoint], CLASSES)
    assert result.verdict == OK
    assert result.detail["ignored_pred_shapes"] == 1


def test_shape_object_predictions_are_supported():
    class FakeShape:
        def __init__(self, label, shape_type, points):
            self.label = label
            self.shape_type = shape_type
            self.points = points
            self.score = 0.75

    class FakePoint:
        def __init__(self, x, y):
            self._x = x
            self._y = y

        def x(self):
            return self._x

        def y(self):
            return self._y

    shape = FakeShape(
        "person",
        "rectangle",
        [
            FakePoint(0, 0),
            FakePoint(10, 0),
            FakePoint(10, 10),
            FakePoint(0, 10),
        ],
    )
    result = judge_record([rect()], [shape], CLASSES)
    assert result.verdict == OK
    assert result.detail["matched"][0]["score"] == 0.75


def test_iou_matrix_and_hungarian():
    matrix = iou_matrix([rect()], [rect()])
    assert matrix[0][0] > 0.9
    pairs, unmatched_gt, unmatched_pred = hungarian_match(
        [[0.9, 0.0], [0.0, 0.8]]
    )
    assert sorted(pairs) == [(0, 0), (1, 1)]
    assert unmatched_gt == []
    assert unmatched_pred == []


def test_hungarian_maximises_total_iou():
    pairs, unmatched_gt, unmatched_pred = hungarian_match([[0.0, 0.5]])
    assert pairs == [(0, 1)]
    assert unmatched_gt == []
    assert unmatched_pred == [0]


def test_hungarian_prefers_the_largest_total_iou():
    matrix = [[0.9, 0.4], [0.4, 0.0]]
    pairs, unmatched_gt, unmatched_pred = hungarian_match(matrix)
    total = sum(matrix[row][column] for row, column in pairs)
    assert pairs == [(0, 0)]
    assert total == pytest.approx(0.9)
    # the same cardinality optimum of the 0/1 matrix would be worse
    assert total > matrix[0][1] + matrix[1][0]
    assert unmatched_gt == [1]
    assert unmatched_pred == [1]


def test_unknown_class_prediction_is_not_double_reported():
    result = judge_record([rect("car")], [rect("ghost")], CLASSES)
    assert result.verdict == NG
    assert PRED_UNKNOWN_CLASS in result.reasons
    assert CLASS_MISMATCH not in result.reasons
    assert result.detail["matched"][0]["class_state"] == "unknown_class"
    assert result.detail["unknown_class_pairs"]["matched_pairs"] == [
        {
            "gt_index": 0,
            "pred_index": 0,
            "gt_label": "car",
            "pred_label": "ghost",
        }
    ]


def test_unmatched_unknown_class_prediction_is_a_false_positive():
    result = judge_record([rect("car")], [rect("ghost", 40.0)], CLASSES)
    assert PRED_UNKNOWN_CLASS in result.reasons
    assert MISS_FP in result.reasons
    assert CLASS_MISMATCH not in result.reasons
    assert result.detail["false_positives"][0]["unknown_class"] is True
    assert result.detail["unknown_class_pairs"]["false_positive_indices"] == [
        0
    ]


def test_hungarian_reports_leftovers():
    pairs, unmatched_gt, unmatched_pred = hungarian_match([[0.5, 0.0, 0.0]])
    assert pairs == [(0, 0)]
    assert unmatched_pred == [1, 2]
