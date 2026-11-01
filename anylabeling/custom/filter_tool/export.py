"""Export the display filter chain over the images of one file list.

The module is Qt free: it plans the source/target pairs, applies the
same pure filter chain to every image and writes new files. Source files
are only read; plan_export rejects a target that is one of the sources,
so an export can never overwrite an original image. Files are written
as 8-bit RGB or RGBA, matching the display pipeline. Each image
can carry its sidecar label json; the json is copied untouched.
"""

from __future__ import annotations

import os
import os.path as osp
import shutil
from dataclasses import dataclass, field
from typing import List, Tuple

import numpy as np
import PIL.Image
import PIL.ImageOps

from . import core

__all__ = [
    "EXPORT_DIR_SUFFIX",
    "ExportError",
    "ExportResult",
    "ExportTarget",
    "collect_image_paths",
    "common_root",
    "existing_targets",
    "export_directory",
    "export_images",
    "filter_image_file",
    "label_file_for",
    "plan_export",
]

EXPORT_DIR_SUFFIX = "_filtered"
JPEG_QUALITY = 95
ALPHA_FORMATS = {"PNG", "WEBP", "TIFF", "TGA", "ICO", "HEIF"}


class ExportError(Exception):
    """Raised when an export plan cannot be built."""


@dataclass(frozen=True)
class ExportTarget:
    """One source image, its output and its optional sidecar json."""

    source: str
    target: str
    relative: str
    label_source: str = ""
    label_target: str = ""


@dataclass
class ExportResult:
    """Outcome of one export run."""

    written: List[str] = field(default_factory=list)
    labels_written: List[str] = field(default_factory=list)
    label_missing: int = 0
    failed: List[Tuple[str, str]] = field(default_factory=list)
    cancelled: bool = False


def collect_image_paths(widget):
    """Return the unique image paths of a labeling widget.

    The current file list is used when it has entries; a single loaded
    image is the fallback. Paths are made absolute and de-duplicated.
    """

    paths = []
    image_list = getattr(widget, "image_list", None)
    if image_list:
        paths = [str(path) for path in image_list if path]
    if not paths:
        filename = getattr(widget, "filename", None)
        if filename:
            paths = [str(filename)]
    unique = []
    seen = set()
    for path in paths:
        absolute = osp.abspath(path)
        key = osp.normcase(absolute)
        if key in seen:
            continue
        seen.add(key)
        unique.append(absolute)
    return unique


def common_root(paths):
    """Return the common directory of every source path."""

    if not paths:
        raise ExportError("没有可导出的图像")
    directories = [osp.dirname(osp.abspath(path)) for path in paths]
    try:
        return osp.commonpath(directories)
    except ValueError as exc:
        raise ExportError("图像不在同一个磁盘或根目录下") from exc


def export_directory(base_dir, source_root):
    """Return <selected dir>/<source dir name>_filtered."""

    if not str(base_dir or "").strip():
        raise ExportError("导出目录为空")
    name = osp.basename(osp.normpath(osp.abspath(source_root)))
    if not name:
        name = "images"
    return osp.abspath(osp.join(base_dir, name + EXPORT_DIR_SUFFIX))


def label_file_for(image_path, output_dir=""):
    """Return the sidecar json of one image, or an empty string.

    The app can store labels next to the image or, when an output_dir
    is configured, in that directory under the image's basename.
    """

    image_path = osp.abspath(image_path)
    stem = osp.splitext(image_path)[0]
    candidates = []
    if output_dir:
        candidates.append(
            osp.join(
                osp.abspath(output_dir),
                osp.basename(stem) + ".json",
            )
        )
    candidates.append(stem + ".json")
    for candidate in candidates:
        if osp.isfile(candidate):
            return candidate
    return ""


def plan_export(paths, output_dir, label_paths=None):
    """Build the source/target pairs of an export.

    The relative directory structure below the common source root is
    preserved. A target equal to one of the sources is rejected, so the
    source data can never be overwritten by this plan. label_paths maps
    source image paths to their sidecar json paths; when it is omitted
    the sidecar next to each image is used.
    """

    paths = [osp.abspath(path) for path in paths]
    if not paths:
        raise ExportError("没有可导出的图像")
    if not str(output_dir or "").strip():
        raise ExportError("导出目录为空")
    output_dir = osp.abspath(output_dir)
    if osp.exists(output_dir) and not osp.isdir(output_dir):
        raise ExportError("导出路径不是目录: %s" % output_dir)
    root = common_root(paths)
    labels = {}
    for key, value in dict(label_paths or {}).items():
        if value:
            labels[osp.normcase(osp.abspath(key))] = osp.abspath(value)
    sources = {osp.normcase(path): path for path in paths}
    targets = []
    seen = set()
    for source in paths:
        relative = osp.relpath(source, root)
        target = osp.abspath(osp.join(output_dir, relative))
        try:
            inside = osp.commonpath([output_dir, target])
        except ValueError as exc:
            raise ExportError("导出路径越界: %s" % target) from exc
        if osp.normcase(inside) != osp.normcase(output_dir):
            raise ExportError("导出路径越界: %s" % target)
        key = osp.normcase(target)
        if key in sources:
            raise ExportError("导出会覆盖原图: %s" % source)
        if key in seen:
            raise ExportError("导出目标重名: %s" % target)
        seen.add(key)
        label_source = labels.get(osp.normcase(source), "")
        if label_source and not osp.isfile(label_source):
            label_source = ""
        if not label_source:
            label_source = label_file_for(source)
        label_target = ""
        if label_source:
            label_target = osp.splitext(target)[0] + ".json"
        targets.append(
            ExportTarget(
                source,
                target,
                relative,
                label_source,
                label_target,
            )
        )
    return targets


def existing_targets(targets):
    """Return the output paths that already exist (image or json)."""

    existing = []
    for item in targets:
        if osp.exists(item.target):
            existing.append(item.target)
        if item.label_target and osp.exists(item.label_target):
            existing.append(item.label_target)
    return existing


def _image_format(source, image_format):
    if image_format == "MPO":
        return "JPEG"
    if image_format:
        return image_format
    extension = osp.splitext(source)[1].lower()
    known = PIL.Image.registered_extensions().get(extension)
    if known is None:
        raise ExportError("不支持的图像格式: %s" % extension)
    return known


def _rgb_and_alpha(image):
    """Return the 8-bit RGB array and an optional alpha array."""

    image = PIL.ImageOps.exif_transpose(image)
    has_alpha = "A" in image.getbands() or "transparency" in image.info
    if has_alpha:
        converted = image.convert("RGBA")
        data = np.array(converted, dtype=np.uint8)
        return (
            np.ascontiguousarray(data[:, :, :3]),
            np.ascontiguousarray(data[:, :, 3]),
        )
    converted = image.convert("RGB")
    data = np.array(converted, dtype=np.uint8)
    return np.ascontiguousarray(data), None


def _flatten_alpha(rgb, alpha):
    """Composite an RGBA image on white for formats without alpha."""

    alpha_float = alpha.astype(np.float32)[:, :, None] / 255.0
    mixed = rgb.astype(np.float32) * alpha_float
    mixed += 255.0 * (1.0 - alpha_float)
    return np.clip(mixed, 0.0, 255.0).astype(np.uint8)


def filter_image_file(source, target, specs):
    """Read one image, apply the chain and write the filtered copy."""

    try:
        with PIL.Image.open(source) as image:
            image_format = _image_format(source, image.format)
            rgb, alpha = _rgb_and_alpha(image)
    except ExportError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ExportError("无法读取图像 %s: %s" % (source, exc)) from exc

    try:
        filtered = core.apply_chain(rgb, specs)
    except Exception as exc:  # noqa: BLE001
        raise ExportError("滤镜失败 %s: %s" % (source, exc)) from exc

    if alpha is not None and image_format in ALPHA_FORMATS:
        output = PIL.Image.fromarray(
            np.dstack((filtered, alpha)), "RGBA"
        )
    else:
        if alpha is not None:
            filtered = _flatten_alpha(filtered, alpha)
        output = PIL.Image.fromarray(filtered, "RGB")

    options = {}
    if image_format == "JPEG":
        options["quality"] = JPEG_QUALITY
    try:
        os.makedirs(osp.dirname(target), exist_ok=True)
        output.save(target, format=image_format, **options)
    except Exception as exc:  # noqa: BLE001
        raise ExportError("无法写入 %s: %s" % (target, exc)) from exc


def export_images(
    targets,
    specs,
    progress=None,
    cancelled=None,
    overwrite=False,
):
    """Apply the chain to every target and return the run result.

    A per-image failure is recorded and the next image is tried. The
    cancellation callback is checked before each image, so a running
    export stops at an image boundary.
    """

    result = ExportResult()
    result.label_missing = sum(
        1 for item in targets if not item.label_source
    )
    total = len(targets)
    for index, item in enumerate(targets, start=1):
        if cancelled is not None and cancelled():
            result.cancelled = True
            break
        try:
            if osp.exists(item.target) and not overwrite:
                raise ExportError("目标文件已存在: %s" % item.target)
            filter_image_file(item.source, item.target, specs)
            result.written.append(item.target)
            if item.label_source:
                if (
                    osp.exists(item.label_target)
                    and not overwrite
                ):
                    raise ExportError(
                        "目标 json 已存在: %s" % item.label_target
                    )
                os.makedirs(
                    osp.dirname(item.label_target), exist_ok=True
                )
                shutil.copy2(item.label_source, item.label_target)
                result.labels_written.append(item.label_target)
        except Exception as exc:  # noqa: BLE001
            result.failed.append((item.source, str(exc)))
        if progress is not None:
            progress(index, total)
    return result
