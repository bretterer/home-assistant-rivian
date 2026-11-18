"""Rivian services."""

from __future__ import annotations

from datetime import time
from typing import Any, Final

import voluptuous as vol

from homeassistant.auth.permissions.const import CAT_ENTITIES, POLICY_CONTROL
from homeassistant.const import (
    ATTR_DEVICE_ID,
    ATTR_NAME,
    ATTR_TEMPERATURE,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError, Unauthorized, UnknownUser
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.util.unit_conversion import TemperatureConverter

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
    PRECONDITION_LEAD_MINUTES_MAXIMUM,
    PRECONDITION_LEAD_MINUTES_MINIMUM,
    PRECONDITION_SCHEDULE_NAME,
    WEEK_DAYS_ORDERED,
)
from .coordinator import VehicleCoordinator, is_precondition_schedule
from .helpers import deep_merge, schedule_cabin_temperature

ATTR_DAYS: Final = "days"
ATTR_DEPARTURE_TIME: Final = "departure_time"
ATTR_ENABLED: Final = "enabled"
ATTR_FRONT_DEFROST: Final = "front_defrost"
ATTR_LEAD_TIME: Final = "lead_time"
ATTR_OVERRIDE_CHARGE_SCHEDULE: Final = "override_charge_schedule"
ATTR_SCHEDULE_ID: Final = "schedule_id"

SERVICE_CREATE_DEPARTURE_SCHEDULE: Final = "create_departure_schedule"
SERVICE_UPDATE_DEPARTURE_SCHEDULE: Final = "update_departure_schedule"
SERVICE_DELETE_DEPARTURE_SCHEDULE: Final = "delete_departure_schedule"
SERVICE_PRECONDITION_CABIN: Final = "precondition_cabin"


def _not_reserved_name(value: str) -> str:
    """Reject the name Home Assistant uses for its temporary precondition schedules.

    They're recognized by it and deleted once departed.
    """
    if is_precondition_schedule({"name": value}):
        raise vol.Invalid(
            f'"{PRECONDITION_SCHEDULE_NAME}" is reserved for the Precondition '
            "Cabin button"
        )
    return value


_NAME_SCHEMA = vol.All(
    cv.string, vol.Length(max=DEPARTURE_SCHEDULE_NAME_MAX_LENGTH), _not_reserved_name
)
_DAYS_SCHEMA = vol.All(cv.ensure_list, vol.Length(min=1), [vol.In(WEEK_DAYS_ORDERED)])
_SETTINGS_SCHEMA: Final[dict[vol.Marker, Any]] = {
    vol.Optional(ATTR_ENABLED): cv.boolean,
    # entered in the user's unit system; the valid range is enforced after the
    # conversion to Celsius in _get_schedule_changes
    vol.Optional(ATTR_TEMPERATURE): vol.Coerce(float),
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
        vol.Required(ATTR_DEPARTURE_TIME): cv.time,
        vol.Required(ATTR_DAYS): _DAYS_SCHEMA,
        **_SETTINGS_SCHEMA,
    }
)
UPDATE_DEPARTURE_SCHEDULE_SCHEMA: Final = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_SCHEDULE_ID): cv.string,
        vol.Optional(ATTR_NAME): _NAME_SCHEMA,
        vol.Optional(ATTR_DEPARTURE_TIME): cv.time,
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


PRECONDITION_CABIN_SCHEMA: Final = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Optional(ATTR_LEAD_TIME): vol.All(
            vol.Coerce(int),
            vol.Range(
                min=PRECONDITION_LEAD_MINUTES_MINIMUM,
                max=PRECONDITION_LEAD_MINUTES_MAXIMUM,
            ),
        ),
        # in the user's unit system; checked once converted to Celsius
        vol.Optional(ATTR_TEMPERATURE): vol.Coerce(float),
    }
)


def _get_coordinator(hass: HomeAssistant, device_id: str) -> VehicleCoordinator:
    """Get the vehicle coordinator for a device."""
    device = dr.async_get(hass).async_get(device_id)
    ids = (
        {id_ for domain, id_ in device.identifiers if domain == DOMAIN}
        if device
        else set()
    )
    loaded = hass.data.get(DOMAIN, {})
    for entry_data in loaded.values():
        coordinators = entry_data[ATTR_COORDINATOR][ATTR_VEHICLE]
        for vehicle_id, vehicle in entry_data[ATTR_VEHICLE].items():
            if ids & {vehicle_id, vehicle["vin"]}:
                return coordinators[vehicle_id]
    if ids and device.config_entries.isdisjoint(loaded):
        # a Rivian device whose config entry isn't set up; a loaded one that has no
        # vehicle coordinator is a wallbox
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="vehicle_not_loaded"
        )
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="invalid_device",
        translation_placeholders={"device_id": device_id},
    )


async def _async_get_controlled_vehicle(
    hass: HomeAssistant, call: ServiceCall
) -> VehicleCoordinator:
    """Return the target vehicle's coordinator, if the caller may control it.

    The vehicle is looked up first, so an unknown device is reported as such rather
    than as unauthorized (it has no entities the caller could control).

    These actions target a device, so Home Assistant's entity permission checks do
    not run for them. A user may act on a vehicle when they can control at least one
    of its entities. Calls without a user (automations, scripts) are trusted.
    """
    coordinator = _get_coordinator(hass, call.data[ATTR_DEVICE_ID])
    if not (user_id := call.context.user_id):
        return coordinator
    if (user := await hass.auth.async_get_user(user_id)) is None:
        raise UnknownUser(
            context=call.context, permission=POLICY_CONTROL, user_id=user_id
        )
    if user.is_admin:
        # even with no entities to check, such as all disabled
        return coordinator
    entities = er.async_entries_for_device(
        er.async_get(hass), call.data[ATTR_DEVICE_ID]
    )
    if not any(
        user.permissions.check_entity(entity.entity_id, POLICY_CONTROL)
        for entity in entities
    ):
        raise Unauthorized(
            context=call.context,
            permission=POLICY_CONTROL,
            user_id=user_id,
            perm_category=CAT_ENTITIES,
        )
    return coordinator


def _to_celsius(hass: HomeAssistant, temperature: float) -> float:
    """Convert a cabin temperature in the user's unit to °C for a schedule.

    Raises if it's outside what a schedule allows. The limits are compared in the
    user's unit, as shown, so the minimum itself (60.8 °F) isn't rejected for a
    rounding error in the conversion.
    """
    unit = hass.config.units.temperature_unit
    low, high = (
        round(TemperatureConverter.convert(limit, UnitOfTemperature.CELSIUS, unit), 6)
        for limit in (
            DEPARTURE_SCHEDULE_TEMPERATURE_MINIMUM,
            DEPARTURE_SCHEDULE_TEMPERATURE_MAXIMUM,
        )
    )
    if not low <= temperature <= high:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="temperature_out_of_range",
            translation_placeholders={
                "min": f"{low:g}",
                "max": f"{high:g}",
                "unit": unit,
            },
        )
    return schedule_cabin_temperature(
        TemperatureConverter.convert(temperature, unit, UnitOfTemperature.CELSIUS)
    )


def _get_schedule_changes(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Convert service data to departure schedule changes.

    The cabin temperature is entered in the user's configured unit system and
    converted to Celsius here, as the Rivian API only takes Celsius.
    """
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
    if ATTR_DEPARTURE_TIME in data:
        value: time = data[ATTR_DEPARTURE_TIME]
        weekly["startsAtMin"] = value.hour * MINUTES_PER_HOUR + value.minute
    if ATTR_TEMPERATURE in data:
        comfort["cabinTempCelsius"] = _to_celsius(hass, data[ATTR_TEMPERATURE])
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
        coordinator = await _async_get_controlled_vehicle(hass, call)
        await coordinator.create_departure_schedule(
            deep_merge(
                DEFAULT_DEPARTURE_SCHEDULE, _get_schedule_changes(hass, call.data)
            )
        )

    async def update_departure_schedule(call: ServiceCall) -> None:
        """Update a departure schedule."""
        coordinator = await _async_get_controlled_vehicle(hass, call)
        await coordinator.update_departure_schedule(
            call.data[ATTR_SCHEDULE_ID], _get_schedule_changes(hass, call.data)
        )

    async def delete_departure_schedule(call: ServiceCall) -> None:
        """Delete a departure schedule."""
        coordinator = await _async_get_controlled_vehicle(hass, call)
        await coordinator.delete_departure_schedule(call.data[ATTR_SCHEDULE_ID])

    async def precondition_cabin(call: ServiceCall) -> None:
        """Start cabin preconditioning through a temporary departure schedule."""
        coordinator = await _async_get_controlled_vehicle(hass, call)
        temperature = (
            _to_celsius(hass, call.data[ATTR_TEMPERATURE])
            if ATTR_TEMPERATURE in call.data
            else None
        )
        await coordinator.precondition_now(call.data.get(ATTR_LEAD_TIME), temperature)

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
    hass.services.async_register(
        DOMAIN,
        SERVICE_PRECONDITION_CABIN,
        precondition_cabin,
        schema=PRECONDITION_CABIN_SCHEMA,
    )
