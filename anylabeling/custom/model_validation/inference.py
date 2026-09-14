"""ONNX inference wrapper for the model validation tool.

The ultralytics YOLO model class of this repository is reused so that
the verdicts are produced by exactly the same preprocessing, NMS and
shape construction code that powers auto labeling.
"""

from __future__ import annotations

import os
import os.path as osp
from typing import Any, Dict, List, Optional, Sequence

from .app_config import INFERENCE_CONF_THRESHOLD, ValidationConfigError
from .judge import (
    shape_label,
    shape_point_list,
    shape_score,
    shape_type_of,
)
from .onnx_meta import (
    class_count_mismatch_message,
    read_onnx_metadata,
    truncate_diff,
    validate_meta,
)

MODEL_NAME = "model_validation"
MODEL_DISPLAY_NAME = "模型验证"

# The status line of the validation window shows this hint when the
# embedded names differ from the class table: the run continues and
# classes.txt decides the label of every prediction.
NAME_DIFF_HINT = (
    "已以 classes.txt 的类名为准（模型内嵌 names 为占位名，差异 {count} 处）"
)

# The decode of the wrapper is silent: predict_shapes logs a warning and
# answers an empty shape list when it cannot decode the file, therefore
# an empty prediction is ambiguous. These messages name the failure of
# the checks that remove the ambiguity.
IMAGE_PATH_MESSAGE = (
    "predict expects the path of an image (str, bytes or os.PathLike), "
    "got {kind}: pixels must be decoded by path instead of being passed "
    "to predict_shapes"
)

QIMAGE_MESSAGE = (
    "predict_shapes expects a QImage placeholder, got {kind}: a pixel "
    "array makes the wrapper answer an empty prediction instead of "
    "raising"
)

DECODE_PATH_MESSAGE = (
    "the repository decoder cannot decode this image ({error}); its "
    "empty prediction is not a trustworthy result"
)

CHANGED_IMAGE_MESSAGE = (
    "the image changed while it was inferred: the precheck decoded "
    "{precheck} but the decoder of the wrapper produced {decoded}"
)


class InferenceError(RuntimeError):
    """Raised when one image cannot be decoded or inferred."""


def ensure_current_config_file() -> str:
    """Point anylabeling.config at the shared rc file when unset.

    The model base class reads the global configuration, therefore the
    worker thread must make sure the rc path is available before it
    builds a model.
    """

    from anylabeling import config as config_module

    if config_module.current_config_file:
        return config_module.current_config_file
    rc_path = osp.join(config_module.get_work_directory(), ".xanylabelingrc")
    config_module.current_config_file = rc_path
    return rc_path


def load_image_rgb(path: str):
    """Decode an image from disk into an RGB uint8 array."""

    import cv2
    import numpy as np

    if not path or not osp.isfile(path):
        raise InferenceError(f"image not found: {path}")
    try:
        buffer = np.fromfile(path, dtype=np.uint8)
        image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    except (OSError, ValueError, cv2.error) as error:
        raise InferenceError(f"failed to decode image: {error}") from error
    if image is None:
        raise InferenceError(f"failed to decode image: {path}")
    return cv2.cvtColor(image, cv2.COLOR_BGR2RGB)


def as_image_path(value: Any) -> str:
    """Return the image path of value and refuse anything else.

    The runner only infers files: an already decoded array - the very
    argument that made every prediction empty - is rejected here, before
    any model call, with an explicit error.
    """

    if isinstance(value, (str, bytes, os.PathLike)):
        return os.fsdecode(value)
    raise InferenceError(IMAGE_PATH_MESSAGE.format(kind=type(value).__name__))


def build_placeholder_qimage(image_path: str) -> Any:
    """Return the QImage placeholder handed to the model wrapper.

    The pixels never come from this object: the decoder of the
    repository reads the file of the path itself (cv2.imdecode of the
    raw bytes, which stays safe for non ASCII paths). The wrapper
    branches on a QImage though, therefore one must be passed; keeping
    it loaded also covers a file that disappears between the precheck
    and the call, because the wrapper then falls back to these pixels
    instead of logging a warning and answering an empty list.
    """

    from PyQt6 import QtGui

    return QtGui.QImage(str(image_path))


def ensure_qimage(image: Any) -> Any:
    """Return image when it is a QImage, raise InferenceError otherwise.

    This guard removes the silent degradation at its source: a pixel
    array handed to predict_shapes is not decoded at all, the wrapper
    only logs a warning and answers an empty shape list, and the caller
    sees every image judged as if the model had found nothing.
    """

    from PyQt6 import QtGui

    if not isinstance(image, QtGui.QImage):
        raise InferenceError(QIMAGE_MESSAGE.format(kind=type(image).__name__))
    return image


def verify_decoder_path(
    image_path: str, qimage: Any, expected: Optional[Any] = None
) -> None:
    """Decode the file through the repository branch and report failures.

    predict_shapes swallows every exception of its path based decode, so
    an empty prediction has two origins: an image without any object
    (legal) or a decode that failed (not legal). Re-running the very
    same decode is the only way to tell both apart from the outside; it
    therefore only happens for an empty prediction.
    """

    from anylabeling.views.labeling.utils.opencv import qt_img_to_rgb_cv_img

    try:
        decoded = qt_img_to_rgb_cv_img(qimage, image_path)
    except Exception as error:  # noqa: BLE001
        raise InferenceError(
            DECODE_PATH_MESSAGE.format(error=error)
        ) from error
    if decoded is None or getattr(decoded, "size", 0) == 0:
        raise InferenceError(DECODE_PATH_MESSAGE.format(error="empty image"))
    if expected is not None and tuple(decoded.shape[:2]) != tuple(
        expected.shape[:2]
    ):
        raise InferenceError(
            CHANGED_IMAGE_MESSAGE.format(
                precheck=tuple(expected.shape[:2]),
                decoded=tuple(decoded.shape[:2]),
            )
        )


class ThreadedOnnxSession:
    """ONNX session of one worker with an explicit thread budget.

    The engine of the repository builds its session with the default
    options, which hand the whole machine to every session: running
    several of them side by side then over-subscribes the CPUs and the
    stage ends up slower than its serial loop. This session mirrors the
    OnnxBaseModel surface the YOLO wrapper uses - nothing else - and
    pins the two thread pools to the budget of a single worker.
    """

    def __init__(
        self,
        model_path: str,
        device_type: str = "cpu",
        intra_op_threads: int = 1,
        inter_op_threads: int = 1,
        log_severity_level: int = 3,
    ) -> None:
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.log_severity_level = int(log_severity_level)
        options.intra_op_num_threads = max(1, int(intra_op_threads))
        options.inter_op_num_threads = max(1, int(inter_op_threads))
        self.sess_opts = options
        self.providers = ["CPUExecutionProvider"]
        if str(device_type).lower() == "gpu":
            self.providers = ["CUDAExecutionProvider"]
        self.ort_session = ort.InferenceSession(
            model_path,
            providers=self.providers,
            sess_options=options,
        )
        self.model_path = model_path

    def get_ort_inference(
        self, blob=None, inputs=None, extract=True, squeeze=False
    ):
        """Run one forward pass, exactly like OnnxBaseModel does."""

        if inputs is None:
            inputs = self.get_input_name()
            outs = self.ort_session.run(None, {inputs: blob})
        else:
            outs = self.ort_session.run(None, inputs)
        if extract:
            outs = outs[0]
        if squeeze:
            outs = outs.squeeze(axis=0)
        return outs

    def get_input_name(self) -> str:
        """Return the name of the first input of the graph."""

        return self.ort_session.get_inputs()[0].name

    def get_input_shape(self) -> Any:
        """Return the shape of the first input of the graph."""

        return self.ort_session.get_inputs()[0].shape

    def get_output_name(self) -> List[str]:
        """Return the names of the outputs of the graph."""

        return [out.name for out in self.ort_session.get_outputs()]

    def get_metadata_info(self, field: str) -> Optional[str]:
        """Return one ONNX metadata property of the model file."""

        import onnx

        model = onnx.load(self.model_path)
        for prop in model.metadata_props:
            if prop.key == field:
                return prop.value
        return None


class ModelRunner:
    """Build a YOLO model from ONNX metadata and run full frame inference."""

    def __init__(
        self,
        model_path: str,
        classes: Sequence[str],
        iou_threshold: float = 0.45,
        on_message: Optional[Any] = None,
        intra_op_threads: Optional[int] = None,
        inter_op_threads: Optional[int] = None,
    ) -> None:
        if not model_path or not osp.isfile(model_path):
            raise ValidationConfigError(f"ONNX model not found: {model_path}")
        if not classes:
            raise ValidationConfigError("classes.txt is empty")

        ensure_current_config_file()

        self.model_path = osp.abspath(model_path)
        # classes.txt is the authority: the class table drives the label
        # of every predicted shape and the class set used by the judge.
        self.classes: List[str] = [str(name) for name in classes]
        self.metadata = read_onnx_metadata(self.model_path)

        from anylabeling.services.auto_labeling.engines import OnnxBaseModel

        session = OnnxBaseModel(self.model_path, "cpu")
        info = validate_meta(
            self.metadata, session.get_input_shape(), self.classes
        )
        self.info = info
        # Only a different number of classes blocks the run: the embedded
        # names of a model exported by a training script are often
        # placeholders (class_0, class_1, ...) while classes.txt carries
        # the real names.
        if not info["classes_count_match"]:
            raise ValidationConfigError(
                class_count_mismatch_message(
                    len(self.classes), len(info["names"])
                )
            )
        self.name_diff: List[str] = list(info["classes_name_diff"])
        self._warnings: List[str] = list(info.get("warnings", []))
        if self.name_diff:
            # A name difference is a non blocking hint: it reaches the
            # user through ModelRunner.warnings - the worker publishes it
            # on the progress log and stores it in the report.
            self._warnings.append(
                self.tr(NAME_DIFF_HINT).format(count=len(self.name_diff))
            )

        from anylabeling.services.auto_labeling.__base__.yolo import YOLO

        config: Dict[str, Any] = {
            "type": info["family"],
            "name": MODEL_NAME,
            "display_name": MODEL_DISPLAY_NAME,
            "model_path": self.model_path,
            "classes": list(self.classes),
            # the confidence cut is fixed and decoupled from the page
            "conf_threshold": INFERENCE_CONF_THRESHOLD,
            # obb and pose stay single label: this tool validates box
            # predictions and the interaction of their tensors with the
            # multi row output was never verified upstream.
            "multi_label": info["task"] in ("detect", "segment"),
            "iou_threshold": float(iou_threshold),
            "stride": int(info["stride"]),
            "config_file": self.model_path,
        }
        message = on_message or (lambda text: None)
        self.model = YOLO(config, message)
        if intra_op_threads is not None:
            self._pin_session_threads(intra_op_threads, inter_op_threads)
        self.input_shape = list(self.model.net.get_input_shape())

    def _pin_session_threads(
        self, intra_op_threads: int, inter_op_threads: Optional[int] = None
    ) -> None:
        """Replace the session of the wrapper by a pinned one.

        The engine takes no thread argument, therefore the session it
        built is swapped for an equivalent one carrying the budget of
        this worker; the replaced session is released right away. Only
        the session owned by this runner is touched: every worker builds
        its own runner, so no session is ever shared between threads.
        """

        from anylabeling.app_info import __preferred_device__

        self.model.net = ThreadedOnnxSession(
            self.model_path,
            __preferred_device__,
            intra_op_threads=intra_op_threads,
            inter_op_threads=(
                1 if inter_op_threads is None else inter_op_threads
            ),
        )

    @staticmethod
    def tr(text: str) -> str:
        """Return the translatable text of a user facing message.

        The runner is a plain object: it cannot inherit QObject without
        dragging a QApplication into the worker thread, therefore the
        indirection keeps the Qt style call site of every other message
        of the tool.
        """

        return text

    @property
    def task(self) -> str:
        """Return the ultralytics task of the loaded model."""

        return str(self.info["task"])

    @property
    def warnings(self) -> List[str]:
        """Return the non blocking metadata warnings."""

        return list(self._warnings)

    def predict(self, image_path: str) -> List[Any]:
        """Run inference on one staging image and return the raw shapes.

        The file is decoded twice on purpose: once here as a precheck,
        which is the reliable source of a decoding failure, and once
        inside predict_shapes, which performs the very same decode as
        auto labeling. An empty prediction is verified against a third
        decode, so a swallowed failure can never pass for an image
        without any object.
        """

        path = as_image_path(image_path)
        # the wrapper decodes the file by path once it receives the
        # path: the array of the precheck is never handed to the model
        precheck = load_image_rgb(path)
        placeholder = ensure_qimage(build_placeholder_qimage(path))
        try:
            result = self.model.predict_shapes(placeholder, path)
        except Exception as error:  # noqa: BLE001
            raise InferenceError(str(error)) from error
        if result is None:
            raise InferenceError("the model returned no result")
        shapes = list(getattr(result, "shapes", []) or [])
        if not shapes:
            # an empty prediction is legal (an image may hold no object)
            # but the wrapper also answers [] when its own decode broke:
            # only a second decode can tell both apart.
            verify_decoder_path(path, placeholder, expected=precheck)
            return shapes
        # the rows the multi label NMS produced for one box collapse into
        # the one box the canvas draws, with one label line per row
        return merge_multilabel_predictions(shapes)

    def model_info(self) -> Dict[str, Any]:
        """Return the model snapshot stored inside the validation report."""

        from .dataset import sha256_file

        return {
            "path": self.model_path,
            "sha256": sha256_file(self.model_path),
            "task": self.info["task"],
            "family": self.info["family"],
            "imgsz_meta": self.info["imgsz_meta"],
            "imgsz_session": self.info["imgsz_session"],
            "input_shape": self.input_shape,
            "stride": self.info["stride"],
            "batch": self.info["batch"],
            "dynamic": self.info["dynamic"],
            "names": list(self.info["names"]),
            "names_model": list(self.info["names"]),
            "classes": list(self.classes),
            "classes_count_match": bool(self.info["classes_count_match"]),
            "classes_name_diff": truncate_diff(self.name_diff),
            "warnings": list(self._warnings),
        }


def _multilabel_group_key(shape: Any) -> Any:
    """Return the key that groups the rows of one predicted box.

    The multi label NMS copies the very same box once per class, so the
    rows of one box carry coordinates that are identical down to the
    bit; rounding to six decimals absorbs the float noise of the tensor
    arithmetic without joining two boxes that really differ. Two rows
    of one shape type at the very same coordinates cannot describe two
    distinct objects either: their IoU is 1 and the NMS would have
    suppressed one of them.
    """

    points = tuple(
        (round(float(x), 6), round(float(y), 6))
        for x, y in shape_point_list(shape)
    )
    return (shape_type_of(shape), points)


def _multilabel_sort_key(shape: Any) -> Any:
    """Return the ranking key of one row inside its group.

    A missing score cannot be ranked: it sorts after every scored row.
    The label breaks the ties of two equal scores, therefore both the
    order and the primary label it picks are deterministic.
    """

    score = shape_score(shape)
    # the ascending order of -score puts the highest score first; a row
    # without a score can never outrank a scored one
    rank = float("inf") if score is None else -float(score)
    return (rank, shape_label(shape))


def merge_multilabel_predictions(shapes: Sequence[Any]) -> List[Any]:
    """Collapse the multi label rows of one predicted box into one shape.

    The multi label NMS of the engine answers one row per class whose
    score passed the fixed inference cut, all of them carrying the very
    same box. The canvas draws one box with one label line per row, so
    the rows of a box are folded into a single shape: the highest score
    of the group stays the label/score the judge reads, while the whole
    group is attached as the parallel, score descending lists labels and
    scores.

    A group of a single row is returned untouched: the payload of a
    single label prediction keeps exactly the keys it had before this
    function existed. A Shape object is reused in place (the two lists
    become dynamic attributes), a dict is copied so that the rows the
    caller built stay untouched. The groups keep the order of their
    first row, which is the score descending order of the NMS output.
    """

    groups: Dict[Any, List[Any]] = {}
    order: List[Any] = []
    for shape in shapes:
        key = _multilabel_group_key(shape)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(shape)

    merged: List[Any] = []
    for key in order:
        rows = sorted(groups[key], key=_multilabel_sort_key)
        primary = rows[0]
        if len(rows) == 1:
            merged.append(primary)
            continue
        labels = [shape_label(row) for row in rows]
        scores = [shape_score(row) for row in rows]
        if isinstance(primary, dict):
            primary = dict(primary)
            primary["labels"] = labels
            primary["scores"] = scores
        else:
            primary.labels = labels
            primary.scores = scores
        merged.append(primary)
    return merged


def build_runner_pool(
    model_path: str,
    classes: Sequence[str],
    workers: int,
    iou_threshold: float = 0.45,
    on_message: Optional[Any] = None,
    intra_op_threads: Optional[int] = None,
    inter_op_threads: Optional[int] = None,
) -> List[ModelRunner]:
    """Build one independent runner per inference worker.

    Every runner owns its own ONNX session, and a session is not thread
    safe for concurrent forward passes of unrelated images (its internal
    state and arenas are shared), therefore the pool is built upfront,
    once per run, and every thread later takes one runner for itself.
    """

    return [
        ModelRunner(
            model_path,
            classes,
            iou_threshold=iou_threshold,
            on_message=on_message,
            intra_op_threads=intra_op_threads,
            inter_op_threads=inter_op_threads,
        )
        for _ in range(max(1, int(workers)))
    ]


__all__ = [
    "InferenceError",
    "MODEL_DISPLAY_NAME",
    "MODEL_NAME",
    "NAME_DIFF_HINT",
    "ModelRunner",
    "ThreadedOnnxSession",
    "as_image_path",
    "build_placeholder_qimage",
    "build_runner_pool",
    "ensure_current_config_file",
    "ensure_qimage",
    "load_image_rgb",
    "merge_multilabel_predictions",
    "verify_decoder_path",
]
