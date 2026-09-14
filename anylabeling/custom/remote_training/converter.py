"""Label conversion and byte freezing (spec §5.2.6).

Wraps upstream LabelConverter.custom_to_yolo with a per-file
try/except (spec §5.2.4: an exception must never escape to the worker
thread) and freezes the produced label bytes: each txt is written once,
read once, hashed once, and never rewritten afterwards.
"""

from __future__ import annotations

import logging
import os
import os.path as osp
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from anylabeling.custom.model_validation.dataset import sha256_file

from .scanner import (
    SHAPE_TYPES_BY_MODE,
    DatasetScan,
    ScanIssue,
    task_to_mode,
)

__all__ = [
    "EMPTY_SHA256",
    "LABELS_DIRNAME",
    "SKIPPED_LABEL_HINT_LIMIT",
    "ConversionRecord",
    "ConversionResult",
    "FrozenLabel",
    "SkippedLabels",
    "convert_labels",
]

_LOGGER = logging.getLogger(__name__)

#: sha256 of the empty file: the frozen value of a 0 byte background label.
EMPTY_SHA256 = (
    "e3b0c44298fc1c149afbf4c8996fb924"
    "27ae41e4649b934ca495991b7852b855"
)
LABELS_DIRNAME = "labels"
#: How many distinct out-of-class label names the summary hints at
#: ("去重后取前 N 个", spec §5.2.6).
SKIPPED_LABEL_HINT_LIMIT = 5


def _load_label_converter():
    """Import the converter lazily (it pulls opencv / numpy)."""

    from anylabeling.views.labeling.label_converter import LabelConverter

    return LabelConverter


@dataclass
class SkippedLabels:
    """Skipped object counter of one image (spec §5.2.6)."""

    name: str
    shapes_total: int = 0
    shapes_written: int = 0
    skipped: int = 0
    labels: List[str] = field(default_factory=list)


@dataclass
class ConversionRecord:
    """Per image conversion detail for the pre-check summary.

    The four fields the spec fixes are name / shapes_total /
    shapes_written / skipped_labels (spec §5.2.6); empty_shapes and
    zero_byte_label keep the two background counters apart (spec §5.2.5
    #2 vs #8).
    """

    name: str
    stem: str
    shapes_total: int = 0
    shapes_written: int = 0
    skipped: int = 0
    skipped_labels: List[str] = field(default_factory=list)
    #: True only when the .json really had zero shapes (spec §5.2.5 #2).
    empty_shapes: bool = False
    #: True when the produced txt is 0 bytes (may also come from #8).
    zero_byte_label: bool = False
    downgraded_rectangle: bool = False


@dataclass
class FrozenLabel:
    """A label txt whose bytes were frozen once (spec §5.2.6)."""

    stem: str
    stage_path: str
    sha256: str
    size: int
    label_name: str

    def verify(self) -> bool:
        """True when the on-disk bytes still hash to the frozen value."""

        try:
            if osp.getsize(self.stage_path) != self.size:
                return False
        except OSError:
            return False
        return sha256_file(self.stage_path) == self.sha256


@dataclass
class ConversionResult:
    """Outcome of convert_labels()."""

    labels_dir: str = ""
    by_name: Dict[str, FrozenLabel] = field(default_factory=dict)
    by_stem: Dict[str, FrozenLabel] = field(default_factory=dict)
    records: List[ConversionRecord] = field(default_factory=list)
    issues: List[ScanIssue] = field(default_factory=list)
    skipped_objects: int = 0
    skipped_files: int = 0
    #: Images whose .json had zero shapes (spec §5.2.5 #2).
    background_images: int = 0
    #: Images that produced a 0 byte txt for any reason (spec §5.2.6).
    zero_byte_labels: int = 0

    @property
    def blocked(self) -> bool:
        return bool(self.issues)

    def blocking_messages(self) -> List[str]:
        return [issue.text() for issue in self.issues]

    def label_files(self) -> List[FrozenLabel]:
        return [self.by_stem[stem] for stem in sorted(self.by_stem)]

    def skipped_label_names(self) -> List[str]:
        """Distinct out-of-class label names, first-seen order, capped N."""

        names: List[str] = []
        for record in self.records:
            for label in record.skipped_labels:
                if label not in names:
                    names.append(label)
        return names[:SKIPPED_LABEL_HINT_LIMIT]

    def skipped_detail(self) -> List[SkippedLabels]:
        """Per file skip detail for the UI (spec §5.2.6)."""

        return [
            SkippedLabels(
                name=record.name,
                shapes_total=record.shapes_total,
                shapes_written=record.shapes_written,
                skipped=record.skipped,
                labels=list(record.skipped_labels),
            )
            for record in self.records
            if record.skipped
        ]

    def skip_message(self) -> Optional[str]:
        """§5.2.5 #3 wording, or None when nothing was skipped."""

        if not self.skipped_objects:
            return None
        worst: Optional[ConversionRecord] = None
        for record in self.records:
            if not record.skipped:
                continue
            if worst is None or record.skipped > worst.skipped:
                worst = record
        text = (
            "{0} 个标注对象被跳过"
            "（不在类别表 classes.txt 中或形状不合法）"
        ).format(self.skipped_objects)
        if worst is not None:
            text += "，已保留可用标注；跳过最多的文件：{0}（{1} 个）".format(
                worst.name, worst.skipped
            )
        names = self.skipped_label_names()
        if names:
            text += "；涉及标签（去重后前 {0} 个）：{1}".format(
                SKIPPED_LABEL_HINT_LIMIT, "、".join(names)
            )
        return text

    def all_skipped_message(self) -> Optional[str]:
        """§5.2.5 #8 wording, or None when it does not apply."""

        names = sorted(
            record.name
            for record in self.records
            if record.shapes_total and not record.shapes_written
        )
        if not names:
            return None
        return (
            "有 {0} 张图片的标注全部无法转换，它们将作为背景图参与训练；"
            "请确认类别表是否选对".format(len(names))
        )

    def background_message(self) -> Optional[str]:
        """§5.2.5 #2 wording, or None when there is no background image."""

        if not self.background_images:
            return None
        return (
            "{0} 张图片的标注为空，将作为背景图参与训练"
            "（生成空标签文件）".format(self.background_images)
        )

    def downgraded_message(self) -> Optional[str]:
        if not any(record.downgraded_rectangle for record in self.records):
            return None
        return "有标注使用旧版对角线矩形（2 点），已按四点矩形等价转换"


def _count_lines(path: str) -> int:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())
    except (OSError, UnicodeDecodeError):
        return 0


def convert_labels(
    scan: DatasetScan,
    staging_dir: str,
    *,
    classes_file: Optional[str],
    task: str,
    should_stop: Optional[Any] = None,
) -> ConversionResult:
    """Convert every validated pair into <staging>/labels/<stem>.txt.

    The optional should_stop callable is checked between images.  A
    cancellation raises InterruptedError instead of stopping quietly, so
    a half converted dataset can never be mistaken for a complete run
    (spec §5.2.9: the worker owns the cleanup of the staging dir).
    """

    mode = task_to_mode(task)
    result = ConversionResult(
        labels_dir=osp.join(staging_dir, LABELS_DIRNAME)
    )
    os.makedirs(result.labels_dir, exist_ok=True)
    converter_cls = _load_label_converter()
    converter = converter_cls(classes_file)
    classes_set = set(getattr(converter, "classes", None) or [])
    expected = SHAPE_TYPES_BY_MODE.get(mode, ())

    failures: List[Tuple[str, str]] = []
    for pair in scan.pairs:
        if should_stop is not None and should_stop():
            raise InterruptedError("标签转换已取消")
        record = ConversionRecord(name=pair.name, stem=pair.stem)
        data = pair.label_data or {}
        shapes = data.get("shapes")
        record.shapes_total = (
            len(shapes) if isinstance(shapes, (list, tuple)) else 0
        )
        out_path = osp.join(result.labels_dir, pair.stem + ".txt")
        try:
            # A retry must never see a stale txt: the file is removed
            # first (inside staging, never inside dataset_dir).
            if osp.exists(out_path):
                os.remove(out_path)
            converter.custom_to_yolo(
                pair.label_path, out_path, mode, skip_empty_files=False
            )
        except Exception as exc:  # noqa: BLE001 - per file safety net
            _LOGGER.warning(
                "label conversion failed for %s: %s", pair.name, exc
            )
            failures.append((pair.name, str(exc)))
            continue

        if not osp.exists(out_path):
            # custom_to_yolo only touches the output when the input
            # exists; a missing output is treated as the zero byte
            # background label that "shapes == []" produces (§5.2.6).
            with open(out_path, "wb"):
                pass
        written = _count_lines(out_path)
        record.shapes_written = written
        record.skipped = max(0, record.shapes_total - written)
        # Two different background counters (spec §5.2.5 #2 vs #8):
        # empty_shapes is the "shapes == []" case the #2 line reports,
        # zero_byte_label is the produced 0 byte txt.
        record.empty_shapes = record.shapes_total == 0
        record.zero_byte_label = written == 0

        # Out-of-class label names of this file, deduplicated and capped
        # (spec §5.2.6: "提示该标签名（去重后取前 N 个）").
        skipped_names: List[str] = []
        for shape in shapes or []:
            if not isinstance(shape, Mapping):
                continue
            if shape.get("shape_type") not in expected:
                continue
            label = shape.get("label")
            if not isinstance(label, str) or label in classes_set:
                continue
            if label not in skipped_names:
                skipped_names.append(label)
        record.skipped_labels = skipped_names[:SKIPPED_LABEL_HINT_LIMIT]

        # The deprecated diagonal rectangle message is only honest when
        # the mode really converts rectangles and this one really became
        # a label line (label in classes.txt, exactly 2 points).
        record.downgraded_rectangle = mode == "hbb" and any(
            shape.get("shape_type") == "rectangle"
            and isinstance(shape.get("points"), (list, tuple))
            and len(shape["points"]) == 2
            and isinstance(shape.get("label"), str)
            and shape["label"] in classes_set
            for shape in (shapes or [])
            if isinstance(shape, Mapping)
        )
        if record.skipped:
            result.skipped_objects += record.skipped
            result.skipped_files += 1
        if record.empty_shapes:
            result.background_images += 1
        if record.zero_byte_label:
            result.zero_byte_labels += 1
        size = osp.getsize(out_path)
        digest = sha256_file(out_path)
        frozen = FrozenLabel(
            stem=pair.stem,
            stage_path=out_path,
            sha256=digest,
            size=size,
            label_name=pair.stem + ".txt",
        )
        result.by_name[pair.name] = frozen
        result.by_stem[pair.stem] = frozen
        result.records.append(record)

    if failures:
        result.issues.append(
            ScanIssue(
                code="CONVERSION_FAILED",
                summary=(
                    "发现 {0} 张图片的标注转换失败，已停止上传"
                    "，请修复后重试".format(len(failures))
                ),
                details=[
                    "- {0}（{1}）".format(name, reason)
                    for name, reason in failures
                ],
            )
        )
    return result
