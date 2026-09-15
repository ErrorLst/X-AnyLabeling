"""Pure data layer of the preview tool.

Nothing in this module imports a GUI toolkit, and no function ever
raises on its own: a scan runs inside a worker thread, so a folder that
disappeared, a label file that is not valid JSON or a permission error
all have to come back as an empty result instead of an exception.

The filter combines three axes. The score threshold and the size mode
are joined with "and", and both have to be satisfied by the shapes the
category axis selected - one shape may satisfy the score and another
one the size. A shape without a score counts as 0.0. Turning the master
switch off keeps every image; a threshold of zero turns its axis off.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from enum import Enum
from typing import FrozenSet, List, Optional, Tuple

__all__ = [
    "BACKGROUND_LABEL",
    "FILTER_DEFAULT_SCORE",
    "FILTER_DEFAULT_SIZE",
    "PreviewFilter",
    "SizeFilterMode",
    "SHAPE_TYPES",
    "default_output_dir",
    "filter_images",
    "find_label_file",
    "localize",
    "natural_key",
    "parse_annotations",
    "scan_directory",
]

#: Pseudo category of an image whose label file carries no shape.
BACKGROUND_LABEL = "背景"

#: Shape types the preview draws, in the order of the reference tool.
SHAPE_TYPES = ("rectangle", "polygon", "circle", "line", "point", "rotation")

#: Extensions a scan accepts, lower case.
IMAGE_EXTS = (
    ".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif", ".tif", ".tiff",
)

FILTER_DEFAULT_SCORE = 0.45
FILTER_DEFAULT_SIZE = 10.0
SCAN_WORKERS = 4


class SizeFilterMode(str, Enum):
    """How the width and the height of a shape are compared."""

    ANY_ABOVE = "any_above"
    BOTH_ABOVE = "both_above"
    ANY_BELOW = "any_below"
    BOTH_BELOW = "both_below"

    @property
    def label(self) -> str:
        """Return the Chinese text the mode combo box shows."""

        return _MODE_LABELS[self]

    @property
    def above(self) -> bool:
        """Return True when the mode compares against "greater"."""

        return self in (SizeFilterMode.ANY_ABOVE, SizeFilterMode.BOTH_ABOVE)


_MODE_LABELS = {
    SizeFilterMode.ANY_ABOVE: "宽或高大于阈值",
    SizeFilterMode.BOTH_ABOVE: "宽和高都大于阈值",
    SizeFilterMode.ANY_BELOW: "宽或高小于阈值",
    SizeFilterMode.BOTH_BELOW: "宽和高都小于阈值",
}


@dataclass(frozen=True, slots=True)
class ShapeInfo:
    """One annotation, already converted to plain Python values.

    The points stay tuples of floats on purpose: the overlay converts
    them into QPointF on the main thread only, never inside a worker.
    """

    label: str
    score: Optional[float]
    shape_type: str
    points: Tuple[Tuple[float, float], ...]
    width: float
    height: float

    def bbox(self) -> Tuple[float, float, float, float]:
        """Return the bounding box of the points as x0, y0, x1, y1."""

        if not self.points:
            return (0.0, 0.0, 0.0, 0.0)
        xs = [point[0] for point in self.points]
        ys = [point[1] for point in self.points]
        return (min(xs), min(ys), max(xs), max(ys))


@dataclass(slots=True)
class ImageEntry:
    """One image of a scanned folder and what is known about it."""

    path: str
    name: str
    json_path: Optional[str] = None
    labels: FrozenSet[str] = frozenset()
    shapes: Tuple[ShapeInfo, ...] = ()


@dataclass(frozen=True, slots=True)
class PreviewFilter:
    """The parameters of the filter, None meaning "filter is off"."""

    score_threshold: float = FILTER_DEFAULT_SCORE
    size_mode: SizeFilterMode = SizeFilterMode.ANY_ABOVE
    width_threshold: float = FILTER_DEFAULT_SIZE
    height_threshold: float = FILTER_DEFAULT_SIZE


def natural_key(value: str) -> List[object]:
    """Sort key that keeps image_2 before image_10.

    Only decimal digits open a numeric run. A superscript such as the
    two of a2 passes str.isdigit() but int() rejects it with a
    ValueError, so it is compared as the ordinary character it is.
    """

    chunks: List[object] = []
    buffer = ""
    for char in str(value):
        if char.isdecimal():
            buffer += char
        else:
            if buffer:
                chunks.append(int(buffer))
                buffer = ""
            chunks.append(char.lower())
    if buffer:
        chunks.append(int(buffer))
    return chunks


def default_output_dir() -> str:
    """Return the output directory of a run that has none configured.

    The current working directory is fetched on every call: the
    default is deliberately never cached and never written back.
    """

    return os.getcwd()


def _parse_shape(raw) -> Optional[ShapeInfo]:
    """Return one shape of a LabelMe document, None when it is broken."""

    if not isinstance(raw, dict):
        return None
    shape_type = raw.get("shape_type", "")
    if not isinstance(shape_type, str) or shape_type not in SHAPE_TYPES:
        return None
    raw_points = raw.get("points")
    if not isinstance(raw_points, (list, tuple)) or not raw_points:
        return None
    points: List[Tuple[float, float]] = []
    try:
        for point in raw_points:
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                return None
            points.append((float(point[0]), float(point[1])))
    except (TypeError, ValueError):
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    label = raw.get("label", "")
    if not isinstance(label, str):
        label = str(label)
    raw_score = raw.get("score")
    if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
        score = None
    else:
        score = float(raw_score)
    return ShapeInfo(
        label=label,
        score=score,
        shape_type=shape_type,
        points=tuple(points),
        width=max(xs) - min(xs),
        height=max(ys) - min(ys),
    )


def parse_annotations(data) -> List[ShapeInfo]:
    """Return the shapes of an already loaded LabelMe document.

    A broken shape is skipped, a document that is not a dictionary or
    whose "shapes" is not a list yields an empty list. This function
    never raises, whatever the caller hands over.
    """

    if not isinstance(data, dict):
        return []
    raw_shapes = data.get("shapes")
    if not isinstance(raw_shapes, list):
        return []
    shapes: List[ShapeInfo] = []
    for raw in raw_shapes:
        shape = _parse_shape(raw)
        if shape is not None:
            shapes.append(shape)
    return shapes


def find_label_file(image_path: str) -> Optional[str]:
    """Return the LabelMe side car of an image, None when absent."""

    try:
        candidate = os.path.splitext(str(image_path))[0] + ".json"
    except (TypeError, ValueError):
        return None
    try:
        if os.path.isfile(candidate):
            return candidate
    except OSError:
        return None
    return None


def _load_label_file(json_path: str):
    """Return the parsed document of a label file, None when unreadable."""

    try:
        with open(json_path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _sorted_entries(entries: List[ImageEntry]) -> List[ImageEntry]:
    """Sort entries naturally, falling back to text when types clash."""

    try:
        return sorted(entries, key=lambda entry: natural_key(entry.name))
    except TypeError:
        return sorted(
            entries, key=lambda entry: (entry.name.lower(), entry.name)
        )


def scan_directory(directory: str) -> List[ImageEntry]:
    """List the top level images of a directory in natural order.

    The scan is deliberately not recursive and never raises: a missing
    directory, a path that is a file or a permission error all yield an
    empty list. The label file of an image is only located, not read;
    the worker parses the documents in its own pool.
    """

    entries: List[ImageEntry] = []
    try:
        with os.scandir(directory) as iterator:
            for item in iterator:
                try:
                    if not item.is_file():
                        continue
                except OSError:
                    continue
                name = item.name
                if os.path.splitext(name)[1].lower() not in IMAGE_EXTS:
                    continue
                entries.append(
                    ImageEntry(
                        path=os.path.join(directory, name),
                        name=name,
                        json_path=find_label_file(item.path),
                    )
                )
    except (OSError, TypeError, ValueError):
        return []
    return _sorted_entries(entries)


def _record_missing(missing, name: str) -> None:
    """Add one image name to the collector of unlabelled images."""

    if missing is None:
        return
    try:
        add = getattr(missing, "add", None)
        if callable(add):
            add(name)
            return
        missing.append(name)
    except (AttributeError, TypeError, ValueError):
        return


def localize(
    directory: str,
    image_path: str,
    missing=None,
) -> Tuple[Optional[str], Tuple[ShapeInfo, ...]]:
    """Return the label file and the shapes of one image.

    The image path may be relative to the directory. The optional
    collector "missing" receives the name of every image without a
    readable label file. The shapes stay plain tuples; a document that
    cannot be read yields an empty tuple, never a raise.
    """

    try:
        path = str(image_path)
        if not os.path.isabs(path):
            path = os.path.join(str(directory), path)
        json_path = find_label_file(path)
        if json_path is None:
            _record_missing(missing, os.path.basename(path))
            return None, ()
        data = _load_label_file(json_path)
        if data is None:
            _record_missing(missing, os.path.basename(path))
            return json_path, ()
        return json_path, tuple(parse_annotations(data))
    except (OSError, TypeError, ValueError):
        return None, ()


def score_ok(score, cfg: PreviewFilter) -> bool:
    """Return True when one score satisfies the score axis.

    A threshold of zero turns the axis off. An annotation without a
    score counts as 0.0, so it can never pass a positive threshold.
    The argument is normally the score itself; a shape carrying one is
    accepted as well, so both call styles keep working.
    """

    if cfg.score_threshold <= 0.0:
        return True
    value = getattr(score, "score", score)
    try:
        value = 0.0 if value is None else float(value)
    except (TypeError, ValueError):
        value = 0.0
    if value != value:  # NaN never satisfies a threshold.
        value = 0.0
    return value >= cfg.score_threshold


def size_ok(shape: ShapeInfo, cfg: PreviewFilter) -> bool:
    """Return True when one shape satisfies the size axis.

    The comparisons are strict, so a shape exactly as wide as the
    threshold does not pass either an "above" or a "below" mode.
    """

    width = float(shape.width)
    height = float(shape.height)
    tw = cfg.width_threshold
    th = cfg.height_threshold
    mode = cfg.size_mode
    if mode == SizeFilterMode.BOTH_ABOVE:
        return width > tw and height > th
    if mode == SizeFilterMode.ANY_BELOW:
        return width < tw or height < th
    if mode == SizeFilterMode.BOTH_BELOW:
        return width < tw and height < th
    return width > tw or height > th


def filter_images(entries, cfg, checked):
    """Split a scan into the kept entries, its total and its categories.

    The selection "checked" is None when the category axis is off,
    otherwise it is the set of selected labels; an image without shapes
    only survives when the background pseudo category is selected. The
    category list always starts with the background entry and carries
    the labels of every entry, kept or not, so the combo box can offer
    the full set.
    """

    items = list(entries or ())
    labels = set()
    kept: List[ImageEntry] = []
    for entry in items:
        shapes = tuple(getattr(entry, "shapes", ()) or ())
        for shape in shapes:
            labels.add(shape.label)
        if cfg is None:
            kept.append(entry)
            continue
        if not shapes:
            if checked is None or BACKGROUND_LABEL in checked:
                kept.append(entry)
            continue
        if checked is None:
            selected = shapes
        else:
            selected = tuple(s for s in shapes if s.label in checked)
        if not selected:
            continue
        if any(score_ok(shape.score, cfg) for shape in selected) and any(
            size_ok(shape, cfg) for shape in selected
        ):
            kept.append(entry)
    categories = [BACKGROUND_LABEL] + sorted(
        labels.difference({BACKGROUND_LABEL})
    )
    return kept, len(items), categories
