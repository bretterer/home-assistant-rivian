"""Serve the integration's dashboard cards."""

from __future__ import annotations

import hashlib
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

TRIP_CARD_FILENAME = "rivian-trip-card.js"
TRIP_CARD_URL = f"/rivian/{TRIP_CARD_FILENAME}"
TRIP_CARD_PATH = Path(__file__).parent / "frontend" / TRIP_CARD_FILENAME


def _file_hash(path: Path) -> str:
    """Return a short hash of a file, so browsers refetch it when it changes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:8]


async def async_register_cards(hass: HomeAssistant) -> None:
    """Serve the cards and load them in the frontend."""
    if "frontend" not in hass.config.components:
        return
    version = await hass.async_add_executor_job(_file_hash, TRIP_CARD_PATH)
    await hass.http.async_register_static_paths(
        [StaticPathConfig(TRIP_CARD_URL, str(TRIP_CARD_PATH), True)]
    )
    add_extra_js_url(hass, f"{TRIP_CARD_URL}?v={version}")
