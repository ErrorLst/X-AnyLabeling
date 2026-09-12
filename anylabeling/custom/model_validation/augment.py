"""Single image augmentation built on Albumentations ReplayCompose.

Only one augmentation backend is implemented on purpose: the official
Albumentations transform stack. The image and every shape geometry walk
through the very same random decision by replaying the recorded
transform, therefore an augmented label keeps the exact shape_type,
point count and field structure of its original.

Grayscale content - a single channel image, or three channels holding
the very same value - travels through RGB: the decoded gray image is
promoted to three channels before the stack runs and is collapsed back
to a single channel before it is encoded, exactly like a colour image.
The colour parameters therefore stay meaningful on gray data (hsv_h and
hsv_s end up as a brightness change of the RGB representation) and the
albumentations "not applicable to grayscale image" warning can never
fire. Only pixels travel through the conversion, never a coordinate,
so the geometry of every shape is untouched.
"""

from __future__ import annotations

import logging
import math
import os
import os.path as osp
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

from .app_config import AugmentParams, attempt_seed
from .labelme_io import to_points

# The logger of the module: a sample that no attempt could fit into the
# canvas is reported here and in the summary of the stage, never on the
# interface.
LOGGER = logging.getLogger(__name__)

JPEG_QUALITY = 100
WEBP_QUALITY = 100
ENCODE_FALLBACK_EXT = ".png"
CLIP_EPSILON = 1e-6
CIRCLE_PROBE_COUNT = 8

# How many times one sample may be generated with a fresh seed before it
# is dropped. Every attempt draws its seed from (base seed, sample index,
# attempt), so the outcome of a sample never depends on the number of
# threads nor on the order the samples finish in.
MAX_ATTEMPTS = 5

IMAGE_ENCODING = {
    ".jpg": (".jpg", cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY),
    ".jpeg": (".jpg", cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY),
    ".png": (".png", None, None),
    ".bmp": (".bmp", None, None),
    ".webp": (".webp", cv2.IMWRITE_WEBP_QUALITY, WEBP_QUALITY),
    ".tif": (".tif", None, None),
    ".tiff": (".tif", None, None),
}

# The gray path of augment_sample: a single channel image is promoted to
# three channels for the albumentations stack and collapsed back before
# encoding, so the augmented copy keeps the channel count of its source.
GRAYSCALE_DIMENSIONS = 2
BGR_CHANNELS = 3

# The fill of the canvas a geometric transform uncovers: pure black, a
# fixed value that never depends on the content of the picture being
# augmented. It is not configurable - build_transforms pins the constant
# border mode AND this fill on Affine and on Perspective alike - and the
# word the snapshot records for it is app_config.BORDER_FILL.
BLACK_FILL = 0
BLACK_FILL_RGB = (0, 0, 0)

GRAYSCALE_PATH_NOTE = (
    "grayscale content is promoted to three channels for the augmentation "
    "stack and collapsed back to a single channel before it is encoded; "
    "the very same parameter set is applied as to a colour image"
)


def is_grayscale_content(image: np.ndarray) -> bool:
    """Return True when every pixel of an image holds one intensity only.

    Two shapes are covered because the real datasets carry both: a
    single channel image (what cv2.imdecode with IMREAD_UNCHANGED gives
    back for an L mode file) and a three channel image whose R, G and B
    planes are equal pixel by pixel (a gray picture saved as RGB).
    """

    if image is None:
        return False
    if image.ndim == GRAYSCALE_DIMENSIONS:
        return True
    if image.ndim != 3:
        return False
    channels = int(image.shape[2])
    if channels == 1:
        return True
    if channels < BGR_CHANNELS:
        return False
    first = image[:, :, 0]
    return bool(
        np.array_equal(first, image[:, :, 1])
        and np.array_equal(first, image[:, :, 2])
    )


def promote_to_bgr(image: np.ndarray) -> np.ndarray:
    """Return a three channel BGR view of a possibly gray image."""

    if image.ndim == GRAYSCALE_DIMENSIONS:
        return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    if image.ndim == 3 and int(image.shape[2]) == 1:
        return cv2.cvtColor(image[:, :, 0], cv2.COLOR_GRAY2BGR)
    return image


def collapse_to_grayscale(image: np.ndarray) -> np.ndarray:
    """Return the single channel luminance of a colour image."""

    if image.ndim == GRAYSCALE_DIMENSIONS:
        return image
    if image.ndim == 3 and int(image.shape[2]) >= BGR_CHANNELS:
        return cv2.cvtColor(
            np.ascontiguousarray(image[:, :, :BGR_CHANNELS]),
            cv2.COLOR_BGR2GRAY,
        )
    return image


@dataclass
class ClipReport:
    """Description of one shape that had to be clipped to the image."""

    shape_index: int
    label: str
    shape_type: str
    clipped: bool = False
    lost: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON friendly representation."""

        return {
            "shape_index": self.shape_index,
            "label": self.label,
            "shape_type": self.shape_type,
            "clipped": self.clipped,
            "lost": self.lost,
        }


@dataclass
class AugmentOutcome:
    """Result of augmenting a single image.

    A produced outcome always holds every shape of its original fully
    inside the canvas: a try whose geometry leaves the frame is repeated
    with a fresh seed and a sample no try could fit is dropped, so the
    clipped list below is empty for every sample that reaches a file.
    """

    image: np.ndarray
    label: Dict[str, Any]
    width: int
    height: int
    clipped: List[Dict[str, Any]] = field(default_factory=list)
    type_counts: Dict[str, int] = field(default_factory=dict)
    # True when the sample travelled through the gray -> RGB -> gray path
    grayscale: bool = False
    # How many tries the sample needed, one by default, and the zero
    # based index plus the seed of the try that really produced it: the
    # pair makes the produced copy reproducible on its own.
    attempts: int = 1
    attempt: int = 0
    seed: int = 0


def black_fill_tuple(channels: Any) -> Tuple[float, ...]:
    """Return the pure black fill of a picture with that many channels.

    One value per channel for a picture the caller described (a single
    channel one included, since albumentations accepts a scalar and a
    tuple alike) and the three channel fill of the colour path for one it
    could not describe: the fill is black whatever the data looks like.
    """

    try:
        count = int(channels)
    except (TypeError, ValueError):
        count = 0
    if count == 1:
        return (float(BLACK_FILL),)
    if count >= 2:
        return tuple(float(BLACK_FILL) for _ in range(count))
    return tuple(float(value) for value in BLACK_FILL_RGB)


def image_channels(image: Optional[np.ndarray]) -> int:
    """Return the channel count the fill of an image is made of.

    A 2D picture (a gray array without a channel axis) counts as a single
    channel, a 3D one as its last dimension. A picture that is missing or
    carries no shape at all falls back to the three channel fill of the
    colour path.
    """

    shape = getattr(image, "shape", None)
    if shape is None:
        return BGR_CHANNELS
    try:
        dimensions = len(shape)
    except TypeError:
        return BGR_CHANNELS
    if dimensions >= BGR_CHANNELS:
        try:
            return max(1, int(shape[2]))
        except (TypeError, ValueError, IndexError):
            return BGR_CHANNELS
    if dimensions == GRAYSCALE_DIMENSIONS:
        return 1
    return BGR_CHANNELS


def black_fill_for(image: Optional[np.ndarray] = None) -> Tuple[float, ...]:
    """Return the pure black fill of the canvas this picture uncovers.

    The one fill of the tool, always black: a fixed value, never derived
    from the picture being augmented (the median of a bright picture used
    to be filled in, which the user saw as a grey band where the canvas
    was uncovered). The value simply matches the channel count of the
    picture so that a gray copy keeps its shape.
    """

    return black_fill_tuple(image_channels(image))


def build_transforms(
    params: AugmentParams, fill: Optional[Sequence[float]] = None
) -> List[Any]:
    """Build the Albumentations transform stack from the parameters.

    Every geometric transform that can uncover canvas (Affine and
    Perspective) is pinned to the constant border mode and to the pure
    black fill augment_sample derives with augment.black_fill_for: the
    fill is not an option any more, therefore no mode can replicate the
    edge pixels or mirror the picture into the uncovered band. A caller
    that hands no fill over gets the black one all the same, so the
    constant mode never falls back to the library default either.
    """

    import albumentations as albu

    values = black_fill_for(None) if fill is None else fill
    border: Dict[str, Any] = {
        "border_mode": cv2.BORDER_CONSTANT,
        "fill": tuple(float(value) for value in values),
    }

    return [
        albu.HueSaturationValue(
            hue_shift_limit=params.hsv_h * 180.0,
            sat_shift_limit=params.hsv_s * 100.0,
            val_shift_limit=params.hsv_v * 100.0,
            p=1.0,
        ),
        albu.Affine(
            rotate=(-params.degrees, params.degrees),
            translate_percent={
                "x": (-params.translate, params.translate),
                "y": (-params.translate, params.translate),
            },
            scale=(params.scale_min, params.scale_max),
            shear={
                "x": (-params.shear, params.shear),
                "y": (-params.shear, params.shear),
            },
            p=1.0,
            **border,
        ),
        albu.Perspective(
            scale=(0.0, params.perspective),
            p=1.0,
            **border,
        ),
        albu.VerticalFlip(p=params.flipud),
        albu.HorizontalFlip(p=params.fliplr),
    ]


# The transform stack intentionally has no bbox channel: albumentations
# validates axis aligned boxes between every transform and rejects the
# degenerate boxes that strong geometric augmentation produces. Every shape
# therefore travels through the keypoint channel, which keeps the point count
# and the exact geometry, and the frame is restored by ReplayCompose.


def build_replay_compose(
    params: AugmentParams, fill: Optional[Sequence[float]] = None
):
    """Build a ReplayCompose with the keypoint channel attached."""

    import albumentations as albu

    return albu.ReplayCompose(
        build_transforms(params, fill),
        keypoint_params=albu.KeypointParams(
            format="xy",
            label_fields=["kp_labels"],
            remove_invisible=False,
        ),
    )


def shape_points(shape: Dict[str, Any]) -> List[Tuple[float, float]]:
    """Return the float point list of one xlabel shape."""

    return to_points(shape.get("points"))


def _aabb(points: Sequence[Tuple[float, float]]):
    if not points:
        return None
    xs = [float(p[0]) for p in points]
    ys = [float(p[1]) for p in points]
    box = (min(xs), min(ys), max(xs), max(ys))
    if box[2] - box[0] <= 0 or box[3] - box[1] <= 0:
        return None
    return box


def clip_points(
    points: Sequence[Tuple[float, float]], width: int, height: int
) -> Tuple[List[Tuple[float, float]], bool]:
    """Clip points into [0, width - 1] x [0, height - 1]."""

    max_x = float(max(width - 1, 0))
    max_y = float(max(height - 1, 0))
    clipped = False
    result: List[Tuple[float, float]] = []
    for x, y in points:
        cx = min(max(float(x), 0.0), max_x)
        cy = min(max(float(y), 0.0), max_y)
        if abs(cx - float(x)) > CLIP_EPSILON:
            clipped = True
        if abs(cy - float(y)) > CLIP_EPSILON:
            clipped = True
        result.append((cx, cy))
    return result, clipped


def polygon_area(points: Sequence[Tuple[float, float]]) -> float:
    """Return the unsigned polygon area of a point ring."""

    if len(points) < 3:
        return 0.0
    total = 0.0
    count = len(points)
    for index in range(count):
        x1, y1 = points[index]
        x2, y2 = points[(index + 1) % count]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def rotation_direction(points: Sequence[Tuple[float, float]]) -> float:
    """Compute the rotation direction in radians for a four point quad.

    The repository convention stores the rotation direction in radians
    within [0, 2 * pi) (see calculate_rotation_theta), which is what
    the main application reads back from shape.direction.
    """

    if len(points) < 2:
        return 0.0
    (x0, y0), (x1, y1) = points[0], points[1]
    angle = float(np.degrees(np.arctan2(y1 - y0, x1 - x0)))
    return math.radians(angle % 360.0)


def is_circle(points: Sequence[Tuple[float, float]]) -> bool:
    """Return True when the points describe a circle within tolerance."""

    if len(points) < 3:
        return False
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    if max(xs) - min(xs) <= CLIP_EPSILON or max(ys) - min(ys) <= CLIP_EPSILON:
        return False
    cx = sum(p[0] for p in points) / len(points)
    cy = sum(p[1] for p in points) / len(points)
    radii = [float(np.hypot(p[0] - cx, p[1] - cy)) for p in points]
    mean = float(np.mean(radii))
    if mean <= CLIP_EPSILON:
        return False
    tolerance = max(1e-3, mean * 1e-3)
    return float(np.max(np.abs(np.array(radii) - mean))) <= tolerance


def circle_probe_points(
    first: Tuple[float, float], second: Tuple[float, float]
) -> List[Tuple[float, float]]:
    """Return an evenly spaced probe ring for a circle shape."""

    cx, cy = float(first[0]), float(first[1])
    radius = float(np.hypot(second[0] - cx, second[1] - cy))
    angles = np.linspace(0.0, 2.0 * np.pi, CIRCLE_PROBE_COUNT, endpoint=False)
    return [
        (float(cx + radius * np.cos(a)), float(cy + radius * np.sin(a)))
        for a in angles
    ]


def fit_circle(
    points: Sequence[Tuple[float, float]],
) -> Tuple[Tuple[float, float], Tuple[float, float]]:
    """Approximate a transformed circle by centroid and mean radius."""

    cx = sum(p[0] for p in points) / len(points)
    cy = sum(p[1] for p in points) / len(points)
    radius = float(np.mean([np.hypot(p[0] - cx, p[1] - cy) for p in points]))
    angle = float(np.arctan2(points[0][1] - cy, points[0][0] - cx))
    centre = (float(cx), float(cy))
    edge = (
        float(cx + radius * np.cos(angle)),
        float(cy + radius * np.sin(angle)),
    )
    return centre, edge


def max_point_gap(points: Sequence[Tuple[float, float]]) -> float:
    "Return the longest distance between neighbouring points of a run."

    if len(points) < 2:
        return 0.0
    return max(
        float(
            np.hypot(
                points[index + 1][0] - points[index][0],
                points[index + 1][1] - points[index][1],
            )
        )
        for index in range(len(points) - 1)
    )


def replay_geometry(replay: Any) -> List[Tuple[str, Any]]:
    """Return the ordered keypoint operations a replay recorded.

    ReplayCompose stores, for every transform of the stack, whether it
    was applied and the parameters it sampled: the affine matrix, the
    perspective homography and the shape a flip mirrors the points of.
    Replaying those operations on the original points rebuilds the
    transform of the shape exactly, which is what tells the run of a
    duplicated keypoint grid that belongs to the picture from the copies.

    The operations come back in the order of the stack:
      ("matrix", ndarray) applies a 3x3 matrix,
      ("flip_x", width) mirrors x about the width,
      ("flip_y", height) mirrors y about the height.
    """

    operations: List[Tuple[str, Any]] = []
    stack = [replay]
    while stack:
        node = stack.pop(0)
        if not isinstance(node, dict):
            continue
        params = node.get("params") or {}
        applied = bool(node.get("applied", True))
        name = str(node.get("__class_fullname__") or "")
        shape = params.get("shape")
        if applied:
            matrix = params.get("matrix")
            if matrix is not None:
                operations.append(("matrix", matrix))
            elif name.endswith("HorizontalFlip") and shape is not None:
                operations.append(("flip_x", int(shape[1])))
            elif name.endswith("VerticalFlip") and shape is not None:
                operations.append(("flip_y", int(shape[0])))
        children = node.get("transforms")
        if isinstance(children, (list, tuple)):
            stack.extend(children)
    return operations


def transform_points(
    points: Sequence[Tuple[float, float]], operations: Sequence[Any]
) -> List[Tuple[float, float]]:
    """Apply recorded keypoint operations to a list of points."""

    if not points:
        return []
    array = np.asarray(points, dtype=np.float64).reshape(-1, 1, 2)
    for kind, payload in operations:
        if kind == "matrix":
            matrix = np.asarray(payload, dtype=np.float64)
            if matrix.shape == (2, 3):
                matrix = np.vstack([matrix, [0.0, 0.0, 1.0]])
            array = cv2.perspectiveTransform(array, matrix)
        elif kind == "flip_x":
            array[:, 0, 0] = float(payload) - 1.0 - array[:, 0, 0]
        elif kind == "flip_y":
            array[:, 0, 1] = float(payload) - 1.0 - array[:, 0, 1]
    return [(float(x), float(y)) for x, y in array.reshape(-1, 2)]


def truthful_run(
    runs: Sequence[List[Tuple[float, float]]],
    expected: Sequence[Tuple[float, float]],
    tolerance: float = 1.0,
) -> List[Tuple[float, float]]:
    """Return the run of a duplicated grid that belongs to the picture.

    A transform that hands back a grid of copies of the picture repeats
    every keypoint: the points of one shape come back as whole runs of
    the form      original, copy, copy, ...    and only the run the
    recorded transform really produced belongs to the shape. That run
    lands on the expected points, while every copy is the very same run
    translated by a whole tile of the grid. Measured on albumentations
    2.0.8: the matching run is exact (distance 0) and the closest copy
    sits 128 to 253 pixels away on a 200x100 picture.

    When no run matches the recorded transform the tightest run is the
    answer: a shape the grid did not copy has a single run and nothing
    to choose.
    """

    if not runs:
        return []
    if len(runs) == 1:
        return list(runs[0])
    if expected and len(expected) == len(runs[0]):
        wanted = np.asarray(expected, dtype=np.float64)
        best: Optional[List[Tuple[float, float]]] = None
        best_distance = float("inf")
        for run in runs:
            distance = float(
                np.max(np.abs(np.asarray(run, dtype=np.float64) - wanted))
            )
            if distance < best_distance:
                best_distance = distance
                best = list(run)
        if best is not None and best_distance <= tolerance:
            return best
    return list(min(runs, key=max_point_gap))


def group_keypoints(
    keypoints: Sequence[Any],
    labels: Sequence[Any],
    counts: Optional[Dict[str, int]] = None,
    expected: Optional[Dict[str, Any]] = None,
) -> Dict[str, List[Tuple[float, float]]]:
    """Group replayed keypoints back to their originating shape index.

    counts names the amount of keypoints the shape of a label really
    has and expected the points the recorded transform maps those
    keypoints to: a transform that hands back a grid of copies of every
    keypoint is then reduced to the run that belongs to the picture.
    """

    grouped: Dict[str, List[Tuple[float, float]]] = {}
    for point, label in zip(keypoints, labels):
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        grouped.setdefault(str(label), []).append(
            (float(point[0]), float(point[1]))
        )
    if not counts:
        return grouped
    reduced: Dict[str, List[Tuple[float, float]]] = {}
    for label, points in grouped.items():
        count = counts.get(label, 0)
        if count <= 0 or len(points) <= count or len(points) % count:
            reduced[label] = points
            continue
        runs = [
            points[start : start + count]
            for start in range(0, len(points), count)
        ]
        wanted = (expected or {}).get(label, [])
        reduced[label] = truthful_run(runs, wanted)
    return reduced


def rebuild_shape(
    shape: Dict[str, Any],
    shape_type: str,
    points: Sequence[Tuple[float, float]],
    width: int,
    height: int,
) -> Tuple[List[Tuple[float, float]], bool, bool]:
    """Rebuild one shape keeping its original shape_type.

    Returns the clipped point list plus the (clipped, lost) flags. The
    shape is never dropped and never downgraded to an axis aligned
    rectangle.
    """

    original = shape_points(shape)
    if not points:
        points = list(original)
    if shape_type == "rectangle":
        box = _aabb(points) or _aabb(original)
        if box is None:
            box = (0.0, 0.0, 0.0, 0.0)
        points = [
            (box[0], box[1]),
            (box[2], box[1]),
            (box[2], box[3]),
            (box[0], box[3]),
        ]
    elif shape_type == "polygon":
        if len(points) > 3 and points[0] == points[-1]:
            points = list(points[:-1])
    elif shape_type == "circle":
        if len(points) < 2:
            points = list(original)
        elif len(points) >= 3 and not is_circle(points):
            centre, edge = fit_circle(points)
            points = [centre, edge]
        elif len(points) > 2:
            points = [points[0], points[1]]
    elif shape_type == "point":
        if len(points) > 1:
            points = [points[0]]

    clipped_points, clipped = clip_points(points, width, height)
    if shape_type == "point":
        lost = not clipped_points
    elif shape_type in ("line", "linestrip", "circle"):
        lost = len(clipped_points) < 2
    else:
        lost = polygon_area(clipped_points) <= 0.0
    return clipped_points, clipped, lost


def shape_fits_canvas(shape: Dict[str, Any], width: int, height: int) -> bool:
    """Tell whether one rebuilt shape lies fully inside the canvas.

    Both rules of a fit are checked here: every point the shape is stored
    with has to land inside [0, width - 1] x [0, height - 1] - a point on
    the border belongs to the picture - and the shape itself has to stay
    a shape. A point set that lost points, a polygon collapsed onto a
    line (no area left) or a line collapsed onto a dot is not a fit
    either: such a label would no longer describe what the original
    annotation described.

    A try that needed a single clamp - one point sitting even a hair
    outside the frame - is rejected by the caller as well: the clipped
    flag rebuild_shape returns is the wider test, this predicate is the
    narrower one and both have to hold.
    """

    points = shape_points(shape)
    if not points:
        return False
    max_x = float(max(int(width) - 1, 0))
    max_y = float(max(int(height) - 1, 0))
    for x, y in points:
        if not -CLIP_EPSILON <= float(x) <= max_x + CLIP_EPSILON:
            return False
        if not -CLIP_EPSILON <= float(y) <= max_y + CLIP_EPSILON:
            return False

    shape_type = str(shape.get("shape_type") or "polygon")
    if shape_type == "point":
        return len(points) >= 1
    if shape_type in ("line", "linestrip", "circle"):
        return len(points) >= 2
    # every remaining type is a closed region: a region without area is
    # a degenerate shape, never a fit
    return polygon_area(points) > 0.0


def all_shapes_fit(
    shapes: Sequence[Dict[str, Any]], width: int, height: int
) -> bool:
    """Tell whether every shape of a rebuilt label fits the canvas."""

    return all(shape_fits_canvas(shape, width, height) for shape in shapes)


def resolve_max_attempts(value: Any = None) -> int:
    """Return the attempt budget of one sample, never below one.

    A missing or unreadable value falls back to MAX_ATTEMPTS, so a caller
    that hands over None or a string cannot turn the loop off by accident
    and cannot make it run forever either.
    """

    try:
        count = int(value)
    except (TypeError, ValueError):
        return MAX_ATTEMPTS
    return max(1, count)


def augment_sample(
    image: np.ndarray,
    label: Dict[str, Any],
    params: AugmentParams,
    sample_index: int,
    image_name: str = "aug.png",
    max_attempts: int = MAX_ATTEMPTS,
) -> Optional[AugmentOutcome]:
    """Augment one image, retrying until every shape fits the canvas.

    The first try uses the seed of the sample itself; a try that leaves a
    shape outside the picture is repeated with the next derived seed of
    the very same (base seed, sample index) pair, at most max_attempts
    times. None comes back when no try fitted: the sample is then dropped
    by the caller instead of being produced with a clipped or a vanished
    defect. Every seed is a function of the pair and the attempt number
    alone, therefore the result of a sample is the same whatever the
    number of threads and whatever the order the samples finish in.

    A sample whose shapes fit already on the first try is byte identical
    with what the tool produced before the retry loop existed: attempt 0
    derives the very seed the sample always had
    (derive_seed(seed, sample_index)), while every retry uses the seed of
    its own attempt number.
    """

    attempts = resolve_max_attempts(max_attempts)
    for attempt in range(attempts):
        seed = attempt_seed(params.seed, sample_index, attempt)
        outcome, clipped = _augment_attempt(
            image, label, params, seed, image_name, attempt
        )
        if not clipped and all_shapes_fit(
            outcome.label["shapes"], outcome.width, outcome.height
        ):
            return outcome
    LOGGER.debug(
        "dropped sample %s: no attempt of %d kept every shape inside the "
        "%dx%d canvas",
        image_name,
        attempts,
        int(image.shape[1]),
        int(image.shape[0]),
    )
    return None


def _augment_attempt(
    image: np.ndarray,
    label: Dict[str, Any],
    params: AugmentParams,
    seed: int,
    image_name: str,
    attempt: int,
) -> Tuple[AugmentOutcome, bool]:
    """Run one augmentation of one image with the given seed.

    Returns the outcome plus the flag telling whether rebuilding the
    label needed a single clamp: a clamped point means the transform
    pushed that point out of the picture, which is the wider test of a
    fit (the narrow one is shape_fits_canvas).
    """

    height, width = int(image.shape[0]), int(image.shape[1])
    shapes = list(label.get("shapes") or [])

    # A gray picture is augmented in RGB and collapsed back afterwards:
    # albumentations refuses hue and saturation shifts on a single
    # channel image (and warns about them), while the very same stack on
    # the promoted copy is a plain pixel operation.
    grayscale = is_grayscale_content(image)
    working_image = promote_to_bgr(image) if grayscale else image

    keypoints: List[Any] = []
    kp_labels: List[str] = []
    kp_counts: Dict[str, int] = {}
    # the very points every shape contributes, kept in the order they are
    # handed to the stack: the replay turns them into the expected points
    shape_points_by_index: Dict[str, List[Tuple[float, float]]] = {}
    for index, shape in enumerate(shapes):
        shape_type = str(shape.get("shape_type") or "polygon")
        points = shape_points(shape)
        if shape_type == "circle" and len(points) > 2:
            points = circle_probe_points(points[0], points[1])
        kp_counts[str(index)] = len(points)
        shape_points_by_index[str(index)] = [
            (float(point[0]), float(point[1])) for point in points
        ]
        for point in points:
            keypoints.append((float(point[0]), float(point[1])))
            kp_labels.append(str(index))

    compose = build_replay_compose(params, black_fill_for(working_image))
    compose.set_random_seed(int(seed))

    payload: Dict[str, Any] = {
        "image": working_image,
        "keypoints": keypoints,
        "kp_labels": kp_labels,
    }

    # A single call already applies one random decision to the image and
    # to every keypoint, replaying it again would only repeat it.
    result = compose(**payload)

    augmented_image = result["image"]
    if params.bgr > 0 and compose.py_random.random() < params.bgr:
        augmented_image = np.ascontiguousarray(augmented_image[:, :, ::-1])
    if grayscale:
        # the channel swap above is an identity on gray content, the
        # collapse below is what gives the dataset single channel copies
        augmented_image = collapse_to_grayscale(augmented_image)

    # The replay records the transform it really applied to the picture,
    # so the points every shape ends up with are known exactly and a
    # duplicated run of a keypoint grid is identified by them.
    grouped = group_keypoints(
        result["keypoints"],
        result.get("kp_labels", kp_labels),
        kp_counts,
        {
            str(index): transform_points(
                points, replay_geometry(result.get("replay"))
            )
            for index, points in shape_points_by_index.items()
        },
    )

    new_shapes: List[Dict[str, Any]] = []
    clipped_report: List[Dict[str, Any]] = []
    type_counts: Dict[str, int] = {}
    any_clipped = False
    for index, shape in enumerate(shapes):
        shape_type = str(shape.get("shape_type") or "polygon")
        rebuilt, clipped, lost = rebuild_shape(
            shape,
            shape_type,
            grouped.get(str(index), []),
            width,
            height,
        )
        type_counts[shape_type] = type_counts.get(shape_type, 0) + 1
        new_shape = dict(shape)
        new_shape["shape_type"] = shape_type
        new_shape["points"] = [[float(x), float(y)] for x, y in rebuilt]
        if shape_type == "rotation" and len(rebuilt) >= 2:
            new_shape["direction"] = rotation_direction(rebuilt)
        new_shapes.append(new_shape)
        if clipped:
            any_clipped = True
        if clipped or lost:
            clipped_report.append(
                ClipReport(
                    shape_index=index,
                    label=str(shape.get("label", "")),
                    shape_type=shape_type,
                    clipped=clipped,
                    lost=lost,
                ).to_dict()
            )

    new_label = dict(label)
    new_label["shapes"] = new_shapes
    new_label["imagePath"] = image_name
    new_label["imageHeight"] = height
    new_label["imageWidth"] = width

    outcome = AugmentOutcome(
        image=augmented_image,
        label=new_label,
        width=width,
        height=height,
        clipped=clipped_report,
        type_counts=type_counts,
        grayscale=grayscale,
        attempts=int(attempt) + 1,
        attempt=int(attempt),
        seed=int(seed),
    )
    return outcome, any_clipped


def encode_image(image: np.ndarray, source_ext: str):
    """Encode an image following the source format.

    Returns the encoded bytes, the output extension and whether the
    encoder fell back to PNG.
    """

    ext = (source_ext or "").lower()
    target, flag, value = IMAGE_ENCODING.get(
        ext, (ENCODE_FALLBACK_EXT, None, None)
    )
    fallback = ext not in IMAGE_ENCODING
    params: List[int] = []
    if flag is not None and value is not None:
        params = [int(flag), int(value)]
    ok, buffer = cv2.imencode(target, image, params)
    if not ok:
        fallback = True
        target = ENCODE_FALLBACK_EXT
        ok, buffer = cv2.imencode(target, image)
    if not ok:
        raise RuntimeError("cv2.imencode failed for every format")
    return bytes(buffer.tobytes()), target, fallback


def decode_image(path: str) -> Optional[np.ndarray]:
    """Decode an image from disk using a path safe reader.

    The file is decoded as it is stored, so a single channel picture
    stays single channel: is_grayscale_content then sees the very shape
    PIL reports for that file instead of a silently promoted copy. A
    non 8 bit image or an image carrying an alpha channel falls back to
    the three channel reader every consumer of the module expects, so
    the bytes handed to the stack are the ones it always received.
    """

    if not path or not osp.isfile(path):
        return None
    try:
        buffer = np.fromfile(path, dtype=np.uint8)
        image = cv2.imdecode(buffer, cv2.IMREAD_UNCHANGED)
        if image is not None and (
            image.dtype != np.uint8
            or (
                image.ndim == 3
                and int(image.shape[2]) not in (1, BGR_CHANNELS)
            )
        ):
            image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    except (OSError, ValueError, cv2.error):
        return None
    return image


def augmented_relpath(relpath: str, index: int, ext: str) -> str:
    """Return the augmented relpath mirroring the original folder."""

    folder = osp.dirname(relpath.replace("/", os.sep))
    stem = osp.splitext(osp.basename(relpath))[0]
    name = f"{stem}_aug{index}{ext}"
    if folder:
        return folder.replace(os.sep, "/") + "/" + name
    return name


__all__ = [
    "BGR_CHANNELS",
    "BLACK_FILL",
    "BLACK_FILL_RGB",
    "CLIP_EPSILON",
    "ENCODE_FALLBACK_EXT",
    "GRAYSCALE_DIMENSIONS",
    "GRAYSCALE_PATH_NOTE",
    "IMAGE_ENCODING",
    "JPEG_QUALITY",
    "MAX_ATTEMPTS",
    "WEBP_QUALITY",
    "AugmentOutcome",
    "ClipReport",
    "all_shapes_fit",
    "augment_sample",
    "collapse_to_grayscale",
    "is_grayscale_content",
    "promote_to_bgr",
    "augmented_relpath",
    "black_fill_for",
    "black_fill_tuple",
    "build_replay_compose",
    "build_transforms",
    "circle_probe_points",
    "clip_points",
    "decode_image",
    "encode_image",
    "fit_circle",
    "group_keypoints",
    "image_channels",
    "is_circle",
    "max_point_gap",
    "polygon_area",
    "replay_geometry",
    "resolve_max_attempts",
    "transform_points",
    "truthful_run",
    "rebuild_shape",
    "rotation_direction",
    "shape_fits_canvas",
    "shape_points",
]
