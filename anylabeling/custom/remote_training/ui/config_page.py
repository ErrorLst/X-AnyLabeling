"""Configuration page of the remote training sub window (spec §5.1.3).

Everything the submit needs is collected here: the server URL and its
Token (both editable, unlike the dataset paths), the strictly read only
dataset and classes boxes with their browse buttons, the task and model
pickers, the parameter form driven by `capabilities.param_schema`, the
split parameters with the seed box, the import / export of a training
configuration, the split preview table and the local pre-check summary.

Two rules of the specification shape this module:

- only the parameter keys the user actually set are part of a request or
  of an exported configuration (spec §3.8, §5.2.8) - hence the explicit
  `_explicit` bookkeeping instead of "read every widget";
- the parameter form never offers the server injected keys and never
  offers the logging / artifact recording group (spec §3.8.1, §3.8.4);
  `optimizer` is a preset drop down whose initial value is the first
  entry (no value at all) and therefore never a choice of the user: an
  untouched form sends no `optimizer` key and the server decides by its
  own policy (spec §3.8.4, §5.2.2).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from PyQt6 import QtCore, QtWidgets

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
    "BATCH_AUTO_RATIO",
    "BATCH_AUTO_VRAM",
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
#: The two auto spellings of `batch` (spec §3.8.2).
BATCH_AUTO_VRAM = -1
BATCH_AUTO_RATIO = 0.6
BATCH_UNSET_TEXT = "不设置（服务端默认）"
BATCH_AUTO_TEXT = "自动（按显存）"
BATCH_RATIO_TEXT = "自动（按比例）"
BATCH_MIN = 1
BATCH_MAX = 128

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
    ("数据增强与训练控制", ("amp", "cache", "rect", "close_mosaic")),
    (
        "训练控制与其它",
        (
            "single_cls",
            "patience",
            "save_period",
            "fraction",
            "seed",
            "dropout",
        ),
    ),
)

#: Local mirror of the range table (spec §3.8.2), used before a server
#: answered: (type, min, max, exclusive_min, exclusive_max).
PARAM_SPECS: Dict[str, Any] = {
    "epochs": ("int", 1, 1000, False, False),
    "batch": ("int", BATCH_MIN, BATCH_MAX, False, False),
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
        self._build()
        self.rebuild_params()

    # ------------------------------------------------------------- build

    def _build(self) -> None:
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)

        self.status_row = StatusRow()
        outer.addWidget(self.status_row)

        outer.addWidget(self._build_server_group())
        outer.addWidget(self._build_paths_group())
        outer.addWidget(self._build_split_group())
        outer.addWidget(self._build_params_group())
        outer.addWidget(self._build_preview_group(), 1)
        outer.addWidget(self._build_buttons())

    def _build_server_group(self) -> QtWidgets.QWidget:
        box = QtWidgets.QGroupBox("服务端")
        form = QtWidgets.QFormLayout(box)
        self.server_url_edit = QtWidgets.QLineEdit()
        self.server_url_edit.setPlaceholderText("http://10.0.0.5:8000")
        self.api_key_edit = QtWidgets.QLineEdit()
        self.api_key_edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText("Token")
        self.test_button = QtWidgets.QPushButton("测试连接")
        self.test_button.clicked.connect(self.connection_requested.emit)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.api_key_edit, 1)
        row.addWidget(self.test_button)
        form.addRow("服务器地址", self.server_url_edit)
        form.addRow("Token", row)
        self.health_label = QtWidgets.QLabel("")
        self.health_label.setWordWrap(True)
        form.addRow("", self.health_label)
        return box

    def _build_paths_group(self) -> QtWidgets.QWidget:
        box = QtWidgets.QGroupBox("数据来源（只读）")
        form = QtWidgets.QFormLayout(box)
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
        form = QtWidgets.QFormLayout(box)
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
        self.params_box = QtWidgets.QGroupBox("训练参数（只发送显式设置过的键）")
        self.params_layout = QtWidgets.QVBoxLayout(self.params_box)
        return self.params_box

    def _build_preview_group(self) -> QtWidgets.QWidget:
        box = QtWidgets.QGroupBox("划分预览")
        layout = QtWidgets.QVBoxLayout(box)
        self.preview_table = QtWidgets.QTableWidget(0, 3)
        self.preview_table.setHorizontalHeaderLabels(
            ["类别", "train", "val"]
        )
        self.preview_table.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.preview_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.preview_table, 1)
        self.preview_summary = QtWidgets.QLabel("")
        self.preview_summary.setWordWrap(True)
        layout.addWidget(self.preview_summary)
        self.preview_row = StatusRow()
        layout.addWidget(self.preview_row)
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
        # family lists above, and the family switch runs afterwards on the
        # fresh widgets so that they start on the server policy entry
        # (spec §3.8.4, §5.2.2).
        self.rebuild_params()
        self._on_family_changed()

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
        """A family change re-filters weights and presets (§3.6, §3.8.4)."""

        self._refresh_models()
        entry = self._params.get("optimizer")
        if entry is not None:
            # Only a real preset choice of the old family is carried over;
            # the first entry (None) falls back to the first entry of the
            # new family, which is the server policy entry again (spec
            # §3.8.4, §5.2.8).
            self._fill_presets(
                entry.widget, entry.widget.currentData(), explicit=True
            )

    def _family_presets(self, family: str) -> List[str]:
        """Presets of one family, or the global union as a fallback."""

        presets = list(
            (self._families.get(family) or {}).get("presets") or []
        )
        if not presets and not self._families:
            presets = list(self._presets)
        return sorted(dict.fromkeys(str(name) for name in presets))

    def _fill_presets(
        self,
        widget: QtWidgets.QComboBox,
        keep: Any = None,
        explicit: bool = False,
    ) -> None:
        """Fill one preset combo for the selected family (spec §3.6).

        The first entry means "let the server pick by preset_policy"
        (spec §3.8.4): it carries no value at all and is never counted as
        a choice of the user, so an untouched form sends no `optimizer`
        key and the server decides by its own policy (spec §5.2.2).  The
        remaining entries - `auto` included - are the real choices of
        the user.  A preset of another family is never offered: it would
        come back as 422 OPTIMIZER_UNSUPPORTED.

        `keep` selects that value again (the family switch path); only a
        real preset of the selected family survives the refill, the first
        entry always means "server policy".  The index is looked up in
        the freshly filled list, so the rule holds per selected family, a
        switch included (spec §5.2.2).
        """

        family = self.model_family_combo.currentText()
        presets = self._family_presets(family)
        default = self._default_presets.get(family)
        # The target *value* first, the index only after the items are in
        # place: the item list still belongs to the family just left (or
        # is empty on a freshly made combo), so an index resolved now
        # would be stale and silently land on another entry.
        wanted = keep if keep in presets else None
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
            # The first entry never counts as a choice of the user; it is
            # the server policy default and stays out of the request.
            entry.explicit = bool(explicit and index > 0)

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
        `explicit_params()`: the only valueless explicit parameter was
        the preset combo's first entry, and that entry no longer counts as
        a choice of the user (spec §3.8.4, §5.2.2).
        """

        carried = dict(keep) if keep is not None else self.explicit_params()
        while self.params_layout.count():
            item = self.params_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._params = {}
        for group, names in PARAM_GROUPS:
            box = QtWidgets.QGroupBox(group)
            form = QtWidgets.QFormLayout(box)
            for name in names:
                entry = self._make_param(name)
                self._params[name] = entry
                form.addRow(PARAM_LABELS[name], entry.widget)
            self.params_layout.addWidget(box)
        if carried:
            self.set_params(carried)

    def _make_param(self, name: str) -> _ParamWidget:
        shape = self._schema.get(name) or fallback_spec(name)
        default = DEFAULT_TRAINING_CONFIG.get(name)
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
            self._fill_presets(widget)
            entry = _ParamWidget(
                name, widget, False, lambda w=widget: w.currentData()
            )
            # The initial value is the first entry, which carries no
            # value at all (spec §3.8.4): it hands the choice to the
            # server policy, so it never counts as a choice of the user
            # and an untouched form sends no `optimizer` key (spec
            # §5.2.2).  Only a later user (or import) selection marks the
            # entry explicit.
            entry.explicit = False
            widget.currentIndexChanged.connect(
                lambda _row, e=entry: self._mark_explicit(e)
            )
            return entry
        if name == "batch":
            return self._make_batch_param()
        if shape[0] == "int":
            widget = QtWidgets.QSpinBox()
            _apply_range(widget, shape, default, int)
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
        """`batch` with its two auto spellings (spec §3.8.2)."""

        widget = QtWidgets.QComboBox()
        widget.setEditable(True)
        widget.addItem(BATCH_UNSET_TEXT, None)
        widget.addItem(BATCH_AUTO_TEXT, BATCH_AUTO_VRAM)
        widget.addItem(BATCH_RATIO_TEXT, BATCH_AUTO_RATIO)
        widget.setCurrentIndex(0)

        def read() -> Any:
            return _read_batch(widget)

        entry = _ParamWidget("batch", widget, False, read)
        widget.currentIndexChanged.connect(
            lambda _row, e=entry: self._mark_explicit(e)
        )
        line = widget.lineEdit()
        if line is not None:
            line.textEdited.connect(
                lambda _text, e=entry: self._mark_explicit(e)
            )
        return entry

    def _mark_explicit(self, entry: _ParamWidget) -> None:
        entry.explicit = True

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
        """Backfill the parameter form from an imported file (§5.2.8)."""

        for name, value in params.items():
            entry = self._params.get(name)
            if entry is None:
                continue
            _assign_param(entry.widget, name, value)
            entry.explicit = True

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


def _read_batch(widget: QtWidgets.QComboBox) -> Any:
    """Parse the batch control into `None` / int / auto value."""

    index = widget.currentIndex()
    if index >= 0:
        data = widget.itemData(index)
        if data is not None and widget.currentText() in (
            BATCH_UNSET_TEXT,
            BATCH_AUTO_TEXT,
            BATCH_RATIO_TEXT,
        ):
            return None if data is None else data
    text = widget.currentText().strip()
    if not text or text == BATCH_UNSET_TEXT:
        return None
    if text == BATCH_AUTO_TEXT:
        return BATCH_AUTO_VRAM
    if text == BATCH_RATIO_TEXT:
        return BATCH_AUTO_RATIO
    try:
        number = float(text)
    except ValueError as exc:
        raise ValueError("batch 必须是整数或 auto 取值") from exc
    if number < 0:
        return BATCH_AUTO_VRAM
    if 0 < number < 1:
        return number
    return int(number)


def _assign_param(widget: Any, name: str, value: Any) -> None:
    if isinstance(widget, QtWidgets.QCheckBox):
        widget.setChecked(bool(value))
        return
    if isinstance(widget, QtWidgets.QComboBox):
        if name == "batch":
            if value is None:
                widget.setCurrentIndex(0)
            elif value == BATCH_AUTO_VRAM or (
                isinstance(value, (int, float)) and value == -1
            ):
                widget.setCurrentIndex(1)
            elif isinstance(value, float) and 0 < value < 1:
                widget.setCurrentIndex(2)
            else:
                widget.setEditText(str(value))
            return
        _select_value(widget, value)
        return
    if isinstance(widget, QtWidgets.QSpinBox):
        widget.setValue(int(value))
        return
    if isinstance(widget, QtWidgets.QDoubleSpinBox):
        widget.setValue(float(value))


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
    if shape[0] == "float":
        widget.setDecimals(5)
    low = shape[1] if len(shape) > 1 else None
    high = shape[2] if len(shape) > 2 else None
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
