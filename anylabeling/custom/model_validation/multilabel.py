"""Whole image, class agnostic merge of the predicted rows.

The engine answers one row per class whose score passed the fixed
inference cut, and two anchors of the same object may survive the per
class NMS with coordinates that only differ by a few pixels: one object
then reaches this tool as several stacked boxes. This module folds every
pair of rows whose IoU passes the configured threshold into the one box
the canvas draws - whatever their class, one label line per class - and
expands such a box back into one independent row per class for the
judge ("one box per class").

The module is plain logic: it imports no PyQt and no onnx, only the
shape accessors of the judge, whose polygon helper imports shapely
lazily.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .judge import (
    axis_aligned_box,
    shape_label,
    shape_point_list,
    shape_score,
    shape_type_of,
    to_polygon,
)

# Only these two shape types describe a region the whole image NMS can
# compare: they are what the detect and segment tasks produce, and only
# they have a meaningful IoU. The other types (rotation, point, ...)
# keep the legacy grouping of the rows that share their coordinates.
MERGEABLE_SHAPE_TYPES = ("rectangle", "polygon")


def shape_iou(
    points_a: Sequence[Sequence[float]],
    points_b: Sequence[Sequence[float]],
) -> float:
    """Return the IoU of two point lists, 0.0 when it is undefined.

    Two real polygons are compared by their true overlap (the shapely
    intersection of the judge), so a rotated or curved region is never
    judged by its bounding box; every other pair - the two point
    rectangle the engine answers, a degenerate ring - falls back to the
    axis aligned box. A point list that cannot describe an area at all
    has no overlap with anything and answers 0.0.
    """

    points_a = list(points_a or [])
    points_b = list(points_b or [])
    if len(points_a) >= 3 and len(points_b) >= 3:
        polygon_a = to_polygon(points_a)
        polygon_b = to_polygon(points_b)
        if polygon_a is None or polygon_b is None:
            return 0.0
        union = float(polygon_a.union(polygon_b).area)
        if union <= 0.0:
            return 0.0
        overlap = float(polygon_a.intersection(polygon_b).area)
        return overlap / union

    box_a = axis_aligned_box(points_a)
    box_b = axis_aligned_box(points_b)
    if box_a is None or box_b is None:
        return 0.0
    width = min(box_a[2], box_b[2]) - max(box_a[0], box_b[0])
    height = min(box_a[3], box_b[3]) - max(box_a[1], box_b[1])
    if width <= 0.0 or height <= 0.0:
        return 0.0
    area_a = (box_a[2] - box_a[0]) * (box_a[3] - box_a[1])
    area_b = (box_b[2] - box_b[0]) * (box_b[3] - box_b[1])
    overlap = width * height
    union = area_a + area_b - overlap
    if union <= 0.0:
        return 0.0
    return float(overlap / union)


def _multilabel_group_key(shape: Any) -> Any:
    """Return the key that groups the rows of one predicted box.

    The multi label NMS copies the very same box once per class, so the
    rows of one box carry coordinates that are identical down to the
    bit; rounding to six decimals absorbs the float noise of the tensor
    arithmetic without joining two boxes that really differ. Two rows
    of one shape type at the very same coordinates cannot describe two
    distinct objects either: their IoU is 1 and the NMS would have
    suppressed one of them.
    """

    points = tuple(
        (round(float(x), 6), round(float(y), 6))
        for x, y in shape_point_list(shape)
    )
    return (shape_type_of(shape), points)


def _multilabel_sort_key(shape: Any) -> Any:
    """Return the ranking key of one row inside its group.

    A missing score cannot be ranked: it sorts after every scored row.
    The label breaks the ties of two equal scores, therefore both the
    order and the primary label it picks are deterministic.
    """

    score = shape_score(shape)
    # the ascending order of -score puts the highest score first; a row
    # without a score can never outrank a scored one
    rank = float("inf") if score is None else -float(score)
    return (rank, shape_label(shape))


def _group_by_coordinates(shapes: Sequence[Any]) -> Tuple[Any, Any]:
    """Return the legacy groups of the rows and their first seen order."""

    groups: Dict[Any, List[Any]] = {}
    order: List[Any] = []
    for shape in shapes:
        key = _multilabel_group_key(shape)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(shape)
    return groups, order


def _fold_rows(rows: Sequence[Any]) -> Any:
    """Fold one score descending group of rows into the one box.

    A group of a single row is returned untouched: the payload of a
    single label prediction keeps exactly the keys it had before this
    module existed. The highest scoring row carries the whole group: a
    Shape object is reused in place (the two lists become dynamic
    attributes), a dict is copied so that the rows the caller built stay
    untouched.
    """

    primary = rows[0]
    if len(rows) == 1:
        return primary
    labels = [shape_label(row) for row in rows]
    scores = [shape_score(row) for row in rows]
    if isinstance(primary, dict):
        primary = dict(primary)
        primary["labels"] = labels
        primary["scores"] = scores
    else:
        primary.labels = labels
        primary.scores = scores
    return primary


def merge_multilabel_predictions(shapes: Sequence[Any]) -> List[Any]:
    """Collapse the multi label rows of one predicted box into one shape.

    The multi label NMS of the engine answers one row per class whose
    score passed the fixed inference cut, all of them carrying the very
    same box. The canvas draws one box with one label line per row, so
    the rows of a box are folded into a single shape: the highest score
    of the group stays the label/score the judge reads, while the whole
    group is attached as the parallel, score descending lists labels and
    scores.

    This is the legacy grouping: rows only meet when their shape type
    and their coordinates are identical. merge_overlapping_predictions
    runs it on the shape types the whole image NMS cannot compare and on
    every run that carries no IoU threshold at all (obb, pose).
    """

    groups, order = _group_by_coordinates(shapes)
    return [
        _fold_rows(sorted(groups[key], key=_multilabel_sort_key))
        for key in order
    ]


def _dedupe_rows(rows: Sequence[Any]) -> List[Any]:
    """Keep one row per class inside one merged cluster.

    One class may describe the same object with two anchors that the per
    class NMS did not suppress (their IoU was just below its own cut):
    only the highest scoring row of that class is kept, so one class can
    never count twice inside the cluster and a box can never be reported
    as two objects of the same name. The scan follows the score
    descending order, therefore the first occurrence of a class already
    is its best row; a row without a score is replaced by a scored one
    should the very same class reappear with a score (same score, the
    label keeps the two equal).
    """

    best: Dict[str, Any] = {}
    order: List[str] = []
    for row in rows:
        label = shape_label(row)
        if label not in best:
            best[label] = row
            order.append(label)
            continue
        if shape_score(best[label]) is None and shape_score(row) is not None:
            best[label] = row
    return sorted(
        (best[label] for label in order), key=_multilabel_sort_key
    )


def merge_overlapping_predictions(
    shapes: Sequence[Any], iou_threshold: Optional[float] = None
) -> List[Any]:
    """Collapse every overlapping row of one image into one box.

    With an IoU threshold the mergeable rows (rectangle, polygon) run a
    class agnostic greedy NMS: the rows are walked by descending score
    and a row joins the first kept box it overlaps by more than the
    threshold, otherwise it becomes a kept box of its own. The kept box
    therefore is the highest scoring row of its cluster and it is the
    geometry the canvas draws; the IoU comparison is strict, which is
    the very rule of the engine NMS (it keeps the pairs at or below its
    threshold).

    Every other row keeps the legacy grouping of
    merge_multilabel_predictions. A threshold of None disables the whole
    image NMS and falls back to that legacy grouping for every row,
    which is the path of the obb and pose tasks.

    The clusters come out by descending score, the legacy groups in
    their first seen order; the two sets are disjoint in practice (a
    task answers one shape type), therefore the output order of a run is
    the score descending order of its final boxes.
    """

    mergeable = [
        shape
        for shape in shapes
        if shape_type_of(shape) in MERGEABLE_SHAPE_TYPES
    ]
    if iou_threshold is None or not mergeable:
        return merge_multilabel_predictions(shapes)

    legacy = [
        shape
        for shape in shapes
        if shape_type_of(shape) not in MERGEABLE_SHAPE_TYPES
    ]
    clusters: List[List[Any]] = []
    for shape in sorted(mergeable, key=_multilabel_sort_key):
        points = shape_point_list(shape)
        for cluster in clusters:
            # the cluster is compared through its kept row: the highest
            # scoring one, exactly like a greedy NMS does
            if (
                shape_iou(shape_point_list(cluster[0]), points)
                > float(iou_threshold)
            ):
                cluster.append(shape)
                break
        else:
            clusters.append([shape])
    merged = [_fold_rows(_dedupe_rows(cluster)) for cluster in clusters]
    merged.extend(merge_multilabel_predictions(legacy))
    return merged


def _as_score(value: Any) -> Optional[float]:
    """Return one score entry of a row list, None when it carries none.

    The rows of a box are written as two parallel lists, so one entry
    may be a missing or a non finite number; mirroring the canvas guard
    keeps the judge from ranking a row on a value the canvas does not
    show either.
    """

    if value is None or isinstance(value, bool):
        return None
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(score) or math.isinf(score):
        return None
    return score


def _shape_rows(shape: Any) -> List[Tuple[str, Optional[float]]]:
    """Return the (label, score) rows of one predicted box.

    A merged box carries the parallel lists labels and scores, one entry
    per class it holds; they are only read while they really line up and
    hold more than one entry, which is the same guard the canvas uses
    (see ui/image_view.shape_label_rows). Every other shape - a ground
    truth box, a single label prediction, a record of an older revision
    - falls back to the one row of its own label and score.
    """

    if isinstance(shape, dict):
        labels = shape.get("labels")
        scores = shape.get("scores")
    else:
        labels = getattr(shape, "labels", None)
        scores = getattr(shape, "scores", None)
    if (
        isinstance(labels, (list, tuple))
        and isinstance(scores, (list, tuple))
        and len(labels) == len(scores)
        and len(labels) > 1
    ):
        return [
            (str(label), _as_score(score))
            for label, score in zip(labels, scores)
        ]
    return [(shape_label(shape), shape_score(shape))]


def expand_multilabel_rows(
    shapes: Sequence[Any],
) -> Tuple[List[Dict[str, Any]], List[int]]:
    """Expand every box into one judge row per class it carries.

    The judge matches, scores and reports one prediction per object,
    while the canvas draws one box per object: a merged box therefore
    has to be handed over once per class ("one box per class"), each
    copy carrying the label and the score of that class and the geometry
    of the box it came from.

    The answer is the row list and, for every row, the index of the
    payload box (detail["predictions"]) it belongs to, which is what
    lets the results page colour one box from the states of its rows.
    The rows of one box share the very point list: the judge only reads
    it, and one allocation per box keeps a many class box cheap.
    """

    rows: List[Dict[str, Any]] = []
    boxes: List[int] = []
    for index, shape in enumerate(shapes):
        shape_type = shape_type_of(shape)
        points = [
            [float(x), float(y)] for x, y in shape_point_list(shape)
        ]
        for label, score in _shape_rows(shape):
            rows.append(
                {
                    "label": label,
                    "score": score,
                    "shape_type": shape_type,
                    "points": points,
                }
            )
            boxes.append(index)
    return rows, boxes


__all__ = [
    "MERGEABLE_SHAPE_TYPES",
    "expand_multilabel_rows",
    "merge_multilabel_predictions",
    "merge_overlapping_predictions",
    "shape_iou",
]
