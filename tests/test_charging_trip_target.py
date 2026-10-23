"""Tests for the charging trip target sensors."""

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


def test_trip_target(coor: coordinator.VehicleCoordinator) -> None:
    """The target and the minutes to reach it, cleared once there's no target."""
    rvm = "charging.session.trip_target"
    parallax(
        coor,
        rvm,
        {"chargingTripTargetSoc": 80, "chargingTripTargetMinsRemaining": 45},
    )
    assert coor.get("chargingTripTargetSoc") == 80
    assert coor.get("chargingTripTargetMinsRemaining") == 45

    parallax(
        coor,
        rvm,
        {"chargingTripTargetSoc": None, "chargingTripTargetMinsRemaining": None},
    )
    assert "chargingTripTargetSoc" not in coor.data
    assert "chargingTripTargetMinsRemaining" not in coor.data


def test_trip_target_sensors() -> None:
    """Both sensors read the decoded fields, only where the feature is offered."""
    soc = DESCRIPTIONS["charging_trip_target_soc"]
    minutes = DESCRIPTIONS["charging_trip_target_time_remaining"]
    assert soc.field == "chargingTripTargetSoc"
    assert minutes.field == "chargingTripTargetMinsRemaining"
    assert soc.supported_feature == minutes.supported_feature == "CHARG_TRIP_TARGET"
