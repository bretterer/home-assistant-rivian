"""Tests for the vehicle access diagnostic sensors and binary sensors."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from custom_components.rivian import binary_sensor, const, coordinator
from homeassistant.const import EntityCategory

SENSORS = {d.key: d for ds in const.SENSORS.values() for d in ds}
BINARY_SENSORS = {d.key: d for ds in const.BINARY_SENSORS.values() for d in ds}
VEHICLE = {"id": "vehicle-1", "vin": "VIN1", "name": "Truck", "model": "R1T"}


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


@pytest.mark.parametrize(
    ("key", "rvm", "field", "value", "enabled"),
    [
        (
            "vas_secure_element_faulted",
            "security.access.vas_fault",
            "vasSecureElementFaulted",
            "lost_communication",
            True,
        ),
        (
            "vas_access_can_faulted",
            "security.access.vas_fault",
            "vasAccessCanFaulted",
            "failure",
            True,
        ),
        (
            "secure_immobilizer_status",
            "security.access.immobilizer_state",
            "secureImmobilizerStatus",
            "authorized_to_drive",
            False,
        ),
        (
            "passive_entry_unlock_fail_reason",
            "security.access.passive_entry_debug",
            "passiveEntryUnlockFailReason",
            "passenger_in_seat",
            False,
        ),
        (
            "ccc_passive_permission_status",
            "vehicle_access.state.passive_entry",
            "cccPassivePermissionStatus",
            "enabled",
            False,
        ),
    ],
)
def test_enum_sensor(
    coor: coordinator.VehicleCoordinator,
    key: str,
    rvm: str,
    field: str,
    value: str,
    enabled: bool,
) -> None:
    """The decoded value is kept and is one of the sensor's options."""
    parallax(coor, rvm, {field: value})

    description = SENSORS[key]
    assert coor.get(field) == value
    assert description.field == field
    assert value in description.options
    assert description.entity_category == EntityCategory.DIAGNOSTIC
    assert description.entity_registry_enabled_default is enabled


@pytest.mark.parametrize(
    ("key", "rvm", "field", "enabled"),
    [
        (
            "consecutive_alarm_disabled_notification",
            "security.alarm.state",
            "consecutiveAlarmDisabledNotification",
            True,
        ),
        (
            "passive_entry_bluetooth_in_ccc",
            "vehicle_access.state.passive_entry",
            "passiveEntryBluetoothInCcc",
            False,
        ),
    ],
)
def test_binary_sensor(
    coor: coordinator.VehicleCoordinator,
    key: str,
    rvm: str,
    field: str,
    enabled: bool,
) -> None:
    """The decoded bool turns the binary sensor on and off."""
    description = BINARY_SENSORS[key]
    entity = binary_sensor.RivianBinarySensorEntity(
        coor, MagicMock(), description, VEHICLE
    )
    assert description.entity_category == EntityCategory.DIAGNOSTIC
    assert description.entity_registry_enabled_default is enabled

    parallax(coor, rvm, {field: True})
    assert entity.is_on is True

    parallax(coor, rvm, {field: False})
    assert entity.is_on is False
