"""ModelRunner tests: classes.txt is the authority for the labels.

The ONNX session and the ultralytics YOLO wrapper are replaced by small
stand-ins so that the class semantics of the runner can be asserted
without a real ONNX file. The stand-ins mirror the contract of the
wrapper that caused the reported bug: predict_shapes decodes the file
from the path it receives and answers an empty shape list - never an
exception - whenever that decode cannot run.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from anylabeling.custom.model_validation import inference
from anylabeling.custom.model_validation.app_config import (
    ValidationConfigError,
)
from anylabeling.custom.model_validation.onnx_meta import (
    class_count_mismatch_message,
    name_diff_warning,
)

PLACEHOLDERS = ["class_0", "class_1"]
CLASSES = ["a0_dian", "a1_xian"]


def metadata_for(names) -> dict:
    """Return ONNX metadata embedding the given class names."""

    entries = ", ".join(
        f"{index}: {name!r}" for index, name in enumerate(names)
    )
    return {
        "task": "detect",
        "imgsz": "[640, 640]",
        "names": "{" + entries + "}",
    }


def result_of(shapes) -> object:
    """Return an object shaped like the result of the YOLO wrapper."""

    return type("Result", (), {"shapes": list(shapes)})()


def write_png(directory, name: str, size=(64, 96)) -> str:
    """Write a plain RGB PNG and return its path."""

    import cv2
    import numpy as np

    height, width = size
    image = np.full((height, width, 3), 127, dtype=np.uint8)
    ok, buffer = cv2.imencode(".png", image)
    assert ok
    path = os.path.join(str(directory), name)
    buffer.tofile(path)
    return path


class FakeSession:
    """Stand-in for the ONNX session of the auto labeling engine."""

    def __init__(self, model_path: str, device: str) -> None:
        self.model_path = model_path
        self.device = device

    def get_input_shape(self):
        """Return a static NCHW input shape."""

        return [1, 3, 640, 640]


class FakePoint:
    """Point with the Qt style accessors used by the pipeline."""

    def __init__(self, x: float, y: float) -> None:
        self._x = float(x)
        self._y = float(y)

    def x(self) -> float:
        """Return the x coordinate."""

        return self._x

    def y(self) -> float:
        """Return the y coordinate."""

        return self._y


class FakeShape:
    """Predicted shape carrying the label given by the YOLO wrapper."""

    def __init__(self, label: str, score: float = 0.9) -> None:
        self.label = label
        self.score = float(score)
        self.shape_type = "rectangle"
        self.points = [FakePoint(0.0, 0.0), FakePoint(10.0, 10.0)]


class FakeYOLO:
    """Stand-in for the ultralytics wrapper of the repository.

    predict_shapes returns one fixed shape per class id, labelled with the
    class table the runner put into the configuration - the very same
    expression yolo.py uses: shape.label = str(self.classes[class_id]).
    Like the wrapper it answers an empty list, without raising, as soon as
    it does not receive the path of an existing file: that is exactly the
    silent degradation the runner has to rule out.
    """

    def __init__(self, config, on_message=None) -> None:
        self.config = dict(config)
        self.on_message = on_message
        self.classes = list(self.config.get("classes") or [])
        self.net = FakeSession(self.config.get("model_path", ""), "cpu")
        self.calls = []

    def predict_shapes(self, image, image_path=None):
        """Return one shape per class id of the configured table."""

        self.calls.append((image, image_path))
        if not image_path or not os.path.isfile(image_path):
            return result_of([])
        return result_of(FakeShape(str(name)) for name in self.classes)


class RealDecoderYOLO:
    """Stand-in decoding the file exactly like the wrapper does.

    The pixels come from the repository decoder and from the path, never
    from the first argument: passing an array instead of a QImage, or a
    path that is not the path of the file, is therefore visible here.
    """

    emit_shapes = True

    def __init__(self, config, on_message=None) -> None:
        self.config = dict(config)
        self.on_message = on_message
        self.classes = list(self.config.get("classes") or [])
        self.net = FakeSession(self.config.get("model_path", ""), "cpu")
        self.calls = []
        self.decoded = []

    def predict_shapes(self, image, image_path=None):
        """Decode the file by path and answer what the model stands for."""

        from anylabeling.views.labeling.utils.opencv import (
            qt_img_to_rgb_cv_img,
        )

        self.calls.append((image, image_path))
        self.decoded.append(qt_img_to_rgb_cv_img(image, image_path).shape)
        if not self.emit_shapes:
            return result_of([])
        return result_of(FakeShape(str(name)) for name in self.classes)


class EmptyResultYOLO(RealDecoderYOLO):
    """Same decode, but the image holds no object at all."""

    emit_shapes = False


class SwallowingYOLO(RealDecoderYOLO):
    """The wrapper as it behaves when its own decode cannot run.

    The file is moved away first, which is what a broken decode looks
    like from the outside, and the exception stays inside the wrapper:
    the caller only sees an empty shape list.
    """

    def predict_shapes(self, image, image_path=None):
        """Move the file away, then answer what the wrapper answers."""

        from anylabeling.views.labeling.utils.opencv import (
            qt_img_to_rgb_cv_img,
        )

        self.calls.append((image, image_path))
        os.replace(image_path, image_path + ".moved")
        try:
            qt_img_to_rgb_cv_img(image, image_path)
        except Exception:  # noqa: BLE001
            return result_of([])
        return result_of(FakeShape(str(name)) for name in self.classes)


def install_model(monkeypatch, tmp_path, metadata, yolo_class) -> str:
    """Patch the ONNX session, the YOLO wrapper and the metadata read."""

    import anylabeling.services.auto_labeling.__base__.yolo as yolo_base
    import anylabeling.services.auto_labeling.engines as engines

    model_path = os.path.join(str(tmp_path), "model.onnx")
    with open(model_path, "wb") as handle:
        handle.write(b"onnx-stand-in")
    monkeypatch.setattr(engines, "OnnxBaseModel", FakeSession)
    monkeypatch.setattr(yolo_base, "YOLO", yolo_class)
    monkeypatch.setattr(
        inference, "read_onnx_metadata", lambda _path: dict(metadata)
    )
    return model_path


def null_placeholder(_path: str):
    """Return the null QImage of a file that Qt cannot load."""

    from PyQt6 import QtGui

    return QtGui.QImage()


@pytest.fixture
def runner_env(monkeypatch, tmp_path):
    """Patch the ONNX session, the YOLO wrapper and the metadata read."""

    model_path = install_model(monkeypatch, tmp_path, {}, FakeYOLO)
    # the class semantics only need an array: the decoder stays out of
    # the tests that assert which label a prediction carries
    monkeypatch.setattr(inference, "load_image_rgb", lambda _path: [[0, 0, 0]])

    def install(metadata: dict) -> str:
        monkeypatch.setattr(
            inference,
            "read_onnx_metadata",
            lambda _path: dict(metadata),
        )
        return model_path

    return install


@pytest.fixture
def decoder_env(monkeypatch, tmp_path):
    """Patch everything but the decoders: the wrapper decodes by path."""

    return install_model(
        monkeypatch, tmp_path, metadata_for(PLACEHOLDERS), RealDecoderYOLO
    )


@pytest.fixture
def empty_env(monkeypatch, tmp_path):
    """Patch a model that decodes the file and finds no object."""

    return install_model(
        monkeypatch, tmp_path, metadata_for(PLACEHOLDERS), EmptyResultYOLO
    )


@pytest.fixture
def swallow_env(monkeypatch, tmp_path):
    """Patch a wrapper whose broken decode stays silent.

    The placeholder is null on purpose: it is what a file Qt cannot load
    leaves behind, and the fallback branch of the decoder then has no
    pixels to work with either.
    """

    model_path = install_model(
        monkeypatch, tmp_path, metadata_for(PLACEHOLDERS), SwallowingYOLO
    )
    monkeypatch.setattr(
        inference, "build_placeholder_qimage", null_placeholder
    )
    return model_path


@pytest.fixture
def image_file(tmp_path):
    """A readable, three channel PNG."""

    return write_png(tmp_path, "sample.png")


@pytest.fixture
def chinese_image_file(tmp_path):
    """A readable PNG behind a path cv2 cannot open with imread."""

    return write_png(tmp_path, "中文 图 1.png")


def test_placeholder_names_do_not_block_and_are_reported(runner_env):
    """The reported case: class_0/class_1 against a0_dian/a1_xian."""

    model_path = runner_env(metadata_for(PLACEHOLDERS))
    runner = inference.ModelRunner(model_path, CLASSES)
    assert runner.info["classes_count_match"] is True
    assert runner.info["names"] == PLACEHOLDERS
    assert runner.info["classes"] == CLASSES
    assert len(runner.name_diff) == 2
    assert any(
        "已以 classes.txt 的类名为准" in warning for warning in runner.warnings
    )
    assert name_diff_warning(2) in runner.warnings


def test_prediction_labels_come_from_classes_txt(runner_env, image_file):
    """Every predicted label is a name of the class table."""

    model_path = runner_env(metadata_for(PLACEHOLDERS))
    runner = inference.ModelRunner(model_path, CLASSES)
    labels = [shape.label for shape in runner.predict(image_file)]
    assert labels == CLASSES
    assert runner.model.config["classes"] == CLASSES
    assert set(labels).isdisjoint(PLACEHOLDERS)


def test_class_count_mismatch_still_blocks(runner_env):
    """Three classes in classes.txt against two embedded names."""

    model_path = runner_env(metadata_for(PLACEHOLDERS))
    with pytest.raises(ValidationConfigError) as error:
        inference.ModelRunner(model_path, CLASSES + ["a2_other"])
    message = str(error.value)
    assert "classes.txt 有 3 类" in message
    assert "模型 names 有 2 类" in message
    assert message == class_count_mismatch_message(3, 2)


def test_model_info_records_both_name_lists(runner_env):
    """The report snapshot keeps the embedded and the effective names."""

    model_path = runner_env(metadata_for(PLACEHOLDERS))
    runner = inference.ModelRunner(model_path, CLASSES)
    info = runner.model_info()
    assert info["names"] == PLACEHOLDERS
    assert info["names_model"] == PLACEHOLDERS
    assert info["classes"] == CLASSES
    assert info["classes_count_match"] is True
    assert len(info["classes_name_diff"]) == 2
    assert info["classes_name_diff"][0].startswith("0: model=")


def test_model_info_truncates_a_long_diff(runner_env):
    """A long difference stays truncated inside the report."""

    many = [f"class_{index}" for index in range(9)]
    classes = [f"real_{index}" for index in range(9)]
    model_path = runner_env(metadata_for(many))
    runner = inference.ModelRunner(model_path, classes)
    info = runner.model_info()
    assert len(info["classes_name_diff"]) == 6
    assert info["classes_name_diff"][-1] == "…"


def test_matching_names_keep_the_warning_list_empty(runner_env):
    """Identical names add nothing to the warnings."""

    model_path = runner_env(metadata_for(CLASSES))
    runner = inference.ModelRunner(model_path, CLASSES)
    assert runner.name_diff == []
    assert runner.warnings == []


def test_predict_hands_a_qimage_and_the_path_to_the_model(
    runner_env, image_file
):
    """The regression of the reported bug: the path must be passed.

    Without the second argument the wrapper of the repository cannot
    decode anything, answers an empty shape list and every image of the
    run is judged as if the model had found nothing.
    """

    from PyQt6 import QtGui

    model_path = runner_env(metadata_for(PLACEHOLDERS))
    runner = inference.ModelRunner(model_path, CLASSES)
    shapes = runner.predict(image_file)
    assert [shape.label for shape in shapes] == CLASSES
    image, image_path = runner.model.calls[0]
    assert isinstance(image, QtGui.QImage)
    assert image_path == image_file


def test_predict_decodes_the_file_by_path(decoder_env, chinese_image_file):
    """The pixels come from the path, exactly like auto labeling does."""

    runner = inference.ModelRunner(decoder_env, CLASSES)
    shapes = runner.predict(chinese_image_file)
    assert len(shapes) == len(CLASSES)
    assert runner.model.decoded == [(64, 96, 3)]
    assert runner.model.calls[0][1] == chinese_image_file


def test_predict_accepts_a_path_like_argument(runner_env, image_file):
    """A pathlib argument behaves like the plain string path."""

    from pathlib import Path

    model_path = runner_env(metadata_for(PLACEHOLDERS))
    runner = inference.ModelRunner(model_path, CLASSES)
    assert len(runner.predict(Path(image_file))) == len(CLASSES)
    assert runner.model.calls[0][1] == image_file


def test_predict_refuses_a_pixel_array(runner_env):
    """A decoded array is refused instead of answering nothing."""

    import numpy as np

    model_path = runner_env(metadata_for(PLACEHOLDERS))
    runner = inference.ModelRunner(model_path, CLASSES)
    with pytest.raises(inference.InferenceError) as error:
        runner.predict(np.zeros((8, 8, 3), dtype=np.uint8))
    assert "ndarray" in str(error.value)
    # the model is never asked, so no silent empty prediction is built
    assert runner.model.calls == []


def test_predict_refuses_a_missing_argument(runner_env):
    """A missing path is an explicit error, not an empty prediction."""

    model_path = runner_env(metadata_for(PLACEHOLDERS))
    runner = inference.ModelRunner(model_path, CLASSES)
    with pytest.raises(inference.InferenceError) as error:
        runner.predict(None)
    assert "NoneType" in str(error.value)
    assert runner.model.calls == []


def test_ensure_qimage_refuses_a_pixel_array():
    """The guard rejects the array before the wrapper sees it."""

    import numpy as np

    with pytest.raises(inference.InferenceError) as error:
        inference.ensure_qimage(np.zeros((4, 4, 3), dtype=np.uint8))
    assert "ndarray" in str(error.value)


def test_build_placeholder_qimage_returns_a_qimage(image_file):
    """The placeholder of a readable file is a loaded QImage."""

    from PyQt6 import QtGui

    image = inference.build_placeholder_qimage(image_file)
    assert isinstance(image, QtGui.QImage)
    assert not image.isNull()
    assert inference.ensure_qimage(image) is image


def test_empty_prediction_of_a_readable_file_is_kept(empty_env, image_file):
    """An image without any object stays a normal, empty result."""

    runner = inference.ModelRunner(empty_env, CLASSES)
    assert runner.predict(image_file) == []
    assert runner.model.decoded == [(64, 96, 3)]


def test_verify_decoder_path_accepts_a_readable_file(image_file):
    """The verification decodes the file a second time and agrees."""

    from PyQt6 import QtGui

    precheck = inference.load_image_rgb(image_file)
    image = QtGui.QImage(image_file)
    result = inference.verify_decoder_path(
        image_file, image, expected=precheck
    )
    assert result is None


def test_verify_decoder_path_reports_a_file_that_vanished(image_file):
    """A decode that cannot run raises instead of answering nothing."""

    from PyQt6 import QtGui

    os.replace(image_file, image_file + ".moved")
    with pytest.raises(inference.InferenceError) as error:
        inference.verify_decoder_path(image_file, QtGui.QImage())
    assert "decode" in str(error.value)


def test_a_swallowed_decode_failure_is_not_an_empty_image(
    swallow_env, image_file
):
    """An empty prediction of a broken decode is reported, not judged."""

    runner = inference.ModelRunner(swallow_env, CLASSES)
    with pytest.raises(inference.InferenceError) as error:
        runner.predict(image_file)
    assert "decode" in str(error.value)
