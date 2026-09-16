"""The per-user server.json of spec §5.3.3.

server.json holds the one server URL and its Token, so it no longer
follows the work directory: it lives in ~/.xanylabeling/remote_training
(the convention crash_log already uses) while tasks.json, settings.json
and pending/<id>/ stay in the ledger directory.  The pre §5.3.3 file
inside the work directory is read once, copied byte for byte, and kept
forever.  Everything runs in tmp_path; nothing here uses the network.
"""

from __future__ import annotations

import json
import os
import os.path as osp
import stat

import pytest

from anylabeling.custom.remote_training import store as store_mod
from anylabeling.custom.remote_training.pipeline import (
    Pipeline,
    PipelineConfig,
)
from anylabeling.custom.remote_training.store import (
    LocalSettings,
    ServerConfig,
    Store,
)

JOB_ID = "job_20260101_000001"
TOKEN = "token-abc"


def legacy_payload(**extra):
    """A pre §5.3.3 server.json, unknown keys included."""

    payload = {
        "server_url": "http://legacy:8000",
        "api_key": TOKEN,
        "updated_at": "2026-01-01T00:00:00Z",
    }
    payload.update(extra)
    return payload


def write_legacy(base_dir, payload=None):
    """The old server.json inside the work directory; returns its path."""

    os.makedirs(base_dir, exist_ok=True)
    path = osp.join(str(base_dir), store_mod.SERVER_FILENAME)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(
            legacy_payload() if payload is None else payload, handle
        )
    return path


def block_directory(path):
    """Occupy path with a regular file, so makedirs raises.

    A plain file makes os.makedirs(path, exist_ok=True) raise
    FileExistsError on every platform and for root as well, which a
    chmod based read only directory cannot guarantee.
    """

    parent = osp.dirname(str(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(str(path), "w", encoding="utf-8") as handle:
        handle.write("")


def test_default_server_dir_is_the_fork_user_directory(monkeypatch):
    # The fallback branch, read with the env override out of the way.
    monkeypatch.delenv(store_mod.SERVER_DIR_ENV, raising=False)

    assert store_mod.default_server_dir() == osp.join(
        osp.expanduser("~"),
        store_mod.USER_DIRNAME,
        store_mod.SERVER_DIRNAME,
    )


def test_the_env_override_wins_and_expands_the_tilde(monkeypatch, tmp_path):
    monkeypatch.setenv(
        store_mod.SERVER_DIR_ENV, "~/elsewhere/remote_training"
    )

    expected = osp.join(
        osp.expanduser("~"), "elsewhere", "remote_training"
    )
    assert store_mod.default_server_dir() == expected
    assert Store(str(tmp_path / "ledger")).server_dir == expected


def test_the_store_kwarg_wins_over_the_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv(
        store_mod.SERVER_DIR_ENV, str(tmp_path / "env-server-dir")
    )
    store = Store(
        str(tmp_path / "ledger"), server_dir=str(tmp_path / "kwarg-dir")
    )

    assert store.server_dir == str(tmp_path / "kwarg-dir")
    assert Store(
        str(tmp_path / "ledger"), server_dir="~/explicit"
    ).server_dir == osp.abspath(
        osp.join(osp.expanduser("~"), "explicit")
    )


def test_two_work_directories_share_one_server_json(tmp_path):
    home = str(tmp_path / "home")
    first = Store(str(tmp_path / "work-A"), server_dir=home)
    second = Store(str(tmp_path / "work-B"), server_dir=home)
    first.save_server(
        ServerConfig(server_url="http://shared:8000", api_key=TOKEN)
    )

    loaded = second.load_server()

    assert loaded.server_url == "http://shared:8000"
    assert loaded.api_key == TOKEN
    assert second.server_path == first.server_path
    assert osp.isfile(second.server_path) is True
    for base_dir in ("work-A", "work-B"):
        assert (
            osp.isfile(
                osp.join(
                    str(tmp_path / base_dir), store_mod.SERVER_FILENAME
                )
            )
            is False
        )


def test_other_ledger_files_stay_in_the_work_directory(tmp_path):
    home = str(tmp_path / "home")
    store = Store(str(tmp_path / "work"), server_dir=home)
    store.save_server(
        ServerConfig(server_url="http://shared:8000", api_key=TOKEN)
    )
    store.save_settings(LocalSettings(keep_staging=True))

    assert osp.isfile(store.settings_path) is True
    assert store.settings_path == osp.join(
        store.base_dir, store_mod.SETTINGS_FILENAME
    )
    assert store.pending_root == osp.join(
        store.base_dir, store_mod.PENDING_DIRNAME
    )
    assert osp.isdir(store.ensure_pending_dir(JOB_ID)) is True
    assert osp.isfile(
        osp.join(home, store_mod.SETTINGS_FILENAME)
    ) is False
    assert osp.isdir(osp.join(home, store_mod.PENDING_DIRNAME)) is False


def test_first_read_migrates_the_legacy_file_and_keeps_it(tmp_path):
    home = str(tmp_path / "home")
    legacy = write_legacy(
        str(tmp_path / "work"), legacy_payload(future_key={"a": 1})
    )
    store = Store(str(tmp_path / "work"), server_dir=home)

    loaded = store.load_server()

    assert loaded.server_url == "http://legacy:8000"
    assert loaded.api_key == TOKEN
    assert loaded.extra.get("future_key") == {"a": 1}
    assert osp.isfile(store.server_path) is True
    assert osp.isfile(legacy) is True
    with open(legacy, "rb") as handle:
        before = handle.read()
    with open(store.server_path, "rb") as handle:
        after = handle.read()
    assert after == before
    assert "future_key" in json.loads(after.decode("utf-8"))


def test_migration_does_not_overwrite_the_new_file(tmp_path):
    home = tmp_path / "home"
    os.makedirs(home, exist_ok=True)
    write_legacy(str(tmp_path / "work"))
    new_path = home / store_mod.SERVER_FILENAME
    new_path.write_text(
        json.dumps({"server_url": "http://new:9000", "api_key": "new"}),
        encoding="utf-8",
    )
    store = Store(str(tmp_path / "work"), server_dir=str(home))

    loaded = store.load_server()

    assert loaded.server_url == "http://new:9000"
    assert loaded.api_key == "new"
    assert json.loads(new_path.read_text(encoding="utf-8"))[
        "server_url"
    ] == "http://new:9000"


def test_a_corrupt_new_file_is_not_restored_from_the_legacy_one(
    tmp_path, caplog
):
    home = tmp_path / "home"
    os.makedirs(home, exist_ok=True)
    write_legacy(str(tmp_path / "work"))
    new_path = home / store_mod.SERVER_FILENAME
    new_path.write_text("{not json", encoding="utf-8")
    store = Store(str(tmp_path / "work"), server_dir=str(home))

    loaded = store.load_server()

    assert loaded.server_url == ""
    assert loaded.api_key == ""
    assert "unreadable" in caplog.text
    assert new_path.read_text(encoding="utf-8") == "{not json"


def test_a_corrupt_legacy_file_is_not_migrated(tmp_path):
    home = str(tmp_path / "home")
    base_dir = tmp_path / "work"
    os.makedirs(base_dir, exist_ok=True)
    legacy = base_dir / store_mod.SERVER_FILENAME
    legacy.write_text("{not json", encoding="utf-8")
    store = Store(str(base_dir), server_dir=home)

    loaded = store.load_server()

    assert loaded.server_url == ""
    assert loaded.api_key == ""
    assert osp.isfile(legacy) is True
    assert osp.isfile(store.server_path) is False
    assert osp.isdir(home) is False


def test_a_non_object_legacy_file_is_not_migrated(tmp_path):
    home = str(tmp_path / "home")
    base_dir = tmp_path / "work"
    os.makedirs(base_dir, exist_ok=True)
    legacy = base_dir / store_mod.SERVER_FILENAME
    legacy.write_text("[1, 2, 3]", encoding="utf-8")
    store = Store(str(base_dir), server_dir=home)

    loaded = store.load_server()

    assert loaded.server_url == ""
    assert legacy.read_text(encoding="utf-8") == "[1, 2, 3]"
    assert osp.isfile(store.server_path) is False


def test_a_blocked_user_directory_falls_back_to_the_legacy_file(
    tmp_path, caplog
):
    home = tmp_path / "home"
    write_legacy(str(tmp_path / "work"))
    block_directory(home)
    store = Store(str(tmp_path / "work"), server_dir=str(home))

    loaded = store.load_server()

    assert loaded.server_url == "http://legacy:8000"
    assert loaded.api_key == TOKEN
    assert osp.isfile(store.legacy_server_path) is True
    assert "could not migrate" in caplog.text


def test_save_falls_back_to_the_ledger_file_when_the_user_dir_is_not_writable(
    tmp_path, caplog
):
    home = tmp_path / "home"
    block_directory(home)
    store = Store(str(tmp_path / "work"), server_dir=str(home))

    saved = store.save_server(
        ServerConfig(server_url="http://fallback:8000", api_key=TOKEN)
    )

    assert "falling back" in caplog.text
    assert osp.isfile(store.legacy_server_path) is True
    with open(store.legacy_server_path, encoding="utf-8") as handle:
        written = json.load(handle)
    assert written["server_url"] == "http://fallback:8000"
    assert written["api_key"] == TOKEN
    assert written["updated_at"] == saved.updated_at
    assert store.load_server().api_key == TOKEN


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_the_server_file_is_private_on_posix(tmp_path):
    home = str(tmp_path / "home")
    store = Store(str(tmp_path / "work-A"), server_dir=home)
    store.save_server(
        ServerConfig(server_url="http://shared:8000", api_key=TOKEN)
    )
    assert stat.S_IMODE(os.stat(store.server_path).st_mode) == 0o600

    write_legacy(str(tmp_path / "work-B"))
    migrated = Store(str(tmp_path / "work-B"), server_dir=home)
    migrated.load_server()

    assert migrated.server_path == store.server_path
    assert stat.S_IMODE(os.stat(migrated.server_path).st_mode) == 0o600


def test_exported_and_imported_documents_never_carry_the_token(tmp_path):
    pipeline = Pipeline(
        config=PipelineConfig(
            dataset_dir=str(tmp_path / "annotations"),
            classes_file=str(tmp_path / "annotations" / "classes.txt"),
            task="detect",
            val_ratio=0.5,
            seed=7,
        )
    )

    document = pipeline.export_config(
        {
            "server_url": "http://server:8000",
            "api_key": TOKEN,
            "token": TOKEN,
            "dataset_dir": str(tmp_path / "annotations"),
            "classes_file": str(tmp_path / "annotations" / "classes.txt"),
            "task": "detect",
            "val_ratio": 0.5,
            "seed": 7,
        }
    )

    assert "api_key" not in document
    assert "token" not in document
    assert TOKEN not in json.dumps(document, ensure_ascii=False)

    imported = pipeline.import_config(
        json.dumps(dict(document, api_key=TOKEN))
    )

    assert "api_key" not in imported
    assert imported["seed"] == 7
    assert imported["val_ratio"] == 0.5


def test_two_windows_with_different_work_directories_see_the_same_server(
    qapp, tmp_path
):
    from PyQt6 import QtWidgets

    from anylabeling.custom.remote_training.ui.dialog import (
        RemoteTrainingDialog,
    )

    class Parent(QtWidgets.QMainWindow):
        """Stand in for the labeling main window."""

    def open_window(base_dir):
        parent = Parent()
        parent._remote_training_dialog = None
        window = RemoteTrainingDialog(
            parent, store=Store(str(base_dir)), client_factory=object
        )
        parent._remote_training_dialog = window
        window._confirm_close = lambda _dialog: True
        window._confirm_action = lambda _title, _text: True
        window.show()
        return window

    first = open_window(tmp_path / "work-A")
    first.config_page.set_values(
        {"server_url": "http://shared:8000", "api_key": TOKEN}
    )
    first.save_server_config()
    first.cancel_workers()
    first.reject()
    qapp.processEvents()

    second = open_window(tmp_path / "work-B")

    assert first.store.server_path == second.store.server_path
    assert second.config_page.values().server_url == "http://shared:8000"
    assert second.config_page.values().api_key == TOKEN
    assert second.server_config.api_key == TOKEN
    for base_dir in ("work-A", "work-B"):
        assert (
            osp.isfile(
                osp.join(
                    str(tmp_path / base_dir), store_mod.SERVER_FILENAME
                )
            )
            is False
        )
    second.cancel_workers()
    second.reject()
    qapp.processEvents()
