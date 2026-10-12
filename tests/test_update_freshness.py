"""Tests for the vehicle update freshness tracking and its entities."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from custom_components.rivian import binary_sensor, coordinator, sensor

T0 = datetime(2026, 10, 12, 18, 0, tzinfo=UTC)
VEHICLE = {"id": "vehicle-1", "vin": "VIN1", "name": "Truck", "model": "R1T"}


class Clock:
    """A controllable utcnow."""

    def __init__(self) -> None:
        self.now = T0

    def advance(self, minutes: float) -> None:
        self.now += timedelta(minutes=minutes)


@pytest.fixture
def clock() -> Any:
    clock = Clock()
    with patch.object(coordinator.dt_util, "utcnow", lambda: clock.now):
        yield clock


@pytest.fixture
def coor(clock: Clock) -> coordinator.VehicleCoordinator:
    """A vehicle coordinator that records each state its listeners see."""
    coor = coordinator.VehicleCoordinator(
        hass=MagicMock(), config_entry=MagicMock(), client=MagicMock(), vehicle_id="V"
    )
    coor.data = {}
    coor.seen_stale: list[bool] = []

    def set_data(data: dict[str, Any]) -> None:
        coor.data = data
        coor.seen_stale.append(coor.updates_stale)

    coor.async_set_updated_data = set_data
    coor.charging_coordinator = MagicMock()
    return coor


def parallax(coor: coordinator.VehicleCoordinator, sent: datetime, km: int) -> None:
    """Deliver a Parallax message the vehicle sent at `sent`."""
    message = {
        "payload": {
            "data": {
                "parallaxMessages": {
                    "rvm": "dynamics.vehicle.range",
                    "payload": "",
                    "timestamp": sent.timestamp() * 1000,
                }
            }
        }
    }
    decoded = {"timestamp": sent, "distanceToEmpty": km}
    with patch.object(coordinator, "decode_parallax_message", return_value=decoded):
        coor._process_parallax_data(message)


def graphql(coor: coordinator.VehicleCoordinator, timestamp: str) -> None:
    """Deliver a GraphQL vehicle state message."""
    state = {"activeDriverName": {"value": "Driver", "timeStamp": timestamp}}
    coor._process_new_data({"payload": {"data": {"vehicleState": state}}})


def test_stale_before_any_message(coor: coordinator.VehicleCoordinator) -> None:
    """Nothing has arrived yet."""
    assert coor.updates_stale
    assert coor.stale_since is None


def test_awake_with_only_resends_goes_stale(
    coor: coordinator.VehicleCoordinator, clock: Clock
) -> None:
    """Resent snapshots keep arriving but carry old timestamps."""
    coor.data = {"powerState": {"value": "go"}}
    parallax(coor, T0, 300)
    assert not coor.updates_stale

    for _ in range(10):
        clock.advance(1)
        parallax(coor, T0 - timedelta(days=1), 300)
        assert not coor.updates_stale
    clock.advance(1)
    parallax(coor, T0 - timedelta(days=1), 300)

    assert coor.updates_stale
    assert coor.stale_since == T0


def test_asleep_with_only_resends_is_not_a_problem(
    coor: coordinator.VehicleCoordinator, clock: Clock
) -> None:
    """A day asleep: no problem, but "No updates since" shows when it slept."""
    coor.data = {"powerState": {"value": "sleep"}}
    parallax(coor, T0, 300)
    for _ in range(24 * 60):
        clock.advance(1)
        parallax(coor, T0 - timedelta(days=3), 300)

    assert not coor.updates_stale
    assert coor.stale_since == T0


def test_no_messages_is_stale_even_asleep(
    coor: coordinator.VehicleCoordinator, clock: Clock
) -> None:
    """The connection stopped delivering anything."""
    coor.data = {"powerState": {"value": "sleep"}}
    parallax(coor, T0, 300)
    clock.advance(9)
    assert not coor.updates_stale
    clock.advance(2)
    assert coor.updates_stale


def test_fresh_graphql_clears_before_listeners_run(
    coor: coordinator.VehicleCoordinator, clock: Clock
) -> None:
    """Listeners (the freshness entities) see the updated freshness."""
    coor.data = {"powerState": {"value": "go"}}
    parallax(coor, T0, 300)
    clock.advance(30)
    assert coor.updates_stale

    graphql(coor, clock.now.isoformat().replace("+00:00", "Z"))

    assert coor.seen_stale[-1] is False
    assert not coor.updates_stale
    assert coor.stale_since is None


def test_timestamp_without_timezone_is_ignored(
    coor: coordinator.VehicleCoordinator, clock: Clock
) -> None:
    """It can't be compared with the others, so it doesn't count."""
    coor.data = {"powerState": {"value": "go"}}
    parallax(coor, T0, 300)
    clock.advance(30)

    graphql(coor, "2026-10-12T18:30:00.000")

    assert coor.updates_stale
    assert coor.stale_since == T0


def test_entities(coor: coordinator.VehicleCoordinator, clock: Clock) -> None:
    """The entities reflect the coordinator and always stay available."""
    entry = MagicMock()
    problem = binary_sensor.RivianVehicleUpdatesEntity(
        coor, entry, binary_sensor.VEHICLE_UPDATES_DESCRIPTION, VEHICLE
    )
    since = sensor.RivianNoUpdatesSinceEntity(
        coor, entry, sensor.NO_UPDATES_SINCE_SENSOR, VEHICLE
    )
    assert problem.available and since.available

    coor.data = {"powerState": {"value": "go"}}
    parallax(coor, T0, 300)
    assert problem.is_on is False
    assert since.native_value is None

    clock.advance(11)
    assert problem.is_on is True
    assert since.native_value == T0
    assert problem.available and since.available
