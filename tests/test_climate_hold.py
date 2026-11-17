"""Tests for the climate hold switch and duration."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.rivian import coordinator, number, switch
from custom_components.rivian.const import ATTR_COORDINATOR, ATTR_VEHICLE, DOMAIN
from custom_components.rivian.entity import RivianVehicleControlEntity

DURATION = number.CLIMATE_HOLD_DURATION_NUMBER
SWITCH = next(d for d in switch.SWITCHES if d.key == "climate_hold")


def test_duration_in_minutes() -> None:
    """The vehicle's duration in seconds is shown in minutes, unset as unknown."""
    entity = object.__new__(number.RivianClimateHoldDurationEntity)
    entity.entity_description = DURATION
    entity.coordinator = MagicMock()
    for seconds, minutes in ((28800, 480), (0, None)):
        entity.coordinator.get.side_effect = {"climateHoldDuration": seconds}.get
        assert entity.native_value == minutes


def test_switch_follows_status() -> None:
    """The switch is on only while a hold is running."""
    coor = MagicMock()
    for status, on in (("on", True), ("off", False), ("unavailable", False)):
        coor.get.side_effect = {"climateHoldStatus": status}.get
        assert SWITCH.is_on(coor) is on


async def test_duration_without_a_paired_phone() -> None:
    """Every climate hold vehicle gets the duration, paired phone or not."""
    features = {
        "keyless": {"supported_features": ["CLM_HOLD"]},
        "paired": {"supported_features": ["CLM_HOLD"], "phone_identity_id": "id"},
        "no_hold": {"supported_features": [], "phone_identity_id": "id"},
    }
    vehicles = {
        v: {"id": v, "vin": v, "name": v, "model": "R1S", **f}
        for v, f in features.items()
    }
    coordinators = {v: MagicMock(vehicle_id=v) for v in vehicles}
    hass = MagicMock()
    hass.data = {
        DOMAIN: {
            "entry": {
                ATTR_VEHICLE: vehicles,
                ATTR_COORDINATOR: {ATTR_VEHICLE: coordinators},
            }
        }
    }
    added: list[Any] = []
    with patch.object(number, "RivianChargingScheduleAmperageEntity", MagicMock()):
        await number.async_setup_entry(hass, MagicMock(entry_id="entry"), added.extend)
    durations = [e for e in added if e.entity_description is DURATION]
    assert {e.coordinator.vehicle_id for e in durations} == {"keyless", "paired"}
    # Not a control entity: those look up the paired phone when added
    assert not any(isinstance(e, RivianVehicleControlEntity) for e in durations)


def test_duration_available_without_a_key() -> None:
    """The duration is available whenever the vehicle reports one."""
    entity = object.__new__(number.RivianClimateHoldDurationEntity)
    entity.entity_description = DURATION
    entity._available = True
    entity.coordinator = MagicMock()
    entity.coordinator.get.side_effect = {"climateHoldDuration": 7200}.get
    assert entity.available
    assert entity.native_value == 120


class TranslatedError(Exception):
    """Stands in for Home Assistant's errors, keeping their translation."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__()
        self.translation_key = kwargs.get("translation_key")


@pytest.mark.parametrize(
    ("result", "key"),
    [
        ({"return_value": False}, "climate_hold_duration_rejected"),
        (
            {"side_effect": coordinator.RivianApiException("bad value")},
            "climate_hold_duration_failed",
        ),
    ],
)
async def test_duration_errors_are_translated(result: dict[str, Any], key: str) -> None:
    """A refused duration and an API error both raise a translated error."""
    coor = object.__new__(coordinator.VehicleCoordinator)
    coor.vehicle_id = "vehicle"
    coor.api = MagicMock(set_climate_hold_duration=AsyncMock(**result))
    with (
        patch.object(coordinator, "HomeAssistantError", TranslatedError),
        pytest.raises(TranslatedError) as err,
    ):
        await coor.set_climate_hold_duration(120)
    assert err.value.translation_key == key
