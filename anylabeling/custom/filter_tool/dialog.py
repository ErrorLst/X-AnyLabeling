"""The filter window: an ordered filter chain and a live preview.

The window shows two read-only image panes on the left, the original
image of the canvas and the preview of the working chain, and the
ordered chain on the right. The chain is a copy of the display chain:
cancel discards it, confirm hands it to the controller, which redraws
the canvas and applies the same chain to every image opened later.

The list order is the execution order. Every row has a checkbox that
keeps the entry in the list but skips it while unchecked. The window
never touches the image data of the labeling widget, the label file or
anything on disk; it only builds display pixmaps.
"""

from __future__ import annotations

import os.path as osp

from PyQt6 import QtCore, QtGui, QtWidgets

from . import core
from .display import (
    FilterDisplayController,
    qimage_to_rgba,
    rgba_to_qimage,
)
from .viewer import ImageView

__all__ = [
    "FilterDialog",
    "PREVIEW_DEBOUNCE_MS",
    "WINDOW_SIZE",
    "WINDOW_TITLE",
]

WINDOW_TITLE = "滤镜工具"
WINDOW_SIZE = (1240, 820)
MIN_WINDOW_SIZE = (940, 580)
PREVIEW_DEBOUNCE_MS = 120
RIGHT_MIN_WIDTH = 360

HINT_TEXT = (
    "列表从上到下依次执行；勾选控制启用；确认后当前图与之后打开的"
    "所有图片都会应用。只改变显示，不修改图片与标注文件。"
)
EMPTY_LIST_TEXT = "无滤镜（显示原图）"
NO_SOURCE_TEXT = "主窗口没有打开图片"
SELECT_HINT = "在右侧列表中选择一个滤镜后可编辑参数"
SOURCE_FORMAT = "当前图片：%s"
SOURCE_NONE = "当前图片：未打开"
STATUS_FORMAT = "%s · 滤镜：%s"
STATUS_COUNT = "%d 个（生效 %d 个）"
STATUS_NONE = "无"
STATUS_ERROR_FORMAT = "%s · 错误：%s"
APPLIED_FORMAT = "滤镜已应用：%d 个（仅影响显示）"
CLEARED_TEXT = "滤镜已关闭（显示原图）"


class FilterDialog(QtWidgets.QDialog):
    """Non modal editor of one display filter chain."""

    def __init__(self, widget, controller=None, parent=None):
        parent_widget = parent
        if parent_widget is None and isinstance(
            widget, QtWidgets.QWidget
        ):
            parent_widget = widget
        super().__init__(parent_widget)
        self.setWindowFlag(QtCore.Qt.WindowType.Window, True)
        self.setWindowTitle(WINDOW_TITLE)
        self.setModal(False)
        self.resize(*WINDOW_SIZE)
        self.setMinimumSize(*MIN_WINDOW_SIZE)
        self.setSizeGripEnabled(True)

        self.widget = widget
        if controller is None:
            controller = getattr(widget, "_filter_tool_controller", None)
        if controller is None:
            controller = FilterDisplayController(widget)
            if widget is not None:
                widget._filter_tool_controller = controller
        self.controller = controller
        self._specs = controller.chain()
        self._source_rgb = None
        self._source_alpha = None
        self._source_dpr = 1.0
        self._source_name = ""
        self._original_pixmap = None
        self._status_error = ""
        self._updating_list = False
        self._binding_params = False

        self._preview_timer = QtCore.QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(PREVIEW_DEBOUNCE_MS)
        self._preview_timer.timeout.connect(self._refresh_preview)

        self._build_ui()
        self.reload()

    # ------------------------------------------------------------- build

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 8)
        root.setSpacing(8)

        self.splitter = QtWidgets.QSplitter(
            QtCore.Qt.Orientation.Horizontal
        )
        self.splitter.addWidget(self._build_left())
        self.splitter.addWidget(self._build_right())
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([820, RIGHT_MIN_WIDTH])
        root.addWidget(self.splitter, 1)

        self.status_label = QtWidgets.QLabel(SOURCE_NONE)
        self.status_label.setWordWrap(True)
        self.status_label.setMinimumHeight(32)
        root.addWidget(self.status_label)

    def _build_left(self):
        box = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(6)

        header = QtWidgets.QHBoxLayout()
        self.source_label = QtWidgets.QLabel(SOURCE_NONE)
        self.source_label.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        header.addWidget(self.source_label, 1)
        self.refresh_button = QtWidgets.QPushButton("刷新")
        self.refresh_button.setAutoDefault(False)
        self.refresh_button.setToolTip(
            "重新读取主窗口当前显示的图片与像素"
        )
        self.refresh_button.clicked.connect(self.refresh_source)
        header.addWidget(self.refresh_button)
        layout.addLayout(header)

        panes = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        self.original_view = ImageView(NO_SOURCE_TEXT)
        self.preview_view = ImageView(EMPTY_LIST_TEXT)
        self.original_view.sync_with(self.preview_view)
        panes.addWidget(self._pane("原始图像", self.original_view))
        panes.addWidget(self._pane("滤镜预览", self.preview_view))
        panes.setStretchFactor(0, 1)
        panes.setStretchFactor(1, 1)
        layout.addWidget(panes, 1)
        return box

    def _pane(self, title, view):
        box = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        label = QtWidgets.QLabel(title)
        font = label.font()
        font.setBold(True)
        label.setFont(font)
        layout.addWidget(label)
        layout.addWidget(view, 1)
        return box

    def _build_right(self):
        box = QtWidgets.QWidget()
        box.setMinimumWidth(RIGHT_MIN_WIDTH)
        layout = QtWidgets.QVBoxLayout(box)
        layout.setContentsMargins(8, 0, 0, 0)
        layout.setSpacing(6)

        hint = QtWidgets.QLabel(HINT_TEXT)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.filter_list = QtWidgets.QListWidget()
        self.filter_list.setMinimumHeight(180)
        self.filter_list.itemChanged.connect(self._on_item_changed)
        self.filter_list.currentRowChanged.connect(
            self._on_current_row_changed
        )
        layout.addWidget(self.filter_list, 1)

        buttons = QtWidgets.QHBoxLayout()
        self.add_button = QtWidgets.QToolButton()
        self.add_button.setText("添加滤镜")
        self.add_button.setPopupMode(
            QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup
        )
        add_menu = QtWidgets.QMenu(self.add_button)
        for kind in (
            core.KIND_PIXEL,
            core.KIND_EQUALIZE,
            core.KIND_GAUSSIAN,
        ):
            action = add_menu.addAction(core.KIND_LABELS[kind])
            action.triggered.connect(
                lambda _checked=False, value=kind: self.add_filter(value)
            )
        self.add_button.setMenu(add_menu)
        buttons.addWidget(self.add_button)

        self.remove_button = QtWidgets.QPushButton("删除")
        self.up_button = QtWidgets.QPushButton("上移")
        self.down_button = QtWidgets.QPushButton("下移")
        self.clear_button = QtWidgets.QPushButton("清空")
        for button in (
            self.remove_button,
            self.up_button,
            self.down_button,
            self.clear_button,
        ):
            button.setAutoDefault(False)
        self.remove_button.clicked.connect(self.remove_current)
        self.up_button.clicked.connect(lambda: self.move_current(-1))
        self.down_button.clicked.connect(lambda: self.move_current(1))
        self.clear_button.clicked.connect(self.clear_filters)
        buttons.addWidget(self.remove_button)
        buttons.addWidget(self.up_button)
        buttons.addWidget(self.down_button)
        buttons.addWidget(self.clear_button)
        layout.addLayout(buttons)

        self.param_stack = QtWidgets.QStackedWidget()
        self.param_stack.setMinimumHeight(170)
        self._build_param_pages()
        layout.addWidget(self.param_stack)

        layout.addStretch(1)
        footer = QtWidgets.QHBoxLayout()
        footer.addStretch(1)
        self.cancel_button = QtWidgets.QPushButton("取消")
        self.confirm_button = QtWidgets.QPushButton("确认")
        self.cancel_button.setAutoDefault(False)
        self.confirm_button.setAutoDefault(False)
        self.cancel_button.clicked.connect(self.cancel)
        self.confirm_button.clicked.connect(self.confirm)
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.confirm_button)
        layout.addLayout(footer)
        self._update_buttons()
        return box

    def _build_param_pages(self):
        placeholder = QtWidgets.QWidget()
        placeholder_layout = QtWidgets.QVBoxLayout(placeholder)
        placeholder_layout.addWidget(QtWidgets.QLabel(SELECT_HINT))
        placeholder_layout.addStretch(1)
        self.param_stack.addWidget(placeholder)

        self.param_stack.addWidget(self._build_pixel_page())
        self.param_stack.addWidget(self._build_equalize_page())
        self.param_stack.addWidget(self._build_gaussian_page())
        self._page_index = {
            core.KIND_PIXEL: 1,
            core.KIND_EQUALIZE: 2,
            core.KIND_GAUSSIAN: 3,
        }
        self.param_stack.setCurrentIndex(0)

    def _spin(self, low, high, value=0, step=1):
        spin = QtWidgets.QSpinBox()
        spin.setRange(low, high)
        spin.setValue(value)
        spin.setSingleStep(step)
        spin.setKeyboardTracking(False)
        return spin

    def _build_pixel_page(self):
        page = QtWidgets.QWidget()
        layout = QtWidgets.QFormLayout(page)
        self.pixel_low = self._spin(0, 255, 0)
        self.pixel_high = self._spin(0, 255, 255)
        self.pixel_fill = self._spin(0, 255, 0)
        self.pixel_low.setToolTip("保留的下界（含）")
        self.pixel_high.setToolTip("保留的上界（含）")
        self.pixel_fill.setToolTip(
            "上下界之外的通道值替换成这个值（默认 0）"
        )
        layout.addRow("下限", self.pixel_low)
        layout.addRow("上限", self.pixel_high)
        layout.addRow("填充值", self.pixel_fill)
        note = QtWidgets.QLabel(
            "逐通道：小于下限或大于上限的通道值设为填充值。"
        )
        note.setWordWrap(True)
        layout.addRow(note)
        self.pixel_low.valueChanged.connect(self._on_pixel_changed)
        self.pixel_high.valueChanged.connect(self._on_pixel_changed)
        self.pixel_fill.valueChanged.connect(self._on_pixel_changed)
        return page

    def _build_equalize_page(self):
        page = QtWidgets.QWidget()
        layout = QtWidgets.QFormLayout(page)
        self.eq_exclude_check = QtWidgets.QCheckBox("排除像素值")
        self.eq_exclude_check.setToolTip(
            "勾选后该亮度值不计入直方图统计；像素仍按 LUT 映射"
        )
        self.eq_exclude_value = self._spin(0, 255, 0)
        self.eq_exclude_value.setToolTip(
            "要从直方图统计中排除的亮度值"
        )
        layout.addRow(self.eq_exclude_check)
        layout.addRow("排除值", self.eq_exclude_value)
        note = QtWidgets.QLabel(
            "在亮度通道上均衡，保留颜色；排除只影响统计。"
        )
        note.setWordWrap(True)
        layout.addRow(note)
        self.eq_exclude_check.toggled.connect(
            self._on_equalize_changed
        )
        self.eq_exclude_value.valueChanged.connect(
            self._on_equalize_changed
        )
        return page

    def _build_gaussian_page(self):
        page = QtWidgets.QWidget()
        layout = QtWidgets.QFormLayout(page)
        self.blur_kernel = self._spin(1, 99, 5, 2)
        self.blur_kernel.setToolTip("奇数核大小，偶数会自动加一")
        self.blur_sigma = QtWidgets.QDoubleSpinBox()
        self.blur_sigma.setRange(0.0, 50.0)
        self.blur_sigma.setDecimals(2)
        self.blur_sigma.setSingleStep(0.5)
        self.blur_sigma.setValue(0.0)
        self.blur_sigma.setKeyboardTracking(False)
        self.blur_sigma.setToolTip("0 表示由核大小自动推导")
        layout.addRow("核大小", self.blur_kernel)
        layout.addRow("标准差 σ", self.blur_sigma)
        note = QtWidgets.QLabel("对 RGB 通道做高斯模糊，alpha 不变。")
        note.setWordWrap(True)
        layout.addRow(note)
        self.blur_kernel.valueChanged.connect(self._on_gaussian_changed)
        self.blur_sigma.valueChanged.connect(self._on_gaussian_changed)
        return page

    # ------------------------------------------------------------ state

    def specs(self):
        """Return a copy of the working chain."""

        return [spec.clone() for spec in self._specs]

    def current_spec(self):
        """Return the spec selected in the list, or None."""

        row = self.filter_list.currentRow()
        if 0 <= row < len(self._specs):
            return self._specs[row]
        return None

    def set_specs(self, specs):
        """Replace the working chain, e.g. when a test builds it."""

        normalized = core.normalize_specs(specs)
        self._specs = normalized
        self._rebuild_list(select=0 if normalized else -1)
        self._refresh_preview()
        return self.specs()

    def add_filter(self, kind):
        """Append one default filter of the requested kind."""

        spec = core.default_spec(kind)
        self._specs.append(spec)
        row = len(self._specs) - 1
        self._rebuild_list(select=row)
        self._refresh_preview()
        return row

    def remove_current(self):
        """Drop the selected filter."""

        row = self.filter_list.currentRow()
        if row < 0 or row >= len(self._specs):
            return False
        del self._specs[row]
        self._rebuild_list(select=min(row, len(self._specs) - 1))
        self._refresh_preview()
        return True

    def move_current(self, delta):
        """Move the selected filter one row up (-1) or down (+1)."""

        row = self.filter_list.currentRow()
        if row < 0 or row >= len(self._specs):
            return False
        target = row + int(delta)
        if target < 0 or target >= len(self._specs):
            return False
        self._specs[row], self._specs[target] = (
            self._specs[target],
            self._specs[row],
        )
        self._rebuild_list(select=target)
        self._refresh_preview()
        return True

    def clear_filters(self):
        """Remove every filter from the working chain."""

        if not self._specs:
            return False
        self._specs = []
        self._rebuild_list(select=-1)
        self._refresh_preview()
        return True

    def set_enabled(self, row, enabled):
        """Check or uncheck one list entry."""

        if row < 0 or row >= len(self._specs):
            return False
        self._specs[row].enabled = bool(enabled)
        item = self.filter_list.item(row)
        if item is not None:
            self._updating_list = True
            self.filter_list.blockSignals(True)
            try:
                item.setCheckState(
                    QtCore.Qt.CheckState.Checked
                    if enabled
                    else QtCore.Qt.CheckState.Unchecked
                )
                item.setText(self._item_text(row, self._specs[row]))
            finally:
                self.filter_list.blockSignals(False)
                self._updating_list = False
        self._schedule_preview()
        return True

    def set_current_params(self, **params):
        """Update parameters of the selected filter; bool on success."""

        spec = self.current_spec()
        if spec is None:
            return False
        merged = dict(spec.params)
        merged.update(params)
        try:
            normalized = core.normalize_specs(
                [core.FilterSpec(spec.kind, spec.enabled, merged)]
            )[0]
        except core.FilterParamError as exc:
            self._status_error = str(exc)
            self._update_status()
            return False
        spec.params = normalized.params
        self._status_error = ""
        self._update_current_item_text()
        self._bind_params()
        self._schedule_preview()
        return True

    # ------------------------------------------------------------ list

    def _item_text(self, index, spec):
        text = "%d. %s" % (index + 1, spec.summary())
        if not spec.enabled:
            text += "（已停用）"
        return text

    def _rebuild_list(self, select=-1):
        self._updating_list = True
        self.filter_list.blockSignals(True)
        try:
            self.filter_list.clear()
            for index, spec in enumerate(self._specs):
                item = QtWidgets.QListWidgetItem(
                    self._item_text(index, spec)
                )
                item.setFlags(
                    item.flags()
                    | QtCore.Qt.ItemFlag.ItemIsUserCheckable
                )
                item.setCheckState(
                    QtCore.Qt.CheckState.Checked
                    if spec.enabled
                    else QtCore.Qt.CheckState.Unchecked
                )
                item.setToolTip(self._item_text(index, spec))
                self.filter_list.addItem(item)
            if self._specs:
                row = select
                if row < 0 or row >= len(self._specs):
                    row = 0
                self.filter_list.setCurrentRow(row)
        finally:
            self.filter_list.blockSignals(False)
            self._updating_list = False
        self._bind_params()
        self._update_buttons()

    def _on_item_changed(self, item):
        if self._updating_list:
            return
        row = self.filter_list.row(item)
        if row < 0 or row >= len(self._specs):
            return
        enabled = (
            item.checkState() == QtCore.Qt.CheckState.Checked
        )
        spec = self._specs[row]
        if spec.enabled == enabled:
            return
        spec.enabled = enabled
        self.filter_list.blockSignals(True)
        try:
            item.setText(self._item_text(row, spec))
        finally:
            self.filter_list.blockSignals(False)
        self._schedule_preview()

    def _on_current_row_changed(self, _row):
        self._bind_params()
        self._update_buttons()

    def _update_buttons(self):
        row = self.filter_list.currentRow()
        has_rows = bool(self._specs)
        self.remove_button.setEnabled(has_rows and row >= 0)
        self.up_button.setEnabled(has_rows and row > 0)
        self.down_button.setEnabled(
            has_rows and 0 <= row < len(self._specs) - 1
        )
        self.clear_button.setEnabled(has_rows)
        count = len(self._specs)
        self.add_button.setToolTip(
            "添加一个滤镜；当前共 %d 个" % count
        )

    def _update_current_item_text(self):
        row = self.filter_list.currentRow()
        if row < 0 or row >= len(self._specs):
            return
        item = self.filter_list.item(row)
        if item is None:
            return
        self.filter_list.blockSignals(True)
        try:
            item.setText(self._item_text(row, self._specs[row]))
        finally:
            self.filter_list.blockSignals(False)

    # ----------------------------------------------------------- params

    def _bind_params(self):
        spec = self.current_spec()
        if spec is None:
            self.param_stack.setCurrentIndex(0)
            return
        self._binding_params = True
        try:
            if spec.kind == core.KIND_PIXEL:
                with QtCore.QSignalBlocker(self.pixel_low):
                    self.pixel_low.setValue(spec.params["low"])
                with QtCore.QSignalBlocker(self.pixel_high):
                    self.pixel_high.setValue(spec.params["high"])
                with QtCore.QSignalBlocker(self.pixel_fill):
                    self.pixel_fill.setValue(spec.params["fill"])
            elif spec.kind == core.KIND_EQUALIZE:
                exclude = spec.params.get("exclude")
                with QtCore.QSignalBlocker(self.eq_exclude_check):
                    self.eq_exclude_check.setChecked(exclude is not None)
                with QtCore.QSignalBlocker(self.eq_exclude_value):
                    self.eq_exclude_value.setValue(
                        int(exclude) if exclude is not None else 0
                    )
                self.eq_exclude_value.setEnabled(
                    exclude is not None
                )
            elif spec.kind == core.KIND_GAUSSIAN:
                with QtCore.QSignalBlocker(self.blur_kernel):
                    self.blur_kernel.setValue(spec.params["kernel"])
                with QtCore.QSignalBlocker(self.blur_sigma):
                    self.blur_sigma.setValue(spec.params["sigma"])
            self.param_stack.setCurrentIndex(
                self._page_index.get(spec.kind, 0)
            )
        finally:
            self._binding_params = False

    def _on_pixel_changed(self, _value=None):
        if self._binding_params:
            return
        spec = self.current_spec()
        if spec is None or spec.kind != core.KIND_PIXEL:
            return
        low = self.pixel_low.value()
        high = self.pixel_high.value()
        if low > high:
            with QtCore.QSignalBlocker(self.pixel_high):
                self.pixel_high.setValue(low)
            high = low
        spec.params = {
            "low": low,
            "high": high,
            "fill": self.pixel_fill.value(),
        }
        self._update_current_item_text()
        self._schedule_preview()

    def _on_equalize_changed(self, _value=None):
        if self._binding_params:
            return
        spec = self.current_spec()
        if spec is None or spec.kind != core.KIND_EQUALIZE:
            return
        exclude = (
            self.eq_exclude_value.value()
            if self.eq_exclude_check.isChecked()
            else None
        )
        self.eq_exclude_value.setEnabled(exclude is not None)
        spec.params = {"exclude": exclude}
        self._update_current_item_text()
        self._schedule_preview()

    def _on_gaussian_changed(self, _value=None):
        if self._binding_params:
            return
        spec = self.current_spec()
        if spec is None or spec.kind != core.KIND_GAUSSIAN:
            return
        kernel = self.blur_kernel.value()
        if kernel % 2 == 0:
            kernel += 1
            if kernel > 99:
                kernel = 99
            with QtCore.QSignalBlocker(self.blur_kernel):
                self.blur_kernel.setValue(kernel)
        spec.params = {
            "kernel": kernel,
            "sigma": float(self.blur_sigma.value()),
        }
        self._update_current_item_text()
        self._schedule_preview()

    # ----------------------------------------------------------- source

    def reload(self):
        """Reload the active chain and the source image from the widget."""

        self._specs = self.controller.chain()
        self._rebuild_list(select=0 if self._specs else -1)
        self.refresh_source()

    def refresh_source(self):
        """Read the pixels the canvas currently shows, without filters."""

        self._preview_timer.stop()
        image = self._read_source_image()
        self._source_rgb = None
        self._source_alpha = None
        self._source_dpr = 1.0
        self._original_pixmap = None
        if image is None or image.isNull():
            self._source_name = ""
            self.source_label.setText(SOURCE_NONE)
            self.original_view.clear()
            self.preview_view.clear()
            self._status_error = ""
            self._update_status()
            return False
        self._source_name = self._source_name_of_widget()
        self.source_label.setText(SOURCE_FORMAT % self._source_name)
        self._original_pixmap = QtGui.QPixmap.fromImage(image)
        self.original_view.set_pixmap(self._original_pixmap)
        try:
            rgb, alpha, ratio = qimage_to_rgba(image)
        except Exception as exc:  # noqa: BLE001
            self._status_error = str(exc)
            self.preview_view.clear()
            self._update_status()
            return False
        self._source_rgb = rgb
        self._source_alpha = alpha
        self._source_dpr = ratio
        self._status_error = ""
        self._refresh_preview()
        return True

    def _read_source_image(self):
        widget = self.widget
        if widget is None:
            return None
        canvas = getattr(widget, "canvas", None)
        pixmap = getattr(canvas, "pixmap", None)
        if isinstance(pixmap, QtGui.QPixmap) and not pixmap.isNull():
            return pixmap.toImage()
        image = getattr(widget, "image", None)
        if isinstance(image, QtGui.QImage) and not image.isNull():
            return image
        return None

    def _source_name_of_widget(self):
        if self.widget is None:
            return ""
        filename = getattr(self.widget, "filename", "") or ""
        if not filename:
            return ""
        return osp.basename(str(filename))

    # ---------------------------------------------------------- preview

    def _schedule_preview(self):
        self._preview_timer.start()

    def _refresh_preview(self):
        self._preview_timer.stop()
        if self._source_rgb is None:
            self.preview_view.clear()
            self._update_status()
            return
        try:
            filtered = core.apply_chain(self._source_rgb, self._specs)
            image = rgba_to_qimage(
                filtered, self._source_alpha, self._source_dpr
            )
            self.preview_view.set_image(image)
            self._status_error = ""
        except Exception as exc:  # noqa: BLE001
            self._status_error = str(exc)
            if self._original_pixmap is not None:
                self.preview_view.set_pixmap(self._original_pixmap)
            else:
                self.preview_view.clear()
        self._update_status()

    def preview_pixmap(self):
        """Return the pixmap of the preview pane, or None."""

        return self.preview_view.pixmap()

    def _update_status(self):
        enabled = sum(1 for spec in self._specs if spec.enabled)
        if self._specs:
            chain = STATUS_COUNT % (len(self._specs), enabled)
        else:
            chain = STATUS_NONE
        source = (
            SOURCE_FORMAT % self._source_name
            if self._source_name
            else SOURCE_NONE
        )
        text = STATUS_FORMAT % (source, chain)
        if self._status_error:
            text = STATUS_ERROR_FORMAT % (text, self._status_error)
        self.status_label.setText(text)

    # ---------------------------------------------------------- confirm

    def confirm(self):
        """Apply the working chain to the canvas and hide the window."""

        self._preview_timer.stop()
        try:
            self.controller.set_chain(self._specs)
        except Exception as exc:  # noqa: BLE001
            self._status_error = str(exc)
            self._update_status()
            return False
        enabled = sum(1 for spec in self._specs if spec.enabled)
        self._status_error = ""
        self._update_status()
        message = APPLIED_FORMAT % enabled if enabled else CLEARED_TEXT
        self._notify_widget(message)
        self.accept()
        return True

    def cancel(self):
        """Discard the working chain and hide the window."""

        self._preview_timer.stop()
        self.reject()

    def _notify_widget(self, message):
        if self.widget is None:
            return
        status_bar = getattr(self.widget, "statusBar", None)
        if not callable(status_bar):
            return
        try:
            status_bar().showMessage(message, 3000)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ events

    def showEvent(self, event):
        super().showEvent(event)
        if self.original_view.has_image():
            self.original_view.reset_view()
        if self.preview_view.has_image():
            self.preview_view.reset_view()

    def closeEvent(self, event):
        self._preview_timer.stop()
        super().closeEvent(event)
