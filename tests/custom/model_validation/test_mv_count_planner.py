"""Augment count planning tests."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from anylabeling.custom.model_validation.app_config import (
    COUNT_MODE,
    MULTIPLIER_MODE,
    RATIO_MODE,
    ValidationConfigError,
    make_rng,
    plan_aug_counts,
)

RATIOS = (0, 0.25, 0.5, 0.75, 1)


def test_count_mode_distributes_remainder_first():
    plan = plan_aug_counts(3, COUNT_MODE, total=8)
    assert plan == [3, 3, 2]
    assert sum(plan) == 8


def test_count_mode_exact_multiple():
    assert plan_aug_counts(4, COUNT_MODE, total=8) == [2, 2, 2, 2]


def test_count_mode_zero():
    assert plan_aug_counts(3, COUNT_MODE, total=0) == [0, 0, 0]


def test_multiplier_mode_gives_exactly_k_copies():
    assert plan_aug_counts(3, MULTIPLIER_MODE, multiplier=2) == [2, 2, 2]
    assert plan_aug_counts(3, MULTIPLIER_MODE, multiplier=0) == [0, 0, 0]


def test_ratio_mode_draws_a_subset_without_replacement():
    plan = plan_aug_counts(40, RATIO_MODE, ratio=0.5, rng=make_rng(5))
    assert len(plan) == 40
    assert set(plan) <= {0, 1}
    assert sum(plan) == 20
    assert plan.count(1) == 20


def test_ratio_mode_is_reproducible_and_seed_dependent():
    first = plan_aug_counts(40, RATIO_MODE, ratio=0.5, rng=make_rng(5))
    second = plan_aug_counts(40, RATIO_MODE, ratio=0.5, rng=make_rng(5))
    assert first == second
    different = plan_aug_counts(40, RATIO_MODE, ratio=0.5, rng=make_rng(6))
    assert sum(different) == 20
    assert different != first


def test_ratio_mode_full_ratio_selects_every_image():
    plan = plan_aug_counts(40, RATIO_MODE, ratio=1.0, rng=make_rng(5))
    assert plan == [1] * 40


def test_ratio_mode_rounding():
    plan = plan_aug_counts(3, RATIO_MODE, ratio=0.4, rng=make_rng(1))
    assert sum(plan) == 1
    assert plan.count(1) == 1


def test_ratio_mode_is_binary_for_every_ratio():
    for count in (1, 5, 40):
        for value in RATIOS:
            plan = plan_aug_counts(
                count, RATIO_MODE, ratio=value, rng=make_rng(3)
            )
            assert len(plan) == count
            assert set(plan) <= {0, 1}
            assert sum(plan) == round(count * value)


def test_ratio_mode_zero_and_one_are_constant():
    assert plan_aug_counts(7, RATIO_MODE, ratio=0.0, rng=make_rng(2)) == [
        0
    ] * 7
    assert plan_aug_counts(7, RATIO_MODE, ratio=1.0, rng=make_rng(2)) == [
        1
    ] * 7


def test_ratio_mode_without_rng_keeps_one_entry_per_image():
    plan = plan_aug_counts(3, RATIO_MODE, ratio=0.4)
    assert plan == [1, 0, 0]
    assert len(plan) == 3
    assert sum(plan) == 1
    assert plan_aug_counts(4, RATIO_MODE, ratio=1.0) == [1, 1, 1, 1]
    assert plan_aug_counts(3, RATIO_MODE, ratio=0.0) == [0, 0, 0]
    assert len(plan_aug_counts(7, RATIO_MODE, ratio=1.0)) == 7


def test_ratio_mode_without_rng_follows_plan_order():
    assert plan_aug_counts(7, RATIO_MODE, ratio=0.3) == [
        1,
        1,
        0,
        0,
        0,
        0,
        0,
    ]


def test_zero_samples_raises():
    with pytest.raises(ValidationConfigError):
        plan_aug_counts(0, COUNT_MODE, total=4)
    with pytest.raises(ValidationConfigError):
        plan_aug_counts(0, MULTIPLIER_MODE, multiplier=1)
    with pytest.raises(ValidationConfigError):
        plan_aug_counts(0, RATIO_MODE, ratio=1.0)


def test_invalid_inputs_raise():
    with pytest.raises(ValidationConfigError):
        plan_aug_counts(3, COUNT_MODE, total=-1)
    with pytest.raises(ValidationConfigError):
        plan_aug_counts(3, MULTIPLIER_MODE, multiplier=-1)
    with pytest.raises(ValidationConfigError):
        plan_aug_counts(3, RATIO_MODE, ratio=11.0)
    with pytest.raises(ValidationConfigError):
        plan_aug_counts(3, "unknown", total=1)
