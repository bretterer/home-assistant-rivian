"""Tests for the Next stop sensors while stopped at a stop."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.rivian import sensor

BAKERY = "Bakery"
GROCERY = "Grocery store"
TRAILHEAD = "Trailhead"

GROCERY_ARRIVAL = datetime(2026, 1, 1, 12, 10, tzinfo=UTC)
TRAILHEAD_ARRIVAL = datetime(2026, 1, 1, 12, 30, tzinfo=UTC)
# trip_progress's countdown to the grocery store, still sent while parked there
LIVE_ARRIVAL = datetime(2026, 1, 1, 12, 11, tzinfo=UTC)

ARRIVALS = {GROCERY: GROCERY_ARRIVAL, TRAILHEAD: TRAILHEAD_ARRIVAL}


def coordinator(*stops: tuple[str, str]) -> MagicMock:
    """A coordinator navigating through stops of (name, status)."""
    waypoints = [
        {
            "type": "place",
            "name": name,
            "status": status,
            "latitude": 40.0,
            "longitude": -105.3,
            "arrivalTime": ARRIVALS.get(name),
        }
        for name, status in stops
    ]
    values: dict[str, Any] = {"waypoints": waypoints}
    coor = MagicMock()
    coor.get.side_effect = values.get
    coor.data = {
        "waypoints": {"value": waypoints, "timeStamp": "2026-01-01T12:09:00+00:00"},
        "nextWaypointArrivalTime": {
            "value": LIVE_ARRIVAL,
            "timeStamp": "2026-01-01T12:09:30+00:00",
        },
    }
    return coor


@pytest.mark.parametrize(
    ("stops", "next_stop", "arrived_at"),
    [
        # Setting off
        (
            ((BAKERY, "next_stop"), (GROCERY, "future"), (TRAILHEAD, "future")),
            BAKERY,
            None,
        ),
        # At the first stop
        (
            ((BAKERY, "stopping"), (GROCERY, "future"), (TRAILHEAD, "future")),
            GROCERY,
            BAKERY,
        ),
        # Left it
        (((GROCERY, "next_stop"), (TRAILHEAD, "future")), GROCERY, None),
        # At the grocery store
        (((GROCERY, "stopping"), (TRAILHEAD, "future")), TRAILHEAD, GROCERY),
        # Left it
        (((TRAILHEAD, "next_stop"),), TRAILHEAD, None),
    ],
)
def test_trip_with_two_stops(
    stops: tuple[tuple[str, str], ...], next_stop: str, arrived_at: str | None
) -> None:
    """The waypoint statuses a vehicle reports on a trip with two stops."""
    coor = coordinator(*stops)

    assert sensor._waypoint_name(sensor._next_stop(coor)) == next_stop
    assert sensor._next_stop_attributes(coor)["arrived_at"] == arrived_at


def test_eta_at_a_stop_is_the_planned_one() -> None:
    """trip_progress counts down to the stop the vehicle is at, so skip it."""
    coor = coordinator((GROCERY, "stopping"), (TRAILHEAD, "future"))

    assert sensor._next_stop_arrival(coor) == TRAILHEAD_ARRIVAL


def test_eta_on_the_way_is_live() -> None:
    """Driving to a stop, the live arrival time is used."""
    coor = coordinator((GROCERY, "next_stop"), (TRAILHEAD, "future"))

    assert sensor._next_stop_arrival(coor) == LIVE_ARRIVAL


def test_at_the_destination() -> None:
    """At the last stop there's no next stop, but arrived_at says where."""
    coor = coordinator((TRAILHEAD, "stopping"))

    assert sensor._next_stop(coor) is None
    assert sensor._next_stop_arrival(coor) is None
    assert sensor._next_stop_attributes(coor) == {"arrived_at": TRAILHEAD}


def test_not_navigating() -> None:
    """No trip: no next stop and no attributes."""
    coor = coordinator()

    assert sensor._next_stop(coor) is None
    assert sensor._next_stop_attributes(coor) is None
