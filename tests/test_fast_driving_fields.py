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
    """Deliver a message on GraphQL's position subscription."""
    coor._process_graphql_position({"payload": {"data": {"vehicleState": state}}})


def graphql_location(coor: coordinator.VehicleCoordinator, fix: dict[str, Any]) -> None:
    """Deliver a fix from GraphQL's position subscription."""
    coor._process_graphql_position(
        {"payload": {"data": {"vehicleState": {"gnssLocation": fix}}}}
    )


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
    """Speed, altitude, bearing and the odometer come from GraphQL.

    They're requested in the position subscription, together with the fix
    (see test_graphql_location.py), never in the main one.
    """
    assert {
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

    # Once it passes the next kilometer, Parallax moves it on
    clock.advance(120)
    parallax(coor, {"timestamp": clock.now, "vehicleMileage": 42_842_000.0}, odometer)
    assert coor.get("vehicleMileage") == 42_842_000.0


def test_frozen_graphql_speed_does_not_override_parallax(
    coor: coordinator.VehicleCoordinator, clock: Clock
) -> None:
    """A GraphQL speed stuck months back (upstream #288) never wins over Parallax."""
    parallax(coor, gnss_fix(clock.now, 43.6, speed=25.0), "dynamics.vehicle.gnss")
    assert coor.get("gnssSpeed") == 25.0

    frozen = {"value": 0, "timeStamp": "2026-05-29T09:50:03.045Z"}
    graphql(coor, {"gnssSpeed": frozen, "gnssBearing": frozen})
    assert coor.get("gnssSpeed") == 25.0

    # Nor does it hold Parallax off as if GraphQL were live
    clock.advance(60)
    parallax(coor, gnss_fix(clock.now, 43.6, speed=27.0), "dynamics.vehicle.gnss")
    assert coor.get("gnssSpeed") == 27.0


def test_frozen_graphql_location_does_not_override_parallax(
    coor: coordinator.VehicleCoordinator, clock: Clock
) -> None:
    """A day-old GraphQL position (upstream #274) loses to Parallax's fix."""
    parallax(coor, gnss_fix(clock.now, 43.6), "dynamics.vehicle.gnss")
    graphql_location(
        coor,
        {
            "latitude": 40.0,
            "longitude": -100.0,
            "timeStamp": "2026-10-11T18:00:00.000Z",
            "isAuthorized": True,
        },
    )
    # Its sibling fields, from the same subscription, are current
    graphql(
        coor, {"gnssAltitude": {"value": 830.0, "timeStamp": "2026-10-12T18:00:02Z"}}
    )
    assert coor.data["gnssLocation"]["latitude"] == 43.6
    assert coor.get("gnssAltitude") == 830.0

    clock.advance(60)
    parallax(coor, gnss_fix(clock.now, 43.7), "dynamics.vehicle.gnss")
    assert coor.data["gnssLocation"]["latitude"] == 43.7


def test_graphql_resend_of_an_older_value_is_ignored(
    coor: coordinator.VehicleCoordinator, clock: Clock
) -> None:
    """Only a newer GraphQL report replaces the held value."""
    graphql(coor, {"gnssSpeed": {"value": 20.0, "timeStamp": "2026-10-12T18:00:05Z"}})
    graphql(coor, {"gnssSpeed": {"value": 3.0, "timeStamp": "2026-10-12T18:00:00Z"}})
    assert coor.get("gnssSpeed") == 20.0
    graphql(coor, {"gnssSpeed": {"value": 21.0, "timeStamp": "2026-10-12T18:00:10Z"}})
    assert coor.get("gnssSpeed") == 21.0


def test_fast_fields_and_position_in_one_message(
    coor: coordinator.VehicleCoordinator, clock: Clock
) -> None:
    """One position-subscription message can carry the fix and fast fields."""
    graphql(
        coor,
        {
            "gnssLocation": {
                "latitude": 43.6,
                "longitude": -116.2,
                "timeStamp": "2026-10-12T18:00:05Z",
                "isAuthorized": True,
            },
            "gnssSpeed": {"value": 20.0, "timeStamp": "2026-10-12T18:00:05Z"},
        },
    )
    assert coor.data["gnssLocation"]["latitude"] == 43.6
    assert coor.get("gnssSpeed") == 20.0


def test_late_older_parallax_fix_does_not_regress_graphql_speed(
    coor: coordinator.VehicleCoordinator, clock: Clock
) -> None:
    """Parallax's speed from before GraphQL's report never replaces it."""
    graphql(coor, {"gnssSpeed": {"value": 20.0, "timeStamp": "2026-10-12T18:01:40Z"}})
    clock.advance(300)  # GraphQL's report is no longer fresh
    parallax(
        coor,
        gnss_fix(datetime(2026, 10, 12, 18, 0, 50, tzinfo=UTC), 43.6, speed=5.0),
        "dynamics.vehicle.gnss",
    )
    assert coor.get("gnssSpeed") == 20.0
    # A newer Parallax fix does take over
    parallax(coor, gnss_fix(clock.now, 43.7, speed=6.0), "dynamics.vehicle.gnss")
    assert coor.get("gnssSpeed") == 6.0


def test_invalid_graphql_speed_does_not_block_parallax(
    coor: coordinator.VehicleCoordinator, clock: Clock
) -> None:
    """An invalid GraphQL speed isn't kept, so it never outranks Parallax's."""
    parallax(
        coor,
        gnss_fix(datetime(2026, 10, 12, 18, 0, 0, tzinfo=UTC), 43.6, speed=5.0),
        "dynamics.vehicle.gnss",
    )
    graphql(
        coor,
        {
            "gnssSpeed": {
                "value": "signal_not_available",
                "timeStamp": "2026-10-12T18:01:40Z",
            }
        },
    )
    clock.advance(300)
    parallax(
        coor,
        gnss_fix(datetime(2026, 10, 12, 18, 0, 50, tzinfo=UTC), 43.7, speed=7.0),
        "dynamics.vehicle.gnss",
    )
    assert coor.get("gnssSpeed") == 7.0
