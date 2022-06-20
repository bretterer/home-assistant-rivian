"""Rivian (Unofficial)"""
from __future__ import annotations

from typing import Final

from homeassistant.const import (
    LENGTH_MILES,
    PRESSURE_PSI,
    PERCENTAGE,
    TEMP_CELCIUS,
)

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
    "body/closures/global_closure_locked_state": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Global Closure Locked State",
            icon="mdi:lock",
            key=f"{DOMAIN}_body_closure_global_closure_locked_state",
            native_unit_of_measurement=None,
        )
    ),
    "body/closures/global_closure_state": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Global Closure State",
            icon="mdi:door",
            key=f"{DOMAIN}_body_closure_global_closure_state",
            native_unit_of_measurement=None,
        )
    ),
    "core/power_modes/power_state": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Power State",
            key=f"{DOMAIN}_core_power_modes_power_state",
        ),
    ),
    "dynamics/odometer/value": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Odometer",
            icon="mdi:speedometer",
            key=f"{DOMAIN}_dynamics_odometer_value",
            native_unit_of_measurement=LENGTH_MILES,
        ),
        value_lambda=lambda v: round(v / 1609.344, 2),
    ),
    "dynamics/propulsion_status/PRNDL": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Gear Selector",
            key=f"{DOMAIN}_dynamics_propulsion_status_prndl",
        ),
    ),
    "dynamics/tires/tire_FL_pressure": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Front Left Tire Pressure",
            icon="mdi:tire",
            key=f"{DOMAIN}_dynamics_tires_tire_fl_pressure",
            native_unit_of_measurement=PRESSURE_PSI,
        ),
        value_lambda=lambda v: round(v * 14.503773773, None) if v < 4 else "--",
    ),
    "dynamics/tires/tire_FR_pressure": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Front Right Tire Pressure",
            icon="mdi:tire",
            key=f"{DOMAIN}_dynamics_tires_tire_fr_pressure",
            native_unit_of_measurement=PRESSURE_PSI,
        ),
        value_lambda=lambda v: round(v * 14.503773773, None) if v < 4 else "--",
    ),
    "dynamics/tires/tire_RL_pressure": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Rear Left Tire Pressure",
            icon="mdi:tire",
            key=f"{DOMAIN}_dynamics_tires_tire_rl_pressure",
            native_unit_of_measurement=PRESSURE_PSI,
        ),
        value_lambda=lambda v: round(v * 14.503773773, None) if v < 4 else "--",
    ),
    "dynamics/tires/tire_RR_pressure": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Rear Right Tire Pressure",
            icon="mdi:tire",
            key=f"{DOMAIN}_dynamics_tires_tire_rr_pressure",
            native_unit_of_measurement=PRESSURE_PSI,
        ),
        value_lambda=lambda v: round(v * 14.503773773, None) if v < 4 else "--",
    ),
    "energy_storage/charger/adjusted_soc": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Current Charge",
            icon="mdi:battery-charging",
            key=f"{DOMAIN}_energy_storage_charger_adjusted_soc",
            native_unit_of_measurement=PERCENTAGE,
        ),
    ),
    "energy_storage/charger/stored_user_range_select": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Max Charge Setting",
            icon="mdi:battery-charging-100",
            key=f"{DOMAIN}_energy_storage_charger_stored_user_range_select",
        )
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
        value_lambda=lambda v: round(v * 0.6213711922, None),
    ),
    "thermal/hvac_cabin_control/cabin_temperature": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Cabin Temperature",
            icon="mdi:thermometer",
            key=f"{DOMAIN}_thermal_hvac_cabin_control_cabin_temperature",
            native_unit_of_measurement=TEMP_CELSIUS,
        ),
    ),
}
