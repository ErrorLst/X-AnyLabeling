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
    is_int_dimension,
    load_classes_file,
    make_rng,
    parse_imgsz_literal,
    parse_names_literal,
    validate_augment_params,
)

OFFICIAL_NAMES = [
    "hsv_h",
    "hsv_s",
    "hsv_v",
    "degrees",
    "translate",
    "scale_min",
    "scale_max",
    "shear",
    "perspective",
    "flipud",
    "fliplr",
    "bgr",
    "erasing",
    "crop_fraction",
]


def test_augment_param_names_and_defaults():
    """The dataclass carries every official name and ships usable.

    The shipped values themselves are pinned once, by
    test_mv_defaults.test_the_augmentation_defaults_are_the_shipped_table;
    this test keeps the two structural promises of the parameter set:
    every official Ultralytics argument is a field, and the defaults pass
    the range validator - a default that failed it would block every run
    out of the box.
    """

    params = AugmentParams()
    names = [item.name for item in fields(AugmentParams)]
    for name in OFFICIAL_NAMES:
        assert name in names
    # the fill is not a parameter any more: it cannot be selected
    assert not hasattr(params, "border_mode")
    assert "border_mode" not in names
    validate_augment_params(params)
    assert 0.0 <= params.scale_min <= params.scale_max


def test_augment_snapshot_records_disabled_multi_image():
    params = AugmentParams()
    snapshot = params.snapshot()
    # the snapshot mirrors the dataclass, so a changed default is never
    # chased with a second literal in this file
    assert snapshot["scale"] == [params.scale_min, params.scale_max]
    assert snapshot["border_fill"] == BORDER_FILL == "black"
    assert "border_mode" not in snapshot
    assert snapshot["not_applied"] == ["erasing", "crop_fraction"]
    disabled = snapshot["disabled_multi_image_augmentations"]
    for name in ["mosaic", "mixup", "cutmix", "copy_paste"]:
        assert name in disabled


def test_official_dict_uses_scale_range():
    params = AugmentParams(scale_min=0.5, scale_max=1.5)
    assert params.to_official_dict()["scale"] == [0.5, 1.5]
    single = AugmentParams(scale_min=1.0, scale_max=1.0)
    assert single.to_official_dict()["scale"] == 1.0


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
        validate_augment_params(AugmentParams(perspective=0.5))
    with pytest.raises(ValidationConfigError):
        validate_augment_params(AugmentParams(scale_min=2.0, scale_max=1.0))


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
    assert config.to_dict()["augment_mode"] == RATIO_MODE


def test_augmentation_is_enabled_by_default():
    "A configuration built from the defaults generates augmented copies."

    config = ValidationConfig()
    assert config.augment_enabled is True
    assert config.to_dict()["augment_enabled"] is True


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
    assert payload["total_count"] == 5
    assert "augment_params" in payload
    assert MULTIPLIER_MODE != RATIO_MODE
