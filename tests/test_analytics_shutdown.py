"""Unload, removal and shutdown stop analytics tasks and close the DB once.

Background tasks (backfills, recomputes) are cancelled; must-finish ones (the
statistics rewrite) are waited for, up to a cap.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import custom_components.rivian as integration
from custom_components.rivian.analytics_db import AnalyticsDatabase
from custom_components.rivian.const import ATTR_ANALYTICS_DB, DOMAIN
from custom_components.rivian.drive_storage import (
    async_track_analytics_task,
    async_wait_for_analytics_tasks,
)
from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE
from homeassistant.exceptions import HomeAssistantError


class _Bus:
    """Records listen-once listeners like the real bus (removal after firing errors)."""

    def __init__(self) -> None:
        self.listeners: dict[str, Any] = {}
        self.removed = 0

    def async_listen_once(self, event: str, cb: Any) -> Any:
        self.listeners[event] = cb

        def _remove() -> None:
            self.removed += 1
            if self.listeners.pop(event, None) is None:
                raise ValueError("Unable to remove unknown job listener")

        return _remove

    async def fire(self, event: str) -> None:
        cb = self.listeners.pop(event)
        await cb(SimpleNamespace())

    def async_fire(self, *_a: Any, **_k: Any) -> None:
        return None


def _hass(mock_hass: Any, tmp_path: Any) -> Any:
    mock_hass.bus = _Bus()
    mock_hass.data[DOMAIN] = {}
    mock_hass.config.path = lambda *p: str(tmp_path.joinpath(*p))
    return mock_hass


async def _open_db(hass: Any) -> AnalyticsDatabase:
    return await integration._async_get_or_create_analytics_db(hass)


def _slow_task(
    hass: Any, order: list[str], *, background: bool = True
) -> tuple[asyncio.Event, asyncio.Task[Any]]:
    release = asyncio.Event()

    async def _work() -> None:
        await release.wait()
        order.append("task done")

    task = async_track_analytics_task(hass, _work(), "slow", background=background)
    return release, task


def _unloadable(
    hass: Any, trackers: dict[str, Any] | None = None, *, others: Any = ()
) -> SimpleNamespace:
    entry = SimpleNamespace(
        entry_id="e1",
        runtime_data=SimpleNamespace(
            api=SimpleNamespace(close=AsyncMock()),
            vehicle_coordinators={},
            drive_trackers=trackers or {},
        ),
    )
    hass.config_entries = MagicMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    # Home Assistant doesn't list the unloading entry, but a stale listing of it
    # must not keep the database open either
    hass.config_entries.async_loaded_entries = lambda _domain: [entry, *others]
    return entry


def _record_close(db: AnalyticsDatabase, order: list[str]) -> None:
    real_close = db.close
    db.close = lambda: (order.append("close"), real_close())[1]  # type: ignore[method-assign]


async def test_unload_waits_for_must_finish_task_before_closing(
    mock_hass: Any, tmp_path: Any
) -> None:
    hass = _hass(mock_hass, tmp_path)
    db = await _open_db(hass)
    order: list[str] = []
    _record_close(db, order)
    release, _task = _slow_task(hass, order, background=False)
    entry = _unloadable(hass)

    unload = asyncio.ensure_future(integration.async_unload_entry(hass, entry))
    await asyncio.sleep(0.05)
    assert not unload.done()
    assert order == []
    release.set()
    assert await unload is True
    assert order == ["task done", "close"]
    # The FINAL_WRITE listener was removed with the DB, never fired
    assert hass.bus.removed == 1
    assert EVENT_HOMEASSISTANT_FINAL_WRITE not in hass.bus.listeners


async def test_unload_keeps_the_database_while_another_entry_is_loaded(
    mock_hass: Any, tmp_path: Any
) -> None:
    hass = _hass(mock_hass, tmp_path)
    db = await _open_db(hass)
    order: list[str] = []
    _record_close(db, order)
    entry = _unloadable(hass, others=[SimpleNamespace(entry_id="e2")])

    assert await integration.async_unload_entry(hass, entry) is True

    assert order == []
    assert hass.data[DOMAIN][ATTR_ANALYTICS_DB] is db
    entry.runtime_data.api.close.assert_awaited_once()
    # The last one out then closes it
    last = _unloadable(hass)
    assert await integration.async_unload_entry(hass, last) is True
    assert order == ["close"]


async def test_unload_cancels_background_task_without_waiting(
    mock_hass: Any, tmp_path: Any
) -> None:
    hass = _hass(mock_hass, tmp_path)
    db = await _open_db(hass)
    order: list[str] = []
    _record_close(db, order)
    _release, task = _slow_task(hass, order)  # never released
    entry = _unloadable(hass)

    assert await asyncio.wait_for(integration.async_unload_entry(hass, entry), 1)
    assert task.cancelled()
    assert order == ["close"]


async def test_unload_caps_wait_for_must_finish_task(
    mock_hass: Any, tmp_path: Any, caplog: Any
) -> None:
    hass = _hass(mock_hass, tmp_path)
    db = await _open_db(hass)
    order: list[str] = []
    _record_close(db, order)
    release, task = _slow_task(hass, order, background=False)
    entry = _unloadable(hass)

    with patch.object(integration, "CLOSE_TASK_TIMEOUT_SECONDS", 0.05):
        assert await asyncio.wait_for(integration.async_unload_entry(hass, entry), 1)
    # Closed after the cap; the must-finish task is never cancelled
    assert order == ["close"]
    assert not task.done()
    assert "closing the database anyway" in caplog.text
    release.set()
    await task
    assert order == ["close", "task done"]


async def test_unload_during_backfill_stops_it(mock_hass: Any, tmp_path: Any) -> None:
    hass = _hass(mock_hass, tmp_path)
    db = await _open_db(hass)
    order: list[str] = []
    _record_close(db, order)
    started = asyncio.Event()

    async def _backfill(**_kw: Any) -> None:
        started.set()
        await asyncio.Event().wait()  # a backfill over a year of history
        order.append("backfill done")

    tracker = SimpleNamespace(
        vin="V1",
        store=MagicMock(),
        async_reload_history=AsyncMock(),
        async_unload=AsyncMock(),
    )
    entry = _unloadable(hass, {"v": tracker})
    with (
        patch.object(integration, "async_backfill_from_recorder", _backfill),
        patch.object(integration, "_analytics_trackers", return_value={"V1": tracker}),
    ):
        call = SimpleNamespace(data={"dry_run": True}, service="backfill_drive_history")
        handler = integration._tracked_service(hass, integration._async_handle_backfill)
        service = asyncio.ensure_future(handler(call))
        await started.wait()
        assert await asyncio.wait_for(integration.async_unload_entry(hass, entry), 1)
        with pytest.raises(HomeAssistantError, match="was unloaded"):
            await service
    assert order == ["close"]


async def test_cancelling_service_call_keeps_the_work_running(
    mock_hass: Any, tmp_path: Any
) -> None:
    hass = _hass(mock_hass, tmp_path)
    release = asyncio.Event()
    order: list[str] = []

    async def _handler(_hass: Any, _call: Any) -> None:
        await release.wait()
        order.append("work done")

    service = asyncio.ensure_future(
        integration._tracked_service(hass, _handler)(SimpleNamespace(service="x"))
    )
    await asyncio.sleep(0.01)
    service.cancel()
    with pytest.raises(asyncio.CancelledError):
        await service
    release.set()
    assert await async_wait_for_analytics_tasks(hass, timeout=1) is True
    assert order == ["work done"]


async def test_running_backfill_is_waited_for(mock_hass: Any, tmp_path: Any) -> None:
    hass = _hass(mock_hass, tmp_path)
    await _open_db(hass)
    release = asyncio.Event()
    finished: list[str] = []

    async def _backfill(**_kw: Any) -> None:
        await release.wait()
        finished.append("backfill")

    tracker = SimpleNamespace(
        vin="V1", store=MagicMock(), async_reload_history=AsyncMock()
    )
    with (
        patch.object(integration, "async_backfill_from_recorder", _backfill),
        patch.object(integration, "_analytics_trackers", return_value={"V1": tracker}),
    ):
        call = SimpleNamespace(data={"dry_run": True}, service="backfill_drive_history")
        handler = integration._tracked_service(hass, integration._async_handle_backfill)
        service = asyncio.ensure_future(handler(call))
        await asyncio.sleep(0.05)
        waiter = asyncio.ensure_future(async_wait_for_analytics_tasks(hass))
        await asyncio.sleep(0.05)
        assert not waiter.done()
        release.set()
        assert await waiter is True
        await service
    assert finished == ["backfill"]


async def test_wait_timeout_does_not_cancel(mock_hass: Any, tmp_path: Any) -> None:
    hass = _hass(mock_hass, tmp_path)
    order: list[str] = []
    release, _task = _slow_task(hass, order)
    assert await async_wait_for_analytics_tasks(hass, timeout=0.05) is False
    release.set()
    assert await async_wait_for_analytics_tasks(hass, timeout=5) is True
    assert order == ["task done"]


async def test_final_write_closes_db_and_next_start_skips_quick_check(
    mock_hass: Any, tmp_path: Any
) -> None:
    hass = _hass(mock_hass, tmp_path)
    db = await _open_db(hass)
    order: list[str] = []
    release, _task = _slow_task(hass, order, background=False)
    release.set()

    await hass.bus.fire(EVENT_HOMEASSISTANT_FINAL_WRITE)

    assert order == ["task done"]
    assert ATTR_ANALYTICS_DB not in hass.data[DOMAIN]
    assert db._conn is None
    # Unload afterwards (no listener left to remove) must not error
    integration._remove_final_write_listener(hass)
    assert hass.bus.removed == 0

    reopened = AnalyticsDatabase(hass)
    try:
        with patch.object(
            AnalyticsDatabase, "_quick_check_ok", return_value=True
        ) as check:
            reopened.setup()
        check.assert_not_called()
        assert reopened.get_meta("clean_shutdown") == "0"  # running again
    finally:
        reopened.close()
