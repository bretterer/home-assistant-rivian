"""Rivian (Unofficial)"""
from __future__ import annotations
from typing import Final

from homeassistant.const import (
    LENGTH_METERS,
    LENGTH_KILOMETERS,
    LENGTH_MILES,
    PERCENTAGE,
    TEMP_CELSIUS,
    TEMP_FAHRENHEIT,
    TIME_MINUTES,
)

from homeassistant.util.distance import convert as convert_distance
from homeassistant.util.temperature import convert as convert_temperature

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass

from .data_classes import (
    RivianSensorEntity,
    RivianSensorEntityDescription,
    RivianBinarySensorEntity,
    RivianBinarySensorEntityDescription,
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
CONF_VIN = "vin"
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"

SENSORS: Final[dict[str, RivianSensorEntity]] = {
    "core/ota_status/cgm_ota_install_fast_charging": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="CGM OTA Install - Fast Charging",
            key=f"{DOMAIN}_core_ota_status_cgm_ota_install_fast_charging",
        )
    ),
    "core/ota_status/cgm_ota_install_hv_batt_low": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="CGM OTA Install - HV Battery Low",
            key=f"{DOMAIN}_core_ota_status_cgm_ota_install_hv_batt_low",
        )
    ),
    "core/ota_status/cgm_ota_install_not_parked": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="CGM OTA Install - Not Parked",
            key=f"{DOMAIN}_core_ota_status_cgm_ota_install_not_parked",
        )
    ),
    "core/ota_status/cgm_ota_install_ready": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="CGM OTA Install - Install Ready",
            key=f"{DOMAIN}_core_ota_status_cgm_ota_install_ready",
        )
    ),
    "core/power_modes/power_state": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Power State",
            key=f"{DOMAIN}_core_power_modes_power_state",
        )
    ),
    "dynamics/hv_battery_notifications/BMS_thermal_event": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Battery Thermal Status",
            key=f"{DOMAIN}_dynamics_hv_battery_notifications_BMS_thermal_event",
        )
    ),
    "dynamics/modes/drive_mode": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Drive Mode",
            key=f"{DOMAIN}_dynamics_modes_drive_mode",
        )
    ),
    "dynamics/odometer/value": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Odometer",
            icon="mdi:counter",
            key=f"{DOMAIN}_dynamics_odometer_value",
            native_unit_of_measurement=LENGTH_MILES,
        ),
        value_lambda=lambda v: round(
            convert_distance(v, LENGTH_METERS, LENGTH_MILES), 1
        ),
    ),
    "dynamics/powertrain_status/brake_fluid_level_low": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Brake Fluid Level Low",
            key=f"{DOMAIN}_dynamics/powertrain_status/brake_fluid_level_low",
        ),
    ),
    "dynamics/propulsion_status/PRNDL": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Gear Selector",
            key=f"{DOMAIN}_dynamics_propulsion_status_prndl",
        ),
    ),
    "dynamics/tires/tire_FL_pressure_status": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Tire Pressure Front Left",
            key=f"{DOMAIN}_dynamics_tires_tire_FL_pressure_status",
        )
    ),
    "dynamics/tires/tire_FR_pressure_status": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Tire Pressure Front Right",
            key=f"{DOMAIN}_dynamics_tires_tire_FR_pressure_status",
        )
    ),
    "dynamics/tires/tire_RL_pressure_status": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Tire Pressure Rear Left",
            key=f"{DOMAIN}_dynamics_tires_tire_RL_pressure_status",
        )
    ),
    "dynamics/tires/tire_RR_pressure_status": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Tire Pressure Rear Right",
            key=f"{DOMAIN}_dynamics_tires_tire_RR_pressure_status",
        )
    ),
    "energy_storage/charger/adjusted_soc": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Battery State of Charge",
            key=f"{DOMAIN}_energy_storage_charger_adjusted_soc",
            native_unit_of_measurement=PERCENTAGE,
            device_class=SensorDeviceClass.BATTERY,
        ),
        value_lambda=lambda v: round(v, 1),
    ),
    "energy_storage/charger/EMS_charger_remainingtime_min_1": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Charging Time Remainng",
            key=f"{DOMAIN}_energy_storage_charger_EMS_charger_remainingtime_min_1",
            device_class=SensorDeviceClass.DURATION,
            native_unit_of_measurement=TIME_MINUTES,
        )
    ),
    "energy_storage/icd_cid_notifications/b_pack_thermal_runaway_propagation": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Battery Thermal Runwaway Propagation",
            key=f"{DOMAIN}_energy_storage_icd_cid_notifications_b_pack_thermal_runaway_propagation",
        )
    ),
    "energy_storage/icd_cid_notifications/range_threshold": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Range Threshold",
            key=f"{DOMAIN}_energy_storage_icd_cid_notifications_range_threshold",
        )
    ),
    "energy_storage/vehicle_efficiency/lifetime_wh_per_km": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Lifetime Efficiency (Wh/mi)",
            key=f"{DOMAIN}_energy_storage_vehicle_efficiency_lifetime_wh_per_km",
        ),
        value_lambda=lambda v: round(
            convert_distance(v, LENGTH_MILES, LENGTH_KILOMETERS), 1
        ),
    ),
    "energy_storage/vehicle_energy/vehicle_range": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Estimated Vehicle Range",
            icon="mdi:map-marker-distance",
            key=f"{DOMAIN}_energy_storage_vehicle_energy_vehicle_range",
            native_unit_of_measurement=LENGTH_MILES,
        ),
        value_lambda=lambda v: round(
            convert_distance(v, LENGTH_KILOMETERS, LENGTH_MILES), 1
        ),
    ),
    "telematics/ota_status/available_version_number": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Software OTA - Available Version Number",
            key=f"{DOMAIN}_telematics_ota_status_available_version_number",
        )
    ),
    "telematics/ota_status/available_version_week": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Software OTA - Available Version Week",
            key=f"{DOMAIN}_telematics_ota_status_available_version_week",
        )
    ),
    "telematics/ota_status/available_version_year": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Software OTA - Available Version Year",
            key=f"{DOMAIN}_telematics_ota_status_available_version_year",
        )
    ),
    "telematics/ota_status/available_version": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Software OTA - Available Version",
            key=f"{DOMAIN}_telematics_ota_status_available_version",
        )
    ),
    "telematics/ota_status/current_version_number": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Software OTA - Current Version Number",
            key=f"{DOMAIN}_telematics_ota_status_current_version_number",
        )
    ),
    "telematics/ota_status/current_version_week": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Software OTA - Current Version Week",
            key=f"{DOMAIN}_telematics_ota_status_current_version_week",
        )
    ),
    "telematics/ota_status/current_version_year": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Software OTA - Current Version Year",
            key=f"{DOMAIN}_telematics_ota_status_current_version_year",
        )
    ),
    "telematics/ota_status/current_version": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Software OTA - Current Version",
            key=f"{DOMAIN}_telematics_ota_status_current_version",
        )
    ),
    "telematics/ota_status/download_progress": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Software OTA - Download Progress",
            key=f"{DOMAIN}_telematics_ota_status_download_progress",
        )
    ),
    "telematics/ota_status/install_duration": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Software OTA - Install Duration",
            key=f"{DOMAIN}_telematics_ota_status_install_duration",
        )
    ),
    "telematics/ota_status/install_progress": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Software OTA - Install Progress",
            key=f"{DOMAIN}_telematics_ota_status_install_progress",
        )
    ),
    "telematics/ota_status/install_time": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Software OTA - Install Time",
            key=f"{DOMAIN}_telematics_ota_status_install_time",
        )
    ),
    "telematics/ota_status/install_type": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Software OTA - Install Type",
            key=f"{DOMAIN}_telematics_ota_status_install_type",
        )
    ),
    "telematics/ota_status/pending_reason_active_mode": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Software OTA - Pending Reason Active Mode",
            key=f"{DOMAIN}_telematics_ota_status_pending_reason_active_mode",
        )
    ),
    "telematics/ota_status/pending_reason_lv_batt": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Software OTA - Pending Reason LV Battery",
            key=f"{DOMAIN}_telematics_ota_status_pending_reason_lv_batt",
        )
    ),
    "telematics/ota_status/pending_reason_other": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Software OTA - Pending Reason Other",
            key=f"{DOMAIN}_telematics_ota_status_pending_reason_other",
        )
    ),
    "telematics/ota_status/status_current": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Software OTA - Status Current",
            key=f"{DOMAIN}_telematics_ota_status_status_current",
        )
    ),
    "telematics/ota_status/status": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Software OTA - Status",
            key=f"{DOMAIN}_telematics_ota_status_status",
        )
    ),
    "thermal/hvac_cabin_control/cabin_temperature": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Cabin Temperature",
            icon="mdi:thermometer",
            key=f"{DOMAIN}_thermal_hvac_cabin_control_cabin_temperature",
            native_unit_of_measurement=TEMP_FAHRENHEIT,
        ),
        value_lambda=lambda v: round(
            convert_temperature(v, TEMP_CELSIUS, TEMP_FAHRENHEIT)
        ),
    ),
    "thermal/hvac_cabin_control/driver_temperature": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Driver Temperature",
            icon="mdi:thermometer",
            key=f"{DOMAIN}_thermal_hvac_cabin_control_driver_temperature",
            native_unit_of_measurement=TEMP_FAHRENHEIT,
        ),
        value_lambda=lambda v: round(
            convert_temperature(v, TEMP_CELSIUS, TEMP_FAHRENHEIT)
        ),
    ),
    "thermal/hvac_settings/pet_mode_temperature_status": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Pet Mode Temperature Status",
            key=f"{DOMAIN}_thermal_hvac_settings_pet_mode_temperature_status",
        )
    ),
}

BINARY_SENSORS: Final[dict[str, RivianBinarySensorEntity]] = {
    "body/alarm/sound_alarm": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Gear Guard Alarm",
            icon="mdi:alarm",
            key=f"{DOMAIN}_body_alarm_sound_alarm",
            device_class=BinarySensorDeviceClass.TAMPER,
            on_value="true",
        )
    ),
    "body/closures/door_FL_locked_state": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Door Front Left",
            key=f"{DOMAIN}_body_closures_door_FL_locked_state",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        )
    ),
    "body/closures/door_FL_state": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Door Front Left",
            key=f"{DOMAIN}_body_closures_door_FL_state",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        )
    ),
    "body/closures/door_FR_locked_state": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Door Front Right",
            key=f"{DOMAIN}_body_closures_door_FR_locked_state",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        )
    ),
    "body/closures/door_FR_state": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Door Front Right",
            key=f"{DOMAIN}_body_closures_door_FR_state",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        )
    ),
    "body/closures/door_RL_locked_state": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Door Rear Left",
            key=f"{DOMAIN}_body_closures_door_RL_locked_state",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        )
    ),
    "body/closures/door_RL_state": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Door Rear Left",
            key=f"{DOMAIN}_body_closures_door_RL_state",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        )
    ),
    "body/closures/door_RR_locked_state": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Door Rear Right",
            key=f"{DOMAIN}_body_closures_door_RR_locked_state",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        )
    ),
    "body/closures/door_RR_state": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Door Rear Right",
            key=f"{DOMAIN}_body_closures_door_RR_state",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        )
    ),
    "body/closures/front_left_window_state": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Window Front Left",
            key=f"{DOMAIN}_body_closures_front_left_window_state",
            device_class=BinarySensorDeviceClass.WINDOW,
            on_value="open",
        )
    ),
    "body/closures/front_right_window_state": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Window Front Right",
            key=f"{DOMAIN}_body_closures_front_right_window_state",
            device_class=BinarySensorDeviceClass.WINDOW,
            on_value="open",
        )
    ),
    "body/closures/frunk_locked_state": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Front Trunk",
            key=f"{DOMAIN}_body_closures_frunk_locked_state",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        )
    ),
    "body/closures/frunk_state": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Front Trunk",
            key=f"{DOMAIN}_body_closures_frunk_state",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        )
    ),
    "body/closures/gear_guard_locked_state": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Gear Guard",
            key=f"{DOMAIN}_body_closures_gear_guard_locked_state",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        )
    ),
    "body/closures/liftgate_locked_state": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Liftgate",
            key=f"{DOMAIN}_body_closures_liftgate_locked_state",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        )
    ),
    "body/closures/liftgate_state": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Liftgate",
            key=f"{DOMAIN}_body_closures_liftgate_state",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        )
    ),
    "body/closures/rear_left_window_state": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Window Rear Left",
            key=f"{DOMAIN}_body_closures_rear_left_window_state",
            device_class=BinarySensorDeviceClass.WINDOW,
            on_value="open",
        )
    ),
    "body/closures/rear_right_window_state": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Window Rear Right",
            key=f"{DOMAIN}_body_closures_rear_right_window_state",
            device_class=BinarySensorDeviceClass.WINDOW,
            on_value="open",
        )
    ),
    "body/closures/sidebin_L_locked_state": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Gear Tunnel Left",
            key=f"{DOMAIN}_body_closures_sidebin_L_locked_state",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        )
    ),
    "body/closures/sidebin_L_state": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Gear Tunnel Left",
            key=f"{DOMAIN}_body_closures_sidebin_L_state",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        )
    ),
    "body/closures/sidebin_R_locked_state": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Gear Tunnel Right",
            key=f"{DOMAIN}_body_closures_sidebin_R_locked_state",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        )
    ),
    "body/closures/sidebin_R_state": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Gear Tunnel Right",
            key=f"{DOMAIN}_body_closures_sidebin_R_state",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        )
    ),
    "body/closures/tailgate_locked_state": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Tailgate",
            key=f"{DOMAIN}_body_closures_tailgate_locked_state",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        )
    ),
    "body/closures/tailgate_state": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Tailgate",
            key=f"{DOMAIN}_body_closures_tailgate_state",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        )
    ),
    "body/closures/tonneau_locked_state": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Tonneau",
            key=f"{DOMAIN}_body_closures_tonneau_locked_state",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        )
    ),
    "body/closures/tonneau_state": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Tonneau",
            key=f"{DOMAIN}_body_closures_tonneau_state",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        )
    ),
    "body/door_control_log/BCM_WindowCalibrationFL_Status": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Window Front Left Calibration",
            key=f"{DOMAIN}_body_door_control_log_BCM_WindowCalibrationFL_Status",
            device_class=BinarySensorDeviceClass.PROBLEM,
            on_value="Not_Calibrated",
        )
    ),
    "body/door_control_log/BCM_WindowCalibrationFR_Status": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Window Front Right Calibration",
            key=f"{DOMAIN}_body_door_control_log_BCM_WindowCalibrationFR_Status",
            device_class=BinarySensorDeviceClass.PROBLEM,
            on_value="Not_Calibrated",
        )
    ),
    "body/door_control_log/BCM_WindowCalibrationRL_Status": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Window Rear Left Calibration",
            key=f"{DOMAIN}_body_door_control_log_BCM_WindowCalibrationRL_Status",
            device_class=BinarySensorDeviceClass.PROBLEM,
            on_value="Not_Calibrated",
        )
    ),
    "body/door_control_log/BCM_WindowCalibrationRR_Status": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Window Rear Right Calibration",
            key=f"{DOMAIN}_body_door_control_log_BCM_WindowCalibrationRR_Status",
            device_class=BinarySensorDeviceClass.PROBLEM,
            on_value="Not_Calibrated",
        )
    ),
    "body/wipers/fluid_state": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Wiper Fluid Level",
            key=f"{DOMAIN}_body_wipers_fluid_state",
            device_class=BinarySensorDeviceClass.PROBLEM,
            on_value="low",
        )
    ),
    "dynamics/tires/tire_FL_pressure_status_valid": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Tire Pressure Front Left Validity",
            key=f"{DOMAIN}_dynamics_tires_tire_FL_pressure_status_valid",
            device_class=BinarySensorDeviceClass.PROBLEM,
            on_value="invalid",
        )
    ),
    "dynamics/tires/tire_FR_pressure_status_valid": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Tire Pressure Front Right Validity",
            key=f"{DOMAIN}_dynamics_tires_tire_FR_pressure_status_valid",
            device_class=BinarySensorDeviceClass.PROBLEM,
            on_value="invalid",
        )
    ),
    "dynamics/tires/tire_RL_pressure_status_valid": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Tire Pressure Rear Left Validity",
            key=f"{DOMAIN}_dynamics_tires_tire_RL_pressure_status_valid",
            device_class=BinarySensorDeviceClass.PROBLEM,
            on_value="invalid",
        )
    ),
    "dynamics/tires/tire_RR_pressure_status_valid": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Tire Pressure Rear Right Validity",
            key=f"{DOMAIN}_dynamics_tires_tire_RR_pressure_status_valid",
            device_class=BinarySensorDeviceClass.PROBLEM,
            on_value="invalid",
        )
    ),
    "energy_storage/charger/vehicle_charger_state": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Charging Status",
            key=f"{DOMAIN}_energy_storage_charger_vehicle_charger_state",
            device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
            on_value="charging_active",
        )
    ),
    "thermal/hvac_settings/pet_mode_status": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Pet Mode",
            key=f"{DOMAIN}_thermal_hvac_settings_pet_mode_status",
            on_value="On",
        )
    ),
    "thermal/tmm_status/cabin_precondition_state": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Cabin Climate Preconditioning",
            key=f"{DOMAIN}_thermal_tmm_status_cabin_precondition_state",
            on_value=["undefined", "unavailable"],
            negate=True,
        )
    ),
}
