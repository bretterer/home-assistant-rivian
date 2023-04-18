"""Rivian (Unofficial)"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.components.sensor import (
    DOMAIN as PLATFORM,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    STATE_UNAVAILABLE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType

from .const import ATTR_COORDINATOR, DOMAIN, SENSORS
from .data_classes import (
    RivianSensorEntityDescription,
    RivianWallboxSensorEntityDescription,
)
from .entity import (
    RivianDataUpdateCoordinator,
    RivianEntity,
    RivianWallboxEntity,
    async_update_unique_id,
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the sensor entities"""
    coordinator: RivianDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][
        ATTR_COORDINATOR
    ]

    entities = [
        RivianSensorEntity(coordinator, entry, description, vin)
        for description in SENSORS
        for vin in coordinator.vehicles
    ]

    # Migrate unique ids to support multiple VIN
    async_update_unique_id(hass, PLATFORM, entities)

    # Add wallbox entities
    entities.extend(
        RivianWallboxSensorEntity(coordinator, description, wallbox)
        for wallbox in coordinator.wallboxes
        for description in WALLBOX_SENSORS
    )

    async_add_entities(entities, True)


class RivianSensorEntity(RivianEntity, SensorEntity):
    """Representation of a Rivian sensor entity."""

    entity_description: RivianSensorEntityDescription

    @property
    def native_value(self) -> str | None:
        """Return the value reported by the sensor."""
        if (val := self._get_value(self.entity_description.field)) is not None:
            return _fn(val) if (_fn := self.entity_description.value_lambda) else val
        return STATE_UNAVAILABLE

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Return the state attributes of the device."""
        try:
            entity = self.coordinator.data[self._vin][self.entity_description.field]
            if entity is None:
                return None
            if self.entity_description.value_lambda is None:
                return {
                    "last_update": entity["timeStamp"],
                }
            return {
                "native_value": entity["value"],
                "last_update": entity["timeStamp"],
                "history": str(entity["history"]),
            }
        except KeyError:
            return None


class RivianWallboxSensorEntity(RivianWallboxEntity, SensorEntity):
    """Representation of a Rivian wallbox sensor entity."""

    entity_description: RivianWallboxSensorEntityDescription

    @property
    def native_value(self) -> StateType:
        """Return the value reported by the sensor."""
        value = self.wallbox[self.entity_description.field]
        if self.device_class == SensorDeviceClass.ENUM:
            return value.lower()
        return value


WALLBOX_SENSORS = (
    RivianWallboxSensorEntityDescription(
        key="charging_status",
        field="chargingStatus",
        name="Charging status",
        icon="mdi:ev-plug-type1",
        device_class=SensorDeviceClass.ENUM,
        options=["available", "disconnected", "plugged_in", "charging"],
        translation_key="charging_status",
    ),
    RivianWallboxSensorEntityDescription(
        key="amperage",
        field="currentAmps",
        name="Amperage",
        device_class=SensorDeviceClass.CURRENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    RivianWallboxSensorEntityDescription(
        key="amperage_maximum",
        field="maxAmps",
        name="Amperage maximum",
        device_class=SensorDeviceClass.CURRENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    RivianWallboxSensorEntityDescription(
        key="power",
        field="power",
        name="Power",
        device_class=SensorDeviceClass.POWER,
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        suggested_unit_of_measurement=UnitOfPower.KILO_WATT,
    ),
    RivianWallboxSensorEntityDescription(
        key="power_maximum",
        field="maxPower",
        name="Power maximum",
        device_class=SensorDeviceClass.POWER,
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        suggested_unit_of_measurement=UnitOfPower.KILO_WATT,
    ),
    RivianWallboxSensorEntityDescription(
        key="voltage",
        field="currentVoltage",
        name="Voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    RivianWallboxSensorEntityDescription(
        key="voltage_maximum",
        field="maxVoltage",
        name="Voltage maximum",
        device_class=SensorDeviceClass.VOLTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
)
