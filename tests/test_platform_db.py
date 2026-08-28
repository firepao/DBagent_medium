import sqlite3
import os
from pathlib import Path

import pytest

from tools import platform_db
from tools.platform_db import backup, prune_backups, restore, verify


def test_backup_verify_and_confirmed_restore(tmp_path):
    source = tmp_path / "platform.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE rules (id INTEGER PRIMARY KEY, name TEXT)")
        connection.execute("INSERT INTO rules(name) VALUES ('v1')")

    archived = backup(source, tmp_path / "backups")
    verify(archived)
    with pytest.raises(ValueError, match="--confirm"):
        restore(archived, tmp_path / "restored.sqlite3", confirmed=False)
    target = tmp_path / "restored.sqlite3"
    connection = sqlite3.connect(target)
    try:
        connection.execute("CREATE TABLE stale_data (value TEXT)")
        connection.execute("INSERT INTO stale_data VALUES ('must disappear')")
        connection.commit()
    finally:
        connection.close()
    restore(archived, target, confirmed=True)
    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT name FROM rules").fetchone()[0] == "v1"
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'stale_data'"
        ).fetchone()[0] == 0


def test_backup_retention_and_restore_drill(tmp_path):
    source = tmp_path / "platform.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE rules (id INTEGER)")
    directory = tmp_path / "backups"
    first = backup(source, directory, keep=1, max_age_days=None)
    second = backup(source, directory, keep=1, max_age_days=None)
    assert second.exists()
    assert not first.exists()
    report = restore(second, tmp_path / "drill.sqlite3", confirmed=False, dry_run=True)
    assert report["verified"] is True
    assert not (tmp_path / "drill.sqlite3").exists()


def test_restore_rolls_back_target_when_atomic_replace_fails(tmp_path, monkeypatch):
    source = tmp_path / "source.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE rules (name TEXT)")
        connection.execute("INSERT INTO rules VALUES ('from-backup')")
    archived = backup(source, tmp_path / "backups")
    target = tmp_path / "platform.sqlite3"
    connection = sqlite3.connect(target)
    try:
        connection.execute("CREATE TABLE rules (name TEXT)")
        connection.execute("INSERT INTO rules VALUES ('original')")
        connection.commit()
    finally:
        connection.close()

    real_replace = os.replace
    calls = []

    def fail_new_target(source_path, target_path):
        calls.append((Path(source_path).name, Path(target_path).name))
        if len(calls) == 2:
            raise OSError("simulated replacement failure")
        return real_replace(source_path, target_path)

    monkeypatch.setattr(platform_db.os, "replace", fail_new_target)
    with pytest.raises(OSError, match="simulated replacement failure"):
        restore(archived, target, confirmed=True)
    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT name FROM rules").fetchone()[0] == "original"
    assert not list(tmp_path.glob(".*.previous"))
    assert not list(tmp_path.glob(".*.restore"))


def test_restore_rolls_back_when_final_integrity_check_fails(tmp_path, monkeypatch):
    source = tmp_path / "source.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE rules (name TEXT)")
        connection.execute("INSERT INTO rules VALUES ('from-backup')")
    archived = backup(source, tmp_path / "backups")
    target = tmp_path / "platform.sqlite3"
    connection = sqlite3.connect(target)
    try:
        connection.execute("CREATE TABLE rules (name TEXT)")
        connection.execute("INSERT INTO rules VALUES ('original')")
        connection.commit()
    finally:
        connection.close()

    real_verify = platform_db.verify
    verified = []

    def fail_target_verification(path):
        verified.append(Path(path).name)
        if Path(path) == target:
            raise RuntimeError("simulated final integrity failure")
        return real_verify(path)

    monkeypatch.setattr(platform_db, "verify", fail_target_verification)
    with pytest.raises(RuntimeError, match="simulated final integrity failure"):
        restore(archived, target, confirmed=True)
    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT name FROM rules").fetchone()[0] == "original"
    assert not list(tmp_path.glob(".*.previous"))
    assert not list(tmp_path.glob(".*.restore"))
