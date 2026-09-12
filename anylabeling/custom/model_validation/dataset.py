"""Read-only dataset scanning and staging into the system temp folder.

This module is the ONLY place in the package that touches the user
selected source dataset directory: collect_pairs enumerates the
image/label pairs (strictly read only) and stage_dataset copies them
into a fresh temp staging folder. After staging finished, no other
module of the package may touch the source dataset directory again.
"""

from __future__ import annotations

import hashlib
import json
import os
import os.path as osp
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .labelme_io import IMAGE_EXTENSIONS

STAGING_PREFIX = "xal_validation_"
ORIGINAL_DIRNAME = "original"
AUGMENTED_DIRNAME = "augmented"
IMAGES_DIRNAME = "images"
LABELS_DIRNAME = "labels"
META_FILENAME = "meta.json"
REPORT_FILENAME = "report.json"


@dataclass
class DatasetPair:
    """One image/label pair discovered inside the source dataset."""

    relpath: str
    image_path: str = ""
    label_path: str = ""


@dataclass
class DatasetScan:
    """Result of scanning the source dataset directory."""

    dataset_dir: str = ""
    pairs: List[DatasetPair] = field(default_factory=list)
    image_without_label: List[str] = field(default_factory=list)
    label_without_image: List[str] = field(default_factory=list)
    # image relpaths whose sibling json is not a readable label file:
    # such a pair is neither staged nor validated.
    unreadable_label_pairs: List[str] = field(default_factory=list)


def natural_key(value: str) -> List[object]:
    """Sort key that keeps image_2 before image_10."""

    chunks: List[object] = []
    buffer = ""
    for char in value:
        if char.isdigit():
            buffer += char
        else:
            if buffer:
                chunks.append(int(buffer))
                buffer = ""
            chunks.append(char.lower())
    if buffer:
        chunks.append(int(buffer))
    return chunks


def is_label_usable(path: str) -> bool:
    """Return True when a json label can be loaded as an xlabel file."""

    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    shapes = data.get("shapes")
    if shapes is None:
        return True
    return isinstance(shapes, list)


def collect_pairs(dataset_dir: str) -> DatasetScan:
    """Enumerate image/label pairs under the source dataset directory.

    The traversal is strictly read only. Images without a sibling json
    label are recorded separately: they are still copied into staging
    and later reported as SKIPPED / NO_LABEL. Labels without an image
    are counted as orphan labels, and pairs whose json cannot be read
    as a label file are excluded (never staged, never validated).
    """

    scan = DatasetScan(dataset_dir=osp.abspath(dataset_dir))
    if not osp.isdir(dataset_dir):
        return scan

    images: List[str] = []
    labels: List[str] = []
    for current, _dirs, names in os.walk(dataset_dir):
        for name in names:
            full = osp.join(current, name)
            relpath = osp.relpath(full, dataset_dir).replace(os.sep, "/")
            lower = name.lower()
            if lower.endswith(IMAGE_EXTENSIONS):
                images.append(relpath)
            elif lower.endswith(".json"):
                labels.append(relpath)

    images.sort(key=natural_key)
    labels.sort(key=natural_key)
    label_set = set(labels)
    image_set = set(images)

    for relpath in images:
        stem = osp.splitext(relpath)[0]
        label_rel = stem + ".json"
        image_path = osp.join(dataset_dir, relpath.replace("/", os.sep))
        if label_rel in label_set:
            label_path = osp.join(dataset_dir, label_rel.replace("/", os.sep))
            if is_label_usable(label_path):
                scan.pairs.append(
                    DatasetPair(
                        relpath=relpath,
                        image_path=image_path,
                        label_path=label_path,
                    )
                )
            else:
                scan.unreadable_label_pairs.append(relpath)
        else:
            scan.image_without_label.append(relpath)

    for relpath in labels:
        stem = osp.splitext(relpath)[0]
        if not any((stem + ext) in image_set for ext in IMAGE_EXTENSIONS):
            scan.label_without_image.append(relpath)
    return scan


def create_staging_root(parent: str = None) -> str:
    """Create a brand new staging folder inside the system temp dir.

    A new folder is created for every run; previous folders are kept
    untouched so finished work is never destroyed.
    """

    if parent:
        os.makedirs(parent, exist_ok=True)
    root = tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=parent or None)
    for folder in (ORIGINAL_DIRNAME, AUGMENTED_DIRNAME):
        for sub in (IMAGES_DIRNAME, LABELS_DIRNAME):
            os.makedirs(osp.join(root, folder, sub), exist_ok=True)
    return root


def staging_paths(
    staging_root: str, kind: str, relpath: str
) -> Dict[str, str]:
    """Return the staging image/label paths for one record."""

    folder = ORIGINAL_DIRNAME if kind == "original" else AUGMENTED_DIRNAME
    rel = relpath.replace("/", os.sep)
    return {
        "image": osp.join(staging_root, folder, IMAGES_DIRNAME, rel),
        "label": osp.join(staging_root, folder, LABELS_DIRNAME, rel),
    }


def write_json(path: str, data: object) -> None:
    """Write a JSON document, creating parent folders when required."""

    directory = osp.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


def read_json(path: str):
    """Read a JSON document using UTF-8."""

    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def stage_dataset(
    dataset_dir: str,
    staging_root: str,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Dict[str, object]:
    """Copy every discovered pair into the staging folder.

    Returns a summary dictionary describing what was staged. Nothing is
    removed, neither from the source nor from the staging folder. The
    optional should_stop callback is asked before every image is copied,
    so a cancelled run stops within one file instead of after the whole
    dataset; the partially staged folder is kept like every other
    staging folder and its meta.json describes what was really staged.
    """

    scan = collect_pairs(dataset_dir)
    originals: List[Dict[str, str]] = []
    skipped: List[Dict[str, str]] = []
    for pair in scan.pairs:
        if should_stop is not None and should_stop():
            break
        paths = staging_paths(staging_root, "original", pair.relpath)
        os.makedirs(osp.dirname(paths["image"]), exist_ok=True)
        os.makedirs(osp.dirname(paths["label"]), exist_ok=True)
        shutil.copy2(pair.image_path, paths["image"])
        shutil.copy2(pair.label_path, paths["label"])
        originals.append(
            {
                "relpath": pair.relpath,
                "staging_image_path": paths["image"],
                "staging_label_path": paths["label"],
            }
        )
    for relpath in scan.image_without_label:
        if should_stop is not None and should_stop():
            break
        paths = staging_paths(staging_root, "original", relpath)
        os.makedirs(osp.dirname(paths["image"]), exist_ok=True)
        source = osp.join(dataset_dir, relpath.replace("/", os.sep))
        shutil.copy2(source, paths["image"])
        skipped.append(
            {
                "relpath": relpath,
                "staging_image_path": paths["image"],
                "reason": "NO_LABEL",
            }
        )

    meta: Dict[str, object] = {
        "staging_root": staging_root,
        "source_display": scan.dataset_dir,
        "counts": {
            "original": len(originals),
            "skipped_no_label": len(skipped),
            "image_without_label": len(scan.image_without_label),
            "orphan_labels": len(scan.label_without_image),
            "unreadable_label_pairs": len(scan.unreadable_label_pairs),
        },
        "originals": originals,
        "skipped": skipped,
        "orphan_labels": list(scan.label_without_image),
        "unreadable_label_pairs": list(scan.unreadable_label_pairs),
    }
    write_json(osp.join(staging_root, META_FILENAME), meta)
    return meta


def sha256_file(path: str) -> str:
    """Return the sha256 digest of a file."""

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_directory(directory: str) -> Dict[str, Dict[str, object]]:
    """Return a (relpath, sha256, mtime_ns) snapshot of a directory tree."""

    snapshot: Dict[str, Dict[str, object]] = {}
    if not osp.isdir(directory):
        return snapshot
    for current, _dirs, names in os.walk(directory):
        for name in sorted(names):
            full = osp.join(current, name)
            relpath = osp.relpath(full, directory).replace(os.sep, "/")
            stat = os.stat(full)
            snapshot[relpath] = {
                "sha256": sha256_file(full),
                "mtime_ns": stat.st_mtime_ns,
                "size": stat.st_size,
            }
    return snapshot


__all__ = [
    "AUGMENTED_DIRNAME",
    "IMAGES_DIRNAME",
    "LABELS_DIRNAME",
    "META_FILENAME",
    "ORIGINAL_DIRNAME",
    "REPORT_FILENAME",
    "STAGING_PREFIX",
    "DatasetPair",
    "DatasetScan",
    "collect_pairs",
    "create_staging_root",
    "read_json",
    "sha256_file",
    "snapshot_directory",
    "stage_dataset",
    "staging_paths",
    "write_json",
]
