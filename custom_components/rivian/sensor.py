"""Rivian (Unofficial)"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
import logging
from typing import Any, Final

from homeassistant.components.sensor import (
    DOMAIN as SENSOR_DOMAIN,
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    STATE_UNAVAILABLE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfEnergyDistance,
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
    REMOVED_SENSOR_KEYS,
    SENSORS,
    WEEK_DAYS_ORDERED,
)
from .coordinator import DriverKeyCoordinator, VehicleCoordinator, WallboxCoordinator
from .data_classes import (
    RivianNavigationSensorEntityDescription,
    RivianSensorEntityDescription,
    RivianWallboxSensorEntityDescription,
)
from .drive_models import DriveState, SpeedBinData
from .drive_storage import DriveStore
from .drive_tracker import DriveEvent, DriveTracker
from .entity import (
    RivianChargingEntity,
    RivianEntity,
    RivianVehicleEntity,
    RivianVehicleFreshnessEntity,
    RivianWallboxEntity,
)
from .helpers import async_remove_stale_entities

_LOGGER = logging.getLogger(__name__)

ALL_WEEK_DAYS: Final[frozenset[str]] = frozenset(WEEK_DAYS_ORDERED)
WEEKDAYS_ONLY: Final[frozenset[str]] = frozenset(WEEK_DAYS_ORDERED[:5])

RIVIAN_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%f%z"


def _parse_timestamp(val: datetime | str | None) -> datetime | None:
    """Parse a Rivian timestamp, which may already be a datetime (Parallax)."""
    if not val:
        return None
    if isinstance(val, str):
        return datetime.strptime(val, RIVIAN_TIMESTAMP_FORMAT).astimezone(UTC)
    return val.astimezone(UTC)


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
        for models, descriptions in SENSORS.items()
        if any(model in vehicle["model"] for model in models)
        for description in descriptions
        if description.supported_feature is None
        or description.supported_feature in vehicle.get("supported_features", [])
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
        entities.extend(
            RivianNavigationSensorEntity(coord, entry, description, vehicle)
            for description in NAVIGATION_SENSORS
        )
        entities.append(
            RivianNoUpdatesSinceEntity(coord, entry, NO_UPDATES_SINCE_SENSOR, vehicle)
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

    async_remove_stale_entities(
        hass,
        SENSOR_DOMAIN,
        (
            f"{vehicle['vin']}-{description.key}"
            for vehicle in vehicles.values()
            for descriptions in SENSORS.values()
            for description in descriptions
            # Keep feature-gated entities if the features didn't load
            if description.supported_feature is None
            or vehicle.get("supported_features")
        ),
        (entity.unique_id for entity in entities),
    )
    # TODO: Delete after 2027-04-01, with REMOVED_SENSOR_KEYS (see const.py)
    async_remove_stale_entities(
        hass,
        SENSOR_DOMAIN,
        (
            f"{vehicle['vin']}-{key}"
            for vehicle in vehicles.values()
            for key in REMOVED_SENSOR_KEYS
        ),
        (entity.unique_id for entity in entities),
    )
    async_add_entities(entities)


CHARGING_SCHEDULE_DAYS_SENSOR = RivianSensorEntityDescription(
    key="charging_schedule_days",
    translation_key="charging_schedule_days",
    entity_category=EntityCategory.DIAGNOSTIC,
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


def _waypoints(coordinator: VehicleCoordinator) -> list[dict[str, Any]]:
    """Return the trip's waypoints, or an empty list when not navigating."""
    return coordinator.get("waypoints") or []


# Statuses of the stop the vehicle is at: "stopping", then "arrived"
AT_STOP_STATUSES: Final = ("stopping", "arrived")


def _current_stop(coordinator: VehicleCoordinator) -> dict[str, Any] | None:
    """Return the stop the vehicle is at, if any."""
    waypoints = _waypoints(coordinator)
    if waypoints and waypoints[0].get("status") in AT_STOP_STATUSES:
        return waypoints[0]
    return None


def _next_stop(coordinator: VehicleCoordinator) -> dict[str, Any] | None:
    """Return the trip's next stop, past the one the vehicle is at."""
    waypoints = _waypoints(coordinator)
    current = _current_stop(coordinator)
    return next(
        (waypoint for waypoint in waypoints if waypoint.get("status") == "next_stop"),
        None,
    ) or next(
        (waypoint for waypoint in waypoints if waypoint is not current),
        None,
    )


def _next_stop_attributes(coordinator: VehicleCoordinator) -> dict[str, Any] | None:
    """Return the next stop's details, and the stop the vehicle is at."""
    attributes = _waypoint_attributes(_next_stop(coordinator))
    current = _current_stop(coordinator)
    if attributes is None and current is None:
        return None
    return (attributes or {}) | {"arrived_at": _waypoint_name(current)}


def _next_stop_arrival(coordinator: VehicleCoordinator) -> datetime | None:
    """Return the next stop's live arrival time, or its planned one."""
    if not (waypoint := _next_stop(coordinator)):
        return None
    if not _current_stop(coordinator):
        return _arrival(waypoint, coordinator, "nextWaypointArrivalTime")
    # At a stop, trip_progress counts down to it (sliding later while parked)
    # and the planned arrival isn't updated, so add the drive on from there
    index = next(i for i, w in enumerate(_waypoints(coordinator)) if w is waypoint)
    legs = (coordinator.get("legs") or [])[1 : index + 1]
    if (
        (leaving := _live_arrival(coordinator, "nextWaypointArrivalTime"))
        and len(legs) == index
        and all(leg.get("duration") is not None for leg in legs)
    ):
        return leaving + timedelta(seconds=sum(leg["duration"] for leg in legs))
    return waypoint.get("arrivalTime")


def _destination(coordinator: VehicleCoordinator) -> dict[str, Any] | None:
    """Return the trip's final stop."""
    return waypoints[-1] if (waypoints := _waypoints(coordinator)) else None


def _waypoint_name(waypoint: dict[str, Any] | None) -> str | None:
    """Return a stop's name, or a charging stop's label (its address)."""
    if not waypoint:
        return None
    return waypoint.get("name") or waypoint.get("label")


def _live_arrival(coordinator: VehicleCoordinator, field: str) -> datetime | None:
    """Return trip_progress's live arrival time, if sent since the trip was planned.

    trip_progress keeps resending the last trip's values after navigation ends,
    so they're only used when newer than the trip's waypoints.
    """
    data = coordinator.data or {}
    progress = data.get(field) or {}
    trip = data.get("waypoints") or {}
    if (
        (arrival := progress.get("value"))
        and (progress_time := progress.get("timeStamp"))
        and (trip_time := trip.get("timeStamp"))
        and datetime.fromisoformat(progress_time) >= datetime.fromisoformat(trip_time)
    ):
        return arrival
    return None


def _arrival(
    waypoint: dict[str, Any] | None, coordinator: VehicleCoordinator, live_field: str
) -> datetime | None:
    """Return a stop's live arrival time, or its planned one."""
    if not waypoint:
        return None
    return _live_arrival(coordinator, live_field) or waypoint.get("arrivalTime")


def _waypoint_attributes(waypoint: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a stop's location and charge details."""
    if not waypoint:
        return None
    attributes = {
        "type": waypoint.get("type"),
        "latitude": waypoint.get("latitude"),
        "longitude": waypoint.get("longitude"),
    }
    if waypoint.get("type") == "chargingStop":
        attributes["arrival_state_of_charge"] = waypoint.get("arrivalStateOfCharge")
        attributes["departure_state_of_charge"] = waypoint.get("departureStateOfCharge")
        attributes["charge_duration"] = waypoint.get("chargeDuration")
    else:
        attributes["arrival_state_of_charge"] = waypoint.get("stateOfCharge")
    return attributes


NAVIGATION_SENSORS: Final[tuple[RivianNavigationSensorEntityDescription, ...]] = (
    RivianNavigationSensorEntityDescription(
        key="next_stop",
        translation_key="next_stop",
        value_fn=lambda coor: _waypoint_name(_next_stop(coor)),
        attributes_fn=_next_stop_attributes,
    ),
    RivianNavigationSensorEntityDescription(
        key="next_stop_eta",
        translation_key="next_stop_eta",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_next_stop_arrival,
    ),
    RivianNavigationSensorEntityDescription(
        key="destination",
        translation_key="destination",
        value_fn=lambda coor: _waypoint_name(_destination(coor)),
        attributes_fn=lambda coor: _waypoint_attributes(_destination(coor)),
    ),
    RivianNavigationSensorEntityDescription(
        key="destination_eta",
        translation_key="destination_eta",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda coor: _arrival(
            _destination(coor), coor, "finalDestinationArrivalTime"
        ),
    ),
)


NO_UPDATES_SINCE_SENSOR = SensorEntityDescription(
    key="no_updates_since",
    translation_key="no_updates_since",
    device_class=SensorDeviceClass.TIMESTAMP,
    entity_category=EntityCategory.DIAGNOSTIC,
)


class RivianNoUpdatesSinceEntity(RivianVehicleFreshnessEntity, SensorEntity):
    """When the vehicle last reported data, shown only once that's a while ago."""

    @property
    def native_value(self) -> datetime | None:
        """Return the last report time while updates are stale, else None."""
        return self.coordinator.stale_since


class RivianNavigationSensorEntity(RivianVehicleEntity, SensorEntity):
    """Rivian navigation sensor entity, unknown while not navigating."""

    entity_description: RivianNavigationSensorEntityDescription

    @property
    def available(self) -> bool:
        """Return availability."""
        return self._available

    @property
    def native_value(self) -> StateType | datetime:
        """Return the state."""
        return self.entity_description.value_fn(self.coordinator)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Return the stop's details."""
        return self.entity_description.attributes_fn(self.coordinator)


class RivianSensorEntity(RivianVehicleEntity, RestoreSensor):
    """Representation of a Rivian sensor entity."""

    entity_description: RivianSensorEntityDescription
    _last_value: Any = None

    async def async_added_to_hass(self) -> None:
        """Restore the last value of a sensor that keeps it while unreported."""
        await super().async_added_to_hass()
        if self.entity_description.restore and (
            last := await self.async_get_last_sensor_data()
        ):
            value = last.native_value
            if (options := self.options) and value not in options:
                # Saved before the enum values were lowercased, or since removed
                value = value.lower() if str(value).lower() in options else None
            self._last_value = value

    @property
    def available(self) -> bool:
        """Return the availability of the entity."""
        if self._last_value is not None and self._available:
            return True
        return super().available

    @property
    def native_value(self) -> str | None:
        """Return the value reported by the sensor."""
        if _fn := self.entity_description.value_fn:
            return _fn(self.coordinator)

        if (val := self._get_value(self.entity_description.field)) is None:
            if self._last_value is not None:
                return self._last_value
            return STATE_UNAVAILABLE if not self.native_unit_of_measurement else None

        rval = _fn(val) if (_fn := self.entity_description.value_lambda) else val
        if self.entity_description.restore:
            self._last_value = rval
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
        translation_key="charging_cost",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
    ),
    RivianSensorEntityDescription(
        key="charging_energy_delivered",
        field="totalChargedEnergy",
        translation_key="charging_energy_delivered",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=1,
    ),
    RivianSensorEntityDescription(
        key="charging_range_added",
        field="rangeAddedThisSession",
        translation_key="charging_range_added",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_unit_of_measurement=UnitOfLength.MILES,
    ),
    RivianSensorEntityDescription(
        key="charging_rate",
        field="kilometersChargedPerHour",
        translation_key="charging_rate",
        device_class=SensorDeviceClass.SPEED,
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_unit_of_measurement=UnitOfSpeed.MILES_PER_HOUR,
    ),
    RivianSensorEntityDescription(
        key="charging_speed",
        field="power",
        translation_key="charging_speed",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    RivianSensorEntityDescription(
        key="charging_start_time",
        field="startTime",
        translation_key="charging_start_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_lambda=_parse_timestamp,
    ),
    RivianSensorEntityDescription(
        key="charging_time_elapsed",
        # Minutes spent charging, as shown in the Rivian app (pauses while
        # stopped/scheduled, unlike timeElapsed)
        field="activeChargingTime",
        translation_key="charging_time_elapsed",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_lambda=lambda val: val * 60 if val is not None else None,
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
        device_class=SensorDeviceClass.ENUM,
        options=["unavailable", "available", "disconnected", "plugged_in", "charging"],
        translation_key="charging_status",
    ),
    RivianWallboxSensorEntityDescription(
        key="amperage",
        field="currentAmps",
        translation_key="amperage",
        device_class=SensorDeviceClass.CURRENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    RivianWallboxSensorEntityDescription(
        key="amperage_maximum",
        field="maxAmps",
        translation_key="amperage_maximum",
        device_class=SensorDeviceClass.CURRENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    RivianWallboxSensorEntityDescription(
        key="power",
        field="power",
        translation_key="power",
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
        translation_key="power_maximum",
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
        translation_key="voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    RivianWallboxSensorEntityDescription(
        key="voltage_maximum",
        field="maxVoltage",
        translation_key="voltage_maximum",
        device_class=SensorDeviceClass.VOLTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
)

DRIVER_SENSORS: Final[tuple[RivianSensorEntityDescription, ...]] = (
    RivianSensorEntityDescription(
        key="drivers",
        translation_key="drivers",
        entity_category=EntityCategory.DIAGNOSTIC,
        field="invitedUsers",
        value_lambda=lambda data: len(
            [user for user in (data or []) if user["__typename"] == "ProvisionedUser"]
        ),
    ),
    RivianSensorEntityDescription(
        key="keys",
        translation_key="keys",
        entity_category=EntityCategory.DIAGNOSTIC,
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


_UNSET: Final[object] = object()


class RivianDriveSensorEntity(RivianVehicleEntity, SensorEntity):
    """Representation of a Rivian drive efficiency and status sensor entity."""

    entity_description: RivianSensorEntityDescription
    # Bulk analytics payloads stay out of the recorder (16 KiB attribute limit)
    _unrecorded_attributes = frozenset(
        {
            "recent_drives",
            "recent_segments",
            "recent_vampire_events",
            "recent_dcfc_sessions",
            "speed_bins",
            "stats_90d",
            "stats_365d",
        }
    )

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
        self._last_written_value: Any = _UNSET
        self._last_written_available: Any = _UNSET
        self._attrs_cache_revision: int | None = None
        self._attrs_cache: Mapping[str, Any] | None = None

    @property
    def available(self) -> bool:
        """Return availability."""
        return self._available

    @property
    def suggested_unit_of_measurement(self) -> str | None:
        """Suggest km/kWh for efficiency on a metric Home Assistant.

        The sensors report mi/kWh; their device class lets anyone pick
        mi/kWh, km/kWh or kWh/100 km in the entity settings.
        """
        if (
            self.entity_description.device_class == SensorDeviceClass.ENERGY_DISTANCE
            and self.hass is not None
            and self.hass.config.units.length_unit == UnitOfLength.KILOMETERS
        ):
            return UnitOfEnergyDistance.KM_PER_KILO_WATT_HOUR
        return super().suggested_unit_of_measurement

    async def async_added_to_hass(self) -> None:
        """Register callbacks with DriveTracker."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._tracker.async_add_listener(self._handle_tracker_update)
        )

    @callback
    def _handle_tracker_update(
        self, _drive_state: DriveState, event: DriveEvent
    ) -> None:
        """Handle real-time update from DriveTracker.

        Only the live drive-status entity needs to refresh on every telemetry
        tick; the other seven drive-analytics entities only change meaningfully
        once a drive fully completes.
        """
        if (
            event != DriveEvent.DRIVE_COMPLETE
            and self.entity_description.key != "drive_status"
        ):
            return
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated coordinator data, writing state only on change."""
        new_value = self.native_value
        new_available = self.available
        if (
            new_value == self._last_written_value
            and new_available == self._last_written_available
        ):
            return
        self._last_written_value = new_value
        self._last_written_available = new_available
        super()._handle_coordinator_update()

    @property
    def native_value(self) -> StateType:
        """Return the native value of the sensor."""
        key = self.entity_description.key
        last_drive = self._store.last_drive

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
        last_drive = self._store.last_drive

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
            revision = self._store.revision
            if self._attrs_cache_revision == revision and self._attrs_cache is not None:
                return self._attrs_cache

            stats = (
                self._store.get_stats_30d()
                if key == "efficiency_30d"
                else self._store.get_stats_all_time()
            )
            stats_90d = self._store.get_stats_90d()
            stats_365d = self._store.get_stats_365d()

            attrs: dict[str, Any] = {
                "mpge": stats.mpge,
                "total_miles": stats.total_miles,
                "total_kwh": stats.total_kwh,
                "drive_count": stats.drive_count,
                "total_duration_seconds": stats.total_duration_seconds,
                "avg_distance_miles": stats.avg_distance_miles,
                "total_micro_drives": stats.total_micro_drives,
                "stats_90d": stats_90d.to_dict(),
                "stats_365d": stats_365d.to_dict(),
            }

            self._attrs_cache_revision = revision
            self._attrs_cache = attrs
            return attrs

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
                "stats_90d": self._store.get_stats_90d().to_dict(),
                "stats_365d": self._store.get_stats_365d().to_dict(),
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
