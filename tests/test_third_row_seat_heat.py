"""Tests for the R1S's 3rd row seat heat selects."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.rivian import select
from custom_components.rivian.const import ATTR_COORDINATOR, ATTR_VEHICLE, DOMAIN

THIRD_ROW = {"seat_third_row_left_heat", "seat_third_row_right_heat"}


async def keys_for(model: str) -> set[str]:
    """Select keys created for a paired vehicle of a model."""
    vehicle = {"id": "id", "vin": "VIN", "name": "", "model": model}
    vehicle["phone_identity_id"] = "phone"
    coordinator = MagicMock()
    hass = MagicMock()
    entry = MagicMock(entry_id="entry")
    hass.data = {
        DOMAIN: {
            "entry": {
                ATTR_VEHICLE: {"id": vehicle},
                ATTR_COORDINATOR: {ATTR_VEHICLE: {"id": coordinator}},
            }
        }
    }
    added: list[Any] = []
    await select.async_setup_entry(hass, entry, added.extend)
    return {entity.entity_description.key for entity in added}


async def test_r1s_gets_third_row_selects() -> None:
    keys = await keys_for("R1S")
    assert keys >= THIRD_ROW
    assert "seat_front_left_heat" in keys


@pytest.mark.parametrize("model", ["R1T", "R2"])
async def test_other_models_do_not(model: str) -> None:
    keys = await keys_for(model)
    assert keys.isdisjoint(THIRD_ROW)
    assert "seat_front_left_heat" in keys


@pytest.mark.parametrize(
    ("key", "command"),
    [
        ("seat_third_row_left_heat", "CABIN_HVAC_3RD_ROW_REAR_LEFT_SEAT_HEAT"),
        ("seat_third_row_right_heat", "CABIN_HVAC_3RD_ROW_REAR_RIGHT_SEAT_HEAT"),
    ],
)
async def test_select_sends_level(key: str, command: str) -> None:
    """The vehicle accepts the 3RD_ROW_REAR commands, not THIRD_ROW."""
    description = next(d for d in select.SELECTS[("R1S",)] if d.key == key)
    coordinator = MagicMock(send_vehicle_command=AsyncMock())
    with patch.object(select, "VehicleCommand") as commands:
        await description.select(coordinator, select.LEVEL_MAP["level_2"])
    coordinator.send_vehicle_command.assert_awaited_once_with(
        command=getattr(commands, command), params={"level": 3}
    )
