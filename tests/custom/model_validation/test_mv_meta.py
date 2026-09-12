"""ONNX metadata contract tests."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from anylabeling.custom.model_validation.onnx_meta import (
    ELLIPSIS,
    MISSING_META_MESSAGE,
    NAME_PREVIEW_LIMIT,
    MetaValidationError,
    class_count_mismatch_message,
    compare_names,
    name_diff_warning,
    resolve_family,
    truncate_diff,
    validate_meta,
)

INPUT_SHAPE = [1, 3, 640, 640]

PERSON = "person"
CAR = "car"


def base_metadata(task: str = "detect") -> dict:
    quote = chr(34)
    names = (
        "{0: " + quote + PERSON + quote + ", 1: " + quote + CAR + quote + "}"
    )
    return {
        "task": task,
        "imgsz": "[640, 640]",
        "names": names,
        "stride": "32",
        "batch": "1",
        "dynamic": "False",
    }


def test_valid_metadata_for_every_supported_task():
    expected = {
        "detect": "yolov8",
        "segment": "yolov8_seg",
        "obb": "yolov8_obb",
        "pose": "yolov8_pose",
    }
    for task, family in expected.items():
        info = validate_meta(base_metadata(task), INPUT_SHAPE, [PERSON, CAR])
        assert info["family"] == family
        assert info["names"] == [PERSON, CAR]
        assert info["imgsz_meta"] == [640, 640]
        assert info["imgsz_session"] == [640, 640]
        assert info["classes"] == [PERSON, CAR]
        assert info["classes_count_match"] is True
        assert info["classes_name_diff"] == []
        assert info["warnings"] == []


def test_missing_keys_are_blocked():
    for key in ("task", "imgsz", "names"):
        metadata = base_metadata()
        metadata.pop(key)
        with pytest.raises(MetaValidationError) as error:
            validate_meta(metadata, INPUT_SHAPE)
        assert str(error.value) == MISSING_META_MESSAGE


def test_classify_is_blocked():
    with pytest.raises(MetaValidationError):
        validate_meta(base_metadata("classify"), INPUT_SHAPE)


def test_unknown_task_is_blocked():
    with pytest.raises(MetaValidationError):
        resolve_family("unknown")


def test_classes_count_mismatch_reports_both_amounts():
    # three classes against the two embedded ones: the only blocking
    # signal of the class comparison stays the amount. validate_meta only
    # computes the result, the caller raises the error and shows the text.
    info = validate_meta(base_metadata(), INPUT_SHAPE, [PERSON, CAR, "bus"])
    assert info["classes_count_match"] is False
    assert info["classes_name_diff"] == ["2: model=None vs txt='bus'"]
    assert info["warnings"] == []
    message = class_count_mismatch_message(3, 2)
    assert "classes.txt 有 3 类" in message
    assert "模型 names 有 2 类" in message


def test_different_names_with_matching_count_never_block():
    # class_0/class_1 are the placeholders a training script writes when
    # the class names were never exported: classes.txt stays the
    # authority and the difference is only reported.
    info = validate_meta(base_metadata(), INPUT_SHAPE, ["class_0", "class_1"])
    assert info["names"] == [PERSON, CAR]
    assert info["classes"] == ["class_0", "class_1"]
    assert info["classes_count_match"] is True
    assert len(info["classes_name_diff"]) == 2
    assert info["classes_name_diff"][0].startswith("0: model=")
    assert PERSON in info["classes_name_diff"][0]
    assert CAR in info["classes_name_diff"][1]
    # validate_meta never blocks and never warns on its own: the caller
    # turns classes_name_diff into the status line warning.
    assert info["warnings"] == []
    assert "差异 2 处" in name_diff_warning(2)


def test_classes_order_mismatch_reports_per_index_diff():
    info = validate_meta(base_metadata(), INPUT_SHAPE, [CAR, PERSON])
    assert info["classes_count_match"] is True
    assert len(info["classes_name_diff"]) == 2
    assert info["classes_name_diff"][0].startswith("0: model=")
    assert PERSON in info["classes_name_diff"][0]
    assert info["classes_name_diff"][1].startswith("1: model=")
    assert CAR in info["classes_name_diff"][1]


def test_classes_defaults_to_none_without_a_table():
    info = validate_meta(base_metadata(), INPUT_SHAPE)
    assert info["classes"] is None
    assert info["classes_count_match"] is True
    assert info["classes_name_diff"] == []
    assert info["warnings"] == []


def test_compare_names_helper():
    assert compare_names(["a"], ["a"]) == []
    assert len(compare_names(["a"], ["b"])) == 1


def test_truncate_diff_keeps_the_report_readable():
    diff = [f"{index}: model={index} vs txt={index}" for index in range(8)]
    assert truncate_diff(diff) == diff[:NAME_PREVIEW_LIMIT] + [ELLIPSIS]
    assert truncate_diff(diff[:2]) == diff[:2]
    assert truncate_diff([]) == []


def test_imgsz_mismatch_only_warns():
    metadata = base_metadata()
    metadata["imgsz"] = "[320, 320]"
    info = validate_meta(metadata, INPUT_SHAPE, [PERSON, CAR])
    assert info["imgsz_meta"] == [320, 320]
    assert info["imgsz_session"] == [640, 640]
    assert len(info["warnings"]) == 1
    assert "imgsz_session=[640, 640]" in info["warnings"][0]


def test_name_diff_does_not_hide_the_imgsz_warning():
    metadata = base_metadata()
    metadata["imgsz"] = "[320, 320]"
    info = validate_meta(metadata, INPUT_SHAPE, ["class_0", "class_1"])
    assert info["classes_count_match"] is True
    assert len(info["classes_name_diff"]) == 2
    assert len(info["warnings"]) == 1
    assert "imgsz_session" in info["warnings"][0]


def test_dynamic_dimensions_are_blocked():
    dynamic_shape = ["batch", 3, "height", "width"]
    with pytest.raises(MetaValidationError):
        validate_meta(base_metadata(), dynamic_shape)


def test_dynamic_flag_is_reported():
    metadata = base_metadata()
    metadata["dynamic"] = "True"
    info = validate_meta(metadata, INPUT_SHAPE)
    assert info["dynamic"] is True
    assert any("dynamic" in warning for warning in info["warnings"])


def test_name_preview_limit_is_five():
    assert NAME_PREVIEW_LIMIT == 5


def test_defaults_for_optional_keys():
    quote = chr(34)
    names = "[" + quote + PERSON + quote + "]"
    metadata = {
        "task": "detect",
        "imgsz": "[640, 640]",
        "names": names,
    }
    info = validate_meta(metadata, INPUT_SHAPE)
    assert info["stride"] == 32
    assert info["batch"] == 1
    assert info["dynamic"] is False
