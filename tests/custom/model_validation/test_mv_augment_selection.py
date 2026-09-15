"""The fields one try draws, and the stack that draw builds.

AugmentParams carries one enable bit per flip and a single select_prob:
every try of a sample first draws the set of fields it applies - the
five parameters that carry an amplitude plus the two flips whose bit is
checked, each candidate firing independently with select_prob - and the
transform stack is then built from that very set. These tests pin the
draw down (the candidate set, the independence of an unchecked flip,
the probability 1 and 0 short circuits, the set an outcome records) and
they pin down the stack it builds, including the byte identical
identity a zero contrast is.
"""

import importlib.util
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from anylabeling.custom.model_validation.app_config import (
    AUGMENT_SELECTABLE_BASE,
    AugmentParams,
    attempt_seed,
    draw_selection,
    selectable_fields,
)
from anylabeling.custom.model_validation.augment import (
    augment_sample,
    black_fill_for,
    build_replay_compose,
    build_transforms,
)

HAS_ALBUMENTATIONS = importlib.util.find_spec("albumentations") is not None
requires_albumentations = pytest.mark.skipif(
    not HAS_ALBUMENTATIONS,
    reason="the optional albumentations backend is not installed",
)


def image_200x100() -> np.ndarray:
    "Return a deterministic three channel canvas."

    image = np.zeros((100, 200, 3), dtype=np.uint8)
    ramp = np.linspace(0, 255, 200, dtype=np.uint8)
    image[:, :, 1] = ramp.reshape(1, -1)
    image[:, :, 2] = 90
    return image


def empty_label() -> dict:
    "Return a label without a shape: every try of it fits by definition."

    return {
        "imagePath": "a.png",
        "imageData": None,
        "imageHeight": 100,
        "imageWidth": 200,
        "shapes": [],
    }


def transform_of(transforms, kind):
    "Return the single transform of the stack that is of that class."

    found = [item for item in transforms if isinstance(item, kind)]
    assert len(found) == 1, kind.__name__
    return found[0]


# -------------------------------------------------------- the candidate set
def test_the_candidate_set_grows_with_the_two_enable_bits():
    "Seven candidates with both flips checked, five with neither."

    both = AugmentParams(flipud=True, fliplr=True)
    one = AugmentParams(flipud=True, fliplr=False)
    other = AugmentParams(flipud=False, fliplr=True)
    neither = AugmentParams(flipud=False, fliplr=False)

    assert selectable_fields(both) == AUGMENT_SELECTABLE_BASE + (
        "flipud",
        "fliplr",
    )
    assert selectable_fields(one) == AUGMENT_SELECTABLE_BASE + ("flipud",)
    assert selectable_fields(other) == AUGMENT_SELECTABLE_BASE + ("fliplr",)
    assert selectable_fields(neither) == AUGMENT_SELECTABLE_BASE
    assert len(selectable_fields(both)) == 7
    assert len(selectable_fields(one)) == 6
    assert len(selectable_fields(neither)) == 5


def test_an_unchecked_flip_is_no_candidate_at_all():
    "A disabled flip is never drawn and never keeps a draw alive."

    params = AugmentParams(
        flipud=False, fliplr=False, select_prob=0.5, seed=5
    )
    for sample_index in range(20):
        for attempt in range(5):
            seed = attempt_seed(params.seed, sample_index, attempt)
            chosen = draw_selection(params, seed)
            assert set(chosen) <= set(AUGMENT_SELECTABLE_BASE)
            assert "flipud" not in chosen
            assert "fliplr" not in chosen
    # a checked flip is not added to an empty draw for free either: the
    # five amplitude fields carry the "at least one field" judgement of
    # the caller on their own, and they really can come back empty
    checked = AugmentParams(select_prob=0.5, seed=120)
    assert selectable_fields(checked) == AUGMENT_SELECTABLE_BASE + (
        "flipud",
        "fliplr",
    )
    assert draw_selection(checked, attempt_seed(120, 0, 0)) == ()


# --------------------------------------------------------------- the stack
@requires_albumentations
def test_a_drawn_flip_fires_and_an_undrawn_one_stays_off():
    "The two whole picture mirrors follow the drawn set, not a chance."

    import albumentations as albu

    # The two draws below are pinned down for their concrete seeds; both
    # were found by scanning the seeds, and no assertion here is a
    # statistic.
    vertical = AugmentParams(select_prob=0.5, seed=6)
    vertical_chosen = draw_selection(vertical, attempt_seed(6, 0, 0))
    assert vertical_chosen == ("contrast", "hsv_v", "scale", "flipud")
    horizontal = AugmentParams(select_prob=0.5, seed=0)
    horizontal_chosen = draw_selection(horizontal, attempt_seed(0, 0, 0))
    assert horizontal_chosen == (
        "contrast",
        "hsv_v",
        "degrees",
        "translate",
        "scale",
        "fliplr",
    )

    stack = build_transforms(vertical, selection=vertical_chosen)
    assert transform_of(stack, albu.VerticalFlip).p == 1.0
    assert transform_of(stack, albu.HorizontalFlip).p == 0.0

    stack = build_transforms(horizontal, selection=horizontal_chosen)
    assert transform_of(stack, albu.VerticalFlip).p == 0.0
    assert transform_of(stack, albu.HorizontalFlip).p == 1.0

    # nothing was drawn: neither mirror fires, whatever the bits say
    stack = build_transforms(vertical, selection=())
    assert transform_of(stack, albu.VerticalFlip).p == 0.0
    assert transform_of(stack, albu.HorizontalFlip).p == 0.0


def test_the_shipped_selection_probability_is_one_tenth():
    "The shipped configuration draws one candidate out of ten on average."

    assert AugmentParams().select_prob == 0.1
    assert AugmentParams().contrast == 0.2


@requires_albumentations
def test_a_zero_contrast_is_byte_identical():
    "Contrast 0 makes the gain alpha == 1.0, so nothing changes."

    import albumentations as albu

    params = AugmentParams(
        contrast=0.0,
        hsv_v=0.0,
        degrees=0.0,
        translate=0.0,
        scale_min=1.0,
        scale_max=1.0,
        flipud=False,
        fliplr=False,
        select_prob=1.0,
        seed=3,
    )
    image = image_200x100()
    outcome = augment_sample(image, empty_label(), params, 0)
    assert outcome is not None
    assert np.array_equal(outcome.image, image)

    # the very same stack with the RandomBrightnessContrast call removed
    # hands back the very same array for the very same seed: the zero
    # contrast is an identity and not a rounding of its own
    active = AugmentParams(
        contrast=0.0,
        hsv_v=0.4,
        degrees=12.0,
        translate=0.1,
        scale_min=0.9,
        scale_max=1.1,
        flipud=True,
        fliplr=False,
        select_prob=1.0,
        seed=8,
    )
    outcome = augment_sample(image, empty_label(), active, 0)
    assert outcome is not None
    reduced = [
        item
        for item in build_transforms(active)
        if not isinstance(item, albu.RandomBrightnessContrast)
    ]
    compose = albu.ReplayCompose(
        reduced,
        keypoint_params=albu.KeypointParams(
            format="xy",
            label_fields=["kp_labels"],
            remove_invisible=False,
        ),
    )
    compose.set_random_seed(attempt_seed(active.seed, 0, outcome.attempt))
    replayed = compose(image=image, keypoints=[], kp_labels=[])
    assert np.array_equal(replayed["image"], outcome.image)


# ------------------------------------------------------- the drawn set
@requires_albumentations
def test_the_outcome_records_the_set_its_own_try_drew():
    "outcome.selection is the draw of (base seed, sample, attempt)."

    params = AugmentParams(
        contrast=0.0,
        hsv_v=0.4,
        degrees=12.0,
        translate=0.1,
        scale_min=0.9,
        scale_max=1.1,
        flipud=True,
        fliplr=True,
        select_prob=0.5,
        seed=13,
    )
    outcome = augment_sample(image_200x100(), empty_label(), params, 4)
    assert outcome is not None
    assert outcome.selection
    assert set(outcome.selection) <= set(selectable_fields(params))
    assert outcome.selection == draw_selection(
        params, attempt_seed(params.seed, 4, outcome.attempt)
    )


@requires_albumentations
def test_a_full_draw_is_the_same_as_no_selection_at_all():
    "A probability of one selects the whole set and consumes no number."

    params = AugmentParams(
        contrast=0.0,
        hsv_v=0.3,
        degrees=12.0,
        translate=0.1,
        scale_min=0.9,
        scale_max=1.1,
        flipud=True,
        fliplr=True,
        select_prob=1.0,
        seed=11,
    )
    assert draw_selection(params, 123456) == selectable_fields(params)

    image = image_200x100()
    outcome = augment_sample(image, empty_label(), params, 2)
    assert outcome is not None
    assert outcome.selection == selectable_fields(params)

    # a stack a caller builds without a selection is the candidate set
    # itself, so the two runs are byte identical
    compose = build_replay_compose(params, black_fill_for(image), None)
    compose.set_random_seed(attempt_seed(params.seed, 2, outcome.attempt))
    replayed = compose(image=image, keypoints=[], kp_labels=[])
    assert np.array_equal(replayed["image"], outcome.image)
