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
        value_lambda=lambda v: round(convert_distance(v, LENGTH_METERS, LENGTH_MILES), 1),
    ),
    "dynamics/propulsion_status/PRNDL": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Gear Selector",
            key=f"{DOMAIN}_dynamics_propulsion_status_prndl",
        ),
    ),
    "dynamics/propulsion_status/vehicle_speed_VDM": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Vehicle Speed",
            icon="mdi:battery-heart",
            key=f"{DOMAIN}_dynamics_propulsion_status_vehicle_speed_VDM",
            native_unit_of_measurement=SPEED_MILES_PER_HOUR,
        ),
        value_lambda=lambda v: round(convert_speed(v, SPEED_KILOMETERS_PER_HOUR, SPEED_MILES_PER_HOUR)),
    ),
    "dynamics/speed_performance/mass_estimate": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Vehicle Weight",
            icon="mdi:weight",
            key=f"{DOMAIN}_dynamics_speed_performance_mass_estimate",
            native_unit_of_measurement=MASS_POUNDS,
        ),
        value_lambda=lambda v: round(v * 2.20462262185),
    ),
    "dynamics/tires/tire_FL_pressure": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Tire Pressure Front Left",
            icon="mdi:tire",
            key=f"{DOMAIN}_dynamics_tires_tire_fl_pressure",
            native_unit_of_measurement=PRESSURE_PSI,
        ),
        value_lambda=lambda v: round(convert_pressure(v, PRESSURE_BAR, PRESSURE_PSI)) if v < 4 else "--",
    ),
    "dynamics/tires/tire_FR_pressure": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Tire Pressure Front Right",
            icon="mdi:tire",
            key=f"{DOMAIN}_dynamics_tires_tire_fr_pressure",
            native_unit_of_measurement=PRESSURE_PSI,
        ),
        value_lambda=lambda v: round(convert_pressure(v, PRESSURE_BAR, PRESSURE_PSI)) if v < 4 else "--",
    ),
    "dynamics/tires/tire_RL_pressure": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Tire Pressure Rear Left",
            icon="mdi:tire",
            key=f"{DOMAIN}_dynamics_tires_tire_rl_pressure",
            native_unit_of_measurement=PRESSURE_PSI,
        ),
        value_lambda=lambda v: round(convert_pressure(v, PRESSURE_BAR, PRESSURE_PSI)) if v < 4 else "--",
    ),
    "dynamics/tires/tire_RR_pressure": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Tire Pressure Rear Right",
            icon="mdi:tire",
            key=f"{DOMAIN}_dynamics_tires_tire_rr_pressure",
            native_unit_of_measurement=PRESSURE_PSI,
        ),
        value_lambda=lambda v: round(convert_pressure(v, PRESSURE_BAR, PRESSURE_PSI)) if v < 4 else "--",
    ),
    "energy_storage/charger/adjusted_soc": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Battery State of Charge",
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
        value_lambda=lambda v: round(convert_distance(v, LENGTH_KILOMETERS, LENGTH_MILES)),
    ),
    "energy_storage/main/current": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Battery Pack Current",
            icon="mdi:current-dc",
            key=f"{DOMAIN}_energy_storage_main_current",
            native_unit_of_measurement=ELECTRIC_CURRENT_AMPERE,
        ),
        value_lambda=lambda v: v * -1,
    ),
    "energy_storage/main/voltage": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Battery Pack Voltage",
            icon="mdi:lightning-bolt",
            key=f"{DOMAIN}_energy_storage_main_voltage",
            native_unit_of_measurement=ELECTRIC_POTENTIAL_VOLT,
        ),
    ),
    "energy_storage/SOC/E_SOE_pack_user_real": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Battery Pack Capacity",
            icon="mdi:battery",
            key=f"{DOMAIN}_energy_storage_SOC_E_SOE_pack_user_real",
            native_unit_of_measurement=ENERGY_KILO_WATT_HOUR,
        ),
    ),
    "energy_storage/SOH/rp_SOH_Q_pack": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Battery State of Health",
            icon="mdi:battery-heart",
            key=f"{DOMAIN}_energy_storage_SOH_rp_SOH_Q_pack",
            native_unit_of_measurement=PERCENTAGE,
        ),
    ),
    "energy_storage/charger/wall_power_into_vehicle_actual": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Charging Speed",
            icon="mdi:battery-charging-outline",
            key=f"{DOMAIN}_energy_storage_charger_wall_power_into_vehicle_actual",
            native_unit_of_measurement=POWER_KILO_WATT,
        ),
    ),
    "energy_storage/charger/EMS_charger_evse_type": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Charging Station Type",
            icon="mdi:ev-plug-ccs1",
            key=f"{DOMAIN}_energy_storage_charger_EMS_charger_evse_type",
        ),
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
