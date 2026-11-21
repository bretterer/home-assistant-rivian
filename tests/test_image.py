"""Tests for picking which Rivian vehicle-image version to use."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from custom_components.rivian.const import (
    CONF_DRIVE_ANALYTICS,
    CONF_VEHICLE_IMAGE_STYLE,
    IMAGE_STYLE_NONE,
)
from custom_components.rivian.image import (
    _vehicle_images,
    _version_candidates,
    async_setup_entry,
)

VEHICLES = {"veh-1": {"vin": "7PDSGABA8NN000000"}}


def test_configured_version_is_tried_first_then_the_others() -> None:
    assert _version_candidates("2") == ["2", "1"]
    assert _version_candidates("3") == ["3", "2", "1"]  # cel style asks for "3"


def test_only_large_images_of_known_vehicles_are_used() -> None:
    data = [
        {"vehicleId": "veh-1", "size": "large", "placement": "side"},
        {"vehicleId": "veh-1", "size": "small", "placement": "side"},
        {"vehicleId": "someone-else", "size": "large", "placement": "side"},
        "not a dict",
    ]
    assert _vehicle_images(data, VEHICLES) == [data[0]]


def test_an_empty_or_null_response_means_no_images() -> None:
    """Rivian answered `getVehicleMobileImages: []` for version 2 on a 2025 R1S."""
    assert _vehicle_images([], VEHICLES) == []
    assert _vehicle_images(None, VEHICLES) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("analytics", ["off", "running", "failed"])
@pytest.mark.parametrize("style", [IMAGE_STYLE_NONE, "cel"])
async def test_stale_picture_removed_only_when_analytics_off(
    analytics: str, style: str
) -> None:
    """The picture entity lives in the analytics DB: it goes with analytics off.

    With analytics on but failed to start (no stores, e.g. a locked database)
    it stays, keeping its entity ID and settings.
    """
    hass = MagicMock()
    stores: dict[str, Any] = {}
    if analytics == "running":
        store = MagicMock()
        store.async_get_vehicle_picture = MagicMock(return_value=None)
        stores["veh-1"] = store
    entry = MagicMock(
        entry_id="entry",
        options={
            CONF_VEHICLE_IMAGE_STYLE: style,
            CONF_DRIVE_ANALYTICS: analytics != "off",
        },
    )
    entry.runtime_data = SimpleNamespace(
        vehicles=VEHICLES,
        api=MagicMock(),
        user_coordinator=MagicMock(),
        drive_stores=stores,
        picture_platform={},
    )
    removed: list[str] = []
    registry = MagicMock()
    registry.async_get_entity_id.side_effect = lambda platform, domain, uid: (
        "image.picture" if uid == "7PDSGABA8NN000000-picture" else None
    )
    registry.async_remove.side_effect = removed.append

    async def _no_pictures(*_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    with (
        patch("custom_components.rivian.helpers.er") as er_mock,
        patch("custom_components.rivian.image._async_picture_entities", _no_pictures),
        patch(
            "custom_components.rivian.image._async_legacy_image_entities", _no_pictures
        ),
    ):
        er_mock.async_get.return_value = registry
        await async_setup_entry(hass, entry, MagicMock())

    if analytics == "off":
        assert removed == ["image.picture"]
    elif analytics == "running":
        # No picture was found for the vehicle, so it has none
        assert removed == ([] if style == IMAGE_STYLE_NONE else ["image.picture"])
    else:
        assert removed == []
