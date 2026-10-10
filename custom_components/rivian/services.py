"""Rivian services."""

from __future__ import annotations

from datetime import time
from typing import Any, Final

import voluptuous as vol

from homeassistant.const import ATTR_DEVICE_ID, ATTR_NAME, ATTR_TEMPERATURE, ATTR_TIME
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr

from .const import (
    ATTR_COORDINATOR,
    ATTR_VEHICLE,
    DEFAULT_DEPARTURE_SCHEDULE,
    DEPARTURE_SCHEDULE_DEFROST_MODES,
    DEPARTURE_SCHEDULE_NAME_MAX_LENGTH,
    DEPARTURE_SCHEDULE_SURFACE_LEVELS,
    DEPARTURE_SCHEDULE_SURFACES,
    DEPARTURE_SCHEDULE_TEMPERATURE_MAXIMUM,
    DEPARTURE_SCHEDULE_TEMPERATURE_MINIMUM,
    DOMAIN,
    MINUTES_PER_HOUR,
    PRECONDITION_SCHEDULE_NAME,
    WEEK_DAYS_ORDERED,
)
from .coordinator import VehicleCoordinator
from .helpers import deep_merge

ATTR_DAYS: Final = "days"
ATTR_ENABLED: Final = "enabled"
ATTR_FRONT_DEFROST: Final = "front_defrost"
ATTR_OVERRIDE_CHARGE_SCHEDULE: Final = "override_charge_schedule"
ATTR_SCHEDULE_ID: Final = "schedule_id"

SERVICE_CREATE_DEPARTURE_SCHEDULE: Final = "create_departure_schedule"
SERVICE_UPDATE_DEPARTURE_SCHEDULE: Final = "update_departure_schedule"
SERVICE_DELETE_DEPARTURE_SCHEDULE: Final = "delete_departure_schedule"


def _not_reserved_name(value: str) -> str:
    """Reject names the Precondition Cabin button uses for its own schedules.

    The button names its temporary schedules ``"HA Precondition <marker>"``, so the
    whole prefix is reserved, not just the exact base name.
    """
    if value.strip().casefold().startswith(PRECONDITION_SCHEDULE_NAME.casefold()):
        raise vol.Invalid(
            f'Names starting with "{PRECONDITION_SCHEDULE_NAME}" are reserved for the '
            "Precondition Cabin button"
        )
    return value


_NAME_SCHEMA = vol.All(
    cv.string, vol.Length(max=DEPARTURE_SCHEDULE_NAME_MAX_LENGTH), _not_reserved_name
)
_DAYS_SCHEMA = vol.All(cv.ensure_list, vol.Length(min=1), [vol.In(WEEK_DAYS_ORDERED)])
_SETTINGS_SCHEMA: Final[dict[vol.Marker, Any]] = {
    vol.Optional(ATTR_ENABLED): cv.boolean,
    vol.Optional(ATTR_TEMPERATURE): vol.All(
        vol.Coerce(float),
        vol.Range(
            min=DEPARTURE_SCHEDULE_TEMPERATURE_MINIMUM,
            max=DEPARTURE_SCHEDULE_TEMPERATURE_MAXIMUM,
        ),
    ),
    vol.Optional(ATTR_FRONT_DEFROST): vol.In(DEPARTURE_SCHEDULE_DEFROST_MODES),
    **{
        vol.Optional(surface): vol.In(levels)
        for surface, levels in DEPARTURE_SCHEDULE_SURFACE_LEVELS.items()
    },
    vol.Optional(ATTR_OVERRIDE_CHARGE_SCHEDULE): cv.boolean,
}

CREATE_DEPARTURE_SCHEDULE_SCHEMA: Final = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_NAME): _NAME_SCHEMA,
        vol.Required(ATTR_TIME): cv.time,
        vol.Required(ATTR_DAYS): _DAYS_SCHEMA,
        **_SETTINGS_SCHEMA,
    }
)
UPDATE_DEPARTURE_SCHEDULE_SCHEMA: Final = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_SCHEDULE_ID): cv.string,
        vol.Optional(ATTR_NAME): _NAME_SCHEMA,
        vol.Optional(ATTR_TIME): cv.time,
        vol.Optional(ATTR_DAYS): _DAYS_SCHEMA,
        **_SETTINGS_SCHEMA,
    }
)
DELETE_DEPARTURE_SCHEDULE_SCHEMA: Final = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_SCHEDULE_ID): cv.string,
    }
)


def _get_coordinator(hass: HomeAssistant, device_id: str) -> VehicleCoordinator:
    """Get the vehicle coordinator for a device."""
    if device := dr.async_get(hass).async_get(device_id):
        for entry_data in hass.data.get(DOMAIN, {}).values():
            coordinators = entry_data[ATTR_COORDINATOR][ATTR_VEHICLE]
            for vehicle_id, vehicle in entry_data[ATTR_VEHICLE].items():
                if device.identifiers & {
                    (DOMAIN, vehicle_id),
                    (DOMAIN, vehicle["vin"]),
                }:
                    return coordinators[vehicle_id]
    raise ServiceValidationError(f"{device_id} is not a loaded Rivian vehicle")


def _get_schedule_changes(data: dict[str, Any]) -> dict[str, Any]:
    """Convert service data to departure schedule changes."""
    changes: dict[str, Any] = {}
    weekly: dict[str, Any] = {}
    settings: dict[str, Any] = {}
    comfort: dict[str, Any] = {}

    if ATTR_NAME in data:
        changes["name"] = data[ATTR_NAME]
    if ATTR_ENABLED in data:
        changes["isEnabled"] = data[ATTR_ENABLED]
    if ATTR_DAYS in data:
        weekly["days"] = [day for day in WEEK_DAYS_ORDERED if day in data[ATTR_DAYS]]
    if ATTR_TIME in data:
        value: time = data[ATTR_TIME]
        weekly["startsAtMin"] = value.hour * MINUTES_PER_HOUR + value.minute
    if ATTR_TEMPERATURE in data:
        comfort["cabinTempCelsius"] = data[ATTR_TEMPERATURE]
    if ATTR_FRONT_DEFROST in data:
        comfort["frontDefogDefrost"] = data[ATTR_FRONT_DEFROST]
    if levels := {
        key: data[surface]
        for surface, key in DEPARTURE_SCHEDULE_SURFACES.items()
        if surface in data
    }:
        comfort["surfaceHeatVentLevels"] = levels
    if ATTR_OVERRIDE_CHARGE_SCHEDULE in data:
        settings["shouldOverrideChargeSchedule"] = data[ATTR_OVERRIDE_CHARGE_SCHEDULE]

    if weekly:
        changes["repeatsWeekly"] = weekly
    if comfort:
        settings["comfortSettings"] = comfort
    if settings:
        changes["departureSettings"] = settings
    return changes


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Set up the Rivian services."""

    async def create_departure_schedule(call: ServiceCall) -> None:
        """Create a departure schedule."""
        coordinator = _get_coordinator(hass, call.data[ATTR_DEVICE_ID])
        await coordinator.create_departure_schedule(
            deep_merge(DEFAULT_DEPARTURE_SCHEDULE, _get_schedule_changes(call.data))
        )

    async def update_departure_schedule(call: ServiceCall) -> None:
        """Update a departure schedule."""
        coordinator = _get_coordinator(hass, call.data[ATTR_DEVICE_ID])
        await coordinator.update_departure_schedule(
            call.data[ATTR_SCHEDULE_ID], _get_schedule_changes(call.data)
        )

    async def delete_departure_schedule(call: ServiceCall) -> None:
        """Delete a departure schedule."""
        coordinator = _get_coordinator(hass, call.data[ATTR_DEVICE_ID])
        await coordinator.delete_departure_schedule(call.data[ATTR_SCHEDULE_ID])

    hass.services.async_register(
        DOMAIN,
        SERVICE_CREATE_DEPARTURE_SCHEDULE,
        create_departure_schedule,
        schema=CREATE_DEPARTURE_SCHEDULE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_UPDATE_DEPARTURE_SCHEDULE,
        update_departure_schedule,
        schema=UPDATE_DEPARTURE_SCHEDULE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_DELETE_DEPARTURE_SCHEDULE,
        delete_departure_schedule,
        schema=DELETE_DEPARTURE_SCHEDULE_SCHEMA,
    )
