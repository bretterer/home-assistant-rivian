"""Tests for the charging time remaining validity sensor."""

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


@pytest.mark.parametrize("validity", ["valid", "invalid", "pack_discharging"])
def test_validity(coor: coordinator.VehicleCoordinator, validity: str) -> None:
    """The validity is kept as reported, and is one of the sensor's states."""
    parallax(
        coor,
        "charging.session.time_estimation",
        {"timeToEndOfCharge": 64, "chargingTimeEstimationValidity": validity},
    )

    assert coor.get("chargingTimeEstimationValidity") == validity
    assert coor.get("timeToEndOfCharge") == 64
    assert validity in DESCRIPTIONS["charging_time_estimation_validity"].options


def test_unset_validity_keeps_the_last(coor: coordinator.VehicleCoordinator) -> None:
    """An empty message (not charging) leaves the last reported validity."""
    parallax(
        coor,
        "charging.session.time_estimation",
        {"timeToEndOfCharge": 64, "chargingTimeEstimationValidity": "valid"},
    )
    parallax(
        coor,
        "charging.session.time_estimation",
        {"timeToEndOfCharge": 0, "chargingTimeEstimationValidity": None},
    )

    assert coor.get("chargingTimeEstimationValidity") == "valid"
    assert coor.get("timeToEndOfCharge") == 0
