"""LabelMe export helpers for the model validation tool."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

IMAGE_EXTENSIONS: Tuple[str, ...] = (
    ".jpg",
    ".jpeg",
    ".png",
    ".bmp",
    ".webp",
    ".tif",
    ".tiff",
)

LABELME_VERSION = "5.2.1"

SHAPE_TYPE_MAP: Dict[str, str] = {
    "rectangle": "rectangle",
    "rotation": "polygon",
    "polygon": "polygon",
    "quadrilateral": "polygon",
    "point": "point",
    "line": "line",
    "linestrip": "linestrip",
    "circle": "circle",
    "cuboid": "polygon",
}

LABELME_KNOWN_LIMITATIONS: Tuple[str, ...] = (
    "rotation maps to polygon (the rotation direction field is dropped)",
    "cuboid maps to polygon (the projected 3D box is flattened)",
)

REGION_SHAPE_TYPES = (
    "rectangle",
    "rotation",
    "quadrilateral",
    "polygon",
)

POINT_SET_SHAPE_TYPES = (
    "polygon",
    "rectangle",
    "rotation",
    "quadrilateral",
    "point",
    "line",
    "circle",
    "linestrip",
    "cuboid",
)


def to_points(value: Any) -> List[Tuple[float, float]]:
    """Normalise every accepted point representation to float pairs."""

    points: List[Tuple[float, float]] = []
    if value is None:
        return points
    if hasattr(value, "x") and hasattr(value, "y"):
        return [(float(value.x()), float(value.y()))]
    for item in value:
        if item is None:
            continue
        if hasattr(item, "x") and hasattr(item, "y"):
            points.append((float(item.x()), float(item.y())))
            continue
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            points.append((float(item[0]), float(item[1])))
    return points


def points_to_labelme(
    points: Sequence[Tuple[float, float]],
) -> List[List[float]]:
    """Convert points into the JSON friendly LabelMe representation."""

    return [[float(x), float(y)] for x, y in points]


def shape_to_labelme(shape: Dict[str, Any]) -> Dict[str, Any]:
    """Convert one xlabel shape into a LabelMe shape."""

    shape_type = str(shape.get("shape_type") or "polygon")
    points = to_points(shape.get("points"))
    if shape_type == "rectangle" and len(points) >= 2:
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        points = [
            (min(xs), min(ys)),
            (max(xs), max(ys)),
        ]
    labelme_type = SHAPE_TYPE_MAP.get(shape_type, "polygon")
    return {
        "label": shape.get("label", ""),
        "points": points_to_labelme(points),
        "group_id": shape.get("group_id"),
        "description": shape.get("description", ""),
        "shape_type": labelme_type,
        "flags": shape.get("flags", {}) or {},
        "mask": None,
    }


def to_labelme_dict(
    label_data: Dict[str, Any],
    image_file: str,
    image_height: Optional[int] = None,
    image_width: Optional[int] = None,
) -> Dict[str, Any]:
    """Build a complete LabelMe document from a staging xlabel document."""

    height = image_height
    width = image_width
    if height is None:
        height = int(label_data.get("imageHeight", -1) or -1)
    if width is None:
        width = int(label_data.get("imageWidth", -1) or -1)
    return {
        "version": LABELME_VERSION,
        "flags": label_data.get("flags", {}) or {},
        "shapes": [
            shape_to_labelme(shape)
            for shape in label_data.get("shapes", []) or []
        ],
        "imagePath": os.path.basename(image_file),
        "imageData": None,
        "imageHeight": height,
        "imageWidth": width,
    }


def is_region_shape(shape_type: str) -> bool:
    """Return True for shapes that may participate in box matching."""

    return shape_type in REGION_SHAPE_TYPES


__all__ = [
    "IMAGE_EXTENSIONS",
    "LABELME_KNOWN_LIMITATIONS",
    "LABELME_VERSION",
    "POINT_SET_SHAPE_TYPES",
    "REGION_SHAPE_TYPES",
    "SHAPE_TYPE_MAP",
    "is_region_shape",
    "points_to_labelme",
    "shape_to_labelme",
    "to_labelme_dict",
    "to_points",
]
