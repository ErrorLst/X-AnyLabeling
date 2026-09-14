"""Judgement colours of the two canvases and the colour legend.

A box of the results page is coloured by what the matching of its record
did to it: a matched GT stays green and a matched prediction stays blue,
a GT no prediction met is orange red, a prediction no GT met is magenta,
a matched pair of two different classes is orange, a pair under the
IoU threshold is dark yellow and a matched pair whose prediction scored
under the NG score threshold is teal. Every assertion of this file
reads the rendered pixels of a real canvas - the RGBA the painter really
wrote - and the sizes of the real page, so the rule is checked as the
user sees it and not as the code intends it.

The record of the pixel tests carries one defect of every kind:
a GT no prediction reaches (a miss), a prediction no GT reaches (a false
positive), one matched pair and one matched pair whose IoU stays under
the threshold. The judgement detail is produced by the real judge, so
the indexes and the states under test are the ones a run really writes.

One prediction is one *box*, while the judge counts and matches one
*row* per class of that box: a box a whole image merge built carries
several classes and therefore several rows, and the colour of the box is
the highest priority state of them (see pred_statuses). The last section
of this file pins that aggregation for a real judgement detail, and pins
the reading of a detail written before the rows existed.
"""

import os
import os.path as osp

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import cv2
import numpy as np
import pytest

from PyQt6 import QtCore, QtGui, QtWidgets

from anylabeling.custom.model_validation import dataset
from anylabeling.custom.model_validation import judge as judge_module
from anylabeling.custom.model_validation import multilabel as labels_module
from anylabeling.custom.model_validation import records as records_module
from anylabeling.custom.model_validation.multilabel import (
    expand_multilabel_rows,
)
from anylabeling.custom.model_validation.ui import (
    image_view as image_view_module,
)
from anylabeling.custom.model_validation.ui import (
    results_page as results_page_module,
)
from anylabeling.custom.model_validation.ui.dialog import (
    MINIMUM_WIDTH,
    ModelValidationDialog,
)
from anylabeling.custom.model_validation.ui.image_view import (
    CLASS_MISMATCH_COLOR,
    FALSE_POSITIVE_COLOR,
    GT_COLOR,
    IOU_BELOW_COLOR,
    LOW_SCORE_COLOR,
    MISS_COLOR,
    PRED_COLOR,
    STATE_CLASS_MISMATCH,
    STATE_FALSE_POSITIVE,
    STATE_IOU_BELOW,
    STATE_LOW_SCORE,
    STATE_MISS,
    STATE_OK_PAIR,
    ImageCanvas,
    shape_color,
    shape_colors,
)
from anylabeling.custom.model_validation.ui.results_page import (
    LEGEND_HTML,
    LEGEND_TOOLTIP,
    ResultsPage,
    gt_statuses,
    legend_html,
    _legacy_pred_statuses,
    matched_pair_state,
    pred_statuses,
)

CLASSES = ["a0_dian", "a1_xian"]
# the two canvases refuse a size below their own minimum (320 x 320)
WIDGET_SIZE = (420, 320)
# the flat grey picture the strokes are probed on: no constant colour and
# no white glyph blends into it
PICTURE_FILL = 30
# the picture the boxes are laid out on: 260 x 120, which the canvas
# fits at about 1.6 widget pixels per image pixel
IMAGE_WIDTH = 260
IMAGE_HEIGHT = 120
# The box the judge matches at a high IoU, the box it only meets below
# the threshold, the GT it never meets and the prediction it never
# matches: one defect of every kind on the very same record. The boxes
# are laid out side by side across the picture, wide enough apart that a
# stroke of one of them never reaches the sample of another one.
MATCHED = ((10, 10), (50, 50))
LOW_IOU = ((60, 10), (100, 50))
MISSED = ((110, 10), (150, 50))
FALSE_POSITIVE = ((160, 10), (210, 50))
# The boxes of the multi label section, on the lower half of the same
# picture and far enough from the four boxes above that a stroke probe
# never reaches a neighbour. TWO_LEFT_A and TWO_LEFT_B are the two
# anchors the engine answered for one object: their IoU is 0.95, over
# the 0.5 threshold of these fixtures, so the whole image merge folds
# them into the one box the canvas draws.
TWO_LEFT_A = ((10, 60), (50, 100))
TWO_LEFT_B = ((12, 62), (52, 102))
TWO_RIGHT = ((210, 60), (250, 100))
LOW_SCORE_BOX = ((60, 60), (100, 100))
# the box of the row of the low score fixture no pair points at: it sits
# just below the ground truth, so it stays unmatched without leaving the
# picture
FREE_ROW_BOX = ((60, 80), (100, 120))
# Half of the low IoU box overlaps this prediction - 20 x 40 pixels of
# it, 800 in all - and the union is 1600 + 1600 - 800 = 2400, so its IoU
# is exactly 1/3: above zero, therefore matched, and under the 0.5
# threshold, therefore flagged as a pair the run only half met.
LOW_IOU_PRED = ((80, 10), (120, 50))
BOXES = (MATCHED, LOW_IOU, MISSED, FALSE_POSITIVE)
# the two strokes of a corner meet inside this many pixels of the edges
PROBE_TOLERANCE = 2
# a two pixel outline of a side of at least thirty widget pixels is worth
# far more than this; the floor only guards against probing nothing
MIN_STROKE_PIXELS = 10


@pytest.fixture(scope="module")
def qt_app():
    "Create the offscreen application the page tests need."

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def rect(start, end, label="a0_dian", score=None) -> dict:
    "Return one rectangle of the picture coordinate system."

    shape = {
        "label": label,
        "shape_type": "rectangle",
        "points": [
            list(start),
            [end[0], start[1]],
            list(end),
            [start[0], end[1]],
        ],
    }
    if score is not None:
        shape["score"] = score
    return shape


def gt_shapes():
    "Return the four ground truth boxes of the record under test."

    return [rect(start, end) for start, end in BOXES[:3]]


def pred_shapes():
    "Return the three predictions of the record under test."

    return [
        rect(*MATCHED, score=0.91),
        rect(*LOW_IOU_PRED, score=0.77),
        rect(*FALSE_POSITIVE, score=0.64),
    ]


def judged_record() -> records_module.ValidationRecord:
    """Return a record whose detail is the judgement of the real judge.

    The predictions are the payload the pipeline stores, so the canvas
    and the judge read the very same shape list.
    """

    ground_truth = gt_shapes()
    predictions = pred_shapes()
    result = judge_module.judge_record(
        ground_truth, predictions, CLASSES, ng_iou_threshold=0.5
    )
    record = records_module.ValidationRecord(
        record_id="status-colors",
        kind=records_module.KIND_ORIGINAL,
        relpath="a.png",
        staging_image_path="",
        staging_label_path="",
    )
    record.detail = dict(result.detail)
    record.detail["predictions"] = predictions
    record.judged = True
    record.verdict = result.verdict
    return record


# The NG score threshold the low score rule is judged with. It sits
# over every prediction score of the record (0.91, 0.77, 0.64), so all
# three predictions carry the judge's low score flag. The two matched
# pairs turn teal; the unmatched one keeps the magenta of its own side,
# because a false positive is the more important signal of its box (see
# test_an_unmatched_low_score_box_stays_magenta below).
LOW_SCORE_THRESHOLD = 0.95


def low_score_record() -> records_module.ValidationRecord:
    """Return the record judged with a threshold over its own scores.

    The predictions and the matching of the record are the ones above;
    only the NG score threshold is part of the judgement, so every
    prediction carries a True low score flag. The unmatched prediction -
    the pair no match points at - keeps its magenta all the same,
    however low its score is.
    """

    record = judged_record()
    detail = judge_module.judge_record(
        gt_shapes(),
        pred_shapes(),
        CLASSES,
        ng_iou_threshold=0.5,
        ng_score_threshold=LOW_SCORE_THRESHOLD,
    ).detail
    record.detail = dict(detail)
    record.detail["predictions"] = pred_shapes()
    return record


def multilabel_rows(labels, scores, box, shape_type="rectangle") -> dict:
    """Return one payload box carrying one label row per class.

    The payload of a merged box is a single entry whose parallel lists
    labels and scores hold one row per class, exactly what
    pipeline._prediction_payload writes for a merged shape. The points
    of the entry are the geometry of the highest scoring row, which is
    what a merge of the rows writes as well.
    """

    return {
        "label": labels[0],
        "shape_type": shape_type,
        "points": [list(point) for point in box],
        "score": scores[0],
        "labels": list(labels),
        "scores": list(scores),
    }


def merged_points(box) -> list:
    """Return the point list of a box, the way a merge normalises it."""

    return [[float(x), float(y)] for x, y in box]


def merged_record(
    row_payload,
    ground_truth,
    record_id,
    iou_threshold=0.5,
    score_threshold=None,
) -> records_module.ValidationRecord:
    """Return the record of a payload of rows run through the real merge.

    The judged image is the one of the new revision: the row payload is
    folded by the whole image merge (the highest scoring row of every
    cluster carries the parallel lists of its classes, and the two
    anchors of one object are compared without their class), and the
    very rows of that merge are what the judge counts. The verdict and
    the payload therefore live in two coordinate systems, exactly like
    the ones of pipeline._infer_one: every index of the detail is a row,
    the payload is the box the canvas draws and pred_row_boxes maps one
    onto the other.
    """

    payload = labels_module.merge_overlapping_predictions(
        row_payload, iou_threshold
    )
    rows, row_boxes = expand_multilabel_rows(payload)
    result = judge_module.judge_record(
        ground_truth,
        rows,
        CLASSES,
        ng_iou_threshold=0.5,
        ng_score_threshold=score_threshold,
    )
    record = records_module.ValidationRecord(
        record_id=record_id,
        kind=records_module.KIND_ORIGINAL,
        relpath=f"{record_id}.png",
        staging_image_path="",
        staging_label_path="",
    )
    record.detail = dict(result.detail)
    record.detail["predictions"] = payload
    record.detail["pred_row_boxes"] = row_boxes
    record.judged = True
    record.verdict = result.verdict
    return record


def two_box_record() -> records_module.ValidationRecord:
    """Return a record of two objects, one of them found twice.

    The engine answered the left object twice - two anchors of one box
    that only differ by a few pixels, which is what the whole image
    merge exists for - and the right one once. The merged left box
    carries two classes, one of them matched: the very case the
    aggregation rule of the box colours is about.
    """

    row_payload = [
        multilabel_rows(["a0_dian"], [0.95], TWO_LEFT_A),
        multilabel_rows(["c2_dian"], [0.90], TWO_LEFT_B),
        multilabel_rows(["a0_dian"], [0.80], TWO_LEFT_A),
        multilabel_rows(["c3_dian"], [0.70], TWO_LEFT_A),
        multilabel_rows(["c1_dian"], [0.60], TWO_RIGHT),
    ]
    ground_truth = [rect(*TWO_LEFT_A, label="a0_dian")]
    return merged_record(row_payload, ground_truth, "status-colors-rows")


def low_score_row_record() -> records_module.ValidationRecord:
    """Return a record whose one matched row scores under the threshold.

    One object and two boxes: the anchor on the ground truth carries its
    class and is matched, the anchor below it carries another class of
    the run and matches nothing. Every score of the record sits under
    the NG score threshold of the run, so the matched row is the low
    score state of the judge - what the box has to show even though the
    unmatched row of the very same box is a false positive.
    """

    row_payload = [
        multilabel_rows(["a0_dian"], [0.90], LOW_SCORE_BOX),
        multilabel_rows(["c0_dian"], [0.40], FREE_ROW_BOX),
    ]
    ground_truth = [rect(*LOW_SCORE_BOX, label="a0_dian")]
    payload = labels_module.merge_overlapping_predictions(row_payload, 0.5)
    rows, row_boxes = expand_multilabel_rows(payload)
    result = judge_module.judge_record(
        ground_truth,
        rows,
        CLASSES,
        ng_iou_threshold=0.5,
        ng_score_threshold=LOW_SCORE_THRESHOLD,
    )
    record = records_module.ValidationRecord(
        record_id="status-colors-low-row",
        kind=records_module.KIND_ORIGINAL,
        relpath="status-colors-low-row.png",
        staging_image_path="",
        staging_label_path="",
    )
    record.detail = dict(result.detail)
    record.detail["predictions"] = payload
    record.detail["pred_row_boxes"] = row_boxes
    record.judged = True
    record.verdict = result.verdict
    return record


def pair_palette_record() -> records_module.ValidationRecord:
    """Return a record whose two rows of one box disagree on the class.

    The one box carries two rows at the same place - the record holds
    two ground truth boxes there, one on each anchor - so the judge
    pairs its first row (a0_dian) with the ground truth of the very same
    class (a match) and its second one (a1_xian) with the other (a class
    mismatch). Both scores stay under the NG score threshold of the
    fixture, therefore the mismatched pair is a class mismatch AND a low
    score at once and only the priority rule of the aggregation can
    decide the colour of the box.
    """

    row_payload = [
        multilabel_rows(["a0_dian"], [0.90], TWO_LEFT_A),
        multilabel_rows(["a1_xian"], [0.80], TWO_LEFT_A),
    ]
    ground_truth = [
        rect(*TWO_LEFT_A, label="a0_dian"),
        rect(*TWO_LEFT_B, label="a0_dian"),
    ]
    return merged_record(
        row_payload,
        ground_truth,
        "status-colors-priority",
        score_threshold=LOW_SCORE_THRESHOLD,
    )


def ignored_shape_record() -> records_module.ValidationRecord:
    """Return a record whose row list starts with an ignored shape.

    The payload holds a point - a shape type the judge never matches -
    in front of the rectangle the one ground truth meets, which is the
    order a run of the obb / pose tasks keeps (their merge answers the
    first seen order). The row list is therefore [point, rectangle]
    while the verdict was handed the rectangle alone: its own
    pred_index 0 is the row 1 of the screen, the very shift the reading
    of that index has to undo.
    """

    payload = [
        {
            "label": "a1_xian",
            "shape_type": "point",
            "points": [[5, 5]],
            "score": 0.90,
        },
        rect(*TWO_LEFT_A, label="a0_dian", score=0.80),
    ]
    rows, row_boxes = expand_multilabel_rows(payload)
    ground_truth = [rect(*TWO_LEFT_A, label="a1_xian")]
    result = judge_module.judge_record(
        ground_truth, rows, CLASSES, ng_iou_threshold=0.5
    )
    record = records_module.ValidationRecord(
        record_id="status-colors-ignored",
        kind=records_module.KIND_ORIGINAL,
        relpath="status-colors-ignored.png",
        staging_image_path="",
        staging_label_path="",
    )
    record.detail = dict(result.detail)
    record.detail["predictions"] = payload
    record.detail["pred_row_boxes"] = row_boxes
    record.judged = True
    record.verdict = result.verdict
    return record


def test_a_matched_row_beside_an_ignored_shape_keeps_its_own_box(qt_app):
    """The judge index counts matchable rows, never the rows on screen.

    The row list of the record is [point, rectangle] while the verdict
    was handed the rectangle alone, so its pred_index 0 points at the
    row 1 of the screen. The matched row is the one of another class, so
    its box is the orange of a class mismatch, and the point keeps the
    plain colour of an ignored shape (see pred_valid). A reader of that
    index that skipped the mapping back to the row position would flag
    the matched row as a false positive instead.
    """

    record = ignored_shape_record()
    detail = record.detail
    payload = detail["predictions"]
    rows, row_boxes = expand_multilabel_rows(payload)

    # the fixture really is the shape under test: two rows on screen,
    # one of them matchable, and a verdict whose pair sits at the judge
    # index 0 and at the row position 1
    assert [row["shape_type"] for row in rows] == ["point", "rectangle"]
    assert row_boxes == [0, 1]
    assert detail["pred_row_boxes"] == row_boxes
    assert detail["pred_total"] == 2
    assert detail["pred_valid"] == 1
    assert [pair["pred_index"] for pair in detail["matched"]] == [0]
    assert [pair["class_state"] for pair in detail["matched"]] == [
        "mismatch"
    ]

    states = pred_statuses(detail, payload)
    assert states == ["", STATE_CLASS_MISMATCH]

    canvas = make_canvas()
    canvas.set_shapes([], payload, (), states)
    rendered = render_rgba(canvas)
    assert (
        stroke_pixels(canvas, rendered, CLASS_MISMATCH_COLOR, *TWO_LEFT_A)
        > MIN_STROKE_PIXELS
    )
    # the matched row is never painted as the false positive of another
    # row of the list
    assert (
        stroke_pixels(
            canvas, rendered, FALSE_POSITIVE_COLOR, *TWO_LEFT_A
        )
        == 0
    )


def test_a_box_takes_the_highest_state_of_its_rows(qt_app):
    """A merged box is coloured by the most important state of its rows.

    The record holds two objects and two boxes: the left object was
    answered twice by the engine, so its two anchors became one box of
    four rows, and the right object keeps its own box. The matched row
    of the left box is joined by the false positive of the rows no
    ground truth met, and the false positive is the state the box shows
    - on the stroke as well as in the state list.
    """

    record = two_box_record()
    payload = record.detail["predictions"]
    rows, row_boxes = expand_multilabel_rows(payload)

    # the fixture really is the shape under test: the two overlapping
    # anchors became one box of four rows and the third one its own box
    assert len(payload) == 2
    assert row_boxes == [0, 0, 0, 1]
    assert record.detail["pred_row_boxes"] == row_boxes
    assert len(rows) == record.detail["pred_total"] == 4
    assert record.detail["pred_valid"] == 4
    assert [row["label"] for row in rows] == [
        "a0_dian",
        "c2_dian",
        "c3_dian",
        "c1_dian",
    ]
    assert payload[0]["points"] == merged_points(TWO_LEFT_A)
    assert payload[0]["labels"] == [
        "a0_dian",
        "c2_dian",
        "c3_dian",
    ]
    # one row of the left box is matched (the a0_dian one, the only row
    # the class table of the fixture knows), the two other rows of it
    # are not, and neither is the only row of the right box
    assert [pair["pred_index"] for pair in record.detail["matched"]] == [0]
    assert [pair["class_state"] for pair in record.detail["matched"]] == [
        "match"
    ]
    assert [item["index"] for item in record.detail["false_positives"]] == [
        1,
        2,
        3,
    ]

    states = pred_statuses(record.detail, payload)
    assert states == [STATE_FALSE_POSITIVE, STATE_FALSE_POSITIVE]

    canvas = make_canvas()
    canvas.set_shapes([], payload, (), states)
    rendered = render_rgba(canvas)
    for start, end in (TWO_LEFT_A, TWO_RIGHT):
        assert (
            stroke_pixels(
                canvas, rendered, FALSE_POSITIVE_COLOR, start, end
            )
            > MIN_STROKE_PIXELS
        )
    # the matched row of the left box does not repaint it blue
    assert stroke_pixels(canvas, rendered, PRED_COLOR, *TWO_LEFT_A) == 0


def test_the_state_of_a_matched_row_reaches_its_own_box(qt_app):
    """Two boxes of one card: the state of a matched row reaches its box.

    The anchor on the ground truth is matched and scores under the NG
    score threshold of the run, so its own row is the low score state;
    the anchor below it is a matchable row the judge never pairs, so it
    is the false positive of its own box. Each box therefore shows the
    state of its own row and the two colours coexist on the one card.
    """

    record = low_score_row_record()
    payload = record.detail["predictions"]
    rows, row_boxes = expand_multilabel_rows(payload)

    assert len(payload) == 2
    assert row_boxes == [0, 1]
    assert len(rows) == record.detail["pred_total"] == 2
    assert record.detail["pred_valid"] == 2
    assert [pair["pred_index"] for pair in record.detail["matched"]] == [0]
    assert record.detail["matched"][0]["low_score"] is True
    assert [item["index"] for item in record.detail["false_positives"]] == [
        1
    ]

    states = pred_statuses(record.detail, payload)
    assert states == [STATE_LOW_SCORE, STATE_FALSE_POSITIVE]

    canvas = make_canvas()
    canvas.set_shapes([], payload, (), states)
    rendered = render_rgba(canvas)
    assert (
        stroke_pixels(canvas, rendered, LOW_SCORE_COLOR, *LOW_SCORE_BOX)
        > MIN_STROKE_PIXELS
    )
    assert (
        stroke_pixels(
            canvas, rendered, FALSE_POSITIVE_COLOR, *FREE_ROW_BOX
        )
        > MIN_STROKE_PIXELS
    )
    # neither the plain colour of the matched row nor a colour of the
    # other box reached the box under test
    assert stroke_pixels(canvas, rendered, PRED_COLOR, *LOW_SCORE_BOX) == 0
    assert (
        stroke_pixels(canvas, rendered, LOW_SCORE_COLOR, *FREE_ROW_BOX) == 0
    )


def test_the_class_mismatch_of_a_row_wins_over_its_low_score(qt_app):
    """The class mismatch of a row wins over the low score of the same box.

    The one box carries two rows at the same place and the record holds
    two ground truth boxes there, so the judge pairs the first row with
    the ground truth of its own class (a match) and the second one - of
    another class the run knows - with the other (a class mismatch).
    Both scores of the record sit under
    the NG score threshold, therefore the mismatched pair carries a
    class mismatch AND a low score at once: the class mismatch is the
    reason the judge ranks in front of the low score, so the box is
    orange and never teal.
    """

    record = pair_palette_record()
    payload = record.detail["predictions"]
    rows, row_boxes = expand_multilabel_rows(payload)

    assert len(payload) == 1
    assert row_boxes == [0, 0]
    assert len(rows) == record.detail["pred_total"] == 2
    assert record.detail["pred_valid"] == 2
    assert [pair["pred_index"] for pair in record.detail["matched"]] == [
        0,
        1,
    ]
    assert [pair["class_state"] for pair in record.detail["matched"]] == [
        "match",
        "mismatch",
    ]
    # the two matched rows carry the low score flag of the judge, the
    # mismatched one included: the priority rule is what decides, never
    # the mere presence of the flag (see matched_pair_state)
    assert [pair["low_score"] for pair in record.detail["matched"]] == [
        True,
        True,
    ]
    assert record.detail["false_positives"] == []

    states = pred_statuses(record.detail, payload)
    assert states == [STATE_CLASS_MISMATCH]

    canvas = make_canvas()
    canvas.set_shapes([], payload, (), states)
    rendered = render_rgba(canvas)
    assert (
        stroke_pixels(canvas, rendered, CLASS_MISMATCH_COLOR, *TWO_LEFT_A)
        > MIN_STROKE_PIXELS
    )
    # neither the low score of the very same pair nor the plain colour
    # of the matched row is what the box shows
    assert (
        stroke_pixels(canvas, rendered, LOW_SCORE_COLOR, *TWO_LEFT_A) == 0
    )
    assert stroke_pixels(canvas, rendered, PRED_COLOR, *TWO_LEFT_A) == 0


# --------------------------------------- a detail written before the rows
def test_a_detail_without_the_row_map_keeps_the_legacy_colours(qt_app):
    """A detail of the previous revision is coloured box by box.

    Such a detail indexes its payload boxes directly and carries no
    pred_row_boxes at all, which is what an old record, an old report
    and the legacy fixtures of this file look like. The page has to
    colour every box exactly like it did before the rows existed, so the
    frozen legacy rule and the new entry point are compared on the very
    same detail: the two answers line up entry by entry.
    """

    record = judged_record()
    detail = dict(record.detail)
    assert "pred_row_boxes" not in detail

    expected = _legacy_pred_statuses(detail, detail["predictions"])
    assert pred_statuses(detail, detail["predictions"]) == expected
    assert expected == [
        STATE_OK_PAIR,
        STATE_IOU_BELOW,
        STATE_FALSE_POSITIVE,
    ]

    # and the very colours of the boxes did not move either
    payload = detail["predictions"]
    colors = shape_colors(payload, PRED_COLOR, expected)
    assert rgba(colors[0]) == rgba(PRED_COLOR)
    assert rgba(colors[1]) == rgba(IOU_BELOW_COLOR)
    assert rgba(colors[2]) == rgba(FALSE_POSITIVE_COLOR)


def test_a_row_map_that_does_not_line_up_keeps_every_box_plain(qt_app):
    """A map or a payload of another revision flags no box at all.

    The guard of the new rule is the count of the rows and the box of
    every one of them: a detail whose payload lost a merged box, or
    whose map does not describe the boxes on screen, is answered with
    the plain Pred colour of every box instead of a half coloured
    picture, and never with an exception.
    """

    record = two_box_record()
    payload = record.detail["predictions"]
    # the payload on screen is the one of the previous revision: the box
    # the verdict counted once is not there any more
    assert pred_statuses(record.detail, payload[:1]) == [""]

    # a map of the wrong length, a map naming a box that is not shown, a
    # map whose values are no count at all
    for broken in ([0, 0], [0, 0, 0, 0, 0, 9], [0, 0, 0, 0, 0, "x"]):
        detail = dict(record.detail)
        detail["pred_row_boxes"] = broken
        assert pred_statuses(detail, payload) == ["", ""], broken

    # the rows of the boxes on screen disagree with the rows the verdict
    # counted: the boxes keep their plain colour, one by one
    detail = dict(record.detail)
    detail["pred_total"] = 6
    assert pred_statuses(detail, payload) == ["", ""]

    canvas = make_canvas()
    canvas.set_shapes([], payload, (), pred_statuses(detail, payload))
    rendered = render_rgba(canvas)
    for start, end in (TWO_LEFT_A, TWO_RIGHT):
        assert (
            stroke_pixels(canvas, rendered, PRED_COLOR, start, end)
            > MIN_STROKE_PIXELS
        )


def legacy_routes(monkeypatch) -> list:
    """Return the log of the calls the new rule makes to the legacy one.

    The frozen legacy rule and the body of the new one are the two
    routes of pred_statuses, so a spy on the legacy rule tells which
    branch a case really took: an empty log is the branch of the new
    rule (its row guard and its aggregation), a filled one is the
    fallback of a detail the new rule refuses to read row by row.
    """

    calls: list = []
    original = results_page_module._legacy_pred_statuses

    def spy(detail, shapes):
        calls.append(detail)
        return original(detail, shapes)

    monkeypatch.setattr(
        results_page_module, "_legacy_pred_statuses", spy
    )
    return calls


def test_the_matchable_row_guard_answers_without_the_legacy_rule(
    qt_app, monkeypatch
):
    """The count of the matchable rows is a branch of the new rule.

    The row map of the record is the one of its verdict, so the detail
    is read row by row: its two payload boxes do not fit the four rows
    the verdict counted at all, therefore the frozen legacy rule would
    answer them with the plain colours, while the new rule colours both
    of them. Breaking the count of the matchable rows alone keeps that
    very route - no guard of the row map reads pred_valid - and only
    makes the new rule degrade the whole record to the plain colours:
    its own branch, and the spy sees no legacy call at all.
    """

    record = two_box_record()
    payload = record.detail["predictions"]
    calls = legacy_routes(monkeypatch)

    # the row path colours the two boxes and never asks the legacy rule
    assert pred_statuses(record.detail, payload) == [
        STATE_FALSE_POSITIVE,
        STATE_FALSE_POSITIVE,
    ]
    assert calls == []

    # one matchable row short: the guard of the new rule fails and every
    # box keeps the plain colour, still without any legacy call
    detail = dict(record.detail)
    detail["pred_valid"] = 3
    assert pred_statuses(detail, payload) == ["", ""]
    assert calls == []


def test_a_row_map_of_another_row_list_falls_back_to_the_legacy_rule(
    qt_app, monkeypatch
):
    """A map that rebuilds another row list is refused as a whole.

    The permuted map has the length the verdict counted and its values
    are all in range, so the counts of the map itself pass; the rows it
    describes are not the rows the payload rebuilds, so the new rule
    hands the detail back to the frozen legacy rule (one call per case,
    the spy sees them), which reads the two boxes of the payload box by
    box and answers the plain colours here. Using that map would have
    shifted the matched state of the first row onto the second box.
    """

    record = two_box_record()
    payload = record.detail["predictions"]
    calls = legacy_routes(monkeypatch)

    # the map the record really carries takes the new path
    assert pred_statuses(record.detail, payload) == [
        STATE_FALSE_POSITIVE,
        STATE_FALSE_POSITIVE,
    ]

    # a map of legal values in another order, then three maps that are
    # no row list at all: the last three are refused by the reader of
    # the map before any count is read (see _row_box_indexes)
    for odd in ([0, 0, 1, 0], "0001", 5, {"0": 0}):
        detail = dict(record.detail)
        detail["pred_row_boxes"] = odd
        assert pred_statuses(detail, payload) == ["", ""], odd

    # every one of the four went through the legacy fallback
    assert len(calls) == 4


def picture() -> np.ndarray:
    "Return the flat grey picture of the record under test."

    image = np.full(
        (IMAGE_HEIGHT, IMAGE_WIDTH, 3), PICTURE_FILL, dtype=np.uint8
    )
    return image


def make_canvas() -> ImageCanvas:
    "Return a fitted canvas showing the flat picture."

    canvas = ImageCanvas("")
    canvas.resize(*WIDGET_SIZE)
    image = picture()
    ok, buffer = cv2.imencode(".png", image)
    assert ok
    pixmap = QtGui.QPixmap()
    assert pixmap.loadFromData(buffer.tobytes())
    canvas.set_image(pixmap)
    return canvas


def render_rgba(canvas) -> np.ndarray:
    "Render a canvas offscreen and return its pixels as an RGBA array."

    result = QtGui.QImage(canvas.size(), QtGui.QImage.Format.Format_RGBA8888)
    result.fill(QtGui.QColor(0, 0, 0))
    painter = QtGui.QPainter(result)
    canvas.render(
        painter,
        QtCore.QPoint(0, 0),
        QtGui.QRegion(canvas.rect()),
        QtWidgets.QWidget.RenderFlag.DrawWindowBackground,
    )
    painter.end()
    pointer = result.constBits()
    pointer.setsize(result.sizeInBytes())
    pixels = np.frombuffer(
        bytes(pointer), np.uint8, count=result.sizeInBytes()
    )
    pixels = pixels.reshape(result.height(), result.width(), 4)
    return np.ascontiguousarray(pixels)


def stroke_pixels(
    canvas,
    rendered,
    color: QtGui.QColor,
    start,
    end,
    tolerance: int = PROBE_TOLERANCE,
):
    """Return the count of the stroke pixels of one box of a render.

    The rectangle of the box lies on the four edges the points describe,
    which are sampled as whole pixel lines: the strokes around them are
    fully covered there, so a pixel of the outline holds the pen colour
    itself. The sample is accepted within a pixel or two of the edge, so
    the box really is the one painted in this colour, and the colour must
    be exactly the one the constants name - the pen is opaque, therefore
    an antialiased pixel blends towards the picture instead of towards
    white and can never flood the sample.
    """

    rgb = np.array(
        [color.red(), color.green(), color.blue(), color.alpha()], np.int16
    )
    painted = np.all(np.abs(rendered.astype(np.int16) - rgb) == 0, axis=2)
    near = np.zeros(painted.shape, bool)
    for point in (
        (start[0], start[1]),
        (end[0], start[1]),
        (end[0], end[1]),
        (start[0], end[1]),
    ):
        corner = canvas.image_to_widget(
            QtCore.QPointF(float(point[0]), float(point[1]))
        )
        column = int(round(corner.x()))
        row = int(round(corner.y()))
        near[
            max(row - tolerance, 0) : row + tolerance + 1,
            max(column - tolerance, 0) : column + tolerance + 1,
        ] = True
    return int(np.count_nonzero(painted & near))


def rgba(colors) -> list:
    """Return the RGBA tuples of a colour or of a list of colours.

    QColor never compares equal to another QColor through a list, so the
    colours the helpers hand back are read as the four numbers they
    carry.
    """

    if isinstance(colors, (list, tuple)):
        return [rgba(color) for color in colors]
    return colors.red(), colors.green(), colors.blue(), colors.alpha()


def test_the_judge_detail_of_the_record_has_the_expected_defects(qt_app):
    "The record of the pixel tests really holds one defect of every kind."

    record = judged_record()
    detail = record.detail

    # the threshold of the run sits between the two matched IoUs: the
    # first pair is perfect, the second one is a third
    assert [pair["iou"] for pair in detail["matched"]] == pytest.approx(
        [1.0, 1.0 / 3.0], abs=1e-6
    )
    assert detail["ng_iou_threshold"] == pytest.approx(0.5)
    assert [pair["gt_index"] for pair in detail["matched"]] == [0, 1]
    assert [pair["pred_index"] for pair in detail["matched"]] == [0, 1]
    assert [pair["class_state"] for pair in detail["matched"]] == [
        "match",
        "match",
    ]
    assert [item["index"] for item in detail["missed_gt"]] == [2]
    assert [item["index"] for item in detail["false_positives"]] == [2]
    assert detail["gt_region"] == detail["gt_total"] == 3
    assert detail["pred_valid"] == detail["pred_total"] == 3


def test_the_states_of_a_judged_record_are_the_ones_its_detail_reports(
    qt_app,
):
    "One state per shape: matched, under the threshold, miss, false positive."

    record = judged_record()
    ground_truth = gt_shapes()
    predictions = record.detail["predictions"]

    assert gt_statuses(record.detail, ground_truth) == [
        STATE_OK_PAIR,
        STATE_IOU_BELOW,
        STATE_MISS,
    ]
    assert pred_statuses(record.detail, predictions) == [
        STATE_OK_PAIR,
        STATE_IOU_BELOW,
        STATE_FALSE_POSITIVE,
    ]


def test_the_status_lists_never_write_into_the_record_detail(qt_app):
    "The states are a display side copy: the detail stays as it was."

    record = judged_record()
    before = {key: repr(value) for key, value in record.detail.items()}
    gt_statuses(record.detail, gt_shapes())
    pred_statuses(record.detail, record.detail["predictions"])

    assert {key: repr(value) for key, value in record.detail.items()} == (
        before
    )


def test_every_box_of_the_ground_truth_canvas_has_its_own_colour(qt_app):
    """Pixel evidence: the RGBA of every stroke of the GT canvas.

    The canvas is handed the states of the real judgement, so the three
    strokes have to be the green of a matched GT, the dark yellow of a
    pair under the threshold and the orange red of a miss.
    """

    record = judged_record()
    ground_truth = gt_shapes()
    canvas = make_canvas()
    canvas.set_shapes(
        ground_truth, [], gt_statuses(record.detail, ground_truth), ()
    )
    rendered = render_rgba(canvas)

    matched = stroke_pixels(canvas, rendered, GT_COLOR, *MATCHED)
    below = stroke_pixels(canvas, rendered, IOU_BELOW_COLOR, *LOW_IOU)
    missed = stroke_pixels(canvas, rendered, MISS_COLOR, *MISSED)
    assert matched > MIN_STROKE_PIXELS
    assert below > MIN_STROKE_PIXELS
    assert missed > MIN_STROKE_PIXELS
    # every stroke is opaque and beside the two others: the colours are
    # really the ones the constants name, at the RGBA level
    assert GT_COLOR == QtGui.QColor(46, 204, 113)
    assert MISS_COLOR == QtGui.QColor(231, 76, 60)
    assert IOU_BELOW_COLOR == QtGui.QColor(241, 196, 15)
    for color in (GT_COLOR, IOU_BELOW_COLOR, MISS_COLOR):
        assert color.alpha() == 255


def test_every_box_of_the_prediction_canvas_has_its_own_colour(qt_app):
    "Pixel evidence: the blue, the yellow and the magenta of a prediction."

    record = judged_record()
    predictions = record.detail["predictions"]
    canvas = make_canvas()
    canvas.set_shapes(
        [], predictions, (), pred_statuses(record.detail, predictions)
    )
    rendered = render_rgba(canvas)

    matched = stroke_pixels(canvas, rendered, PRED_COLOR, *MATCHED)
    below = stroke_pixels(canvas, rendered, IOU_BELOW_COLOR, *LOW_IOU_PRED)
    false_positive = stroke_pixels(
        canvas, rendered, FALSE_POSITIVE_COLOR, *FALSE_POSITIVE
    )
    assert matched > MIN_STROKE_PIXELS
    assert below > MIN_STROKE_PIXELS
    assert false_positive > MIN_STROKE_PIXELS
    assert PRED_COLOR == QtGui.QColor(52, 152, 219)
    assert FALSE_POSITIVE_COLOR == QtGui.QColor(155, 89, 182)
    assert PRED_COLOR.alpha() == 255
    assert FALSE_POSITIVE_COLOR.alpha() == 255


def test_a_class_mismatch_is_painted_orange_on_both_sides(qt_app):
    "A matched pair of two classes: orange on the GT and on the Pred box."

    ground_truth = [rect(*MATCHED, label="a0_dian")]
    predictions = [rect(*MATCHED, label="a1_xian", score=0.88)]
    detail = judge_module.judge_record(
        ground_truth, predictions, CLASSES, ng_iou_threshold=0.5
    ).detail

    assert gt_statuses(detail, ground_truth) == [STATE_CLASS_MISMATCH]
    assert pred_statuses(detail, predictions) == [STATE_CLASS_MISMATCH]
    assert shape_color(STATE_CLASS_MISMATCH) == CLASS_MISMATCH_COLOR
    assert CLASS_MISMATCH_COLOR == QtGui.QColor(230, 126, 34)

    for side, shapes, statuses in (
        ("gt", ground_truth, gt_statuses(detail, ground_truth)),
        ("pred", predictions, pred_statuses(detail, predictions)),
    ):
        canvas = make_canvas()
        canvas.set_shapes(
            shapes if side == "gt" else [],
            [] if side == "gt" else shapes,
            statuses if side == "gt" else (),
            () if side == "gt" else statuses,
        )
        painted = stroke_pixels(
            canvas, render_rgba(canvas), CLASS_MISMATCH_COLOR, *MATCHED
        )
        assert painted > MIN_STROKE_PIXELS, side


def test_a_record_without_a_judgement_keeps_the_plain_colours(qt_app):
    "A skipped or never judged record is neither a miss nor a false hit."

    shapes = gt_shapes()
    predictions = pred_shapes()
    assert gt_statuses({}, shapes) == [""] * len(shapes)
    assert pred_statuses({}, predictions) == [""] * len(predictions)

    # the two overlays are shown one canvas each, the way the page shows
    # them: on one canvas the prediction of a matched pair would be
    # painted right on top of its own GT box
    ground_truth_canvas = make_canvas()
    ground_truth_canvas.set_shapes(shapes, [])
    prediction_canvas = make_canvas()
    prediction_canvas.set_shapes([], predictions)
    ground_truth_render = render_rgba(ground_truth_canvas)
    prediction_render = render_rgba(prediction_canvas)
    for start, end in BOXES[:3]:
        assert (
            stroke_pixels(
                ground_truth_canvas, ground_truth_render, GT_COLOR, start, end
            )
            > MIN_STROKE_PIXELS
        )
    for start, end in (MATCHED, LOW_IOU_PRED, FALSE_POSITIVE):
        assert (
            stroke_pixels(
                prediction_canvas, prediction_render, PRED_COLOR, start, end
            )
            > MIN_STROKE_PIXELS
        )
    # not one box was flagged
    for flagged in (MISS_COLOR, FALSE_POSITIVE_COLOR, CLASS_MISMATCH_COLOR):
        assert (
            stroke_pixels(
                ground_truth_canvas, ground_truth_render, flagged, *MISSED
            )
            == 0
        )
        assert (
            stroke_pixels(
                prediction_canvas,
                prediction_render,
                flagged,
                *FALSE_POSITIVE,
            )
            == 0
        )


def test_a_status_list_that_does_not_line_up_is_refused_as_a_whole(qt_app):
    """A shifted state never flags the wrong box.

    A list that is shorter than the shapes, or longer than them, cannot
    be read shape by shape: every box then keeps the plain colour of its
    canvas instead of carrying the judgement of its neighbour.
    """

    shapes = gt_shapes()
    assert (
        rgba(shape_colors(shapes, GT_COLOR, [STATE_MISS]))
        == [rgba(GT_COLOR)] * 3
    )
    assert (
        rgba(shape_colors(shapes, GT_COLOR, [STATE_MISS] * 4))
        == [rgba(GT_COLOR)] * 3
    )
    assert rgba(shape_colors(shapes, GT_COLOR, None)) == [rgba(GT_COLOR)] * 3
    assert (
        rgba(shape_colors(shapes, GT_COLOR, [STATE_MISS] * 3))
        == [rgba(MISS_COLOR)] * 3
    )
    # an unknown state is a box of its own canvas, never a miss
    assert (
        rgba(shape_colors(shapes, GT_COLOR, ["", "NOPE", None]))
        == [rgba(GT_COLOR)] * 3
    )

    canvas = make_canvas()
    canvas.set_shapes(shapes, [], [STATE_MISS])
    rendered = render_rgba(canvas)
    assert stroke_pixels(canvas, rendered, MISS_COLOR, *MISSED) == 0
    assert (
        stroke_pixels(canvas, rendered, GT_COLOR, *MISSED) > MIN_STROKE_PIXELS
    )
    # Only the mismatch of the judge is a class error: a detail without
    # that key, or with a spelling of a later revision, keeps the plain
    # colours of a matched pair.
    for odd in (None, "", "class_mismatch", "unknown_class", "match"):
        assert matched_pair_state(1.0, odd, 0.5) == STATE_OK_PAIR, odd
    assert matched_pair_state(1.0, "mismatch", 0.5) == STATE_CLASS_MISMATCH


# ------------------------------------------------------------ low score
def test_a_low_score_pair_is_teal_on_both_sides(qt_app):
    """Pixel evidence: the pair whose score is under the NG score threshold.

    The matched pairs of the record are judged with a NG score threshold
    over their own prediction scores, so both of them carry the low
    score flag of the judge. Their strokes therefore turn the teal of
    the low score state on the GT canvas as well as on the Pred one.
    """

    record = low_score_record()
    ground_truth = gt_shapes()
    predictions = record.detail["predictions"]
    gt_states = gt_statuses(record.detail, ground_truth)
    pred_states = pred_statuses(record.detail, predictions)

    # both matched pairs carry the flag of the judge, and a flagged pair
    # is teal whatever its IoU is: the perfectly matched pair is NOT
    # hidden behind the plain colour of a valid pair, only a class
    # mismatch wins over the score (see matched_pair_state)
    assert [pair["low_score"] for pair in record.detail["matched"]] == [
        True,
        True,
    ]
    assert record.detail["ng_score_threshold"] == pytest.approx(
        LOW_SCORE_THRESHOLD
    )
    assert gt_states == [STATE_LOW_SCORE, STATE_LOW_SCORE, STATE_MISS]
    assert pred_states == [
        STATE_LOW_SCORE,
        STATE_LOW_SCORE,
        STATE_FALSE_POSITIVE,
    ]

    for side, shapes, states in (
        ("gt", ground_truth, gt_states),
        ("pred", predictions, pred_states),
    ):
        canvas = make_canvas()
        canvas.set_shapes(
            shapes if side == "gt" else [],
            [] if side == "gt" else shapes,
            states if side == "gt" else (),
            () if side == "gt" else states,
        )
        rendered = render_rgba(canvas)
        # the pair under the IoU threshold and the perfect pair are both
        # teal: the score rule is not gated on the IoU rule
        assert (
            stroke_pixels(canvas, rendered, LOW_SCORE_COLOR, *LOW_IOU)
            > MIN_STROKE_PIXELS
        ), side
        assert (
            stroke_pixels(canvas, rendered, LOW_SCORE_COLOR, *MATCHED)
            > MIN_STROKE_PIXELS
        ), side
        # and the teal replaced the dark yellow of the plain IoU rule
        assert (
            stroke_pixels(canvas, rendered, IOU_BELOW_COLOR, *LOW_IOU) == 0
        ), side


def test_an_unmatched_low_score_box_stays_magenta(qt_app):
    """The false positive signal wins over the score of the same box.

    The prediction no GT met scores under the NG score threshold as
    well, but the miss signal of its own side is the more important one:
    the box keeps the magenta of a false positive instead of carrying
    two meanings at once.
    """

    record = low_score_record()
    predictions = record.detail["predictions"]

    # the judge reports all three predictions under the threshold - the
    # rule reads every valid prediction and never only the matched ones -
    # but the state of the box no GT met is the false positive one
    assert [item["index"] for item in record.detail["low_score"]] == [
        0,
        1,
        2,
    ]
    assert pred_statuses(record.detail, predictions)[2] == (
        STATE_FALSE_POSITIVE
    )

    canvas = make_canvas()
    canvas.set_shapes(
        [], predictions, (), pred_statuses(record.detail, predictions)
    )
    rendered = render_rgba(canvas)
    assert (
        stroke_pixels(
            canvas, rendered, FALSE_POSITIVE_COLOR, *FALSE_POSITIVE
        )
        > MIN_STROKE_PIXELS
    )
    # the stroke right around that box is the magenta one and never teal
    assert (
        stroke_pixels(canvas, rendered, LOW_SCORE_COLOR, *FALSE_POSITIVE)
        == 0
    )


def test_the_matched_pair_state_reads_the_low_score_flag(qt_app):
    """The priority matrix of the optional tail argument.

    The approved rule reads the three states in the priority order of
    the judge itself: a class mismatch wins over a low score, a low
    score wins over an IoU under the threshold - a prediction scoring
    under the NG score threshold counts as NG whatever the IoU of its
    pair is, so the pair the run matched well and still scored low on is
    NOT hidden behind the plain colour of a valid pair - and a pair
    whose score is fine keeps the IoU rule it always had.

    A detail of an older revision carries no low score key at all: the
    three argument call of the previous revision therefore answers
    exactly what it always did, and so does an explicit False or an
    empty value.
    """

    # iou fine, score fine, score low
    assert matched_pair_state(1.0, "match", 0.5) == STATE_OK_PAIR
    assert matched_pair_state(1.0, "match", 0.5, False) == STATE_OK_PAIR
    assert matched_pair_state(1.0, "match", 0.5, True) == STATE_LOW_SCORE
    # iou under the threshold, score fine, score low
    assert matched_pair_state(1.0 / 3.0, "match", 0.5) == STATE_IOU_BELOW
    assert (
        matched_pair_state(1.0 / 3.0, "match", 0.5, False)
        == STATE_IOU_BELOW
    )
    assert matched_pair_state(1.0 / 3.0, "match", 0.5, True) == (
        STATE_LOW_SCORE
    )
    # a class mismatch stays the first reason, whatever the score says
    assert matched_pair_state(1.0, "mismatch", 0.5, True) == (
        STATE_CLASS_MISMATCH
    )
    assert matched_pair_state(1.0 / 3.0, "mismatch", 0.5, True) == (
        STATE_CLASS_MISMATCH
    )
    # an unknown class is not an error of this record: the score rule is
    # the only one left, which is not the same as the IoU rule
    assert matched_pair_state(1.0 / 3.0, "unknown_class", 0.5, True) == (
        STATE_LOW_SCORE
    )
    # a missing or empty flag is the old behaviour, in both IoU regimes
    for missing in (None, "", False):
        assert matched_pair_state(1.0, "match", 0.5, missing) == (
            STATE_OK_PAIR
        ), missing
        assert matched_pair_state(1.0 / 3.0, "match", 0.5, missing) == (
            STATE_IOU_BELOW
        ), missing
    # an unusable IoU is no IoU error: only the score rule can still fire
    for odd in ("", None, "abc"):
        assert matched_pair_state(odd, "match", 0.5) == STATE_OK_PAIR, odd
        assert matched_pair_state(odd, "match", 0.5, True) == (
            STATE_LOW_SCORE
        ), odd
    # a detail written before the score rule: no key, no low score state
    old_detail = {
        "gt_total": 1,
        "gt_region": 1,
        "pred_total": 1,
        "pred_valid": 1,
        "ng_iou_threshold": 0.5,
        "matched": [
            {
                "gt_index": 0,
                "pred_index": 0,
                "class_state": "match",
                "iou": 0.25,
            }
        ],
    }
    assert gt_statuses(old_detail, [rect(*MATCHED)]) == [STATE_IOU_BELOW]
    assert pred_statuses(old_detail, [rect(*MATCHED, score=0.91)]) == [
        STATE_IOU_BELOW
    ]


def test_the_low_score_state_has_its_own_colour(qt_app):
    "The teal of the low score state is a named constant of the canvas."

    assert image_view_module.STATE_LOW_SCORE == "LOW_SCORE"
    assert LOW_SCORE_COLOR == QtGui.QColor(26, 188, 156)
    assert shape_color(STATE_LOW_SCORE) == LOW_SCORE_COLOR
    assert image_view_module.STATE_TO_COLOR[STATE_LOW_SCORE] == LOW_SCORE_COLOR
    # and it is not one of the six colours already in use
    used = {
        name: getattr(image_view_module, name)
        for name in (
            "GT_COLOR",
            "PRED_COLOR",
            "MISS_COLOR",
            "FALSE_POSITIVE_COLOR",
            "CLASS_MISMATCH_COLOR",
            "IOU_BELOW_COLOR",
        )
    }
    for name, color in used.items():
        assert rgba(color) != rgba(LOW_SCORE_COLOR), name
    for name in ("STATE_LOW_SCORE", "LOW_SCORE_COLOR"):
        assert name in image_view_module.__all__, name


def test_the_colours_are_module_constants_of_the_canvas(qt_app):
    "The six judgement colours are named once, in image_view."

    for name, value in (
        ("GT_COLOR", (46, 204, 113)),
        ("PRED_COLOR", (52, 152, 219)),
        ("MISS_COLOR", (231, 76, 60)),
        ("FALSE_POSITIVE_COLOR", (155, 89, 182)),
        ("CLASS_MISMATCH_COLOR", (230, 126, 34)),
        ("IOU_BELOW_COLOR", (241, 196, 15)),
        ("LOW_SCORE_COLOR", (26, 188, 156)),
    ):
        color = getattr(image_view_module, name)
        assert (color.red(), color.green(), color.blue()) == value, name
        assert name in image_view_module.__all__, name
    assert shape_color(STATE_OK_PAIR) is None
    assert image_view_module.STATE_TO_COLOR == {
        STATE_MISS: MISS_COLOR,
        STATE_FALSE_POSITIVE: FALSE_POSITIVE_COLOR,
        STATE_CLASS_MISMATCH: CLASS_MISMATCH_COLOR,
        STATE_IOU_BELOW: IOU_BELOW_COLOR,
        STATE_LOW_SCORE: LOW_SCORE_COLOR,
    }
    # the canvas knows the state of a box, never a fill
    canvas = make_canvas()
    assert not hasattr(canvas, "box_fill")


# ------------------------------------------------------------- the legend
def legend_colours() -> dict:
    "Return the colour of every entry of the rendered legend."

    return {
        "miss": MISS_COLOR.name(),
        "fp": FALSE_POSITIVE_COLOR.name(),
        "cls": CLASS_MISMATCH_COLOR.name(),
        "iou": IOU_BELOW_COLOR.name(),
        "low": LOW_SCORE_COLOR.name(),
        "gt": GT_COLOR.name(),
    }


def test_the_legend_names_every_judgement_colour(qt_app):
    "The legend writes the canvas colours next to the state they mean."

    text = legend_html()
    for word in ("图例", "匹配", "漏报", "误报", "类别不一致", "IoU", "低分"):
        assert word in text, word
    for color in legend_colours().values():
        assert color in text, color
    # the sixth entry of the legend writes the teal of the low score
    # state next to the word that names it
    assert (
        "<span style='color:" + LOW_SCORE_COLOR.name() + "'>低分</span>"
    ) in text
    # and the tooltip says what that colour means
    assert "分数阈值" in LEGEND_TOOLTIP
    assert LOW_SCORE_COLOR.name() not in LEGEND_HTML
    # the legend never explains a fill: a box is an outline
    assert "填充" not in text
    page = ResultsPage()
    assert page.legend_label.text() == text
    assert page.legend_label.textFormat() == QtCore.Qt.TextFormat.RichText
    assert page.legend_label.toolTip()
    page.deleteLater()


def test_the_legend_sits_under_the_display_row_and_fits_the_page(qt_app):
    """The legend is the last line of the display controls and is readable.

    The display row is shared with the two canvases, where the five
    entries do not fit, so the legend gets its own line right below the
    row and above the pictures. It is never clipped either: the page is
    laid out at the window width and the label still gets the room of its
    own text.
    """

    page = ResultsPage()
    page.resize(MINIMUM_WIDTH, 640)
    page.show()
    QtWidgets.QApplication.processEvents()
    try:
        legend = page.legend_label
        row = page.label_check.mapTo(page, QtCore.QPoint(0, 0))
        legend_top = legend.mapTo(page, QtCore.QPoint(0, 0))
        canvas_top = page.gt_canvas.mapTo(page, QtCore.QPoint(0, 0))
        # below the display row, above the pictures, on its own line
        assert legend_top.y() > row.y()
        assert legend_top.y() + legend.height() <= canvas_top.y()
        # and the whole text is on screen at the width of the window
        assert legend.width() >= legend.sizeHint().width()
        assert legend_top.x() + legend.width() <= page.width()
        assert legend_top.x() + legend.sizeHint().width() <= page.width()
    finally:
        page.close()
        page.deleteLater()


def test_the_window_minimum_width_and_the_page_hint_do_not_grow(qt_app):
    "The legend never widens the window: 1024 stays the minimum width."

    dialog = ModelValidationDialog()
    try:
        assert dialog.minimumWidth() == MINIMUM_WIDTH == 1024
        assert dialog.minimumSize().width() == 1024
        page = dialog.results_page
        # the page itself still fits the minimum width of the window
        assert page.minimumSizeHint().width() < MINIMUM_WIDTH
    finally:
        dialog.close()
        dialog.deleteLater()


def test_the_page_colours_the_two_canvases_from_one_judgement(
    qt_app, tmp_path
):
    """The page hands the states of one matching to both canvases.

    The two canvases show the very same box with the very same colour:
    the miss of the left picture is orange red and the false positive of
    the right one is magenta, both read from the one judgement detail of
    the record.
    """

    staging = osp.join(str(tmp_path), dataset.STAGING_PREFIX + "status")
    for folder in (dataset.ORIGINAL_DIRNAME, dataset.AUGMENTED_DIRNAME):
        for sub in (dataset.IMAGES_DIRNAME, dataset.LABELS_DIRNAME):
            os.makedirs(osp.join(staging, folder, sub), exist_ok=True)
    paths = dataset.staging_paths(
        staging, records_module.KIND_ORIGINAL, "a.png"
    )
    image = picture()
    ok, buffer = cv2.imencode(".png", image)
    assert ok
    buffer.tofile(paths["image"])
    ground_truth = gt_shapes()
    dataset.write_json(
        paths["label"],
        {
            "version": "3.0.0",
            "flags": {},
            "checked": False,
            "shapes": ground_truth,
            "imagePath": "a.png",
            "imageData": None,
            "imageHeight": image.shape[0],
            "imageWidth": image.shape[1],
        },
    )
    record = records_module.make_record(
        records_module.KIND_ORIGINAL, "a.png", paths["image"], paths["label"]
    )
    judged = judged_record()
    record.detail = dict(judged.detail)
    record.verdict = judged.verdict
    record.judged = True

    page = ResultsPage()
    page.resize(1024, 640)
    page.set_context(list(CLASSES), staging)
    page.set_records([record])
    page.table.selectRow(0)
    page.gt_canvas.resize(*WIDGET_SIZE)
    page.pred_canvas.resize(*WIDGET_SIZE)
    try:
        gt_render = render_rgba(page.gt_canvas)
        pred_render = render_rgba(page.pred_canvas)
        assert (
            stroke_pixels(page.gt_canvas, gt_render, MISS_COLOR, *MISSED)
            > MIN_STROKE_PIXELS
        )
        assert (
            stroke_pixels(
                page.pred_canvas,
                pred_render,
                FALSE_POSITIVE_COLOR,
                *FALSE_POSITIVE,
            )
            > MIN_STROKE_PIXELS
        )
        # the matched pair keeps the plain colour of its own side and the
        # pair under the threshold is yellow on both sides
        assert (
            stroke_pixels(page.gt_canvas, gt_render, GT_COLOR, *MATCHED)
            > MIN_STROKE_PIXELS
        )
        assert (
            stroke_pixels(page.pred_canvas, pred_render, PRED_COLOR, *MATCHED)
            > MIN_STROKE_PIXELS
        )
        assert (
            stroke_pixels(page.gt_canvas, gt_render, IOU_BELOW_COLOR, *LOW_IOU)
            > MIN_STROKE_PIXELS
        )
        assert (
            stroke_pixels(
                page.pred_canvas,
                pred_render,
                IOU_BELOW_COLOR,
                *LOW_IOU_PRED,
            )
            > MIN_STROKE_PIXELS
        )
        # the false positive is not flagged on the left and the miss is
        # not flagged on the right
        assert (
            stroke_pixels(
                page.gt_canvas, gt_render, FALSE_POSITIVE_COLOR, *MISSED
            )
            == 0
        )
        assert (
            stroke_pixels(
                page.pred_canvas, pred_render, MISS_COLOR, *FALSE_POSITIVE
            )
            == 0
        )
    finally:
        page.close()
        page.deleteLater()


def test_a_box_is_still_an_outline_and_never_a_filled_plate(qt_app):
    "A judgement colour changes the outline alone: the inside is the picture."

    record = judged_record()
    ground_truth = gt_shapes()
    canvas = make_canvas()
    canvas.set_shapes(
        ground_truth, [], gt_statuses(record.detail, ground_truth), ()
    )
    rendered = render_rgba(canvas)
    start, end = MATCHED
    inset = 5
    top_left = canvas.image_to_widget(
        QtCore.QPointF(start[0] + inset, start[1] + inset)
    )
    bottom_right = canvas.image_to_widget(
        QtCore.QPointF(end[0] - inset, end[1] - inset)
    )
    inside = rendered[
        int(np.ceil(top_left.y())) : int(np.floor(bottom_right.y())) + 1,
        int(np.ceil(top_left.x())) : int(np.floor(bottom_right.x())) + 1,
    ]
    assert inside.size > 0
    # the flat picture: no plate and no tint was painted inside the box
    assert np.array_equal(
        inside[:, :, :3],
        np.full(inside[:, :, :3].shape, PICTURE_FILL, np.uint8),
    )
    assert np.all(inside[:, :, 3] == 255)
