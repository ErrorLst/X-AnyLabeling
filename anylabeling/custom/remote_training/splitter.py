"""Deterministic per-class stratified split (spec §5.2.7).

The module is the single implementation of the split algorithm and is
deliberately dependency-free: it imports nothing beyond the standard
library, so the pure functions can run under any interpreter (including
/usr/bin/python3 without third party packages).

Inputs are the per-image convertible class sets C_img (spec §5.2.6) plus
the classes order, val_ratio and seed.  Outputs are a per-image
assignment, the reported split_stats rows, the unresolved "both sides"
list and the preview numbers (spec §5.2.8).
"""

from __future__ import annotations

import hashlib
import random
import secrets
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

__all__ = [
    "SAFETY_NET_ROUNDS",
    "SPLIT_STRATEGY",
    "SPLITS",
    "ConfigImportError",
    "SplitCheck",
    "SplitPreview",
    "SplitResult",
    "assign_splits",
    "check_split",
    "export_config",
    "format_split_stats_lines",
    "generate_seed",
    "import_config",
    "split_class_samples",
    "split_dataset",
    "stats_from_assignments",
    "validate_val_ratio",
]

#: Fixed safety-net iteration cap (spec §5.2.7 step 5).
SAFETY_NET_ROUNDS = 3
#: The only accepted split strategy (spec §4.1.2).
SPLIT_STRATEGY = "per_class"
#: The two split prefixes (spec §4.1.2).
SPLITS: Tuple[str, str] = ("train", "val")
#: val_ratio difference above which the preview warns (spec §5.2.8).
VAL_RATIO_WARN_DELTA = 0.10
#: The biggest seed generate_seed() can return (spec §5.2.8).
SEED_UPPER_BOUND = 2**31 - 1

#: Keys of the exported / imported training configuration, in export
#: order (spec §5.2.8).  Anything else - most notably the Token kept in
#: server.json - is deliberately absent.
CONFIG_KEYS: Tuple[str, ...] = (
    "schema_version",
    "server_url",
    "dataset_dir",
    "classes_file",
    "task",
    "model_family",
    "model",
    "val_ratio",
    "seed",
    "split_strategy",
    "params",
)
CONFIG_SCHEMA_VERSION = 1


class ConfigImportError(ValueError):
    """The imported configuration file cannot be read as a config."""


@dataclass
class SplitClassStats:
    """One split_stats row (spec §4.1.2, spec §5.2.8)."""

    train: int = 0
    val: int = 0

    def to_dict(self) -> Dict[str, int]:
        return {"train": self.train, "val": self.val}


@dataclass
class SplitResult:
    """Outcome of assign_splits()."""

    assignments: Dict[str, str] = field(default_factory=dict)
    split_stats: Dict[str, SplitClassStats] = field(default_factory=dict)
    nominal: Dict[str, int] = field(default_factory=dict)
    unresolved: List[str] = field(default_factory=list)
    class_sample_total: int = 0
    order: List[str] = field(default_factory=list)
    rounds: int = 0

    @property
    def val_total(self) -> int:
        return sum(1 for value in self.assignments.values() if value == "val")

    @property
    def nominal_total(self) -> int:
        """Sum of the per-class nominal quotas (upper bound, §5.2.8)."""
        return sum(self.nominal.values())


def split_class_samples(
    image_labels: Mapping[str, Sequence[str]],
    classes: Sequence[str],
) -> Dict[str, List[str]]:
    """I_c per class, each list sorted by image name ascending."""

    per_class: Dict[str, List[str]] = {name: [] for name in classes}
    for image, labels in image_labels.items():
        for label in labels:
            if label in per_class:
                per_class[label].append(image)
    for name in per_class:
        per_class[name].sort()
    return per_class


def _derive_seed(seed: int, class_name: str) -> int:
    """seed_c = first 8 bytes of sha256("<seed>:<class>") big endian."""

    digest = hashlib.sha256(
        f"{seed}:{class_name}".encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:8], "big")


def assign_splits(
    image_labels: Mapping[str, Sequence[str]],
    classes: Sequence[str],
    val_ratio: float,
    seed: int,
    *,
    order: Optional[Sequence[str]] = None,
) -> SplitResult:
    """Run the spec §5.2.7 algorithm and return the raw outcome.

    Only images listed in the result's assignments belong to val; every
    other image goes to train (spec §5.2.7 step 3).
    """

    class_order = [str(name) for name in classes]
    per_class = split_class_samples(image_labels, classes)

    if order is None:
        index = {name: position for position, name in enumerate(class_order)}
        ordered = sorted(
            class_order, key=lambda c: (len(per_class[c]), index[c])
        )
    else:
        ordered = [str(name) for name in order]

    shuffled: Dict[str, List[str]] = {}
    for name in class_order:
        base = sorted(per_class[name])
        random.Random(_derive_seed(seed, name)).shuffle(base)
        shuffled[name] = base

    # Step 3: assignment is created once, before the loop.
    assignment: Dict[str, str] = {}
    nominal: Dict[str, int] = {}

    for name in ordered:
        images = per_class[name]
        inherited = sum(
            1 for img in images if assignment.get(img) == "val"
        )
        quota = round(len(images) * val_ratio)
        if len(images) == 1:
            quota = 0
        elif len(images) >= 2:
            quota = min(max(quota, 1), len(images) - 1)
        nominal[name] = quota
        need = max(0, quota - inherited)
        for img in shuffled[name]:
            if need == 0:
                break
            if img not in assignment:
                assignment[img] = "val"
                need -= 1

    def val_count(name: str) -> int:
        return sum(
            1 for img in per_class[name] if assignment.get(img) == "val"
        )

    def is_safe(img: str, name: str) -> bool:
        for other in class_order:
            if other == name or len(per_class[other]) < 2:
                continue
            if img in per_class[other] and val_count(other) <= 1:
                return False
        return True

    # Step 5: safety net, at most SAFETY_NET_ROUNDS full rechecks.
    unresolved: List[str] = []
    rounds = 0
    for _ in range(SAFETY_NET_ROUNDS):
        rounds += 1
        unresolved = [
            name
            for name in unresolved
            if len(per_class[name]) - val_count(name) == 0
        ]
        fixed = False
        for name in class_order:
            total = len(per_class[name])
            current_val = val_count(name)
            train = total - current_val
            if total >= 2 and train == 0 and current_val > 0:
                pool = [
                    img
                    for img in reversed(shuffled[name])
                    if assignment.get(img) == "val"
                ]
                candidate = next(
                    (img for img in pool if is_safe(img, name)), None
                )
                if candidate is None:
                    if name not in unresolved:
                        unresolved.append(name)
                    continue
                del assignment[candidate]
                fixed = True
        if not fixed:
            break
    unresolved = [
        name
        for name in unresolved
        if len(per_class[name]) - val_count(name) == 0
    ]

    split_stats: Dict[str, SplitClassStats] = {}
    class_sample_total = 0
    for name in class_order:
        total = len(per_class[name])
        current_val = val_count(name)
        class_sample_total += total
        split_stats[name] = SplitClassStats(
            train=total - current_val, val=current_val
        )

    return SplitResult(
        assignments=dict(assignment),
        split_stats=split_stats,
        nominal=nominal,
        unresolved=list(unresolved),
        class_sample_total=class_sample_total,
        order=list(ordered),
        rounds=rounds,
    )


def split_dataset(
    image_labels: Mapping[str, Sequence[str]],
    classes: Sequence[str],
    val_ratio: float,
    seed: int,
    *,
    order: Optional[Sequence[str]] = None,
) -> SplitResult:
    """The canonical entry point of spec §5.2.7 (step 7 discipline).

    Determinism only holds for a well formed
    (dataset, classes order, val_ratio, seed) tuple, so the tuple is
    validated here before the algorithm runs: 0 < val_ratio < 1, a
    non-empty and duplicate free class list, and an integer seed.  All
    callers (pipeline, export/import round trip, UI preview) therefore
    share the same gate and cannot introduce a value the server would
    reject with 400 VALIDATION_FAILED.
    """

    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ConfigImportError("seed 必须是整数")
    ratio = validate_val_ratio(val_ratio)
    class_order = [str(name) for name in classes]
    if not class_order:
        raise ConfigImportError("classes 为空，无法划分")
    if len(set(class_order)) != len(class_order):
        raise ConfigImportError("classes 含重复类别，无法划分")
    return assign_splits(
        image_labels, class_order, ratio, int(seed), order=order
    )


def format_split_stats_lines(
    split_stats: Mapping[str, Mapping[str, int]],
) -> List[str]:
    """One "- <class>: train=N / val=M" line per class, in order."""

    return [
        "- {0}: train={1} / val={2}".format(
            name, row.get("train", 0), row.get("val", 0)
        )
        for name, row in split_stats.items()
    ]


def stats_from_assignments(
    assignments: Mapping[str, str],
    image_classes: Mapping[str, Sequence[str]],
    classes: Sequence[str],
) -> Dict[str, SplitClassStats]:
    """Full split_stats rows for a class list (zero rows included)."""

    rows: Dict[str, SplitClassStats] = {
        name: SplitClassStats() for name in classes
    }
    for image, value in assignments.items():
        side = "val" if value == "val" else "train"
        for name in image_classes.get(image, ()):
            row = rows.get(name)
            if row is None:
                continue
            if side == "val":
                row.val += 1
            else:
                row.train += 1
    return rows


@dataclass
class SplitPreview:
    """Everything the split preview table needs (spec §5.2.8)."""

    total_images: int = 0
    val_total: int = 0
    val_ratio_target: float = 0.0
    nominal_total: int = 0
    split_stats: Dict[str, Dict[str, int]] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    unresolved: List[str] = field(default_factory=list)

    @property
    def actual_val_ratio(self) -> float:
        if not self.total_images:
            return 0.0
        return self.val_total / self.total_images

    def format_lines(self) -> List[str]:
        """User visible preview block (text only, no Qt)."""

        lines = [
            "划分预览：共 {0} 张图片，val {1} 张（实际占比 {2:.1%}，"
            "目标 {3:.1%}，各类名义配额之和 {4}）".format(
                self.total_images,
                self.val_total,
                self.actual_val_ratio,
                self.val_ratio_target,
                self.nominal_total,
            )
        ]
        lines.extend(format_split_stats_lines(self.split_stats))
        if self.unresolved:
            lines.append(
                "两侧代表无法保证的类别：{0}".format(
                    ", ".join(self.unresolved)
                )
            )
        lines.extend(self.warnings)
        return lines


@dataclass
class SplitCheck:
    """Global two-sided guarantee of spec §5.2.7 step 6."""

    train_total: int = 0
    val_total: int = 0
    problems: List[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return bool(self.problems)

    def reason(self) -> str:
        return "\n".join(self.problems)


def check_split(
    assignments: Mapping[str, str],
    files: Optional[Sequence[str]] = None,
) -> SplitCheck:
    """Block when either side is empty (spec §5.2.7 step 6).

    assignments only lists the images that went to val (spec
    §5.2.7 step 3), so the caller must pass the full image list;
    without it the assignment keys themselves are counted.
    """

    names = list(files) if files is not None else list(assignments)
    val_total = sum(
        1 for name in names if assignments.get(name) == "val"
    )
    train_total = len(names) - val_total
    problems: List[str] = []
    if not train_total:
        problems.append("划分结果中训练侧（train）为空，已停止上传")
    if not val_total:
        problems.append("划分结果中验证侧（val）为空，已停止上传")
    return SplitCheck(
        train_total=train_total, val_total=val_total, problems=problems
    )


def generate_seed(
    randbelow: Optional[Callable[[int], int]] = None
) -> int:
    """Seed for an empty seed box (spec §5.2.8): secrets.randbelow(2**31-1)."""

    draw = randbelow or secrets.randbelow
    return int(draw(SEED_UPPER_BOUND))


def validate_val_ratio(value: Any) -> float:
    """Return val_ratio as a float; raise ConfigImportError otherwise.

    This is the single 0 < x < 1 gate of the client (spec §4.1.2): the
    form, the imported configuration and the manifest entry point all
    call it, so an illegal value can never reach the plan request.
    """

    if isinstance(value, bool):
        raise ConfigImportError("val_ratio 必须是 0 与 1 之间的数值")
    try:
        ratio = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigImportError("val_ratio 必须是 0 与 1 之间的数值") from exc
    if not 0.0 < ratio < 1.0:
        raise ConfigImportError("val_ratio 必须满足 0 < val_ratio < 1")
    return ratio


def _coerce_seed(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigImportError("seed 必须是整数")
    return int(value)


def export_config(values: Mapping[str, Any]) -> Dict[str, Any]:
    """Build the export JSON (spec §5.2.8).

    val_ratio / seed / split_strategy are mandatory because the split is
    only reproducible for the same tuple (spec §5.2.7 step 7).  A Token
    or api_key in the input is dropped: server.json never leaves.
    """

    missing = [
        key
        for key in ("val_ratio", "seed")
        if values.get(key) is None
    ]
    if missing:
        raise ConfigImportError(
            "缺少必含键：{0}".format(", ".join(missing))
        )
    payload: Dict[str, Any] = {
        "schema_version": int(
            values.get("schema_version") or CONFIG_SCHEMA_VERSION
        ),
        "server_url": str(values.get("server_url") or ""),
        "dataset_dir": str(values.get("dataset_dir") or ""),
        "classes_file": str(values.get("classes_file") or ""),
        "task": str(values.get("task") or ""),
        "model_family": str(values.get("model_family") or ""),
        "model": str(values.get("model") or ""),
        "val_ratio": validate_val_ratio(values.get("val_ratio")),
        "seed": _coerce_seed(values.get("seed")),
        "split_strategy": SPLIT_STRATEGY,
    }
    params = values.get("params")
    payload["params"] = (
        dict(params) if isinstance(params, Mapping) else {}
    )
    return payload


def import_config(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Read an exported configuration; unknown keys never survive.

    In particular a Token / api_key present in the file is dropped, so an
    import can never carry a token into the form (spec §5.2.8).
    """

    if not isinstance(payload, Mapping):
        raise ConfigImportError("配置文件必须是 JSON 对象")
    result: Dict[str, Any] = {}
    for key in CONFIG_KEYS:
        if key in payload:
            result[key] = payload[key]
    if "val_ratio" not in result:
        raise ConfigImportError("配置文件缺少必含键 val_ratio")
    if "seed" not in result:
        raise ConfigImportError("配置文件缺少必含键 seed")
    result["val_ratio"] = validate_val_ratio(result["val_ratio"])
    result["seed"] = _coerce_seed(result["seed"])
    strategy = result.get("split_strategy", SPLIT_STRATEGY)
    if strategy != SPLIT_STRATEGY:
        raise ConfigImportError(
            "split_strategy 只支持 {0}".format(SPLIT_STRATEGY)
        )
    result["split_strategy"] = SPLIT_STRATEGY
    params = result.get("params")
    result["params"] = dict(params) if isinstance(params, Mapping) else {}
    return result
