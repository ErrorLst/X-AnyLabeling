"""Configuration helper tests for the model validation tool."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import fields

import pytest

from anylabeling.custom.model_validation.app_config import (
    BORDER_FILL,
    COUNT_MODE,
    DEFAULT_RATIO,
    MULTIPLIER_MODE,
    RATIO_MODE,
    AugmentParams,
    ValidationConfig,
    ValidationConfigError,
    derive_seed,
    draw_selection,
    is_int_dimension,
    load_classes_file,
    make_rng,
    parse_imgsz_literal,
    parse_names_literal,
    selectable_fields,
    validate_augment_params,
)

# The official Ultralytics arguments this tool still carries: exactly the
# keys of AugmentParams.to_official_dict().
OFFICIAL_NAMES = [
    "hsv_v",
    "degrees",
    "translate",
    "scale",
    "flipud",
    "fliplr",
]

# The fields of the dataclass in their declared order: the order is the
# grid order of the configuration page and the key order of the snapshot.
AUGMENT_FIELDS = (
    "contrast",
    "hsv_v",
    "degrees",
    "translate",
    "scale_min",
    "scale_max",
    "flipud",
    "fliplr",
    "select_prob",
    "seed",
)

SNAPSHOT_EXTRAS = {
    "scale",
    "border_fill",
    "official_names",
    "non_official_names",
    "disabled_multi_image_augmentations",
    "not_applied",
}


def test_augment_param_names_and_defaults():
    """The dataclass carries the converged set and ships usable.

    The shipped values themselves are pinned once, by
    test_mv_defaults.test_the_augmentation_defaults_are_the_shipped_table;
    this test keeps the structural promises of the parameter set: the
    exact field list and order, the official keys, and defaults that pass
    the range validator - a default that failed it would block every run
    out of the box.
    """

    params = AugmentParams()
    names = [item.name for item in fields(AugmentParams)]
    assert names == list(AUGMENT_FIELDS)
    assert set(params.to_official_dict()) == set(OFFICIAL_NAMES)
    # the fill is not a parameter any more: it cannot be selected
    assert not hasattr(params, "border_mode")
    assert "border_mode" not in names
    validate_augment_params(params)
    assert 0.0 <= params.scale_min <= params.scale_max


def test_augment_snapshot_records_the_converged_key_set():
    params = AugmentParams()
    snapshot = params.snapshot()
    # the snapshot mirrors the dataclass, so a changed default is never
    # chased with a second literal in this file
    assert snapshot["scale"] == [params.scale_min, params.scale_max]
    assert snapshot["border_fill"] == BORDER_FILL == "black"
    assert "border_mode" not in snapshot
    assert set(snapshot) == set(AUGMENT_FIELDS) | SNAPSHOT_EXTRAS
    # nothing is registered as not applied any more, and the two fields
    # without an official name are recorded under their own key
    assert snapshot["not_applied"] == []
    assert snapshot["non_official_names"] == {
        "contrast": params.contrast,
        "select_prob": params.select_prob,
    }
    assert set(snapshot["official_names"]) == set(OFFICIAL_NAMES)
    disabled = snapshot["disabled_multi_image_augmentations"]
    for name in ["mosaic", "mixup", "cutmix", "copy_paste"]:
        assert name in disabled


def test_the_flip_enable_bits_are_booleans():
    params = AugmentParams()
    assert isinstance(params.flipud, bool) is True
    assert isinstance(params.fliplr, bool) is True
    snapshot = params.snapshot()
    assert isinstance(snapshot["flipud"], bool) is True
    assert isinstance(snapshot["fliplr"], bool) is True


def test_official_dict_writes_the_effective_flip_chance():
    params = AugmentParams()
    assert params.select_prob == 0.1
    official = params.to_official_dict()
    assert official["flipud"] == params.select_prob
    assert official["fliplr"] == params.select_prob
    unchecked = AugmentParams(flipud=False)
    assert unchecked.to_official_dict()["flipud"] == 0.0
    # the same name means something else in the two places of the
    # snapshot: enable bit here, chance there
    assert unchecked.snapshot()["flipud"] is False
    assert unchecked.snapshot()["official_names"]["fliplr"] == 0.1


def test_official_dict_uses_scale_range():
    params = AugmentParams(scale_min=0.5, scale_max=1.5)
    assert params.to_official_dict()["scale"] == [0.5, 1.5]
    single = AugmentParams(scale_min=1.0, scale_max=1.0)
    assert single.to_official_dict()["scale"] == 1.0


def test_selectable_fields_follows_the_flip_enable_bits():
    base = ("contrast", "hsv_v", "degrees", "translate", "scale")
    both = selectable_fields(AugmentParams())
    assert both == base + ("flipud", "fliplr")
    assert len(both) == 7
    assert selectable_fields(AugmentParams(flipud=False)) == base + (
        "fliplr",
    )
    assert len(selectable_fields(AugmentParams(fliplr=False))) == 6
    assert selectable_fields(
        AugmentParams(flipud=False, fliplr=False)
    ) == base


def test_draw_selection_edges_and_reproducibility():
    certain = AugmentParams(select_prob=1.0)
    assert draw_selection(certain, 0) == selectable_fields(certain)
    assert draw_selection(AugmentParams(select_prob=0.0), 3) == ()
    mild = AugmentParams()
    first = draw_selection(mild, 5)
    assert first == draw_selection(AugmentParams(), 5)
    assert set(first) <= set(selectable_fields(mild))


def test_draw_selection_never_picks_an_unchecked_flip():
    without = AugmentParams(flipud=False, fliplr=False)
    for seed in range(8):
        selection = draw_selection(without, seed)
        assert "flipud" not in selection
        assert "fliplr" not in selection
    assert draw_selection(
        AugmentParams(flipud=False, fliplr=False, select_prob=1.0), 0
    ) == selectable_fields(without)


def test_load_classes_file_order_and_blanks(tmp_path):
    path = tmp_path / "classes.txt"
    text = "cat" + chr(10) + chr(10) + "dog" + chr(10) + "  bird  " + chr(10)
    path.write_text(text, encoding="utf-8")
    assert load_classes_file(str(path)) == ["cat", "dog", "bird"]


def test_load_classes_file_rejects_duplicates(tmp_path):
    path = tmp_path / "classes.txt"
    path.write_text(
        "cat" + chr(10) + "dog" + chr(10) + "cat", encoding="utf-8"
    )
    with pytest.raises(ValidationConfigError):
        load_classes_file(str(path))


def test_load_classes_file_rejects_empty(tmp_path):
    path = tmp_path / "classes.txt"
    path.write_text("   " + chr(10), encoding="utf-8")
    with pytest.raises(ValidationConfigError):
        load_classes_file(str(path))


def test_load_classes_file_missing():
    with pytest.raises(ValidationConfigError):
        load_classes_file("does-not-exist.txt")


def test_validate_augment_params_ranges():
    validate_augment_params(AugmentParams())
    with pytest.raises(ValidationConfigError):
        validate_augment_params(AugmentParams(contrast=1.5))
    with pytest.raises(ValidationConfigError):
        validate_augment_params(AugmentParams(select_prob=1.5))
    with pytest.raises(ValidationConfigError):
        validate_augment_params(AugmentParams(select_prob=0.0))
    with pytest.raises(ValidationConfigError):
        validate_augment_params(AugmentParams(scale_min=2.0, scale_max=1.0))


def test_validate_augment_params_rejects_a_non_boolean_flip():
    # 0.2 is truthy, so a plain truthiness check would let the old
    # probability contract through and silently change every draw
    with pytest.raises(ValidationConfigError):
        validate_augment_params(AugmentParams(flipud=0.2))
    with pytest.raises(ValidationConfigError):
        validate_augment_params(AugmentParams(fliplr=0))


def test_removed_parameter_keys_are_ignored_silently():
    params = AugmentParams.from_dict(
        {"perspective": 0.5, "shear": 8.0, "erasing": 0.4, "bgr": 1.0}
    )
    assert not hasattr(params, "perspective")
    assert not hasattr(params, "bgr")
    assert not hasattr(params, "erasing")
    validate_augment_params(params)
    assert params == AugmentParams()


def test_parse_names_literal_orders_by_key():
    quote = chr(34)
    mapping = (
        "{1: " + quote + "b" + quote + ", 0: " + quote + "a" + quote + "}"
    )
    assert parse_names_literal(mapping) == ["a", "b"]
    sequence = "[" + quote + "a" + quote + ", " + quote + "b" + quote + "]"
    assert parse_names_literal(sequence) == ["a", "b"]
    with pytest.raises(ValidationConfigError):
        parse_names_literal("not-a-literal")


def test_parse_imgsz_literal():
    assert parse_imgsz_literal("[640, 640]") == [640, 640]
    assert parse_imgsz_literal("640") == [640, 640]
    with pytest.raises(ValidationConfigError):
        parse_imgsz_literal("oops")


def test_is_int_dimension():
    assert is_int_dimension(640)
    assert not is_int_dimension("batch")
    assert not is_int_dimension(True)


def test_derive_seed_and_rng_are_stable():
    assert derive_seed(0, 0) == 0
    assert derive_seed(7, 1) == derive_seed(7, 1)
    assert derive_seed(7, 1) != derive_seed(7, 2)
    first = make_rng(11, 3).integers(0, 1000, size=4).tolist()
    second = make_rng(11, 3).integers(0, 1000, size=4).tolist()
    assert first == second


def test_the_default_augment_mode_is_ratio():
    "A configuration built from the defaults derives the amount by ratio."

    config = ValidationConfig()
    assert config.augment_mode == RATIO_MODE
    assert config.ratio == DEFAULT_RATIO == 0.5
    payload = config.to_dict()
    assert payload["augment_mode"] == RATIO_MODE
    assert payload["augment_mode_fixed"] is True


def test_augmentation_is_disabled_by_default():
    "A configuration built from the defaults generates no copies."

    config = ValidationConfig()
    assert config.augment_enabled is False
    assert config.to_dict()["augment_enabled"] is False
    assert config.judge_augmented is True


def test_validation_config_snapshot():
    config = ValidationConfig(
        dataset_dir="data",
        augment_enabled=True,
        augment_mode=COUNT_MODE,
        total_count=5,
    )
    payload = config.to_dict()
    assert payload["dataset_dir_display"] == "data"
    assert payload["augment_mode"] == COUNT_MODE
    assert payload["augment_mode_fixed"] is True
    assert payload["total_count"] == 5
    assert "augment_params" in payload
    assert MULTIPLIER_MODE != RATIO_MODE
