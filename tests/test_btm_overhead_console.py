"""Tests for the overhead console Bluetooth module failure status sensor."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from custom_components.rivian import const, coordinator

DESCRIPTIONS = {d.key: d for ds in const.SENSORS.values() for d in ds}
KEY = "bluetooth_overhead_console_hardware_failure_status"


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


def test_overhead_console_failure(coor: coordinator.VehicleCoordinator) -> None:
    """A reported failure is kept as reported."""
    parallax(coor, "security.access.btm", {"btmOcHardwareFailureStatus": "set"})

    assert coor.get("btmOcHardwareFailureStatus") == "set"
    assert DESCRIPTIONS[KEY].field == "btmOcHardwareFailureStatus"
    assert "set" in DESCRIPTIONS[KEY].options


def test_overhead_console_unsent(coor: coordinator.VehicleCoordinator) -> None:
    """An unsent status means no fault, the same as the other modules."""
    parallax(coor, "security.access.btm", {"btmOcHardwareFailureStatus": "set"})
    parallax(coor, "security.access.btm", {"btmOcHardwareFailureStatus": None})

    assert coor.get("btmOcHardwareFailureStatus") == "dtc_not_set"
    assert "dtc_not_set" in DESCRIPTIONS[KEY].options
