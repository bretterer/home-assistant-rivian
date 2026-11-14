"""The backup platform checkpoints the analytics database and holds its writes."""

from __future__ import annotations

import asyncio
from datetime import UTC
import os
import threading
import time
from typing import Any

import pytest

from custom_components.rivian import analytics_db, backup
from custom_components.rivian.analytics_db import AnalyticsDatabase
from custom_components.rivian.const import ATTR_ANALYTICS_DB, DOMAIN


async def test_backup_checkpoints_and_holds_writes(
    mock_hass: Any, analytics_db_path: str
) -> None:
    db = AnalyticsDatabase(mock_hass, db_path=analytics_db_path)
    db.setup()
    mock_hass.data[DOMAIN] = {ATTR_ANALYTICS_DB: db}
    try:
        db.set_meta("before_backup", "1")
        await backup.async_pre_backup(mock_hass)

        # The WAL was checkpointed into the main file
        wal = f"{analytics_db_path}-wal"
        assert not os.path.exists(wal) or os.path.getsize(wal) == 0

        # A write waits for the backup to finish
        written = threading.Event()

        def writer() -> None:
            db.set_meta("during_backup", "1")
            written.set()

        thread = threading.Thread(target=writer)
        thread.start()
        await asyncio.sleep(0.2)
        assert not written.is_set()

        await backup.async_post_backup(mock_hass)
        assert written.wait(5)
        thread.join(5)
        assert db.get_meta("during_backup") == "1"
    finally:
        db.close()


async def test_backup_without_analytics_does_nothing(mock_hass: Any) -> None:
    mock_hass.data[DOMAIN] = {}
    await backup.async_pre_backup(mock_hass)
    await backup.async_post_backup(mock_hass)


@pytest.fixture
def held_db(mock_hass: Any, analytics_db_path: str, monkeypatch: pytest.MonkeyPatch):
    """An open database with a short backup-hold deadline."""
    monkeypatch.setattr(analytics_db, "BACKUP_HOLD_MAX_S", 1.0)
    db = AnalyticsDatabase(mock_hass, db_path=analytics_db_path)
    db.setup()
    db.set_meta("k", "0")
    yield db
    db.release_after_backup()
    db.close()


def _start(fn: Any, *args: Any) -> threading.Thread:
    thread = threading.Thread(target=fn, args=args)
    thread.start()
    return thread


def test_hold_blocks_reads_and_each_write(held_db: AnalyticsDatabase) -> None:
    db = held_db
    db.hold_for_backup()  # no post_backup
    writer = _start(db.set_meta, "k", "1")
    time.sleep(0.1)
    start = time.monotonic()
    db.get_meta("k")
    assert time.monotonic() - start < 0.5
    writer.join()
    start = time.monotonic()
    db.set_meta("k", "2")
    db.set_meta("k", "3")
    assert time.monotonic() - start < 0.5
    assert db.get_meta("k") == "3"


def test_reads_proceed_while_writer_waits(held_db: AnalyticsDatabase) -> None:
    db = held_db
    db.hold_for_backup()
    done = threading.Event()
    writer = _start(lambda: (db.set_meta("k", "1"), done.set()))
    time.sleep(0.1)
    assert not done.is_set()
    start = time.monotonic()
    for _ in range(20):
        assert db.get_meta("k") == "0"
    assert time.monotonic() - start < 0.5
    assert not done.is_set()
    db.release_after_backup()
    writer.join(5)
    assert db.get_meta("k") == "1"


def test_release_lets_waiting_writer_proceed_promptly(
    held_db: AnalyticsDatabase,
) -> None:
    db = held_db
    db.hold_for_backup()
    writer = _start(db.set_meta, "k", "1")
    time.sleep(0.1)
    start = time.monotonic()
    db.release_after_backup()
    writer.join(5)
    assert time.monotonic() - start < 0.5
    assert db.get_meta("k") == "1"


def test_close_does_not_wait_for_hold(held_db: AnalyticsDatabase) -> None:
    db = held_db
    db.hold_for_backup()
    start = time.monotonic()
    db.close()
    assert time.monotonic() - start < 0.5


def test_release_without_hold_is_safe(held_db: AnalyticsDatabase) -> None:
    held_db.release_after_backup()
    held_db.release_after_backup()
    start = time.monotonic()
    held_db.set_meta("k", "1")
    assert time.monotonic() - start < 0.5


def test_write_in_flight_finishes_before_checkpoint(
    held_db: AnalyticsDatabase,
) -> None:
    db = held_db
    inside = threading.Event()
    proceed = threading.Event()
    order: list[str] = []

    def slow_write() -> None:
        with db._writing():
            inside.set()
            proceed.wait(5)
            db.set_meta("k", "inflight")  # nested write must not deadlock
            order.append("write")

    writer = _start(slow_write)
    assert inside.wait(5)
    holder = _start(lambda: (db.hold_for_backup(), order.append("hold")))
    time.sleep(0.1)
    assert order == []
    proceed.set()
    writer.join(5)
    holder.join(5)
    assert order == ["write", "hold"]
    assert db.get_meta("k") == "inflight"


@pytest.mark.parametrize(
    "write",
    [
        lambda db: db.delete_drives("V1", ["d1"]),
        lambda db: db.save_cached_roads("key", []),
        lambda db: db.save_track_fill("V1", "d1", 1.0, [], "none"),
        lambda db: db.rebuild_heat("V1", UTC),
        lambda db: db.create_place("real", 43.7, -116.4, "Home"),
        lambda db: db.rebuild_places("real"),
        lambda db: db.rebuild_routes("real"),
        lambda db: db.sync_zones("real", []),
    ],
    ids=[
        "delete_drives",
        "save_cached_roads",
        "save_track_fill",
        "rebuild_heat",
        "create_place",
        "rebuild_places",
        "rebuild_routes",
        "sync_zones",
    ],
)
def test_drive_map_writes_wait_for_the_hold(
    held_db: AnalyticsDatabase, write: Any
) -> None:
    """Writes added with the Drives tab also wait out a backup hold."""
    db = held_db
    db.hold_for_backup()
    done = threading.Event()
    errors: list[Exception] = []

    def _run() -> None:
        try:
            write(db)
        except Exception as err:  # noqa: BLE001
            errors.append(err)
        done.set()

    writer = _start(_run)
    time.sleep(0.2)
    assert not done.is_set()
    db.release_after_backup()
    writer.join(5)
    assert done.is_set()
    assert not errors, errors
