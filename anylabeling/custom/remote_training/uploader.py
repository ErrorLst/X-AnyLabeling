"""Two-phase upload and job submission (spec §4.1, §5.4.1 to §5.4.5).

This module owns the client side state machine between the data pipeline
and the HTTP client:

* 'plan' (stage 1) and the second plan of spec §5.4.3,
* the 'planned -> uploading' transition with its "persist first, then
  send" ordering,
* the replay of spec §5.4.1 (same token, same 'archive.zip'),
* the only failure transition table of spec §5.4.4 (all 17 rows),
* 'POST /jobs' submission with 'client_submission_id' de-duplication,
* the crash reconciliation of all six phases.

Boundaries: no data pipeline logic (B3), no Qt (B5), no polling (B6) and
no result download (B7).  Every disk write goes through the store
module's atomic helpers; the retry ladder of spec §5.5.6 is never
re-implemented here (only the three exceptions of spec §5.4.4 latch a
delay).

The packer is injected as a :class:'Packer' so that this module stays
free of scan / convert / pack sequencing and can be driven by an offline
fixture plus a fake server (spec §6.5).
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import os.path as osp
import re
import secrets
import shutil
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from . import api_client as api
from . import store as store_mod
from .store import (
    VOID_REASONS,
    PendingSubmission,
    PendingUpload,
    Store,
    TaskRecord,
    TasksLedger,
    lock_hash,
    parse_iso8601_epoch,
    pending_dir_name,
    sha256_hex,
    utc_now,
    utc_now_iso,
)

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "ARCHIVE_FILENAME",
    "AUTH_MESSAGE",
    "CLIENT_SUBMISSION_PREFIX",
    "CleanupReport",
    "DEFAULT_ARCHIVE_NAME",
    "EntriesReport",
    "JOB_ARTIFACTS_EXPIRED_IS_NOT_SUBMIT",
    "MANIFEST_FILENAME",
    "META_FILENAME",
    "PACKER_INSTALL_HINT",
    "PackOutcome",
    "Packer",
    "PhaseResult",
    "PlanOutcome",
    "QUOTA_MESSAGE",
    "QUOTA_RETRY_MESSAGE",
    "RETRY_AFTER_UPLOAD_IN_PROGRESS",
    "SECOND_PLAN_PROMPT",
    "STALLED_PREFIX",
    "SUBMIT_REQUEST_FILENAME",
    "SUBMIT_RETRY_HINT",
    "SubmissionConflictError",
    "SubmitOutcome",
    "UPLOAD_CANCELLED_MESSAGE",
    "UPLOAD_CANCELLED_UI",
    "UPLOAD_TRANSITIONS",
    "UploadOutcome",
    "UploadSchedule",
    "UploadTransition",
    "Uploader",
    "canonical_manifest_bytes",
    "default_client_factory",
    "hashes_in_request_body",
    "new_client_submission_id",
    "short_pending_id",
    "submit_request_hash_invariant",
    "upload_token_hash",
]

#: Replay data file names of spec §5.3.1 / §5.4.2.
ARCHIVE_FILENAME = "archive.zip"
MANIFEST_FILENAME = "manifest.json"
SUBMIT_REQUEST_FILENAME = "submit_request.json"
META_FILENAME = "meta.json"
#: Same value as packer.ARCHIVE_FILENAME, without importing the packer.
DEFAULT_ARCHIVE_NAME = "archive.zip"

#: Spec §5.4.4 exception ①: one retry of the same token and body.
RETRY_AFTER_UPLOAD_IN_PROGRESS = 5.0

#: Client visible wording (spec §5.4.3, §5.4.4, §5.6.4).
UPLOAD_CANCELLED_UI = (
    "已停止本次上传；服务端可能已收到，客户端会在对账时用同一凭证核对，"
    "不会重复创建数据集"
)
UPLOAD_CANCELLED_MESSAGE = UPLOAD_CANCELLED_UI
AUTH_MESSAGE = "Token 无效或已过期，请在配置页更新后重试"
QUOTA_MESSAGE = "超出服务端配额：服务端已触发自动回收，请稍后重试"
QUOTA_RETRY_MESSAGE = (
    "超出服务端配额：服务端已触发自动回收，请稍后重试；"
    "若回收仍无法解除，请联系管理员手工清理服务端数据集"
)
SECOND_PLAN_PROMPT = "服务端按条目级拒绝了部分条目，确认后将剔除并重新预检"
SUBMIT_RETRY_HINT = "该提交请求已存在且内容不同，请确认后重新提交"
PACKER_INSTALL_HINT = "缺少可重放数据的重打包器：无法在崩溃对账时重建压缩包"
STALLED_PREFIX = "有 {0} 个未完成的上传/提交，正在自动对账"
#: Spec §5.4.5: 409 JOB_ARTIFACTS_EXPIRED only belongs to an existing
#: job's resume; 'POST /jobs' never returns it.
JOB_ARTIFACTS_EXPIRED_IS_NOT_SUBMIT = False

CLIENT_SUBMISSION_PREFIX = "sub_"
_TOKEN_RE = re.compile("^ut_[0-9a-f]{32}$")
_SUBMISSION_RE = re.compile("^sub_[0-9a-f]{32}$")
_HASH_FIELD_NAMES = (
    "plan_request_hash",
    "upload_request_hash",
    "submit_request_hash",
)
#: No second phase enum lives here: every phase check goes through
#: store_mod.PHASES (spec §5.4.1 six values, one source).


# --------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------


def canonical_manifest_bytes(manifest: Mapping[str, Any]) -> bytes:
    """The canonical bytes of one plan request body (spec §5.4.1)."""

    return store_mod.canonical_json_bytes(dict(manifest))


def upload_token_hash(upload_token: str, manifest_bytes: bytes) -> str:
    """The manifest fingerprint of spec §4.1.4 / §5.4.2.

    sha256(token utf-8 + 0x00 + canonical manifest bytes).  The local
    ledger never latches this value; it is exposed for the request
    invariant checks of spec §5.4.1 conclusion 3.
    """

    return sha256_hex(
        str(upload_token).encode("utf-8") + b"\x00" + bytes(manifest_bytes)
    )


def new_client_submission_id() -> str:
    """sub_<32hex> (spec §5.4.5 step 1, §3.1)."""

    return CLIENT_SUBMISSION_PREFIX + secrets.token_hex(16)


def short_pending_id(value: str) -> str:
    """Directory name of pending/<id>/ (spec §5.4.2)."""

    return pending_dir_name(value)


def hashes_in_request_body(body: Any) -> List[str]:
    """The three latched hash fields found inside one request body.

    Spec §5.4.1 conclusion 3 forbids sending them to the server; this is
    the pure helper the invariant check (CT44) uses.
    """

    found: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                if key in _HASH_FIELD_NAMES and key not in found:
                    found.append(str(key))
                walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(body)
    return found


def submit_request_hash_invariant(
    ledger: TasksLedger, submission: PendingSubmission
) -> bool:
    """True while the latched hashes still satisfy spec §5.4.1.

    The only legal cross stage relation is 'plan_request_hash ==
    upload_request_hash' (both describe the same manifest bytes).
    """

    for entry in ledger.pending_uploads:
        if entry.pending_dir != submission.pending_dir:
            continue
        if not entry.plan_request_hash or not entry.upload_request_hash:
            return True
        return entry.plan_request_hash == entry.upload_request_hash
    return True


def default_client_factory(
    base_url: str, api_key: Optional[str] = None, **kwargs: Any
) -> api.RemoteTrainingClient:
    """RemoteTrainingClient factory; injectable for the fake server."""

    return api.RemoteTrainingClient(base_url, api_key, **kwargs)


def _file_size(path: Optional[str]) -> int:
    if not path:
        return 0
    try:
        return int(osp.getsize(path))
    except OSError:
        return 0


def _as_mapping(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _entry_expiry(entry: PendingUpload) -> Optional[float]:
    if not entry.expires_at:
        return None
    return parse_iso8601_epoch(entry.expires_at)


def _entry_key(entry: Any) -> str:
    return str(
        getattr(entry, "upload_token", None)
        or getattr(entry, "client_submission_id", "")
    )


def _decode_manifest(data: bytes) -> Dict[str, Any]:
    payload = json.loads(bytes(data).decode("utf-8"))
    if not isinstance(payload, dict):
        raise api.ContractViolationError(
            "manifest.json 顶层不是 JSON 对象"
        )
    return payload


def _warning_lines(warnings: Any) -> List[Any]:
    from .packer import warning_info_issues

    return warning_info_issues(warnings)


def _fsync_file(path: str) -> None:
    """fsync one already written file (spec §5.4.5 step 2)."""

    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _move_file(source: str, target: str) -> None:
    """Move one replay file into pending_dir, fsynced (spec §5.4.2 ②)."""

    directory = osp.dirname(target) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = target + ".tmp"
    try:
        with open(source, "rb") as reader, open(tmp, "wb") as writer:
            shutil.copyfileobj(reader, writer, 1 << 20)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(tmp, target)
        store_mod.fsync_directory(directory)
    except OSError:
        try:
            if osp.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise


def _entry_anchor(path: str) -> float:
    """mtime anchor of one pending directory (spec §5.4.2 orphan sweep)."""

    try:
        return float(osp.getmtime(path))
    except OSError:
        return 0.0


# --------------------------------------------------------------------
# Result objects
# --------------------------------------------------------------------


@dataclass
class PhaseResult:
    """Outcome of one ledger transition."""

    entry: Optional[Any] = None
    phase: str = ""
    void_reason: Optional[str] = None
    message: str = ""
    action: str = ""


@dataclass
class UploadSchedule:
    """A 429 wait the caller drives with a timer (spec §5.4.4 ③)."""

    entry: Any
    retry_at: float
    wait_seconds: int
    reason: str = "committed_token_capacity"


@dataclass
class PlanOutcome:
    """Result of one plan call (spec §5.4.3)."""

    ok: bool = False
    attempt: int = 1
    response: Optional[Mapping[str, Any]] = None
    upload_token: Optional[str] = None
    expires_at: Optional[str] = None
    total_images: Optional[int] = None
    blob_hits: Optional[int] = None
    missing_images: List[Dict[str, Any]] = field(default_factory=list)
    upload_bytes: Optional[int] = None
    saved_bytes: int = 0
    rejected: List[Dict[str, Any]] = field(default_factory=list)
    rejected_reasons: List[str] = field(default_factory=list)
    warnings: List[Mapping[str, Any]] = field(default_factory=list)
    entry: Optional[PendingUpload] = None
    hashes: Dict[str, str] = field(default_factory=dict)
    zero_missing: bool = False
    summary_text: str = ""
    messages: List[str] = field(default_factory=list)
    error_message: str = ""
    error_details: Dict[str, Any] = field(default_factory=dict)
    error_code: str = ""
    #: Local blockade code (never sent to the server, never a spec §3.3
    #: code): 'total_images_zero' / 'all_entries_rejected'.
    local_block: str = ""
    http_status: Optional[int] = None
    partial: bool = False

    @property
    def rejected_count(self) -> int:
        return len(self.rejected)

    @property
    def all_entries_rejected(self) -> bool:
        total = self.total_images
        return bool(
            total is not None and total > 0 and not self.missing_images
        )

    @property
    def plan_request_hash(self) -> Optional[str]:
        return self.hashes.get("plan_request_hash")


@dataclass
class UploadOutcome:
    """Result of one upload attempt (spec §5.4.4)."""

    ok: bool = False
    dataset_id: Optional[str] = None
    response: Optional[Mapping[str, Any]] = None
    warnings: List[Mapping[str, Any]] = field(default_factory=list)
    counts: Dict[str, Any] = field(default_factory=dict)
    blobs: Dict[str, Any] = field(default_factory=dict)
    bytes_sent: int = 0
    cancelled: bool = False
    phase: Optional[PhaseResult] = None
    action: str = ""
    reason: str = ""
    error_message: str = ""
    error_code: str = ""
    #: ApiError.details of the failure (spec §5.6.4 requires the lists such
    #: as details.files[] to be shown entry by entry).
    error_details: Dict[str, Any] = field(default_factory=dict)
    http_status: Optional[int] = None
    retry_after: Optional[float] = None
    auto_retried: bool = False


@dataclass
class SubmitOutcome:
    """Result of one submit attempt (spec §5.4.5)."""

    ok: bool = False
    job_id: Optional[str] = None
    response: Optional[Mapping[str, Any]] = None
    warnings: List[Mapping[str, Any]] = field(default_factory=list)
    client_submission_id: str = ""
    submit_request_hash: Optional[str] = None
    request_path: str = ""
    dataset_id: Optional[str] = None
    phase: Optional[PhaseResult] = None
    action: str = ""
    reason: str = ""
    error_message: str = ""
    error_code: str = ""
    http_status: Optional[int] = None
    highlight_settings: bool = False
    retry_hint: str = ""


@dataclass
class EntriesReport:
    """Counters of one reconciliation pass (spec §5.4.1)."""

    planned: List[Any] = field(default_factory=list)
    uploading: List[Any] = field(default_factory=list)
    committed: List[Any] = field(default_factory=list)
    submitting: List[Any] = field(default_factory=list)
    submitted: List[Any] = field(default_factory=list)
    voided: List[Any] = field(default_factory=list)
    records: List[str] = field(default_factory=list)
    messages: List[str] = field(default_factory=list)
    retry_later: List[Any] = field(default_factory=list)

    @property
    def dropped(self) -> List[Any]:
        seen = list(self.voided)
        for entry in self.submitted:
            if entry not in seen:
                seen.append(entry)
        return seen

    @property
    def unfinished_count(self) -> int:
        return (
            len(self.planned)
            + len(self.uploading)
            + len(self.committed)
            + len(self.submitting)
        )

    def status_message(self) -> str:
        if not self.unfinished_count:
            return ""
        return STALLED_PREFIX.format(self.unfinished_count)


@dataclass
class CleanupReport:
    """Outcome of one pending/<id>/ cleanup pass (spec §5.4.2)."""

    removed_entries: List[str] = field(default_factory=list)
    reclaimed_dirs: List[str] = field(default_factory=list)
    kept_referenced: List[str] = field(default_factory=list)
    freed_bytes: int = 0

    @property
    def freed_mb(self) -> float:
        return self.freed_bytes / (1024.0 * 1024.0)


@dataclass
class PackOutcome:
    """What the injected packer produced for one plan."""

    manifest: Dict[str, Any] = field(default_factory=dict)
    missing_images: List[Dict[str, Any]] = field(default_factory=list)
    archive_path: Optional[str] = None
    manifest_bytes: bytes = b""
    messages: List[str] = field(default_factory=list)


class Packer:
    """Protocol of the injected pack / repack collaborator.

    'pack' runs right after a successful first plan (spec §5.2.1 steps 8
    and 10): it serialises the final plan request body and writes the zip
    that contains exactly the plan's missing_images[].

    'restore' rebuilds the run for crash reconciliation (spec §5.4.1
    recovery action 1): it re-reads pending_dir/manifest.json as the
    authoritative plan body and, when archive.zip disappeared, repacks
    from the staging work area.  Replay only ever uses
    pending_dir/archive.zip.
    """

    def pack(
        self,
        run: Any,
        missing_images: Sequence[Mapping[str, Any]],
        manifest_json: bytes = b"",
        labels: Optional[Mapping[str, Any]] = None,
    ) -> PackOutcome:
        """Serialise the frozen plan body and write the archive.

        `manifest_json` is the exact plan request body (the plan hash
        describes it) and `labels` the frozen label bytes; both are
        handed over so the packer never has to guess them.
        """

        raise NotImplementedError

    def restore(self, entry: PendingUpload) -> PackOutcome:
        raise NotImplementedError


class ArchivePersistError(Exception):
    """The packed archive could not be persisted into pending_dir (R1).

    Private: plan() turns it into a failed PlanOutcome so no half
    latched state can escape to the caller.
    """


class SubmissionConflictError(api.RemoteTrainingError):
    """One client_submission_id was reused with a different body.

    Spec §5.4.1 conclusion 2 latches the three request hashes once and
    never rewrites them, and spec §5.4.5 forbids changing the request
    body under the same client_submission_id, so the caller has to use a
    new id (or explicit void) instead.
    """


# --------------------------------------------------------------------
# Failure transition table (spec §5.4.4)
# --------------------------------------------------------------------


@dataclass(frozen=True)
class UploadTransition:
    """One row of the 17 row table that ends in a definite state."""

    phase: str
    void_reason: Optional[str] = None
    action: str = ""
    message: str = ""


#: (HTTP, code) -> transition, for the rows that are a pure lookup.  The
#: remaining rows (409 UPLOAD_IN_PROGRESS, 413, 429 and "no response")
#: are branches of their own and live in the uploader methods.
UPLOAD_TRANSITIONS: Dict[Any, UploadTransition] = {
    (400, "VALIDATION_FAILED"): UploadTransition(
        "void", "validation_failed", "replan"
    ),
    (409, "VALIDATION_FAILED"): UploadTransition(
        "void", "upload_token_reused", "replan"
    ),
    (400, "CHECKSUM_MISMATCH"): UploadTransition(
        "void", "checksum_mismatch", "replan"
    ),
    (400, "LABEL_CHECKSUM_MISMATCH"): UploadTransition(
        "void", "label_checksum_mismatch", "replan"
    ),
    (400, "MISSING_LABELS"): UploadTransition(
        "void", "missing_labels", "replan"
    ),
    (400, "INVALID_LABEL_FORMAT"): UploadTransition(
        "void", "invalid_label_format", "replan"
    ),
    (400, "UNSUPPORTED_EXTENSION"): UploadTransition(
        "void", "unsupported_extension", "replan"
    ),
    (400, "MANIFEST_MISMATCH"): UploadTransition(
        "void", "manifest_mismatch", "replan"
    ),
    (400, "TOKEN_EXPIRED"): UploadTransition(
        "void", "token_expired", "replan"
    ),
    (400, "UNKNOWN_UPLOAD_TOKEN"): UploadTransition(
        "void", "token_expired", "replan"
    ),
    (401, "UNAUTHORIZED"): UploadTransition(
        "keep", None, "manual_retry", AUTH_MESSAGE
    ),
    (500, "INTERNAL_ERROR"): UploadTransition("keep", None, "reconcile"),
}


# --------------------------------------------------------------------
# Uploader
# --------------------------------------------------------------------


class Uploader:
    """State machine of spec §5.1.5 and §5.4.1 to §5.4.5.

    Everything the object owns is either in 'pending/<id>/' (replay data)
    or in 'tasks.json' (the ledger); nothing that a restart would need is
    kept in memory only.
    """

    def __init__(
        self,
        store: Optional[Store] = None,
        *,
        packer: Optional[Packer] = None,
        client_factory: Callable[..., Any] = default_client_factory,
        log: Optional[logging.Logger] = None,
        clock: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], None]] = None,
    ) -> None:
        self.store = store if store is not None else Store()
        self.packer = packer
        self.client_factory = client_factory
        self.log = log or _LOGGER
        self._clock = clock
        self._sleep = sleep or time.sleep
        self.ledger: TasksLedger = self.store.load_ledger()

    # -- ledger -----------------------------------------------------

    def reload(self) -> TasksLedger:
        """Re-read tasks.json (every scan re-reads it, spec §5.1.5)."""

        self.ledger = self.store.load_ledger()
        return self.ledger

    def _save(self) -> None:
        self.store.save_ledger(self.ledger)

    def _now(self) -> float:
        if self._clock is not None:
            return float(self._clock())
        return utc_now().timestamp()

    def _now_iso(self) -> str:
        if self._clock is None:
            return utc_now_iso()
        moment = _dt.datetime.fromtimestamp(self._now(), _dt.timezone.utc)
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")

    def new_client(
        self, server_url: str, api_key: Optional[str] = None
    ) -> Any:
        return self.client_factory(server_url, api_key)

    # -- plan (spec §5.4.3) -----------------------------------------

    def plan(
        self,
        manifest: Mapping[str, Any],
        *,
        client: Any,
        run: Any = None,
        server_url: str = "",
        dataset_dir: str = "",
        classes_file: str = "",
        task: str = "",
        val_ratio: Optional[float] = None,
        seed: Optional[int] = None,
        params: Optional[Mapping[str, Any]] = None,
        staging_dir: Optional[str] = None,
        labels: Optional[Mapping[str, Any]] = None,
        attempt: int = 1,
        pack: bool = True,
        dry_run: bool = False,
    ) -> PlanOutcome:
        """POST /datasets/plan; latch 'planned' only after success.

        Spec §5.4.1: nothing is written before the response is safely on
        disk, a response carrying rejected[] goes to the second plan path
        instead of creating an entry, and a failing plan (400 / 413 / 503
        or an unknown outcome) writes no ledger row at all.
        """

        manifest_bytes = canonical_manifest_bytes(manifest)
        plan_hash = lock_hash(manifest)
        outcome = PlanOutcome(attempt=int(attempt))
        outcome.hashes["plan_request_hash"] = plan_hash

        try:
            response = client.plan_dataset(dict(manifest))
        except Exception as exc:
            return self._plan_failed(outcome, exc)

        data = _as_mapping(response)
        outcome.ok = True
        outcome.response = data
        outcome.messages = [
            issue.text() for issue in _warning_lines(data.get("warnings"))
        ]
        token = data.get("upload_token")
        try:
            self._check_plan_payload(data, token)
        except api.RemoteTrainingError as exc:
            return self._plan_failed(outcome, exc)
        outcome.upload_token = str(token)
        outcome.expires_at = (
            str(data.get("expires_at")) if data.get("expires_at") else None
        )
        outcome.total_images = _as_int(data.get("total_images"))
        outcome.blob_hits = _as_int(data.get("blob_hits"))
        outcome.missing_images = [
            dict(item)
            for item in data.get("missing_images") or ()
            if isinstance(item, Mapping)
        ]
        outcome.upload_bytes = _as_int(data.get("upload_bytes"))
        outcome.saved_bytes = self._saved_bytes(manifest, outcome)
        outcome.rejected = [
            dict(item)
            for item in data.get("rejected") or ()
            if isinstance(item, Mapping)
        ]
        outcome.rejected_reasons = sorted(
            {
                str(item.get("reason") or "")
                for item in outcome.rejected
                if item.get("reason")
            }
        )
        outcome.warnings = list(data.get("warnings") or ())
        outcome.zero_missing = not outcome.missing_images
        outcome.summary_text = self.summary_text(outcome)

        if outcome.rejected:
            # Spec §5.4.3: the whole response, including the old token, is
            # discarded; only a later second plan may latch an entry.
            outcome.partial = True
            outcome.error_message = SECOND_PLAN_PROMPT
            return outcome

        total = outcome.total_images
        if total is None:
            # R-d: a response without total_images is malformed, not a
            # zero-image data set; the two must not share a wording.
            outcome.ok = False
            outcome.error_code = "VALIDATION_FAILED"
            outcome.local_block = "total_images_missing"
            outcome.error_message = (
                "服务端预检响应缺少 total_images：既不打包也不上传，"
                "且未写入任何待提交条目"
            )
            return outcome
        if not total:
            # Spec §5.4.3: only total_images == 0 means "neither pack nor
            # upload" (an empty dataset is blocked by the local pre-check
            # long before this call).  Fail locally and latch nothing.
            outcome.ok = False
            outcome.error_code = "VALIDATION_FAILED"
            outcome.local_block = "total_images_zero"
            outcome.error_message = (
                "服务端预检返回 0 张图片：既不打包也不上传，"
                "且未写入任何待提交条目"
            )
            return outcome
        if not outcome.missing_images and not outcome.blob_hits:
            # Spec §5.4.3 boundary ②: every entry was rejected without a
            # rejected[] list.  Fail here; never upload, never latch.
            # An empty missing_images[] with blob_hits > 0 is the normal
            # warm-cache plan (spec §5.4.3): every image hit the cache
            # and the labels still have to be uploaded, so it latches.
            outcome.ok = False
            outcome.error_code = "VALIDATION_FAILED"
            outcome.local_block = "all_entries_rejected"
            outcome.error_message = (
                "全部条目都被服务端拒绝：首次预检即失败，"
                "已停止上传且未写入任何待提交条目"
            )
            return outcome

        if dry_run:
            return outcome

        archive_path: Optional[str] = None
        if pack:
            packed = self._pack(
                run,
                outcome.missing_images,
                manifest_json=manifest_bytes,
                labels=labels,
            )
            outcome.missing_images = packed.missing_images
            outcome.messages.extend(packed.messages)
            # Spec §5.4.1 conclusions 2 and 3: the latched hash and the
            # pending_dir/manifest.json copy must describe the very body
            # that was sent.  A packer that rewrites the manifest would
            # break both, so it is rejected instead of silently
            # overwriting them.
            if packed.manifest_bytes and (
                packed.manifest_bytes != manifest_bytes
            ):
                raise api.ContractViolationError(
                    "packer rewrote the plan request body; refusing to "
                    "latch a hash that does not describe the sent bytes"
                )
            if packed.manifest and (
                canonical_manifest_bytes(packed.manifest) != manifest_bytes
            ):
                raise api.ContractViolationError(
                    "packer manifest differs from the plan request body"
                )
            # N1: keep the packer's zip for the first upload instead of
            # repacking it through restore() on the happy path.
            archive_path = packed.archive_path

        try:
            entry = self._latch_planned(
                outcome,
                manifest_bytes=manifest_bytes,
                plan_hash=plan_hash,
                archive_path=archive_path,
                server_url=server_url,
                dataset_dir=dataset_dir,
                classes_file=classes_file,
                task=task,
                val_ratio=val_ratio,
                seed=seed,
                params=params,
                staging_dir=staging_dir,
            )
        except ArchivePersistError as exc:
            # R1: the archive is written before the row, so this failure
            # leaves NO latched entry - reconciliation can never upload
            # this token, hence no second dataset.  The pending/<id>/
            # made on the way stays an unreferenced orphan (cleanup
            # path ② / orphan sweep).  A typed failure is reported
            # instead of letting an OSError escape past a half written
            # ledger.
            outcome.ok = False
            outcome.local_block = "archive_persist_failed"
            outcome.error_message = (
                "压缩包落盘失败，已停止上传且未写入任何待提交条目：{0}"
            ).format(exc)
            outcome.entry = None
            return outcome
        outcome.entry = entry
        return outcome

    def _plan_failed(
        self, outcome: PlanOutcome, exc: Exception
    ) -> PlanOutcome:
        """Spec §5.4.1 conclusion 1: a failed plan writes nothing."""

        outcome.ok = False
        outcome.error_message = str(exc)
        if isinstance(exc, api.ApiError):
            outcome.error_code = exc.code
            outcome.http_status = exc.http_status
            outcome.error_details = dict(exc.details)
        outcome.rejected = []
        self.log.info(
            "plan attempt %d failed (no ledger row written): %s",
            outcome.attempt,
            exc,
        )
        return outcome

    def _check_plan_payload(
        self, data: Mapping[str, Any], token: Any
    ) -> None:
        """The planned invariant: token and expires_at come together."""

        if not isinstance(token, str) or not token:
            raise api.MalformedResponseError(
                "plan response carries no upload_token"
            )
        if not data.get("expires_at"):
            raise api.MalformedResponseError(
                "plan response carries no expires_at"
            )
        if not _TOKEN_RE.match(token):
            self.log.warning(
                "plan returned a token outside the ut_<32hex> shape: %r",
                token,
            )

    def _saved_bytes(
        self, manifest: Mapping[str, Any], outcome: PlanOutcome
    ) -> int:
        """Y of the frozen summary: local bytes of the cache hits."""

        missing = {str(item.get("name")) for item in outcome.missing_images}
        total = 0
        for item in manifest.get("images") or ():
            if not isinstance(item, Mapping):
                continue
            if str(item.get("name")) in missing:
                continue
            try:
                total += int(item.get("size") or 0)
            except (TypeError, ValueError):
                continue
        return total

    def summary_text(self, outcome: PlanOutcome) -> str:
        """The frozen pre-check summary (spec §5.4.3, verbatim shape)."""

        total = outcome.total_images or 0
        hits = outcome.blob_hits or 0
        miss = len(outcome.missing_images)
        saved_gb = outcome.saved_bytes / (1024.0 ** 3)
        upload_mb = (outcome.upload_bytes or 0) / (1024.0 * 1024.0)
        return (
            "共 {0} 张，命中缓存 {1} 张（省 {2:.2f} GB），"
            "需上传 {3} 张 / {4:.1f} MB"
        ).format(total, hits, saved_gb, miss, upload_mb)

    def _pack(
        self,
        run: Any,
        missing_images: Sequence[Mapping[str, Any]],
        *,
        manifest_json: bytes = b"",
        labels: Optional[Mapping[str, Any]] = None,
    ) -> PackOutcome:
        if self.packer is None:
            return PackOutcome(
                missing_images=[dict(item) for item in missing_images]
            )
        result = self.packer.pack(
            run, missing_images, manifest_json, labels
        )
        if not isinstance(result, PackOutcome):
            raise api.ContractViolationError(
                "packer did not return a PackOutcome"
            )
        return result

    def _latch_planned(
        self,
        outcome: PlanOutcome,
        *,
        manifest_bytes: bytes,
        plan_hash: str,
        archive_path: Optional[str] = None,
        server_url: str,
        dataset_dir: str,
        classes_file: str,
        task: str,
        val_ratio: Optional[float],
        seed: Optional[int],
        params: Optional[Mapping[str, Any]],
        staging_dir: Optional[str],
    ) -> PendingUpload:
        """Create pending/<id>/ plus manifest.json, then the entry.

        Spec §5.4.1: the directory and its manifest.json are created in
        the same action as entering 'planned'; there is no 'planned' entry
        without a pending_dir/.
        """

        token = str(outcome.upload_token)
        existing = self.ledger.upload(token)
        if existing is not None:
            # N3: the same token can be latched twice (double click or a
            # retried identical response).  The write-once rule of spec
            # §5.4.1 forbids rewriting the row, so the first one wins.
            self.log.warning(
                "upload_token %s is already latched (phase=%s); keeping "
                "the existing row and not writing a duplicate",
                token,
                existing.phase,
            )
            return existing
        pending_dir = self.store.ensure_pending_dir(token)
        manifest_path = osp.join(pending_dir, MANIFEST_FILENAME)
        self.store.write_pending_json(
            manifest_path, _decode_manifest(manifest_bytes)
        )
        entry = PendingUpload(
            upload_token=token,
            phase="planned",
            plan_request_hash=plan_hash,
            upload_request_hash=None,
            server_url=str(server_url or ""),
            pending_dir=pending_dir,
            archive_path=None,
            manifest_path=manifest_path,
            staging_dir=staging_dir or None,
            dataset_dir=str(dataset_dir or ""),
            classes_file=str(classes_file or ""),
            task=str(task or ""),
            val_ratio=val_ratio,
            seed=seed,
            params=dict(params or {}),
            missing_images=[dict(item) for item in outcome.missing_images],
            dataset_id=None,
            created_at=self._now_iso(),
            expires_at=outcome.expires_at,
            void_reason=None,
        )
        # The planned response is also kept in the row: the crash path can
        # then rebuild the archive byte for byte without a second scan
        # (spec §5.4.1 recovery action 1).  Unknown keys survive the
        # ledger round trip by construction (spec §5.3.2).
        entry.extra["missing_images"] = [
            dict(item) for item in outcome.missing_images
        ]
        entry.extra["plan_response"] = {
            "warnings": list(outcome.warnings or ()),
        }
        if archive_path and osp.isfile(archive_path):
            # N1: the archive is persisted together with the 'planned'
            # entry, so the first upload never has to repack; restore()
            # only serves the crash path.  spec §5.4.2 write order ②.
            #
            # R1: the move happens BEFORE the row is appended, so a
            # failing copy can never leave a latched 'planned' row
            # behind (that would let reconciliation upload the same
            # dataset a second time).  On failure nothing is latched and
            # the pending/<id>/ made above is an unreferenced orphan
            # reclaimed by cleanup path ② (spec §5.4.2).
            target = osp.join(pending_dir, ARCHIVE_FILENAME)
            if osp.normpath(osp.abspath(archive_path)) != osp.normpath(
                osp.abspath(target)
            ):
                try:
                    _move_file(archive_path, target)
                except OSError as exc:
                    self.log.error(
                        "压缩包落盘失败（%s）：%s；未写入任何待提交条目",
                        target,
                        exc,
                    )
                    raise ArchivePersistError(str(exc)) from exc
            entry.archive_path = target
        self._write_meta(
            pending_dir,
            {
                "server_url": entry.server_url,
                "upload_token": token,
                "phase": entry.phase,
                "created_at": entry.created_at,
                "expires_at": entry.expires_at,
                "staging_dir": entry.staging_dir or "",
                "archive_path": entry.archive_path or "",
            },
        )
        self.ledger.pending_uploads.append(entry)
        # One single write latches the row together with its archive, so
        # there is no "row on disk but archive not persisted" window.
        self._save()
        self.log.info(
            "plan latched %s (phase=planned, %d missing image(s))",
            token,
            len(entry.missing_images),
        )
        return entry

    def _write_meta(
        self, pending_dir: str, payload: Mapping[str, Any]
    ) -> None:
        """meta.json of spec §5.4.2; losing it never breaks replay."""

        try:
            self.store.write_pending_json(
                osp.join(pending_dir, META_FILENAME), dict(payload)
            )
        except OSError as exc:
            self.log.warning(
                "could not write %s in %s: %s",
                META_FILENAME,
                pending_dir,
                exc,
            )

    # -- uploading (spec §5.4.2, §5.4.4) ----------------------------

    def upload(
        self,
        client: Any,
        entry: Optional[PendingUpload] = None,
        *,
        on_progress: Optional[Callable[[int, int], None]] = None,
        cancel: Optional[threading.Event] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> UploadOutcome:
        """Persist the archive, move to 'uploading', then send it."""

        entry = entry if entry is not None else self._encode_target()
        if entry is None:
            return UploadOutcome(action="nothing_to_do")
        if entry.phase in store_mod.TERMINAL_PENDING_PHASES:
            # Spec §5.4.1: a voided entry "清除前不参与任何重放", and a
            # 'submitted' row is being carried into records.  A caller
            # holding a stale object must not revive either of them.
            return UploadOutcome(
                action="blocked",
                reason="phase_" + str(entry.phase),
                phase=self._view(entry),
            )
        if entry.phase == "committed":
            # Spec §5.4.1 recovery action 3: never replay an upload that
            # already returned a dataset_id.
            return UploadOutcome(
                ok=True,
                dataset_id=entry.dataset_id,
                action="submit",
                phase=self._view(entry),
            )

        archive_path, manifest_bytes = self._ensure_replay_files(
            entry, on_progress=on_progress, should_cancel=should_cancel
        )
        if entry.phase != "uploading":
            self._advance(
                entry,
                "uploading",
                save=True,
                upload_request_hash=lock_hash(
                    _decode_manifest(manifest_bytes)
                ),
                archive_path=archive_path,
                manifest_path=entry.manifest_path
                or osp.join(entry.pending_dir, MANIFEST_FILENAME),
            )

        cancelled = cancel if cancel is not None else threading.Event()
        stop = should_cancel or cancelled.is_set
        return self._send_upload(
            client,
            entry,
            archive_path,
            cancel=cancelled,
            on_progress=on_progress,
            should_cancel=stop,
        )

    def _encode_target(self) -> Optional[PendingUpload]:
        for entry in self.ledger.pending_uploads:
            if entry.phase in ("planned", "uploading"):
                return entry
        return None

    def _ensure_replay_files(
        self,
        entry: PendingUpload,
        *,
        on_progress: Optional[Callable[[int, int], None]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> Any:
        """Guarantee pending_dir/archive.zip and manifest.json exist."""

        pending_dir = entry.pending_dir
        manifest_path = entry.manifest_path or osp.join(
            pending_dir, MANIFEST_FILENAME
        )
        archive_path = entry.archive_path or osp.join(
            pending_dir, ARCHIVE_FILENAME
        )
        if osp.isfile(archive_path) and osp.isfile(manifest_path):
            with open(manifest_path, "rb") as handle:
                return archive_path, handle.read()
        if self.packer is None:
            raise api.MissingDependencyError(PACKER_INSTALL_HINT)
        restored = self.packer.restore(entry)
        if not isinstance(restored, PackOutcome):
            raise api.ContractViolationError(
                "packer did not return a PackOutcome"
            )
        manifest_bytes = restored.manifest_bytes
        if not manifest_bytes and osp.isfile(manifest_path):
            with open(manifest_path, "rb") as handle:
                manifest_bytes = handle.read()
        if not manifest_bytes:
            raise api.MissingDependencyError(PACKER_INSTALL_HINT)
        if not osp.isfile(manifest_path):
            self.store.write_pending_json(
                manifest_path, _decode_manifest(manifest_bytes)
            )
        if not osp.isfile(archive_path):
            if not restored.archive_path or not osp.isfile(
                restored.archive_path
            ):
                raise api.MissingDependencyError(PACKER_INSTALL_HINT)
            _move_file(restored.archive_path, archive_path)
        return archive_path, manifest_bytes

    def _send_upload(
        self,
        client: Any,
        entry: PendingUpload,
        archive_path: str,
        *,
        cancel: threading.Event,
        on_progress: Optional[Callable[[int, int], None]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> UploadOutcome:
        token = entry.upload_token
        attempts = 0
        auto_retried = False
        while True:
            attempts += 1
            try:
                response = client.upload_dataset(
                    token,
                    archive_path,
                    on_progress=on_progress,
                    should_cancel=should_cancel,
                )
            except api.UploadCancelledError:
                return self._upload_cancelled(entry)
            except api.UploadInProgressError as exc:
                if attempts >= 2:
                    return self._keep_uploading(
                        entry,
                        exc,
                        action="manual_retry",
                        auto_retried=True,
                    )
                self.log.info(
                    "409 UPLOAD_IN_PROGRESS; retrying the same token and "
                    "body once after %.0f s",
                    RETRY_AFTER_UPLOAD_IN_PROGRESS,
                )
                self._sleep(RETRY_AFTER_UPLOAD_IN_PROGRESS)
                auto_retried = True
                continue
            except api.QuotaExceededError as exc:
                return self._keep_uploading(
                    entry,
                    exc,
                    message=QUOTA_MESSAGE,
                    action="manual_retry",
                    auto_retried=auto_retried,
                )
            except api.CommittedTokenCapacityError as exc:
                return self._capacity_branch(entry, exc)
            except api.ApiError as exc:
                return self._from_transition(
                    entry, exc, auto_retried=auto_retried
                )
            except api.RemoteTrainingError as exc:
                return self._from_transition(
                    entry, exc, auto_retried=auto_retried
                )
            except Exception as exc:
                return self._from_transition(
                    entry, exc, auto_retried=auto_retried
                )

            data = _as_mapping(response)
            dataset_id = data.get("dataset_id")
            if not dataset_id:
                # D5: 'committed' means "dataset_id in hand" (spec §5.4.1).
                # Without it the entry must stay 'uploading' so the next
                # reconciliation replays the same token + archive, exactly
                # like the missing job_id case of POST /jobs.
                return self._from_transition(
                    entry,
                    api.MalformedResponseError(
                        "POST /datasets/upload returned no dataset_id",
                        None,
                    ),
                    auto_retried=auto_retried,
                )
            warnings = api.upload_warnings(data)
            self._advance(
                entry, "committed", save=True, dataset_id=dataset_id
            )
            self._write_meta(
                entry.pending_dir,
                {
                    "server_url": entry.server_url,
                    "upload_token": entry.upload_token,
                    "phase": "committed",
                    "created_at": entry.created_at,
                    "expires_at": entry.expires_at,
                    "staging_dir": entry.staging_dir or "",
                    "dataset_id": dataset_id,
                },
            )
            self.log.info(
                "upload committed %s -> dataset_id=%s",
                entry.upload_token,
                dataset_id,
            )
            return UploadOutcome(
                ok=True,
                dataset_id=str(dataset_id) if dataset_id else None,
                response=data,
                warnings=warnings,
                counts=_as_mapping(data.get("counts")),
                blobs=_as_mapping(data.get("blob")),
                bytes_sent=_file_size(archive_path),
                phase=self._view(entry),
                action="submit",
                auto_retried=auto_retried,
            )

    def _upload_cancelled(self, entry: PendingUpload) -> UploadOutcome:
        """Spec §5.4.4: all three cancellation shapes keep 'uploading'."""

        self.log.info(
            "upload cancelled by the user; %s stays uploading and keeps "
            "archive.zip for the next reconciliation",
            entry.upload_token,
        )
        return UploadOutcome(
            ok=False,
            cancelled=True,
            phase=self._view(entry),
            action="cancel",
            reason="cancelled",
            error_message=UPLOAD_CANCELLED_UI,
        )

    def _keep_uploading(
        self,
        entry: PendingUpload,
        exc: Exception,
        *,
        message: str = "",
        action: str = "reconcile",
        auto_retried: bool = False,
    ) -> UploadOutcome:
        """A row that keeps the phase and the replay data."""

        if entry.phase != "uploading":
            self._advance(entry, "uploading", save=True)
        code = str(getattr(exc, "code", "") or "")
        return UploadOutcome(
            ok=False,
            phase=self._view(entry),
            action=action,
            reason=code,
            error_message=message or str(exc),
            error_code=code,
            error_details=dict(getattr(exc, "details", None) or {}),
            http_status=getattr(exc, "http_status", None),
            auto_retried=auto_retried,
        )

    def _capacity_branch(
        self, entry: PendingUpload, exc: api.CommittedTokenCapacityError
    ) -> UploadOutcome:
        """Spec §5.4.4 exception ③: judge expiry before deciding."""

        wait = int(exc.wait_seconds())
        expires_at = _entry_expiry(entry)
        now = self._now()
        if expires_at is not None and now + wait < expires_at:
            self.log.info(
                "429 COMMITTED_TOKEN_CAPACITY_EXCEEDED; retrying the same "
                "token and body in %d s (token still valid)",
                wait,
            )
            return UploadOutcome(
                ok=False,
                phase=self._view(entry),
                action="retry_after",
                reason="committed_token_capacity",
                retry_after=float(wait),
                error_code=exc.code,
                error_details=dict(getattr(exc, "details", None) or {}),
                http_status=exc.http_status,
                error_message=str(exc),
            )
        self.log.info(
            "429 COMMITTED_TOKEN_CAPACITY_EXCEEDED and the token cannot "
            "outlive the wait; voiding as token_expired and re-planning"
        )
        result = self._void(
            entry, "token_expired", action="replan", message=str(exc)
        )
        return UploadOutcome(
            ok=False,
            phase=result,
            action="replan",
            reason="token_expired",
            error_code=exc.code,
            error_details=dict(getattr(exc, "details", None) or {}),
            http_status=exc.http_status,
            error_message=str(exc),
        )

    def _from_transition(
        self,
        entry: PendingUpload,
        exc: Exception,
        *,
        auto_retried: bool = False,
    ) -> UploadOutcome:
        status = getattr(exc, "http_status", None)
        code = str(getattr(exc, "code", "") or "")
        transition = UPLOAD_TRANSITIONS.get((status, code))
        if transition is None and isinstance(exc, api.ApiError):
            if exc.http_status >= 500:
                # 500 / 501 / 502 / 503 / 504 without a spec code: the
                # generic fallback keeps the entry and its archive.
                transition = UPLOAD_TRANSITIONS[(500, "INTERNAL_ERROR")]
        if transition is None and isinstance(exc, api.TransportError):
            # No HTTP response at all: never change the phase.
            transition = UploadTransition("keep", None, "reconcile")
        if transition is None:
            self.log.warning(
                "upload failed with an unmapped error %s (HTTP %s / %r); "
                "keeping the entry for reconciliation",
                exc,
                status,
                code,
            )
            transition = UploadTransition("keep", None, "reconcile")

        if transition.phase == "void":
            result = self._void(
                entry,
                transition.void_reason,
                action=transition.action,
                message=transition.message or str(exc),
            )
        else:
            result = self._keep_uploading(entry, exc)
        return UploadOutcome(
            ok=False,
            phase=result if result.phase else self._view(entry),
            action=transition.action or result.action,
            reason=transition.void_reason or code,
            error_code=code,
            error_details=dict(getattr(exc, "details", None) or {}),
            http_status=status,
            error_message=transition.message or str(exc),
            auto_retried=auto_retried,
        )

    def _void(
        self,
        entry: Any,
        reason: Optional[str],
        *,
        action: str = "replan",
        message: str = "",
    ) -> PhaseResult:
        if reason not in VOID_REASONS:
            raise api.ContractViolationError(
                "void_reason {0!r} is not one of the twelve values".format(
                    reason
                )
            )
        entry.void_reason = reason
        self._advance(entry, "void", save=True)
        self.log.info(
            "pending entry %s voided (void_reason=%s); its directory is "
            "reclaimed by the next reconciliation pass",
            _entry_key(entry),
            reason,
        )
        return PhaseResult(
            entry=entry,
            phase="void",
            void_reason=reason,
            message=message,
            action=action,
        )

    def void_pending(
        self, entry: Any, *, reason: str = "user_void"
    ) -> PhaseResult:
        """The only path that may void a still usable entry (§5.4.1)."""

        result = self._void(
            entry, reason, action="user_void", message="用户显式作废"
        )
        # Cleanup path ③ of spec §5.4.2: an explicit void reclaims the
        # entry's pending_dir right away (reference checked, so a
        # directory another row still points at survives).
        self._reclaim_explicit(
            str(getattr(entry, "pending_dir", "") or ""), reason="user_void"
        )
        return result

    def _reclaim_explicit(self, path: str, *, reason: str) -> bool:
        """Immediate reclamation for cleanup paths ① and ③ (§5.4.2).

        The directory is only removed when the freshly re-read ledger
        references it nowhere: a pending_dir another pending row still
        points at must survive (D1, spec §5.4.2).

        R-a wording fix: if the process dies between the ledger write
        and this call, the directory is left unreferenced and there is
        no row left for the next scan to drop, so cleanup path ② cannot
        pick it up there; it is then only reclaimed by the orphan sweep
        (pending_ttl_days / pending_max_gb, default 7 days).  That is
        loss free - the row is gone, so nothing needs the directory -
        but it is NOT the same promise as path ②.
        """

        if not path or not osp.isdir(path):
            return False
        if self.store.is_pending_dir_referenced(path):
            self.log.warning(
                "pending 目录 %s 仍被台账引用，跳过 %s 回收；交由下一次"
                "对账扫描兜底",
                osp.basename(path),
                reason,
            )
            return False
        size = store_mod.directory_size_bytes(path)
        removed = self.store.reclaim_pending_dir(path, reason=reason)
        if removed:
            self.log.info(
                "已按 %s 提前回收 pending 目录 %s（释放 %.1f MB）",
                reason,
                osp.basename(path),
                size / (1024.0 * 1024.0),
            )
        return removed

    def _advance(self, entry: Any, phase: str, **fields: Any) -> None:
        if phase not in store_mod.PHASES:
            raise api.ContractViolationError(
                "phase {0!r} is not one of the six values".format(phase)
            )
        save = bool(fields.pop("save", False))
        entry.phase = phase
        for key, value in fields.items():
            setattr(entry, key, value)
        if save:
            self._save()

    def _view(self, entry: Any, *, message: str = "") -> PhaseResult:
        return PhaseResult(
            entry=entry,
            phase=getattr(entry, "phase", ""),
            void_reason=getattr(entry, "void_reason", None),
            message=message,
        )

    # -- submit (spec §5.4.5) ---------------------------------------

    def submit(
        self,
        client: Any,
        entry: PendingUpload,
        body: Mapping[str, Any],
        *,
        run_ready: bool = True,
    ) -> SubmitOutcome:
        """Persist submit_request.json, then POST /jobs.

        Spec §5.4.5: the snapshot is fsynced *before* the ledger row is
        written; a failed fsync or a missing field keeps the entry in
        'committed' and sends nothing.
        """

        if not entry.dataset_id:
            return SubmitOutcome(
                action="blocked",
                reason="dataset_id_missing",
                error_message="缺少 dataset_id，无法生成提交请求体快照",
                phase=self._view(entry),
            )
        if not entry.pending_dir or not osp.isdir(entry.pending_dir):
            # N9: without pending_dir the snapshot would land in the
            # current working directory, which the client must never
            # write (spec §5.1.5 不写工作区).
            return SubmitOutcome(
                action="blocked",
                reason="pending_dir_missing",
                error_message="待提交目录缺失，未发起提交请求",
                phase=self._view(entry),
            )
        if not entry.server_url:
            return SubmitOutcome(
                action="blocked",
                reason="server_url_missing",
                error_message="缺少 server_url，未发起提交请求",
                phase=self._view(entry),
            )
        request_body = dict(body)
        if not request_body.get("dataset_id"):
            request_body["dataset_id"] = entry.dataset_id
        client_submission_id = self._submission_id(request_body)
        request_path = osp.join(entry.pending_dir, SUBMIT_REQUEST_FILENAME)

        if not run_ready:
            # D4: a locally blocked submit must not touch the ledger at
            # all (spec §5.4.5 pre-submit capabilities check): no
            # snapshot, no pending_submissions row, so reconciliation can
            # never send a request the local pre-check refused.
            return SubmitOutcome(
                client_submission_id=client_submission_id,
                request_path=request_path,
                dataset_id=entry.dataset_id,
                action="blocked",
                reason="local_precheck",
                error_message="本地预检未通过，未发起提交请求",
                phase=self._view(entry),
            )
        try:
            submission = self._latch_submitting(
                entry,
                request_body,
                client_submission_id=client_submission_id,
                request_path=request_path,
            )
        except SubmissionConflictError as exc:
            self.log.warning("submit blocked: %s", exc)
            return SubmitOutcome(
                client_submission_id=client_submission_id,
                request_path=request_path,
                dataset_id=entry.dataset_id,
                action="conflict",
                reason="submission_conflict",
                error_message=SUBMIT_RETRY_HINT,
                retry_hint="请改用新的 client_submission_id 重新提交",
                phase=self._view(entry),
            )
        outcome = SubmitOutcome(
            client_submission_id=client_submission_id,
            submit_request_hash=submission.submit_request_hash,
            request_path=request_path,
            dataset_id=entry.dataset_id,
        )
        return self._post_job(client, submission, outcome)

    def _submission_id(self, body: Dict[str, Any]) -> str:
        value = body.get("client_submission_id")
        if value:
            if not _SUBMISSION_RE.match(str(value)):
                self.log.warning(
                    "client_submission_id outside sub_<32hex>: %r", value
                )
            return str(value)
        generated = new_client_submission_id()
        body["client_submission_id"] = generated
        return generated

    def _latch_submitting(
        self,
        entry: PendingUpload,
        body: Mapping[str, Any],
        *,
        client_submission_id: str,
        request_path: str,
    ) -> PendingSubmission:
        """Snapshot first, ledger row second (spec §5.4.5 step 2)."""

        submit_hash = lock_hash(body)
        existing = self.ledger.submission(client_submission_id)
        if (
            existing is not None
            and existing.submit_request_hash
            and existing.submit_request_hash != submit_hash
        ):
            # D6: spec §5.4.1 conclusion 2 - a latched hash is written
            # once and never rewritten - and spec §5.4.5 forbids changing
            # the body under the same client_submission_id.  The existing
            # row and its snapshot are left untouched.
            raise SubmissionConflictError(
                "client_submission_id {0!r} already carries a different "
                "request body".format(client_submission_id)
            )
        try:
            self.store.write_pending_json(request_path, dict(body))
            _fsync_file(request_path)
        except OSError as exc:
            self.log.error(
                "could not persist %s (%s); the entry stays committed and "
                "no request is sent",
                SUBMIT_REQUEST_FILENAME,
                exc,
            )
            raise api.MissingDependencyError(
                "提交请求体快照写入失败，未进入 submitting"
            ) from exc
        if existing is None:
            submission = PendingSubmission(
                client_submission_id=client_submission_id,
                phase="submitting",
                submit_request_hash=submit_hash,
                server_url=entry.server_url,
                pending_dir=entry.pending_dir,
                request_path=request_path,
                job_id=None,
                dataset_id=entry.dataset_id,
                created_at=self._now_iso(),
                void_reason=None,
            )
            self.ledger.pending_submissions.append(submission)
        else:
            submission = existing
            submission.phase = "submitting"
            # submit_request_hash is deliberately NOT rewritten when it
            # is already there: it is the same body (checked above), so
            # the latched value stays byte-for-byte the one written on
            # the first attempt.
            if not submission.submit_request_hash:
                # R-c: a row from an older version (or a hand edited
                # ledger) carries no hash yet, so there is nothing to
                # compare against; fill it once with this body's hash.
                submission.submit_request_hash = submit_hash
            submission.server_url = entry.server_url
            submission.pending_dir = entry.pending_dir
            submission.request_path = request_path
            submission.dataset_id = entry.dataset_id
            submission.void_reason = None
        self._save()
        self._write_meta(
            entry.pending_dir,
            {
                "server_url": entry.server_url,
                "upload_token": entry.upload_token,
                "client_submission_id": client_submission_id,
                "phase": "submitting",
                "created_at": entry.created_at,
                "expires_at": entry.expires_at,
                "staging_dir": entry.staging_dir or "",
                "dataset_id": entry.dataset_id,
            },
        )
        self.log.info(
            "submission latched %s (phase=submitting)",
            client_submission_id,
        )
        return submission

    def _post_job(
        self,
        client: Any,
        submission: PendingSubmission,
        outcome: SubmitOutcome,
        *,
        body: Optional[Mapping[str, Any]] = None,
    ) -> SubmitOutcome:
        try:
            payload = dict(
                body
                if body is not None
                else self.read_submit_snapshot(submission)
            )
            response = client.create_job(payload)
        except Exception as exc:
            return self._submit_failed(submission, outcome, exc)

        data = _as_mapping(response)
        job_id = data.get("job_id")
        if not job_id:
            return self._submit_failed(
                submission,
                outcome,
                api.MalformedResponseError("POST /jobs returned no job_id"),
            )
        submission.job_id = str(job_id)
        self._save()
        outcome.ok = True
        outcome.job_id = str(job_id)
        outcome.response = data
        outcome.warnings = api.submit_warnings(data)
        outcome.phase = self._view(submission)
        outcome.action = "finalize"
        self.log.info(
            "job submitted %s -> job_id=%s",
            submission.client_submission_id,
            job_id,
        )
        return outcome

    def _submit_failed(
        self,
        submission: PendingSubmission,
        outcome: SubmitOutcome,
        exc: Exception,
    ) -> SubmitOutcome:
        status = getattr(exc, "http_status", None)
        code = str(getattr(exc, "code", "") or "")
        outcome.error_message = str(exc)
        outcome.error_code = code
        outcome.http_status = status
        if status == 409 and code == "VALIDATION_FAILED":
            # Same client_submission_id with a different body (§5.4.5).
            self._void_submission(submission, "submission_conflict")
            outcome.action = "void"
            outcome.reason = "submission_conflict"
            outcome.retry_hint = SUBMIT_RETRY_HINT
            outcome.error_message = SUBMIT_RETRY_HINT
            outcome.phase = self._view(submission)
            return outcome
        if status == 404 and code == "DATASET_NOT_FOUND":
            # N6: the twelve value enum of spec §5.4.4 has no dedicated
            # value for "the dataset was cleaned up server side", so this
            # mapping is frozen here as user_void + reason
            # 'dataset_not_found'.  It is NOT a user action: B5 must word
            # it as "数据集已被服务端清理，请重新上传".
            self._void_submission(submission, "user_void")
            outcome.action = "replan"
            outcome.reason = "dataset_not_found"
            outcome.retry_hint = "数据集已被服务端清理，请重新上传"
            outcome.phase = self._view(submission)
            return outcome
        if status == 401:
            outcome.action = "manual_retry"
            outcome.reason = "unauthorized"
            outcome.highlight_settings = True
            outcome.error_message = AUTH_MESSAGE
            outcome.retry_hint = "更新 Token 后请用同一 client_submission_id 手动重试"
        elif status == 400 and code == "VALIDATION_FAILED":
            outcome.action = "replan"
            outcome.reason = "validation_failed"
        elif status is not None and status >= 500:
            outcome.action = "reconcile"
            outcome.reason = "server_error"
        elif isinstance(exc, api.TransportError):
            outcome.action = "reconcile"
            outcome.reason = "no_response"
        else:
            outcome.action = "reconcile"
            outcome.reason = code or "unknown"
        outcome.phase = self._view(submission)
        self.log.info(
            "submit %s failed (HTTP %s / %s): %s; the entry stays "
            "submitting for reconciliation",
            submission.client_submission_id,
            status,
            code,
            exc,
        )
        return outcome

    def _void_submission(
        self, submission: PendingSubmission, reason: str
    ) -> None:
        if reason not in VOID_REASONS:
            raise api.ContractViolationError(
                "void_reason {0!r} is not one of the twelve values".format(
                    reason
                )
            )
        submission.void_reason = reason
        submission.phase = "void"
        self._save()
        self.log.info(
            "pending submission %s voided (void_reason=%s)",
            submission.client_submission_id,
            reason,
        )

    def read_submit_snapshot(
        self, submission: PendingSubmission
    ) -> Dict[str, Any]:
        """The submit request body snapshot (spec §5.4.5 step 5)."""

        path = submission.request_path or ""
        if not path or not osp.isfile(path):
            raise api.MissingDependencyError(
                "提交请求体快照缺失，无法重放：{0}".format(path)
            )
        return store_mod.read_json_file(path)

    def complete_submission(
        self,
        submission: PendingSubmission,
        job_id: str,
        *,
        response: Optional[Mapping[str, Any]] = None,
        record: Optional[TaskRecord] = None,
    ) -> TaskRecord:
        """The atomic triple of spec §5.4.5 step 4.

        One ledger write does all three things: the row moves into
        'records', the pending_uploads row disappears, and the pending_dir
        becomes unreferenced - which is then reclaimed immediately as
        cleanup path ① of spec §5.4.2 (reason 'submitted').

        R-a wording fix: a crash between that write and the immediate
        reclamation leaves an unreferenced directory with no row to drop
        (path ② has nothing to act on), so it is only reclaimed by the
        orphan sweep (pending_ttl_days / pending_max_gb, default 7 days).
        Nothing else needs it, so no replay data is lost.
        """

        data = _as_mapping(response)
        row = record if record is not None else TaskRecord()
        row.job_id = str(job_id)
        if not row.server_url:
            row.server_url = submission.server_url
        if not row.dataset_id:
            row.dataset_id = submission.dataset_id
        if not row.status:
            row.status = str(data.get("status") or "queued")
        if not row.created_at:
            row.created_at = str(
                data.get("created_at") or self._now_iso()
            )
        self.ledger.upsert_record(row)
        self.ledger.pending_uploads = [
            entry
            for entry in self.ledger.pending_uploads
            if entry.pending_dir != submission.pending_dir
        ]
        self.ledger.pending_submissions = [
            entry
            for entry in self.ledger.pending_submissions
            if entry.client_submission_id != submission.client_submission_id
        ]
        self._save()
        removed = self._reclaim_explicit(
            str(submission.pending_dir or ""), reason="submitted"
        )
        self.log.info(
            "submission %s recorded as job %s; pending_dir reclaimed=%s "
            "(reason: submitted)",
            submission.client_submission_id,
            job_id,
            removed,
        )
        return row

    # -- reconciliation (spec §5.4.1) -------------------------------

    def reconcile(
        self,
        client_for: Optional[Callable[[str], Any]] = None,
        *,
        cancel: Optional[threading.Event] = None,
        on_progress: Optional[Callable[[int, int], None]] = None,
        depth: int = 0,
    ) -> EntriesReport:
        """One pass of the six per phase recovery actions.

        Called on start-up and on every entry into the jobs page.  The
        pass re-reads tasks.json first, so every exemption is evaluated
        against the live ledger (spec §5.1.5, risk R9).
        """

        if depth > 2:
            self.log.error("reconciliation recursion guard tripped")
            return EntriesReport()
        self.reload()
        report = EntriesReport()
        for entry in list(self.ledger.pending_uploads):
            self._recover_upload(
                entry,
                report,
                client_for=client_for,
                cancel=cancel,
                on_progress=on_progress,
            )
        for submission in list(self.ledger.pending_submissions):
            self._recover_submission(
                submission, report, client_for=client_for
            )
        self._void_expired_planned(report)
        cleanup = self.cleanup_pending_dirs()
        report.messages.append(
            "pending 目录清理：移除 {0} 条条目，回收 {1} 个目录，"
            "释放 {2:.1f} MB".format(
                len(cleanup.removed_entries),
                len(cleanup.reclaimed_dirs),
                cleanup.freed_mb,
            )
        )
        banner = report.status_message()
        if banner:
            report.messages.insert(0, banner)
        return report

    def _client_for(
        self,
        server_url: str,
        client_for: Optional[Callable[[str], Any]],
    ) -> Any:
        if client_for is not None:
            return client_for(server_url)
        return self.new_client(server_url)

    def _recover_upload(
        self,
        entry: PendingUpload,
        report: EntriesReport,
        *,
        client_for: Optional[Callable[[str], Any]],
        cancel: Optional[threading.Event],
        on_progress: Optional[Callable[[int, int], None]],
    ) -> None:
        if entry.phase == "planned":
            report.planned.append(entry)
            expires_at = _entry_expiry(entry)
            if expires_at is not None and self._now() >= expires_at:
                self._void(entry, "token_expired_unused", action="replan")
                report.voided.append(entry)
                self.log.info(
                    "planned entry %s expired before its upload; voided as "
                    "token_expired_unused (informational)",
                    entry.upload_token,
                )
                return
            # Recovery action 1: keep going with this upload, never
            # re-plan (that would create a second dataset).
            self._replay_upload(
                entry,
                report,
                client_for=client_for,
                cancel=cancel,
                on_progress=on_progress,
            )
            return
        if entry.phase == "uploading":
            report.uploading.append(entry)
            self._replay_upload(
                entry,
                report,
                client_for=client_for,
                cancel=cancel,
                on_progress=on_progress,
            )
            return
        if entry.phase == "committed":
            report.committed.append(entry)
            self.log.info(
                "entry %s is committed (dataset_id=%s); the upload is not "
                "replayed, only the submit step is left",
                entry.upload_token,
                entry.dataset_id,
            )
            return
        report.submitted.append(entry)

    def _replay_upload(
        self,
        entry: PendingUpload,
        report: EntriesReport,
        *,
        client_for: Optional[Callable[[str], Any]],
        cancel: Optional[threading.Event],
        on_progress: Optional[Callable[[int, int], None]],
    ) -> None:
        try:
            client = self._client_for(entry.server_url, client_for)
            outcome = self.upload(
                client, entry, cancel=cancel, on_progress=on_progress
            )
        except api.RemoteTrainingError as exc:
            self.log.info(
                "replay of upload %s failed: %s", entry.upload_token, exc
            )
            report.retry_later.append(entry)
            return
        if outcome.ok and outcome.dataset_id:
            report.records.append(str(outcome.dataset_id))
            return
        if outcome.action == "replan":
            if entry not in report.voided:
                report.voided.append(entry)
            return
        report.retry_later.append(entry)

    def _recover_submission(
        self,
        submission: PendingSubmission,
        report: EntriesReport,
        *,
        client_for: Optional[Callable[[str], Any]],
    ) -> None:
        if submission.phase == "submitted":
            # A crash between the response and the ledger write: finish
            # the atomic move (recovery action 5).
            report.submitted.append(submission)
            if submission.job_id:
                self.complete_submission(submission, submission.job_id)
            return
        if submission.phase == "void":
            report.voided.append(submission)
            return
        if submission.phase != "submitting":
            return
        report.submitting.append(submission)
        outcome = SubmitOutcome(
            client_submission_id=submission.client_submission_id,
            submit_request_hash=submission.submit_request_hash,
            request_path=submission.request_path or "",
            dataset_id=submission.dataset_id,
        )
        try:
            client = self._client_for(submission.server_url, client_for)
            self._post_job(client, submission, outcome)
        except api.RemoteTrainingError as exc:
            self.log.info(
                "replay of submission %s failed: %s",
                submission.client_submission_id,
                exc,
            )
            report.retry_later.append(submission)
            return
        if outcome.ok and outcome.job_id:
            self.complete_submission(
                submission, outcome.job_id, response=outcome.response
            )
        else:
            report.retry_later.append(submission)

    def _void_expired_planned(self, report: EntriesReport) -> None:
        for entry in list(self.ledger.pending_uploads):
            if entry.phase != "planned":
                continue
            expires_at = _entry_expiry(entry)
            if expires_at is None or self._now() < expires_at:
                continue
            self._void(entry, "token_expired_unused", action="replan")
            if entry not in report.voided:
                report.voided.append(entry)

    # -- pending/<id>/ cleanup (spec §5.4.2) ------------------------

    def cleanup_pending_dirs(
        self,
        *,
        settings: Optional[Any] = None,
        now: Optional[float] = None,
    ) -> CleanupReport:
        """The cleanup paths of spec §5.4.2.

        'void' entries of any source and already recorded entries are
        dropped first; the remaining sweep only touches directories that
        the live ledger references nowhere (orphans, oldest first), and it
        re-reads the ledger before every single directory.
        """

        report = CleanupReport()
        self.reload()
        settled = store_mod.TERMINAL_PENDING_PHASES
        dropped = [
            entry
            for entry in self.ledger.pending_uploads
            if entry.phase in settled
        ] + [
            entry
            for entry in self.ledger.pending_submissions
            if entry.phase in settled
        ]
        if dropped:
            self.ledger.pending_uploads = [
                entry
                for entry in self.ledger.pending_uploads
                if entry.phase not in settled
            ]
            self.ledger.pending_submissions = [
                entry
                for entry in self.ledger.pending_submissions
                if entry.phase not in settled
            ]
            # Publish the removal first: the reference check below reads
            # tasks.json back, and it must see the settled rows gone.
            self._save()
            seen_paths = set()
            for entry in dropped:
                report.removed_entries.append(_entry_key(entry))
                self.log.info(
                    "待提交条目 %s 已从台账移除（phase=%s, void_reason=%s）",
                    _entry_key(entry),
                    entry.phase,
                    getattr(entry, "void_reason", None),
                )
                path = str(getattr(entry, "pending_dir", "") or "")
                if not path or path in seen_paths:
                    continue
                seen_paths.add(path)
                if not osp.isdir(path):
                    report.reclaimed_dirs.append(path)
                    continue
                # D1: spec §5.4.2 - "有台账引用的 pending_dir 绝不受影响".
                # A committed/submitting row may still share this
                # directory (e.g. 409 submission_conflict keeps the
                # replay data for the user's retry), so the live ledger
                # is re-read for this one directory before anything is
                # removed.
                if self.store.is_pending_dir_referenced(path):
                    if path not in report.kept_referenced:
                        report.kept_referenced.append(path)
                    self.log.warning(
                        "pending 目录 %s 仍被台账引用，跳过回收",
                        osp.basename(path),
                    )
                    continue
                # Cleanup path ② of spec §5.4.2: dropping the row
                # reclaims its pending directory in the same scan.
                size = store_mod.directory_size_bytes(path)
                reason = self._reclaim_reason(entry)
                if self.store.reclaim_pending_dir(path, reason=reason):
                    report.reclaimed_dirs.append(path)
                    report.freed_bytes += size

        settings = settings or self.store.load_settings()
        moment = float(now) if now is not None else self._now()
        ttl_seconds = float(settings.pending_ttl_days) * 86400.0
        max_bytes = float(settings.pending_max_gb) * (1024.0 ** 3)
        referenced = self._referenced_names(self.ledger)
        candidates = []
        for path in self._list_pending_dirs():
            name = osp.basename(path)
            if name in referenced:
                if path not in report.kept_referenced:
                    report.kept_referenced.append(path)
                continue
            candidates.append((_entry_anchor(path), path))
        candidates.sort(key=lambda item: item[0])
        total = self._pending_total_bytes()
        for anchor, path in candidates:
            expired = moment - anchor >= ttl_seconds
            over_capacity = total > max_bytes
            if not expired and not over_capacity:
                continue
            name = osp.basename(path)
            if name in self._live_referenced_names():
                report.kept_referenced.append(path)
                self.log.warning(
                    "pending 目录 %s 在本次扫描中仍被台账引用，跳过回收",
                    name,
                )
                continue
            size = store_mod.directory_size_bytes(path)
            # R-e: this sweep is the local orphan policy of spec §5.4.2
            # (pending_ttl_days / pending_max_gb), NOT one of the three
            # cleanup timings, so it must not reuse their reason words.
            if self.store.reclaim_pending_dir(
                path,
                reason="pending_orphan_ttl" if expired
                else "pending_capacity",
            ):
                report.reclaimed_dirs.append(path)
                report.freed_bytes += size
                total -= size
        if report.reclaimed_dirs:
            self.log.info(
                "回收 %d 个 pending 目录，释放 %.1f MB",
                len(report.reclaimed_dirs),
                report.freed_mb,
            )
        return report

    def _reclaim_reason(self, entry: Any) -> str:
        """The reason recorded for one settled row (spec §5.4.2).

        The three paths are told apart by the caller: an explicit user
        void is 'user_void', an already recorded row is 'submitted', and
        every automatic void path is 'void'.
        """

        if str(getattr(entry, "phase", "")) == "submitted":
            return "submitted"
        if str(getattr(entry, "void_reason", "") or "") == "user_void":
            return "user_void"
        return "void"

    def _referenced_names(self, ledger: TasksLedger) -> set:
        return self._names_of(ledger.referenced_pending_dirs())

    def _live_referenced_names(self) -> set:
        """Re-read tasks.json for one directory (spec §5.1.5)."""

        return self._referenced_names(self.store.load_ledger(migrate=False))

    def _names_of(self, paths: Sequence[str]) -> set:
        names = set()
        for path in paths:
            text = str(path).rstrip("/")
            if text:
                names.add(osp.basename(text))
        return names

    def _list_pending_dirs(self) -> List[str]:
        """Spec §5.4.2 walk, delegated to the store helper (N8)."""

        return list(self.store.iter_pending_dirs())

    def _pending_total_bytes(self) -> int:
        return self.store.pending_dirs_total_bytes()
