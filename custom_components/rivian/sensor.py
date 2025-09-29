"""Rivian"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import logging
from typing import Any, Final

from homeassistant.components.sensor import (
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
    UnitOfEnergy,
    UnitOfLength,
    UnitOfPower,
    UnitOfSpeed,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType

from .const import ATTR_COORDINATOR, ATTR_VEHICLE, ATTR_WALLBOX, DOMAIN, SENSORS
from .coordinator import DriverKeyCoordinator, VehicleCoordinator, WallboxCoordinator
from .data_classes import (
    RivianSensorEntityDescription,
    RivianWallboxSensorEntityDescription,
)
from .entity import (
    RivianChargingEntity,
    RivianEntity,
    RivianVehicleEntity,
    RivianWallboxEntity,
)

_LOGGER = logging.getLogger(__name__)

RIVIAN_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%f%z"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the sensor entities"""
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    vehicles: dict[str, Any] = data[ATTR_VEHICLE]
    coordinators: dict[str, Any] = data[ATTR_COORDINATOR]

    # Add vehicle entities
    vehicle_coordinators: dict[str, VehicleCoordinator] = coordinators[ATTR_VEHICLE]
    entities = [
        RivianSensorEntity(
            vehicle_coordinators[vehicle_id], entry, description, vehicle
        )
        for vehicle_id, vehicle in vehicles.items()
        for model, descriptions in SENSORS.items()
        if model in vehicle["model"]
        for description in descriptions
        if description.required_feature in (vehicle.get("supported_features") + [None])
    ]

    # Add charging entities
    entities.extend(
        RivianChargingSensorEntity(
            vehicle_coordinators[vehicle_id].charging_coordinator,
            description,
            vehicle["vin"],
        )
        for vehicle_id, vehicle in vehicles.items()
        for description in CHARGING_SENSORS
    )

    # Add drivers and keys entities
    entities.extend(
        RivianDriverSensorEntity(
            vehicle_coordinators[vehicle_id].drivers_coordinator,
            description,
            vehicle["vin"],
        )
        for vehicle_id, vehicle in vehicles.items()
        for description in DRIVER_SENSORS
    )

    # Add wallbox entities
    wallbox_coordinator: WallboxCoordinator = coordinators[ATTR_WALLBOX]
    entities.extend(
        RivianWallboxSensorEntity(wallbox_coordinator, description, wallbox)
        for wallbox in wallbox_coordinator.data
        for description in WALLBOX_SENSORS
    )

    async_add_entities(entities)


class RivianSensorEntity(RivianVehicleEntity, SensorEntity):
    """Representation of a Rivian sensor entity."""

    entity_description: RivianSensorEntityDescription

    @property
    def native_value(self) -> str | None:
        """Return the value reported by the sensor."""
        if _fn := self.entity_description.value_fn:
            return _fn(self.coordinator)

        if (val := self._get_value(self.entity_description.field)) is None:
            return STATE_UNAVAILABLE if not self.native_unit_of_measurement else None

        if self.device_class == SensorDeviceClass.ENUM:
            if (val := val.lower()) not in self.options:
                _LOGGER.error(
                    "Sensor %s provides state value '%s', which is not in the list of known options. Please consider opening an issue at https://github.com/bretterer/home-assistant-rivian/issues with the following info: 'field: \"%s\" / value: \"%s\"'",
                    self.name,
                    val,
                    self.entity_description.field,
                    val,
                )
                self.options.append(val)
        return val


class RivianChargingSensorEntity(RivianChargingEntity, SensorEntity):
    """Representation of a Rivian charging sensor entity."""

    entity_description: RivianSensorEntityDescription

    @property
    def native_value(self) -> str | float | None:
        """Return the value reported by the sensor."""
        if value_fn := self.entity_description.value_fn:
            return value_fn(self.coordinator)
        val = self.coordinator.data.get(self.entity_description.field)
        if isinstance(val, dict):
            val = val["value"]
        return val

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the unit of measurement of the sensor, if any."""
        if self.entity_description.field == "currentPrice":
            return self.coordinator.data.get(
                "currentCurrency", self.hass.config.currency
            )
        return super().native_unit_of_measurement


CHARGING_SENSORS: Final[tuple[RivianSensorEntityDescription, ...]] = (
    RivianSensorEntityDescription(
        key="charging_cost",
        translation_key="charging_cost",
        field="currentPrice",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
    ),
    RivianSensorEntityDescription(
        key="charging_energy_delivered",
        translation_key="charging_energy_delivered",
        field="totalChargedEnergy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=1,
    ),
    RivianSensorEntityDescription(
        key="charging_range_added",
        translation_key="charging_range_added",
        field="rangeAddedThisSession",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_unit_of_measurement=UnitOfLength.MILES,
    ),
    RivianSensorEntityDescription(
        key="charging_rate",
        translation_key="charging_rate",
        field="kilometersChargedPerHour",
        device_class=SensorDeviceClass.SPEED,
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_unit_of_measurement=UnitOfSpeed.MILES_PER_HOUR,
    ),
    RivianSensorEntityDescription(
        key="charging_speed",
        translation_key="charging_speed",
        field="power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    RivianSensorEntityDescription(
        key="charging_start_time",
        translation_key="charging_start_time",
        field="startTime",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda coor: (
            datetime.strptime(val, RIVIAN_TIMESTAMP_FORMAT)
            if (val := coor.data.get("startTime"))
            else val
        ),
    ),
    RivianSensorEntityDescription(
        key="charging_time_elapsed",
        translation_key="charging_time_elapsed",
        field="timeElapsed",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
)


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
        translation_key="charging_status",
        field="chargingStatus",
        device_class=SensorDeviceClass.ENUM,
        options=["available", "charging", "disconnected", "plugged_in", "unavailable"],
    ),
    RivianWallboxSensorEntityDescription(
        key="amperage",
        translation_key="amperage",
        field="currentAmps",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    RivianWallboxSensorEntityDescription(
        key="amperage_maximum",
        translation_key="amperage_maximum",
        field="maxAmps",
        device_class=SensorDeviceClass.CURRENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    RivianWallboxSensorEntityDescription(
        key="power",
        translation_key="power",
        field="power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        suggested_unit_of_measurement=UnitOfPower.KILO_WATT,
    ),
    RivianWallboxSensorEntityDescription(
        key="power_maximum",
        translation_key="power_maximum",
        field="maxPower",
        device_class=SensorDeviceClass.POWER,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        suggested_unit_of_measurement=UnitOfPower.KILO_WATT,
    ),
    RivianWallboxSensorEntityDescription(
        key="voltage",
        translation_key="voltage",
        field="currentVoltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    RivianWallboxSensorEntityDescription(
        key="voltage_maximum",
        translation_key="voltage_maximum",
        field="maxVoltage",
        device_class=SensorDeviceClass.VOLTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    RivianWallboxSensorEntityDescription(
        key="ssid",
        translation_key="ssid",
        field="wifiId",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
)

DRIVER_SENSORS: Final[tuple[RivianSensorEntityDescription, ...]] = (
    RivianSensorEntityDescription(
        key="drivers",
        translation_key="drivers",
        field="invitedUsers",
        value_fn=lambda coor: len(
            [
                user
                for user in (coor.data.get("invitedUsers") or [])
                if user["__typename"] == "ProvisionedUser"
            ]
        ),
    ),
    RivianSensorEntityDescription(
        key="keys",
        translation_key="keys",
        field="invitedUsers",
        value_fn=lambda coor: len(
            [
                keys
                for user in (coor.data.get("invitedUsers") or [])
                if user["__typename"] == "ProvisionedUser"
                for keys in user.get("devices", [])
            ]
        ),
    ),
)


class RivianDriverSensorEntity(RivianEntity[DriverKeyCoordinator], SensorEntity):
    """Representation of a Rivian driver sensor entity."""

    entity_description: RivianSensorEntityDescription
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: DriverKeyCoordinator,
        entity_description: RivianSensorEntityDescription,
        vin: str,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self.entity_description = entity_description
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, vin)})
        self._attr_unique_id = f"{vin}-{entity_description.key}"

    @property
    def native_value(self) -> int:
        """Return the value reported by the sensor."""
        if self.coordinator.data:
            return self.entity_description.value_fn(self.coordinator)
        return 0

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Return entity specific state attributes."""
        if self.entity_description.key == "keys":

            def get_count(key: str) -> int:
                field = self.entity_description.field
                return len(
                    [
                        keys
                        for user in (self.coordinator.data.get(field) or [])
                        if user["__typename"] == "ProvisionedUser"
                        for keys in user.get("devices", [])
                        if keys[key]
                    ]
                )

            return {"paired": get_count("isPaired"), "enabled": get_count("isEnabled")}
        return super().extra_state_attributes
