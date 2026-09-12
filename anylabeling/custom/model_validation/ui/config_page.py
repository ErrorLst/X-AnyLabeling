"""Configuration page of the model validation sub window."""

from __future__ import annotations

import os.path as osp
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PyQt6 import QtCore, QtGui, QtWidgets

from ..app_config import (
    AUGMENT_WORKERS_LIMIT,
    COUNT_MODE,
    MULTIPLIER_MODE,
    RATIO_MODE,
    AugmentParams,
    ValidationConfig,
    ValidationConfigError,
    default_workers,
    load_classes_file,
)

# The amount of every augmentation run is derived with the mode the
# configuration dataclass ships unless the user picks another one.
DEFAULT_AUGMENT_MODE = ValidationConfig().augment_mode


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


def default_marker(mode: str) -> str:
    """Return the "default" marker of one choice, empty for the others."""

    return "（默认）" if mode == DEFAULT_AUGMENT_MODE else ""


SINGLE_IMAGE_ONLY_HINT = (
    "仅分类任务有效；本工具 classify 已阻断，恒不生效（控件置灰保留仅为对齐"
    "官方参数表）"
)

# The controls of the augmentation grid share one compact width so that
# the twelve single image parameters fit into three columns.
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
    "本组三个阈值：置信度阈值 conf、NMS IoU 阈值、NG IoU 阈值——取值范围与"
    "逐项含义见各控件自身的悬停说明。"
)
TOOLTIP_CONF = (
    "低于该分数的预测框被丢弃。范围 0.00-1.00（建议 ≥0.01），默认 "
    f"{default_text(config_defaults().conf_threshold)}。调低"
    "召回更多目标、同时引入更多误检（更易判 NG）。"
)
TOOLTIP_IOU = (
    "非极大值抑制的重叠阈值：两个同类框 IoU 超过它时只保留分数高的那个。范围 "
    "0.00-1.00（建议 ≥0.01），默认 "
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
    "默认开启：按下面的数量模式生成增强样本（只写入系统临时目录）；关闭后只"
    "验证原始数据。"
)
TOOLTIP_MODE = (
    "三种模式只影响『增强数量』的推导方式，增强参数、判定与导出完全一致："
    f"① 倍数 k{default_marker(MULTIPLIER_MODE)}：每张有效原图各生成 k 份；"
    f"② 比例 r{default_marker(RATIO_MODE)}：只控制总量 "
    "round(有效原图数 × r)，来源从有效原图中随机抽取；"
    f"③ 总数 N{default_marker(COUNT_MODE)}：总量语义，"
    "按 natsort 顺序确定分配。"
)
TOOLTIP_MULTIPLIER = (
    "每张有效原图各生成 k 份，增强图与原图一一对应（100 张原图、k=2 → 共 "
    "200 张，每张原图都有 2 个增强版）。默认 "
    f"{default_text(config_defaults().multiplier)}，整数。"
)
TOOLTIP_RATIO = (
    "只控制总量 = round(有效原图数 × r)，来源从有效原图中随机抽取（可重复），"
    "因此有的原图可能被抽 0 次、有的被抽多次；r=1 与 k=1 总量相同但来源分布"
    "不同。默认模式，范围 0-10，默认 "
    f"{default_text(config_defaults().ratio)}。"
)
TOOLTIP_COUNT = (
    "总量语义但分配确定：按 natsort 顺序每张分 N // 原图数 份，余数分给前 "
    "N % 原图数 张。整数。"
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
TOOLTIP_HSV_H = (
    "色调抖动幅度，映射为 ±(hsv_h × 180)°（hue_shift_limit = hsv_h × 180）；"
    f"默认 {default_text(augment_defaults().hsv_h)} 约 ±"
    f"{augment_defaults().hsv_h * 180:.1f}°。取值 0-1。"
)
TOOLTIP_HSV_S = (
    "饱和度抖动幅度，占 100% 的比例。默认 "
    f"{default_text(augment_defaults().hsv_s)}。取值 0-1。"
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
TOOLTIP_SHEAR = (
    "随机剪切角度上限，单位：度（x/y 方向各自采样 ±该值）。范围 0-180，默认 "
    f"{default_text(augment_defaults().shear)}。"
)
TOOLTIP_PERSPECTIVE = (
    "透视扰动强度，四角随机位移幅度（相对短边比例）。默认 0.0（关闭）。典型"
    "范围 0-0.001。"
)
TOOLTIP_FLIPUD = (
    "垂直翻转概率。默认 "
    f"{default_text(augment_defaults().flipud)}"
    "（每张图按该概率独立决定）。取值 0-1。"
)
TOOLTIP_FLIPLR = (
    "水平翻转概率。默认 "
    f"{default_text(augment_defaults().fliplr)}。取值 0-1。"
)
TOOLTIP_BGR = (
    "RGB↔BGR 通道互换概率。默认 "
    f"{default_text(augment_defaults().bgr)}。用于模拟通道顺序错配的异常输入。"
)
TOOLTIP_ERASING = (
    "随机擦除概率。官方仅对分类任务生效；本工具 classify 已被阻断，因此恒不"
    "生效（控件置灰保留仅为对齐官方参数表）。默认 "
    f"{default_text(augment_defaults().erasing)}。"
)
TOOLTIP_CROP_FRACTION = (
    "分类数据集的中心裁剪比例。官方仅分类任务生效，本工具恒不生效（控件置灰"
    "保留）。默认 "
    f"{default_text(augment_defaults().crop_fraction)}。"
)
# Gray sources are augmented in RGB and collapsed back to one channel
# before they are encoded: the note says so without promising anything
# about a parameter the stack still applies.
GRAYSCALE_HINT = (
    "灰度图会先转 RGB 增强再转回灰度；色彩类参数对灰度数据的影响会部分体现在"
    "明暗上"
)
TOOLTIP_GRAYSCALE_HINT = (
    "灰度内容（单通道，或三通道但 R=G=B 逐像素相等）经 灰度→RGB→增强→灰度 处理："
    "增强流水线与参数和彩色图完全相同，几何类参数与标签坐标完全不变，产物仍是"
    "单通道灰度图。实测中只有亮度类参数（hsv_v）会改变灰度像素；hue / saturation "
    "对饱和度恒为 0 的灰度图不产生可见变化（albumentations 会把 S=0 的像素保持为"
    "灰色），bgr 通道互换在灰度内容上也是恒等。"
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
                "实时统计：有效原图（有同名 .json 且含标注的图片）、按当前数量模式"
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
        # line edits and the combo box would otherwise swallow the drag
        # once the cursor is over them.
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
            ("置信度 conf", TOOLTIP_CONF, self.conf_spin),
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

        Every label names the parameter in Chinese first and keeps the
        official Ultralytics argument name behind it, so the page stays
        comparable 1:1 with the documentation of the augmentation
        arguments. The suffix carries what a name cannot spell (the unit
        of the two angle parameters, the min / max half of the scale).
        """

        return (
            ("色调 hsv_h", "", self.hsv_h_spin, TOOLTIP_HSV_H),
            ("饱和度 hsv_s", "", self.hsv_s_spin, TOOLTIP_HSV_S),
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
            ("剪切 shear", "（°）", self.shear_spin, TOOLTIP_SHEAR),
            (
                "透视 perspective",
                "",
                self.perspective_spin,
                TOOLTIP_PERSPECTIVE,
            ),
            ("垂直翻转 flipud", "", self.flipud_spin, TOOLTIP_FLIPUD),
            ("水平翻转 fliplr", "", self.fliplr_spin, TOOLTIP_FLIPLR),
            ("通道互换 bgr", "", self.bgr_spin, TOOLTIP_BGR),
            ("随机擦除 erasing", "", self.erasing_spin, TOOLTIP_ERASING),
            (
                "裁剪 crop_fraction",
                "",
                self.crop_fraction_spin,
                TOOLTIP_CROP_FRACTION,
            ),
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

        self.hsv_h_spin = _double_spin(0.0, 1.0, params.hsv_h, 0.001)
        self.hsv_s_spin = _double_spin(0.0, 1.0, params.hsv_s, 0.05)
        self.hsv_v_spin = _double_spin(0.0, 1.0, params.hsv_v, 0.05)
        self.degrees_spin = _double_spin(0.0, 180.0, params.degrees, 1.0)
        self.translate_spin = _double_spin(0.0, 1.0, params.translate, 0.05)
        self.scale_min_spin = _double_spin(0.0, 10.0, params.scale_min, 0.05)
        self.scale_max_spin = _double_spin(0.0, 10.0, params.scale_max, 0.05)
        self.shear_spin = _double_spin(0.0, 180.0, params.shear, 1.0)
        self.perspective_spin = _double_spin(
            0.0, 0.001, params.perspective, 0.0001
        )
        self.flipud_spin = _double_spin(0.0, 1.0, params.flipud, 0.05)
        self.fliplr_spin = _double_spin(0.0, 1.0, params.fliplr, 0.05)
        self.bgr_spin = _double_spin(0.0, 1.0, params.bgr, 0.05)
        self.erasing_spin = _double_spin(0.0, 1.0, params.erasing, 0.05)
        self.crop_fraction_spin = _double_spin(
            0.0, 1.0, params.crop_fraction, 0.05
        )
        self.seed_spin = _int_spin(0, 2147483647, params.seed)
        # the parameters the tool never applies are identified by name: the
        # same C++ widget can be wrapped in more than one python object, so
        # identity comparisons over these controls are not reliable.
        self.erasing_spin.setObjectName("erasing")
        self.crop_fraction_spin.setObjectName("crop_fraction")

        self.mode_combo = QtWidgets.QComboBox()
        for mode, label in (
            (MULTIPLIER_MODE, "倍数 multiplier"),
            (RATIO_MODE, "比例 ratio"),
            (COUNT_MODE, "总数 count"),
        ):
            self.mode_combo.addItem(
                self.tr(label + default_marker(mode)), mode
            )
        self.mode_combo.setCurrentIndex(
            self.mode_combo.findData(DEFAULT_AUGMENT_MODE)
        )
        self.mode_combo.setToolTip(self.tr(TOOLTIP_MODE))
        self.multiplier_spin = _int_spin(0, 1000, defaults.multiplier)
        self.ratio_spin = _double_spin(0.0, 10.0, defaults.ratio, 0.1)
        self.count_spin = _int_spin(0, 100000, defaults.total_count)

        amount = QtWidgets.QHBoxLayout()
        amount.setContentsMargins(0, 0, 0, 0)
        amount.setSpacing(6)
        amount_label = self._titled("数量模式", TOOLTIP_MODE)
        amount.addWidget(amount_label)
        amount.addWidget(self.mode_combo)
        for label, text, spin in (
            ("倍数 k", TOOLTIP_MULTIPLIER, self.multiplier_spin),
            ("比例 r", TOOLTIP_RATIO, self.ratio_spin),
            ("总数 N", TOOLTIP_COUNT, self.count_spin),
        ):
            spin.setToolTip(self.tr(text))
            name = QtWidgets.QLabel(self.tr(label))
            name.setToolTip(self.tr(text))
            amount.addWidget(name)
            amount.addWidget(spin)
        amount.addStretch(1)
        form.addRow(amount)

        self.mode_specs: Sequence[Tuple[str, QtWidgets.QWidget]] = (
            (MULTIPLIER_MODE, self.multiplier_spin),
            (RATIO_MODE, self.ratio_spin),
            (COUNT_MODE, self.count_spin),
        )

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

        # the two parameters the tool never applies keep their grey state
        # no matter what the augment checkbox says
        self.always_disabled_names: Sequence[str] = (
            self.erasing_spin.objectName(),
            self.crop_fraction_spin.objectName(),
        )
        self.erasing_spin.setEnabled(False)
        self.crop_fraction_spin.setEnabled(False)

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
        self.mode_combo.currentIndexChanged.connect(self._sync_mode)
        self._sync_mode()
        default_augment = self.augment_check.isChecked()
        self._sync_augment_enabled(default_augment)
        return box

    # ------------------------------------------------------------ drag/drop
    def _release_child_drops(self) -> None:
        """Let the page handle every drag that lands on one of its children.

        The read only line edits and the combo box accept drops on their
        own and would swallow the drag as soon as the cursor hovers over
        them. Turning the flag off for every descendant hands the event
        back to the page, which is the widget owning the drop targets.
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

    def _sync_augment_enabled(self, enabled: bool) -> None:
        """Enable or grey the augmentation amount and parameter controls."""

        self.mode_combo.setEnabled(bool(enabled))
        self._sync_mode()
        grey = self.always_disabled_names
        for spin in self._augment_param_spins():
            if spin.objectName() in grey:
                continue
            spin.setEnabled(bool(enabled))

    def _augment_param_spins(self) -> Sequence[QtWidgets.QAbstractSpinBox]:
        """Return the single image parameter controls of the grid."""

        return [widget for _l, _s, widget, _t in self._augment_specs()]

    def _sync_mode(self) -> None:
        """Show only the input of the selected amount mode."""

        enabled = self.augment_check.isChecked()
        mode = self.mode_combo.currentData()
        for name, widget in self.mode_specs:
            visible = name == mode
            widget.setEnabled(enabled and visible)
            widget.setVisible(visible)

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
            hsv_h=self.hsv_h_spin.value(),
            hsv_s=self.hsv_s_spin.value(),
            hsv_v=self.hsv_v_spin.value(),
            degrees=self.degrees_spin.value(),
            translate=self.translate_spin.value(),
            scale_min=self.scale_min_spin.value(),
            scale_max=self.scale_max_spin.value(),
            shear=self.shear_spin.value(),
            perspective=self.perspective_spin.value(),
            flipud=self.flipud_spin.value(),
            fliplr=self.fliplr_spin.value(),
            bgr=self.bgr_spin.value(),
            erasing=self.erasing_spin.value(),
            crop_fraction=self.crop_fraction_spin.value(),
            seed=self.seed_spin.value(),
        )

    def collect_config(self) -> ValidationConfig:
        """Build the validation configuration from the current form state.

        The concurrency of the two CPU bound stages is not collected:
        the page owns no control for it any more, so both fields keep
        the fixed value every run derives from this machine.
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
            augment_mode=self.mode_combo.currentData() or DEFAULT_AUGMENT_MODE,
            multiplier=self.multiplier_spin.value(),
            ratio=self.ratio_spin.value(),
            total_count=self.count_spin.value(),
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
    "SINGLE_IMAGE_ONLY_HINT",
    "TOOLTIP_GRAYSCALE_HINT",
    "ConfigPage",
]
