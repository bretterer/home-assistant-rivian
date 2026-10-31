"""Rivian (Unofficial)"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Final

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    DEGREE,
    PERCENTAGE,
    EntityCategory,
    UnitOfEnergy,
    UnitOfEnergyDistance,
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
ISSUE_STATE_VALUES_CHANGED = "state_values_changed"
ISSUE_RATE_LIMITED = "rate_limited"

# Bumped whenever the generated efficiency dashboard's config shape changes,
# so a stale, previously-generated dashboard can be detected and regenerated.
DASHBOARD_SCHEMA_VERSION: Final[int] = 13

# Fired with {"vin": ...} when a VIN's stored analytics change (drive completed,
# backfill finished); the WebSocket subscribe command relays it to the frontend.
RIVIAN_ANALYTICS_UPDATED_EVENT: Final[str] = "rivian_analytics_updated"

# Attributes
ATTR_API = "api"
ATTR_COORDINATOR = "coordinator"
ATTR_ANALYTICS_DB = (
    "_analytics_db"  # the shared AnalyticsDatabase, under hass.data[DOMAIN]
)
ATTR_DRIVE_STORE = "drive_store"
ATTR_DRIVE_TRACKER = "drive_tracker"
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

# Charger states in which the vehicle is charging (or about to)
ACTIVE_CHARGING_STATES: Final[tuple[str, ...]] = (
    "charging_active",
    "charging_connecting",
    "smart_charging_active",
)


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
            translation_key="altitude",
            device_class=SensorDeviceClass.DISTANCE,
            native_unit_of_measurement=UnitOfLength.METERS,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=0,
        ),
        RivianSensorEntityDescription(
            key="battery_thermal_status",
            translation_key="battery_thermal_status",
            entity_category=EntityCategory.DIAGNOSTIC,
            field="batteryHvThermalEvent",
            device_class=SensorDeviceClass.ENUM,
            options=[
                "nominal",
                "detected",
            ],
        ),
        RivianSensorEntityDescription(
            key="battery_thermal_runaway_propagation",
            translation_key="battery_thermal_runaway_propagation",
            entity_category=EntityCategory.DIAGNOSTIC,
            field="batteryHvThermalEventPropagation",
            device_class=SensorDeviceClass.ENUM,
            options=[
                "nominal",
                "detected",
            ],
        ),
        RivianSensorEntityDescription(
            key="battery_level",
            field="batteryLevel",
            translation_key="battery_level",
            device_class=SensorDeviceClass.BATTERY,
            native_unit_of_measurement=PERCENTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=1,
        ),
        RivianSensorEntityDescription(
            key="battery_limit",
            field="batteryLimit",
            translation_key="battery_limit",
            native_unit_of_measurement=PERCENTAGE,
        ),
        RivianSensorEntityDescription(
            key="battery_capacity",
            field="batteryCapacity",
            translation_key="battery_capacity",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.ENERGY_STORAGE,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=2,
        ),
        RivianSensorEntityDescription(
            key="bearing",
            field="gnssBearing",
            translation_key="bearing",
            native_unit_of_measurement=DEGREE,
            suggested_display_precision=0,
        ),
        RivianSensorEntityDescription(
            key="brake_fluid_low",
            translation_key="brake_fluid_low",
            entity_category=EntityCategory.DIAGNOSTIC,
            field="brakeFluidLow",
            device_class=SensorDeviceClass.ENUM,
            options=["inactive", "active"],
        ),
        RivianSensorEntityDescription(
            key="driver_temperature",
            field="hvacTargetTemperature",
            translation_key="driver_temperature",
            device_class=SensorDeviceClass.TEMPERATURE,
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            suggested_display_precision=1,
        ),
        RivianSensorEntityDescription(
            key="cabin_temperature",
            field="cabinClimateInteriorTemperature",
            translation_key="cabin_temperature",
            device_class=SensorDeviceClass.TEMPERATURE,
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=1,
        ),
        RivianSensorEntityDescription(
            key="cabin_preconditioning_type",
            translation_key="cabin_preconditioning_type",
            entity_category=EntityCategory.DIAGNOSTIC,
            field="cabinPreconditioningType",
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
            entity_category=EntityCategory.DIAGNOSTIC,
            field="chargerDerateStatus",
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
            translation_key="distance_to_empty",
            device_class=SensorDeviceClass.DISTANCE,
            native_unit_of_measurement=UnitOfLength.KILOMETERS,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=1,
        ),
        RivianSensorEntityDescription(
            key="drive_mode",
            translation_key="drive_mode",
            field="driveMode",
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
            entity_category=EntityCategory.DIAGNOSTIC,
            field="gearGuardVideoMode",
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
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            device_class=SensorDeviceClass.ENUM,
            options=[
                "not_accepted",
                "accepted",
            ],
        ),
        RivianSensorEntityDescription(
            key="ota_current_status",
            translation_key="ota_current_status",
            field="otaCurrentStatus",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.ENUM,
            options=[
                "install_success",
                "install_failed",
                "install_unable_to_start",
            ],
        ),
        RivianSensorEntityDescription(
            key="ota_download_progress",
            field="otaDownloadProgress",
            translation_key="ota_download_progress",
            entity_category=EntityCategory.DIAGNOSTIC,
            native_unit_of_measurement=PERCENTAGE,
        ),
        RivianSensorEntityDescription(
            key="ota_install_duration",
            field="otaInstallDuration",
            translation_key="ota_install_duration",
            device_class=SensorDeviceClass.DURATION,
            entity_category=EntityCategory.DIAGNOSTIC,
            native_unit_of_measurement=UnitOfTime.MINUTES,
            # 0 when no update is pending
            value_lambda=lambda v: v or None,
        ),
        RivianSensorEntityDescription(
            key="ota_install_ready",
            translation_key="ota_install_ready",
            field="otaInstallReady",
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
            translation_key="ota_install_time",
            device_class=SensorDeviceClass.TIMESTAMP,
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
        RivianSensorEntityDescription(
            key="ota_status",
            translation_key="ota_status",
            field="otaStatus",
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
            entity_category=EntityCategory.DIAGNOSTIC,
            field="rangeThreshold",
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
            entity_category=EntityCategory.DIAGNOSTIC,
            field="remoteChargingAvailable",
            device_class=SensorDeviceClass.ENUM,
            options=[
                "true",
                "false",
            ],
        ),
        RivianSensorEntityDescription(
            key="service_mode",
            translation_key="service_mode",
            entity_category=EntityCategory.DIAGNOSTIC,
            field="serviceMode",
            device_class=SensorDeviceClass.ENUM,
            options=[
                "off",
                "on",
            ],
        ),
        RivianSensorEntityDescription(
            key="speed",
            field="gnssSpeed",
            translation_key="speed",
            device_class=SensorDeviceClass.SPEED,
            native_unit_of_measurement=UnitOfSpeed.METERS_PER_SECOND,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=0,
        ),
        RivianSensorEntityDescription(
            key="time_to_end_of_charge",
            field="timeToEndOfCharge",
            translation_key="time_to_end_of_charge",
            device_class=SensorDeviceClass.DURATION,
            native_unit_of_measurement=UnitOfTime.MINUTES,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        RivianSensorEntityDescription(
            key="tire_pressure_front_left",
            field="tirePressureFrontLeft",
            translation_key="tire_pressure_front_left",
            device_class=SensorDeviceClass.PRESSURE,
            native_unit_of_measurement=UnitOfPressure.BAR,
            state_class=SensorStateClass.MEASUREMENT,
            restore=True,
        ),
        RivianSensorEntityDescription(
            key="tire_pressure_front_right",
            field="tirePressureFrontRight",
            translation_key="tire_pressure_front_right",
            device_class=SensorDeviceClass.PRESSURE,
            native_unit_of_measurement=UnitOfPressure.BAR,
            state_class=SensorStateClass.MEASUREMENT,
            restore=True,
        ),
        RivianSensorEntityDescription(
            key="tire_pressure_rear_left",
            field="tirePressureRearLeft",
            translation_key="tire_pressure_rear_left",
            device_class=SensorDeviceClass.PRESSURE,
            native_unit_of_measurement=UnitOfPressure.BAR,
            state_class=SensorStateClass.MEASUREMENT,
            restore=True,
        ),
        RivianSensorEntityDescription(
            key="tire_pressure_rear_right",
            field="tirePressureRearRight",
            translation_key="tire_pressure_rear_right",
            device_class=SensorDeviceClass.PRESSURE,
            native_unit_of_measurement=UnitOfPressure.BAR,
            state_class=SensorStateClass.MEASUREMENT,
            restore=True,
        ),
        RivianSensorEntityDescription(
            key="tire_pressure_status_front_left",
            translation_key="tire_pressure_status_front_left",
            entity_category=EntityCategory.DIAGNOSTIC,
            field="tirePressureStatusFrontLeft",
            restore=True,
            device_class=SensorDeviceClass.ENUM,
            options=TIRE_PRESSURE_STATUS_OPTIONS,
        ),
        RivianSensorEntityDescription(
            key="tire_pressure_status_front_right",
            translation_key="tire_pressure_status_front_right",
            entity_category=EntityCategory.DIAGNOSTIC,
            field="tirePressureStatusFrontRight",
            restore=True,
            device_class=SensorDeviceClass.ENUM,
            options=TIRE_PRESSURE_STATUS_OPTIONS,
        ),
        RivianSensorEntityDescription(
            key="tire_pressure_status_rear_left",
            translation_key="tire_pressure_status_rear_left",
            entity_category=EntityCategory.DIAGNOSTIC,
            field="tirePressureStatusRearLeft",
            restore=True,
            device_class=SensorDeviceClass.ENUM,
            options=TIRE_PRESSURE_STATUS_OPTIONS,
        ),
        RivianSensorEntityDescription(
            key="tire_pressure_status_rear_right",
            translation_key="tire_pressure_status_rear_right",
            entity_category=EntityCategory.DIAGNOSTIC,
            field="tirePressureStatusRearRight",
            restore=True,
            device_class=SensorDeviceClass.ENUM,
            options=TIRE_PRESSURE_STATUS_OPTIONS,
        ),
        RivianSensorEntityDescription(
            key="vehicle_mileage",
            field="vehicleMileage",
            translation_key="vehicle_mileage",
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
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.ENUM,
            options=WINDOW_CALIBRATION_OPTIONS,
        ),
        RivianSensorEntityDescription(
            key="window_front_right_calibrated",
            translation_key="window_front_right_calibrated",
            field="windowFrontRightCalibrated",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.ENUM,
            options=WINDOW_CALIBRATION_OPTIONS,
        ),
        RivianSensorEntityDescription(
            key="window_rear_left_calibrated",
            translation_key="window_rear_left_calibrated",
            field="windowRearLeftCalibrated",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.ENUM,
            options=WINDOW_CALIBRATION_OPTIONS,
        ),
        RivianSensorEntityDescription(
            key="window_rear_right_calibrated",
            translation_key="window_rear_right_calibrated",
            field="windowRearRightCalibrated",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.ENUM,
            options=WINDOW_CALIBRATION_OPTIONS,
        ),
        RivianSensorEntityDescription(
            key="windows_next_action",
            translation_key="windows_next_action",
            field="windowsNextAction",
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
            translation_key="limited_acceleration_cold",
            entity_category=EntityCategory.DIAGNOSTIC,
            # Parallax decodes these as bools; keep GraphQL's 0/1
            value_lambda=int,
        ),
        RivianSensorEntityDescription(
            key="limited_regen_braking_cold",
            field="limitedRegenCold",
            translation_key="limited_regen_braking_cold",
            entity_category=EntityCategory.DIAGNOSTIC,
            # Parallax decodes these as bools; keep GraphQL's 0/1
            value_lambda=int,
        ),
        RivianSensorEntityDescription(
            key="bluetooth_front_fascia_hardware_failure_status",
            translation_key="bluetooth_front_fascia_hardware_failure_status",
            field="btmFfHardwareFailureStatus",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.ENUM,
            options=BTM_FAILURE_STATUS_OPTIONS,
        ),
        RivianSensorEntityDescription(
            key="bluetooth_rear_fascia_hardware_failure_status",
            translation_key="bluetooth_rear_fascia_hardware_failure_status",
            field="btmRfHardwareFailureStatus",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.ENUM,
            options=BTM_FAILURE_STATUS_OPTIONS,
        ),
        RivianSensorEntityDescription(
            key="bluetooth_instrument_controls_hardware_failure_status",
            translation_key="bluetooth_instrument_controls_hardware_failure_status",
            field="btmIcHardwareFailureStatus",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.ENUM,
            options=BTM_FAILURE_STATUS_OPTIONS,
        ),
        RivianSensorEntityDescription(
            key="bluetooth_right_front_door_hardware_failure_status",
            translation_key="bluetooth_right_front_door_hardware_failure_status",
            field="btmRfdHardwareFailureStatus",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.ENUM,
            options=BTM_FAILURE_STATUS_OPTIONS,
        ),
        RivianSensorEntityDescription(
            key="bluetooth_left_front_door_hardware_failure_status",
            translation_key="bluetooth_left_front_door_hardware_failure_status",
            field="btmLfdHardwareFailureStatus",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.ENUM,
            options=BTM_FAILURE_STATUS_OPTIONS,
        ),
        RivianSensorEntityDescription(
            key="frunk_next_action",
            translation_key="frunk_next_action",
            field="closureFrunkNextAction",
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
            ],
        ),
        RivianSensorEntityDescription(
            key="rear_hitch_status",
            translation_key="rear_hitch_status",
            field="rearHitchStatus",
            device_class=SensorDeviceClass.ENUM,
            options=[
                "not_present",
                "accessory",
                "trailer1",
                "trailer2",
                "trailer3",
            ],
        ),
        RivianSensorEntityDescription(
            key="cold_range_notification",
            translation_key="cold_range_notification",
            entity_category=EntityCategory.DIAGNOSTIC,
            field="coldRangeNotification",
            device_class=SensorDeviceClass.ENUM,
            options=[
                "normal_range",
                "cold_may_impact",
                "cold_impact",
            ],
        ),
        RivianSensorEntityDescription(
            key="charger_state",
            translation_key="charger_state",
            field="chargerState",
            device_class=SensorDeviceClass.ENUM,
            options=[
                "charging_ready",
                "charging_connecting",
                "charging_active",
                "charging_complete",
                "charging_scheduled",
                "charging_vehicle_error",
                "charging_station_error",
                "charging_stopped_by_user",
                "charging_stopped_by_station",
                "charging_payment_error",
                "charging_cert_error",
                "charging_tls_error",
                "charging_error_ac_adapter_used_on_dc",
                "charging_error_dc_adapter_used_on_ac",
                "charging_error_incompatible_charger",
                "charging_sd_compensation",
                "waiting_on_charger",
                "charging_error_not_ready_or_incompatible_charger",
                "charging_vehicle_stopped",
                "charging_payment_error_start_rivian_app",
                "charging_tls_error_unknown_charger",
                "charging_tls_error_unexpected_fail",
                "charging_tls_error_start_rivian_app",
                "smart_charging_paused",
                "smart_charging_active",
            ],
        ),
        RivianSensorEntityDescription(
            key="charging_fault",
            translation_key="charging_fault",
            field="chargingFaultChime",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.ENUM,
            options=[
                "none",
                "charging_disabled_all",
                "charging_disabled_dc",
                "charging_disabled_pin_temp_dc",
                "charging_disabled_pin_temp_gradient_dc",
                "charging_degraded_dc",
                "charging_disabled_ac",
                "charging_disabled_pin_temp_ac",
                "charging_degraded_ac",
                "charging_disabled_partial_connection",
                "charging_disabled_not_parked",
            ],
        ),
    ),
    # The R2 has a manual charge port door and doesn't get GraphQL vehicle state
    # updates for the install type
    ("R1",): (
        RivianSensorEntityDescription(
            key="charge_port_door_next_action",
            translation_key="charge_port_door_next_action",
            field="closureChargePortDoorNextAction",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.ENUM,
            options=[
                "sna",
                "open_allowed",
                "open_not_available",
                "open_not_allowed_faulted",
                "opening",
                "close_allowed",
                "obstructed_while_opening_close_allowed",
                "obstructed_while_opening_open_allowed",
                "obstructed_while_closing_close_allowed",
                "close_not_available",
                "close_not_allowed_faulted",
                "closing",
            ],
        ),
        RivianSensorEntityDescription(
            key="ota_install_type",
            field="otaInstallType",
            translation_key="ota_install_type",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.ENUM,
            options=["convenience"],
            # GraphQL sends "Convenience"
            value_lambda=lambda v: str(v).lower(),
        ),
    ),
    ("R1S", "R2"): (
        RivianSensorEntityDescription(
            key="liftgate_next_action",
            translation_key="liftgate_next_action",
            field="closureLiftgateNextAction",
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
    ("R1T",): (
        RivianSensorEntityDescription(
            key="gear_tunnel_left_next_action",
            translation_key="gear_tunnel_left_next_action",
            field="closureSideBinLeftNextAction",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.ENUM,
            options=[
                "sna",
                "open_allowed",
                "opening",
                "open_not_available",
                "open_not_allowed_faulted",
                "stuck_ajar_while_opening_open_allowed",
                "open_already_no_action_available",
                "open_allowed_confirm_vehicle_angle",
            ],
        ),
        RivianSensorEntityDescription(
            key="gear_tunnel_right_next_action",
            translation_key="gear_tunnel_right_next_action",
            field="closureSideBinRightNextAction",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.ENUM,
            options=[
                "sna",
                "open_allowed",
                "opening",
                "open_not_available",
                "open_not_allowed_faulted",
                "stuck_ajar_while_opening_open_allowed",
                "open_already_no_action_available",
                "open_allowed_confirm_vehicle_angle",
            ],
        ),
        RivianSensorEntityDescription(
            key="tailgate_next_action",
            translation_key="tailgate_next_action",
            field="closureTailgateNextAction",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.ENUM,
            options=[
                "sna",
                "open_allowed",
                "opening",
                "open_not_available",
                "open_not_allowed_faulted",
                "stuck_ajar_while_opening_open_allowed",
                "open_already_no_action_available",
                "open_allowed_confirm_vehicle_angle",
                "open_allowed_obstacle_detected",
                "open_allowed_trailer_detected",
            ],
        ),
    ),
    # The R2's rear glass, which drops into the liftgate
    ("R2",): (
        RivianSensorEntityDescription(
            key="window_rear_calibrated",
            translation_key="window_rear_calibrated",
            field="windowRearCalibrated",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.ENUM,
            options=WINDOW_CALIBRATION_OPTIONS,
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
            translation_key="alarm_sound_status",
            device_class=BinarySensorDeviceClass.TAMPER,
            on_value="true",
        ),
        RivianBinarySensorEntityDescription(
            key="cabin_preconditioning_status",
            field="cabinPreconditioningStatus",
            translation_key="cabin_preconditioning_status",
            device_class=BinarySensorDeviceClass.RUNNING,
            on_value=["active", "complete_maintain", "initiate"],
        ),
        RivianBinarySensorEntityDescription(
            key="charger_state",
            field="chargerState",
            translation_key="charger_state",
            device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
            on_value=list(ACTIVE_CHARGING_STATES),
        ),
        RivianBinarySensorEntityDescription(
            key="charger_status",
            # chargerStatus is only sent briefly on plug-in; connectionState is
            # in every charging.session.status ("error" and "v2l_connected"
            # count as plugged in)
            field="connectionState",
            translation_key="charger_status",
            device_class=BinarySensorDeviceClass.PLUG,
            on_value="disconnected",
            negate=True,
        ),
        RivianBinarySensorEntityDescription(
            key="closure_frunk_closed",
            field="closureFrunkClosed",
            translation_key="closure_frunk_closed",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        ),
        RivianBinarySensorEntityDescription(
            key="closure_frunk_locked",
            field="closureFrunkLocked",
            translation_key="closure_frunk_locked",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        ),
        RivianBinarySensorEntityDescription(
            key="defrost_defog_status",
            field="defrostDefogStatus",
            translation_key="defrost_defog_status",
            device_class=BinarySensorDeviceClass.RUNNING,
            on_value="off",
            negate=True,
        ),
        RivianBinarySensorEntityDescription(
            key="door_front_left_closed",
            field="doorFrontLeftClosed",
            translation_key="door_front_left_closed",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        ),
        RivianBinarySensorEntityDescription(
            key="door_front_left_locked",
            field="doorFrontLeftLocked",
            translation_key="door_front_left_locked",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        ),
        RivianBinarySensorEntityDescription(
            key="door_front_right_closed",
            field="doorFrontRightClosed",
            translation_key="door_front_right_closed",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        ),
        RivianBinarySensorEntityDescription(
            key="door_front_right_locked",
            field="doorFrontRightLocked",
            translation_key="door_front_right_locked",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        ),
        RivianBinarySensorEntityDescription(
            key="door_rear_left_closed",
            field="doorRearLeftClosed",
            translation_key="door_rear_left_closed",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        ),
        RivianBinarySensorEntityDescription(
            key="door_rear_left_locked",
            field="doorRearLeftLocked",
            translation_key="door_rear_left_locked",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        ),
        RivianBinarySensorEntityDescription(
            key="door_rear_right_closed",
            field="doorRearRightClosed",
            translation_key="door_rear_right_closed",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        ),
        RivianBinarySensorEntityDescription(
            key="door_rear_right_locked",
            field="doorRearRightLocked",
            translation_key="door_rear_right_locked",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        ),
        RivianBinarySensorEntityDescription(
            key="pet_mode_status",
            field="petModeStatus",
            translation_key="pet_mode_status",
            device_class=BinarySensorDeviceClass.RUNNING,
            on_value="on",
        ),
        RivianBinarySensorEntityDescription(
            key="seat_front_left_heat",
            field="seatFrontLeftHeat",
            translation_key="seat_front_left_heat",
            device_class=BinarySensorDeviceClass.RUNNING,
            on_value=["level_1", "level_2", "level_3"],
        ),
        RivianBinarySensorEntityDescription(
            key="seat_front_left_vent",
            field="seatFrontLeftVent",
            translation_key="seat_front_left_vent",
            device_class=BinarySensorDeviceClass.RUNNING,
            on_value=["level_1", "level_2", "level_3"],
        ),
        RivianBinarySensorEntityDescription(
            key="seat_front_right_heat",
            field="seatFrontRightHeat",
            translation_key="seat_front_right_heat",
            device_class=BinarySensorDeviceClass.RUNNING,
            on_value=["level_1", "level_2", "level_3"],
        ),
        RivianBinarySensorEntityDescription(
            key="seat_front_right_vent",
            field="seatFrontRightVent",
            translation_key="seat_front_right_vent",
            device_class=BinarySensorDeviceClass.RUNNING,
            on_value=["level_1", "level_2", "level_3"],
        ),
        RivianBinarySensorEntityDescription(
            key="seat_rear_left_heat",
            field="seatRearLeftHeat",
            translation_key="seat_rear_left_heat",
            device_class=BinarySensorDeviceClass.RUNNING,
            on_value=["level_1", "level_2", "level_3"],
        ),
        RivianBinarySensorEntityDescription(
            key="seat_rear_right_heat",
            field="seatRearRightHeat",
            translation_key="seat_rear_right_heat",
            device_class=BinarySensorDeviceClass.RUNNING,
            on_value=["level_1", "level_2", "level_3"],
        ),
        RivianBinarySensorEntityDescription(
            key="steering_wheel_heat",
            field="steeringWheelHeat",
            translation_key="steering_wheel_heat",
            device_class=BinarySensorDeviceClass.RUNNING,
            on_value="level_1",
        ),
        RivianBinarySensorEntityDescription(
            key="tire_pressure_status_valid_front_left",
            field="tirePressureStatusValidFrontLeft",
            translation_key="tire_pressure_status_valid_front_left",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=BinarySensorDeviceClass.PROBLEM,
            on_value="invalid",
        ),
        RivianBinarySensorEntityDescription(
            key="tire_pressure_status_valid_front_right",
            field="tirePressureStatusValidFrontRight",
            translation_key="tire_pressure_status_valid_front_right",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=BinarySensorDeviceClass.PROBLEM,
            on_value="invalid",
        ),
        RivianBinarySensorEntityDescription(
            key="tire_pressure_status_valid_rear_left",
            field="tirePressureStatusValidRearLeft",
            translation_key="tire_pressure_status_valid_rear_left",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=BinarySensorDeviceClass.PROBLEM,
            on_value="invalid",
        ),
        RivianBinarySensorEntityDescription(
            key="tire_pressure_status_valid_rear_right",
            field="tirePressureStatusValidRearRight",
            translation_key="tire_pressure_status_valid_rear_right",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=BinarySensorDeviceClass.PROBLEM,
            on_value="invalid",
        ),
        RivianBinarySensorEntityDescription(
            key="window_front_left_closed",
            field="windowFrontLeftClosed",
            translation_key="window_front_left_closed",
            device_class=BinarySensorDeviceClass.WINDOW,
            on_value="open",
        ),
        RivianBinarySensorEntityDescription(
            key="window_front_right_closed",
            field="windowFrontRightClosed",
            translation_key="window_front_right_closed",
            device_class=BinarySensorDeviceClass.WINDOW,
            on_value="open",
        ),
        RivianBinarySensorEntityDescription(
            key="window_rear_left_closed",
            field="windowRearLeftClosed",
            translation_key="window_rear_left_closed",
            device_class=BinarySensorDeviceClass.WINDOW,
            on_value="open",
        ),
        RivianBinarySensorEntityDescription(
            key="window_rear_right_closed",
            field="windowRearRightClosed",
            translation_key="window_rear_right_closed",
            device_class=BinarySensorDeviceClass.WINDOW,
            on_value="open",
        ),
        RivianBinarySensorEntityDescription(
            key="wiper_fluid_state",
            field="wiperFluidState",
            translation_key="wiper_fluid_state",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=BinarySensorDeviceClass.PROBLEM,
            on_value="normal",
            negate=True,
        ),
        RivianBinarySensorEntityDescription(
            key="locked_state",
            field=LOCK_STATE_ENTITIES,
            translation_key="locked_state",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        ),
        RivianBinarySensorEntityDescription(
            key="door_state",
            field=DOOR_STATE_ENTITIES,
            translation_key="door_state",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        ),
        RivianBinarySensorEntityDescription(
            key="closure_state",
            field=CLOSURE_STATE_ENTITIES,
            translation_key="closure_state",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        ),
        RivianBinarySensorEntityDescription(
            key="use_state",
            field="powerState",
            translation_key="use_state",
            device_class=BinarySensorDeviceClass.MOVING,
            on_value="go",
        ),
        RivianBinarySensorEntityDescription(
            key="car_wash_mode",
            field="carWashMode",
            translation_key="car_wash_mode",
            entity_category=EntityCategory.DIAGNOSTIC,
            on_value="on",
        ),
    ),
    # The R2 has a manual charge port door and no tailgate
    ("R1",): (
        RivianBinarySensorEntityDescription(
            key="charge_port",
            field="chargePortState",
            translation_key="charge_port",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        ),
        RivianBinarySensorEntityDescription(
            key="closure_tailgate_locked",
            field="closureTailgateLocked",
            translation_key="closure_tailgate_locked",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        ),
        RivianBinarySensorEntityDescription(
            key="gear_guard_locked",
            field="gearGuardLocked",
            translation_key="gear_guard_locked",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        ),
    ),
    # The R1S doesn't report tailgate open/closed
    ("R1T",): (
        RivianBinarySensorEntityDescription(
            key="closure_tailgate_closed",
            field="closureTailgateClosed",
            translation_key="closure_tailgate_closed",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        ),
        RivianBinarySensorEntityDescription(
            key="closure_side_bin_left_closed",
            field="closureSideBinLeftClosed",
            translation_key="closure_side_bin_left_closed",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        ),
        RivianBinarySensorEntityDescription(
            key="closure_side_bin_left_locked",
            field="closureSideBinLeftLocked",
            translation_key="closure_side_bin_left_locked",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        ),
        RivianBinarySensorEntityDescription(
            key="closure_side_bin_right_closed",
            field="closureSideBinRightClosed",
            translation_key="closure_side_bin_right_closed",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        ),
        RivianBinarySensorEntityDescription(
            key="closure_side_bin_right_locked",
            field="closureSideBinRightLocked",
            translation_key="closure_side_bin_right_locked",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        ),
        RivianBinarySensorEntityDescription(
            key="closure_tonneau_closed",
            field="closureTonneauClosed",
            translation_key="closure_tonneau_closed",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
            supported_feature="TONNEAU_CMD",
        ),
        RivianBinarySensorEntityDescription(
            key="closure_tonneau_locked",
            field="closureTonneauLocked",
            translation_key="closure_tonneau_locked",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
            supported_feature="TONNEAU_CMD",
        ),
    ),
    ("R1S", "R2"): (
        RivianBinarySensorEntityDescription(
            key="closure_liftgate_closed",
            field="closureLiftgateClosed",
            translation_key="closure_liftgate_closed",
            device_class=BinarySensorDeviceClass.DOOR,
            on_value="open",
        ),
        RivianBinarySensorEntityDescription(
            key="closure_liftgate_locked",
            field="closureLiftgateLocked",
            translation_key="closure_liftgate_locked",
            device_class=BinarySensorDeviceClass.LOCK,
            on_value="unlocked",
        ),
    ),
    ("R1S",): (
        RivianBinarySensorEntityDescription(
            key="seat_third_row_left_heat",
            field="seatThirdRowLeftHeat",
            translation_key="seat_third_row_left_heat",
            device_class=BinarySensorDeviceClass.RUNNING,
            on_value=["level_1", "level_2", "level_3"],
        ),
        RivianBinarySensorEntityDescription(
            key="seat_third_row_right_heat",
            field="seatThirdRowRightHeat",
            translation_key="seat_third_row_right_heat",
            device_class=BinarySensorDeviceClass.RUNNING,
            on_value=["level_1", "level_2", "level_3"],
        ),
    ),
    # The R2's rear glass, which drops into the liftgate
    ("R2",): (
        RivianBinarySensorEntityDescription(
            key="window_rear_closed",
            field="windowRearClosed",
            translation_key="window_rear_closed",
            device_class=BinarySensorDeviceClass.WINDOW,
            on_value="open",
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
        "windowRearCalibrated",
        "windowRearLeftCalibrated",
        "windowRearRightCalibrated",
    }
)

# Sensors that were removed (the update entity shows their values), so their
# registry entries are cleaned up.
# TODO: Delete this, its use in sensor.py and tests/test_removed_sensors.py after
# 2027-04-01; by then installs will have updated past 1.5.3 and had these
# cleaned up.
REMOVED_SENSOR_KEYS: Final[frozenset[str]] = frozenset(
    {
        "ota_available_version",
        "ota_available_version_git_hash",
        "ota_available_version_number",
        "ota_available_version_week",
        "ota_available_version_year",
        "ota_current_version",
        "ota_current_version_git_hash",
        "ota_current_version_number",
        "ota_current_version_week",
        "ota_current_version_year",
        "ota_install_progress",
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

# navigation_service.trip_info fields. Once navigation ends, trip_info is sent
# empty, so they're cleared rather than kept from the last trip.
TRIP_INFO_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "batteryEmptyLatitude",
        "batteryEmptyLongitude",
        "batteryEmptyToDestinationDistance",
        "distance",
        "duration",
        "fasterRouteTimeSaved",
        "finalRangeRemaining",
        "finalStateOfCharge",
        "legs",
        "nextWaypointDepartureTime",
        "originBearing",
        "originLatitude",
        "originLongitude",
        "originStateOfCharge",
        "originTime",
        "overviewPolyline",
        "socIsBelowLimit",
        "tripId",
        "waypoints",
    }
)

# Parallax fields whose None value means "not set", so it clears the field
# rather than being skipped as unsent
PARALLAX_NONE_CLEARS: Final[frozenset[str]] = frozenset(
    {"otaScheduledInstallTime", *TRIP_INFO_FIELDS}
)

# Parallax topics that send one message per device/schedule, which can't be
# stored as flat vehicle state (and no entity uses)
PARALLAX_IGNORED_RVMS: Final[frozenset[str]] = frozenset(
    {"device_table.vas_keyper.devices", "ota.user_schedule.ota_config"}
)

# Values for fields an RVM topic leaves out when they don't apply, so stale
# values are cleared
PARALLAX_RVM_DEFAULTS: Final[dict[str, dict[str, Any]]] = {
    "ota.deployment.state": OTA_AVAILABLE_VERSION_IDLE,
    "navigation.navigation_service.trip_info": dict.fromkeys(TRIP_INFO_FIELDS),
}

# Vehicle state fields Parallax doesn't provide, so they're requested from the
# GraphQL vehicle state subscription. Every other field comes from Parallax,
# which sends a snapshot of every topic on subscribe.
VEHICLE_STATE_API_FIELDS: Final[set[str]] = {
    "activeDriverName",
    "otaInstallType",
}

# Driving fields GraphQL still sends every few seconds while driving, where
# Parallax sends position and speed about once a minute and the odometer about
# every two minutes. They're requested from GraphQL too. A GraphQL value only
# replaces an older one, and Parallax's values are used once GraphQL's report
# time for the field is older than GRAPHQL_FAST_FIELD_FRESHNESS, so Parallax
# takes over if GraphQL stops or keeps resending a frozen value.
VEHICLE_STATE_FAST_FIELDS: Final[frozenset[str]] = frozenset(
    {"gnssLocation", "gnssSpeed", "gnssAltitude", "gnssBearing", "vehicleMileage"}
)
GRAPHQL_FAST_FIELD_FRESHNESS: Final[timedelta] = timedelta(minutes=2)

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

DEFAULT_CHARGING_SCHEDULE_START: Final[int] = 0  # midnight
DEFAULT_CHARGING_SCHEDULE_DURATION: Final[int] = MINUTES_PER_DAY  # all day
DEFAULT_CHARGING_SCHEDULE_AMPERAGE: Final[int] = 48
DEFAULT_CHARGING_SCHEDULE: Final[dict[str, Any]] = {
    "startTime": DEFAULT_CHARGING_SCHEDULE_START,
    "duration": DEFAULT_CHARGING_SCHEDULE_DURATION,
    "amperage": DEFAULT_CHARGING_SCHEDULE_AMPERAGE,
    "enabled": True,
    "weekDays": list(WEEK_DAYS_ORDERED),
}

MPGE_CONVERSION_FACTOR: Final[float] = 33.705

DRIVE_SENSORS: Final[tuple[RivianSensorEntityDescription, ...]] = (
    RivianSensorEntityDescription(
        key="last_drive_efficiency",
        field="last_drive_efficiency",
        translation_key="last_drive_efficiency",
        device_class=SensorDeviceClass.ENERGY_DISTANCE,
        native_unit_of_measurement=UnitOfEnergyDistance.MILES_PER_KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
    ),
    RivianSensorEntityDescription(
        key="efficiency_30d",
        field="efficiency_30d",
        translation_key="efficiency_30d",
        device_class=SensorDeviceClass.ENERGY_DISTANCE,
        native_unit_of_measurement=UnitOfEnergyDistance.MILES_PER_KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
    ),
    RivianSensorEntityDescription(
        key="efficiency_all_time",
        field="efficiency_all_time",
        translation_key="efficiency_all_time",
        device_class=SensorDeviceClass.ENERGY_DISTANCE,
        native_unit_of_measurement=UnitOfEnergyDistance.MILES_PER_KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
    ),
    RivianSensorEntityDescription(
        key="last_drive_distance",
        field="last_drive_distance",
        translation_key="last_drive_distance",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.MILES,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    RivianSensorEntityDescription(
        key="last_drive_mpge",
        field="last_drive_mpge",
        translation_key="last_drive_mpge",
        native_unit_of_measurement="MPGe",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    RivianSensorEntityDescription(
        key="mpge_30d",
        field="mpge_30d",
        translation_key="mpge_30d",
        native_unit_of_measurement="MPGe",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    RivianSensorEntityDescription(
        key="mpge_all_time",
        field="mpge_all_time",
        translation_key="mpge_all_time",
        native_unit_of_measurement="MPGe",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    RivianSensorEntityDescription(
        key="drive_status",
        field="drive_status",
        translation_key="drive_status",
        device_class=SensorDeviceClass.ENUM,
        options=["parked", "driving"],
    ),
)
