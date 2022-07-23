"""Rivian (Unofficial)"""
from __future__ import annotations
from typing import Final

from homeassistant.const import (
    LENGTH_METERS,
    LENGTH_KILOMETERS,
    LENGTH_MILES,
    PRESSURE_BAR,
    PRESSURE_PSI,
    PERCENTAGE,
    TEMP_CELSIUS,
    TEMP_FAHRENHEIT,
    ELECTRIC_CURRENT_AMPERE,
    ELECTRIC_POTENTIAL_VOLT,
    ENERGY_KILO_WATT_HOUR,
    SPEED_KILOMETERS_PER_HOUR,
    SPEED_MILES_PER_HOUR,
    MASS_POUNDS,
    POWER_KILO_WATT,
)

from homeassistant.util.distance import convert as convert_distance
from homeassistant.util.pressure import convert as convert_pressure
from homeassistant.util.speed import convert as convert_speed
from homeassistant.util.temperature import convert as convert_temperature

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
    "core/power_modes/power_state": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Power State",
            key=f"{DOMAIN}_core_power_modes_power_state",
        ),
    ),
    "dynamics/odometer/value": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Odometer",
            icon="mdi:counter",
            key=f"{DOMAIN}_dynamics_odometer_value",
            native_unit_of_measurement=LENGTH_MILES,
        ),
        value_lambda=lambda v: round(convert_distance(v, LENGTH_METERS, LENGTH_MILES), 1),
    ),
    "dynamics/propulsion_status/PRNDL": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Gear Selector",
            key=f"{DOMAIN}_dynamics_propulsion_status_prndl",
        ),
    ),
    "energy_storage/charger/adjusted_soc": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Battery State of Charge",
            icon="mdi:battery-charging",
            key=f"{DOMAIN}_energy_storage_charger_adjusted_soc",
            native_unit_of_measurement=PERCENTAGE,
        ),
    ),
    "energy_storage/charger/vehicle_charger_state": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Charging Status",
            icon="mdi:ev-station",
            key=f"{DOMAIN}_energy_storage_charger_vehicle_charger_state",
        )
    ),
    "energy_storage/vehicle_energy/vehicle_range": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Estimated Vehicle Range",
            icon="mdi:map-marker-distance",
            key=f"{DOMAIN}_energy_storage_vehicle_energy_vehicle_range",
            native_unit_of_measurement=LENGTH_MILES,
        ),
        value_lambda=lambda v: round(convert_distance(v, LENGTH_KILOMETERS, LENGTH_MILES)),
    ),
    "thermal/hvac_cabin_control/cabin_temperature": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Cabin Temperature",
            icon="mdi:thermometer",
            key=f"{DOMAIN}_thermal_hvac_cabin_control_cabin_temperature",
            native_unit_of_measurement=TEMP_FAHRENHEIT,
        ),
        value_lambda=lambda v: round(convert_temperature(v, TEMP_CELSIUS, TEMP_FAHRENHEIT)),
    ),
}
