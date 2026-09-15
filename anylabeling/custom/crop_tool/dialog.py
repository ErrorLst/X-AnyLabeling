"""Crop window: a source folder, a fixed box, one crop per click.

The window shows the top level images of one folder on its left and the
selected image on its right. Every image carries a badge, the number of
crops the output folder already holds for it, read back from the crop
file names, so the same number is drawn in the list, in the status bar
and on the green outlines of the view.

Only two things ever touch a disk:

* crop_current writes one crop into the output folder through
  crop_core.crop_image, which never overwrites a name;
* delete_marks removes crops of the current image through
  crop_core.delete_crops, after an explicit confirmation whose default
  answer is 'no'.

Nothing else is written: no json side car, no temporary file, no
settings next to the images. The input folder is opened read only and
never changes.

The keyboard is handled in one place, CropDialog.keyPressEvent, which
is also fed by an event filter so that the shortcuts work wherever the
focus sits: A and D switch images (Left and Right are aliases),
Delete removes the crops of the current image and Escape closes the
window. Return and Enter mean nothing at all; the only crop trigger is
the right mouse button.
"""

from __future__ import annotations

import os.path as osp

from PyQt6 import QtCore, QtGui, QtWidgets

from anylabeling.views.labeling.utils import qt as qt_utils

from . import crop_core
from .settings import CropSettings
from .viewer import CropView

__all__ = [
    "ACTION_ICON",
    "ACTION_TEXT",
    "ACTION_TIP",
    "BAD_DIR_TEXT",
    "BOX_FORMAT",
    "COORD_FORMAT",
    "COUNT_FORMAT",
    "CROP_FAIL_TITLE",
    "CropDialog",
    "DELETED_FORMAT",
    "DELETED_SKIPPED_FORMAT",
    "DELETE_CONFIRM_FORMAT",
    "DELETE_TITLE",
    "DROP_EXTRA_FOLDER_REASON",
    "DROP_FILE_REASON",
    "DROP_IGNORED_PREFIX",
    "DROP_ONLY_FOLDERS",
    "EMPTY_HINT",
    "HINT_TEXT",
    "INDEX_FORMAT",
    "KEYS_TEXT",
    "LIST_MIN_WIDTH",
    "MIN_WINDOW_SIZE",
    "NO_CROPS_TEXT",
    "NO_IMAGES_TEXT",
    "NO_IMAGE_TEXT",
    "SAVED_FORMAT",
    "SMALL_IMAGE_TEXT",
    "WINDOW_SIZE",
    "WINDOW_TITLE",
    "install_crop_tool",
    "launch_crop_tool",
]

WINDOW_TITLE = "裁图工具"
ACTION_TEXT = "裁图工具"
ACTION_ICON = "crop"
ACTION_TIP = "拖入图片目录，固定 W×H 裁切框逐张裁图，只输出裁好的图片"
WINDOW_SIZE = (1100, 720)
MIN_WINDOW_SIZE = (760, 520)
LIST_MIN_WIDTH = 200

EMPTY_HINT = "把图片目录拖到这里，或点「打开目录」"
HINT_TEXT = "A/D 换图 · 滚轮缩放 · 左键平移 · 右键裁切 · Del 删裁切 · Esc 关闭"
KEYS_TEXT = "A/D 换图 · 右键裁切 · Del 删裁切"

DROP_ONLY_FOLDERS = "仅支持文件夹，未改动当前目录"
DROP_FILE_REASON = "仅支持文件夹"
DROP_EXTRA_FOLDER_REASON = "仅取第一个文件夹"
DROP_IGNORED_PREFIX = "已忽略: "

NO_IMAGES_TEXT = "该目录里没有可裁切的图片"
BAD_DIR_TEXT = "目录不可读：%s"
NO_IMAGE_TEXT = "当前图片不可裁切（未加载成功）"
NO_CROPS_TEXT = "当前图片没有裁切子图"

CROP_FAIL_TITLE = "裁切失败"
DELETE_TITLE = "删除裁切子图"
DELETE_CONFIRM_FORMAT = "将删除 %d 个裁切子图：\n%s\n\n此操作不可撤销，是否继续？"
DELETED_FORMAT = "已删除 %d 个裁切子图"
DELETED_SKIPPED_FORMAT = "已删除 %d 个裁切子图，跳过 %d 个：%s"
SAVED_FORMAT = "已保存: %s"
SMALL_IMAGE_TEXT = "图片小于裁切框，右侧/下侧填 0"

COORD_FORMAT = "坐标: (%d, %d)"
COUNT_FORMAT = "已裁切: %d 次"
BOX_FORMAT = "框: %d×%d"
INDEX_FORMAT = "%d / %d"

INPUT_BUTTON_TEXT = "打开目录"
OUTPUT_BUTTON_TEXT = "输出目录"
INPUT_PICKER_TITLE = "选择图片目录"
OUTPUT_PICKER_TITLE = "选择输出目录"
INPUT_LABEL_TEXT = "输入：未选择"
OUTPUT_LABEL_TEXT = "输出：未选择"
EMPTY_COUNT_TEXT = ""
MESSAGE_TIMEOUT = 3000
STATUS_PATH_LIMIT = 260
LABEL_MIN_WIDTH = 150
SPIN_MAX_WIDTH = 80
SPLITTER_SIZES = (LIST_MIN_WIDTH, 880)


def install_crop_tool(widget):
    """Attach the crop action to the Tool menu of a widget.

    Installing twice is a no op, so the mount point can be called again
    without adding a second entry to the menu.

    Args:
        widget: The labeling widget to extend. It has to expose
            menus.tool.

    Returns:
        The installed QAction, or None when the widget has no Tool
        menu.
    """

    existing = getattr(widget, "_crop_tool_action", None)
    if existing is not None:
        return existing
    menus = getattr(widget, "menus", None)
    menu = getattr(menus, "tool", None)
    if menu is None:
        return None
    action = qt_utils.new_action(
        menu,
        ACTION_TEXT,
        lambda _checked=False: launch_crop_tool(widget),
        icon=ACTION_ICON,
        tip=ACTION_TIP,
    )
    menu.addAction(action)
    widget._crop_tool_action = action
    return action


def launch_crop_tool(parent=None):
    """Lazy wrapper around the launcher, keeping imports acyclic."""

    from .launcher import launch_crop_tool as _launch

    return _launch(parent)


def _spin(low, high) -> QtWidgets.QSpinBox:
    """Return one crop parameter spin box."""

    box = QtWidgets.QSpinBox()
    box.setRange(low, high)
    box.setMaximumWidth(SPIN_MAX_WIDTH)
    box.setKeyboardTracking(False)
    return box


def _button(text, slot) -> QtWidgets.QPushButton:
    """Return a button that never becomes the default of the dialog.

    Enter must not trigger anything here: the crop is a mouse only
    action, so no button of this window may claim the default role.
    """

    button = QtWidgets.QPushButton(text)
    button.setAutoDefault(False)
    button.clicked.connect(slot)
    return button


def _stem_of(name) -> str:
    """Return the stem of a base name without its extension."""

    return osp.splitext(name)[0]


class CropDialog(QtWidgets.QDialog):
    """The crop window: a file list, a view and one shared cache.

    The cache of the window maps the stem of a source image to the
    crops the output folder holds for it. It is the single source of
    the badges, of the marks of the view and of the counter of the
    status bar, and it is rebuilt whenever a crop is added or removed
    and whenever the output folder changes.
    """

    def __init__(self, parent=None, settings=None):
        """Build the window and restore the last source folder.

        Args:
            parent: The parent widget, usually the labeling window.
            settings: The parameter store. None builds the QSettings
                backed store of the application.
        """

        super().__init__(parent)
        self._settings = (
            settings if settings is not None else CropSettings()
        )
        self._entries = []
        self._crops = {}
        self._input_dir = ""
        self.setWindowTitle(WINDOW_TITLE)
        self.resize(*WINDOW_SIZE)
        self.setMinimumSize(*MIN_WINDOW_SIZE)
        self.setAcceptDrops(True)
        self._build_ui()
        self._init_values()
        self._refresh_path_labels()
        self._show_empty_hint()

    # ---------------------------------------------------------------- UI

    def _build_ui(self) -> None:
        """Build the control row, the splitter and the status bar."""

        root = QtWidgets.QVBoxLayout(self)
        root.setSpacing(6)
        root.addLayout(self._build_top_row())
        self.hint_label = QtWidgets.QLabel(HINT_TEXT)
        root.addWidget(self.hint_label)
        self._build_splitter(root)
        self._build_status_bar(root)
        self.installEventFilter(self)
        self._watch_key_eaters()

    def _watch_key_eaters(self) -> None:
        """Filter the keys of the children that would eat them first.

        An event filter installed on the dialog alone never sees an
        event that is delivered to a child: the key would be handled
        by that child and never reach CropDialog.keyPressEvent. The
        spin boxes are the only control of this window that does so,
        because the line edit they embed takes A, D, the arrows and
        Delete for its own text editing, so the filter is installed on
        every one of them and on the line edit they embed.
        """

        for box in (
            self.width_box,
            self.height_box,
            self.pad_x_box,
            self.pad_y_box,
        ):
            box.installEventFilter(self)
            editor = box.findChild(QtWidgets.QLineEdit)
            if editor is not None:
                editor.installEventFilter(self)

    def _build_top_row(self) -> QtWidgets.QHBoxLayout:
        """Build the row of the buttons, the paths and the spin boxes."""

        row = QtWidgets.QHBoxLayout()
        self.input_button = _button(INPUT_BUTTON_TEXT, self.pick_input_dir)
        row.addWidget(self.input_button)
        self.input_label = QtWidgets.QLabel(INPUT_LABEL_TEXT)
        self.input_label.setMinimumWidth(LABEL_MIN_WIDTH)
        row.addWidget(self.input_label, stretch=1)
        self.width_box = _spin(1, crop_core.SIZE_MAX)
        self.height_box = _spin(1, crop_core.SIZE_MAX)
        self.pad_x_box = _spin(0, crop_core.SIZE_MAX)
        self.pad_y_box = _spin(0, crop_core.SIZE_MAX)
        for text, box in (
            ("W:", self.width_box),
            ("H:", self.height_box),
            ("Pad X:", self.pad_x_box),
            ("Pad Y:", self.pad_y_box),
        ):
            row.addWidget(QtWidgets.QLabel(text))
            row.addWidget(box)
        self.output_button = _button(
            OUTPUT_BUTTON_TEXT, self.pick_output_dir
        )
        row.addWidget(self.output_button)
        self.output_label = QtWidgets.QLabel(OUTPUT_LABEL_TEXT)
        self.output_label.setMinimumWidth(LABEL_MIN_WIDTH)
        row.addWidget(self.output_label, stretch=1)
        return row

    def _build_splitter(self, root) -> None:
        """Build the file list and the view, side by side."""

        self.list = QtWidgets.QListWidget()
        self.list.setMinimumWidth(LIST_MIN_WIDTH)
        self.list.setUniformItemSizes(True)
        self.list.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.list.setAcceptDrops(False)
        self.viewer = CropView(self)
        self.viewer.setAcceptDrops(False)
        self.splitter = QtWidgets.QSplitter(
            QtCore.Qt.Orientation.Horizontal
        )
        self.splitter.addWidget(self.list)
        self.splitter.addWidget(self.viewer)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes(list(SPLITTER_SIZES))
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(1)
        root.addWidget(self.splitter, stretch=1)

    def _build_status_bar(self, root) -> None:
        """Build the status bar with its two permanent zones."""

        self.status_bar = QtWidgets.QStatusBar()
        self.input_path_label = QtWidgets.QLabel("")
        self.input_path_label.setMinimumWidth(120)
        self.output_path_label = QtWidgets.QLabel("")
        self.output_path_label.setMinimumWidth(120)
        for label in (self.input_path_label, self.output_path_label):
            self.status_bar.addWidget(label)
        self.coord_label = QtWidgets.QLabel(COORD_FORMAT % (0, 0))
        self.count_label = QtWidgets.QLabel(COUNT_FORMAT % 0)
        self.box_label = QtWidgets.QLabel(BOX_FORMAT % (1, 1))
        self.index_label = QtWidgets.QLabel(EMPTY_COUNT_TEXT)
        self.keys_label = QtWidgets.QLabel(KEYS_TEXT)
        for label in (
            self.coord_label,
            self.count_label,
            self.box_label,
            self.index_label,
            self.keys_label,
        ):
            self.status_bar.addPermanentWidget(label)
        root.addWidget(self.status_bar)

    def _init_values(self) -> None:
        """Push the stored parameters into the widgets and connect them."""

        for box, value in (
            (self.width_box, self._settings.width()),
            (self.height_box, self._settings.height()),
            (self.pad_x_box, self._settings.pad_width()),
            (self.pad_y_box, self._settings.pad_height()),
        ):
            box.blockSignals(True)
            box.setValue(value)
            box.blockSignals(False)
        self._sync_crop_size()
        self._sync_pad()
        self._refresh_path_labels()
        self._refresh_status()
        self.list.currentRowChanged.connect(self._on_row_changed)
        self.viewer.crop_requested.connect(self._on_crop_requested)
        self.viewer.cursor_moved.connect(self._on_cursor_moved)
        self.width_box.valueChanged.connect(self._on_width_changed)
        self.height_box.valueChanged.connect(self._on_height_changed)
        self.pad_x_box.valueChanged.connect(self._on_pad_x_changed)
        self.pad_y_box.valueChanged.connect(self._on_pad_y_changed)

    # ------------------------------------------------------------ 目录

    def set_input_dir(self, directory) -> list:
        """Open a source folder, scan it and show its first image.

        The folder lives in memory only: nothing about it is stored,
        so the window always opens empty and never reopens a folder
        the user picked in an earlier session.
        """

        self._input_dir = osp.abspath(directory) if directory else ""
        self._refresh_path_labels()
        self._entries = self._scan()
        self.rebuild_crops()
        self._fill_list()
        if self._entries:
            self.show_image(0)
        else:
            self._show_empty_hint()
        return list(self._entries)

    def input_dir(self) -> str:
        """Return the source folder currently open, '' when none."""

        return self._input_dir

    def output_dir(self) -> str:
        """Return the folder the crops are written into."""

        return self._settings.output_dir()

    def rescan(self) -> list:
        """Scan both folders again, keeping the current image by path."""

        path = self.current_path()
        self._entries = self._scan()
        self._fill_list()
        index = self._index_of(path)
        if self._entries:
            self.show_image(index if index >= 0 else 0)
        else:
            self._show_empty_hint()
        return list(self._entries)

    def set_output_dir(self, directory) -> None:
        """Store a new output folder, then rebuild and rescan."""

        self._settings.set_output_dir(directory)
        self._refresh_path_labels()
        self.rebuild_crops()
        self.rescan()

    def pick_input_dir(self) -> None:
        """Ask for the source folder and open it."""

        start = self.input_dir() or self.output_dir()
        chosen = QtWidgets.QFileDialog.getExistingDirectory(
            self, INPUT_PICKER_TITLE, start
        )
        if chosen:
            self.set_input_dir(chosen)

    def pick_output_dir(self) -> None:
        """Ask for the output folder and rescan."""

        chosen = QtWidgets.QFileDialog.getExistingDirectory(
            self, OUTPUT_PICKER_TITLE, self.output_dir()
        )
        if chosen:
            self.set_output_dir(chosen)

    def _scan(self) -> list:
        """Return the images of the source folder, its own crops excluded."""

        return crop_core.scan_directory(self.input_dir(), self.output_dir())

    def _index_of(self, path) -> int:
        """Return the row of a path, or -1 when it is not listed."""

        if not path:
            return -1
        for index, entry in enumerate(self._entries):
            if entry.path == path:
                return index
        return -1

    def _refresh_path_labels(self) -> None:
        """Write both folders into the labels and into the status bar."""

        source = self.input_dir()
        target = self.output_dir()
        self.input_label.setText(source or INPUT_LABEL_TEXT)
        self.input_label.setToolTip(source)
        self.output_label.setText(target or OUTPUT_LABEL_TEXT)
        self.output_label.setToolTip(target)
        self._elide_paths()

    def _elide_paths(self) -> None:
        """Elide the two folder labels of the status bar in their middle."""

        for label, path in (
            (self.input_path_label, self.input_dir()),
            (self.output_path_label, self.output_dir()),
        ):
            if not path:
                label.setText("")
                label.setToolTip("")
                continue
            label.setToolTip(path)
            metrics = QtGui.QFontMetrics(label.font())
            label.setText(
                metrics.elidedText(
                    path,
                    QtCore.Qt.TextElideMode.ElideMiddle,
                    STATUS_PATH_LIMIT,
                )
            )

    # ------------------------------------------------------------ 列表

    def _fill_list(self) -> None:
        """Rewrite the file list from the current scan."""

        self.list.blockSignals(True)
        self.list.clear()
        for entry in self._entries:
            item = QtWidgets.QListWidgetItem(self._item_text(entry))
            item.setToolTip(entry.path)
            self.list.addItem(item)
        self.list.blockSignals(False)

    def _item_text(self, entry) -> str:
        """Return the label of a list row, with its crop badge."""

        count = len(self.crops_of(_stem_of(entry.name)))
        if count <= 0:
            return entry.name
        return "%s  ✓%d" % (entry.name, count)

    def _refresh_badges(self) -> None:
        """Rewrite the badge of every row from the cache."""

        for index, entry in enumerate(self._entries):
            item = self.list.item(index)
            if item is not None:
                item.setText(self._item_text(entry))

    def _on_row_changed(self, index) -> None:
        """Show the image of a row the user clicked."""

        if 0 <= index < len(self._entries):
            self.show_image(index)

    # ------------------------------------------------------------ 显示

    def image_count(self) -> int:
        """Return how many images the source folder holds."""

        return len(self._entries)

    def current_index(self) -> int:
        """Return the row of the shown image, -1 when none is shown."""

        return self.list.currentRow()

    def current_path(self) -> str:
        """Return the path of the shown image, '' when none is shown."""

        index = self.current_index()
        if 0 <= index < len(self._entries):
            return self._entries[index].path
        return ""

    def show_image(self, index) -> bool:
        """Show one image of the list and refresh marks and counters."""

        if not 0 <= index < len(self._entries):
            return False
        self.list.blockSignals(True)
        self.list.setCurrentRow(index)
        self.list.blockSignals(False)
        self.viewer.load_image(self._entries[index].path)
        # The box keeps its width and height across images and recenters
        # inside the new one, so a small image never clips it.
        self.viewer.center_crop()
        self.viewer.set_marks(self.marks_of_current())
        self._refresh_status()
        self._update_size_hint()
        return True

    def next_image(self) -> bool:
        """Show the next image; False at the end of the list."""

        index = self.current_index()
        if index + 1 >= len(self._entries):
            return False
        return self.show_image(index + 1)

    def prev_image(self) -> bool:
        """Show the previous image; False at the start of the list."""

        index = self.current_index()
        if index <= 0:
            return False
        return self.show_image(index - 1)

    def _show_empty_hint(self) -> None:
        """Report an empty or unreadable folder in the status bar."""

        self.viewer.clear_image()
        source = self.input_dir()
        if not source:
            self.status_bar.showMessage(EMPTY_HINT)
            return
        if not osp.isdir(source):
            self.status_bar.showMessage(BAD_DIR_TEXT % source)
            return
        self.status_bar.showMessage(NO_IMAGES_TEXT)

    # ------------------------------------------------------------ 裁切

    def crop_current(self, x=None, y=None):
        """Crop the current box and return the new path, else None."""

        path = self.current_path()
        if not path:
            self.status_bar.showMessage(NO_IMAGE_TEXT, MESSAGE_TIMEOUT)
            return None
        if x is None or y is None:
            x, y = self.viewer.crop_pos()
        width, height = self.viewer.crop_size()
        pad_x, pad_y = self.viewer.pad_size()
        try:
            result = crop_core.crop_image(
                path,
                x,
                y,
                width,
                height,
                pad_x=pad_x,
                pad_y=pad_y,
                output_dir=self.output_dir(),
            )
        except crop_core.CropError as error:
            self.status_bar.showMessage(str(error), MESSAGE_TIMEOUT)
            QtWidgets.QMessageBox.warning(
                self, CROP_FAIL_TITLE, str(error)
            )
            return None
        stem = _stem_of(osp.basename(path))
        self._crops.setdefault(stem, []).append(result.record)
        self._refresh_badges()
        self.viewer.set_marks(self.marks_of_current())
        self._refresh_status()
        self.status_bar.showMessage(
            SAVED_FORMAT % osp.basename(result.path), MESSAGE_TIMEOUT
        )
        return result.path

    def _on_crop_requested(self, _x=0, _y=0) -> None:
        """Crop after the view asked for it, through the right button."""

        self.crop_current()

    def _on_cursor_moved(self, x, y) -> None:
        """Report the pointer and center the crop box on it.

        The view reports (-1, -1) as soon as the pointer sits outside
        the image; the box then stays where it is instead of jumping
        to the top left corner. A pan reports the pointer as well, so
        the box keeps following the cursor while the left button is
        held.
        """

        self.coord_label.setText(COORD_FORMAT % (x, y))
        if x >= 0 and y >= 0:
            self.viewer.center_crop_on(x, y)

    # ------------------------------------------------------------ 删除

    def delete_marks(self):
        """Delete the crops of the current image after a confirmation."""

        records = self.crops_of(self.current_stem())
        if not records:
            self.status_bar.showMessage(NO_CROPS_TEXT, MESSAGE_TIMEOUT)
            return None
        listing = "\n".join(record.path for record in records)
        answer = QtWidgets.QMessageBox.question(
            self,
            DELETE_TITLE,
            DELETE_CONFIRM_FORMAT % (len(records), listing),
            QtWidgets.QMessageBox.StandardButton.Yes
            | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return None
        deleted, skipped = crop_core.delete_crops(
            [record.path for record in records], self.output_dir()
        )
        self.rebuild_crops()
        self._refresh_badges()
        self.viewer.set_marks(self.marks_of_current())
        self._refresh_status()
        if skipped:
            reasons = "; ".join(reason for _path, reason in skipped)
            self.status_bar.showMessage(
                DELETED_SKIPPED_FORMAT
                % (len(deleted), len(skipped), reasons),
                MESSAGE_TIMEOUT,
            )
        else:
            self.status_bar.showMessage(
                DELETED_FORMAT % len(deleted), MESSAGE_TIMEOUT
            )
        return (deleted, skipped)

    def current_stem(self) -> str:
        """Return the stem of the shown image, '' when none is shown."""

        return _stem_of(osp.basename(self.current_path()))

    def crops_of(self, stem) -> list:
        """Return the crops the output folder holds for one stem."""

        if not stem:
            return []
        return list(self._crops.get(stem, []))

    def marks_of_current(self) -> list:
        """Return the (x, y, w, h) rectangles of the shown image."""

        return [
            (record.x, record.y, record.w, record.h)
            for record in self.crops_of(self.current_stem())
        ]

    def rebuild_crops(self) -> dict:
        """Read the output folder again and refresh what is derived."""

        self._crops = crop_core.scan_crops(self.output_dir())
        self._refresh_badges()
        self.viewer.set_marks(self.marks_of_current())
        self._refresh_status()
        return self._crops

    # ---------------------------------------------------------- 状态栏

    def _refresh_status(self) -> None:
        """Rewrite every permanent label of the status bar."""

        index = self.current_index()
        total = self.image_count()
        self.index_label.setText(
            INDEX_FORMAT % (index + 1, total) if total else EMPTY_COUNT_TEXT
        )
        self.count_label.setText(
            COUNT_FORMAT % len(self.crops_of(self.current_stem()))
        )
        self.box_label.setText(BOX_FORMAT % self.viewer.crop_size())
        self._elide_paths()

    def _update_size_hint(self) -> None:
        """Show or withdraw the small image hint of the status bar.

        The box keeps its width and height across images, so the
        current image can be smaller than the box at any time; the
        hint then says that the right and the bottom band are filled
        with 0. An empty view is not a small image: without an image
        the hint is only ever withdrawn, never raised. The hint is
        withdrawn only while the status bar still shows that very
        text, so a parallel message such as "已保存: x" is kept.
        """

        width, height = self.viewer.image_size()
        box_width, box_height = self.viewer.crop_size()
        if width <= 0 or height <= 0:
            if self.status_bar.currentMessage() == SMALL_IMAGE_TEXT:
                self.status_bar.clearMessage()
            return
        if width < box_width or height < box_height:
            self.status_bar.showMessage(SMALL_IMAGE_TEXT)
            return
        if self.status_bar.currentMessage() == SMALL_IMAGE_TEXT:
            self.status_bar.clearMessage()

    # ------------------------------------------------------------ 参数

    def _sync_crop_size(self) -> None:
        """Push the two size spin boxes into the view."""

        self.viewer.set_crop_size(
            self.width_box.value(), self.height_box.value()
        )
        self._update_size_hint()

    def _sync_pad(self) -> None:
        """Push the two padding spin boxes into the view."""

        self.viewer.set_pad(self.pad_x_box.value(), self.pad_y_box.value())

    def _on_width_changed(self, value) -> None:
        """Store the new crop width and resize the box."""

        self._settings.set_width(value)
        self._sync_crop_size()
        self._refresh_status()

    def _on_height_changed(self, value) -> None:
        """Store the new crop height and resize the box."""

        self._settings.set_height(value)
        self._sync_crop_size()
        self._refresh_status()

    def _on_pad_x_changed(self, value) -> None:
        """Store the new horizontal padding and redraw the box."""

        self._settings.set_pad_width(value)
        self._sync_pad()

    def _on_pad_y_changed(self, value) -> None:
        """Store the new vertical padding and redraw the box."""

        self._settings.set_pad_height(value)
        self._sync_pad()

    # ------------------------------------------------------------ 拖放

    def dragEnterEvent(self, event) -> None:
        """Accept a drag as soon as it carries at least one folder."""

        self.accept_folders(event)

    def dragMoveEvent(self, event) -> None:
        """Keep the same answer while the drag moves over the window.

        Qt only delivers the drop once the move event is accepted, so
        this repeats the folder test of dragEnterEvent through the same
        helper instead of leaving the event to the default handler,
        which would ignore it and cancel the drop.
        """

        self.accept_folders(event)

    def accept_folders(self, event) -> bool:
        """Accept a drag event exactly when it carries a folder."""

        folders, _ignored = self.drop_entries(event.mimeData())
        if folders:
            event.acceptProposedAction()
            return True
        event.ignore()
        return False

    def dropEvent(self, event) -> None:
        """Open the first dropped folder, ignoring everything else."""

        folders, ignored = self.drop_entries(event.mimeData())
        if not folders:
            self.status_bar.showMessage(DROP_ONLY_FOLDERS, MESSAGE_TIMEOUT)
            event.ignore()
            return
        self.set_input_dir(folders[0])
        if ignored:
            self.status_bar.showMessage(
                self.ignored_text(ignored), MESSAGE_TIMEOUT
            )
        event.acceptProposedAction()

    def drop_entries(self, mime):
        """Split a drop into its folders and its ignored items.

        Args:
            mime: The QMimeData of the drop, or None.

        Returns:
            A (folders, ignored) pair. folders holds the absolute paths
            of the dropped folders in drop order; ignored holds one
            (name, reason) pair per entry that was not used.
        """

        if mime is None or not mime.hasUrls():
            return [], []
        folders = []
        ignored = []
        for url in mime.urls():
            path = url.toLocalFile()
            if not path:
                continue
            if not osp.isdir(path):
                ignored.append((osp.basename(path), DROP_FILE_REASON))
                continue
            full = osp.abspath(path)
            if folders:
                ignored.append(
                    (osp.basename(full), DROP_EXTRA_FOLDER_REASON)
                )
            else:
                folders.append(full)
        return folders, ignored

    def ignored_text(self, ignored) -> str:
        """Return the status line that lists the ignored drop items."""

        parts = ["%s（%s）" % (name, reason) for name, reason in ignored]
        return DROP_IGNORED_PREFIX + "、".join(parts)

    # ------------------------------------------------------------ 键盘

    def keyPressEvent(self, event) -> None:
        """Turn a navigation key into an action.

        This is the only keyboard entry point of the tool. There is no
        Return or Enter branch on purpose: the crop is a mouse only
        action, so a stray Enter can never write a file.
        """

        if self._handle_key(event.key()):
            event.accept()
            return
        super().keyPressEvent(event)

    def _handle_key(self, key) -> bool:
        """Run the action of one key; False when the key has none."""

        if key in (QtCore.Qt.Key.Key_A, QtCore.Qt.Key.Key_Left):
            self.prev_image()
        elif key in (QtCore.Qt.Key.Key_D, QtCore.Qt.Key.Key_Right):
            self.next_image()
        elif key == QtCore.Qt.Key.Key_Delete:
            self.delete_marks()
        elif key == QtCore.Qt.Key.Key_Escape:
            self.close()
        else:
            return False
        return True

    def eventFilter(self, watched, event):
        """Route the shortcuts to the dialog from wherever focus sits.

        The view deliberately ignores the navigation keys instead of
        handling them, and the focus can sit on a spin box, whose line
        edit would otherwise take those keys for its own text editing,
        so the dialog is filtered onto both of them (see
        _watch_key_eaters). A handled key is accepted here and never
        reaches the child, so it can not be typed into a spin box.
        """

        if (
            event.type() == QtCore.QEvent.Type.KeyPress
            and self.isAncestorOf(watched)
            and self._handle_key(event.key())
        ):
            event.accept()
            return True
        return super().eventFilter(watched, event)

    # ---------------------------------------------------------- 事件钩子

    def resizeEvent(self, event) -> None:
        """Keep the elided folder labels in step with the window."""

        super().resizeEvent(event)
        self._elide_paths()
