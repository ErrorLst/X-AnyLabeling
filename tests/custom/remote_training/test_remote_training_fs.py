"""Filesystem drills of the monitoring step (spec §5.1.5, §5.5).

CT18, CT40 and CT43 live here: the staging leftovers of a crash are
reclaimed by the fixed seven day TTL, the directories still referenced by
an unfinished pending entry are exempt, keep_staging keeps everything and
shows the full path, and the whole flow writes nothing outside the ledger,
pending directory and settings file.  Everything runs in tmp_path.
"""

from __future__ import annotations

import hashlib
import json
import os
import os.path as osp
import tempfile
import threading
import time
import zipfile
from typing import Any

import pytest
from PyQt6 import QtCore, QtWidgets

from anylabeling.custom.remote_training import api_client as api
from anylabeling.custom.remote_training import poller as poll_mod
from anylabeling.custom.remote_training import store as store_mod
from anylabeling.custom.remote_training.pipeline import Pipeline
from anylabeling.custom.remote_training.store import Store, TaskRecord
from anylabeling.custom.remote_training.ui.dialog import RemoteTrainingDialog
from anylabeling.custom.remote_training.uploader import Uploader
from anylabeling.custom.remote_training.worker import DatasetPacker

JOB_ID = "job_20260101_000001"


def make_dataset(root, classes=("person", "car"), names=("0001", "0002")):
    """A tiny root only dataset (the source directory must stay untouched)."""

    os.makedirs(root, exist_ok=True)
    with open(osp.join(root, "classes.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(classes) + "\n")
    for name in names:
        shapes = [
            {"label": classes[0], "shape_type": "rectangle",
             "points": [[0, 0], [1, 1]]},
        ]
        payload = {
            "version": "1.0", "imageWidth": 10, "imageHeight": 10,
            "shapes": shapes,
        }
        with open(osp.join(root, name + ".json"), "w",
                  encoding="utf-8") as fh:
            json.dump(payload, fh)
        with open(osp.join(root, name + ".jpg"), "wb") as fh:
            fh.write(b"jpeg-bytes")
    return root


def tree_fingerprint(root):
    """sha256 + mtime + size of every file below root (spec §5.3.1)."""

    digest = hashlib.sha256()
    entries = []
    for current, dirs, names in os.walk(root):
        dirs.sort()
        for name in sorted(names):
            path = osp.join(current, name)
            relative = osp.relpath(path, root)
            stat = os.lstat(path)
            with open(path, "rb") as handle:
                digest.update(handle.read())
            entries.append(
                (relative, stat.st_size, stat.st_mtime_ns,
                 hashlib.sha256(open(path, "rb").read()).hexdigest())
            )
    return digest.hexdigest(), entries


def workspace_files(root):
    """Every path the flow created under one root."""

    found = []
    for current, dirs, names in os.walk(root):
        dirs.sort()
        for name in sorted(names):
            found.append(osp.relpath(osp.join(current, name), root))
    return found


def staging(temp_root, name, age_days):
    """One staging directory with a fresh or old owner.json marker."""

    path = tempfile.mkdtemp(prefix=store_mod.STAGING_PREFIX, dir=temp_root)
    os.rename(path, osp.join(temp_root, name))
    path = osp.join(temp_root, name)
    started = time.time() - age_days * 24 * 3600
    moment = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started))
    store_mod.write_owner_marker(path, pid=os.getpid(), started_at=moment)
    with open(osp.join(path, "payload.bin"), "wb") as handle:
        handle.write(b"x" * 1024)
    return path


class QuietClient:
    """A client that answers one job and counts every call."""

    def __init__(self):
        self.calls = []

    def get_job(self, job_id):
        self.calls.append(("get_job", job_id))
        return {
            "job_id": job_id,
            "status": "running",
            "is_terminal": False,
            "progress": {"epoch": 2, "total_epochs": 10, "percent": 20.0},
        }

    def get_events(self, job_id, after=0):
        self.calls.append(("get_events", job_id, int(after)))
        return {"events": [], "last_seq": int(after)}

    def list_job_files(self, job_id, include_partial=True):
        self.calls.append(("list_job_files", job_id))
        return {"files": []}

    def list_jobs(self, ids=None, status=None, limit=50):
        self.calls.append(("list_jobs", list(ids or [])))
        return {"jobs": [], "not_found_ids": list(ids or [])}

    def cancel_job(self, job_id):
        self.calls.append(("cancel_job", job_id))
        return {"job_id": job_id, "status": "cancelled"}

    def resume_job(self, job_id, mode="resume"):
        self.calls.append(("resume_job", job_id, mode))
        return {
            "job_id": job_id,
            "status": "queued",
            "mode": mode,
            "attempt": 1,
            "resume_cycles": 1,
        }


class Parent(QtWidgets.QMainWindow):
    """Stand in for the labeling main window."""


def open_window(parent, store, client, *, client_factory=None):
    """Open one window with its ledger and its client.

    client_factory makes the client of the START-UP reconciliation pass
    deterministic: the window enqueues that pass from __init__, so a test
    that only assigns window.client afterwards races with it.  The
    production default (None) is unchanged: the window builds its own
    client from the saved server configuration.
    """

    parent._remote_training_dialog = None
    window = RemoteTrainingDialog(
        parent, store=store, client_factory=client_factory
    )
    parent._remote_training_dialog = window
    window._confirm_close = lambda _dialog: True
    window._confirm_action = lambda _title, _text: True
    window.client = client
    window.show()
    return window


def seed(store, **overrides):
    ledger = store.load_ledger()
    fields = {"status": "running", "is_terminal": False}
    fields.update(overrides)
    ledger.upsert_record(TaskRecord(
        job_id=JOB_ID,
        client_job_name="任务",
        **fields,
    ))
    store.save_ledger(ledger)


class KeepStagingClient:
    """A submit stub that answers a fresh token / job on every run.

    The staging drill of spec §5.1.5 submits several times through the
    real window, so every run needs its own upload_token and job id:
    the uploader de-duplicates by token (spec §5.4.1).
    """

    def __init__(self):
        self.planned = 0
        self.uploaded = 0
        self.created = 0

    def plan_dataset(self, manifest):
        self.planned += 1
        missing = [
            {"name": item["name"], "split": item["split"],
             "sha256": item["sha256"], "size": item["size"]}
            for item in manifest.get("images") or ()
        ]
        return {
            "upload_token": "ut_{0:032x}".format(self.planned),
            "expires_at": "2099-01-01T00:00:00Z",
            "total_images": len(manifest.get("images") or ()),
            "blob_hits": 0,
            "missing_images": missing,
            "upload_bytes": 10,
            "rejected": [],
            "warnings": [],
        }

    def upload_dataset(self, token, archive_path, **kwargs):
        self.uploaded += 1
        return {
            "dataset_id": "ds_{0:08x}".format(self.uploaded),
            "counts": {},
            "blob": {},
            "warnings": [],
        }

    def create_job(self, body):
        self.created += 1
        return {
            "job_id": "job_2026010{0}_000001".format(self.created),
            "status": "queued",
            "warnings": [],
        }

    def get_job(self, job_id):
        return {"job_id": job_id, "status": "queued", "is_terminal": False}

    def list_jobs(self, ids=None, status=None, limit=50):
        return {"jobs": [], "not_found_ids": []}

    def get_events(self, job_id, after=0):
        return {"events": [], "last_seq": int(after)}

    def list_job_files(self, job_id, include_partial=True):
        return {"files": []}


class FlakySubmitClient(KeepStagingClient):
    """Refuses the first POST /jobs with a retryable 503 (spec §5.6.4)."""

    def __init__(self):
        super().__init__()
        self.fail_jobs = 1

    def create_job(self, body):
        if self.fail_jobs > 0:
            self.fail_jobs -= 1
            raise api.ServerUnavailableError(503, "", "server down")
        return super().create_job(body)


class BlockingUploader(Uploader):
    """A real uploader that parks inside upload() until released.

    It makes "the user starts a pre-check while the submit chain is in
    flight" deterministic, which is the second trigger path of the
    staging / submittability矛盾 (spec §5.1.5 / §5.6.4).
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.entered = threading.Event()
        self.release = threading.Event()

    def upload(self, client, entry, **kwargs):
        self.entered.set()
        self.release.wait(30)
        return super().upload(client, entry, **kwargs)


def window_with_staging(parent, store, client, staging_root):
    """One window whose pipeline keeps its staging work area local."""

    parent._remote_training_dialog = None
    window = RemoteTrainingDialog(
        parent,
        store=store,
        pipeline_factory=lambda **kwargs: Pipeline(
            staging_parent=str(staging_root), **kwargs
        ),
    )
    parent._remote_training_dialog = window
    window._confirm_close = lambda _dialog: True
    window._confirm_action = lambda _title, _text: True
    window.client = client
    window.show()
    return window


def precheck_and_submit(qapp, window, dataset, staging_root):
    """One real pre-check plus submit; returns (staging, outcome)."""

    window.show_config()
    window.config_page.set_values({
        "server_url": "http://server:8000",
        "dataset_dir": dataset,
        "classes_file": osp.join(dataset, "classes.txt"),
        "task": "detect",
        "model": "yolo11n.pt",
        "val_ratio": 0.5,
        "seed": 7,
    })
    window.server_config = store_mod.ServerConfig(
        server_url="http://server:8000"
    )
    precheck = window.start_precheck()
    assert precheck is not None
    assert precheck.wait(15000) is True
    qapp.processEvents()
    run = window.prepared_run
    assert run is not None
    staging = str(run.staging_dir)
    assert staging.startswith(str(staging_root))
    window.config_page.submit_button.click()
    worker = window.submit_worker
    assert worker is not None
    assert worker.wait(15000) is True
    qapp.processEvents()
    return staging, worker.outcome


def submit_one_run(qapp, window, dataset, staging_root):
    """One real pre-check plus a submit that must succeed."""

    staging, outcome = precheck_and_submit(
        qapp, window, dataset, staging_root
    )
    assert outcome.ok is True, outcome.error_message
    return staging


def resubmit(qapp, window):
    """Click 提交任务 again on the still frozen run; returns outcome."""

    window.config_page.submit_button.click()
    worker = window.submit_worker
    assert worker is not None
    assert worker.wait(15000) is True
    qapp.processEvents()
    return worker.outcome

# ------------------------------------------------------------- CT18


def test_ct18_expired_leftovers_are_reclaimed(tmp_path):
    """CT18(a): a crash leftover older than the 7 day TTL is reclaimed."""

    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    old = staging(str(temp_root), "xal_remote_training_old", 8)
    fresh = staging(str(temp_root), "xal_remote_training_fresh", 1)
    other = temp_root / "not_ours"
    other.mkdir()
    store = Store(str(tmp_path / "ledger"))

    report = store.reclaim_staging_leftovers(temp_root=str(temp_root))
    assert report.reclaimed == [old]
    assert osp.isdir(old) is False
    assert osp.isdir(fresh) is True
    assert other.is_dir() is True  # only the app prefix is scanned
    assert report.kept_unexpired == [fresh]
    assert store_mod.STAGING_TTL_SECONDS == 7 * 24 * 3600


def test_ct18_referenced_staging_is_exempt(tmp_path):
    """CT18: a directory a pending entry still references is never freed."""

    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    referenced = staging(str(temp_root), "xal_remote_training_ref", 30)
    orphan = staging(str(temp_root), "xal_remote_training_orphan", 30)
    store = Store(str(tmp_path / "ledger"))
    ledger = store.load_ledger()
    entry = store_mod.PendingUpload(
        upload_token="ut_" + "a" * 32,
        phase="uploading",
        server_url="http://server:8000",
        pending_dir=str(tmp_path / "pending" / "ut_a"),
        staging_dir=referenced,
    )
    ledger.pending_uploads.append(entry)
    store.save_ledger(ledger)

    report = store.reclaim_staging_leftovers(temp_root=str(temp_root))
    assert report.kept_referenced == [referenced]
    assert report.reclaimed == [orphan]
    assert osp.isdir(referenced) is True
    assert osp.isdir(orphan) is False

    # Once the entry is settled the exemption is gone (live re-evaluation).
    ledger = store.load_ledger()
    ledger.pending_uploads[0].phase = "submitted"
    store.save_ledger(ledger)
    report = store.reclaim_staging_leftovers(temp_root=str(temp_root))
    assert report.reclaimed == [referenced]


def test_ct18_keep_staging_keeps_every_directory(qapp, tmp_path):
    """CT18(b): keep_staging keeps every finished run, path visible.

    Three real pre-check + submit runs through the window: this is the
    production branch ② of spec §5.1.5, not a hand made debug line.
    """

    staging_root = tmp_path / "tmp"
    staging_root.mkdir()
    store = Store(str(tmp_path / "ledger"))
    dataset = make_dataset(str(tmp_path / "annotations"))
    client = KeepStagingClient()
    settings = store.load_settings()
    settings.keep_staging = True
    store.save_settings(settings)
    assert store.load_settings().keep_staging is True

    window = window_with_staging(Parent(), store, client, staging_root)
    kept = [
        submit_one_run(qapp, window, dataset, staging_root)
        for _index in range(3)
    ]
    assert client.created == 3
    assert len(set(kept)) == 3
    for path in kept:
        assert osp.isdir(path) is True
        assert osp.isfile(osp.join(path, store_mod.OWNER_FILENAME))
    lines = [
        line for line in window.results_page.debug_text().splitlines()
        if "已保留" in line
    ]
    assert len(lines) == 3
    assert all(path in line for path, line in zip(kept, lines))
    # There is no count limit; only the fixed 7 day TTL can reclaim them.
    report = store.reclaim_staging_leftovers(temp_root=str(staging_root))
    assert report.reclaimed == []
    assert sorted(report.kept_unexpired) == sorted(kept)
    assert store_mod.STAGING_TTL_SECONDS == 7 * 24 * 3600
    window.cancel_workers()
    window.reject()
    qapp.processEvents()


def test_ct18_reclaim_logs_the_count(tmp_path, caplog):
    """Spec §5.1.5: every scan logs reclaimed count and freed size."""

    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    staging(str(temp_root), "xal_remote_training_old", 9)
    store = Store(str(tmp_path / "ledger"))
    with caplog.at_level("INFO"):
        store.reclaim_staging_leftovers(temp_root=str(temp_root))
    assert any("reclaimed" in record.getMessage() for record in caplog.records)


def test_ct18_default_branch_removes_the_run_staging(qapp, tmp_path):
    """Spec §5.1.5 branch ① fires once the run cannot be submitted.

    With keep_staging off, the staging of a finished submit survives
    while its frozen run is still submittable (§5.6.4 manual retry) and
    is removed as soon as a newer pre-check replaces that run; a run
    that was only pre-checked is removed by step 6 of the close
    machine.  Branch ② is proven by CT18(b) / CT43 below.
    """

    staging_root = tmp_path / "tmp"
    staging_root.mkdir()
    store = Store(str(tmp_path / "ledger"))
    dataset = make_dataset(str(tmp_path / "annotations"))
    client = KeepStagingClient()
    window = window_with_staging(Parent(), store, client, staging_root)
    staging = submit_one_run(qapp, window, dataset, staging_root)
    assert client.created == 1
    # Still there: the frozen run can be submitted again by hand.
    assert window.prepared_run is not None
    assert osp.isdir(staging) is True
    assert "已保留" not in window.results_page.debug_text()
    # The run itself is recorded; only its staging work area is pending.
    record = store.load_ledger().record("job_20260101_000001")
    assert record is not None
    assert record.status == "queued"
    # A newer pre-check replaces the frozen run: branch ① fires now.
    old_run = window.prepared_run
    window.show_config()
    precheck = window.start_precheck()
    assert precheck.wait(15000) is True
    qapp.processEvents()
    assert window.prepared_run is not old_run
    assert osp.isdir(staging) is False
    # The fresh pre-check owns a new staging area; a run that is never
    # submitted is retired by step 6 of the close machine.
    prechecked = str(window.pipeline.staging_dir)
    assert osp.isdir(prechecked) is True
    window.cancel_workers()
    window.reject()
    qapp.processEvents()
    assert osp.isdir(prechecked) is False


def test_submit_again_after_a_successful_submit_keeps_the_run(
    qapp, tmp_path
):
    """§5.6.4: the frozen run stays submittable once the chain ended.

    Deleting its staging at task end made the second click fail with a
    FileNotFoundError while packing staging/labels/<stem>.txt, so the
    directory must survive for as long as the run can be submitted.
    """

    staging_root = tmp_path / "tmp"
    staging_root.mkdir()
    store = Store(str(tmp_path / "ledger"))
    dataset = make_dataset(str(tmp_path / "annotations"))
    client = KeepStagingClient()
    window = window_with_staging(Parent(), store, client, staging_root)
    staging = submit_one_run(qapp, window, dataset, staging_root)
    assert osp.isdir(staging) is True
    assert window.prepared_run is not None

    outcome = resubmit(qapp, window)
    assert outcome.ok is True, (outcome.error, outcome.error_message)
    assert client.created == 2
    assert osp.isdir(staging) is True
    window.cancel_workers()
    window.reject()
    qapp.processEvents()


def test_submit_again_after_a_failure_still_packs_the_labels(
    qapp, tmp_path
):
    """A failed POST keeps its staging so the manual retry can pack."""

    staging_root = tmp_path / "tmp"
    staging_root.mkdir()
    store = Store(str(tmp_path / "ledger"))
    dataset = make_dataset(str(tmp_path / "annotations"))
    client = FlakySubmitClient()
    window = window_with_staging(Parent(), store, client, staging_root)
    staging, first = precheck_and_submit(
        qapp, window, dataset, staging_root
    )
    assert first.ok is False
    assert "503" in str(first.error_message or "")
    # §5.6.4: the run is still frozen and the retry can still pack.
    assert window.prepared_run is not None
    assert osp.isdir(staging) is True

    second = resubmit(qapp, window)
    assert second.ok is True, (second.error, second.error_message)
    assert client.created == 1
    assert client.uploaded == 2
    window.cancel_workers()
    window.reject()
    qapp.processEvents()


def test_a_precheck_during_a_submit_keeps_both_staging_areas(
    qapp, tmp_path
):
    """The chain identity decides whose staging is reclaimed (§5.1.5).

    A pre-check started while a submit chain is in flight replaces the
    frozen run.  The live chain must keep its own staging (it is still
    packing), the abandoned run is reclaimed when that chain ends, and
    the new run must stay submittable (never a FileNotFoundError while
    packing staging/labels/).
    """

    staging_root = tmp_path / "tmp"
    staging_root.mkdir()
    store = Store(str(tmp_path / "ledger"))
    dataset = make_dataset(str(tmp_path / "annotations"))
    client = KeepStagingClient()
    window = window_with_staging(Parent(), store, client, staging_root)
    blocking = BlockingUploader(
        store=store, packer=DatasetPacker(), clock=time.time
    )
    window._uploader = blocking

    window.show_config()
    window.config_page.set_values({
        "server_url": "http://server:8000",
        "dataset_dir": dataset,
        "classes_file": osp.join(dataset, "classes.txt"),
        "task": "detect",
        "model": "yolo11n.pt",
        "val_ratio": 0.5,
        "seed": 7,
    })
    window.server_config = store_mod.ServerConfig(
        server_url="http://server:8000"
    )
    first = window.start_precheck()
    assert first.wait(15000) is True
    qapp.processEvents()
    staging1 = str(window.prepared_run.staging_dir)
    window.config_page.submit_button.click()
    submit1 = window.submit_worker
    assert submit1 is not None
    assert blocking.entered.wait(10) is True

    # The user runs a new pre-check while that chain is still in flight.
    second = window.start_precheck()
    assert second.wait(15000) is True
    qapp.processEvents()
    staging2 = str(window.prepared_run.staging_dir)
    assert staging2 != staging1
    assert osp.isdir(staging1) is True, "a live chain still owns its run"
    assert osp.isdir(staging2) is True

    blocking.release.set()
    assert submit1.wait(15000) is True
    qapp.processEvents()
    # The abandoned chain's staging is reclaimed; the new run keeps its.
    assert osp.isdir(staging1) is False
    assert osp.isdir(staging2) is True

    outcome = resubmit(qapp, window)
    assert outcome.ok is True, (outcome.error, outcome.error_message)
    window.cancel_workers()
    window.reject()
    qapp.processEvents()


def test_a_referenced_staging_survives_the_retire_pass(qapp, tmp_path):
    """B2: branch ① never frees a directory a replay may still read.

    A reconciliation replays an unfinished upload and
    DatasetPacker.restore() re-reads entry.staging_dir to repack the
    labels, so the retire pass - a new pre-check or a dataset change
    while that replay runs - must skip exactly that directory: the same
    exemption the TTL scan applies (spec §5.1.5).  Once the entry
    settles, the next pass reclaims the directory.
    """

    staging_root = tmp_path / "tmp"
    staging_root.mkdir()
    store = Store(str(tmp_path / "ledger"))
    dataset = make_dataset(str(tmp_path / "annotations"))
    client = KeepStagingClient()
    window = window_with_staging(Parent(), store, client, staging_root)
    window.show_config()
    window.config_page.set_values({
        "server_url": "http://server:8000",
        "dataset_dir": dataset,
        "classes_file": osp.join(dataset, "classes.txt"),
        "task": "detect",
        "model": "yolo11n.pt",
        "val_ratio": 0.5,
        "seed": 7,
    })
    window.server_config = store_mod.ServerConfig(
        server_url="http://server:8000"
    )
    precheck = window.start_precheck()
    assert precheck.wait(15000) is True
    qapp.processEvents()
    pipeline = window.prepared_pipeline
    assert pipeline is not None
    staging = str(pipeline.staging_dir)
    assert osp.isdir(staging) is True

    # The run is latched as an unfinished upload: exactly the state a
    # reconciliation pass replays out of this directory.
    ledger = store.load_ledger()
    ledger.pending_uploads.append(store_mod.PendingUpload(
        upload_token="ut_" + "b" * 32,
        phase="uploading",
        server_url="http://server:8000",
        pending_dir=str(tmp_path / "pending" / "ut_b"),
        staging_dir=staging,
    ))
    store.save_ledger(ledger)
    assert store.is_staging_referenced(staging) is True

    # The retire pass of a replaced run: the dataset changed under it.
    window._on_dataset_changed(dataset)
    qapp.processEvents()
    assert osp.isdir(staging) is True, "a referenced staging is exempt"
    assert window.staging_is_referenced(staging) is True
    assert "仍被未完成的待提交条目引用" in window.results_page.debug_text()
    assert getattr(pipeline, "staging_finished", False) is False, (
        "a skipped retirement must stay retryable"
    )

    # Settling the entry ends the exemption: the next pass reclaims it.
    ledger = store.load_ledger()
    ledger.pending_uploads[0].phase = "submitted"
    store.save_ledger(ledger)
    assert store.is_staging_referenced(staging) is False
    window._finish_pipeline_staging(pipeline)
    assert osp.isdir(staging) is False
    assert getattr(pipeline, "staging_finished", False) is True
    window.cancel_workers()
    window.reject()
    qapp.processEvents()


def test_a_degraded_ledger_also_keeps_the_staging(qapp, tmp_path):
    """Item 3: load_ledger() never raises, so the safety net must not
    depend on an exception that a damaged ledger never produces.

    With tasks.json and tasks.json.bak both damaged, spec §5.3.4 rebuilds
    an empty ledger, which cannot tell 'no pending entry references this
    directory' apart from 'the entry that did is gone'.  The quarantine
    marker is the observable state, and it keeps the directory."""

    staging_root = tmp_path / "tmp"
    staging_root.mkdir()
    store = Store(str(tmp_path / "ledger"))
    dataset = make_dataset(str(tmp_path / "annotations"))
    client = KeepStagingClient()
    window = window_with_staging(Parent(), store, client, staging_root)
    window.show_config()
    window.config_page.set_values({
        "server_url": "http://server:8000",
        "dataset_dir": dataset,
        "classes_file": osp.join(dataset, "classes.txt"),
        "task": "detect",
        "model": "yolo11n.pt",
        "val_ratio": 0.5,
        "seed": 7,
    })
    window.server_config = store_mod.ServerConfig(
        server_url="http://server:8000"
    )
    precheck = window.start_precheck()
    assert precheck.wait(15000) is True
    qapp.processEvents()
    pipeline = window.prepared_pipeline
    assert pipeline is not None
    staging = str(pipeline.staging_dir)
    assert osp.isdir(staging) is True

    # Both ledger files are damaged: the read degrades silently.
    os.makedirs(store.base_dir, exist_ok=True)
    with open(store.tasks_path, "w", encoding="utf-8") as handle:
        handle.write("{not json")
    with open(store.tasks_bak_path, "w", encoding="utf-8") as handle:
        handle.write("{not json")
    assert store.load_ledger().pending_uploads == []
    assert store.corrupt_files(), "the recovery left no marker"
    assert store.is_staging_referenced(staging) is False
    assert window.staging_is_referenced(staging) is True

    window._on_dataset_changed(dataset)
    qapp.processEvents()
    assert osp.isdir(staging) is True
    window.cancel_workers()
    window.reject()
    qapp.processEvents()


def test_a_degraded_ledger_is_reported_once_in_the_debug_area(
    qapp, tmp_path
):
    """Item 3a: §5.3.4 wants the recovered ledger made visible.

    The quarantine marker is sticky, so the staging exemption it drives
    lasts for the whole session; the debug area (the B7 deliverable of
    §5.1.5) says so at start-up instead of leaving it to a log file."""

    staging_root = tmp_path / "tmp"
    staging_root.mkdir()
    store = Store(str(tmp_path / "ledger"))
    os.makedirs(store.base_dir, exist_ok=True)
    with open(store.tasks_path, "w", encoding="utf-8") as handle:
        handle.write("{not json")
    with open(store.tasks_bak_path, "w", encoding="utf-8") as handle:
        handle.write("{not json")
    assert store.load_ledger().records == []
    assert store.ledger_is_degraded() is True

    window = window_with_staging(
        Parent(), store, KeepStagingClient(), staging_root
    )
    assert "台账曾损坏并被隔离" in window.results_page.debug_text()
    window.cancel_workers()
    window.reject()
    qapp.processEvents()

    # A healthy ledger says nothing about it (one window at a time: the
    # close guard is a process wide singleton, spec §5.1.4).
    clean = Store(str(tmp_path / "clean"))
    other = window_with_staging(
        Parent(), clean, KeepStagingClient(), staging_root
    )
    assert "台账曾损坏并被隔离" not in other.results_page.debug_text()
    other.cancel_workers()
    other.reject()
    qapp.processEvents()


def test_an_unreadable_ledger_directory_counts_as_degraded(
    tmp_path, monkeypatch
):
    """Item 3b: 'cannot tell' must not read as 'nothing quarantined'."""

    store = Store(str(tmp_path / "ledger"))
    os.makedirs(store.base_dir, exist_ok=True)
    real_listdir = os.listdir

    def fake_listdir(path="."):
        if str(path) == store.base_dir:
            raise OSError(13, "permission denied")
        return real_listdir(path)

    monkeypatch.setattr(store_mod.os, "listdir", fake_listdir)
    assert store.corrupt_files() == []
    assert store.ledger_is_degraded() is True

def test_import_config_retires_the_frozen_run(qapp, tmp_path):
    """B1: an import replaces the pipeline, so its run is retired.

    import_config used to swap self.pipeline only: the frozen run stayed
    in self.prepared_run, could no longer be submitted (the submit
    compares identity with self.prepared_pipeline) and its staging
    waited for the window close or the fixed seven day TTL.
    """

    staging_root = tmp_path / "tmp"
    staging_root.mkdir()
    store = Store(str(tmp_path / "ledger"))
    dataset = make_dataset(str(tmp_path / "annotations"))
    client = KeepStagingClient()
    window = window_with_staging(Parent(), store, client, staging_root)
    window.show_config()
    window.config_page.set_values({
        "server_url": "http://server:8000",
        "dataset_dir": dataset,
        "classes_file": osp.join(dataset, "classes.txt"),
        "task": "detect",
        "model": "yolo11n.pt",
        "val_ratio": 0.5,
        "seed": 7,
    })
    window.server_config = store_mod.ServerConfig(
        server_url="http://server:8000"
    )
    precheck = window.start_precheck()
    assert precheck.wait(15000) is True
    qapp.processEvents()
    run = window.prepared_run
    assert run is not None
    staging = str(run.staging_dir)
    assert osp.isdir(staging) is True

    # A real export document of the current form, edited like a user
    # would edit it before importing it back.
    values = window.config_page.values().export_values()
    values["seed"] = 7
    document = window.pipeline.export_config(values)
    document["seed"] = 11
    path = tmp_path / "imported.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    imported = window.import_config(str(path))
    qapp.processEvents()
    assert imported, "the document must have been accepted"
    # The frozen run is gone and the submit refuses to upload it ...
    assert window.prepared_run is None
    assert window.prepared_pipeline is None
    assert window.start_submit() is None
    # ... so its staging area is reclaimed right away instead of waiting
    # for the window close or the seven day TTL (spec §5.1.5 branch ①).
    assert osp.isdir(staging) is False
    window.cancel_workers()
    window.reject()
    qapp.processEvents()

def test_ct18_startup_scan_reclaims_the_leftovers(qapp, tmp_path):
    """CT18(a) through the production start-up path (spec §5.1.5).

    The window scans the temporary root once when it opens: the count
    and the freed size are written to the debug area, and only the
    directories older than the fixed TTL are reclaimed.
    """

    temp_root = tmp_path / "sys-temp"
    temp_root.mkdir()
    old = staging(str(temp_root), "xal_remote_training_old", 9)
    fresh = staging(str(temp_root), "xal_remote_training_fresh", 1)
    store = Store(str(tmp_path / "ledger"))
    seed(store)
    client = QuietClient()
    parent = Parent()
    parent._remote_training_dialog = None
    window = RemoteTrainingDialog(
        parent, store=store, staging_temp_root=str(temp_root)
    )
    parent._remote_training_dialog = window
    window._confirm_close = lambda _dialog: True
    window._confirm_action = lambda _title, _text: True
    window.client = client
    window.show()
    assert window.staging_reclaim is not None
    assert window.staging_reclaim.reclaimed == [old]
    assert osp.isdir(old) is False
    assert osp.isdir(fresh) is True
    text = window.results_page.debug_text()
    assert "启动扫描 staging 遗留" in text
    assert "回收 1 个" in text
    window.cancel_workers()
    window.reject()
    qapp.processEvents()


# ------------------------------------------------------------- CT40


def test_ct40_dataset_and_workspace_are_written_by_nothing(
    qapp, tmp_path, qt_messages
):
    """CT40: the source bytes never change and only the ledger is written.

    The drill runs a scan, a poll tick, a resume and a cancel, then
    reclaims the leftovers - and compares the dataset directory and the
    set of created workspace files before and after.
    """

    dataset = make_dataset(str(tmp_path / "annotations"))
    before_digest, before_entries = tree_fingerprint(dataset)

    workspace = tmp_path / "work"
    ledger_dir = workspace / "xanylabeling_data" / "remote_training"
    store = Store(str(ledger_dir))
    seed(store)
    client = QuietClient()
    window = open_window(Parent(), store, client)
    opened = workspace_files(str(workspace))

    # A detail tick, a results tick and a list tick, each one through the
    # worker its page starts (nothing here touches the network inline).
    window.show_detail(JOB_ID)
    detail_worker = window.poll_worker
    assert detail_worker is not None
    assert detail_worker.one_shot(1) is detail_worker
    assert detail_worker.wait(5000) is True
    qapp.processEvents()
    assert detail_worker.ticks >= 1
    window.show_results(JOB_ID)
    results_worker = window.poll_worker
    assert results_worker is not None
    results_worker.cancel()
    assert results_worker.wait(5000) is True
    window.show_jobs()
    jobs_worker = window.poll_worker
    assert jobs_worker is not None
    assert jobs_worker._page == "jobs"
    # Let the list batch really run (one poll tick over the ledger), then
    # stop it: this is the list path of the zero write assertion below.
    assert jobs_worker.wait(2000) is False or jobs_worker.ticks >= 1
    jobs_worker.cancel()
    assert jobs_worker.wait(5000) is True
    qapp.processEvents()

    # A resume and a cancel.
    window.detail_page.resume_modes = ["resume"]
    resume = window.resume_job(JOB_ID, "resume")
    assert resume is not None and resume.wait(5000) is True
    qapp.processEvents()
    cancel = window.cancel_job(JOB_ID)
    assert cancel is not None and cancel.wait(5000) is True
    qapp.processEvents()

    # The staging drill of the same window.
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    staging(str(temp_root), "xal_remote_training_old", 20)
    store.reclaim_staging_leftovers(temp_root=str(temp_root))

    after_digest, after_entries = tree_fingerprint(dataset)
    assert after_digest == before_digest
    assert after_entries == before_entries

    created = set(workspace_files(str(workspace))) - set(opened)
    allowed_names = {
        store_mod.TASKS_FILENAME,
        store_mod.TASKS_BACKUP_FILENAME,
        store_mod.SERVER_FILENAME,
        store_mod.SETTINGS_FILENAME,
    }
    for relative in created:
        name = osp.basename(relative)
        if name.endswith(".tmp"):
            # Atomic write intermediate (tasks.json.tmp): it is renamed
            # into place, so it is never a surviving artifact.
            continue
        assert name in allowed_names, relative
        assert store_mod.PENDING_DIRNAME not in relative.split(osp.sep)

    text = window.results_page.debug_text()
    assert "远程训练窗口已打开" in text
    assert "Destroyed while thread is still running" not in qt_messages()
    window.cancel_workers()
    window.reject()
    qapp.processEvents()


def test_ct40_no_write_goes_to_the_user_configuration_tree(tmp_path):
    """The ledger is the only writer: a fresh HOME stays empty."""

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    store = Store(str(tmp_path / "elsewhere"))
    seed(store)
    client = QuietClient()
    scheduler = poll_mod.Scheduler(
        store,
        list_fn=client.list_jobs,
        detail_fn=client.get_job,
        events_fn=client.get_events,
        files_fn=client.list_job_files,
    )
    outcome = scheduler.tick(poll_mod.PAGE_DETAIL, job_id=JOB_ID)
    assert outcome.results
    scheduler.tick(poll_mod.PAGE_JOBS, active=[JOB_ID])
    assert workspace_files(str(fake_home)) == []
    assert osp.isdir(store.base_dir) is True

# ------------------------------------------------------------- CT43


def test_ct43_kept_staging_path_is_visible_and_selectable(
    qapp, tmp_path, qt_messages
):
    """CT43: keep_staging keeps 3 finished runs, path copyable."""

    staging_root = tmp_path / "tmp"
    staging_root.mkdir()
    store = Store(str(tmp_path / "ledger"))
    dataset = make_dataset(str(tmp_path / "annotations"))
    client = KeepStagingClient()
    settings = store.load_settings()
    settings.keep_staging = True
    store.save_settings(settings)
    assert store.load_settings().keep_staging is True

    window = window_with_staging(Parent(), store, client, staging_root)
    kept = [
        submit_one_run(qapp, window, dataset, staging_root)
        for _index in range(3)
    ]
    assert len(set(kept)) == 3

    text = window.results_page.debug_text()
    lines = [line for line in text.splitlines() if "已保留" in line]
    assert len(lines) == 3
    for path, line in zip(kept, lines):
        assert path in line
        assert osp.isabs(path) and osp.basename(path) in line
    # The area is selectable and copyable (never NoTextInteraction).
    from anylabeling.custom.remote_training.ui.results_page import (
        debug_area_is_selectable,
    )

    assert debug_area_is_selectable(window.results_page.debug_edit) is True
    assert window.results_page.debug_edit.isReadOnly() is True
    assert window.results_page.debug_edit.objectName() == "debugInfoEdit"

    # Nothing limited the count: only the fixed TTL can reclaim them.
    report = store.reclaim_staging_leftovers(temp_root=str(staging_root))
    assert report.reclaimed == []
    assert sorted(report.kept_unexpired) == sorted(kept)
    assert "Destroyed while thread is still running" not in qt_messages()
    window.cancel_workers()
    window.reject()
    qapp.processEvents()


# ------------------------------------------------- D5: reconcile banner


class StubClientBase:
    """The read routes a window needs once its pages start polling."""

    def get_job(self, job_id):
        return {"job_id": job_id, "status": "queued", "is_terminal": False}

    def list_jobs(self, ids=None, status=None, limit=50):
        return {"jobs": [], "not_found_ids": []}

    def get_events(self, job_id, after=0):
        return {"events": [], "last_seq": int(after)}

    def list_job_files(self, job_id, include_partial=True):
        return {"files": []}


class ReplayClient(StubClientBase):
    """Answers one replayed upload (spec §5.4.4 recovery action)."""

    def __init__(self):
        self.uploaded = 0
        self.created = 0

    def upload_dataset(self, token, archive_path, **kwargs):
        self.uploaded += 1
        return {
            "dataset_id": "ds_replay_0001",
            "counts": {},
            "blob": {},
            "warnings": [],
        }

    def create_job(self, body):
        self.created += 1
        return {"job_id": JOB_ID, "status": "queued", "warnings": []}


def test_d5_jobs_page_entry_reconciles_and_shows_the_banner(
    qapp, tmp_path
):
    """D5 ①: an unfinished entry plus an orphan directory, on entry.

    Entering the jobs page runs the reconciliation pass.  Its banner
    ("有 N 个未完成的上传 / 提交，正在自动对账") and the dropped count
    become visible on the jobs page status row.
    """

    workspace = tmp_path / "work"
    ledger_dir = workspace / "xanylabeling_data" / "remote_training"
    store = Store(str(ledger_dir))
    seed(store)
    pending = store.ensure_pending_dir("ut_" + "a" * 32)
    manifest = {
        "schema_version": 1,
        "task": "detect",
        "val_ratio": 0.5,
        "seed": 7,
        "classes": ["person"],
        "images": [],
    }
    manifest_path = osp.join(pending, "manifest.json")
    store.write_pending_json(manifest_path, manifest)
    archive = osp.join(pending, "archive.zip")
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr(
            "manifest.json",
            store_mod.canonical_json_bytes(manifest).decode("utf-8"),
        )
    ledger = store.load_ledger()
    ledger.pending_uploads.append(store_mod.PendingUpload(
        upload_token="ut_" + "a" * 32,
        phase="uploading",
        server_url="http://server:8000",
        pending_dir=pending,
        archive_path=archive,
        manifest_path=manifest_path,
        missing_images=[
            {"name": "0001.jpg", "split": "train",
             "sha256": "0" * 64, "size": 10},
        ],
    ))
    store.save_ledger(ledger)
    assert store.load_ledger().problems() == []

    client = ReplayClient()
    window = open_window(
        Parent(), store, client, client_factory=lambda: client
    )
    assert window.reconciliation_needed() is True
    # The two passes are serialized on purpose.  __init__ already starts
    # the start-up pass and show_jobs() starts a second one for the same
    # token; both may legally replay the upload, so asserting the count
    # before the first one landed was a race (it read 1 or 2 depending on
    # the thread schedule).  Waiting for the start-up pass first makes the
    # count deterministic: it moves the entry to committed, and the second
    # pass only reports that committed entry (spec §5.4.1: a committed
    # entry is never re-uploaded).
    startup = window.reconcile_worker
    assert startup is not None
    assert startup.wait(10000) is True
    qapp.processEvents()
    window.show_jobs()
    worker = window.reconcile_worker
    assert worker is not None
    assert worker is not startup
    assert worker.wait(10000) is True
    qapp.processEvents()

    lines = window._status_rows("jobs")[0].lines()
    assert any("未完成" in text for text in lines), lines
    report = window.entries_report
    assert report is not None
    assert client.uploaded == 1, (
        "the unfinished entry was replayed",
        window.entries_report.messages if window.entries_report else None,
        store.load_ledger().pending_uploads[0].phase,
    )
    assert client.uploaded == 1, (
        "the unfinished entry was replayed through the window client",
        report.messages,
    )
    # The entry moved from uploading to committed (it now waits for its
    # own submit), so the chain is live rather than merely counted.
    ledger = store.load_ledger()
    assert ledger.pending_uploads[0].phase == "committed"

    # The banner survives a later polling tick that really returns a
    # task: the list summary is one more line of the same composed row,
    # not a second writer that wipes the reconciliation banner.
    listed = poll_mod.PollJob(
        job_id=JOB_ID,
        page="jobs",
        job={"job_id": JOB_ID, "status": "running",
             "is_terminal": False},
    )
    tick = poll_mod.PollOutcome(
        page="jobs", results=[listed], batches=1, interval=10.0
    )
    window._on_polled(tick, None)
    window._on_polled_job(tick, listed, None)
    lines_after = window._status_rows("jobs")[0].lines()
    assert any("未完成" in text for text in lines_after), lines_after
    assert any("共 1 个任务" in text for text in lines_after), lines_after
    assert window.reconcile_lines, window.reconcile_lines
    window.cancel_workers()


def test_reconcile_banner_is_cleared_when_nothing_is_pending(
    qapp, tmp_path
):
    """A banner of the previous pass must not linger (D5 residual)."""

    store = Store(str(tmp_path / "ledger"))
    seed(store)
    client = ReplayClient()
    window = open_window(Parent(), store, client)
    window.reconcile_lines = [
        ("yellow", "有 1 个未完成的上传 / 提交，正在自动对账")
    ]
    window._status_rows("jobs")[0].set_lines(window.reconcile_lines)
    assert window.reconciliation_needed() is False
    window.start_reconcile()
    assert window.reconcile_lines == []
    assert window._status_rows("jobs")[0].is_empty() is True
    window.cancel_workers()


class StubSubmitClient(StubClientBase):
    """The three routes the real plan -> upload -> submit chain needs."""

    def __init__(self):
        self.planned = 0
        self.uploaded = 0
        self.created = 0

    def plan_dataset(self, manifest):
        self.planned += 1
        missing = [
            {"name": item["name"], "split": item["split"],
             "sha256": item["sha256"], "size": item["size"]}
            for item in manifest.get("images") or ()
        ]
        return {
            "upload_token": "ut_" + "c" * 32,
            "expires_at": "2099-01-01T00:00:00Z",
            "total_images": len(manifest.get("images") or ()),
            "blob_hits": 0,
            "missing_images": missing,
            "upload_bytes": 10,
            "rejected": [],
            "warnings": [],
        }

    def upload_dataset(self, token, archive_path, **kwargs):
        self.uploaded += 1
        return {
            "dataset_id": "ds_20260101_ab12cd",
            "counts": {},
            "blob": {},
            "warnings": [],
        }

    def create_job(self, body):
        self.created += 1
        return {"job_id": JOB_ID, "status": "queued", "warnings": []}


def test_d5_submit_flow_is_wired_from_the_config_page(qapp, tmp_path):
    """D5 ②: submit_requested runs the whole chain in a worker."""

    workspace = tmp_path / "work"
    ledger_dir = workspace / "xanylabeling_data" / "remote_training"
    store = Store(str(ledger_dir))
    dataset = make_dataset(str(tmp_path / "annotations"))
    client = StubSubmitClient()
    window = open_window(Parent(), store, client)
    window.config_page.set_values({
        "server_url": "http://server:8000",
        "dataset_dir": dataset,
        "classes_file": osp.join(dataset, "classes.txt"),
        "task": "detect",
        "model": "yolo11n.pt",
        "val_ratio": 0.5,
        "seed": 7,
    })
    window.server_config = store_mod.ServerConfig(
        server_url="http://server:8000"
    )

    # No pre-check yet: the window refuses instead of sending anything.
    # Nothing here pokes at internals: this is the user clicking 提交任务.
    window.config_page.submit_button.click()
    assert window.start_submit() is None
    assert client.created == 0
    assert "本地预检" in " ".join(window.config_page.status_row.lines())

    # R1: the user clicks 本地预检, and nothing else.  The window itself
    # must freeze the result and hand it to the submit - there is no
    # pipeline.run bridge in production code.
    precheck = window.start_precheck()
    assert precheck is not None
    assert precheck.wait(15000) is True
    qapp.processEvents()
    assert window.prepared_run is not None
    assert window.prepared_pipeline is window.pipeline

    # R1: the user then clicks 提交任务 (via the signal the button emits).
    window.config_page.submit_button.click()
    worker = window.submit_worker
    assert worker is not None
    assert worker.wait(15000) is True
    qapp.processEvents()
    assert client.planned == 1
    assert client.uploaded == 1, worker.outcome.error_message or (
        worker.outcome.error
    )
    assert client.created == 1
    assert worker.outcome.ok is True
    text = " ".join(window.config_page.status_row.lines())
    assert JOB_ID in text
    # The new job is opened on the detail page with its poller running.
    assert window.detail_page.job_id == JOB_ID
    assert window.poll_worker is not None
    window.cancel_workers()

def test_r1_a_changed_form_refuses_the_stale_frozen_run(qapp, tmp_path):
    """R1: after a form change the submit asks for a new pre-check."""

    workspace = tmp_path / "work"
    ledger_dir = workspace / "xanylabeling_data" / "remote_training"
    store = Store(str(ledger_dir))
    dataset = make_dataset(str(tmp_path / "annotations"))
    client = StubSubmitClient()
    window = open_window(Parent(), store, client)
    window.config_page.set_values({
        "server_url": "http://server:8000",
        "dataset_dir": dataset,
        "classes_file": osp.join(dataset, "classes.txt"),
        "task": "detect",
        "model": "yolo11n.pt",
        "val_ratio": 0.5,
        "seed": 7,
    })
    window.server_config = store_mod.ServerConfig(
        server_url="http://server:8000"
    )
    precheck = window.start_precheck()
    assert precheck.wait(15000) is True
    qapp.processEvents()
    assert window.prepared_run is not None
    assert window.start_submit() is not None
    window.submit_worker.wait(15000)
    qapp.processEvents()
    assert client.planned == 1

    # The user changes the split ratio: the frozen bytes are stale now.
    window.config_page.set_values({"val_ratio": 0.25})
    assert window.start_submit() is None
    assert client.planned == 1, "no second plan may be sent"
    assert "重新预检" in " ".join(window.config_page.status_row.lines())
    assert window.prepared_run is None
    window.cancel_workers()

def test_r1_a_blocked_precheck_freezes_nothing(qapp, tmp_path):
    """R1: a refused local pre-check must not leave an uploadable run."""

    workspace = tmp_path / "work"
    ledger_dir = workspace / "xanylabeling_data" / "remote_training"
    store = Store(str(ledger_dir))
    dataset = make_dataset(
        str(tmp_path / "one"), classes=("person",), names=("0001",)
    )
    client = StubSubmitClient()
    window = open_window(Parent(), store, client)
    window.config_page.set_values({
        "server_url": "http://server:8000",
        "dataset_dir": dataset,
        "classes_file": osp.join(dataset, "classes.txt"),
        "task": "detect",
        "model": "yolo11n.pt",
        "val_ratio": 0.5,
        "seed": 7,
    })
    window.server_config = store_mod.ServerConfig(
        server_url="http://server:8000"
    )
    precheck = window.start_precheck()
    assert precheck.wait(15000) is True
    qapp.processEvents()
    # One image for one class: the validation side is empty, so the split
    # is blocked and the frozen result is dropped on purpose.
    assert window.prepared_run is None
    assert window.start_submit() is None
    assert client.planned == 0
    window.cancel_workers()

# ------------------------------------------------- S1: the precheck window


class BlockingPrecheck(QtCore.QThread):
    """A pre-check worker that only finishes when the test releases it.

    This makes the "user edits the form while the pre-check runs" window
    deterministic: start_precheck() latches the form, the test changes it,
    and only then does the worker emit its result.
    """

    finished_with = QtCore.pyqtSignal(object, object, object)

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self.release_event = threading.Event()
        self.pipeline: Any = None
        self.run_result: Any = None
        self.error: Any = None

    def cancel(self) -> None:  # the close machine hook
        self.release_event.set()

    def run(self) -> None:  # noqa: D102 - QThread entry point
        self.release_event.wait(10)
        self.finished_with.emit(self.pipeline, self.run_result, self.error)


def prepare_window_for_precheck(tmp_path):
    """One window whose form is complete, plus a run to hand back."""

    workspace = tmp_path / "work"
    ledger_dir = workspace / "xanylabeling_data" / "remote_training"
    store = Store(str(ledger_dir))
    dataset = make_dataset(str(tmp_path / "annotations"))
    client = StubSubmitClient()
    window = open_window(Parent(), store, client)
    window.config_page.set_values({
        "server_url": "http://server:8000",
        "dataset_dir": dataset,
        "classes_file": osp.join(dataset, "classes.txt"),
        "task": "detect",
        "model": "yolo11n.pt",
        "val_ratio": 0.5,
        "seed": 7,
    })
    window.server_config = store_mod.ServerConfig(
        server_url="http://server:8000"
    )
    probe = window._new_pipeline()
    probe.validate_config()
    run = probe.prepare()
    return window, run, dataset, client


def _release_precheck(qapp, window, run, fake):
    fake.pipeline = window.pipeline
    fake.run_result = run
    fake.error = None
    fake.release_event.set()
    assert fake.wait(15000) is True
    qapp.processEvents()


def test_s1_a_late_seed_change_is_not_frozen(qapp, tmp_path):
    """S1: a val_ratio edit during the run must not whitewash the run."""

    window, run, _dataset, _client = prepare_window_for_precheck(tmp_path)
    fake = BlockingPrecheck()
    worker = window.start_precheck(worker=fake)
    assert worker is fake
    assert window._precheck_form is not None
    # The user edits the split while the pre-check is still running.
    window.config_page.set_values({"val_ratio": 0.25})
    _release_precheck(qapp, window, run, fake)
    assert window.prepared_run is None
    assert "预检期间配置已变更" in " ".join(
        window.config_page.status_row.lines()
    )
    # The submit refuses as well (its own red line replaces the hint).
    assert window.start_submit() is None
    window.cancel_workers()


def test_s1_a_late_dataset_change_is_not_frozen(qapp, tmp_path):
    """S1: a dataset_dir edit during the run must not whitewash the run."""

    window, run, _dataset, _client = prepare_window_for_precheck(tmp_path)
    fake = BlockingPrecheck()
    assert window.start_precheck(worker=fake) is fake
    # The browse button path: only the dataset box changes.
    window.config_page.set_values({
        "dataset_dir": str(tmp_path / "somewhere-else")
    })
    _release_precheck(qapp, window, run, fake)
    assert window.prepared_run is None
    assert "预检期间配置已变更" in " ".join(
        window.config_page.status_row.lines()
    )
    # The submit refuses as well (its own red line replaces the hint).
    assert window.start_submit() is None
    window.cancel_workers()


def test_s1_an_untouched_form_is_still_frozen(qapp, tmp_path):
    """S1 control: no edit during the run ⇒ the run stays frozen."""

    window, run, _dataset, _client = prepare_window_for_precheck(tmp_path)
    fake = BlockingPrecheck()
    assert window.start_precheck(worker=fake) is fake
    latched = window._precheck_form
    _release_precheck(qapp, window, run, fake)
    assert window.prepared_run is run
    assert window.prepared_pipeline is window.pipeline
    assert window.prepared_fingerprint == latched
    assert "预检期间配置已变更" not in " ".join(
        window.config_page.status_row.lines()
    )
    window.cancel_workers()
