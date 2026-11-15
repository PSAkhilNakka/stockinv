"""A desktop close must leave a restorable local database and uploads."""

import sqlite3
from zipfile import ZipFile

import pytest


def test_macos_uses_application_support_for_local_data():
    import desktop_launcher as launcher
    from pathlib import Path

    assert launcher.default_data_dir("darwin") == (
        Path.home() / "Library" / "Application Support" / "HarithaEnterprises"
    )


def test_only_one_desktop_instance_can_lock_store(tmp_path):
    import desktop_launcher as launcher

    first = launcher.acquire_lock(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="already open"):
            launcher.acquire_lock(tmp_path)
    finally:
        first.close()
    reopened = launcher.acquire_lock(tmp_path)
    reopened.close()


def test_desktop_storage_and_backup_survive_restart(tmp_path, monkeypatch):
    import desktop_launcher as launcher
    import os

    for name in ("DATABASE_URL", "UPLOAD_DIR", "SECRET_KEY", "APP_HOST"):
        monkeypatch.setenv(name, os.environ.get(name, ""))

    db_path = launcher.configure_data(tmp_path)
    original_key = os.environ["SECRET_KEY"]
    assert db_path.parent == tmp_path
    assert os.environ["UPLOAD_DIR"] == str(tmp_path / "uploads")

    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE saved (value TEXT)")
        db.execute("INSERT INTO saved VALUES ('persisted sale')")
    (tmp_path / "uploads" / "receipt.txt").write_text("saved attachment", encoding="utf-8")

    archive = launcher.backup_data(tmp_path, db_path)
    with ZipFile(archive) as snapshot:
        assert "store.sqlite3" in snapshot.namelist()
        assert snapshot.read("uploads/receipt.txt") == b"saved attachment"
        restored = tmp_path / "restored.sqlite3"
        restored.write_bytes(snapshot.read("store.sqlite3"))
    with sqlite3.connect(restored) as db:
        assert db.execute("SELECT value FROM saved").fetchone()[0] == "persisted sale"

    monkeypatch.delenv("SECRET_KEY")
    assert launcher.configure_data(tmp_path) == db_path
    assert os.environ["SECRET_KEY"] == original_key
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT value FROM saved").fetchone()[0] == "persisted sale"
