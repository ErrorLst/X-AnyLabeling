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
memory and never runs inference again.

The middle button on either canvas switches the *ROI edit mode* on and
off, and only the left canvas is edited: the ground truth of the current
record is the annotation, the prediction of the right canvas is the
output of the model and never a thing to correct. In the edit mode the
left button selects and moves a box, a handle of the selection resizes
it, a double click inside a box opens the label chooser of the class
table (a choice, never a free text) and a double click on empty space
keeps fitting the picture. The drag of the canvas is stored by this page
- records.update_shape_points writes the staging json - and the row of
the record is rewritten in place, so the list never loses its scroll
position over an edit. The verdict of the record is deliberately left
alone: a corrected box does not re-run the matching, the reason of the
run is what the report says.

The mode belongs to the record it was armed on. Moving the selection to
another row is what leaves it (see _on_selection_changed): the gestures
of the mode would otherwise travel to the next picture without a word,
while a repaint of the *same* record - a finished drag, a renamed
label, a display switch - keeps the mode exactly where the user left
it.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from PyQt6 import QtCore, QtGui, QtWidgets

from .. import dataset
from .. import records as records_module
from ..records import ValidationRecord
from .. import judge as judge_module
from ..labelme_io import REGION_SHAPE_TYPES
from . import image_view as image_view_module
from . import label_dialog
from .image_view import (
    CLASS_MISMATCH_COLOR,
    FALSE_POSITIVE_COLOR,
    GT_COLOR,
    IOU_BELOW_COLOR,
    MISS_COLOR,
    PRED_COLOR,
    STATE_CLASS_MISMATCH,
    STATE_FALSE_POSITIVE,
    STATE_IOU_BELOW,
    STATE_MISS,
    STATE_OK_PAIR,
    ImageCanvas,
    load_pixmap,
)

COLUMN_MARK = 0
COLUMN_VERDICT = 1
COLUMN_RELPATH = 2
COLUMN_KIND = 3

# the mark column was called the export column before both kinds grew
# their own checkbox: the alias keeps existing callers working.
COLUMN_EXPORT = COLUMN_MARK

HEADERS = ("标记", "状态", "relpath", "kind")

MARK_TOOLTIP_ORIGINAL = (
    "删除标记：勾选 = 该原图（连同它的 json 与全部增强子代）从导出中排除；"
    "默认全部不勾选。"
)
MARK_TOOLTIP_AUGMENTED = "选择标记：勾选 = 该增强图加入导出；默认全部不勾选。"
COLUMN_MARK_TOOLTIP = (
    "标记列即是导出列表：原图勾选 = 删除标记（移出导出），增强图勾选 = 选择标记"
    "（加入导出）。两者互相独立，取消勾选即恢复原状。"
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
# two hands never leave the mouse row.
SHORTCUT_HINT = (
    "快捷键：A 上一张 / D 下一张（到首尾停住，选中行自动滚动到可见）。"
)
SHORTCUT_TOOLTIP = (
    "结果页快捷键：A = 上一张，D = 下一张；焦点在本页（含列表内）时生效，"
    "到首尾即停，不循环。"
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
    "<span style='color:{iou}'>IoU 低</span>"
    "<br/>两侧同源判定：左侧按 GT 状态、右侧按 Pred 状态着色；"
    "无判定的记录不改色。"
)
LEGEND_TOOLTIP = (
    "框色即判定：匹配对保持 GT 绿 / Pred 蓝；漏报（GT 未匹配到预测）橙红；"
    "误报（预测未匹配到 GT）品红；类别不一致橙；IoU 低于阈值黄。"
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
# The tooltip of the canvases stays the one of the preview, word for word:
# the existing delivery pins that string. The edit gesture is documented
# by the hint line of the edit mode instead (see EDIT_HINT_TOOLTIP).
PREVIEW_NOTE_NOT_JUDGED = "（原图未判定，框保持本色）"

# The one line of the ROI edit mode. It is hidden while the mode is off,
# so the page keeps the compact layout of the previous revision until the
# user really asks for the editing gestures.
EDIT_HINT = (
    "ROI 编辑模式：中键再次退出。左键点框选中并拖动整体移动；"
    "拖动选中框的 8 个手柄修改尺寸；框内双击重命名标签"
    "（标签只能从分类表中选择）；空白处双击仍是整图适配。"
)
EDIT_HINT_TOOLTIP = (
    "进入/退出：在任意一幅画布上按鼠标中键；只有左幅 GT 是可编辑的标注，"
    "右幅 Pred 只读。编辑只写暂存副本，源数据集不会被修改；判定结果不重算。"
)
EDIT_HINT_NO_REGION = "当前记录没有可编辑的区域标注（rectangle/rotation/quadrilateral/polygon）。"
EDIT_HINT_NO_PREVIEW = "编辑模式下右键预览已关闭：先中键退出编辑模式。"
EDIT_HINT_NO_CLASSES = "分类表为空：本次运行没有可选的标签，无法重命名。"
EDIT_TITLE_SUFFIX = image_view_module.EDIT_MODE_TITLE_SUFFIX


def legend_html() -> str:
    """Return the rich text of the colour legend of the two canvases."""

    return LEGEND_HTML.format(
        gt=GT_COLOR.name(),
        miss=MISS_COLOR.name(),
        fp=FALSE_POSITIVE_COLOR.name(),
        cls=CLASS_MISMATCH_COLOR.name(),
        iou=IOU_BELOW_COLOR.name(),
    )


def matched_pair_state(iou: Any, class_state: Any, threshold: Any) -> str:
    """Return the state of one matched pair of a judgement detail.

    The pair of the judge detail carries its class outcome and its IoU;
    the two states a reader has to tell apart are the pair of two
    different classes and the pair that stayed under the threshold of the
    run. The pair of two classes that are not even known is reported as
    an unknown label instead of a class mismatch (class_state is
    "unknown_class"), so it keeps the plain colour of its canvas, exactly
    like a pair that is fine. Only the mismatch of the judge is read as
    one: a detail without the key, or with a spelling of a later
    revision, is not a class error of this record.
    """

    if str(class_state or "") == "mismatch":
        return STATE_CLASS_MISMATCH
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
    without a judgement - keeps an empty state and the default colour.
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
        )
    matched_indexes = {_count(pair.get("gt_index")) for pair in matched}
    for position, offset in enumerate(offsets):
        if position not in matched_indexes:
            statuses[offset] = STATE_MISS
    return statuses


def pred_statuses(detail: Dict[str, Any], shapes: Sequence[Any]) -> List[Any]:
    """Return the judgement state of every prediction of one record.

    A matchable prediction no match points at is a false positive. A
    prediction of an ignored shape type is never matched at all and
    keeps the plain Pred colour, and so does every prediction of a
    record without a judgement.
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
        )
    matched_indexes = {_count(pair.get("pred_index")) for pair in matched}
    for position, offset in enumerate(offsets):
        if position not in matched_indexes:
            statuses[offset] = STATE_FALSE_POSITIVE
    return statuses


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


SHAPE_TYPE_CHOICES = (
    "rectangle",
    "polygon",
    "rotation",
    "quadrilateral",
    "point",
    "line",
    "linestrip",
    "circle",
    "cuboid",
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
    record can leave its own run: the *edit* flags a correction of the
    annotation writes (records.update_shape / records.update_shape_points
    set records.edited) and the two marks of the list - the delete mark
    of an original and the export mark of an augmented copy (see
    marked_record). The filter reads those flags themselves and never
    keeps a list of its own.
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
    """Read only, one record per row, four columns.

    The export list was a QTableWidget, whose cells are editable by
    default: a stray double click or F2 let the user retype a status or a
    relpath that the run had just produced. This widget is the read only
    replacement. It is a tree with the decoration switched off and every
    edit trigger refused, so a row can only be selected and the checkbox
    it carries can only be toggled.

    The page reads a row as (mark, verdict, relpath, kind), the very
    shape the table exposed: item(row, column) returns the cell text
    item, cellWidget the checkbox of the mark column and rowCount the
    amount of shown rows.
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
        for column in (COLUMN_MARK, COLUMN_VERDICT, COLUMN_KIND):
            header.setSectionResizeMode(
                column, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
            )
        mark_header = self.headerItem()
        if mark_header is not None:
            mark_header.setToolTip(COLUMN_MARK, COLUMN_MARK_TOOLTIP)

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
        """Return the four cell indexes of every selected row."""

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
    # kept as the documented hook of the record layer (records.update_shape
    # is still there); the read only view of this revision emits nothing.
    edit_requested = QtCore.pyqtSignal(str, int, object, object)
    selection_changed = QtCore.pyqtSignal(str)
    # The signal of the ROI edit mode: the canvas publishes the point
    # set a drag produced and the dialog of this page is what stores it
    # (records.update_shape_points), so the page itself keeps owning no
    # file. The rename of a box needs no signal of its own: the page
    # asks for the new label itself and requests it on edit_requested,
    # the very seam the label edit of the previous revision used.
    shape_moved = QtCore.pyqtSignal(str, int, object)

    def __init__(self, parent: Optional[Any] = None) -> None:
        super().__init__(parent)
        self.records: List[ValidationRecord] = []
        self.classes: List[str] = []
        self.model_info: Dict[str, Any] = {}
        self._loading = False
        self._syncing_view = False
        # the rows the table currently shows: row -> record id for the
        # point update of a mark, record id -> row to find them back
        self._row_ids: List[str] = []
        self._rows: Dict[str, int] = {}
        self._records_by_id: Dict[str, ValidationRecord] = {}
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
        # the ROI edit mode: off out of the box, and it only ever holds
        # the ground truth of the record on screen (the predictions of
        # the right canvas are the output of the model, never a thing to
        # correct); _edit_record_id is the record the editable shapes
        # were handed over for, so the selection survives a repaint of
        # the same record and is dropped when another one arrives
        self.edit_mode = False
        self._edit_record_id = ""
        self._edit_selection = -1
        # the record the two canvases were last answered the selection
        # signal for: the edit mode is dropped on a *change* of it, not
        # on the signal (see _on_selection_changed)
        self._shown_record_id = ""
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
        self.filter_combo.currentIndexChanged.connect(self.refresh)
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
        # state into: hidden while the current record is the one on
        # screen, so it never takes room from a plain look at a record
        self.preview_note = QtWidgets.QLabel("")
        self.preview_note.setWordWrap(True)
        self.preview_note.setVisible(False)
        right_layout.addWidget(self.preview_note)

        # the one line of the edit mode, written where the gestures
        # happen and hidden while the mode is off
        self.edit_hint = QtWidgets.QLabel("")
        self.edit_hint.setWordWrap(True)
        self.edit_hint.setToolTip(self.tr(EDIT_HINT_TOOLTIP))
        self.edit_hint.setVisible(False)
        right_layout.addWidget(self.edit_hint)

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
        # the edit mode always belongs to the left canvas, whatever
        # canvas the user asked on: the right one only mirrors the mode
        # for its title and forwards every gesture to the left one
        self.gt_canvas.edit_mode_toggled.connect(self._sync_edit_mode)
        self.pred_canvas.edit_mode_toggled.connect(self._sync_edit_mode)
        self.gt_canvas.shape_selected.connect(self._on_shape_selected)
        self.pred_canvas.shape_selected.connect(lambda *args: None)
        for canvas in (self.gt_canvas, self.pred_canvas):
            canvas.shape_move_finished.connect(self.on_shape_moved)
            canvas.shape_rename_requested.connect(self.on_shape_rename)
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
        """Install the A / D navigation of the record list.

        The two shortcuts belong to this page *and to its children*, so
        they fire while the list (or any other control of the page) holds
        the focus and they never fire while the user works in another
        window. A single letter is no shortcut of the tree itself, so the
        list never swallows the key.
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
        """Replace the displayed records."""

        self.records = list(records)
        self._records_by_id = records_module.record_lookup(self.records)
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

    def refresh(self) -> None:
        """Rebuild the table from the record list.

        A rebuild is what a filter change or a new record list needs:
        every row is created again, in file name order, and the selection
        is restored by record id - the first row when the record that was
        shown is not part of the list any more. A mark toggle never comes
        here: it refreshes its own rows alone (see refresh_rows), so the
        scroll position and the selection of a long list survive a click.
        """

        current = self.current_record()
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
        of its own and the three text cells are plain cells of the tree,
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

        texts = (verdict_text, relpath_text, record.kind)
        columns = (COLUMN_VERDICT, COLUMN_RELPATH, COLUMN_KIND)
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
        self._refresh_canvases()
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
            self._refresh_canvases()
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
            # the edit mode is a state of the left canvas alone, so its
            # suffix is written there and nowhere else: the right canvas
            # keeps the title of the record it shows, whose predictions
            # are never edited
            if self.edit_mode and canvas is self.gt_canvas:
                text += self.tr(EDIT_TITLE_SUFFIX)
            if canvas.title != text:
                canvas.title = text
                canvas.update()

    def _set_note(self, text: str) -> None:
        """Write the one status line of the right hand side.

        The line carries the state of the preview and the refusal of the
        edit mode; an empty text hides it, which is what a page without
        a held button and without an edit mode shows.
        """

        self.preview_note.setText(str(text))
        self.preview_note.setVisible(bool(text))

    def _set_preview_note(self, text: str) -> None:
        """Write the one status line of the preview (empty hides it)."""

        self._set_note(text)

    def eventFilter(  # noqa: N802
        self, watched: Any, event: QtCore.QEvent
    ) -> bool:
        """Turn the right button of the two canvases into the preview.

        The press enters the preview and the release leaves it; both are
        consumed here, so a canvas never starts a pan or a context menu
        on the right button. A left press is the second way out: the
        preview ends and the canvas pans the picture it shows.

        The middle button of the edit mode is filtered here as well, and
        its rule is deliberately one sided: *entering* the mode needs the
        pointer to sit on the picture, while *leaving* it works wherever
        the pointer is - the black band around a fitted image included.
        Arming the gesture on the annotation from the letterbox would be
        a click that selects nothing, but a way out that a user cannot
        reach - a picture zoomed past the widget, a pointer that happens
        to rest on the border - would lock the mode in place. The tests
        pin both directions (test_mv_edit_mode).
        """

        if watched in (self.gt_canvas, self.pred_canvas):
            kind = event.type()
            if kind == QtCore.QEvent.Type.MouseButtonPress:
                if event.button() == QtCore.Qt.MouseButton.MiddleButton:
                    # The middle button is the switch of the edit mode,
                    # on either canvas and whether the mode is on or
                    # off: it always acts on the *left* canvas, because
                    # the ground truth is the annotation and the
                    # prediction of the right one is the output of the
                    # model. A press in the letterbox around the picture
                    # starts nothing: it is swallowed here so the
                    # canvases below never see a half gesture either.
                    if self.edit_mode or self._canvas_has_picture(
                        watched, QtCore.QPointF(event.position())
                    ):
                        self.toggle_edit_mode()
                    return True
                if event.button() == QtCore.Qt.MouseButton.RightButton:
                    # the edit mode refuses the preview: a held right
                    # button would replace the very picture the user is
                    # dragging, so the press is answered with one line
                    # of status instead
                    if self.edit_mode:
                        self._set_note(self.tr(EDIT_HINT_NO_PREVIEW))
                        return True
                    self.begin_parent_preview()
                    return True
                if event.button() == QtCore.Qt.MouseButton.LeftButton:
                    self.end_parent_preview()
            elif kind == QtCore.QEvent.Type.MouseButtonRelease:
                if event.button() == QtCore.Qt.MouseButton.RightButton:
                    self.end_parent_preview()
                    return True
        return super().eventFilter(watched, event)

    # ------------------------------------------------------------- edit ROI
    def _canvas_has_picture(
        self, canvas: ImageCanvas, position: QtCore.QPointF
    ) -> bool:
        """Return True when a position sits on the picture of a canvas.

        The edit mode is only ever asked for on the picture itself: a
        middle click in the letterbox around a fitted image is no
        request to edit anything and must not arm the gesture either.
        """

        size = canvas.image_size()
        if size.width() <= 0 or size.height() <= 0:
            return False
        return bool(canvas._target_rect().contains(position))

    def editable_regions(self) -> List[Any]:
        """Return the region shapes of the current record, editable ones.

        The interface of the *original* annotation is the only thing the
        edit mode ever hands over: the ground truth of the record on
        screen, filtered down to the region shape types of
        labelme_io.REGION_SHAPE_TYPES. A point, a line, a circle or a
        cuboid is a shape this tool does not resize and stays read only.
        """

        record = self.current_record()
        if record is None:
            return []
        ground_truth, _predictions, _detail = self._load_shapes(record)
        return [
            shape
            for shape in ground_truth
            if str(shape.get("shape_type") or "") in REGION_SHAPE_TYPES
        ]

    def leave_edit_mode(self) -> bool:
        """Drop the ROI edit mode of the record that is leaving the screen.

        Called wherever the page stops showing one record and starts
        showing another (see _on_selection_changed) and by the way out
        of the mode itself. The mode, the selection of the left canvas,
        the title suffix, the hint line and the shapes the canvas was
        allowed to edit all go back to the state of a plain viewer in
        one step - so the gestures of the mode can never travel to a
        picture the user has not armed them on. An already left mode is
        answered with False and costs nothing.
        """

        if not self.edit_mode:
            return False
        self.edit_mode = False
        self._edit_record_id = ""
        self._edit_selection = -1
        self.gt_canvas.clear_selection()
        self.gt_canvas.set_edit_mode(False)
        self.pred_canvas.set_edit_mode(False)
        self._sync_edit_mode(False)
        # the canvas keeps the objects it was handed over until the next
        # repaint hands it another set (see ImageCanvas.set_editable_shapes),
        # so the mode drops the set itself as well: an inactive canvas
        # holding a shape the user may not touch any more would make the
        # state of the page read as two answers to one question
        self.gt_canvas.set_editable_shapes("", [], [])
        return True

    def toggle_edit_mode(self) -> bool:
        """Enter or leave the ROI edit mode (the middle button).

        Entering the mode leaves the parent preview first: the preview
        replaces the picture on screen by another record, while an edit
        always belongs to the row the user selected. A record without a
        single editable region shape answers with one line of status
        instead of arming gestures that could not do anything.
        """

        # the way out of the mode always works: only *entering* it is
        # refused when the record has nothing to edit, so a record that
        # turns empty under the user never traps the gestures
        if not self.edit_mode and not self.editable_regions():
            self._set_note(self.tr(EDIT_HINT_NO_REGION))
            return False
        self._drop_preview()
        self._set_note("")
        if self.edit_mode:
            self.leave_edit_mode()
            self._refresh_canvases()
            return True
        self.edit_mode = True
        self.gt_canvas.set_edit_mode(True)
        self.pred_canvas.set_edit_mode(False)
        self._sync_edit_mode(True)
        self._refresh_canvases()
        return True

    def _sync_edit_mode(self, enabled: bool) -> None:
        """Write the edit state into the titles and the hint line.

        The mode is one state of the whole page, so both canvases are
        told about it: the right one mirrors it (a middle click there
        ends the mode as well) while only the left one ever holds an
        editable shape.
        """

        self.edit_mode = bool(enabled)
        self.pred_canvas.set_edit_mode(False)
        self._apply_canvas_titles(self.preview_active())
        self.edit_hint.setText(self.tr(EDIT_HINT) if self.edit_mode else "")
        self.edit_hint.setVisible(self.edit_mode)

    def _on_shape_selected(self, record_id: str, index: int) -> None:
        """Remember which shape of which record the user picked."""

        self._edit_selection = int(index)

    def on_shape_moved(self, record_id: str, index: int, points: Any) -> None:
        """Forward a finished drag to the dialog that stores it.

        The page owns no file: the assignment itself lives in the owner
        of the records (see ModelValidationDialog.on_shape_moved), the
        very split the label edit of the previous revision already used.
        """

        self.shape_moved.emit(str(record_id), int(index), points)

    def on_shape_rename(self, record_id: str, index: int) -> None:
        """Ask for a new label of one shape and request the rename.

        The label is picked out of the class table of the run and the
        request is only emitted when the user really chose another
        name: a cancel, an empty class table and the label the shape
        already carries all leave the staging json untouched.
        """

        shape = self.shape_of(str(record_id), int(index))
        if shape is None:
            return
        current = image_view_module.shape_label(shape)
        if not self.classes:
            self._set_note(self.tr(EDIT_HINT_NO_CLASSES))
            return
        chosen = label_dialog.choose_label(self, self.classes, current)
        if chosen is None or str(chosen) == current:
            return
        self.edit_requested.emit(str(record_id), int(index), str(chosen), None)

    def shape_of(self, record_id: str, index: int) -> Optional[Dict[str, Any]]:
        """Return one shape of a record of this run, None when there is none."""

        record = self._records_by_id.get(str(record_id))
        if record is None:
            return None
        label = records_module.read_staging_label(record)
        shapes = list((label or {}).get("shapes") or [])
        position = int(index)
        if position < 0 or position >= len(shapes):
            return None
        shape = shapes[position]
        return shape if isinstance(shape, dict) else None

    def apply_edit_result(
        self, record_id: str, index: int, points: Any
    ) -> bool:
        """Store one moved point set and rewrite the row in place.

        The cheap path of an edit: the staging json of the record is
        written (records.update_shape_points), the one row of the list
        is rewritten - so the "(edited)" note appears without the list
        rebuilding itself and without losing its scroll position - and
        the two canvases are repainted from the new points. The verdict
        of the record is deliberately left alone: correcting a box does
        not re-run the matching of the run.
        """

        record = self._records_by_id.get(str(record_id))
        if record is None:
            return False
        if not records_module.update_shape_points(record, int(index), points):
            return False
        self.refresh_rows([record.record_id])
        self._refresh_canvases()
        return True

    def reload_record(self, record_id: str) -> bool:
        """Show one record again after its staging json was rewritten.

        A write into the annotation of a record - the label of a rename
        (see dialog.on_edit_shape) - is invisible to a table that only
        rewrites its own cells: the picture of the two canvases is drawn
        from the shapes the page loaded, so the left canvas would keep
        painting the label the geometry came with until another record
        is visited. This is the one public entry point that closes that
        gap: the row of the list is rewritten in place and the two
        canvases are read from the staging file again, with the very
        cheap path a finished point drag already takes (see
        apply_edit_result). The list itself is never rebuilt, so the
        scroll position and the selection stay where the user left them.
        """

        record = self._records_by_id.get(str(record_id))
        if record is None:
            return False
        self.refresh_rows([record.record_id])
        self._refresh_canvases()
        return True

    def _editable_shapes_for(self, record: ValidationRecord, shapes=None):
        """Return the (shapes, flags) the left canvas may edit of a record.

        The shapes are the very objects the canvas paints - the ground
        truth of the staging label - and a flag says which of them the
        edit mode accepts (see REGION_SHAPE_TYPES). A flag of False is
        not a refusal to draw: the shape is painted grey and dashed and
        is simply never picked.

        The caller may hand the ground truth it already read over: the
        repaint of the canvases has it in hand, and reading the staging
        json twice per repaint would be wasted work on a long list.
        """

        if shapes is None:
            shapes, _predictions, _detail = self._load_shapes(record)
        ground_truth = list(shapes)
        flags = [
            str(shape.get("shape_type") or "") in REGION_SHAPE_TYPES
            for shape in ground_truth
        ]
        return ground_truth, flags

    # -------------------------------------------------------------- viewers
    def _on_selection_changed(self) -> None:
        """Refresh the right hand side for the current row.

        The record id the selection signal was answered for is written
        down here, and the edit mode is dropped *only* while that id
        really changed. The signal itself is the wrong question: the
        page rewrites the rows of one record in place after a drag, a
        rename or a display switch, and a rebuild of the list answers
        the very same signal with the very same record - leaving the
        mode there would throw the user out of the gestures after every
        finished drag.

        A signal raised *while* the table is rebuilt is no switch
        either: refresh clears the table before it fills it again, and
        the empty selection of that moment is answered with the early
        return below (see the _loading flag).
        """

        if self._loading:
            # a rebuild clears the table before it fills it again (see
            # refresh): the empty selection of that moment is not a
            # switch of the record, and the row the page means to show
            # is selected once the flag is down again. Dropping the
            # mode here would throw the user out of the gestures for
            # nothing - a filter that keeps the row keeps the mode.
            return
        # another row is another record: the preview of a held right
        # button belongs to the row it was asked on and never survives
        # the move to the next one
        self._drop_preview()
        self._set_note("")
        record = self.current_record()
        record_id = record.record_id if record else ""
        if record_id != self._shown_record_id:
            self._shown_record_id = record_id
            self.leave_edit_mode()
        self.selection_changed.emit(record_id)
        self._refresh_canvases()

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
            # an emptied page has no record left to edit: the two
            # canvases are told so as well, or a shape of the record
            # that just left would keep its handles on a blank picture
            self.gt_canvas.set_editable_shapes("", [], [])
            self.pred_canvas.set_editable_shapes("", [], [])
            self._edit_record_id = ""
            self._edit_selection = -1
            return
        pixmap = self._pixmap_for(record)
        ground_truth, predictions, detail = self._load_shapes(record)
        # The right canvas never edits anything: the predictions are the
        # output of the model and stay read only whatever the mode says.
        self.pred_canvas.set_editable_shapes("", [], [])
        if self.edit_mode:
            current = self.current_record()
            shown = current if current is not None else record
            # The picture on screen may be another record than the row
            # the user selected: the preview of a held right button
            # shows the parent of that row. The edit overlay only ever
            # decorates the shapes the canvases draw (see
            # ImageCanvas.set_editable_shapes), so a previewed original
            # hands over no shape at all - no box of a record that is
            # not the selected one can be picked, moved or resized
            # while the original it came from is on screen.
            selected = str(current.record_id) if current is not None else ""
            if selected and str(record.record_id) == selected:
                edit_shapes, editable_flags = self._editable_shapes_for(
                    shown, ground_truth
                )
            else:
                edit_shapes, editable_flags = [], []
                self._edit_selection = -1
                self.gt_canvas.select_shape(-1)
            if str(shown.record_id) != self._edit_record_id:
                self._edit_record_id = str(shown.record_id)
                self.gt_canvas.select_shape(-1)
            self.gt_canvas.set_editable_shapes(
                str(shown.record_id), edit_shapes, editable_flags
            )
        else:
            self.gt_canvas.set_editable_shapes("", [], [])
            self._edit_record_id = ""
            self._edit_selection = -1
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
    "COLUMN_EXPORT",
    "LEGEND_HTML",
    "LEGEND_TOOLTIP",
    "COLUMN_KIND",
    "COLUMN_MARK",
    "COLUMN_MARK_TOOLTIP",
    "COLUMN_RELPATH",
    "COLUMN_VERDICT",
    "DEFAULT_FILTER",
    "EDIT_HINT",
    "EDIT_HINT_NO_CLASSES",
    "EDIT_HINT_NO_PREVIEW",
    "EDIT_HINT_NO_REGION",
    "EDIT_HINT_TOOLTIP",
    "EDIT_TITLE_SUFFIX",
    "FILTER_EDITED",
    "GREY_TEXT_COLOR",
    "GT_CANVAS_TITLE",
    "HEADERS",
    "KIND_SORT_ORDER",
    "MARK_TOOLTIP_AUGMENTED",
    "MARK_TOOLTIP_ORIGINAL",
    "PRED_CANVAS_TITLE",
    "PREVIEW_HINT_MISSING",
    "PREVIEW_HINT_ORIGINAL",
    "PREVIEW_NOTE_NOT_JUDGED",
    "PREVIEW_NOTE_PREFIX",
    "PREVIEW_NOTE_SUFFIX",
    "PREVIEW_TITLE_PREFIX",
    "PREVIEW_TOOLTIP",
    "RecordItem",
    "RecordTree",
    "ResultsPage",
    "SHAPE_TYPE_CHOICES",
    "SHORTCUT_HINT",
    "SHORTCUT_TOOLTIP",
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
