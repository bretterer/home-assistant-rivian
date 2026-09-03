"""Rivian (Unofficial)"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
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
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType

from .const import (
    ATTR_COORDINATOR,
    ATTR_DRIVE_STORE,
    ATTR_DRIVE_TRACKER,
    ATTR_VEHICLE,
    ATTR_WALLBOX,
    DOMAIN,
    DRIVE_SENSORS,
    SENSORS,
    WEEK_DAYS_ORDERED,
)
from .coordinator import DriverKeyCoordinator, VehicleCoordinator, WallboxCoordinator
from .data_classes import (
    RivianSensorEntityDescription,
    RivianWallboxSensorEntityDescription,
)
from .drive_models import DriveState, SpeedBinData
from .drive_storage import DriveStore
from .drive_tracker import DriveTracker
from .entity import (
    RivianChargingEntity,
    RivianEntity,
    RivianVehicleEntity,
    RivianWallboxEntity,
)

_LOGGER = logging.getLogger(__name__)

ALL_WEEK_DAYS: Final[frozenset[str]] = frozenset(WEEK_DAYS_ORDERED)
WEEKDAYS_ONLY: Final[frozenset[str]] = frozenset(WEEK_DAYS_ORDERED[:5])

RIVIAN_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%f%z"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the sensor entities."""
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

    for vehicle_id, vehicle in vehicles.items():
        coord = vehicle_coordinators[vehicle_id]
        entities.append(
            RivianChargingScheduleDaysEntity(
                coord, entry, CHARGING_SCHEDULE_DAYS_SENSOR, vehicle
            )
        )

    # Add drive efficiency and status entities
    drive_trackers: dict[str, DriveTracker] = data.get(ATTR_DRIVE_TRACKER, {})
    drive_stores: dict[str, DriveStore] = data.get(ATTR_DRIVE_STORE, {})
    for vehicle_id, vehicle in vehicles.items():
        if (
            vehicle_id in vehicle_coordinators
            and vehicle_id in drive_trackers
            and vehicle_id in drive_stores
        ):
            coord = vehicle_coordinators[vehicle_id]
            tracker = drive_trackers[vehicle_id]
            store = drive_stores[vehicle_id]
            entities.extend(
                RivianDriveSensorEntity(
                    coordinator=coord,
                    config_entry=entry,
                    description=description,
                    vehicle=vehicle,
                    tracker=tracker,
                    store=store,
                )
                for description in DRIVE_SENSORS
            )

    async_add_entities(entities)


CHARGING_SCHEDULE_DAYS_SENSOR = RivianSensorEntityDescription(
    key="charging_schedule_days",
    translation_key="charging_schedule_days",
    field="charging_schedule_days",
)


class RivianChargingScheduleDaysEntity(RivianVehicleEntity, SensorEntity):
    """Charging Schedule Days Entity."""

    @property
    def available(self) -> bool:
        """Return availability."""
        return self._available

    @property
    def native_value(self) -> str | None:
        """Return native value."""
        sched = self.coordinator.charging_schedule
        raw_days = sched.get("weekDays", [])
        if not raw_days or not isinstance(raw_days, list):
            return None
        days = frozenset(raw_days)

        if days == ALL_WEEK_DAYS:
            return "daily"
        if days == WEEKDAYS_ONLY:
            return "weekdays"

        ordered = [d for d in WEEK_DAYS_ORDERED if d in days]
        return ", ".join(ordered)


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

        rval = _fn(val) if (_fn := self.entity_description.value_lambda) else val
        if self.device_class == SensorDeviceClass.ENUM and rval not in self.options:
            _LOGGER.error(
                "Sensor %s provides state value '%s', which is not in the list of known options. Please consider opening an issue at https://github.com/bretterer/home-assistant-rivian/issues with the following info: 'field: \"%s\" / value: \"%s\"'",
                self.name,
                rval,
                self.entity_description.field,
                val,
            )
            self.options.append(rval)
        return rval

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Return the state attributes of the device."""
        try:
            entity = self.coordinator.data[self.entity_description.field]
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


class RivianChargingSensorEntity(RivianChargingEntity, SensorEntity):
    """Representation of a Rivian charging sensor entity."""

    entity_description: RivianSensorEntityDescription

    @property
    def native_value(self) -> str | float | None:
        """Return the value reported by the sensor."""
        val = self.coordinator.data.get(self.entity_description.field)
        if isinstance(val, dict):
            val = val["value"]
        if value_fn := self.entity_description.value_lambda:
            return value_fn(val)
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
        field="currentPrice",
        name="Charging Cost",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
    ),
    RivianSensorEntityDescription(
        key="charging_energy_delivered",
        field="totalChargedEnergy",
        name="Charging Energy Delivered",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=1,
    ),
    RivianSensorEntityDescription(
        key="charging_range_added",
        field="rangeAddedThisSession",
        name="Charging Range Added",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_unit_of_measurement=UnitOfLength.MILES,
    ),
    RivianSensorEntityDescription(
        key="charging_rate",
        field="kilometersChargedPerHour",
        name="Charging Rate",
        device_class=SensorDeviceClass.SPEED,
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_unit_of_measurement=UnitOfSpeed.MILES_PER_HOUR,
    ),
    RivianSensorEntityDescription(
        key="charging_speed",
        field="power",
        name="Charging Speed",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    RivianSensorEntityDescription(
        key="charging_start_time",
        field="startTime",
        name="Charging Start Time",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_lambda=lambda val: (
            datetime.strptime(val, RIVIAN_TIMESTAMP_FORMAT).astimezone(UTC)
            if val
            else val
        ),
    ),
    RivianSensorEntityDescription(
        key="charging_time_elapsed",
        field="timeElapsed",
        name="Charging Time Elapsed",
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
        field="chargingStatus",
        name="Charging status",
        icon="mdi:ev-plug-type1",
        device_class=SensorDeviceClass.ENUM,
        options=["unavailable", "available", "disconnected", "plugged_in", "charging"],
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

DRIVER_SENSORS: Final[tuple[RivianSensorEntityDescription, ...]] = (
    RivianSensorEntityDescription(
        key="drivers",
        icon="mdi:account-multiple",
        name="Drivers",
        field="invitedUsers",
        value_lambda=lambda data: len(
            [user for user in (data or []) if user["__typename"] == "ProvisionedUser"]
        ),
    ),
    RivianSensorEntityDescription(
        key="keys",
        icon="mdi:car-key",
        name="Keys",
        field="invitedUsers",
        value_lambda=lambda data: len(
            [
                keys
                for user in (data or [])
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
            data = self.coordinator.data.get(self.entity_description.field)
            return self.entity_description.value_lambda(data)
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


class RivianDriveSensorEntity(RivianVehicleEntity, SensorEntity):
    """Representation of a Rivian drive efficiency and status sensor entity."""

    entity_description: RivianSensorEntityDescription

    def __init__(
        self,
        coordinator: VehicleCoordinator,
        config_entry: ConfigEntry,
        description: RivianSensorEntityDescription,
        vehicle: dict[str, Any],
        tracker: DriveTracker,
        store: DriveStore,
    ) -> None:
        """Initialize the drive sensor entity."""
        super().__init__(coordinator, config_entry, description, vehicle)
        self._tracker = tracker
        self._store = store

    @property
    def available(self) -> bool:
        """Return availability."""
        return self._available

    async def async_added_to_hass(self) -> None:
        """Register callbacks with DriveTracker."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._tracker.async_add_listener(self._handle_tracker_update)
        )

    @callback
    def _handle_tracker_update(self, _drive_state: DriveState) -> None:
        """Handle real-time update from DriveTracker."""
        self.async_write_ha_state()

    @property
    def native_value(self) -> StateType:
        """Return the native value of the sensor."""
        key = self.entity_description.key
        drives = self._store.drives
        last_drive = drives[-1] if drives else None

        if key == "last_drive_efficiency":
            return round(last_drive.efficiency_mi_kwh, 2) if last_drive else None

        if key == "efficiency_30d":
            stats = self._store.get_stats_30d()
            return round(stats.efficiency_mi_kwh, 2) if stats.drive_count > 0 else None

        if key == "efficiency_all_time":
            stats = self._store.get_stats_all_time()
            return round(stats.efficiency_mi_kwh, 2) if stats.drive_count > 0 else None

        if key == "last_drive_distance":
            return round(last_drive.distance_miles, 1) if last_drive else None

        if key == "last_drive_mpge":
            return round(last_drive.mpge, 1) if last_drive else None

        if key == "mpge_30d":
            stats = self._store.get_stats_30d()
            return round(stats.mpge, 1) if stats.drive_count > 0 else None

        if key == "mpge_all_time":
            stats = self._store.get_stats_all_time()
            return round(stats.mpge, 1) if stats.drive_count > 0 else None

        if key == "drive_status":
            return self._tracker.drive_state.status

        return None

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Return extra state attributes."""
        key = self.entity_description.key
        drives = self._store.drives
        last_drive = drives[-1] if drives else None

        if key == "last_drive_efficiency":
            if not last_drive:
                return None
            kwh_per_mi = (
                round(last_drive.energy_kwh / last_drive.distance_miles, 3)
                if last_drive.distance_miles > 0
                else 0.0
            )
            speed_bins_dict: dict[str, Any] = {}
            for bin_key, val in last_drive.speed_bins.items():
                if isinstance(val, SpeedBinData):
                    speed_bins_dict[bin_key] = val.to_dict()
                elif isinstance(val, dict):
                    speed_bins_dict[bin_key] = val
                elif isinstance(val, (int, float)):
                    speed_bins_dict[bin_key] = {
                        "miles": round(float(val), 3),
                        "seconds": 0.0,
                    }
                else:
                    speed_bins_dict[bin_key] = val

            return {
                "mpge": last_drive.mpge,
                "kwh_per_mi": kwh_per_mi,
                "distance": last_drive.distance_miles,
                "distance_miles": last_drive.distance_miles,
                "duration": last_drive.duration_seconds,
                "duration_seconds": last_drive.duration_seconds,
                "elevation_change_ft": last_drive.elevation_change_ft,
                "avg_speed": last_drive.avg_speed_mph,
                "avg_speed_mph": last_drive.avg_speed_mph,
                "max_speed": last_drive.max_speed_mph,
                "max_speed_mph": last_drive.max_speed_mph,
                "integrated_temp_f": last_drive.integrated_temperature_f,
                "speed_bins": speed_bins_dict,
                "start_time": last_drive.start_time,
                "end_time": last_drive.end_time,
                "start_soc": last_drive.start_soc,
                "end_soc": last_drive.end_soc,
                "energy_kwh": last_drive.energy_kwh,
                "is_micro_drive": last_drive.is_micro_drive,
            }

        if key in ("efficiency_30d", "efficiency_all_time"):
            stats = (
                self._store.get_stats_30d()
                if key == "efficiency_30d"
                else self._store.get_stats_all_time()
            )
            recent_drives = [
                {
                    "start_time": d.start_time,
                    "distance": round(d.distance_miles, 2),
                    "energy_kwh": round(d.energy_kwh, 2),
                    "efficiency": round(d.efficiency_mi_kwh, 2),
                    "mpge": round(d.mpge, 1),
                    "elevation_change_ft": round(d.elevation_change_ft, 0),
                    "avg_speed_mph": round(d.avg_speed_mph, 1),
                    "temp_f": (
                        round(d.integrated_temperature_f, 1)
                        if d.integrated_temperature_f is not None
                        else None
                    ),
                }
                for d in [
                    drive
                    for drive in self._store.drives
                    if not drive.is_micro_drive and drive.distance_miles >= 0.5
                ][-50:]
            ]
            return {
                "mpge": stats.mpge,
                "total_miles": stats.total_miles,
                "total_kwh": stats.total_kwh,
                "drive_count": stats.drive_count,
                "total_duration_seconds": stats.total_duration_seconds,
                "avg_distance_miles": stats.avg_distance_miles,
                "total_micro_drives": stats.total_micro_drives,
                "recent_drives": recent_drives,
            }

        if key == "last_drive_distance":
            if not last_drive:
                return None
            return {
                "start_time": last_drive.start_time,
                "end_time": last_drive.end_time,
                "duration_seconds": last_drive.duration_seconds,
                "energy_kwh": last_drive.energy_kwh,
                "is_micro_drive": last_drive.is_micro_drive,
            }

        if key == "last_drive_mpge":
            if not last_drive:
                return None
            return {
                "efficiency_mi_kwh": last_drive.efficiency_mi_kwh,
                "distance_miles": last_drive.distance_miles,
                "energy_kwh": last_drive.energy_kwh,
            }

        if key in ("mpge_30d", "mpge_all_time"):
            stats = (
                self._store.get_stats_30d()
                if key == "mpge_30d"
                else self._store.get_stats_all_time()
            )
            return {
                "total_miles": stats.total_miles,
                "total_kwh": stats.total_kwh,
                "drive_count": stats.drive_count,
                "efficiency_mi_kwh": stats.efficiency_mi_kwh,
            }

        if key == "drive_status":
            state = self._tracker.drive_state
            return {
                "is_driving": state.is_driving,
                "current_trip_distance_mi": state.current_trip_distance_mi,
                "current_trip_duration": state.current_trip_duration,
                "current_trip_duration_s": state.current_trip_duration,
                "current_trip_kwh": state.current_trip_kwh,
                "current_trip_efficiency": state.current_trip_efficiency,
                "current_speed_mph": state.current_speed_mph,
                "current_altitude_ft": state.current_altitude_ft,
                "gps_locked": state.gps_locked,
                "is_debouncing_park": self._tracker.is_debouncing_park,
            }

        return None
