"""Tests for the GNSS error estimates on the location, altitude, speed and bearing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from custom_components.rivian import const, coordinator, sensor

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
ERROR = {
    "positionVertical": 2.5,
    "positionHorizontal": 4.2,
    "speed": 0.3,
    "bearing": 1.5,
}
SENSORS = {d.key: d for ds in const.SENSORS.values() for d in ds}


@pytest.fixture
def coor() -> coordinator.VehicleCoordinator:
    """A vehicle coordinator whose data updates are kept."""
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
    coor: coordinator.VehicleCoordinator, rvm: str, at: datetime, decoded: dict
) -> None:
    """Deliver a Parallax message that decodes to `decoded`."""
    message = {
        "payload": {
            "data": {
                "parallaxMessages": {
                    "rvm": rvm,
                    "payload": "",
                    "timestamp": at.timestamp() * 1000,
                }
            }
        }
    }
    decoded = {"timestamp": at} | decoded
    with patch.object(coordinator, "decode_parallax_message", return_value=decoded):
        coor._process_parallax_data(message)


def gnss_fix(coor: coordinator.VehicleCoordinator, at: datetime) -> None:
    """Deliver a dynamics.vehicle.gnss fix taken at `at`, with its error."""
    parallax(
        coor,
        "dynamics.vehicle.gnss",
        at,
        {
            "gnssLocation": {"latitude": 40.2, "longitude": -111.8, "timeStamp": at},
            "gnssAltitude": 1400.0,
            "gnssSpeed": 12.5,
            "gnssBearing": 90.0,
            "gnssError": {"timeStamp": at} | ERROR,
        },
    )


def navigation_fix(coor: coordinator.VehicleCoordinator, at: datetime) -> None:
    """Deliver a trip_progress fix taken at `at`, newer than Parallax's."""
    parallax(
        coor,
        "navigation.navigation_service.trip_progress",
        at,
        {
            "latitude": 40.3,
            "longitude": -111.7,
            "locationTime": at,
            "speed": 20.0,
            "bearing": 180.0,
        },
    )


@pytest.mark.parametrize("component", list(ERROR))
def test_error_for_parallax_fix(
    coor: coordinator.VehicleCoordinator, component: str
) -> None:
    """Every estimate applies while the values shown are Parallax's fix."""
    gnss_fix(coor, T0)

    assert coor.gnss_error(component) == ERROR[component]


def test_no_error_before_any_fix(coor: coordinator.VehicleCoordinator) -> None:
    assert coor.gnss_error("positionHorizontal") is None


def test_newer_navigation_fix(coor: coordinator.VehicleCoordinator) -> None:
    """A newer fix replaces the location, speed and bearing, but not the altitude."""
    gnss_fix(coor, T0)
    with patch.object(coordinator.dt_util, "utcnow", return_value=T0):
        navigation_fix(coor, T0 + timedelta(seconds=5))

    assert coor.gnss_error("positionHorizontal") is None
    assert coor.gnss_error("speed") is None
    assert coor.gnss_error("bearing") is None
    assert coor.gnss_error("positionVertical") == ERROR["positionVertical"]


def test_newer_graphql_position(coor: coordinator.VehicleCoordinator) -> None:
    """A newer GraphQL position has no error, but speed and bearing keep theirs."""
    gnss_fix(coor, T0)
    coor._process_graphql_position(
        {
            "type": "next",
            "payload": {
                "data": {
                    "vehicleState": {
                        "gnssLocation": {
                            "latitude": 40.1,
                            "longitude": -111.9,
                            "timeStamp": "2026-01-01T12:00:05Z",
                            "isAuthorized": True,
                        }
                    }
                }
            },
        }
    )

    assert coor.gnss_error("positionHorizontal") is None
    assert coor.gnss_error("speed") == ERROR["speed"]


def graphql_fast(
    coor: coordinator.VehicleCoordinator, at: datetime, **values: float
) -> None:
    """Deliver GraphQL fast fields (gnssAltitude=..., ...) reported at `at`."""
    stamp = at.isoformat().replace("+00:00", "Z")
    coor._process_graphql_position(
        {
            "type": "next",
            "payload": {
                "data": {
                    "vehicleState": {
                        key: {"value": value, "timeStamp": stamp}
                        for key, value in values.items()
                    }
                }
            },
        }
    )


def test_newer_graphql_altitude(coor: coordinator.VehicleCoordinator) -> None:
    """A newer GraphQL altitude has no error, but the location keeps its own."""
    gnss_fix(coor, T0)
    graphql_fast(coor, T0 + timedelta(seconds=5), gnssAltitude=1700.0, gnssSpeed=13.0)

    assert coor.get("gnssAltitude") == 1700.0
    assert coor.gnss_error("positionVertical") is None
    assert coor.gnss_error("speed") is None
    assert coor.gnss_error("positionHorizontal") == ERROR["positionHorizontal"]


def test_parallax_altitude_after_graphql(
    coor: coordinator.VehicleCoordinator,
) -> None:
    """Once GraphQL's altitude is no longer recent, Parallax's fix has its error."""
    gnss_fix(coor, T0)
    graphql_fast(coor, T0 + timedelta(seconds=5), gnssAltitude=1700.0)
    later = T0 + const.GRAPHQL_FAST_FIELD_FRESHNESS + timedelta(minutes=1)
    with patch.object(coordinator.dt_util, "utcnow", return_value=later):
        gnss_fix(coor, later)

    assert coor.get("gnssAltitude") == 1400.0
    assert coor.gnss_error("positionVertical") == ERROR["positionVertical"]


def test_unchanged_parallax_altitude(coor: coordinator.VehicleCoordinator) -> None:
    """A newer Parallax fix with the same altitude still has its error."""
    gnss_fix(coor, T0)
    gnss_fix(coor, T0 + timedelta(minutes=1))

    assert coor.gnss_error("positionVertical") == ERROR["positionVertical"]


@pytest.mark.parametrize(
    ("key", "component"),
    [("altitude", "positionVertical"), ("speed", "speed"), ("bearing", "bearing")],
)
def test_sensor_gnss_error(key: str, component: str) -> None:
    """Each sensor names the error of its own value."""
    assert SENSORS[key].gnss_error == component


@pytest.mark.parametrize(
    ("key", "unit", "accuracy"),
    [
        # Shown in the native unit
        ("altitude", None, 2.5),
        ("altitude", "meters", 2.5),
        # Converted to the unit the sensor is shown in
        ("altitude", "feet", 8.2),
        ("speed", "miles_per_hour", 6.7),
        # Bearing has no units to convert
        ("bearing", "°", 3.0),
    ],
)
def test_accuracy_in_shown_unit(key: str, unit: str | None, accuracy: float) -> None:
    error = {"altitude": 2.5, "speed": 3.0, "bearing": 3.0}[key]

    assert sensor._accuracy(SENSORS[key], error, unit) == accuracy
