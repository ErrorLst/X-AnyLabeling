"""Read-only dataset scan, pairing and full schema validation.

Implements spec §5.2.3 (root-only scan), spec §5.2.4 (complete schema
validation and the points cardinality table) and spec §5.2.5 (the N1
matrix with its user visible wording).

The scan never writes inside dataset_dir: everything here is opendir /
stat / open(..., "r").
"""

from __future__ import annotations

import json
import os
import os.path as osp
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from anylabeling.custom.model_validation.labelme_io import IMAGE_EXTENSIONS

__all__ = [
    "CLASSES_DUPLICATE",
    "JSON_SUFFIX",
    "SHAPE_TYPES_BY_MODE",
    "TASK_MODES",
    "TASK_PROTOCOL",
    "SUBDIRECTORY_IMAGE_LIMIT",
    "DatasetScan",
    "LabelSchema",
    "ScanIssue",
    "ScanPair",
    "protocol_task",
    "read_classes_file",
    "scan_dataset",
    "task_to_mode",
    "validate_label_schema",
]

JSON_SUFFIX = ".json"

#: Task spellings accepted on input -> converter mode (spec §5.2.2).
#: Both the UI spellings (Detect / Segment) and the protocol
#: spellings (detect / segment, spec §4.1.2) are accepted.
TASK_MODES: Dict[str, str] = {
    "Detect": "hbb",
    "Segment": "seg",
    "detect": "hbb",
    "segment": "seg",
}
#: Task spellings accepted on input -> protocol value (spec §4.1.2).
TASK_PROTOCOL: Dict[str, str] = {
    "Detect": "detect",
    "Segment": "segment",
    "detect": "detect",
    "segment": "segment",
}
#: Shape types each mode converts (spec §5.2.4).
SHAPE_TYPES_BY_MODE: Dict[str, Tuple[str, ...]] = {
    "hbb": ("rectangle",),
    "seg": ("polygon",),
}
#: How many nested image paths one subdirectory issue lists (spec §5.2.3).
SUBDIRECTORY_IMAGE_LIMIT = 20

CLASSES_UNREADABLE = "CLASSES_UNREADABLE"
CLASSES_DUPLICATE = "CLASSES_DUPLICATE"
CLASSES_EMPTY = "CLASSES_EMPTY"
TASK_UNSUPPORTED = "TASK_UNSUPPORTED"

_IMAGE_SUFFIXES = tuple(IMAGE_EXTENSIONS)


@dataclass
class ScanIssue:
    """One finding of the scan / N1 matrix."""

    code: str
    summary: str
    details: List[str] = field(default_factory=list)
    blocking: bool = True
    #: Trailing line printed after the file list (spec §5.2.5 #4 writes
    #: "请修复后重试" after the list, not before it).
    footer: str = ""

    def text(self) -> str:
        lines = [self.summary]
        lines.extend(self.details)
        if self.footer:
            lines.append(self.footer)
        return "\n".join(lines)


@dataclass
class LabelSchema:
    """Result of validating one .json label file (spec §5.2.4)."""

    ok: bool = False
    blocking: bool = False
    skipped: int = 0
    skipped_labels: List[str] = field(default_factory=list)
    shapes_total: int = 0
    detected: int = 0
    reason: str = ""
    downgraded_rectangles: int = 0


@dataclass
class ScanPair:
    """One image / label pair; label_data is the parsed JSON object."""

    name: str
    stem: str
    image_path: str
    label_path: str
    label_data: Optional[Dict[str, Any]] = None


@dataclass
class DatasetScan:
    """Everything the pipeline needs from dataset_dir."""

    dataset_dir: str = ""
    classes: List[str] = field(default_factory=list)
    pairs: List[ScanPair] = field(default_factory=list)
    orphan_labels: List[str] = field(default_factory=list)
    issues: List[ScanIssue] = field(default_factory=list)
    skipped_objects: int = 0
    background_images: int = 0
    downgraded_rectangles: int = 0

    @property
    def blocked(self) -> bool:
        return any(issue.blocking for issue in self.issues)

    def blocking(self) -> List[ScanIssue]:
        return [issue for issue in self.issues if issue.blocking]

    def warnings(self) -> List[ScanIssue]:
        return [issue for issue in self.issues if not issue.blocking]

    def blocking_messages(self) -> List[str]:
        return [issue.text() for issue in self.blocking()]

    def warning_messages(self) -> List[str]:
        return [issue.text() for issue in self.warnings()]

    def image_names(self) -> List[str]:
        return [pair.name for pair in self.pairs]

    def label_data_by_name(self) -> Dict[str, Dict[str, Any]]:
        return {
            pair.name: pair.label_data
            for pair in self.pairs
            if pair.label_data is not None
        }


def task_to_mode(task: str) -> str:
    """Detect/detect -> hbb, Segment/segment -> seg (spec §5.2.2)."""

    try:
        return TASK_MODES[str(task)]
    except KeyError as exc:
        raise ValueError(
            "unsupported task {0!r}; expected one of {1}".format(
                task, ", ".join(sorted(TASK_PROTOCOL))
            )
        ) from exc


def protocol_task(task: str) -> str:
    """The protocol spelling of a task (spec §4.1.2: detect / segment).

    This is the single normalisation point: the manifest
    (spec §4.1.2) and the exported configuration (spec §5.2.8) both
    carry the protocol value, while the converter still works on the
    UI spelling.
    """

    try:
        return TASK_PROTOCOL[str(task)]
    except KeyError as exc:
        raise ValueError(
            "unsupported task {0!r}; expected one of {1}".format(
                task, ", ".join(sorted(TASK_PROTOCOL))
            )
        ) from exc


def _is_positive_int(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value > 0
    if isinstance(value, float):
        return value > 0 and float(value).is_integer()
    return False


def _point_list(value: Any) -> Optional[List[Tuple[float, float]]]:
    if not isinstance(value, (list, tuple)):
        return None
    points: List[Tuple[float, float]] = []
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            return None
        x, y = item
        for number in (x, y):
            if isinstance(number, bool) or not isinstance(
                number, (int, float)
            ):
                return None
        points.append((float(x), float(y)))
    return points


def validate_label_schema(
    data: Any, mode: str, classes: Sequence[str]
) -> LabelSchema:
    """Full schema validation of one parsed .json (spec §5.2.4).

    Three things block: a structurally illegal shape entry, a Detect
    rectangle whose point count is not in {2, 4}, and - by this step's
    acceptance criterion (CT25) - a two point polygon.  For entries
    whose shape type belongs to the mode, the cardinality rule is
    evaluated before the label membership test (spec §5.2.4 "阻断规则
    的单一口径"); everything else that cannot be converted is a skip
    plus a counter.
    """

    if not isinstance(data, dict):
        return LabelSchema(
            ok=False, blocking=True, reason="顶层不是 JSON 对象"
        )
    if not _is_positive_int(data.get("imageWidth")):
        return LabelSchema(
            ok=False, blocking=True, reason="imageWidth 非正整数"
        )
    if not _is_positive_int(data.get("imageHeight")):
        return LabelSchema(
            ok=False, blocking=True, reason="imageHeight 非正整数"
        )
    if "shapes" not in data:
        return LabelSchema(ok=False, blocking=True, reason="shapes 缺失")
    shapes = data["shapes"]
    if not isinstance(shapes, list):
        return LabelSchema(
            ok=False, blocking=True, reason="shapes 不是列表"
        )

    expected = SHAPE_TYPES_BY_MODE.get(mode, ())
    schema = LabelSchema(ok=True, shapes_total=len(shapes))
    seen: List[str] = []
    for entry in shapes:
        if not isinstance(entry, dict):
            schema.ok = False
            schema.blocking = True
            schema.reason = "标注条目不是对象"
            return schema
        if "shape_type" not in entry:
            schema.ok = False
            schema.blocking = True
            schema.reason = "标注条目缺少 shape_type"
            return schema
        shape_type = entry.get("shape_type")
        label = entry.get("label")
        points = _point_list(entry.get("points"))
        if points is None:
            schema.ok = False
            schema.blocking = True
            schema.reason = "points 不是两个数值的点序列"
            return schema
        if shape_type not in expected:
            # Shape type outside this mode: skip + count, never blocking.
            schema.skipped += 1
            continue
        # The one and only blocking rule (spec §5.2.4) is evaluated
        # BEFORE the label membership test, so a Detect rectangle with
        # 0, 1, 3 or more than 4 points blocks even when its label is
        # not in classes.txt.
        if mode == "hbb":
            if len(points) not in (2, 4):
                schema.ok = False
                schema.blocking = True
                schema.reason = (
                    "rectangle 的点数不在 {{2, 4}} 内（实际 {0}）".format(
                        len(points)
                    )
                )
                return schema
        elif len(points) < 3:
            # B7 / CT25: a two point polygon blocks as an illegal
            # structure instead of being skipped (PENDING ITEM: spec
            # §5.2.4 says "polygon 点数 < 3 ⇒ 跳过该对象 + 计数", the
            # plan's CT25 asks for "非法 + 可读原因 + 不上传"; the plan
            # wins here because it is this step's acceptance criterion,
            # and the tension is registered for the spec owner instead
            # of being patched in the document).  A 0 or 1 point
            # polygon is still skipped: CT25 names only the 2 point
            # case.
            if len(points) == 2:
                schema.ok = False
                schema.blocking = True
                schema.reason = (
                    "polygon 的点数不足（实际 2，至少需要 3）"
                )
                return schema
            schema.skipped += 1
            continue
        if not isinstance(label, str) or label not in classes:
            schema.skipped += 1
            if isinstance(label, str) and label not in seen:
                seen.append(label)
            continue
        if mode == "hbb" and len(points) == 2:
            schema.downgraded_rectangles += 1
        schema.detected += 1
    schema.skipped_labels = seen
    return schema


def read_classes_file(
    classes_file: Optional[str],
) -> Tuple[List[str], List[ScanIssue]]:
    """Read classes.txt; empty / unreadable is a blocking issue."""

    if not classes_file:
        return [], [
            ScanIssue(
                code=CLASSES_UNREADABLE,
                summary="类别表 classes.txt 为空或不可读，请重新选择",
            )
        ]
    path = str(classes_file)
    if not osp.isfile(path):
        return [], [
            ScanIssue(
                code=CLASSES_UNREADABLE,
                summary="类别表 classes.txt 为空或不可读，请重新选择",
                details=["- {0}（文件不存在）".format(path)],
            )
        ]
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read()
    except (OSError, UnicodeDecodeError) as exc:
        return [], [
            ScanIssue(
                code=CLASSES_UNREADABLE,
                summary="类别表 classes.txt 为空或不可读，请重新选择",
                details=["- {0}（{1}）".format(path, exc)],
            )
        ]
    classes = [line.strip() for line in raw.splitlines()]
    classes = [name for name in classes if name]
    if not classes:
        return [], [
            ScanIssue(
                code=CLASSES_EMPTY,
                summary="类别表 classes.txt 为空或不可读，请重新选择",
                details=["- {0}（没有非空类别行）".format(path)],
            )
        ]
    duplicates = sorted({name for name in classes if classes.count(name) > 1})
    if duplicates:
        return [], [
            ScanIssue(
                code=CLASSES_DUPLICATE,
                summary="类别表 classes.txt 含重复类别，请去掉重复项后重试",
                details=["- {0}".format(name) for name in duplicates],
            )
        ]
    return classes, []


def _root_entries(dataset_dir: str):
    try:
        with os.scandir(dataset_dir) as iterator:
            return list(iterator)
    except OSError:
        return []


def _nested_images(
    dataset_dir: str, subdirs: Sequence[str]
) -> Tuple[List[str], int]:
    """Image paths inside the root subdirectories (spec §5.2.3).

    The message lists the images that cannot be expressed as flat
    manifest names, so the user knows what to move.  Returns the
    bounded list plus the total count found.
    """

    found: List[str] = []
    total = 0
    for subdir in subdirs:
        root = osp.join(dataset_dir, subdir)
        for current, _dirs, names in os.walk(root):
            for name in sorted(names):
                if not name.endswith(_IMAGE_SUFFIXES):
                    continue
                total += 1
                if len(found) < SUBDIRECTORY_IMAGE_LIMIT:
                    relative = osp.relpath(
                        osp.join(current, name), dataset_dir
                    )
                    found.append(relative.replace(os.sep, "/"))
    return found, total


def scan_dataset(
    dataset_dir: str,
    *,
    classes_file: Optional[str],
    task: str,
) -> DatasetScan:
    """Scan the dataset root once and run the whole N1 matrix.

    Only dataset_dir itself is enumerated (no recursion); a subdirectory
    entry blocks the run (spec §5.2.3).
    """

    scan = DatasetScan(dataset_dir=osp.abspath(dataset_dir))
    try:
        mode = task_to_mode(task)
    except ValueError as exc:
        # An unsupported task must surface as a blocking pre-check
        # issue, never as a bare exception escaping to the worker.
        scan.issues.append(
            ScanIssue(code=TASK_UNSUPPORTED, summary=str(exc))
        )
        return scan

    if not osp.isdir(dataset_dir):
        scan.issues.append(
            ScanIssue(
                code="EMPTY_DATASET",
                summary="所选目录中没有找到图片（支持：{0}）".format(
                    "/".join(_IMAGE_SUFFIXES)
                ),
                details=["- {0}（目录不存在）".format(dataset_dir)],
            )
        )
        return scan

    classes, class_issues = read_classes_file(classes_file)
    scan.issues.extend(class_issues)
    scan.classes = classes

    image_names: List[str] = []
    label_names: List[str] = []
    subdirs: List[str] = []
    unsafe: List[Tuple[str, str]] = []
    for entry in _root_entries(dataset_dir):
        name = entry.name
        if name in (".", ".."):
            continue
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
        except OSError:
            is_dir = False
        if is_dir:
            subdirs.append(name)
            continue
        try:
            is_file = entry.is_file(follow_symlinks=False)
        except OSError:
            is_file = False
        if not is_file:
            unsafe.append((name, "不是常规文件"))
            continue
        if name.endswith(_IMAGE_SUFFIXES):
            image_names.append(name)
        elif name.endswith(JSON_SUFFIX):
            label_names.append(name)

    image_names.sort()
    label_names.sort()
    subdirs.sort()
    unsafe.sort()

    if subdirs:
        nested, nested_total = _nested_images(dataset_dir, subdirs)
        details = ["- {0}".format(path) for path in nested]
        footer = ""
        if nested_total > len(nested):
            footer = "（共 {0} 个，仅列出前 {1} 个）".format(
                nested_total, len(nested)
            )
        scan.issues.append(
            ScanIssue(
                code="SUBDIRECTORY",
                summary=(
                    "数据集包含子目录，v1 只支持根目录下的图片，"
                    "请把它们移到根目录或改选子目录："
                ),
                details=details,
                footer=footer,
            )
        )
    if unsafe:
        scan.issues.append(
            ScanIssue(
                code="UNSAFE_ENTRY",
                summary="数据集根目录存在无法作为扁平文件名表达的项目",
                details=[
                    "- {0}（{1}）".format(name, reason)
                    for name, reason in unsafe
                ],
            )
        )
    if not image_names:
        scan.issues.append(
            ScanIssue(
                code="EMPTY_DATASET",
                summary="所选目录中没有找到图片（支持：{0}）".format(
                    "/".join(_IMAGE_SUFFIXES)
                ),
            )
        )

    by_stem: Dict[str, List[str]] = {}
    for name in image_names:
        stem = osp.splitext(name)[0]
        by_stem.setdefault(stem, []).append(name)
    collisions = {
        stem: names for stem, names in by_stem.items() if len(names) > 1
    }
    if collisions:
        details: List[str] = []
        for stem in sorted(collisions):
            details.append(
                "- {0}".format(" / ".join(sorted(collisions[stem])))
            )
        scan.issues.append(
            ScanIssue(
                code="STEM_COLLISION",
                summary=(
                    "发现 {0} 组同名不同扩展名的图片"
                    "（标签文件同名会互相覆盖），"
                    "请重命名或移出其中一张后重试".format(len(collisions))
                ),
                details=details,
            )
        )

    missing: List[str] = []
    for name in image_names:
        stem = osp.splitext(name)[0]
        if stem in collisions:
            continue
        if stem + JSON_SUFFIX not in label_names:
            missing.append(name)
    if missing:
        scan.issues.append(
            ScanIssue(
                code="IMAGE_WITHOUT_LABEL",
                summary=(
                    "发现 {0} 张图片缺少同名标注文件（.json）"
                    "，已停止上传：".format(len(missing))
                ),
                details=["- {0}".format(name) for name in missing],
            )
        )

    # Every label whose stem carries an image is consumed, including
    # the colliding stems: they are blocked by STEM_COLLISION, not
    # reported again as orphan labels (spec §5.2.5 #5).
    consumed: List[str] = [
        name for name in label_names if osp.splitext(name)[0] in by_stem
    ]
    corrupt: List[Tuple[str, str]] = []
    for name in image_names:
        stem = osp.splitext(name)[0]
        if stem in collisions:
            continue
        label_name = stem + JSON_SUFFIX
        if label_name not in label_names:
            continue
        label_path = osp.join(dataset_dir, label_name)
        try:
            with open(label_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, UnicodeDecodeError) as exc:
            corrupt.append((label_name, "不可读：{0}".format(exc)))
            continue
        except ValueError as exc:
            corrupt.append((label_name, "JSON 解析失败：{0}".format(exc)))
            continue
        schema = validate_label_schema(data, mode, classes)
        if not schema.ok:
            corrupt.append((label_name, schema.reason or "结构非法"))
            continue
        if schema.skipped:
            scan.skipped_objects += schema.skipped
        if not schema.detected:
            scan.background_images += 1
        scan.downgraded_rectangles += schema.downgraded_rectangles
        scan.pairs.append(
            ScanPair(
                name=name,
                stem=stem,
                image_path=osp.join(dataset_dir, name),
                label_path=label_path,
                label_data=data,
            )
        )
    if corrupt:
        scan.issues.append(
            ScanIssue(
                code="CORRUPT_LABEL",
                summary=(
                    "发现 {0} 个标注文件损坏，无法解析为 "
                    "X-AnyLabeling 标注：".format(len(corrupt))
                ),
                details=[
                    "- {0}（{1}）".format(name, reason)
                    for name, reason in corrupt
                ],
                footer="请修复后重试",
            )
        )

    consumed_set = set(consumed)
    scan.orphan_labels = [
        name for name in label_names if name not in consumed_set
    ]
    if scan.orphan_labels:
        scan.issues.append(
            ScanIssue(
                code="ORPHAN_LABELS",
                summary=(
                    "忽略 {0} 个没有对应图片的标注文件"
                    "（服务端同样忽略并计入 warnings）".format(
                        len(scan.orphan_labels)
                    )
                ),
                details=[
                    "- {0}".format(name) for name in scan.orphan_labels
                ],
                blocking=False,
            )
        )

    unreadable: List[Tuple[str, str]] = []
    for pair in scan.pairs:
        try:
            with open(pair.image_path, "rb") as handle:
                handle.read(1)
        except OSError as exc:
            unreadable.append((pair.name, str(exc)))
    if unreadable:
        scan.issues.append(
            ScanIssue(
                code="UNREADABLE_IMAGE",
                summary="打包过程中有 {0} 个文件不可读，已停止".format(
                    len(unreadable)
                ),
                details=[
                    "- {0}（{1}）".format(name, reason)
                    for name, reason in unreadable
                ],
            )
        )

    return scan
