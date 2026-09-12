"""Unit tests for Rivian drive efficiency and status sensor entities, platform registration, and translations."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.rivian import (
    async_setup_entry as integration_async_setup_entry,
    async_unload_entry as integration_async_unload_entry,
)
from custom_components.rivian.const import (
    ATTR_API,
    ATTR_COORDINATOR,
    ATTR_DRIVE_STORE,
    ATTR_DRIVE_TRACKER,
    ATTR_USER,
    ATTR_VEHICLE,
    ATTR_WALLBOX,
    DOMAIN,
    DRIVE_SENSORS,
    MPGE_CONVERSION_FACTOR,
)
from custom_components.rivian.drive_models import (
    ChargingSample,
    ChargingSessionRecord,
    DriveRecord,
    DriveSegment,
    SpeedBinData,
    VampireDrainRecord,
)
from custom_components.rivian.drive_storage import DriveStore
from custom_components.rivian.drive_tracker import DriveTracker
from custom_components.rivian.sensor import (
    RivianDriveSensorEntity,
    async_setup_entry as sensor_async_setup_entry,
)
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfLength

TEST_VIN = "7PDSGABA8NN000000"
TEST_VEHICLE_ID = "01894b9f-0000-0000-0000-000000000000"
REPO_ROOT = Path(__file__).parent.parent


class MockVehicleCoordinator:
    """Mock VehicleCoordinator providing vehicle telemetry feeds."""

    def __init__(self, vehicle_id: str = TEST_VEHICLE_ID) -> None:
        self.vehicle_id = vehicle_id
        self.data: dict[str, Any] = {
            "otaCurrentVersion": {"value": "2026.30.0"},
        }
        self._listeners: list[Any] = []
        self.last_update_success = True
        self.charging_coordinator = MagicMock()
        self.charging_coordinator.async_config_entry_first_refresh = AsyncMock()
        self.drivers_coordinator = MagicMock()
        self.drivers_coordinator.async_config_entry_first_refresh = AsyncMock()

    def get(self, key: str) -> Any | None:
        if entity := self.data.get(key, {}):
            return entity.get("value")
        return None

    def async_add_listener(self, update_callback: Any) -> Any:
        self._listeners.append(update_callback)

        def remove_listener() -> None:
            if update_callback in self._listeners:
                self._listeners.remove(update_callback)

        return remove_listener

    def set_telemetry(
        self,
        gear: str = "park",
        speed_mps: float = 0.0,
        odometer_m: float = 1609344.0,
        battery_soc: float = 80.0,
        battery_capacity: float = 135.0,
        altitude_m: float = 1600.0,
        latitude: float = 39.7392,
        longitude: float = -104.9903,
    ) -> None:
        """Update coordinator data and broadcast to listeners."""
        self.data = {
            "otaCurrentVersion": {"value": "2026.30.0"},
            "gearStatus": {"value": gear},
            "gnssSpeed": {"value": speed_mps},
            "vehicleMileage": {"value": odometer_m},
            "batteryLevel": {"value": battery_soc},
            "batteryCapacity": {"value": battery_capacity},
            "gnssAltitude": {"value": altitude_m},
            "gnssLocation": {
                "latitude": latitude,
                "longitude": longitude,
                "timeStamp": "2026-08-20T14:30:00Z",
            },
        }
        for listener in list(self._listeners):
            listener()


@pytest.fixture
def mock_vehicle_info() -> dict[str, Any]:
    """Sample vehicle info dictionary."""
    return {
        "id": TEST_VEHICLE_ID,
        "vin": TEST_VIN,
        "name": "r1s_adventure",
        "model": "R1S",
        "battery_capacity": 135.0,
    }


@pytest.fixture
def mock_config_entry() -> MagicMock:
    """Mock ConfigEntry instance."""
    entry = MagicMock()
    entry.entry_id = "test_entry_rivian_123"
    entry.options = {}
    entry.add_update_listener = MagicMock()
    entry.async_on_unload = MagicMock()
    return entry


class TestTranslationFiles:
    """Validation tests for strings.json and translations/en.json."""

    def test_strings_json_is_valid(self) -> None:
        """Verify strings.json exists, is valid JSON, and has entity definitions."""
        strings_path = REPO_ROOT / "custom_components" / "rivian" / "strings.json"
        assert strings_path.exists()
        with strings_path.open("r", encoding="utf-8") as f:
            data = json.load(f)

        assert "entity" in data
        assert "sensor" in data["entity"]
        sensors = data["entity"]["sensor"]

        expected_keys = [
            "last_drive_efficiency",
            "efficiency_30d",
            "efficiency_all_time",
            "last_drive_distance",
            "last_drive_mpge",
            "mpge_30d",
            "mpge_all_time",
            "drive_status",
        ]
        for key in expected_keys:
            assert key in sensors, f"Missing {key} in strings.json"
            assert "name" in sensors[key]

        assert "state" in sensors["drive_status"]
        assert sensors["drive_status"]["state"]["parked"] == "Parked"
        assert sensors["drive_status"]["state"]["driving"] == "Driving"

    def test_en_json_is_valid(self) -> None:
        """Verify translations/en.json exists and is valid JSON."""
        en_path = (
            REPO_ROOT / "custom_components" / "rivian" / "translations" / "en.json"
        )
        assert en_path.exists()
        with en_path.open("r", encoding="utf-8") as f:
            data = json.load(f)

        assert "entity" in data
        assert "sensor" in data["entity"]
        sensors = data["entity"]["sensor"]

        expected_keys = [
            "last_drive_efficiency",
            "efficiency_30d",
            "efficiency_all_time",
            "last_drive_distance",
            "last_drive_mpge",
            "mpge_30d",
            "mpge_all_time",
            "drive_status",
        ]
        for key in expected_keys:
            assert key in sensors, f"Missing {key} in en.json"
            assert "name" in sensors[key]

        assert sensors["drive_status"]["state"]["parked"] == "Parked"
        assert sensors["drive_status"]["state"]["driving"] == "Driving"

    def test_strings_and_en_json_parity(self) -> None:
        """Verify strings.json and en.json have exact matching keys for sensors and services."""
        strings_path = REPO_ROOT / "custom_components" / "rivian" / "strings.json"
        en_path = (
            REPO_ROOT / "custom_components" / "rivian" / "translations" / "en.json"
        )
        with strings_path.open("r", encoding="utf-8") as f:
            strings_data = json.load(f)
        with en_path.open("r", encoding="utf-8") as f:
            en_data = json.load(f)

        strings_sensors = strings_data["entity"]["sensor"]
        en_sensors = en_data["entity"]["sensor"]

        for key in (
            "last_drive_efficiency",
            "efficiency_30d",
            "efficiency_all_time",
            "last_drive_distance",
            "last_drive_mpge",
            "mpge_30d",
            "mpge_all_time",
            "drive_status",
        ):
            assert key in strings_sensors
            assert key in en_sensors
            assert strings_sensors[key]["name"] == en_sensors[key]["name"]

        # Verify services parity
        assert "services" in strings_data
        assert "services" in en_data
        strings_services = strings_data["services"]
        en_services = en_data["services"]

        for svc in ("backfill_drive_history", "create_efficiency_dashboard"):
            assert svc in strings_services, f"Missing service {svc} in strings.json"
            assert svc in en_services, f"Missing service {svc} in en.json"
            assert strings_services[svc]["name"] == en_services[svc]["name"]
            assert (
                strings_services[svc]["description"] == en_services[svc]["description"]
            )
            for f_key in strings_services[svc]["fields"]:
                assert f_key in en_services[svc]["fields"]
                assert (
                    strings_services[svc]["fields"][f_key]["name"]
                    == en_services[svc]["fields"][f_key]["name"]
                )


class TestConstDefinitions:
    """Tests for constants and entity descriptions in const.py."""

    def test_constants_values(self) -> None:
        """Verify conversion factor and attribute constants."""
        assert MPGE_CONVERSION_FACTOR == 33.705
        assert ATTR_DRIVE_STORE == "drive_store"
        assert ATTR_DRIVE_TRACKER == "drive_tracker"

    def test_drive_sensors_descriptions(self) -> None:
        """Verify the 8 sensor entity descriptions in const.py."""
        assert len(DRIVE_SENSORS) == 8
        keys = [d.key for d in DRIVE_SENSORS]
        assert keys == [
            "last_drive_efficiency",
            "efficiency_30d",
            "efficiency_all_time",
            "last_drive_distance",
            "last_drive_mpge",
            "mpge_30d",
            "mpge_all_time",
            "drive_status",
        ]

        desc_by_key = {d.key: d for d in DRIVE_SENSORS}

        eff_desc = desc_by_key["last_drive_efficiency"]
        assert eff_desc.native_unit_of_measurement == "mi/kWh"
        assert eff_desc.state_class == SensorStateClass.MEASUREMENT
        assert eff_desc.suggested_display_precision == 2

        eff_30d_desc = desc_by_key["efficiency_30d"]
        assert eff_30d_desc.native_unit_of_measurement == "mi/kWh"
        assert eff_30d_desc.state_class == SensorStateClass.MEASUREMENT
        assert eff_30d_desc.suggested_display_precision == 2

        eff_all_desc = desc_by_key["efficiency_all_time"]
        assert eff_all_desc.native_unit_of_measurement == "mi/kWh"
        assert eff_all_desc.state_class == SensorStateClass.MEASUREMENT
        assert eff_all_desc.suggested_display_precision == 2

        dist_desc = desc_by_key["last_drive_distance"]
        assert dist_desc.device_class == SensorDeviceClass.DISTANCE
        assert dist_desc.native_unit_of_measurement == UnitOfLength.MILES
        assert dist_desc.suggested_display_precision == 1

        mpge_desc = desc_by_key["last_drive_mpge"]
        assert mpge_desc.native_unit_of_measurement == "MPGe"
        assert mpge_desc.state_class == SensorStateClass.MEASUREMENT
        assert mpge_desc.suggested_display_precision == 1

        mpge_30d = desc_by_key["mpge_30d"]
        assert mpge_30d.native_unit_of_measurement == "MPGe"
        assert mpge_30d.suggested_display_precision == 1

        mpge_all = desc_by_key["mpge_all_time"]
        assert mpge_all.native_unit_of_measurement == "MPGe"
        assert mpge_all.suggested_display_precision == 1

        status_desc = desc_by_key["drive_status"]
        assert status_desc.device_class == SensorDeviceClass.ENUM
        assert "Parked" in status_desc.options
        assert "Driving" in status_desc.options


class TestDriveSensorEntities:
    """Unit tests for RivianDriveSensorEntity instances."""

    @pytest.mark.asyncio
    async def test_entity_creation_and_device_info(
        self,
        mock_hass: Any,
        mock_vehicle_info: dict[str, Any],
        mock_config_entry: MagicMock,
    ) -> None:
        """Verify device info, unique IDs, availability, and attributes."""
        coordinator = MockVehicleCoordinator()
        coordinator.set_telemetry()
        store = DriveStore(mock_hass, TEST_VIN)
        await store.async_load()
        tracker = DriveTracker(
            mock_hass, mock_config_entry, coordinator, mock_vehicle_info, store
        )
        await tracker.async_setup()

        entities = [
            RivianDriveSensorEntity(
                coordinator=coordinator,
                config_entry=mock_config_entry,
                description=desc,
                vehicle=mock_vehicle_info,
                tracker=tracker,
                store=store,
            )
            for desc in DRIVE_SENSORS
        ]

        assert len(entities) == 8

        for entity in entities:
            assert entity.available is True
            assert entity.has_entity_name is True
            assert entity.unique_id == f"{TEST_VIN}-{entity.entity_description.key}"
            device_info = entity.device_info
            assert device_info is not None
            assert (DOMAIN, TEST_VIN) in device_info["identifiers"]
            assert (DOMAIN, TEST_VEHICLE_ID) in device_info["identifiers"]
            assert device_info["manufacturer"] == "Rivian"
            assert device_info["model"] == "R1S"
            assert device_info["serial_number"] == TEST_VIN

        await tracker.async_unload()

    @pytest.mark.asyncio
    async def test_empty_storage_initial_states(
        self,
        mock_hass: Any,
        mock_vehicle_info: dict[str, Any],
        mock_config_entry: MagicMock,
    ) -> None:
        """Verify entity values and attributes when no drives exist in store."""
        coordinator = MockVehicleCoordinator()
        coordinator.set_telemetry(gear="park")
        store = DriveStore(mock_hass, TEST_VIN)
        await store.async_load()
        tracker = DriveTracker(
            mock_hass, mock_config_entry, coordinator, mock_vehicle_info, store
        )
        await tracker.async_setup()

        entities_by_key = {
            desc.key: RivianDriveSensorEntity(
                coordinator=coordinator,
                config_entry=mock_config_entry,
                description=desc,
                vehicle=mock_vehicle_info,
                tracker=tracker,
                store=store,
            )
            for desc in DRIVE_SENSORS
        }

        # Value checks for empty store
        assert entities_by_key["last_drive_efficiency"].native_value is None
        assert entities_by_key["efficiency_30d"].native_value is None
        assert entities_by_key["efficiency_all_time"].native_value is None
        assert entities_by_key["last_drive_distance"].native_value is None
        assert entities_by_key["last_drive_mpge"].native_value is None
        assert entities_by_key["mpge_30d"].native_value is None
        assert entities_by_key["mpge_all_time"].native_value is None
        assert entities_by_key["drive_status"].native_value == "Parked"

        # Extra state attributes for empty store
        assert entities_by_key["last_drive_efficiency"].extra_state_attributes is None
        assert entities_by_key["last_drive_distance"].extra_state_attributes is None
        assert entities_by_key["last_drive_mpge"].extra_state_attributes is None

        # Stats attributes for 30d and all-time
        stats_30d_attrs = entities_by_key["efficiency_30d"].extra_state_attributes
        assert stats_30d_attrs is not None
        assert stats_30d_attrs["drive_count"] == 0
        assert stats_30d_attrs["total_miles"] == 0.0
        assert stats_30d_attrs["total_kwh"] == 0.0

        # Drive status attributes in parked state
        status_attrs = entities_by_key["drive_status"].extra_state_attributes
        assert status_attrs is not None
        assert status_attrs["is_driving"] is False
        assert status_attrs["current_trip_distance_mi"] == 0.0
        assert status_attrs["current_trip_kwh"] == 0.0
        assert status_attrs["is_debouncing_park"] is False

        await tracker.async_unload()

    @pytest.mark.asyncio
    async def test_live_driving_state_updates(
        self,
        mock_hass: Any,
        mock_vehicle_info: dict[str, Any],
        mock_config_entry: MagicMock,
    ) -> None:
        """Verify real-time sensor states and attributes during active drive."""
        coordinator = MockVehicleCoordinator()
        coordinator.set_telemetry(gear="park", odometer_m=1609344.0, battery_soc=80.0)
        store = DriveStore(mock_hass, TEST_VIN)
        await store.async_load()
        tracker = DriveTracker(
            mock_hass, mock_config_entry, coordinator, mock_vehicle_info, store
        )
        await tracker.async_setup()

        entities_by_key = {
            desc.key: RivianDriveSensorEntity(
                coordinator=coordinator,
                config_entry=mock_config_entry,
                description=desc,
                vehicle=mock_vehicle_info,
                tracker=tracker,
                store=store,
            )
            for desc in DRIVE_SENSORS
        }

        # Mock async_write_ha_state
        for e in entities_by_key.values():
            e.async_write_ha_state = MagicMock()
            # Simulate async_added_to_hass
            await e.async_added_to_hass()

        # Shift into drive
        coordinator.set_telemetry(
            gear="drive",
            speed_mps=15.0,  # ~33.5 mph
            odometer_m=1609344.0,
            battery_soc=80.0,
        )

        # Drive status changes to Driving
        assert entities_by_key["drive_status"].native_value == "Driving"
        status_attrs = entities_by_key["drive_status"].extra_state_attributes
        assert status_attrs is not None
        assert status_attrs["is_driving"] is True

        # Telemetry progress: 10 miles traveled, 4.0 kWh used (SOC from 80% to 77.037% on 135kWh)
        coordinator.set_telemetry(
            gear="drive",
            speed_mps=20.0,
            odometer_m=1609344.0 + (10.0 * 1609.344),  # +10 miles
            battery_soc=77.0,  # 3% drop of 135 kWh = 4.05 kWh
            altitude_m=1650.0,
        )

        status_attrs = entities_by_key["drive_status"].extra_state_attributes
        assert status_attrs is not None
        assert status_attrs["is_driving"] is True
        assert round(status_attrs["current_trip_distance_mi"], 1) == 10.0
        assert status_attrs["current_trip_kwh"] > 0.0
        assert status_attrs["current_trip_efficiency"] > 0.0
        assert status_attrs["current_speed_mph"] > 0.0

        await tracker.async_unload()

    @pytest.mark.asyncio
    async def test_single_finalized_drive_metrics_and_attributes(
        self,
        mock_hass: Any,
        mock_vehicle_info: dict[str, Any],
        mock_config_entry: MagicMock,
    ) -> None:
        """Verify all 8 sensors after a drive is finalized and stored."""
        coordinator = MockVehicleCoordinator()
        coordinator.set_telemetry(gear="park")
        store = DriveStore(mock_hass, TEST_VIN)
        await store.async_load()

        speed_bins = {
            "0-9": SpeedBinData(miles=0.5, seconds=120.0),
            "10-19": SpeedBinData(miles=1.5, seconds=240.0),
            "20-29": SpeedBinData(miles=3.0, seconds=360.0),
            "30-39": SpeedBinData(miles=5.0, seconds=450.0),
            "40-49": SpeedBinData(miles=8.0, seconds=600.0),
            "50-59": SpeedBinData(miles=2.0, seconds=150.0),
            "60-69": SpeedBinData(miles=0.0, seconds=0.0),
            "70-79": SpeedBinData(miles=0.0, seconds=0.0),
            "80+": SpeedBinData(miles=0.0, seconds=0.0),
        }

        segment = DriveSegment(
            start_time="2026-08-20T14:30:00Z",
            duration_seconds=180.0,
            distance_miles=2.0,
            energy_kwh=0.6,
            efficiency_mi_kwh=3.33,
            avg_speed_mph=40.0,
            speed_bin="40-49",
            elevation_change_ft=10.0,
        )

        # 20.0 miles, 6.0 kWh -> 3.33 mi/kWh -> 112.35 MPGe
        drive = DriveRecord(
            vin=TEST_VIN,
            drive_id=f"{TEST_VIN}_1724164200",
            start_time="2026-08-20T14:30:00Z",
            end_time="2026-08-20T15:05:00Z",
            distance_miles=20.0,
            duration_seconds=2100.0,
            start_soc=85.0,
            end_soc=80.55,
            battery_capacity_kwh=135.0,
            energy_kwh=6.0,
            efficiency_mi_kwh=3.33,
            mpge=112.35,
            start_altitude_ft=5280.0,
            end_altitude_ft=5100.0,
            elevation_change_ft=-180.0,
            avg_speed_mph=34.3,
            max_speed_mph=58.2,
            integrated_temperature_f=72.5,
            speed_bins=speed_bins,
            is_micro_drive=False,
            segments=[segment],
        )
        await store.async_save_drive(drive)
        v_event = VampireDrainRecord(
            start_time="2026-08-20T10:00:00Z",
            end_time="2026-08-20T14:30:00Z",
            idle_hours=4.5,
            start_soc=83.0,
            end_soc=82.5,
            drain_soc=0.5,
            drain_kwh=0.68,
            rate_pct_per_day=2.67,
            avg_watts=150.0,
            avg_temp_f=70.0,
        )
        await store.async_save_vampire_events([v_event])
        dcfc_session = ChargingSessionRecord(
            session_id=f"{TEST_VIN}_1724160000",
            start_time="2026-08-20T13:00:00Z",
            end_time="2026-08-20T13:30:00Z",
            start_soc=20.0,
            end_soc=80.0,
            energy_added_kwh=81.0,
            max_power_kw=180.0,
            avg_power_kw=120.0,
            samples=[
                ChargingSample(
                    timestamp="2026-08-20T13:00:00Z",
                    soc=20.0,
                    power_kw=180.0,
                    battery_temp_f=85.0,
                ),
                ChargingSample(
                    timestamp="2026-08-20T13:30:00Z",
                    soc=80.0,
                    power_kw=65.0,
                    battery_temp_f=98.0,
                ),
            ],
            is_dcfc=True,
        )
        await store.async_save_dcfc_sessions([dcfc_session])

        tracker = DriveTracker(
            mock_hass, mock_config_entry, coordinator, mock_vehicle_info, store
        )
        await tracker.async_setup()

        entities_by_key = {
            desc.key: RivianDriveSensorEntity(
                coordinator=coordinator,
                config_entry=mock_config_entry,
                description=desc,
                vehicle=mock_vehicle_info,
                tracker=tracker,
                store=store,
            )
            for desc in DRIVE_SENSORS
        }

        # 1. last_drive_efficiency
        assert entities_by_key["last_drive_efficiency"].native_value == 3.33
        eff_attrs = entities_by_key["last_drive_efficiency"].extra_state_attributes
        assert eff_attrs is not None
        assert eff_attrs["mpge"] == 112.35
        assert eff_attrs["kwh_per_mi"] == round(6.0 / 20.0, 3)  # 0.3
        assert eff_attrs["distance"] == 20.0
        assert eff_attrs["duration"] == 2100.0
        assert eff_attrs["elevation_change_ft"] == -180.0
        assert eff_attrs["avg_speed"] == 34.3
        assert eff_attrs["max_speed"] == 58.2
        assert eff_attrs["integrated_temp_f"] == 72.5
        assert isinstance(eff_attrs["speed_bins"], dict)
        assert eff_attrs["speed_bins"]["40-49"]["miles"] == 8.0
        assert eff_attrs["start_time"] == "2026-08-20T14:30:00Z"
        assert eff_attrs["is_micro_drive"] is False

        # 2. efficiency_30d
        assert entities_by_key["efficiency_30d"].native_value == 3.33
        eff_30d_attrs = entities_by_key["efficiency_30d"].extra_state_attributes
        assert eff_30d_attrs is not None
        assert eff_30d_attrs["drive_count"] == 1
        assert eff_30d_attrs["total_miles"] == 20.0
        assert eff_30d_attrs["total_kwh"] == 6.0
        assert eff_30d_attrs["mpge"] == 112.35
        assert "stats_90d" in eff_30d_attrs
        assert "stats_365d" in eff_30d_attrs
        assert eff_30d_attrs["stats_90d"]["total_miles"] == 20.0
        assert eff_30d_attrs["stats_365d"]["total_miles"] == 20.0
        assert "recent_vampire_events" in eff_30d_attrs
        assert len(eff_30d_attrs["recent_vampire_events"]) == 1
        assert eff_30d_attrs["recent_vampire_events"][0]["idle_hours"] == 4.5
        assert eff_30d_attrs["recent_vampire_events"][0]["drain_kwh"] == 0.68
        assert "recent_segments" in eff_30d_attrs
        assert len(eff_30d_attrs["recent_segments"]) == 1
        assert eff_30d_attrs["recent_segments"][0]["mpge"] == segment.mpge
        assert eff_30d_attrs["recent_segments"][0]["efficiency_mi_kwh"] == 3.33
        assert "recent_dcfc_sessions" in eff_30d_attrs
        assert len(eff_30d_attrs["recent_dcfc_sessions"]) == 1
        assert eff_30d_attrs["recent_dcfc_sessions"][0]["max_power_kw"] == 180.0
        assert eff_30d_attrs["recent_dcfc_sessions"][0]["start_soc"] == 20.0
        assert eff_30d_attrs["recent_dcfc_sessions"][0]["end_soc"] == 80.0

        # 3. efficiency_all_time
        assert entities_by_key["efficiency_all_time"].native_value == 3.33
        eff_all_attrs = entities_by_key["efficiency_all_time"].extra_state_attributes
        assert eff_all_attrs is not None
        assert eff_all_attrs["drive_count"] == 1
        assert eff_all_attrs["total_miles"] == 20.0
        assert "recent_dcfc_sessions" in eff_all_attrs
        assert len(eff_all_attrs["recent_dcfc_sessions"]) == 1

        # 4. last_drive_distance
        assert entities_by_key["last_drive_distance"].native_value == 20.0
        dist_attrs = entities_by_key["last_drive_distance"].extra_state_attributes
        assert dist_attrs is not None
        assert dist_attrs["duration_seconds"] == 2100.0

        # 5. last_drive_mpge
        assert entities_by_key["last_drive_mpge"].native_value == round(
            112.35, 1
        )  # 112.3
        mpge_attrs = entities_by_key["last_drive_mpge"].extra_state_attributes
        assert mpge_attrs is not None
        assert mpge_attrs["efficiency_mi_kwh"] == 3.33

        # 6. mpge_30d
        assert entities_by_key["mpge_30d"].native_value == round(112.35, 1)  # 112.3

        # 7. mpge_all_time
        assert entities_by_key["mpge_all_time"].native_value == round(
            112.35, 1
        )  # 112.3

        # 8. drive_status
        assert entities_by_key["drive_status"].native_value == "Parked"

        await tracker.async_unload()

    @pytest.mark.asyncio
    async def test_multiple_drives_weighted_aggregations_and_micro_drive_filter(
        self,
        mock_hass: Any,
        mock_vehicle_info: dict[str, Any],
        mock_config_entry: MagicMock,
    ) -> None:
        """Verify weighted aggregation sum(miles)/sum(kWh) and micro-drive exclusion."""
        coordinator = MockVehicleCoordinator()
        coordinator.set_telemetry(gear="park")
        store = DriveStore(mock_hass, TEST_VIN)
        await store.async_load()

        # Drive 1: 100 miles, 40 kWh (2.50 mi/kWh, 84.26 MPGe)
        now_dt = datetime.now(timezone.utc)
        d1 = DriveRecord(
            vin=TEST_VIN,
            drive_id=f"{TEST_VIN}_1001",
            start_time=now_dt.isoformat(),
            end_time=now_dt.isoformat(),
            distance_miles=100.0,
            duration_seconds=7200.0,
            start_soc=90.0,
            end_soc=60.37,
            battery_capacity_kwh=135.0,
            energy_kwh=40.0,
            efficiency_mi_kwh=2.50,
            mpge=round(2.50 * 33.705, 2),
            is_micro_drive=False,
        )

        # Drive 2: 200 miles, 60 kWh (3.33 mi/kWh, 112.24 MPGe)
        d2 = DriveRecord(
            vin=TEST_VIN,
            drive_id=f"{TEST_VIN}_1002",
            start_time=now_dt.isoformat(),
            end_time=now_dt.isoformat(),
            distance_miles=200.0,
            duration_seconds=14400.0,
            start_soc=90.0,
            end_soc=45.55,
            battery_capacity_kwh=135.0,
            energy_kwh=60.0,
            efficiency_mi_kwh=3.33,
            mpge=round(3.33 * 33.705, 2),
            is_micro_drive=False,
        )

        # Drive 3: Micro-drive 0.3 miles, 0.5 kWh (should be excluded from rollups)
        d3 = DriveRecord(
            vin=TEST_VIN,
            drive_id=f"{TEST_VIN}_1003",
            start_time=now_dt.isoformat(),
            end_time=now_dt.isoformat(),
            distance_miles=0.3,
            duration_seconds=60.0,
            start_soc=90.0,
            end_soc=89.6,
            battery_capacity_kwh=135.0,
            energy_kwh=0.5,
            efficiency_mi_kwh=0.6,
            mpge=round(0.6 * 33.705, 2),
            is_micro_drive=True,
        )

        await store.async_save_drives_batch([d1, d2, d3])

        tracker = DriveTracker(
            mock_hass, mock_config_entry, coordinator, mock_vehicle_info, store
        )
        await tracker.async_setup()

        entities_by_key = {
            desc.key: RivianDriveSensorEntity(
                coordinator=coordinator,
                config_entry=mock_config_entry,
                description=desc,
                vehicle=mock_vehicle_info,
                tracker=tracker,
                store=store,
            )
            for desc in DRIVE_SENSORS
        }

        # Weighted calculation: (100 + 200) / (40 + 60) = 300 / 100 = 3.00 mi/kWh
        # Notice: Simple average would be (2.50 + 3.33) / 2 = 2.915 mi/kWh
        # Weighted period efficiency is 3.00 mi/kWh (101.12 MPGe)
        assert entities_by_key["efficiency_30d"].native_value == 3.00
        assert entities_by_key["efficiency_all_time"].native_value == 3.00
        assert entities_by_key["mpge_30d"].native_value == round(
            3.0 * 33.705, 1
        )  # 101.1
        assert entities_by_key["mpge_all_time"].native_value == round(3.0 * 33.705, 1)

        # Attributes show 2 valid drives, 1 micro drive
        attrs_30d = entities_by_key["efficiency_30d"].extra_state_attributes
        assert attrs_30d is not None
        assert attrs_30d["drive_count"] == 2
        assert attrs_30d["total_miles"] == 300.0
        assert attrs_30d["total_kwh"] == 100.0
        assert attrs_30d["total_micro_drives"] == 1

        # Last drive refers to the last appended drive (d3 micro-drive)
        assert entities_by_key["last_drive_distance"].native_value == 0.3
        assert entities_by_key["last_drive_efficiency"].native_value == 0.6

        await tracker.async_unload()

    @pytest.mark.asyncio
    async def test_tracker_listener_callback_triggers_ha_state_update(
        self,
        mock_hass: Any,
        mock_vehicle_info: dict[str, Any],
        mock_config_entry: MagicMock,
    ) -> None:
        """Verify that DriveTracker state changes notify and trigger async_write_ha_state."""
        coordinator = MockVehicleCoordinator()
        coordinator.set_telemetry(gear="park")
        store = DriveStore(mock_hass, TEST_VIN)
        await store.async_load()
        tracker = DriveTracker(
            mock_hass, mock_config_entry, coordinator, mock_vehicle_info, store
        )
        await tracker.async_setup()

        entity = RivianDriveSensorEntity(
            coordinator=coordinator,
            config_entry=mock_config_entry,
            description=DRIVE_SENSORS[0],
            vehicle=mock_vehicle_info,
            tracker=tracker,
            store=store,
        )

        entity.async_write_ha_state = MagicMock()
        await entity.async_added_to_hass()

        # Fire tracker notification
        tracker._notify_listeners()
        assert entity.async_write_ha_state.call_count == 1

        await tracker.async_unload()


class TestSensorPlatformSetup:
    """Tests for sensor platform async_setup_entry."""

    @pytest.mark.asyncio
    async def test_sensor_async_setup_entry_adds_drive_sensors(
        self,
        mock_hass: Any,
        mock_vehicle_info: dict[str, Any],
        mock_config_entry: MagicMock,
    ) -> None:
        """Verify sensor.async_setup_entry registers all 8 drive entities."""
        coordinator = MockVehicleCoordinator()
        store = DriveStore(mock_hass, TEST_VIN)
        await store.async_load()
        tracker = DriveTracker(
            mock_hass, mock_config_entry, coordinator, mock_vehicle_info, store
        )
        await tracker.async_setup()

        mock_hass.data.setdefault(DOMAIN, {})
        mock_hass.data[DOMAIN][mock_config_entry.entry_id] = {
            ATTR_API: MagicMock(),
            ATTR_VEHICLE: {TEST_VEHICLE_ID: mock_vehicle_info},
            ATTR_COORDINATOR: {
                ATTR_USER: MagicMock(),
                ATTR_VEHICLE: {TEST_VEHICLE_ID: coordinator},
                ATTR_WALLBOX: MagicMock(data=[]),
            },
            ATTR_DRIVE_TRACKER: {TEST_VEHICLE_ID: tracker},
            ATTR_DRIVE_STORE: {TEST_VEHICLE_ID: store},
        }

        added_entities: list[Any] = []

        def mock_add_entities(new_entities: list[Any]) -> None:
            added_entities.extend(new_entities)

        await sensor_async_setup_entry(mock_hass, mock_config_entry, mock_add_entities)

        # Filter added drive entities
        drive_entities = [
            e for e in added_entities if isinstance(e, RivianDriveSensorEntity)
        ]
        assert len(drive_entities) == 8

        registered_keys = [e.entity_description.key for e in drive_entities]
        for key in (
            "last_drive_efficiency",
            "efficiency_30d",
            "efficiency_all_time",
            "last_drive_distance",
            "last_drive_mpge",
            "mpge_30d",
            "mpge_all_time",
            "drive_status",
        ):
            assert key in registered_keys

        await tracker.async_unload()


class TestIntegrationLifecycle:
    """Tests for integration async_setup_entry and async_unload_entry lifecycle."""

    @pytest.mark.asyncio
    async def test_integration_lifecycle_setup_and_unload(
        self,
        mock_hass: Any,
        mock_vehicle_info: dict[str, Any],
        mock_config_entry: MagicMock,
    ) -> None:
        """Verify __init__.py initializes DriveStore/DriveTracker and unloads cleanly."""
        mock_api = AsyncMock()
        mock_api.create_csrf_token = AsyncMock()
        mock_api.close = AsyncMock()

        mock_user_coordinator = MagicMock()
        mock_user_coordinator.async_config_entry_first_refresh = AsyncMock()
        mock_user_coordinator.data = {"registrationChannels": []}
        mock_user_coordinator.get_vehicles = MagicMock(
            return_value={TEST_VEHICLE_ID: mock_vehicle_info}
        )

        mock_vehicle_coordinator = MockVehicleCoordinator()
        mock_vehicle_coordinator.async_config_entry_first_refresh = AsyncMock()

        mock_wallbox_coordinator = MagicMock()
        mock_wallbox_coordinator.async_config_entry_first_refresh = AsyncMock()

        mock_hass.config_entries = MagicMock()
        mock_hass.config_entries.async_forward_entry_setups = AsyncMock()
        mock_hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)

        with (
            patch(
                "custom_components.rivian.get_rivian_api_from_entry",
                return_value=mock_api,
            ),
            patch(
                "custom_components.rivian.UserCoordinator",
                return_value=mock_user_coordinator,
            ),
            patch(
                "custom_components.rivian.VehicleCoordinator",
                return_value=mock_vehicle_coordinator,
            ),
            patch(
                "custom_components.rivian.WallboxCoordinator",
                return_value=mock_wallbox_coordinator,
            ),
        ):
            success = await integration_async_setup_entry(mock_hass, mock_config_entry)
            assert success is True

            entry_data = mock_hass.data[DOMAIN][mock_config_entry.entry_id]
            assert ATTR_DRIVE_TRACKER in entry_data
            assert ATTR_DRIVE_STORE in entry_data
            assert TEST_VEHICLE_ID in entry_data[ATTR_DRIVE_TRACKER]
            assert TEST_VEHICLE_ID in entry_data[ATTR_DRIVE_STORE]

            tracker: DriveTracker = entry_data[ATTR_DRIVE_TRACKER][TEST_VEHICLE_ID]
            assert isinstance(tracker, DriveTracker)

            # Test unload
            unload_success = await integration_async_unload_entry(
                mock_hass, mock_config_entry
            )
            assert unload_success is True
            assert mock_config_entry.entry_id not in mock_hass.data[DOMAIN]
