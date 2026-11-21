"""Analytics starts after the last step that can raise ConfigEntryNotReady."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import custom_components.rivian as integration
from custom_components.rivian.const import CONF_DRIVE_ANALYTICS, CONF_WEATHER_LOOKUPS
from custom_components.rivian.weather import OpenMeteoWeatherClient
from homeassistant.exceptions import ConfigEntryNotReady


def _patched(
    wallbox_error: Exception | None,
    setup_analytics: AsyncMock,
    forward: AsyncMock | None = None,
) -> Any:
    user = MagicMock()
    user.async_config_entry_first_refresh = AsyncMock()
    user.get_vehicles.return_value = {}
    wallbox = MagicMock()
    wallbox.async_config_entry_first_refresh = AsyncMock(side_effect=wallbox_error)
    client = MagicMock()
    client.create_csrf_token = AsyncMock()
    return (
        patch.object(integration, "get_rivian_api_from_entry", return_value=client),
        patch.object(integration, "UserCoordinator", return_value=user),
        patch.object(integration, "WallboxCoordinator", return_value=wallbox),
        patch.object(integration, "_async_setup_analytics", setup_analytics),
        patch.object(integration, "async_register_websocket_api"),
    )


async def test_failed_wallbox_refresh_never_starts_analytics(mock_hass: Any) -> None:
    analytics = AsyncMock()
    entry = MagicMock()
    entry.options = {CONF_DRIVE_ANALYTICS: True}
    ps = _patched(ConfigEntryNotReady("wallbox"), analytics)
    for p in ps:
        p.start()
    try:
        with pytest.raises(ConfigEntryNotReady):
            await integration.async_setup_entry(mock_hass, entry)
    finally:
        for p in ps:
            p.stop()
    analytics.assert_not_called()


async def test_failed_platform_forward_unloads_the_trackers(mock_hass: Any) -> None:
    forward = AsyncMock(side_effect=RuntimeError("platform setup failed"))
    trackers: dict[str, Any] = {"v": SimpleNamespace(vin="V", async_unload=AsyncMock())}
    analytics = AsyncMock(return_value=({"v": object()}, trackers))
    entry = MagicMock()
    entry.options = {CONF_DRIVE_ANALYTICS: True}
    entry.entry_id = "e1"
    ps = _patched(None, analytics)
    mock_hass.config_entries = SimpleNamespace(async_forward_entry_setups=forward)
    for p in ps:
        p.start()
    try:
        with pytest.raises(RuntimeError):
            await integration.async_setup_entry(mock_hass, entry)
    finally:
        for p in ps:
            p.stop()
    trackers["v"].async_unload.assert_awaited_once()


@pytest.mark.parametrize("weather", [False, True])
async def test_analytics_gets_the_entrys_weather_client(
    mock_hass: Any, weather: bool
) -> None:
    """Drive analytics and the Outside temperature sensor share one client."""
    analytics = AsyncMock(return_value=({}, {}))
    entry = MagicMock()
    entry.options = {CONF_DRIVE_ANALYTICS: True, CONF_WEATHER_LOOKUPS: weather}
    entry.entry_id = "e1"
    ps = _patched(None, analytics)
    mock_hass.config_entries = SimpleNamespace(async_forward_entry_setups=AsyncMock())
    for p in ps:
        p.start()
    try:
        await integration.async_setup_entry(mock_hass, entry)
    finally:
        for p in ps:
            p.stop()
    client = entry.runtime_data.weather_client
    assert isinstance(client, OpenMeteoWeatherClient)
    # Off sends nothing, for the drives too
    assert client.enabled is weather
    assert analytics.call_args.args[4] is client
