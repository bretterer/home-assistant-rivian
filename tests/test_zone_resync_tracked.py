"""The debounced zone resync is a tracked task, so unload waits for it."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import custom_components.rivian as integration
from custom_components.rivian.const import DOMAIN
from custom_components.rivian.drive_storage import async_wait_for_analytics_tasks


async def test_zone_resync_runs_as_a_tracked_task(mock_hass: Any) -> None:
    mock_hass.data[DOMAIN] = {}
    store = SimpleNamespace(vin="V1", async_sync_zones=AsyncMock())
    entry = SimpleNamespace(runtime_data=SimpleNamespace(drive_stores={"v": store}))
    mock_hass.config_entries = MagicMock()
    mock_hass.config_entries.async_loaded_entries.return_value = [entry]
    listeners: list[Any] = []
    mock_hass.bus = MagicMock()
    mock_hass.bus.async_listen.side_effect = lambda _event, cb: listeners.append(cb)
    scheduled: list[Any] = []

    def _call_later(_hass: Any, _delay: float, action: Any) -> Any:
        scheduled.append(action)
        return lambda: None

    with (
        patch.object(integration, "async_call_later", _call_later),
        patch.object(integration, "read_zone_states", return_value=[]),
    ):
        integration._async_register_zone_listener(mock_hass)
        listeners[0](SimpleNamespace(data={"entity_id": "zone.home"}))
        # The timer's action is a plain callback that schedules the work
        assert not inspect.iscoroutinefunction(scheduled[0])
        assert scheduled[0](None) is None
        assert await async_wait_for_analytics_tasks(mock_hass) is True
    store.async_sync_zones.assert_awaited_once_with([])
