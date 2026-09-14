"""Full chain offline drill of the client track (B8 / checkpoint CK4).

The B8 criterion is one continuous walk of the whole client chain -
scan, split, pack, plan, upload, submit, poll, events, cancel, resume
and download - against a *fake server* and an offline fixture dataset.
Nothing here needs a real training server and nothing is written into
the repository: the dataset, the ledger, the staging area and the
download targets all live in tmp_path.

The fake server is a real loopback HTTP server (http.server), not
another dict backed stub: the drill therefore also covers the transport
layer - the streaming multipart upload body, the streamed download body
and the JSON envelope decoding - which is where a stub would be blind.
The four UI pages are driven through the production window, so the
chain under test is the one a user actually runs.

Two tests: the first walks scan -> split -> pack -> plan -> upload ->
submit and checks the bytes that reached the server, the second
continues with poll -> events -> cancel -> resume -> download.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import os.path as osp
import re
import threading
import time
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

import pytest
from PyQt6 import QtCore, QtWidgets

from anylabeling.custom.remote_training import api_client as api
from anylabeling.custom.remote_training.api_client import (
    RemoteTrainingClient,
)
from anylabeling.custom.remote_training import store as store_mod
from anylabeling.custom.remote_training.pipeline import Pipeline
from anylabeling.custom.remote_training.store import ServerConfig, Store
from anylabeling.custom.remote_training.ui.dialog import (
    RemoteTrainingDialog,
)

JOB_ID = "job_20260101_abc123"
DATASET_ID = "ds_20260101_abc123"
TOKEN = "ut_" + "0" * 32
CLASSES = ("person", "car")
#: stem -> the single class of its one rectangle (spec 5.2.2 layout).
IMAGE_CLASSES = {
    "0001": "person",
    "0002": "person",
    "0003": "car",
    "0004": "car",
}
EXPECTED_IMAGES = ["{0}.jpg".format(stem) for stem in sorted(IMAGE_CLASSES)]
MTIME = "2026-01-01T11:00:00Z"


# --------------------------------------------------------------------
# Offline fixture (tmp_path only, never the repository)
# --------------------------------------------------------------------


def make_dataset(root: str) -> str:
    """A four image / two class root dataset (strictly read only)."""

    os.makedirs(root)
    with open(osp.join(root, "classes.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(CLASSES) + "\n")
    for stem, label in IMAGE_CLASSES.items():
        with open(osp.join(root, stem + ".jpg"), "wb") as fh:
            fh.write(b"image-bytes-" + stem.encode("ascii"))
        payload = {
            "version": "1.0",
            "imageWidth": 10,
            "imageHeight": 10,
            "shapes": [
                {
                    "label": label,
                    "shape_type": "rectangle",
                    "points": [[0, 0], [1, 1]],
                }
            ],
        }
        with open(
            osp.join(root, stem + ".json"), "w", encoding="utf-8"
        ) as fh:
            json.dump(payload, fh)
    return root


def tree_fingerprint(root: str) -> List[Tuple[str, int, int, str]]:
    """(relative path, size, mtime_ns, sha256) of every file below root."""

    entries: List[Tuple[str, int, int, str]] = []
    for current, dirs, names in os.walk(root):
        dirs.sort()
        for name in sorted(names):
            path = osp.join(current, name)
            stat = os.lstat(path)
            with open(path, "rb") as handle:
                digest = hashlib.sha256(handle.read()).hexdigest()
            entries.append(
                (
                    osp.relpath(path, root),
                    stat.st_size,
                    stat.st_mtime_ns,
                    digest,
                )
            )
    return entries


def file_id_for(path: str) -> str:
    """The 3.10.1 short id: f_ + the first 8 hex of sha256(path)."""

    return "f_" + hashlib.sha256(path.encode("utf-8")).hexdigest()[:8]


# --------------------------------------------------------------------
# The fake server
# --------------------------------------------------------------------


def _read_body(handler: BaseHTTPRequestHandler) -> bytes:
    """The whole request body, Content-Length or chunked."""

    length = handler.headers.get("Content-Length")
    if length is not None:
        return handler.rfile.read(int(length))
    encoding = (handler.headers.get("Transfer-Encoding") or "").lower()
    if "chunked" not in encoding:
        return b""
    chunks: List[bytes] = []
    while True:
        line = handler.rfile.readline().strip()
        size = int(line.split(b";")[0], 16)
        if size == 0:
            handler.rfile.readline()
            break
        chunks.append(handler.rfile.read(size))
        handler.rfile.read(2)
    return b"".join(chunks)


def _send_json(
    handler: BaseHTTPRequestHandler, status: int, payload: Any
) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.wfile.write(body)
    handler.close_connection = True


def _send_bytes(
    handler: BaseHTTPRequestHandler,
    status: int,
    body: bytes,
    content_type: str,
    extra: Optional[Dict[str, str]] = None,
) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Connection", "close")
    for name, value in (extra or {}).items():
        handler.send_header(name, value)
    handler.end_headers()
    handler.wfile.write(body)
    handler.close_connection = True


def _ok(handler: BaseHTTPRequestHandler, data: Any) -> None:
    _send_json(handler, 200, {"success": True, "data": data})


def _error(
    handler: BaseHTTPRequestHandler,
    status: int,
    code: str,
    message: str,
) -> None:
    _send_json(
        handler,
        status,
        {
            "success": False,
            "error": {"code": code, "message": message, "details": {}},
        },
    )


def _multipart_parts(body: bytes, boundary: bytes) -> Dict[str, bytes]:
    """Split one multipart body into name -> payload (drill helper)."""

    delimiter = b"--" + boundary
    parts: Dict[str, bytes] = {}
    for chunk in body.split(delimiter):
        if not chunk or chunk.startswith(b"--"):
            continue
        head, separator, payload = chunk.partition(b"\r\n\r\n")
        if not separator:
            continue
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]
        match = re.search(rb'name="([^"]+)"', head)
        if match:
            parts[match.group(1).decode("ascii")] = payload
    return parts


def _handler_class(owner: Any) -> type:
    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:
            """Keep the pytest output clean."""

        def do_GET(self) -> None:  # noqa: N802
            owner.dispatch(self, "GET")

        def do_POST(self) -> None:  # noqa: N802
            owner.dispatch(self, "POST")

    return _Handler


class FakeTrainServer:
    """A loopback fake of the consumed routes (spec 3.2.3).

    Every call is counted and every body is kept, so the tests assert
    what the client actually sent instead of only that it did not
    raise.  The job state is advanced by the test through
    set_running / set_terminal / push_event, which keeps the drill
    deterministic.
    """

    def __init__(self) -> None:
        self.plan_calls = 0
        self.upload_calls = 0
        self.submit_calls = 0
        self.list_calls = 0
        self.detail_calls = 0
        self.event_calls = 0
        self.files_calls = 0
        self.cancel_calls = 0
        self.resume_calls = 0
        self.file_calls = 0
        self.download_calls = 0
        self.plan_body: Optional[Dict[str, Any]] = None
        self.submitted_body: Optional[Dict[str, Any]] = None
        self.received_token = ""
        self.received_archive = b""
        self.uploaded_names: List[str] = []
        self.resume_mode = ""
        self.job_id = JOB_ID
        self.job: Dict[str, Any] = self._job()
        self.events: List[Dict[str, Any]] = []
        self.files: List[Dict[str, Any]] = []
        self.artifacts: Dict[str, bytes] = {}
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), _handler_class(self)
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()

    # -- lifecycle ---------------------------------------------------

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return "http://{0}:{1}".format(host, port)

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    # -- fixtures ----------------------------------------------------

    def _job(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": "queued",
            "is_terminal": False,
            "attempt": 1,
            "max_attempts": 3,
            "resume_cycles": 0,
            "needs_attention": False,
            "needs_attention_reason": None,
            "artifact_suspect": False,
            "partial_available": False,
            "resume_mode_available": ["resume"],
            "progress": {"epoch": 0, "total_epochs": 10, "percent": 0.0},
            "created_at": "2026-01-01T10:00:00Z",
            "finished_at": None,
        }

    def set_running(self, percent: float, epoch: int) -> None:
        self.job.update(
            {
                "status": "running",
                "is_terminal": False,
                "finished_at": None,
                "progress": {
                    "epoch": epoch,
                    "total_epochs": 10,
                    "percent": float(percent),
                },
            }
        )

    def set_terminal(self, status: str = "completed") -> None:
        self.job.update(
            {
                "status": status,
                "is_terminal": True,
                "finished_at": "2026-01-01T11:05:00Z",
            }
        )

    def push_event(self, etype: str, data: Dict[str, Any]) -> int:
        seq = len(self.events) + 1
        self.events.append(
            {
                "seq": seq,
                "ts": "2026-01-01T10:00:00Z",
                "type": etype,
                "data": data,
            }
        )
        return seq

    def publish_artifacts(self) -> Dict[str, Any]:
        """The artifact manifest of spec 3.2.2 #13 plus its content."""

        summary = {
            "job_id": self.job_id,
            "status": "completed",
            "attempt": 1,
            "resume_cycles": 1,
            "duration_seconds": 42.0,
            "final_metrics": {"mAP50": 0.87},
            "verified_metrics": {"precision": 0.91},
        }
        rows = [
            (
                "summary.json",
                False,
                json.dumps(summary).encode("utf-8"),
            ),
            ("weights/best.pt", False, b"best-weights-" * 64),
            ("partial/weights/last.pt", True, b"last-partial-" * 16),
        ]
        self.files = []
        self.artifacts = {}
        for path, partial, payload in rows:
            file_id = file_id_for(path)
            self.files.append(
                {
                    "file_id": file_id,
                    "path": path,
                    "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "mtime": MTIME,
                    "partial": partial,
                }
            )
            self.artifacts[file_id] = payload
        self.job["partial_available"] = True
        return summary

    def artifact_ids(self) -> Dict[str, str]:
        return {row["path"]: row["file_id"] for row in self.files}

    # -- routing -----------------------------------------------------

    def dispatch(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        parsed = urllib.parse.urlparse(handler.path)
        query = urllib.parse.parse_qs(parsed.query)
        if not parsed.path.startswith(api.TRAIN_PREFIX):
            _error(handler, 404, "VALIDATION_FAILED", "unknown path")
            return
        parts = parsed.path[len(api.TRAIN_PREFIX):].strip("/").split("/")
        try:
            self._route(handler, method, parts, query)
        except Exception as exc:  # pragma: no cover - drill diagnostics
            _error(handler, 500, "INTERNAL_ERROR", str(exc))

    def _route(
        self,
        handler: BaseHTTPRequestHandler,
        method: str,
        parts: List[str],
        query: Dict[str, List[str]],
    ) -> None:
        if method == "POST" and parts == ["datasets", "plan"]:
            return self._plan(handler)
        if method == "POST" and parts == ["datasets", "upload"]:
            return self._upload(handler)
        if method == "POST" and parts == ["jobs"]:
            return self._submit(handler)
        if method == "GET" and parts == ["jobs"]:
            return self._list_jobs(handler, query)
        if len(parts) >= 2 and parts[0] == "jobs":
            job_id, rest = parts[1], parts[2:]
            if job_id != self.job_id:
                return _error(handler, 404, "JOB_NOT_FOUND", "no such job")
            if method == "GET" and not rest:
                return self._detail(handler)
            if method == "GET" and rest == ["events"]:
                return self._events(handler, query)
            if method == "POST" and rest == ["cancel"]:
                return self._cancel(handler)
            if method == "POST" and rest == ["resume"]:
                return self._resume(handler)
            if method == "GET" and rest == ["files"]:
                return self._files(handler, query)
            if method == "GET" and rest == ["download"]:
                return self._download(handler)
            if method == "GET" and len(rest) == 2 and rest[0] == "files":
                return self._file(handler, rest[1])
        _error(handler, 404, "VALIDATION_FAILED", "unrouted request")

    # -- the routes --------------------------------------------------

    def _plan(self, handler: BaseHTTPRequestHandler) -> None:
        manifest = json.loads(_read_body(handler).decode("utf-8"))
        self.plan_calls += 1
        self.plan_body = manifest
        missing = [
            {
                "name": item["name"],
                "split": item["split"],
                "sha256": item["sha256"],
                "size": item["size"],
            }
            for item in manifest.get("images") or ()
        ]
        _ok(
            handler,
            {
                "upload_token": TOKEN,
                "expires_at": "2099-01-01T00:00:00Z",
                "total_images": len(manifest.get("images") or ()),
                "blob_hits": 0,
                "missing_images": missing,
                "upload_bytes": sum(item["size"] for item in missing),
                "rejected": [],
                "warnings": [],
            },
        )

    def _upload(self, handler: BaseHTTPRequestHandler) -> None:
        body = _read_body(handler)
        content_type = handler.headers.get("Content-Type") or ""
        boundary = content_type.split("boundary=", 1)[1].encode("ascii")
        parts = _multipart_parts(body, boundary)
        self.upload_calls += 1
        self.received_token = parts.get("upload_token", b"").decode("utf-8")
        self.received_archive = parts.get("archive", b"")
        with zipfile.ZipFile(io.BytesIO(self.received_archive)) as archive:
            self.uploaded_names = sorted(archive.namelist())
        _ok(
            handler,
            {
                "dataset_id": DATASET_ID,
                "counts": {"uploaded": len(self.uploaded_names)},
                "blob": {},
                "warnings": [],
            },
        )

    def _submit(self, handler: BaseHTTPRequestHandler) -> None:
        self.submit_calls += 1
        self.submitted_body = json.loads(
            _read_body(handler).decode("utf-8")
        )
        self.set_running(20.0, 2)
        _ok(
            handler,
            {"job_id": self.job_id, "status": "running", "warnings": []},
        )

    def _list_jobs(
        self,
        handler: BaseHTTPRequestHandler,
        query: Dict[str, List[str]],
    ) -> None:
        self.list_calls += 1
        wanted = [
            item
            for item in (query.get("ids", [""])[0] or "").split(",")
            if item
        ]
        jobs = [dict(self.job)] if self.job_id in wanted else []
        missing = [item for item in wanted if item != self.job_id]
        _ok(handler, {"jobs": jobs, "not_found_ids": missing})

    def _detail(self, handler: BaseHTTPRequestHandler) -> None:
        self.detail_calls += 1
        _ok(handler, dict(self.job))

    def _events(
        self,
        handler: BaseHTTPRequestHandler,
        query: Dict[str, List[str]],
    ) -> None:
        self.event_calls += 1
        after = int((query.get("after") or ["0"])[0])
        fresh = [item for item in self.events if item["seq"] > after]
        last_seq = max([after] + [item["seq"] for item in self.events])
        _ok(handler, {"events": fresh, "last_seq": last_seq})

    def _files(
        self,
        handler: BaseHTTPRequestHandler,
        query: Dict[str, List[str]],
    ) -> None:
        self.files_calls += 1
        include_partial = (query.get("include_partial") or ["true"])[0]
        entries = [
            dict(row)
            for row in self.files
            if include_partial.lower() != "false" or not row["partial"]
        ]
        _ok(
            handler,
            {
                "job_id": self.job_id,
                "status": self.job["status"],
                "partial_available": self.job["partial_available"],
                "files": entries,
            },
        )

    def _file(self, handler: BaseHTTPRequestHandler, file_id: str) -> None:
        self.file_calls += 1
        payload = self.artifacts.get(file_id)
        if payload is None:
            return _error(
                handler,
                400,
                "VALIDATION_FAILED",
                "file_id not in the manifest",
            )
        digest = hashlib.sha256(payload).hexdigest()
        _send_bytes(
            handler,
            200,
            payload,
            "application/octet-stream",
            {"ETag": '"{0}.{1}"'.format(file_id, digest)},
        )

    def _download(self, handler: BaseHTTPRequestHandler) -> None:
        self.download_calls += 1
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for row in self.files:
                archive.writestr(row["path"], self.artifacts[row["file_id"]])
        _send_bytes(handler, 200, buffer.getvalue(), "application/zip")

    def _cancel(self, handler: BaseHTTPRequestHandler) -> None:
        self.cancel_calls += 1
        self.set_terminal("cancelled")
        self.job["partial_available"] = True
        self.push_event("log", {"message": "取消请求已受理"})
        _ok(
            handler,
            {
                "job_id": self.job_id,
                "status": "cancelled",
                "partial_available": True,
            },
        )

    def _resume(self, handler: BaseHTTPRequestHandler) -> None:
        self.resume_calls += 1
        payload = json.loads(_read_body(handler).decode("utf-8") or "{}")
        self.resume_mode = str(payload.get("mode") or "")
        cycles = int(self.job["resume_cycles"]) + 1
        self.job.update(
            {
                "status": "running",
                "is_terminal": False,
                "finished_at": None,
                "attempt": 1,
                "resume_cycles": cycles,
                "needs_attention": False,
            }
        )
        self.push_event(
            "manual_resume",
            {
                "attempt": 1,
                "mode": self.resume_mode,
                "resume_cycles": cycles,
            },
        )
        _ok(
            handler,
            {
                "job_id": self.job_id,
                "status": "running",
                "mode": self.resume_mode,
                "attempt": 1,
                "resume_cycles": cycles,
            },
        )


@pytest.fixture
def server():
    fake = FakeTrainServer()
    yield fake
    fake.stop()


# --------------------------------------------------------------------
# Window helpers
# --------------------------------------------------------------------


def pump(qapp: Any, milliseconds: int = 300) -> None:
    timer = QtCore.QElapsedTimer()
    timer.start()
    while timer.elapsed() < milliseconds:
        qapp.processEvents()
        QtCore.QThread.msleep(5)


def open_window(
    qapp: Any,
    store: Store,
    client: Any,
    staging_parent: Any,
    staging_scan_root: Any,
) -> Tuple[QtWidgets.QMainWindow, RemoteTrainingDialog]:
    """One window whose staging *and* start-up scan stay inside tmp_path.

    Without staging_temp_root the dialog's start-up pass
    (reclaim_staging_leftovers) would walk the real system temp
    directory: product behaviour, but not hermetic enough for a drill
    that claims to touch tmp_path only.
    """

    parent = QtWidgets.QMainWindow()
    window = RemoteTrainingDialog(
        parent,
        store=store,
        staging_temp_root=str(staging_scan_root),
        pipeline_factory=lambda **kwargs: Pipeline(
            staging_parent=str(staging_parent), **kwargs
        ),
    )
    parent._remote_training_dialog = window
    window._confirm_close = lambda _dialog: True
    window._confirm_action = lambda _title, _text: True
    window.client = client
    window.show()
    return parent, window


def precheck_and_submit(
    qapp: Any, window: RemoteTrainingDialog, dataset: str, base_url: str
) -> Tuple[Any, Any]:
    """The production pre-check + submit path, one worker each."""

    window.show_config()
    window.config_page.set_values(
        {
            "server_url": base_url,
            "dataset_dir": dataset,
            "classes_file": osp.join(dataset, "classes.txt"),
            "task": "Detect",
            "model": "yolo11n.pt",
            "val_ratio": 0.5,
            "seed": 7,
        }
    )
    window.server_config = ServerConfig(server_url=base_url)
    precheck = window.start_precheck()
    assert precheck is not None
    assert precheck.wait(30000) is True
    qapp.processEvents()
    run = window.prepared_run
    assert run is not None
    window.config_page.submit_button.click()
    worker = window.submit_worker
    assert worker is not None
    assert worker.wait(60000) is True
    qapp.processEvents()
    return run, worker.outcome


def close_window(window: RemoteTrainingDialog, qapp: Any) -> None:
    try:
        window.stop_polling()
        window.cancel_workers()
        window.reject()
    except RuntimeError:  # pragma: no cover - already destroyed
        pass
    qapp.processEvents()


# --------------------------------------------------------------------
# 1) scan -> split -> pack -> plan -> upload -> submit
# --------------------------------------------------------------------


def test_b8_chain_scans_splits_packs_and_submits(qapp, tmp_path, server):
    """The local pipeline and the three upload-stage routes, for real."""

    dataset = make_dataset(str(tmp_path / "dataset"))
    before = tree_fingerprint(dataset)
    store = Store(str(tmp_path / "ledger"))
    client = RemoteTrainingClient(server.base_url, "secret-token")
    # A fresh staging area inside the injected scan root: the start-up
    # pass reports exactly the roots it walked, so its presence in
    # kept_unexpired proves the walk never left tmp_path.
    scan_root = tmp_path / "staging_scan"
    scan_root.mkdir()
    decoy = scan_root / (store_mod.STAGING_PREFIX + "probe")
    decoy.mkdir()
    (decoy / "payload.bin").write_bytes(b"x" * 16)
    store_mod.write_owner_marker(
        str(decoy),
        pid=os.getpid(),
        started_at=time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        ),
    )
    _parent, window = open_window(
        qapp,
        store,
        client,
        tmp_path / "staging",
        scan_root,
    )
    try:
        assert str(decoy) in window.staging_reclaim.kept_unexpired
        run, outcome = precheck_and_submit(
            qapp, window, dataset, server.base_url
        )
        pipeline = window.pipeline

        # 1/2) scan + split: only the val side is materialised (every
        # other image goes to train, spec 5.2.7 step 3), and every class
        # keeps a representative on both sides.
        assert run.files == EXPECTED_IMAGES
        assert run.seed == 7
        assert run.assignments == {
            "0001.jpg": "val",
            "0004.jpg": "val",
        }
        assert [
            name for name in run.files if name not in run.assignments
        ] == ["0002.jpg", "0003.jpg"]
        assert run.split_check().blocked is False
        assert run.split_stats == {
            "person": {"train": 1, "val": 1},
            "car": {"train": 1, "val": 1},
        }
        assert run.val_ratio == 0.5
        # The staging work area holds the frozen labels of every image.
        assert sorted(
            os.listdir(osp.join(str(run.staging_dir), "labels"))
        ) == ["{0}.txt".format(stem) for stem in sorted(IMAGE_CLASSES)]
        # The dataset root is byte for byte, mtime for mtime unchanged.
        assert tree_fingerprint(dataset) == before

        # 3/4) pack + plan: the manifest is the contract shape of
        # 3.2.2 #13 and the body the server got is exactly that object.
        assert server.plan_calls == 1
        manifest = server.plan_body or {}
        assert manifest["task"] == "detect"
        assert manifest["val_ratio"] == 0.5
        assert manifest["classes"] == list(CLASSES)
        assert [
            item["name"] for item in manifest["images"]
        ] == EXPECTED_IMAGES
        for entry in manifest["images"]:
            assert len(entry["sha256"]) == 64
            assert len(entry["label_sha256"]) == 64
            assert entry["split"] in ("train", "val")
        assert manifest == run.manifest(pipeline.labels)

        # 5) upload: the streaming multipart body really arrived, under
        # the latched token, and the archive is the frozen pack.
        assert outcome.ok is True, outcome.error_message
        assert server.upload_calls == 1
        assert server.received_token == TOKEN
        expected_names = (
            ["manifest.json"]
            + [
                "images/{0}/{1}".format(run.split_of(name), name)
                for name in EXPECTED_IMAGES
            ]
            + [
                "labels/{0}/{1}.txt".format(
                    run.split_of(name), osp.splitext(name)[0]
                )
                for name in EXPECTED_IMAGES
            ]
        )
        assert server.uploaded_names == sorted(expected_names)

        # 6) submit: one POST /jobs, a job id, and a ledger row that
        # carries the dataset id and the request hashes.
        assert server.submit_calls == 1
        assert (server.submitted_body or {})["dataset_id"] == DATASET_ID
        assert outcome.job_id == JOB_ID
        record = store.load_ledger().record(JOB_ID)
        assert record is not None
        assert record.dataset_id == DATASET_ID
        assert record.status == "running"
        # The archive the server received carries the frozen plan body
        # as manifest.json, byte for byte (5.4.1 conclusion 3), and the
        # completed submission dropped its pending row (5.4.2 path 1).
        with zipfile.ZipFile(
            io.BytesIO(server.received_archive)
        ) as archive:
            assert archive.read("manifest.json") == (
                run.manifest_json_bytes(pipeline.labels)
            )
        assert store.load_ledger().pending_uploads == []
    finally:
        close_window(window, qapp)


# --------------------------------------------------------------------
# 7) poll -> events -> cancel -> resume -> download
# --------------------------------------------------------------------


def test_b8_chain_polls_cancels_resumes_and_downloads(
    qapp, tmp_path, server
):
    """The rest of the chain, on the window the submit opened."""

    dataset = make_dataset(str(tmp_path / "dataset"))
    store = Store(str(tmp_path / "ledger"))
    client = RemoteTrainingClient(server.base_url, "secret-token")
    _parent, window = open_window(
        qapp,
        store,
        client,
        tmp_path / "staging",
        tmp_path / "staging_scan",
    )
    try:
        _run, outcome = precheck_and_submit(
            qapp, window, dataset, server.base_url
        )
        assert outcome.ok is True, outcome.error_message
        job_id = outcome.job_id
        assert job_id == JOB_ID

        # 7) poll: the submit opened the detail page, whose worker pulls
        # GET /jobs/{id} and renders the running progress.
        pump(qapp, 400)
        assert server.detail_calls >= 1
        assert window.detail_page.job_id == JOB_ID
        assert window.detail_page.bar.value() == 20
        assert "进度 20.0%" in window.detail_page.progress_label.text()

        # 8) events: a fresh event is pulled with ?after=<seq>, rendered
        # in the log and the cursor is written back to the ledger.
        seq = server.push_event(
            "progress", {"percent": 25.0, "epoch": 3, "total_epochs": 10}
        )
        assert seq == 1
        server.set_running(25.0, 3)
        window.show_detail(JOB_ID)
        pump(qapp, 400)
        assert server.event_calls >= 1
        log = window.detail_page.log.toPlainText()
        assert "#1" in log
        assert "progress" in log
        assert store.load_ledger().record(JOB_ID).last_seq == 1

        # 9) cancel: one POST, and the next tick moves the ledger row to
        # the terminal cancelled state the server reported.
        command = window.cancel_job(JOB_ID)
        assert command is not None
        assert command.wait(15000) is True
        pump(qapp, 200)
        assert server.cancel_calls == 1
        assert command.result.ok is True
        assert command.result.status == "cancelled"
        window.show_detail(JOB_ID)
        pump(qapp, 400)
        record = store.load_ledger().record(JOB_ID)
        assert record.status == "cancelled"
        assert record.is_terminal is True

        # 10) resume: one POST with the mode, the response written back
        # into the ledger, and the manual_resume event picked up by the
        # next detail tick.
        command = window.resume_job(JOB_ID, "resume")
        assert command is not None
        assert command.wait(15000) is True
        # The command completion wakes the live detail poller (R2), whose
        # next tick pulls both the cancel log line and the manual_resume
        # event.  The log is read before any later page entry, because
        # entering the detail page on purpose clears it.
        pump(qapp, 400)
        assert server.resume_calls == 1
        assert server.resume_mode == "resume"
        assert command.result.ok is True
        assert command.result.resume_cycles == 1
        record = store.load_ledger().record(JOB_ID)
        assert record.resume_cycles == 1
        assert record.manual_resume == {
            "attempt": 1,
            "resume_cycles": 1,
            "mode": "resume",
        }
        assert record.needs_attention is False
        log = window.detail_page.log.toPlainText()
        assert "第 1 次人工恢复" in log
        assert record.last_seq == 3

        # 11) download: the artifact manifest of a *running* job (the
        # only state that fetches it, 5.5.2) plus the in-memory summary
        # read and the two download routes.
        summary = server.publish_artifacts()
        server.set_running(30.0, 3)
        window.show_results(JOB_ID)
        pump(qapp, 600)
        assert server.files_calls >= 1
        assert window.results_page.files_job_id == JOB_ID
        assert window.results_page.tree.rowCount() == 3
        assert [row["path"] for row in window.results_page.files] == [
            "summary.json",
            "weights/best.pt",
            "partial/weights/last.pt",
        ]
        # summary.json is read by its file_id and rendered in memory.
        ids = server.artifact_ids()
        assert server.file_calls >= 1
        summary_text = window.results_page.summary_edit.toPlainText()
        assert "final_metrics: mAP50={0}".format(
            summary["final_metrics"]["mAP50"]
        ) in summary_text
        assert "已加载 summary.json" in window.results_page.debug_text()

        # The single artifact streams to the chosen path, byte for byte,
        # and the ledger remembers where it landed.
        target = tmp_path / "downloads" / "best.pt"
        os.makedirs(str(target.parent), exist_ok=True)
        worker = window.download_job_file(
            JOB_ID, ids["weights/best.pt"], str(target)
        )
        assert worker is not None
        assert worker.wait(15000) is True
        pump(qapp, 200)
        assert worker.error is None
        assert worker.outcome.complete is True
        assert worker.outcome.bytes_written == len(
            server.artifacts[ids["weights/best.pt"]]
        )
        with open(str(target), "rb") as handle:
            assert handle.read() == server.artifacts[
                ids["weights/best.pt"]
            ]
        assert window.results_page.last_saved_path == str(target)
        assert store.load_ledger().record(
            JOB_ID
        ).download_path == str(target)

        # The zip route (15) streams the whole artifact tree.
        archive_target = tmp_path / "downloads" / "all.zip"
        worker = window.download_results(JOB_ID, str(archive_target))
        assert worker is not None
        assert worker.wait(15000) is True
        pump(qapp, 200)
        assert server.download_calls == 1
        assert worker.outcome.complete is True
        with zipfile.ZipFile(str(archive_target)) as archive:
            assert sorted(archive.namelist()) == [
                "partial/weights/last.pt",
                "summary.json",
                "weights/best.pt",
            ]
            assert archive.read("weights/best.pt") == server.artifacts[
                ids["weights/best.pt"]
            ]

        # 12) the terminal state: one done event while still running,
        # then the terminal job object on the next tick.
        server.push_event("done", {"status": "completed", "exit_code": 0})
        window.show_detail(JOB_ID)
        pump(qapp, 400)
        assert "done" in window.detail_page.log.toPlainText()
        server.set_terminal("completed")
        window.show_detail(JOB_ID)
        pump(qapp, 400)
        record = store.load_ledger().record(JOB_ID)
        assert record.status == "completed"
        assert record.is_terminal is True
        assert window.detail_page.badge.text() == "已完成"
    finally:
        close_window(window, qapp)
