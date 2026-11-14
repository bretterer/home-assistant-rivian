"""Backup platform: back up a consistent drive analytics database."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from .analytics_db import AnalyticsDatabase
from .const import ATTR_ANALYTICS_DB, DOMAIN


def _analytics_db(hass: HomeAssistant) -> AnalyticsDatabase | None:
    return hass.data.get(DOMAIN, {}).get(ATTR_ANALYTICS_DB)


async def async_pre_backup(hass: HomeAssistant) -> None:
    """Checkpoint the analytics database and hold its writes during the backup."""
    if (db := _analytics_db(hass)) is not None:
        await hass.async_add_executor_job(db.hold_for_backup)


async def async_post_backup(hass: HomeAssistant) -> None:
    """Let the analytics database write again."""
    if (db := _analytics_db(hass)) is not None:
        await hass.async_add_executor_job(db.release_after_backup)
