"""Rivian helpers."""

from __future__ import annotations

from collections.abc import Iterable
import logging
from typing import Any

from rivian import Rivian

from homeassistant.components.diagnostics.util import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_LATITUDE, CONF_LONGITUDE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_MFA_VERIFIED,
    CONF_REFRESH_TOKEN,
    CONF_USER_SESSION_TOKEN,
    DOMAIN,
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
