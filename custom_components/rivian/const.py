"""Rivian (Unofficial)"""

from __future__ import annotations

from typing import Any, Final

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    DEGREE,
    PERCENTAGE,
    EntityCategory,
    UnitOfEnergy,
    UnitOfLength,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
    UnitOfTime,
)

from .data_classes import (
    RivianBinarySensorEntityDescription,
    RivianSensorEntityDescription,
)

NAME = "Rivian (Unofficial)"
DOMAIN = "rivian"
VERSION = "0.0.0"
ISSUE_URL = "https://github.com/bretterer/home-assistant-rivian/issues"

# Attributes
ATTR_API = "api"
ATTR_COORDINATOR = "coordinator"
ATTR_USER = "user"
ATTR_VEHICLE = "vehicle"
ATTR_WALLBOX = "wallbox"

# Config properties
CONF_ACCESS_TOKEN = "access_token"
CONF_MFA_VERIFIED = "mfa_verified"
CONF_OTP = "otp"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_USER_SESSION_TOKEN = "user_session_token"
CONF_VEHICLE_CONTROL = "vehicle_control"
CONF_VEHICLE_IMAGE_STYLE = "vehicle_image_style"

IMAGE_STYLE_CEL = "cel"
IMAGE_STYLE_PHOTO = "photo"
IMAGE_STYLE_NONE = "none"

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

INVALID_SENSOR_STATES = {"fault", "signal_not_available", "undefined"}


TIRE_PRESSURE_STATUS_OPTIONS: Final[list[str]] = [
    "ok",
    "warning_hard",
    "warning_soft",
    "warning_puncture",
]

WINDOW_CALIBRATION_OPTIONS: Final[list[str]] = [
    "calibrated",
    "not_calibrated",
]

BTM_FAILURE_STATUS_OPTIONS: Final[list[str]] = [
    "dtc_not_set",
    "set",
]


SENSORS: Final[dict[tuple[str, ...], tuple[RivianSensorEntityDescription, ...]]] = {
    ("R1", "R2"): (
        RivianSensorEntityDescription(
            key="active_driver",
            translation_key="active_driver",
            field="activeDriverName",
        ),
        RivianSensorEntityDescription(
            key="altitude",
            field="gnssAltitude",
            name="Altitude",
            icon="mdi:altimeter",
            device_class=SensorDeviceClass.DISTANCE,
            native_unit_of_measurement=UnitOfLength.METERS,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=0,
        ),
        RivianSensorEntityDescription(
            key="battery_thermal_status",
            translation_key="battery_thermal_status",
            field="batteryHvThermalEvent",
            icon="mdi:battery-alert",
            device_class=SensorDeviceClass.ENUM,
            options=[
                "nominal",
                "detected",
            ],
        ),
        RivianSensorEntityDescription(
            key="battery_thermal_runaway_propagation",
            translation_key="battery_thermal_runaway_propagation",
            field="batteryHvThermalEventPropagation",
            icon="mdi:battery-alert",
            device_class=SensorDeviceClass.ENUM,
            options=[
                "nominal",
                "detected",
            ],
        ),
        RivianSensorEntityDescription(
            key="battery_level",
            field="batteryLevel",
            name="Battery State of Charge",
            device_class=SensorDeviceClass.BATTERY,
            native_unit_of_measurement=PERCENTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=1,
        ),
        RivianSensorEntityDescription(
            key="battery_limit",
            field="batteryLimit",
            name="Battery State of Charge Limit",
            icon="mdi:battery-charging-80",
            native_unit_of_measurement=PERCENTAGE,
        ),
        RivianSensorEntityDescription(
            key="battery_capacity",
            field="batteryCapacity",
            name="Battery Capacity",
            device_class=SensorDeviceClass.ENERGY_STORAGE,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:battery-check",
            suggested_display_precision=2,
        ),
        RivianSensorEntityDescription(
            key="bearing",
            field="gnssBearing",
            name="Bearing",
            icon="mdi:compass",
            native_unit_of_measurement=DEGREE,
            suggested_display_precision=0,
        ),
        RivianSensorEntityDescription(
            key="brake_fluid_low",
            translation_key="brake_fluid_low",
            field="brakeFluidLow",
            icon="mdi:car-brake-fluid-level",
            device_class=SensorDeviceClass.ENUM,
            options=["inactive", "active"],
        ),
        RivianSensorEntityDescription(
            key="driver_temperature",
            field="hvacTargetTemperature",
            name="Driver Temperature",
            device_class=SensorDeviceClass.TEMPERATURE,
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            suggested_display_precision=1,
        ),
        RivianSensorEntityDescription(
            key="cabin_temperature",
            field="cabinClimateInteriorTemperature",
            name="Cabin Temperature",
            device_class=SensorDeviceClass.TEMPERATURE,
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=1,
        ),
        RivianSensorEntityDescription(
            key="cabin_preconditioning_type",
            translation_key="cabin_preconditioning_type",
            field="cabinPreconditioningType",
            icon="mdi:thermostat",
            device_class=SensorDeviceClass.ENUM,
            options=[
                "none",
                "user_selected",
                "screen_protection",
                "scheduled",
                "auto_cabin_ventilation",
            ],
        ),
        RivianSensorEntityDescription(
            key="charger_derate_status",
            translation_key="charger_derate_status",
            field="chargerDerateStatus",
            icon="mdi:ev-station",
            device_class=SensorDeviceClass.ENUM,
            options=[
                "none",
                "warm_adapter",
                "dc_warm_plug",
                "ac_warm_plug",
                "evse_derating",
                "nearing_toc",
                "near_toc_lfp_batt_calibrating",
                "hvac_prioritized",
                "battery_heating",
                "battery_cooling",
                "cell_thermal_lim_cold_no_current",
                "cell_thermal_lim_hot_no_current",
                "cell_thermal_lim_cold",
                "cell_thermal_lim_hot",
                "pack_hardware_thermal_lim",
                "high_soc_sigma",
                "hv_battery_fault",
                "dcac_export",
            ],
        ),
        RivianSensorEntityDescription(
            key="distance_to_empty",
            field="distanceToEmpty",
            name="Estimated Vehicle Range",
            icon="mdi:map-marker-distance",
            device_class=SensorDeviceClass.DISTANCE,
            native_unit_of_measurement=UnitOfLength.KILOMETERS,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=1,
        ),
        RivianSensorEntityDescription(
            key="drive_mode",
            translation_key="drive_mode",
            field="driveMode",
            icon="mdi:car-speed-limiter",
            device_class=SensorDeviceClass.ENUM,
            options=[
                "init_mode",
                "everyday",
                "off_road_snow_ice",
                "off_road_sport_auto",
                "off_road_sport_drift",
                "sport_launch",
                "fault",
                "sport",
                "distance",
                "towing",
                "off_road_auto",
                "off_road_sand",
                "off_road_rocks",
                "off_road_mud",
                "winter",
            ],
        ),
        RivianSensorEntityDescription(
            key="gear_status",
            translation_key="gear_status",
            field="gearStatus",
            icon="mdi:car-shift-pattern",
            device_class=SensorDeviceClass.ENUM,
            options=[
                "not_defined",
                "park",
                "reverse",
                "neutral",
                "drive",
            ],
        ),
        RivianSensorEntityDescription(
            key="trailer_status",
            translation_key="trailer_status",
            field="trailerStatus",
            icon="mdi:truck-trailer",
            device_class=SensorDeviceClass.ENUM,
            options=[
                "trailer_not_present",
                "trailer_present",
                "trailer_present_with_brakes",
                "trailer_invalid",
            ],
        ),
        RivianSensorEntityDescription(
            key="gear_guard_video_mode",
            translation_key="gear_guard_video_mode",
            field="gearGuardVideoMode",
            icon="mdi:cctv",
            device_class=SensorDeviceClass.ENUM,
            options=[
                "none",
                "everywhere",
                "away_from_home",
            ],
        ),
        RivianSensorEntityDescription(
            key="gear_guard_video_status",
            translation_key="gear_guard_video_status",
            field="gearGuardVideoStatus",
            icon="mdi:cctv",
            device_class=SensorDeviceClass.ENUM,
            options=[
                "disabled",
                "enabled",
                "active",
                "faulted",
            ],
        ),
        RivianSensorEntityDescription(
            key="gear_guard_video_terms_accepted",
            translation_key="gear_guard_video_terms_accepted",
            field="gearGuardVideoTermsAccepted",
            icon="mdi:cctv",
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            device_class=SensorDeviceClass.ENUM,
            options=[
                "not_accepted",
                "accepted",
            ],
        ),
        RivianSensorEntityDescription(
            key="ota_available_version",
            field="otaAvailableVersion",
            name="Software OTA - Available Version",
            icon="mdi:package",
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
        ),
        RivianSensorEntityDescription(
            key="ota_available_version_git_hash",
            field="otaAvailableVersionGitHash",
            name="Software OTA - Available Version Git Hash",
            icon="mdi:source-commit",
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
        ),
        RivianSensorEntityDescription(
            key="ota_available_version_number",
            field="otaAvailableVersionNumber",
            name="Software OTA - Available Version Number",
            icon="mdi:numeric",
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
        ),
        RivianSensorEntityDescription(
            key="ota_available_version_week",
            field="otaAvailableVersionWeek",
            name="Software OTA - Available Version Week",
            icon="mdi:calendar-week",
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
        ),
        RivianSensorEntityDescription(
            key="ota_available_version_year",
            field="otaAvailableVersionYear",
            name="Software OTA - Available Version Year",
            icon="mdi:calendar",
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
        ),
        RivianSensorEntityDescription(
            key="ota_current_status",
            translation_key="ota_current_status",
            field="otaCurrentStatus",
            icon="mdi:package",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.ENUM,
            options=[
                "install_success",
                "install_failed",
                "install_unable_to_start",
            ],
        ),
        RivianSensorEntityDescription(
            key="ota_current_version",
            field="otaCurrentVersion",
            name="Software OTA - Current Version",
            icon="mdi:package",
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
        ),
        RivianSensorEntityDescription(
            key="ota_current_version_git_hash",
            field="otaCurrentVersionGitHash",
            name="Software OTA - Current Version Git Hash",
            icon="mdi:source-commit",
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
        ),
        RivianSensorEntityDescription(
            key="ota_current_version_number",
            field="otaCurrentVersionNumber",
            name="Software OTA - Current Version Number",
            icon="mdi:numeric",
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
        ),
        RivianSensorEntityDescription(
            key="ota_current_version_week",
            field="otaCurrentVersionWeek",
            name="Software OTA - Current Version Week",
            icon="mdi:calendar-week",
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
        ),
        RivianSensorEntityDescription(
            key="ota_current_version_year",
            field="otaCurrentVersionYear",
            name="Software OTA - Current Version Year",
            icon="mdi:calendar",
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
        ),
        RivianSensorEntityDescription(
            key="ota_download_progress",
            field="otaDownloadProgress",
            name="Software OTA - Download Progress",
            icon="mdi:progress-download",
            entity_category=EntityCategory.DIAGNOSTIC,
            native_unit_of_measurement=PERCENTAGE,
        ),
        RivianSensorEntityDescription(
            key="ota_install_duration",
            field="otaInstallDuration",
            name="Software OTA - Install Duration",
            icon="mdi:wrench-clock",
            device_class=SensorDeviceClass.DURATION,
            entity_category=EntityCategory.DIAGNOSTIC,
            native_unit_of_measurement=UnitOfTime.MINUTES,
            # 0 when no update is pending
            value_lambda=lambda v: v or None,
        ),
        RivianSensorEntityDescription(
            key="ota_install_progress",
            field="otaInstallProgress",
            name="Software OTA - Install Progress",
            icon="mdi:progress-clock",
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            native_unit_of_measurement=PERCENTAGE,
        ),
        RivianSensorEntityDescription(
            key="ota_install_ready",
            translation_key="ota_install_ready",
            field="otaInstallReady",
            icon="mdi:progress-check",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.ENUM,
            options=[
                "ota_available",
                "ota_not_available",
            ],
        ),
        RivianSensorEntityDescription(
            key="ota_install_time",
            field="otaScheduledInstallTime",
            name="Software OTA - Install Time",
            icon="mdi:clock",
            device_class=SensorDeviceClass.TIMESTAMP,
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
        RivianSensorEntityDescription(
            key="ota_status",
            translation_key="ota_status",
            field="otaStatus",
            icon="mdi:package",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.ENUM,
            options=[
                "idle",
                "ready_to_download",
                "fault",
                "connection_lost",
                "install_countdown",
                "preparing",
                "downloading",
                "ready_to_install",
                "scheduled_to_install",
                "awaiting_install",
                "installing",
                "install_success",
                "download_failed",
                "install_failed",
            ],
        ),
        RivianSensorEntityDescription(
            key="pet_mode_temperature_status",
            translation_key="pet_mode_temperature_status",
            field="petModeTemperatureStatus",
            icon="mdi:dog-side",
            device_class=SensorDeviceClass.ENUM,
            options=[
                "default",
                "cold",
                "hot",
                "faulty",
            ],
        ),
        RivianSensorEntityDescription(
            key="power_state",
            translation_key="power_state",
            field="powerState",
            icon="mdi:power",
            device_class=SensorDeviceClass.ENUM,
            options=[
                "sleep",
                "standby",
                "ready",
                "go",
                "vehicle_reset",
                "ota_update",
                "shutdown",
            ],
        ),
        RivianSensorEntityDescription(
            key="range_threshold",
            translation_key="range_threshold",
            field="rangeThreshold",
            icon="mdi:map-marker-distance",
            device_class=SensorDeviceClass.ENUM,
            options=[
                "normal",
                "low",
                "red",
                "critically_low",
            ],
        ),
        RivianSensorEntityDescription(
            key="remote_charging_available",
            translation_key="remote_charging_available",
            field="remoteChargingAvailable",
            icon="mdi:battery-charging-wireless-80",
            device_class=SensorDeviceClass.ENUM,
            options=[
                "true",
                "false",
            ],
        ),
        RivianSensorEntityDescription(
            key="service_mode",
            translation_key="service_mode",
            field="serviceMode",
            icon="mdi:account-wrench",
            device_class=SensorDeviceClass.ENUM,
            options=[
                "off",
                "on",
            ],
        ),
        RivianSensorEntityDescription(
            key="speed",
            field="gnssSpeed",
            name="Speed",
            device_class=SensorDeviceClass.SPEED,
            native_unit_of_measurement=UnitOfSpeed.METERS_PER_SECOND,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=0,
        ),
        RivianSensorEntityDescription(
            key="time_to_end_of_charge",
            field="timeToEndOfCharge",
            name="Charging Time Remaining",
            device_class=SensorDeviceClass.DURATION,
            native_unit_of_measurement=UnitOfTime.MINUTES,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        RivianSensorEntityDescription(
            key="tire_pressure_front_left",
            field="tirePressureFrontLeft",
            name="Tire Pressure Front Left",
            icon="mdi:tire",
            device_class=SensorDeviceClass.PRESSURE,
            native_unit_of_measurement=UnitOfPressure.BAR,
            state_class=SensorStateClass.MEASUREMENT,
            restore=True,
        ),
        RivianSensorEntityDescription(
            key="tire_pressure_front_right",
            field="tirePressureFrontRight",
            name="Tire Pressure Front Right",
            icon="mdi:tire",
            device_class=SensorDeviceClass.PRESSURE,
            native_unit_of_measurement=UnitOfPressure.BAR,
            state_class=SensorStateClass.MEASUREMENT,
            restore=True,
        ),
        RivianSensorEntityDescription(
            key="tire_pressure_rear_left",
            field="tirePressureRearLeft",
            name="Tire Pressure Rear Left",
            icon="mdi:tire",
            device_class=SensorDeviceClass.PRESSURE,
            native_unit_of_measurement=UnitOfPressure.BAR,
            state_class=SensorStateClass.MEASUREMENT,
            restore=True,
        ),
        RivianSensorEntityDescription(
            key="tire_pressure_rear_right",
            field="tirePressureRearRight",
            name="Tire Pressure Rear Right",
            icon="mdi:tire",
            device_class=SensorDeviceClass.PRESSURE,
            native_unit_of_measurement=UnitOfPressure.BAR,
            state_class=SensorStateClass.MEASUREMENT,
            restore=True,
        ),
        RivianSensorEntityDescription(
            key="tire_pressure_status_front_left",
            translation_key="tire_pressure_status_front_left",
            field="tirePressureStatusFrontLeft",
            icon="mdi:tire",
            restore=True,
            device_class=SensorDeviceClass.ENUM,
            options=TIRE_PRESSURE_STATUS_OPTIONS,
        ),
        RivianSensorEntityDescription(
            key="tire_pressure_status_front_right",
            translation_key="tire_pressure_status_front_right",
            field="tirePressureStatusFrontRight",
            icon="mdi:tire",
            restore=True,
            device_class=SensorDeviceClass.ENUM,
            options=TIRE_PRESSURE_STATUS_OPTIONS,
        ),
        RivianSensorEntityDescription(
            key="tire_pressure_status_rear_left",
            translation_key="tire_pressure_status_rear_left",
            field="tirePressureStatusRearLeft",
            icon="mdi:tire",
            restore=True,
            device_class=SensorDeviceClass.ENUM,
            options=TIRE_PRESSURE_STATUS_OPTIONS,
        ),
        RivianSensorEntityDescription(
            key="tire_pressure_status_rear_right",
            translation_key="tire_pressure_status_rear_right",
            field="tirePressureStatusRearRight",
            icon="mdi:tire",
            restore=True,
            device_class=SensorDeviceClass.ENUM,
            options=TIRE_PRESSURE_STATUS_OPTIONS,
        ),
        RivianSensorEntityDescription(
            key="vehicle_mileage",
            field="vehicleMileage",
            name="Odometer",
            icon="mdi:counter",
            device_class=SensorDeviceClass.DISTANCE,
            native_unit_of_measurement=UnitOfLength.METERS,
            state_class=SensorStateClass.TOTAL_INCREASING,
            suggested_display_precision=1,
            suggested_unit_of_measurement=UnitOfLength.MILES,
        ),
        RivianSensorEntityDescription(
            key="window_front_left_calibrated",
            translation_key="window_front_left_calibrated",
            field="windowFrontLeftCalibrated",
            icon="mdi:window-closed",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.ENUM,
            options=WINDOW_CALIBRATION_OPTIONS,
        ),
        RivianSensorEntityDescription(
            key="window_front_right_calibrated",
            translation_key="window_front_right_calibrated",
            field="windowFrontRightCalibrated",
            icon="mdi:window-closed",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.ENUM,
            options=WINDOW_CALIBRATION_OPTIONS,
        ),
        RivianSensorEntityDescription(
            key="window_rear_left_calibrated",
            translation_key="window_rear_left_calibrated",
            field="windowRearLeftCalibrated",
            icon="mdi:window-closed",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.ENUM,
            options=WINDOW_CALIBRATION_OPTIONS,
        ),
        RivianSensorEntityDescription(
            key="window_rear_right_calibrated",
            translation_key="window_rear_right_calibrated",
            field="windowRearRightCalibrated",
            icon="mdi:window-closed",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.ENUM,
            options=WINDOW_CALIBRATION_OPTIONS,
        ),
        RivianSensorEntityDescription(
            key="windows_next_action",
            translation_key="windows_next_action",
            field="windowsNextAction",
            icon="mdi:window-closed",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.ENUM,
            options=[
                "sna",
                "open_allowed",
                "close_allowed",
                "opening",
                "closing",
                "open_not_available",
                "close_not_available",
                "open_not_allowed_faulted",
                "close_not_allowed_faulted",
                "moving",
                "obstructed_while_closing_close_allowed",
                "close_not_allowed_uncalibrated",
                "open_not_allowed_uncalibrated",
            ],
        ),
        RivianSensorEntityDescription(
            key="twelve_volt_battery_health",
            translation_key="twelve_volt_battery_health",
            field="twelveVoltBatteryHealth",
            icon="mdi:car-battery",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.ENUM,
            options=[
                "normal",
                "low",
            ],
        ),
        RivianSensorEntityDescription(
            key="limited_acceleration_cold",
            field="limitedAccelCold",
            name="Limited Acceleration (Cold)",
            icon="mdi:snowflake-thermometer",
            entity_category=EntityCategory.DIAGNOSTIC,
            # Parallax decodes these as bools; keep GraphQL's 0/1
            value_lambda=int,
        ),
        RivianSensorEntityDescription(
            key="limited_regen_braking_cold",
            field="limitedRegenCold",
            name="Limited Regenerative Braking (Cold)",
            icon="mdi:snowflake-thermometer",
            entity_category=EntityCategory.DIAGNOSTIC,
            # Parallax decodes these as bools; keep GraphQL's 0/1
            value_lambda=int,
        ),
        RivianSensorEntityDescription(
            key="bluetooth_front_fascia_hardware_failure_status",
            translation_key="bluetooth_front_fascia_hardware_failure_status",
            field="btmFfHardwareFailureStatus",
            icon="mdi:bluetooth",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.ENUM,
            options=BTM_FAILURE_STATUS_OPTIONS,
        ),
        RivianSensorEntityDescription(
            key="bluetooth_rear_fascia_hardware_failure_status",
            translation_key="bluetooth_rear_fascia_hardware_failure_status",
            field="btmRfHardwareFailureStatus",
            icon="mdi:bluetooth",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.ENUM,
            options=BTM_FAILURE_STATUS_OPTIONS,
        ),
        RivianSensorEntityDescription(
            key="bluetooth_instrument_controls_hardware_failure_status",
            translation_key="bluetooth_instrument_controls_hardware_failure_status",
            field="btmIcHardwareFailureStatus",
            icon="mdi:bluetooth",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.ENUM,
            options=BTM_FAILURE_STATUS_OPTIONS,
        ),
        RivianSensorEntityDescription(
            key="bluetooth_right_front_door_hardware_failure_status",
            translation_key="bluetooth_right_front_door_hardware_failure_status",
            field="btmRfdHardwareFailureStatus",
            icon="mdi:bluetooth",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.ENUM,
            options=BTM_FAILURE_STATUS_OPTIONS,
        ),
        RivianSensorEntityDescription(
            key="bluetooth_left_front_door_hardware_failure_status",
            translation_key="bluetooth_left_front_door_hardware_failure_status",
            field="btmLfdHardwareFailureStatus",
            icon="mdi:bluetooth",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.ENUM,
            options=BTM_FAILURE_STATUS_OPTIONS,
        ),
    ),
    # R2s don't get GraphQL vehicle state updates for these
    ("R1",): (
        RivianSensorEntityDescription(
            key="ota_install_type",
            field="otaInstallType",
            name="Software OTA - Install Type",
            icon="mdi:package",
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
    ),
    ("R1S", "R2"): (
        RivianSensorEntityDescription(
            key="liftgate_next_action",
            translation_key="liftgate_next_action",
            field="closureLiftgateNextAction",
            icon="mdi:gesture-tap-button",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.ENUM,
            options=[
                "sna",
                "open_allowed",
                "close_allowed",
                "opening",
                "closing",
                "open_not_available",
                "close_not_available",
                "open_not_allowed_faulted",
                "close_not_allowed_faulted",
                "open_allowed_no_power_operation",
                "close_not_allowed_no_power_operation",
                "obstructed_opening_close_allowed",
                "obstructed_closing_close_allowed",
                "lower_gate_open_close_not_allowed",
                "opening_pause_not_allowed",
                "closing_pause_not_allowed",
                "open_allowed_obstacle_detected",
                "open_allowed_trailer_detected",
                "close_allowed_obstacle_detected",
                "close_allowed_trailer_detected",
                "processing",
            ],
        ),
    ),
}
BINARY_SENSORS: Final[
    dict[tuple[str, ...], tuple[RivianBinarySensorEntityDescription, ...]]
] = {
    ("R1", "R2"): (
        RivianBinarySensorEntityDescription(
            key="alarm_sound_status",
            field="alarmSoundStatus",
            name="Gear Guard Alarm",
            device_class=BinarySensorDeviceClass.TAMPER,
            on_value="true",
        ),
        RivianBinarySensorEntityDescription(
            key="cabin_preconditioning_status",
            field="cabinPreconditioningStatus",
            name="Cabin Climate Preconditioning",
            device_class=BinarySensorDeviceClass.RUNNING,
            on_value=["active", "complete_maintain", "initiate"],
        ),
        RivianBinarySensorEntityDescription(
            key="charger_state",
            field="chargerState",
            name="Charging Status",
            device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
            on_value=["charging_active", "charging_connecting"],
        ),
        RivianBinarySensorEntityDescription(
            key="charger_status",
            # chargerStatus is only sent briefly on plug-in; connectionState is
            # in every charging.session.status ("error" and "v2l_connected"
            # count as plugged in)
            field="connectionState",
            name="Charger Connection",
            device_class=BinarySensorDeviceClass.PLUG,
            on_value="disconnected",
            negate=True,
        ),
        RivianBinarySensorEntityDescription(
            key="closure_frunk_closed",
            field="closureFrunkClosed",
            name="Front Trunk",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        ),
        RivianBinarySensorEntityDescription(
            key="closure_frunk_locked",
            field="closureFrunkLocked",
            name="Front Trunk Lock",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        ),
        RivianBinarySensorEntityDescription(
            key="defrost_defog_status",
            field="defrostDefogStatus",
            name="Defrost/Defog",
            icon="mdi:car-defrost-front",
            device_class=BinarySensorDeviceClass.RUNNING,
            on_value="off",
            negate=True,
        ),
        RivianBinarySensorEntityDescription(
            key="door_front_left_closed",
            field="doorFrontLeftClosed",
            name="Door Front Left",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        ),
        RivianBinarySensorEntityDescription(
            key="door_front_left_locked",
            field="doorFrontLeftLocked",
            name="Door Front Left Lock",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        ),
        RivianBinarySensorEntityDescription(
            key="door_front_right_closed",
            field="doorFrontRightClosed",
            name="Door Front Right",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        ),
        RivianBinarySensorEntityDescription(
            key="door_front_right_locked",
            field="doorFrontRightLocked",
            name="Door Front Right Lock",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        ),
        RivianBinarySensorEntityDescription(
            key="door_rear_left_closed",
            field="doorRearLeftClosed",
            name="Door Rear Left",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        ),
        RivianBinarySensorEntityDescription(
            key="door_rear_left_locked",
            field="doorRearLeftLocked",
            name="Door Rear Left Lock",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        ),
        RivianBinarySensorEntityDescription(
            key="door_rear_right_closed",
            field="doorRearRightClosed",
            name="Door Rear Right",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        ),
        RivianBinarySensorEntityDescription(
            key="door_rear_right_locked",
            field="doorRearRightLocked",
            name="Door Rear Right Lock",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        ),
        RivianBinarySensorEntityDescription(
            key="pet_mode_status",
            field="petModeStatus",
            name="Pet Mode",
            device_class=BinarySensorDeviceClass.RUNNING,
            on_value="on",
        ),
        RivianBinarySensorEntityDescription(
            key="seat_front_left_heat",
            field="seatFrontLeftHeat",
            name="Heated Seat Front Left",
            icon="mdi:car-seat-heater",
            device_class=BinarySensorDeviceClass.RUNNING,
            on_value=["level_1", "level_2", "level_3"],
        ),
        RivianBinarySensorEntityDescription(
            key="seat_front_left_vent",
            field="seatFrontLeftVent",
            name="Vented Seat Front Left",
            icon="mdi:car-seat-cooler",
            device_class=BinarySensorDeviceClass.RUNNING,
            on_value=["level_1", "level_2", "level_3"],
        ),
        RivianBinarySensorEntityDescription(
            key="seat_front_right_heat",
            field="seatFrontRightHeat",
            name="Heated Seat Front Right",
            icon="mdi:car-seat-heater",
            device_class=BinarySensorDeviceClass.RUNNING,
            on_value=["level_1", "level_2", "level_3"],
        ),
        RivianBinarySensorEntityDescription(
            key="seat_front_right_vent",
            field="seatFrontRightVent",
            name="Vented Seat Front Right",
            icon="mdi:car-seat-cooler",
            device_class=BinarySensorDeviceClass.RUNNING,
            on_value=["level_1", "level_2", "level_3"],
        ),
        RivianBinarySensorEntityDescription(
            key="seat_rear_left_heat",
            field="seatRearLeftHeat",
            name="Heated Seat Rear Left",
            icon="mdi:car-seat-heater",
            device_class=BinarySensorDeviceClass.RUNNING,
            on_value=["level_1", "level_2", "level_3"],
        ),
        RivianBinarySensorEntityDescription(
            key="seat_rear_right_heat",
            field="seatRearRightHeat",
            name="Heated Seat Rear Right",
            icon="mdi:car-seat-heater",
            device_class=BinarySensorDeviceClass.RUNNING,
            on_value=["level_1", "level_2", "level_3"],
        ),
        RivianBinarySensorEntityDescription(
            key="steering_wheel_heat",
            field="steeringWheelHeat",
            name="Heated Steering Wheel",
            icon="mdi:steering",  # mdi:steering-heater, https://github.com/Templarian/MaterialDesign/issues/6925
            device_class=BinarySensorDeviceClass.RUNNING,
            on_value="level_1",
        ),
        RivianBinarySensorEntityDescription(
            key="tire_pressure_status_valid_front_left",
            field="tirePressureStatusValidFrontLeft",
            name="Tire Pressure Front Left Validity",
            device_class=BinarySensorDeviceClass.PROBLEM,
            on_value="invalid",
        ),
        RivianBinarySensorEntityDescription(
            key="tire_pressure_status_valid_front_right",
            field="tirePressureStatusValidFrontRight",
            name="Tire Pressure Front Right Validity",
            device_class=BinarySensorDeviceClass.PROBLEM,
            on_value="invalid",
        ),
        RivianBinarySensorEntityDescription(
            key="tire_pressure_status_valid_rear_left",
            field="tirePressureStatusValidRearLeft",
            name="Tire Pressure Rear Left Validity",
            device_class=BinarySensorDeviceClass.PROBLEM,
            on_value="invalid",
        ),
        RivianBinarySensorEntityDescription(
            key="tire_pressure_status_valid_rear_right",
            field="tirePressureStatusValidRearRight",
            name="Tire Pressure Rear Right Validity",
            device_class=BinarySensorDeviceClass.PROBLEM,
            on_value="invalid",
        ),
        RivianBinarySensorEntityDescription(
            key="window_front_left_closed",
            field="windowFrontLeftClosed",
            name="Window Front Left",
            device_class=BinarySensorDeviceClass.WINDOW,
            on_value="open",
        ),
        RivianBinarySensorEntityDescription(
            key="window_front_right_closed",
            field="windowFrontRightClosed",
            name="Window Front Right",
            device_class=BinarySensorDeviceClass.WINDOW,
            on_value="open",
        ),
        RivianBinarySensorEntityDescription(
            key="window_rear_left_closed",
            field="windowRearLeftClosed",
            name="Window Rear Left",
            device_class=BinarySensorDeviceClass.WINDOW,
            on_value="open",
        ),
        RivianBinarySensorEntityDescription(
            key="window_rear_right_closed",
            field="windowRearRightClosed",
            name="Window Rear Right",
            device_class=BinarySensorDeviceClass.WINDOW,
            on_value="open",
        ),
        RivianBinarySensorEntityDescription(
            key="wiper_fluid_state",
            field="wiperFluidState",
            name="Wiper Fluid Level",
            icon="mdi:wiper-wash",
            device_class=BinarySensorDeviceClass.PROBLEM,
            on_value="normal",
            negate=True,
        ),
        RivianBinarySensorEntityDescription(
            key="locked_state",
            field=LOCK_STATE_ENTITIES,
            name="Locked State",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        ),
        RivianBinarySensorEntityDescription(
            key="door_state",
            field=DOOR_STATE_ENTITIES,
            name="Door State",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        ),
        RivianBinarySensorEntityDescription(
            key="closure_state",
            field=CLOSURE_STATE_ENTITIES,
            name="Closure State",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        ),
        RivianBinarySensorEntityDescription(
            key="use_state",
            field="powerState",
            name="In Use State",
            device_class=BinarySensorDeviceClass.MOVING,
            on_value="go",
        ),
        RivianBinarySensorEntityDescription(
            key="car_wash_mode",
            field="carWashMode",
            name="Car Wash Mode",
            icon="mdi:car-wash",
            on_value="on",
        ),
    ),
    # The R2 has a manual charge port door and no tailgate
    ("R1",): (
        RivianBinarySensorEntityDescription(
            key="charge_port",
            field="chargePortState",
            name="Charge Port",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        ),
        RivianBinarySensorEntityDescription(
            key="closure_tailgate_locked",
            field="closureTailgateLocked",
            name="Tailgate Lock",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        ),
        RivianBinarySensorEntityDescription(
            key="gear_guard_locked",
            field="gearGuardLocked",
            name="Gear Guard",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        ),
    ),
    # The R1S doesn't report tailgate open/closed
    ("R1T",): (
        RivianBinarySensorEntityDescription(
            key="closure_tailgate_closed",
            field="closureTailgateClosed",
            name="Tailgate",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        ),
        RivianBinarySensorEntityDescription(
            key="closure_side_bin_left_closed",
            field="closureSideBinLeftClosed",
            name="Gear Tunnel Left",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        ),
        RivianBinarySensorEntityDescription(
            key="closure_side_bin_left_locked",
            field="closureSideBinLeftLocked",
            name="Gear Tunnel Left Lock",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        ),
        RivianBinarySensorEntityDescription(
            key="closure_side_bin_right_closed",
            field="closureSideBinRightClosed",
            name="Gear Tunnel Right",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        ),
        RivianBinarySensorEntityDescription(
            key="closure_side_bin_right_locked",
            field="closureSideBinRightLocked",
            name="Gear Tunnel Right Lock",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        ),
        RivianBinarySensorEntityDescription(
            key="closure_tonneau_closed",
            field="closureTonneauClosed",
            name="Tonneau",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
            supported_feature="TONNEAU_CMD",
        ),
        RivianBinarySensorEntityDescription(
            key="closure_tonneau_locked",
            field="closureTonneauLocked",
            name="Tonneau Lock",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
            supported_feature="TONNEAU_CMD",
        ),
    ),
    ("R1S", "R2"): (
        RivianBinarySensorEntityDescription(
            key="closure_liftgate_closed",
            field="closureLiftgateClosed",
            name="Liftgate",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        ),
        RivianBinarySensorEntityDescription(
            key="closure_liftgate_locked",
            field="closureLiftgateLocked",
            name="Liftgate Lock",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        ),
    ),
    ("R1S",): (
        RivianBinarySensorEntityDescription(
            key="seat_third_row_left_heat",
            field="seatThirdRowLeftHeat",
            name="Heated Seat 3rd Row Left",
            icon="mdi:car-seat-heater",
            device_class=BinarySensorDeviceClass.RUNNING,
            on_value=["level_1", "level_2", "level_3"],
        ),
        RivianBinarySensorEntityDescription(
            key="seat_third_row_right_heat",
            field="seatThirdRowRightHeat",
            name="Heated Seat 3rd Row Right",
            icon="mdi:car-seat-heater",
            device_class=BinarySensorDeviceClass.RUNNING,
            on_value=["level_1", "level_2", "level_3"],
        ),
    ),
}

BTM_FAILURE_STATUS_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "btmFfHardwareFailureStatus",
        "btmIcHardwareFailureStatus",
        "btmLfdHardwareFailureStatus",
        "btmRfHardwareFailureStatus",
        "btmRfdHardwareFailureStatus",
    }
)

WINDOW_CALIBRATION_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "windowFrontLeftCalibrated",
        "windowFrontRightCalibrated",
        "windowRearLeftCalibrated",
        "windowRearRightCalibrated",
    }
)

# Vehicle state values assumed until the vehicle reports otherwise
DEFAULT_VEHICLE_STATE: Final[dict[str, Any]] = dict.fromkeys(
    WINDOW_CALIBRATION_FIELDS, "calibrated"
)

# ota.deployment.state only has an available version while an update is in
# flight; GraphQL's values for no available update
OTA_AVAILABLE_VERSION_IDLE: Final[dict[str, Any]] = {
    "otaAvailableVersion": "0.0.0",
    "otaAvailableVersionGitHash": "",
    "otaAvailableVersionNumber": 0,
    "otaAvailableVersionWeek": 0,
    "otaAvailableVersionYear": 0,
}

# Values for Parallax fields that decode to None (unsent) when in their zero
# state, as GraphQL reports it
PARALLAX_NONE_VALUES: Final[dict[str, str]] = {
    **dict.fromkeys(BTM_FAILURE_STATUS_FIELDS, "dtc_not_set"),
    **DEFAULT_VEHICLE_STATE,
    "alarmSoundStatus": "false",
    "cabinPreconditioningType": "none",
    "gearGuardVideoMode": "none",
}

# Parallax fields whose "undefined" value is a real state (not running) rather
# than an invalid reading to skip
PARALLAX_UNDEFINED_IS_VALID: Final[frozenset[str]] = frozenset(
    {"cabinPreconditioningStatus"}
)

# Parallax fields whose None value means "not set", so it clears the field
# rather than being skipped as unsent
PARALLAX_NONE_CLEARS: Final[frozenset[str]] = frozenset({"otaScheduledInstallTime"})

# Parallax topics that send one message per device/schedule, which can't be
# stored as flat vehicle state (and no entity uses)
PARALLAX_IGNORED_RVMS: Final[frozenset[str]] = frozenset(
    {"device_table.vas_keyper.devices", "ota.user_schedule.ota_config"}
)

# Values for fields an RVM topic leaves out when they don't apply, so stale
# values are cleared
PARALLAX_RVM_DEFAULTS: Final[dict[str, dict[str, Any]]] = {
    "ota.deployment.state": OTA_AVAILABLE_VERSION_IDLE,
}

# Vehicle state fields Parallax doesn't provide, so they're requested from the
# GraphQL vehicle state subscription. Every other field comes from Parallax,
# which sends a snapshot of every topic on subscribe.
VEHICLE_STATE_API_FIELDS: Final[set[str]] = {
    "activeDriverName",
    "otaInstallType",
}

CHARGING_STATE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "activeChargingTime",
        "currentCurrency",
        "currentPrice",
        "kilometersChargedPerHour",
        "power",
        "rangeAddedThisSession",
        "startTime",
        "timeElapsed",
        "timeToEndOfCharge",
        "totalChargedEnergy",
    }
)

WEEK_DAYS_ORDERED: Final[tuple[str, ...]] = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

MINUTES_PER_DAY: Final[int] = 1440
MINUTES_PER_HOUR: Final[int] = 60

CHARGING_SCHEDULE_AMPERAGE_MINIMUM: Final[int] = 8
CHARGING_SCHEDULE_AMPERAGE_MAXIMUM: Final[int] = 48
CHARGING_SCHEDULE_AMPERAGE_STEP: Final[int] = 2

DEFAULT_CHARGING_SCHEDULE_START: Final[int] = 1320  # 10:00 PM
DEFAULT_CHARGING_SCHEDULE_DURATION: Final[int] = 480  # 8 hours
DEFAULT_CHARGING_SCHEDULE_AMPERAGE: Final[int] = 48
DEFAULT_CHARGING_SCHEDULE: Final[dict[str, Any]] = {
    "startTime": DEFAULT_CHARGING_SCHEDULE_START,
    "duration": DEFAULT_CHARGING_SCHEDULE_DURATION,
    "amperage": DEFAULT_CHARGING_SCHEDULE_AMPERAGE,
    "enabled": True,
    "weekDays": list(WEEK_DAYS_ORDERED),
}
