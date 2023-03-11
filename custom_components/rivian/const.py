"""Rivian (Unofficial)"""
from __future__ import annotations
from typing import Final

from homeassistant.const import (
    LENGTH_MILES,
    PERCENTAGE,
    TEMP_FAHRENHEIT,
    TIME_MINUTES,
)

from homeassistant.const import UnitOfLength, UnitOfTemperature
from homeassistant.util.unit_conversion import DistanceConverter, TemperatureConverter

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
CONF_USER_SESSION_TOKEN = "user_session_token"

LOCK_STATE_ENTITIES = {
    "closureFrunkLocked",
    "closureLiftgateLocked",
    "closureSideBinLeftLocked",
    "closureSideBinRightLocked",
    "closureTailgateLocked",
    "closureTonneauLocked",
    "doorFrontLeftLocked",
    "doorFrontRightLocked",
    "doorRearLeftLocked",
    "doorRearRightLocked",
}

DOOR_STATE_ENTITIES = {
    "doorFrontLeftClosed",
    "doorFrontRightClosed",
    "doorRearLeftClosed",
    "doorRearRightClosed",
}

CLOSURE_STATE_ENTITIES = {
    "closureFrunkClosed",
    "closureLiftgateClosed",
    "closureSideBinLeftClosed",
    "closureSideBinRightClosed",
    "closureTailgateClosed",
    "closureTonneauClosed",
}

INVALID_SENSOR_STATES = ('fault', 'signal_not_available')

SENSORS: Final[dict[str, RivianSensorEntity]] = {
    "batteryHvThermalEvent": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Battery Thermal Status",
            icon="mdi:battery-alert",
            key=f"{DOMAIN}_dynamics_hv_battery_notifications_BMS_thermal_event",
        ),
        value_lambda=lambda v: v.replace("_", " ").title(),
    ),
    "batteryHvThermalEventPropagation": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Battery Thermal Runaway Propagation",
            icon="mdi:battery-alert",
            key=f"{DOMAIN}_energy_storage_icd_cid_notifications_b_pack_thermal_runaway_propagation",
        )
    ),
    "batteryLevel": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Battery State of Charge",
            key=f"{DOMAIN}_energy_storage_charger_adjusted_soc",
            native_unit_of_measurement=PERCENTAGE,
            device_class=SensorDeviceClass.BATTERY,
        ),
        value_lambda=lambda v: round(v, 1),
    ),
    "batteryLimit": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="SOC Limit",
            icon="mdi:battery-charging-80",
            key=f"{DOMAIN}_energy_storage_mobile_soc_limit",
            native_unit_of_measurement=PERCENTAGE,
        )
    ),
    "brakeFluidLow": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Brake Fluid Level Low",
            icon="mdi:car-brake-fluid-level",
            key=f"{DOMAIN}_dynamics_powertrain_status_brake_fluid_level_low",
        ),
    ),
    "cabinClimateDriverTemperature": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Driver Temperature",
            icon="mdi:thermometer",
            key=f"{DOMAIN}_thermal_hvac_cabin_control_driver_temperature",
            native_unit_of_measurement=TEMP_FAHRENHEIT,
        ),
        value_lambda=lambda v: round(
            TemperatureConverter.convert(v, UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT),
            1,
        ),
    ),
    "cabinClimateInteriorTemperature": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Cabin Temperature",
            icon="mdi:thermometer",
            key=f"{DOMAIN}_thermal_hvac_cabin_control_cabin_temperature",
            native_unit_of_measurement=TEMP_FAHRENHEIT,
        ),
        value_lambda=lambda v: round(
            TemperatureConverter.convert(v, UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT),
            1,
        ),
    ),
    "cabinPreconditioningType": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Cabin Climate Preconditioning Type",
            icon="mdi:thermostat",
            key=f"{DOMAIN}_cabin_preconditioning_type",
        ),
        value_lambda=lambda v: v.replace("_", " ").title(),
    ),
    "distanceToEmpty": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Estimated Vehicle Range",
            icon="mdi:map-marker-distance",
            key=f"{DOMAIN}_energy_storage_vehicle_energy_vehicle_range",
            native_unit_of_measurement=LENGTH_MILES,
        ),
        value_lambda=lambda v: round(DistanceConverter.convert(v, UnitOfLength.KILOMETERS, UnitOfLength.MILES), 1),
    ),
    "driveMode": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Drive Mode",
            icon="mdi:car-speed-limiter",
            key=f"{DOMAIN}_dynamics_modes_drive_mode",
            device_class=SensorDeviceClass.ENUM,
            options=[
                "All-Purpose",
                "Conserve",
                "Snow",
                "Sport",
                "Towing",
                "All-Terrain",
                "Drift",
                "Rally",
                "Rock Crawl",
                "Soft Sand",
            ],
        ),
        value_lambda=lambda v: {
            "everyday": "All-Purpose",
            "sport": "Sport",
            "distance": "Conserve",
            "winter": "Snow",
            "towing": "Towing",
            "off_road_towing": "All-Terrain",
            "off_road_sand": "Soft Sand",
            "off_road_rocks": "Rock Crawl",
            "off_road_sport_auto": "Rally",
            "off_road_sport_drift": "Drift",
        }.get(v, v),
    ),
    "gearStatus": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Gear Selector",
            icon="mdi:car-shift-pattern",
            key=f"{DOMAIN}_dynamics_propulsion_status_prndl",
            device_class=SensorDeviceClass.ENUM,
            options=[
                "Drive",
                "Neutral",
                "Park",
                "Reverse",
            ],
        ),
        value_lambda=lambda v: v.title(),
    ),
    "gearGuardVideoMode": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Gear Guard Video Mode",
            icon="mdi:cctv",
            key=f"{DOMAIN}_gear_guard_video_mode",
            device_class=SensorDeviceClass.ENUM,
            options=[
                "Away From Home",
                "Everywhere",
            ],
        ),
        value_lambda=lambda v: v.replace("_", " ").title(),
    ),
    "gearGuardVideoStatus": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Gear Guard Video Status",
            icon="mdi:cctv",
            key=f"{DOMAIN}_gear_guard_video_status",
            device_class=SensorDeviceClass.ENUM,
            options=[
                "Disabled",
                "Enabled",
                "Engaged",
            ],
        ),
        value_lambda=lambda v: v.replace("_", " ").title(),
    ),
    "gearGuardVideoTermsAccepted": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Gear Guard Video Terms Accepted",
            icon="mdi:cctv",
            key=f"{DOMAIN}_gear_guard_video_terms_accepted",
        ),
        value_lambda=lambda v: v.replace("_", " ").title(),
    ),
    "otaAvailableVersion": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Software OTA - Available Version",
            icon="mdi:package",
            key=f"{DOMAIN}_telematics_ota_status_available_version",
        )
    ),
    "otaAvailableVersionGitHash": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Software OTA - Available Version Git Hash",
            icon="mdi:commit",
            key=f"{DOMAIN}_telematics_ota_status_available_version_git_hash",
        )
    ),
    "otaAvailableVersionNumber": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Software OTA - Available Version Number",
            icon="mdi:package",
            key=f"{DOMAIN}_telematics_ota_status_available_version_number",
        )
    ),
    "otaAvailableVersionWeek": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Software OTA - Available Version Week",
            icon="mdi:package",
            key=f"{DOMAIN}_telematics_ota_status_available_version_week",
        )
    ),
    "otaAvailableVersionYear": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Software OTA - Available Version Year",
            icon="mdi:package",
            key=f"{DOMAIN}_telematics_ota_status_available_version_year",
        )
    ),
    "otaCurrentStatus": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Software OTA - Status Current",
            icon="mdi:package",
            key=f"{DOMAIN}_telematics_ota_status_status_current",
        ),
        value_lambda=lambda v: v.replace("_", " ").title(),
    ),
    "otaCurrentVersion": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Software OTA - Current Version",
            icon="mdi:package",
            key=f"{DOMAIN}_telematics_ota_status_current_version",
        )
    ),
    "otaCurrentVersionGitHash": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Software OTA - Current Version Git Hash",
            icon="mdi:commit",
            key=f"{DOMAIN}_telematics_ota_status_current_version_git_hash",
        )
    ),
    "otaCurrentVersionNumber": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Software OTA - Current Version Number",
            icon="mdi:package",
            key=f"{DOMAIN}_telematics_ota_status_current_version_number",
        )
    ),
    "otaCurrentVersionWeek": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Software OTA - Current Version Week",
            icon="mdi:package",
            key=f"{DOMAIN}_telematics_ota_status_current_version_week",
        )
    ),
    "otaCurrentVersionYear": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Software OTA - Current Version Year",
            icon="mdi:package",
            key=f"{DOMAIN}_telematics_ota_status_current_version_year",
        )
    ),
    "otaDownloadProgress": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Software OTA - Download Progress",
            icon="mdi:package",
            key=f"{DOMAIN}_telematics_ota_status_download_progress",
        )
    ),
    "otaInstallDuration": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Software OTA - Install Duration",
            icon="mdi:package",
            key=f"{DOMAIN}_telematics_ota_status_install_duration",
        )
    ),
    "otaInstallProgress": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Software OTA - Install Progress",
            icon="mdi:package",
            key=f"{DOMAIN}_telematics_ota_status_install_progress",
        )
    ),
    "otaInstallReady": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="CGM OTA Install - Install Ready",
            icon="mdi:package",
            key=f"{DOMAIN}_core_ota_status_cgm_ota_install_ready",
        ),
        value_lambda=lambda v: v.replace("_", " ").title().replace("Ota", "OTA"),
    ),
    "otaInstallTime": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Software OTA - Install Time",
            icon="mdi:package",
            key=f"{DOMAIN}_telematics_ota_status_install_time",
        )
    ),
    "otaInstallType": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Software OTA - Install Type",
            icon="mdi:package",
            key=f"{DOMAIN}_telematics_ota_status_install_type",
        )
    ),
    "otaStatus": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Software OTA - Status",
            icon="mdi:package",
            key=f"{DOMAIN}_telematics_ota_status_status",
            device_class=SensorDeviceClass.ENUM,
            options=[
                "Idle",
                "Downloading",
                "Preparing",
                "Ready To Install",
                "Install Countdown",
                "Installing",
                "Connection Lost",
            ],
        ),
        value_lambda=lambda v: v.replace("_", " ").title(),
    ),
    "petModeTemperatureStatus": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Pet Mode Temperature Status",
            icon="mdi:dog-side",
            key=f"{DOMAIN}_thermal_hvac_settings_pet_mode_temperature_status",
        ),
        value_lambda=lambda v: v.replace("_", " ").title(),
    ),
    "powerState": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Power State",
            icon="mdi:power",
            key=f"{DOMAIN}_core_power_modes_power_state",
            device_class=SensorDeviceClass.ENUM,
            options=[
                "Go",
                "Ready",
                "Sleep",
                "Standby",
            ],
        ),
        value_lambda=lambda v: v.title(),
    ),
    "rangeThreshold": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Range Threshold",
            icon="mdi:map-marker-distance",
            key=f"{DOMAIN}_energy_storage_icd_cid_notifications_range_threshold",
        ),
        value_lambda=lambda v: v.replace("_", " ").title(),
    ),
    "remoteChargingAvailable": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Remote Charging Available",
            icon="mdi:battery-charging-wireless-80",
            key=f"{DOMAIN}_energy_storage_mobile_remote_charging_available",
        )
    ),
    "timeToEndOfCharge": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Charging Time Remaining",
            key=f"{DOMAIN}_energy_storage_charger_EMS_charger_remaining_time_min_1",
            device_class=SensorDeviceClass.DURATION,
            native_unit_of_measurement=TIME_MINUTES,
        )
    ),
    "tirePressureStatusFrontLeft": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Tire Pressure Front Left",
            key=f"{DOMAIN}_dynamics_tires_tire_FL_pressure_status",
            icon="mdi:tire",
        )
    ),
    "tirePressureStatusFrontRight": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Tire Pressure Front Right",
            key=f"{DOMAIN}_dynamics_tires_tire_FR_pressure_status",
            icon="mdi:tire",
        )
    ),
    "tirePressureStatusRearLeft": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Tire Pressure Rear Left",
            key=f"{DOMAIN}_dynamics_tires_tire_RL_pressure_status",
            icon="mdi:tire",
        )
    ),
    "tirePressureStatusRearRight": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Tire Pressure Rear Right",
            key=f"{DOMAIN}_dynamics_tires_tire_RR_pressure_status",
            icon="mdi:tire",
        )
    ),
    "vehicleMileage": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Odometer",
            icon="mdi:counter",
            key=f"{DOMAIN}_dynamics_odometer_value",
            native_unit_of_measurement=LENGTH_MILES,
        ),
        value_lambda=lambda v: round(DistanceConverter.convert(v, UnitOfLength.METERS, UnitOfLength.MILES), 1),
    ),
    "windowFrontLeftCalibrated": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Window Calibration Front Left State",
            key=f"{DOMAIN}_body_closures_window_calibration_FL_state",
            icon="mdi:window-closed",
        )
    ),
    "windowFrontRightCalibrated": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Window Calibration Front Right State",
            key=f"{DOMAIN}_body_closures_window_calibration_FR_state",
            icon="mdi:window-closed",
        )
    ),
    "windowRearLeftCalibrated": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Window Calibration Rear Left State",
            key=f"{DOMAIN}_body_closures_window_calibration_RL_state",
            icon="mdi:window-closed",
        )
    ),
    "windowRearRightCalibrated": RivianSensorEntity(
        entity_description=RivianSensorEntityDescription(
            name="Window Calibration Rear Right State",
            key=f"{DOMAIN}_body_closures_window_calibration_RR_state",
            icon="mdi:window-closed",
        )
    ),
    # "core/ota_status/cgm_ota_install_fast_charging": RivianSensorEntity(
    #     entity_description=RivianSensorEntityDescription(
    #         name="CGM OTA Install - Fast Charging",
    #         key=f"{DOMAIN}_core_ota_status_cgm_ota_install_fast_charging",
    #     )
    # ),
    # "core/ota_status/cgm_ota_install_hv_batt_low": RivianSensorEntity(
    #     entity_description=RivianSensorEntityDescription(
    #         name="CGM OTA Install - HV Battery Low",
    #         key=f"{DOMAIN}_core_ota_status_cgm_ota_install_hv_batt_low",
    #     )
    # ),
    # "core/ota_status/cgm_ota_install_not_parked": RivianSensorEntity(
    #     entity_description=RivianSensorEntityDescription(
    #         name="CGM OTA Install - Not Parked",
    #         key=f"{DOMAIN}_core_ota_status_cgm_ota_install_not_parked",
    #     )
    # ),
    # "energy_storage/vehicle_efficiency/lifetime_wh_per_km": RivianSensorEntity(
    #     entity_description=RivianSensorEntityDescription(
    #         name="Lifetime Efficiency (Wh/mi)",
    #         key=f"{DOMAIN}_energy_storage_vehicle_efficiency_lifetime_wh_per_km",
    #     ),
    #     value_lambda=lambda v: round(DistanceConverter.convert(v, UnitOfLength.MILES, UnitOfLength.KILOMETERS), 1),
    # ),
    # "telematics/ota_status/pending_reason_active_mode": RivianSensorEntity(
    #     entity_description=RivianSensorEntityDescription(
    #         name="Software OTA - Pending Reason Active Mode",
    #         key=f"{DOMAIN}_telematics_ota_status_pending_reason_active_mode",
    #     )
    # ),
    # "telematics/ota_status/pending_reason_lv_batt": RivianSensorEntity(
    #     entity_description=RivianSensorEntityDescription(
    #         name="Software OTA - Pending Reason LV Battery",
    #         key=f"{DOMAIN}_telematics_ota_status_pending_reason_lv_batt",
    #     )
    # ),
    # "telematics/ota_status/pending_reason_other": RivianSensorEntity(
    #     entity_description=RivianSensorEntityDescription(
    #         name="Software OTA - Pending Reason Other",
    #         key=f"{DOMAIN}_telematics_ota_status_pending_reason_other",
    #     )
    # ),
    # "energy_storage/charger/EMS_charger_evse_type": RivianSensorEntity(
    #     entity_description=RivianSensorEntityDescription(
    #         name="EMS Charger EVSE Type",
    #         key=f"{DOMAIN}_energy_storage_charger_EMS_charger_evse_type",
    #     )
    # ),
    # "telematics/privacy/offline_mode_enabled": RivianSensorEntity(
    #     entity_description=RivianSensorEntityDescription(
    #         name="Offline Mode Enabled",
    #         key=f"{DOMAIN}_telematics_privacy_offline_mode_enabled",
    #     )
    # ),
    # "thermal/hvac_mobile_status/estimated_cabin_temp_mobile": RivianSensorEntity(
    #     entity_description=RivianSensorEntityDescription(
    #         name="Estimated Cabin Temperature",
    #         icon="mdi:thermometer",
    #         key=f"{DOMAIN}_thermal_hvac_mobile_status_estimated_cabin_temp_mobile",
    #         native_unit_of_measurement=TEMP_FAHRENHEIT,
    #     ),
    #     value_lambda=lambda v: round(
    #         TemperatureConverter.convert(v, UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT), 1
    #     ),
    # ),
    # "thermal/hvac_mobile_status/set_temp_status_mobile": RivianSensorEntity(
    #     entity_description=RivianSensorEntityDescription(
    #         name="Set Temperature Mobile Status",
    #         icon="mdi:thermometer",
    #         key=f"{DOMAIN}_thermal_hvac_mobile_status_set_temp_status_mobile",
    #         native_unit_of_measurement=TEMP_FAHRENHEIT,
    #     ),
    #     value_lambda=lambda v: round(
    #         TemperatureConverter.convert(v, UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT), 1
    #     ),
    # ),
}

BINARY_SENSORS: Final[dict[str, RivianBinarySensorEntity]] = {
    "alarmSoundStatus": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Gear Guard Alarm",
            key=f"{DOMAIN}_body_alarm_sound_alarm",
            device_class=BinarySensorDeviceClass.TAMPER,
            on_value="true",
        )
    ),
    "cabinPreconditioningStatus": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Cabin Climate Preconditioning",
            key=f"{DOMAIN}_thermal_tmm_status_cabin_precondition_state",
            device_class=BinarySensorDeviceClass.RUNNING,
            on_value=["undefined", "unavailable"],
            negate=True,
        )
    ),
    "chargerState": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Charging Status",
            key=f"{DOMAIN}_energy_storage_charger_vehicle_charger_state",
            device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
            on_value="charging_active",
        )
    ),
    "chargerStatus": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Charger Connection",
            key=f"{DOMAIN}_energy_storage_charger_status_vehicle_charger_status",
            device_class=BinarySensorDeviceClass.PLUG,
            on_value="chrgr_sts_not_connected",
            negate=True,
        )
    ),
    "closureFrunkClosed": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Front Trunk",
            key=f"{DOMAIN}_body_closures_frunk_state",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        )
    ),
    "closureFrunkLocked": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Front Trunk",
            key=f"{DOMAIN}_body_closures_frunk_locked_state",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        )
    ),
    "closureLiftgateClosed": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Liftgate",
            key=f"{DOMAIN}_body_closures_liftgate_state",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        )
    ),
    "closureLiftgateLocked": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Liftgate",
            key=f"{DOMAIN}_body_closures_liftgate_locked_state",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        )
    ),
    "closureSideBinLeftClosed": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Gear Tunnel Left",
            key=f"{DOMAIN}_body_closures_sidebin_L_state",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        )
    ),
    "closureSideBinLeftLocked": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Gear Tunnel Left",
            key=f"{DOMAIN}_body_closures_sidebin_L_locked_state",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        )
    ),
    "closureSideBinRightClosed": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Gear Tunnel Right",
            key=f"{DOMAIN}_body_closures_sidebin_R_state",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        )
    ),
    "closureSideBinRightLocked": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Gear Tunnel Right",
            key=f"{DOMAIN}_body_closures_sidebin_R_locked_state",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        )
    ),
    "closureTailgateClosed": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Tailgate",
            key=f"{DOMAIN}_body_closures_tailgate_state",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        )
    ),
    "closureTailgateLocked": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Tailgate",
            key=f"{DOMAIN}_body_closures_tailgate_locked_state",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        )
    ),
    "closureTonneauClosed": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Tonneau",
            key=f"{DOMAIN}_body_closures_tonneau_state",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        )
    ),
    "closureTonneauLocked": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Tonneau",
            key=f"{DOMAIN}_body_closures_tonneau_locked_state",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        )
    ),
    "defrostDefogStatus": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Defrost/Defog",
            icon="mdi:car-defrost-front",
            key=f"{DOMAIN}_thermal_hvac_mobile_status_defrost_defog_status",
            device_class=BinarySensorDeviceClass.RUNNING,
            on_value="Off",
            negate=True,
        )
    ),
    "doorFrontLeftClosed": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Door Front Left",
            key=f"{DOMAIN}_body_closures_door_FL_state",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        )
    ),
    "doorFrontLeftLocked": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Door Front Left",
            key=f"{DOMAIN}_body_closures_door_FL_locked_state",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        )
    ),
    "doorFrontRightClosed": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Door Front Right",
            key=f"{DOMAIN}_body_closures_door_FR_state",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        )
    ),
    "doorFrontRightLocked": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Door Front Right",
            key=f"{DOMAIN}_body_closures_door_FR_locked_state",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        )
    ),
    "doorRearLeftClosed": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Door Rear Left",
            key=f"{DOMAIN}_body_closures_door_RL_state",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        )
    ),
    "doorRearLeftLocked": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Door Rear Left",
            key=f"{DOMAIN}_body_closures_door_RL_locked_state",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        )
    ),
    "doorRearRightClosed": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Door Rear Right",
            key=f"{DOMAIN}_body_closures_door_RR_state",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        )
    ),
    "doorRearRightLocked": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Door Rear Right",
            key=f"{DOMAIN}_body_closures_door_RR_locked_state",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        )
    ),
    "gearGuardLocked": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Gear Guard",
            key=f"{DOMAIN}_body_closures_gear_guard_locked_state",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        )
    ),
    "petModeStatus": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Pet Mode",
            key=f"{DOMAIN}_thermal_hvac_settings_pet_mode_status",
            device_class=BinarySensorDeviceClass.RUNNING,
            on_value="On",
        )
    ),
    "seatFrontLeftHeat": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Heated Seat Front Left",
            icon="mdi:car-seat-heater",
            key=f"{DOMAIN}_thermal_hvac_mobile_status_left_seat_heat_status",
            device_class=BinarySensorDeviceClass.RUNNING,
            on_value=["Level_1", "Level_2", "Level_3"],
        )
    ),
    "seatFrontLeftVent": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Vented Seat Front Left",
            icon="mdi:car-seat-cooler",
            key=f"{DOMAIN}_thermal_hvac_mobile_status_left_seat_vent_status",
            device_class=BinarySensorDeviceClass.RUNNING,
            on_value=["Level_1", "Level_2", "Level_3"],
        )
    ),
    "seatFrontRightHeat": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Heated Seat Front Right",
            icon="mdi:car-seat-heater",
            key=f"{DOMAIN}_thermal_hvac_mobile_status_right_seat_heat_status",
            device_class=BinarySensorDeviceClass.RUNNING,
            on_value=["Level_1", "Level_2", "Level_3"],
        )
    ),
    "seatFrontRightVent": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Vented Seat Front Right",
            icon="mdi:car-seat-cooler",
            key=f"{DOMAIN}_thermal_hvac_mobile_status_right_seat_vent_status",
            device_class=BinarySensorDeviceClass.RUNNING,
            on_value=["Level_1", "Level_2", "Level_3"],
        )
    ),
    "seatRearLeftHeat": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Heated Seat Rear Left",
            icon="mdi:car-seat-heater",
            key=f"{DOMAIN}_thermal_hvac_mobile_status_rear_left_seat_heat_status",
            device_class=BinarySensorDeviceClass.RUNNING,
            on_value=["Level_1", "Level_2", "Level_3"],
        )
    ),
    "seatRearRightHeat": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Heated Seat Rear Right",
            icon="mdi:car-seat-heater",
            key=f"{DOMAIN}_thermal_hvac_mobile_status_rear_right_seat_heat_status",
            device_class=BinarySensorDeviceClass.RUNNING,
            on_value=["Level_1", "Level_2", "Level_3"],
        )
    ),
    "seatThirdRowLeftHeat": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Heated Seat 3rd Row Left",
            icon="mdi:car-seat-heater",
            key=f"{DOMAIN}_thermal_hvac_mobile_status_3rd_row_left_seat_heat_status",
            device_class=BinarySensorDeviceClass.RUNNING,
            on_value=["Level_1", "Level_2", "Level_3"],
        )
    ),
    "seatThirdRowRightHeat": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Heated Seat 3rd Row Right",
            icon="mdi:car-seat-heater",
            key=f"{DOMAIN}_thermal_hvac_mobile_status_3rd_row_right_seat_heat_status",
            device_class=BinarySensorDeviceClass.RUNNING,
            on_value=["Level_1", "Level_2", "Level_3"],
        )
    ),
    "steeringWheelHeat": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Heated Steering Wheel",
            icon="mdi:steering",  # mdi:steering-heater, https://github.com/Templarian/MaterialDesign/issues/6925
            key=f"{DOMAIN}_thermal_hvac_mobile_status_steering_wheel_heat_status",
            device_class=BinarySensorDeviceClass.RUNNING,
            on_value="Level_1",
        )
    ),
    "tirePressureStatusValidFrontLeft": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Tire Pressure Front Left Validity",
            key=f"{DOMAIN}_dynamics_tires_tire_FL_pressure_status_valid",
            device_class=BinarySensorDeviceClass.PROBLEM,
            on_value="invalid",
        )
    ),
    "tirePressureStatusValidFrontRight": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Tire Pressure Front Right Validity",
            key=f"{DOMAIN}_dynamics_tires_tire_FR_pressure_status_valid",
            device_class=BinarySensorDeviceClass.PROBLEM,
            on_value="invalid",
        )
    ),
    "tirePressureStatusValidRearLeft": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Tire Pressure Rear Left Validity",
            key=f"{DOMAIN}_dynamics_tires_tire_RL_pressure_status_valid",
            device_class=BinarySensorDeviceClass.PROBLEM,
            on_value="invalid",
        )
    ),
    "tirePressureStatusValidRearRight": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Tire Pressure Rear Right Validity",
            key=f"{DOMAIN}_dynamics_tires_tire_RR_pressure_status_valid",
            device_class=BinarySensorDeviceClass.PROBLEM,
            on_value="invalid",
        )
    ),
    "windowFrontLeftClosed": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Window Front Left",
            key=f"{DOMAIN}_body_closures_front_left_window_state",
            device_class=BinarySensorDeviceClass.WINDOW,
            on_value="open",
        )
    ),
    "windowFrontRightClosed": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Window Front Right",
            key=f"{DOMAIN}_body_closures_front_right_window_state",
            device_class=BinarySensorDeviceClass.WINDOW,
            on_value="open",
        )
    ),
    "windowRearLeftClosed": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Window Rear Left",
            key=f"{DOMAIN}_body_closures_rear_left_window_state",
            device_class=BinarySensorDeviceClass.WINDOW,
            on_value="open",
        )
    ),
    "windowRearRightClosed": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Window Rear Right",
            key=f"{DOMAIN}_body_closures_rear_right_window_state",
            device_class=BinarySensorDeviceClass.WINDOW,
            on_value="open",
        )
    ),
    "wiperFluidState": RivianBinarySensorEntity(
        entity_description=RivianBinarySensorEntityDescription(
            name="Wiper Fluid Level",
            icon="mdi:wiper-wash",
            key=f"{DOMAIN}_body_wipers_fluid_state",
            device_class=BinarySensorDeviceClass.PROBLEM,
            on_value="low",
        )
    ),
}
