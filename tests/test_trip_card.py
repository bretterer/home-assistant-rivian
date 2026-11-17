"""Tests for the trip route card's backend: attributes, recording and serving."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.rivian import frontend, sensor

START = datetime(2026, 10, 12, 18, 0, tzinfo=UTC)
ARRIVAL = datetime(2026, 10, 12, 20, 0, tzinfo=UTC)


def coordinator(values: dict[str, Any]) -> MagicMock:
    coor = MagicMock()
    coor.get.side_effect = values.get
    coor.data = {key: {"value": value} for key, value in values.items()}
    return coor


def test_destination_includes_route() -> None:
    """The Destination sensor carries the route and timing for the card."""
    coor = coordinator(
        {
            "waypoints": [
                {
                    "type": "place",
                    "name": "Trailhead",
                    "latitude": 40.0,
                    "longitude": -105.3,
                    "arrivalTime": ARRIVAL,
                }
            ],
            "overviewPolyline": "_p~iF~ps|U_ulLnnqC_mqNvxq`@",
            "originTime": START,
        }
    )

    attributes = sensor._destination_attributes(coor)

    assert attributes["route"] == "_p~iF~ps|U_ulLnnqC_mqNvxq`@"
    assert attributes["stops"] == [
        {
            "name": "Trailhead",
            "latitude": 40.0,
            "longitude": -105.3,
            "charging": False,
            "arrival": ARRIVAL,
            "next": True,
        }
    ]
    assert attributes["trip_start"] == START
    assert attributes["arrival"] == ARRIVAL
    assert attributes["latitude"] == 40.0


def test_destination_without_trip() -> None:
    """Not navigating: no attributes at all."""
    assert sensor._destination_attributes(coordinator({})) is None


def test_route_is_not_recorded() -> None:
    """The route can be larger than the recorder allows; stop times churn."""
    unrecorded = sensor.RivianNavigationSensorEntity._unrecorded_attributes
    assert unrecorded >= {"route", "stops"}


async def test_card_is_served_and_loaded() -> None:
    """The card is served from the integration and added to the frontend."""
    hass = MagicMock()
    hass.config.components = {"frontend", "http"}
    hass.async_add_executor_job = AsyncMock(side_effect=lambda func, *args: func(*args))
    hass.http.async_register_static_paths = AsyncMock()
    with (
        patch.object(frontend, "StaticPathConfig", lambda *args: args),
        patch.object(frontend, "add_extra_js_url") as add_js,
    ):
        await frontend.async_register_cards(hass)

    (paths,) = hass.http.async_register_static_paths.call_args.args
    assert paths == [(frontend.TRIP_CARD_URL, str(frontend.TRIP_CARD_PATH), True)]
    assert frontend.TRIP_CARD_PATH.is_file()
    url = add_js.call_args.args[1]
    assert (
        url.startswith(f"{frontend.TRIP_CARD_URL}?v=") and len(url.split("=")[1]) == 8
    )


async def test_card_skipped_without_frontend() -> None:
    """Headless installs don't load the frontend, so there's nothing to add."""
    hass = MagicMock()
    hass.config.components = {"http"}
    hass.http.async_register_static_paths = AsyncMock()
    with patch.object(frontend, "add_extra_js_url") as add_js:
        await frontend.async_register_cards(hass)
    hass.http.async_register_static_paths.assert_not_called()
    add_js.assert_not_called()
