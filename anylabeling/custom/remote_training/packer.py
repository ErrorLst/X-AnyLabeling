"""Manifest assembly, client side plan review and zip packing.

Covers spec §5.2.1 step 7 (streamed image hashing), spec §5.2.6 (label
byte freezing reused, never recomputed after the split), spec §5.2.9
(archive layout) and the client half of spec §4.1.2 / spec §4.1.6 (the
review matrix that must pass before anything is uploaded).

Nothing here writes into dataset_dir; images are opened for reading
only.
"""

from __future__ import annotations

import os
import os.path as osp
import re
import zipfile
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from anylabeling.custom.model_validation.dataset import sha256_file

from .converter import EMPTY_SHA256, FrozenLabel
from .scanner import IMAGE_EXTENSIONS
from .splitter import (
    SplitCheck,
    SplitResult,
    check_split,
    stats_from_assignments,
)

__all__ = [
    "ARCHIVE_FILENAME",
    "IMAGES_DIRNAME",
    "MANIFEST_FILENAME",
    "PackerRun",
    "PlanReview",
    "label_line_cardinality_issues",
    "ReviewIssue",
    "build_manifest",
    "manifest_bytes",
    "pack_archive",
    "read_manifest_bytes",
    "review_plan",
    "warning_info_issues",
]

MANIFEST_FILENAME = "manifest.json"
ARCHIVE_FILENAME = "archive.zip"
LABELS_DIRNAME = "labels"
IMAGES_DIRNAME = "images"

_SHA256_RE = re.compile("^[0-9a-f]{64}$")
_IMAGE_SUFFIXES = tuple(IMAGE_EXTENSIONS)
_MANIFEST_IMAGE_FIELDS = (
    "name",
    "sha256",
    "size",
    "split",
    "label_sha256",
    "label_size",
)
#: The four fields plan returns inside missing_images[] (spec §4.1.2).
MISSING_IMAGE_FIELDS = frozenset({"name", "split", "sha256", "size"})


@dataclass
class ReviewIssue:
    """One finding of the pre-packing review."""

    code: str
    summary: str
    details: List[str] = field(default_factory=list)
    blocking: bool = True

    def text(self) -> str:
        if self.details:
            return self.summary + "\n" + "\n".join(self.details)
        return self.summary


@dataclass
class PlanReview:
    """Outcome of review_plan()."""

    issues: List[ReviewIssue] = field(default_factory=list)
    missing_images: List[Dict[str, Any]] = field(default_factory=list)
    rejected: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(issue.blocking for issue in self.issues)

    def blocking_issues(self) -> List[ReviewIssue]:
        return [issue for issue in self.issues if issue.blocking]

    def info_issues(self) -> List[ReviewIssue]:
        return [issue for issue in self.issues if not issue.blocking]

    def status_lines(self) -> List[str]:
        return [issue.text() for issue in self.issues]


@dataclass
class PackerRun:
    """One dataset ready for manifest / archive construction."""

    dataset_dir: str = ""
    task: str = ""
    classes: List[str] = field(default_factory=list)
    classes_file: str = ""
    val_ratio: float = 0.0
    seed: int = 0
    split_strategy: str = "per_class"
    split_result: SplitResult = field(default_factory=SplitResult)
    files: List[str] = field(default_factory=list)
    image_paths: Dict[str, str] = field(default_factory=dict)
    image_sha256: Dict[str, str] = field(default_factory=dict)
    image_size: Dict[str, int] = field(default_factory=dict)
    image_classes: Dict[str, List[str]] = field(default_factory=dict)
    staging_dir: Optional[str] = None
    issues: List[str] = field(default_factory=list)
    info_lines: List[str] = field(default_factory=list)
    #: Cached views of split_result, kept private so that the public
    #: properties below are not shadowed by dataclass fields.
    _assignments: Dict[str, str] = field(default_factory=dict)
    _split_stats: Dict[str, Dict[str, int]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.refresh_split_views()

    def refresh_split_views(self) -> "PackerRun":
        """Recompute the cached assignment / split_stats views."""

        self._assignments = dict(self.split_result.assignments)
        self._split_stats = {
            name: row.to_dict()
            for name, row in self.split_result.split_stats.items()
        }
        return self

    @property
    def assignments(self) -> Dict[str, str]:
        return dict(self._assignments)

    @property
    def split_stats(self) -> Dict[str, Dict[str, int]]:
        return {name: dict(row) for name, row in self._split_stats.items()}

    def split_of(self, name: str) -> str:
        return self.split_result.assignments.get(name, "train")

    def split_check(self) -> SplitCheck:
        return check_split(self.split_result.assignments, self.files)

    def manifest(
        self, labels: Mapping[str, FrozenLabel]
    ) -> Dict[str, Any]:
        """The manifest object (plan request body) for this run."""

        return build_manifest(self, labels)

    def manifest_json_bytes(
        self, labels: Mapping[str, FrozenLabel]
    ) -> bytes:
        """The exact plan request body bytes (spec §5.4.1)."""

        return manifest_bytes(self, labels)

    def stems(self) -> List[str]:
        return [osp.splitext(name)[0] for name in self.files]

    def restricted_to(self, names: Sequence[str]) -> "PackerRun":
        """Keep only the given image names and rebuild split_stats.

        This is the local re-check of the second plan path (spec §5.4.3
        step 1): rejected entries disappear from the manifest together
        with their label_sha256 / label_size.
        """

        keep = set(names)
        files = [name for name in self.files if name in keep]
        assignments = {
            name: value
            for name, value in self.split_result.assignments.items()
            if name in keep
        }
        image_classes = {
            name: list(labels)
            for name, labels in self.image_classes.items()
            if name in keep
        }
        split_result = _rebuild_split_result(
            self.split_result, assignments, image_classes, self.classes
        )
        return PackerRun(
            dataset_dir=self.dataset_dir,
            task=self.task,
            classes=list(self.classes),
            classes_file=self.classes_file,
            val_ratio=self.val_ratio,
            seed=self.seed,
            split_strategy=self.split_strategy,
            split_result=split_result,
            files=files,
            image_paths={
                name: value
                for name, value in self.image_paths.items()
                if name in keep
            },
            image_sha256={
                name: value
                for name, value in self.image_sha256.items()
                if name in keep
            },
            image_size={
                name: value
                for name, value in self.image_size.items()
                if name in keep
            },
            image_classes=image_classes,
            staging_dir=self.staging_dir,
            issues=list(self.issues),
            info_lines=list(self.info_lines),
        )


def _rebuild_split_result(
    split_result: SplitResult,
    assignments: Mapping[str, str],
    image_classes: Mapping[str, Sequence[str]],
    classes: Sequence[str],
) -> SplitResult:
    """A copy of split_result with the given assignments / stats."""

    return SplitResult(
        assignments=dict(assignments),
        split_stats=stats_from_assignments(
            assignments, image_classes, classes
        ),
        nominal=dict(split_result.nominal),
        unresolved=list(split_result.unresolved),
        class_sample_total=split_result.class_sample_total,
        order=list(split_result.order),
        rounds=split_result.rounds,
    )


def _is_sha256_hex(digest: str) -> bool:
    """True for a 64 character lower case hex digest (spec §4.1.2)."""

    return bool(_SHA256_RE.match(digest or ""))


def _label_ref(
    run: PackerRun,
    labels: Mapping[str, FrozenLabel],
    name: str,
    blocks: List[ReviewIssue],
) -> Tuple[str, int]:
    """label_sha256 / label_size for one image, or a blocking issue."""

    stem = osp.splitext(name)[0]
    frozen = labels.get(stem)
    if frozen is None:
        blocks.append(
            ReviewIssue(
                code="MISSING_LABEL_BYTES",
                summary="本地缺少已冻结的标签字节，已停止打包",
                details=["- {0}（labels/{1}.txt）".format(name, stem)],
            )
        )
        return "", 0
    if not _is_sha256_hex(frozen.sha256):
        blocks.append(
            ReviewIssue(
                code="LABEL_SHA_INVALID",
                summary="标签哈希不是 64 位小写十六进制，已停止打包",
                details=["- {0}".format(name)],
            )
        )
        return "", 0
    if frozen.size == 0 and frozen.sha256 != EMPTY_SHA256:
        # A 0 byte label is the empty file: its hash is fixed (spec
        # §5.2.6 "不得跳过、也不得用其它替代值").
        blocks.append(
            ReviewIssue(
                code="LABEL_EMPTY_SHA_MISMATCH",
                summary=(
                    "0 字节标签的哈希不是空文件哈希，已停止打包"
                ),
                details=[
                    "- {0}（{1}）".format(name, frozen.sha256),
                ],
            )
        )
        return "", 0
    if not frozen.verify():
        blocks.append(
            ReviewIssue(
                code="LABEL_BYTES_CHANGED",
                summary=(
                    "标签字节在冻结之后被改写，已停止打包"
                    "（先冻结、后打包，中途不得再改写）"
                ),
                details=["- {0}（labels/{1}.txt）".format(name, stem)],
            )
        )
        return "", 0
    return frozen.sha256, int(frozen.size)


def build_manifest(
    run: PackerRun, labels: Mapping[str, FrozenLabel]
) -> Dict[str, Any]:
    """Assemble the manifest (plan request body, spec §4.1.2).

    Every images[] entry carries exactly the six declared fields; the
    order of images[] follows the sorted file list so two runs over the
    same dataset serialise to identical bytes.
    """

    blocks: List[ReviewIssue] = []
    images: List[Dict[str, Any]] = []
    for name in sorted(run.files):
        digest = run.image_sha256.get(name, "")
        if not _is_sha256_hex(digest):
            blocks.append(
                ReviewIssue(
                    code="IMAGE_SHA_INVALID",
                    summary="图片哈希不是 64 位小写十六进制，已停止打包",
                    details=["- {0}".format(name)],
                )
            )
            continue
        label_sha, label_size = _label_ref(run, labels, name, blocks)
        entry = {
            "name": name,
            "sha256": digest,
            "size": int(run.image_size.get(name, 0)),
            "split": run.split_of(name),
            "label_sha256": label_sha,
            "label_size": label_size,
        }
        assert tuple(entry.keys()) == _MANIFEST_IMAGE_FIELDS
        images.append(entry)
    if blocks:
        raise ValueError(
            "manifest 组装失败：\n"
            + "\n".join(issue.text() for issue in blocks)
        )
    manifest: Dict[str, Any] = {
        "schema_version": 1,
        "task": run.task,
        "val_ratio": run.val_ratio,
        "seed": run.seed,
        "split_strategy": run.split_strategy,
        "split_stats": run.split_stats,
        "classes": list(run.classes),
        "images": images,
    }
    return manifest


def manifest_bytes(
    run: PackerRun, labels: Mapping[str, FrozenLabel]
) -> bytes:
    """The exact plan request body bytes (spec §5.4.1 canonical JSON)."""

    from .store import canonical_json_bytes

    return canonical_json_bytes(build_manifest(run, labels))


def read_manifest_bytes(data: bytes) -> Dict[str, Any]:
    """Parse manifest.json back; a non object is an error."""

    import json

    payload = json.loads(data.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("manifest.json 顶层不是 JSON 对象")
    return payload


def label_line_cardinality_issues(
    run: PackerRun, labels: Mapping[str, FrozenLabel]
) -> List[ReviewIssue]:
    """Point set cardinality of the frozen label bytes (spec §5.2.4).

    The schema validation blocks a Detect rectangle whose point count is
    not in {2, 4} before the conversion; this is the second half of the
    "双保险" the same section asks for, evaluated on the bytes that
    really go into the zip:

    - Detect (hbb): every line has exactly five fields (cls cx cy w h);
    - Segment (seg): every line has 1 + 2k fields with k >= 3 points.

    A violation means the conversion output no longer matches the
    contract the server validates (a rectangle it would reject, or a
    polygon that lost its points), so it blocks the upload with the file
    and the line number instead of shipping it silently.
    """

    issues: List[ReviewIssue] = []
    seg = str(getattr(run, "task", "") or "").lower() == "segment"
    details: List[str] = []
    for stem in sorted(labels or ()):
        label = labels[stem]
        path = str(getattr(label, "stage_path", "") or "")
        if not path or not osp.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as handle:
                lines = handle.read().splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            details.append("- {0}（不可读：{1}）".format(stem, exc))
            continue
        for number, raw in enumerate(lines, start=1):
            text = raw.strip()
            if not text:
                continue
            count = len(text.split())
            if not seg:
                if count != 5:
                    details.append(
                        "- {0}:{1}（rectangle 行应为 5 字段，实际 {2}）"
                        .format(stem, number, count)
                    )
                continue
            if count < 7 or (count - 1) % 2:
                details.append(
                    "- {0}:{1}（polygon 行应为 1+2k 且 k ≥ 3，"
                    "实际 {2} 字段 / {3} 点）".format(
                        stem, number, count, (count - 1) // 2
                    )
                )
    if details:
        issues.append(
            ReviewIssue(
                code="LABEL_POINTS_CARDINALITY",
                summary=(
                    "标签行的点集基数不满足转换契约"
                    "（Detect 固定 5 字段；Segment 为 1+2k 且 k ≥ 3），"
                    "已停止上传"
                ),
                details=details,
            )
        )
    return issues


def review_plan(
    run: PackerRun,
    labels: Mapping[str, FrozenLabel],
    plan: Mapping[str, Any],
) -> PlanReview:
    """Client side review matrix before packing (spec §5.2.9, §4.1.2).

    Checks the plan response against the local run: entry level rejections,
    the six manifest fields, names that are unsafe as zip entry names,
    the name set of missing_images and the two sided split guarantee.
    """

    review = PlanReview()
    missing = _as_list(plan.get("missing_images"))
    rejected = _as_list(plan.get("rejected"))
    review.missing_images = [
        item for item in missing if isinstance(item, dict)
    ]
    review.rejected = [
        item for item in rejected if isinstance(item, dict)
    ]

    incomplete = [
        item
        for item in review.missing_images
        if not MISSING_IMAGE_FIELDS.issubset(set(item.keys()))
    ]
    if incomplete:
        review.issues.append(
            ReviewIssue(
                code="MISSING_ENTRY_FIELDS",
                summary=(
                    "plan 返回的 missing_images 条目缺少必需字段"
                    "（应为 name / split / sha256 / size），已停止打包"
                ),
                details=[
                    "- {0}（缺 {1}）".format(
                        item.get("name", "<unnamed>"),
                        ", ".join(
                            sorted(MISSING_IMAGE_FIELDS - set(item.keys()))
                        ),
                    )
                    for item in incomplete
                ],
            )
        )
    blocked_names: List[str] = []
    unsafe: List[str] = []
    for item in review.missing_images:
        name = item.get("name")
        if not isinstance(name, str) or not name:
            blocked_names.append(repr(name))
            continue
        if "/" in name or "\\" in name or osp.sep in name:
            unsafe.append(name)
        if name not in run.files:
            blocked_names.append(name)
        declared = item.get("sha256")
        local = run.image_sha256.get(name)
        if isinstance(declared, str) and local and declared != local:
            review.issues.append(
                ReviewIssue(
                    code="IMAGE_SHA_DIFFERS",
                    summary=(
                        "plan 返回的图片哈希与本地不一致"
                        "（服务端建议重传），已按本地哈希打包"
                    ),
                    details=["- {0}".format(name)],
                    blocking=False,
                )
            )
        declared_size = item.get("size")
        local_size = run.image_size.get(name)
        if (
            isinstance(declared_size, int)
            and local_size is not None
            and declared_size != local_size
        ):
            review.issues.append(
                ReviewIssue(
                    code="IMAGE_SIZE_DIFFERS",
                    summary=(
                        "plan 返回的图片字节数与本地不一致，"
                        "服务端会把它判为未命中并重传"
                    ),
                    details=["- {0}".format(name)],
                    blocking=False,
                )
            )
        declared_split = item.get("split")
        local_split = run.split_of(name)
        if isinstance(declared_split, str) and declared_split != local_split:
            # Not blocking: the zip path is written from the local
            # decision (the manifest is the authority), but a drift
            # must never stay silent.
            review.issues.append(
                ReviewIssue(
                    code="SPLIT_DIFFERS",
                    summary=(
                        "plan 返回的 split 与本地划分不一致，"
                        "仍按本地划分打包（zip 路径以本地为准）"
                    ),
                    details=[
                        "- {0}（plan={1}，本地={2}）".format(
                            name, declared_split, local_split
                        )
                    ],
                    blocking=False,
                )
            )

    if unsafe:
        review.issues.append(
            ReviewIssue(
                code="NAME_UNSAFE",
                summary=(
                    "plan 返回的图片名含目录分隔符，"
                    "无法作为扁平 zip 条目名，已停止打包"
                ),
                details=["- {0}".format(name) for name in sorted(unsafe)],
            )
        )
    if blocked_names:
        review.issues.append(
            ReviewIssue(
                code="MISSING_SET_MISMATCH",
                summary=(
                    "plan 返回了本地清单里不存在的图片，已停止打包"
                    "（本地清单与 plan 不一致）"
                ),
                details=["- {0}".format(name) for name in blocked_names],
            )
        )

    if review.rejected:
        names = [
            str(item.get("name") or "<unknown>") for item in review.rejected
        ]
        reasons = sorted(
            {
                str(item.get("reason") or "")
                for item in review.rejected
                if item.get("reason")
            }
        )
        review.issues.append(
            ReviewIssue(
                code="REJECTED_ENTRIES",
                summary=(
                    "服务端按条目级拒绝了 {0} 个条目，"
                    "需用户确认后剔除并二次 plan".format(len(names))
                ),
                details=["- {0}".format(name) for name in names]
                + (
                    ["原因：{0}".format(", ".join(reasons))]
                    if reasons
                    else []
                ),
                blocking=False,
            )
        )

    check = run.split_check()
    if check.blocked:
        review.issues.append(
            ReviewIssue(
                code="SPLIT_EMPTY",
                summary=check.reason(),
                details=[],
            )
        )

    for issue in warning_info_issues(plan.get("warnings")):
        review.issues.append(issue)
    for issue in _manifest_structure_issues(run):
        review.issues.append(issue)
    # The second half of the §5.2.4 "双保险": the cardinality of the
    # bytes that really go into the zip, not of the parsed .json.
    for issue in label_line_cardinality_issues(run, labels):
        review.issues.append(issue)
    return review


def warning_info_issues(warnings: Any) -> List[ReviewIssue]:
    """Server warnings as informational lines (spec §5.2.8).

    The same shape is used for the plan response and, by the uploader,
    for the upload response receipt (channel B, spec §3.9): an
    information line that never blocks.
    """

    issues: List[ReviewIssue] = []
    for item in _as_list(warnings):
        if isinstance(item, Mapping):
            code = str(item.get("code") or "WARNING")
            details = item.get("files") or item.get("details") or []
            extra: List[str] = []
            if isinstance(details, (list, tuple)):
                extra = ["- {0}".format(entry) for entry in details]
            elif details:
                extra = ["- {0}".format(details)]
            message = item.get("message")
            summary = (
                "{0}：{1}".format(code, message)
                if message
                else code
            )
            issues.append(
                ReviewIssue(
                    code=code,
                    summary=summary,
                    details=extra,
                    blocking=False,
                )
            )
        elif item:
            issues.append(
                ReviewIssue(
                    code="WARNING",
                    summary=str(item),
                    blocking=False,
                )
            )
    return issues


def _as_list(value: Any) -> List[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _manifest_structure_issues(run: PackerRun) -> List[ReviewIssue]:
    """Static checks on the manifest the client is about to send."""

    issues: List[ReviewIssue] = []
    if not run.classes:
        issues.append(
            ReviewIssue(
                code="CLASSES_EMPTY",
                summary="类别表为空，无法组装 manifest",
            )
        )
    seen_stems: Dict[str, List[str]] = {}
    bad_fields: List[str] = []
    bad_names: List[str] = []
    for name in run.files:
        stem = osp.splitext(name)[0]
        seen_stems.setdefault(stem, []).append(name)
        suffix = osp.splitext(name)[1]
        if suffix not in _IMAGE_SUFFIXES:
            bad_fields.append("- {0}（扩展名不在白名单）".format(name))
        if not name or "/" in name or "\\" in name:
            bad_names.append(name)
    collisions = {
        stem: names for stem, names in seen_stems.items() if len(names) > 1
    }
    if collisions:
        details = [
            "- {0}".format(" / ".join(sorted(collisions[stem])))
            for stem in sorted(collisions)
        ]
        issues.append(
            ReviewIssue(
                code="STEM_COLLISION",
                summary="清单内出现同 stem 的图片，标签文件会互相覆盖",
                details=details,
            )
        )
    if bad_names:
        issues.append(
            ReviewIssue(
                code="NAME_UNSAFE",
                summary="清单内出现含目录分隔符的图片名",
                details=["- {0}".format(name) for name in bad_names],
            )
        )
    if bad_fields:
        issues.append(
            ReviewIssue(
                code="EXTENSION_NOT_ALLOWED",
                summary="清单内出现不在扩展名白名单内的图片",
                details=bad_fields,
            )
        )
    return issues


def pack_archive(
    run: PackerRun,
    labels: Mapping[str, FrozenLabel],
    missing_images: Sequence[Mapping[str, Any]],
    manifest_json: bytes,
    work_dir: str,
    *,
    archive_path: Optional[str] = None,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> str:
    """Write the zip of spec §5.2.9 and return its path.

    Layout: manifest.json (the frozen plan bytes, written verbatim),
    images/<split>/<name> for the missing entries only, and the full
    labels/<split>/<stem>.txt set from the frozen bytes.  Every label is
    re-hashed while packing and compared against the manifest
    (cheap self check of spec §5.2.6).
    """

    if not manifest_json:
        raise ValueError("manifest.json 字节为空，拒绝打包")
    declared = read_manifest_bytes(manifest_json)
    by_name: Dict[str, Mapping[str, Any]] = {}
    for item in declared.get("images") or []:
        if isinstance(item, Mapping) and isinstance(item.get("name"), str):
            by_name[item["name"]] = item

    missing: List[Mapping[str, Any]] = []
    for item in missing_images:
        if not isinstance(item, Mapping):
            raise ValueError("missing_images 条目不是对象")
        missing.append(item)

    # The archive always lands under work_dir (never in the process
    # working directory, which may be the repository).
    if archive_path and osp.dirname(archive_path):
        archive = archive_path
    elif archive_path:
        archive = osp.join(work_dir, osp.basename(archive_path))
    else:
        archive = osp.join(work_dir, ARCHIVE_FILENAME)
    tmp_path = archive + ".tmp"
    os.makedirs(osp.dirname(archive) or work_dir, exist_ok=True)
    total = len(missing) + len(run.files) + 1
    done = 0

    def advance(name: str) -> None:
        nonlocal done
        done += 1
        if on_progress is not None:
            on_progress(done, total, name)

    try:
        with zipfile.ZipFile(
            tmp_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True
        ) as archive_handle:
            archive_handle.writestr(MANIFEST_FILENAME, manifest_json)
            advance(MANIFEST_FILENAME)

            seen: set = set()
            for item in missing:
                if should_stop is not None and should_stop():
                    raise InterruptedError("打包已取消")
                name = item.get("name")
                if not isinstance(name, str) or not name:
                    raise ValueError("missing_images 缺少 name")
                if name in seen:
                    raise ValueError(
                        "missing_images 出现重复条目：{0}".format(name)
                    )
                seen.add(name)
                source = run.image_paths.get(name)
                if source is None:
                    raise ValueError(
                        "missing_images 含本地不存在的图片：{0}".format(name)
                    )
                split = run.split_of(name)
                archive_handle.write(
                    source, "{0}/{1}/{2}".format(IMAGES_DIRNAME, split, name)
                )
                advance(name)

            for name in sorted(run.files):
                if should_stop is not None and should_stop():
                    raise InterruptedError("打包已取消")
                stem = osp.splitext(name)[0]
                frozen = labels.get(stem)
                if frozen is None:
                    raise ValueError(
                        "缺少已冻结的标签文件：{0}".format(name)
                    )
                expected = by_name.get(name, {})
                digest = sha256_file(frozen.stage_path)
                declared_hash = expected.get("label_sha256")
                if declared_hash and digest != declared_hash:
                    raise ValueError(
                        "标签冻结后被改写：{0}（labels/{1}.txt）".format(
                            name, stem
                        )
                    )
                split = run.split_of(name)
                archive_handle.write(
                    frozen.stage_path,
                    "{0}/{1}/{2}".format(LABELS_DIRNAME, split, stem + ".txt"),
                )
                advance(name)
        os.replace(tmp_path, archive)
    except BaseException:
        try:
            if osp.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise
    return archive
