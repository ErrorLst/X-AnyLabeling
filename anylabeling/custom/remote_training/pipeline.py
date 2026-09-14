"""Scan to archive pipeline (spec §5.2.1 steps 1 to 7 and 10).

This is the only place that sequences the four data pipeline modules.
It performs no network I/O: the plan request (step 8), the rejected
entry handling (step 9), the persistence into pending/ (step 11) and
the upload (step 12) belong to the uploader (B4).

Everything the pipeline writes lands in staging_dir; dataset_dir is
only ever read.
"""

from __future__ import annotations

import dataclasses
import json
import os.path as osp
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from anylabeling.custom.model_validation.dataset import sha256_file

from .converter import convert_labels
from .packer import PackerRun
from .scanner import (
    DatasetScan,
    ScanIssue,
    protocol_task,
    scan_dataset,
    task_to_mode,
)
from .splitter import (
    SPLIT_STRATEGY,
    VAL_RATIO_WARN_DELTA,
    ConfigImportError,
    SplitCheck,
    SplitPreview,
    SplitResult,
    check_split,
    export_config,
    generate_seed,
    import_config,
    split_dataset,
    validate_val_ratio,
)
from .store import create_staging_root

__all__ = [
    "Pipeline",
    "PipelineConfig",
    "PipelineError",
    "assemble_run",
    "build_split_preview",
    "dataset_image_labels",
]


class PipelineError(RuntimeError):
    """A blocking local condition (状态行文案在 issues 里)."""

    def __init__(self, message: str, issues: Sequence[Any] = ()) -> None:
        super().__init__(message)
        self.issues: List[Any] = list(issues)

    def status_text(self) -> str:
        lines = [str(self)]
        lines.extend(_issue_text(issue) for issue in self.issues)
        return "\n".join(line for line in lines if line)


def _issue_text(issue: Any) -> str:
    text = getattr(issue, "text", None)
    if callable(text):
        return str(text())
    return str(issue)


#: The PipelineConfig fields an imported config may overwrite.
PIPELINE_CONFIG_KEYS = (
    "dataset_dir",
    "classes_file",
    "task",
    "val_ratio",
    "seed",
)


@dataclass
class PipelineConfig:
    """Everything steps 1 to 7 need (spec §5.2.1).

    task accepts both spellings: the UI one (Detect / Segment) and the
    protocol one (detect / segment).  The UI value is stored as given
    and normalised only where the contract demands it, i.e. in the
    manifest (spec §4.1.2) and in the exported configuration
    (spec §5.2.8).
    """

    dataset_dir: str = ""
    classes_file: str = ""
    task: str = "Detect"
    val_ratio: float = 0.2
    seed: Optional[int] = None

    def mode(self) -> str:
        """Converter mode (hbb / seg) for this task (spec §5.2.2)."""

        return task_to_mode(self.task)

    def protocol_task(self) -> str:
        """Protocol task value (detect / segment, spec §4.1.2)."""

        return protocol_task(self.task)


@dataclass
class Pipeline:
    """Stateless orchestrator with a lazily created staging root."""

    config: PipelineConfig = field(default_factory=PipelineConfig)
    staging_parent: Optional[str] = None
    staging_dir: Optional[str] = None
    split_result: Optional[SplitResult] = None
    split_preview: Optional[SplitPreview] = None
    split_check: Optional[SplitCheck] = None
    labels: Dict[str, Any] = field(default_factory=dict)

    def conversion_labels(self) -> Dict[str, Any]:
        """Frozen label entries keyed by stem (spec §5.2.6)."""

        return dict(self.labels)

    def ensure_staging_dir(self) -> str:
        if not self.staging_dir:
            self.staging_dir = create_staging_root(
                parent=self.staging_parent
            )
        return self.staging_dir

    def effective_seed(
        self,
        *,
        randbelow: Optional[Callable[[int], int]] = None,
    ) -> int:
        """The seed actually used; an empty box is filled in once."""

        if self.config.seed is None:
            self.config.seed = generate_seed(randbelow)
        return int(self.config.seed)

    def validate_config(self) -> None:
        """Entry gate: a bad task / val_ratio never reaches the manifest.

        export_config and import_config both validate, but the
        PipelineConfig built by the form (or edited in code) must be
        validated too, otherwise an illegal value would be serialised
        into the plan request and rejected with 400 VALIDATION_FAILED.
        """

        try:
            validate_val_ratio(self.config.val_ratio)
            task_to_mode(self.config.task)
        except (ConfigImportError, ValueError) as exc:
            raise PipelineError(
                "配置无效，已停止上传（未发起任何请求）",
                [ScanIssue(code="INVALID_CONFIG", summary=str(exc))],
            ) from exc

    def scan(self) -> DatasetScan:
        self.validate_config()
        return scan_dataset(
            self.config.dataset_dir,
            classes_file=self.config.classes_file,
            task=self.config.task,
        )

    def assemble(
        self,
        scan: Optional[DatasetScan] = None,
        *,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> PackerRun:
        """Steps 1 to 7: scan, convert, split, manifest and image hashes."""

        self.validate_config()
        if scan is None:
            scan = self.scan()
        if scan.blocked:
            raise PipelineError(
                "数据集预检未通过，已停止上传（未发起任何请求）",
                scan.blocking(),
            )
        staging_dir = self.ensure_staging_dir()
        conversion = convert_labels(
            scan,
            staging_dir,
            classes_file=self.config.classes_file,
            task=self.config.task,
            should_stop=should_stop,
        )
        if conversion.blocked:
            raise PipelineError(
                "标签转换失败，已停止上传（未发起任何请求）",
                conversion.issues,
            )

        #: The manifest is keyed by stem (labels/<split>/<stem>.txt), so
        #: the frozen bytes are held by stem from here on.
        labels = conversion.by_stem
        self.labels = dict(labels)
        pairs = [p for p in scan.pairs if p.stem in labels]
        image_labels = dataset_image_labels(
            scan, pairs, self.config.task
        )
        seed = self.effective_seed()
        split_result = split_dataset(
            image_labels,
            scan.classes,
            self.config.val_ratio,
            seed,
        )
        self.split_result = split_result

        image_paths: Dict[str, str] = {}
        image_sha256: Dict[str, str] = {}
        image_size: Dict[str, int] = {}
        info_lines: List[str] = []
        for pair in sorted(pairs, key=lambda item: item.name):
            if should_stop is not None and should_stop():
                raise PipelineError("打包前被取消", [])
            path = pair.image_path
            try:
                size = osp.getsize(path)
                digest = sha256_file(path)
            except OSError as exc:
                raise PipelineError(
                    "图片文件不可读，已停止上传（未发起任何请求）",
                    [
                        ScanIssue(
                            code="UNREADABLE_IMAGE",
                            summary="打包过程中有文件不可读，已停止",
                            details=["- {0}（{1}）".format(pair.name, exc)],
                        )
                    ],
                ) from exc
            image_paths[pair.name] = path
            image_sha256[pair.name] = digest
            image_size[pair.name] = size

        for message in (
            conversion.background_message(),
            conversion.skip_message(),
            conversion.all_skipped_message(),
            conversion.downgraded_message(),
        ):
            if message:
                info_lines.append(message)

        check = check_split(
            split_result.assignments, sorted(image_paths)
        )
        self.split_check = check
        run = PackerRun(
            dataset_dir=osp.abspath(self.config.dataset_dir),
            task=self.config.protocol_task(),
            classes=list(scan.classes),
            classes_file=str(self.config.classes_file or ""),
            val_ratio=float(self.config.val_ratio),
            seed=seed,
            split_strategy=SPLIT_STRATEGY,
            split_result=split_result,
            files=sorted(image_paths),
            image_paths=image_paths,
            image_sha256=image_sha256,
            image_size=image_size,
            image_classes={
                name: list(labels_for)
                for name, labels_for in image_labels.items()
            },
            staging_dir=staging_dir,
            issues=[],
            info_lines=info_lines,
        )
        self.split_preview = build_split_preview(run, split_result)
        return run

    def prepare(
        self, *, should_stop: Optional[Callable[[], bool]] = None
    ) -> PackerRun:
        """assemble() on a fresh scan, then the two sided check."""

        run = self.assemble(should_stop=should_stop)
        check = run.split_check()
        self.split_check = check
        if check.blocked:
            issues = [
                ScanIssue(
                    code="SPLIT_EMPTY",
                    summary=check.reason(),
                    details=[],
                )
            ]
            raise PipelineError(
                "划分未通过，已停止上传（未发起任何请求）", issues
            )
        return run

    def export_config(self, values: Mapping[str, Any]) -> Dict[str, Any]:
        """Export JSON; the task is written protocol style (§5.2.8)."""

        payload = dict(values)
        if payload.get("task"):
            payload["task"] = protocol_task(payload["task"])
        return export_config(payload)

    def import_config(
        self, text: str, *, apply: bool = True
    ) -> Dict[str, Any]:
        """Import a configuration; a Token in the file is dropped."""

        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise PipelineError(
                "配置文件不是合法 JSON：{0}".format(exc)
            ) from exc
        values = import_config(payload)
        if not apply:
            return values
        # Only the keys this pipeline owns are applied; the remaining
        # form fields (server_url / model / params) stay with the UI,
        # which owns those widgets (spec §5.2.8).
        changes = {
            key: values[key]
            for key in PIPELINE_CONFIG_KEYS
            if key in values
        }
        self.config = dataclasses.replace(self.config, **changes)
        # An imported file must not be able to install a configuration
        # the plan request would reject.
        self.validate_config()
        return values


def dataset_image_labels(
    scan: DatasetScan,
    pairs: Sequence[Any],
    task: Optional[str] = None,
) -> Dict[str, List[str]]:
    """Convertible class list C_img per image (spec §5.2.6).

    Only objects that actually produced a label line count: shapes the
    converter skips must not influence the split.
    """

    mode = task_to_mode(task or "Detect")
    classes = set(scan.classes)
    result: Dict[str, List[str]] = {}
    for pair in pairs:
        data = pair.label_data or {}
        shapes = data.get("shapes")
        labels: List[str] = []
        for shape in shapes or []:
            if not isinstance(shape, Mapping):
                continue
            label = shape.get("label")
            if not isinstance(label, str) or label not in classes:
                continue
            shape_type = shape.get("shape_type")
            points = shape.get("points")
            if not isinstance(points, (list, tuple)):
                continue
            if mode == "hbb":
                if shape_type != "rectangle" or len(points) not in (2, 4):
                    continue
            elif mode == "seg":
                if shape_type != "polygon" or len(points) < 3:
                    continue
            else:
                continue
            if label not in labels:
                labels.append(label)
        result[pair.name] = labels
    return result


def build_split_preview(
    run: PackerRun, split_result: SplitResult
) -> SplitPreview:
    """Preview numbers of spec §5.2.8 (counts, warnings, actual ratio)."""

    preview = SplitPreview(
        total_images=len(run.files),
        val_total=split_result.val_total,
        val_ratio_target=run.val_ratio,
        nominal_total=split_result.nominal_total,
        split_stats=run.split_stats,
        unresolved=list(split_result.unresolved),
    )
    for name in split_result.order:
        row = split_result.split_stats.get(name)
        if row is None:
            continue
        if row.train + row.val >= 1 and row.val == 0:
            preview.warnings.append(
                "类别 {0} 未出现在验证侧（val=0）".format(name)
            )
    if split_result.unresolved:
        preview.warnings.append(
            "两侧代表无法保证的类别：{0}".format(
                ", ".join(split_result.unresolved)
            )
        )
    if run.files and abs(
        preview.actual_val_ratio - run.val_ratio
    ) > VAL_RATIO_WARN_DELTA:
        preview.warnings.append(
            "实际 val 占比 {0:.1%} 与目标 {1:.1%} 相差较大，"
            "可调整 val_ratio 或换 seed 重算".format(
                preview.actual_val_ratio, run.val_ratio
            )
        )
    return preview


def assemble_run(
    config: PipelineConfig,
    *,
    staging_parent: Optional[str] = None,
    randbelow: Optional[Callable[[int], int]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> PackerRun:
    """Convenience wrapper: Pipeline(config).prepare()."""

    pipeline = Pipeline(config=config, staging_parent=staging_parent)
    if config.seed is None:
        pipeline.config.seed = generate_seed(randbelow)
    return pipeline.prepare(should_stop=should_stop)
