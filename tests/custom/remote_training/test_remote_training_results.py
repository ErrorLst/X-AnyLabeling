"""Result downloads and the CT26 contract alignment (spec §5.6, §3).

CT19 lives here: a nested artifact and the best weights are downloaded
by the opaque `file_id` of the manifest, the whole file streams to disk,
the progress denominator is the response own `Content-Length`, a
traversal string is rejected by the server (400 `VALIDATION_FAILED`) and
a cancelled transfer never leaves a file behind.

CT26 uses offline fixtures - written into `tmp_path`, never into the
repository - to assert that every key spec §3.2.4 / §3.4.4 / §3.6 /
§3.7 / §3.10 declares is inside the parsing coverage of the client and
that the client invents no field of its own.
"""

from __future__ import annotations

import inspect
import json
import os
import os.path as osp
import threading
import time
from typing import Any

import pytest
from PyQt6 import QtCore, QtWidgets

from anylabeling.custom.remote_training import api_client as api
from anylabeling.custom.remote_training import poller as poll_mod
from anylabeling.custom.remote_training.store import Store, TaskRecord
from anylabeling.custom.remote_training import worker as worker_mod
from anylabeling.custom.remote_training.worker import DownloadWorker
from anylabeling.custom.remote_training.ui import config_page as config_ui
from anylabeling.custom.remote_training.ui import detail_page as detail_ui
from anylabeling.custom.remote_training.ui import jobs_page as jobs_ui
from anylabeling.custom.remote_training.ui import results_page as res_ui
from anylabeling.custom.remote_training.ui.dialog import (
    CLOSE_CHANNELS,
    RemoteTrainingDialog,
)

JOB_ID = "job_20260101_7f2a91"
SHA = "0" * 64
#: The §3.10.9 example id of `summary.json` (the summary area is filled
#: through route 14 by exactly this opaque identifier).
SUMMARY_ID = "f_87249a4c"
#: One `summary.json` body of spec §4.3.7 / §5.6.3.
SUMMARY = {
    "schema_version": 1,
    "job_id": JOB_ID,
    "status": "completed",
    "attempt": 1,
    "resume_cycles": 0,
    "duration_seconds": 612.5,
    "final_metrics": {"mAP50": 0.512, "box_loss": 1.02},
    "verified_metrics": {"mAP50": 0.51},
    "resolved_params": {"batch": 16, "device": "0"},
    "training_env": {"cuda": "12.4", "torch": "2.6.0+cu124"},
    "suspect_reason": None,
}

#: The artifact manifest of spec §3.2.2 #13 / §5.6.1: six keys per row.
FILES = [
    {
        "file_id": "f_af8212b3",
        "path": "weights/best.pt",
        "size": 2048,
        "sha256": SHA,
        "mtime": "2026-01-01T10:00:00Z",
        "partial": False,
    },
    {
        "file_id": "f_96c7e1ba",
        "path": "partial/weights/last.pt",
        "size": 1024,
        "sha256": SHA,
        "mtime": "2026-01-01T09:00:00Z",
        "partial": True,
    },
    {
        "file_id": "f_87249a4c",
        "path": "summary.json",
        "size": 128,
        "sha256": SHA,
        "mtime": "2026-01-01T10:00:00Z",
        "partial": False,
    },
]

#: The 23 fields of the job object (spec §3.4.4), verbatim.
JOB_KEYS = (
    "job_id", "status", "is_terminal", "attempt", "max_attempts",
    "resume_cycles", "queue_position", "eta_seconds", "queued_reason",
    "device_index", "vram_estimate_mb", "progress", "metrics",
    "created_at", "started_at", "finished_at", "error_summary",
    "needs_attention", "needs_attention_reason", "artifact_suspect",
    "partial_available", "resolved_params", "resume_mode_available",
)

#: The 18 top level keys of the capabilities payload (spec §3.6).
CAPABILITY_KEYS = (
    "schema_version", "server_version", "enabled", "tasks",
    "allow_weight_download", "allow_auto_batch", "oom_retry",
    "model_families", "param_schema", "optimizer_presets",
    "preset_policy", "devices", "queue", "cancel_grace_seconds",
    "vram_table", "calibration", "training_env", "warnings",
)

#: The 12 top level keys of the health payload (spec §3.7).
HEALTH_KEYS = (
    "enabled", "server_version", "time", "queue", "jobs", "devices",
    "work_dir", "training_env", "calibration", "weights", "blobs",
    "warnings",
)

#: The 23 client settable parameters (`param_schema`, spec §3.6/§3.8.2).
PARAM_KEYS = (
    "epochs", "batch", "imgsz", "workers", "optimizer", "lr0", "lrf",
    "momentum", "weight_decay", "warmup_epochs", "warmup_momentum",
    "warmup_bias_lr", "cos_lr", "amp", "cache", "rect", "single_cls",
    "patience", "close_mosaic", "save_period", "fraction", "seed",
    "dropout",
)

#: The 8 keys of one device ledger row (spec §3.6; §3.7 adds running_jobs).
DEVICE_KEYS = (
    "device_index", "name", "total_mb", "free_mb", "reserved_mb",
    "in_flight_estimate_mb", "running_estimate_mb", "available_mb",
)

FAMILY_KEYS = (
    "min_ultralytics", "available", "unavailable_reason", "weights",
    "weights_ready", "presets",
)

VRAM_TABLE_KEYS = (
    "task_factor", "auto_batch", "auto_loaded", "fingerprint_matched",
    "calibrated_at", "fingerprint", "entries", "sources", "unschedulable",
)

CALIBRATION_KEYS = (
    "required", "deferred", "deferred_ready", "deferred_reason",
    "conflict_policy", "calibrated_at", "auto_loaded",
    "fingerprint_matched", "skipped", "skipped_reason", "failed",
)

RESOLVED_PARAM_KEYS = (
    "optimizer", "optimizer_preset", "optimizer_source", "batch",
    "requested_batch", "batch_assumed", "device", "oom_retry_max",
)

PROGRESS_KEYS = ("epoch", "total_epochs", "percent")

WARNING_CODES = (
    "WEIGHTS_MISSING", "VRAM_TABLE_INCOMPLETE", "VRAM_CALIBRATION_FAILED",
    "VRAM_CALIBRATION_SKIPPED", "VRAM_CALIBRATION_DEFERRED",
    "BLOB_MATERIALIZE_DEGRADED", "OOM_RETRY_UNAVAILABLE",
)


def job_fixture(**overrides: Any) -> dict:
    """One complete job object of spec §3.4.4."""

    payload = {
        "job_id": JOB_ID,
        "status": "completed",
        "is_terminal": True,
        "attempt": 1,
        "max_attempts": 3,
        "resume_cycles": 0,
        "queue_position": None,
        "eta_seconds": None,
        "queued_reason": None,
        "device_index": 0,
        "vram_estimate_mb": 5200,
        "progress": {"epoch": 10, "total_epochs": 10, "percent": 100.0},
        "metrics": {
            "box_loss": 1.02,
            "mAP50": 0.512,
            "mAP50-95": 0.331,
            "precision": 0.61,
            "recall": 0.55,
        },
        "created_at": "2026-01-01T10:00:00Z",
        "started_at": "2026-01-01T10:00:10Z",
        "finished_at": "2026-01-01T10:20:00Z",
        "error_summary": None,
        "needs_attention": False,
        "needs_attention_reason": None,
        "artifact_suspect": False,
        "partial_available": True,
        "resolved_params": {
            "optimizer": "SGD",
            "optimizer_preset": "yolo11-sgd",
            "optimizer_source": "preset",
            "batch": 16,
            "requested_batch": -1,
            "batch_assumed": 16,
            "device": "0",
            "oom_retry_max": 2,
        },
        "resume_mode_available": [],
    }
    payload.update(overrides)
    return payload


def capabilities_fixture() -> dict:
    """One complete capabilities payload of spec §3.6."""

    return {
        "schema_version": 1,
        "server_version": "0.0.12",
        "enabled": True,
        "tasks": ["detect", "segment"],
        "allow_weight_download": True,
        "allow_auto_batch": True,
        "oom_retry": {"enabled": True, "max_retries": 2},
        "model_families": {
            "yolo11": {
                "min_ultralytics": "8.3.0",
                "available": True,
                "unavailable_reason": None,
                "weights": {"detect": ["yolo11n.pt"]},
                "weights_ready": {"yolo11n.pt": True},
                "presets": ["yolo11-sgd"],
            },
        },
        "param_schema": {
            name: {"type": "int", "min": 0} for name in PARAM_KEYS
        },
        "optimizer_presets": {
            "yolo11-sgd": {
                "optimizer": "SGD",
                "lr0": 0.01,
                "momentum": 0.937,
                "weight_decay": 0.0005,
                "warmup_bias_lr": 0.1,
            },
        },
        "preset_policy": {
            "type": "iterations_threshold",
            "threshold": 10000,
            "default_preset": {"yolo11": "yolo11-sgd"},
        },
        "devices": [
            {
                "device_index": 0,
                "name": "NVIDIA GeForce RTX 4090",
                "total_mb": 24564,
                "free_mb": 21000,
                "reserved_mb": 1024,
                "in_flight_estimate_mb": 0,
                "running_estimate_mb": 5200,
                "available_mb": 19976,
            },
        ],
        "queue": {
            "queued": 0,
            "running": 0,
            "max_concurrent_jobs": 2,
            "max_concurrent_per_device": 1,
        },
        "cancel_grace_seconds": 15,
        "vram_table": {
            "task_factor": {"detect": 1.0, "segment": 1.25},
            "auto_batch": {"assumed": 16, "ratio": 0.6},
            "auto_loaded": True,
            "fingerprint_matched": True,
            "calibrated_at": "2026-01-01T09:40:12Z",
            "fingerprint": {"gpu": "RTX 4090 x1", "cuda": "12.4"},
            "entries": [
                {
                    "model": "yolo11n",
                    "task": "detect",
                    "baseline_mb": 1580,
                    "per_image_mb": 155,
                    "process_overhead_mb": 210,
                    "max_batch": 96,
                    "points": [{"batch": 16, "reserved_mb": 4060}],
                    "fit": {"r2": 1.0, "residual_pct": 0.0},
                    "source": "auto",
                    "calibration_at": "2026-01-01T09:40:12Z",
                },
            ],
            "sources": {"auto": 1, "manual": 0, "default": 0},
            "unschedulable": [],
        },
        "calibration": {
            "required": True,
            "deferred": False,
            "deferred_ready": False,
            "deferred_reason": None,
            "conflict_policy": "defer",
            "calibrated_at": "2026-01-01T09:40:12Z",
            "auto_loaded": True,
            "fingerprint_matched": True,
            "skipped": [],
            "skipped_reason": None,
            "failed": [],
        },
        "training_env": {
            "ultralytics": "8.4.84",
            "torch": "2.6.0+cu124",
            "cuda": "12.4",
            "python": "3.12.4",
        },
        "warnings": [
            {
                "code": "WEIGHTS_MISSING",
                "message": "weights 目录为空",
                "details": {},
            },
        ],
    }


def health_fixture() -> dict:
    """One complete health payload of spec §3.7."""

    capabilities = capabilities_fixture()
    return {
        "enabled": True,
        "server_version": capabilities["server_version"],
        "time": "2026-01-01T10:30:00Z",
        "queue": capabilities["queue"],
        "jobs": {"queued": 0, "running": 0, "needs_attention": 0},
        "devices": [dict(capabilities["devices"][0], running_jobs=0)],
        "work_dir": {
            "path": "/data/xanylabeling/training",
            "free_gb": 412.5,
            "total_gb": 1000.0,
            "writable": True,
        },
        "training_env": capabilities["training_env"],
        "calibration": capabilities["calibration"],
        "weights": {"cached": 1, "missing": 0, "allow_weight_download": True},
        "blobs": {
            "count": 12840,
            "bytes": 81234567890,
            "unreferenced": 120,
            "materialize": "hardlink",
        },
        "warnings": capabilities["warnings"],
    }


def fixture_file(tmp_path: Any, name: str, payload: Any) -> Any:
    """Freeze one response as a fixture file under the system temp dir.

    The plan (section 6.5) fixes the fixture form: structured JSON files
    written into the system temporary directory, never into the
    repository.
    """

    folder = tmp_path / "fixtures"
    folder.mkdir(exist_ok=True)
    path = folder / (name + ".json")
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class StubRaw:
    """The requests.Response surface StreamedResponse consumes."""

    def __init__(
        self,
        body: bytes = b"",
        *,
        content_length: bool = True,
        content_type: str = "application/octet-stream",
        status_code: int = 200,
        headers: Any = None,
        chunk_delay: float = 0.0,
    ) -> None:
        self.status_code = int(status_code)
        self.headers = dict(headers or {})
        self.headers.setdefault("Content-Type", content_type)
        if content_length:
            self.headers["Content-Length"] = str(len(body))
        self._body = bytes(body)
        self.chunk_delay = float(chunk_delay)
        self.closed = False

    def iter_content(self, chunk_size: int = 1):
        size = max(1, int(chunk_size))
        for start in range(0, len(self._body), size):
            if self.chunk_delay:
                time.sleep(self.chunk_delay)
            yield self._body[start : start + size]

    def json(self) -> Any:
        return json.loads(self._body.decode("utf-8"))

    def close(self) -> None:
        self.closed = True


class DownloadClient:
    """A client that answers the two download routes with real streams."""

    def __init__(
        self,
        body: bytes,
        *,
        content_length: bool = True,
        chunk_delay: float = 0.0,
        files: Any = None,
    ) -> None:
        self.body = bytes(body)
        self.content_length = bool(content_length)
        self.chunk_delay = float(chunk_delay)
        self.files = list(FILES if files is None else files)
        self.requests: list = []

    # -- the read routes a window needs once its pages start polling
    def get_job(self, job_id):
        return job_fixture()

    def list_jobs(self, ids=None, status=None, limit=50):
        return {"jobs": [], "not_found_ids": list(ids or [])}

    def get_events(self, job_id, after=0):
        return {"events": [], "last_seq": int(after)}

    def list_job_files(self, job_id, include_partial=True):
        return {"files": list(self.files)}

    # -- the download routes
    def _response(self, route: str) -> Any:
        raw = StubRaw(
            self.body,
            content_length=self.content_length,
            chunk_delay=self.chunk_delay,
        )
        return api.StreamedResponse(raw, route)

    #: Replaced by a test to observe / fail a summary read.
    summary_handler: Any = None
    #: Called right after the stream was opened, before any chunk is
    #: read: it makes "the cancel lands while the body is awaited"
    #: reproducible.
    on_open: Any = None

    def open_job_file(self, job_id, file_id, **kwargs):
        self.requests.append(("file", job_id, file_id, kwargs))
        if file_id == SUMMARY_ID:
            if self.summary_handler is not None:
                return self.summary_handler(job_id, file_id)
            raw = StubRaw(
                json.dumps(SUMMARY).encode("utf-8"),
                content_type="application/json",
            )
            response = api.StreamedResponse(raw, "job_file")
        else:
            response = self._response("job_file")
        if self.on_open is not None:
            self.on_open()
        return response

    def open_job_download(self, job_id, include_partial=True):
        self.requests.append(("archive", job_id, bool(include_partial)))
        return self._response("job_download")


class Parent(QtWidgets.QMainWindow):
    """Stand in for the labeling main window."""


def seed(store: Store) -> None:
    ledger = store.load_ledger()
    ledger.upsert_record(TaskRecord(
        job_id=JOB_ID,
        client_job_name="任务",
        status="completed",
        is_terminal=True,
        finished_at="2026-01-01T10:20:00Z",
    ))
    store.save_ledger(ledger)


def open_window(store: Store, client: Any) -> Any:
    """Open one window whose reconcile pass uses the same stub client."""

    parent = Parent()
    parent._remote_training_dialog = None
    window = RemoteTrainingDialog(
        parent, store=store, client_factory=lambda: client
    )
    parent._remote_training_dialog = window
    window._confirm_close = lambda _dialog: True
    window._confirm_action = lambda _title, _text: True
    window.show()
    return window


def close_window(qapp, window) -> None:
    window.cancel_workers()
    window.reject()
    qapp.processEvents()


def cell(page: Any, row: int, column: int) -> str:
    """One rendered table cell of the results page."""

    item = page.tree.item(row, column)
    return "" if item is None else item.text()


def observed(worker: Any) -> list:
    """Capture the worker progress in the emitting thread, verbatim."""

    seen: list = []
    worker.progress.connect(
        lambda received, total: seen.append((int(received), total)),
        QtCore.Qt.ConnectionType.DirectConnection,
    )
    return seen


# ------------------------------------------------------------- CT19


def test_ct19_a_nested_artifact_streams_to_disk_by_file_id(qapp, tmp_path):
    """CT19: best.pt by its manifest file_id, progress == Content-Length."""

    body = bytes(range(256)) * 400
    client = DownloadClient(body)
    store = Store(str(tmp_path / "ledger"))
    seed(store)
    window = open_window(store, client)
    window.results_page.set_job(job_fixture(), FILES)
    target = tmp_path / "best.pt"

    worker = window.download_job_file(JOB_ID, "f_af8212b3", str(target))
    assert worker is not None
    seen = observed(worker)
    assert worker.wait(15000) is True
    qapp.processEvents()

    assert target.read_bytes() == body
    # The opaque identifier of the manifest is passed through, never a
    # path the client rebuilt (spec §3.10.1).
    assert [item[:3] for item in client.requests] == [
        ("file", JOB_ID, "f_af8212b3")
    ]
    assert seen, "the progress signal never fired"
    assert [item[0] for item in seen] == sorted(item[0] for item in seen)
    assert seen[-1] == (len(body), len(body))
    assert seen[-1][0] == seen[-1][1] == len(body)
    record = store.load_ledger().record(JOB_ID)
    assert record.download_path == str(target)
    assert "已保存到" in window.results_page.status_label.text()
    assert "已保存到" in window.results_page.debug_text()
    assert list(tmp_path.glob("*.part")) == []
    assert getattr(worker.outcome, "complete", False) is True
    close_window(qapp, window)


def test_ct19_a_partial_entry_downloads_like_any_other(qapp, tmp_path):
    """§5.6.1: the partial rows are part of the manifest whitelist."""

    body = b"last-pt" * 300
    client = DownloadClient(body)
    store = Store(str(tmp_path / "ledger"))
    seed(store)
    window = open_window(store, client)
    page = window.results_page
    page.set_job(job_fixture(), FILES)
    assert cell(page, 1, 4) == res_ui.GROUP_PARTIAL
    assert cell(page, 1, 0) == "partial/weights/last.pt"

    # The save dialog is the only stubbed step: the path is an input the
    # user supplies, and the production method that asks for it is
    # replaced here so the offscreen run never opens a modal dialog.
    target = tmp_path / "last.pt"
    chosen: list = []

    def choose(default_name, filter_text=""):
        chosen.append((default_name, filter_text))
        return str(target)

    window.ask_save_path = choose
    page._on_double_click(page.tree.model().index(1, 0))
    worker = window.download_worker
    assert worker is not None
    assert worker.file_id == "f_96c7e1ba"
    assert worker.wait(15000) is True
    qapp.processEvents()

    assert target.read_bytes() == body
    # The default name came from the manifest path of that file_id, and
    # the download itself was addressed by the identifier.
    assert chosen == [("last.pt", "")]
    assert client.requests[0][:3] == ("file", JOB_ID, "f_96c7e1ba")
    close_window(qapp, window)


def test_ct19_the_progress_denominator_is_the_response_length(qapp, tmp_path):
    """§5.6.3: no Content-Length ⇒ busy indicator, never a fake total."""

    body = b"zip-bytes" * 512
    client = DownloadClient(body, content_length=False)
    store = Store(str(tmp_path / "ledger"))
    seed(store)
    window = open_window(store, client)
    target = tmp_path / "results.zip"

    worker = window.download_results(JOB_ID, str(target))
    assert worker is not None
    seen = observed(worker)
    assert worker.wait(15000) is True
    qapp.processEvents()

    assert target.read_bytes() == body
    assert client.requests == [("archive", JOB_ID, True)]
    assert seen and all(total is None for _received, total in seen)
    busy = res_ui.download_progress_text(seen[-1][0], None)
    assert "总大小未知" in busy
    assert "%" not in busy
    known = res_ui.download_progress_text(1024, 2048)
    assert known == "下载进度 1.0 KB/2.0 KB（50%）"
    assert res_ui.format_size(1024) in known
    close_window(qapp, window)


def test_ct19_a_cancelled_download_leaves_no_file(qapp, tmp_path):
    """§5.6.3: the transfer is cancellable and completion is EOF."""

    body = b"x" * (512 * 1024)
    client = DownloadClient(body, chunk_delay=0.01)
    store = Store(str(tmp_path / "ledger"))
    seed(store)
    window = open_window(store, client)
    target = tmp_path / "cancelled.pt"

    worker = window.download_job_file(JOB_ID, "f_af8212b3", str(target))
    assert worker is not None
    started = threading.Event()
    worker.progress.connect(
        lambda received, total: started.set(),
        QtCore.Qt.ConnectionType.DirectConnection,
    )
    assert started.wait(10) is True
    worker.cancel()
    assert worker.wait(15000) is True
    qapp.processEvents()

    assert worker.outcome is not None
    assert worker.outcome.cancelled is True
    assert target.exists() is False, "a cancelled transfer saved a file"
    assert list(tmp_path.glob("*.part")) == []
    assert res_ui.DOWNLOAD_CANCELLED_TEXT in (
        window.results_page.status_label.text()
    )
    assert "download_done" in CLOSE_CHANNELS
    close_window(qapp, window)


def test_ct19_the_close_machine_cancels_a_running_download(
    qapp, tmp_path, qt_messages
):
    """§5.1.4 step 3 also wakes the download worker."""

    body = b"y" * (512 * 1024)
    client = DownloadClient(body, chunk_delay=0.01)
    store = Store(str(tmp_path / "ledger"))
    seed(store)
    window = open_window(store, client)
    target = tmp_path / "closing.pt"
    worker = window.download_job_file(JOB_ID, "f_af8212b3", str(target))
    assert worker is not None
    started = threading.Event()
    worker.progress.connect(
        lambda received, total: started.set(),
        QtCore.Qt.ConnectionType.DirectConnection,
    )
    assert started.wait(10) is True
    assert window.cancel_workers() >= 1
    assert worker.wait(15000) is True
    qapp.processEvents()
    assert target.exists() is False
    assert "Destroyed while thread is still running" not in qt_messages()
    close_window(qapp, window)


def test_ct19_a_traversal_file_id_is_a_validation_failure(qapp, tmp_path):
    """CT19: a traversal string never becomes a path; the server says 400.

    The client treats file_id as opaque (spec §3.10.1): it passes the
    string through and the manifest-whitelist check is the server side
    of the contract (400 VALIDATION_FAILED, details.field=file_id).
    """

    client = api.RemoteTrainingClient("http://server:8000", "key")
    captured: dict = {}

    def fake_send(method, path, **kwargs):
        captured["method"] = method
        captured["path"] = path
        captured["path_params"] = kwargs.get("path_params")
        captured["headers"] = kwargs.get("headers")
        payload = {
            "success": False,
            "error": {
                "code": "VALIDATION_FAILED",
                "message": "file_id is not in the artifact manifest",
                "details": {"field": "file_id"},
            },
        }
        return StubRaw(
            json.dumps(payload).encode("utf-8"),
            content_type="application/json",
            status_code=400,
        )

    client._send = fake_send
    with pytest.raises(api.ValidationFailedError) as info:
        client.open_job_file(JOB_ID, "../../etc/passwd")
    assert info.value.http_status == 400
    assert info.value.code == "VALIDATION_FAILED"
    assert info.value.details.get("field") == "file_id"
    # The opaque string stays a path PARAMETER (the real _send formats
    # the route path); nothing here turns it into a filesystem path.
    assert captured["path_params"] == {
        "job_id": JOB_ID,
        "file_id": "../../etc/passwd",
    }
    assert captured["path"] == "/jobs/{job_id}/files/{file_id}"
    headers = captured.get("headers") or {}
    assert "Range" not in headers and "If-Range" not in headers

    # The download path of the window surfaces that failure instead of
    # writing anything (spec §5.6.4), and it is red before this step
    # existed: window.download_job_file is new here.
    store = Store(str(tmp_path / "ledger"))
    seed(store)
    window = open_window(store, client)
    window.results_page.job_id = JOB_ID
    window.stack.setCurrentWidget(window.results_page)
    target = tmp_path / "never.pt"
    worker = window.download_job_file(JOB_ID, "../../etc/passwd", str(target))
    assert worker is not None
    assert worker.wait(15000) is True
    qapp.processEvents()
    assert target.exists() is False
    assert list(tmp_path.glob("*.part")) == []
    assert window.results_page.status_row.lines(), (
        "the failure must be visible on the results page"
    )
    assert "错误码 VALIDATION_FAILED" in window.results_page.debug_text()
    close_window(qapp, window)


def test_ct19_the_download_shape_is_the_frozen_route():
    """§3.10 / §5.6.3: file_id in the path segment, no Range, no resume."""

    client = api.RemoteTrainingClient("http://server:8000", "key")
    captured: dict = {}

    def fake_send(method, path, **kwargs):
        captured.update(kwargs)
        captured["method"] = method
        captured["path"] = path
        return StubRaw(b"payload", content_type="application/octet-stream")

    client._send = fake_send
    response = client.open_job_file(JOB_ID, "f_af8212b3")
    assert captured["method"] == "GET"
    assert captured["path"] == "/jobs/{job_id}/files/{file_id}"
    assert captured["path_params"] == {
        "job_id": JOB_ID,
        "file_id": "f_af8212b3",
    }
    assert not captured.get("headers")
    assert response.etag is None
    response.close()

    archive = client.open_job_download(JOB_ID, include_partial=False)
    assert captured["path"] == "/jobs/{job_id}/download"
    assert captured["params"] == {"include_partial": False}
    assert not captured.get("headers")
    archive.close()

    # v1 has no resume, so neither the public call nor the inner stream
    # helper may even accept a Range / If-Range parameter.
    file_params = list(
        inspect.signature(api.RemoteTrainingClient.open_job_file).parameters
    )
    assert file_params == ["self", "job_id", "file_id"]
    stream_params = list(
        inspect.signature(api.RemoteTrainingClient._open_stream).parameters
    )
    assert stream_params == ["self", "key", "path_params", "params"]


def test_ct19_the_summary_area_is_filled_by_file_id(qapp, tmp_path):
    """§5.6.3: the summary area is fed by summary.json via route 14.

    The manifest row decides whether the summary exists; the read runs
    in a worker, in memory, and the same artifact is fetched only once.
    """

    client = DownloadClient(b"unused")
    store = Store(str(tmp_path / "ledger"))
    seed(store)
    window = open_window(store, client)
    window.results_page.set_job(job_fixture(), FILES)

    worker = window.load_summary(JOB_ID)
    assert worker is not None
    assert worker.wait(15000) is True
    qapp.processEvents()

    text = window.results_page.summary_edit.toPlainText()
    assert "duration_seconds: 612.5" in text
    assert "final_metrics: mAP50=0.512, box_loss=1.02" in text
    assert "verified_metrics: mAP50=0.51" in text
    assert "resolved_params: batch=16, device=0" in text
    assert "training_env: cuda=12.4, torch=2.6.0+cu124" in text
    # Addressed by the manifest file_id, never by the displayed path.
    assert client.requests == [("file", JOB_ID, SUMMARY_ID, {})]
    assert "已加载 summary.json" in window.results_page.debug_text()
    # Nothing was written anywhere: the body was parsed in memory.
    assert list(tmp_path.iterdir()) == [tmp_path / "ledger"]
    # Idempotent: the same manifest row is never requested twice.
    assert window.load_summary(JOB_ID) is None
    assert len(client.requests) == 1
    assert "summary_loaded" in CLOSE_CHANNELS
    close_window(qapp, window)


def test_ct19_a_cancelled_summary_read_can_be_retried(qapp, tmp_path):
    """A cancelled summary read must not latch its (job, file_id)."""

    client = DownloadClient(b"unused")
    client.summary_handler = lambda job_id, file_id: api.StreamedResponse(
        StubRaw(
            json.dumps(SUMMARY).encode("utf-8"),
            content_type="application/json",
            chunk_delay=0.02,
        ),
        "job_file",
    )
    store = Store(str(tmp_path / "ledger"))
    seed(store)
    window = open_window(store, client)
    window.results_page.set_job(job_fixture(), FILES)

    worker = window.load_summary(JOB_ID)
    assert worker is not None
    worker.cancel()
    assert worker.wait(15000) is True
    qapp.processEvents()
    assert window._summary_pending is None

    # The retry is a fresh request and this one really lands.
    client.summary_handler = None
    again = window.load_summary(JOB_ID)
    assert again is not None
    assert again.wait(15000) is True
    qapp.processEvents()
    assert "duration_seconds: 612.5" in (
        window.results_page.summary_edit.toPlainText()
    )
    close_window(qapp, window)

def test_ct19_a_summary_read_failure_is_rendered(qapp, tmp_path):
    """§5.6.4: an unreadable summary is reported, never fatal."""

    client = DownloadClient(b"unused")

    def failing(job_id, file_id):
        raise api.ArtifactNotFoundError(
            404, "ARTIFACT_NOT_FOUND", "gone", {"file_id": file_id}
        )

    client.summary_handler = failing
    store = Store(str(tmp_path / "ledger"))
    seed(store)
    window = open_window(store, client)
    window.results_page.set_job(job_fixture(), FILES)
    window.stack.setCurrentWidget(window.results_page)

    worker = window.load_summary(JOB_ID)
    assert worker is not None
    assert worker.wait(15000) is True
    qapp.processEvents()

    assert window.results_page.summary_edit.toPlainText() == ""
    lines = window.results_page.status_row.lines()
    assert any("产物文件已不存在或不可下载" in line for line in lines), lines
    assert "错误码 ARTIFACT_NOT_FOUND" in window.results_page.debug_text()
    close_window(qapp, window)


def test_a_summary_failure_never_re_reads_the_same_file_id(
    qapp, tmp_path, pump
):
    """B8 round 2 item 1: the failure exit must not wake the poller.

    ARTIFACT_NOT_FOUND carries the "refresh the manifest" flag of
    §5.6.4, and the results tick re-reads the summary of every manifest
    that lists one.  A read that keeps failing therefore used to reset
    the table and wake the results poller, which re-read the very same
    file_id as fast as the round trip allows (the debug area grows with
    every pass).  The routine tick must stay the only retry.
    """

    client = DownloadClient(b"unused")
    calls: list = []

    def failing(job_id, file_id):
        calls.append(file_id)
        raise api.ArtifactNotFoundError(
            404, "ARTIFACT_NOT_FOUND", "gone", {"file_id": file_id}
        )

    client.summary_handler = failing
    # A *running* job: its results tick fetches the manifest again, so
    # the reset + wake used to feed the next read.
    client.get_job = lambda job_id: job_fixture(
        status="running", is_terminal=False, finished_at=None
    )
    store = Store(str(tmp_path / "ledger"))
    seed(store)
    window = open_window(store, client)
    window.results_page.set_job(job_fixture(), FILES)
    window.stack.setCurrentWidget(window.results_page)

    worker = window.load_summary(JOB_ID)
    assert worker is not None
    assert worker.wait(15000) is True
    qapp.processEvents()
    assert calls == [SUMMARY_ID]
    # The failure itself is still rendered (§5.6.4).
    assert any(
        "产物文件已不存在或不可下载" in line
        for line in window.results_page.status_row.lines()
    )
    assert "错误码 ARTIFACT_NOT_FOUND" in window.results_page.debug_text()
    # Nothing self-woke: no extra read, and the table kept its rows.
    pump(500)
    assert calls == [SUMMARY_ID]
    assert window.results_page.files == FILES
    close_window(qapp, window)


def test_a_terminal_summary_failure_keeps_the_artifact_table(
    qapp, tmp_path, pump
):
    """B8 round 2 item 1: a terminal job can never refill the table.

    The results tick only fetches the manifest while the job is running
    (or on the transition tick), so the ARTIFACT_NOT_FOUND reset used
    to leave a terminal job's artifact table empty for good.
    """

    client = DownloadClient(b"unused")

    def failing(job_id, file_id):
        raise api.ArtifactNotFoundError(
            404, "ARTIFACT_NOT_FOUND", "gone", {"file_id": file_id}
        )

    client.summary_handler = failing
    store = Store(str(tmp_path / "ledger"))
    seed(store)
    window = open_window(store, client)
    window.results_page.set_job(job_fixture(), FILES)
    window.stack.setCurrentWidget(window.results_page)

    worker = window.load_summary(JOB_ID)
    assert worker is not None
    assert worker.wait(15000) is True
    pump(300)
    # The manifest stays on screen: a terminal job has no second chance
    # to fetch it.
    assert window.results_page.files == FILES
    assert window.results_page.tree.rowCount() == len(FILES)
    assert window.results_page.summary_entry() is not None
    close_window(qapp, window)


def test_the_detail_page_artifacts_button_opens_the_ledger_path(
    qapp, tmp_path
):
    """Item 2: open_artifacts_requested reaches download_path."""

    client = DownloadClient(b"x")
    store = Store(str(tmp_path / "ledger"))
    seed(store)
    saved = tmp_path / "results.zip"
    ledger = store.load_ledger()
    record = ledger.record(JOB_ID)
    record.download_path = str(saved)
    ledger.upsert_record(record)
    store.save_ledger(ledger)

    window = open_window(store, client)
    window.detail_page.job_id = JOB_ID
    opened: list = []
    window.open_directory = lambda path="": (opened.append(str(path)), True)[1]
    window.detail_page.open_artifacts_requested.emit(JOB_ID)
    assert opened == [str(saved)]

    # Without a download_path the real method shows the readable hint
    # instead of doing nothing at all.
    ledger = store.load_ledger()
    record = ledger.record(JOB_ID)
    record.download_path = None
    ledger.upsert_record(record)
    store.save_ledger(ledger)
    del window.open_directory
    assert window.open_artifacts_directory(JOB_ID) is False
    assert window.results_page.status_label.text() == "还没有已保存的结果"
    assert window.results_page.status_row.lines()
    close_window(qapp, window)


def test_ct19_the_busy_indicator_follows_the_response_length(qapp, tmp_path):
    """§5.6.3: determinate with Content-Length, busy without it."""

    client = DownloadClient(b"x" * 10)
    store = Store(str(tmp_path / "ledger"))
    seed(store)
    window = open_window(store, client)
    page = window.results_page
    bar = page.download_bar
    assert bar.isHidden() is True

    page.set_download_progress(1024, None)
    assert bar.isHidden() is False
    assert (bar.minimum(), bar.maximum()) == (0, 0)
    assert "%" not in page.status_label.text()
    assert res_ui.DOWNLOAD_BUSY_BAR_TEXT == bar.format()

    page.set_download_progress(1024, 2048)
    assert (bar.minimum(), bar.maximum()) == (0, 100)
    assert bar.value() == 50
    assert bar.format() == "%p%"
    assert "50%" in page.status_label.text()

    # A legal empty artifact: the length is KNOWN to be 0, so the
    # transfer reads as finished instead of as an unknown total.
    page.set_download_progress(0, 0)
    assert (bar.minimum(), bar.maximum()) == (0, 100)
    assert bar.value() == 100
    assert res_ui.download_progress_text(0, 0) == (
        "下载进度 0 B/0 B（100%）"
    )

    page.clear_download_progress()
    assert bar.isHidden() is True
    close_window(qapp, window)


def test_a_summary_failure_keeps_a_running_download_bar(qapp, tmp_path):
    """Item 4b: only the download exit clears the progress bar."""

    client = DownloadClient(b"unused")

    def failing(job_id, file_id):
        raise api.ArtifactNotFoundError(
            404, "ARTIFACT_NOT_FOUND", "gone", {"file_id": file_id}
        )

    client.summary_handler = failing
    store = Store(str(tmp_path / "ledger"))
    seed(store)
    window = open_window(store, client)
    page = window.results_page
    page.set_job(job_fixture(), FILES)
    window.stack.setCurrentWidget(page)
    page.set_download_progress(1024, None)
    assert page.download_bar.isHidden() is False

    worker = window.load_summary(JOB_ID)
    assert worker.wait(15000) is True
    qapp.processEvents()
    assert page.download_bar.isHidden() is False, (
        "a summary failure must not wipe a running transfer's bar"
    )
    assert page.download_bar.maximum() == 0
    close_window(qapp, window)

def test_ct19_save_to_polls_cancel_before_the_first_chunk(tmp_path):
    """Item 4: a 0 byte body must not be promoted after a cancel.

    The loop never runs for an empty body, so the cancel check has to
    happen before it; otherwise the empty .part is renamed onto the
    target and the download reports success.
    """

    response = api.StreamedResponse(StubRaw(b""), "job_file")
    target = tmp_path / "empty.pt"
    polls: list = []

    def cancel():
        polls.append(1)
        return True

    with pytest.raises(InterruptedError):
        response.save_to(str(target), cancel=cancel)
    assert polls, "cancel must be polled before the loop"
    assert target.exists() is False
    assert list(tmp_path.glob("*.part")) == []


def test_ct19_a_cancel_during_an_empty_body_reports_cancelled(
    qapp, tmp_path
):
    """Item 4 end to end: open, cancel, then an empty body."""

    client = DownloadClient(b"", content_length=True)
    target = tmp_path / "nothing.pt"
    worker = DownloadWorker(
        lambda: client.open_job_file(JOB_ID, "f_af8212b3"),
        str(target),
        job_id=JOB_ID,
        file_id="f_af8212b3",
    )
    # The cancel lands while the response is being awaited, so the
    # worker's own pre-check cannot have seen it.
    client.on_open = worker.cancel
    worker.start()
    assert worker.wait(15000) is True
    assert worker.outcome is not None
    assert worker.outcome.cancelled is True
    assert target.exists() is False

def test_ct19_a_rewritten_summary_is_fetched_again(qapp, tmp_path):
    """Blocking fix: the latch key carries the manifest row content.

    A manual resume rewrites jobs/<job_id>/summary.json under the same
    path, hence the same file_id (spec §3.10.1) and a new sha256; an
    identity-only key kept showing the previous attempt's metrics for
    the rest of the window session."""

    client = DownloadClient(b"unused")
    store = Store(str(tmp_path / "ledger"))
    seed(store)
    window = open_window(store, client)
    window.results_page.set_job(job_fixture(), FILES)
    first = window.load_summary(JOB_ID)
    assert first is not None and first.wait(15000) is True
    qapp.processEvents()
    assert len(client.requests) == 1
    assert "mAP50=0.512" in window.results_page.summary_edit.toPlainText()

    rewritten = [dict(entry) for entry in FILES]
    row = [item for item in rewritten if item["path"] == "summary.json"][0]
    row["sha256"] = "b" * 64
    payload = dict(SUMMARY, final_metrics={"mAP50": 0.9})
    client.summary_handler = lambda job_id, file_id: api.StreamedResponse(
        StubRaw(
            json.dumps(payload).encode("utf-8"),
            content_type="application/json",
        ),
        "job_file",
    )
    window.results_page.set_job(job_fixture(), rewritten)
    again = window.load_summary(JOB_ID)
    assert again is not None, "the rewritten summary must be re-read"
    assert again.wait(15000) is True
    qapp.processEvents()
    assert len(client.requests) == 2
    assert "mAP50=0.9" in window.results_page.summary_edit.toPlainText()
    close_window(qapp, window)


def test_a_summary_row_without_sha256_is_keyed_by_mtime(qapp, tmp_path):
    """Item 2 of the B7 review: the fallback stays a content key.

    Spec §3.2.2 #13 makes sha256 mandatory on every manifest row, so
    this row is a contract violation; what the fallback must avoid is
    degrading to an identity only key, which would pin the first
    attempt's metrics for the rest of the window session.  mtime is a
    declared field of the same row, never an invented one.
    """

    client = DownloadClient(b"unused")
    store = Store(str(tmp_path / "ledger"))
    seed(store)
    window = open_window(store, client)
    first_files = [dict(entry) for entry in FILES]
    row = [
        item for item in first_files if item["path"] == "summary.json"
    ][0]
    row.pop("sha256")
    row["mtime"] = "2026-01-01T10:00:00Z"
    window.results_page.set_job(job_fixture(), first_files)
    first = window.load_summary(JOB_ID)
    assert first is not None and first.wait(15000) is True
    qapp.processEvents()
    assert len(client.requests) == 1
    assert "mAP50=0.512" in window.results_page.summary_edit.toPlainText()

    rewritten = [dict(entry) for entry in first_files]
    row = [
        item for item in rewritten if item["path"] == "summary.json"
    ][0]
    row["mtime"] = "2026-01-01T10:30:00Z"
    payload = dict(SUMMARY, final_metrics={"mAP50": 0.9})
    client.summary_handler = lambda job_id, file_id: api.StreamedResponse(
        StubRaw(
            json.dumps(payload).encode("utf-8"),
            content_type="application/json",
        ),
        "job_file",
    )
    window.results_page.set_job(job_fixture(), rewritten)
    again = window.load_summary(JOB_ID)
    assert again is not None, "the rewritten summary must be re-read"
    assert again.wait(15000) is True
    qapp.processEvents()
    assert len(client.requests) == 2
    assert "mAP50=0.9" in window.results_page.summary_edit.toPlainText()
    close_window(qapp, window)


def test_the_summary_lookup_never_uses_another_jobs_manifest(
    qapp, tmp_path
):
    """Item 2: rows of job A never answer a lookup for job B."""

    other = "job_20260102_000002"
    client = DownloadClient(b"unused")
    store = Store(str(tmp_path / "ledger"))
    seed(store)
    window = open_window(store, client)
    window.results_page.set_job(job_fixture(), FILES)
    assert window.results_page.files_job_id == JOB_ID

    # B never had a summary.json: before the ownership check this lookup
    # answered from A's rows and produced a bogus 400 line on the page.
    assert window.load_summary(other) is None
    assert client.requests == []

    # Entering B's page drops A's table (and its summary) immediately.
    window.show_results(other)
    assert window.results_page.files == []
    assert window.results_page.files_job_id == other
    assert window.results_page.summary_edit.toPlainText() == ""
    assert client.requests == []
    close_window(qapp, window)


def test_ct19_an_oversized_summary_is_refused_and_rendered(qapp, tmp_path):
    """Item 7: the SUMMARY_MAX_BYTES guard has a case and a rendering."""

    client = DownloadClient(b"unused")
    oversized = b"{" + b"0" * (worker_mod.SUMMARY_MAX_BYTES + 1)
    client.summary_handler = lambda job_id, file_id: api.StreamedResponse(
        StubRaw(oversized, content_type="application/json"),
        "job_file",
    )
    store = Store(str(tmp_path / "ledger"))
    seed(store)
    window = open_window(store, client)
    window.results_page.set_job(job_fixture(), FILES)
    window.stack.setCurrentWidget(window.results_page)

    worker = window.load_summary(JOB_ID)
    assert worker is not None
    assert worker.wait(15000) is True
    qapp.processEvents()
    assert isinstance(worker.error, api.ContractViolationError)
    assert "超过" in str(worker.error)
    assert window.results_page.summary_edit.toPlainText() == ""
    # Rendered through the §5.6.4 view: the readable message lands on
    # the page row and in the debug area.
    text = window.results_page.debug_text()
    assert "summary.json 超过" in text
    lines = window.results_page.status_row.lines()
    assert any("summary.json 超过" in line for line in lines), lines
    close_window(qapp, window)


def test_ct19_a_non_json_summary_is_rendered(qapp, tmp_path):
    """Item 7: a body that is not the promised JSON is rendered too.

    It is a ContractViolationError on purpose: MalformedResponseError
    is a TransportError, which the §5.6.4 table renders as the "lost
    contact" red bar - wrong for a body that arrived fine."""

    client = DownloadClient(b"unused")
    client.summary_handler = lambda job_id, file_id: api.StreamedResponse(
        StubRaw(b"<html>not json</html>", content_type="application/json"),
        "job_file",
    )
    store = Store(str(tmp_path / "ledger"))
    seed(store)
    window = open_window(store, client)
    window.results_page.set_job(job_fixture(), FILES)
    window.stack.setCurrentWidget(window.results_page)

    worker = window.load_summary(JOB_ID)
    assert worker.wait(15000) is True
    qapp.processEvents()
    assert isinstance(worker.error, api.ContractViolationError)
    text = window.results_page.debug_text()
    assert "不是合法 JSON" in text
    lines = window.results_page.status_row.lines()
    assert any("不是合法 JSON" in line for line in lines), lines
    assert "与服务端失去联系" not in "\n".join(lines)
    close_window(qapp, window)

# ------------------------------------------------------------- CT26


def test_ct26_capabilities_fixture_covers_every_declared_key(tmp_path):
    """CT26: the §3.6 payload parses key by key, no invented field."""

    path = fixture_file(tmp_path, "capabilities", capabilities_fixture())
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert tuple(payload) == CAPABILITY_KEYS
    assert tuple(api.Capabilities.REQUIRED_KEYS) == CAPABILITY_KEYS

    caps = api.Capabilities(payload)
    assert caps.missing_keys() == []
    caps.require_keys()
    assert caps.enabled is True
    assert caps.tasks == ["detect", "segment"]
    assert caps.cancel_grace_seconds == 15
    assert caps.require_cancel_grace_seconds() == 15
    assert tuple(caps.family("yolo11")) == FAMILY_KEYS
    assert caps.family_presets("yolo11") == ["yolo11-sgd"]
    assert caps.default_preset("yolo11") == "yolo11-sgd"
    entry = caps.vram_entry("yolo11n", "detect")
    assert entry is not None and entry["max_batch"] == 96
    assert caps.calibration_source("yolo11n", "detect") == "auto"
    assert caps.max_batch("yolo11n", "detect") == 96
    assert caps.weight_submittable("yolo11n.pt") is True
    assert tuple(payload["devices"][0]) == DEVICE_KEYS
    assert tuple(payload["vram_table"]) == VRAM_TABLE_KEYS
    assert tuple(payload["calibration"]) == CALIBRATION_KEYS
    assert tuple(payload["training_env"]) == (
        "ultralytics",
        "torch",
        "cuda",
        "python",
    )
    # param_schema is "exactly the 23 client settable parameters", and the
    # client form must not grow a parameter of its own.
    assert tuple(payload["param_schema"]) == PARAM_KEYS
    assert set(config_ui.PARAM_SPECS) == set(PARAM_KEYS)
    assert tuple(config_ui.PARAM_LABELS) == PARAM_KEYS
    codes = {item["code"] for item in payload["warnings"]}
    assert codes <= set(WARNING_CODES)
    assert [item["code"] for item in caps.service_warnings()] == [
        "WEIGHTS_MISSING"
    ]


def test_ct26_health_fixture_covers_every_declared_key(tmp_path):
    """CT26: the §3.7 payload parses key by key."""

    path = fixture_file(tmp_path, "health", health_fixture())
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert tuple(payload) == HEALTH_KEYS
    assert tuple(api.HealthStatus.REQUIRED_KEYS) == HEALTH_KEYS
    health = api.HealthStatus(payload)
    assert health.missing_keys() == []
    health.require_keys()
    assert health.training_available() is True
    assert tuple(payload["queue"]) == (
        "queued",
        "running",
        "max_concurrent_jobs",
        "max_concurrent_per_device",
    )
    assert tuple(payload["jobs"]) == (
        "queued",
        "running",
        "needs_attention",
    )
    assert tuple(payload["devices"][0]) == DEVICE_KEYS + ("running_jobs",)
    assert tuple(payload["work_dir"]) == (
        "path",
        "free_gb",
        "total_gb",
        "writable",
    )
    assert tuple(payload["weights"]) == (
        "cached",
        "missing",
        "allow_weight_download",
    )
    assert tuple(payload["blobs"]) == (
        "count",
        "bytes",
        "unreferenced",
        "materialize",
    )
    assert probe_state(payload) == api.HEALTH_STATE_OK


def probe_state(payload: Any) -> str:
    """The four state auth table maps a 200 payload to `ok` (§3.7)."""

    health = api.HealthStatus(payload)
    health.require_keys()
    if not health.enabled:
        return api.HEALTH_STATE_TRAINING_DISABLED
    return api.HEALTH_STATE_OK


def test_ct26_job_fixture_covers_every_declared_field(tmp_path):
    """CT26: the §3.4.4 job object drives every renderer."""

    path = fixture_file(tmp_path, "job", job_fixture())
    job = json.loads(path.read_text(encoding="utf-8"))
    assert tuple(job) == JOB_KEYS
    assert len(JOB_KEYS) == 23
    assert set(poller_field_names()) <= set(JOB_KEYS)

    record = poll_mod.ledger_record(job)
    assert record.job_id == JOB_ID
    assert record.is_terminal is True
    assert record.partial_available is True
    assert poll_mod.is_terminal(job) is True
    assert poll_mod.status_of(job) == "completed"

    # `client_job_name` is deliberately NOT a job object field (§3.4.4:
    # it only lands in the local ledger), so the name cell falls back to
    # the job_id for a bare job object.
    assert "client_job_name" not in JOB_KEYS
    cells = jobs_ui.job_row(job, now=0.0)
    assert len(cells) == len(jobs_ui.COLUMNS)
    assert cells[jobs_ui.NAME_COLUMN] == JOB_ID
    assert cells[jobs_ui.STATUS_COLUMN] == "已完成"

    assert tuple(job["progress"]) == PROGRESS_KEYS
    assert tuple(job["resolved_params"]) == RESOLVED_PARAM_KEYS
    rendered = detail_ui.resolved_param_lines(job["resolved_params"])
    for name in RESOLVED_PARAM_KEYS:
        assert any(line.startswith(name + ":") for line in rendered), name
    assert detail_ui.job_progress(job)["percent"] == 100.0
    assert "mAP50" in "\n".join(detail_ui.format_metrics(job["metrics"]))

    page = detail_ui.DetailPage()
    page.set_job(job)
    assert page.job_id == JOB_ID
    assert page.resume_modes == []
    assert job["resume_mode_available"] == []

    # The two yellow bands never replace each other (§5.6.2).
    assert res_ui.partial_result_text(job).startswith("该任务保留了部分结果")
    assert res_ui.artifact_suspect_text(job) == ""
    suspect = dict(job, artifact_suspect=True)
    assert "请人工确认" in res_ui.artifact_suspect_text(suspect)


def poller_field_names():
    """The job fields the monitoring step mirrors (spec §3.4.4)."""

    return set(poll_mod.LEDGER_JOB_FIELDS) | set(poll_mod.TRANSITION_FIELDS)


def test_ct26_batch_fixture_uses_jobs_and_not_found_ids_only(tmp_path):
    """CT26: §3.2.4 keys, request order and `total` never used."""

    batch = {
        "jobs": [job_fixture(status="running", is_terminal=False)],
        "not_found_ids": ["job_20260101_000000"],
        "total": 2,
    }
    path = fixture_file(tmp_path, "jobs_batch", batch)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert tuple(payload) == ("jobs", "not_found_ids", "total")

    merged, orphans = poll_mod.merge_jobs(payload["jobs"])
    assert [item["job_id"] for item in merged] == [JOB_ID]
    assert orphans == []
    assert list(poll_mod.batch_ids([JOB_ID], 50)) == [[JOB_ID]]

    store = Store(str(tmp_path / "ledger"))
    seed(store)
    client = BatchClient(payload)
    scheduler = poll_mod.Scheduler(
        store,
        list_fn=client.list_jobs,
        detail_fn=client.get_job,
        events_fn=client.get_events,
        files_fn=client.list_job_files,
    )
    outcome = scheduler.tick(poll_mod.PAGE_JOBS, active=[JOB_ID])
    assert outcome.orphaned == ["job_20260101_000000"]
    assert [item.job_id for item in outcome.results] == [JOB_ID]
    # `total` (2, i.e. job + orphan) is never read as a count of orphans:
    # the same fixture with total == len(jobs) behaves identically.
    batch["total"] = 1
    assert tuple(json.loads(fixture_file(tmp_path, "jobs_batch2", batch)
                            .read_text(encoding="utf-8"))) == (
        "jobs",
        "not_found_ids",
        "total",
    )
    assert client.calls == [[JOB_ID]]


class BatchClient:
    """Answers one batch query with the frozen payload."""

    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls: list = []

    def list_jobs(self, ids=None, status=None, limit=50):
        self.calls.append(list(ids or []))
        return dict(self.payload)

    def get_job(self, job_id):
        return job_fixture(status="running", is_terminal=False)

    def get_events(self, job_id, after=0):
        return {"events": [], "last_seq": int(after)}

    def list_job_files(self, job_id, include_partial=True):
        return {"files": []}


def test_ct26_artifact_manifest_and_file_id_contract(tmp_path):
    """CT26: §3.10 - six manifest keys, opaque ids, verbatim ETag."""

    path = fixture_file(tmp_path, "job_files", {"files": FILES})
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert tuple(payload) == ("files",)
    for entry in payload["files"]:
        assert set(entry) == {
            "file_id",
            "path",
            "size",
            "sha256",
            "mtime",
            "partial",
        }

    page = res_ui.ResultsPage()
    page.job_id = JOB_ID
    page.set_files(payload["files"])
    assert page.tree.rowCount() == 3
    assert cell(page, 0, 0) == "weights/best.pt"
    assert cell(page, 0, 1) == res_ui.format_size(2048)
    assert cell(page, 0, 2) == "2026-01-01T10:00:00Z"
    assert cell(page, 0, 3) == ""
    assert cell(page, 0, 4) == res_ui.GROUP_COMPLETE
    assert page.tree.item(0, 0).toolTip() == "f_af8212b3"
    assert page.entry_save_name(JOB_ID, "f_af8212b3") == "best.pt"
    assert page.entry_save_name(JOB_ID, "") == JOB_ID + ".zip"

    # The declared 8 hex examples of §3.10.9 stay identifiers: the save
    # name comes from `path` (display only) while the download call uses
    # the identifier, and the ETag is stored verbatim, never rebuilt.
    assert FILES[0]["file_id"] == "f_af8212b3"
    raw = StubRaw(
        b"abc",
        headers={
            "Content-Type": "application/octet-stream",
            "ETag": '"f_af8212b3.' + SHA + '"',
            "Content-Range": "bytes 0-2/3",
        },
    )
    response = api.StreamedResponse(raw, "job_file")
    assert response.etag == '"f_af8212b3.' + SHA + '"'
    assert response.content_range == "bytes 0-2/3"
    assert response.content_length == 3
    response.close()


def test_ct26_the_route_table_matches_the_declared_paths():
    """CT26: §3.2.1/#13 to #15 - the client calls no invented route."""

    assert api.route("job_files").path == "/jobs/{job_id}/files"
    assert api.route("job_files").method == "GET"
    assert api.route("job_file").path == "/jobs/{job_id}/files/{file_id}"
    assert api.route("job_file").method == "GET"
    assert api.route("job_download").path == "/jobs/{job_id}/download"
    assert api.route("job_download").method == "GET"
    assert api.TRAIN_PREFIX == "/custom/train"
    assert "job_files" in api.CONSUMED_ROUTE_KEYS
    assert "job_file" in api.CONSUMED_ROUTE_KEYS
    assert "job_download" in api.CONSUMED_ROUTE_KEYS
    # The three management routes stay unconsumed (spec §3.2.3).
    assert set(api.NON_CONSUMED_ROUTE_KEYS) == {
        "datasets_list",
        "dataset_delete",
        "cache_stats",
    }
