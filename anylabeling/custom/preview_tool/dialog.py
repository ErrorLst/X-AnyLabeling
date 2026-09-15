"""The preview window: one folder, its images and its filters.

The window opens a folder, lists its top level images in file name
order and shows one image at a time with its LabelMe annotations drawn
on top; a rectangle may be grown by the expand parameter. The filter
switch of the second toolbar row combines three axes with "and": score,
size and category. The score and the size axis may be satisfied by
different shapes, which is on purpose; lowering the threshold of one
axis does not widen the other one.

Picking is a two way switch of the image on screen, not a history:

* not picked - the image and its json side car are copied into
  <output>/picked/, the row gains the marker of list_panel and the
  button becomes "移入 dsh-trash";
* picked - every copy of that stem inside picked/ (image and side car)
  is moved into the trash folder of the system, the marker disappears
  and the button becomes "拷贝到 picked" again.

Space and the toolbar button mean the same thing, Delete removes the
current image only, and there is no undo stack at all: the source image
is never touched, so the opposite action is always available again.
Nothing in this module deletes a file; a removal is a move into
<temp>/dsh-trash/<stamp>-<name>.

Every disk job runs in a PickJob or in the PreviewWorker, every decoded
image is a QImage from the preloader, and the keyboard is handled by an
event filter that is installed on this dialog alone - never on the
application - so the shortcuts of the main window stay quiet while the
preview window is not active.
"""

from __future__ import annotations

import os
from typing import FrozenSet, List, Optional, Sequence

from PyQt6 import QtCore, QtGui, QtWidgets

from anylabeling.views.labeling.utils import qt as qt_utils

from . import core
from .category_filter import CategoryFilterButton
from .list_panel import PreviewFileList
from .pick_worker import PickJob
from .settings import PreviewSettings
from .viewer import PreviewView, first_directory
from .worker import PreviewPreloader, PreviewWorker

__all__ = [
    "FILTER_DEBOUNCE_MS",
    "HOLD_DELAY_MS",
    "LIST_MIN_WIDTH",
    "MIN_WINDOW_SIZE",
    "NO_IMAGES_TEXT",
    "NOT_PICKED_TEXT",
    "PICK_ALL_TEXT",
    "PICK_PROBE_DEBOUNCE_MS",
    "PreviewDialog",
    "SPLITTER_SIZES",
    "TOGGLE_PICK_ICON",
    "TOGGLE_PICK_TEXT",
    "TOGGLE_REMOVE_ICON",
    "TOGGLE_REMOVE_TEXT",
    "WINDOW_SIZE",
    "WINDOW_TITLE",
]

WINDOW_TITLE = "预览工具"
WINDOW_SIZE = (1200, 800)
MIN_WINDOW_SIZE = (900, 560)
LIST_MIN_WIDTH = 200
SPLITTER_SIZES = (LIST_MIN_WIDTH, 880)
FILTER_DEBOUNCE_MS = 250
PICK_PROBE_DEBOUNCE_MS = 250
HOLD_DELAY_MS = 500
MIN_SPEED = 1
MAX_SPEED = 30

OPEN_TEXT = "打开目录…"
OPEN_TIP = "选择一个图片目录（Ctrl+O）"
OUTPUT_TEXT = "输出目录…"
OUTPUT_TIP = "picked/ 安放在这个目录里，默认当前工作目录"
PICK_ALL_TEXT = "全部拷贝"
PICK_ALL_TIP = "把列表里尚未挑选的图片拷进 picked/"
TOGGLE_PICK_TEXT = "拷贝到 picked"
TOGGLE_PICK_TIP = "拷贝当前图片与它的 json 边车（Space）"
TOGGLE_REMOVE_TEXT = "移入 dsh-trash"
TOGGLE_REMOVE_TIP = "把 picked/ 里同名的全部副本移入 dsh-trash（Space）"
TOGGLE_PICK_ICON = "export"
TOGGLE_REMOVE_ICON = "trash"

PREV_TEXT = "←"
PREV_TIP = "上一张（← / A，长按连发）"
NEXT_TEXT = "→"
NEXT_TIP = "下一张（→ / D，长按连发）"

FILTER_TEXT = "过滤"
FILTER_TIP = "过滤总开关：关闭时完全不过滤（默认关闭）"
SCORE_TEXT = "得分≥"
SIZE_TEXT = "尺寸"
WIDTH_ABOVE_TEXT = "宽>"
HEIGHT_ABOVE_TEXT = "高>"
WIDTH_BELOW_TEXT = "宽<"
HEIGHT_BELOW_TEXT = "高<"
CATEGORY_TEXT = "类别"
EXPAND_TEXT = "拓展"
EXPAND_SUFFIX = " px"
SPEED_TEXT = "速度"
SPEED_SUFFIX = " 张/s"

NO_DIRECTORY_TEXT = "尚未打开目录：把图片目录拖进窗口，或点「打开目录」"
NO_IMAGES_TEXT = "该目录里没有图片"
NO_CURRENT_TEXT = "当前没有可操作的图片"
NOT_PICKED_TEXT = "当前图不在 picked/，无操作"
BUSY_TEXT = "上一个任务还在进行中"
PICK_ALL_EMPTY_TEXT = "没有需要拷贝的图片"
PICK_ALL_TITLE = "全部拷贝"
PICK_ALL_QUESTION = "把 %d 张尚未挑选的图片拷进 picked/？"

SCAN_TEXT = "正在扫描 %d/%d …"
WORK_TEXT = "正在处理 %d/%d …"
STATUS_FORMAT = "共 %d 张 · 过滤后 %d 张 · 已挑选 %d 张 · 输出 %s"
STATUS_PICKED_FORMAT = "已拷贝到 picked/：%s"
STATUS_REMOVED_FORMAT = "已移入 dsh-trash：%s"
STATUS_FAILED_FORMAT = "失败：%s"
STATUS_BACKGROUND_ON = "视图背景：纯色（再按 B 回到主题）"
STATUS_BACKGROUND_OFF = "视图背景：跟随主题"
INDEX_FORMAT = "%d/%d"
FILTER_SUFFIX_FORMAT = "（过滤后 %d/%d）"
COORD_FORMAT = "坐标: (%d, %d)"
KEYS_TEXT = "←/→ 翻图 · Space 挑选 · Delete 移除 · B 背景 · Esc 关闭"


def _normalize(directory) -> str:
    """Return an absolute, normalized directory path, or ''."""

    if not directory:
        return ""
    try:
        text = os.fspath(directory)
    except TypeError:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    text = str(text).strip()
    if not text:
        return ""
    try:
        return os.path.normpath(os.path.abspath(os.path.expanduser(text)))
    except (OSError, ValueError):
        return ""


def _icon(name: str) -> QtGui.QIcon:
    """Return one resource icon, a null icon when it is missing."""

    try:
        path = qt_utils.new_icon_path(name, "svg")
        if QtCore.QFile(path).exists():
            return QtGui.QIcon(path)
    except Exception:
        pass
    return QtGui.QIcon()


def _size_modes() -> List:
    """Return the four size modes, in the order of the combo box."""

    modes = []
    enum = getattr(core, "SizeFilterMode", None)
    for name in ("ANY_ABOVE", "BOTH_ABOVE", "ANY_BELOW", "BOTH_BELOW"):
        mode = getattr(enum, name, None) if enum is not None else None
        if mode is not None:
            modes.append(mode)
    return modes


def _mode_name(mode) -> str:
    """Return the stored name of one size mode, '' when unknown."""

    for name in ("value", "name"):
        value = getattr(mode, name, None)
        if isinstance(value, str) and value:
            return value
    if isinstance(mode, str):
        return mode
    return ""


class PreviewDialog(QtWidgets.QDialog):
    """The preview window of the Tool menu."""

    def __init__(self, parent=None, explorer=None, settings=None):
        """Build the window.

        Args:
            parent: The parent widget.
            explorer: The labeling widget; only its last_open_dir is read
                once, as the initial directory.
            settings: The parameter store. None builds the QSettings
                backed one, a test injects an INI backed one.
        """

        super().__init__(parent)
        self._settings = (
            settings if settings is not None else PreviewSettings()
        )
        self._directory = ""
        self._entries: List = []
        self._kept: List = []
        self._picked: FrozenSet[str] = frozenset()
        self._index = -1
        self._current_path = ""
        self._saved_categories = None
        self._scan_worker: Optional[PreviewWorker] = None
        self._pick_job: Optional[PickJob] = None
        self._pending_probe = False
        self._filter_installed = False
        self._repeat_step = 0
        self._toggle_icon_name = ""
        self.setWindowTitle(WINDOW_TITLE)
        self.resize(*WINDOW_SIZE)
        self.setMinimumSize(*MIN_WINDOW_SIZE)
        self.setAcceptDrops(True)
        self._build_ui()
        self._restore_settings()
        self._center()
        self.installEventFilter(self)
        self._filter_installed = True
        initial = _normalize(getattr(explorer, "last_open_dir", None))
        if initial:
            self.set_directory(initial)

    # ------------------------------------------------------------- build

    def _build_ui(self) -> None:
        """Lay out the two toolbar rows, the splitter and the status."""

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 8)
        outer.setSpacing(6)
        outer.addLayout(self._build_toolbar())
        outer.addLayout(self._build_filter_row())
        self._build_splitter(outer)
        self._build_status_bar(outer)

        self._debounce_timer = QtCore.QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.timeout.connect(self._on_debounce_timeout)
        self._hold_timer = QtCore.QTimer(self)
        self._hold_timer.setSingleShot(True)
        self._hold_timer.setInterval(HOLD_DELAY_MS)
        self._hold_timer.timeout.connect(self._on_hold_timeout)
        self._repeat_timer = QtCore.QTimer(self)
        self._repeat_timer.setInterval(self._repeat_interval())
        self._repeat_timer.timeout.connect(self._on_repeat_timeout)

        self._preloader = PreviewPreloader(self)
        self._preloader.ready.connect(self._on_image_ready)
        self._view.pixel_moved.connect(self._on_pixel_moved)
        self._view.directory_dropped.connect(self._on_directory_dropped)
        self._list.directory_dropped.connect(self._on_directory_dropped)
        self._list.selection_changed.connect(self._on_row_selected)
        self._update_index_label()

    def _build_splitter(self, root) -> None:
        """Build the file list and the view, side by side.

        The list sits on the left, like every other tool of this fork,
        and the view takes the room that is left over.
        """

        self._list = PreviewFileList()
        self._list.setMinimumWidth(LIST_MIN_WIDTH)
        self._list.setUniformItemSizes(True)
        # The list never takes the keyboard: the shortcuts of the window
        # live in the event filter of the dialog, and a focused
        # QListWidget would eat Space and End before they get there.
        self._list.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self._view = PreviewView()
        self._splitter = QtWidgets.QSplitter(
            QtCore.Qt.Orientation.Horizontal, self
        )
        self._splitter.addWidget(self._list)
        self._splitter.addWidget(self._view)
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setSizes(list(SPLITTER_SIZES))
        self._splitter.setChildrenCollapsible(False)
        self._splitter.setHandleWidth(1)
        root.addWidget(self._splitter, 1)

    def _build_status_bar(self, root) -> None:
        """Build the status bar: summary left, hints right."""

        self._status = QtWidgets.QLabel(self)
        self._status.setWordWrap(False)
        self._coords = QtWidgets.QLabel(self)
        self._hint = QtWidgets.QLabel(KEYS_TEXT, self)
        self.status_bar = QtWidgets.QStatusBar(self)
        self.status_bar.addWidget(self._status)
        self.status_bar.addPermanentWidget(self._hint)
        self.status_bar.addPermanentWidget(self._coords)
        root.addWidget(self.status_bar)

    def _build_toolbar(self) -> QtWidgets.QHBoxLayout:
        """Build the first row: folders, picking and navigation."""

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(6)
        self._open_btn = QtWidgets.QPushButton(OPEN_TEXT, self)
        self._open_btn.setToolTip(OPEN_TIP)
        self._open_btn.clicked.connect(self._choose_directory)
        row.addWidget(self._open_btn)

        self._output_btn = QtWidgets.QPushButton(OUTPUT_TEXT, self)
        self._output_btn.setToolTip(OUTPUT_TIP)
        self._output_btn.clicked.connect(self._choose_output_dir)
        row.addWidget(self._output_btn)

        self._toggle_btn = QtWidgets.QToolButton(self)
        self._toggle_btn.setToolButtonStyle(
            QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self._toggle_btn.setText(TOGGLE_PICK_TEXT)
        self._toggle_btn.setToolTip(TOGGLE_PICK_TIP)
        self._toggle_btn.clicked.connect(self._on_toggle_current)
        row.addWidget(self._toggle_btn)

        self._pick_all_btn = QtWidgets.QPushButton(PICK_ALL_TEXT, self)
        self._pick_all_btn.setToolTip(PICK_ALL_TIP)
        self._pick_all_btn.clicked.connect(self._on_pick_all)
        row.addWidget(self._pick_all_btn)

        row.addStretch(1)

        self._prev_btn = QtWidgets.QToolButton(self)
        self._prev_btn.setText(PREV_TEXT)
        self._prev_btn.setToolTip(PREV_TIP)
        self._prev_btn.clicked.connect(lambda: self._navigate(-1))
        row.addWidget(self._prev_btn)

        self._index_label = QtWidgets.QLabel(self)
        self._index_label.setMinimumWidth(150)
        self._index_label.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignCenter
        )
        row.addWidget(self._index_label)

        self._next_btn = QtWidgets.QToolButton(self)
        self._next_btn.setText(NEXT_TEXT)
        self._next_btn.setToolTip(NEXT_TIP)
        self._next_btn.clicked.connect(lambda: self._navigate(1))
        row.addWidget(self._next_btn)
        return row

    def _build_filter_row(self) -> QtWidgets.QHBoxLayout:
        """Build the second row: the filter switch and its axes."""

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(6)
        self._filter_enabled = QtWidgets.QCheckBox(FILTER_TEXT, self)
        self._filter_enabled.setToolTip(FILTER_TIP)
        self._filter_enabled.toggled.connect(self._on_filter_toggled)
        row.addWidget(self._filter_enabled)

        self._score_label = QtWidgets.QLabel(SCORE_TEXT, self)
        row.addWidget(self._score_label)
        self._score_box = QtWidgets.QDoubleSpinBox(self)
        self._score_box.setRange(0.0, 1.0)
        self._score_box.setDecimals(2)
        self._score_box.setSingleStep(0.05)
        self._score_box.setFixedWidth(76)
        self._score_box.valueChanged.connect(self._on_score_changed)
        row.addWidget(self._score_box)

        self._size_label = QtWidgets.QLabel(SIZE_TEXT, self)
        row.addWidget(self._size_label)
        self._size_mode = QtWidgets.QComboBox(self)
        self._size_mode.setMinimumWidth(150)
        for mode in _size_modes():
            self._size_mode.addItem(
                str(getattr(mode, "label", "") or _mode_name(mode)),
                _mode_name(mode),
            )
        self._size_mode.currentIndexChanged.connect(
            self._on_size_mode_changed
        )
        row.addWidget(self._size_mode)

        self._width_label = QtWidgets.QLabel(WIDTH_ABOVE_TEXT, self)
        row.addWidget(self._width_label)
        self._width_box = QtWidgets.QDoubleSpinBox(self)
        self._width_box.setRange(0.0, 9999.0)
        self._width_box.setDecimals(1)
        self._width_box.setFixedWidth(84)
        self._width_box.valueChanged.connect(self._on_width_changed)
        row.addWidget(self._width_box)

        self._height_label = QtWidgets.QLabel(HEIGHT_ABOVE_TEXT, self)
        row.addWidget(self._height_label)
        self._height_box = QtWidgets.QDoubleSpinBox(self)
        self._height_box.setRange(0.0, 9999.0)
        self._height_box.setDecimals(1)
        self._height_box.setFixedWidth(84)
        self._height_box.valueChanged.connect(self._on_height_changed)
        row.addWidget(self._height_box)

        self._category_label = QtWidgets.QLabel(CATEGORY_TEXT, self)
        row.addWidget(self._category_label)
        self._categories = CategoryFilterButton(self)
        self._categories.changed.connect(self._on_categories_changed)
        row.addWidget(self._categories)

        self._expand_label = QtWidgets.QLabel(EXPAND_TEXT, self)
        row.addWidget(self._expand_label)
        self._expand_box = QtWidgets.QSpinBox(self)
        self._expand_box.setRange(0, 100)
        self._expand_box.setSuffix(EXPAND_SUFFIX)
        self._expand_box.setFixedWidth(90)
        self._expand_box.valueChanged.connect(self._on_expand_changed)
        row.addWidget(self._expand_box)

        self._speed_label = QtWidgets.QLabel(SPEED_TEXT, self)
        row.addWidget(self._speed_label)
        self._speed_box = QtWidgets.QSpinBox(self)
        self._speed_box.setRange(MIN_SPEED, MAX_SPEED)
        self._speed_box.setSuffix(SPEED_SUFFIX)
        self._speed_box.setFixedWidth(96)
        self._speed_box.valueChanged.connect(self._on_speed_changed)
        row.addWidget(self._speed_box)

        row.addStretch(1)
        return row

    def _filter_controls(self) -> List[QtWidgets.QWidget]:
        """Return every control the filter switch drives."""

        return [
            self._score_label,
            self._score_box,
            self._size_label,
            self._size_mode,
            self._width_label,
            self._width_box,
            self._height_label,
            self._height_box,
            self._category_label,
            self._categories,
        ]

    def _restore_settings(self) -> None:
        """Read the eight stored parameters into the widgets."""

        settings = self._settings
        try:
            self._filter_enabled.setChecked(
                bool(settings.filter_enabled())
            )
            self._score_box.setValue(float(settings.score_threshold()))
            name = _mode_name(settings.size_mode())
            row = self._size_mode.findData(name)
            if row >= 0:
                self._size_mode.setCurrentIndex(row)
            self._width_box.setValue(float(settings.width_threshold()))
            self._height_box.setValue(float(settings.height_threshold()))
            self._expand_box.setValue(int(settings.expand_px()))
            self._speed_box.setValue(int(settings.speed()))
            output = str(settings.output_dir() or "")
        except Exception:
            output = core.default_output_dir()
        self._output_btn.setToolTip(
            f"{OUTPUT_TIP}\n{output}"
        )
        self._view.set_expand_px(self._expand_box.value())
        self._set_filter_controls_enabled(self._filter_enabled.isChecked())
        self._update_size_labels()
        self._update_repeat_interval()
        self._refresh_pick_button()
        self._update_status()

    def _center(self) -> None:
        """Move the window to the middle of the screen."""

        try:
            frame = self.frameGeometry()
            screen = self.screen()
            if screen is None:
                return
            frame.moveCenter(screen.availableGeometry().center())
            self.move(frame.topLeft())
        except Exception:
            pass

    # ---------------------------------------------------------- settings

    def _set_filter_controls_enabled(self, enabled: bool) -> None:
        """Enable or disable the three filter axes as a group."""

        for widget in self._filter_controls():
            widget.setEnabled(bool(enabled))

    def _update_size_labels(self) -> None:
        """Show 「宽>/高>」 or 「宽</高<」 after the mode."""

        mode = self._current_mode()
        above = bool(getattr(mode, "above", True)) if mode else True
        self._width_label.setText(
            WIDTH_ABOVE_TEXT if above else WIDTH_BELOW_TEXT
        )
        self._height_label.setText(
            HEIGHT_ABOVE_TEXT if above else HEIGHT_BELOW_TEXT
        )

    def _current_mode(self):
        """Return the size mode the combo box holds."""

        name = self._size_mode.currentData()
        enum = getattr(core, "SizeFilterMode", None)
        if enum is None:
            return None
        try:
            return enum(name)
        except Exception:
            return None

    def _build_filter(self) -> Optional[object]:
        """Return the core filter of the widgets, None when it is off."""

        if not self._filter_enabled.isChecked():
            return None
        mode = self._current_mode()
        if mode is None:
            modes = _size_modes()
            mode = modes[0] if modes else None
        score = float(self._score_box.value())
        width = float(self._width_box.value())
        height = float(self._height_box.value())
        try:
            return core.PreviewFilter(
                score_threshold=score,
                size_mode=mode,
                width_threshold=width,
                height_threshold=height,
            )
        except TypeError:
            return core.PreviewFilter(score, mode, width, height)

    def rescan(self) -> None:
        """Read the current directory again."""

        if self._directory:
            self._start_scan(self._directory)

    # --------------------------------------------------------- directory

    def set_directory(self, directory) -> None:
        """Open one directory: scan it and restore its category checks."""

        path = _normalize(directory)
        if not path:
            return
        if path == self._directory and self._entries:
            return
        self._directory = path
        self._saved_categories = self._read_saved_categories(path)
        self._entries = []
        self._kept = []
        self._picked = frozenset()
        self._index = -1
        self._current_path = ""
        self._list.set_rows([])
        self._list.set_picked(self._picked)
        self._view.clear()
        self._refresh_pick_button()
        self._update_index_label()
        self._start_scan(path)

    def _read_saved_categories(self, directory: str):
        """Return the stored categories of one directory, or None."""

        try:
            return self._settings.categories_for(directory)
        except Exception:
            return None

    def _start_scan(self, directory: str) -> None:
        """Start one background scan."""

        self._stop_scan()
        worker = PreviewWorker(directory, self)
        worker.progress.connect(self._on_scan_progress)
        worker.scanned.connect(self._on_scanned)
        worker.finished.connect(self._on_scan_finished)
        worker.finished.connect(worker.deleteLater)
        self._scan_worker = worker
        self._status.setText(SCAN_TEXT % (0, 0))
        worker.start()

    def _stop_scan(self) -> None:
        """Interrupt a running scan and wait for it."""

        worker = self._scan_worker
        self._scan_worker = None
        if worker is None:
            return
        try:
            worker.requestInterruption()
            worker.wait(2000)
        except Exception:
            pass

    def _on_scan_finished(self) -> None:
        """Forget a worker that finished on its own."""

        worker = self.sender()
        if worker is self._scan_worker:
            self._scan_worker = None

    def _on_scan_progress(self, done: int, total: int) -> None:
        """Show how far the scan is."""

        self._status.setText(SCAN_TEXT % (int(done), int(total)))

    def _on_scanned(self, snapshot) -> None:
        """Take the entries of one finished scan."""

        if snapshot is None:
            return
        directory = _normalize(getattr(snapshot, "directory", ""))
        if directory and self._directory and directory != self._directory:
            return
        self._entries = list(getattr(snapshot, "entries", ()) or ())
        if getattr(snapshot, "stopped", False):
            return
        names = self._collect_categories()
        saved = self._saved_categories
        self._saved_categories = None
        self._categories.set_categories(names, saved)
        self._persist_categories()
        self._apply_filter()
        self._refresh_pick_button()
        if self._entries:
            self._schedule_probe()

    def _collect_categories(self) -> List[str]:
        """Return every label of the directory plus the background one."""

        names: List[str] = []
        try:
            _kept, _total, categories = core.filter_images(
                self._entries, None, None
            )
            names = [str(name) for name in (categories or ())]
        except Exception:
            names = []
        for entry in self._entries:
            for label in getattr(entry, "labels", ()) or ():
                text = str(label)
                if text and text not in names:
                    names.append(text)
        if core.BACKGROUND_LABEL not in names:
            names.append(core.BACKGROUND_LABEL)
        return names

    def _persist_categories(self) -> None:
        """Store the checked categories of the directory."""

        if not self._directory:
            return
        checked = self._categories.checked()
        if checked is None:
            names = self._categories.categories()
        else:
            names = sorted(checked)
        try:
            self._settings.set_categories(self._directory, names)
        except Exception:
            pass

    # ------------------------------------------------------------ filter

    def _apply_filter(self) -> List:
        """Filter the entries, fill the list and show the current image."""

        cfg = self._build_filter()
        checked = None
        if cfg is not None:
            checked = self._categories.checked()
        try:
            kept, _total, _categories = core.filter_images(
                self._entries, cfg, checked
            )
            kept = list(kept)
        except Exception:
            kept = list(self._entries)
        self._kept = kept
        self._list.set_rows([entry.name for entry in kept])
        self._list.set_picked(self._picked)
        self._set_current(self._pick_target(kept, self._current_path))
        self._update_status()
        return kept

    def _pick_target(self, kept: Sequence, current_path: str) -> int:
        """Return the row of the image that should be on screen.

        The current image wins, then the first image with the same stem,
        then the first row of the list. -1 means "nothing to show".
        """

        entries = list(kept or ())
        if not entries:
            return -1
        target = current_path or self._current_path
        if target:
            for row, entry in enumerate(entries):
                if getattr(entry, "path", "") == target:
                    return row
            stem = os.path.splitext(os.path.basename(str(target)))[0]
            if stem:
                for row, entry in enumerate(entries):
                    name = str(getattr(entry, "name", "") or "")
                    if os.path.splitext(name)[0] == stem:
                        return row
        return 0

    def _on_filter_toggled(self, enabled: bool) -> None:
        """Apply the master switch of the filter group."""

        self._set_filter_controls_enabled(bool(enabled))
        self._write("set_filter_enabled", bool(enabled))
        self._debounce(FILTER_DEBOUNCE_MS)

    def _on_score_changed(self, value: float) -> None:
        """Store the score threshold and refresh after a pause."""

        self._write("set_score_threshold", float(value))
        self._debounce(FILTER_DEBOUNCE_MS)

    def _on_size_mode_changed(self, _row: int = 0) -> None:
        """Store the size mode and refresh after a pause."""

        name = self._size_mode.currentData()
        if name:
            self._write("set_size_mode", str(name))
        self._update_size_labels()
        self._debounce(FILTER_DEBOUNCE_MS)

    def _on_width_changed(self, value: float) -> None:
        """Store the width threshold and refresh after a pause."""

        self._write("set_width_threshold", float(value))
        self._debounce(FILTER_DEBOUNCE_MS)

    def _on_height_changed(self, value: float) -> None:
        """Store the height threshold and refresh after a pause."""

        self._write("set_height_threshold", float(value))
        self._debounce(FILTER_DEBOUNCE_MS)

    def _on_categories_changed(self) -> None:
        """Store the category checks and refresh after a pause."""

        self._persist_categories()
        self._debounce(FILTER_DEBOUNCE_MS)

    def _on_expand_changed(self, value: int) -> None:
        """Grow or shrink the rectangles of the current image."""

        self._write("set_expand_px", int(value))
        self._view.set_expand_px(int(value))

    def _on_speed_changed(self, value: int) -> None:
        """Store the repeat speed of a held arrow key."""

        self._write("set_speed", int(value))
        self._update_repeat_interval()

    def _write(self, method: str, value) -> None:
        """Store one parameter, never raising."""

        writer = getattr(self._settings, method, None)
        if writer is None:
            return
        try:
            writer(value)
        except Exception:
            pass

    def _debounce(self, milliseconds: int) -> None:
        """Restart the timer that refreshes the view."""

        self._debounce_timer.start(max(0, int(milliseconds)))

    def _on_debounce_timeout(self) -> None:
        """Run the pending refresh."""

        if self._pending_probe:
            self._pending_probe = False
            self._start_pick("detect")
            return
        self._apply_filter()

    def _schedule_probe(self) -> None:
        """Ask for a picked state refresh after a short pause."""

        self._pending_probe = True
        self._debounce(PICK_PROBE_DEBOUNCE_MS)

    # -------------------------------------------------------- navigation

    def _set_current(self, row: int) -> None:
        """Show one row of the filtered list."""

        try:
            row = int(row)
        except (TypeError, ValueError):
            row = -1
        if not self._kept or row < 0:
            self._index = -1
            self._current_path = ""
        else:
            self._index = min(row, len(self._kept) - 1)
            self._current_path = str(
                getattr(self._kept[self._index], "path", "") or ""
            )
        self._list.set_current_row(self._index)
        self._show_current()
        self._refresh_pick_button()
        self._update_index_label()

    def _on_row_selected(self, row: int) -> None:
        """Show the row the user clicked in the list."""

        if row < 0 or row >= len(self._kept):
            return
        if row == self._index:
            return
        self._set_current(row)

    def _show_current(self) -> None:
        """Load the image of the current row into the view."""

        if self._index < 0 or self._index >= len(self._kept):
            self._view.clear()
            return
        entry = self._kept[self._index]
        path = str(getattr(entry, "path", "") or "")
        shapes = tuple(getattr(entry, "shapes", ()) or ())
        if not path:
            self._view.clear()
            return
        image = self._preloader.get(path)
        if image is None:
            image = QtGui.QImage(path)
        if image is None or image.isNull():
            self._view.clear()
            self._view.show_error(os.path.basename(path))
        else:
            self._view.show_image(image, shapes)
        self._preload_neighbours()

    def _preload_neighbours(self) -> None:
        """Ask the preloader for the images around the current one."""

        if not self._kept:
            return
        paths = [
            str(getattr(entry, "path", "") or "")
            for entry in self._kept
        ]
        self._preloader.request(self._index, paths)

    def _on_image_ready(self, path: str) -> None:
        """Show a preloaded image when it is still the current one."""

        if not path or path != self._current_path:
            return
        if self._index < 0 or self._index >= len(self._kept):
            return
        image = self._preloader.get(path)
        if image is None or image.isNull():
            return
        entry = self._kept[self._index]
        shapes = tuple(getattr(entry, "shapes", ()) or ())
        self._view.show_image(image, shapes)

    def _navigate(self, step: int) -> None:
        """Move the current row by one step and show it."""

        if not self._kept:
            return
        row = self._index if self._index >= 0 else 0
        row += int(step)
        row = max(0, min(len(self._kept) - 1, row))
        if row == self._index:
            return
        self._set_current(row)

    def _jump(self, row: int) -> None:
        """Show the first or the last row of the list."""

        if not self._kept:
            return
        if row < 0:
            row = len(self._kept) - 1
        self._set_current(max(0, min(len(self._kept) - 1, row)))

    # ------------------------------------------------------------ picking

    def _current_stem(self) -> str:
        """Return the stem of the image on screen, or ''."""

        if self._index < 0 or self._index >= len(self._kept):
            return ""
        name = str(getattr(self._kept[self._index], "name", "") or "")
        return os.path.splitext(name)[0]

    def _is_picked(self, stem: str = "") -> bool:
        """Return True when the stem exists inside picked/."""

        stem = stem or self._current_stem()
        if not stem:
            return False
        return stem in self._picked

    def _refresh_pick_button(self) -> None:
        """Show the copy or the remove face of the toggle button."""

        picked = self._is_picked()
        if picked:
            self._toggle_btn.setText(TOGGLE_REMOVE_TEXT)
            self._toggle_btn.setToolTip(TOGGLE_REMOVE_TIP)
            self._toggle_icon_name = TOGGLE_REMOVE_ICON
        else:
            self._toggle_btn.setText(TOGGLE_PICK_TEXT)
            self._toggle_btn.setToolTip(TOGGLE_PICK_TIP)
            self._toggle_icon_name = TOGGLE_PICK_ICON
        self._toggle_btn.setIcon(_icon(self._toggle_icon_name))
        self._toggle_btn.setEnabled(self._index >= 0)

    def _on_toggle_current(self) -> None:
        """Copy or remove the image on screen, the two way switch."""

        if self._index < 0:
            self._set_status(NO_CURRENT_TEXT)
            return
        if self._pick_busy():
            self._set_status(BUSY_TEXT)
            return
        stem = self._current_stem()
        if stem and stem in self._picked:
            self._start_pick("remove", targets=(stem,))
        else:
            self._start_pick("pick", targets=(self._current_path,))

    def _on_remove_current(self) -> None:
        """Delete key: remove the current image, never anything else."""

        if self._index < 0:
            self._set_status(NO_CURRENT_TEXT)
            return
        stem = self._current_stem()
        if not stem or stem not in self._picked:
            self._set_status(NOT_PICKED_TEXT)
            return
        if self._pick_busy():
            self._set_status(BUSY_TEXT)
            return
        self._start_pick("remove", targets=(stem,))

    def _on_pick_all(self) -> None:
        """Copy every image of the list that is not picked yet."""

        pending = [
            entry for entry in self._kept
            if os.path.splitext(str(getattr(entry, "name", "")))[0]
            not in self._picked
        ]
        if not pending:
            self._set_status(PICK_ALL_EMPTY_TEXT)
            return
        if self._pick_busy():
            self._set_status(BUSY_TEXT)
            return
        answer = QtWidgets.QMessageBox.question(
            self,
            PICK_ALL_TITLE,
            PICK_ALL_QUESTION % len(pending),
        )
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self._start_pick("all")

    def _start_pick(self, kind: str, **kwargs) -> Optional[PickJob]:
        """Start one pick job and connect every one of its signals."""

        if self._pick_busy():
            self._set_status(BUSY_TEXT)
            return None
        entries = tuple(kwargs.get("entries", ()) or ())
        targets = tuple(kwargs.get("targets", ()) or ())
        if kind == "detect":
            entries = tuple(self._entries)
        elif kind == "all":
            entries = tuple(self._kept)
        output_dir = ""
        try:
            output_dir = str(self._settings.output_dir() or "")
        except Exception:
            output_dir = ""
        job = PickJob(
            kind,
            entries=entries,
            output_dir=output_dir,
            targets=targets,
            parent=self,
        )
        job.progress.connect(self._on_pick_progress)
        job.item_picked.connect(self._on_pick_item)
        job.item_removed.connect(self._on_pick_removed)
        job.item_failed.connect(self._on_pick_failed)
        job.detect_finished.connect(self._on_detect_finished)
        job.job_done.connect(self._on_pick_done)
        self._pick_job = job
        self._status.setText(WORK_TEXT % (0, len(entries) or len(targets)))
        job.start()
        return job

    def _pick_busy(self) -> bool:
        """Return True while a pick job is running."""

        job = self._pick_job
        if job is None:
            return False
        try:
            return bool(job.isRunning())
        except Exception:
            return False

    def _on_pick_progress(self, done: int, total: int) -> None:
        """Show how far the running job is."""

        self._status.setText(WORK_TEXT % (int(done), int(total)))

    def _on_pick_item(self, stem: str, dest: str) -> None:
        """Remember one copied image and mark its row."""

        if stem:
            self._picked = frozenset(set(self._picked) | {stem})
        self._mark_picked()
        self._set_status(STATUS_PICKED_FORMAT % dest)

    def _on_pick_removed(self, stem: str, moved: str) -> None:
        """Forget one removed image and clear its row marker."""

        if stem:
            self._picked = frozenset(set(self._picked) - {stem})
        self._mark_picked()
        self._set_status(STATUS_REMOVED_FORMAT % moved)

    def _on_pick_failed(self, stem: str, message: str) -> None:
        """Report one file that could not be handled."""

        text = f"{stem}: {message}" if stem else str(message)
        self._set_status(STATUS_FAILED_FORMAT % text)

    def _on_detect_finished(self, stems) -> None:
        """Take the picked stems of a finished detection."""

        try:
            self._picked = frozenset(str(stem) for stem in (stems or ()))
        except TypeError:
            self._picked = frozenset()
        self._mark_picked()
        self._update_status()

    def _on_pick_done(self, kind: str, ok: int, total: int) -> None:
        """Close one job and refresh the picked state."""

        sender = self.sender()
        if sender is not None and sender is self._pick_job:
            self._pick_job = None
        elif sender is None and kind == self._pick_job_kind():
            self._pick_job = None
        self._mark_picked()
        self._update_status()
        if kind in ("pick", "all", "remove"):
            self._schedule_probe()

    def _pick_job_kind(self) -> str:
        """Return the kind of the running job, '' when there is none."""

        job = self._pick_job
        return str(getattr(job, "kind", "") or "")

    def _mark_picked(self) -> None:
        """Sync the rows and the toggle button with the picked set."""

        self._list.set_picked(self._picked)
        self._refresh_pick_button()

    # --------------------------------------------------- output directory

    def _choose_directory(self) -> str:
        """Ask for a directory and open it."""

        start = self._directory or core.default_output_dir()
        try:
            path = QtWidgets.QFileDialog.getExistingDirectory(
                self, OPEN_TEXT, start
            )
        except Exception:
            path = ""
        path = _normalize(path)
        if path:
            self.set_directory(path)
        return path

    def _choose_output_dir(self) -> str:
        """Ask for the directory that holds picked/."""

        try:
            start = str(self._settings.output_dir() or "")
        except Exception:
            start = ""
        if not start:
            start = core.default_output_dir()
        try:
            path = QtWidgets.QFileDialog.getExistingDirectory(
                self, OUTPUT_TEXT, start
            )
        except Exception:
            path = ""
        path = _normalize(path)
        if path:
            self._on_output_dir_changed(path)
        return path

    def _on_output_dir_changed(self, path) -> None:
        """Store a new output directory and probe it again."""

        target = _normalize(path)
        if not target:
            return
        self._write("set_output_dir", target)
        self._output_btn.setToolTip(
            f"{OUTPUT_TIP}\n{target}"
        )
        self._picked = frozenset()
        self._mark_picked()
        self._update_status()
        if self._entries:
            self._schedule_probe()

    def _on_directory_dropped(self, directory) -> None:
        """Open the directory the user dropped on the window."""

        path = _normalize(directory)
        if not path:
            return
        if not os.path.isdir(path):
            self._set_status(f"不是目录：{path}")
            return
        self.set_directory(path)

    def dragEnterEvent(self, event) -> None:
        """Accept a directory dropped on the window itself."""

        if first_directory(event.mimeData()):
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event) -> None:
        """Keep the drag alive over the window."""

        if first_directory(event.mimeData()):
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event) -> None:
        """Open the directory dropped on the window."""

        path = first_directory(event.mimeData())
        if not path:
            event.ignore()
            return
        event.acceptProposedAction()
        self._on_directory_dropped(path)

    # ------------------------------------------------------------ status

    def _status_text(self) -> str:
        """Return the summary line of the window."""

        if not self._directory:
            return NO_DIRECTORY_TEXT
        total = len(self._entries)
        if total == 0:
            return NO_IMAGES_TEXT
        try:
            output = str(self._settings.output_dir() or "")
        except Exception:
            output = ""
        return STATUS_FORMAT % (
            total,
            len(self._kept),
            len(self._picked),
            output,
        )

    def _update_status(self) -> None:
        """Write the summary line."""

        self._status.setText(self._status_text())

    def _set_status(self, text: str) -> None:
        """Write one transient message into the status line."""

        self._status.setText(str(text or ""))

    def _index_text(self) -> str:
        """Return the text of the position label."""

        kept = len(self._kept)
        if self._index < 0 or not kept:
            base = INDEX_FORMAT % (0, kept)
        else:
            base = INDEX_FORMAT % (self._index + 1, kept)
        if self._filters_active():
            base += FILTER_SUFFIX_FORMAT % (kept, len(self._entries))
        return base

    def _filters_active(self) -> bool:
        """Return True when something filters the list."""

        if self._build_filter() is not None:
            return True
        return self._categories.checked() is not None

    def _update_index_label(self) -> None:
        """Refresh the position label."""

        self._index_label.setText(self._index_text())

    def _on_pixel_moved(self, x: int, y: int) -> None:
        """Show the pointer position in image pixels."""

        if int(x) < 0 or int(y) < 0:
            self._coords.setText("")
            return
        self._coords.setText(COORD_FORMAT % (int(x), int(y)))

    # ---------------------------------------------------------- keyboard

    def _begin_repeat(self, step: int) -> None:
        """Move once now and start the auto repeat of a held key."""

        self._navigate(step)
        self._repeat_step = int(step)
        self._hold_timer.start(HOLD_DELAY_MS)

    def _on_hold_timeout(self) -> None:
        """Start the fast repeat of a held arrow key."""

        if not self._repeat_step:
            return
        self._navigate(self._repeat_step)
        self._repeat_timer.start()

    def _on_repeat_timeout(self) -> None:
        """One tick of the fast repeat."""

        if not self._repeat_step:
            self._stop_repeat()
            return
        self._navigate(self._repeat_step)

    def _stop_repeat(self) -> None:
        """Stop the auto repeat of a held key."""

        self._repeat_step = 0
        self._hold_timer.stop()
        self._repeat_timer.stop()

    def _repeat_interval(self) -> int:
        """Return the interval of one repeat tick, in milliseconds."""

        try:
            speed = int(self._speed_box.value())
        except (TypeError, ValueError):
            speed = 5
        speed = max(MIN_SPEED, min(MAX_SPEED, speed))
        return max(1, int(round(1000.0 / speed)))

    def _update_repeat_interval(self) -> None:
        """Apply the speed of the spin box to the repeat timer."""

        self._repeat_timer.setInterval(self._repeat_interval())

    def _toggle_background(self) -> None:
        """Switch the viewport background and report it."""

        if self._view.toggle_background():
            self._set_status(STATUS_BACKGROUND_ON)
        else:
            self._set_status(STATUS_BACKGROUND_OFF)

    def _is_text_input(self, widget) -> bool:
        """Return True for every text or numeric input widget."""

        if widget is None:
            return False
        for kind in (
            QtWidgets.QLineEdit,
            QtWidgets.QAbstractSpinBox,
            QtWidgets.QComboBox,
            QtWidgets.QTextEdit,
            QtWidgets.QPlainTextEdit,
        ):
            if isinstance(widget, kind):
                return True
        if isinstance(widget, QtWidgets.QAbstractItemView):
            try:
                editing = QtWidgets.QAbstractItemView.State.EditingState
                return widget.state() == editing
            except Exception:
                return False
        return False

    def _on_key_press(self, event) -> bool:
        """Handle one key press; True when it was consumed."""

        if event is None:
            return False
        try:
            key = event.key()
            modifiers = event.modifiers()
        except AttributeError:
            return False
        control = bool(
            modifiers & QtCore.Qt.KeyboardModifier.ControlModifier
        )
        Key = QtCore.Qt.Key
        if key == Key.Key_Escape:
            self.close()
            return True
        if control and key == Key.Key_O:
            self._choose_directory()
            return True
        if control:
            return False
        if key in (Key.Key_Left, Key.Key_A):
            self._begin_repeat(-1)
            return True
        if key in (Key.Key_Right, Key.Key_D):
            self._begin_repeat(1)
            return True
        if key == Key.Key_Space:
            self._on_toggle_current()
            return True
        if key == Key.Key_Delete:
            self._on_remove_current()
            return True
        if key == Key.Key_B:
            self._toggle_background()
            return True
        if key == Key.Key_Home:
            self._jump(0)
            return True
        if key == Key.Key_End:
            self._jump(-1)
            return True
        return False

    def _on_key_release(self, event) -> bool:
        """Stop the auto repeat when an arrow key comes back up."""

        if event is None:
            return False
        try:
            key = event.key()
        except AttributeError:
            return False
        Key = QtCore.Qt.Key
        if key in (
            Key.Key_Left,
            Key.Key_Right,
            Key.Key_A,
            Key.Key_D,
            Key.Key_Home,
            Key.Key_End,
        ):
            self._stop_repeat()
            return True
        return False

    def eventFilter(self, obj, event) -> bool:
        """Take the shortcuts of this window and of nothing else."""

        kind = event.type()
        if kind not in (
            QtCore.QEvent.Type.KeyPress,
            QtCore.QEvent.Type.KeyRelease,
        ):
            return super().eventFilter(obj, event)
        if self._is_text_input(obj):
            return super().eventFilter(obj, event)
        if obj is self:
            if not self.isActiveWindow():
                return super().eventFilter(obj, event)
        elif not isinstance(obj, QtWidgets.QDialogButtonBox):
            return super().eventFilter(obj, event)
        if kind == QtCore.QEvent.Type.KeyPress:
            if self._on_key_press(event):
                return True
            return super().eventFilter(obj, event)
        if self._on_key_release(event):
            return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event) -> None:
        """Fallback for a key that reaches the window directly."""

        if self._on_key_press(event):
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:
        """Fallback for a released key."""

        if self._on_key_release(event):
            event.accept()
            return
        super().keyReleaseEvent(event)

    # ------------------------------------------------------------ closing

    def closeEvent(self, event) -> None:
        """Stop every background job before the window goes away."""

        self._stop_repeat()
        try:
            self._debounce_timer.stop()
        except Exception:
            pass
        job = self._pick_job
        if job is not None:
            try:
                job.requestInterruption()
                job.cancel()
                job.wait(2000)
            except Exception:
                pass
            self._pick_job = None
        worker = self._scan_worker
        if worker is not None:
            try:
                worker.requestInterruption()
                worker.wait(2000)
            except Exception:
                pass
            self._scan_worker = None
        try:
            self._preloader.invalidate()
        except Exception:
            pass
        try:
            self.removeEventFilter(self)
        except Exception:
            pass
        self._filter_installed = False
        super().closeEvent(event)
        # The window is a per widget singleton: closing it releases it,
        # so the next call of the launcher builds a fresh one.
        self.deleteLater()


PreviewDialog.FILTER_DEBOUNCE_MS = FILTER_DEBOUNCE_MS
PreviewDialog.PICK_PROBE_DEBOUNCE_MS = PICK_PROBE_DEBOUNCE_MS
PreviewDialog.HOLD_DELAY_MS = HOLD_DELAY_MS
