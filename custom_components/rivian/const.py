"""Rivian (Unofficial)"""
from __future__ import annotations

from typing import Final

from homeassistant.const import LENGTH_MILES, LENGTH_KILOMETERS

from .data_classes import RivianSensorEntity, RivianSensorEntityDescription

NAME = "Rivian (Unofficial)"
DOMAIN = "rivian"
VERSION = "0.0.1-alpha.2"
ISSUE_URL = "https://github.com/bretterer/home-assistant-rivian/issues"
COORDINATOR = "rivian_coordinator"
UPDATE_INTERVAL = 5


# Attributes
ATTR_COORDINATOR = "rivian_coordinator"

# Config properties
CONF_OTP = "otp"
CONF_VIN = "vehicle_id"
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"


SENSORS: Final[dict[str, RivianSensorEntity]] = {
    "dynamics/odometer/value": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Odometer",
            icon="mdi:speedometer",
            key=f"{DOMAIN}_dynamics_odometer_value",
            native_unit_of_measurement=LENGTH_MILES,
        ),
        value_lambda=lambda v: round(v / 1609.344, 2),
    ),
    "body/closures/global_closure_state": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Global Closure State",
            icon="mdi:door",
            key=f"{DOMAIN}_body_closure_global_closure_state",
            native_unit_of_measurement=None,
        )
    ),
    "body/closures/global_closure_locked_state": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Global Closure Locked State",
            icon="mdi:lock",
            key=f"{DOMAIN}_body_closure_global_closure_locked_state",
            native_unit_of_measurement=None,
        )
    ),
    "energy_storage/vehicle_energy/vehicle_range": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Estimated Vehicle Range",
            icon="mdi:map-marker-distance",
            key=f"{DOMAIN}_energy_storage_vehicle_energy_vehicle_range",
            native_unit_of_measurement=LENGTH_MILES,
        ),
        value_lambda=lambda v: round(v * 0.6213711922, None),
    ),
    "energy_storage/charger/vehicle_charger_state": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Charging Status",
            icon="mdi:ev-station",
            key=f"{DOMAIN}_energy_storage_charger_vehicle_charger_state",
        )
    ),
}
