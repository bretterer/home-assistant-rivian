"""Tests for the charging switch's availability (issue #301)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.rivian import const, entity, switch

DESCRIPTION = next(d for d in switch.SWITCHES if d.key == "charging_enabled")
T0 = datetime(2026, 10, 13, 18, 0, tzinfo=UTC)


def coordinator(values: dict[str, Any]) -> MagicMock:
    coor = MagicMock()
    coor.get.side_effect = values.get
    coor.data = {key: {"value": value} for key, value in values.items()}
    return coor


@pytest.mark.parametrize(
    ("remote", "state", "available", "on"),
    [
        # Held by a schedule: startable
        ("true", "charging_scheduled", True, False),
        # Just started, or started on plug-in: no longer startable, connecting
        ("false", "charging_connecting", True, True),
        ("false", "charging_active", True, True),
        ("false", "smart_charging_active", True, True),
        # Nothing to start or stop
        ("false", "charging_ready", False, False),
        ("false", "charging_complete", False, False),
    ],
)
def test_availability_follows_charging(
    remote: str, state: str, available: bool, on: bool
) -> None:
    coor = coordinator({"remoteChargingAvailable": remote, "chargerState": state})
    assert DESCRIPTION.available(coor) is available
    assert DESCRIPTION.is_on(coor) is on


def test_charging_status_uses_the_same_states() -> None:
    """The Charging status binary sensor agrees with the switch."""
    binary = next(
        d
        for descriptions in const.BINARY_SENSORS.values()
        for d in descriptions
        if d.key == "charger_state"
    )
    assert set(binary.on_value) == set(const.ACTIVE_CHARGING_STATES)


class Clock:
    def __init__(self) -> None:
        self.now = T0


@pytest.fixture
def clock() -> Any:
    clock = Clock()
    with (
        patch.object(entity.dt_util, "utcnow", lambda: clock.now),
        patch.object(entity, "async_call_later", MagicMock()),
    ):
        yield clock


def make_switch(values: dict[str, Any]) -> switch.RivianSwitchEntity:
    entry = MagicMock()
    entry.options = {}
    sw = switch.RivianSwitchEntity(
        coordinator(values),
        entry,
        DESCRIPTION,
        {"id": "vehicle-1", "vin": "VIN1", "name": "Truck", "model": "R1S"},
    )
    sw.hass = MagicMock()
    return sw


async def test_stays_available_after_its_own_command(clock: Clock) -> None:
    """Between the start and charging connecting, the switch doesn't flicker."""
    sw = make_switch(
        {
            "gearStatus": "park",
            "remoteChargingAvailable": "false",
            "chargerState": "charging_ready",
        }
    )
    assert not sw.available

    with patch.object(DESCRIPTION, "turn_on", AsyncMock()):
        await sw.async_turn_on()
    assert sw.available
    entity.async_call_later.assert_called_once()

    clock.now += timedelta(seconds=29)
    assert sw.available
    clock.now += timedelta(seconds=2)
    assert not sw.available


async def test_settling_never_overrides_park(clock: Clock) -> None:
    sw = make_switch(
        {
            "gearStatus": "drive",
            "remoteChargingAvailable": "false",
            "chargerState": "charging_ready",
        }
    )
    with patch.object(DESCRIPTION, "turn_on", AsyncMock()):
        await sw.async_turn_on()
    assert not sw.available


async def test_failed_command_does_not_settle(clock: Clock) -> None:
    sw = make_switch(
        {
            "gearStatus": "park",
            "remoteChargingAvailable": "false",
            "chargerState": "charging_ready",
        }
    )
    with (
        patch.object(DESCRIPTION, "turn_on", AsyncMock(side_effect=RuntimeError)),
        pytest.raises(RuntimeError),
    ):
        await sw.async_turn_on()
    assert not sw.available


async def test_reported_sequence_from_issue_301(clock: Clock) -> None:
    """The order the vehicle reported a remote start in, from the issue's log."""
    values = {
        "gearStatus": "park",
        "remoteChargingAvailable": "true",
        "chargerState": "charging_scheduled",
    }
    sw = make_switch(values)
    sw.coordinator.get.side_effect = values.get

    with patch.object(DESCRIPTION, "turn_on", AsyncMock()):
        await sw.async_turn_on()  # 09:35:39.527
    for seconds, field, value in (
        (2.067, "remoteChargingAvailable", "false"),  # 09:35:41.594
        (2.071, "chargerState", "charging_connecting"),  # 09:35:41.598
        (7.067, "chargerState", "charging_active"),  # 09:35:46.594
    ):
        clock.now = T0 + timedelta(seconds=seconds)
        values[field] = value
        assert sw.available, f"unavailable after {field} -> {value}"
        assert sw.is_on is (values["chargerState"] != "charging_scheduled")


async def test_reported_plug_in_from_issue_301(clock: Clock) -> None:
    """Plugging in with no schedule, from the issue's second log."""
    values = {
        "gearStatus": "park",
        "remoteChargingAvailable": "false",
        "chargerState": "charging_ready",
    }
    sw = make_switch(values)
    sw.coordinator.get.side_effect = values.get

    # Plugged in, 15:34:15.952: nothing to start or stop yet
    assert not sw.available
    for state in ("charging_connecting", "charging_active"):  # 15:34:20.956, 25.977
        values["chargerState"] = state
        assert sw.available, f"unavailable at {state}"
        assert sw.is_on
