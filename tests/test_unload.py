"""Tests for unloading the integration."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from custom_components import rivian
from custom_components.rivian.const import (
    ATTR_API,
    ATTR_COORDINATOR,
    ATTR_VEHICLE,
    DOMAIN,
)


async def test_vehicles_stop_before_the_client_closes() -> None:
    """Background work, such as a precondition cleanup, doesn't use a closed client."""
    order: list[str] = []
    vehicle = SimpleNamespace(
        async_shutdown=AsyncMock(side_effect=lambda: order.append("vehicle"))
    )
    api = SimpleNamespace(close=AsyncMock(side_effect=lambda: order.append("api")))
    entry = SimpleNamespace(entry_id="entry")
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_unload_platforms=AsyncMock(return_value=True)
        ),
        data={
            DOMAIN: {
                "entry": {
                    ATTR_API: api,
                    ATTR_COORDINATOR: {ATTR_VEHICLE: {"V": vehicle}},
                }
            }
        },
    )

    assert await rivian.async_unload_entry(hass, entry)

    assert order == ["vehicle", "api"]
    assert "entry" not in hass.data[DOMAIN]
