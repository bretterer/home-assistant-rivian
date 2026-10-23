"""Tests for the known location and GNSS error sensors."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from custom_components.rivian import const, coordinator

DESCRIPTIONS = {d.key: d for ds in const.SENSORS.values() for d in ds}


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
    coor: coordinator.VehicleCoordinator, rvm: str, decoded: dict[str, Any]
) -> None:
    """Deliver a Parallax message that decodes to `decoded`."""
    message = {
        "payload": {
            "data": {"parallaxMessages": {"rvm": rvm, "payload": "", "timestamp": 0}}
        }
    }
    decoded = {"timestamp": datetime(2026, 10, 12, tzinfo=UTC)} | decoded
    with patch.object(coordinator, "decode_parallax_message", return_value=decoded):
        coor._process_parallax_data(message)


def native_value(coor: coordinator.VehicleCoordinator, key: str) -> Any:
    """The state of sensor `key`, from its field as the sensor entity reads it."""
    description = DESCRIPTIONS[key]
    return description.value_lambda(coor.get(description.field))


@pytest.mark.parametrize(
    ("value", "state"),
    [("unknown", "elsewhere"), ("home", "home"), ("work", "work")],
)
def test_known_location(
    coor: coordinator.VehicleCoordinator, value: str, state: str
) -> None:
    """Not being at a known place is a state of its own, not an unknown state."""
    parallax(coor, "dynamics.vehicle.location", {"knownLocation": value})

    assert native_value(coor, "known_location") == state
    assert state in DESCRIPTIONS["known_location"].options


def test_gnss_error(coor: coordinator.VehicleCoordinator) -> None:
    """Each sensor reads its own part of the fix's error estimate."""
    parallax(
        coor,
        "dynamics.vehicle.gnss",
        {
            "gnssError": {
                "timeStamp": datetime(2026, 10, 12, tzinfo=UTC),
                "positionVertical": 2.5,
                "positionHorizontal": 1.2,
                "speed": 0.3,
                "bearing": 4.1,
            }
        },
    )

    assert native_value(coor, "gnss_error_position_vertical") == 2.5
    assert native_value(coor, "gnss_error_position_horizontal") == 1.2
    assert native_value(coor, "gnss_error_speed") == 0.3
    assert native_value(coor, "gnss_error_bearing") == 4.1
