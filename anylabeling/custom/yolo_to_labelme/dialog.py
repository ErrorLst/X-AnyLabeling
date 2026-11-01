"""The YOLO to LabelMe window: pick, scan, convert, report.

The window has no editor and no modal popup: every problem is written to
the read only summary text or to the one status line. The conversion is
synchronous (the upstream converter is fast per image) with
QCoreApplication.processEvents between files, so the progress bar moves
and the window stays responsive enough for a normal dataset. The window
is disabled while a conversion runs and refuses to close, which keeps the
source folder and the written json files consistent.
"""

from __future__ import annotations

import os.path as osp

from PyQt6 import QtCore, QtWidgets

from anylabeling.views.labeling.logger import logger

from . import conversion, yolo_core

__all__ = [
    "MAX_REPORTED_ERRORS",
    "WINDOW_SIZE",
    "WINDOW_TITLE",
    "YoloToLabelmeDialog",
]

WINDOW_TITLE = "YOLO 转 LabelMe"
WINDOW_SIZE = (860, 640)
SUMMARY_MIN_HEIGHT = 260
MAX_REPORTED_ERRORS = 20

SOURCE_LABEL = "数据集目录："
CLASS_LABEL = "类别文件 class.txt："
SOURCE_PLACEHOLDER = "选择含图片与同名 txt 的 YOLO 导出目录"
CLASS_PLACEHOLDER = "选择每行一个类名的 class.txt / label.txt"
BROWSE_TEXT = "浏览…"
MODE_LABEL = "标注类型："
OVERWRITE_TEXT = "覆盖已有同名 json（会改写原标注）"
RESCAN_TEXT = "重新扫描"
CONVERT_TEXT = "开始转换"
CLOSE_TEXT = "关闭"
SUMMARY_PLACEHOLDER = "选择数据集目录与 class.txt 后自动扫描"
INITIAL_STATUS = "选择数据集目录与 class.txt 后自动扫描"
BUSY_STATUS = "转换进行中，请稍候"
CLOSE_REFUSED = "转换进行中，请稍候再关闭"
NO_SOURCE = "请选择数据集目录"
NO_CLASSES = "请选择类别文件 class.txt"
SCAN_FAILED = "扫描失败：%s"
CONVERT_FAILED = "转换失败：%s"
DONE_FORMAT = (
    "转换完成：成功 %d，空标注 %d，跳过已有 %d，失败 %d"
)
CANCELLED_TEXT = "转换已取消（已完成的部分保留）"
DROP_IGNORED = "仅支持文件夹与 .txt 类别文件"
DROP_EXTRA = "只取第一个目录和第一个类别文件"


def _selectable_path_edit(placeholder: str) -> QtWidgets.QLineEdit:
    """Return the read only path display of one input row."""

    edit = QtWidgets.QLineEdit()
    edit.setReadOnly(True)
    edit.setPlaceholderText(placeholder)
    return edit


class YoloToLabelmeDialog(QtWidgets.QDialog):
    """One window: two inputs, a mode, a summary and one button."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._source_dir = ""
        self._class_file = ""
        self._scan = None
        self._busy = False
        self.setWindowTitle(WINDOW_TITLE)
        self.resize(*WINDOW_SIZE)
        self.setAcceptDrops(True)
        self._build_ui()
        self._set_status(INITIAL_STATUS)

    # ------------------------------------------------------------- UI

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setSpacing(8)

        self.source_edit = _selectable_path_edit(SOURCE_PLACEHOLDER)
        root.addLayout(
            self._path_row(
                SOURCE_LABEL, self.source_edit, self.pick_source_dir
            )
        )

        self.class_edit = _selectable_path_edit(CLASS_PLACEHOLDER)
        root.addLayout(
            self._path_row(CLASS_LABEL, self.class_edit, self.pick_class_file)
        )

        options = QtWidgets.QHBoxLayout()
        options.addWidget(QtWidgets.QLabel(MODE_LABEL))
        self.mode_combo = QtWidgets.QComboBox()
        for mode in (
            yolo_core.MODE_AUTO,
            yolo_core.MODE_RECTANGLE,
            yolo_core.MODE_POLYGON,
            yolo_core.MODE_OBB,
        ):
            self.mode_combo.addItem(yolo_core.MODE_NAMES[mode], mode)
        options.addWidget(self.mode_combo)
        self.overwrite_check = QtWidgets.QCheckBox(OVERWRITE_TEXT)
        self.overwrite_check.setChecked(False)
        options.addWidget(self.overwrite_check)
        options.addStretch(1)
        root.addLayout(options)

        self.summary = QtWidgets.QPlainTextEdit()
        self.summary.setReadOnly(True)
        self.summary.setLineWrapMode(
            QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap
        )
        self.summary.setPlaceholderText(SUMMARY_PLACEHOLDER)
        self.summary.setMinimumHeight(SUMMARY_MIN_HEIGHT)
        root.addWidget(self.summary, 1)

        self.progress = QtWidgets.QProgressBar()
        self.progress.setTextVisible(True)
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        self.status_label = QtWidgets.QLabel("")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        buttons = QtWidgets.QHBoxLayout()
        self.rescan_button = QtWidgets.QPushButton(RESCAN_TEXT)
        self.rescan_button.setAutoDefault(False)
        self.rescan_button.clicked.connect(self.rescan)
        buttons.addWidget(self.rescan_button)
        buttons.addStretch(1)
        self.convert_button = QtWidgets.QPushButton(CONVERT_TEXT)
        self.convert_button.setAutoDefault(False)
        self.convert_button.clicked.connect(self.convert)
        buttons.addWidget(self.convert_button)
        self.close_button = QtWidgets.QPushButton(CLOSE_TEXT)
        self.close_button.setAutoDefault(False)
        self.close_button.clicked.connect(self.close)
        buttons.addWidget(self.close_button)
        root.addLayout(buttons)

        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        self.overwrite_check.toggled.connect(self._refresh_buttons)
        self._refresh_buttons()

    def _path_row(self, label, edit, slot):
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel(label))
        row.addWidget(edit, 1)
        button = QtWidgets.QPushButton(BROWSE_TEXT)
        button.setAutoDefault(False)
        button.clicked.connect(slot)
        row.addWidget(button)
        return row

    # ---------------------------------------------------------- inputs

    def set_source_dir(self, path):
        """Set the dataset directory and rescan when possible."""

        if not path or not osp.isdir(path):
            self._set_status("目录不存在：%s" % (path,))
            return
        self._source_dir = osp.abspath(path)
        self.source_edit.setText(self._source_dir)
        self.rescan()

    def set_class_file(self, path):
        """Set the classes file and rescan when possible."""

        if not path or not osp.isfile(path):
            self._set_status("文件不存在：%s" % (path,))
            return
        self._class_file = osp.abspath(path)
        self.class_edit.setText(self._class_file)
        self.rescan()

    def pick_source_dir(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self, "选择数据集目录", self._source_dir
        )
        if path:
            self.set_source_dir(path)

    def pick_class_file(self):
        path, _selected = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "选择类别文件 class.txt",
            self._class_file,
            "Text Files (*.txt);;All Files (*)",
        )
        if path:
            self.set_class_file(path)

    def _mode_changed(self, _index):
        self.rescan()

    def _mode(self):
        return self.mode_combo.currentData() or yolo_core.MODE_AUTO

    # ------------------------------------------------------------ scan

    def rescan(self):
        """Scan the two inputs and refresh the summary."""

        if self._busy:
            return
        self._scan = None
        self._refresh_buttons()
        if not self._source_dir:
            self._show_scan_error(NO_SOURCE)
            return
        if not self._class_file:
            self._show_scan_error(NO_CLASSES)
            return
        try:
            scan = yolo_core.scan_dataset(
                self._source_dir, self._class_file, self._mode()
            )
        except yolo_core.YoloCoreError as error:
            self._show_scan_error(str(error))
            return
        except Exception as error:  # noqa: BLE001
            logger.exception(error)
            self._show_scan_error(str(error))
            return
        self._scan = scan
        self.summary.setPlainText(self._format_scan(scan))
        self._refresh_buttons()
        self._set_status(self._scan_status(scan))

    def _show_scan_error(self, message):
        self._scan = None
        self.summary.setPlainText(SCAN_FAILED % message)
        self._set_status(SCAN_FAILED % message)
        self._refresh_buttons()

    def _scan_status(self, scan):
        if scan.error:
            return scan.error
        if scan.collisions:
            return "存在输出同名冲突，已禁用转换"
        if not scan.pairs:
            if scan.hint_coco:
                return "没有 YOLO 配对；检测到 COCO 标注文件，本工具不处理 COCO"
            return "没有找到可转换的 YOLO 配对"
        return (
            "已扫描：%d 组配对，%d 个无效标注，%d 个已有 json"
            % (len(scan.pairs), len(scan.invalid), scan.existing_outputs)
        )

    def _format_scan(self, scan):
        names = ", ".join(scan.class_names[:12])
        if len(scan.class_names) > 12:
            names += ", …"
        layout = (
            "images/ + labels/ 分离"
            if scan.layout == yolo_core.LAYOUT_SPLIT
            else "同目录/递归"
        )
        counts = scan.mode_counts
        lines = [
            "数据集：%s" % scan.root,
            "布局：%s" % layout,
            "类别：%d 类（%s）" % (scan.class_count, names),
            (
                "图片：%d 张；已配对：%d 组；无同名 txt（跳过）：%d 张；"
                "仅 txt 无图片（忽略）：%d 个"
                % (
                    len(scan.pairs)
                    + len(scan.invalid)
                    + scan.images_without_label,
                    len(scan.pairs),
                    scan.images_without_label,
                    scan.orphan_labels,
                )
            ),
            (
                "标注格式：检测框 %d / 分割多边形 %d / 旋转框 %d / "
                "空标注 %d"
                % (
                    counts.get(yolo_core.MODE_RECTANGLE, 0),
                    counts.get(yolo_core.MODE_POLYGON, 0),
                    counts.get(yolo_core.MODE_OBB, 0),
                    counts.get("empty", 0),
                )
            ),
            (
                "输出：图片旁同名 json；已存在：%d 个（默认跳过）"
                % scan.existing_outputs
            ),
        ]
        if scan.invalid:
            lines.append("无效标注：%d 个" % len(scan.invalid))
            for pair in scan.invalid[:MAX_REPORTED_ERRORS]:
                lines.append(
                    "  %s: %s"
                    % (osp.basename(pair.image_path), pair.issue)
                )
            if len(scan.invalid) > MAX_REPORTED_ERRORS:
                lines.append("  …")
        if scan.collisions:
            lines.append("输出冲突：%d 个（已禁用转换）" % len(scan.collisions))
            for collision in scan.collisions[:MAX_REPORTED_ERRORS]:
                lines.append("  %s" % collision)
        if not scan.pairs and not scan.invalid:
            lines.append("没有找到可转换的 YOLO 配对。")
        if scan.hint_coco:
            lines.append(
                "提示：检测到 COCO 标注文件，本工具只处理 YOLO 同名 txt；"
                "COCO 请走 Upload -> Upload COCO Annotation 或 CLI "
                "coco2xlabel。"
            )
        return "\n".join(lines)

    # ------------------------------------------------------- convert

    def _all_outputs_exist(self):
        if not self._scan or not self._scan.pairs:
            return False
        return all(
            osp.isfile(pair.output_path) for pair in self._scan.pairs
        )

    def _refresh_buttons(self):
        scan = self._scan
        reason = ""
        if scan is None:
            reason = "尚未扫描"
        elif scan.collisions:
            reason = "存在输出同名冲突"
        elif not scan.pairs:
            reason = "没有可转换的配对"
        elif (
            self._all_outputs_exist()
            and not self.overwrite_check.isChecked()
        ):
            reason = "所有 json 已存在；勾选覆盖后可重写"
        self.convert_button.setEnabled(bool(reason == "" and scan and scan.ok))
        self.convert_button.setToolTip(reason)
        self.rescan_button.setEnabled(not self._busy)
        self.close_button.setEnabled(not self._busy)

    def convert(self):
        """Convert every ready pair and append the result to the summary."""

        if self._busy or self._scan is None or not self._scan.ok:
            return
        if self._all_outputs_exist() and not self.overwrite_check.isChecked():
            return
        pairs = list(self._scan.pairs)
        total = len(pairs)
        self._busy = True
        self._refresh_buttons()
        self.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, total)
        self.progress.setValue(0)
        self._set_status("转换中… 0 / %d" % total)
        try:
            result = conversion.convert_pairs(
                pairs,
                self._scan.class_names,
                overwrite=self.overwrite_check.isChecked(),
                on_progress=self._on_progress,
            )
        except Exception as error:  # noqa: BLE001
            logger.exception(error)
            self.setEnabled(True)
            self._busy = False
            self.progress.setVisible(False)
            self._set_status(CONVERT_FAILED % error)
            self.summary.setPlainText(
                self._format_scan(self._scan)
                + "\n\n"
                + (CONVERT_FAILED % error)
            )
            self._refresh_buttons()
            return
        self.setEnabled(True)
        self._busy = False
        self.progress.setVisible(False)
        self.summary.setPlainText(
            self._format_scan(self._scan)
            + "\n\n"
            + self._format_result(result)
        )
        self._set_status(
            DONE_FORMAT
            % (
                result.converted,
                result.empty,
                result.skipped_existing,
                len(result.failed),
            )
        )
        self._refresh_buttons()

    def _on_progress(self, done, total, _image):
        self.progress.setValue(done)
        self._set_status("转换中… %d / %d" % (done, total))
        QtCore.QCoreApplication.processEvents()

    def _format_result(self, result):
        lines = [
            DONE_FORMAT
            % (
                result.converted,
                result.empty,
                result.skipped_existing,
                len(result.failed),
            )
        ]
        if result.cancelled:
            lines.append(CANCELLED_TEXT)
        if result.failed:
            lines.append("失败文件：")
            for image, reason in result.failed[:MAX_REPORTED_ERRORS]:
                lines.append("  %s: %s" % (osp.basename(image), reason))
            if len(result.failed) > MAX_REPORTED_ERRORS:
                lines.append("  …")
        return "\n".join(lines)

    # ----------------------------------------------------- window hooks

    def closeEvent(self, event):
        if self._busy:
            event.ignore()
            self._set_status(CLOSE_REFUSED)
            return
        super().closeEvent(event)

    def _dropped_paths(self, event):
        mime = event.mimeData()
        if not mime.hasUrls():
            return []
        return [
            url.toLocalFile()
            for url in mime.urls()
            if url.isLocalFile()
        ]

    def dragEnterEvent(self, event):
        paths = self._dropped_paths(event)
        if any(
            osp.isdir(path)
            or (osp.isfile(path) and path.lower().endswith(".txt"))
            for path in paths
        ):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        paths = self._dropped_paths(event)
        dirs = [path for path in paths if osp.isdir(path)]
        files = [
            path
            for path in paths
            if osp.isfile(path) and path.lower().endswith(".txt")
        ]
        if not dirs and not files:
            event.ignore()
            self._set_status(DROP_IGNORED)
            return
        event.acceptProposedAction()
        if len(dirs) > 1 or len(files) > 1:
            self._set_status(DROP_EXTRA)
        if dirs:
            self.set_source_dir(dirs[0])
        if files:
            self.set_class_file(files[0])

    def _set_status(self, text):
        self.status_label.setText(text)

    # ------------------------------------------------------ test hooks

    def summary_text(self):
        return self.summary.toPlainText()

    def status_text(self):
        return self.status_label.text()
