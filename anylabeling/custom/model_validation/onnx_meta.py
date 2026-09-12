"""ONNX model metadata reading and validation.

Ultralytics exports store task/imgsz/names inside the ONNX metadata
properties. The model validation tool requires them and refuses to
continue when they are missing or when the number of embedded names
does not match the number of lines of classes.txt.

The embedded names are treated as a hint only: models exported by a
training script that never wrote the class names carry placeholders
such as class_0/class_1. The name comparison therefore never blocks a
run - classes.txt stays authoritative and a per index name difference
is reported as a non blocking warning instead.
"""

from __future__ import annotations

import os.path as osp
from typing import Any, Dict, List, Optional, Sequence

from .app_config import (
    ValidationConfigError,
    is_int_dimension,
    parse_imgsz_literal,
    parse_names_literal,
)

TASK_FAMILY_MAP: Dict[str, str] = {
    "detect": "yolov8",
    "segment": "yolov8_seg",
    "obb": "yolov8_obb",
    "pose": "yolov8_pose",
}

MISSING_META_MESSAGE = (
    "该 ONNX 缺少 Ultralytics 元数据（task/imgsz/names），"
    "请用 yolo export format=onnx 或本仓库 tools/onnx_exporter/ "
    "重新导出后重试"
)

CLASSIFY_MESSAGE = "task=classify 不受支持，本工具只做检测类框验证"

# Only a different number of classes blocks a run: the class table of
# the user is the authority while the embedded names may be placeholders
# written by the training script.
CLASS_COUNT_MESSAGE = (
    "类别数量不一致：classes.txt 有 {n_txt} 类，模型 names 有 {n_model} 类"
)

CLASS_COUNT_HINT = (
    "类别数量不一致：classes.txt 有 {n_txt} 类，模型 names 有 {n_model} 类；"
    "请确认使用的类别表与该 ONNX 匹配"
)

NAME_DIFF_MESSAGE = (
    "已以 classes.txt 的类名为准（模型内嵌 names 为占位名，差异 {count} 处）"
)

# The diff text shown by the UI and stored in the report stays readable.
NAME_PREVIEW_LIMIT = 5
ELLIPSIS = "…"


class MetaValidationError(ValidationConfigError):
    """Raised when the ONNX metadata does not satisfy the contract."""


def read_onnx_metadata(model_path: str) -> Dict[str, str]:
    """Return the ONNX metadata_props mapping of a model file."""

    import onnx

    if not model_path or not osp.isfile(model_path):
        raise MetaValidationError(f"ONNX model not found: {model_path}")
    model = onnx.load(model_path)
    return {prop.key: prop.value for prop in model.metadata_props}


def resolve_family(task: str) -> str:
    """Map an Ultralytics task to the internal model family."""

    family = TASK_FAMILY_MAP.get(task)
    if family is None:
        raise MetaValidationError(
            f"不支持的任务类型 task={task!r}，本工具只做检测类框验证"
        )
    return family


def compare_names(
    model_names: Sequence[str], classes: Sequence[str]
) -> List[str]:
    """Return the per index diff between the model names and classes.txt."""

    diff: List[str] = []
    total = max(len(model_names), len(classes))
    for index in range(total):
        model_name = model_names[index] if index < len(model_names) else None
        txt_name = classes[index] if index < len(classes) else None
        if model_name != txt_name:
            diff.append(f"{index}: model={model_name!r} vs txt={txt_name!r}")
    return diff


def class_count_mismatch_message(n_txt: int, n_model: int) -> str:
    """Return the blocking message naming both amounts."""

    return CLASS_COUNT_HINT.format(n_txt=int(n_txt), n_model=int(n_model))


def name_diff_warning(count: int) -> str:
    """Return the warning shown when classes.txt overrides the names."""

    return NAME_DIFF_MESSAGE.format(count=int(count))


def truncate_diff(
    diff: Sequence[str], limit: int = NAME_PREVIEW_LIMIT
) -> List[str]:
    """Return the first diff entries plus an ellipsis marker."""

    items = [str(item) for item in diff]
    if limit <= 0 or len(items) <= limit:
        return items
    return items[:limit] + [ELLIPSIS]


def validate_meta(
    metadata: Dict[str, str],
    input_shape: Optional[Sequence[Any]] = None,
    classes: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Validate the metadata contract and return the parsed information.

    The result exposes the embedded names (the hint of the model) and
    the effective classes (the class table of the user) side by side.
    classes_count_match is the only blocking class signal: a name
    difference is collected in classes_name_diff, mirrored as a warning
    and never stops the run.
    """

    raw_task = metadata.get("task")
    raw_imgsz = metadata.get("imgsz")
    raw_names = metadata.get("names")
    if not raw_task or not raw_imgsz or not raw_names:
        raise MetaValidationError(MISSING_META_MESSAGE)

    task = str(raw_task).strip().lower()
    if task == "classify":
        raise MetaValidationError(CLASSIFY_MESSAGE)
    family = resolve_family(task)

    names = parse_names_literal(str(raw_names))
    imgsz_meta = parse_imgsz_literal(str(raw_imgsz))

    stride = int(str(metadata.get("stride", "32") or 32))
    batch = int(str(metadata.get("batch", "1") or 1))
    dynamic = str(metadata.get("dynamic", "False")).strip().lower()
    is_dynamic = dynamic in ("true", "1", "yes")

    imgsz_session: Optional[List[int]] = None
    if input_shape is not None and len(input_shape) >= 4:
        height, width = input_shape[2], input_shape[3]
        if not is_int_dimension(height) or not is_int_dimension(width):
            raise MetaValidationError(
                "ONNX 输入空间维不是固定值（动态导出），无法用于验证："
                f"input shape={list(input_shape)}"
            )
        imgsz_session = [int(height), int(width)]

    classes_count_match = True
    classes_name_diff: List[str] = []
    effective_classes: Optional[List[str]] = None
    # classes_count_match blocks a run, classes_name_diff only informs:
    # the diff is built whenever the name lists differ, whatever the
    # amounts are, so that the report can show both sides.
    if classes is not None:
        effective_classes = [str(name) for name in classes]
        classes_count_match = len(effective_classes) == len(names)
        if list(effective_classes) != list(names):
            classes_name_diff = compare_names(names, effective_classes)

    # The class comparison produces two results and no warning: the
    # caller decides what blocks the run (a different amount of classes)
    # and what is only reported (a different name).
    warnings: List[str] = []
    if imgsz_session is not None and imgsz_meta != imgsz_session:
        warnings.append(
            "ONNX 元数据 imgsz="
            f"{imgsz_meta} 与推理会话输入尺寸 "
            f"imgsz_session={imgsz_session} 不一致，已以会话尺寸为准"
        )
    if is_dynamic:
        warnings.append("ONNX 元数据 dynamic=True，按会话输入尺寸处理")

    return {
        "task": task,
        "family": family,
        "imgsz_meta": imgsz_meta,
        "imgsz_session": imgsz_session,
        "stride": stride,
        "batch": batch,
        "dynamic": is_dynamic,
        "names": names,
        "classes": effective_classes,
        "classes_count_match": classes_count_match,
        "classes_name_diff": classes_name_diff,
        "warnings": warnings,
    }


__all__ = [
    "CLASSIFY_MESSAGE",
    "CLASS_COUNT_HINT",
    "CLASS_COUNT_MESSAGE",
    "ELLIPSIS",
    "MISSING_META_MESSAGE",
    "MetaValidationError",
    "NAME_DIFF_MESSAGE",
    "NAME_PREVIEW_LIMIT",
    "TASK_FAMILY_MAP",
    "class_count_mismatch_message",
    "compare_names",
    "name_diff_warning",
    "read_onnx_metadata",
    "resolve_family",
    "truncate_diff",
    "validate_meta",
]
