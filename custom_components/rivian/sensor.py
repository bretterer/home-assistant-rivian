"""Rivian (Unofficial)"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType

from . import RivianDataUpdateCoordinator, RivianEntity, RivianWallboxEntity
from .const import ATTR_COORDINATOR, CONF_VIN, DOMAIN, SENSORS
from .data_classes import RivianSensorEntity, RivianWallboxSensorEntityDescription


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the sensor entities"""
    coordinator = hass.data[DOMAIN][entry.entry_id][ATTR_COORDINATOR]

    entities = []
    for _, value in enumerate(SENSORS):
        entities.append(
            RivianSensor(
                coordinator=coordinator,
                config_entry=entry,
                sensor=SENSORS[value],
                prop_key=value,
            )
        )

    # Add wallbox entities
    entities.extend(
        RivianWallboxSensorEntity(coordinator, description, wallbox)
        for wallbox in coordinator.wallboxes
        for description in WALLBOX_SENSORS
    )

    async_add_entities(entities, True)


class RivianSensor(RivianEntity, SensorEntity):
    """Representation of a Sensor."""

    def __init__(
        self,
        coordinator: RivianDataUpdateCoordinator,
        config_entry: ConfigEntry,
        sensor: RivianSensorEntity,
        prop_key: str,
    ) -> None:
        """Create a Rivian sensor."""
        super().__init__(coordinator, config_entry)
        self._sensor = sensor
        self.entity_description = sensor.entity_description
        self._name = self.entity_description.key
        self._prop_key = prop_key
        self.entity_id = f"sensor.{self.entity_description.key}"
        self._vin = config_entry.data.get(CONF_VIN)

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""
        return f"{DOMAIN}_{self._name}_{self._config_entry.entry_id}"

    @property
    def native_value(self) -> str:
        try:
            entity = self.coordinator.data[self._prop_key]
            if entity is None:
                return "Sensor Unavailable"
            if self._sensor.value_lambda is None:
                return entity["value"]
            else:
                return self._sensor.value_lambda(entity["value"])
        except KeyError:
            return None

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Return the state attributes of the device."""
        try:
            entity = self.coordinator.data[self._prop_key]
            if entity is None:
                return None
            if self._sensor.value_lambda is None:
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
