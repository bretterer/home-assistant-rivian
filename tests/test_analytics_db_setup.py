"""Setup-time behavior of AnalyticsDatabase: lock safety and auto_vacuum."""

from __future__ import annotations

import glob
import os
import sqlite3
from typing import Any

import pytest

from custom_components.rivian import analytics_db as analytics_db_module
from custom_components.rivian.analytics_db import AnalyticsDatabase


def _make(hass: Any, path: str) -> AnalyticsDatabase:
    db = AnalyticsDatabase(hass, db_path=path)
    db.setup()
    return db


def _old_style(path: str) -> None:
    """Turn a database into one created before auto_vacuum was honored."""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA auto_vacuum = NONE")
    conn.execute("VACUUM")
    assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 0
    conn.close()


class TestLockedDatabaseIsKept:
    """A locked/busy database must fail setup, never be moved aside."""

    def test_locked_database_raises_and_keeps_files(
        self, mock_hass: Any, analytics_db_path: str, monkeypatch: Any
    ) -> None:
        """An exclusive lock held elsewhere makes setup raise, with nothing deleted."""
        db = _make(mock_hass, analytics_db_path)
        db.close()
        wal = f"{analytics_db_path}-wal"
        # A -wal sibling standing in for writes not yet merged into the file
        with open(wal, "ab"):
            pass
        monkeypatch.setattr(analytics_db_module, "BUSY_TIMEOUT_MS", 100)
        holder = sqlite3.connect(analytics_db_path, isolation_level=None)
        holder.execute("BEGIN EXCLUSIVE")
        try:
            db2 = AnalyticsDatabase(mock_hass, db_path=analytics_db_path)
            with pytest.raises(sqlite3.OperationalError):
                db2.setup()
            assert db2._conn is None
            assert os.path.exists(analytics_db_path)
            assert not glob.glob(f"{analytics_db_path}.corrupt-*")
            assert os.path.exists(wal)
            assert not db2.was_repaired
        finally:
            holder.execute("ROLLBACK")
            holder.close()
        # Lock released: the same file opens normally
        db3 = _make(mock_hass, analytics_db_path)
        assert not db3.was_repaired
        db3.close()

    def test_garbage_file_is_quarantined(
        self, mock_hass: Any, analytics_db_path: str
    ) -> None:
        """A file that isn't SQLite at all is still moved aside and recreated."""
        with open(analytics_db_path, "wb") as fh:
            fh.write(b"this is not a database" * 200)
        db = _make(mock_hass, analytics_db_path)
        try:
            assert db.was_repaired
            assert len(glob.glob(f"{analytics_db_path}.corrupt-*")) == 1
        finally:
            db.close()

    def test_failed_quick_check_is_quarantined(
        self, mock_hass: Any, analytics_db_path: str, monkeypatch: Any
    ) -> None:
        """A quick_check that reports damage still quarantines the file."""
        db = _make(mock_hass, analytics_db_path)
        db.close()
        monkeypatch.setattr(AnalyticsDatabase, "_needs_quick_check", lambda self: True)
        calls = {"n": 0}

        def fake_ok(conn: sqlite3.Connection) -> bool:
            calls["n"] += 1
            return calls["n"] > 1

        monkeypatch.setattr(AnalyticsDatabase, "_quick_check_ok", staticmethod(fake_ok))
        db2 = _make(mock_hass, analytics_db_path)
        try:
            assert db2.was_repaired
            assert len(glob.glob(f"{analytics_db_path}.corrupt-*")) == 1
        finally:
            db2.close()


class TestAutoVacuum:
    """auto_vacuum=INCREMENTAL must really take effect."""

    def test_new_database_is_incremental(
        self, mock_hass: Any, analytics_db_path: str
    ) -> None:
        """A fresh database reads back auto_vacuum 2 after setup."""
        db = _make(mock_hass, analytics_db_path)
        try:
            with db._lock:
                assert db._conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
        finally:
            db.close()

    def test_old_database_is_converted_once(
        self, mock_hass: Any, analytics_db_path: str, caplog: Any
    ) -> None:
        """A database made with auto_vacuum 0 is converted by a one-time VACUUM."""
        db = _make(mock_hass, analytics_db_path)
        db.close()
        _old_style(analytics_db_path)
        caplog.set_level("INFO")
        db = _make(mock_hass, analytics_db_path)
        try:
            with db._lock:
                assert db._conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
        finally:
            db.close()
        assert any("auto_vacuum" in r.message for r in caplog.records)
        caplog.clear()
        _make(mock_hass, analytics_db_path).close()
        assert not any("auto_vacuum" in r.message for r in caplog.records)

    def test_vacuum_failure_does_not_fail_setup(
        self, mock_hass: Any, analytics_db_path: str, caplog: Any, monkeypatch: Any
    ) -> None:
        """A failing conversion logs a warning and setup still succeeds."""
        db = _make(mock_hass, analytics_db_path)
        db.close()
        _old_style(analytics_db_path)
        real_connect = AnalyticsDatabase._connect
        monkeypatch.setattr(
            AnalyticsDatabase,
            "_connect",
            staticmethod(lambda path: _VacuumFails(real_connect(path))),
        )
        db = _make(mock_hass, analytics_db_path)
        try:
            assert db._conn is not None
        finally:
            db.close()
        assert any(
            r.levelname == "WARNING" and "auto_vacuum" in r.message
            for r in caplog.records
        )

    def test_incremental_vacuum_shrinks_file(
        self, mock_hass: Any, analytics_db_path: str
    ) -> None:
        """After deleting many rows, the prune's vacuum returns pages to the OS."""
        db = _make(mock_hass, analytics_db_path)
        try:
            with db._lock:
                db._conn.execute("CREATE TABLE junk (id INTEGER PRIMARY KEY, b BLOB)")
                db._conn.execute("BEGIN")
                for _ in range(400):
                    db._conn.execute("INSERT INTO junk (b) VALUES (?)", (b"x" * 4000,))
                db._conn.execute("COMMIT")
                db._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            big = os.path.getsize(analytics_db_path)
            with db._lock:
                db._conn.execute("DELETE FROM junk")
                db._incremental_vacuum()
                db._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            # 200 pages of 4 KiB per call; the junk is ~1.6 MB, so one call
            # must drop at least 700 KB
            assert os.path.getsize(analytics_db_path) < big - 700_000
        finally:
            db.close()


class _VacuumFails:
    """Connection proxy whose VACUUM raises, like a locked or full database."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._c = conn

    def execute(self, sql: str, *args: Any) -> Any:
        if sql.strip().upper() == "VACUUM":
            raise sqlite3.OperationalError("database or disk is full")
        return self._c.execute(sql, *args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._c, name)
