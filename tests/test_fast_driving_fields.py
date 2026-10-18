"""Tests for the driving fields GraphQL sends faster than Parallax."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from custom_components.rivian import coordinator
from custom_components.rivian.const import (
    GRAPHQL_FAST_FIELD_FRESHNESS,
    VEHICLE_STATE_FAST_FIELDS,
)

T0 = datetime(2026, 10, 12, 18, 0, tzinfo=UTC)


class Clock:
    """A controllable utcnow."""

    def __init__(self) -> None:
        self.now = T0

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> Any:
    clock = Clock()
    with patch.object(coordinator.dt_util, "utcnow", lambda: clock.now):
        yield clock


@pytest.fixture
def coor(clock: Clock) -> coordinator.VehicleCoordinator:
    coor = coordinator.VehicleCoordinator(
        hass=MagicMock(), config_entry=MagicMock(), client=MagicMock(), vehicle_id="V"
    )
    coor.data = {}

    def set_data(data: dict[str, Any]) -> None:
        coor.data = data

    coor.async_set_updated_data = set_data
    coor.charging_coordinator = MagicMock()
    return coor


def parallax(
    coor: coordinator.VehicleCoordinator, decoded: dict[str, Any], rvm: str
) -> None:
    """Deliver a decoded Parallax message."""
    message = {"payload": {"data": {"parallaxMessages": {"rvm": rvm, "payload": ""}}}}
    with patch.object(coordinator, "decode_parallax_message", return_value=decoded):
        coor._process_parallax_data(message)


def graphql(coor: coordinator.VehicleCoordinator, state: dict[str, Any]) -> None:
    """Deliver a GraphQL vehicle state message."""
    coor._process_new_data({"payload": {"data": {"vehicleState": state}}})


def gnss_fix(fix_time: datetime, lat: float, speed: float = 1.0) -> dict[str, Any]:
    """A decoded dynamics.vehicle.gnss message."""
    return {
        "timestamp": fix_time,
        "gnssTimeStamp": fix_time,
        "gnssLocation": {"latitude": lat, "longitude": -116.2, "timeStamp": fix_time},
        "gnssAltitude": 820.0,
        "gnssBearing": 90.0,
        "gnssSpeed": speed,
    }


def test_fast_fields_are_driving_fields() -> None:
    """Position, speed, altitude, bearing and the odometer come from GraphQL."""
    assert {
        "gnssLocation",
        "gnssSpeed",
        "gnssAltitude",
        "gnssBearing",
        "vehicleMileage",
    } == VEHICLE_STATE_FAST_FIELDS


def test_parallax_value_waits_while_graphql_is_fresh(
    coor: coordinator.VehicleCoordinator, clock: Clock
) -> None:
    """GraphQL's speed wins while fresh; Parallax's is used once it goes quiet."""
    graphql(coor, {"gnssSpeed": {"value": 20.0, "timeStamp": "2026-10-12T18:00:00Z"}})
    clock.advance(30)
    parallax(coor, gnss_fix(clock.now, 43.6, speed=5.0), "dynamics.vehicle.gnss")
    assert coor.get("gnssSpeed") == 20.0

    clock.advance(GRAPHQL_FAST_FIELD_FRESHNESS.total_seconds())
    parallax(coor, gnss_fix(clock.now, 43.6, speed=6.0), "dynamics.vehicle.gnss")
    assert coor.get("gnssSpeed") == 6.0


def test_odometer_falls_back_to_parallax(
    coor: coordinator.VehicleCoordinator, clock: Clock
) -> None:
    """Without GraphQL odometer updates, Parallax's odometer is used."""
    parallax(
        coor,
        {"timestamp": clock.now, "vehicleMileage": 1_000_000.0},
        "dynamics.vehicle.odometer",
    )
    assert coor.get("vehicleMileage") == 1_000_000.0


def test_parallax_odometer_never_moves_it_back(
    coor: coordinator.VehicleCoordinator, clock: Clock
) -> None:
    """Parallax's whole-km odometer can't undercut GraphQL's exact one while parked."""
    graphql(
        coor,
        {
            "vehicleMileage": {
                "value": 42_841_422.0,
                "timeStamp": "2026-10-12T18:00:00Z",
            }
        },
    )
    clock.advance(GRAPHQL_FAST_FIELD_FRESHNESS.total_seconds() + 60)
    odometer = "dynamics.vehicle.odometer"
    parallax(coor, {"timestamp": clock.now, "vehicleMileage": 42_841_000.0}, odometer)
    assert coor.get("vehicleMileage") == 42_841_422.0

    # Once it passes the next kilometre, Parallax moves it on
    clock.advance(120)
    parallax(coor, {"timestamp": clock.now, "vehicleMileage": 42_842_000.0}, odometer)
    assert coor.get("vehicleMileage") == 42_842_000.0


def test_graphql_fix_time_becomes_a_datetime(
    coor: coordinator.VehicleCoordinator, clock: Clock
) -> None:
    """A GraphQL fix time is stored as a datetime, so Parallax fixes compare with it."""
    graphql(
        coor,
        {
            "gnssLocation": {
                "latitude": 43.6,
                "longitude": -116.2,
                "timeStamp": "2026-10-12T18:00:00.000Z",
            }
        },
    )
    assert coor.data["gnssLocation"]["timeStamp"] == T0

    # Once GraphQL is quiet, an older Parallax fix is ignored, a newer one used
    clock.advance(GRAPHQL_FAST_FIELD_FRESHNESS.total_seconds() + 1)
    parallax(coor, gnss_fix(T0 - timedelta(seconds=30), 43.5), "dynamics.vehicle.gnss")
    assert coor.data["gnssLocation"]["latitude"] == 43.6
    parallax(coor, gnss_fix(clock.now, 43.7), "dynamics.vehicle.gnss")
    assert coor.data["gnssLocation"]["latitude"] == 43.7
    assert coor.data["gnssLocation"]["timeStamp"] == clock.now


def test_fresh_graphql_location_ignores_parallax_fix(
    coor: coordinator.VehicleCoordinator, clock: Clock
) -> None:
    """While GraphQL sends positions, Parallax's once-a-minute fix doesn't replace them."""
    graphql(
        coor,
        {
            "gnssLocation": {
                "latitude": 43.6,
                "longitude": -116.2,
                "timeStamp": "2026-10-12T18:00:00.000Z",
            }
        },
    )
    clock.advance(5)
    parallax(coor, gnss_fix(clock.now, 43.9), "dynamics.vehicle.gnss")
    assert coor.data["gnssLocation"]["latitude"] == 43.6
