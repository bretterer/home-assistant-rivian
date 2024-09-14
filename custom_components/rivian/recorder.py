"""Integration platform for recorder."""

from __future__ import annotations

from homeassistant.core import HomeAssistant, callback


@callback
def exclude_attributes(hass: HomeAssistant) -> set[str]:
    """Exclude last_update from being recorded in the database."""
    return {"last_update"}
