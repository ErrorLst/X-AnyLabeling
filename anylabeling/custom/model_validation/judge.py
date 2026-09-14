"""OK / NG verdict computation for one validated image.

Every deviation from the ground truth produces NG. The reasons are
evaluated in a fixed priority order; the first hit becomes the primary
reason while lower priority hits are still recorded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .labelme_io import REGION_SHAPE_TYPES, to_points

INFER_ERROR = "INFER_ERROR"
GT_DEGENERATE = "GT_DEGENERATE"
GT_UNKNOWN_CLASS = "GT_UNKNOWN_CLASS"
UNSUPPORTED_GT_SHAPE = "UNSUPPORTED_GT_SHAPE"
PRED_UNKNOWN_CLASS = "PRED_UNKNOWN_CLASS"
COUNT_MISMATCH = "COUNT_MISMATCH"
CLASS_MISMATCH = "CLASS_MISMATCH"
LOW_SCORE = "LOW_SCORE"
IOU_BELOW = "IOU_BELOW"
MISS_FP = "MISS_FP"

REASON_PRIORITY = (
    INFER_ERROR,
    GT_DEGENERATE,
    GT_UNKNOWN_CLASS,
    UNSUPPORTED_GT_SHAPE,
    PRED_UNKNOWN_CLASS,
    COUNT_MISMATCH,
    CLASS_MISMATCH,
    LOW_SCORE,
    IOU_BELOW,
    MISS_FP,
)

MATCHABLE_PRED_TYPES = ("rectangle", "rotation", "polygon")
IGNORED_PRED_TYPES = (
    "point",
    "line",
    "linestrip",
    "circle",
    "cuboid",
)
IOU_ZERO_EPSILON = 1e-6


def polygon_area(points: Sequence[Tuple[float, float]]) -> float:
    """Return the unsigned polygon area of a point ring."""

    if len(points) < 3:
        return 0.0
    total = 0.0
    count = len(points)
    for index in range(count):
        x1, y1 = points[index]
        x2, y2 = points[(index + 1) % count]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def shape_label(shape: Any) -> str:
    """Return the label of an xlabel dict or Shape object."""

    if isinstance(shape, dict):
        return str(shape.get("label", ""))
    return str(getattr(shape, "label", ""))


def shape_type_of(shape: Any) -> str:
    """Return the shape type of an xlabel dict or Shape object."""

    if isinstance(shape, dict):
        return str(shape.get("shape_type") or "polygon")
    return str(getattr(shape, "shape_type", "polygon") or "polygon")


def shape_score(shape: Any) -> Optional[float]:
    """Return the prediction score when available."""

    if isinstance(shape, dict):
        value = shape.get("score")
    else:
        value = getattr(shape, "score", None)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def shape_point_list(shape: Any) -> List[Tuple[float, float]]:
    """Return the float point list of an xlabel dict or Shape object."""

    if isinstance(shape, dict):
        return to_points(shape.get("points"))
    return to_points(getattr(shape, "points", []))


def axis_aligned_box(points: Sequence[Tuple[float, float]]):
    """Return the axis aligned bounding box of a point list."""

    if not points:
        return None
    xs = [float(p[0]) for p in points]
    ys = [float(p[1]) for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def to_polygon(points: Sequence[Tuple[float, float]]):
    """Build a shapely polygon from points, falling back to the AABB."""

    from shapely.geometry import Polygon

    if len(points) >= 3:
        polygon = Polygon(points)
        if polygon.is_valid and polygon.area > 0:
            return polygon
    box = axis_aligned_box(points)
    if box is None:
        return None
    if box[2] - box[0] <= 0 or box[3] - box[1] <= 0:
        return None
    return Polygon(
        [
            (box[0], box[1]),
            (box[2], box[1]),
            (box[2], box[3]),
            (box[0], box[3]),
        ]
    )


def is_degenerate_shape(points: Sequence[Tuple[float, float]]) -> bool:
    """Return True when a point list cannot describe a region.

    The test mirrors to_polygon: point lists that do not form a valid
    ring fall back to their axis aligned bounding box, therefore the
    legacy two point rectangle is not degenerate while a box with a
    zero width or a zero height is.
    """

    polygon = to_polygon(points)
    if polygon is None:
        return True
    return float(polygon.area) <= 0.0


def iou_matrix(
    gt_shapes: Sequence[Any], pred_shapes: Sequence[Any]
) -> List[List[float]]:
    """Return the pairwise IoU matrix between GT and predicted shapes."""

    gt_polygons = [to_polygon(shape_point_list(s)) for s in gt_shapes]
    pred_polygons = [to_polygon(shape_point_list(s)) for s in pred_shapes]
    matrix: List[List[float]] = []
    for gt_polygon in gt_polygons:
        row: List[float] = []
        for pred_polygon in pred_polygons:
            if gt_polygon is None or pred_polygon is None:
                row.append(0.0)
                continue
            union = gt_polygon.union(pred_polygon).area
            if union <= 0:
                row.append(0.0)
                continue
            overlap = gt_polygon.intersection(pred_polygon).area
            row.append(float(overlap / union))
        matrix.append(row)
    return matrix


def hungarian_match(matrix: Sequence[Sequence[float]]):
    """Maximise the total IoU with the scipy linear sum assignment.

    The IoU values are used as they are: turning the matrix into a 0/1
    indicator before the assignment would maximise the number of matched
    pairs and may pick a worse assignment when several assignments share
    the same cardinality.
    """

    from scipy.optimize import linear_sum_assignment

    row_count = len(matrix)
    column_count = len(matrix[0]) if row_count else 0
    if row_count == 0 or column_count == 0:
        return [], list(range(row_count)), list(range(column_count))
    values = [[float(value) for value in row] for row in matrix]
    rows, columns = linear_sum_assignment(values, maximize=True)
    pairs: List[Tuple[int, int]] = []
    used_rows = set()
    used_columns = set()
    for row_index, column_index in zip(rows, columns):
        row_index = int(row_index)
        column_index = int(column_index)
        if values[row_index][column_index] < IOU_ZERO_EPSILON:
            continue
        pairs.append((row_index, column_index))
        used_rows.add(row_index)
        used_columns.add(column_index)
    unmatched_gt = [i for i in range(row_count) if i not in used_rows]
    unmatched_pred = [i for i in range(column_count) if i not in used_columns]
    return pairs, unmatched_gt, unmatched_pred


@dataclass
class JudgeResult:
    """Verdict of one record together with the full reason list."""

    verdict: str
    reasons: List[str] = field(default_factory=list)
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def primary_reason(self) -> str:
        """Return the highest priority reason or an empty string."""

        return self.reasons[0] if self.reasons else ""


def order_reasons(reasons: Sequence[str]) -> List[str]:
    """Sort the reasons by the fixed priority order."""

    ranked = [r for r in REASON_PRIORITY if r in reasons]
    extras = [r for r in reasons if r not in REASON_PRIORITY]
    return ranked + extras


def judge_record(
    gt_shapes: Sequence[Any],
    pred_shapes: Sequence[Any],
    classes: Sequence[str],
    ng_iou_threshold: float = 0.5,
    infer_error: Optional[str] = None,
    ng_score_threshold: Optional[float] = None,
) -> JudgeResult:
    """Compute the OK / NG verdict of one image."""

    if infer_error:
        return JudgeResult(
            verdict="NG",
            reasons=[INFER_ERROR],
            detail={"error": str(infer_error)},
        )

    reasons: List[str] = []
    known_classes = {str(name) for name in classes}

    region_gt = [
        shape
        for shape in gt_shapes
        if shape_type_of(shape) in REGION_SHAPE_TYPES
    ]
    unsupported_gt = [
        shape
        for shape in gt_shapes
        if shape_type_of(shape) not in REGION_SHAPE_TYPES
    ]

    degenerate_gt = [
        shape
        for shape in region_gt
        if is_degenerate_shape(shape_point_list(shape))
    ]
    if degenerate_gt:
        reasons.append(GT_DEGENERATE)

    gt_unknown = [
        shape for shape in gt_shapes if shape_label(shape) not in known_classes
    ]
    if gt_unknown:
        reasons.append(GT_UNKNOWN_CLASS)

    if unsupported_gt:
        reasons.append(UNSUPPORTED_GT_SHAPE)

    valid_pred = [
        shape
        for shape in pred_shapes
        if shape_type_of(shape) in MATCHABLE_PRED_TYPES
    ]
    ignored_pred = [
        shape
        for shape in pred_shapes
        if shape_type_of(shape) not in MATCHABLE_PRED_TYPES
    ]

    pred_unknown = [
        shape
        for shape in valid_pred
        if shape_label(shape) not in known_classes
    ]
    if pred_unknown:
        reasons.append(PRED_UNKNOWN_CLASS)

    dual_empty = not region_gt and not valid_pred
    if len(region_gt) != len(valid_pred):
        reasons.append(COUNT_MISMATCH)

    matrix = iou_matrix(region_gt, valid_pred)
    pairs, unmatched_gt, unmatched_pred = hungarian_match(matrix)

    # The score rule works on the same valid prediction list and on
    # the same indices as the matching, so its detail shares the
    # pred_index coordinate system.
    low_score_indices: Set[int] = set()
    low_score_shapes: List[Dict[str, Any]] = []
    if ng_score_threshold is not None:
        score_threshold = float(ng_score_threshold)
        for pred_index, pred_shape in enumerate(valid_pred):
            score = shape_score(pred_shape)
            # a shape without a score cannot be judged by the rule:
            # it is skipped instead of reported as a low score.
            if score is None or score >= score_threshold:
                continue
            low_score_indices.add(pred_index)
            low_score_shapes.append(
                {
                    "index": pred_index,
                    "label": shape_label(pred_shape),
                    "score": round(float(score), 6),
                }
            )

    matched: List[Dict[str, Any]] = []
    unknown_class_pairs: List[Dict[str, Any]] = []
    class_mismatch = False
    iou_below = False
    for gt_index, pred_index in pairs:
        gt_shape = region_gt[gt_index]
        pred_shape = valid_pred[pred_index]
        iou = matrix[gt_index][pred_index]
        gt_label = shape_label(gt_shape)
        pred_label = shape_label(pred_shape)
        same_class = gt_label == pred_label
        both_known = gt_label in known_classes and pred_label in known_classes
        if same_class:
            class_state = "match"
        elif both_known:
            class_state = "mismatch"
            class_mismatch = True
        else:
            # an unknown label is already reported once, such a pair
            # must not add a second class error on top of it.
            class_state = "unknown_class"
            unknown_class_pairs.append(
                {
                    "gt_index": gt_index,
                    "pred_index": pred_index,
                    "gt_label": gt_label,
                    "pred_label": pred_label,
                }
            )
        if iou < float(ng_iou_threshold):
            iou_below = True
        matched.append(
            {
                "gt_index": gt_index,
                "pred_index": pred_index,
                "gt_label": gt_label,
                "pred_label": pred_label,
                "iou": round(float(iou), 6),
                "score": shape_score(pred_shape),
                "class_ok": same_class,
                "class_state": class_state,
                "low_score": pred_index in low_score_indices,
            }
        )
    if class_mismatch:
        reasons.append(CLASS_MISMATCH)
    if low_score_indices:
        reasons.append(LOW_SCORE)
    if iou_below:
        reasons.append(IOU_BELOW)
    if unmatched_gt or unmatched_pred:
        reasons.append(MISS_FP)

    ordered = order_reasons(reasons)
    detail: Dict[str, Any] = {
        "gt_total": len(gt_shapes),
        "gt_region": len(region_gt),
        "pred_total": len(pred_shapes),
        "pred_valid": len(valid_pred),
        "matched": matched,
        "missed_gt": [
            {
                "index": index,
                "label": shape_label(region_gt[index]),
                "shape_type": shape_type_of(region_gt[index]),
            }
            for index in unmatched_gt
        ],
        "false_positives": [
            {
                "index": index,
                "label": shape_label(valid_pred[index]),
                "shape_type": shape_type_of(valid_pred[index]),
                "score": shape_score(valid_pred[index]),
                "unknown_class": (
                    shape_label(valid_pred[index]) not in known_classes
                ),
            }
            for index in unmatched_pred
        ],
        "ignored_pred_shapes": len(ignored_pred),
        "unsupported_gt_shapes": [
            {
                "index": index,
                "label": shape_label(shape),
                "shape_type": shape_type_of(shape),
            }
            for index, shape in enumerate(gt_shapes)
            if shape_type_of(shape) not in REGION_SHAPE_TYPES
        ],
        "unknown_gt_labels": sorted({shape_label(s) for s in gt_unknown}),
        "unknown_pred_labels": sorted({shape_label(s) for s in pred_unknown}),
        "unknown_class_pairs": {
            "matched_pairs": unknown_class_pairs,
            "false_positive_indices": [
                index
                for index in unmatched_pred
                if shape_label(valid_pred[index]) not in known_classes
            ],
            "note": (
                "a pair that involves an unknown label is reported as "
                "GT_UNKNOWN_CLASS / PRED_UNKNOWN_CLASS only: it is "
                "excluded from CLASS_MISMATCH, while an unmatched unknown "
                "class prediction still counts as a false positive"
            ),
        },
        "ng_iou_threshold": float(ng_iou_threshold),
        "ng_score_threshold": (
            None
            if ng_score_threshold is None
            else float(ng_score_threshold)
        ),
        "low_score": low_score_shapes,
        "dual_empty": dual_empty,
    }
    return JudgeResult(
        verdict="NG" if ordered else "OK",
        reasons=ordered,
        detail=detail,
    )


__all__ = [
    "CLASS_MISMATCH",
    "COUNT_MISMATCH",
    "GT_DEGENERATE",
    "GT_UNKNOWN_CLASS",
    "IGNORED_PRED_TYPES",
    "INFER_ERROR",
    "IOU_BELOW",
    "IOU_ZERO_EPSILON",
    "JudgeResult",
    "LOW_SCORE",
    "MATCHABLE_PRED_TYPES",
    "MISS_FP",
    "PRED_UNKNOWN_CLASS",
    "REASON_PRIORITY",
    "UNSUPPORTED_GT_SHAPE",
    "axis_aligned_box",
    "hungarian_match",
    "iou_matrix",
    "is_degenerate_shape",
    "judge_record",
    "order_reasons",
    "polygon_area",
    "shape_label",
    "shape_point_list",
    "shape_score",
    "shape_type_of",
    "to_polygon",
]
