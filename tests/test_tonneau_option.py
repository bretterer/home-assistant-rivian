"""Tests for gating the tonneau entities on the vehicle's tonneau option."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.rivian import const
from custom_components.rivian.coordinator import UserCoordinator

TONNEAU_BINARY_SENSORS = {"closure_tonneau_closed", "closure_tonneau_locked"}


def get_vehicles(mobile_configuration: Any) -> dict[str, dict[str, Any]]:
    """Vehicles as setup sees them, for one R1T with a mobile configuration."""
    vehicle = {
        "id": "id",
        "name": "Truck",
        "vas": {"vasVehicleId": "vas", "vehiclePublicKey": "key"},
        "vehicle": {
            "id": "id",
            "vin": "VIN",
            "model": "R1T",
            "mobileConfiguration": mobile_configuration,
            "vehicleState": {
                "supportedFeatures": [
                    {"name": "CHARG_CMD", "status": "AVAILABLE"},
                    {"name": "LIFTGATE_CMD", "status": "UNAVAILABLE"},
                ]
            },
        },
    }
    user = SimpleNamespace(data={"vehicles": [vehicle]})
    return UserCoordinator.get_vehicles(user)  # type: ignore[arg-type]


def option(option_id: str) -> dict[str, Any]:
    return {"tonneauOption": {"optionId": option_id, "optionName": "Name"}}


def test_powered_tonneau_is_supported() -> None:
    features = get_vehicles(option("TON-P01"))["id"]["supported_features"]
    assert features == ["CHARG_CMD", "TONNEAU_CMD"]


@pytest.mark.parametrize(
    "mobile_configuration",
    [None, {}, {"tonneauOption": None}, option("TON-M01"), option("")],
)
def test_no_powered_tonneau(mobile_configuration: Any) -> None:
    features = get_vehicles(mobile_configuration)["id"]["supported_features"]
    assert features == ["CHARG_CMD"]


@pytest.mark.parametrize(("option_id", "created"), [("TON-P01", True), ("", False)])
def test_tonneau_binary_sensors_follow_the_option(
    option_id: str, created: bool
) -> None:
    vehicle = get_vehicles(option(option_id))["id"]
    keys = {
        description.key
        for models, descriptions in const.BINARY_SENSORS.items()
        if any(model in vehicle["model"] for model in models)
        for description in descriptions
        if description.supported_feature is None
        or description.supported_feature in vehicle["supported_features"]
    }
    assert (TONNEAU_BINARY_SENSORS <= keys) is created
