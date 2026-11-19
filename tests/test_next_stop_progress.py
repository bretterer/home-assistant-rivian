"""Tests for the Next stop distance and drive time sensors."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.rivian import sensor

GROCERY = "Grocery store"
TRAILHEAD = "Trailhead"

ARRIVALS = {
    GROCERY: datetime(2026, 1, 1, 12, 10, tzinfo=UTC),
    TRAILHEAD: datetime(2026, 1, 1, 12, 30, tzinfo=UTC),
}

# trip_progress's countdown to the next waypoint, in meters and seconds
DISTANCE = 1609.3
DURATION = 120.0

SENSORS = {description.key: description for description in sensor.NAVIGATION_SENSORS}


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
        "distanceRemaining": {
            "value": DISTANCE,
            "timeStamp": "2026-01-01T12:09:30+00:00",
        },
        "durationRemaining": {
            "value": DURATION,
            "timeStamp": "2026-01-01T12:09:30+00:00",
        },
    }
    return coor


def values(coor: MagicMock) -> tuple[Any, Any]:
    """Return the Next stop distance and drive time sensors' values."""
    return (
        SENSORS["next_stop_distance"].value_fn(coor),
        SENSORS["next_stop_drive_time"].value_fn(coor),
    )


def test_on_the_way_is_live() -> None:
    """Driving to a stop, trip_progress's countdown is used."""
    coor = coordinator((GROCERY, "next_stop"), (TRAILHEAD, "future"))

    assert values(coor) == (DISTANCE, DURATION)


def test_stale_progress_is_unknown() -> None:
    """trip_progress resent from before the trip was planned isn't used."""
    coor = coordinator((GROCERY, "next_stop"), (TRAILHEAD, "future"))
    for field in ("distanceRemaining", "durationRemaining"):
        coor.data[field]["timeStamp"] = "2026-01-01T12:08:00+00:00"

    assert values(coor) == (None, None)


@pytest.mark.parametrize("status", ["stopping", "arrived"])
def test_at_a_stop_is_unknown(status: str) -> None:
    """At a stop, trip_progress counts down to it, not the next stop."""
    coor = coordinator((GROCERY, status), (TRAILHEAD, "future"))

    assert values(coor) == (None, None)


def test_at_the_destination_is_unknown() -> None:
    """At the last stop there's no next stop."""
    coor = coordinator((TRAILHEAD, "stopping"))

    assert values(coor) == (None, None)


def test_not_navigating() -> None:
    """No trip: trip_progress's last values are left over, so unknown."""
    coor = coordinator()
    del coor.data["waypoints"]

    assert values(coor) == (None, None)


def test_no_distance_left() -> None:
    """A distance of 0.0 is a value, not a missing one."""
    coor = coordinator((GROCERY, "next_stop"), (TRAILHEAD, "future"))
    coor.data["distanceRemaining"]["value"] = 0.0
    coor.data["durationRemaining"]["value"] = 0.0

    assert values(coor) == (0.0, 0.0)
