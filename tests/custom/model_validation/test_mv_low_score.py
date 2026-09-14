"""LOW_SCORE verdict tests: threshold rule, priority and detail."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from typing import Optional

from anylabeling.custom.model_validation import judge as judge_module
from anylabeling.custom.model_validation.judge import (
    CLASS_MISMATCH,
    IOU_BELOW,
    LOW_SCORE,
    MISS_FP,
    REASON_PRIORITY,
    judge_record,
)
from anylabeling.custom.model_validation.records import NG, OK

CLASSES = ["person", "car"]


def rect(
    label: str = "person",
    offset: float = 0.0,
    size: float = 10.0,
    score: Optional[float] = None,
) -> dict:
    "Return a rectangle shape, without a score when it is None."

    shape = {
        "label": label,
        "shape_type": "rectangle",
        "points": [
            [offset, offset],
            [offset + size, offset],
            [offset + size, offset + size],
            [offset, offset + size],
        ],
    }
    if score is not None:
        shape["score"] = score
    return shape


def multilabel(score: float, labels, scores, offset: float = 0.0) -> dict:
    "Return a prediction carrying the merged label rows of one target."

    shape = rect(offset=offset, score=score)
    shape["labels"] = list(labels)
    shape["scores"] = list(scores)
    return shape


def test_low_score_constant_is_exported():
    assert judge_module.LOW_SCORE == "LOW_SCORE"
    assert "LOW_SCORE" in judge_module.__all__


def test_no_threshold_keeps_the_rule_off():
    result = judge_record([rect()], [rect(score=0.01)], CLASSES)
    assert result.verdict == OK
    assert LOW_SCORE not in result.reasons
    assert result.detail["ng_score_threshold"] is None
    assert result.detail["low_score"] == []
    assert result.detail["matched"][0]["low_score"] is False


def test_a_low_scoring_prediction_turns_the_image_ng():
    ground_truth = [rect(), rect(offset=40.0)]
    predictions = [rect(score=0.9), rect(offset=40.0, score=0.2)]
    result = judge_record(
        ground_truth, predictions, CLASSES, ng_score_threshold=0.5
    )
    assert result.verdict == NG
    assert result.reasons == [LOW_SCORE]


def test_priority_is_after_class_mismatch_and_before_iou_below():
    ground_truth = [rect("person")]
    predictions = [rect("car", offset=7.0, score=0.1)]
    result = judge_record(
        ground_truth,
        predictions,
        CLASSES,
        ng_iou_threshold=0.5,
        ng_score_threshold=0.5,
    )
    assert result.reasons[:3] == [CLASS_MISMATCH, LOW_SCORE, IOU_BELOW]
    assert result.primary_reason == CLASS_MISMATCH
    order = REASON_PRIORITY.index
    assert order(CLASS_MISMATCH) < order(LOW_SCORE) < order(IOU_BELOW)


def test_low_score_and_iou_below_stack_on_the_same_pair():
    ground_truth = [rect()]
    predictions = [rect(offset=7.0, score=0.1)]
    result = judge_record(
        ground_truth,
        predictions,
        CLASSES,
        ng_iou_threshold=0.5,
        ng_score_threshold=0.5,
    )
    assert result.reasons == [LOW_SCORE, IOU_BELOW]
    pair = result.detail["matched"][0]
    assert pair["iou"] < 0.5
    assert pair["low_score"] is True


def test_low_score_does_not_hide_the_false_positive():
    ground_truth = [rect()]
    predictions = [rect(offset=40.0, score=0.1)]
    result = judge_record(
        ground_truth, predictions, CLASSES, ng_score_threshold=0.5
    )
    assert MISS_FP in result.reasons
    assert result.detail["false_positives"][0]["index"] == 0
    assert result.detail["low_score"] == [
        {"index": 0, "label": "person", "score": 0.1}
    ]


def test_detail_uses_the_prediction_index_coordinate_system():
    ground_truth = [rect(), rect(offset=40.0)]
    predictions = [
        rect(score=0.4),
        rect(offset=40.0, score=0.9),
        rect(offset=80.0, score=0.3),
    ]
    result = judge_record(
        ground_truth, predictions, CLASSES, ng_score_threshold=0.5
    )
    assert result.detail["ng_score_threshold"] == 0.5
    assert result.detail["low_score"] == [
        {"index": 0, "label": "person", "score": 0.4},
        {"index": 2, "label": "person", "score": 0.3},
    ]
    first = result.detail["matched"][0]
    assert first["pred_index"] == 0
    assert first["low_score"] is True
    assert result.detail["matched"][1]["pred_index"] == 1
    assert result.detail["matched"][1]["low_score"] is False
    assert result.detail["false_positives"][0]["index"] == 2


def test_a_prediction_without_a_score_is_skipped():
    result = judge_record(
        [rect()], [rect()], CLASSES, ng_score_threshold=0.5
    )
    assert result.verdict == OK
    assert LOW_SCORE not in result.reasons
    assert result.detail["low_score"] == []
    assert result.detail["matched"][0]["low_score"] is False


def test_scores_at_or_above_the_inference_floor_never_trigger():
    ground_truth = [rect(), rect(offset=40.0)]
    predictions = [rect(score=0.25), rect(offset=40.0, score=0.3)]
    for threshold in (0.25, 0.2, 0.0):
        result = judge_record(
            ground_truth, predictions, CLASSES,
            ng_score_threshold=threshold,
        )
        assert result.verdict == OK
        assert result.detail["low_score"] == []


def test_a_multilabel_shape_is_judged_by_its_main_score():
    high = multilabel(0.62, ["person", "car"], [0.62, 0.31])
    result = judge_record(
        [rect()], [high], CLASSES, ng_score_threshold=0.5
    )
    assert result.verdict == OK
    assert result.detail["low_score"] == []

    low = multilabel(0.4, ["person", "car"], [0.4, 0.31])
    result = judge_record(
        [rect()], [low], CLASSES, ng_score_threshold=0.5
    )
    assert result.verdict == NG
    assert result.reasons == [LOW_SCORE]
    assert result.detail["low_score"] == [
        {"index": 0, "label": "person", "score": 0.4}
    ]
    pair = result.detail["matched"][0]
    assert pair["score"] == 0.4
    assert pair["low_score"] is True
