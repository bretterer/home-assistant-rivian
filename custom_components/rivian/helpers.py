"""Rivian helpers."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from copy import deepcopy
import logging
from typing import Any

from rivian import Rivian

from homeassistant.components.diagnostics.util import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_LATITUDE, CONF_LONGITUDE, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_MFA_VERIFIED,
    CONF_REFRESH_TOKEN,
    CONF_USER_SESSION_TOKEN,
    DEPARTURE_SCHEDULE_SURFACES,
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


def departure_schedule_to_input(schedule: dict[str, Any]) -> dict[str, Any]:
    """Convert a departure schedule as received into the format used to set one."""
    data = deepcopy({k: v for k, v in schedule.items() if k != "id"})
    occurrence = data.pop("occurrence", None) or {}
    data["repeatsWeekly"] = {k: v for k, v in occurrence.items() if k != "__typename"}
    return data


def departure_schedule_summary(schedule: dict[str, Any]) -> dict[str, Any]:
    """Flatten a departure schedule for use in state attributes."""
    occurrence = schedule.get("occurrence") or {}
    settings = schedule.get("departureSettings") or {}
    comfort = settings.get("comfortSettings") or {}
    levels = comfort.get("surfaceHeatVentLevels") or {}
    time = None
    if (minutes := occurrence.get("startsAtMin")) is not None:
        time = f"{minutes // MINUTES_PER_HOUR:02d}:{minutes % MINUTES_PER_HOUR:02d}"
    if (temperature := comfort.get("cabinTempCelsius")) is not None:
        temperature = round(temperature, 1)
    return {
        "schedule_id": schedule.get("id"),
        "name": schedule.get("name"),
        "enabled": schedule.get("isEnabled"),
        "days": occurrence.get("days"),
        "time": time,
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
    were deleted while Home Assistant was not running.
    """
    unique_id_prefix = f"{vin}-{key}_"
    known_ids: set[str] | None = None

    @callback
    def _update() -> None:
        nonlocal known_ids
        if (schedules := coordinator.departure_schedules) is None:
            return
        schedule_ids = {schedule["id"] for schedule in schedules}
        if schedule_ids == known_ids:
            return

        registry = er.async_get(hass)
        for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
            if (
                reg_entry.domain == platform
                and reg_entry.unique_id.startswith(unique_id_prefix)
                and reg_entry.unique_id.removeprefix(unique_id_prefix)
                not in schedule_ids
            ):
                registry.async_remove(reg_entry.entity_id)

        async_add_entities(
            entity_factory(schedule_id)
            for schedule_id in schedule_ids - (known_ids or set())
        )
        known_ids = schedule_ids

    entry.async_on_unload(coordinator.async_add_listener(_update))
    _update()
