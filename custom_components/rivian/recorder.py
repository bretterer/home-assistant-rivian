"""Integration platform for recorder."""

from __future__ import annotations

from homeassistant.core import HomeAssistant, callback


@callback
def exclude_attributes(hass: HomeAssistant) -> set[str]:
    """Exclude bulk analytics payloads from recorder to avoid exceeding 16 KiB attribute limit.

    This platform is domain-scoped, so these keys are excluded for every sensor.* entity.
    """
    return {
        "last_update",
        "recent_drives",
        "recent_segments",
        "recent_vampire_events",
        "recent_dcfc_sessions",
        "speed_bins",
        "stats_90d",
        "stats_365d",
    }
