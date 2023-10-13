"""Rivian helpers."""
from __future__ import annotations

from rivian import Rivian

from homeassistant.components.diagnostics.util import _T, async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_LATITUDE, CONF_LONGITUDE

from .const import CONF_ACCESS_TOKEN, CONF_REFRESH_TOKEN, CONF_USER_SESSION_TOKEN

TO_REDACT = {
    CONF_EMAIL,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    "hrid",
    "id",
    "identityId",
    "inviteId",
    "mappedIdentityId",
    "orderId",
    "serialNumber",
    "userId",
    "vas",
    "vehicleId",
    "vin",
    "wallboxId",
}


def redact(data: _T) -> dict:
    """Redact sensitive data."""
    return async_redact_data(data, TO_REDACT)