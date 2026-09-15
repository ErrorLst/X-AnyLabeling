"""Configuration page of the remote training sub window (spec §5.1.3).

Everything the submit needs is collected here: the server URL and its
Token (both editable, unlike the dataset paths), the strictly read only
dataset and classes boxes with their browse buttons, the task and model
pickers, the parameter form driven by `capabilities.param_schema`, the
split parameters with the seed box, the import / export of a training
configuration, the split preview table and the local pre-check summary.

Three rules shape this module:

- only the parameter keys the user actually set are part of a request or
  of an exported configuration (spec §3.8, §5.2.8) - hence the explicit
  `_explicit` bookkeeping instead of "read every widget";
- the parameter form never offers the server injected keys and never
  offers the logging / artifact recording group (spec §3.8.1, §3.8.4);
- two defaults are *values* now, not absences (what you see is what you
  send): `batch` is one fixed step of the four BATCH_CHOICES, and the
  `optimizer` combo starts on its `auto` entry whenever the selected
  family declares one.  Both therefore start explicit and reach the
  request body of an untouched form (spec §3.8.4, §5.2.2).  The optimizer
  policy entry (index 0) is still offered, and choosing it keeps the key
  out of the body - `_preset_selection` carries that choice through a
  rebuild (a late `capabilities` answer), where the widgets themselves
  do not survive.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from PyQt6 import QtCore, QtGui, QtWidgets

from ....services.auto_training.ultralytics.config import (
    DEFAULT_TRAINING_CONFIG,
)
from ..pipeline import PipelineConfig
from ..scanner import protocol_task
from ..splitter import SPLIT_STRATEGY, format_split_stats_lines
from .widgets import (
    STATUS_RED,
    STATUS_YELLOW,
    StatusRow,
    browse_directory,
    browse_file,
    readonly_path,
    wrap_with_button,
)

__all__ = [
    "BATCH_CHOICES",
    "FALLBACK_PARAM_SCHEMA",
    "PARAM_GROUPS",
    "PARAM_LABELS",
    "ConfigPage",
    "PARAM_SPECS",
]

# --------------------------------------------------------------------
# Parameter surface (spec §3.8.2): labels, groups and the local fallback
# --------------------------------------------------------------------

IMGSZ_CHOICES = (320, 416, 512, 640, 768, 896, 1024, 1280, 1536)
#: The four steps the `batch` drop down offers: a fixed, server
#: independent choice, never a free number any more.
BATCH_CHOICES = (8, 16, 32, 64)
#: Step pre-selected on a freshly built form; it is explicit like any
#: other choice of the four, so an untouched form does send it.
BATCH_DEFAULT = 16

PARAM_LABELS: Dict[str, str] = {
    "epochs": "训练轮数 epochs",
    "batch": "批大小 batch",
    "imgsz": "输入尺寸 imgsz",
    "workers": "数据加载线程 workers",
    "optimizer": "优化器 preset",
    "lr0": "初始学习率 lr0",
    "lrf": "最终学习率比例 lrf",
    "momentum": "动量 momentum",
    "weight_decay": "权重衰减 weight_decay",
    "warmup_epochs": "预热轮数 warmup_epochs",
    "warmup_momentum": "预热动量 warmup_momentum",
    "warmup_bias_lr": "预热偏置学习率 warmup_bias_lr",
    "cos_lr": "余弦学习率 cos_lr",
    "amp": "混合精度 amp",
    "cache": "缓存图片 cache",
    "rect": "矩形训练 rect",
    "single_cls": "单类别 single_cls",
    "patience": "早停耐心 patience",
    "close_mosaic": "关闭 Mosaic 的轮数 close_mosaic",
    "save_period": "快照周期 save_period",
    "fraction": "数据比例 fraction",
    "seed": "训练侧种子 seed",
    "dropout": "Dropout 比率 dropout",
    "hsv_h": "HSV-H 色相 hsv_h",
    "hsv_s": "HSV-S 饱和度 hsv_s",
    "hsv_v": "HSV-V 明度 hsv_v",
    "degrees": "旋转角度 degrees",
    "translate": "平移比例 translate",
    "scale": "缩放增益 scale",
    "shear": "错切角度 shear",
    "perspective": "透视变换 perspective",
    "flipud": "垂直翻转概率 flipud",
    "fliplr": "水平翻转概率 fliplr",
    "bgr": "BGR 通道互换概率 bgr",
    "mosaic": "Mosaic 增强概率 mosaic",
    "mixup": "MixUp 增强概率 mixup",
    "cutmix": "CutMix 增强概率 cutmix",
    "copy_paste": "Copy-Paste 概率 copy_paste",
    "copy_paste_mode": "Copy-Paste 模式 copy_paste_mode",
    "overlap_mask": "合并实例掩码 overlap_mask",
    "mask_ratio": "掩码下采样比例 mask_ratio",
}

#: Four visible groups (spec §5.1.3); the logging / artifact recording
#: keys are deliberately not part of any group.
PARAM_GROUPS: Sequence[Any] = (
    ("常用参数", ("epochs", "batch", "imgsz", "workers")),
    (
        "学习率与优化器",
        (
            "optimizer",
            "lr0",
            "lrf",
            "momentum",
            "weight_decay",
            "warmup_epochs",
            "warmup_momentum",
            "warmup_bias_lr",
            "cos_lr",
        ),
    ),
    (
        "数据增强与训练控制",
        (
            "amp",
            "cache",
            "rect",
            "close_mosaic",
            "hsv_h",
            "hsv_s",
            "hsv_v",
            "degrees",
            "translate",
            "scale",
            "shear",
            "perspective",
            "flipud",
            "fliplr",
            "bgr",
            "mosaic",
            "mixup",
            "cutmix",
            "copy_paste",
            "copy_paste_mode",
        ),
    ),
    (
        "训练控制与其它",
        (
            "single_cls",
            "patience",
            "save_period",
            "fraction",
            "seed",
            "dropout",
            "overlap_mask",
            "mask_ratio",
        ),
    ),
)

# --------------------------------------------------------------------
# Layout (presentation only: spec §5.1.3 fixes the fields, never their
# arrangement)
# --------------------------------------------------------------------

#: Deterministic lower bound of one parameter control, whatever growth
#: strategy the active style gives a form layout field.
PARAM_CONTROL_MIN_WIDTH = 240
#: Horizontal gap of the parameter grid: between a label and its
#: control as well as between the two columns.
PARAM_GRID_HSPACING = 12
#: Vertical gap of the parameter grid.
PARAM_GRID_VSPACING = 4
#: Groups that start folded, by group name (spec §5.1.3).  The two
#: groups a first run usually leaves untouched are the folded ones.
DEFAULT_COLLAPSED = {
    "常用参数": False,
    "学习率与优化器": False,
    "数据增强与训练控制": True,
    "训练控制与其它": True,
}
#: Constant hint of the parameter box: a submit sends the explicit keys
#: only (spec §3.8, §5.2.8).
PARAMS_HINT_TEMPLATE = "已显式设置 {0} 项（提交时才会发送）"
#: Badge of one group title: how many keys of that group are set.
GROUP_BADGE_TEMPLATE = "{0}（已设置 {1} 项）"
#: The split preview table never gets smaller than this (it used to
#: collapse to a 70 px strip at a 1000 px high window).
PREVIEW_TABLE_MIN_HEIGHT = 150

#: Width the vertical scroll bar of the content area reserves.  The
#: column count is decided on the page width plus this reserve, never on
#: the live viewport: a two column layout that overflows would show the
#: bar, shrink the viewport and flip to a single column, which raises the
#: content again and hides the bar - a cycle.
SCROLL_RESERVE_WIDTH = 16
#: `optimizer=auto` next to any of the seven hyper-parameters is a 422
#: (spec §3.8.4): the client mirrors the server's own conflict table.
AUTO_CONFLICTING_PARAMS = (
    "lr0",
    "lrf",
    "momentum",
    "weight_decay",
    "warmup_epochs",
    "warmup_momentum",
    "warmup_bias_lr",
)
#: The advisory line the parameter box shows for that conflict.
NOTICE_OPTIMIZER_AUTO = (
    "优化器已选 auto，与 lr0 / lrf / momentum / weight_decay / "
    "warmup_epochs / warmup_momentum / warmup_bias_lr 互斥"
    "（同送会 422 OPTIMIZER_UNSUPPORTED），请先改选具体 preset。"
)
#: Import notice of a `batch` outside the four steps.
BATCH_SNAP_TEMPLATE = "导入的 batch={0} 不在 8/16/32/64 内，已取 {1}"
#: Sentinel of `_preset_selection` before the user ever touched the
#: optimizer combo: the form starts on the `auto` entry then (the policy
#: entry once the family declares no `auto`).
_UNSET = object()

def _round_up(value: Any, step: int = 20) -> int:
    """Round one pixel amount up to the next multiple of `step`."""

    return -(-int(value) // step) * step


def _label_width(texts: Sequence[str]) -> int:
    """Return the width that lines up every control of a column.

    The application font is the one the controls are really drawn with,
    so the fixed label width follows a HiDPI setup instead of a hard
    coded number (the model validation page measures its grid the same
    way).  An import outside a GUI process has no font to measure: 0
    then keeps every caller on its own floor.
    """

    app = QtWidgets.QApplication.instance()
    if app is None:
        return 0
    metrics = QtGui.QFontMetrics(app.font())
    return max([metrics.horizontalAdvance(text) for text in texts] + [0])


#: Logical width below which the parameter form falls back to a single
#: column: the two column form needs both controls (2 * 240), the label
#: column of the left cell, two horizontal gaps (2 * 12) and the few
#: pixels the group box and the page margins take (40).  The floor is
#: 980, not the 730 the formula alone gives: the two column grid is
#: itself 964 px wide minimum (each control keeps its 240 px, spec
#: §5.1.3), and a switch below that could never be reached - the
#: window could not become narrow enough to earn the single column.
NARROW_PAGE_WIDTH = _round_up(
    max(
        980,
        2 * PARAM_CONTROL_MIN_WIDTH
        + _label_width(list(PARAM_LABELS.values()))
        + 2 * PARAM_GRID_HSPACING
        + 40,
    )
)


def _tight_form(box: QtWidgets.QWidget) -> QtWidgets.QFormLayout:
    """Create the compact form layout owned by a group box.

    Same numbers as the model validation page: an upper group shares
    its row with another one now, so its margins are not the place to
    spend width.
    """

    form = QtWidgets.QFormLayout(box)
    form.setContentsMargins(8, 6, 8, 6)
    form.setHorizontalSpacing(8)
    form.setVerticalSpacing(4)
    form.setLabelAlignment(
        QtCore.Qt.AlignmentFlag.AlignRight
        | QtCore.Qt.AlignmentFlag.AlignVCenter
    )
    return form


#: Local mirror of the range table (spec §3.8.2), used before a server
#: answered: (type, min, max, exclusive_min, exclusive_max).
PARAM_SPECS: Dict[str, Any] = {
    "epochs": ("int", 1, 1000, False, False),
    "batch": ("int", BATCH_CHOICES[0], BATCH_CHOICES[-1], False, False),
    "imgsz": ("enum", IMGSZ_CHOICES),
    "workers": ("int", 0, 16, False, False),
    "optimizer": ("preset",),
    "lr0": ("float", 0, 0.05, True, False),
    "lrf": ("float", 0, 1, True, False),
    "momentum": ("float", 0, 1, False, False),
    "weight_decay": ("float", 0, 1, False, False),
    "warmup_epochs": ("float", 0, None, False, False),
    "warmup_momentum": ("float", 0, None, False, False),
    "warmup_bias_lr": ("float", 0, None, False, False),
    "cos_lr": ("bool",),
    "amp": ("bool",),
    "cache": ("bool",),
    "rect": ("bool",),
    "single_cls": ("bool",),
    "patience": ("int", 0, 1000, False, False),
    "close_mosaic": ("int", 0, 1000, False, False),
    "save_period": ("int", 0, 1000, False, False),
    "fraction": ("float", 0, 1, True, False),
    "seed": ("int", None, None, False, False),
    "dropout": ("float", 0, 1, False, False),
    "hsv_h": ("float", 0, 1, False, False),
    "hsv_s": ("float", 0, 1, False, False),
    "hsv_v": ("float", 0, 1, False, False),
    "degrees": ("float", 0, 180, False, False),
    "translate": ("float", 0, 1, False, False),
    "scale": ("float", 0, 1, False, False),
    "shear": ("float", 0, 180, False, False),
    "perspective": ("float", 0, 0.001, False, False),
    "flipud": ("float", 0, 1, False, False),
    "fliplr": ("float", 0, 1, False, False),
    "bgr": ("float", 0, 1, False, False),
    "mosaic": ("float", 0, 1, False, False),
    "mixup": ("float", 0, 1, False, False),
    "cutmix": ("float", 0, 1, False, False),
    "copy_paste": ("float", 0, 1, False, False),
    "copy_paste_mode": ("enum", ("flip", "mixup")),
    "overlap_mask": ("bool",),
    "mask_ratio": ("int", 1, 16, False, False),
}

#: Display values of the keys the ultralytics default config declares but
#: `DEFAULT_TRAINING_CONFIG` does not carry.  They are the two backends'
#: shared defaults; `_make_param` reads them before the training config,
#: and nothing here ever forces a value into a request.
PARAM_DISPLAY_DEFAULTS: Dict[str, Any] = {
    "flipud": 0.0,
    "fliplr": 0.5,
    "bgr": 0.0,
    "mosaic": 1.0,
    "mixup": 0.0,
    "cutmix": 0.0,
    "copy_paste": 0.0,
    "copy_paste_mode": "flip",
    "overlap_mask": True,
    "mask_ratio": 4,
}

#: Shape of `capabilities.param_schema` in the absence of a server
#: (same two shapes the server sends: range object or enum list).
FALLBACK_PARAM_SCHEMA: Dict[str, Any] = {
    name: spec for name, spec in PARAM_SPECS.items()
}

#: Task choices of the configuration page (spec §1.2).
TASK_CHOICES = ("Detect", "Segment")

#: First entry of the preset combo: the server picks by preset_policy.
PRESET_UNSET_TEXT = "（服务端按默认策略选择）"
#: The same entry once the family default is known (spec §3.8.4).  It
#: names the family default as a hint only: `preset_policy` may still pick
#: another preset of the family (its iteration threshold), so the wording
#: must not promise a concrete value.
PRESET_UNSET_TEMPLATE = "（由服务端默认策略决定；家族默认 {0}）"

CLASSES_FILTER = "类别表 (*.txt);;所有文件 (*)"
CONFIG_FILTER = "训练配置 (*.json);;所有文件 (*)"
CONFIG_SCHEMA_VERSION = 1


def fallback_spec(name: str) -> Any:
    """Return the local shape of one parameter."""

    return FALLBACK_PARAM_SCHEMA.get(name)


def _schema_shape(schema: Mapping[str, Any], name: str) -> Any:
    """Normalise one `param_schema` entry to the local tuple shape."""

    entry = schema.get(name)
    if isinstance(entry, Mapping):
        kind = str(entry.get("type") or "")
        if kind == "preset":
            # `optimizer` values are the union of every family preset
            # (spec §3.6); they must not become an enum, the widget has to
            # filter them by the selected family (spec §3.6, §3.8.2).
            return ("preset",)
        if "values" in entry and isinstance(entry.get("values"), list):
            return ("enum", tuple(entry["values"]))
        kind = str(entry.get("type") or "float")
        if kind == "bool":
            return ("bool",)
        if kind == "int":
            kind = "int"
        else:
            kind = "float"
        return (
            kind,
            entry.get("min"),
            entry.get("max"),
            bool(entry.get("exclusive_min")),
            bool(entry.get("exclusive_max")),
        )
    if isinstance(entry, (list, tuple)):
        return ("enum", tuple(entry))
    return fallback_spec(name)


# --------------------------------------------------------------------
# Form state
# --------------------------------------------------------------------


@dataclass
class FormState:
    """The configuration page as plain values (spec §5.2.8 keys)."""

    server_url: str = ""
    api_key: str = ""
    dataset_dir: str = ""
    classes_file: str = ""
    task: str = TASK_CHOICES[0]
    model_family: str = ""
    model: str = ""
    val_ratio: float = 0.2
    seed: Optional[int] = None
    params: Dict[str, Any] = field(default_factory=dict)

    def export_values(self) -> Dict[str, Any]:
        """The form values the pipeline turns into an export document.

        The key set of the document itself belongs to
        `pipeline.export_config` / `splitter.export_config` (the single
        source of truth of spec §5.2.8); this only carries the raw form
        values, and never the Token.
        """

        return {
            "server_url": self.server_url,
            "dataset_dir": self.dataset_dir,
            "classes_file": self.classes_file,
            "task": self.task,
            "model_family": self.model_family,
            "model": self.model,
            "val_ratio": self.val_ratio,
            "seed": self.seed,
            "params": dict(self.params),
        }

    def pipeline_config(self) -> PipelineConfig:
        """The pipeline half of the form (spec §5.2.1 step 1)."""

        return PipelineConfig(
            dataset_dir=self.dataset_dir,
            classes_file=self.classes_file,
            task=self.task,
            val_ratio=float(self.val_ratio),
            seed=self.seed,
        )


class _ParamWidget:
    """One parameter control plus its explicitness bookkeeping."""

    def __init__(
        self,
        name: str,
        widget: QtWidgets.QWidget,
        explicit: bool,
        read: Callable[[], Any],
    ) -> None:
        self.name = name
        self.widget = widget
        self.explicit = explicit
        self.read = read

    def value(self) -> Any:
        return self.read()


class _ContentScroll(QtWidgets.QScrollArea):
    """The scroll area of the page content (spec §5.1.3 layout).

    `sizeHint` follows the natural height of the content: the
    `QScrollArea` default reports a viewport sized hint and the window
    size computed from the page hint would be wrong.  The viewport itself
    drives the column count, so the reflow also survives a scroll bar
    appearing or disappearing.
    """

    def __init__(self, content: QtWidgets.QWidget) -> None:
        super().__init__()
        self.setObjectName("trainingContentScroll")
        self.setWidgetResizable(True)
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.setWidget(content)
        self.viewport().installEventFilter(self)

    def eventFilter(self, watched: Any, event: Any) -> bool:
        """Schedule the page reflow when the viewport resizes.

        Only the coalesced entry is called: a viewport resize arrives
        once per pixel while a window edge is dragged, and a
        synchronous `_resize_page` here would re-run the full layout of
        all 41 controls that often.  The 0ms timer behind
        `_tick_layout` folds a burst into one reflow.
        """

        if watched is self.viewport() and event.type() in (
            QtCore.QEvent.Type.Resize,
            QtCore.QEvent.Type.Show,
        ):
            page = self.parent()
            if isinstance(page, ConfigPage):
                page._tick_layout()
        return super().eventFilter(watched, event)

    def sizeHint(self) -> QtCore.QSize:  # noqa: N802 (Qt override)
        content = self.widget()
        if content is not None:
            return content.sizeHint()
        return super().sizeHint()


class ConfigPage(QtWidgets.QWidget):
    """Collect the submit of one remote training task (spec §5.1.3)."""

    submit_requested = QtCore.pyqtSignal()
    precheck_requested = QtCore.pyqtSignal()
    connection_requested = QtCore.pyqtSignal()
    import_requested = QtCore.pyqtSignal()
    export_requested = QtCore.pyqtSignal()
    dataset_changed = QtCore.pyqtSignal(str)

    def __init__(self, parent: Optional[Any] = None) -> None:
        super().__init__(parent)
        self._params: Dict[str, _ParamWidget] = {}
        self._schema: Dict[str, Any] = dict(FALLBACK_PARAM_SCHEMA)
        self._presets: List[str] = []
        self._families: Dict[str, Any] = {}
        self._default_presets: Dict[str, str] = {}
        self._preview: Any = None
        # Latched advisories of the parameter box (import notices); the
        # optimizer conflict is recomputed on every refresh.
        self.notices: List[str] = []
        # The untouched-form sentinel of the optimizer combo: a late
        # capabilities answer rebuilds the widget, so the choice cannot
        # live on the widget alone.
        self._preset_selection: Any = _UNSET

        # --- presentation state (never part of the form semantics) -----
        # The folded groups, the current column count and the four
        # toggle buttons live on the instance: rebuild_params() destroys
        # and recreates every control, and neither the folding nor the
        # badge may be reset by a late capabilities answer.
        self._collapsed: Dict[str, bool] = dict(DEFAULT_COLLAPSED)
        self._param_columns = 2
        self._group_buttons: Dict[str, QtWidgets.QToolButton] = {}
        self._group_contents: Dict[str, QtWidgets.QWidget] = {}
        self._group_grids: Dict[str, QtWidgets.QGridLayout] = {}
        self._group_names: Dict[str, Sequence[str]] = {}
        self._param_labels: Dict[str, QtWidgets.QLabel] = {}
        self._label_width = _label_width(list(PARAM_LABELS.values()))
        self._badge_pending = False
        self._upper_stacked: Optional[bool] = None
        # The scrollable content and the four fixed button rows.
        self.content: QtWidgets.QWidget = None
        self.scroll: _ContentScroll = None
        self.buttons: QtWidgets.QWidget = None
        # One timer owned by the page: a pending refresh dies with it, a
        # static QTimer.singleShot would outlive a destroyed widget.
        self._badge_timer = QtCore.QTimer(self)
        self._badge_timer.setSingleShot(True)
        self._badge_timer.setInterval(0)
        self._badge_timer.timeout.connect(self._refresh_badges)
        # A second one for the reflow: the geometry a resize event
        # reports is the one of the *old* pass, so the columns and the
        # content height are settled one event loop turn later.
        self._layout_pending = False
        self._layout_timer = QtCore.QTimer(self)
        self._layout_timer.setSingleShot(True)
        self._layout_timer.setInterval(0)
        self._layout_timer.timeout.connect(self._refresh_layout)
        self._build()
        self.rebuild_params()
        self._refresh_badges()

    def _resize_page(self, page_width: int, page_height: int) -> None:
        """Reflow the page from the page geometry.

        The decision is taken on the *page* width, not on the live
        viewport width: the viewport loses the scroll bar exactly when
        a two column layout starts to overflow, and a decision made on
        that narrower number would flip to one column, grow taller and
        hide the bar again - a cycle.  The bar reserve is therefore
        counted on top of the page width, whether the bar is visible
        or not.  Only the coalesced `_refresh_layout` calls this.
        """

        del page_height
        stacked = page_width < NARROW_PAGE_WIDTH + SCROLL_RESERVE_WIDTH
        if stacked != self._upper_stacked:
            self._upper_stacked = stacked
            self._apply_upper_row(stacked)
        columns = 1 if stacked else 2
        if columns != self._param_columns:
            self._apply_param_columns(columns)
        self._sync_content_height()

    # ------------------------------------------------------------- build

    def _build(self) -> None:
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)

        # Everything except the button row scrolls: the four buttons
        # stay pinned below the scroll area, at every window height.
        shell = QtWidgets.QWidget()
        shell_layout = QtWidgets.QVBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(10)
        self.content = QtWidgets.QWidget()
        content_layout = QtWidgets.QVBoxLayout(self.content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(10)

        self.status_row = StatusRow()
        content_layout.addWidget(self.status_row)

        content_layout.addWidget(self._build_server_group())
        content_layout.addWidget(self._build_upper_row())
        content_layout.addWidget(self._build_params_group())
        # The preview keeps its natural height here: a stretch inside the
        # scrolled widget would fill the viewport and could hide the
        # scroll bar of a page whose natural height is larger.
        content_layout.addWidget(self._build_preview_group())

        self.scroll = _ContentScroll(self.content)
        shell_layout.addWidget(self.scroll, 1)
        self.buttons = self._build_buttons()
        shell_layout.addWidget(self.buttons)
        outer.addWidget(shell)

    def _build_server_group(self) -> QtWidgets.QWidget:
        """The connection fields, on one row (spec §5.1.3).

        A form layout would stack the address, the Token and the test
        button; two stretch columns put the address and the Token side
        by side and leave the button on the same line.  The widgets,
        their placeholders and the signal are unchanged.
        """

        box = QtWidgets.QGroupBox("服务端")
        grid = QtWidgets.QGridLayout(box)
        grid.setContentsMargins(8, 6, 8, 6)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(4)
        self.server_url_edit = QtWidgets.QLineEdit()
        self.server_url_edit.setPlaceholderText("http://10.0.0.5:8000")
        self.api_key_edit = QtWidgets.QLineEdit()
        self.api_key_edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText("Token")
        self.test_button = QtWidgets.QPushButton("测试连接")
        self.test_button.clicked.connect(self.connection_requested.emit)
        grid.addWidget(QtWidgets.QLabel("服务器地址"), 0, 0)
        grid.addWidget(self.server_url_edit, 0, 1)
        grid.addWidget(QtWidgets.QLabel("Token"), 0, 2)
        grid.addWidget(self.api_key_edit, 0, 3)
        grid.addWidget(self.test_button, 0, 4)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        self.health_label = QtWidgets.QLabel("")
        self.health_label.setWordWrap(True)
        grid.addWidget(self.health_label, 1, 1, 1, 4)
        return box

    def _build_upper_row(self) -> QtWidgets.QWidget:
        """The two short upper groups, sharing one row.

        The read only sources and the split / task pickers are 2 and 6
        rows high; side by side they cost the height of the taller one,
        which is what the parameter form below needs (spec §5.1.3).
        The fields themselves are untouched.
        """

        self.upper_row = QtWidgets.QWidget()
        self.upper_grid = QtWidgets.QGridLayout(self.upper_row)
        self.upper_grid.setContentsMargins(0, 0, 0, 0)
        self.upper_grid.setSpacing(10)
        self.paths_box = self._build_paths_group()
        self.split_box = self._build_split_group()
        self._upper_stacked = False
        self.upper_grid.addWidget(self.paths_box, 0, 0)
        self.upper_grid.setColumnStretch(0, 1)
        self.upper_grid.setColumnStretch(1, 2)
        self.upper_grid.addWidget(self.split_box, 0, 1)
        return self.upper_row

    def _apply_upper_row(self, stacked: bool) -> None:
        """Stack the two upper groups or put them back side by side."""

        if stacked:
            self.upper_grid.addWidget(self.split_box, 1, 0)
            self.upper_grid.setColumnStretch(0, 1)
            self.upper_grid.setColumnStretch(1, 0)
            return
        self.upper_grid.addWidget(self.split_box, 0, 1)
        self.upper_grid.setColumnStretch(0, 1)
        self.upper_grid.setColumnStretch(1, 2)

    def _build_paths_group(self) -> QtWidgets.QWidget:
        box = QtWidgets.QGroupBox("数据来源（只读）")
        form = _tight_form(box)
        self.dataset_edit = readonly_path(
            "选择数据来源目录（只读）", "数据集目录严格只读"
        )
        self.classes_edit = readonly_path(
            "选择类别表 classes.txt（只读）"
        )
        form.addRow(
            "数据集目录",
            wrap_with_button(
                self.dataset_edit, "浏览…", self.browse_dataset
            ),
        )
        form.addRow(
            "类别表",
            wrap_with_button(
                self.classes_edit, "浏览…", self.browse_classes
            ),
        )
        self.dataset_edit.textChanged.connect(self.dataset_changed.emit)
        return box

    def _build_split_group(self) -> QtWidgets.QWidget:
        box = QtWidgets.QGroupBox("划分与任务")
        form = _tight_form(box)
        self.task_combo = QtWidgets.QComboBox()
        for task in TASK_CHOICES:
            # data carries the protocol spelling, the label the UI one
            self.task_combo.addItem(_task_label(protocol_task(task)),
                                    protocol_task(task))
        self.model_family_combo = QtWidgets.QComboBox()
        self.model_combo = QtWidgets.QComboBox()
        self.task_combo.currentIndexChanged.connect(self._on_task_changed)
        self.model_family_combo.currentIndexChanged.connect(
            self._on_family_changed
        )
        self.val_ratio_spin = QtWidgets.QDoubleSpinBox()
        self.val_ratio_spin.setRange(0.01, 0.99)
        self.val_ratio_spin.setSingleStep(0.05)
        self.val_ratio_spin.setDecimals(2)
        self.val_ratio_spin.setValue(0.2)
        self.seed_edit = QtWidgets.QLineEdit()
        self.seed_edit.setPlaceholderText("留空则自动生成")
        form.addRow("任务类型", self.task_combo)
        form.addRow("模型家族", self.model_family_combo)
        form.addRow("权重", self.model_combo)
        form.addRow("val 占比 val_ratio", self.val_ratio_spin)
        form.addRow("随机种子 seed", self.seed_edit)
        self.split_strategy_label = QtWidgets.QLabel(SPLIT_STRATEGY)
        form.addRow("划分策略 split_strategy", self.split_strategy_label)
        return box

    def _build_params_group(self) -> QtWidgets.QWidget:
        """The parameter box: one tool row plus the four groups (§5.1.3).

        The tool row lives outside `params_layout`, which only holds
        the groups: `rebuild_params()` drops and rebuilds those, and the
        hint label with its two buttons has to survive that.
        """

        self.params_box = QtWidgets.QGroupBox(
            "训练参数（只发送显式设置过的键）"
        )
        outer = QtWidgets.QVBoxLayout(self.params_box)
        outer.setContentsMargins(6, 4, 6, 4)
        outer.setSpacing(2)
        tool = QtWidgets.QHBoxLayout()
        tool.setContentsMargins(0, 0, 0, 0)
        tool.setSpacing(8)
        self.params_hint_label = QtWidgets.QLabel("")
        tool.addWidget(self.params_hint_label)
        # The yellow advisory line, next to the hint: the batch snap note
        # of an import and the optimizer / hyper-parameter conflict.  It
        # never changes a value on its own.
        self.notice_label = QtWidgets.QLabel("")
        self.notice_label.setWordWrap(True)
        self.notice_label.setStyleSheet("color: #8a6d00;")
        tool.addWidget(self.notice_label)
        tool.addStretch(1)
        self.expand_all_button = QtWidgets.QPushButton("全部展开")
        self.collapse_all_button = QtWidgets.QPushButton("全部折叠")
        self.expand_all_button.clicked.connect(self._on_expand_all)
        self.collapse_all_button.clicked.connect(self._on_collapse_all)
        tool.addWidget(self.expand_all_button)
        tool.addWidget(self.collapse_all_button)
        outer.addLayout(tool)
        self.params_layout = QtWidgets.QVBoxLayout()
        self.params_layout.setContentsMargins(0, 0, 0, 0)
        self.params_layout.setSpacing(2)
        outer.addLayout(self.params_layout)
        return self.params_box

    def _build_preview_group(self) -> QtWidgets.QWidget:
        box = QtWidgets.QGroupBox("划分预览")
        layout = QtWidgets.QVBoxLayout(box)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)
        self.preview_table = QtWidgets.QTableWidget(0, 3)
        self.preview_table.setHorizontalHeaderLabels(
            ["类别", "train", "val"]
        )
        self.preview_table.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.preview_table.horizontalHeader().setStretchLastSection(True)
        # The table never shrinks below a readable height: the summary
        # and the status row below it are what gives way first, and the
        # splitter handle lets the user decide per session.
        self.preview_table.setMinimumHeight(PREVIEW_TABLE_MIN_HEIGHT)
        bottom = QtWidgets.QWidget()
        bottom_layout = QtWidgets.QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.setSpacing(4)
        self.preview_summary = QtWidgets.QLabel("")
        self.preview_summary.setWordWrap(True)
        bottom_layout.addWidget(self.preview_summary)
        self.preview_row = StatusRow()
        bottom_layout.addWidget(self.preview_row)
        self.preview_splitter = QtWidgets.QSplitter(
            QtCore.Qt.Orientation.Vertical
        )
        self.preview_splitter.setChildrenCollapsible(False)
        self.preview_splitter.addWidget(self.preview_table)
        self.preview_splitter.addWidget(bottom)
        self.preview_splitter.setStretchFactor(0, 1)
        self.preview_splitter.setStretchFactor(1, 0)
        self.preview_splitter.setSizes([340, 90])
        layout.addWidget(self.preview_splitter, 1)
        return box

    def _build_buttons(self) -> QtWidgets.QWidget:
        row = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        self.import_button = QtWidgets.QPushButton("导入配置")
        self.export_button = QtWidgets.QPushButton("导出配置")
        self.precheck_button = QtWidgets.QPushButton("参数预检")
        self.submit_button = QtWidgets.QPushButton("提交任务")
        self.import_button.clicked.connect(self.import_requested.emit)
        self.export_button.clicked.connect(self.export_requested.emit)
        self.precheck_button.clicked.connect(self.precheck_requested.emit)
        self.submit_button.clicked.connect(self.submit_requested.emit)
        layout.addWidget(self.import_button)
        layout.addWidget(self.export_button)
        layout.addStretch(1)
        layout.addWidget(self.precheck_button)
        layout.addWidget(self.submit_button)
        return row

    # ------------------------------------------------- param form build

    def set_capabilities(self, capabilities: Any) -> None:
        """Rebuild the parameter form from `capabilities` (spec §5.1.3).

        The task list, the model families with their weights and the
        presets all come from the server; the local table is only the
        fallback used before a server ever answered.  The parameters the
        user already set survive the rebuild (spec §5.2.8: a late
        capabilities answer must not drop what was set by hand or by an
        imported configuration).
        """

        payload = _payload_of(capabilities)
        self._schema = {
            name: _schema_shape(
                payload.get("param_schema") or {}, name
            )
            for name in PARAM_SPECS
        }
        self._presets = sorted(
            str(name) for name in (payload.get("optimizer_presets") or {})
        )
        self._families = {
            str(name): dict(entry)
            for name, entry in (payload.get("model_families") or {}).items()
            if isinstance(entry, Mapping)
        }
        default_preset = (payload.get("preset_policy") or {}).get(
            "default_preset"
        ) or {}
        self._default_presets = {
            str(name): str(value)
            for name, value in default_preset.items()
            if value
        }
        self.model_family_combo.blockSignals(True)
        self.model_family_combo.clear()
        self.model_family_combo.addItems(list(self._families))
        self.model_family_combo.blockSignals(False)
        self.task_combo.blockSignals(True)
        self.task_combo.clear()
        for task in payload.get("tasks") or TASK_CHOICES:
            self.task_combo.addItem(_task_label(str(task)), str(task))
        self.task_combo.blockSignals(False)
        # The rebuild comes first: the preset combos are built from the
        # family lists above, and the family switch runs afterwards on
        # the fresh widgets so that they start on the family's initial
        # entry (spec §3.8.4, §5.2.2).  `_preset_selection` is *not*
        # touched: the choice of the user (the policy entry included)
        # has to survive a late answer, and only `_fill_presets` reads
        # it.
        self.rebuild_params()
        self._on_family_changed()
        self._sync_content_height()

    def _refresh_models(self, payload: Optional[Mapping[str, Any]] = None
                        ) -> None:
        """Weights of the selected family **for the selected task**.

        model_families[family].weights is a task to file list mapping
        (spec §3.6), not a flat list: sorting the mapping itself would put
        the task names into the widget.  A family without a list for this
        task - or a server that only sends a flat list - falls back to the
        union of every task.
        """

        if payload is not None:
            families = {
                str(name): dict(entry)
                for name, entry in (
                    payload.get("model_families") or {}
                ).items()
                if isinstance(entry, Mapping)
            }
        else:
            families = self._families
        family = self.model_family_combo.currentText()
        weights = (families.get(family) or {}).get("weights") or {}
        models: List[str] = []
        if isinstance(weights, Mapping):
            task = protocol_task(self.current_task())
            models = [str(name) for name in (weights.get(task) or [])]
            if not models:
                for value in weights.values():
                    models.extend(str(name) for name in (value or []))
        elif isinstance(weights, (list, tuple)):
            models = [str(name) for name in weights]
        models = sorted(dict.fromkeys(models))
        previous = self.model_combo.currentText()
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        self.model_combo.addItems(models)
        if previous:
            # Keep the selection only when it is still valid for this
            # (family, task): re-adding it would offer a weight the server
            # does not accept for the task.
            index = self.model_combo.findText(previous)
            if index >= 0:
                self.model_combo.setCurrentIndex(index)
        self.model_combo.blockSignals(False)

    def current_task(self) -> str:
        """The task selected in the combo (either spelling)."""

        return str(
            self.task_combo.currentData() or self.task_combo.currentText()
        )

    def _on_task_changed(self, _index: int = -1) -> None:
        """A task change re-filters the weights of the family (spec §3.6)."""

        self._refresh_models()

    def _on_family_changed(self, _index: int = -1) -> None:
        """A family change re-filters weights and presets (§3.6, §3.8.4).

        The remembered choice of the user is never reset here, a
        programmatic refresh included: `_fill_presets` resolves it
        against the fresh list of the family now selected and only
        *falls back* to the initial value when that family does not
        offer it.  The fallback is a display / send level downgrade,
        the memory itself is kept, so going back to a family that does
        offer the value restores it (spec §3.8.4, §5.2.2).
        """

        self._refresh_models()
        entry = self._params.get("optimizer")
        if entry is not None:
            self._fill_presets(entry.widget)

    def _family_presets(self, family: str) -> List[str]:
        """Presets of one family, or the global union as a fallback."""

        presets = list(
            (self._families.get(family) or {}).get("presets") or []
        )
        if not presets and not self._families:
            presets = list(self._presets)
        return sorted(dict.fromkeys(str(name) for name in presets))

    def _initial_preset(self, family: str) -> Any:
        """The initial value rule of one family (spec §3.8.4).

        The family's `auto` preset when it declares that preset, the
        valueless server policy entry otherwise.
        """

        presets = self._family_presets(family)
        return "auto" if "auto" in presets else None

    def _preset_choice(self) -> Any:
        """The preset a freshly built combo has to show.

        `_UNSET` is the untouched form: the initial value rule of the
        selected family.  `None` is the user's explicit "let the server
        pick" and a string is his concrete choice.  Without this field a
        rebuild (a late `capabilities` answer) would fall back to the
        initial value and lose the choice of the user, because the
        widget carrying it does not survive the rebuild.
        """

        if self._preset_selection is _UNSET:
            family = self.model_family_combo.currentText()
            return self._initial_preset(family)
        return self._preset_selection


    def _fill_presets(self, widget: QtWidgets.QComboBox) -> None:
        """Fill one preset combo for the selected family (spec §3.6).

        The first entry still means "let the server pick by
        preset_policy" (spec §3.8.4): it carries no value at all and
        `_make_param` keeps it out of the request.  The entries that
        follow - `auto` included - are the real choices of the user, and
        a preset of another family is never offered: it would come back
        as 422 OPTIMIZER_UNSUPPORTED.

        The value shown comes from `_preset_choice()`, never from a
        `keep` argument: a value that does not exist in the freshly
        filled list (a preset of the family just left) falls back to the
        initial value rule (spec §5.2.2).
        """

        family = self.model_family_combo.currentText()
        presets = self._family_presets(family)
        default = self._default_presets.get(family)
        wanted = self._preset_choice()
        label = (
            PRESET_UNSET_TEMPLATE.format(default)
            if default in presets
            else PRESET_UNSET_TEXT
        )
        widget.blockSignals(True)
        widget.clear()
        widget.addItem(label, None)
        for preset in presets:
            widget.addItem(preset, preset)
        index = widget.findData(wanted)
        if index < 0:
            index = 0
        widget.setCurrentIndex(index)
        widget.blockSignals(False)
        entry = self._params.get("optimizer")
        if entry is not None and entry.widget is widget:
            # An entry carrying a value is a choice of the user (the
            # `auto` start included); the policy entry is not.
            entry.explicit = widget.currentData() is not None
            self._tick_badge()

    def explicit_params(self) -> Dict[str, Any]:
        """The parameters the user actually set, value by value."""

        values: Dict[str, Any] = {}
        for name, entry in self._params.items():
            if not entry.explicit:
                continue
            try:
                value = entry.value()
            except (ValueError, TypeError):
                continue
            if value is not None:
                values[name] = value
        return values

    def rebuild_params(self, keep: Optional[Mapping[str, Any]] = None) -> None:
        """(Re)create the four parameter groups from the current schema.

        The explicit parameters are carried over: a rebuild triggered by a
        late `capabilities` answer must not silently drop what the user
        (or an imported configuration) already set.  The carry is
        `explicit_params()`; the preset combo is the one control whose
        state is not a plain value, and `_preset_selection` carries it
        across the rebuild (spec §3.8.4, §5.2.2).
        """

        carried = dict(keep) if keep is not None else self.explicit_params()
        while self.params_layout.count():
            item = self.params_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._params = {}
        self._param_labels = {}
        self._group_buttons = {}
        self._group_contents = {}
        self._group_grids = {}
        self._group_names = {}
        for group, names in PARAM_GROUPS:
            self.params_layout.addWidget(
                self._build_param_group(group, names)
            )
        # The column count of the window is applied to the fresh
        # widgets; the folding state of the instance is what the new
        # toggles start from.  The preset combo gets its value from
        # `_preset_selection`, so a rebuild carries the choice the
        # destroyed widget used to hold.
        self._apply_param_columns(self._param_columns)
        entry = self._params.get("optimizer")
        if entry is not None:
            self._fill_presets(entry.widget)
        self._sync_content_height()
        self._tick_badge()
        if carried:
            self.set_params(carried)

    def _build_param_group(
        self, group: str, names: Sequence[str]
    ) -> QtWidgets.QWidget:
        """Build one collapsible group: a toggle title plus its grid.

        The folding state comes from the instance dictionary, which
        survives a rebuild, and the arrow is rendered by the toggle
        slot (the initial state goes through the same slot, so the
        dictionary, the visibility and the arrow cannot disagree).
        """

        box = QtWidgets.QGroupBox()
        layout = QtWidgets.QVBoxLayout(box)
        # Tight on purpose: four of these plus their titles are what
        # stands between the page and a 1180 px natural height.
        layout.setContentsMargins(6, 2, 6, 4)
        layout.setSpacing(1)
        toggle = self._build_group_toggle(group)
        toggle.setChecked(not self._collapsed.get(group, False))
        layout.addWidget(toggle, 0, QtCore.Qt.AlignmentFlag.AlignLeft)
        content = QtWidgets.QWidget()
        grid = self._param_grid(names)
        content.setLayout(grid)
        layout.addWidget(content)
        toggle.toggled.connect(
            lambda checked, name=group: self._on_group_toggled(
                name, bool(checked)
            )
        )
        self._group_buttons[group] = toggle
        self._group_contents[group] = content
        self._group_grids[group] = grid
        self._group_names[group] = tuple(names)
        self._on_group_toggled(group, bool(toggle.isChecked()))
        return box

    def _build_group_toggle(self, name: str) -> QtWidgets.QToolButton:
        """Create the collapsible title of one parameter group."""

        button = QtWidgets.QToolButton()
        button.setCheckable(True)
        button.setToolButtonStyle(
            QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        button.setAutoRaise(True)
        button.setText(name)
        return button

    def _content_height(self) -> int:
        """The height the content layout asks for right now.

        The minimum of the widget is dropped and the layout re-run first:
        both the fold state and the column count change the answer, and a
        cached `sizeHint` of the previous state would pin the widget to a
        height that no longer matches what it holds.
        """

        content = self.content
        layout = content.layout()
        if layout is None:
            return 0
        content.setMinimumHeight(0)
        layout.invalidate()
        layout.activate()
        return layout.sizeHint().height()

    def _sync_content_height(self) -> None:
        """Pin the real page height of the scrollable content.

        `QScrollArea` hands the viewport height to its widget when the
        widget is resizable, which would crush the parameter groups into
        their minimum layout.  The minimum height is therefore the height
        the layout itself asks for in the current column count and fold
        state; beyond that the scroll bar takes over (it is the answer to
        a window too small for the page, never a reason to squeeze the
        page).  The viewport resize event repeats this pass, which is
        what keeps the two in step while the window is dragged.
        """

        height = self._content_height()
        if height > 0:
            self.content.setMinimumHeight(height)

    def _on_group_toggled(self, name: str, checked: bool) -> None:
        """Fold / unfold one group; no parameter value is touched."""

        self._collapsed[name] = not checked
        content = self._group_contents.get(name)
        if content is not None:
            content.setVisible(checked)
        button = self._group_buttons.get(name)
        if button is not None:
            button.setArrowType(
                QtCore.Qt.ArrowType.DownArrow
                if checked
                else QtCore.Qt.ArrowType.RightArrow
            )
        self._sync_content_height()

    def _param_grid(self, names: Sequence[str]) -> QtWidgets.QGridLayout:
        """Create the controls of one group inside a fresh grid.

        The grid starts without a single cell filled: where each label
        and control pair sits is the business of the row sync, so a
        column count change can move the widgets without rebuilding one
        of them.
        """

        grid = QtWidgets.QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(PARAM_GRID_HSPACING)
        grid.setVerticalSpacing(PARAM_GRID_VSPACING)
        for name in names:
            entry = self._make_param(name)
            self._params[name] = entry
            # A minimum, never a fixed width: the column stretches and
            # the control fills whatever the active style gives the
            # cell, in every style (spec §5.1.3).
            entry.widget.setMinimumWidth(PARAM_CONTROL_MIN_WIDTH)
            label = QtWidgets.QLabel(PARAM_LABELS[name])
            label.setFixedWidth(self._label_width)
            self._param_labels[name] = label
        return grid

    def _apply_param_columns(self, columns: int) -> None:
        """Move the existing parameter widgets into one or two columns."""

        self._param_columns = int(columns)
        for group, _names in PARAM_GROUPS:
            self._sync_group_row(group)

    def _sync_group_row(self, group: str) -> None:
        """Re-place the widgets of one group (never rebuild them).

        Adding a widget to a grid cell moves it out of its old one, so
        this is the only operation the column switch needs: the
        parameter entries, their explicit flags and the keyboard focus
        all survive a resize.
        """

        grid = self._group_grids.get(group)
        names = self._group_names.get(group) or ()
        if grid is None or not names:
            return
        columns = 2 if self._param_columns > 1 else 1
        rows = -(-len(names) // columns)
        for index, name in enumerate(names):
            row = index % rows
            cell = index // rows
            grid.addWidget(self._param_labels[name], row, cell * 2)
            grid.addWidget(self._params[name].widget, row, cell * 2 + 1)
        for cell in range(2):
            grid.setColumnStretch(cell * 2, 0)
            grid.setColumnStretch(cell * 2 + 1, 1)
            grid.setColumnMinimumWidth(cell * 2, self._label_width)
            grid.setColumnMinimumWidth(
                cell * 2 + 1, PARAM_CONTROL_MIN_WIDTH
            )
        if columns < 2:
            # The empty right half must stop claiming its 240 px: the
            # minimumSizeHint of the page follows the grid, and a
            # narrow page has to stay narrower than the two column
            # form.
            grid.setColumnStretch(2, 0)
            grid.setColumnStretch(3, 0)
            grid.setColumnMinimumWidth(2, 0)
            grid.setColumnMinimumWidth(3, 0)

    def _on_expand_all(self) -> None:
        """Open every parameter group (the tool row button)."""

        self._set_all_collapsed(False)

    def _on_collapse_all(self) -> None:
        """Fold every parameter group (the tool row button)."""

        self._set_all_collapsed(True)

    def _set_all_collapsed(self, collapsed: bool) -> None:
        """Drive every toggle, so the toggle slot stays the only path."""

        for button in self._group_buttons.values():
            button.setChecked(not collapsed)

    def _tick_badge(self) -> None:
        """Schedule one badge refresh (debounced, never re-entrant)."""

        if self._badge_pending:
            return
        self._badge_pending = True
        self._badge_timer.start()

    def _tick_layout(self) -> None:
        """Schedule one reflow of the scroll area (never re-entrant)."""

        if self._layout_pending:
            return
        self._layout_pending = True
        self._layout_timer.start()

    def _refresh_layout(self) -> None:
        """Re-run the page reflow on the settled geometry."""

        self._layout_pending = False
        self._resize_page(self.width(), self.height())

    def _refresh_notices(self) -> None:
        """Render the two advisories of the parameter box.

        The optimizer conflict is recomputed from the form itself:
        `auto` next to any of the seven hyper-parameters is a 422 (spec
        §3.8.4), and the combo state is the only source of truth for it.
        The batch notice is *latched* by the import path and cleared as
        soon as the user picks a step himself.  This never touches an
        explicit flag: it is a rendering pass, exactly like
        `_refresh_badges` below.
        """

        messages: List[str] = []
        optimizer = self._params.get("optimizer")
        if (
            optimizer is not None
            and optimizer.explicit
            and optimizer.value() == "auto"
        ):
            conflicting = [
                name
                for name in AUTO_CONFLICTING_PARAMS
                if self._params.get(name) is not None
                and self._params[name].explicit
            ]
            if conflicting:
                messages.append(NOTICE_OPTIMIZER_AUTO)
        messages.extend(self.notices)
        self.notice_label.setText("\\n".join(messages))

    def _refresh_badges(self) -> None:
        """Render the "N set" hints of the box and of every group.

        A count only: this never writes an explicit flag, and it reads
        the value defensively (a half typed batch box raises on
        purpose, see the batch reader below).
        """

        self._badge_pending = False
        self._refresh_notices()
        counts = {group: 0 for group, _names in PARAM_GROUPS}
        total = 0
        for group, names in PARAM_GROUPS:
            for name in names:
                entry = self._params.get(name)
                if entry is None or not entry.explicit:
                    continue
                try:
                    value = entry.value()
                except (ValueError, TypeError):
                    value = None
                if value is not None:
                    counts[group] += 1
                    total += 1
        for group, button in self._group_buttons.items():
            button.setText(
                GROUP_BADGE_TEMPLATE.format(group, counts.get(group, 0))
            )
        self.params_hint_label.setText(PARAMS_HINT_TEMPLATE.format(total))

    def resizeEvent(self, event: Any) -> None:
        """Reflow the page: two columns wide, one column narrow.

        Only a real change of the geometry touches the layout: a drag
        of the window edge that keeps the same column count and the
        same stacked / side by side choice returns right away and never
        runs a relayout per pixel.  The scroll viewport runs the same
        reflow through its own event filter, because the width the
        parameter grid really gets is the viewport width.
        """

        super().resizeEvent(event)
        width = int(event.size().width())
        stacked = width < NARROW_PAGE_WIDTH + SCROLL_RESERVE_WIDTH
        if stacked != self._upper_stacked:
            self._upper_stacked = stacked
            self._apply_upper_row(stacked)
        columns = 1 if stacked else 2
        if columns != self._param_columns:
            self._apply_param_columns(columns)
        # Deferred: `event.size()` is already the new geometry, but the
        # viewport and the scroll bar are laid out one turn later, and
        # the content height has to follow *that* number.
        self._tick_layout()

    def _make_param(self, name: str) -> _ParamWidget:
        shape = self._schema.get(name) or fallback_spec(name)
        default = DEFAULT_TRAINING_CONFIG.get(name)
        if default is None:
            # Ten new keys are not part of the training config table;
            # their display value is the two backends' shared default.
            default = PARAM_DISPLAY_DEFAULTS.get(name)
        if name == "batch":
            # Tested before the shape dispatch on purpose: the four
            # steps stay a drop down even if a server ever declares
            # `batch` as an enum.
            return self._make_batch_param()
        if shape[0] == "bool":
            widget = QtWidgets.QCheckBox()
            widget.setChecked(bool(default))
            entry = _ParamWidget(
                name, widget, False, lambda w=widget: bool(w.isChecked())
            )
            widget.toggled.connect(
                lambda _value, e=entry: self._mark_explicit(e)
            )
            return entry
        if shape[0] == "enum":
            widget = QtWidgets.QComboBox()
            for value in shape[1]:
                widget.addItem(str(value), value)
            _select_value(widget, default)
            entry = _ParamWidget(
                name, widget, False, lambda w=widget: w.currentData()
            )
            widget.currentIndexChanged.connect(
                lambda _row, e=entry: self._mark_explicit(e)
            )
            return entry
        if shape[0] == "preset":
            widget = QtWidgets.QComboBox()
            entry = _ParamWidget(
                name, widget, False, lambda w=widget: w.currentData()
            )
            self._fill_presets(widget)
            # `auto` (or, without it, the policy entry) is the start value
            # of the form; a real preset value is explicit, so an
            # untouched form really sends `optimizer: auto`.
            entry.explicit = widget.currentData() is not None
            widget.currentIndexChanged.connect(
                lambda _row, e=entry: self._on_preset_changed(e)
            )
            return entry
        if shape[0] == "int":
            widget = QtWidgets.QSpinBox()
            _apply_range(widget, shape, default, int)
            widget.setSingleStep(1)
            entry = _ParamWidget(
                name, widget, False, lambda w=widget: int(w.value())
            )
            widget.valueChanged.connect(
                lambda _value, e=entry: self._mark_explicit(e)
            )
            return entry
        widget = QtWidgets.QDoubleSpinBox()
        _apply_range(widget, shape, default, float)
        entry = _ParamWidget(
            name, widget, False, lambda w=widget: float(w.value())
        )
        widget.valueChanged.connect(
            lambda _value, e=entry: self._mark_explicit(e)
        )
        return entry

    def _make_batch_param(self) -> _ParamWidget:
        """`batch`: one non editable drop down of four fixed steps.

        The default is the 16 step and it is explicit from the start
        (what you see is what you send): an untouched form sends
        `batch: 16` (spec §3.8.2, §5.2.2).
        """

        widget = QtWidgets.QComboBox()
        widget.setEditable(False)
        for value in BATCH_CHOICES:
            widget.addItem(str(value), value)
        _select_value(widget, BATCH_DEFAULT)

        entry = _ParamWidget(
            "batch", widget, False, lambda w=widget: _read_batch(w)
        )
        entry.explicit = True
        widget.currentIndexChanged.connect(
            lambda _row, e=entry: self._mark_explicit(e)
        )
        # Only a real user change clears the import notice; the
        # programmatic call of `_assign_param` has to keep it visible.
        widget.activated.connect(lambda _row: self._clear_batch_notice())
        return entry

    def _on_preset_changed(self, entry: _ParamWidget) -> None:
        """Remember and mark one optimizer choice of the user.

        The combo outlives nothing: every rebuild (a late
        `capabilities` answer) destroys it, so the choice has to be
        copied out here.  `_fill_presets` runs its own
        `setCurrentIndex` under `blockSignals`, so a programmatic fill
        never calls this and never overwrites the remembered choice.
        """

        self._preset_selection = entry.widget.currentData()
        self._mark_explicit(entry)

    def _mark_explicit(self, entry: _ParamWidget) -> None:
        entry.explicit = True
        self._tick_badge()

    # ------------------------------------------------------------- state

    def values(self) -> FormState:
        """Read the page into a `FormState` (only explicit params)."""

        params: Dict[str, Any] = {}
        for name, entry in self._params.items():
            if not entry.explicit:
                continue
            value = entry.value()
            if value is None:
                continue
            params[name] = value
        return FormState(
            server_url=self.server_url_edit.text().strip(),
            api_key=self.api_key_edit.text(),
            dataset_dir=self.dataset_edit.text().strip(),
            classes_file=self.classes_edit.text().strip(),
            task=(
                self.task_combo.currentData()
                or self.task_combo.currentText()
            ),
            model_family=self.model_family_combo.currentText(),
            model=self.model_combo.currentText(),
            val_ratio=float(self.val_ratio_spin.value()),
            seed=parse_seed(self.seed_edit.text()),
            params=params,
        )

    def set_values(self, values: Mapping[str, Any]) -> None:
        """Fill the form key by key; absent keys keep their value (§5.2.8)."""

        if "server_url" in values:
            self.server_url_edit.setText(str(values.get("server_url") or ""))
        if "api_key" in values:
            self.api_key_edit.setText(str(values.get("api_key") or ""))
        if "dataset_dir" in values:
            self.dataset_edit.setText(str(values.get("dataset_dir") or ""))
        if "classes_file" in values:
            self.classes_edit.setText(str(values.get("classes_file") or ""))
        if values.get("task"):
            self.set_task(str(values["task"]))
        if "model_family" in values:
            _select_text(
                self.model_family_combo, str(values.get("model_family") or "")
            )
            self._refresh_models()
        if "model" in values:
            # The imported value wins over the current list: it is the only
            # way an export / import round trip stays lossless when the
            # capabilities answer has not arrived yet (spec §5.2.8).  This
            # is the single place that may add an entry; _refresh_models()
            # never re-adds, so switching family or task still drops a
            # model that does not belong to the new (family, task).
            _select_text(self.model_combo, str(values.get("model") or ""))
        if "val_ratio" in values:
            try:
                self.val_ratio_spin.setValue(float(values["val_ratio"]))
            except (TypeError, ValueError):
                pass
        if "seed" in values:
            seed = values.get("seed")
            self.seed_edit.setText("" if seed is None else str(seed))
        params = values.get("params")
        if isinstance(params, Mapping):
            self.set_params(params)

    def set_params(self, params: Mapping[str, Any]) -> None:
        """Backfill the parameter form from an imported file (§5.2.8).

        A `batch` outside the four steps is snapped onto the closest of
        them and earns the yellow notice of the parameter box; the value
        is never silently kept and never silently dropped.
        """

        for name, value in params.items():
            entry = self._params.get(name)
            if entry is None:
                continue
            notice = _assign_param(entry.widget, name, value)
            if notice is not None:
                self.notices = [
                    kept
                    for kept in self.notices
                    if "batch" not in kept
                ]
                self.notices.append(notice)
            if name == "optimizer":
                # The *request* value is remembered, never the resolved
                # display: `rebuild_params` carries the explicit
                # parameters through this same method, and reading
                # `currentData()` back would overwrite the memory with
                # whatever the combo happens to show (spec §3.8.4).
                # `None` keeps its policy meaning.
                self._preset_selection = value
                widget = entry.widget
                if value is not None and widget.currentData() != value:
                    # The selected family does not offer that preset:
                    # show - and send - the policy entry, exactly like a
                    # family switch does, and keep the value in the
                    # memory for the family that does offer it.  This
                    # also keeps the rebuild carry honest: it transports
                    # `explicit_params()`, which no longer holds a stale
                    # display value that could clobber the memory.
                    widget.blockSignals(True)
                    widget.setCurrentIndex(0)
                    widget.blockSignals(False)
            entry.explicit = True
        self._tick_badge()

    def set_task(self, task: str) -> None:
        """Select a task by either spelling (spec §5.2.8 import)."""

        wanted = str(task or "").strip().lower()
        if not wanted:
            return
        for index in range(self.task_combo.count()):
            data = str(self.task_combo.itemData(index) or "").strip()
            text = self.task_combo.itemText(index).strip()
            if wanted in (data.lower(), text.lower()):
                self.task_combo.setCurrentIndex(index)
                return

    def current_pipeline_config(self) -> PipelineConfig:
        return self.values().pipeline_config()

    def seed_text(self) -> str:
        return self.seed_edit.text()

    def set_seed(self, seed: Any) -> None:
        """Backfill the seed box (spec §5.2.8 seed generation)."""

        self.seed_edit.setText("" if seed is None else str(int(seed)))

    def config_json(self, params: Mapping[str, Any]) -> str:
        """Serialise one export document; the Token never leaves."""

        return json.dumps(
            dict(params), ensure_ascii=False, indent=2, sort_keys=False
        )

    # ------------------------------------------------------------ render

    def set_status_lines(self, lines: Sequence[Any]) -> None:
        self.status_row.set_lines(lines)

    def set_status(self, text: str, error: bool = False) -> None:
        severity = STATUS_RED if error else STATUS_YELLOW
        self.status_row.set_lines([(severity, text)] if text else [])

    def set_health_text(self, text: str, severity: str) -> None:
        self.health_label.setText(text)
        self.health_label.setStyleSheet(
            "color: {0};".format(
                "#b00020" if severity == STATUS_RED else "#8a6d00"
            )
        )

    def set_preview(self, preview: Any) -> None:
        """Render the split preview (spec §5.2.8).

        The unresolved classes - the red highlight of the spec - are the
        classes `SplitPreview.unresolved` lists; the warnings of the
        preview become the yellow / information lines below the table.
        """

        self._preview = preview
        unresolved = set(getattr(preview, "unresolved", ()) or ())
        stats = getattr(preview, "split_stats", None) or {}
        self.preview_table.setRowCount(len(stats))
        for row, (name, counts) in enumerate(stats.items()):
            train = int((counts or {}).get("train", 0))
            val = int((counts or {}).get("val", 0))
            for column, text in enumerate((name, str(train), str(val))):
                item = QtWidgets.QTableWidgetItem(text)
                if name in unresolved and column == 0:
                    item.setBackground(QtCore.Qt.GlobalColor.red)
                    item.setToolTip("两侧代表无法保证")
                self.preview_table.setItem(row, column, item)
        self.preview_table.resizeColumnsToContents()
        lines = list(getattr(preview, "format_lines", lambda: [])() or [])
        self.preview_summary.setText(lines[0] if lines else "")
        # The per class counts are the table above and the unresolved
        # classes are the red status line of the dialog: only the warnings
        # of the preview belong on this yellow row (spec §5.2.8).
        self.preview_row.set_lines(
            [
                (STATUS_YELLOW, warning)
                for warning in getattr(preview, "warnings", ()) or ()
            ]
        )
        if not stats and not lines:
            self.clear_preview()

    def clear_preview(self) -> None:
        self._preview = None
        self.preview_table.setRowCount(0)
        self.preview_summary.setText("")
        self.preview_row.clear()

    def preview_raw_lines(self) -> List[str]:
        """The stats block of the preview (used by the pre-check)."""

        preview = self._preview
        stats = getattr(preview, "split_stats", None)
        if not stats:
            return []
        return format_split_stats_lines(stats)

    # ------------------------------------------------------------ browse

    def _clear_batch_notice(self) -> None:
        """A user picked a step himself: the import advisory is over."""

        self.notices = [
            notice
            for notice in self.notices
            if "batch" not in notice
        ]
        self._tick_badge()

    def browse_dataset(self) -> str:
        path = browse_directory(
            self, "选择数据来源目录（只读）", self.dataset_edit.text()
        )
        if path:
            self.dataset_edit.setText(path)
        return path

    def browse_classes(self) -> str:
        path = browse_file(
            self,
            "选择类别表 classes.txt",
            CLASSES_FILTER,
            self.classes_edit.text(),
        )
        if path:
            self.classes_edit.setText(path)
        return path


def parse_seed(text: Any) -> Optional[int]:
    """Parse the seed box; an empty box means "generate one" (§5.2.8)."""

    value = str(text or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError("seed 必须是整数") from exc


def _read_batch(widget: QtWidgets.QComboBox) -> int:
    """Read the batch drop down: one int of BATCH_CHOICES, never None.

    The combo is not editable any more, so there is no half typed text
    to parse and no auto spelling to fold: every item carries one of the
    four steps as its data (spec §3.8.2).
    """

    data = widget.currentData()
    if data is None:
        return BATCH_DEFAULT
    return int(data)


def _is_number(value: Any) -> bool:
    """True for the int / float a JSON document can carry.

    `bool` is a subclass of `int` in Python but never a batch value: a
    document spelling `true` has to snap to the default, not to 1.
    """

    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _snap_batch(value: Any) -> int:
    """Snap one imported `batch` onto the closest of the four steps.

    A whole number goes to the nearest step (a tie takes the smaller
    one); an outside-of-domain spelling - the historical -1 and the
    0 < ratio < 1 auto form - becomes the 16 default (spec §3.8.2).
    """

    if _is_number(value) and float(value) == int(value):
        number = int(value)
        if number in BATCH_CHOICES:
            return number
        if number >= 1:
            return min(
                BATCH_CHOICES,
                key=lambda choice: (abs(choice - number), choice),
            )
    return BATCH_DEFAULT


def _assign_param(widget: Any, name: str, value: Any) -> Optional[str]:
    """Backfill one control; returns the notice the value earned.

    Only `batch` can earn one: an imported value outside the four steps
    is snapped onto the closest of them (a tie takes the smaller) and
    the caller shows the yellow line.  The value is never silently kept
    as it was.
    """

    if isinstance(widget, QtWidgets.QCheckBox):
        widget.setChecked(bool(value))
        return None
    if isinstance(widget, QtWidgets.QComboBox):
        if name == "batch":
            snapped = _snap_batch(value)
            _select_value(widget, snapped)
            if snapped == value or (
                _is_number(value) and float(value) == snapped
            ):
                return None
            return BATCH_SNAP_TEMPLATE.format(value, snapped)
        _select_value(widget, value)
        return None
    if isinstance(widget, QtWidgets.QSpinBox):
        widget.setValue(int(value))
        return None
    if isinstance(widget, QtWidgets.QDoubleSpinBox):
        widget.setValue(float(value))
    return None


def _select_value(widget: QtWidgets.QComboBox, value: Any) -> None:
    index = widget.findData(value)
    if index < 0:
        index = widget.findText(str(value))
    if index >= 0:
        widget.setCurrentIndex(index)


def _select_text(widget: QtWidgets.QComboBox, text: str) -> None:
    """Select a value, adding it when the list does not hold it yet.

    Only the import path of the configuration page uses this: a
    configuration file is authoritative for its own value, while the
    capabilities driven refresh (`_refresh_models`) never adds.
    """

    if not text:
        return
    index = widget.findText(text)
    if index < 0:
        widget.addItem(text)
        index = widget.count() - 1
    widget.setCurrentIndex(index)


def _apply_range(
    widget: Any, shape: Any, default: Any, caster: Any
) -> None:
    """Apply the local fallback range to one numeric control.

    Both the normalised tuple and the raw `param_schema` mapping are
    accepted, so the caller can hand in whatever `self._schema` holds.
    A float control steps by a hundredth of its own span: without that a
    single arrow click moves `perspective` from 0 to its 0.001 ceiling
    at once.
    """

    if isinstance(shape, Mapping):
        low = shape.get("min")
        high = shape.get("max")
    else:
        low = shape[1] if len(shape) > 1 else None
        high = shape[2] if len(shape) > 2 else None
    if shape[0] == "float":
        widget.setDecimals(5)
        if low is not None and high is not None:
            span = float(high) - float(low)
            widget.setSingleStep(
                max(1e-05, span / 100) if span > 0 else 0.001
            )
        else:
            widget.setSingleStep(0.001)
    if low is not None:
        widget.setMinimum(caster(low))
    else:
        widget.setMinimum(-(2**31) if caster is int else -1e9)
    if high is not None:
        widget.setMaximum(caster(high))
    else:
        widget.setMaximum(2**31 - 1 if caster is int else 1e9)
    if default is not None:
        widget.setValue(caster(default))


def _payload_of(capabilities: Any) -> Mapping[str, Any]:
    payload = getattr(capabilities, "payload", None)
    if isinstance(payload, Mapping):
        return payload
    if isinstance(capabilities, Mapping):
        return capabilities
    return {}


def _task_label(task: str) -> str:
    if not task:
        return task
    return task[0].upper() + task[1:]
