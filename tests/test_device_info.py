"""Tests for the vehicle's device info."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.rivian import binary_sensor, const

VEHICLE = {"id": "V", "vin": "VIN1", "name": "Truck", "model": "R1T"}


def device_info(data: dict[str, Any]) -> dict[str, Any]:
    coordinator = MagicMock()
    coordinator.get = lambda key: (data.get(key) or {}).get("value")
    description = next(iter(const.BINARY_SENSORS.values()))[0]
    entity = binary_sensor.RivianBinarySensorEntity(
        coordinator, MagicMock(), description, VEHICLE
    )
    return entity.device_info


@pytest.mark.parametrize(
    "mac", ["AA:BB:CC:DD:EE:0F", "aa-bb-cc-dd-ee-0f", "aabbccddee0f"]
)
def test_wifi_mac_address_is_a_connection(mac: str) -> None:
    """The vehicle's Wi-Fi MAC address links its device by network connection."""
    info = device_info({"wifiMacAddress": {"value": mac}})
    assert info["connections"] == {("mac", "aa:bb:cc:dd:ee:0f")}


def test_no_connection_without_a_mac_address() -> None:
    assert "connections" not in device_info({})
