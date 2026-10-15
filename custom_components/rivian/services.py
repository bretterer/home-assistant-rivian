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
    ATTR_TIME,
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


async def _async_verify_vehicle_control(hass: HomeAssistant, call: ServiceCall) -> None:
    """Require the caller to be allowed to control the target vehicle.

    These actions target a device, so Home Assistant's entity permission checks do
    not run for them. A user may act on a vehicle when they can control at least one
    of its entities. Calls without a user (automations, scripts) are trusted.
    """
    if not (user_id := call.context.user_id):
        return
    if (user := await hass.auth.async_get_user(user_id)) is None:
        raise UnknownUser(
            context=call.context, permission=POLICY_CONTROL, user_id=user_id
        )
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


def _get_schedule_changes(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Convert service data to departure schedule changes.

    The cabin temperature is entered in the user's configured unit system and
    converted to Celsius here (the Rivian API only takes Celsius), mirroring how
    the Precondition Temperature number entity localizes.
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
    if ATTR_TIME in data:
        value: time = data[ATTR_TIME]
        weekly["startsAtMin"] = value.hour * MINUTES_PER_HOUR + value.minute
    if ATTR_TEMPERATURE in data:
        unit = hass.config.units.temperature_unit
        celsius = TemperatureConverter.convert(
            data[ATTR_TEMPERATURE], unit, UnitOfTemperature.CELSIUS
        )
        if not (
            DEPARTURE_SCHEDULE_TEMPERATURE_MINIMUM
            <= celsius
            <= DEPARTURE_SCHEDULE_TEMPERATURE_MAXIMUM
        ):
            low = TemperatureConverter.convert(
                DEPARTURE_SCHEDULE_TEMPERATURE_MINIMUM, UnitOfTemperature.CELSIUS, unit
            )
            high = TemperatureConverter.convert(
                DEPARTURE_SCHEDULE_TEMPERATURE_MAXIMUM, UnitOfTemperature.CELSIUS, unit
            )
            raise ServiceValidationError(
                f"Cabin temperature must be between {low:g} and {high:g} {unit}"
            )
        # the Rivian app uses 0.5 °C increments; keep whole values as ints
        celsius = round(celsius * 2) / 2
        comfort["cabinTempCelsius"] = int(celsius) if celsius.is_integer() else celsius
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
        await _async_verify_vehicle_control(hass, call)
        coordinator = _get_coordinator(hass, call.data[ATTR_DEVICE_ID])
        await coordinator.create_departure_schedule(
            deep_merge(
                DEFAULT_DEPARTURE_SCHEDULE, _get_schedule_changes(hass, call.data)
            )
        )

    async def update_departure_schedule(call: ServiceCall) -> None:
        """Update a departure schedule."""
        await _async_verify_vehicle_control(hass, call)
        coordinator = _get_coordinator(hass, call.data[ATTR_DEVICE_ID])
        await coordinator.update_departure_schedule(
            call.data[ATTR_SCHEDULE_ID], _get_schedule_changes(hass, call.data)
        )

    async def delete_departure_schedule(call: ServiceCall) -> None:
        """Delete a departure schedule."""
        await _async_verify_vehicle_control(hass, call)
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
