"""Job polling, event intake and the client error wording (spec §5.5).

This module is the pure half of the monitoring step: every rule of
spec §5.5 is a function or a small state machine over plain mappings, so
the whole of it runs without Qt and without a server.  The Qt carriers
(the polling QThread and the cancel / resume command worker) live in
worker.py.

What is frozen here, with the spec section that owns it:

- is_terminal has exactly one formula (spec §5.5.4): the server
  is_terminal field first, finished_at != null only when that field is
  missing.  The three pages, the idle downgrade and the transition
  detection all call this one function.
- the intervals of spec §5.5.1 and the fallback channel of §5.5.2;
- the batch query of spec §5.5.5 (api_client.chunked at limit, request
  order merge, not_found_ids[] -> orphaned, oversized batch -> 400
  without silent truncation);
- the single failure schedule of spec §5.5.6, whose delay ladder is
  api_client.RetryPolicy / api_client.backoff_delay - this module never
  writes a second ladder;
- the event intake of spec §5.5.3 (seq dedup, last_seq cursor,
  manual_resume write back, unknown type degraded to a log line);
- the 30 (HTTP, code) pairs / 29 codes of spec §5.6.4.  The table below
  only carries the client wording: the codes themselves are the spec
  §3.3 table, and nothing here invents a code.

Inherent boundary of the cancellation channel (never describe this as
"cancellation always lands within one chunk"): every wait here is an
`Event.wait(timeout)` and is therefore woken in milliseconds, but a
request already handed to `requests` can only be ended by its own
connect / read timeout.  For the upload route that bound is the 600 s
read timeout of `api_client.Timeouts.upload_read` (spec §5.4.4 shape
②, `stream=True` + `iter_content`), which is exactly the window CT24
covers: if the server sends neither a status line nor a single body
byte, cancelling still blocks on the response headers until that
timeout fires.  Nothing on the client side can shorten it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional
from typing import Sequence, Tuple  # noqa: F401 - Sequence is public API

from . import api_client as api
from . import store as store_mod
from .store import Store, TaskRecord, TasksLedger
from .store import utc_now_iso

__all__ = [
    "CANCEL_GRACE_TEMPLATE",
    "ACTIVE_STATUSES",
    "CLIENT_ERROR_TABLE",
    "ClientErrorView",
    "CommandOutcome",
    "DETAIL_INTERVAL",
    "ERROR_CODE_COUNT",
    "ERROR_PAIR_COUNT",
    "EventBatch",
    "FailureState",
    "FailureTracker",
    "FALLBACK_INTERVAL",
    "IDLE_INTERVAL",
    "INTERVAL_MAX",
    "INTERVAL_MIN",
    "JOBS_INTERVAL",
    "PendingBatch",
    "PollJob",
    "PollOutcome",
    "RED_BAR_TEXT",
    "RESULTS_INTERVAL",
    "SERVER_RETRY_TEMPLATE",
    "Scheduler",
    "TRANSITION_FIELDS",
    "apply_event",
    "backoff_wait",
    "batch_ids",
    "cancel_grace_seconds",
    "cancel_message",
    "cancel_outcome",
    "client_error_view",
    "close_by_model",
    "command_outcome",
    "event_display",
    "failure_exit",
    "format_event",
    "has_field",
    "is_terminal",
    "job_interval",
    "job_total",
    "ledger_record",
    "looks_terminal_by_model",
    "manual_resume_payload",
    "merge_job_into_ledger",
    "merge_jobs",
    "next_interval",
    "normalize_command_data",
    "resume_mode",
    "resume_outcome",
    "status_of",
    "stop_reason",
    "terminal_from_finished_at",
]

# --------------------------------------------------------------------
# Intervals (spec §5.5.1) and the fallback channel (spec §5.5.2)
# --------------------------------------------------------------------

#: Task list page: 10 s, configurable 5-60 s.
JOBS_INTERVAL = 10.0
INTERVAL_MIN = 5.0
INTERVAL_MAX = 60.0
#: Detail page while the job is active.
DETAIL_INTERVAL = 3.0
#: Results page while the job is active.
RESULTS_INTERVAL = 15.0
#: The one 60 s tier: terminal fallback and "no active job" idle.
FALLBACK_INTERVAL = 60.0
IDLE_INTERVAL = FALLBACK_INTERVAL

#: The seven fields the fallback channel may update while the job is
#: still terminal (spec §5.5.2, verbatim).
TRANSITION_FIELDS: Tuple[str, ...] = (
    "status",
    "is_terminal",
    "needs_attention",
    "needs_attention_reason",
    "resume_cycles",
    "resume_mode_available",
    "partial_available",
)

RED_BAR_TEXT = "与服务端失去联系，正在重试…"
SERVER_RETRY_TEMPLATE = "服务端暂时不可用（HTTP {0}），正在重试…"
UNAUTHORIZED_TEXT = "Token 无效或已过期，请到配置页更新"
CANCEL_GRACE_TEMPLATE = "正在停止…（等待进程退出，最长 {0} 秒，默认 15）"
MANUAL_RESUME_TEMPLATE = "第 {0} 次人工恢复（mode={1}），本轮重试预算已重置"
ORPHAN_NOTE = "服务端已不存在该任务"
WAITING_TEXTS = {
    "WAITING_DEVICE_VRAM": "正在等待显存释放",
    "WAITING_PREVIOUS_JOBS": "正在等待同卡上的任务",
    "WAITING_CONCURRENCY_SLOT": "正在等待并发位",
}
WAITING_TEMPLATE = "（排队原因：{0}）"

_MISSING = object()

#: The three statuses that mean "the job is alive again" (spec §3.4.1):
#: they are the external-resume signal of the fallback channel.
ACTIVE_STATUSES: Tuple[str, ...] = ("queued", "preparing", "running")

PAGE_JOBS = "jobs"
PAGE_DETAIL = "detail"
PAGE_RESULTS = "results"
POLL_PAGES: Tuple[str, ...] = (PAGE_JOBS, PAGE_DETAIL, PAGE_RESULTS)


# --------------------------------------------------------------------
# The single terminal formula (spec §5.5.4)
# --------------------------------------------------------------------


def job_total(job: Any, key: str, default: Any = None) -> Any:
    """Read one field from a mapping or an object job."""

    if isinstance(job, Mapping):
        return job.get(key, default)
    return getattr(job, key, default)


def terminal_from_finished_at(job: Any) -> bool:
    """The fallback formula: the server only writes finished_at once."""

    return job_total(job, "finished_at", None) is not None


def has_field(job: Any, key: str) -> bool:
    """True when the response really carries that key.

    A field the server did not send must never overwrite the ledger:
    the local row (attempt, status, the last known timestamps) is
    authoritative whenever the response is silent about a field.
    """

    if isinstance(job, Mapping):
        return job.get(key, _MISSING) is not _MISSING
    return getattr(job, key, _MISSING) is not _MISSING


def looks_terminal_by_model(job: Any) -> Optional[bool]:
    """The server is_terminal flag, or None when the field is absent.

    False is a real answer (an automatically requeued failed job is not
    terminal), so the caller must be able to tell it apart from "the old
    server never sent the field".
    """

    value = job_total(job, "is_terminal", _MISSING)
    if value is _MISSING or value is None:
        return None
    return bool(value)


def is_terminal(job: Any) -> bool:
    """The unique terminal predicate of spec §5.5.4.

    Every caller - the three pages, the idle downgrade and the transition
    detection - goes through this function; the "status in a set AND/OR
    finished_at" spellings are forbidden by the spec.
    """

    authoritative = looks_terminal_by_model(job)
    if authoritative is not None:
        return authoritative
    return terminal_from_finished_at(job)


def status_of(job: Any) -> str:
    """The status string of a job object or None (never a KeyError)."""

    if job is None:
        return ""
    return str(job_total(job, "status", "") or "")


def stop_reason(job: Any) -> str:
    """Why the high frequency polling of an active job has to stop."""

    status = str(job_total(job, "status", "") or "")
    if is_terminal(job):
        return "terminal"
    attempt = job_total(job, "attempt")
    maximum = job_total(job, "max_attempts")
    if status == "failed" and attempt is not None and maximum is not None:
        return "auto_retry"
    if status == "queued":
        return "requeued"
    return "active"


# --------------------------------------------------------------------
# Error wording (spec §5.6.4; codes come from the spec §3.3 table)
# --------------------------------------------------------------------

#: One row per code: the (HTTP, code) pairs it appears with, its
#: severity, its wording and the action flags of spec §5.6.4.  A code
#: with several triggers carries a "branches" map keyed the way the
#: spec keys it (details.field, or details.reason).
CLIENT_ERROR_TABLE: Dict[str, Dict[str, Any]] = {
    "UNAUTHORIZED": {
        "http": (401,),
        "severity": "red",
        "text": "Token 无效或已过期，请在配置页更新后重试",
        "highlight_settings": True,
        "stop_polling": True,
    },
    "VALIDATION_FAILED": {
        "http": (400, 409),
        "severity": "red",
        "text": "请求体校验失败",
        "branches": {
            "split": "上传内容校验失败：split 取值非法，或划分结果某一侧为空",
            "split_strategy": "上传内容校验失败：划分策略取值非法（当前只支持 per_class）",
            "mode": "恢复方式不被接受：当前只允许 {allowed}",
            "upload_token": "该上传凭证已提交过且内容不同，将重新预检并上传",
            "client_submission_id": "该提交请求已存在且内容不同，请确认后重新提交",
            "files": "上传内容校验失败",
            "default": "请求体校验失败",
        },
    },
    "MISSING_LABELS": {
        "http": (400,),
        "severity": "red",
        "text": "服务端发现缺少标注文件",
    },
    "CHECKSUM_MISMATCH": {
        "http": (400,),
        "severity": "red",
        "text": "上传文件与本地声明不一致（可能传输损坏）",
    },
    "MANIFEST_MISMATCH": {
        "http": (400,),
        "severity": "red",
        "text": "上传内容与预检结果不一致",
        "refresh_plan": True,
    },
    "LABEL_CHECKSUM_MISMATCH": {
        "http": (400,),
        "severity": "red",
        "text": "标注文件内容与本地声明不一致（可能传输损坏或被改写）",
    },
    "INVALID_LABEL_FORMAT": {
        "http": (400,),
        "severity": "red",
        "text": "标签格式非法",
    },
    "UNSUPPORTED_EXTENSION": {
        "http": (400,),
        "severity": "red",
        "text": "存在不支持的图片扩展名",
    },
    "TOKEN_EXPIRED": {
        "http": (400,),
        "severity": "red",
        "text": "上传凭证已过期或无效，请重新预检",
    },
    "UNKNOWN_UPLOAD_TOKEN": {
        "http": (400,),
        "severity": "red",
        "text": "上传凭证已过期或无效，请重新预检",
    },
    "JOB_NOT_FOUND": {
        "http": (404,),
        "severity": "yellow",
        "text": "服务端已不存在该任务",
        "mark_orphaned": True,
    },
    "DATASET_NOT_FOUND": {
        "http": (404,),
        "severity": "red",
        "text": "服务端已不存在该数据集",
    },
    "ARTIFACT_NOT_FOUND": {
        "http": (404,),
        "severity": "yellow",
        "text": "产物文件已不存在或不可下载",
        "refresh_files": True,
    },
    "DATASET_IN_USE": {
        "http": (409,),
        "severity": "yellow",
        "text": "数据集正被排队 / 运行中的任务使用，无法删除",
    },
    "JOB_NOT_RESUMABLE": {
        "http": (409,),
        "severity": "yellow",
        "text": "该任务当前状态不允许恢复（已完成的任务不能恢复）",
        "branches": {
            "resume_in_progress": "上一次恢复尚未收尾，请稍后重试",
            "lock_timeout": "服务端正忙，请重试",
            "process_alive": "该任务仍有存活进程，请先取消或等它结束",
        },
        "refresh_detail": True,
    },
    "JOB_ARTIFACTS_EXPIRED": {
        "http": (409,),
        "severity": "red",
        "text": "无法恢复：检查点或数据集已过期",
        "branches": {
            "checkpoint_missing": "没有可用的训练检查点且服务端未启用自动重训",
            "dataset_expired": "数据集已过期或被删除",
        },
        "refresh_detail": True,
    },
    "UPLOAD_IN_PROGRESS": {
        "http": (409,),
        "severity": "yellow",
        "text": "同一上传凭证正在上传中",
        "auto_retry": True,
        "retry_after": 5.0,
    },
    "QUOTA_EXCEEDED": {
        "http": (413,),
        "severity": "red",
        # The 413 wording is the two step one owned by uploader.py: the
        # automatic reclaim first, the administrator only when that
        # cannot clear it (spec §5.6.4 and §7.1 decision 4).
        "text": "",
    },
    "COMMITTED_TOKEN_CAPACITY_EXCEEDED": {
        "http": (429,),
        "severity": "yellow",
        "text": "服务端上传凭证保留区已满（保留期内不淘汰旧记录），请稍后重试",
        "auto_retry": True,
    },
    "PARAM_OUT_OF_RANGE": {
        "http": (422,),
        "severity": "red",
        "text": "参数超出允许范围",
        "highlight_field": True,
    },
    "PARAM_NOT_OVERRIDABLE": {
        "http": (422,),
        "severity": "red",
        "text": "该参数由服务端注入，客户端不可指定",
    },
    "OPTIMIZER_UNSUPPORTED": {
        "http": (422,),
        "severity": "red",
        "text": "当前环境不支持该优化器预设",
        "refresh_capabilities": True,
    },
    "MODEL_FAMILY_UNSUPPORTED": {
        "http": (422,),
        "severity": "red",
        "text": "服务端 ultralytics 版本低于该模型家族要求",
    },
    "WEIGHT_NOT_AVAILABLE": {
        "http": (422,),
        "severity": "red",
        "text": "服务端没有该权重文件且未开启联网下载",
    },
    "INSUFFICIENT_VRAM": {
        "http": (422,),
        "severity": "red",
        "text": "估算显存超过本机最小单卡容量，本机无法运行该配置",
        "branches": {
            "insufficient_capacity": "估算显存超过本机最小单卡容量，本机永远跑不了该配置",
            "ratio_cap_below_one": "该配置的自动 batch 折算上限小于 1，服务端判定为容量不足",
            "table_cap_below_one": "该配置的自动 batch 折算上限小于 1，服务端判定为容量不足",
        },
    },
    "VRAM_ESTIMATE_UNAVAILABLE": {
        "http": (422,),
        "severity": "red",
        "text": "该模型与任务组合在服务端本机不可调度",
        "branches": {
            "VRAM_CALIBRATION_FAILED": "该组合在服务端本机的显存标定中全部点位 OOM",
            "calibration_failed": "该组合在服务端本机的显存标定中全部点位 OOM",
            "no_table_row": "缺少显存数据",
            "no_vram_row": "缺少显存数据",
        },
    },
    "TRAINING_DISABLED": {
        "http": (503,),
        "severity": "red",
        "text": "服务端未启用远程训练",
    },
    "NO_DEVICE_AVAILABLE": {
        "http": (503,),
        "severity": "red",
        "text": "服务端当前没有可用 GPU",
    },
    "INTERNAL_ERROR": {
        "http": (500,),
        "severity": "red",
        "text": "服务端内部错误",
        "safe_retry": True,
    },
}

ERROR_CODE_COUNT = len(CLIENT_ERROR_TABLE)
#: 30 (HTTP, code) pairs over 29 codes: VALIDATION_FAILED is 400 and 409.
ERROR_PAIR_COUNT = sum(
    len(entry["http"]) for entry in CLIENT_ERROR_TABLE.values()
)


@dataclass
class ClientErrorView:
    """One rendered (HTTP, code) pair: wording, colour and actions."""

    http_status: Optional[int] = None
    code: str = ""
    severity: str = "red"
    text: str = ""
    detail: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    auto_retry: bool = False
    retry_after: Optional[float] = None
    highlight_settings: bool = False
    highlight_field: bool = False
    refresh_detail: bool = False
    refresh_files: bool = False
    refresh_capabilities: bool = False
    refresh_plan: bool = False
    mark_orphaned: bool = False
    stop_polling: bool = False
    generic: bool = False

    def lines(self) -> List[Tuple[str, str]]:
        """The status row lines: the main wording first, details last."""

        rows: List[Tuple[str, str]] = [(self.severity, self.text)]
        for text in self.detail_lines():
            rows.append((self.severity, text))
        return rows

    def detail_lines(self) -> List[str]:
        """details.files[] and the other lists, entry by entry."""

        lines: List[str] = []
        for key in ("files", "rejected", "missing", "classes"):
            entries = self.details.get(key)
            if not isinstance(entries, (list, tuple)) or not entries:
                continue
            for entry in entries:
                lines.append("  " + _summarize_entry(entry))
        return lines


def _summarize_entry(entry: Any) -> str:
    if isinstance(entry, Mapping):
        return json.dumps(dict(entry), ensure_ascii=False, sort_keys=True)
    return str(entry)


def _branch_text(entry: Mapping[str, Any], details: Mapping[str, Any]) -> str:
    branches = entry.get("branches")
    if not isinstance(branches, Mapping):
        return str(entry.get("text") or "")
    # Two keys drive a branch: details.field (VALIDATION_FAILED and the
    # 422 family) and details.reason (JOB_NOT_RESUMABLE, the expired
    # artifacts and the vram reasons).
    field = details.get("field")
    reason = details.get("reason")
    key = "default"
    if field is not None:
        key = str(field)
    elif reason is not None:
        key = str(reason)
    if key == "files" and not details.get("files"):
        key = "default"
    template = branches.get(key)
    if template is None:
        template = branches.get("default")
    if template is None:
        return str(entry.get("text") or "")
    if key == "mode":
        allowed = details.get("allowed")
        if isinstance(allowed, (list, tuple)):
            allowed = ", ".join(str(item) for item in allowed)
        return str(template).replace("{allowed}", str(allowed or "-"))
    return str(template)


def client_error_view(error: BaseException) -> ClientErrorView:
    """Render one exception as the spec §5.6.4 wording.

    code is always the server own code (or "" for the two generic
    fallback rows: "其它 5xx" and the network layer).  The function never
    invents a code and never guesses a trigger.
    """

    status = getattr(error, "http_status", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    code = str(getattr(error, "code", "") or "")
    details = getattr(error, "details", None)
    details = dict(details) if isinstance(details, Mapping) else {}
    entry = CLIENT_ERROR_TABLE.get(code)
    if entry is not None and status is None:
        status = int(entry["http"][0])

    if entry is None:
        if isinstance(error, api.TransportError) and status is None:
            # The red bar wording of the no-response row (spec §5.5.6).
            # It only reaches a status row from the second consecutive
            # failure on: the first one is resent inside the same tick by
            # Scheduler._safe_call and never rendered.
            return ClientErrorView(
                http_status=None,
                code="",
                severity="red",
                text=RED_BAR_TEXT,
                details=details,
                auto_retry=True,
                generic=True,
            )
        if status is not None and status >= 500:
            text = SERVER_RETRY_TEMPLATE.format(status)
            error_id = details.get("error_id")
            if error_id:
                text += "（错误号 {0}）".format(error_id)
            return ClientErrorView(
                http_status=status,
                code=code,
                severity="red",
                text=text,
                details=details,
                auto_retry=True,
                generic=True,
            )
        return ClientErrorView(
            http_status=status,
            code=code,
            severity="red",
            text=str(getattr(error, "message", "") or error),
            details=details,
            generic=True,
        )

    text = _branch_text(entry, details)
    if code == "INSUFFICIENT_VRAM":
        capacity = details.get("min_device_total_mb")
        estimate = details.get("vram_estimate_mb")
        reserved = details.get("reserved_mb")
        if None not in (capacity, estimate, reserved):
            text = (
                "估算显存 {0} MB 超过本机最小单卡容量 {1} MB"
                "（已扣推理预留 {2} MB），本机永远跑不了该配置"
            ).format(estimate, capacity, reserved)
    if code == "INTERNAL_ERROR":
        error_id = details.get("error_id")
        if error_id:
            text = "服务端内部错误（错误号 {0}）".format(error_id)
    return ClientErrorView(
        http_status=status,
        code=code,
        severity=str(entry.get("severity") or "red"),
        text=text,
        detail=str(getattr(error, "message", "") or ""),
        details=details,
        # 500 / the other 5xx rows are auto retried on safe requests only;
        # the POST callers clear the flag through failure_exit().
        auto_retry=bool(entry.get("auto_retry"))
        or bool(entry.get("safe_retry")),
        retry_after=entry.get("retry_after"),
        highlight_settings=bool(entry.get("highlight_settings")),
        highlight_field=bool(entry.get("highlight_field")),
        refresh_detail=bool(entry.get("refresh_detail")),
        refresh_files=bool(entry.get("refresh_files")),
        refresh_capabilities=bool(entry.get("refresh_capabilities")),
        refresh_plan=bool(entry.get("refresh_plan")),
        mark_orphaned=bool(entry.get("mark_orphaned")),
        stop_polling=bool(entry.get("stop_polling")),
    )


def failure_exit(
    error: BaseException,
    *,
    safe_request: bool = True,
    on_status: Optional[Callable[[List[Tuple[str, str]]], Any]] = None,
    on_capabilities: Optional[Callable[[], Any]] = None,
    on_refresh_detail: Optional[Callable[[str], Any]] = None,
) -> ClientErrorView:
    """The one failure exit of spec §5.6.4.

    Every failed call funnels through here, so "no automatic retry on a
    POST" (safe_request=False) and "highlight the settings / the field"
    are decided in exactly one place.
    """

    view = client_error_view(error)
    if not safe_request:
        view.auto_retry = False
    details = view.details
    job_id = details.get("job_id")
    if view.refresh_detail and on_refresh_detail is not None and job_id:
        on_refresh_detail(str(job_id))
    if view.refresh_capabilities and on_capabilities is not None:
        on_capabilities()
    if on_status is not None:
        on_status(view.lines())
    return view


# The 413 wording is owned by uploader.py (one implementation, used by
# both steps); this assignment is the only link between the two.
from .uploader import QUOTA_RETRY_MESSAGE as _QUOTA_TEXT  # noqa: E402

CLIENT_ERROR_TABLE["QUOTA_EXCEEDED"]["text"] = _QUOTA_TEXT


# --------------------------------------------------------------------
# Failure schedule (spec §5.5.6)
# --------------------------------------------------------------------


@dataclass
class FailureState:
    """The two counters of spec §5.5.6 plus the red bar flag."""

    consecutive_failures: int = 0
    no_response_failures: int = 0
    red_bar: bool = False
    last_kind: str = ""
    last_status: Optional[int] = None

    def wait_seconds(
        self, policy: Optional[api.RetryPolicy] = None
    ) -> float:
        """The ladder position derived from the failure count alone."""

        policy = policy or api.RetryPolicy()
        return float(policy.delay_for(self.consecutive_failures))


def backoff_wait(consecutive_failures: int) -> float:
    """The delay of one ladder position (spec §5.5.6).

    The ladder itself lives in api_client.backoff_delay; this wrapper
    exists so callers never import a second copy of the sequence.
    """

    return float(api.backoff_delay(int(consecutive_failures)))


class FailureTracker:
    """The single schedule state shared by every page of one worker."""

    def __init__(self, policy: Optional[api.RetryPolicy] = None) -> None:
        self.policy = policy or api.RetryPolicy()
        self.state = FailureState()

    @property
    def consecutive_failures(self) -> int:
        return self.state.consecutive_failures

    @property
    def red_bar(self) -> bool:
        return self.state.red_bar

    def wait_seconds(self) -> float:
        return self.state.wait_seconds(self.policy)

    def record_failure(
        self, error: BaseException, http_status: Optional[int] = None
    ) -> FailureState:
        """Count one failure; the kind never resets the ladder."""

        status = http_status
        if status is None:
            raw = getattr(error, "http_status", None)
            try:
                status = int(raw) if raw is not None else None
            except (TypeError, ValueError):
                status = None
        self.state.consecutive_failures += 1
        if status is None:
            self.state.no_response_failures += 1
            self.state.last_kind = "no_response"
        else:
            self.state.no_response_failures = 0
            self.state.last_kind = "http"
        self.state.last_status = status
        self.state.red_bar = (
            self.state.no_response_failures
            >= api.RED_BAR_NO_RESPONSE_THRESHOLD
        )
        # A snapshot: the caller keeps the counters of that moment, so a
        # later tick can never rewrite an outcome already handed out.
        return replace(self.state)

    def record_response(self, http_status: Optional[int] = None) -> bool:
        """Any HTTP response hides the red bar; 2xx / non 401 4xx reset.

        Returns True when the ladder position was reset, which is what
        spec §5.5.6 calls 归零.
        """

        self.state.no_response_failures = 0
        self.state.red_bar = False
        if http_status is None:
            return False
        try:
            status = int(http_status)
        except (TypeError, ValueError):
            return False
        if 200 <= status < 300:
            return self._reset()
        if 400 <= status < 500 and status != 401:
            return self._reset()
        # 5xx and 401 keep the position (401 stops the polling).
        return False

    def reset(self) -> None:
        self._reset()
        self.state.no_response_failures = 0
        self.state.red_bar = False
        self.state.last_kind = ""
        self.state.last_status = None

    def _reset(self) -> bool:
        changed = self.state.consecutive_failures != 0
        self.state.consecutive_failures = 0
        return changed

    def status_lines(self) -> List[Tuple[str, str]]:
        """The red bar / 5xx hint for the current state (spec §5.5.6)."""

        if self.state.red_bar:
            return [("red", RED_BAR_TEXT)]
        if self.state.last_kind == "http" and (
            self.state.last_status or 0
        ) >= 500:
            return [
                (
                    "yellow",
                    SERVER_RETRY_TEMPLATE.format(self.state.last_status),
                )
            ]
        return []


# --------------------------------------------------------------------
# Job objects -> ledger records (spec §3.4.4, §5.3.2)
# --------------------------------------------------------------------

#: The job fields mirrored into one ledger record, verbatim.
LEDGER_JOB_FIELDS: Tuple[str, ...] = (
    "job_id",
    "status",
    "is_terminal",
    "attempt",
    "resume_cycles",
    "needs_attention",
    "needs_attention_reason",
    "artifact_suspect",
    "partial_available",
    "created_at",
    "finished_at",
)


def ledger_record(job: Any) -> TaskRecord:
    """Build a TaskRecord from one job object."""

    payload: Dict[str, Any] = {}
    for name in LEDGER_JOB_FIELDS:
        if has_field(job, name):
            payload[name] = job_total(job, name)
    record = TaskRecord(
        job_id=str(payload.pop("job_id", "") or ""),
        **payload,
    )
    record._is_terminal_present = looks_terminal_by_model(job) is not None
    record.normalize()
    return record


def merge_job_into_ledger(
    record: Optional[TaskRecord],
    job: Any,
    *,
    seen_at: Optional[str] = None,
    partial_fields: Optional[Sequence[str]] = None,
    files: Optional[Any] = None,
    resume_event: Optional[Mapping[str, Any]] = None,
) -> TaskRecord:
    """Merge one job response into the ledger row (spec §5.5.2, §5.3.2).

    partial_fields is the fallback channel whitelist: while a job is
    still terminal only those seven fields may change, so the response
    never redraws the progress or artifact area.
    """

    apply_all = partial_fields is None
    job_id = str(job_total(job, "job_id", "") or "")
    base = record if record is not None else TaskRecord(job_id=job_id)
    data = base.to_dict()
    data.pop("extra", None)
    # The whitelist of the fallback channel (spec §5.5.2) decides which
    # fields may change; every other field keeps the ledger value, and a
    # field the server did not send is never overwritten by a default.
    names = LEDGER_JOB_FIELDS if apply_all else tuple(partial_fields or ())
    for name in names:
        if not has_field(job, name):
            # Silence about a field is not a new value for it.
            continue
        data[name] = job_total(job, name)
    data["job_id"] = str(data.get("job_id") or job_id)
    data["last_seen_at"] = seen_at or utc_now_iso()
    # The (is_terminal, finished_at) pair must agree after every merge
    # (spec §3.4.3 invariant 3, §5.3.2): the authoritative flag wins, a
    # response carrying only finished_at is derived from it, and one
    # silent about both keeps the row own evidence instead of claiming a
    # terminal state it has no proof for (N4).
    if has_field(job, "is_terminal"):
        data["is_terminal"] = bool(job_total(job, "is_terminal"))
    elif has_field(job, "finished_at"):
        data["is_terminal"] = terminal_from_finished_at(job)
    else:
        data["is_terminal"] = data.get("finished_at") is not None
    if has_field(job, "finished_at"):
        data["finished_at"] = job_total(job, "finished_at")
    elif not data.get("is_terminal"):
        # No timestamp in the response and the row is not terminal: a
        # stale timestamp would contradict the flag on the page.
        data["finished_at"] = None
    if not apply_all and not has_field(job, "is_terminal") and not has_field(
        job, "finished_at"
    ):
        # Old server boundary (N-a): the fallback channel of an old
        # server may carry neither terminality field.  A status that is
        # active again is the external-resume signal of spec §5.5.2, so
        # the row must not stay stuck at is_terminal=true (which would
        # re-detect the same transition on every 60 s tick).
        if str(data.get("status") or "") in ACTIVE_STATUSES:
            data["is_terminal"] = False
            data["finished_at"] = None
    extra = dict(base.extra or {})
    if apply_all:
        extra.pop("orphaned_at", None)
    data.update(extra)
    merged = TaskRecord.from_dict(data)
    merged._is_terminal_present = True
    # The resume trace survives every merge: it is local write back, not
    # a job object field (spec §5.5.3, §5.5.7 step 2).
    merged.manual_resume = dict(base.manual_resume or {})
    if resume_event:
        resume = manual_resume_payload(resume_event)
        if resume:
            merged.manual_resume = resume
    if files is not None:
        merged.extra["files_count"] = len(list(files or []))
    return merged


def manual_resume_payload(event: Any) -> Dict[str, Any]:
    """The written back fields of a manual_resume event."""

    data = job_total(event, "data", None)
    if not isinstance(data, Mapping):
        data = event if isinstance(event, Mapping) else {}
    attempt = data.get("attempt")
    resume_cycles = data.get("resume_cycles")
    mode = str(data.get("mode") or "")
    if attempt is None and resume_cycles is None and not mode:
        return {}
    payload: Dict[str, Any] = {}
    if attempt is not None:
        payload["attempt"] = attempt
    if resume_cycles is not None:
        payload["resume_cycles"] = resume_cycles
    if mode:
        payload["mode"] = mode
    return payload


def close_by_model(record: TaskRecord) -> bool:
    """True when the row carries an authoritative terminal flag."""

    return bool(getattr(record, "_is_terminal_present", True))


def resume_mode(modes: Any) -> str:
    """The mode a resume click sends (spec §5.5.7 step 1)."""

    available = [str(item) for item in (modes or [])]
    if "resume" in available:
        return "resume"
    if "restart" in available:
        return "restart"
    return ""


# --------------------------------------------------------------------
# Batch query (spec §5.5.5)
# --------------------------------------------------------------------


def batch_ids(
    job_ids: Sequence[str], limit: int = api.JOBS_BATCH_LIMIT_DEFAULT
) -> List[List[str]]:
    """Slice the ids with api_client.chunked (one implementation)."""

    if int(limit) < 1:
        raise ValueError("limit must be >= 1")
    return [list(chunk) for chunk in api.chunked(list(job_ids), int(limit))]


@dataclass
class PendingBatch:
    """The outcome of one GET /jobs?ids= batch."""

    ok: bool = False
    requested: List[str] = field(default_factory=list)
    jobs: List[Any] = field(default_factory=list)
    not_found_ids: List[str] = field(default_factory=list)
    error: Optional[BaseException] = None


def merge_jobs(
    jobs: Iterable[Any], *, orphaned: Iterable[str] = ()
) -> Tuple[List[Any], List[str]]:
    """Merge batches back by request order and list the orphans.

    The response order is the request order already (spec §5.5.5 rule
    ①), so one stable pass is enough; not_found_ids[] is the only orphan
    source - "absent from the response" is not.
    """

    ordered: List[Any] = []
    seen: set = set()
    for job in jobs or []:
        job_id = str(job_total(job, "job_id", "") or "")
        if job_id and job_id in seen:
            continue
        if job_id:
            seen.add(job_id)
        ordered.append(job)
    orphans: List[str] = []
    for job_id in orphaned or []:
        text = str(job_id)
        if text and text not in orphans:
            orphans.append(text)
    return ordered, orphans


# --------------------------------------------------------------------
# Events (spec §5.5.3, §3.5)
# --------------------------------------------------------------------

#: The four required event types, plus the optional fifth one.
KNOWN_EVENT_TYPES: Tuple[str, ...] = (
    "progress",
    "metrics",
    "log",
    "done",
    "manual_resume",
)


@dataclass
class EventBatch:
    """One consumed GET /jobs/{id}/events?after= response."""

    events: List[Any] = field(default_factory=list)
    last_seq: int = 0
    previous_seq: int = 0
    resume: Dict[str, Any] = field(default_factory=dict)
    unknown_types: List[str] = field(default_factory=list)
    done: bool = False

    def advanced(self) -> bool:
        return self.last_seq > self.previous_seq


def event_display(event: Any) -> Tuple[str, str]:
    """One event as (kind, line) for the log panel."""

    etype = str(job_total(event, "type", "") or "")
    data = job_total(event, "data", None) or {}
    if not isinstance(data, Mapping):
        data = {}
    if etype == "progress":
        percent = data.get("percent")
        try:
            text = "进度 {0:.1f}%".format(float(percent))
        except (TypeError, ValueError):
            text = "进度更新"
        return "progress", "{0}（epoch {1}/{2}）".format(
            text, data.get("epoch"), data.get("total_epochs")
        )
    if etype == "metrics":
        return "metrics", "指标：" + ", ".join(
            "{0}={1}".format(key, value) for key, value in data.items()
        )
    if etype == "log":
        return "log", str(data.get("message") or "")
    if etype == "done":
        return "done", "任务结束（status={0}, exit_code={1}）".format(
            data.get("status"), data.get("exit_code")
        )
    if etype == "manual_resume":
        return "log", MANUAL_RESUME_TEMPLATE.format(
            data.get("resume_cycles"), data.get("mode")
        )
    # Forward compatibility (spec §5.5.3): never drop, never raise.
    return "log", json.dumps(dict(data), ensure_ascii=False)


def format_event(event: Any) -> str:
    """The event log line, reusing the detail page rendering."""

    from .ui.detail_page import event_line

    return event_line(event)


def apply_event(record: Optional[TaskRecord], event: Any) -> TaskRecord:
    """Write one event into the ledger (spec §5.5.3).

    manual_resume writes attempt / resume_cycles / mode back into the
    ledger; every other type only moves the seq cursor.
    """

    base = record if record is not None else TaskRecord()
    if str(job_total(event, "type", "") or "") == "manual_resume":
        payload = manual_resume_payload(event)
        if payload:
            updated = replace(base)
            updated.extra = dict(base.extra or {})
            if payload.get("attempt") is not None:
                updated.attempt = int(payload["attempt"])
            if payload.get("resume_cycles") is not None:
                updated.resume_cycles = int(payload["resume_cycles"])
            updated.manual_resume = payload
            return updated
    return base


# --------------------------------------------------------------------
# Cancel / resume (spec §5.4.4, §5.5.7, §3.11)
# --------------------------------------------------------------------


def cancel_grace_seconds(capabilities: Any, default: int = 15) -> int:
    """capabilities.cancel_grace_seconds; never a local constant."""

    if capabilities is None:
        return int(default)
    require = getattr(capabilities, "require_cancel_grace_seconds", None)
    if callable(require):
        try:
            return int(require())
        except Exception:  # pragma: no cover - defensive
            pass
    value = getattr(capabilities, "cancel_grace_seconds", None)
    if callable(value):
        value = value()
    if value is None and isinstance(capabilities, Mapping):
        value = capabilities.get("cancel_grace_seconds")
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def cancel_message(capabilities: Any) -> str:
    """The stopping line; N comes from the capabilities (spec §5.5.7)."""

    return CANCEL_GRACE_TEMPLATE.format(cancel_grace_seconds(capabilities))


@dataclass
class CommandOutcome:
    """The outcome of one cancel / resume command."""

    action: str = ""
    ok: bool = False
    job_id: str = ""
    status: str = ""
    mode: str = ""
    attempt: Optional[int] = None
    resume_cycles: Optional[int] = None
    partial_available: Optional[bool] = None
    error: Optional[BaseException] = None
    view: Optional[ClientErrorView] = None
    refresh_detail: bool = False
    retryable: bool = False
    notes: List[str] = field(default_factory=list)

    def lines(self) -> List[Tuple[str, str]]:
        if self.view is not None:
            return self.view.lines()
        if self.ok:
            return [("info", self.message())]
        return []

    def message(self) -> str:
        if self.action == "cancel":
            return "已发送取消请求（status={0}）".format(self.status or "-")
        return (
            "已发送恢复请求（mode={0}, attempt={1}, resume_cycles={2}）"
        ).format(self.mode or "-", self.attempt, self.resume_cycles)


def normalize_command_data(data: Any) -> Dict[str, Any]:
    """Read the cancel / resume response payload."""

    if not isinstance(data, Mapping):
        return {}
    return dict(data)


def cancel_outcome(
    job_id: str,
    data: Any = None,
    error: Optional[BaseException] = None,
) -> CommandOutcome:
    """One cancel result (idempotent: a repeat call is not an error)."""

    if error is not None:
        return CommandOutcome(
            action="cancel",
            job_id=job_id,
            error=error,
            view=failure_exit(error, safe_request=False),
        )
    payload = normalize_command_data(data)
    return CommandOutcome(
        action="cancel",
        ok=True,
        job_id=str(payload.get("job_id") or job_id),
        status=str(payload.get("status") or ""),
        partial_available=payload.get("partial_available"),
    )


def resume_outcome(
    job_id: str,
    data: Any = None,
    error: Optional[BaseException] = None,
) -> CommandOutcome:
    """One resume result plus the write back of spec §5.5.7 step 2."""

    if error is not None:
        view = failure_exit(error, safe_request=False)
        reason = str(getattr(error, "reason", "") or "")
        retryable = bool(
            str(getattr(error, "code", "") or "") == "JOB_NOT_RESUMABLE"
            and reason in ("lock_timeout", "resume_in_progress")
        )
        return CommandOutcome(
            action="resume",
            job_id=job_id,
            error=error,
            view=view,
            refresh_detail=view.refresh_detail,
            retryable=retryable,
        )
    payload = normalize_command_data(data)
    attempt = payload.get("attempt")
    cycles = payload.get("resume_cycles")
    return CommandOutcome(
        action="resume",
        ok=True,
        job_id=str(payload.get("job_id") or job_id),
        mode=str(payload.get("mode") or ""),
        attempt=int(attempt) if attempt is not None else None,
        resume_cycles=int(cycles) if cycles is not None else None,
        status=str(payload.get("status") or "queued"),
    )


def command_outcome(
    action: str,
    job_id: str,
    data: Any = None,
    error: Optional[BaseException] = None,
) -> CommandOutcome:
    """Dispatch one cancel / resume result."""

    if action == "cancel":
        return cancel_outcome(job_id, data, error)
    return resume_outcome(job_id, data, error)


# --------------------------------------------------------------------
# The scheduler: one poll tick
# --------------------------------------------------------------------


@dataclass
class PollJob:
    """Everything the GUI thread needs to render one polled job."""

    job_id: str
    page: str
    job: Optional[Any] = None
    events: List[Any] = field(default_factory=list)
    files: Optional[List[Any]] = None
    intervals: List[float] = field(default_factory=list)
    orphaned: bool = False
    transitioned: bool = False
    resume: Dict[str, Any] = field(default_factory=dict)
    unknown_types: List[str] = field(default_factory=list)
    done_event: bool = False
    manual_resume_event: bool = False
    batch: bool = False
    jobs: List[Any] = field(default_factory=list)


@dataclass
class PollOutcome:
    """The outcome of one tick."""

    page: str = PAGE_JOBS
    interval: float = JOBS_INTERVAL
    results: List[PollJob] = field(default_factory=list)
    failures: Optional[FailureState] = None
    red_bar: bool = False
    unauthorized: bool = False
    stopped: bool = False
    error: Optional[BaseException] = None
    error_view: Optional[ClientErrorView] = None
    attempted: int = 0
    batches: int = 0
    orphaned: List[str] = field(default_factory=list)
    pauses: bool = False
    idle: bool = False

    def status_lines(self) -> List[Tuple[str, str]]:
        if self.unauthorized:
            return [("red", UNAUTHORIZED_TEXT)]
        if self.error_view is not None:
            return self.error_view.lines()
        if self.failures is None:
            return []
        tracker = FailureTracker()
        tracker.state = self.failures
        return tracker.status_lines()


def next_interval(
    page: str,
    *,
    terminal: bool = False,
    idle: bool = False,
    jobs_interval: float = JOBS_INTERVAL,
    interval_min: float = INTERVAL_MIN,
    interval_max: float = INTERVAL_MAX,
) -> float:
    """The interval of one page (spec §5.5.1 / §5.5.2).

    The 60 s tier is shared by the terminal fallback, the results page
    after a terminal job and the "no active job" idle refresh.
    """

    if idle:
        return IDLE_INTERVAL
    if page == PAGE_JOBS:
        span = max(float(interval_min), 0.0)
        return max(span, min(float(jobs_interval), float(interval_max)))
    if page == PAGE_DETAIL:
        return FALLBACK_INTERVAL if terminal else DETAIL_INTERVAL
    if page == PAGE_RESULTS:
        return FALLBACK_INTERVAL if terminal else RESULTS_INTERVAL
    return JOBS_INTERVAL


def _record_terminal(record: Optional[TaskRecord]) -> bool:
    """The same formula, applied to a ledger row."""

    if record is None:
        return False
    if close_by_model(record):
        return bool(record.is_terminal)
    return record.finished_at is not None


def job_interval(
    page: str,
    record: Optional[TaskRecord],
    *,
    jobs_interval: float = JOBS_INTERVAL,
    idle: bool = False,
) -> float:
    """The interval derived from the ledger row and the unique formula."""

    return next_interval(
        page,
        terminal=_record_terminal(record),
        idle=idle,
        jobs_interval=jobs_interval,
    )


class Scheduler:
    """The pure state machine behind one polling worker (spec §5.5).

    list_fn / detail_fn / events_fn / files_fn are the only seams:
    production passes the client methods, the tests pass fakes.
    Nothing here touches Qt.
    """

    def __init__(
        self,
        store: Store,
        *,
        list_fn: Callable[..., Any],
        detail_fn: Callable[[str], Any],
        events_fn: Optional[Callable[[str, int], Any]] = None,
        files_fn: Optional[Callable[..., Any]] = None,
        policy: Optional[api.RetryPolicy] = None,
        limit: int = api.JOBS_BATCH_LIMIT_DEFAULT,
        jobs_interval: float = JOBS_INTERVAL,
        record: Optional[Callable[[TaskRecord], Any]] = None,
        render: Optional[Callable[[PollJob], Any]] = None,
        interval_min: float = INTERVAL_MIN,
        interval_max: float = INTERVAL_MAX,
    ) -> None:
        self.store = store
        self.list_fn = list_fn
        self.detail_fn = detail_fn
        self.events_fn = events_fn
        self.files_fn = files_fn
        self.tracker = FailureTracker(policy)
        self.limit = int(limit)
        self.jobs_interval = float(jobs_interval)
        self.record = record
        self.render = render
        self.interval_min = float(interval_min)
        self.interval_max = float(interval_max)

    # -- ledger ------------------------------------------------------

    def ledger(self) -> TasksLedger:
        return self.store.load_ledger()

    def _save(self, ledger: TasksLedger) -> None:
        self.store.save_ledger(ledger)

    def _publish(self, record: TaskRecord) -> None:
        if self.record is not None:
            self.record(record)

    def _note(self, job_id: str, **fields: Any) -> Optional[TaskRecord]:
        """Patch one ledger row and persist it."""

        ledger = self.ledger()
        current = ledger.record(job_id)
        if current is None:
            return None
        for name, value in fields.items():
            if name == "manual_resume":
                current.manual_resume = dict(value or {})
            else:
                setattr(current, name, value)
        ledger.upsert_record(current)
        self._save(ledger)
        self._publish(current)
        return current

    # -- request path ------------------------------------------------

    def _safe_call(
        self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any
    ) -> Any:
        """One safe GET, with the silent same-tick resend of §5.5.6.

        The first consecutive failure of a safe request - no HTTP
        response or a 5xx - is neither shown nor waited on: the very
        same call is issued once more before the tick ends (position
        c == 1 waits 0 s).  401, a 4xx answer and every failure from the
        second one on go straight to the failure exit, which counts them
        and renders the ladder position.
        """

        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if self.tracker.consecutive_failures != 0 or not (
                self.tracker.policy.retryable("GET", exc)
            ):
                raise
            # Count the first failure (its own ladder position is 0 s)
            # and resend the identical request inside this same tick.
            self.tracker.record_failure(exc)
            return fn(*args, **kwargs)

    def _jobs_interval(self, *, idle: bool) -> float:
        """The list page interval, honouring the injected bounds."""

        return next_interval(
            PAGE_JOBS,
            jobs_interval=self.jobs_interval,
            idle=idle,
            interval_min=self.interval_min,
            interval_max=self.interval_max,
        )

    # -- the tick ----------------------------------------------------

    def tick(
        self,
        page: str,
        *,
        job_id: str = "",
        active: Optional[Sequence[str]] = None,
        visible: bool = True,
        permissions_ok: bool = True,
    ) -> PollOutcome:
        """Run exactly one poll tick of one page."""

        outcome = PollOutcome(page=page)
        if not permissions_ok:
            # Only 401 stops the polling for good (spec §5.5.6).
            self.tracker.reset()
            outcome.unauthorized = True
            outcome.stopped = True
            outcome.interval = FALLBACK_INTERVAL
            return outcome
        if not visible:
            # Minimised window / hidden page: pause and pull once on show.
            outcome.pauses = True
            outcome.interval = next_interval(
                page, jobs_interval=self.jobs_interval
            )
            return outcome

        ledger = self.ledger()
        if page == PAGE_JOBS:
            ids = list(active or [])
            if not ids:
                ids = [item.job_id for item in ledger.records if item.job_id]
            outcome.idle = not any(
                not _record_terminal(ledger.record(item)) for item in ids
            )
            return self._poll_jobs(outcome, ids, ledger)

        current = ledger.record(job_id) if job_id else None
        if not job_id:
            outcome.interval = IDLE_INTERVAL
            outcome.idle = True
            return outcome
        return self._poll_job(outcome, job_id, current)

    # -- list page ---------------------------------------------------

    def _poll_jobs(
        self,
        outcome: PollOutcome,
        job_ids: Sequence[str],
        ledger: TasksLedger,
    ) -> PollOutcome:
        chunks = batch_ids(job_ids, self.limit)
        outcome.batches = len(chunks)
        collected: List[Any] = []
        orphans: List[str] = []
        terminal = True
        for chunk in chunks:
            batch = self._fetch_batch(chunk)
            if batch.error is not None:
                outcome.error = batch.error
                outcome.error_view = self._fail(outcome, batch.error)
                outcome.failures = replace(self.tracker.state)
                outcome.red_bar = self.tracker.red_bar
                outcome.interval = max(
                    self._jobs_interval(idle=outcome.idle),
                    self.tracker.wait_seconds(),
                )
                return outcome
            outcome.attempted += 1
            self._success()
            collected.extend(batch.jobs)
            orphans.extend(batch.not_found_ids)
        outcome.orphaned = orphans
        merged, _ = merge_jobs(collected)
        for job in merged:
            result = self._store_job(
                page=PAGE_JOBS, job=job, ledger=ledger
            )
            if result is not None:
                outcome.results.append(result)
                if not result.orphaned:
                    terminal = terminal and _record_terminal(
                        ledger.record(result.job_id)
                    )
        for orphan in orphans:
            self._note(
                orphan,
                status=store_mod.RECORD_STATUS_ORPHANED,
                notes=ORPHAN_NOTE,
            )
        outcome.idle = bool(job_ids) and terminal
        outcome.interval = self._jobs_interval(idle=outcome.idle)
        outcome.failures = replace(self.tracker.state)
        outcome.red_bar = self.tracker.red_bar
        return outcome

    def _success(self) -> None:
        """A 2xx arrived: back to the page interval, tier reset."""

        self.tracker.record_response(200)

    def _fetch_batch(self, ids: Sequence[str]) -> PendingBatch:
        batch = PendingBatch(requested=list(ids))
        try:
            data = self._safe_call(
                self.list_fn, ids=list(ids), limit=self.limit
            )
        except Exception as exc:  # noqa: BLE001 - reported as a batch error
            batch.error = exc
            return batch
        batch.ok = True
        jobs: List[Any] = []
        not_found: List[str] = []
        if isinstance(data, Mapping):
            raw_jobs = data.get("jobs")
            if isinstance(raw_jobs, (list, tuple)):
                jobs = list(raw_jobs)
            raw_missing = data.get("not_found_ids")
            if isinstance(raw_missing, (list, tuple)):
                not_found = [str(item) for item in raw_missing]
        batch.jobs = jobs
        batch.not_found_ids = not_found
        return batch

    # -- one job -----------------------------------------------------

    def _poll_job(
        self,
        outcome: PollOutcome,
        job_id: str,
        record: Optional[TaskRecord],
    ) -> PollOutcome:
        page = outcome.page
        try:
            data = self._safe_call(self.detail_fn, job_id)
        except Exception as exc:  # noqa: BLE001 - failure exit
            outcome.error = exc
            outcome.error_view = self._fail(outcome, exc, job_id)
            outcome.failures = replace(self.tracker.state)
            outcome.red_bar = self.tracker.red_bar
            outcome.interval = max(
                job_interval(
                    page, record, jobs_interval=self.jobs_interval
                ),
                self.tracker.wait_seconds(),
            )
            return outcome
        outcome.attempted += 1
        self._success()
        job = data if data is not None else {}
        was_terminal = _record_terminal(record)
        now_terminal = is_terminal(job)
        transitioned = bool(was_terminal) and not now_terminal
        previous_seq = int(getattr(record, "last_seq", 0) or 0)
        if transitioned:
            # The fallback channel may only touch the seven fields.
            result = self._store_job(
                page=page,
                job=job,
                ledger=self.ledger(),
                record=record,
                partial_fields=TRANSITION_FIELDS,
            )
            if result is not None:
                result.transitioned = True
                outcome.results.append(result)
        else:
            result = self._store_job(
                page=page, job=job, ledger=self.ledger(), record=record
            )
            if result is not None:
                outcome.results.append(result)
        if page == PAGE_DETAIL and self.events_fn is not None:
            # The fallback channel pulls the job object only (spec §5.5.2).
            if not now_terminal:
                self._consume_events(
                    outcome, job_id, previous_seq, transitioned=transitioned
                )
        if page == PAGE_RESULTS and self.files_fn is not None:
            if not now_terminal or transitioned:
                self._consume_files(outcome, job_id, transitioned)
        merged = self.ledger().record(job_id)
        # The events / files call of this tick shares the one failure
        # schedule: a failed sub request must not be repeated at the page
        # cadence once the ladder is above it (spec §5.5.6).  On a
        # successful tick wait_seconds() is 0 and this is a no-op.
        outcome.interval = max(
            job_interval(page, merged, jobs_interval=self.jobs_interval),
            self.tracker.wait_seconds(),
        )
        if now_terminal and not transitioned:
            outcome.interval = FALLBACK_INTERVAL
        outcome.idle = False
        outcome.failures = replace(self.tracker.state)
        outcome.red_bar = self.tracker.red_bar
        return outcome

    def _consume_events(
        self,
        outcome: PollOutcome,
        job_id: str,
        previous_seq: int,
        *,
        transitioned: bool,
    ) -> None:
        try:
            data = self._safe_call(self.events_fn, job_id, previous_seq)
        except Exception as exc:  # noqa: BLE001 - failure exit
            outcome.error = exc
            outcome.error_view = self._fail(outcome, exc, job_id)
            outcome.failures = replace(self.tracker.state)
            return
        outcome.attempted += 1
        self._success()
        events: List[Any] = []
        last_seq = previous_seq
        if isinstance(data, Mapping):
            raw = data.get("events")
            if isinstance(raw, (list, tuple)):
                events = list(raw)
            try:
                last_seq = int(data.get("last_seq"))
            except (TypeError, ValueError):
                last_seq = previous_seq
        fresh: List[Any] = []
        seen: set = set()
        highest = previous_seq
        for event in events:
            seq = job_total(event, "seq")
            if isinstance(seq, int) and not isinstance(seq, bool):
                if seq in seen or seq <= previous_seq:
                    continue
                seen.add(seq)
                highest = max(highest, seq)
            fresh.append(event)
        batch = EventBatch(
            events=fresh,
            last_seq=max(highest, last_seq),
            previous_seq=previous_seq,
            unknown_types=[
                str(job_total(item, "type", "") or "")
                for item in fresh
                if str(job_total(item, "type", "") or "")
                not in KNOWN_EVENT_TYPES
            ],
            done=any(
                str(job_total(item, "type", "") or "") == "done"
                for item in fresh
            ),
        )
        for event in fresh:
            if str(job_total(event, "type", "") or "") == "manual_resume":
                batch.resume = manual_resume_payload(event)
        self._note(job_id, last_seq=batch.last_seq)
        if batch.resume:
            # Same guard as apply_event / mark_manual_resume: a payload
            # that only carries "mode" (no attempt / resume_cycles) must
            # not overwrite the two counter fields with None (spec
            # §5.5.3, §5.5.7 step 2).
            updates: Dict[str, Any] = {"manual_resume": dict(batch.resume)}
            if batch.resume.get("attempt") is not None:
                updates["attempt"] = batch.resume["attempt"]
            if batch.resume.get("resume_cycles") is not None:
                updates["resume_cycles"] = batch.resume["resume_cycles"]
            self._note(job_id, **updates)
        if outcome.results:
            target = outcome.results[-1]
            target.events = batch.events
            target.unknown_types = batch.unknown_types
            target.done_event = batch.done
            target.manual_resume_event = bool(batch.resume)
            target.resume = dict(batch.resume)
            if transitioned:
                target.transitioned = True

    def _consume_files(
        self, outcome: PollOutcome, job_id: str, transitioned: bool
    ) -> None:
        try:
            data = self._safe_call(
                self.files_fn, job_id, include_partial=True
            )
        except Exception as exc:  # noqa: BLE001 - failure exit
            outcome.error = exc
            outcome.error_view = self._fail(outcome, exc, job_id)
            outcome.failures = replace(self.tracker.state)
            return
        outcome.attempted += 1
        self._success()
        files: List[Any] = []
        if isinstance(data, Mapping):
            raw = data.get("files")
            if isinstance(raw, (list, tuple)):
                files = list(raw)
        elif isinstance(data, (list, tuple)):
            files = list(data)
        self._note(job_id, files_count=len(files))
        if outcome.results:
            target = outcome.results[-1]
            target.files = files
            if transitioned:
                target.transitioned = True

    # -- shared ------------------------------------------------------

    def _store_job(
        self,
        *,
        page: str,
        job: Any,
        ledger: TasksLedger,
        record: Optional[TaskRecord] = None,
        partial_fields: Optional[Sequence[str]] = None,
    ) -> Optional[PollJob]:
        job_id = str(job_total(job, "job_id", "") or "")
        if not job_id:
            return None
        current = record if record is not None else ledger.record(job_id)
        before = _record_terminal(current)
        merged = merge_job_into_ledger(
            current,
            job,
            partial_fields=partial_fields,
        )
        ledger.upsert_record(merged)
        self._save(ledger)
        self._publish(merged)
        after_terminal = is_terminal(job)
        return PollJob(
            job_id=job_id,
            page=page,
            job=job,
            intervals=[job_interval(page, merged)],
            transitioned=bool(before and not after_terminal),
        )

    def _fail(
        self,
        outcome: PollOutcome,
        error: BaseException,
        job_id: str = "",
    ) -> ClientErrorView:
        """One failed call: count it, render it, honour its actions."""

        status = getattr(error, "http_status", None)
        try:
            status = int(status) if status is not None else None
        except (TypeError, ValueError):
            status = None
        if isinstance(error, api.UnauthorizedError) or status == 401:
            self.tracker.reset()
            outcome.unauthorized = True
            outcome.stopped = True
            view = client_error_view(error)
            view.stop_polling = True
            return view
        self.tracker.record_failure(error, status)
        if isinstance(error, api.JobNotFoundError) or status == 404:
            if job_id:
                self._note(
                    job_id,
                    status=store_mod.RECORD_STATUS_ORPHANED,
                    notes=ORPHAN_NOTE,
                )
            # A 404 is an answer carrying a 4xx status: it hides the red
            # bar and resets the ladder (spec §5.5.6 "归零 / 保留档位",
            # whose own example is 404 JOB_NOT_FOUND -> orphaned, "no
            # more network back off").
            self.tracker.record_response(status if status else 404)
            return client_error_view(error)
        return client_error_view(error)




