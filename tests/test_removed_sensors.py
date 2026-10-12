"""Tests for cleaning up removed sensors."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from custom_components.rivian import const, helpers


def test_removed_keys_are_not_live_sensors() -> None:
    """A removed key must never match a sensor that's still created."""
    live = {d.key for descriptions in const.SENSORS.values() for d in descriptions}
    assert const.REMOVED_SENSOR_KEYS.isdisjoint(live)


def test_removed_sensors_are_deleted_from_the_registry() -> None:
    """Registered removed sensors are deleted; everything else is left alone."""
    registered = {
        "VIN1-ota_current_version": "sensor.truck_software_ota_current_version",
        "VIN1-ota_install_progress": "sensor.truck_software_ota_install_progress",
        "VIN1-ota_status": "sensor.truck_software_ota_status",
    }
    registry = MagicMock()
    registry.async_get_entity_id.side_effect = lambda platform, domain, unique_id: (
        registered.get(unique_id)
    )

    with patch.object(helpers, "er", MagicMock(async_get=lambda hass: registry)):
        helpers.async_remove_stale_entities(
            MagicMock(),
            "sensor",
            (f"VIN1-{key}" for key in const.REMOVED_SENSOR_KEYS),
            ["VIN1-ota_status"],
        )

    removed = {call.args[0] for call in registry.async_remove.call_args_list}
    assert removed == {
        "sensor.truck_software_ota_current_version",
        "sensor.truck_software_ota_install_progress",
    }
