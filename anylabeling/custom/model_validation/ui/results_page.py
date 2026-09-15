"""Results page of the model validation sub window.

The left list is the only place where a record is marked: its first
column carries one checkbox per record and the meaning of that checkbox
follows the kind of the record - a *delete mark* for an original (check
it and the original, its json and its augmented children leave the
export) and a *selection mark* for an augmented copy (check it and the
copy joins the export). Both marks are the very flags the export
formula reads, so the list never keeps a second, private selection.

The list itself is a *tree*, not an editable table: nothing in it may be
typed over. A double click, F2 and every edit trigger are refused by the
widget (RecordTree sets NoEditTriggers, keeps the read only item flags
and refuses the non virtual editItem), the only interactive control of a
row is its own checkbox and the only keyboard interaction is the row
selection.

The right hand side is read only and shows pictures only: the two
canvases display the ground truth and the predictions of the current row,
sharing one zoom and one pan, and every box is labelled in place - the
prediction canvas writes "label score" into the upper left corner of each
predicted box, the ground truth canvas writes the label of each GT box. A
label never carries a background plate: it is white text with a thin dark
outline, and a box is never filled either - only its outline is painted.

The colour of that outline is the verdict of the box. The page reads the
judgement detail of the displayed record once and hands each canvas the
states of its own shapes - the states of the very same matching, so a
defect keeps one colour on both pictures: a matched GT stays green and a
matched prediction stays blue, a GT no prediction met (a miss) is drawn
orange red, a prediction no GT met (a false positive) is drawn magenta, a
matched pair of two different classes is drawn orange and a pair whose
IoU stayed under the threshold is drawn dark yellow. The states travel as
a display side copy: the detail of the record is only read, never
written. A record without a judgement - an augmented copy nobody judged, a
skipped record - colours no box and keeps the plain GT / Pred colour. The
legend under the display row writes those five colours next to the state
each of them means.

Ticking a mark never rebuilds the list. The checkbox of a mark is the
flag itself, so a toggle is answered by rewriting the rows that really
changed - the marked record and, for an original, its augmented children -
and the scroll position, the selection and every other row stay exactly
where the user left them.

The list opens on *NG*: the filter combo widens it to every verdict, to
one of the two verdicts a run is read for, or to the records a user
really touched - a renamed label, a moved box, the delete mark of an
original and the export mark of an augmented copy (see record_modified).

Holding the right button on one of the two canvases previews the
original an augmented record was made from: the parent picture, its own
ground truth, its own predictions and its own judgement replace the
augmented copy until the button is released. The preview is a display
state of this page alone - it reads the records that are already in
memory and never runs inference again, and it moves no control either:
the one status line of the preview keeps its reserved row while it is
empty, so the press, the two "no parent" hints and the release only ever
change a text and never the geometry of the pictures below it.

The page itself never edits a record: a correction is made in the main
window of the tool, which owns the annotation editor, and the results
page only shows the staging json the editor wrote. No key carries the
record over any more: every switch of the record on screen - a click on
another row, A / D, a filter that shows another record, the first fill
of a finished run - is announced on current_record_changed, and the
window that holds this page follows it in the main window on its own
(see dialog). The staging copy, the list and the selection of this page
stay exactly where the user left them.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from PyQt6 import QtCore, QtGui, QtWidgets

from .. import dataset
from .. import records as records_module
from ..records import ValidationRecord
from .. import judge as judge_module
from ..labelme_io import REGION_SHAPE_TYPES
from ..multilabel import expand_multilabel_rows
from . import image_view as image_view_module
from .image_view import (
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
    load_pixmap,
)

COLUMN_MARK = 0
COLUMN_VERDICT = 1
COLUMN_RELPATH = 2
COLUMN_KIND = 3
COLUMN_AUGMENT = 4

# the mark column was called the export column before both kinds grew
# their own checkbox: the alias keeps existing callers working.
COLUMN_EXPORT = COLUMN_MARK

HEADERS = ("标记", "状态", "relpath", "kind", "增强")

MARK_TOOLTIP_ORIGINAL = (
    "删除标记：勾选 = 该原图（连同它的 json 与全部增强子代）从导出中排除；"
    "默认全部不勾选。"
)
MARK_TOOLTIP_AUGMENTED = "选择标记：勾选 = 该增强图加入导出；默认全部不勾选。"
COLUMN_MARK_TOOLTIP = (
    "标记列即是导出列表：原图勾选 = 删除标记（移出导出），增强图勾选 = 选择标记"
    "（加入导出）。两者互相独立，取消勾选即恢复原状。"
)

# The augmentation a copy really got: the draw records the fields it
# selected and the column names them, so the list says what each copy is
# instead of only that it is one. The labels live here, next to the
# column, and a field without a label is shown under its own name.
AUGMENT_FIELD_LABELS = {
    "contrast": "对比度",
    "hsv_v": "亮度",
    "degrees": "旋转",
    "translate": "平移",
    "scale": "缩放",
    "flipud": "垂直翻转",
    "fliplr": "水平翻转",
}
AUGMENT_ITEM_SEPARATOR = "+"
# U+2014 em dash, the one mark of "nothing was drawn here".
AUGMENT_SELECTION_NONE = "—"
COLUMN_AUGMENT_TOOLTIP = (
    "增强列：该副本本次实际抽中的增强项（候选顺序：对比度 / 亮度 / 旋转 / "
    "平移 / 缩放 / 垂直翻转 / 水平翻转），以 + 连接；"
    "原图与未记录抽签结果的副本显示 —。悬停可看副本序号、基种子与尝试号。"
)

# The list follows the file name of a record, so the two blocks the
# kind separates are what is left of the ordering of the previous
# revision: every original first, then the augmented copies. The verdict
# is deliberately not a sort key any more - the filter combo is what
# groups the rows by verdict.
KIND_SORT_ORDER = {
    records_module.KIND_ORIGINAL: 0,
    records_module.KIND_AUGMENTED: 1,
}

GREY_TEXT_COLOR = QtGui.QColor(140, 140, 140)

# The keyboard navigation of the list: one letter per direction, so the
# two hands never leave the mouse row. The follow of the current record
# in the main window costs no letter at all, so the hint only says that
# it happens by itself.
SHORTCUT_HINT = (
    "快捷键：A 上一张 / D 下一张（到首尾停住，选中行自动滚动到可见）；"
    "主窗口跟随当前记录自动打开。"
)
SHORTCUT_TOOLTIP = (
    "结果页快捷键：A = 上一张，D = 下一张；"
    "焦点在本页（含列表内）时生效，到首尾即停，不循环；"
    "当前记录一变，主窗口自动打开该记录，无需按键。"
)

# The legend of the judgement colours, on its own line under the display
# controls. Every entry writes the colour of the canvas constants next to
# the state it means, so the legend can never drift away from what the
# boxes really use. The hint names the source of the states: both
# canvases are coloured by the one matching of the displayed record, so a
# single defect carries a single colour on the left and on the right.
LEGEND_HTML = (
    "<b>图例</b> "
    "<span style='color:{gt}'>匹配</span>=框本色 "
    "<span style='color:{miss}'>漏报 GT</span> "
    "<span style='color:{fp}'>误报 Pred</span> "
    "<span style='color:{cls}'>类别不一致</span> "
    "<span style='color:{iou}'>IoU 低</span> "
    "<span style='color:{low}'>低分</span>"
    "<br/>两侧同源判定：左侧按 GT 状态、右侧按 Pred 状态着色；"
    "无判定的记录不改色。"
)
LEGEND_TOOLTIP = (
    "框色即判定：匹配对保持 GT 绿 / Pred 蓝；漏报（GT 未匹配到预测）橙红；"
    "误报（预测未匹配到 GT）品红；类别不一致橙；IoU 低于阈值黄；"
    "匹配对中预测分数低于 NG 分数阈值青绿。"
    "两侧状态来自同一次匹配，无判定明细的记录保持默认框色。"
)

# The filters of the list: an empty key shows every record, a verdict of
# the records layer shows that verdict (OK and NG are the two entries of
# the combo), and the edit key shows the records a user really touched
# (see record_modified).
FILTER_EDITED = "edited"

# The list opens on the defects: a run is looked at to find its NG rows,
# so the combo starts on NG and every other entry is one click away.
DEFAULT_FILTER = records_module.NG

# The two titles of the canvases; the preview of a held right button
# prefixes them with the state they show instead.
GT_CANVAS_TITLE = "GT 原始标记"
PRED_CANVAS_TITLE = "Pred 推理结果"
PREVIEW_TITLE_PREFIX = "原图结果（预览）"

PREVIEW_TOOLTIP = (
    "按住鼠标右键：增强图显示它的原图（父记录）的图片、标注与判定结果；"
    "松开右键（或再按左键、切换记录）恢复当前增强图。"
)
PREVIEW_HINT_ORIGINAL = "该记录没有父图：右键预览只用于增强图。"
PREVIEW_HINT_MISSING = "该记录的父图不在本次记录列表里，无法预览。"
PREVIEW_NOTE_PREFIX = "原图结果（预览）："
PREVIEW_NOTE_SUFFIX = "松开右键恢复当前增强图"
# The tooltip of the canvases stays the one of the preview, word for
# word: the existing delivery pins that string. The keyboard of the page
# is documented by the hint line under the list instead (see
# SHORTCUT_HINT).
PREVIEW_NOTE_NOT_JUDGED = "（原图未判定，框保持本色）"


class PreviewNoteLabel(QtWidgets.QLabel):
    """The one status line of the preview: one line tall, elided.

    The row this label occupies is reserved while the line is empty, so
    holding and releasing the right button changes a text and never the
    geometry of the pictures below it. A long text is elided at paint
    time instead of being wrapped: the full text stays in text() and in
    toolTip(), the stored text is never rewritten by a paint, and the
    width of the text takes no part in the minimum width of the page.
    """

    def __init__(self, parent: Optional[Any] = None) -> None:
        super().__init__(parent)
        self.setWordWrap(False)
        # the row is as tall as one line of the font of this label, never
        # a fixed number of pixels: a bigger system font moves it with it
        self.setFixedHeight(self.fontMetrics().height())

    def minimumSizeHint(self) -> QtCore.QSize:  # noqa: N802
        """Return one line of room, whatever the text asks for."""

        return QtCore.QSize(0, self.fontMetrics().height())

    def paintEvent(self, event: Any) -> None:  # noqa: N802
        """Paint the elided text without touching the stored text.

        The base paint is deliberately not called: the stored text stays
        the full one for text() and toolTip(), while the row shows what
        fits. Rewriting text() here would raise a fresh update() and the
        line would repaint itself forever.
        """

        painter = QtGui.QPainter(self)
        painter.setFont(self.font())
        painter.setPen(self.palette().color(self.foregroundRole()))
        painter.drawText(
            self.rect(),
            int(self.alignment()),
            self.fontMetrics().elidedText(
                self.text(),
                QtCore.Qt.TextElideMode.ElideRight,
                self.width(),
            ),
        )
        painter.end()


def legend_html() -> str:
    """Return the rich text of the colour legend of the two canvases."""

    return LEGEND_HTML.format(
        gt=GT_COLOR.name(),
        miss=MISS_COLOR.name(),
        fp=FALSE_POSITIVE_COLOR.name(),
        cls=CLASS_MISMATCH_COLOR.name(),
        iou=IOU_BELOW_COLOR.name(),
        low=LOW_SCORE_COLOR.name(),
    )


def matched_pair_state(
    iou: Any, class_state: Any, threshold: Any, low_score: Any = None
) -> str:
    """Return the state of one matched pair of a judgement detail.

    The pair of the judge detail carries its class outcome, its IoU and
    whether its prediction scored under the NG score threshold of the
    run. The three states a reader has to tell apart are the pair of two
    different classes, the pair whose prediction scored under the NG
    score threshold and the pair that stayed under the IoU threshold of
    the run; they are read in the very priority order the judge writes
    its own reason in (see judge.REASON_PRIORITY): a class mismatch wins
    over a low score, which wins over an IoU under the threshold.

    A low score is therefore a state of its own, whatever the IoU of the
    pair is: the user rule of the score threshold is that a prediction
    scoring under it counts as NG as well, and the pair the run matched
    well and still scored low on is exactly the one that would otherwise
    hide behind the plain colour of a valid pair. The IoU rule is only
    read for a pair whose score is fine.

    The pair of two classes that are not even known is reported as an
    unknown label instead of a class mismatch (class_state is
    "unknown_class"), so it keeps the plain colour of its canvas, exactly
    like a pair that is fine. Only the mismatch of the judge is read as
    one: a detail without the key, or with a spelling of a later
    revision, is not a class error of this record.

    `low_score` is the optional tail argument of the frozen contract: a
    detail of an older revision carries no such key, and None - like
    False, or an empty string - is read as "the score of this pair is
    fine", so the three argument call of the previous revision answers
    exactly what it always did.

    The caller decides what a low score means for a box no match points
    at: an unmatched prediction stays the magenta of a false positive
    (see pred_statuses), because the miss signal of its own side is the
    more important one.
    """

    if str(class_state or "") == "mismatch":
        return STATE_CLASS_MISMATCH
    if low_score:
        return STATE_LOW_SCORE
    try:
        below = float(iou) < float(threshold)
    except (TypeError, ValueError):
        return STATE_OK_PAIR
    return STATE_IOU_BELOW if below else STATE_OK_PAIR


def _count(value: Any) -> Optional[int]:
    """Return a non negative count of a judgement detail, None otherwise."""

    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return int(value)


def gt_region_offsets(
    detail: Dict[str, Any], shapes: Sequence[Any]
) -> List[int]:
    """Return the shapes the judge treated as GT regions.

    The judge filters the label it was given - the shapes it can match
    are the region shapes whose point list is not degenerate - and its
    indexes are the positions inside that filtered list, while the
    canvas draws every shape of the label. The very same filter is
    applied again here on the list the canvas shows, which is what maps
    one order onto the other.

    The mapping is only handed out while it provably lines up: the two
    totals have to be the size of the list the canvas has and the filter
    has to keep as many shapes as the detail counted. Two lists that
    disagree - a record judged from another revision, a label rewritten
    in the meantime - keep every box in its default colour instead of
    flagging one box with the judgement of another.
    """

    total = _count(detail.get("gt_total"))
    kept = _count(detail.get("gt_region"))
    if total is None or kept is None or total != len(shapes):
        return []
    offsets = [
        index
        for index, shape in enumerate(shapes)
        if image_view_module.shape_type_of(shape) in REGION_SHAPE_TYPES
        and not judge_module.is_degenerate_shape(
            judge_module.shape_point_list(shape)
        )
    ]
    return offsets if len(offsets) == kept else []


def pred_valid_offsets(
    detail: Dict[str, Any], shapes: Sequence[Any]
) -> List[int]:
    """Return the predictions the judge was able to match.

    A prediction of an ignored shape type is left out of the matching
    entirely and must therefore keep the plain Pred colour: it is a
    shape the tool does not judge, not a false positive.
    """

    total = _count(detail.get("pred_total"))
    kept = _count(detail.get("pred_valid"))
    if total is None or kept is None or total != len(shapes):
        return []
    offsets = [
        index
        for index, shape in enumerate(shapes)
        if judge_module.shape_type_of(shape)
        in judge_module.MATCHABLE_PRED_TYPES
    ]
    return offsets if len(offsets) == kept else []


def gt_statuses(detail: Dict[str, Any], shapes: Sequence[Any]) -> List[Any]:
    """Return the judgement state of every GT shape of one record.

    A region shape no match points at is a miss, the false negative of
    the run; the pair states are read from the matched list of the very
    same judgement. Every other shape - a non region shape, a record
    without a judgement, a detail of an older revision without the score
    keys - keeps an empty state and the default colour.
    """

    statuses: List[Any] = [""] * len(shapes)
    offsets = gt_region_offsets(detail, shapes)
    if not offsets:
        return statuses
    matched = [
        pair
        for pair in (detail.get("matched") or [])
        if isinstance(pair, dict)
    ]
    for pair in matched:
        index = _count(pair.get("gt_index"))
        if index is None or index >= len(offsets):
            continue
        statuses[offsets[index]] = matched_pair_state(
            pair.get("iou"),
            pair.get("class_state"),
            detail.get("ng_iou_threshold"),
            pair.get("low_score"),
        )
    matched_indexes = {_count(pair.get("gt_index")) for pair in matched}
    for position, offset in enumerate(offsets):
        if position not in matched_indexes:
            statuses[offset] = STATE_MISS
    return statuses


def _legacy_pred_statuses(
    detail: Dict[str, Any], shapes: Sequence[Any]
) -> List[Any]:
    """Return the prediction states of a detail written before the rows.

    This is the whole rule of the revision that drew one box per payload
    entry: the judge indexes and the shape list are the very same list,
    so a matchable prediction no match points at is a false positive and
    a prediction of an ignored shape type keeps the plain Pred colour.
    The path of a detail without the row map is frozen here, byte for
    byte, so an old record or an old report keeps exactly the colours it
    always had.
    """

    statuses: List[Any] = [""] * len(shapes)
    offsets = pred_valid_offsets(detail, shapes)
    if not offsets:
        return statuses
    matched = [
        pair
        for pair in (detail.get("matched") or [])
        if isinstance(pair, dict)
    ]
    for pair in matched:
        index = _count(pair.get("pred_index"))
        if index is None or index >= len(offsets):
            continue
        statuses[offsets[index]] = matched_pair_state(
            pair.get("iou"),
            pair.get("class_state"),
            detail.get("ng_iou_threshold"),
            pair.get("low_score"),
        )
    matched_indexes = {_count(pair.get("pred_index")) for pair in matched}
    for position, offset in enumerate(offsets):
        if position not in matched_indexes:
            statuses[offset] = STATE_FALSE_POSITIVE
    return statuses


# The ranking of the prediction states inside one box: the state of a
# box is the highest priority state of its own rows. The order follows
# the reasons of the judge (see judge.REASON_PRIORITY) and puts the
# false positive in front of all of them, exactly like the rule of the
# single row revision (see _legacy_pred_statuses): the miss signal of
# this side is the more important one, whatever the score, the class or
# the IoU of the other rows of the box say.
PRED_STATE_PRIORITY = (
    STATE_FALSE_POSITIVE,
    STATE_CLASS_MISMATCH,
    STATE_LOW_SCORE,
    STATE_IOU_BELOW,
)


def _state_rank(state: Any) -> int:
    """Return the rank of one prediction state, lower is more important.

    An unknown or an empty state - a plain OK pair, a shape the judge
    never matched - is ranked behind every state of the list, so it can
    never overwrite one of them.
    """

    try:
        return PRED_STATE_PRIORITY.index(state)
    except ValueError:
        return len(PRED_STATE_PRIORITY)


def _row_box_indexes(
    detail: Dict[str, Any], row_boxes: Any, box_count: int
) -> Optional[List[int]]:
    """Return the box of every row, None when the map cannot be read.

    The row map is the one the pipeline wrote next to the judgement (see
    pipeline._infer_one): one box index per judged row, in the row
    coordinate system of the matching. It is only handed out while the
    new rule provably lines up with the displayed boxes; every other map
    answers None, which is the "this detail is not one of the row
    revision" answer of the caller.
    """

    if isinstance(row_boxes, (str, bytes)) or not isinstance(
        row_boxes, (list, tuple)
    ):
        return None
    total = _count(detail.get("pred_total"))
    if total is None or total != len(row_boxes):
        return None
    indexes: List[int] = []
    for value in row_boxes:
        index = _count(value)
        if index is None or index >= box_count:
            return None
        indexes.append(index)
    return indexes


def _aligned_rows(
    detail: Dict[str, Any], predictions: Sequence[Any]
) -> Optional[Tuple[List[Dict[str, Any]], List[int]]]:
    """Return the judged rows of a detail and the box of each of them.

    The new rule reads an index of the judge as a row and an index of
    the payload as the box the canvas draws, so the two have to be
    proven to line up before a single box may be flagged: the rows are
    rebuilt from the displayed payload exactly like the pipeline built
    them for the verdict, and the rebuilt rows have to be as many as the
    judgement counted (detail["pred_total"]). None - a detail of the
    previous revision, a report whose payload was rewritten - sends the
    caller to the frozen legacy rule, so no box ever carries the
    judgement of another one.
    """

    if "pred_row_boxes" not in detail:
        return None
    indexes = _row_box_indexes(
        detail, detail.get("pred_row_boxes"), len(predictions)
    )
    if indexes is None:
        return None
    rows, row_boxes = expand_multilabel_rows(predictions)
    if len(rows) != len(indexes) or row_boxes != indexes:
        return None
    return rows, indexes


def pred_statuses(detail: Dict[str, Any], shapes: Sequence[Any]) -> List[Any]:
    """Return the judgement state of every predicted box of one record.

    The judge counts and matches one row per class, while the canvas
    draws one box per prediction: a merged box is therefore coloured by
    the highest priority state of its own rows (see PRED_STATE_PRIORITY),
    which is the aggregation rule of the user: a box whose rows disagree
    carries the most important of them, and the false positive of a row
    no match points at wins over every other row of its box.

    A detail without a row map is not read row by row at all: it is the
    detail of a previous revision and is coloured by the frozen legacy
    rule (see _legacy_pred_statuses), which is exactly what the page did
    before the rows existed. A detail whose rows do not provably line up
    with the displayed payload is answered the same way as a record
    without a judgement: every box keeps the plain Pred colour instead
    of half of them being flagged.

    The states are read from the detail alone; the display copy never
    writes anything back into the record (see matched_pair_state).

    The pred_index of the judge is the index of a pair inside the
    *matchable* rows the verdict was handed (its valid_pred, see
    judge.judge_record), never a position of the row list the canvas
    draws: every index of the matched list is therefore mapped back
    through the matchable row positions below, the very counterpart of
    pred_valid_offsets on the legacy path. A row of another shape type
    may sit in front of a matchable one, and reading pred_index as a row
    position would then flag the rows of one box with the judgement of
    another one.
    """

    if "pred_row_boxes" not in detail:
        return _legacy_pred_statuses(detail, shapes)
    aligned = _aligned_rows(detail, shapes)
    if aligned is None:
        return _legacy_pred_statuses(detail, shapes)
    rows, row_boxes = aligned
    valid = [
        index
        for index, row in enumerate(rows)
        if str(row.get("shape_type") or "")
        in judge_module.MATCHABLE_PRED_TYPES
    ]
    if len(valid) != _count(detail.get("pred_valid")):
        # the verdict counted another matchable list than the one the
        # canvas shows: the guard failed, so the whole record degrades to
        # the plain colours instead of a half coloured picture
        return [""] * len(shapes)
    matched: Dict[int, str] = {}
    for pair in detail.get("matched") or []:
        if not isinstance(pair, dict):
            continue
        # the pred_index of the judge is the index of the pair inside the
        # matchable rows it was handed, so valid - the row positions of
        # the matchable rows on screen - is what maps it back to a row
        # (pred_valid_offsets does the same for the payload of the legacy
        # path). Reading it as a row position would shift the judgement
        # of a row onto another one as soon as the row list carries a
        # shape type the judge never matches.
        index = _count(pair.get("pred_index"))
        if index is None or index >= len(valid):
            continue
        matched[valid[index]] = matched_pair_state(
            pair.get("iou"),
            pair.get("class_state"),
            detail.get("ng_iou_threshold"),
            pair.get("low_score"),
        )
    # the rows of one box are the only ones sharing their box index, so
    # the highest priority state of a box is found in one scan
    boxes: List[Any] = [""] * len(shapes)
    for index in valid:
        state = matched.get(index, STATE_FALSE_POSITIVE)
        box = row_boxes[index]
        if _state_rank(boxes[box]) > _state_rank(state):
            boxes[box] = state
    return boxes


def sort_chunk_key(chunk: object) -> Tuple[int, int, str]:
    """Return a comparable key of one natural sort chunk.

    dataset.natural_key already tags every chunk with its kind: (0,
    count) for a run of digits, (1, char) for any other character, so
    the chunks of two names are comparable even when one name starts
    with a digit and the other with a letter. A bare int or str - a
    chunk list a caller built by hand - is tagged here as well. The tag
    keeps the numeric order of the digits ("a2" before "a10") and puts a
    digit before any other character, which is the order the file
    manager shows as well.
    """

    if isinstance(chunk, tuple) and len(chunk) == 2:
        kind, value = chunk
        if kind == 0 and isinstance(value, int):
            return (0, int(value), "")
        return (1, 0, str(value))
    if isinstance(chunk, bool):
        return (1, 0, str(chunk))
    if isinstance(chunk, int):
        return (0, int(chunk), "")
    return (1, 0, str(chunk))


def record_sort_key(record: ValidationRecord) -> Tuple[int, List[Any]]:
    """Return the list sort key of one record: kind, then file name.

    The file name is compared in natural order, so a0_dian_2 sits right
    before a0_dian_10 instead of after it. The kind is the only key in
    front of it: every original is shown before the augmented copies.
    """

    return (
        KIND_SORT_ORDER.get(record.kind, 9),
        [
            sort_chunk_key(chunk)
            for chunk in dataset.natural_key(record.relpath)
        ],
    )


def marked_record(record: ValidationRecord) -> bool:
    """Return True when a record carries one of the two marks.

    The delete mark of an original (records.deleted) and the selection
    mark of an augmented copy (records.include_in_export) are the two
    flags the export formula reads, and the 标记 filter is exactly that
    union: the filter never keeps a second, private list of its own.
    """

    return bool(record.deleted or record.include_in_export)


def marked_records(
    records: Sequence[ValidationRecord],
) -> List[ValidationRecord]:
    """Return the records carrying the delete or the export mark."""

    return [record for record in records if marked_record(record)]


def record_modified(record: ValidationRecord) -> bool:
    """Return True when a user changed or marked a record.

    This is the 修改 filter, and it is the union of the two ways a
    record can leave its own run: the *edit* flag a correction made in
    the main window writes (records.edited) and the two marks of the
    list - the delete mark of an original and the export mark of an
    augmented copy (see marked_record). The filter reads those flags
    themselves and never keeps a list of its own.
    """

    return bool(record.edited) or marked_record(record)


def edited_records(
    records: Sequence[ValidationRecord],
) -> List[ValidationRecord]:
    """Return the records a user changed or marked (the 修改 filter)."""

    return [record for record in records if record_modified(record)]


def parent_of(
    lookup: Dict[str, ValidationRecord],
    record: Optional[ValidationRecord],
) -> Optional[ValidationRecord]:
    """Return the original an augmented record was made from.

    Only an augmented copy has a parent, so an original - and a copy
    whose original is not part of the list - answers None; the right
    button preview of the page is built on that answer.
    """

    if record is None or record.kind != records_module.KIND_AUGMENTED:
        return None
    parent_id = str(record.parent_record_id or "")
    if not parent_id:
        return None
    return lookup.get(parent_id)


def augment_field_label(name: Any) -> str:
    """Return the label of one augmentation field of the draw.

    A field this page does not know - a name a later revision added - is
    shown as it is, so the column never swallows what the run recorded.
    """

    text = str(name)
    return AUGMENT_FIELD_LABELS.get(text, text)


def augment_selection_text(record: ValidationRecord) -> str:
    """Return the augmentation column text of one record.

    Only an augmented copy carries a draw: an original and a copy whose
    aug_detail holds no usable selection both answer the dash. The names
    keep the order the draw recorded them in - the cell never sorts and
    never deduplicates what the run really applied - and a name without a
    label of its own is written as it came. Nothing here raises: a record
    of an older shape answers the dash instead.
    """

    if str(getattr(record, "kind", "")) != records_module.KIND_AUGMENTED:
        return AUGMENT_SELECTION_NONE
    detail = getattr(record, "aug_detail", None)
    if not isinstance(detail, dict):
        return AUGMENT_SELECTION_NONE
    selected = detail.get("selected")
    if not isinstance(selected, (list, tuple)) or not selected:
        return AUGMENT_SELECTION_NONE
    return AUGMENT_ITEM_SEPARATOR.join(
        augment_field_label(name) for name in selected
    )


def augment_detail_tooltip(record: ValidationRecord) -> str:
    """Return the reproducible detail of one augmented copy.

    The tooltip carries what it takes to replay the very copy: the fields
    the draw selected, its copy index, the base seed, the try that really
    produced it and the seed of that try. A part is written only when the
    record really holds the number - an older record keeps the parts it
    has instead of getting a made up one - and an original always answers
    the empty string.
    """

    if str(getattr(record, "kind", "")) != records_module.KIND_AUGMENTED:
        return ""
    detail = getattr(record, "aug_detail", None)
    if not isinstance(detail, dict):
        return ""
    parts = ["增强项：" + augment_selection_text(record)]
    copy_index = detail.get("copy_index")
    if isinstance(copy_index, int):
        parts.append("副本 #{0}".format(copy_index))
    seed = detail.get("seed")
    if isinstance(seed, int):
        parts.append("基种子 {0}".format(seed))
    attempt = detail.get("attempt")
    if isinstance(attempt, int):
        parts.append("第 {0} 次尝试".format(attempt + 1))
    attempt_seed = detail.get("attempt_seed")
    if isinstance(attempt_seed, int):
        parts.append("尝试种子 {0}".format(attempt_seed))
    return "；".join(parts)


class RecordItem(QtWidgets.QTreeWidgetItem):
    """One cell item of the read only record tree.

    A tree item holds its text and its data per column, while the table
    cell the page used to build had one column only and defaulted to it.
    The three overrides keep that convenience - they do not add any
    editing capability, the item stays exactly as read only as its base
    class and the widget refuses every edit trigger anyway.
    """

    def text(self, column: int = COLUMN_VERDICT) -> str:  # noqa: N802
        """Return the text of one column, the status column by default."""

        return super().text(int(column))

    def setText(self, column: int, text: str) -> None:  # noqa: N802
        """Set the text of one column."""

        super().setText(int(column), text)

    def data(  # noqa: N802
        self,
        column: Any = COLUMN_VERDICT,
        role: Any = QtCore.Qt.ItemDataRole.UserRole,
    ) -> Any:
        """Return the data of one column and role of this item.

        A table item carries a single value, so its data(role) call names
        the role alone; a tree item names the column first. Both spellings
        come back with the data of the status column, which is the column
        the page stores the record id in.
        """

        if isinstance(column, QtCore.Qt.ItemDataRole):
            column, role = COLUMN_VERDICT, column
        return super().data(int(column), role)


class RecordTree(QtWidgets.QTreeWidget):
    """Read only, one record per row, five columns.

    The export list was a QTableWidget, whose cells are editable by
    default: a stray double click or F2 let the user retype a status or a
    relpath that the run had just produced. This widget is the read only
    replacement. It is a tree with the decoration switched off and every
    edit trigger refused, so a row can only be selected and the checkbox
    it carries can only be toggled.

    The page reads a row as (mark, verdict, relpath, kind, augment),
    the very shape the table exposed: item(row, column) returns the cell
    text item, cellWidget the checkbox of the mark column and rowCount
    the amount of shown rows.
    """

    def __init__(
        self,
        headers: Sequence[str],
        parent: Optional[Any] = None,
    ) -> None:
        super().__init__(parent)
        self.setColumnCount(len(headers))
        self.setHeaderLabels([str(header) for header in headers])
        self.setRootIsDecorated(False)
        self.setUniformRowHeights(True)
        self.setItemsExpandable(False)
        self.setAllColumnsShowFocus(False)
        # Editing is refused three times over: no edit trigger (the
        # default of the class is spelled out because it is the whole
        # point of this widget), no edit flag on an item and an editItem
        # that never enters the edit state.
        self.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection
        )
        header = self.header()
        header.setSectionsClickable(True)
        header.setStretchLastSection(False)
        header.setSectionResizeMode(
            COLUMN_RELPATH, QtWidgets.QHeaderView.ResizeMode.Stretch
        )
        for column in (
            COLUMN_MARK,
            COLUMN_VERDICT,
            COLUMN_KIND,
            COLUMN_AUGMENT,
        ):
            header.setSectionResizeMode(
                column, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
            )
        mark_header = self.headerItem()
        if mark_header is not None:
            mark_header.setToolTip(COLUMN_MARK, COLUMN_MARK_TOOLTIP)
            mark_header.setToolTip(COLUMN_AUGMENT, COLUMN_AUGMENT_TOOLTIP)

    # ------------------------------------------------- table like accessors
    def rowCount(self) -> int:  # noqa: N802
        """Return the amount of shown records."""

        return self.topLevelItemCount()

    def item(  # noqa: N802
        self, row: int, column: int
    ) -> Optional[QtWidgets.QTreeWidgetItem]:
        """Return the cell of one row, None for an empty mark cell."""

        top = self.topLevelItem(int(row))
        if top is None or not 0 <= int(column) < self.columnCount():
            return None
        if int(column) == COLUMN_MARK and self.itemWidget(top, column):
            # the mark column carries a checkbox widget, so its cell is
            # never the carrier of the record id: a caller iterating the
            # cells finds the very same split the table had
            return None
        return top

    def cellWidget(  # noqa: N802
        self, row: int, column: int
    ) -> Optional[QtWidgets.QWidget]:
        """Return the widget of a cell, the mark checkbox in practice."""

        top = self.topLevelItem(int(row))
        if top is None:
            return None
        return self.itemWidget(top, int(column))

    def setCellWidget(  # noqa: N802
        self, row: int, column: int, widget: QtWidgets.QWidget
    ) -> None:
        """Put a widget into one cell, the mark checkbox in practice."""

        top = self.topLevelItem(int(row))
        if top is not None:
            self.setItemWidget(top, int(column), widget)

    def selectRow(self, row: int) -> None:  # noqa: N802
        """Select one row and refresh the right hand side."""

        top = self.topLevelItem(int(row))
        if top is not None:
            self.setCurrentItem(top)

    def currentRow(self) -> int:  # noqa: N802
        """Return the row of the current item, -1 when there is none."""

        return self.indexOfTopLevelItem(self.currentItem())

    def horizontalHeaderItem(  # noqa: N802
        self, column: int
    ) -> Optional[QtWidgets.QTreeWidgetItem]:
        """Return the header cell of a column, the tree header item.

        A tree keeps one header item holding every column, while a table
        returns a per column wrapper of it: both expose text() and
        setToolTip(), which is what the page and the tests read.
        """

        if not 0 <= int(column) < self.columnCount():
            return None
        return self.headerItem()

    def selectedIndexes(self) -> List[QtCore.QModelIndex]:  # noqa: N802
        """Return one cell index per column of every selected row."""

        indexes: List[QtCore.QModelIndex] = []
        for item in self.selectedItems():
            if self.indexOfTopLevelItem(item) < 0:
                continue
            for column in range(self.columnCount()):
                indexes.append(self.indexFromItem(item, column))
        return indexes

    # Every path that could open an editor is closed here as well, so a
    # caller reaching the widget through Qt itself cannot edit a row.
    #
    # editItem is refused with the signature Qt declares for it,
    # editItem(item, column=0). The virtual edit(index, trigger, event) is
    # deliberately NOT overridden: Qt itself calls it with three
    # arguments, so a one argument override of that name turns every
    # standard edit path - a double click, F2, a programmatic call - into
    # a TypeError inside the event handler instead of a refusal.
    # NoEditTriggers and the read only item flags are what close the
    # editor here, not this method.
    def editItem(  # noqa: N802
        self,
        item: QtWidgets.QTreeWidgetItem,
        column: int = 0,
    ) -> None:
        """Refuse to start editing an item."""

        return None


def model_note_text(classes: Sequence[str], diff_count: int = 0) -> str:
    """Return the line naming the class table that produced the labels.

    The names embedded in the ONNX are a hint only: the note states the
    effective table of classes.txt and, when the embedded names differ,
    how many differences were found.
    """

    names = [str(name) for name in classes]
    if not names:
        return ""
    text = "生效类别表：{count} 类（以 classes.txt 为准）".format(
        count=len(names)
    )
    if diff_count:
        text += "；与模型内嵌 names 差异 {count} 处".format(
            count=int(diff_count)
        )
    return text


class ResultsPage(QtWidgets.QWidget):
    """Browse the validated records and mark the export selection."""

    toggle_deleted = QtCore.pyqtSignal(list, bool)
    toggle_export = QtCore.pyqtSignal(list, bool)
    export_requested = QtCore.pyqtSignal()
    # The record the page shows changed. The window that holds this page
    # is what follows it: the signal carries the record id of the new
    # current row, and "" once the page is left with nothing to show,
    # which is what a filter or a selection that drops the record on
    # screen produces. A new list is not one of those switches: the
    # announced record is reset together with the list (see set_records),
    # so a list that arrives empty never turns the move into a "". It is
    # raised once per change, never for a selection event that landed on
    # the row already on screen.
    current_record_changed = QtCore.pyqtSignal(str)
    selection_changed = QtCore.pyqtSignal(str)

    def __init__(self, parent: Optional[Any] = None) -> None:
        super().__init__(parent)
        self.records: List[ValidationRecord] = []
        self.classes: List[str] = []
        self.model_info: Dict[str, Any] = {}
        self._loading = False
        self._syncing_view = False
        # True while the rebuild a filter switch asked for is waiting for
        # the next turn of the event loop (see _schedule_refresh)
        self._refresh_scheduled = False
        # the rows the table currently shows: row -> record id for the
        # point update of a mark, record id -> row to find them back
        self._row_ids: List[str] = []
        self._rows: Dict[str, int] = {}
        self._records_by_id: Dict[str, ValidationRecord] = {}
        # the record id the page announced last: the follow in the main
        # window is only asked for when the record on screen moved on
        self._shown_record_id = ""
        # the picture of the current row is decoded once: a display
        # toggle must repaint the overlays, never reload the file and
        # therefore never refit a view the user zoomed by hand
        self._pixmap_path = ""
        self._pixmap: Optional[QtGui.QPixmap] = None
        # the preview of a held right button: while _preview_active is
        # True the two canvases show _preview_record, the original the
        # current augmented record was made from
        self._preview_active = False
        self._preview_record: Optional[ValidationRecord] = None
        self._build_ui()

    def _build_ui(self) -> None:
        """Create every widget of the page."""

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        toolbar = QtWidgets.QHBoxLayout()
        self.filter_combo = QtWidgets.QComboBox()
        self.filter_combo.addItem(self.tr("全部"), "")
        for verdict in (records_module.OK, records_module.NG):
            self.filter_combo.addItem(verdict, verdict)
        self.filter_combo.addItem(self.tr("修改"), FILTER_EDITED)
        # the page opens on the defects of the run; 全部 is the first
        # entry of the list and stays one click away
        self.filter_combo.setCurrentIndex(
            max(self.filter_combo.findData(DEFAULT_FILTER), 0)
        )
        self.filter_combo.setToolTip(
            self.tr(
                "按状态过滤左表：修改 = 改过 ROI、改过标签、带删除标记的"
                "原图或已加入导出的增强图；过滤只决定左表显示哪些行，"
                "标记与导出规则不受影响。"
            )
        )
        # the switch only asks for a rebuild: the heavy part of it runs
        # one turn later, never in the stack that is still closing the
        # popup of the list (see _schedule_refresh)
        self.filter_combo.currentIndexChanged.connect(self._schedule_refresh)
        toolbar.addWidget(QtWidgets.QLabel(self.tr("过滤")))
        toolbar.addWidget(self.filter_combo)
        # the toolbar holds the filter alone: the two batch commands of
        # the previous revision ("增强图全选加入" / "全部取消") and the
        # delete toggle of the selected originals are gone from the page.
        # The three of them stay reachable on the context menu of the
        # list, which is where a batch command costs no room.
        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        self.table = RecordTree([self.tr(h) for h in HEADERS])
        self.table.setToolTip(
            self.tr(
                "只读列表：单元格不可编辑（双击/F2 均无效），"
                "仅第一列的复选框可勾选。"
            )
            + self.tr(SHORTCUT_TOOLTIP)
        )
        self.table.setContextMenuPolicy(
            QtCore.Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        splitter.addWidget(self.table)

        right = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        options = QtWidgets.QHBoxLayout()
        self.gt_check = QtWidgets.QCheckBox(self.tr("GT 绿色"))
        self.gt_check.setChecked(True)
        self.gt_check.setToolTip(self.tr("显示左幅的原始标注（GT）。"))
        self.gt_check.toggled.connect(self._refresh_canvases)
        self.pred_check = QtWidgets.QCheckBox(self.tr("Pred 蓝色"))
        self.pred_check.setChecked(True)
        self.pred_check.setToolTip(self.tr("显示右幅的推理结果（Pred）。"))
        self.pred_check.toggled.connect(self._refresh_canvases)
        self.overlay_check = QtWidgets.QCheckBox(self.tr("叠加"))
        self.overlay_check.setChecked(True)
        self.overlay_check.setToolTip(
            self.tr("显示两幅上的标注叠加层；只影响显示，不影响导出。")
        )
        self.overlay_check.toggled.connect(self._refresh_canvases)
        # the display switch of the annotated boxes: the labels
        # themselves. A label never carries a background of its own and a
        # box is never filled, whatever this switch says.
        self.label_check = QtWidgets.QCheckBox(self.tr("显示标签"))
        self.label_check.setChecked(True)
        self.label_check.setToolTip(
            self.tr(
                "显示每个框左上角的 label（Pred 另带 score）；默认显示，"
                "取消勾选后只留框本身。标签始终只画文字加一圈深色细描边，"
                "不带任何背景块。"
            )
        )
        self.label_check.toggled.connect(self._refresh_canvases)
        options.addWidget(self.gt_check)
        options.addWidget(self.pred_check)
        options.addWidget(self.overlay_check)
        options.addWidget(self.label_check)
        options.addStretch(1)
        right_layout.addLayout(options)

        # The legend of the judgement colours closes the display controls:
        # the display row shares its width with the two canvases below it,
        # where the five entries of the legend do not fit, so the legend
        # gets the line of its own under the row. It is rich text of a few
        # words and never pushes the window wider than the width the two
        # canvases need: a label only ever shrinks.
        self.legend_label = QtWidgets.QLabel(legend_html())
        self.legend_label.setToolTip(self.tr(LEGEND_TOOLTIP))
        self.legend_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
        legend_row = QtWidgets.QHBoxLayout()
        legend_row.setContentsMargins(0, 0, 0, 0)
        legend_row.addWidget(self.legend_label)
        legend_row.addStretch(1)
        right_layout.addLayout(legend_row)

        # the one line the preview of a held right button writes its
        # state into: the row stays while the text is empty, so a press
        # and a release change a word and never the room the pictures
        # below get (see PreviewNoteLabel)
        self.preview_note = PreviewNoteLabel()
        right_layout.addWidget(self.preview_note)

        canvases = QtWidgets.QHBoxLayout()
        self.gt_canvas = ImageCanvas(self.tr(GT_CANVAS_TITLE))
        self.pred_canvas = ImageCanvas(self.tr(PRED_CANVAS_TITLE))
        # the right button previews the original (see
        # begin_parent_preview): both canvases hand their press and their
        # release to this page as an event, and neither of them opens a
        # context menu, which would fight that press
        for canvas in (self.gt_canvas, self.pred_canvas):
            canvas.setToolTip(self.tr(PREVIEW_TOOLTIP))
            canvas.setContextMenuPolicy(
                QtCore.Qt.ContextMenuPolicy.NoContextMenu
            )
            canvas.installEventFilter(self)
        self.gt_canvas.view_changed.connect(
            lambda *_state: self._mirror_view(self.gt_canvas, self.pred_canvas)
        )
        self.pred_canvas.view_changed.connect(
            lambda *_state: self._mirror_view(self.pred_canvas, self.gt_canvas)
        )
        canvases.addWidget(self.gt_canvas, 1)
        canvases.addWidget(self.pred_canvas, 1)
        # the two pictures own the whole right hand side: the detail
        # table of the previous revision is gone, every label and every
        # score is written into the box it belongs to instead
        right_layout.addLayout(canvases, 1)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 4)
        layout.addWidget(splitter, 1)

        bottom = QtWidgets.QHBoxLayout()
        self.staging_edit = QtWidgets.QLineEdit()
        self.staging_edit.setReadOnly(True)
        self.staging_edit.setPlaceholderText(self.tr("暂存目录"))
        bottom.addWidget(self.staging_edit, 1)
        # the path line stays and stays read only: the user selects and
        # copies the text itself, the page owns no Copy button
        self.export_button = QtWidgets.QPushButton(self.tr("导出 Zip..."))
        self.export_button.setToolTip(
            self.tr("按当前标记导出：未删原图 + 勾选的增强图。")
        )
        self.export_button.clicked.connect(self.export_requested.emit)
        bottom.addWidget(self.export_button)
        layout.addLayout(bottom)

        self.model_note = QtWidgets.QLabel("")
        self.model_note.setWordWrap(True)
        self.model_note.setVisible(False)
        layout.addWidget(self.model_note)

        self.status_label = QtWidgets.QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        # the keyboard navigation is written down where it is used: the
        # hint sits under the export line, one line below the list
        self.shortcut_hint = QtWidgets.QLabel(self.tr(SHORTCUT_HINT))
        self.shortcut_hint.setToolTip(self.tr(SHORTCUT_TOOLTIP))
        layout.addWidget(self.shortcut_hint)

        self._install_shortcuts()

    def _install_shortcuts(self) -> None:
        """Install the A / D navigation of the list.

        Both shortcuts belong to this page *and to its children*, so they
        fire while the list (or any other control of the page) holds the
        focus and they never fire while the user works in another window.
        A single letter is no shortcut of the tree itself, so the list
        never swallows the key.
        """

        self.previous_shortcut = QtGui.QShortcut(
            QtGui.QKeySequence(QtCore.Qt.Key.Key_A), self
        )
        self.previous_shortcut.setContext(
            QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        self.previous_shortcut.activated.connect(self.select_previous_record)
        self.next_shortcut = QtGui.QShortcut(
            QtGui.QKeySequence(QtCore.Qt.Key.Key_D), self
        )
        self.next_shortcut.setContext(
            QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        self.next_shortcut.activated.connect(self.select_next_record)

    # ---------------------------------------------------------- navigation
    def select_relative_row(self, step: int) -> bool:
        """Move the row selection by one step and show the new record.

        The list never wraps: the first and the last row are the two ends
        of the run and the selection stops on them. The new row is made
        current and scrolled into view, and the right hand side follows
        through the selection signal of the tree, which keeps the
        existing "a fresh record is fitted once" behaviour.
        """

        count = self.table.rowCount()
        if count <= 0 or not step:
            return False
        row = self.table.currentRow()
        if row < 0:
            row = 0 if step > 0 else count - 1
        else:
            row = int(min(max(row + int(step), 0), count - 1))
        item = self.table.topLevelItem(row)
        if item is None:
            return False
        self.table.setCurrentItem(item)
        self.table.scrollToItem(item)
        return True

    def select_next_record(self) -> bool:
        """Select the next row (the D shortcut)."""

        return self.select_relative_row(1)

    def select_previous_record(self) -> bool:
        """Select the previous row (the A shortcut)."""

        return self.select_relative_row(-1)

    # ----------------------------------------------------------------- data
    def set_model_note(self, model_info: Optional[Dict[str, Any]] = None):
        """Show which class table produced the labels of the results.

        The line stays hidden while no class table is known, so the
        results page keeps the room of its own compact layout until a
        real run filled the model snapshot.
        """

        if model_info is not None:
            self.model_info = dict(model_info)
        classes = list(self.model_info.get("classes") or [])
        if not classes:
            classes = list(self.classes)
        text = model_note_text(
            classes, len(self.model_info.get("classes_name_diff") or [])
        )
        self.model_note.setText(self.tr(text))
        self.model_note.setVisible(bool(text))

    def set_context(self, classes: Sequence[str], staging_root: str) -> None:
        """Update the class list and the staging folder shown at the bottom."""

        self.classes = [str(name) for name in classes]
        self.staging_edit.setText(staging_root)
        self.set_model_note()

    def set_records(self, records: Sequence[ValidationRecord]) -> None:
        """Replace the displayed records.

        A new list is a new state of the page: the record it opens on is
        announced again even when it carries the id of the record the
        previous list was left on (the same dataset validated twice), so
        the follow in the main window opens the picture of the new run
        instead of keeping the one of the old staging folder.
        """

        self.records = list(records)
        self._records_by_id = records_module.record_lookup(self.records)
        self._shown_record_id = ""
        self.refresh()

    def visible_records(self) -> List[ValidationRecord]:
        """Return the records matching the current filter.

        The combo is what decides: an empty key shows every record, the
        edit key shows the records a user touched (see record_modified)
        and every other key is a verdict of the records layer. A verdict
        the combo does not offer - PENDING, SKIPPED, NOT_JUDGED - is
        still part of 全部 and keeps the status of its own row: it is
        only the *filter* that has no entry of its own for it.
        """

        key = self.filter_combo.currentData()
        if not key:
            return list(self.records)
        if key == FILTER_EDITED:
            return edited_records(self.records)
        return [r for r in self.records if r.verdict == key]

    def record_at(self, row: int) -> Optional[ValidationRecord]:
        """Return the record one shown row belongs to."""

        if not 0 <= int(row) < len(self._row_ids):
            return None
        return self._records_by_id.get(self._row_ids[int(row)])

    def deleted_parent_ids(self) -> set:
        """Return the ids of the originals carrying a delete mark.

        The set is built once per pass over the list: the parent state of
        a row is then a dictionary hit instead of a scan of every record,
        which is what keeps the rebuild of a long list linear.
        """

        return {r.record_id for r in self.records if r.deleted}

    def _schedule_refresh(self) -> None:
        """Ask for the rebuild a filter switch needs, once per turn.

        The rebuild of the table is the heavy part of a filter switch -
        every row of a list of up to 19000 records is created again - and
        the combo raises its signal while the popup of the list is still
        closing and a held arrow key still moves through the entries.
        The work is therefore moved out of that synchronous stack: the
        flag keeps a burst of switches down to a single rebuild, and the
        rebuild reads the combo again, so the state the list is left in
        is the one that is built (see refresh).
        """

        if self._refresh_scheduled:
            return
        self._refresh_scheduled = True
        QtCore.QTimer.singleShot(0, self._run_scheduled_refresh)

    def _run_scheduled_refresh(self) -> None:
        """Run the rebuild a filter switch asked for, if it is still due.

        An entry that rebuilds synchronously - set_records, or a refresh
        the asker ran itself - already cleared the flag, so the rebuild
        of a switch that is over costs nothing here.
        """

        if not self._refresh_scheduled:
            return
        self.refresh()

    def refresh(self) -> None:
        """Rebuild the table from the record list.

        A rebuild is what a filter change or a new record list needs:
        every row is created again, in file name order, and the selection
        is restored by record id - the first row when the record that was
        shown is not part of the list any more. A mark toggle never comes
        here: it refreshes its own rows alone (see refresh_rows), so the
        scroll position and the selection of a long list survive a click.

        The filter switch reaches this method through
        _schedule_refresh, one turn after the combo moved, and a
        synchronous caller cancels the rebuild such a switch left
        pending by rebuilding right away.

        A rebuild of a list that is on screen costs about a hundred
        times more than the very same rebuild of a hidden one: every row
        that is added is laid out and painted, and the cost grows with
        the length of the list (five seconds for the 1500 records of a
        real run - the freeze a filter switch used to cause). The rows
        are therefore created with the table hidden and shown again
        right after; a page that is not on screen pays none of that, so
        nothing is hidden for it.
        """

        self._refresh_scheduled = False
        current = self.current_record()
        was_visible = self.table.isVisible()
        had_focus = self.table.hasFocus()
        if was_visible:
            self.table.hide()
        try:
            self._rebuild_rows(current)
        finally:
            if was_visible:
                self.table.show()
                if had_focus:
                    # hide() takes the keyboard away from the list, and
                    # the A / D navigation of the page is used from that
                    # focus: only a list that had it gets it back, so a
                    # focus that was somewhere else stays there.
                    self.table.setFocus()

    def _rebuild_rows(self, current: Optional[ValidationRecord]) -> None:
        """Create every row of the table for the current filter.

        Called by refresh() alone, which owns hiding the table around
        this pass (see there).
        """

        self._loading = True
        records = sorted(self.visible_records(), key=record_sort_key)
        deleted_parents = self.deleted_parent_ids()
        self.table.clear()
        for record in records:
            row = self.table.topLevelItemCount()
            self.table.addTopLevelItem(RecordItem(self.table))
            self._fill_row(
                row,
                record,
                parent_deleted=record.parent_record_id in deleted_parents,
            )
        self._row_ids = [record.record_id for record in records]
        self._rows = {
            record_id: row for row, record_id in enumerate(self._row_ids)
        }
        self._loading = False
        self._select_default_row(current)
        self._on_selection_changed()

    def refresh_rows(self, record_ids: Sequence[str]) -> List[int]:
        """Rewrite the rows of the given records in place.

        This is the cheap path of a mark toggle: the cells of the
        affected rows are written again - the status text, the relpath
        note, the strike out, the grey of an orphaned copy and the state
        of the mark checkbox - and nothing else of the table is touched.
        No row is created, destroyed or moved, therefore the scroll
        position, the selection and every row the toggle did not affect
        stay exactly as they were.

        Returns the rows that were rewritten, the very set the caller
        computed (see records.affected_record_ids).
        """

        rows: List[int] = []
        deleted_parents = self.deleted_parent_ids()
        self._loading = True
        try:
            for record_id in record_ids:
                row = self._rows.get(str(record_id))
                if row is None:
                    continue
                record = self.record_at(row)
                if record is None:
                    continue
                self._fill_row(
                    row,
                    record,
                    parent_deleted=record.parent_record_id in deleted_parents,
                )
                rows.append(row)
        finally:
            self._loading = False
        return rows

    def _select_default_row(
        self, previous: Optional[ValidationRecord]
    ) -> None:
        """Select the row shown after the list was rebuilt.

        The record the page showed before is selected again while it is
        still part of the list; otherwise the first row is selected, so
        the page opens on a picture and its annotations instead of an
        empty, black area. An empty list keeps the empty selection.
        """

        if not self._row_ids:
            return
        row = self._rows.get(previous.record_id if previous else "")
        if row is None:
            row = 0
        self.table.selectRow(row)

    def _fill_row(
        self,
        row: int,
        record: ValidationRecord,
        parent_deleted: Optional[bool] = None,
    ) -> None:
        """Fill one row for a record, in place.

        The row is read only: the checkbox of the mark column is a widget
        of its own and the four text cells are plain cells of the tree,
        none of them can be edited in place. The method is shared by the
        full rebuild of refresh() and by the point update of
        refresh_rows(), which is why it writes every attribute it owns: a
        row whose record was restored has to lose its strike out and its
        grey again instead of keeping the paint of the previous pass.
        """

        if parent_deleted is None:
            parent_deleted = records_module.parent_deleted(
                self.records, record
            )
        verdict_text = record.verdict
        if record.edited:
            verdict_text += " (edited)"
        relpath_text = record.relpath
        if parent_deleted:
            relpath_text += self.tr("（父图已删除，导出排除）")

        texts = (
            verdict_text,
            relpath_text,
            record.kind,
            augment_selection_text(record),
        )
        columns = (
            COLUMN_VERDICT,
            COLUMN_RELPATH,
            COLUMN_KIND,
            COLUMN_AUGMENT,
        )
        for column, text in zip(columns, texts):
            item = self.table.item(row, column)
            if item is None:
                continue
            item.setText(column, text)
            if column == COLUMN_VERDICT:
                item.setData(
                    column,
                    QtCore.Qt.ItemDataRole.UserRole,
                    record.record_id,
                )
            if column == COLUMN_AUGMENT:
                item.setToolTip(COLUMN_AUGMENT, augment_detail_tooltip(record))
            # the flags of a row are read only, so the strike out of a
            # deleted original and the grey of an orphaned copy are
            # painted instead of being typed over; the empty brush gives
            # the default colour back to a restored row
            font = item.font(column)
            font.setStrikeOut(bool(record.deleted))
            item.setFont(column, font)
            item.setForeground(
                column,
                (
                    QtGui.QBrush(GREY_TEXT_COLOR)
                    if parent_deleted
                    else QtGui.QBrush()
                ),
            )

        self._set_mark_box(row, record)

    def _set_mark_box(self, row: int, record: ValidationRecord) -> None:
        """Write the mark state into the checkbox of one row.

        The checkbox of a row is created once, when the row is built, and
        a point update only writes its state back: the widget the user is
        clicking is never replaced, and the write is signal guarded so
        that it can not travel back as a fresh toggle.
        """

        box = self.table.cellWidget(row, COLUMN_MARK)
        if (
            not isinstance(box, QtWidgets.QCheckBox)
            or box.property("recordId") != record.record_id
        ):
            self.table.setCellWidget(row, COLUMN_MARK, self._mark_box(record))
            return
        checked = self.mark_state(record)
        if box.isChecked() == checked:
            return
        blocked = box.blockSignals(True)
        box.setChecked(checked)
        box.blockSignals(blocked)

    def _mark_box(self, record: ValidationRecord) -> QtWidgets.QCheckBox:
        """Build the checkbox of the mark column for one record.

        The checkbox shows the flag itself instead of copying it: an
        original carries the delete mark (records.deleted) and an
        augmented record the selection mark
        (records.include_in_export), both False out of the box.
        """

        augmented = record.kind == records_module.KIND_AUGMENTED
        checked = (
            bool(record.include_in_export)
            if augmented
            else bool(record.deleted)
        )
        box = QtWidgets.QCheckBox()
        box.setChecked(checked)
        # the record a checkbox belongs to: a point update finds its own
        # widget back and only writes the state into it
        box.setProperty("recordId", record.record_id)
        box.setToolTip(
            self.tr(
                MARK_TOOLTIP_AUGMENTED if augmented else MARK_TOOLTIP_ORIGINAL
            )
        )
        box.stateChanged.connect(
            lambda state, rid=record.record_id, aug=augmented: (
                self._on_mark_changed(rid, aug, state)
            )
        )
        return box

    def _on_mark_changed(
        self, record_id: str, augmented: bool, state: int
    ) -> None:
        """Forward one mark toggle to the dialog.

        The two marks stay independent switches: a checkbox of an
        original moves records.deleted and a checkbox of an augmented
        record moves records.include_in_export, never both.
        """

        if self._loading:
            return
        checked = state == QtCore.Qt.CheckState.Checked.value
        if augmented:
            self.toggle_export.emit([str(record_id)], checked)
        else:
            self.toggle_deleted.emit([str(record_id)], checked)

    def mark_state(self, record: ValidationRecord) -> bool:
        """Return the state the mark column shows for one record."""

        if record.kind == records_module.KIND_AUGMENTED:
            return bool(record.include_in_export)
        return bool(record.deleted)

    def selected_record_ids(self) -> List[str]:
        """Return the record ids of the selected rows."""

        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        ids: List[str] = []
        for row in rows:
            item = self.table.item(row, COLUMN_VERDICT)
            if item is None:
                continue
            value = item.data(QtCore.Qt.ItemDataRole.UserRole)
            if value:
                ids.append(str(value))
        return ids

    def selected_ids_of_kind(self, kind: str) -> List[str]:
        """Return the selected record ids carrying one kind."""

        wanted = set(self.selected_record_ids())
        return [
            record.record_id
            for record in self.records
            if record.record_id in wanted and record.kind == kind
        ]

    def focus_results(self) -> None:
        """Give the keyboard back to this page.

        This is the page level entry of the focus: the list is the
        widget the A / D navigation is meant to be used from, and both
        shortcuts fire while any child of this page holds the focus.
        A window that was closed and whose C++ side was deleted is the
        ordinary end of a window, not a failure.
        """

        try:
            self.table.setFocus()
        except RuntimeError:
            pass

    def current_record(self) -> Optional[ValidationRecord]:
        """Return the record of the current row."""

        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, COLUMN_VERDICT)
        if item is None:
            return None
        record_id = str(item.data(QtCore.Qt.ItemDataRole.UserRole) or "")
        for record in self.records:
            if record.record_id == record_id:
                return record
        return None

    # ------------------------------------------------------------- marking
    # The three batch commands below own no button of the page any more:
    # they are the slots of the context menu of the list (see
    # _show_context_menu). A single row is marked through its own
    # checkbox, a batch of rows through that menu.
    def toggle_selected_deleted(self) -> None:
        """Toggle the delete mark of the selected original records."""

        ids = self.selected_ids_of_kind(records_module.KIND_ORIGINAL)
        if not ids:
            return
        originals = [r for r in self.records if r.record_id in set(ids)]
        target = not all(r.deleted for r in originals)
        self.toggle_deleted.emit(ids, target)

    def select_all_augmented(self) -> None:
        """Select every augmented record for the export."""

        ids = [
            r.record_id
            for r in self.records
            if r.kind == records_module.KIND_AUGMENTED
        ]
        self.toggle_export.emit(ids, True)

    def clear_all_augmented(self) -> None:
        """Clear the export flag of every augmented record."""

        ids = [
            r.record_id
            for r in self.records
            if r.kind == records_module.KIND_AUGMENTED
        ]
        self.toggle_export.emit(ids, False)

    def _show_context_menu(self, position: QtCore.QPoint) -> None:
        """Offer the batch actions on the table."""

        menu = QtWidgets.QMenu(self)
        menu.addAction(
            self.tr("删除/恢复选中原图（original）"),
            self.toggle_selected_deleted,
        )
        menu.addSeparator()
        menu.addAction(
            self.tr("加入导出（选中增强图）"),
            lambda: self.toggle_export.emit(
                self.selected_ids_of_kind(records_module.KIND_AUGMENTED), True
            ),
        )
        menu.addAction(
            self.tr("取消导出（选中增强图）"),
            lambda: self.toggle_export.emit(
                self.selected_ids_of_kind(records_module.KIND_AUGMENTED), False
            ),
        )
        menu.addSeparator()
        menu.addAction(self.tr("增强图全选加入"), self.select_all_augmented)
        menu.addAction(self.tr("全部取消"), self.clear_all_augmented)
        menu.exec(self.table.viewport().mapToGlobal(position))

    # ------------------------------------------------------- parent preview
    def preview_active(self) -> bool:
        """Return True while the original of the current row is shown."""

        return bool(self._preview_active and self._preview_record)

    def displayed_record(self) -> Optional[ValidationRecord]:
        """Return the record the two canvases show.

        Without a held right button this is the record of the current
        row; while the preview is on it is the parent original instead.
        """

        if self.preview_active():
            return self._preview_record
        return self.current_record()

    def preview_note_text(self, record: ValidationRecord) -> str:
        """Return the status line written while one parent is previewed."""

        parts = [
            self.tr(PREVIEW_NOTE_PREFIX) + str(record.relpath),
            self.tr("状态") + " " + str(record.verdict),
        ]
        reasons = ", ".join(str(reason) for reason in (record.reasons or []))
        if reasons:
            parts.append(self.tr("原因") + " " + reasons)
        if str(record.verdict) in (
            records_module.PENDING,
            records_module.NOT_JUDGED,
            records_module.SKIPPED,
        ):
            parts.append(self.tr(PREVIEW_NOTE_NOT_JUDGED))
        parts.append(self.tr(PREVIEW_NOTE_SUFFIX))
        return " · ".join(parts)

    def begin_parent_preview(self) -> bool:
        """Show the original result of the current augmented record.

        The parent picture, its own ground truth, its own predictions and
        its own judgement replace the augmented copy until the button is
        released; nothing is recomputed, every byte of the preview is
        already in memory. A record without a parent - an original, or a
        copy whose original is not part of the list - keeps the current
        picture and is answered with one line of status instead, so the
        press never ends in a blank area and never raises.
        """

        record = self.current_record()
        if record is None:
            return False
        parent = parent_of(self._records_by_id, record)
        if parent is None:
            self._drop_preview()
            self._set_preview_note(
                self.tr(
                    PREVIEW_HINT_MISSING
                    if record.kind == records_module.KIND_AUGMENTED
                    else PREVIEW_HINT_ORIGINAL
                )
            )
            return False
        self._preview_record = parent
        self._preview_active = True
        self._set_preview_note(self.preview_note_text(parent))
        self._apply_canvas_titles(True)
        self._refresh_canvases_keeping_view()
        return True

    def end_parent_preview(self) -> bool:
        """Show the current record again (the right button was released).

        The call is the way out of the preview: it restores the plain
        titles and the current picture, and it is a no op while no
        preview is on screen.
        """

        active = self.preview_active()
        self._drop_preview()
        self._set_preview_note("")
        if active:
            self._refresh_canvases_keeping_view()
        return active

    def _drop_preview(self) -> None:
        """Forget the previewed parent and restore the plain titles."""

        self._preview_active = False
        self._preview_record = None
        self._apply_canvas_titles(False)

    def _apply_canvas_titles(self, preview: bool) -> None:
        """Write the title of both canvases, with or without the preview."""

        prefix = self.tr(PREVIEW_TITLE_PREFIX) + " · " if preview else ""
        for canvas, title in (
            (self.gt_canvas, GT_CANVAS_TITLE),
            (self.pred_canvas, PRED_CANVAS_TITLE),
        ):
            text = prefix + self.tr(title)
            if canvas.title != text:
                canvas.title = text
                canvas.update()

    def _set_note(self, text: str) -> None:
        """Write the one status line of the right hand side.

        The line carries the state of the preview and its row is always
        reserved: only the text changes here, so an empty line - what a
        page without a held button shows - keeps the exact geometry of a
        full one. The whole text is kept in the tooltip of a line that
        may well be elided.
        """

        value = str(text)
        self.preview_note.setText(value)
        self.preview_note.setToolTip(value)

    def _set_preview_note(self, text: str) -> None:
        """Write the one status line of the preview.

        The empty text is the idle line and keeps the reserved row (see
        _set_note).
        """

        self._set_note(text)

    def eventFilter(  # noqa: N802
        self, watched: Any, event: QtCore.QEvent
    ) -> bool:
        """Turn the right button of the two canvases into the preview.

        The press enters the preview and the release leaves it; both are
        consumed here, so a canvas never starts a pan or a context menu
        on the right button. A left press is the second way out: the
        preview ends and the canvas pans the picture it shows.
        """

        if watched in (self.gt_canvas, self.pred_canvas):
            kind = event.type()
            if kind == QtCore.QEvent.Type.MouseButtonPress:
                if event.button() == QtCore.Qt.MouseButton.RightButton:
                    self.begin_parent_preview()
                    return True
                if event.button() == QtCore.Qt.MouseButton.LeftButton:
                    self.end_parent_preview()
            elif kind == QtCore.QEvent.Type.MouseButtonRelease:
                if event.button() == QtCore.Qt.MouseButton.RightButton:
                    self.end_parent_preview()
                    return True
        return super().eventFilter(watched, event)

    def reload_record(self, record_id: str) -> bool:
        """Show one record again after its staging json was rewritten.

        A write into the annotation of a record - the label a rename in
        the main window changed - is invisible to a table that only
        rewrites its own cells: the picture of the two canvases is drawn
        from the shapes the page loaded, so the left canvas would keep
        painting the label the geometry came with until another record
        is visited. This is the one public entry point that closes that
        gap: the row of the list is rewritten in place and the two
        canvases are read from the staging file again, with the very
        cheap path a mark toggle already takes (see refresh_rows). The
        list itself is never rebuilt, so the scroll position and the
        selection stay where the user left them. The record on screen is
        announced again at the end: a refresh that really moved the page
        on to another record is a switch like any other, while a reload
        of the record already on screen is silently a no op.
        """

        record = self._records_by_id.get(str(record_id))
        if record is None:
            return False
        self.refresh_rows([record.record_id])
        self._refresh_canvases()
        current = self.current_record()
        self._note_shown_record(current.record_id if current else "")
        return True

    # -------------------------------------------------------------- viewers
    def _on_selection_changed(self) -> None:
        """Refresh the right hand side for the current row.

        A signal raised *while* the table is rebuilt is no switch
        either: refresh clears the table before it fills it again, and
        the empty selection of that moment is answered with the early
        return below (see the _loading flag), so the row the page finds
        selected once the flag is down again is the one it reports.
        """

        if self._loading:
            # a rebuild clears the table before it fills it again (see
            # refresh): the empty selection of that moment is not a
            # switch of the record, and the row the page means to show
            # is selected once the flag is down again
            return
        # another row is another record: the preview of a held right
        # button belongs to the row it was asked on and never survives
        # the move to the next one
        self._drop_preview()
        self._set_note("")
        record = self.current_record()
        record_id = record.record_id if record else ""
        self.selection_changed.emit(record_id)
        self._note_shown_record(record_id)
        self._refresh_canvases()

    def _note_shown_record(self, record_id: str) -> None:
        """Announce the record the page shows, once per change.

        Every path that can put another record on screen ends here: a
        click on a row, A / D, a filter that selects another row, the
        first fill of a run and the reload a save of the main window
        asks for. The signal is only raised when the id really moved on,
        so a selection event that reselected the row already on screen -
        a mark toggle, a save the watcher carried back - never asks the
        window for a second jump. The empty id is a switch like any
        other, but only away from a record the page really had on
        screen: replacing the list resets the announced record with
        it (see set_records), so a list that arrives empty is silent.
        """

        record_id = str(record_id or "")
        if record_id == self._shown_record_id:
            return
        self._shown_record_id = record_id
        self.current_record_changed.emit(record_id)

    def _refresh_canvases(self) -> None:
        """Repaint both canvases from the staging files.

        The record painted is the one on screen (see displayed_record):
        the current row, or the original a held right button previews.
        """

        record = self.displayed_record()
        self.gt_canvas.show_ground_truth = self.gt_check.isChecked()
        self.pred_canvas.show_predictions = self.pred_check.isChecked()
        self.gt_canvas.show_overlay = self.overlay_check.isChecked()
        self.pred_canvas.show_overlay = self.overlay_check.isChecked()
        self.gt_canvas.show_predictions = False
        self.pred_canvas.show_ground_truth = False
        show_labels = self.label_check.isChecked()
        self.gt_canvas.show_labels = show_labels
        self.pred_canvas.show_labels = show_labels
        if record is None:
            self._pixmap_path = ""
            self._pixmap = None
            self.gt_canvas.clear()
            self.pred_canvas.clear()
            return
        pixmap = self._pixmap_for(record)
        ground_truth, predictions, detail = self._load_shapes(record)
        # both canvases read the states of the very same judgement, so a
        # defect carries one colour on the left and on the right; only
        # the shapes of each side are handed over, never the record
        ground_truth_status = gt_statuses(detail, ground_truth)
        prediction_status = pred_statuses(detail, predictions)
        self.gt_canvas.set_image(pixmap)
        self.gt_canvas.set_shapes(ground_truth, [], ground_truth_status, ())
        self.pred_canvas.set_image(pixmap)
        self.pred_canvas.set_shapes([], predictions, (), prediction_status)
        # one zoom, one anchor and one pan for both pictures: the left
        # canvas is the reference of a freshly loaded record.
        self._mirror_view(self.gt_canvas, self.pred_canvas)

    def _refresh_canvases_keeping_view(self) -> None:
        """Repaint both canvases without moving the view they show.

        The preview swaps the picture under the two canvases while the
        zoom and the pan of the user stay: both states are read before
        the refresh fits the fresh pictures and written back right after,
        silently - the write back never raises view_changed, which would
        mirror a half restored state onto the other canvas. A view the
        user moved by hand stays hand made, so it is never refitted by
        the resize the restored row can trigger.
        """

        canvases = (self.gt_canvas, self.pred_canvas)
        saved = [
            (canvas.view_state(), canvas.user_adjusted())
            for canvas in canvases
        ]
        self._refresh_canvases()
        for canvas, (state, adjusted) in zip(canvases, saved):
            canvas.apply_view_state(*state)
            canvas.set_user_adjusted(adjusted)

    def _pixmap_for(self, record: ValidationRecord) -> Optional[QtGui.QPixmap]:
        """Return the decoded picture of a record, decoded only once."""

        path = record.staging_image_path
        if path != self._pixmap_path:
            self._pixmap = load_pixmap(path)
            self._pixmap_path = path
        return self._pixmap

    def _mirror_view(self, source: ImageCanvas, target: ImageCanvas) -> None:
        """Copy the view state of one canvas onto the other."""

        if self._syncing_view:
            return
        self._syncing_view = True
        try:
            scale, center_x, center_y = source.view_state()
            target.apply_view_state(scale, center_x, center_y)
            # the flag travels too: a view the user moved by hand stays
            # hand made on both sides, a fitted one keeps refitting
            target.set_user_adjusted(source.user_adjusted())
        finally:
            self._syncing_view = False

    def _load_shapes(self, record: ValidationRecord):
        """Read the staging label, the cached predictions and the detail.

        The judgement detail travels with the two shape lists because
        the colours of the boxes are read from it. It is only ever read:
        the states the canvases get are built as new lists.
        """

        label = records_module.read_staging_label(record)
        ground_truth = list((label or {}).get("shapes") or [])
        predictions = list(record.detail.get("predictions") or [])
        return ground_truth, predictions, dict(record.detail or {})

    def set_summary(self, text: str) -> None:
        """Update the export summary line."""

        self.status_label.setText(text)


__all__ = [
    "AUGMENT_FIELD_LABELS",
    "AUGMENT_ITEM_SEPARATOR",
    "AUGMENT_SELECTION_NONE",
    "COLUMN_AUGMENT",
    "COLUMN_AUGMENT_TOOLTIP",
    "COLUMN_EXPORT",
    "LEGEND_HTML",
    "LEGEND_TOOLTIP",
    "COLUMN_KIND",
    "COLUMN_MARK",
    "COLUMN_MARK_TOOLTIP",
    "COLUMN_RELPATH",
    "COLUMN_VERDICT",
    "DEFAULT_FILTER",
    "FILTER_EDITED",
    "GREY_TEXT_COLOR",
    "GT_CANVAS_TITLE",
    "HEADERS",
    "KIND_SORT_ORDER",
    "MARK_TOOLTIP_AUGMENTED",
    "MARK_TOOLTIP_ORIGINAL",
    "PRED_CANVAS_TITLE",
    "PRED_STATE_PRIORITY",
    "PREVIEW_HINT_MISSING",
    "PREVIEW_HINT_ORIGINAL",
    "PREVIEW_NOTE_NOT_JUDGED",
    "PREVIEW_NOTE_PREFIX",
    "PREVIEW_NOTE_SUFFIX",
    "PREVIEW_TITLE_PREFIX",
    "PREVIEW_TOOLTIP",
    "PreviewNoteLabel",
    "RecordItem",
    "RecordTree",
    "ResultsPage",
    "SHORTCUT_HINT",
    "SHORTCUT_TOOLTIP",
    "augment_detail_tooltip",
    "augment_field_label",
    "augment_selection_text",
    "edited_records",
    "gt_statuses",
    "legend_html",
    "marked_record",
    "marked_records",
    "matched_pair_state",
    "model_note_text",
    "parent_of",
    "pred_statuses",
    "record_modified",
    "record_sort_key",
    "sort_chunk_key",
]
