"""Rivian (Unofficial)"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
import logging
import math
from typing import Any, Final

from homeassistant.components.sensor import (
    DOMAIN as SENSOR_DOMAIN,
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    STATE_UNAVAILABLE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfLength,
    UnitOfPower,
    UnitOfSpeed,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.typing import StateType
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import DistanceConverter, SpeedConverter

from .config_flow import CONF_WEATHER_LOOKUPS, DEFAULT_WEATHER_LOOKUPS
from .const import DOMAIN, REMOVED_SENSOR_KEYS, SENSORS, WEEK_DAYS_ORDERED
from .coordinator import DriverKeyCoordinator, RivianConfigEntry, VehicleCoordinator
from .data_classes import (
    RivianNavigationSensorEntityDescription,
    RivianSensorEntityDescription,
    RivianWallboxSensorEntityDescription,
)
from .entity import (
    RivianChargingEntity,
    RivianEntity,
    RivianVehicleEntity,
    RivianVehicleFreshnessEntity,
    RivianWallboxEntity,
)
from .helpers import async_remove_stale_entities
from .weather import OpenMeteoWeatherClient

_LOGGER = logging.getLogger(__name__)

ALL_WEEK_DAYS: Final[frozenset[str]] = frozenset(WEEK_DAYS_ORDERED)
WEEKDAYS_ONLY: Final[frozenset[str]] = frozenset(WEEK_DAYS_ORDERED[:5])

RIVIAN_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%f%z"


# Converts an accuracy to the unit a sensor is shown in, which Home Assistant
# does for its state but not its attributes
ACCURACY_CONVERTERS: Final = {
    SensorDeviceClass.DISTANCE: DistanceConverter,
    SensorDeviceClass.SPEED: SpeedConverter,
}


def _accuracy(
    description: RivianSensorEntityDescription, error: float, unit: str | None
) -> float:
    """Return an error estimate in the unit the sensor is shown in."""
    native = description.native_unit_of_measurement
    if (
        (converter := ACCURACY_CONVERTERS.get(description.device_class))
        and unit
        and unit != native
    ):
        error = converter.convert(error, native, unit)
    return round(error, 1)


def _parse_timestamp(val: datetime | str | None) -> datetime | None:
    """Parse a Rivian timestamp, which may already be a datetime (Parallax)."""
    if not val:
        return None
    if isinstance(val, str):
        return datetime.strptime(val, RIVIAN_TIMESTAMP_FORMAT).astimezone(UTC)
    return val.astimezone(UTC)


def _supports(vehicle: dict[str, Any], feature: str | frozenset[str] | None) -> bool:
    """Return whether a vehicle reports a supported feature, or all of a set."""
    if feature is None:
        return True
    features = set(vehicle.get("supported_features") or [])
    return feature in features if isinstance(feature, str) else feature <= features


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RivianConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensor entities."""
    vehicles = entry.runtime_data.vehicles

    # Add vehicle entities
    vehicle_coordinators = entry.runtime_data.vehicle_coordinators
    entities = [
        RivianSensorEntity(
            vehicle_coordinators[vehicle_id], entry, description, vehicle
        )
        for vehicle_id, vehicle in vehicles.items()
        for models, descriptions in SENSORS.items()
        if any(model in vehicle["model"] for model in models)
        for description in descriptions
        if _supports(vehicle, description.supported_feature)
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
    wallbox_coordinator = entry.runtime_data.wallbox_coordinator
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

    # Outside temperature, only with the option on: it sends each vehicle's
    # location (rounded to about 1 km) to Open-Meteo
    if entry.options.get(CONF_WEATHER_LOOKUPS, DEFAULT_WEATHER_LOOKUPS):
        weather_client = OpenMeteoWeatherClient(hass=hass)
        entities.extend(
            RivianOutsideTemperatureEntity(
                vehicle_coordinators[vehicle_id],
                entry,
                OUTSIDE_TEMPERATURE_SENSOR,
                vehicle,
                weather_client,
            )
            for vehicle_id, vehicle in vehicles.items()
        )
    async_remove_stale_entities(
        hass,
        SENSOR_DOMAIN,
        (
            f"{vehicle['vin']}-{OUTSIDE_TEMPERATURE_SENSOR.key}"
            for vehicle in vehicles.values()
        ),
        (entity.unique_id for entity in entities),
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
        (leaving := _live_progress(coordinator, "nextWaypointArrivalTime"))
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


def _live_progress(coordinator: VehicleCoordinator, field: str) -> Any:
    """Return a trip_progress value, if sent since the trip was planned.

    trip_progress keeps resending the last trip's values after navigation ends,
    so they're only used when newer than the trip's waypoints.
    """
    data = coordinator.data or {}
    progress = data.get(field) or {}
    trip = data.get("waypoints") or {}
    if (
        (value := progress.get("value")) is not None
        and (progress_time := progress.get("timeStamp"))
        and (trip_time := trip.get("timeStamp"))
        and datetime.fromisoformat(progress_time) >= datetime.fromisoformat(trip_time)
    ):
        return value
    return None


def _next_stop_progress(coordinator: VehicleCoordinator, field: str) -> Any:
    """Return trip_progress's countdown to the next stop, unknown while at a stop.

    At a stop, trip_progress counts down to that stop, not the next one.
    """
    if not _next_stop(coordinator) or _current_stop(coordinator):
        return None
    return _live_progress(coordinator, field)


def _arrival(
    waypoint: dict[str, Any] | None, coordinator: VehicleCoordinator, live_field: str
) -> datetime | None:
    """Return a stop's live arrival time, or its planned one."""
    if not waypoint:
        return None
    return _live_progress(coordinator, live_field) or waypoint.get("arrivalTime")


def _round(value: Any, digits: int | None = None) -> Any:
    """Round a number, leaving anything else (such as None) as it is."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return round(value, digits)
    return value


def _coordinate(value: Any) -> Any:
    """Round a stop's latitude or longitude to 6 decimals, like the vehicle's."""
    return _round(value, 6)


def _waypoint_attributes(waypoint: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a stop's location and charge details (charge in whole percent)."""
    if not waypoint:
        return None
    attributes = {
        "type": waypoint.get("type"),
        "latitude": _coordinate(waypoint.get("latitude")),
        "longitude": _coordinate(waypoint.get("longitude")),
    }
    if waypoint.get("type") == "chargingStop":
        attributes["arrival_state_of_charge"] = _round(
            waypoint.get("arrivalStateOfCharge")
        )
        attributes["departure_state_of_charge"] = _round(
            waypoint.get("departureStateOfCharge")
        )
        attributes["charge_duration"] = waypoint.get("chargeDuration")
    else:
        attributes["arrival_state_of_charge"] = _round(waypoint.get("stateOfCharge"))
    return attributes


def _destination_attributes(coordinator: VehicleCoordinator) -> dict[str, Any] | None:
    """Return the destination's details, plus the trip's route for the trip card."""
    if (
        attributes := _waypoint_attributes(destination := _destination(coordinator))
    ) is None:
        return None
    next_stop = _next_stop(coordinator)
    return attributes | {
        # Google-encoded polyline of the whole trip, and every stop (both
        # excluded from the recorder)
        "route": coordinator.get("overviewPolyline"),
        "stops": [
            {
                "name": _waypoint_name(waypoint),
                "latitude": _coordinate(waypoint.get("latitude")),
                "longitude": _coordinate(waypoint.get("longitude")),
                "charging": waypoint.get("type") == "chargingStop",
                "arrival": waypoint.get("arrivalTime"),
                "next": waypoint is next_stop,
            }
            for waypoint in _waypoints(coordinator)
        ],
        "trip_start": coordinator.get("originTime"),
        "arrival": _arrival(destination, coordinator, "finalDestinationArrivalTime"),
    }


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
        key="next_stop_distance",
        translation_key="next_stop_distance",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.METERS,
        suggested_display_precision=1,
        suggested_unit_of_measurement=UnitOfLength.MILES,
        value_fn=lambda coor: _next_stop_progress(coor, "distanceRemaining"),
    ),
    RivianNavigationSensorEntityDescription(
        key="next_stop_drive_time",
        translation_key="next_stop_drive_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_display_precision=0,
        suggested_unit_of_measurement=UnitOfTime.MINUTES,
        value_fn=lambda coor: _next_stop_progress(coor, "durationRemaining"),
    ),
    RivianNavigationSensorEntityDescription(
        key="destination",
        translation_key="destination",
        value_fn=lambda coor: _waypoint_name(_destination(coor)),
        attributes_fn=_destination_attributes,
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


OUTSIDE_TEMPERATURE_SENSOR = SensorEntityDescription(
    key="outside_temperature",
    translation_key="outside_temperature",
    device_class=SensorDeviceClass.TEMPERATURE,
    state_class=SensorStateClass.MEASUREMENT,
    # The weather client asks Open-Meteo for Fahrenheit
    native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
)
# Open-Meteo's current conditions change every 15 minutes
OUTSIDE_TEMPERATURE_REFRESH = timedelta(minutes=15)
# Moving further than this from the last lookup looks the temperature up again
OUTSIDE_TEMPERATURE_MOVE_M: Final = 1000.0
# ...but never more often than this, so a drive sends a few requests at most
OUTSIDE_TEMPERATURE_MIN_GAP = timedelta(minutes=5)
# A value older than this (the lookups keep failing) is shown as unavailable
OUTSIDE_TEMPERATURE_MAX_AGE = timedelta(hours=1)
_EARTH_RADIUS_M: Final = 6_371_000.0


def _distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Return the great-circle distance between two (lat, lon) points in metres."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, h)))


class RivianOutsideTemperatureEntity(RivianVehicleEntity, SensorEntity):
    """Open-Meteo's current temperature at the vehicle's location.

    The vehicle doesn't report an outside temperature. It's looked up when
    the location is first known, 15 minutes after each lookup, and when the
    vehicle has moved over a kilometre from the last lookup, at most once per
    5 minutes. The weather client rounds the location to about 1 km.
    """

    def __init__(
        self,
        coordinator: VehicleCoordinator,
        config_entry: RivianConfigEntry,
        description: SensorEntityDescription,
        vehicle: dict[str, Any],
        client: OpenMeteoWeatherClient,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator, config_entry, description, vehicle)
        self._client = client
        self._value: float | None = None
        self._value_time: datetime | None = None
        self._lookup_time: datetime | None = None
        self._lookup_location: tuple[float, float] | None = None
        self._looking_up = False
        self._unsub_refresh: Any = None
        self._lookup_task: asyncio.Task[None] | None = None

    async def async_added_to_hass(self) -> None:
        """Look the temperature up once the vehicle's location is known."""
        await super().async_added_to_hass()
        self.async_on_remove(self._async_stop)
        self._async_maybe_look_up(refresh=False)

    @property
    def available(self) -> bool:
        """Return True while the looked-up value is recent enough."""
        return (
            self._value is not None
            and self._value_time is not None
            and dt_util.utcnow() - self._value_time <= OUTSIDE_TEMPERATURE_MAX_AGE
        )

    @property
    def native_value(self) -> float | None:
        """Return the last looked-up temperature."""
        return self._value

    def _location(self) -> tuple[float, float] | None:
        """Return the vehicle's (lat, lon), or None when it isn't known."""
        # The fix itself, as the device tracker reads it: it has no "value"
        # key, so _get_value can't return it
        data = self.coordinator.data
        location = data.get("gnssLocation") if data else None
        if not isinstance(location, dict):
            return None
        try:
            latitude = float(location["latitude"])
            longitude = float(location["longitude"])
        except (KeyError, TypeError, ValueError):
            return None
        if not (math.isfinite(latitude) and math.isfinite(longitude)):
            return None
        return latitude, longitude

    @callback
    def _cancel_refresh(self) -> None:
        """Cancel the scheduled refresh."""
        if self._unsub_refresh:
            self._unsub_refresh()
            self._unsub_refresh = None

    @callback
    def _async_stop(self) -> None:
        """Stop the refresh timer and any lookup still running."""
        self._cancel_refresh()
        if self._lookup_task is not None:
            self._lookup_task.cancel()
            self._lookup_task = None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Look the temperature up again once the vehicle has moved far enough."""
        self._async_maybe_look_up(refresh=False)
        self.async_write_ha_state()

    @callback
    def _async_refresh(self, _now: datetime) -> None:
        """Look the temperature up again where the vehicle is now."""
        self._unsub_refresh = None
        self._async_maybe_look_up(refresh=True)
        if not self._looking_up:
            # No location yet: check again later
            self._unsub_refresh = async_call_later(
                self.hass, OUTSIDE_TEMPERATURE_REFRESH, self._async_refresh
            )
        # Turns the value unavailable once lookups have failed for an hour
        self.async_write_ha_state()

    @callback
    def _async_maybe_look_up(self, *, refresh: bool) -> None:
        """Start a lookup if one is due."""
        if self._looking_up or (location := self._location()) is None:
            return
        now = dt_util.utcnow()
        if self._lookup_time is not None and self._lookup_location is not None:
            if now - self._lookup_time < OUTSIDE_TEMPERATURE_MIN_GAP:
                return
            if (
                not refresh
                and _distance_m(self._lookup_location, location)
                <= OUTSIDE_TEMPERATURE_MOVE_M
            ):
                return
        self._looking_up = True
        self._lookup_time = now
        self._lookup_location = location
        self._cancel_refresh()
        self._lookup_task = self.hass.async_create_background_task(
            self._async_look_up(location), f"{self.entity_id} outside temperature"
        )

    async def _async_look_up(self, location: tuple[float, float]) -> None:
        """Look the temperature up, then schedule the next refresh."""
        try:
            value = await self._client.async_get_current_temperature(*location)
        except Exception:
            _LOGGER.debug("Outside temperature lookup failed", exc_info=True)
            value = None
        finally:
            # Also on removal, which cancels the lookup and ends here
            self._looking_up = False
            self._lookup_task = None
        # Counted from the lookup's end, so the client's 15-minute cache of
        # this spot has expired by then
        self._unsub_refresh = async_call_later(
            self.hass, OUTSIDE_TEMPERATURE_REFRESH, self._async_refresh
        )
        if value is not None:
            self._value = value
            self._value_time = dt_util.utcnow()
        self.async_write_ha_state()


class RivianNavigationSensorEntity(RivianVehicleEntity, SensorEntity):
    """Rivian navigation sensor entity, unknown while not navigating."""

    # The destination's route for the trip card can exceed the recorder's
    # attribute size limit, the stops' arrival times change throughout a trip,
    # and the live arrival time is recorded by the Destination ETA sensor
    _unrecorded_attributes = frozenset({"route", "stops", "arrival"})

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
    # Changes with every fix, so it's left out of the recorder
    _unrecorded_attributes = frozenset({"accuracy"})

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Return the estimated error of the value, when known."""
        description = self.entity_description
        if not description.gnss_error or (
            (error := self.coordinator.gnss_error(description.gnss_error)) is None
        ):
            return None
        return {"accuracy": _accuracy(description, error, self.unit_of_measurement)}

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
