"""Tests for picking which Rivian vehicle-image version to use."""

from __future__ import annotations

from custom_components.rivian.image import _vehicle_images, _version_candidates

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
