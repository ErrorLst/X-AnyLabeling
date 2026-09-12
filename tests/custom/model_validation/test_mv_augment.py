"""Albumentations augmentation tests."""

import importlib.util
import json
import math
import os
import os.path as osp

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from anylabeling.custom.model_validation.app_config import AugmentParams
from anylabeling.custom.model_validation.augment import (
    augment_sample,
    augmented_relpath,
    build_replay_compose,
    build_transforms,
    clip_points,
    is_circle,
    polygon_area,
    rotation_direction,
)

CLASSES = ["person", "car"]

# The albumentations backend is optional: only the tests that really
# need it are skipped, the geometry helpers always run.
HAS_ALBUMENTATIONS = importlib.util.find_spec("albumentations") is not None
ALBUMENTATIONS_SKIP_REASON = (
    "the optional albumentations backend is not installed"
)
requires_albumentations = pytest.mark.skipif(
    not HAS_ALBUMENTATIONS, reason=ALBUMENTATIONS_SKIP_REASON
)


def base_image(width: int = 200, height: int = 100) -> np.ndarray:
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:, :, 1] = np.linspace(0, 255, width, dtype=np.uint8).reshape(1, -1)
    image[:, :, 2] = 90
    return image


def base_label() -> dict:
    return {
        "version": "3.0.0",
        "flags": {"reviewed": True},
        "checked": True,
        "description": "keep me",
        "custom_field": {"nested": [1, 2, 3]},
        "imagePath": "original.png",
        "imageData": None,
        "imageHeight": 100,
        "imageWidth": 200,
        "shapes": [
            {
                "label": "car",
                "score": None,
                "points": [[10, 10], [60, 12], [55, 70], [12, 65]],
                "group_id": 1,
                "description": "poly",
                "difficult": False,
                "shape_type": "polygon",
                "flags": {},
                "attributes": {},
                "kie_linking": [],
            },
            {
                "label": "person",
                "score": None,
                "points": [[100, 20], [150, 20], [150, 80], [100, 80]],
                "group_id": None,
                "description": "",
                "difficult": False,
                "shape_type": "rectangle",
                "flags": {},
                "attributes": {},
                "kie_linking": [],
            },
            {
                "label": "person",
                "score": None,
                "points": [[20, 20], [60, 10], [70, 40], [30, 50]],
                "group_id": None,
                "description": "",
                "difficult": False,
                "shape_type": "rotation",
                "direction": 12.5,
                "flags": {},
                "attributes": {},
                "kie_linking": [],
            },
            {
                "label": "car",
                "score": None,
                "points": [[150, 10], [190, 30], [170, 60], [140, 40]],
                "group_id": None,
                "description": "",
                "difficult": False,
                "shape_type": "quadrilateral",
                "flags": {},
                "attributes": {},
                "kie_linking": [],
            },
            {
                "label": "person",
                "score": None,
                "points": [[180, 40]],
                "group_id": None,
                "description": "",
                "difficult": False,
                "shape_type": "point",
                "flags": {},
                "attributes": {},
                "kie_linking": [],
            },
            {
                "label": "car",
                "score": None,
                "points": [[5, 90], [190, 95]],
                "group_id": None,
                "description": "",
                "difficult": False,
                "shape_type": "line",
                "flags": {},
                "attributes": {},
                "kie_linking": [],
            },
            {
                "label": "car",
                "score": None,
                "points": [[30, 80], [40, 85], [50, 80]],
                "group_id": None,
                "description": "",
                "difficult": False,
                "shape_type": "linestrip",
                "flags": {},
                "attributes": {},
                "kie_linking": [],
            },
            {
                "label": "person",
                "score": None,
                "points": [[90, 45], [110, 45]],
                "group_id": None,
                "description": "",
                "difficult": False,
                "shape_type": "circle",
                "flags": {},
                "attributes": {},
                "kie_linking": [],
            },
            {
                "label": "car",
                "score": None,
                "points": [
                    [120, 5],
                    [170, 5],
                    [170, 30],
                    [120, 30],
                    [130, 15],
                    [180, 15],
                    [180, 40],
                    [130, 40],
                ],
                "group_id": None,
                "description": "",
                "difficult": False,
                "shape_type": "cuboid",
                "flags": {},
                "attributes": {},
                "kie_linking": [],
                "cuboid3d": {
                    "mode": "from_rectangle",
                    "source": "manual",
                    "depth_vector": [10.0, 10.0],
                },
            },
        ],
    }


def compact_label() -> dict:
    """Return the label of every shape type, kept inside the frame.

    The shipped test transforms are strong (a rotation of 25 degrees, a
    zoom of 0.7 to 1.3, a shear of 8 degrees and a translation of 0.15),
    so a shape hugging a border would leave the picture: augment_sample
    then retries the sample and never hands back a clipped label. These
    shapes are laid out around the middle of a 200x100 canvas and stay
    small enough that every one of them survives the transform, which is
    what makes the count and type assertions below meaningful for every
    seed the tests use.

    The two derived types keep the layout of their original: a rotation
    is a quad and a cuboid is the eight points of the projected box.
    """

    def shape(label: str, shape_type: str, points) -> dict:
        entry = {
            "label": label,
            "score": None,
            "points": points,
            "group_id": None,
            "description": "",
            "difficult": False,
            "shape_type": shape_type,
            "flags": {},
            "attributes": {},
            "kie_linking": [],
        }
        if shape_type == "cuboid":
            entry["cuboid3d"] = {
                "mode": "from_rectangle",
                "source": "manual",
                "depth_vector": [10.0, 10.0],
            }
        return entry

    return {
        "version": "3.0.0",
        "flags": {"reviewed": True},
        "checked": True,
        "description": "keep me",
        "custom_field": {"nested": [1, 2, 3]},
        "imagePath": "original.png",
        "imageData": None,
        "imageHeight": 100,
        "imageWidth": 200,
        "shapes": [
            shape("car", "polygon", [[58, 32], [86, 30], [82, 58], [60, 62]]),
            shape(
                "person",
                "rectangle",
                [[98, 36], [118, 36], [118, 60], [98, 60]],
            ),
            shape(
                "person", "rotation", [[72, 44], [98, 34], [114, 52], [88, 62]]
            ),
            shape(
                "car",
                "quadrilateral",
                [[126, 38], [142, 44], [134, 60], [122, 50]],
            ),
            shape("person", "point", [[62, 40]]),
            shape("car", "line", [[68, 58], [104, 64]]),
            shape("car", "linestrip", [[70, 52], [80, 58], [90, 50]]),
            shape("person", "circle", [[90, 48], [97, 48]]),
            shape(
                "car",
                "cuboid",
                [
                    [92, 42],
                    [108, 36],
                    [114, 48],
                    [98, 54],
                    [96, 32],
                    [112, 26],
                    [118, 38],
                    [102, 44],
                ],
            ),
        ],
    }


def shifted_params(seed: int = 6) -> AugmentParams:
    """Return a forced translation that leaves the colour untouched.

    The shift alone is enough to push a box out of the picture, which is
    the geometry the retry loop is asserted on: no rotation, no zoom and
    no shear, so only the sampled direction of the shift decides.
    """

    return AugmentParams(
        hsv_h=0.0,
        hsv_s=0.0,
        hsv_v=0.0,
        degrees=0.0,
        translate=0.6,
        scale_min=1.0,
        scale_max=1.0,
        shear=0.0,
        perspective=0.0,
        flipud=0.0,
        fliplr=0.0,
        bgr=0.0,
        seed=seed,
    )


def centred_box_label(width: int = 120, height: int = 80) -> dict:
    """Return one rectangle around the middle of a small canvas.

    Used by the extreme parameter test: a box whose centre is the centre
    of the picture survives even a 180 degree rotation, a 45 degree
    shear and a ten times zoom, while a box hugging the border does not.
    """

    centre_x = float(width) / 2.0
    centre_y = float(height) / 2.0
    half_x, half_y = 20.0, 12.0
    return {
        "imagePath": "small.png",
        "imageData": None,
        "imageHeight": height,
        "imageWidth": width,
        "shapes": [
            {
                "label": "car",
                "score": None,
                "points": [
                    [centre_x - half_x, centre_y - half_y],
                    [centre_x + half_x, centre_y - half_y],
                    [centre_x + half_x, centre_y + half_y],
                    [centre_x - half_x, centre_y + half_y],
                ],
                "group_id": None,
                "description": "",
                "difficult": False,
                "shape_type": "rectangle",
                "flags": {},
                "attributes": {},
                "kie_linking": [],
            }
        ],
    }


def strong_params(**overrides) -> AugmentParams:
    data = {
        "degrees": 25.0,
        "translate": 0.15,
        "scale_min": 0.7,
        "scale_max": 1.3,
        "shear": 8.0,
        "perspective": 0.0008,
        "flipud": 0.5,
        "fliplr": 0.5,
        "bgr": 0.0,
        "seed": 2024,
    }
    data.update(overrides)
    return AugmentParams(**data)


@requires_albumentations
def test_same_seed_is_reproducible():
    image = base_image()
    label = base_label()
    first = augment_sample(image, label, strong_params(), 7)
    second = augment_sample(image, label, strong_params(), 7)
    assert np.array_equal(first.image, second.image)
    assert first.label == second.label


@requires_albumentations
def test_different_seed_changes_the_result():
    image = base_image()
    label = compact_label()
    first = augment_sample(image, label, strong_params(), 0)
    second = augment_sample(image, label, strong_params(), 1)
    assert first is not None and second is not None
    assert first.label != second.label or not np.array_equal(
        first.image, second.image
    )


@requires_albumentations
def test_shape_types_and_counts_are_preserved():
    image = base_image()
    label = compact_label()
    outcome = augment_sample(image, label, strong_params(), 3)
    original_types = [s["shape_type"] for s in label["shapes"]]
    augmented_types = [s["shape_type"] for s in outcome.label["shapes"]]
    assert augmented_types == original_types
    assert "rectangle" not in set(augmented_types) - {"rectangle"}
    assert len(outcome.label["shapes"]) == len(label["shapes"])
    assert outcome.type_counts["polygon"] == 1
    assert outcome.type_counts["rectangle"] == 1
    assert outcome.type_counts["rotation"] == 1
    assert outcome.type_counts["quadrilateral"] == 1
    assert outcome.type_counts["point"] == 1
    assert outcome.type_counts["line"] == 1
    assert outcome.type_counts["linestrip"] == 1
    assert outcome.type_counts["circle"] == 1
    assert outcome.type_counts["cuboid"] == 1


@requires_albumentations
def test_no_axis_aligned_downgrade():
    image = base_image()
    label = compact_label()
    outcome = augment_sample(image, label, strong_params(), 11)
    for original, augmented in zip(label["shapes"], outcome.label["shapes"]):
        assert augmented["shape_type"] == original["shape_type"]
        if original["shape_type"] == "rectangle":
            assert len(augmented["points"]) >= 4
        else:
            assert len(augmented["points"]) == len(original["points"])


def test_rotation_direction_is_radians_within_one_turn():
    quad = [(10.0, 10.0), (60.0, 12.0), (55.0, 70.0), (12.0, 65.0)]
    direction = rotation_direction(quad)
    expected = math.atan2(12.0 - 10.0, 60.0 - 10.0) % (2 * math.pi)
    assert 0.0 <= direction < 2 * math.pi
    assert direction == pytest.approx(expected, abs=1e-9)
    assert direction != pytest.approx(math.degrees(expected))
    assert rotation_direction([(0.0, 0.0), (0.0, 5.0)]) == pytest.approx(
        math.pi / 2
    )
    assert rotation_direction([(7.0, 7.0)]) == 0.0


@requires_albumentations
def test_rotation_direction_is_recomputed():
    image = base_image()
    label = compact_label()
    outcome = augment_sample(image, label, strong_params(), 5)
    rotation = outcome.label["shapes"][2]
    assert rotation["shape_type"] == "rotation"
    assert "direction" in rotation
    expected = rotation_direction([tuple(p) for p in rotation["points"]])
    assert abs(float(rotation["direction"]) - expected) < 1e-6


@requires_albumentations
def test_label_template_is_cloned():
    image = base_image()
    label = base_label()
    outcome = augment_sample(
        image, label, strong_params(), 2, image_name="a_aug1.png"
    )
    result = outcome.label
    assert result["version"] == label["version"]
    assert result["flags"] == label["flags"]
    assert result["checked"] is True
    assert result["description"] == "keep me"
    assert result["custom_field"] == label["custom_field"]
    assert result["imagePath"] == "a_aug1.png"
    assert result["imageHeight"] == outcome.height
    assert result["imageWidth"] == outcome.width
    assert result["shapes"][0]["group_id"] == 1
    assert result["shapes"][0]["description"] == "poly"
    assert result["shapes"][0]["score"] is None
    assert result["shapes"][0]["difficult"] is False
    assert result["shapes"][0]["kie_linking"] == []
    assert label["imagePath"] == "original.png"


@requires_albumentations
def test_augmented_image_keeps_the_source_size():
    image = base_image(160, 90)
    outcome = augment_sample(image, compact_label(), strong_params(), 1)
    assert outcome is not None
    assert outcome.image.shape == image.shape
    assert outcome.width == 160
    assert outcome.height == 90


@requires_albumentations
def test_flip_probability_one_and_zero():
    image = base_image()
    label = base_label()
    params = AugmentParams(
        degrees=0.0,
        translate=0.0,
        scale_min=1.0,
        scale_max=1.0,
        shear=0.0,
        perspective=0.0,
        hsv_h=0.0,
        hsv_s=0.0,
        hsv_v=0.0,
        flipud=0.0,
        fliplr=1.0,
        seed=99,
    )
    flipped = augment_sample(image, label, params, 0)
    assert np.array_equal(flipped.image, image[:, ::-1])
    identity = AugmentParams(
        degrees=0.0,
        translate=0.0,
        scale_min=1.0,
        scale_max=1.0,
        shear=0.0,
        perspective=0.0,
        hsv_h=0.0,
        hsv_s=0.0,
        hsv_v=0.0,
        flipud=0.0,
        fliplr=0.0,
        seed=99,
    )
    same = augment_sample(image, label, identity, 0)
    assert np.array_equal(same.image, image)


@requires_albumentations
def test_extreme_parameters_do_not_crash():
    """No parameter set crashes the stack.

    The geometry may not survive every parameter set: a try that leaves a
    shape outside the frame is retried and a sample no try could fit is
    dropped - that is the contract of the retry loop - so the assertion
    is that a produced sample keeps every shape of the original.
    """

    image = base_image(120, 80)
    label = centred_box_label(120, 80)
    for overrides in (
        {"degrees": 180.0},
        {"shear": 45.0},
        {"perspective": 0.001},
        {"translate": 0.5},
        {"scale_min": 0.1, "scale_max": 3.0},
    ):
        params = strong_params(**overrides)
        outcome = augment_sample(image, label, params, 4)
        if outcome is None:
            continue
        assert len(outcome.label["shapes"]) == len(label["shapes"])
        shape = outcome.label["shapes"][0]
        assert shape["shape_type"] == "rectangle"
        for x, y in shape["points"]:
            assert 0 <= x <= 119
            assert 0 <= y <= 79


@requires_albumentations
def test_a_box_the_shift_pushed_out_is_retried_and_then_produced():
    """A try that leaves the picture is repeated with a fresh seed.

    Twelve samples of the very same box over the forced shift of 0.6: no
    first try fits, because the box starts 30 pixels away from every
    border and the shift is drawn from a much wider range, while the
    retry loop saves some of the samples outright. Every produced sample
    is complete and carries no clip report at all.
    """

    image = base_image(200, 100)
    slack = 30
    wide_box = {
        "shapes": [
            {
                "label": "car",
                "shape_type": "rectangle",
                "points": [
                    [slack, slack],
                    [199 - slack, slack],
                    [199 - slack, 99 - slack],
                    [slack, 99 - slack],
                ],
            }
        ]
    }
    params = shifted_params()
    produced = []
    for sample_index in range(12):
        outcome = augment_sample(image, wide_box, params, sample_index)
        if outcome is None:
            continue
        produced.append(outcome)
        shape = outcome.label["shapes"][0]
        assert shape["shape_type"] == "rectangle"
        for x, y in shape["points"]:
            assert 0 <= x <= 199
            assert 0 <= y <= 99
        # a produced sample never carries a clip report any more
        assert outcome.clipped == []

    assert produced, "the retry loop has to fit this box somewhere"
    assert all(outcome.attempt > 0 for outcome in produced)
    assert all(outcome.attempts > 1 for outcome in produced)


@requires_albumentations
def test_a_shape_that_never_fits_is_dropped():
    """No seed of an unfittable sample produces anything.

    The box below covers the whole frame, so the very first shift of the
    transform pushes it out of the canvas and no other seed can help: the
    sample comes back as None, which is what makes the stage write no
    file and no record for it.
    """

    image = base_image(200, 100)
    full_frame = {
        "shapes": [
            {
                "label": "car",
                "shape_type": "rectangle",
                "points": [[0, 0], [199, 0], [199, 99], [0, 99]],
            }
        ]
    }
    params = shifted_params()
    for sample_index in range(12):
        assert augment_sample(image, full_frame, params, sample_index) is None


def test_clip_points_helper():
    points, clipped = clip_points([(-5.0, 3.0), (10.0, 3.0)], 10, 10)
    assert clipped is True
    assert points == [(0.0, 3.0), (9.0, 3.0)]
    points, clipped = clip_points([(1.0, 1.0)], 10, 10)
    assert clipped is False


def test_polygon_area_and_circle_helpers():
    assert polygon_area([(0, 0), (10, 0), (10, 10), (0, 10)]) == 100.0
    assert polygon_area([(0, 0), (1, 1)]) == 0.0
    ring = [
        (10.0, 0.0),
        (3.0901699437494745, 9.510565162951535),
        (-8.090169943749475, 5.877852522924732),
        (-8.090169943749475, -5.877852522924732),
        (3.0901699437494745, -9.510565162951535),
    ]
    assert is_circle(ring)
    assert not is_circle([(0, 0), (10, 0), (0, 1)])


def test_augmented_relpath_mirrors_folders():
    assert augmented_relpath("a.png", 1, ".png") == "a_aug1.png"
    assert augmented_relpath("train/a.png", 2, ".jpg") == "train/a_aug2.jpg"


@requires_albumentations
def test_transforms_follow_the_official_mapping():
    params = AugmentParams(
        hsv_h=0.02,
        hsv_s=0.5,
        hsv_v=0.3,
        degrees=10.0,
        translate=0.2,
        scale_min=0.4,
        scale_max=1.6,
        shear=5.0,
        perspective=0.0005,
        flipud=0.25,
        fliplr=0.75,
        seed=1,
    )
    transforms = build_transforms(params)
    hue = transforms[0]
    assert hue.hue_shift_limit == pytest.approx((-3.6, 3.6))
    assert hue.sat_shift_limit == pytest.approx((-50.0, 50.0))
    assert hue.val_shift_limit == pytest.approx((-30.0, 30.0))
    affine = transforms[1]
    assert affine.rotate == pytest.approx((-10.0, 10.0))
    assert affine.scale == {
        "x": pytest.approx((0.4, 1.6)),
        "y": pytest.approx((0.4, 1.6)),
    }
    assert transforms[3].p == 0.25
    assert transforms[4].p == 0.75
    compose = build_replay_compose(params)
    assert compose is not None


@requires_albumentations
def test_json_serialisable_label():
    image = base_image()
    outcome = augment_sample(image, compact_label(), strong_params(), 6)
    text = json.dumps(outcome.label)
    restored = json.loads(text)
    assert restored["shapes"][0]["shape_type"] == "polygon"
