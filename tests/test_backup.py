"""The backup platform checkpoints the analytics database and holds its writes."""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Any

from custom_components.rivian import backup
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
