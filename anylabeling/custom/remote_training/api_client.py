"""HTTP client for the remote training contract (spec §3).

Route coverage
    The route table below mirrors all 16 entries of spec §3.2.1.  Client
    v1 consumes the 13 routes marked as consumed there (spec §3.2.3) and
    never calls the three read-only / management routes.

Error mapping
    Codes are only dispatched to exception types here.  Trigger
    conditions, the (HTTP, code) pairs and the "details" field names stay
    authoritative in spec §3.3 and are deliberately not restated: the
    client must not grow a second code table (spec §5.6.4).

Warning channels
    capabilities/health warnings (service status), upload response
    warnings and submit response warnings are three separate channels
    (spec §3.9).  Each parser validates that no code crossed a channel
    boundary.

Timeouts and retry
    Timeouts are client policy: connect 10 s, read 60 s for JSON and
    600 s for streaming bodies (spec §5.4.4).  The retry ladder of spec
    §5.5.6 is exposed as backoff_delay(); the state machine that drives
    it (counters, red bar, event driven waits) belongs to the polling
    worker.
"""

from __future__ import annotations

import html
import json
import os
import os.path as osp
import re
import time
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)
from urllib.parse import urlsplit

import requests

__all__ = [
    "API_ERROR_TYPES",
    "BACKOFF_LADDER",
    "BACKOFF_MAX_SECONDS",
    "CONSUMED_ROUTE_KEYS",
    "DEFAULT_BODY_EXCERPT_LIMIT",
    "HEALTH_STATE_OK",
    "HEALTH_STATE_TRAINING_DISABLED",
    "HEALTH_STATE_UNAUTHORIZED",
    "HEALTH_STATE_UNREACHABLE",
    "INSTALL_COMMAND",
    "JOBS_BATCH_LIMIT_DEFAULT",
    "NON_CONSUMED_ROUTE_KEYS",
    "NO_PROXY_MAPPING",
    "PROXY_POLICY_NOTE",
    "RED_BAR_NO_RESPONSE_THRESHOLD",
    "RESUME_MODES",
    "ROUTES",
    "SAFE_METHODS",
    "SCHEMA_VERSION",
    "TRAIN_PREFIX",
    "WARNING_CHANNELS",
    "ApiError",
    "ArtifactNotFoundError",
    "Capabilities",
    "ChecksumMismatchError",
    "CommittedTokenCapacityError",
    "ConflictError",
    "ContractViolationError",
    "DatasetInUseError",
    "DatasetNotFoundError",
    "DOWNLOAD_CHUNK_SIZE",
    "DOWNLOAD_PARTIAL_SUFFIX",
    "DownloadResult",
    "HealthProbe",
    "HealthStatus",
    "InsufficientVramError",
    "InternalServerError",
    "InvalidLabelFormatError",
    "JobArtifactsExpiredError",
    "JobNotFoundError",
    "JobNotResumableError",
    "NonJsonResponseError",
    "LabelChecksumMismatchError",
    "MalformedResponseError",
    "ManifestMismatchError",
    "MissingDependencyError",
    "MissingLabelsError",
    "ModelFamilyUnsupportedError",
    "NoDeviceAvailableError",
    "NotFoundError",
    "OptimizerUnsupportedError",
    "ParamNotOverridableError",
    "ParamOutOfRangeError",
    "ParameterError",
    "QuotaExceededError",
    "RemoteTrainingClient",
    "RemoteTrainingError",
    "RetryPolicy",
    "Route",
    "ServerError",
    "ServerUnavailableError",
    "StreamedResponse",
    "Timeouts",
    "TokenExpiredError",
    "TrainingDisabledError",
    "TransportError",
    "UnauthorizedError",
    "UnknownUploadTokenError",
    "UnsupportedExtensionError",
    "UploadCancelledError",
    "UploadInProgressError",
    "UploadTokenInvalidError",
    "ValidationFailedError",
    "VramEstimateUnavailableError",
    "WeightNotAvailableError",
    "backoff_delay",
    "chunked",
    "error_type_for",
    "host_is_loopback",
    "is_safe_method",
    "map_error",
    "normalize_base_url",
    "probe_health_states",
    "redact_secret",
    "request_proxies",
    "raise_for_envelope",
    "require_streaming_multipart",
    "reset_multipart_cache",
    "response_body_excerpt",
    "response_category",
    "route",
    "submit_warnings",
    "upload_warnings",
    "validate_warning_channel",
]

TRAIN_PREFIX = "/custom/train"
SCHEMA_VERSION = 1

# Client-side timeout policy: connect 10 s, JSON read 60 s, streaming
# (upload / download) read 600 s (spec §5.4.4).
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_READ_TIMEOUT = 60.0
UPLOAD_READ_TIMEOUT = 600.0

# "limit" of GET /jobs is 50 by default and is also the per-request cap
# of ?ids= (spec §3.11, spec §3.2.4).
JOBS_BATCH_LIMIT_DEFAULT = 50

# Backoff ladder shared by all safe requests (spec §5.5.6).
BACKOFF_LADDER = (5.0, 10.0, 20.0, 30.0)
BACKOFF_MAX_SECONDS = 30.0
RED_BAR_NO_RESPONSE_THRESHOLD = 3

INSTALL_COMMAND = (
    "pip install -r requirements/custom/remote_training_client.txt"
)
_MISSING_TOOLBELT = (
    "requests-toolbelt is required for streaming multipart uploads "
    "(MultipartEncoder); install it with:\n    " + INSTALL_COMMAND
)

SAFE_METHODS: FrozenSet[str] = frozenset({"GET"})

#: The only two values POST /jobs/{job_id}/resume accepts (spec §3.2.2).
RESUME_MODES: Tuple[str, ...] = ("resume", "restart")


# --------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------


class RemoteTrainingError(Exception):
    """Base class for every remote training client failure."""


class TransportError(RemoteTrainingError):
    """No HTTP response was received (connection, TLS or timeout)."""


class MalformedResponseError(TransportError):
    """The response body is not a valid spec §3.1 envelope.

    It carries the HTTP status whenever a response was actually
    received, so ``RetryPolicy`` never mistakes a gateway HTML page
    for "no HTTP response" (spec §5.5.6: any HTTP response clears the
    no-response counter and only 5xx keeps climbing the ladder).

    The same status is what makes a response that failed to decode
    distinguishable (see ``NonJsonResponseError``): the message text
    stays identical to v1 on purpose.
    """

    def __init__(
        self, message: str = "", http_status: Optional[int] = None
    ) -> None:
        super().__init__(message)
        self.http_status: Optional[int] = (
            int(http_status) if http_status is not None else None
        )


class NonJsonResponseError(MalformedResponseError):
    """A response that carried no decodable JSON envelope.

    The message and ``http_status`` are unchanged from v1
    (``"HTTP <status>: response is not JSON"``), so nothing that
    branches on the text or on ``RetryPolicy.retryable`` changes.  The
    class adds machine readable discriminators instead of new prose:

    content_type
        Lower case media type without parameters ("" when absent).
    category
        "proxy" when a middlebox most likely answered instead of the
        training service, "port" when the target answered with something
        that is not the contract, "other" otherwise.  See
        ``response_category``.
    body_excerpt
        Sanitised, truncated start of the body; never a header and never
        a credential (``redact_secret`` ran over it).
    url
        Full request URL when the caller could supply it.
    loopback
        True when the request went to a loopback / local address.

    Everything here is per-instance: ``str(exc)`` is unchanged.
    """

    def __init__(
        self,
        response: Any = None,
        message: Optional[str] = None,
        *,
        secret: Optional[str] = None,
        url: Optional[str] = None,
    ) -> None:
        status = getattr(response, "status_code", None)
        if message is None:
            message = f"HTTP {status}: response is not JSON"
        if url is None:
            url = getattr(response, "url", None)
        self.content_type: str = _content_type(response)
        self.body_excerpt: str = redact_secret(
            response_body_excerpt(response), secret
        )
        self.url: Optional[str] = None if url is None else str(url)
        self.loopback: bool = host_is_loopback(self.url)
        self.category: str = response_category(response)
        super().__init__(message, status)


class MissingDependencyError(RemoteTrainingError):
    """A declared client dependency is missing at runtime."""


class UploadCancelledError(RemoteTrainingError):
    """The user cancelled a streaming upload (client side)."""


class ContractViolationError(RemoteTrainingError):
    """A response violated a frozen contract rule (spec §3)."""


class ApiError(RemoteTrainingError):
    """A spec §3.1 error envelope ("success": false)."""

    def __init__(
        self,
        http_status: int,
        code: str,
        message: str = "",
        details: Optional[Mapping[str, Any]] = None,
        route: Optional[str] = None,
    ) -> None:
        super().__init__(f"HTTP {http_status} {code}: {message}")
        self.http_status = int(http_status)
        self.code = str(code)
        self.message = str(message or "")
        self.details: Dict[str, Any] = dict(details or {})
        self.route = route
        self.retry_after_header: Optional[str] = None

    @property
    def field(self) -> Optional[str]:
        value = self.details.get("field")
        return str(value) if value is not None else None

    @property
    def reason(self) -> Optional[str]:
        value = self.details.get("reason")
        return str(value) if value is not None else None


class UnauthorizedError(ApiError):
    """401 UNAUTHORIZED (upstream middleware)."""


class NotFoundError(ApiError):
    """404 family."""


class JobNotFoundError(NotFoundError):
    """404 JOB_NOT_FOUND."""


class DatasetNotFoundError(NotFoundError):
    """404 DATASET_NOT_FOUND."""


class ArtifactNotFoundError(NotFoundError):
    """404 ARTIFACT_NOT_FOUND."""


class ValidationFailedError(ApiError):
    """400 or 409 VALIDATION_FAILED; branch on http_status."""


class UploadTokenInvalidError(ApiError):
    """The upload token cannot be replayed (spec §5.4.4 例外 ②)."""


class TokenExpiredError(UploadTokenInvalidError):
    """400 TOKEN_EXPIRED."""


class UnknownUploadTokenError(UploadTokenInvalidError):
    """400 UNKNOWN_UPLOAD_TOKEN."""


class ManifestMismatchError(ApiError):
    """400 MANIFEST_MISMATCH."""


class ChecksumMismatchError(ApiError):
    """400 CHECKSUM_MISMATCH."""


class LabelChecksumMismatchError(ApiError):
    """400 LABEL_CHECKSUM_MISMATCH."""


class MissingLabelsError(ApiError):
    """400 MISSING_LABELS."""


class InvalidLabelFormatError(ApiError):
    """400 INVALID_LABEL_FORMAT."""


class UnsupportedExtensionError(ApiError):
    """400 UNSUPPORTED_EXTENSION."""


class UploadInProgressError(ApiError):
    """409 UPLOAD_IN_PROGRESS (auto retry once after 5 s)."""


class QuotaExceededError(ApiError):
    """413 QUOTA_EXCEEDED."""


class CommittedTokenCapacityError(ApiError):
    """429 COMMITTED_TOKEN_CAPACITY_EXCEEDED."""

    @property
    def retry_after_seconds(self) -> int:
        value = self.details.get("retry_after_seconds")
        try:
            seconds = int(value)
        except (TypeError, ValueError):
            seconds = 1
        return max(1, seconds)

    def wait_seconds(self) -> float:
        """Retry-After header first, details fallback (spec §5.4.4 ③)."""

        raw = self.retry_after_header
        if raw is not None:
            try:
                return max(1.0, float(str(raw).strip()))
            except (TypeError, ValueError):
                pass
        return float(self.retry_after_seconds)


class ConflictError(ApiError):
    """409 family outside VALIDATION_FAILED."""


class DatasetInUseError(ConflictError):
    """409 DATASET_IN_USE."""


class JobNotResumableError(ConflictError):
    """409 JOB_NOT_RESUMABLE."""


class JobArtifactsExpiredError(ConflictError):
    """409 JOB_ARTIFACTS_EXPIRED."""


class ParameterError(ApiError):
    """422 family."""


class ParamOutOfRangeError(ParameterError):
    """422 PARAM_OUT_OF_RANGE."""


class ParamNotOverridableError(ParameterError):
    """422 PARAM_NOT_OVERRIDABLE."""


class OptimizerUnsupportedError(ParameterError):
    """422 OPTIMIZER_UNSUPPORTED."""


class ModelFamilyUnsupportedError(ParameterError):
    """422 MODEL_FAMILY_UNSUPPORTED."""


class WeightNotAvailableError(ParameterError):
    """422 WEIGHT_NOT_AVAILABLE."""


class InsufficientVramError(ParameterError):
    """422 INSUFFICIENT_VRAM."""


class VramEstimateUnavailableError(ParameterError):
    """422 VRAM_ESTIMATE_UNAVAILABLE."""


class TrainingDisabledError(ApiError):
    """503 TRAINING_DISABLED."""


class NoDeviceAvailableError(ApiError):
    """503 NO_DEVICE_AVAILABLE."""


class ServerError(ApiError):
    """5xx family: retryable for safe requests only."""


class InternalServerError(ServerError):
    """500 INTERNAL_ERROR."""


class ServerUnavailableError(ServerError):
    """Generic 5xx without a code from the spec §3.3 table."""


#: code (spec §3.3) -> exception type.  The table is a dispatch only; it
#: never restates triggers, status pairs or details field names.
API_ERROR_TYPES: Dict[str, type] = {
    "VALIDATION_FAILED": ValidationFailedError,
    "CHECKSUM_MISMATCH": ChecksumMismatchError,
    "LABEL_CHECKSUM_MISMATCH": LabelChecksumMismatchError,
    "MISSING_LABELS": MissingLabelsError,
    "MANIFEST_MISMATCH": ManifestMismatchError,
    "INVALID_LABEL_FORMAT": InvalidLabelFormatError,
    "UNSUPPORTED_EXTENSION": UnsupportedExtensionError,
    "UNKNOWN_UPLOAD_TOKEN": UnknownUploadTokenError,
    "TOKEN_EXPIRED": TokenExpiredError,
    "UNAUTHORIZED": UnauthorizedError,
    "JOB_NOT_FOUND": JobNotFoundError,
    "DATASET_NOT_FOUND": DatasetNotFoundError,
    "ARTIFACT_NOT_FOUND": ArtifactNotFoundError,
    "DATASET_IN_USE": DatasetInUseError,
    "JOB_NOT_RESUMABLE": JobNotResumableError,
    "JOB_ARTIFACTS_EXPIRED": JobArtifactsExpiredError,
    "UPLOAD_IN_PROGRESS": UploadInProgressError,
    "QUOTA_EXCEEDED": QuotaExceededError,
    "PARAM_OUT_OF_RANGE": ParamOutOfRangeError,
    "PARAM_NOT_OVERRIDABLE": ParamNotOverridableError,
    "OPTIMIZER_UNSUPPORTED": OptimizerUnsupportedError,
    "MODEL_FAMILY_UNSUPPORTED": ModelFamilyUnsupportedError,
    "WEIGHT_NOT_AVAILABLE": WeightNotAvailableError,
    "INSUFFICIENT_VRAM": InsufficientVramError,
    "VRAM_ESTIMATE_UNAVAILABLE": VramEstimateUnavailableError,
    "COMMITTED_TOKEN_CAPACITY_EXCEEDED": CommittedTokenCapacityError,
    "TRAINING_DISABLED": TrainingDisabledError,
    "NO_DEVICE_AVAILABLE": NoDeviceAvailableError,
    "INTERNAL_ERROR": InternalServerError,
}


def error_type_for(code: str, http_status: int) -> type:
    """Return the exception type for one (code, HTTP status) pair."""

    mapped = API_ERROR_TYPES.get(str(code))
    if mapped is not None:
        return mapped
    try:
        status = int(http_status)
    except (TypeError, ValueError):
        status = 0
    if status >= 500:
        return ServerUnavailableError
    if status == 401:
        return UnauthorizedError
    return ApiError


def map_error(
    http_status: int,
    code: str,
    message: str = "",
    details: Optional[Mapping[str, Any]] = None,
    route: Optional[str] = None,
) -> ApiError:
    """Build the exception instance for one error envelope."""

    return error_type_for(code, http_status)(
        http_status, code, message, details, route
    )


def _header(headers: Any, name: str) -> Optional[str]:
    if not headers:
        return None
    try:
        return headers.get(name)
    except AttributeError:
        return None


def raise_for_envelope(
    payload: Any,
    http_status: int,
    route: Optional[str] = None,
    headers: Any = None,
) -> Any:
    """Return data of a success envelope or raise for an error one."""

    if not isinstance(payload, Mapping):
        raise MalformedResponseError(
            f"HTTP {http_status}: response is not a JSON object", http_status
        )
    success = payload.get("success")
    if success is True:
        return payload.get("data")
    if success is False:
        error = payload.get("error")
        if not isinstance(error, Mapping) or not error.get("code"):
            raise MalformedResponseError(
                f"HTTP {http_status}: error envelope without a code",
                http_status,
            )
        exc = map_error(
            http_status,
            error.get("code"),
            error.get("message", ""),
            error.get("details"),
            route,
        )
        exc.retry_after_header = _header(headers, "Retry-After")
        raise exc
    raise MalformedResponseError(
        f"HTTP {http_status}: envelope without a boolean success",
        http_status,
    )


# --------------------------------------------------------------------
# Warning channels (spec §3.9)
# --------------------------------------------------------------------

_SUBMIT_WARNING_CODES = frozenset(
    {"ADAMW_LR0_HIGH", "CONVERGED_TO_DEVICE_MAX"}
)
_UPLOAD_WARNING_CODES = frozenset(
    {
        "BACKGROUND_IMAGES",
        "ORPHAN_LABELS",
        "WARN_COUNT_MISMATCH",
        "SPLIT_CLASS_MISSING_VAL",
    }
)
_SERVICE_WARNING_CODES = frozenset(
    {
        "WEIGHTS_MISSING",
        "VRAM_TABLE_INCOMPLETE",
        "VRAM_CALIBRATION_FAILED",
        "VRAM_CALIBRATION_SKIPPED",
        "VRAM_CALIBRATION_DEFERRED",
        "BLOB_MATERIALIZE_DEGRADED",
        "OOM_RETRY_UNAVAILABLE",
    }
)

#: The three warning channels of spec §3.9 never share a code.
WARNING_CHANNELS: Dict[str, FrozenSet[str]] = {
    "submit": _SUBMIT_WARNING_CODES,
    "upload": _UPLOAD_WARNING_CODES,
    "service": _SERVICE_WARNING_CODES,
}


def validate_warning_channel(
    channel: str, warnings: Optional[Sequence[Any]]
) -> List[Mapping[str, Any]]:
    """Keep only this channel's codes; reject any cross-channel code."""

    allowed = WARNING_CHANNELS[channel]
    kept: List[Mapping[str, Any]] = []
    crossed: List[Any] = []
    for item in warnings or ():
        code = item.get("code") if isinstance(item, Mapping) else None
        if code in allowed:
            kept.append(item)
        else:
            crossed.append(code)
    if crossed:
        raise ContractViolationError(
            f"{channel} warnings carried foreign codes: {crossed}"
        )
    return kept


def submit_warnings(data: Any) -> List[Mapping[str, Any]]:
    """Submit response warnings only (spec §3.9 channel A)."""

    raw = data.get("warnings") if isinstance(data, Mapping) else None
    return validate_warning_channel("submit", raw)


def upload_warnings(data: Any) -> List[Mapping[str, Any]]:
    """Upload response warnings only (spec §3.9 channel B)."""

    raw = data.get("warnings") if isinstance(data, Mapping) else None
    return validate_warning_channel("upload", raw)


# --------------------------------------------------------------------
# Route table (spec §3.2.1 / §3.2.3)
# --------------------------------------------------------------------


@dataclass(frozen=True)
class Route:
    """One entry of the frozen route table."""

    key: str
    method: str
    path: str
    consumed: bool
    purpose: str = ""

    @property
    def full_path(self) -> str:
        return TRAIN_PREFIX + self.path


ROUTES: Tuple[Route, ...] = (
    Route(
        "capabilities",
        "GET",
        "/capabilities",
        True,
        "capability negotiation",
    ),
    Route("dataset_plan", "POST", "/datasets/plan", True, "upload stage 1"),
    Route(
        "dataset_upload", "POST", "/datasets/upload", True, "upload stage 2"
    ),
    Route("datasets_list", "GET", "/datasets", False, "dataset list"),
    Route(
        "dataset_delete",
        "DELETE",
        "/datasets/{dataset_id}",
        False,
        "soft delete",
    ),
    Route("cache_stats", "GET", "/cache/stats", False, "blob statistics"),
    Route("jobs_create", "POST", "/jobs", True, "submit"),
    Route("jobs_list", "GET", "/jobs", True, "list / batch query"),
    Route("job_detail", "GET", "/jobs/{job_id}", True, "job object"),
    Route(
        "job_events", "GET", "/jobs/{job_id}/events", True, "event stream"
    ),
    Route("job_cancel", "POST", "/jobs/{job_id}/cancel", True, "cancel"),
    Route("job_resume", "POST", "/jobs/{job_id}/resume", True, "resume"),
    Route(
        "job_files", "GET", "/jobs/{job_id}/files", True, "artifact manifest"
    ),
    Route(
        "job_file",
        "GET",
        "/jobs/{job_id}/files/{file_id}",
        True,
        "single artifact",
    ),
    Route(
        "job_download", "GET", "/jobs/{job_id}/download", True, "zip download"
    ),
    Route("health", "GET", "/health", True, "training health"),
)

_ROUTES_BY_KEY: Dict[str, Route] = {item.key: item for item in ROUTES}

CONSUMED_ROUTE_KEYS: Tuple[str, ...] = tuple(
    item.key for item in ROUTES if item.consumed
)
NON_CONSUMED_ROUTE_KEYS: Tuple[str, ...] = tuple(
    item.key for item in ROUTES if not item.consumed
)


def route(key: str) -> Route:
    """Return the frozen route entry for a key."""

    try:
        return _ROUTES_BY_KEY[key]
    except KeyError as exc:
        raise KeyError(f"unknown remote training route: {key}") from exc


def chunked(
    values: Sequence[Any], size: int = JOBS_BATCH_LIMIT_DEFAULT
) -> Iterator[Sequence[Any]]:
    """Split values into batches of at most size (spec §5.5.5)."""

    if size < 1:
        raise ValueError("size must be >= 1")
    for start in range(0, len(values), size):
        yield values[start : start + size]


def backoff_delay(consecutive_failures: int) -> float:
    """Wait before the next attempt (spec §5.5.6).

    c == 1 is the silent resend (0 s); the ladder is 5, 10, 20, 30 s and
    is capped at 30 s forever.
    """

    if consecutive_failures <= 1:
        return 0.0
    index = min(consecutive_failures - 2, len(BACKOFF_LADDER) - 1)
    return BACKOFF_LADDER[index]


def is_safe_method(method: str) -> bool:
    """True only for the idempotent methods the retry ladder applies to."""

    return str(method).upper() in SAFE_METHODS


@dataclass(frozen=True)
class RetryPolicy:
    """Retry decision inputs for safe requests (spec §5.5.6)."""

    max_delay: float = BACKOFF_MAX_SECONDS

    def delay_for(self, consecutive_failures: int) -> float:
        return min(
            backoff_delay(consecutive_failures), float(self.max_delay)
        )

    def retryable(self, method: str, error: Exception) -> bool:
        if not is_safe_method(method):
            return False
        if isinstance(error, UnauthorizedError):
            return False
        if isinstance(error, MalformedResponseError):
            # A response was received, so it is not a "no HTTP response"
            # failure: only 5xx (or an unknown origin) goes up the ladder.
            status = error.http_status
            return status is None or status >= 500
        return isinstance(error, (TransportError, ServerError))


@dataclass(frozen=True)
class Timeouts:
    """Client timeout policy in seconds."""

    connect: float = DEFAULT_CONNECT_TIMEOUT
    read: float = DEFAULT_READ_TIMEOUT
    upload_read: float = UPLOAD_READ_TIMEOUT

    def json_request(self) -> Tuple[float, float]:
        return (self.connect, self.read)

    def streaming_request(self) -> Tuple[float, float]:
        return (self.connect, self.upload_read)


# --------------------------------------------------------------------
# Capabilities / health parsing
# --------------------------------------------------------------------


class _PayloadView:
    """Read-only view over one negotiated payload."""

    REQUIRED_KEYS: Tuple[str, ...] = ()

    def __init__(self, payload: Mapping[str, Any]) -> None:
        if not isinstance(payload, Mapping):
            raise MalformedResponseError("payload is not a JSON object")
        self.payload: Dict[str, Any] = dict(payload)

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.payload[key]

    def __contains__(self, key: object) -> bool:
        return key in self.payload

    def keys(self):
        return self.payload.keys()

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.payload)

    def missing_keys(self) -> List[str]:
        return [key for key in self.REQUIRED_KEYS if key not in self.payload]

    def require_keys(self) -> None:
        missing = self.missing_keys()
        if missing:
            raise MalformedResponseError(
                "response is missing required keys: " + ", ".join(missing)
            )

    def service_warnings(self) -> List[Mapping[str, Any]]:
        """Service-status warnings only (spec §3.9 channel C)."""

        return validate_warning_channel(
            "service", self.payload.get("warnings")
        )


class Capabilities(_PayloadView):
    """Parsed GET /capabilities payload (spec §3.6, 18 required keys)."""

    REQUIRED_KEYS = (
        "schema_version",
        "server_version",
        "enabled",
        "tasks",
        "allow_weight_download",
        "allow_auto_batch",
        "oom_retry",
        "model_families",
        "param_schema",
        "optimizer_presets",
        "preset_policy",
        "devices",
        "queue",
        "cancel_grace_seconds",
        "vram_table",
        "calibration",
        "training_env",
        "warnings",
    )

    @property
    def enabled(self) -> bool:
        return bool(self.payload.get("enabled"))

    @property
    def server_version(self) -> Optional[str]:
        value = self.payload.get("server_version")
        return str(value) if value is not None else None

    @property
    def tasks(self) -> List[str]:
        return list(self.payload.get("tasks") or [])

    @property
    def cancel_grace_seconds(self) -> Optional[int]:
        """Negotiated SIGTERM grace; never hardcoded (spec §3.11)."""

        value = self.payload.get("cancel_grace_seconds")
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

    def require_cancel_grace_seconds(self) -> int:
        """Return the negotiated grace or fail loudly when absent."""

        value = self.cancel_grace_seconds
        if value is None:
            raise MalformedResponseError(
                "capabilities is missing cancel_grace_seconds"
            )
        return value

    def family(self, name: str) -> Mapping[str, Any]:
        families = self.payload.get("model_families") or {}
        value = families.get(name)
        return value if isinstance(value, Mapping) else {}

    def family_presets(self, name: str) -> List[str]:
        return list(self.family(name).get("presets") or [])

    def default_preset(self, family: str) -> Optional[str]:
        policy = self.payload.get("preset_policy") or {}
        mapping = policy.get("default_preset") or {}
        value = mapping.get(family)
        return str(value) if value is not None else None

    def vram_entry(
        self, model: str, task: str
    ) -> Optional[Mapping[str, Any]]:
        table = self.payload.get("vram_table") or {}
        for entry in table.get("entries") or []:
            if not isinstance(entry, Mapping):
                continue
            if entry.get("model") == model and entry.get("task") == task:
                return entry
        return None

    def calibration_source(self, model: str, task: str) -> Optional[str]:
        """auto / manual / default for one (model, task) (spec §5.3.2)."""

        entry = self.vram_entry(model, task)
        if entry is None:
            return None
        value = entry.get("source")
        return str(value) if value is not None else None

    def max_batch(self, model: str, task: str) -> Optional[int]:
        entry = self.vram_entry(model, task)
        if entry is None:
            return None
        value = entry.get("max_batch")
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

    def unschedulable_reason(
        self, model: str, task: str
    ) -> Optional[str]:
        table = self.payload.get("vram_table") or {}
        for item in table.get("unschedulable") or []:
            if not isinstance(item, Mapping):
                continue
            if item.get("model") == model and item.get("task") == task:
                value = item.get("reason")
                return str(value) if value is not None else None
        return None

    def weight_submittable(self, filename: str) -> bool:
        """weights_ready[file] or allow_weight_download (spec §3.6)."""

        if self.payload.get("allow_weight_download"):
            return True
        for family in (self.payload.get("model_families") or {}).values():
            if not isinstance(family, Mapping):
                continue
            ready = family.get("weights_ready") or {}
            if ready.get(filename) is True:
                return True
        return False


class HealthStatus(_PayloadView):
    """Parsed GET /health payload (spec §3.7, 12 required keys)."""

    REQUIRED_KEYS = (
        "enabled",
        "server_version",
        "time",
        "queue",
        "jobs",
        "devices",
        "work_dir",
        "training_env",
        "calibration",
        "weights",
        "blobs",
        "warnings",
    )

    @property
    def enabled(self) -> bool:
        return bool(self.payload.get("enabled"))

    @property
    def server_version(self) -> Optional[str]:
        value = self.payload.get("server_version")
        return str(value) if value is not None else None

    def training_available(self) -> bool:
        """True when /custom/train/* routes are callable (spec §3.7)."""

        return self.enabled


HEALTH_STATE_OK = "ok"
HEALTH_STATE_UNAUTHORIZED = "unauthorized"
HEALTH_STATE_TRAINING_DISABLED = "training_disabled"
HEALTH_STATE_UNREACHABLE = "unreachable"

#: The four observable states of one health probe under the four-state
#: auth table (spec §3.7): the client can only see 200 / 401 / no reply.
probe_health_states = (
    HEALTH_STATE_OK,
    HEALTH_STATE_UNAUTHORIZED,
    HEALTH_STATE_TRAINING_DISABLED,
    HEALTH_STATE_UNREACHABLE,
)


@dataclass
class HealthProbe:
    """Result of one connection probe."""

    state: str
    health: Optional[HealthStatus] = None
    error: Optional[RemoteTrainingError] = None


# --------------------------------------------------------------------
# Streaming multipart guard (spec §5.3.6 / §5.4.4)
# --------------------------------------------------------------------

_MULTIPART_ENCODER = None
_MULTIPART_MONITOR = None


def reset_multipart_cache() -> None:
    """Forget the cached requests-toolbelt import (tests / late install)."""

    global _MULTIPART_ENCODER, _MULTIPART_MONITOR
    _MULTIPART_ENCODER = None
    _MULTIPART_MONITOR = None


def require_streaming_multipart() -> Tuple[Any, Any]:
    """Return (MultipartEncoder, MultipartEncoderMonitor).

    A missing dependency is reported explicitly with the install command;
    there is deliberately no fallback to the plain "files" keyword of
    requests, which would buffer the whole archive in memory (§5.3.6).
    """

    global _MULTIPART_ENCODER, _MULTIPART_MONITOR
    if _MULTIPART_ENCODER is None:
        try:
            from requests_toolbelt.multipart.encoder import (  # noqa: E501
                MultipartEncoder,
                MultipartEncoderMonitor,
            )
        except Exception as exc:  # pragma: no cover - environment only
            raise MissingDependencyError(_MISSING_TOOLBELT) from exc
        _MULTIPART_ENCODER = MultipartEncoder
        _MULTIPART_MONITOR = MultipartEncoderMonitor
    return _MULTIPART_ENCODER, _MULTIPART_MONITOR


# --------------------------------------------------------------------
# Streaming download response
# --------------------------------------------------------------------


#: Download chunk size: 64 KiB keeps both the progress callback and the
#: cancellation check responsive without one syscall per byte.
DOWNLOAD_CHUNK_SIZE = 1 << 16
#: Suffix of the half written file.  The target path only appears once
#: the stream really reached EOF, which is the completion criterion of
#: spec §5.6.3 (never "a size the manifest claimed").
DOWNLOAD_PARTIAL_SUFFIX = ".part"


@dataclass(frozen=True)
class DownloadResult:
    """Outcome of one streamed download (spec §5.6.3)."""

    path: str
    bytes_written: int
    content_length: Optional[int] = None

    @property
    def complete(self) -> bool:
        """True when the received byte count matches Content-Length.

        A response without Content-Length (Transfer-Encoding: chunked)
        reports None and is never called incomplete: EOF is the only
        completion criterion, so this flag is a diagnostic, not a gate.
        """

        if self.content_length is None:
            return True
        return int(self.bytes_written) == int(self.content_length)


class StreamedResponse:
    """A successful download response (spec §3.1: no JSON envelope).

    Change registration (B2 frozen file; kept true across B7):
      file: anylabeling/custom/remote_training/api_client.py
      anchors: (a) the top import block gains "import os"; (b)
        DOWNLOAD_CHUNK_SIZE / DOWNLOAD_PARTIAL_SUFFIX / DownloadResult
        and StreamedResponse.save_to sit right above this class;
        (c) B7 round 3 narrowed two download signatures: open_job_file
        is now (self, job_id, file_id) - the range_header / if_range
        parameters were deleted - and _open_stream is now (self, key,
        *, path_params, params) - its headers parameter was deleted.
      contract: the class, its properties and its constructor are
        unchanged, so every existing caller behaves byte for byte as
        before; save_to is a new method only the B7 download path
        calls.  (c) removes capability instead of adding it: v1 never
        resumes (spec §5.6.3), and no caller ever passed those
        parameters.  auth headers are still added by _send, and the
        etag / content_range readers stay because spec §3.10.6 wants
        the ETag kept verbatim for a later v2.
    """

    def __init__(self, response: Any, route: Optional[str] = None) -> None:
        self._response = response
        self.route = route

    @property
    def status_code(self) -> int:
        return int(self._response.status_code)

    @property
    def raw(self) -> Any:
        return self._response

    @property
    def content_type(self) -> str:
        return _content_type(self._response)

    @property
    def content_length(self) -> Optional[int]:
        value = _header(self._response.headers, "Content-Length")
        if value is None:
            return None
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    @property
    def etag(self) -> Optional[str]:
        return _header(self._response.headers, "ETag")

    @property
    def content_range(self) -> Optional[str]:
        return _header(self._response.headers, "Content-Range")

    def iter_chunks(self, chunk_size: int = 1 << 16) -> Iterator[bytes]:
        for chunk in self._response.iter_content(chunk_size):
            if chunk:
                yield chunk

    def save_to(
        self,
        path: str,
        *,
        on_progress: Optional[Callable[[int, Optional[int]], None]] = None,
        cancel: Optional[Callable[[], bool]] = None,
        chunk_size: int = DOWNLOAD_CHUNK_SIZE,
    ) -> DownloadResult:
        """Stream this response into `path` until EOF (spec §5.6.3).

        The body is written to `path` + DOWNLOAD_PARTIAL_SUFFIX and
        renamed onto `path` only after the stream ended, so a cancelled
        or truncated transfer never leaves a file that looks complete
        (completion is EOF, not a byte count).  `on_progress` receives
        (received, total) where total is the response's own
        Content-Length or None for a chunked body: the caller must not
        fall back to the manifest's uncompressed sizes - spec §5.6.3
        forbids that denominator because a zip is a compressed stream.
        `cancel` is polled between chunks and raises InterruptedError.
        v1 has no resume: no Range / If-Range header is ever sent.
        """

        target = str(path or "")
        if not target:
            raise ValueError("a download target path is required")
        # Poll before the first chunk.  A 0 byte body never enters the
        # loop below, so without this check a cancel that arrives before
        # (or while) the body is awaited would promote an empty .part to
        # the target path and report success.
        if cancel is not None and cancel():
            raise InterruptedError("下载已取消")
        total = self.content_length
        partial = target + DOWNLOAD_PARTIAL_SUFFIX
        received = 0
        try:
            with open(partial, "wb") as handle:
                for chunk in self.iter_chunks(chunk_size):
                    if cancel is not None and cancel():
                        raise InterruptedError("下载已取消")
                    handle.write(chunk)
                    received += len(chunk)
                    if on_progress is not None:
                        on_progress(received, total)
                handle.flush()
        except BaseException:
            self._discard_partial(partial)
            raise
        os.replace(partial, target)
        return DownloadResult(
            path=target, bytes_written=received, content_length=total
        )

    @staticmethod
    def _discard_partial(path: str) -> None:
        """Drop the half written file; never fail the caller over it."""

        try:
            os.remove(path)
        except OSError:  # pragma: no cover - already gone / not ours
            pass

    def close(self) -> None:
        self._response.close()

    def __enter__(self) -> "StreamedResponse":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


def _content_type(response: Any) -> str:
    raw = _header(response.headers, "Content-Type") or ""
    return str(raw).split(";")[0].strip().lower()


# --------------------------------------------------------------------
# Client
# --------------------------------------------------------------------


def normalize_base_url(base_url: str) -> str:
    """Return the server root without a trailing slash."""

    text = str(base_url or "").strip()
    return text.rstrip("/")


def _decode_json(response: Any, secret: Optional[str] = None) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise NonJsonResponseError(response, secret=secret) from exc


# --------------------------------------------------------------------
# Proxy policy and non-JSON diagnostics
# --------------------------------------------------------------------

#: Characters of a body kept in NonJsonResponseError.body_excerpt.
DEFAULT_BODY_EXCERPT_LIMIT = 200

#: This client never uses an HTTP/HTTPS proxy: every request carries
#: this pair of empty mappings explicitly, so neither the environment
#: nor a system (WinINET) configuration can route it - the loopback
#: address included.
NO_PROXY_MAPPING: Dict[str, None] = {"http": None, "https": None}

#: One line description of the policy, for callers that display it.
PROXY_POLICY_NOTE = "本客户端从不使用 HTTP / HTTPS 代理（逐请求显式禁代理）"

#: Key/value pairs that must never reach a message or an excerpt.
#: The value side runs to the end of the line on purpose: a
#: "Bearer <token>" credential must not leave its first word behind.
_REDACT_PATTERN = re.compile(
    r"(?i)(authorization|token|cookie|set-cookie"
    r"|x-api-key|api[_-]?key)\b\s*[:=]\s*\S.*"
)


def request_proxies(url: Any = None) -> Dict[str, None]:
    """Always return a copy of NO_PROXY_MAPPING.

    Loopback and non-loopback hosts are treated identically: this
    client simply never uses a proxy (recorded user decision).
    """

    return dict(NO_PROXY_MAPPING)


def host_is_loopback(url: Any) -> bool:
    """True only for the loopback / local address forms the client names.

    Used for diagnostics and wording only; it never selects a proxy
    policy (that is request_proxies, which is proxy free for every
    host).  Accepted: "localhost", "*.localhost", dotted quads
    "127.<x>.<y>.<z>" (every octet digits only), "0.0.0.0", "::1" and
    "[::1]".  Scheme-less input is treated as a host and port.
    """

    if not url:
        return False
    try:
        parts = urlsplit(str(url))
    except ValueError:  # pragma: no cover - urlsplit rarely raises
        return False
    if not parts.scheme and not parts.netloc:
        parts = urlsplit("//" + str(url))
    host = str(parts.hostname or "").strip().strip("[]").lower()
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    if host in ("::1", "0.0.0.0"):
        return True
    octets = host.split(".")
    if len(octets) != 4 or octets[0] != "127":
        return False
    return all(octet.isdigit() for octet in octets)


def redact_secret(text: Any, secret: Any = None) -> str:
    """Strip credentials and the caller's token out of free text.

    Two passes: header style "Key: value" pairs, then the client token
    itself (when it is a non-empty string of at least 4 characters).
    Never raises: a diagnostic must not become a new failure mode.
    """

    try:
        out = str(text)
    except Exception:  # pragma: no cover - defensive
        return ""
    try:
        out = _REDACT_PATTERN.sub(r"\1: ***", out)
    except Exception:  # pragma: no cover - defensive
        pass
    if secret is not None:
        token = str(secret)
        if len(token) >= 4:
            try:
                out = re.sub(re.escape(token), "***", out)
            except Exception:  # pragma: no cover - defensive
                pass
    return out


def _body_text(response: Any) -> str:
    """Decode the first body representation the response offers."""

    raw = getattr(response, "content", None)
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw).decode("utf-8", "replace")
    if raw is not None:
        return str(raw)
    text = getattr(response, "text", None)
    if text is not None:
        return str(text)
    try:
        payload = response.json()
    except Exception:
        return ""
    try:
        return json.dumps(payload, ensure_ascii=False)
    except Exception:  # pragma: no cover - defensive
        return str(payload)


def _clean_excerpt(text: str) -> str:
    """HTML comment / tag strip + whitespace fold of a short fragment."""

    try:
        text = html.unescape(html.unescape(text))
        text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
        text = re.sub(r"<[^>]*>", " ", text)
        return re.sub(r"\s+", " ", text).strip()
    except Exception:  # pragma: no cover - defensive
        return text


def response_body_excerpt(
    response: Any, limit: int = DEFAULT_BODY_EXCERPT_LIMIT
) -> str:
    """Sanitised, bounded start of a response body (never a header).

    The body is cut to ``limit`` characters *before* it is cleaned, so
    both the work and the result stay bounded.  Cleaning is
    html.unescape -> drop "<!-- ... -->" -> drop "<...>" -> fold
    whitespace -> strip; a cut fragment gains a trailing ellipsis.
    """

    try:
        limit = int(limit)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        limit = DEFAULT_BODY_EXCERPT_LIMIT
    if limit <= 0:
        return ""
    try:
        text = _body_text(response)
    except Exception:  # pragma: no cover - defensive
        return ""
    truncated = len(text) > limit
    cleaned = _clean_excerpt(text[:limit])
    if truncated and cleaned:
        cleaned += "\u2026"
    return cleaned


def response_category(response: Any) -> str:
    """Classify a response that is not a decodable contract envelope.

    "proxy"  the status and media type of an intercepting middlebox
             (403, 407, 502, 503, 504 with an HTML body).
    "port"   something unrelated answered on this port: an explicit
             missing/unsupported/unimplemented status, or a short
             non-HTML body no training service would return.
    "other"  anything left over.
    """

    status = int(getattr(response, "status_code", 0) or 0)
    content_type = _content_type(response)
    excerpt = response_body_excerpt(response)
    is_html = "html" in content_type or excerpt.lstrip()[:1] == "<"
    if status in (403, 407, 502, 503, 504) and is_html:
        return "proxy"
    if status in (404, 426, 501):
        return "port"
    if not is_html and len(excerpt) < 200:
        return "port"
    return "other"


class _DrainedBody:
    """Response facade over a body already read in cancellable chunks."""

    def __init__(self, response: Any, content: bytes) -> None:
        self.status_code = response.status_code
        self.headers = response.headers
        self.content = content

    def json(self) -> Any:
        return json.loads(self.content.decode("utf-8"))


def _drain_cancellable(
    response: Any,
    should_cancel: Callable[[], bool],
    chunk_size: int = 1,
) -> "_DrainedBody":
    """Read a streaming body in chunks so a cancel lands within one chunk.

    Spec §5.4.4 shape ② ("body already sent, waiting for the response"):
    reading the body in one blocking call would only be interruptible
    through the 600 s read timeout, so the response is opened with
    stream=True and drained while the cancel flag is checked between
    chunks (monitor callbacks already cover shape ①).

    chunk_size is 1 on purpose: iter_content/urllib3 read(amt) blocks
    until amt bytes or EOF, so any larger value would only wake the
    cancel check after the whole envelope arrived.  The body on this
    path is always the small JSON envelope of POST /datasets/upload, so
    the byte-wise walk costs nothing.  Only upload_dataset() uses this;
    every other caller keeps the previous behaviour byte for byte.

    Change registration (B2 frozen file, one purely additive change):
      file: anylabeling/custom/remote_training/api_client.py
      anchors: (a) the top import block gains "import json";
        (b) this class and function are added right after _decode_json;
        (c) RemoteTrainingClient.upload_dataset passes
        stream=(should_cancel is not None) and routes the response
        through this helper only when should_cancel was given.
      contract: signature, defaults, return shape and the exception
        types are unchanged; without should_cancel the previous non
        streaming path is taken byte for byte.

    Inherent boundary (never describe this as "cancellation always
    lands within one chunk"): if the server sends neither a status line
    nor a single body byte, the call still blocks waiting for the
    response headers and only the 600 s read timeout can end it.  That
    window is exactly spec §5.4.4 shape ② as specified, and CT24 is the
    case covering it.
    """

    chunks: List[bytes] = []
    try:
        for chunk in response.iter_content(chunk_size=chunk_size):
            if should_cancel():
                response.close()
                raise UploadCancelledError(
                    "upload cancelled while waiting for the response"
                )
            if chunk:
                chunks.append(chunk)
    finally:
        raw = getattr(response, "raw", None)
        if raw is not None:
            try:
                raw.release_conn()
            except Exception:  # pragma: no cover - defensive
                pass
    return _DrainedBody(response, b"".join(chunks))


class RemoteTrainingClient:
    """requests wrapper for the 13 consumed routes (spec §3.2.3)."""

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        *,
        session: Optional[Any] = None,
        timeouts: Optional[Timeouts] = None,
        retry: Optional[RetryPolicy] = None,
    ) -> None:
        self.base_url = normalize_base_url(base_url)
        self.api_key = api_key or ""
        self.timeouts = timeouts or Timeouts()
        self.retry = retry
        self._owns_session = session is None
        self._session = session if session is not None else requests.Session()

    # -- plumbing ---------------------------------------------------

    def url_for(self, path: str) -> str:
        return self.base_url + TRAIN_PREFIX + path

    def auth_headers(self) -> Dict[str, str]:
        """Token header of spec §3.1; omitted when no token is set."""

        if self.api_key:
            return {"Token": self.api_key}
        return {}

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:  # pragma: no cover - defensive
            pass

    def discard_session(self) -> None:
        """Drop the session so a stuck socket cannot block cancellation."""

        self.close()
        if self._owns_session:
            self._session = requests.Session()

    def _send(
        self,
        method: str,
        path: str,
        *,
        path_params: Optional[Mapping[str, Any]] = None,
        params: Optional[Mapping[str, Any]] = None,
        json_body: Any = None,
        data: Any = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout: Optional[Tuple[float, float]] = None,
        stream: bool = False,
    ) -> Any:
        """One transport call for every route of this client.

        The proxy policy is applied here and nowhere else: the
        ``proxies`` argument is always ``request_proxies(url)``,
        i.e. proxy free for loopback and non-loopback hosts alike.
        ``trust_env`` / an injected session are left untouched; the
        explicit per-request mapping is what overrides both the
        environment and a system proxy configuration.
        Any connection error message is redacted (Token header and
        credential shaped fields) before it reaches TransportError.
        """

        url = self.url_for(path.format(**(path_params or {})))
        merged = self.auth_headers()
        if headers:
            merged.update(headers)
        try:
            return self._session.request(
                method,
                url,
                params=params,
                json=json_body,
                data=data,
                headers=merged,
                timeout=timeout or self.timeouts.json_request(),
                stream=stream,
                proxies=request_proxies(url),
            )
        except requests.RequestException as exc:
            raise TransportError(
                redact_secret(f"{method} {url}: {exc}", self.api_key)
            ) from exc

    def _json(
        self,
        method: str,
        key: str,
        *,
        path_params: Optional[Mapping[str, Any]] = None,
        params: Optional[Mapping[str, Any]] = None,
        json_body: Any = None,
        data: Any = None,
        headers: Optional[Mapping[str, str]] = None,
        retry: Optional[RetryPolicy] = None,
    ) -> Any:
        policy = self.retry if retry is None else retry
        path = route(key).path
        attempts = 0
        while True:
            attempts += 1
            try:
                response = self._send(
                    method,
                    path,
                    path_params=path_params,
                    params=params,
                    json_body=json_body,
                    data=data,
                    headers=headers,
                )
                payload = _decode_json(response, self.api_key)
                return raise_for_envelope(
                    payload, response.status_code, key, response.headers
                )
            except (TransportError, ServerError) as exc:
                if policy is None or not policy.retryable(method, exc):
                    raise
                delay = policy.delay_for(attempts)
                if delay > 0:
                    time.sleep(delay)

    def _open_stream(
        self,
        key: str,
        *,
        path_params: Optional[Mapping[str, Any]] = None,
        params: Optional[Mapping[str, Any]] = None,
    ) -> StreamedResponse:
        """Open one download route; no request header is ever added.

        Both download routes of spec §5.6.3 are header free in v1 (no
        Range / If-Range, no resume), and keeping the transport call
        closed over here means no caller can opt back in.
        """

        entry = route(key)
        response = self._send(
            entry.method,
            entry.path,
            path_params=path_params,
            params=params,
            timeout=self.timeouts.streaming_request(),
            stream=True,
        )
        content_type = _content_type(response)
        if response.status_code >= 400:
            try:
                if content_type == "application/json":
                    payload = _decode_json(response, self.api_key)
                    raise_for_envelope(
                        payload, response.status_code, key, response.headers
                    )
                    raise MalformedResponseError(
                        f"HTTP {response.status_code}: download route "
                        "returned a success envelope on a failing status"
                    )
                raise map_error(
                    response.status_code,
                    "",
                    f"HTTP {response.status_code} without an error envelope",
                    None,
                    key,
                )
            finally:
                response.close()
        if content_type == "application/json":
            response.close()
            raise ContractViolationError(
                f"{key}: download success must not be a JSON envelope"
            )
        return StreamedResponse(response, key)

    # -- consumed routes -------------------------------------------

    def get_capabilities(self) -> Capabilities:
        """GET /capabilities (route 1)."""

        data = self._json("GET", "capabilities")
        caps = Capabilities(data if data is not None else {})
        caps.require_keys()
        return caps

    def get_health(self) -> HealthStatus:
        """GET /health of the training subsystem (route 16)."""

        data = self._json("GET", "health")
        health = HealthStatus(data if data is not None else {})
        health.require_keys()
        return health

    def probe_health(self) -> HealthProbe:
        """One connection probe under the four-state auth table (§3.7).

        Always returns one of the four states: 401 -> unauthorized,
        connection failure and any 5xx (500 / code-less 503, 502) ->
        unreachable, 200 -> ok or training_disabled.  Non-5xx ApiErrors
        (e.g. a 404) are still raised, they are not connection states.
        """

        try:
            health = self.get_health()
        except UnauthorizedError as exc:
            return HealthProbe(HEALTH_STATE_UNAUTHORIZED, None, exc)
        except TransportError as exc:
            return HealthProbe(HEALTH_STATE_UNREACHABLE, None, exc)
        except ApiError as exc:
            if exc.http_status >= 500:
                return HealthProbe(HEALTH_STATE_UNREACHABLE, None, exc)
            raise
        if not health.enabled:
            return HealthProbe(HEALTH_STATE_TRAINING_DISABLED, health)
        return HealthProbe(HEALTH_STATE_OK, health)

    def plan_dataset(self, manifest: Mapping[str, Any]) -> Any:
        """POST /datasets/plan (route 2)."""

        return self._json("POST", "dataset_plan", json_body=dict(manifest))

    def upload_dataset(
        self,
        upload_token: str,
        archive_path: str,
        *,
        on_progress: Optional[Callable[[int, int], None]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> Any:
        """POST /datasets/upload with a streaming body (route 3).

        The body is the requests-toolbelt MultipartEncoder, so the
        archive is never buffered in memory (spec §5.4.4).  Ledger
        transitions around this call belong to the uploader.
        """

        encoder_cls, monitor_cls = require_streaming_multipart()
        handle = open(archive_path, "rb")
        try:
            encoder = encoder_cls(
                fields={
                    "upload_token": upload_token,
                    "archive": (
                        osp.basename(archive_path),
                        handle,
                        "application/zip",
                    ),
                }
            )
            body: Any = encoder
            if on_progress is not None or should_cancel is not None:

                def _on_read(monitor: Any) -> None:
                    if should_cancel is not None and should_cancel():
                        raise UploadCancelledError("upload cancelled")
                    if on_progress is not None:
                        on_progress(monitor.bytes_read, monitor.len)

                body = monitor_cls(encoder, _on_read)
            cancellable = should_cancel is not None
            try:
                response = self._send(
                    "POST",
                    route("dataset_upload").path,
                    data=body,
                    headers={"Content-Type": body.content_type},
                    timeout=self.timeouts.streaming_request(),
                    stream=cancellable,
                )
                if cancellable:
                    response = _drain_cancellable(response, should_cancel)
            except UploadCancelledError:
                self.discard_session()
                raise
            payload = _decode_json(response, self.api_key)
            return raise_for_envelope(
                payload,
                response.status_code,
                "dataset_upload",
                response.headers,
            )
        finally:
            handle.close()

    def create_job(self, body: Mapping[str, Any]) -> Any:
        """POST /jobs (route 7)."""

        return self._json("POST", "jobs_create", json_body=dict(body))

    def list_jobs(
        self,
        *,
        ids: Optional[Sequence[str]] = None,
        status: Optional[Sequence[str]] = None,
        limit: int = JOBS_BATCH_LIMIT_DEFAULT,
    ) -> Any:
        """GET /jobs (route 8); ids order is preserved by the server."""

        params: Dict[str, Any] = {"limit": int(limit)}
        if ids:
            params["ids"] = ",".join(str(item) for item in ids)
        if status:
            params["status"] = ",".join(str(item) for item in status)
        return self._json("GET", "jobs_list", params=params)

    def get_job(self, job_id: str) -> Any:
        """GET /jobs/{job_id} (route 9)."""

        return self._json(
            "GET", "job_detail", path_params={"job_id": job_id}
        )

    def get_events(self, job_id: str, after: int = 0) -> Any:
        """GET /jobs/{job_id}/events?after=<seq> (route 10)."""

        return self._json(
            "GET",
            "job_events",
            path_params={"job_id": job_id},
            params={"after": int(after)},
        )

    def cancel_job(self, job_id: str) -> Any:
        """POST /jobs/{job_id}/cancel (route 11)."""

        return self._json(
            "POST", "job_cancel", path_params={"job_id": job_id}
        )

    def resume_job(self, job_id: str, mode: str = "resume") -> Any:
        """POST /jobs/{job_id}/resume (route 12).

        The value set is frozen to the two contract values; whether the
        mode is allowed for this job is still decided by the server
        through ``resume_mode_available`` (400 VALIDATION_FAILED).
        """

        if mode not in RESUME_MODES:
            raise ValueError(
                f"mode must be one of {RESUME_MODES}, got {mode!r}"
            )
        return self._json(
            "POST",
            "job_resume",
            path_params={"job_id": job_id},
            json_body={"mode": mode},
        )

    def list_job_files(
        self, job_id: str, *, include_partial: bool = True
    ) -> Any:
        """GET /jobs/{job_id}/files (route 13)."""

        return self._json(
            "GET",
            "job_files",
            path_params={"job_id": job_id},
            params={"include_partial": bool(include_partial)},
        )

    def open_job_file(self, job_id: str, file_id: str) -> StreamedResponse:
        """GET /jobs/{job_id}/files/{file_id} (route 14).

        v1 has no resume (spec §5.6.3), so this route takes no Range /
        If-Range parameter: a download cannot grow one by accident.
        The response ETag / Content-Range stay readable on the returned
        object because spec §3.10.6 tells the client to keep the ETag
        verbatim for a later v2, but nothing sends it back from here.
        """

        return self._open_stream(
            "job_file",
            path_params={"job_id": job_id, "file_id": file_id},
        )

    def open_job_download(
        self, job_id: str, *, include_partial: bool = True
    ) -> StreamedResponse:
        """GET /jobs/{job_id}/download (route 15)."""

        return self._open_stream(
            "job_download",
            path_params={"job_id": job_id},
            params={"include_partial": bool(include_partial)},
        )
