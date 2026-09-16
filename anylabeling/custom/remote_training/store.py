"""Local ledger for remote training (spec §5.1.5, §5.3, §5.4).

Client-side persistence only:

* server.json in the per-user directory (spec §5.3.3), the one
  convention crash_log already uses for ~/.xanylabeling/logs; the
  Token must not follow a user changeable work directory,
* tasks.json / settings.json under the work directory and the
  pending/<id>/ replay tree (spec §5.3.1),
* the staging owner marker and its TTL reclamation.

Atomic write and corruption recovery follow spec §5.3.4: write
<name>.tmp, flush + fsync, os.replace; keep the previous file as
tasks.json.bak; on a parse failure fall back to the backup and, when
that fails too, rename the damaged file to tasks.json.corrupt.<ts> and
rebuild an empty ledger instead of raising.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
import os.path as osp
import re
import shutil
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, List, Optional, Tuple

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "CORRUPT_TAG",
    "LEDGER_DIRNAME",
    "LocalSettings",
    "OWNER_FILENAME",
    "OwnerMarker",
    "PENDING_DIRNAME",
    "PENDING_PHASES",
    "PHASES",
    "SERVER_DIRNAME",
    "SERVER_DIR_ENV",
    "PendingSubmission",
    "PendingUpload",
    "RECORD_STATUSES",
    "RECORD_STATUS_ORPHANED",
    "ReclaimReport",
    "SCHEMA_VERSION",
    "SERVER_FILENAME",
    "SETTINGS_DEFAULTS",
    "SETTINGS_FILENAME",
    "STAGING_PREFIX",
    "STAGING_TTL_SECONDS",
    "ServerConfig",
    "Store",
    "TASKS_BACKUP_FILENAME",
    "TASKS_FILENAME",
    "TERMINAL_PENDING_PHASES",
    "TaskRecord",
    "TasksLedger",
    "VOID_REASONS",
    "WORK_DATA_DIRNAME",
    "atomic_write_bytes",
    "atomic_write_json",
    "atomic_write_text",
    "canonical_json_bytes",
    "create_staging_root",
    "default_ledger_dir",
    "default_server_dir",
    "directory_size_bytes",
    "fsync_directory",
    "lock_hash",
    "parse_iso8601_epoch",
    "pending_dir_name",
    "read_json_file",
    "read_owner_marker",
    "USER_DIRNAME",
    "sha256_hex",
    "utc_now",
    "utc_now_iso",
    "write_owner_marker",
]

SCHEMA_VERSION = 1
WORK_DATA_DIRNAME = "xanylabeling_data"
LEDGER_DIRNAME = "remote_training"
SERVER_FILENAME = "server.json"
TASKS_FILENAME = "tasks.json"
TASKS_BACKUP_FILENAME = "tasks.json.bak"
SETTINGS_FILENAME = "settings.json"
OWNER_FILENAME = "owner.json"
PENDING_DIRNAME = "pending"
#: Per-user directory of server.json (spec §5.3.3).  The convention is
#: the fork's own ~/.xanylabeling, the one crash_log writes its
#: ~/.xanylabeling/logs into (anylabeling/custom/crash_log/paths.py);
#: a Token must not follow a user changeable work directory.
USER_DIRNAME = ".xanylabeling"
SERVER_DIRNAME = "remote_training"
#: Escape hatch for troubleshooting and tests, like XANY_LOG_DIR: the
#: value is the directory itself and `~` is expanded.
SERVER_DIR_ENV = "XANY_REMOTE_TRAINING_SERVER_DIR"

#: Staging folders live in the system temp dir under this prefix and are
#: scanned by prefix only (spec §5.1.5).
STAGING_PREFIX = "xal_remote_training_"
#: Fixed staging TTL of spec §5.1.5: seven days.  It is a constant and is
#: deliberately not wired to settings.json.
STAGING_TTL_SECONDS = 7 * 24 * 60 * 60
CORRUPT_TAG = "corrupt"

#: phase enum, six values, shared by both pending arrays (spec §5.4.1).
PHASES: Tuple[str, ...] = (
    "planned",
    "uploading",
    "committed",
    "submitting",
    "submitted",
    "void",
)
#: Unfinished phases: still replayed, so their directories are exempt
#: from every reclamation path (spec §5.1.5, spec §5.4.2).
PENDING_PHASES: Tuple[str, ...] = (
    "planned",
    "uploading",
    "committed",
    "submitting",
)
#: The two settled phases.  Kept public for B4: the reconciliation scan
#: and the pending/<id>/ cleanup timing are driven by this complement of
#: PENDING_PHASES (spec §5.4.1, spec §5.4.2).
TERMINAL_PENDING_PHASES: Tuple[str, ...] = ("submitted", "void")

#: void_reason enum, twelve values (spec §5.4.4).
VOID_REASONS: Tuple[str, ...] = (
    "token_expired",
    "token_expired_unused",
    "manifest_mismatch",
    "validation_failed",
    "checksum_mismatch",
    "label_checksum_mismatch",
    "missing_labels",
    "invalid_label_format",
    "unsupported_extension",
    "upload_token_reused",
    "submission_conflict",
    "user_void",
)

RECORD_STATUS_ORPHANED = "orphaned"
#: Server statuses plus the client-only orphaned marker (spec §5.3.4).
RECORD_STATUSES: Tuple[str, ...] = (
    "queued",
    "preparing",
    "running",
    "completed",
    "failed",
    "cancelled",
    "interrupted",
    RECORD_STATUS_ORPHANED,
)

SETTINGS_DEFAULTS: Dict[str, Any] = {
    "keep_staging": False,
    "pending_max_gb": 5,
    "pending_ttl_days": 7,
}


# --------------------------------------------------------------------
# Small filesystem utilities
# --------------------------------------------------------------------


def canonical_json_bytes(obj: Any) -> bytes:
    """The one canonical JSON byte form (spec §5.4.1, cross-repo same)."""

    return json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def lock_hash(obj: Any) -> str:
    """Latch hash of one request body, as stored in the ledger."""

    return "sha256:" + sha256_hex(canonical_json_bytes(obj))


def fsync_directory(path: str) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def atomic_write_bytes(path: str, data: bytes) -> None:
    """Write path.tmp, fsync, then os.replace over path."""

    directory = osp.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    fsync_directory(directory)


def atomic_write_text(path: str, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: str, obj: Any, *, canonical: bool = False) -> None:
    """Atomic JSON write; canonical=True writes the spec §5.4.1 bytes."""

    if canonical:
        atomic_write_bytes(path, canonical_json_bytes(obj))
    else:
        atomic_write_text(
            path, json.dumps(obj, ensure_ascii=False, indent=2)
        )


def read_json_file(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def utc_now_iso() -> str:
    return utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso8601_epoch(value: Any) -> Optional[float]:
    """Parse an ISO8601 UTC timestamp into epoch seconds."""

    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.timestamp()


def directory_size_bytes(path: str) -> int:
    total = 0
    for current, _dirs, names in os.walk(path):
        for name in names:
            try:
                total += osp.getsize(osp.join(current, name))
            except OSError:
                continue
    return total


def default_ledger_dir() -> str:
    """Ledger root under the work directory (spec §5.3.1)."""

    from anylabeling.config import get_work_directory

    return osp.join(
        get_work_directory(), WORK_DATA_DIRNAME, LEDGER_DIRNAME
    )


def default_server_dir() -> str:
    """Per-user home of server.json (spec §5.3.3)."""

    requested = os.environ.get(SERVER_DIR_ENV, "").strip()
    if requested:
        return osp.abspath(osp.expanduser(requested))
    return osp.join(osp.expanduser("~"), USER_DIRNAME, SERVER_DIRNAME)


#: The three array-valued top-level keys of tasks.json (spec §5.3.2).
_LEDGER_LIST_KEYS: Tuple[str, ...] = (
    "pending_uploads",
    "pending_submissions",
    "records",
)


def _ledger_structure_problem(raw: Mapping[str, Any]) -> Optional[str]:
    """Describe a structural defect that must be treated as damage.

    A non-list array field, or a list holding a non-object element, is
    not something the loader may silently drop: dropping it and then
    saving would erase those bytes for good (spec §5.3.4).
    """

    for key in _LEDGER_LIST_KEYS:
        if key not in raw:
            continue
        value = raw.get(key)
        if value is None:
            continue
        if not isinstance(value, list):
            return f"{key} is not a list"
        for index, item in enumerate(value):
            if not isinstance(item, Mapping):
                return f"{key}[{index}] is not an object"
    return None


_SAFE_ID_CHARS = re.compile(r"[^0-9a-zA-Z]")


def pending_dir_name(value: str) -> str:
    """Directory-safe pending/<id>/ name (spec §5.4.2)."""

    prefix, _sep, body = str(value).partition("_")
    safe = _SAFE_ID_CHARS.sub("", body)[:8]
    if not safe:
        safe = sha256_hex(str(value).encode("utf-8"))[:8]
        prefix = prefix or "pending"
    return f"{prefix}_{safe}" if prefix else safe


# --------------------------------------------------------------------
# Lossless record base
# --------------------------------------------------------------------


class _RecordMixin:
    """Dict round trip that keeps unknown keys so a rewrite loses none."""

    extra: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        extra = data.pop("extra", None) or {}
        data.update(extra)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]):
        known = {item.name for item in fields(cls)}
        payload = {
            key: value
            for key, value in data.items()
            if key in known and key != "extra"
        }
        extra = {
            key: value for key, value in data.items() if key not in known
        }
        return cls(**payload, extra=extra)


def _extra_field() -> Any:
    return field(default_factory=dict, repr=False, compare=False)


# --------------------------------------------------------------------
# Records (spec §5.3.2)
# --------------------------------------------------------------------


@dataclass
class TaskRecord(_RecordMixin):
    """One row of the local task ledger.

    The 25 fields of spec §5.3.2 come first; the single addition is
    "manual_resume", a client local trace of the newest manual resume
    (spec §5.5.3 / §5.5.7 step 2) that has no place in the contract
    table.  It is additive, so a row written before it existed still
    loads (unknown keys are preserved either way).
    """

    #: True unless from_dict() saw a missing is_terminal (spec §5.3.2).
    _is_terminal_present = True

    job_id: str = ""
    client_job_name: str = ""
    server_url: str = ""
    dataset_id: Optional[str] = None
    dataset_dir: str = ""
    classes_file: str = ""
    task: str = ""
    model_family: str = ""
    model: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    status: str = ""
    is_terminal: bool = False
    attempt: int = 0
    resume_cycles: int = 0
    needs_attention: bool = False
    needs_attention_reason: Optional[str] = None
    calibration_source: Optional[str] = None
    artifact_suspect: bool = False
    partial_available: bool = False
    last_seq: int = 0
    last_seen_at: Optional[str] = None
    created_at: Optional[str] = None
    finished_at: Optional[str] = None
    download_path: Optional[str] = None
    notes: str = ""
    #: Client local write back of the last manual resume (spec §5.5.3 /
    #: §5.5.7 step 2): the "attempt" / "resume_cycles" / "mode" triple of
    #: the newest "manual_resume" event or resume response.  The three
    #: values themselves live in the documented "attempt" /
    #: "resume_cycles" fields; this one only keeps the resume trace.
    manual_resume: Dict[str, Any] = field(default_factory=dict)
    extra: Dict[str, Any] = _extra_field()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TaskRecord":
        record = super().from_dict(data)
        record._is_terminal_present = "is_terminal" in data
        return record

    def mark_manual_resume(
        self,
        attempt: Any = None,
        resume_cycles: Any = None,
        mode: Any = None,
    ) -> "TaskRecord":
        """Write a resume back into the row (spec §5.5.7 step 2).

        The counter fields are the authoritative ones; the three values
        are kept together under "manual_resume" as the resume trace.
        """

        payload: Dict[str, Any] = {}
        if attempt is not None:
            payload["attempt"] = attempt
        if resume_cycles is not None:
            payload["resume_cycles"] = resume_cycles
        if mode:
            payload["mode"] = str(mode)
        self.manual_resume = payload
        if attempt is not None:
            self.attempt = int(attempt)
        if resume_cycles is not None:
            self.resume_cycles = int(resume_cycles)
        return self

    def normalize(self) -> bool:
        """Backfill is_terminal from finished_at; True when changed."""

        if getattr(self, "_is_terminal_present", True):
            return False
        self.is_terminal = self.finished_at is not None
        self._is_terminal_present = True
        return True

    @property
    def is_orphaned(self) -> bool:
        return self.status == RECORD_STATUS_ORPHANED


@dataclass
class PendingUpload(_RecordMixin):
    """One row of pending_uploads (20 fields, key = upload_token)."""

    upload_token: str = ""
    phase: str = "planned"
    plan_request_hash: Optional[str] = None
    upload_request_hash: Optional[str] = None
    server_url: str = ""
    pending_dir: str = ""
    archive_path: Optional[str] = None
    manifest_path: Optional[str] = None
    staging_dir: Optional[str] = None
    dataset_dir: str = ""
    classes_file: str = ""
    task: str = ""
    val_ratio: Optional[float] = None
    seed: Optional[int] = None
    params: Dict[str, Any] = field(default_factory=dict)
    missing_images: List[Dict[str, Any]] = field(default_factory=list)
    dataset_id: Optional[str] = None
    created_at: Optional[str] = None
    expires_at: Optional[str] = None
    void_reason: Optional[str] = None
    extra: Dict[str, Any] = _extra_field()

    @property
    def unfinished(self) -> bool:
        return self.phase in PENDING_PHASES


@dataclass
class PendingSubmission(_RecordMixin):
    """One row of pending_submissions (10 fields)."""

    client_submission_id: str = ""
    phase: str = "submitting"
    submit_request_hash: Optional[str] = None
    server_url: str = ""
    pending_dir: str = ""
    request_path: Optional[str] = None
    job_id: Optional[str] = None
    dataset_id: Optional[str] = None
    created_at: Optional[str] = None
    void_reason: Optional[str] = None
    extra: Dict[str, Any] = _extra_field()

    @property
    def unfinished(self) -> bool:
        return self.phase in PENDING_PHASES


@dataclass
class TasksLedger:
    """tasks.json content (spec §5.3.2)."""

    schema_version: int = SCHEMA_VERSION
    pending_uploads: List[PendingUpload] = field(default_factory=list)
    pending_submissions: List[PendingSubmission] = field(
        default_factory=list
    )
    records: List[TaskRecord] = field(default_factory=list)
    extra: Dict[str, Any] = _extra_field()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TasksLedger":
        version = data.get("schema_version", SCHEMA_VERSION)
        if isinstance(version, bool) or not isinstance(version, int):
            version = SCHEMA_VERSION
        known = {
            "schema_version",
            "pending_uploads",
            "pending_submissions",
            "records",
        }
        uploads = [
            PendingUpload.from_dict(item)
            for item in data.get("pending_uploads") or []
            if isinstance(item, Mapping)
        ]
        submissions = [
            PendingSubmission.from_dict(item)
            for item in data.get("pending_submissions") or []
            if isinstance(item, Mapping)
        ]
        records = [
            TaskRecord.from_dict(item)
            for item in data.get("records") or []
            if isinstance(item, Mapping)
        ]
        return cls(
            schema_version=version,
            pending_uploads=uploads,
            pending_submissions=submissions,
            records=records,
            extra={
                key: value
                for key, value in data.items()
                if key not in known
            },
        )

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "schema_version": self.schema_version,
            "pending_uploads": [
                entry.to_dict() for entry in self.pending_uploads
            ],
            "pending_submissions": [
                entry.to_dict() for entry in self.pending_submissions
            ],
            "records": [entry.to_dict() for entry in self.records],
        }
        data.update(self.extra)
        return data

    def record(self, job_id: str) -> Optional[TaskRecord]:
        for item in self.records:
            if item.job_id == job_id:
                return item
        return None

    def upsert_record(self, record: TaskRecord) -> TaskRecord:
        for index, item in enumerate(self.records):
            if item.job_id == record.job_id:
                self.records[index] = record
                return record
        self.records.append(record)
        return record

    def upload(self, token: str) -> Optional[PendingUpload]:
        for item in self.pending_uploads:
            if item.upload_token == token:
                return item
        return None

    def submission(
        self, client_submission_id: str
    ) -> Optional[PendingSubmission]:
        for item in self.pending_submissions:
            if item.client_submission_id == client_submission_id:
                return item
        return None

    def referenced_staging_dirs(self) -> List[str]:
        """staging_dir values of unfinished pending uploads (§5.1.5)."""

        found: List[str] = []
        for entry in self.pending_uploads:
            if entry.unfinished and entry.staging_dir:
                found.append(str(entry.staging_dir))
        return found

    def referenced_pending_dirs(self) -> List[str]:
        """pending_dir values of every unfinished pending entry (§5.1.5)."""

        found: List[str] = []
        for entry in self.pending_uploads:
            if entry.unfinished and entry.pending_dir:
                found.append(str(entry.pending_dir))
        for entry in self.pending_submissions:
            if entry.unfinished and entry.pending_dir:
                found.append(str(entry.pending_dir))
        return found

    def problems(self) -> List[str]:
        """Contract violations of the ledger enums (never raises)."""

        issues: List[str] = []
        seen_tokens = set()
        for entry in self.pending_uploads:
            if entry.phase not in PHASES:
                issues.append(
                    f"pending_uploads phase={entry.phase!r} is not in "
                    "the six-value enum"
                )
            if entry.void_reason is not None and (
                entry.void_reason not in VOID_REASONS
            ):
                issues.append(
                    f"void_reason={entry.void_reason!r} is not in the "
                    "twelve-value enum"
                )
            if entry.upload_token in seen_tokens:
                issues.append(
                    f"duplicate upload_token {entry.upload_token!r}"
                )
            seen_tokens.add(entry.upload_token)
        seen_ids = set()
        for entry in self.pending_submissions:
            if entry.phase not in PHASES:
                issues.append(
                    f"pending_submissions phase={entry.phase!r} is not "
                    "in the six-value enum"
                )
            if entry.void_reason is not None and (
                entry.void_reason not in VOID_REASONS
            ):
                issues.append(
                    f"void_reason={entry.void_reason!r} is not in the "
                    "twelve-value enum"
                )
            if entry.client_submission_id in seen_ids:
                issues.append(
                    "duplicate client_submission_id "
                    f"{entry.client_submission_id!r}"
                )
            seen_ids.add(entry.client_submission_id)
        seen_jobs = set()
        for record in self.records:
            if not record.job_id:
                issues.append("record without job_id")
            if record.job_id in seen_jobs:
                issues.append(f"duplicate job_id {record.job_id!r}")
            seen_jobs.add(record.job_id)
        return issues


# --------------------------------------------------------------------
# server.json / settings.json (spec §5.3.3)
# --------------------------------------------------------------------


@dataclass
class ServerConfig:
    """server.json: the single server URL plus its Token."""

    server_url: str = ""
    api_key: str = ""
    updated_at: Optional[str] = None
    extra: Dict[str, Any] = _extra_field()

    @property
    def configured(self) -> bool:
        return bool(self.server_url)

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "server_url": self.server_url,
            "api_key": self.api_key,
            "updated_at": self.updated_at,
        }
        data.update(self.extra)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ServerConfig":
        known = {"server_url", "api_key", "updated_at"}
        return cls(
            server_url=str(data.get("server_url") or ""),
            api_key=str(data.get("api_key") or ""),
            updated_at=data.get("updated_at"),
            extra={
                key: value
                for key, value in data.items()
                if key not in known
            },
        )


@dataclass
class LocalSettings:
    """settings.json: three independent local keys (spec §5.3.3)."""

    keep_staging: bool = False
    pending_max_gb: float = 5
    pending_ttl_days: float = 7
    extra: Dict[str, Any] = _extra_field()

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "keep_staging": self.keep_staging,
            "pending_max_gb": self.pending_max_gb,
            "pending_ttl_days": self.pending_ttl_days,
        }
        data.update(self.extra)
        return data

    @classmethod
    def from_tolerant(
        cls,
        data: Mapping[str, Any],
        *,
        log: Optional[logging.Logger] = None,
    ) -> "LocalSettings":
        """Missing key or wrong type falls back to the default (§5.3.3)."""

        settings = cls()
        problems: List[str] = []
        if "keep_staging" in data:
            value = data.get("keep_staging")
            if isinstance(value, bool):
                settings.keep_staging = value
            else:
                problems.append("keep_staging is not a boolean")
        for name in ("pending_max_gb", "pending_ttl_days"):
            if name not in data:
                continue
            value = data.get(name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or value <= 0
            ):
                problems.append(f"{name} is not a positive number")
            else:
                setattr(settings, name, value)
        settings.extra = {
            key: value
            for key, value in data.items()
            if key not in SETTINGS_DEFAULTS
        }
        if problems:
            (log or _LOGGER).warning(
                "%s: %s; falling back to the defaults for those keys",
                SETTINGS_FILENAME,
                "; ".join(problems),
            )
        return settings


# --------------------------------------------------------------------
# Staging owner marker (spec §5.1.5)
# --------------------------------------------------------------------


@dataclass
class OwnerMarker:
    """The only two consumed fields of owner.json."""

    pid: Optional[int] = None
    started_at: Optional[str] = None


def read_owner_marker(staging_dir: str) -> Optional[OwnerMarker]:
    """Parse owner.json; a half written file yields None."""

    path = osp.join(staging_dir, OWNER_FILENAME)
    if not osp.isfile(path):
        return None
    try:
        data = read_json_file(path)
    except (OSError, ValueError):
        return None
    if not isinstance(data, Mapping):
        return None
    pid = data.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int):
        pid = None
    started_at = data.get("started_at")
    return OwnerMarker(
        pid=pid,
        started_at=str(started_at) if started_at else None,
    )


def write_owner_marker(
    staging_dir: str,
    *,
    pid: Optional[int] = None,
    started_at: Optional[str] = None,
    log: Optional[logging.Logger] = None,
) -> bool:
    """Atomically write owner.json; failure never fails the upload."""

    payload = {
        "pid": int(os.getpid() if pid is None else pid),
        "started_at": started_at or utc_now_iso(),
    }
    try:
        atomic_write_json(osp.join(staging_dir, OWNER_FILENAME), payload)
    except OSError as exc:
        (log or _LOGGER).warning(
            "could not write %s in %s: %s", OWNER_FILENAME, staging_dir, exc
        )
        return False
    return True


def create_staging_root(
    *,
    parent: Optional[str] = None,
    pid: Optional[int] = None,
    log: Optional[logging.Logger] = None,
) -> str:
    """mkdtemp under the app prefix and stamp owner.json immediately."""

    if parent:
        os.makedirs(parent, exist_ok=True)
    root = tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=parent or None)
    write_owner_marker(root, pid=pid, log=log)
    return root


@dataclass
class ReclaimReport:
    """Outcome of one staging scan (spec §5.1.5)."""

    reclaimed: List[str] = field(default_factory=list)
    kept_referenced: List[str] = field(default_factory=list)
    kept_unexpired: List[str] = field(default_factory=list)
    freed_bytes: int = 0
    anchors: Dict[str, str] = field(default_factory=dict)

    @property
    def reclaimed_count(self) -> int:
        return len(self.reclaimed)

    @property
    def freed_mb(self) -> float:
        return self.freed_bytes / (1024.0 * 1024.0)


# --------------------------------------------------------------------
# Store
# --------------------------------------------------------------------


class Store:
    """Ledger reader / writer for one work directory.

    Every path belongs to that directory except server.json: the URL
    and the Token are the user's, not the work directory's (spec
    §5.3.3), so they live in server_dir.
    """

    def __init__(
        self,
        base_dir: Optional[str] = None,
        *,
        log: Optional[Any] = None,
        server_dir: Optional[str] = None,
    ) -> None:
        self.base_dir = (
            osp.abspath(base_dir) if base_dir else default_ledger_dir()
        )
        self.server_dir = (
            osp.abspath(osp.expanduser(server_dir))
            if server_dir
            else default_server_dir()
        )
        self.log = log or _LOGGER

    # -- paths ------------------------------------------------------

    @property
    def server_path(self) -> str:
        """The live server.json (per-user, spec §5.3.3)."""

        return osp.join(self.server_dir, SERVER_FILENAME)

    @property
    def legacy_server_path(self) -> str:
        """The pre §5.3.3 server.json inside the work directory.

        Kept readable forever: the one time migration below copies its
        bytes to server_path but never moves or removes it.
        """

        return osp.join(self.base_dir, SERVER_FILENAME)

    @property
    def tasks_path(self) -> str:
        return osp.join(self.base_dir, TASKS_FILENAME)

    @property
    def tasks_bak_path(self) -> str:
        return osp.join(self.base_dir, TASKS_BACKUP_FILENAME)

    @property
    def settings_path(self) -> str:
        return osp.join(self.base_dir, SETTINGS_FILENAME)

    @property
    def pending_root(self) -> str:
        return osp.join(self.base_dir, PENDING_DIRNAME)

    def pending_dir_path(self, pending_id: str) -> str:
        return osp.join(self.pending_root, pending_dir_name(pending_id))

    def ensure_pending_dir(self, pending_id: str) -> str:
        path = self.pending_dir_path(pending_id)
        os.makedirs(path, exist_ok=True)
        return path

    def write_pending_json(self, path: str, obj: Any) -> None:
        """Replay data is always stored as canonical bytes (§5.4.1)."""

        atomic_write_json(path, obj, canonical=True)

    # -- tasks.json -------------------------------------------------

    def _read_ledger_file(
        self, path: str
    ) -> Tuple[Optional[TasksLedger], int, str]:
        if not osp.isfile(path):
            return None, 0, "missing"
        try:
            raw = read_json_file(path)
        except (OSError, ValueError):
            return None, 0, "corrupt"
        if not isinstance(raw, Mapping):
            return None, 0, "corrupt"
        problem = _ledger_structure_problem(raw)
        if problem is not None:
            # The message names the file actually read: a structural
            # defect found in tasks.json.bak must not be reported as
            # tasks.json (that misleads field debugging).
            self.log.warning(
                "%s: %s; treating the file as damaged so no entry is "
                "dropped silently",
                osp.basename(path) or TASKS_FILENAME,
                problem,
            )
            return None, 0, "corrupt"
        ledger = TasksLedger.from_dict(raw)
        normalized = sum(
            1 for record in ledger.records if record.normalize()
        )
        return ledger, normalized, "ok"

    def _write_ledger(self, ledger: TasksLedger, *, backup: bool) -> None:
        os.makedirs(self.base_dir, exist_ok=True)
        tmp = self.tasks_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(ledger.to_dict(), handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        if backup and osp.isfile(self.tasks_path):
            try:
                shutil.copy2(self.tasks_path, self.tasks_bak_path)
            except OSError as exc:
                self.log.warning(
                    "could not refresh %s: %s", TASKS_BACKUP_FILENAME, exc
                )
        os.replace(tmp, self.tasks_path)
        fsync_directory(self.base_dir)

    def _try_write_ledger(self, ledger: TasksLedger, *, backup: bool) -> bool:
        """Persist during recovery; a read-only or full disk only logs."""

        try:
            self._write_ledger(ledger, backup=backup)
        except OSError as exc:
            self.log.error(
                "could not persist %s (%s); continuing in memory",
                TASKS_FILENAME,
                exc,
            )
            return False
        return True

    def _quarantine(self, path: str) -> Optional[str]:
        """Rename a damaged file aside; never delete it (spec §5.3.4)."""

        if not osp.isfile(path):
            return None
        stamp = _dt.datetime.now().strftime("%Y%m%d%H%M%S")
        target = f"{path}.{CORRUPT_TAG}.{stamp}"
        index = 0
        while osp.exists(target):
            index += 1
            target = f"{path}.{CORRUPT_TAG}.{stamp}.{index}"
        try:
            os.replace(path, target)
        except OSError as exc:
            self.log.warning("could not quarantine %s: %s", path, exc)
            return None
        return target

    def load_ledger(self, *, migrate: bool = True) -> TasksLedger:
        """Read tasks.json; never raises (spec §5.3.4).

        A damaged file (unparseable, or one whose three array fields are
        structurally wrong) falls back to tasks.json.bak, then to an
        empty ledger, always quarantining the damaged file aside.  Every
        write on this path is best effort: a read-only directory or a
        full disk is logged, never raised.
        """

        ledger, normalized, state = self._read_ledger_file(self.tasks_path)
        if state == "ok" and ledger is not None:
            if normalized and migrate:
                self._try_write_ledger(ledger, backup=True)
            return ledger
        if state == "missing":
            return TasksLedger()
        backup, _count, backup_state = self._read_ledger_file(
            self.tasks_bak_path
        )
        quarantined = self._quarantine(self.tasks_path)
        if backup_state == "ok" and backup is not None:
            self.log.warning(
                "%s is unreadable; recovered from %s; damaged file kept "
                "at %s",
                TASKS_FILENAME,
                TASKS_BACKUP_FILENAME,
                quarantined,
            )
            self._try_write_ledger(backup, backup=False)
            return backup
        self.log.warning(
            "%s and %s are unreadable; rebuilt an empty ledger; damaged "
            "file kept at %s",
            TASKS_FILENAME,
            TASKS_BACKUP_FILENAME,
            quarantined,
        )
        empty = TasksLedger()
        self._try_write_ledger(empty, backup=False)
        return empty

    def save_ledger(self, ledger: TasksLedger) -> None:
        """Persist the ledger, refreshing tasks.json.bak first."""

        self._write_ledger(ledger, backup=True)

    def corrupt_files(self) -> List[str]:
        """Damaged files kept aside by previous recoveries."""

        if not osp.isdir(self.base_dir):
            return []
        marker = f"{TASKS_FILENAME}.{CORRUPT_TAG}."
        try:
            names = os.listdir(self.base_dir)
        except OSError as exc:  # pragma: no cover - unreadable directory
            self.log.warning(
                "could not list %s: %s", self.base_dir, exc
            )
            return []
        return sorted(
            osp.join(self.base_dir, name)
            for name in names
            if name.startswith(marker)
        )

    def ledger_is_degraded(self) -> bool:
        """True when the ledger could not be read as a usable file.

        Spec §5.3.4 makes load_ledger() total: a damaged tasks.json (and
        a damaged backup) silently degrades to an empty ledger instead
        of raising, so 'no pending entry references this directory'
        cannot be told apart from 'the ledger that referenced it is
        gone' by looking at the returned object alone.  The quarantine
        file the recovery leaves behind is the observable evidence; it
        stays behind on purpose (§5.3.4 renames, never deletes), which
        biases the §5.1.5 exemption towards keeping a directory rather
        than deleting one a replay may still need - the fixed seven day
        TTL scan is the only thing that still reclaims it, and the
        dialog reports the state once at start-up so the permanent
        exemption is visible instead of merely logged.

        An unlistable directory counts as degraded: 'cannot tell' must
        not read as 'nothing was quarantined'.
        """

        if not osp.isdir(self.base_dir):
            return False
        marker = f"{TASKS_FILENAME}.{CORRUPT_TAG}."
        try:
            names = os.listdir(self.base_dir)
        except OSError as exc:
            self.log.warning(
                "could not list %s (%s); treating the ledger as "
                "degraded",
                self.base_dir,
                exc,
            )
            return True
        return any(name.startswith(marker) for name in names)

    # -- server.json / settings.json --------------------------------

    def _read_server_file(self, path: str) -> Optional[ServerConfig]:
        """Parse one server.json; a missing / unreadable file is None.

        Silent on purpose: the caller owns the wording of the warning,
        because "the legacy file is corrupt" and "the live file is
        corrupt" are two different messages (spec §5.3.3).
        """

        if not osp.isfile(path):
            return None
        try:
            raw = read_json_file(path)
        except (OSError, ValueError):
            return None
        if not isinstance(raw, Mapping):
            return None
        return ServerConfig.from_dict(raw)

    def _migrate_server_file(self) -> bool:
        """One time byte copy of the legacy file; True when it landed.

        The copy is deliberately not a move and the JSON is never
        rewritten: server.json may carry keys this version does not
        know, and those bytes must survive the migration (spec §5.3.4).
        Every failure is an OSError the caller turns into a warning.
        """

        with open(self.legacy_server_path, "rb") as handle:
            data = handle.read()
        atomic_write_bytes(self.server_path, data)
        self._chmod_server_file(self.server_path)
        return True

    def _write_server_file(self, path: str, payload: Any) -> None:
        """Atomic write plus the 0600 mode of spec §5.3.3."""

        os.makedirs(osp.dirname(path) or ".", exist_ok=True)
        atomic_write_json(path, payload)
        self._chmod_server_file(path)

    def _chmod_server_file(self, path: str) -> None:
        try:
            os.chmod(path, 0o600)
        except OSError as exc:
            self.log.warning(
                "could not chmod %s to 0600: %s", SERVER_FILENAME, exc
            )

    def load_server(self) -> ServerConfig:
        """Read server.json, migrating the legacy one on first sight.

        The order is fixed (spec §5.3.3):

        1. a live file in the per-user directory wins outright - it is
           never compared with, and never repaired from, the legacy
           file, which is only a source for the first migration;
        2. with no live file, a legacy file that parses as a JSON object
           is copied over, while a missing / damaged / non object one is
           returned as the empty configuration and not migrated;
        3. a failed migration keeps the legacy file (and logs), so the
           user still reaches the server.
        """

        if osp.isfile(self.server_path):
            config = self._read_server_file(self.server_path)
            if config is not None:
                return config
            self.log.warning(
                "%s is unreadable; using an empty configuration",
                SERVER_FILENAME,
            )
            return ServerConfig()
        legacy = self._read_server_file(self.legacy_server_path)
        if legacy is None:
            return ServerConfig()
        try:
            self._migrate_server_file()
        except OSError as exc:
            self.log.warning(
                "could not migrate %s to %s (%s); keeping the old file",
                self.legacy_server_path,
                self.server_path,
                exc,
            )
            return legacy
        self.log.info(
            "migrated %s to %s", self.legacy_server_path, self.server_path
        )
        return self._read_server_file(self.server_path) or legacy

    def save_server(self, config: ServerConfig) -> ServerConfig:
        """Persist URL and Token in the per-user directory (spec §5.3.3).

        A per-user directory that cannot be created or written falls
        back to the legacy ledger location, so a locked down home
        directory never costs the user the configuration; only when that
        second write fails too does the OSError reach the caller.
        """

        payload = config.to_dict()
        if not payload.get("updated_at"):
            payload["updated_at"] = utc_now_iso()
        try:
            self._write_server_file(self.server_path, payload)
        except OSError as exc:
            self.log.warning(
                "could not write %s to %s (%s); falling back to %s",
                SERVER_FILENAME,
                self.server_path,
                exc,
                self.legacy_server_path,
            )
            self._write_server_file(self.legacy_server_path, payload)
        config.updated_at = payload["updated_at"]
        return config

    def load_settings(self) -> LocalSettings:
        """Missing / unreadable / wrong typed keys all fall back (§5.3.3)."""

        if not osp.isfile(self.settings_path):
            return LocalSettings()
        try:
            raw = read_json_file(self.settings_path)
        except (OSError, ValueError) as exc:
            self.log.warning(
                "%s is unreadable (%s); falling back to the defaults",
                SETTINGS_FILENAME,
                exc,
            )
            return LocalSettings()
        if not isinstance(raw, Mapping):
            self.log.warning(
                "%s is not a JSON object; falling back to the defaults",
                SETTINGS_FILENAME,
            )
            return LocalSettings()
        return LocalSettings.from_tolerant(raw, log=self.log)

    def save_settings(self, settings: LocalSettings) -> None:
        os.makedirs(self.base_dir, exist_ok=True)
        atomic_write_json(self.settings_path, settings.to_dict())

    # -- pending/<id>/ (spec §5.4.2) --------------------------------

    def pending_dir_name_for(self, value: str) -> str:
        """Directory name of the pending entry with this key."""

        return pending_dir_name(value)

    def iter_pending_dirs(self) -> Iterator[str]:
        """Existing pending/<id>/ directories, name sorted."""

        root = self.pending_root
        if not osp.isdir(root):
            return
        for name in sorted(os.listdir(root)):
            path = osp.join(root, name)
            if name.endswith(".tmp"):
                continue
            if osp.isdir(path) and not osp.islink(path):
                yield path

    def pending_dirs_total_bytes(self) -> int:
        """Total bytes currently held by the pending tree."""

        return sum(
            directory_size_bytes(path) for path in self.iter_pending_dirs()
        )

    def is_pending_dir_referenced(self, path: str) -> bool:
        """True while the live ledger references this pending directory.

        The ledger is re-read on every call: the exemption set of spec
        §5.1.5 is evaluated per directory, never from a scan snapshot
        (risk R9).  An entry whose phase is already settled
        ('submitted' / 'void') no longer protects its directory.
        """

        target = osp.normpath(osp.abspath(path))
        for reference in self.load_ledger(
            migrate=False
        ).referenced_pending_dirs():
            try:
                candidate = osp.normpath(osp.abspath(reference))
            except (TypeError, ValueError):
                continue
            if candidate == target:
                return True
        return False

    def reclaim_pending_dir(self, path: str, *, reason: str) -> bool:
        """Remove one unreferenced pending directory and log the reason.

        The caller has already re-read tasks.json for this directory, so
        a still referenced directory must never reach this method.  The
        removal is the only place that deletes a pending directory and it
        always logs the three way reason of spec §5.4.2.
        """

        name = osp.basename(path)
        size = directory_size_bytes(path)
        try:
            shutil.rmtree(path, ignore_errors=True)
        except OSError as exc:  # pragma: no cover - defensive
            self.log.warning(
                "could not reclaim pending directory %s: %s", name, exc
            )
            return False
        if osp.isdir(path):
            self.log.warning(
                "pending directory %s survived the reclamation attempt",
                name,
            )
            return False
        self.log.info(
            "回收 pending 目录 %s（原因：%s），释放 %.1f MB",
            name,
            reason,
            size / (1024.0 * 1024.0),
        )
        return True

    # -- staging ----------------------------------------------------


    def create_staging_root(self, *, parent: Optional[str] = None) -> str:
        return create_staging_root(parent=parent, log=self.log)

    def write_owner_marker(
        self, staging_dir: str, *, pid: Optional[int] = None
    ) -> bool:
        return write_owner_marker(staging_dir, pid=pid, log=self.log)

    def read_owner_marker(self, staging_dir: str) -> Optional[OwnerMarker]:
        return read_owner_marker(staging_dir)

    def list_staging_dirs(self, temp_root: Optional[str] = None) -> List[str]:
        """Directories under the app prefix only (spec §5.1.5)."""

        root = temp_root or tempfile.gettempdir()
        try:
            names = os.listdir(root)
        except OSError:
            return []
        found: List[str] = []
        for name in sorted(names):
            if not name.startswith(STAGING_PREFIX):
                continue
            path = osp.join(root, name)
            if osp.isdir(path) and not osp.islink(path):
                found.append(path)
        return found

    def iter_referenced_staging_dirs(self) -> Iterator[str]:
        """Re-read tasks.json on every call: never a scan snapshot."""

        ledger = self.load_ledger(migrate=False)
        seen = set()
        for entry in ledger.pending_uploads:
            if not entry.unfinished:
                continue
            for value in (entry.pending_dir, entry.staging_dir):
                if value and value not in seen:
                    seen.add(value)
                    yield str(value)
        for entry in ledger.pending_submissions:
            if not entry.unfinished:
                continue
            if entry.pending_dir and entry.pending_dir not in seen:
                seen.add(entry.pending_dir)
                yield str(entry.pending_dir)

    def is_staging_referenced(self, path: str) -> bool:
        target = osp.normpath(osp.abspath(path))
        for reference in self.iter_referenced_staging_dirs():
            try:
                candidate = osp.normpath(osp.abspath(reference))
            except (TypeError, ValueError):
                continue
            if candidate == target:
                return True
        return False

    def _staging_anchor(self, path: str) -> Tuple[Optional[float], str]:
        marker = read_owner_marker(path)
        if marker is not None and marker.started_at:
            epoch = parse_iso8601_epoch(marker.started_at)
            if epoch is not None:
                return epoch, OWNER_FILENAME
        try:
            return osp.getmtime(path), "mtime"
        except OSError:
            return None, "unknown"

    def reclaim_staging_leftovers(
        self,
        *,
        temp_root: Optional[str] = None,
        now: Optional[float] = None,
        ttl_seconds: int = STAGING_TTL_SECONDS,
    ) -> ReclaimReport:
        """Reclaim expired staging dirs that no pending entry references.

        The exemption set is recomputed per directory (a live ledger read),
        never from a snapshot taken when the scan started (spec §5.1.5).
        """

        report = ReclaimReport()
        moment = float(now) if now is not None else _dt.datetime.now(
            _dt.timezone.utc
        ).timestamp()
        for path in self.list_staging_dirs(temp_root):
            if self.is_staging_referenced(path):
                report.kept_referenced.append(path)
                continue
            anchor, source = self._staging_anchor(path)
            report.anchors[path] = source
            if anchor is None:
                continue
            if moment - anchor < float(ttl_seconds):
                report.kept_unexpired.append(path)
                continue
            size = directory_size_bytes(path)
            shutil.rmtree(path, ignore_errors=True)
            report.reclaimed.append(path)
            report.freed_bytes += size
        if report.reclaimed:
            self.log.info(
                "reclaimed %d leftover staging director(ies), freed %.1f MB",
                report.reclaimed_count,
                report.freed_mb,
            )
        return report
