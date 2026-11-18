"""Rivian helpers."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from copy import deepcopy
import logging
from typing import Any

from rivian import Rivian

from homeassistant.components.diagnostics.util import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_EMAIL,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    Platform,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.unit_conversion import TemperatureConverter

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_MFA_VERIFIED,
    CONF_REFRESH_TOKEN,
    CONF_USER_SESSION_TOKEN,
    DEPARTURE_SCHEDULE_SURFACES,
    DEPARTURE_SCHEDULE_TEMPERATURE_MAXIMUM,
    DEPARTURE_SCHEDULE_TEMPERATURE_MINIMUM,
    DOMAIN,
    MINUTES_PER_HOUR,
)

_LOGGER = logging.getLogger(__name__)

TO_REDACT = {
    CONF_EMAIL,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    "batteryEmptyLatitude",
    "batteryEmptyLongitude",
    "favoriteGeofences",
    "hrid",
    "id",
    "identityId",
    "inviteId",
    "legs",
    "mappedIdentityId",
    "orderId",
    "originLatitude",
    "originLongitude",
    "overviewPolyline",
    "serialNumber",
    "snappedLatitude",
    "snappedLongitude",
    "userId",
    "vas",
    "vehicleId",
    "vin",
    "wallboxId",
    "waypoints",
    "wifiIpAddresses",
    "wifiMacAddress",
}


def get_rivian_api_from_entry(hass: HomeAssistant, entry: ConfigEntry) -> Rivian:
    """Get Rivian API from a config entry."""
    return Rivian(
        request_timeout=30,
        session=async_get_clientsession(hass),
        access_token=entry.data.get(CONF_ACCESS_TOKEN),
        refresh_token=entry.data.get(CONF_REFRESH_TOKEN),
        user_session_token=entry.data.get(CONF_USER_SESSION_TOKEN),
    )


def redact(data: Any) -> dict:
    """Redact sensitive data."""
    return async_redact_data(data, TO_REDACT)


@callback
def async_remove_stale_entities(
    hass: HomeAssistant,
    platform: str,
    possible_unique_ids: Iterable[str],
    created_unique_ids: Iterable[str | None],
) -> None:
    """Remove registry entries for entities that are no longer created.

    For entities gated by vehicle model or supported features, which earlier
    versions created for vehicles that don't report them, and removed entities.
    """
    registry = er.async_get(hass)
    for unique_id in set(possible_unique_ids) - set(created_unique_ids):
        if entity_id := registry.async_get_entity_id(platform, DOMAIN, unique_id):
            _LOGGER.info("Removing %s, which is no longer provided", entity_id)
            registry.async_remove(entity_id)


def has_verified_2fa(entry: ConfigEntry, user_data: dict[str, Any]) -> bool:
    """Return True if the account is known to use 2FA.

    registrationChannels only lists SMS numbers, so authenticator-app and email
    OTP users show up empty there. Rivian asking for an OTP at login proves 2FA
    of any type, so the config flow records that as CONF_MFA_VERIFIED.
    """
    return bool(
        entry.data.get(CONF_MFA_VERIFIED) or user_data.get("registrationChannels")
    )


def deep_merge(base: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of base with changes merged in, recursing into dicts."""
    merged = deepcopy(base)
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def schedule_cabin_temperature(celsius: float) -> float:
    """Return a cabin temperature in °C as a departure schedule takes it.

    Rounded to the Rivian app's 0.5 °C steps, within the schedule limits, so LO
    and HI become the nearest limit. Whole values are ints.
    """
    celsius = max(
        DEPARTURE_SCHEDULE_TEMPERATURE_MINIMUM,
        min(DEPARTURE_SCHEDULE_TEMPERATURE_MAXIMUM, round(celsius * 2) / 2),
    )
    return int(celsius) if float(celsius).is_integer() else celsius


def departure_schedules_from_message(
    data: dict[str, Any],
) -> list[dict[str, Any]] | None:
    """Return the schedules a departure schedule subscription message holds."""
    schedules = ((data.get("payload") or {}).get("data") or {}).get(
        "vehicleDepartureSchedules"
    )
    return schedules if isinstance(schedules, list) else None


def departure_mutation_succeeded(data: dict[str, Any], key: str) -> bool | None:
    """Return a departure schedule mutation's reported success.

    None if it reports neither, such as partial data with an error, after which
    the write may still have taken effect.
    """
    success = ((data.get("data") or {}).get(key) or {}).get("success")
    return success if isinstance(success, bool) else None


def departure_schedule_to_input(schedule: dict[str, Any]) -> dict[str, Any]:
    """Convert a departure schedule as received into the format used to set one."""
    data = deepcopy({k: v for k, v in schedule.items() if k != "id"})
    occurrence = data.pop("occurrence", None) or {}
    data["repeatsWeekly"] = {k: v for k, v in occurrence.items() if k != "__typename"}
    return data


def departure_schedule_summary(
    schedule: dict[str, Any], temperature_unit: str
) -> dict[str, Any]:
    """Flatten a departure schedule for use in state attributes.

    The temperature is in ``temperature_unit``, the user's unit, as the actions
    take it, so a value can be copied from here into an update.
    """
    occurrence = schedule.get("occurrence") or {}
    settings = schedule.get("departureSettings") or {}
    comfort = settings.get("comfortSettings") or {}
    levels = comfort.get("surfaceHeatVentLevels") or {}
    time = None
    if (minutes := occurrence.get("startsAtMin")) is not None:
        time = f"{minutes // MINUTES_PER_HOUR:02d}:{minutes % MINUTES_PER_HOUR:02d}"
    if (temperature := comfort.get("cabinTempCelsius")) is not None:
        temperature = round(
            TemperatureConverter.convert(
                temperature, UnitOfTemperature.CELSIUS, temperature_unit
            ),
            1,
        )
    return {
        "schedule_id": schedule.get("id"),
        "name": schedule.get("name"),
        "enabled": schedule.get("isEnabled"),
        "days": occurrence.get("days"),
        "departure_time": time,
        "skipped_on": occurrence.get("skippedOn"),
        "temperature": temperature,
        "front_defrost": comfort.get("frontDefogDefrost"),
        **{
            field: levels.get(key) for field, key in DEPARTURE_SCHEDULE_SURFACES.items()
        },
        "override_charge_schedule": settings.get("shouldOverrideChargeSchedule"),
    }


@callback
def track_departure_schedule_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: Any,
    vin: str,
    platform: Platform,
    key: str,
    entity_factory: Callable[[str], Entity],
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Keep one entity per departure schedule, added and removed as they change.

    ``key`` is the unique-id segment the entities use (``{vin}-{key}_{schedule_id}``).
    Removing the registry entry removes the entity, which also clears schedules that
    were deleted while Home Assistant was not running. The coordinator only passes
    on confirmed lists, holding back the brief empty one Rivian can send, so a
    schedule missing from the list is gone.
    """
    unique_id_prefix = f"{vin}-{key}_"
    known_ids: set[str] = set()
    last_schedules: list[dict[str, Any]] | None = None

    @callback
    def _update() -> None:
        nonlocal last_schedules
        # The coordinator updates its listeners for every vehicle message, but only
        # replaces the list when it changes, so skip scanning the registry otherwise
        if (schedules := coordinator.departure_schedules) is None or (
            schedules is last_schedules
        ):
            return
        last_schedules = schedules
        schedule_ids = {schedule["id"] for schedule in schedules}
        if new_ids := schedule_ids - known_ids:
            async_add_entities(entity_factory(schedule_id) for schedule_id in new_ids)
            known_ids.update(new_ids)
        registry = er.async_get(hass)
        for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
            if (
                reg_entry.domain == platform
                and reg_entry.unique_id.startswith(unique_id_prefix)
                and (schedule_id := reg_entry.unique_id.removeprefix(unique_id_prefix))
                not in schedule_ids
            ):
                registry.async_remove(reg_entry.entity_id)
                # added again if its schedule comes back
                known_ids.discard(schedule_id)

    entry.async_on_unload(coordinator.async_add_listener(_update))
    _update()
