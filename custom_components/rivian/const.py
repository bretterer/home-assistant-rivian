"""Rivian (Unofficial)"""
from __future__ import annotations

from typing import Final

# from homeassistant.components.sensor import SensorDeviceClass, SensorEntityDescription
# from homeassistant.helpers.entity import EntityCategory
from homeassistant.const import (
    LENGTH_MILES,
)

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


# Sensors
# SENSORS: Final[dict[str, SensorEntityDescription]] = {
#     "dynamics/odometer/value": SensorEntityDescription(
#         name="Odometer",
#         icon="mdi:speedometer",
#         key="dynamics/odometer/value",
#         entity_category=EntityCategory.DIAGNOSTIC,
#         device_class=SensorDeviceClass.TIMESTAMP,
#         unit_of_measurement=LENGTH_MILES,
#     ),
# }

# SENSORS: Final[dict[str, RivianEntity]] = {
#     "dynamics/odometer/value": RivianEntity(
#         entity_id=f"{DOMAIN}_dynamics_odometer_value",
#         entity_description=RivianSensorEntityDescription(
#             name="Odometer",
#             icon="mdi:speedometer",
#             key=f"{DOMAIN}_dynamics_odometer_value",
#             native_unit_of_measurement=LENGTH_MILES,
#         ),

#     )
# }
