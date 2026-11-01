"""Scan a YOLO export and plan the LabelMe files it can restore.

The tool works on one dataset directory plus one classes file. It never
writes anything: this module only builds the plan (which image pairs with
which .txt, which output file would be written, which pairs are invalid,
which output names collide). The conversion itself lives in conversion.py
and reuses the upstream LabelConverter.

Layouts the scanner understands:

* split: <root>/images and <root>/labels exist. The image tree is
  <root>/images and the label tree is <root>/labels; a label mirrors the
  relative directory of its image, and a flat label tree is accepted too.
* split selected on the image side: the selected directory is named
  "images" and a sibling "labels" directory exists.
* flat: anything else. Every image is paired with the .txt of the same
  stem in its own directory; when that file does not exist, the nearest
  "images" path segment is replaced by "labels" and the same stem is
  tried there. This is the layout of a folder that holds both the images
  and their YOLO labels.

The module is deliberately standard library only. No PyQt6, no cv2 and
no numpy is imported here, so the scan can be unit tested without the
desktop stack.
"""

from __future__ import annotations

import math
import os
import os.path as osp
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

__all__ = [
    "IMAGE_EXTS",
    "LAYOUT_FLAT",
    "LAYOUT_SPLIT",
    "MODE_AUTO",
    "MODE_OBB",
    "MODE_POLYGON",
    "MODE_RECTANGLE",
    "MODE_NAMES",
    "Pair",
    "ScanResult",
    "YoloCoreError",
    "detect_layout",
    "load_class_names",
    "scan_dataset",
]

#: Image extensions the scanner accepts, compared in lower case.
IMAGE_EXTS = (
    ".jpg",
    ".jpeg",
    ".png",
    ".bmp",
    ".webp",
    ".tif",
    ".tiff",
    ".gif",
)

LAYOUT_FLAT = "flat"
LAYOUT_SPLIT = "split"

MODE_AUTO = "auto"
MODE_RECTANGLE = "rectangle"
MODE_POLYGON = "polygon"
MODE_OBB = "obb"

MODE_NAMES = {
    MODE_AUTO: "自动识别",
    MODE_RECTANGLE: "检测框",
    MODE_POLYGON: "分割多边形",
    MODE_OBB: "旋转框 OBB",
}

#: Directories the recursive scan never enters.
SKIP_DIRS = frozenset({"__pycache__"})

#: File names that suggest a COCO annotation file; only used to explain
#: to the user why nothing was paired.
COCO_PREFIXES = ("coco_", "instances")


class YoloCoreError(ValueError):
    """Raised when the dataset directory or the classes file is unusable."""


@dataclass(frozen=True)
class Pair:
    """One image and the .txt file that annotates it."""

    image_path: str
    label_path: str
    output_path: str
    mode: str = ""
    line_count: int = 0
    issue: str = ""


@dataclass
class ScanResult:
    """Everything the dialog needs to show the scan and plan a run."""

    root: str
    layout: str
    class_names: List[str]
    pairs: List[Pair] = field(default_factory=list)
    invalid: List[Pair] = field(default_factory=list)
    images_without_label: int = 0
    orphan_labels: int = 0
    collisions: List[str] = field(default_factory=list)
    existing_outputs: int = 0
    mode_counts: Dict[str, int] = field(default_factory=dict)
    hint_coco: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        """True when the plan can be converted as a whole."""

        return (
            not self.error
            and not self.collisions
            and bool(self.pairs)
        )

    @property
    def class_count(self) -> int:
        return len(self.class_names)


def _norm(path: str) -> str:
    """Return the case folded absolute form used as a dictionary key."""

    return osp.normcase(osp.abspath(path))


def _walk_files(root: str):
    """Yield every file below root in a deterministic order.

    Hidden directories and __pycache__ are pruned. Symlinked directories
    are not followed, os.walk does that by default.
    """

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not name.startswith(".") and name not in SKIP_DIRS
        )
        for name in sorted(filenames):
            yield osp.join(dirpath, name)


def _iter_images(root: str):
    for path in _walk_files(root):
        if osp.splitext(path)[1].lower() in IMAGE_EXTS:
            yield path


def _iter_labels(root: str):
    for path in _walk_files(root):
        if osp.splitext(path)[1].lower() == ".txt":
            yield path


def detect_layout(root: str) -> Tuple[str, str, Optional[str]]:
    """Return (layout, image_root, label_root).

    label_root is None for the flat layout.
    """

    root = osp.abspath(root)
    images = osp.join(root, "images")
    labels = osp.join(root, "labels")
    if osp.isdir(images) and osp.isdir(labels):
        return LAYOUT_SPLIT, images, labels
    if osp.basename(root).lower() == "images":
        parent = osp.dirname(root)
        sibling = osp.join(parent, "labels")
        if osp.isdir(sibling):
            return LAYOUT_SPLIT, root, sibling
    return LAYOUT_FLAT, root, None


def load_class_names(path: str) -> List[str]:
    """Load the class table of a YOLO dataset.

    utf-8-sig, one name per line, surrounding whitespace stripped and
    empty lines dropped. Duplicated names are allowed: two class indices
    then map to the same LabelMe label. An empty table is an error.
    """

    if not path or not osp.isfile(path):
        raise YoloCoreError("classes file not found: %s" % (path,))
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            text = handle.read()
    except OSError as error:
        raise YoloCoreError(
            "classes file unreadable: %s (%s)" % (path, error)
        ) from error
    names = [line.strip() for line in text.splitlines()]
    names = [name for name in names if name]
    if not names:
        raise YoloCoreError("classes file is empty: %s" % (path,))
    return names


def _flat_label_candidates(image_path: str, stem: str) -> List[str]:
    """Return the .txt candidates of a flat/recursive image."""

    candidates = [osp.join(osp.dirname(image_path), stem + ".txt")]
    absolute = osp.normpath(osp.abspath(image_path))
    parts = absolute.split(osp.sep)
    for index in range(len(parts) - 2, -1, -1):
        if parts[index].lower() != "images":
            continue
        replaced = list(parts)
        replaced[index] = "labels"
        replaced[-1] = stem + ".txt"
        candidates.append(osp.sep.join(replaced))
    return candidates


def _resolve_label(
    image_path: str,
    image_root: str,
    label_root: Optional[str],
) -> Optional[str]:
    """Return the existing .txt of one image, or None."""

    stem = osp.splitext(osp.basename(image_path))[0]
    if label_root is None:
        candidates = _flat_label_candidates(image_path, stem)
    else:
        rel_dir = osp.relpath(osp.dirname(image_path), image_root)
        candidates = []
        if rel_dir not in ("", "."):
            candidates.append(
                osp.join(label_root, rel_dir, stem + ".txt")
            )
        candidates.append(osp.join(label_root, stem + ".txt"))
    for candidate in candidates:
        if osp.isfile(candidate):
            return candidate
    return None


def _split_line(raw: str) -> Tuple[Optional[List[str]], str]:
    """Split one YOLO line on single spaces, or return an error."""

    line = raw.strip()
    if not line:
        return [], ""
    parts = line.split(" ")
    if any(part == "" for part in parts):
        return None, "multiple spaces or tabs between fields"
    return parts, ""


def parse_label_file(
    path: str,
    class_count: int,
    requested_mode: str,
) -> Tuple[str, int, str]:
    """Validate one YOLO label file.

    Returns (mode, line_count, issue). mode is "" for an empty file and
    issue is "" when the file is usable. The mode is the internal name:
    rectangle / polygon / obb.
    """

    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            lines = handle.read().splitlines()
    except (OSError, UnicodeError) as error:
        return "", 0, "label unreadable: %s" % (error,)

    kinds: List[str] = []
    token_counts: List[int] = []
    count = 0
    for raw in lines:
        parts, issue = _split_line(raw)
        if issue:
            return "", count, issue
        if not parts:
            continue
        count += 1
        if len(parts) < 5:
            return "", count, "expected at least 5 fields"
        try:
            class_index = int(parts[0])
        except ValueError:
            return "", count, "class index is not an integer"
        if class_index < 0 or class_index >= class_count:
            return "", count, (
                "class index %d is outside 0..%d"
                % (class_index, class_count - 1)
            )
        for token in parts[1:]:
            try:
                value = float(token)
            except ValueError:
                return "", count, "coordinate is not a number"
            if not math.isfinite(value):
                return "", count, "coordinate is not finite"
        token_counts.append(len(parts))
        if len(parts) == 5:
            kinds.append(MODE_RECTANGLE)
        elif len(parts) > 5 and (len(parts) - 1) % 2 == 0:
            kinds.append(MODE_POLYGON)
        else:
            return "", count, (
                "expected 5 fields or an even number of coordinates"
            )

    if not kinds:
        return "", 0, ""
    unique = set(kinds)
    if len(unique) != 1:
        return "", count, "mixed box and polygon lines in one file"
    detected = kinds[0]

    if requested_mode == MODE_AUTO:
        return detected, count, ""
    if requested_mode == MODE_RECTANGLE:
        if detected != MODE_RECTANGLE:
            return "", count, "file is not a detection box file"
        return MODE_RECTANGLE, count, ""
    if requested_mode == MODE_POLYGON:
        if detected != MODE_POLYGON:
            return "", count, "file is not a polygon file"
        return MODE_POLYGON, count, ""
    if requested_mode == MODE_OBB:
        if any(token_count != 9 for token_count in token_counts):
            return "", count, "OBB needs exactly 8 coordinates per line"
        return MODE_OBB, count, ""
    raise YoloCoreError("unknown mode: %s" % (requested_mode,))


def _coco_hint(root: str) -> bool:
    """True when the folder looks like it holds a COCO annotation."""

    for path in _walk_files(root):
        name = osp.basename(path).lower()
        if not name.endswith(".json"):
            continue
        if name.startswith(COCO_PREFIXES):
            return True
    return False


def scan_dataset(
    root: str,
    class_file: str,
    mode: str = MODE_AUTO,
) -> ScanResult:
    """Scan one dataset directory and return the conversion plan."""

    if mode not in (MODE_AUTO, MODE_RECTANGLE, MODE_POLYGON, MODE_OBB):
        raise YoloCoreError("unknown mode: %s" % (mode,))
    root = osp.abspath(root)
    if not osp.isdir(root):
        raise YoloCoreError("dataset directory not found: %s" % (root,))
    class_names = load_class_names(class_file)

    layout, image_root, label_root = detect_layout(root)
    images = list(_iter_images(image_root))
    if layout == LAYOUT_SPLIT and not images:
        layout = LAYOUT_FLAT
        image_root = root
        label_root = None
        images = list(_iter_images(root))

    result = ScanResult(
        root=root,
        layout=layout,
        class_names=class_names,
    )
    class_key = _norm(class_file)
    used_labels = set()
    outputs: Dict[str, str] = {}
    seen_collisions = set()

    for image_path in images:
        label_path = _resolve_label(image_path, image_root, label_root)
        if label_path is None:
            result.images_without_label += 1
            continue
        used_labels.add(_norm(label_path))
        stem = osp.splitext(osp.basename(image_path))[0]
        output_path = osp.join(osp.dirname(image_path), stem + ".json")
        pair_mode, line_count, issue = parse_label_file(
            label_path, len(class_names), mode
        )
        pair = Pair(
            image_path=image_path,
            label_path=label_path,
            output_path=output_path,
            mode=pair_mode,
            line_count=line_count,
            issue=issue,
        )
        if issue:
            result.invalid.append(pair)
            continue
        if osp.isdir(output_path):
            pair = Pair(
                image_path=image_path,
                label_path=label_path,
                output_path=output_path,
                mode="",
                line_count=line_count,
                issue="output path is a directory",
            )
            result.invalid.append(pair)
            continue
        result.pairs.append(pair)
        key = _norm(output_path)
        if key in outputs:
            label = "%s / %s -> %s" % (
                outputs[key],
                image_path,
                output_path,
            )
            if label not in seen_collisions:
                result.collisions.append(label)
                seen_collisions.add(label)
        else:
            outputs[key] = image_path
        bucket = pair_mode or "empty"
        result.mode_counts[bucket] = result.mode_counts.get(bucket, 0) + 1

    for path in _iter_labels(label_root if label_root else root):
        key = _norm(path)
        if key == class_key:
            continue
        if key not in used_labels:
            result.orphan_labels += 1

    result.existing_outputs = sum(
        1 for pair in result.pairs if osp.isfile(pair.output_path)
    )
    if not result.pairs and not result.invalid:
        result.hint_coco = _coco_hint(root)
    return result
