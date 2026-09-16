"""Configuration page of the model validation sub window."""

from __future__ import annotations

import os.path as osp
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PyQt6 import QtCore, QtGui, QtWidgets

from ..app_config import (
    AUGMENT_WORKERS_LIMIT,
    INFERENCE_CONF_THRESHOLD,
    RATIO_MODE,
    AugmentParams,
    ValidationConfig,
    ValidationConfigError,
    default_workers,
    load_classes_file,
)


def default_text(value: Any) -> str:
    """Return a default value the way the tooltips spell it out."""

    return "{0:g}".format(float(value))


def augment_defaults() -> AugmentParams:
    """Return the augmentation defaults every control starts from.

    The page never repeats a default literal: the spin boxes and the
    sentences explaining them read the value from the dataclass the run
    consumes, so a changed default reaches the whole page - the control
    and its tooltip - without a second place to edit.
    """

    return AugmentParams()


def config_defaults() -> ValidationConfig:
    """Return the validation defaults every control starts from."""

    return ValidationConfig()


# The controls of the augmentation grid share one compact width so that
# the nine single image parameters of the grid fit into three columns.
SPIN_WIDTH = 96

# What the window accepts when the user drags files onto it: a folder is
# the source dataset, a .txt is the classes table and a .onnx model is the
# detector. Everything else is ignored with a note on the status line.
DRAG_SUPPORTED_HINT = "可拖拽 目录 / classes.txt / .onnx 到本窗口"
DRAG_TOOLTIP_SUFFIX = "也可直接把对应文件/目录拖到窗口里。"

TOOLTIP_DATASET = (
    "只读。仅用于把该目录下『图片 + 同名 .json』成对的有效数据拷贝到系统临时"
    "目录；验证全程只操作副本，绝不修改原始数据集。" + DRAG_TOOLTIP_SUFFIX
)
# classes.txt is the authority: the embedded names of the ONNX are a
# hint only, therefore the tooltip explains what the user must keep in
# sync (the amount of classes) and what he may ignore (the names).
TOOLTIP_CLASSES = (
    "每行一个类别名，行号即类别 id（与 X-AnyLabeling 导出 YOLO "
    "hbb/obb/seg 时选择的 classes.txt 完全同格式）。类名以本文件为准："
    "行数（类别数量）必须与 ONNX 内嵌 names 的数量一致，否则会阻断；"
    "名字不同不阻断，模型内嵌 names 多为训练脚本写下的占位名"
    "（如 class_0、class_1）。" + DRAG_TOOLTIP_SUFFIX
)
TOOLTIP_MODEL = (
    "仅支持 Ultralytics 导出的 .onnx 检测类模型，且必须自带元数据 "
    "task/imgsz/names；缺失会直接阻断。.pt 请先用 yolo export "
    "format=onnx 导出。" + DRAG_TOOLTIP_SUFFIX
)
TOOLTIP_BROWSE = "点击浏览并选择本地文件或目录；已选路径只读，不会写回原数据。"

TOOLTIP_THRESHOLDS_ROW = (
    "本组三个阈值：NG 分数 score、NMS IoU iou、判定 NG IoU——取值范围与"
    f"逐项含义见各控件自身的悬停说明。推理过滤阈值固定 {INFERENCE_CONF_THRESHOLD}（不在此组）。"
)
TOOLTIP_CONF = (
    "NG 分数判定阈值：任一预测框得分低于该值即记 LOW_SCORE 判 NG。推理期的"
    f"置信度过滤已固定为 {INFERENCE_CONF_THRESHOLD}、不受本值影响；本值设为 ≤"
    f"{INFERENCE_CONF_THRESHOLD} 时不会有任何框低于它，LOW_SCORE 永不触发。范围 "
    "0.00-1.00，默认 "
    f"{default_text(config_defaults().conf_threshold)}。"
)
TOOLTIP_IOU = (
    "非极大值抑制的重叠阈值，同时是『整张图片一起合并』的阈值（仅 detect / "
    "segment 任务）：推理后把整张图的所有预测框放在一起、不分类别地比较，两个框"
    "IoU 超过它就合并成一个框，标签按类多行显示；每类再各自参与判定，因此这类"
    "框的框色取它各行状态的最高者。范围 0.00-1.00（建议 ≥0.01），默认 "
    f"{default_text(config_defaults().iou_threshold)}。调低会更激进地合并"
    "重叠框。"
)
TOOLTIP_NG_IOU = (
    "判定 NG 用的匹配阈值：原图标注框与预测框匹配后，若 IoU 低于该值就记 "
    "IOU_BELOW 判为 NG。范围 0.00-1.00（建议 ≥0.01），默认 "
    f"{default_text(config_defaults().ng_iou_threshold)}。调高判定更严格。"
)
TOOLTIP_JUDGE_AUGMENTED = (
    "勾选（默认）：增强图与原图副本一起推理并参与 OK/NG 判定；不勾选：仅生成"
    "增强图供导出，不做判定（状态记为 NOT_JUDGED）。"
)
TOOLTIP_AUGMENT_ENABLED = (
    "增强默认关闭：勾选后才会按下面的固定比例生成增强样本（只写入系统临时"
    "目录）；不勾选则只验证原始数据。"
)
TOOLTIP_MODE_FIXED = (
    "数量模式固定为比例 r：先按比例从有效原图中随机挑选 "
    "round(有效原图数 x r) 张（无放回、每张最多 1 份），再逐张生成 1 个增强"
    "副本。倍数 k 与总数 N 两种模式已取消（历史报告里可能仍记录它们）。"
)
TOOLTIP_RATIO = (
    "只控制总量：按比例从有效原图随机挑选 round(有效原图数 x r) 张（无放回、"
    "每张最多 1 份），被选中的原图各生成 1 张增强副本，未被选中的不生成。"
    "范围 0-1，默认 "
    f"{default_text(config_defaults().ratio)}；r=1 表示全部原图各 1 份，"
    "r=0 表示不生成任何副本。"
)
TOOLTIP_SEED = (
    "随机种子。相同种子 + 相同参数可完全复现同一批增强图；report 中会记录实际"
    "使用的种子。"
)
# The concurrency of both stages is fixed and the page only displays
# it: the read only note line shows the value a run derives from this
# machine, the tooltip spells out the formula behind that value.
WORKERS_NOTE_TEMPLATE = "{name}并发：{workers}（固定为逻辑处理器数量的一半）"


def workers_note(name: str, workers: int) -> str:
    """Return the read only note line of one fixed concurrency."""

    return WORKERS_NOTE_TEMPLATE.format(name=name, workers=int(workers))


TOOLTIP_AUGMENT_WORKERS = (
    "增强并发数不可配置：固定为逻辑处理器数量的一半，即 "
    f"max(1, os.cpu_count() // 2)，本机为 {default_workers()}（换一台机器会随"
    "核数自动变化）。并发度只影响耗时，不改变任何一张增强图与标签文件的内容；"
    "并行时每个线程还会把 OpenCV 自身的线程池限制为 1 线程，避免与固定并发叠加"
    "造成超订。"
)
TOOLTIP_INFER_WORKERS = (
    "推理并发数同样不可配置：与增强共用同一个固定值（逻辑处理器数量的一半），"
    f"本机为 {default_workers()} 个独立 ONNX 会话（每个会话一条线程）。每会话线程数"
    "按 max(1, 逻辑处理器数 // 会话数) 平分；会话数为 1 时沿用运行时默认线程数。"
    "导出模型 batch 固定为 1，只能靠多会话并行提速；会话数只影响耗时，判定结论"
    "（OK/NG 与原因）不变。"
)
TOOLTIP_CONTRAST = (
    "对比度抖动幅度：每个副本在 [1-c, 1+c] 内随机取一个乘性增益作用于整幅像素"
    "（对应 albumentations RandomBrightnessContrast 的 contrast_limit，以黑为轴："
    "增益<1 会整体压暗）。默认 "
    f"{default_text(augment_defaults().contrast)}，范围 0-1，0 = 不改变对比度；"
    "灰度图同样生效。"
)
TOOLTIP_HSV_V = (
    "亮度（明度）抖动幅度，占 100% 的比例。默认 "
    f"{default_text(augment_defaults().hsv_v)}。取值 0-1。"
)
TOOLTIP_DEGREES = (
    "随机旋转角度上限，单位：度。默认 "
    f"{default_text(augment_defaults().degrees)}（在该角度内随机旋转）。"
    "范围 0-180，实际采样 ±该值。"
)
TOOLTIP_TRANSLATE = (
    "随机平移幅度，占图像宽/高的比例。默认 "
    f"{default_text(augment_defaults().translate)}（±"
    f"{augment_defaults().translate * 100:g}%）。范围 0-1。"
)
TOOLTIP_SCALE = (
    "随机缩放增益范围。官方 float 语义 s 等价于增益区间 [1-s, 1+s]；默认 "
    f"{default_text(augment_defaults().scale_min)} 即 ["
    f"{default_text(augment_defaults().scale_min)}, "
    f"{default_text(augment_defaults().scale_max)}]。『scale min』为最小增益、"
    "『scale max』为最大增益，两者范围均为 0-10。"
)
TOOLTIP_FLIPUD = (
    "勾选后本项进入抽签：每次尝试按统一概率 p 决定是否整体上下翻转——抽中即 "
    "100% 翻转，没有幅度概念；不勾选则本项完全不参与。默认勾选。对应官方参数 "
    "flipud（Ultralytics 里是概率，本工具是启用位，生效概率见报告 "
    "official_names.flipud）。"
)
TOOLTIP_FLIPLR = (
    "勾选后本项进入抽签：每次尝试按统一概率 p 决定是否整体左右翻转——抽中即 "
    "100% 翻转，没有幅度概念；不勾选则本项完全不参与。默认勾选。对应官方参数 "
    "fliplr（Ultralytics 里是概率，本工具是启用位，生效概率见报告 "
    "official_names.fliplr）。"
)
TOOLTIP_SELECT_PROB = (
    "每个增强项的选中概率 p：每次尝试各自独立抽签，未选中的项按恒等处理"
    "（旋转 0 度、平移 0、缩放 1、亮度 0、对比度 0、不翻转）。只有被勾选的翻转"
    "进入抽签；勾选即表示这一项参与抽签，抽中即整体翻转（无幅度概念）。默认 "
    f"{default_text(augment_defaults().select_prob)}：每次尝试平均抽中约 0.7 项、"
    "约 85% 的尝试抽中 0 或 1 项；落到实际产出的副本上（必有 ≥1 项）平均约 1.3 项、"
    "约 71.3% 只带 1 项。若某次抽签一项都没抽中，该次不产生副本并自动"
    "进入下一次尝试；尝试用尽仍无副本的样本会被丢弃，报告里计入 "
    "discarded_empty_selection。范围 0.05-1（p=1 = 全部项都生效，仅作对照）。"
)
# Gray sources are augmented in RGB and collapsed back to one channel
# before they are encoded: the note says so without promising anything
# about a parameter the stack still applies.
GRAYSCALE_HINT = "灰度图会先转 RGB 增强再转回灰度；亮度与对比度都会改变灰度像素"
TOOLTIP_GRAYSCALE_HINT = (
    "灰度内容（单通道，或三通道但 R=G=B 逐像素相等）经 灰度→RGB→增强→灰度 处理："
    "增强流水线与参数和彩色图完全相同，几何类参数与标签坐标完全不变，产物仍是"
    "单通道灰度图。实测中亮度（hsv_v）与对比度（contrast）都会改变灰度像素："
    "亮度沿灰色轴移动明暗，对比度以黑为轴缩放全部像素；两者对灰度内容都真实"
    "生效，产物保持单通道。"
)
TOOLTIP_MULTI_IMAGE_HINT = (
    "mosaic / mixup / cutmix / copy_paste / copy_paste_mode / auto_augment "
    "需要多张图合成或仅用于分类，本工具只做单图增强故不实现，report 中会记录"
    "未启用清单。"
)


def _compact_spin(spin: QtWidgets.QAbstractSpinBox) -> None:
    """Remove the arrows and clamp the width of one spin box."""

    spin.setButtonSymbols(QtWidgets.QAbstractSpinBox.ButtonSymbols.NoButtons)
    spin.setFixedWidth(SPIN_WIDTH)


def _double_spin(
    minimum: float, maximum: float, value: float, step: float
) -> QtWidgets.QDoubleSpinBox:
    """Create a configured, compact double spin box."""

    box = QtWidgets.QDoubleSpinBox()
    box.setDecimals(4)
    box.setRange(minimum, maximum)
    box.setSingleStep(step)
    box.setValue(value)
    _compact_spin(box)
    return box


def _int_spin(minimum: int, maximum: int, value: int) -> QtWidgets.QSpinBox:
    """Create a configured, compact integer spin box."""

    box = QtWidgets.QSpinBox()
    box.setRange(minimum, maximum)
    box.setValue(value)
    _compact_spin(box)
    return box


class ConfigPage(QtWidgets.QWidget):
    """Collect the dataset, model and augmentation options."""

    start_requested = QtCore.pyqtSignal()
    # the request of the history page: the window lists the run folders
    # that are still in the system temporary directory and may restore
    # one of them. The page owns no scan of its own (see ui/history_page).
    history_requested = QtCore.pyqtSignal()

    def __init__(self, parent: Optional[Any] = None) -> None:
        super().__init__(parent)
        self.classes: list = []
        self.meta_info: Dict[str, Any] = {}
        self._build_ui()

    # -------------------------------------------------------------- helpers
    def _tooltip(self, text: str, widget: QtWidgets.QWidget) -> str:
        """Set a translated tooltip on a control and return the text."""

        widget.setToolTip(self.tr(text))
        return text

    def _titled(self, label: str, text: str) -> QtWidgets.QLabel:
        """Create a field label carrying the tooltip of its control."""

        widget = QtWidgets.QLabel(self.tr(label))
        widget.setToolTip(self.tr(text))
        return widget

    def _tight_form(self, box: QtWidgets.QGroupBox) -> QtWidgets.QFormLayout:
        """Create the compact form layout owned by a group box."""

        form = QtWidgets.QFormLayout(box)
        form.setContentsMargins(8, 6, 8, 6)
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(4)
        form.setLabelAlignment(
            QtCore.Qt.AlignmentFlag.AlignRight
            | QtCore.Qt.AlignmentFlag.AlignVCenter
        )
        return form

    # ------------------------------------------------------------------ ui
    def _build_ui(self) -> None:
        """Create every widget of the page."""

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        title = QtWidgets.QLabel(self.tr("模型验证"))
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        outer.addWidget(title)

        # the drop targets are announced once, right below the title
        self.drop_hint = QtWidgets.QLabel(self.tr(DRAG_SUPPORTED_HINT))
        hint_font = self.drop_hint.font()
        hint_font.setPointSizeF(max(hint_font.pointSizeF() - 1.0, 6.0))
        self.drop_hint.setFont(hint_font)
        self.drop_hint.setToolTip(self.tr(DRAG_SUPPORTED_HINT))
        outer.addWidget(self.drop_hint)

        outer.addWidget(self._build_source_box())
        outer.addWidget(self._build_threshold_box())
        outer.addWidget(self._build_augment_box())

        # the live counters stay on a single line and are always visible
        self.preview_label = QtWidgets.QLabel(
            self.tr("有效原图 0 / 将生成增强 0 / 验证总数 0 / 预计导出 0")
        )
        self.preview_label.setWordWrap(False)
        self.preview_label.setToolTip(
            self.tr(
                "实时统计：有效原图（有同名 .json 且含标注的图片）、按当前固定比例"
                "将要生成的增强数、参与判定的总数、以及按当前勾选预计导出的数量。"
            )
        )
        outer.addWidget(self.preview_label)

        self.status_label = QtWidgets.QLabel("")
        self.status_label.setToolTip(
            self.tr("校验与运行状态：缺失项、阻断原因或最近一次失败信息。")
        )
        self.status_label.setMinimumHeight(18)
        self.status_label.setMaximumHeight(40)
        outer.addWidget(self.status_label)

        outer.addStretch(1)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addStretch(1)
        # the history sits left of the start: it is the second way into
        # the tool, not a second way to start a run
        self.history_button = QtWidgets.QPushButton(
            self.tr("历史记录…")
        )
        self.history_button.setToolTip(
            self.tr(
                "列出系统临时目录里的历次验证，可恢复到某次的判定与标记。"
            )
        )
        self.history_button.clicked.connect(self.history_requested.emit)
        buttons.addWidget(self.history_button)
        self.start_button = QtWidgets.QPushButton(self.tr("开始验证"))
        self.start_button.setToolTip(
            self.tr(
                "校验配置（数据目录、classes.txt、ONNX 及其元数据）后开始拷贝副本、"
                "生成增强、推理判定；校验不通过会在下方状态行列出原因。"
            )
        )
        self.start_button.clicked.connect(self.start_requested.emit)
        buttons.addWidget(self.start_button)
        outer.addLayout(buttons)

        # the page owns the drag and drop of the window; the read only
        # line edits would otherwise swallow the drag once the cursor is
        # over them.
        self.setAcceptDrops(True)
        self._release_child_drops()

    def _build_source_box(self) -> QtWidgets.QGroupBox:
        """Create the read only data source group box."""

        box = QtWidgets.QGroupBox(
            self.tr("数据来源与模型（数据来源目录只读，仅用于生成临时副本）")
        )
        form = self._tight_form(box)

        self.dataset_edit = QtWidgets.QLineEdit()
        self.dataset_edit.setReadOnly(True)
        self.dataset_edit.setPlaceholderText(
            self.tr("数据来源目录（只读，仅用于生成临时副本）")
        )
        form.addRow(
            self._titled("数据来源目录", TOOLTIP_DATASET),
            self._with_button(
                self.dataset_edit,
                self.tr("浏览…"),
                self.browse_dataset,
                self._tooltip(TOOLTIP_DATASET, self.dataset_edit),
            ),
        )

        self.classes_edit = QtWidgets.QLineEdit()
        self.classes_edit.setReadOnly(True)
        form.addRow(
            self._titled("类别表 classes.txt", TOOLTIP_CLASSES),
            self._with_button(
                self.classes_edit,
                self.tr("浏览…"),
                self.browse_classes,
                self._tooltip(TOOLTIP_CLASSES, self.classes_edit),
            ),
        )

        self.model_edit = QtWidgets.QLineEdit()
        self.model_edit.setReadOnly(True)
        form.addRow(
            self._titled("ONNX 模型", TOOLTIP_MODEL),
            self._with_button(
                self.model_edit,
                self.tr("浏览…"),
                self.browse_model,
                self._tooltip(TOOLTIP_MODEL, self.model_edit),
            ),
        )
        return box

    def _build_threshold_box(self) -> QtWidgets.QGroupBox:
        """Create the inference threshold group box."""

        box = QtWidgets.QGroupBox(self.tr("推理与判定阈值"))
        form = self._tight_form(box)

        # every initial value is the one the dataclass the run consumes
        # ships, so a changed default reaches this form unchanged
        defaults = config_defaults()
        self.conf_spin = _double_spin(0.0, 1.0, defaults.conf_threshold, 0.05)
        self.iou_spin = _double_spin(0.0, 1.0, defaults.iou_threshold, 0.05)
        self.ng_iou_spin = _double_spin(
            0.0, 1.0, defaults.ng_iou_threshold, 0.05
        )
        thresholds = QtWidgets.QHBoxLayout()
        thresholds.setContentsMargins(0, 0, 0, 0)
        thresholds.setSpacing(12)
        for label, text, spin in (
            ("NG 分数 score", TOOLTIP_CONF, self.conf_spin),
            ("NMS IoU iou", TOOLTIP_IOU, self.iou_spin),
            ("判定 NG IoU", TOOLTIP_NG_IOU, self.ng_iou_spin),
        ):
            thresholds.addWidget(self._titled(label, text))
            spin.setToolTip(self.tr(text))
            thresholds.addWidget(spin)
        thresholds.addStretch(1)
        form.addRow(self._titled("阈值", TOOLTIP_THRESHOLDS_ROW), thresholds)

        self.judge_augmented_check = QtWidgets.QCheckBox(
            self.tr("增强图参与推理判定（默认开启）")
        )
        self.judge_augmented_check.setChecked(defaults.judge_augmented)
        self.judge_augmented_check.setToolTip(self.tr(TOOLTIP_JUDGE_AUGMENTED))
        # the concurrency of the inference is fixed as well: its read
        # only note shares this row and shows the value of the machine
        self.infer_workers_note = QtWidgets.QLabel(
            self.tr(workers_note("推理", default_workers()))
        )
        self.infer_workers_note.setToolTip(self.tr(TOOLTIP_INFER_WORKERS))
        judge_row = QtWidgets.QHBoxLayout()
        judge_row.setContentsMargins(0, 0, 0, 0)
        judge_row.setSpacing(12)
        judge_row.addWidget(self.judge_augmented_check)
        judge_row.addWidget(self.infer_workers_note)
        judge_row.addStretch(1)
        form.addRow(judge_row)
        return box

    def _with_button(
        self,
        line_edit: QtWidgets.QLineEdit,
        text: str,
        slot,
        tooltip: str = "",
    ) -> QtWidgets.QWidget:
        """Wrap a read only line edit with a browse button."""

        container = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(line_edit, 1)
        button = QtWidgets.QPushButton(text)
        button.setToolTip(self.tr(tooltip or TOOLTIP_BROWSE))
        button.clicked.connect(slot)
        layout.addWidget(button)
        return container

    @staticmethod
    def _label_width(texts: Sequence[str]) -> int:
        """Return the width that lines up every control of a column.

        A fixed width for all parameter names makes the label column of
        every grid cell identical, therefore the controls start at the
        very same x no matter how long a single name is.
        """

        metrics = QtGui.QFontMetrics(QtWidgets.QApplication.font())
        return max([metrics.horizontalAdvance(text) for text in texts] + [0])

    def _cell(
        self,
        label: str,
        widget: QtWidgets.QWidget,
        tooltip: str,
        suffix: str = "",
        label_width: int = 0,
    ) -> QtWidgets.QWidget:
        """Lay out one compact "name + control" group of the grid."""

        container = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        text = self.tr(label) + suffix
        name = QtWidgets.QLabel(text)
        name.setToolTip(self.tr(tooltip))
        if label_width > 0:
            name.setFixedWidth(label_width)
        widget.setToolTip(self.tr(tooltip))
        layout.addWidget(name)
        layout.addWidget(widget)
        layout.addStretch(1)
        return container

    def _augment_specs(
        self,
    ) -> Sequence[Tuple[str, str, QtWidgets.QWidget, str]]:
        """Return the (label, suffix, control, tooltip) rows of the grid.

        The order is the field order of AugmentParams, so the grid reads
        top to bottom, left to right exactly like the parameter
        dataclass, the configuration snapshot and the report. The two
        flips are check boxes here: they are enable bits, not
        probabilities, and the tooltip of each one spells out the
        effective chance the report writes down.

        The select probability is the one augment parameter that is not
        a cell of the grid: it shares the amount row with the ratio,
        because it is the chance of the very same draw that picks the
        amount. It still follows the augment checkbox through
        _augment_param_widgets, which adds it back explicitly.
        """

        return (
            ("对比度 contrast", "", self.contrast_spin, TOOLTIP_CONTRAST),
            ("亮度 hsv_v", "", self.hsv_v_spin, TOOLTIP_HSV_V),
            (
                "旋转角度 degrees",
                "（°）",
                self.degrees_spin,
                TOOLTIP_DEGREES,
            ),
            ("平移 translate", "", self.translate_spin, TOOLTIP_TRANSLATE),
            ("缩放下限 scale", " min", self.scale_min_spin, TOOLTIP_SCALE),
            ("缩放上限 scale", " max", self.scale_max_spin, TOOLTIP_SCALE),
            ("垂直翻转", "", self.flipud_check, TOOLTIP_FLIPUD),
            ("水平翻转", "", self.fliplr_check, TOOLTIP_FLIPLR),
            ("随机种子 seed", "", self.seed_spin, TOOLTIP_SEED),
        )

    def _build_grid(self) -> QtWidgets.QGridLayout:
        """Create the multi column grid holding the augment parameters.

        The three columns are deliberately identical: they carry the
        same minimum width and the same stretch factor, and every label
        of a column is clamped to the width of the longest parameter
        name, so the controls of all three columns start on one line.
        """

        specs = list(self._augment_specs())
        columns = 3
        label_width = self._label_width(
            [self.tr(label) + suffix for label, suffix, _, _ in specs]
        )
        grid = QtWidgets.QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(4)
        for index, (label, suffix, widget, tooltip) in enumerate(specs):
            grid.addWidget(
                self._cell(label, widget, tooltip, suffix, label_width),
                index // columns,
                index % columns,
            )
        needs = label_width + 6 + SPIN_WIDTH
        for column in range(columns):
            grid.setColumnMinimumWidth(column, needs)
            grid.setColumnStretch(column, 1)
        grid.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetMinimumSize)
        return grid

    def _build_augment_box(self) -> QtWidgets.QGroupBox:
        """Create the single image augmentation group box."""

        box = QtWidgets.QGroupBox(self.tr("增强（仅单图增强）"))
        form = self._tight_form(box)

        # the shipped defaults come from the two dataclasses, never from
        # a literal repeated on this page
        params = augment_defaults()
        defaults = config_defaults()

        self.augment_check = QtWidgets.QCheckBox(self.tr("生成增强副本"))
        self.augment_check.setChecked(defaults.augment_enabled)
        self.augment_check.setToolTip(self.tr(TOOLTIP_AUGMENT_ENABLED))
        # the concurrency of the stage is fixed: the read only note
        # shares the row of the checkbox, so the value stays visible in
        # this group without making the compact page one row taller
        self.augment_workers_note = QtWidgets.QLabel(
            self.tr(workers_note("增强", default_workers()))
        )
        self.augment_workers_note.setToolTip(self.tr(TOOLTIP_AUGMENT_WORKERS))
        augment_row = QtWidgets.QHBoxLayout()
        augment_row.setContentsMargins(0, 0, 0, 0)
        augment_row.setSpacing(12)
        augment_row.addWidget(self.augment_check)
        augment_row.addWidget(self.augment_workers_note)
        augment_row.addStretch(1)
        form.addRow(augment_row)

        self.contrast_spin = _double_spin(0.0, 1.0, params.contrast, 0.05)
        self.hsv_v_spin = _double_spin(0.0, 1.0, params.hsv_v, 0.05)
        self.degrees_spin = _double_spin(0.0, 180.0, params.degrees, 1.0)
        self.translate_spin = _double_spin(0.0, 1.0, params.translate, 0.05)
        self.scale_min_spin = _double_spin(0.0, 10.0, params.scale_min, 0.05)
        self.scale_max_spin = _double_spin(0.0, 10.0, params.scale_max, 0.05)
        # the two flips are enable bits: a check box answers whether the
        # flip takes part in the draw at all, the chance it really fires
        # is select_prob like for every other selected field
        self.flipud_check = QtWidgets.QCheckBox()
        self.flipud_check.setChecked(params.flipud)
        self.flipud_check.setToolTip(self.tr(TOOLTIP_FLIPUD))
        self.fliplr_check = QtWidgets.QCheckBox()
        self.fliplr_check.setChecked(params.fliplr)
        self.fliplr_check.setToolTip(self.tr(TOOLTIP_FLIPLR))
        self.select_prob_spin = _double_spin(
            0.05, 1.0, params.select_prob, 0.05
        )
        self.seed_spin = _int_spin(0, 2147483647, params.seed)

        # the amount mode is not a choice any more: the page states the
        # one mode every run uses and keeps the ratio editable, so the
        # collected configuration always carries RATIO_MODE
        self.ratio_spin = _double_spin(0.0, 1.0, defaults.ratio, 0.1)

        amount = QtWidgets.QHBoxLayout()
        amount.setContentsMargins(0, 0, 0, 0)
        amount.setSpacing(6)
        self.mode_note = QtWidgets.QLabel(self.tr("数量模式：比例 r（固定）"))
        self.mode_note.setToolTip(self.tr(TOOLTIP_MODE_FIXED))
        amount.addWidget(self.mode_note)
        amount.addWidget(self._titled("比例 r", TOOLTIP_RATIO))
        self.ratio_spin.setToolTip(self.tr(TOOLTIP_RATIO))
        amount.addWidget(self.ratio_spin)
        # the select probability is the chance of the very draw that
        # turns the ratio into a copy count, so it sits next to it on
        # the amount row instead of inside the parameter grid
        amount.addWidget(self._titled("选中概率 p", TOOLTIP_SELECT_PROB))
        self.select_prob_spin.setToolTip(self.tr(TOOLTIP_SELECT_PROB))
        amount.addWidget(self.select_prob_spin)
        amount.addStretch(1)
        form.addRow(amount)

        form.addRow(self._build_grid())

        # the gray note belongs to the parameter grid it explains and is
        # read only: no control of the page depends on it
        self.grayscale_hint = QtWidgets.QLabel(self.tr(GRAYSCALE_HINT))
        self.grayscale_hint.setWordWrap(True)
        hint_font = self.grayscale_hint.font()
        hint_font.setPointSizeF(max(hint_font.pointSizeF() - 1.0, 6.0))
        self.grayscale_hint.setFont(hint_font)
        self.grayscale_hint.setToolTip(self.tr(TOOLTIP_GRAYSCALE_HINT))
        form.addRow(self.grayscale_hint)

        self.multi_image_hint = QtWidgets.QLabel(
            self.tr(
                "多图增强（mosaic / mixup / cutmix / copy_paste / "
                "copy_paste_mode / auto_augment）一律不实现"
            )
        )
        self.multi_image_hint.setWordWrap(True)
        self.multi_image_hint.setMaximumWidth(520)
        hint_font = self.multi_image_hint.font()
        hint_font.setPointSizeF(max(hint_font.pointSizeF() - 1.0, 6.0))
        self.multi_image_hint.setFont(hint_font)
        self.multi_image_hint.setToolTip(self.tr(TOOLTIP_MULTI_IMAGE_HINT))
        form.addRow(self.multi_image_hint)

        self.augment_check.toggled.connect(self._sync_augment_enabled)
        default_augment = self.augment_check.isChecked()
        self._sync_augment_enabled(default_augment)
        return box

    # ------------------------------------------------------------ drag/drop
    def _release_child_drops(self) -> None:
        """Let the page handle every drag that lands on one of its children.

        The read only line edits accept drops on their own and would
        swallow the drag as soon as the cursor hovers over them. Turning
        the flag off for every descendant hands the event back to the
        page, which is the widget owning the drop targets.
        """

        for child in self.findChildren(QtWidgets.QWidget):
            child.setAcceptDrops(False)

    @staticmethod
    def _classify_drop(path: str) -> Tuple[str, str]:
        """Return the (kind, reason) of one dropped path.

        The kind is "dataset" for a directory, "classes" for a .txt and
        "model" for a .onnx file. Everything else comes back as
        "unsupported" with the reason naming the file.
        """

        base = osp.basename(path)
        if osp.isdir(path):
            return "dataset", ""
        suffix = osp.splitext(path)[1].lower()
        if suffix == ".txt":
            return "classes", ""
        if suffix == ".onnx":
            return "model", ""
        return "unsupported", base

    def _accept_urls(self, event: QtGui.QDropEvent) -> None:
        """Route a drop event to :meth:`_handle_urls`."""

        mime = event.mimeData()
        # QUrl spells a windows path with forward slashes, so the dropped
        # paths are normalised to the very form the file dialogs produce
        paths = [
            osp.normpath(url.toLocalFile())
            for url in mime.urls()
            if url.isLocalFile() and url.toLocalFile()
        ]
        self._handle_urls(paths)
        event.acceptProposedAction()

    def _handle_urls(self, paths: Sequence[str]) -> List[str]:
        """Apply the dropped paths and return the notes shown to the user.

        Directories fill the source dataset, .txt files the classes table
        and .onnx files the model path. When several paths of one kind are
        dropped the last one wins and the note says so. Anything else is
        ignored but reported on the status line.
        """

        picked: Dict[str, str] = {}
        rejected: List[str] = []
        for path in paths:
            text = str(path)
            kind, reason = self._classify_drop(text)
            if kind == "unsupported":
                rejected.append(reason)
            else:
                picked[kind] = text

        notes: List[str] = []
        dataset = picked.get("dataset")
        if dataset:
            # set_dataset fires textChanged, the very signal that caches
            # the pair count of the new directory and refreshes the view
            self.set_dataset(dataset)
            notes.append(self.tr("数据来源目录：") + dataset)

        classes = picked.get("classes")
        classes_loaded = True
        if classes:
            classes_loaded = self.load_classes(classes) is not None
            if classes_loaded:
                notes.append(self.tr("类别表：") + classes)
            else:
                # the path stays empty on purpose, but the user must see
                # that the dropped file was recognised and refused
                notes.append(self.tr("类别表未能加载：") + classes)

        model = picked.get("model")
        if model:
            self.set_model(model)
            notes.append(self.tr("ONNX 模型：") + model)

        if rejected:
            dropped = self.tr(
                "已忽略 {n} 个不支持的文件（仅支持 目录 / classes.txt / .onnx）：{names}"
            )
            notes.append(
                dropped.format(n=len(rejected), names=", ".join(rejected))
            )
        for kind, label, chosen in (
            ("dataset", self.tr("目录"), dataset),
            ("classes", ".txt", classes),
            ("model", ".onnx", model),
        ):
            if not chosen:
                continue
            same_kind = [
                path
                for path in paths
                if self._classify_drop(str(path))[0] == kind
            ]
            if len(same_kind) > 1:
                notes.append(
                    self.tr("多个 {label}，已使用最后一个：").format(
                        label=label
                    )
                    + osp.basename(chosen)
                )

        if not notes:
            self.set_status(
                self.tr(
                    "拖入的内容没有可用目标，仅支持 目录 / classes.txt / .onnx"
                )
            )
            return notes
        message = "；".join(notes)
        if classes and not classes_loaded:
            message = message + "；" + self.tr("类别表未能加载，请检查文件")
        self.set_status(message, error=bool(rejected) or not classes_loaded)
        return notes

    def dragEnterEvent(self, event: QtGui.QDragEnterEvent) -> None:
        """Accept a file drag entering the page."""

        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QtGui.QDragMoveEvent) -> None:
        """Accept a file drag hovering over the page."""

        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QtGui.QDropEvent) -> None:
        """Route every dropped url to the matching input of the page."""

        if event.mimeData().hasUrls():
            self._accept_urls(event)
        else:
            event.ignore()

    def _augment_param_widgets(self) -> Sequence[QtWidgets.QWidget]:
        """Return every augment parameter control the checkbox gates.

        The grid mixes spin boxes and the two flip check boxes: those
        controls are read from the very specs the grid was built from,
        so a cell can never be missing here. The select probability is
        the one parameter whose control lives on the amount row instead
        of a grid cell, therefore it is added explicitly: it belongs to
        this group all the same, because the augment checkbox gates the
        whole parameter face of the page.
        """

        widgets = [widget for _l, _s, widget, _t in self._augment_specs()]
        widgets.append(self.select_prob_spin)
        return widgets

    def _sync_augment_enabled(self, enabled: bool) -> None:
        """Enable or grey the augmentation amount and parameter controls."""

        on = bool(enabled)
        # the amount mode is fixed: only the ratio stays editable, and it
        # follows the augment checkbox like the parameter grid does
        self.ratio_spin.setEnabled(on)
        for widget in self._augment_param_widgets():
            widget.setEnabled(on)

    def load_classes(self, path: str) -> Optional[list]:
        """Load a classes file into the page and return the parsed names.

        The path is only recorded together with the names it produced:
        an unreadable or empty file leaves the form as it was and puts
        the reason on the status line, exactly like the manual browse
        button does.
        """

        try:
            classes = load_classes_file(path)
        except ValidationConfigError as error:
            self.set_status(str(error), error=True)
            return None
        self.set_classes(path, classes)
        return classes

    def set_dataset(self, path: str) -> None:
        """Record the chosen dataset directory."""

        self.dataset_edit.setText(path)

    def set_classes(self, path: str, classes: list) -> None:
        """Record the chosen classes file and its parsed names."""

        self.classes_edit.setText(path)
        self.classes = list(classes)

    def set_model(self, path: str) -> None:
        """Record the chosen ONNX model."""

        self.model_edit.setText(path)

    def set_status(self, message: str, error: bool = False) -> None:
        """Show a status message below the form."""

        color = "#c0392b" if error else "#27ae60"
        self.status_label.setStyleSheet(f"color: {color};")
        self.status_label.setText(message)

    def set_preview(self, text: str) -> None:
        """Update the live preview line."""

        self.preview_label.setText(text)

    def augment_params(self) -> AugmentParams:
        """Read the augmentation parameters from the form."""

        return AugmentParams(
            contrast=self.contrast_spin.value(),
            hsv_v=self.hsv_v_spin.value(),
            degrees=self.degrees_spin.value(),
            translate=self.translate_spin.value(),
            scale_min=self.scale_min_spin.value(),
            scale_max=self.scale_max_spin.value(),
            flipud=self.flipud_check.isChecked(),
            fliplr=self.fliplr_check.isChecked(),
            select_prob=self.select_prob_spin.value(),
            seed=self.seed_spin.value(),
        )

    def collect_config(self) -> ValidationConfig:
        """Build the validation configuration from the current form state.

        Neither the amount mode nor the concurrency of the two CPU bound
        stages is collected: the mode is fixed to RATIO_MODE and the page
        owns no control for the workers any more, so both amount fields
        the ratio mode does not use (multiplier, total_count) keep the
        default of the dataclass and every worker field keeps the fixed
        value a run derives from this machine.
        """

        return ValidationConfig(
            dataset_dir=self.dataset_edit.text().strip(),
            classes_file=self.classes_edit.text().strip(),
            model_path=self.model_edit.text().strip(),
            conf_threshold=self.conf_spin.value(),
            iou_threshold=self.iou_spin.value(),
            ng_iou_threshold=self.ng_iou_spin.value(),
            judge_augmented=self.judge_augmented_check.isChecked(),
            augment_enabled=self.augment_check.isChecked(),
            augment_mode=RATIO_MODE,
            ratio=self.ratio_spin.value(),
            augment_params=self.augment_params(),
        )

    def browse_dataset(self) -> None:
        """Browse for the dataset directory."""

        path = QtWidgets.QFileDialog.getExistingDirectory(
            self, self.tr("选择数据来源目录（只读）")
        )
        if path:
            self.set_dataset(path)

    def browse_classes(self) -> None:
        """Browse for the classes file."""

        path, _filter = QtWidgets.QFileDialog.getOpenFileName(
            self,
            self.tr("选择类别表 classes.txt"),
            "",
            self.tr("类别表 (*.txt);;所有文件 (*)"),
        )
        if path:
            self.set_classes(path, self.classes)

    def browse_model(self) -> None:
        """Browse for the ONNX model."""

        path, _filter = QtWidgets.QFileDialog.getOpenFileName(
            self,
            self.tr("选择 ONNX 模型"),
            "",
            self.tr("ONNX 模型 (*.onnx);;所有文件 (*)"),
        )
        if path:
            self.set_model(path)


__all__ = [
    "GRAYSCALE_HINT",
    "TOOLTIP_GRAYSCALE_HINT",
    "ConfigPage",
]
