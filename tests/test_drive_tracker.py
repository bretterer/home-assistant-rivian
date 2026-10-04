"""Unit tests for Rivian real-time drive tracker lifecycle and telemetry engine."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.rivian.config_flow import CONF_TRACK_CAPTURE
from custom_components.rivian.drive_models import (
    MPGE_FACTOR,
    DriveChunk,
    DriveState,
    DriveStatus,
    SpeedBinData,
)
from custom_components.rivian.drive_storage import DriveStore
from custom_components.rivian.drive_track import DriveTrack
from custom_components.rivian.drive_tracker import (
    CHECKPOINT_INTERVAL_SECONDS,
    RESUME_MAX_AGE_SECONDS,
    DriveTracker,
    get_speed_bin_key,
)

TEST_VIN = "7PDSGABA8NN000000"
TEST_VEHICLE_ID = "01894b9f-0000-0000-0000-000000000000"


class MockVehicleCoordinator:
    """Mock VehicleCoordinator providing controllable vehicle telemetry feeds."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self._listeners: list[Any] = []
        # Auto-incrementing fallback GPS fix clock, used only when a test
        # doesn't pass gps_ts explicitly, so every set_telemetry() call still
        # produces a "new" GPS fix (preserving pre-existing test behavior
        # that relies on every push producing a track point / GPS-lock check).
        self._auto_gps_dt = datetime.now(timezone.utc)

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
        odometer_m: float = 1609344.0,  # 1000.0 miles
        battery_soc: float = 80.0,
        battery_capacity: float = 135.0,
        altitude_m: float = 1600.0,
        latitude: float = 39.7392,
        longitude: float = -104.9903,
        power_state: str = "go",
        charger_state: str | None = None,
        charger_power: float | None = None,
        battery_temp_f: float | None = None,
        gps_ts: str | None = None,
        distance_to_empty_km: float | str | None = None,
        drive_mode: str | None = None,
        trailer_status: str | None = None,
        active_driver_name: str | None = None,
    ) -> None:
        """Update coordinator data and broadcast to listeners.

        ``gps_ts`` sets gnssLocation.timeStamp explicitly (an ISO-8601
        string, matching what Rivian sends). If omitted, a fresh timestamp
        is auto-generated each call (advancing by one second) so existing
        tests that don't care about GPS fix timing keep seeing a "new" fix
        on every push.
        """
        if gps_ts is None:
            self._auto_gps_dt += timedelta(seconds=1)
            gps_ts = self._auto_gps_dt.isoformat()

        self.data = {
            "gearStatus": {"value": gear},
            "gnssSpeed": {"value": speed_mps},
            "vehicleMileage": {"value": odometer_m},
            "batteryLevel": {"value": battery_soc},
            "batteryCapacity": {"value": battery_capacity},
            "gnssAltitude": {"value": altitude_m},
            "gnssLocation": {
                "latitude": latitude,
                "longitude": longitude,
                "timeStamp": gps_ts,
            },
            "powerState": {"value": power_state},
        }
        if charger_state is not None:
            self.data["chargerState"] = {"value": charger_state}
        if charger_power is not None:
            self.data["chargerPower"] = {"value": charger_power}
        if battery_temp_f is not None:
            self.data["batteryTemperature"] = {"value": battery_temp_f}
        if distance_to_empty_km is not None:
            self.data["distanceToEmpty"] = {"value": distance_to_empty_km}
        if drive_mode is not None:
            self.data["driveMode"] = {"value": drive_mode}
        if trailer_status is not None:
            self.data["trailerStatus"] = {"value": trailer_status}
        if active_driver_name is not None:
            self.data["activeDriverName"] = {"value": active_driver_name}
        for listener in list(self._listeners):
            listener()


class TestSpeedBinKey:
    """Tests for speed bin identifier determination."""

    def test_speed_bin_ranges(self) -> None:
        """Test speed bin classification across various speeds."""
        assert get_speed_bin_key(-5.0) == "0-9"
        assert get_speed_bin_key(0.0) == "0-9"
        assert get_speed_bin_key(5.5) == "0-9"
        assert get_speed_bin_key(9.99) == "0-9"
        assert get_speed_bin_key(10.0) == "10-19"
        assert get_speed_bin_key(19.9) == "10-19"
        assert get_speed_bin_key(20.0) == "20-29"
        assert get_speed_bin_key(35.0) == "30-39"
        assert get_speed_bin_key(45.0) == "40-49"
        assert get_speed_bin_key(55.0) == "50-59"
        assert get_speed_bin_key(65.0) == "60-69"
        assert get_speed_bin_key(75.0) == "70-79"
        assert get_speed_bin_key(80.0) == "80+"
        assert get_speed_bin_key(95.5) == "80+"


class TestDriveTrackerLifecycle:
    """Tests for DriveTracker state machine, debounce, and finalization."""

    @pytest.fixture
    def setup_tracker(
        self, mock_hass: Any, analytics_db: Any
    ) -> tuple[DriveTracker, MockVehicleCoordinator, DriveStore, AsyncMock]:
        """Create and initialize a DriveTracker instance with mock coordinator and weather client."""
        coordinator = MockVehicleCoordinator()
        store = DriveStore(mock_hass, TEST_VIN, analytics_db)
        weather_client = AsyncMock()
        weather_client.async_get_current_temperature = AsyncMock(return_value=72.0)

        vehicle_info = {
            "vin": TEST_VIN,
            "id": TEST_VEHICLE_ID,
            "name": "r1s_adventure",
            "model": "R1S",
            "battery_capacity": 135.0,
        }
        mock_entry = MagicMock()

        tracker = DriveTracker(
            hass=mock_hass,
            entry=mock_entry,
            coordinator=coordinator,  # type: ignore[arg-type]
            vehicle_info=vehicle_info,
            store=store,
            weather_client=weather_client,
        )
        return tracker, coordinator, store, weather_client

    @pytest.mark.asyncio
    async def test_initial_state_parked(
        self,
        setup_tracker: tuple[
            DriveTracker, MockVehicleCoordinator, DriveStore, AsyncMock
        ],
    ) -> None:
        """Test initial state before gear shifts."""
        tracker, coordinator, _store, _ = setup_tracker
        coordinator.set_telemetry(gear="park")
        await tracker.async_setup()

        assert tracker.is_driving is False
        assert tracker.drive_state.status == DriveStatus.PARKED.value
        assert tracker.drive_state.current_trip_distance_mi == 0.0
        assert tracker.drive_state.gps_locked is False

    @pytest.mark.asyncio
    async def test_gear_shift_to_drive_starts_session(
        self,
        setup_tracker: tuple[
            DriveTracker, MockVehicleCoordinator, DriveStore, AsyncMock
        ],
    ) -> None:
        """Test transitioning gearStatus from park to drive initiates active drive."""
        tracker, coordinator, _store, _ = setup_tracker
        coordinator.set_telemetry(gear="park", odometer_m=1609344.0, battery_soc=80.0)
        await tracker.async_setup()

        # Shift to drive
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=1609344.0,
            speed_mps=0.0,
            battery_soc=80.0,
        )

        assert tracker.is_driving is True
        assert tracker.drive_state.status == DriveStatus.DRIVING.value
        assert tracker.active_drive is not None
        assert tracker.active_drive["start_soc"] == 80.0
        assert tracker.active_drive["start_odometer_m"] == 1609344.0

    @pytest.mark.asyncio
    async def test_gear_shift_to_reverse_starts_session(
        self,
        setup_tracker: tuple[
            DriveTracker, MockVehicleCoordinator, DriveStore, AsyncMock
        ],
    ) -> None:
        """Test transitioning gearStatus from park to reverse initiates active drive."""
        tracker, coordinator, _store, _ = setup_tracker
        coordinator.set_telemetry(gear="park")
        await tracker.async_setup()

        # Shift to reverse
        coordinator.set_telemetry(gear="reverse", speed_mps=1.0)

        assert tracker.is_driving is True
        assert tracker.drive_state.status == DriveStatus.DRIVING.value

    @pytest.mark.asyncio
    async def test_park_debounce_and_cancellation_on_drive_resume(
        self,
        setup_tracker: tuple[
            DriveTracker, MockVehicleCoordinator, DriveStore, AsyncMock
        ],
    ) -> None:
        """Test shifting to park starts 60s debounce, and resuming drive cancels debounce without drive split."""
        tracker, coordinator, store, _ = setup_tracker
        coordinator.set_telemetry(gear="park", odometer_m=1609344.0)
        await tracker.async_setup()

        # Start drive
        coordinator.set_telemetry(gear="drive", odometer_m=1609344.0, speed_mps=10.0)
        assert tracker.is_driving is True
        assert tracker.is_debouncing_park is False

        # Stop at mailbox / gate -> Shift to park
        coordinator.set_telemetry(gear="park", odometer_m=1609500.0, speed_mps=0.0)
        assert tracker.is_driving is True
        assert tracker.is_debouncing_park is True  # 60s debounce started

        # Vehicle shifts back to drive within 60s
        coordinator.set_telemetry(gear="drive", odometer_m=1609500.0, speed_mps=5.0)
        assert tracker.is_driving is True
        assert tracker.is_debouncing_park is False  # Debounce cancelled!

        # Verify no drive has been saved to store yet
        assert store.drive_count == 0

    @pytest.mark.asyncio
    async def test_debounce_expiration_finalizes_drive(
        self,
        setup_tracker: tuple[
            DriveTracker, MockVehicleCoordinator, DriveStore, AsyncMock
        ],
    ) -> None:
        """Test 60s park debounce expiration finalizes the drive and persists to storage."""
        tracker, coordinator, store, _ = setup_tracker
        start_odo = 1609344.0  # 1000.0 mi
        # Drive 10 miles (16093.44 meters)
        end_odo = start_odo + 16093.44  # 1010.0 mi

        coordinator.set_telemetry(gear="park", odometer_m=start_odo, battery_soc=80.0)
        await tracker.async_setup()

        # Start drive
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo,
            battery_soc=80.0,
            speed_mps=15.0,
        )

        # Drive to destination
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=end_odo,
            battery_soc=77.0,  # 3% consumed from 135 kWh -> 4.05 kWh
            speed_mps=20.0,
        )

        # Shift to park
        coordinator.set_telemetry(
            gear="park",
            odometer_m=end_odo,
            battery_soc=77.0,
            speed_mps=0.0,
        )
        assert tracker.is_debouncing_park is True

        # Simulate expiration of debounce timer
        finalized_record = await tracker.async_finalize_drive()

        assert finalized_record is not None
        assert tracker.is_driving is False
        assert tracker.drive_state.status == DriveStatus.PARKED.value
        assert finalized_record.distance_miles == pytest.approx(10.0, rel=1e-2)
        assert finalized_record.energy_kwh == pytest.approx(4.05, rel=1e-2)
        assert finalized_record.efficiency_mi_kwh == pytest.approx(2.47, rel=1e-2)
        assert finalized_record.mpge == pytest.approx(2.47 * MPGE_FACTOR, rel=1e-2)
        assert finalized_record.is_micro_drive is False

        # Verify saved in store
        assert store.drive_count == 1
        assert store.last_drive is not None
        assert store.last_drive.drive_id == finalized_record.drive_id


class TestGPSLockGateAndWeatherSampling:
    """Tests for GPS lock gate validation and route weather sampling."""

    @pytest.fixture
    def setup_tracker(
        self, mock_hass: Any, analytics_db: Any
    ) -> tuple[DriveTracker, MockVehicleCoordinator, AsyncMock]:
        """Create and initialize tracker with weather mock."""
        coordinator = MockVehicleCoordinator()
        store = DriveStore(mock_hass, TEST_VIN, analytics_db)
        weather_client = AsyncMock()
        weather_client.async_get_current_temperature = AsyncMock(return_value=68.5)

        vehicle_info = {
            "vin": TEST_VIN,
            "id": TEST_VEHICLE_ID,
            "name": "r1s_adventure",
            "model": "R1S",
        }
        tracker = DriveTracker(
            hass=mock_hass,
            entry=MagicMock(),
            coordinator=coordinator,  # type: ignore[arg-type]
            vehicle_info=vehicle_info,
            store=store,
            weather_client=weather_client,
        )
        return tracker, coordinator, weather_client

    @pytest.mark.asyncio
    async def test_gps_lock_gate_by_speed(
        self,
        setup_tracker: tuple[DriveTracker, MockVehicleCoordinator, AsyncMock],
    ) -> None:
        """Test GPS lock triggers when vehicle speed exceeds 2 mph (0.894 m/s)."""
        tracker, coordinator, weather_client = setup_tracker
        start_odo = 100000.0
        coordinator.set_telemetry(gear="park", odometer_m=start_odo)
        await tracker.async_setup()

        # Shift to drive at standstill
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo,
            speed_mps=0.0,
        )
        await asyncio.sleep(0)
        assert tracker.active_drive["gps_locked"] is False
        assert weather_client.async_get_current_temperature.call_count == 0

        # Creeping in garage at 0.5 m/s (< 2 mph), delta odo 5m
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo + 5.0,
            speed_mps=0.5,
        )
        await asyncio.sleep(0)
        assert tracker.active_drive["gps_locked"] is False
        assert weather_client.async_get_current_temperature.call_count == 0

        # Exit garage, accelerate to 1.5 m/s (> 2 mph)
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo + 15.0,
            speed_mps=1.5,
        )
        await asyncio.sleep(0)
        assert tracker.active_drive["gps_locked"] is True
        assert tracker.drive_state.gps_locked is True
        # Initial weather waypoint requested
        assert weather_client.async_get_current_temperature.call_count == 1

    @pytest.mark.asyncio
    async def test_gps_lock_gate_by_odometer_delta(
        self,
        setup_tracker: tuple[DriveTracker, MockVehicleCoordinator, AsyncMock],
    ) -> None:
        """Test GPS lock triggers when odometer delta exceeds 50m even at low speed."""
        tracker, coordinator, weather_client = setup_tracker
        start_odo = 100000.0
        coordinator.set_telemetry(gear="park", odometer_m=start_odo)
        await tracker.async_setup()

        # Shift to drive at standstill
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo,
            speed_mps=0.0,
        )
        await asyncio.sleep(0)
        assert tracker.active_drive["gps_locked"] is False

        # Shift to drive, slow crawl 0.4 m/s across 55m
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo + 55.0,
            speed_mps=0.4,
        )
        await asyncio.sleep(0)
        assert tracker.active_drive["gps_locked"] is True
        assert weather_client.async_get_current_temperature.call_count == 1

    @pytest.mark.asyncio
    async def test_periodic_weather_sampling_by_distance(
        self,
        setup_tracker: tuple[DriveTracker, MockVehicleCoordinator, AsyncMock],
    ) -> None:
        """Test periodic weather waypoint sampling every 15 miles."""
        tracker, coordinator, weather_client = setup_tracker
        start_odo = 100000.0
        coordinator.set_telemetry(gear="park", odometer_m=start_odo)
        await tracker.async_setup()

        # Unlock GPS with speed
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo + 10.0,
            speed_mps=15.0,
        )
        await asyncio.sleep(0)
        assert weather_client.async_get_current_temperature.call_count == 1

        # Drive 10 miles (less than 15 mi interval) -> no new sample
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo + (10.0 * 1609.344),
            speed_mps=25.0,
        )
        await asyncio.sleep(0)
        assert weather_client.async_get_current_temperature.call_count == 1

        # Drive 16 miles from start (exceeds 15 mi interval) -> sample taken
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo + (16.0 * 1609.344),
            speed_mps=25.0,
        )
        await asyncio.sleep(0)
        assert weather_client.async_get_current_temperature.call_count == 2


class TestSpeedBinningAndCalculations:
    """Tests for speed bin accumulation, elevation deltas, and micro-drive logic."""

    @pytest.fixture
    def setup_tracker(
        self, mock_hass: Any, analytics_db: Any
    ) -> tuple[DriveTracker, MockVehicleCoordinator]:
        """Create tracker for binning tests."""
        coordinator = MockVehicleCoordinator()
        store = DriveStore(mock_hass, TEST_VIN, analytics_db)
        weather_client = AsyncMock()
        # Configured with a real float (not an unconfigured AsyncMock return
        # value) since a completed drive is now persisted through SQLite,
        # which requires weather_samples to be genuinely JSON-serializable.
        weather_client.async_get_current_temperature = AsyncMock(return_value=70.0)

        vehicle_info = {
            "vin": TEST_VIN,
            "id": TEST_VEHICLE_ID,
            "name": "r1s_adventure",
            "model": "R1S",
        }
        tracker = DriveTracker(
            hass=mock_hass,
            entry=MagicMock(),
            coordinator=coordinator,  # type: ignore[arg-type]
            vehicle_info=vehicle_info,
            store=store,
            weather_client=weather_client,
        )
        return tracker, coordinator

    @pytest.mark.asyncio
    async def test_speed_binning_distribution(
        self, setup_tracker: tuple[DriveTracker, MockVehicleCoordinator]
    ) -> None:
        """Test distance accumulation into appropriate 10 mph bins."""
        tracker, coordinator = setup_tracker
        start_odo = 500000.0
        coordinator.set_telemetry(gear="park", odometer_m=start_odo)
        await tracker.async_setup()

        # Start drive
        coordinator.set_telemetry(gear="drive", odometer_m=start_odo, speed_mps=0.0)

        # Drive 2 miles at 25 mph (speed bin "20-29")
        # 25 mph ~= 11.176 m/s
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo + (2.0 * 1609.344),
            speed_mps=11.176,
        )

        # Drive 5 miles at 65 mph (speed bin "60-69")
        # 65 mph ~= 29.057 m/s
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo + (7.0 * 1609.344),
            speed_mps=29.057,
        )

        # Drive 1 mile at 85 mph (speed bin "80+")
        # 85 mph ~= 37.998 m/s
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo + (8.0 * 1609.344),
            speed_mps=37.998,
        )

        drive = await tracker.async_finalize_drive()
        assert drive is not None

        bins = drive.speed_bins
        assert bins["20-29"].miles == pytest.approx(2.0, rel=1e-2)
        assert bins["60-69"].miles == pytest.approx(5.0, rel=1e-2)
        assert bins["80+"].miles == pytest.approx(1.0, rel=1e-2)

    @pytest.mark.asyncio
    async def test_elevation_delta_computation(
        self, setup_tracker: tuple[DriveTracker, MockVehicleCoordinator]
    ) -> None:
        """Test elevation delta in feet from meters."""
        tracker, coordinator = setup_tracker
        # Start at 1600 m (~5249.3 ft), end at 1700 m (~5577.4 ft) -> +328.1 ft delta
        coordinator.set_telemetry(gear="park", odometer_m=100000.0, altitude_m=1600.0)
        await tracker.async_setup()

        coordinator.set_telemetry(
            gear="drive",
            odometer_m=100000.0,
            altitude_m=1600.0,
            speed_mps=10.0,
        )
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=105000.0,
            altitude_m=1700.0,
            speed_mps=10.0,
        )

        drive = await tracker.async_finalize_drive()
        assert drive is not None
        assert drive.start_altitude_ft == pytest.approx(5249.3, rel=1e-2)
        assert drive.end_altitude_ft == pytest.approx(5577.4, rel=1e-2)
        assert drive.elevation_change_ft == pytest.approx(328.1, rel=1e-2)

    @pytest.mark.asyncio
    async def test_micro_drive_tagging(
        self, setup_tracker: tuple[DriveTracker, MockVehicleCoordinator]
    ) -> None:
        """Test micro-drive tagging for trips under 0.5 miles."""
        tracker, coordinator = setup_tracker
        start_odo = 100000.0
        coordinator.set_telemetry(gear="park", odometer_m=start_odo)
        await tracker.async_setup()

        # Drive 0.3 miles (482.8 meters)
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo,
            speed_mps=5.0,
        )
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo + 482.8,
            speed_mps=5.0,
        )

        micro_drive = await tracker.async_finalize_drive()
        assert micro_drive is not None
        assert micro_drive.distance_miles == pytest.approx(0.3, rel=1e-2)
        assert micro_drive.is_micro_drive is True


class TestListenersAndUnload:
    """Tests for listener notifications and clean unloading."""

    @pytest.mark.asyncio
    async def test_state_listener_notifications(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        """Test external listeners receive state update callbacks."""
        coordinator = MockVehicleCoordinator()
        store = DriveStore(mock_hass, TEST_VIN, analytics_db)
        tracker = DriveTracker(
            hass=mock_hass,
            entry=MagicMock(),
            coordinator=coordinator,  # type: ignore[arg-type]
            vehicle_info={"vin": TEST_VIN, "id": TEST_VEHICLE_ID},
            store=store,
        )
        coordinator.set_telemetry(gear="park")
        await tracker.async_setup()

        updates: list[DriveState] = []
        tracker.async_add_listener(lambda state, event: updates.append(state))

        # Shift to drive
        coordinator.set_telemetry(gear="drive", speed_mps=10.0)
        assert len(updates) >= 1
        assert updates[-1].is_driving is True
        assert updates[-1].status == "Driving"

    @pytest.mark.asyncio
    async def test_clean_unload(self, mock_hass: Any, analytics_db: Any) -> None:
        """Test async_unload clears listeners and cancels debounce timer."""
        coordinator = MockVehicleCoordinator()
        store = DriveStore(mock_hass, TEST_VIN, analytics_db)
        tracker = DriveTracker(
            hass=mock_hass,
            entry=MagicMock(),
            coordinator=coordinator,  # type: ignore[arg-type]
            vehicle_info={"vin": TEST_VIN, "id": TEST_VEHICLE_ID},
            store=store,
        )
        coordinator.set_telemetry(gear="park")
        await tracker.async_setup()

        # Start drive and shift to park to activate debounce timer
        coordinator.set_telemetry(gear="drive")
        coordinator.set_telemetry(gear="park")
        assert tracker.is_debouncing_park is True

        # Unload
        await tracker.async_unload()
        assert tracker.is_debouncing_park is False
        assert tracker._unsub_coordinator_listener is None

    @pytest.mark.asyncio
    async def test_idle_time_binning_at_traffic_lights(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        """Test idle time at 0 mph accumulates into 0-9 bin seconds without distance."""
        coordinator = MockVehicleCoordinator()
        store = DriveStore(mock_hass, TEST_VIN, analytics_db)
        tracker = DriveTracker(
            hass=mock_hass,
            entry=MagicMock(),
            coordinator=coordinator,  # type: ignore[arg-type]
            vehicle_info={"vin": TEST_VIN, "id": TEST_VEHICLE_ID},
            store=store,
        )
        start_odo = 100000.0
        coordinator.set_telemetry(gear="park", odometer_m=start_odo)
        await tracker.async_setup()

        # Start drive at start_odo
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo,
            speed_mps=0.0,
        )

        # Drive 1 mile at 30 mph
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo + (1.0 * 1609.344),
            speed_mps=13.41,
        )

        # Stop at red light for a few seconds at 0 speed, same odometer
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo + (1.0 * 1609.344),
            speed_mps=0.0,
        )

        drive = await tracker.async_finalize_drive()
        assert drive is not None
        assert drive.speed_bins["0-9"].miles == 0.0
        assert drive.speed_bins["30-39"].miles == pytest.approx(1.0, rel=1e-2)

    @pytest.mark.asyncio
    async def test_neutral_gear_behavior(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        """Test neutral gear while parked vs neutral gear while driving."""
        coordinator = MockVehicleCoordinator()
        store = DriveStore(mock_hass, TEST_VIN, analytics_db)
        tracker = DriveTracker(
            hass=mock_hass,
            entry=MagicMock(),
            coordinator=coordinator,  # type: ignore[arg-type]
            vehicle_info={"vin": TEST_VIN, "id": TEST_VEHICLE_ID},
            store=store,
        )
        coordinator.set_telemetry(gear="park")
        await tracker.async_setup()

        # Shifting to neutral while parked does not start driving
        coordinator.set_telemetry(gear="neutral")
        assert tracker.is_driving is False

        # Shifting to drive starts driving
        coordinator.set_telemetry(gear="drive", speed_mps=10.0)
        assert tracker.is_driving is True

        # Coasting in neutral while driving remains driving
        coordinator.set_telemetry(gear="neutral", speed_mps=8.0)
        assert tracker.is_driving is True

    @pytest.mark.asyncio
    async def test_repeated_rapid_park_toggles(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        """Test rapid drive -> park -> drive -> park toggling within debounce window."""
        coordinator = MockVehicleCoordinator()
        store = DriveStore(mock_hass, TEST_VIN, analytics_db)
        tracker = DriveTracker(
            hass=mock_hass,
            entry=MagicMock(),
            coordinator=coordinator,  # type: ignore[arg-type]
            vehicle_info={"vin": TEST_VIN, "id": TEST_VEHICLE_ID},
            store=store,
        )
        start_odo = 100000.0
        coordinator.set_telemetry(gear="park", odometer_m=start_odo)
        await tracker.async_setup()

        # Start drive at start_odo
        coordinator.set_telemetry(gear="drive", odometer_m=start_odo, speed_mps=0.0)
        # Drive 1 mi
        coordinator.set_telemetry(
            gear="drive", odometer_m=start_odo + 1609.344, speed_mps=15.0
        )
        # Park 1 (debounce on)
        coordinator.set_telemetry(
            gear="park", odometer_m=start_odo + 1609.344, speed_mps=0.0
        )
        assert tracker.is_debouncing_park is True
        # Drive again (debounce cancelled)
        coordinator.set_telemetry(
            gear="drive", odometer_m=start_odo + 3218.688, speed_mps=15.0
        )
        assert tracker.is_debouncing_park is False
        # Park 2 (debounce on)
        coordinator.set_telemetry(
            gear="park", odometer_m=start_odo + 3218.688, speed_mps=0.0
        )
        assert tracker.is_debouncing_park is True

        # Finalize
        drive = await tracker.async_finalize_drive()
        assert drive is not None
        assert drive.distance_miles == pytest.approx(2.0, rel=1e-2)
        assert store.drive_count == 1

    @pytest.mark.asyncio
    async def test_weather_sampling_integrated_temp_on_finalize(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        """Test finalized drive computes distance-weighted integrated temperature from samples."""
        coordinator = MockVehicleCoordinator()
        store = DriveStore(mock_hass, TEST_VIN, analytics_db)
        weather_client = AsyncMock()
        # Return 70°F on first sample, 80°F on second sample
        weather_client.async_get_current_temperature = AsyncMock(
            side_effect=[70.0, 80.0]
        )

        tracker = DriveTracker(
            hass=mock_hass,
            entry=MagicMock(),
            coordinator=coordinator,  # type: ignore[arg-type]
            vehicle_info={"vin": TEST_VIN, "id": TEST_VEHICLE_ID},
            store=store,
            weather_client=weather_client,
        )
        start_odo = 100000.0
        coordinator.set_telemetry(gear="park", odometer_m=start_odo)
        await tracker.async_setup()

        # Start drive and unlock GPS
        coordinator.set_telemetry(gear="drive", odometer_m=start_odo, speed_mps=15.0)
        await asyncio.sleep(0)  # processes sample 1 (70°F at 0.0 mi)

        # Drive 20 miles (triggers second periodic sample at 20.0 mi)
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo + (20.0 * 1609.344),
            speed_mps=25.0,
        )
        await asyncio.sleep(0)  # processes sample 2 (80°F at 20.0 mi)

        drive = await tracker.async_finalize_drive()
        assert drive is not None
        assert len(drive.weather_samples) == 2
        # Average of 70°F (0-20 mi) and 80°F (at 20 mi) = 75.0°F
        assert drive.integrated_temperature_f == pytest.approx(75.0, rel=1e-1)


class TestDriveTrackerCharging:
    """Tests for DriveTracker DC fast charging detection, sampling, and L1/L2 filtering."""

    @pytest.mark.asyncio
    async def test_dcfc_session_recorded_and_persisted(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        """Verify DC fast charging sessions (>22 kW) are sampled and saved to DriveStore."""
        coordinator = MockVehicleCoordinator()
        store = DriveStore(mock_hass, TEST_VIN, analytics_db)
        tracker = DriveTracker(
            hass=mock_hass,
            entry=MagicMock(),
            coordinator=coordinator,  # type: ignore[arg-type]
            vehicle_info={
                "vin": TEST_VIN,
                "id": TEST_VEHICLE_ID,
                "battery_capacity": 135.0,
            },
            store=store,
        )
        await tracker.async_setup()

        # Vehicle parked, plug in and initiate DC fast charge
        coordinator.set_telemetry(
            gear="park",
            battery_soc=20.0,
            charger_state="charging_active",
            charger_power=150.0,
            battery_temp_f=85.0,
        )
        assert tracker._active_charge_session is not None
        assert tracker._active_charge_session["max_power_kw"] == 150.0
        assert len(tracker._active_charge_session["samples"]) == 1

        # Mid-charge update (SoC rises to 35%, power adjusts to 135 kW)
        coordinator.set_telemetry(
            gear="park",
            battery_soc=35.0,
            charger_state="charging_active",
            charger_power=135.0,
            battery_temp_f=92.0,
        )
        assert len(tracker._active_charge_session["samples"]) == 2

        # Final charge point (SoC 50%, power tapering to 100 kW)
        coordinator.set_telemetry(
            gear="park",
            battery_soc=50.0,
            charger_state="charging_active",
            charger_power=100.0,
            battery_temp_f=95.0,
        )
        assert len(tracker._active_charge_session["samples"]) == 3

        # Unplug / complete charging
        coordinator.set_telemetry(
            gear="park",
            battery_soc=50.0,
            charger_state="charging_complete",
            charger_power=0.0,
        )
        assert tracker._active_charge_session is None

        # Allow the scheduled coroutine to run its full executor round-trip
        # (SQLite upsert + hot cache rebuild both hop through the executor).
        await asyncio.sleep(0.05)

        # Verify persisted DCFC session in DriveStore
        dcfc_sessions = store.get_dcfc_sessions()
        assert len(dcfc_sessions) == 1
        session = dcfc_sessions[0]
        assert session.is_dcfc is True
        assert session.start_soc == 20.0
        assert session.end_soc == 50.0
        assert session.max_power_kw == 150.0
        assert session.avg_power_kw == pytest.approx(
            (150.0 + 135.0 + 100.0) / 3.0, rel=1e-2
        )
        assert len(session.samples) == 3
        # Energy added: (50 - 20) * 135 / 100 = 40.5 kWh
        assert session.energy_added_kwh == pytest.approx(40.5, rel=1e-2)

    @pytest.mark.asyncio
    async def test_l2_charging_discarded_under_22kw(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        """A Level 2 session that lasts under 5 minutes is a blip, not kept as AC."""
        coordinator = MockVehicleCoordinator()
        store = DriveStore(mock_hass, TEST_VIN, analytics_db)
        tracker = DriveTracker(
            hass=mock_hass,
            entry=MagicMock(),
            coordinator=coordinator,  # type: ignore[arg-type]
            vehicle_info={
                "vin": TEST_VIN,
                "id": TEST_VEHICLE_ID,
                "battery_capacity": 135.0,
            },
            store=store,
        )
        await tracker.async_setup()

        # Connect to 48A L2 Wallbox (11.5 kW)
        coordinator.set_telemetry(
            gear="park",
            battery_soc=40.0,
            charger_state="charging_active",
            charger_power=11.5,
        )
        assert tracker._active_charge_session is not None

        # Charge to 60.0%
        coordinator.set_telemetry(
            gear="park",
            battery_soc=60.0,
            charger_state="charging_active",
            charger_power=11.5,
        )

        # Disconnect charger
        coordinator.set_telemetry(
            gear="park",
            battery_soc=60.0,
            charger_state="charging_ready",
            charger_power=0.0,
        )
        assert tracker._active_charge_session is None

        await asyncio.sleep(0)

        # Verify nothing persisted
        assert len(store.get_dcfc_sessions()) == 0

    @pytest.mark.asyncio
    async def test_charging_pauses_vampire_drain(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        """Verify active charging resets/pauses vampire drain idle tracking."""
        coordinator = MockVehicleCoordinator()
        store = DriveStore(mock_hass, TEST_VIN, analytics_db)
        tracker = DriveTracker(
            hass=mock_hass,
            entry=MagicMock(),
            coordinator=coordinator,  # type: ignore[arg-type]
            vehicle_info={"vin": TEST_VIN, "id": TEST_VEHICLE_ID},
            store=store,
        )
        await tracker.async_setup()

        # Simulate prior drive finalization establishing park baseline
        tracker._park_start_dt = datetime.now(timezone.utc)
        tracker._park_start_soc = 80.0

        # Charging starts: park idle baseline is paused
        coordinator.set_telemetry(
            gear="park",
            battery_soc=80.0,
            charger_state="charging_active",
            charger_power=150.0,
        )
        assert tracker._park_start_dt is None
        assert tracker._park_start_soc is None

    @pytest.mark.asyncio
    async def test_dcfc_session_finalized_on_shift_to_drive(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        """Verify active DCFC session is cleanly finalized when shifting into drive."""
        coordinator = MockVehicleCoordinator()
        store = DriveStore(mock_hass, TEST_VIN, analytics_db)
        tracker = DriveTracker(
            hass=mock_hass,
            entry=MagicMock(),
            coordinator=coordinator,  # type: ignore[arg-type]
            vehicle_info={
                "vin": TEST_VIN,
                "id": TEST_VEHICLE_ID,
                "battery_capacity": 135.0,
            },
            store=store,
        )
        await tracker.async_setup()

        # Start DC fast charge at 150 kW
        coordinator.set_telemetry(
            gear="park",
            battery_soc=30.0,
            charger_state="charging_active",
            charger_power=150.0,
        )
        assert tracker._active_charge_session is not None

        # Shift to drive (unplug and depart)
        coordinator.set_telemetry(
            gear="drive",
            battery_soc=75.0,
            charger_state="charging_ready",
            charger_power=0.0,
        )
        assert tracker._active_charge_session is None

        # Allow the scheduled coroutine to run its full executor round-trip
        # (SQLite upsert + hot cache rebuild both hop through the executor).
        await asyncio.sleep(0.05)

        # Verify DCFC session was finalized and recorded
        dcfc_sessions = store.get_dcfc_sessions()
        assert len(dcfc_sessions) == 1
        assert dcfc_sessions[0].start_soc == 30.0
        assert dcfc_sessions[0].end_soc == 75.0
        assert dcfc_sessions[0].max_power_kw == 150.0

    @pytest.mark.asyncio
    async def test_live_charging_tracks_soc_points_without_power(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        """Verify soc_points accumulate (deduplicated) while no power is reported."""
        coordinator = MockVehicleCoordinator()
        store = DriveStore(mock_hass, TEST_VIN, analytics_db)
        tracker = DriveTracker(
            hass=mock_hass,
            entry=MagicMock(),
            coordinator=coordinator,  # type: ignore[arg-type]
            vehicle_info={"vin": TEST_VIN, "id": TEST_VEHICLE_ID},
            store=store,
        )
        await tracker.async_setup()

        base = datetime.now(timezone.utc)
        clock = {"now": base}
        tracker._utcnow = lambda: clock["now"]  # type: ignore[method-assign]

        # No charger_power / power field reported - only chargerState + SoC.
        coordinator.set_telemetry(
            gear="park", battery_soc=45.0, charger_state="charging_active"
        )
        assert tracker._active_charge_session is not None
        assert tracker._active_charge_session["max_power_kw"] == 0.0
        assert tracker._active_charge_session["soc_points"] == [
            (base.timestamp(), 45.0)
        ]

        # A near-duplicate point (tiny SoC move, well under 20s later) is deduped.
        clock["now"] = base + timedelta(seconds=5)
        coordinator.set_telemetry(
            gear="park", battery_soc=45.02, charger_state="charging_active"
        )
        assert len(tracker._active_charge_session["soc_points"]) == 1

        # A meaningfully later/different point is kept.
        clock["now"] = base + timedelta(seconds=30)
        coordinator.set_telemetry(
            gear="park", battery_soc=46.0, charger_state="charging_active"
        )
        assert len(tracker._active_charge_session["soc_points"]) == 2
        assert tracker._active_charge_session["soc_points"][-1] == (
            (base + timedelta(seconds=30)).timestamp(),
            46.0,
        )

    @pytest.mark.asyncio
    async def test_dcfc_estimated_from_soc_when_no_power_reported(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        """A real DCFC session with no power telemetry is still recorded via SoC estimation."""
        coordinator = MockVehicleCoordinator()
        store = DriveStore(mock_hass, TEST_VIN, analytics_db)
        tracker = DriveTracker(
            hass=mock_hass,
            entry=MagicMock(),
            coordinator=coordinator,  # type: ignore[arg-type]
            vehicle_info={
                "vin": TEST_VIN,
                "id": TEST_VEHICLE_ID,
                "battery_capacity": 135.0,
            },
            store=store,
        )
        await tracker.async_setup()

        base = datetime.now(timezone.utc)
        clock = {"now": base}
        tracker._utcnow = lambda: clock["now"]  # type: ignore[method-assign]

        coordinator.set_telemetry(
            gear="park", battery_soc=45.0, charger_state="charging_active"
        )
        assert tracker._active_charge_session is not None

        # 45.0% -> 80.9% over 33 minutes, sampled every 30s (no power field ever).
        total_seconds = 33 * 60
        step = 30
        start_soc = 45.0
        end_soc = 80.9
        n_steps = total_seconds // step
        for i in range(1, n_steps + 1):
            clock["now"] = base + timedelta(seconds=i * step)
            soc = start_soc + (end_soc - start_soc) * (i / n_steps)
            coordinator.set_telemetry(
                gear="park", battery_soc=soc, charger_state="charging_active"
            )

        assert tracker._active_charge_session["max_power_kw"] == 0.0
        assert tracker._active_charge_session["samples"] == []

        # Unplug / complete charging
        clock["now"] = base + timedelta(seconds=total_seconds)
        coordinator.set_telemetry(
            gear="park", battery_soc=end_soc, charger_state="charging_complete"
        )
        assert tracker._active_charge_session is None

        await asyncio.sleep(0.05)

        dcfc_sessions = store.get_dcfc_sessions()
        assert len(dcfc_sessions) == 1
        session = dcfc_sessions[0]
        assert session.is_dcfc is True
        assert session.start_soc == pytest.approx(45.0, abs=0.5)
        assert session.end_soc == pytest.approx(80.9, abs=0.5)
        assert 60.0 < session.max_power_kw < 225.0
        assert len(session.samples) > 0

    @pytest.mark.asyncio
    async def test_l2_like_soc_rise_still_discarded_without_power(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        """A slow, Level-2-like SoC rise with no power telemetry stays below the DCFC floor."""
        coordinator = MockVehicleCoordinator()
        store = DriveStore(mock_hass, TEST_VIN, analytics_db)
        tracker = DriveTracker(
            hass=mock_hass,
            entry=MagicMock(),
            coordinator=coordinator,  # type: ignore[arg-type]
            vehicle_info={
                "vin": TEST_VIN,
                "id": TEST_VEHICLE_ID,
                "battery_capacity": 135.0,
            },
            store=store,
        )
        await tracker.async_setup()

        base = datetime.now(timezone.utc)
        clock = {"now": base}
        tracker._utcnow = lambda: clock["now"]  # type: ignore[method-assign]

        coordinator.set_telemetry(
            gear="park", battery_soc=40.0, charger_state="charging_active"
        )

        # ~7 kW-equivalent rise over 1 hour: dSoC = 7 kWh / 135 kWh * 100.
        total_seconds = 3600
        step = 30
        start_soc = 40.0
        end_soc = start_soc + (7.0 / 135.0) * 100.0
        n_steps = total_seconds // step
        for i in range(1, n_steps + 1):
            clock["now"] = base + timedelta(seconds=i * step)
            soc = start_soc + (end_soc - start_soc) * (i / n_steps)
            coordinator.set_telemetry(
                gear="park", battery_soc=soc, charger_state="charging_active"
            )

        clock["now"] = base + timedelta(seconds=total_seconds)
        coordinator.set_telemetry(
            gear="park", battery_soc=end_soc, charger_state="charging_complete"
        )
        assert tracker._active_charge_session is None

        await asyncio.sleep(0.05)

        assert len(store.get_dcfc_sessions()) == 0


def _make_tracker(
    mock_hass: Any,
    analytics_db: Any,
    coordinator: MockVehicleCoordinator,
    *,
    track_capture: bool | None = None,
) -> tuple[DriveTracker, DriveStore]:
    """Build a DriveTracker + DriveStore pair with a real options dict on entry."""
    store = DriveStore(mock_hass, TEST_VIN, analytics_db)
    entry = MagicMock()
    entry.options = {} if track_capture is None else {CONF_TRACK_CAPTURE: track_capture}
    tracker = DriveTracker(
        hass=mock_hass,
        entry=entry,
        coordinator=coordinator,  # type: ignore[arg-type]
        vehicle_info={
            "vin": TEST_VIN,
            "id": TEST_VEHICLE_ID,
            "battery_capacity": 135.0,
        },
        store=store,
    )
    return tracker, store


class TestGPSTrackCapture:
    """Tests for live GPS track capture: dedup, staleness, and the disable option."""

    @pytest.mark.asyncio
    async def test_repeated_identical_fix_produces_one_point(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        """Identical gnssLocation pushes must not produce duplicate track points."""
        coordinator = MockVehicleCoordinator()
        tracker, _store = _make_tracker(mock_hass, analytics_db, coordinator)
        start_odo = 100000.0
        coordinator.set_telemetry(gear="park", odometer_m=start_odo)
        await tracker.async_setup()

        now = datetime.now(timezone.utc)
        fix_1 = (now + timedelta(seconds=1)).isoformat()

        coordinator.set_telemetry(
            gear="drive", odometer_m=start_odo, speed_mps=5.0, gps_ts=fix_1
        )
        assert isinstance(tracker._active_track, DriveTrack)
        assert len(tracker._active_track) == 1

        # Identical push (same fix, more telemetry noise) -> no new point.
        coordinator.set_telemetry(
            gear="drive", odometer_m=start_odo + 10.0, speed_mps=5.0, gps_ts=fix_1
        )
        assert len(tracker._active_track) == 1

        # A genuinely new fix timestamp -> point appended.
        fix_2 = (now + timedelta(seconds=6)).isoformat()
        coordinator.set_telemetry(
            gear="drive", odometer_m=start_odo + 50.0, speed_mps=5.0, gps_ts=fix_2
        )
        assert len(tracker._active_track) == 2

    @pytest.mark.asyncio
    async def test_stale_fix_skipped_once_track_started(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        """A fix older than drive-start minus 120s is rejected once the track has a point."""
        coordinator = MockVehicleCoordinator()
        tracker, _store = _make_tracker(mock_hass, analytics_db, coordinator)
        start_odo = 100000.0
        coordinator.set_telemetry(gear="park", odometer_m=start_odo)
        await tracker.async_setup()

        now = datetime.now(timezone.utc)
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo,
            speed_mps=5.0,
            gps_ts=(now + timedelta(seconds=1)).isoformat(),
        )
        assert len(tracker._active_track) == 1

        start_epoch = tracker.active_drive["start_epoch"]
        stale_fix = datetime.fromtimestamp(
            start_epoch - 300, tz=timezone.utc
        ).isoformat()
        coordinator.set_telemetry(
            gear="drive", odometer_m=start_odo + 20.0, speed_mps=5.0, gps_ts=stale_fix
        )
        assert len(tracker._active_track) == 1  # stale fix rejected

    @pytest.mark.asyncio
    async def test_capture_disabled_leaves_drive_without_track(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        """With track_capture disabled, the drive is still saved but has no GPS track."""
        coordinator = MockVehicleCoordinator()
        tracker, store = _make_tracker(
            mock_hass, analytics_db, coordinator, track_capture=False
        )
        start_odo = 100000.0
        coordinator.set_telemetry(gear="park", odometer_m=start_odo)
        await tracker.async_setup()

        coordinator.set_telemetry(gear="drive", odometer_m=start_odo, speed_mps=10.0)
        coordinator.set_telemetry(
            gear="drive", odometer_m=start_odo + 3218.688, speed_mps=15.0
        )

        drive = await tracker.async_finalize_drive()
        assert drive is not None
        assert store.drive_count == 1

        track = await store.async_get_track(drive.drive_id)
        assert track is None

    @pytest.mark.asyncio
    async def test_fix_without_timestamp_dedupes_on_position(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        """Without a timeStamp every push would look identical; position decides."""
        coordinator = MockVehicleCoordinator()
        tracker, _store = _make_tracker(mock_hass, analytics_db, coordinator)
        coordinator.set_telemetry(gear="park", odometer_m=100000.0)
        await tracker.async_setup()
        coordinator.set_telemetry(gear="drive", odometer_m=100000.0, speed_mps=5.0)
        assert len(tracker._active_track) == 1

        fix_times = iter(range(tracker.active_drive["start_epoch"] + 10, 10**10, 5))
        tracker._parse_fix_timestamp = lambda _raw: float(next(fix_times))
        for lat in (39.741, 39.742, 39.743):
            coordinator.data["gnssLocation"] = {"latitude": lat, "longitude": -104.99}
            tracker.handle_coordinator_update()
        assert len(tracker._active_track) == 4

        tracker.handle_coordinator_update()  # same position again
        assert len(tracker._active_track) == 4

    @pytest.mark.asyncio
    async def test_failed_save_does_not_wedge_checkpointing(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        """A DB error during finalize must leave the drive active and retryable."""
        coordinator = MockVehicleCoordinator()
        tracker, store = _make_tracker(mock_hass, analytics_db, coordinator)
        coordinator.set_telemetry(gear="park", odometer_m=100000.0)
        await tracker.async_setup()
        coordinator.set_telemetry(gear="drive", odometer_m=100000.0, speed_mps=10.0)
        coordinator.set_telemetry(gear="drive", odometer_m=108046.72, speed_mps=15.0)

        real_finalize = store.async_finalize_drive

        async def _fail_once(*_args: Any) -> bool:
            store.async_finalize_drive = real_finalize
            raise RuntimeError("database is locked")

        store.async_finalize_drive = _fail_once
        with pytest.raises(RuntimeError):
            await tracker.async_finalize_drive()

        assert tracker._finalizing is False
        assert tracker.active_drive is not None
        drive = await tracker.async_finalize_drive()
        assert drive is not None
        assert store.drive_count == 1


class TestFinalizeWithTrack:
    """Tests that finalize persists the drive and its GPS track together."""

    @pytest.mark.asyncio
    async def test_finalize_writes_drive_and_track_and_clears_checkpoint(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        """Finalizing writes the drive + track atomically and clears the checkpoint."""
        coordinator = MockVehicleCoordinator()
        tracker, store = _make_tracker(mock_hass, analytics_db, coordinator)
        start_odo = 400000.0
        coordinator.set_telemetry(gear="park", odometer_m=start_odo)
        await tracker.async_setup()

        coordinator.set_telemetry(gear="drive", odometer_m=start_odo, speed_mps=10.0)
        coordinator.set_telemetry(
            gear="drive", odometer_m=start_odo + 8046.72, speed_mps=15.0
        )  # 5 miles

        await tracker._async_flush_checkpoint()
        assert (await store.async_load_checkpoint()) is not None

        drive = await tracker.async_finalize_drive()
        assert drive is not None

        track = await store.async_get_track(drive.drive_id)
        assert track is not None
        assert len(track) >= 2

        assert (await store.async_load_checkpoint()) is None


class TestVehicleContextCapture:
    """Tests for range/drive-mode/trailer/driver capture and track-derived stats."""

    @pytest.mark.asyncio
    async def test_context_fields_captured_and_finalized(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        """Range, drive modes, trailer, and driver are captured and land on the record."""
        coordinator = MockVehicleCoordinator()
        tracker, _store = _make_tracker(mock_hass, analytics_db, coordinator)
        start_odo = 700000.0
        coordinator.set_telemetry(gear="park", odometer_m=start_odo)
        await tracker.async_setup()

        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo,
            speed_mps=10.0,
            distance_to_empty_km=300.0,
            drive_mode="everyday",
            trailer_status="not_connected",
            active_driver_name="Kelly",
        )
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo + 8046.72,
            speed_mps=15.0,
            distance_to_empty_km=290.0,
            drive_mode="sport",
            trailer_status="connected",
            active_driver_name="Kelly",
        )

        drive = await tracker.async_finalize_drive()
        assert drive is not None
        assert drive.start_range_mi == pytest.approx(300.0 * 0.621371, rel=1e-3)
        assert drive.end_range_mi == pytest.approx(290.0 * 0.621371, rel=1e-3)
        assert drive.drive_modes == ["All-Purpose", "Sport"]
        assert (
            drive.trailer is True
        )  # sticky True even though "not_connected" came first
        assert drive.driver == "Kelly"

    @pytest.mark.asyncio
    async def test_invalid_context_values_are_ignored(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        """fault/signal_not_available/undefined values never populate context fields."""
        coordinator = MockVehicleCoordinator()
        tracker, _store = _make_tracker(mock_hass, analytics_db, coordinator)
        start_odo = 710000.0
        coordinator.set_telemetry(gear="park", odometer_m=start_odo)
        await tracker.async_setup()

        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo,
            speed_mps=10.0,
            distance_to_empty_km="fault",
            drive_mode="signal_not_available",
            trailer_status="undefined",
            active_driver_name="undefined",
        )

        drive = await tracker.async_finalize_drive()
        assert drive is not None
        assert drive.start_range_mi is None
        assert drive.end_range_mi is None
        assert drive.drive_modes == []
        assert drive.trailer is None
        assert drive.driver is None

    @pytest.mark.asyncio
    async def test_track_derived_stats_populated_on_finalize(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        """Finalize computes and stores track-derived summary stats when a track exists."""
        coordinator = MockVehicleCoordinator()
        tracker, _store = _make_tracker(mock_hass, analytics_db, coordinator)
        start_odo = 720000.0
        coordinator.set_telemetry(gear="park", odometer_m=start_odo)
        await tracker.async_setup()

        now = datetime.now(timezone.utc)
        for i in range(5):
            coordinator.set_telemetry(
                gear="drive",
                odometer_m=start_odo + i * 200.0,
                speed_mps=15.0,
                gps_ts=(now + timedelta(seconds=i * 10)).isoformat(),
            )

        drive = await tracker.async_finalize_drive()
        assert drive is not None
        assert drive.moving_seconds is not None
        assert drive.moving_seconds > 0.0
        assert drive.stopped_seconds == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_no_track_leaves_track_stats_none(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        """Without a usable track (capture off), track-derived stats stay None."""
        coordinator = MockVehicleCoordinator()
        tracker, _store = _make_tracker(
            mock_hass, analytics_db, coordinator, track_capture=False
        )
        start_odo = 730000.0
        coordinator.set_telemetry(gear="park", odometer_m=start_odo)
        await tracker.async_setup()

        coordinator.set_telemetry(gear="drive", odometer_m=start_odo, speed_mps=10.0)
        coordinator.set_telemetry(
            gear="drive", odometer_m=start_odo + 8046.72, speed_mps=15.0
        )

        drive = await tracker.async_finalize_drive()
        assert drive is not None
        assert drive.moving_seconds is None
        assert drive.stop_count is None


class TestCheckpointCadence:
    """Tests for checkpoint scheduling cadence, controlled via the _utcnow() seam."""

    @pytest.mark.asyncio
    async def test_checkpoint_written_after_interval_elapses(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        """No checkpoint is written before CHECKPOINT_INTERVAL_SECONDS of telemetry."""
        coordinator = MockVehicleCoordinator()
        tracker, store = _make_tracker(mock_hass, analytics_db, coordinator)
        start_odo = 500000.0
        coordinator.set_telemetry(gear="park", odometer_m=start_odo)

        base = datetime.now(timezone.utc)
        tracker._utcnow = lambda: base  # type: ignore[method-assign]
        await tracker.async_setup()

        coordinator.set_telemetry(gear="drive", odometer_m=start_odo, speed_mps=10.0)
        await asyncio.sleep(0.02)
        assert (await store.async_load_checkpoint()) is None

        tracker._utcnow = (  # type: ignore[method-assign]
            lambda: base + timedelta(seconds=CHECKPOINT_INTERVAL_SECONDS + 1)
        )
        coordinator.set_telemetry(
            gear="drive", odometer_m=start_odo + 500.0, speed_mps=12.0
        )
        await asyncio.sleep(0.05)

        cp = await store.async_load_checkpoint()
        assert cp is not None
        assert cp.drive_id == f"{TEST_VIN}_{tracker.active_drive['start_epoch']}"


class TestActiveDriveSerialization:
    """Tests for the checkpoint state serialize/deserialize round trip."""

    @pytest.mark.asyncio
    async def test_round_trip_reproduces_equivalent_active_drive(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        """Serializing and deserializing an active drive reproduces its data."""
        coordinator = MockVehicleCoordinator()
        tracker, _store = _make_tracker(mock_hass, analytics_db, coordinator)
        coordinator.set_telemetry(gear="park", odometer_m=100000.0)
        await tracker.async_setup()

        coordinator.set_telemetry(gear="drive", odometer_m=100000.0, speed_mps=10.0)
        active = tracker.active_drive
        assert active is not None

        active["chunks"].append(
            DriveChunk(
                start_time="2026-01-01T00:00:00+00:00",
                duration_seconds=180.0,
                distance_miles=2.0,
                energy_kwh=0.8,
                efficiency_mi_kwh=2.5,
                avg_speed_mph=40.0,
                speed_bin="40-49",
                elevation_change_ft=12.0,
                temp_f=55.0,
            )
        )
        active["weather_samples"].append(
            {
                "timestamp": "2026-01-01T00:00:00+00:00",
                "lat": 39.7,
                "lon": -104.9,
                "temp_f": 60.0,
                "distance_at_sample": 1.0,
            }
        )
        active["speed_bins"]["40-49"] = SpeedBinData(miles=3.5, seconds=240.0)

        active["start_range_mi"] = 250.0
        active["last_range_mi"] = 230.0
        active["drive_modes"] = ["All-Purpose", "Sport"]
        active["trailer"] = True
        active["driver"] = "Kelly"

        state = tracker._serialize_active_drive(active)
        assert state["schema"] == 3

        restored = tracker._deserialize_active_drive(state)

        for key in (
            "vin",
            "start_epoch",
            "start_time_iso",
            "start_odometer_m",
            "last_odometer_m",
            "start_soc",
            "last_soc",
            "battery_capacity_kwh",
            "start_altitude_m",
            "last_altitude_m",
            "start_lat",
            "start_lon",
            "last_lat",
            "last_lon",
            "gps_locked",
            "max_speed_mph",
            "distance_miles",
            "last_weather_sample_distance_mi",
            "current_chunk_start_odo_m",
            "current_chunk_start_soc",
            "current_chunk_start_alt_m",
            "last_fix_raw",
            "start_range_mi",
            "last_range_mi",
            "drive_modes",
            "trailer",
            "driver",
        ):
            assert restored[key] == active[key], key

        assert restored["start_dt"] == active["start_dt"]
        assert restored["last_update_dt"] == active["last_update_dt"]
        assert restored["last_weather_sample_dt"] == active["last_weather_sample_dt"]
        assert restored["current_chunk_start_dt"] == active["current_chunk_start_dt"]

        assert restored["speed_bins"]["40-49"].miles == pytest.approx(3.5)
        assert restored["speed_bins"]["40-49"].seconds == pytest.approx(240.0)

        assert len(restored["chunks"]) == 1
        assert restored["chunks"][0].distance_miles == pytest.approx(2.0)
        assert restored["chunks"][0].speed_bin == "40-49"
        assert restored["chunks"][0].temp_f == pytest.approx(55.0)

        assert restored["weather_samples"] == active["weather_samples"]

    @pytest.mark.asyncio
    async def test_schema_1_checkpoint_deserializes_with_legacy_keys(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        """A schema-1 checkpoint (old segment* keys) must still resume."""
        coordinator = MockVehicleCoordinator()
        tracker, _store = _make_tracker(mock_hass, analytics_db, coordinator)
        coordinator.set_telemetry(gear="park", odometer_m=100000.0)
        await tracker.async_setup()

        coordinator.set_telemetry(gear="drive", odometer_m=100000.0, speed_mps=10.0)
        active = tracker.active_drive
        assert active is not None

        state = tracker._serialize_active_drive(active)
        # Rewrite as a schema-1 checkpoint using the pre-rename key names.
        legacy_state = dict(state)
        del legacy_state["chunks"]
        del legacy_state["current_chunk_start_dt"]
        del legacy_state["current_chunk_start_odo_m"]
        del legacy_state["current_chunk_start_soc"]
        del legacy_state["current_chunk_start_alt_m"]
        del legacy_state["current_chunk_speeds"]
        legacy_state["schema"] = 1
        legacy_state["segments"] = [
            {
                "start_time": "2026-01-01T00:00:00+00:00",
                "duration_seconds": 180.0,
                "distance_miles": 2.0,
                "energy_kwh": 0.8,
                "efficiency_mi_kwh": 2.5,
                "avg_speed_mph": 40.0,
                "speed_bin": "40-49",
                "elevation_change_ft": 12.0,
                "temp_f": 55.0,
            }
        ]
        legacy_state["current_segment_start_dt"] = state["current_chunk_start_dt"]
        legacy_state["current_segment_start_odo_m"] = state["current_chunk_start_odo_m"]
        legacy_state["current_segment_start_soc"] = state["current_chunk_start_soc"]
        legacy_state["current_segment_start_alt_m"] = state["current_chunk_start_alt_m"]
        legacy_state["current_segment_speeds"] = state["current_chunk_speeds"]

        restored = tracker._deserialize_active_drive(legacy_state)

        assert restored["current_chunk_start_dt"] == active["current_chunk_start_dt"]
        assert (
            restored["current_chunk_start_odo_m"] == active["current_chunk_start_odo_m"]
        )
        assert len(restored["chunks"]) == 1
        assert restored["chunks"][0].distance_miles == pytest.approx(2.0)
        assert restored["chunks"][0].speed_bin == "40-49"

    @pytest.mark.asyncio
    async def test_schema_2_checkpoint_deserializes_with_context_keys_defaulted(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        """A schema-2 checkpoint (pre-context-capture) resumes with context keys None/empty."""
        coordinator = MockVehicleCoordinator()
        tracker, _store = _make_tracker(mock_hass, analytics_db, coordinator)
        coordinator.set_telemetry(gear="park", odometer_m=100000.0)
        await tracker.async_setup()

        coordinator.set_telemetry(gear="drive", odometer_m=100000.0, speed_mps=10.0)
        active = tracker.active_drive
        assert active is not None

        state = tracker._serialize_active_drive(active)
        legacy_state = dict(state)
        legacy_state["schema"] = 2
        for key in (
            "start_range_mi",
            "last_range_mi",
            "drive_modes",
            "trailer",
            "driver",
        ):
            del legacy_state[key]

        restored = tracker._deserialize_active_drive(legacy_state)
        assert restored["start_range_mi"] is None
        assert restored["last_range_mi"] is None
        assert restored["drive_modes"] == []
        assert restored["trailer"] is None
        assert restored["driver"] is None


class TestCheckpointRestore:
    """Tests for resuming or finalizing an in-progress drive from a checkpoint."""

    @pytest.mark.asyncio
    async def test_resume_continues_same_drive_across_restart(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        """A fresh DriveTracker resumes an unfinished drive left by a 'crashed' one."""
        coordinator_a = MockVehicleCoordinator()
        tracker_a, store_a = _make_tracker(mock_hass, analytics_db, coordinator_a)
        start_odo = 600000.0
        coordinator_a.set_telemetry(gear="park", odometer_m=start_odo, battery_soc=80.0)
        await tracker_a.async_setup()

        coordinator_a.set_telemetry(
            gear="drive", odometer_m=start_odo, battery_soc=80.0, speed_mps=10.0
        )
        coordinator_a.set_telemetry(
            gear="drive",
            odometer_m=start_odo + 5000.0,
            battery_soc=79.0,
            speed_mps=15.0,
        )
        await tracker_a._async_flush_checkpoint()

        active_a = tracker_a.active_drive
        assert active_a is not None
        drive_id = f"{TEST_VIN}_{active_a['start_epoch']}"

        cp = await store_a.async_load_checkpoint()
        assert cp is not None
        assert cp.drive_id == drive_id
        assert len(cp.track) >= 1

        # Simulate a crash: tracker_a is simply abandoned without finalizing,
        # and a brand-new DriveTracker (post-restart) picks up the same VIN.
        coordinator_b = MockVehicleCoordinator()
        coordinator_b.set_telemetry(
            gear="drive",
            odometer_m=start_odo + 5000.0,
            battery_soc=79.0,
            speed_mps=12.0,
        )
        tracker_b, store_b = _make_tracker(mock_hass, analytics_db, coordinator_b)
        await tracker_b.async_setup()

        assert tracker_b.is_driving is True
        assert tracker_b.active_drive is not None
        assert tracker_b.active_drive["start_epoch"] == active_a["start_epoch"]
        assert isinstance(tracker_b._active_track, DriveTrack)
        assert len(tracker_b._active_track) >= 1

        # Continue driving, then park and finalize.
        coordinator_b.set_telemetry(
            gear="drive",
            odometer_m=start_odo + 20000.0,
            battery_soc=76.0,
            speed_mps=15.0,
        )
        coordinator_b.set_telemetry(
            gear="park",
            odometer_m=start_odo + 20000.0,
            battery_soc=76.0,
            speed_mps=0.0,
        )

        drive = await tracker_b.async_finalize_drive()
        assert drive is not None
        assert drive.drive_id == drive_id

        track = await store_b.async_get_track(drive_id)
        assert track is not None
        assert len(track) >= 2
        times = [p.t for p in track.points]
        assert times == sorted(times)

    @pytest.mark.asyncio
    async def test_finalize_stale_checkpoint_uses_checkpoint_state(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        """A checkpoint older than RESUME_MAX_AGE_SECONDS is finalized, not resumed."""
        coordinator_a = MockVehicleCoordinator()
        tracker_a, _store_a = _make_tracker(mock_hass, analytics_db, coordinator_a)
        start_odo = 700000.0
        coordinator_a.set_telemetry(gear="park", odometer_m=start_odo, battery_soc=80.0)
        await tracker_a.async_setup()

        coordinator_a.set_telemetry(
            gear="drive", odometer_m=start_odo, battery_soc=80.0, speed_mps=10.0
        )
        coordinator_a.set_telemetry(
            gear="drive",
            odometer_m=start_odo + 8046.72,  # 5 miles
            battery_soc=77.0,
            speed_mps=15.0,
        )
        await tracker_a._async_flush_checkpoint()
        drive_id = f"{TEST_VIN}_{tracker_a.active_drive['start_epoch']}"

        # Back-date the checkpoint's updated_ts to simulate a long-dead process.
        old_ts = datetime.now(timezone.utc).timestamp() - (
            RESUME_MAX_AGE_SECONDS + 120.0
        )
        analytics_db._conn.execute(
            "UPDATE active_drive SET updated_ts = ? WHERE vin = ?",
            (old_ts, TEST_VIN),
        )

        # A new tracker starts up post-restart with the vehicle now parked;
        # the checkpoint's age alone (regardless of gear) forces finalize.
        coordinator_b = MockVehicleCoordinator()
        coordinator_b.set_telemetry(
            gear="park",
            odometer_m=start_odo + 8046.72,
            battery_soc=90.0,  # current SoC differs from the checkpoint on purpose
            speed_mps=0.0,
        )
        tracker_b, store_b = _make_tracker(mock_hass, analytics_db, coordinator_b)
        await tracker_b.async_setup()

        assert tracker_b.is_driving is False
        assert tracker_b.active_drive is None
        assert (await store_b.async_load_checkpoint()) is None

        finalized = store_b.last_drive
        assert finalized is not None
        assert finalized.drive_id == drive_id
        assert finalized.distance_miles == pytest.approx(5.0, rel=1e-2)
        # Uses the checkpointed SoC (77.0), not the live coordinator's 90.0.
        assert finalized.end_soc == pytest.approx(77.0, rel=1e-2)

        # The next drive starting shortly after must not record a bogus
        # vampire-drain event spanning the "lost" downtime.
        coordinator_b.set_telemetry(
            gear="drive",
            odometer_m=start_odo + 8046.72,
            battery_soc=90.0,
            speed_mps=10.0,
        )
        await asyncio.sleep(0.05)
        assert store_b.recent_vampire_events == []

    @pytest.mark.asyncio
    async def test_finalize_parked_checkpoint_regardless_of_age(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        """A fresh checkpoint is still finalized (not resumed) if the vehicle is parked."""
        coordinator_a = MockVehicleCoordinator()
        tracker_a, _store_a = _make_tracker(mock_hass, analytics_db, coordinator_a)
        start_odo = 800000.0
        coordinator_a.set_telemetry(gear="park", odometer_m=start_odo, battery_soc=80.0)
        await tracker_a.async_setup()

        coordinator_a.set_telemetry(
            gear="drive", odometer_m=start_odo, battery_soc=80.0, speed_mps=10.0
        )
        coordinator_a.set_telemetry(
            gear="drive",
            odometer_m=start_odo + 3218.688,  # 2 miles
            battery_soc=78.0,
            speed_mps=12.0,
        )
        await tracker_a._async_flush_checkpoint()
        drive_id = f"{TEST_VIN}_{tracker_a.active_drive['start_epoch']}"

        # Checkpoint is fresh (age ~0), but the vehicle now reports "park".
        coordinator_b = MockVehicleCoordinator()
        coordinator_b.set_telemetry(
            gear="park",
            odometer_m=start_odo + 3218.688,
            battery_soc=78.0,
            speed_mps=0.0,
        )
        tracker_b, store_b = _make_tracker(mock_hass, analytics_db, coordinator_b)
        await tracker_b.async_setup()

        assert tracker_b.is_driving is False
        assert tracker_b.active_drive is None
        assert (await store_b.async_load_checkpoint()) is None
        assert store_b.last_drive is not None
        assert store_b.last_drive.drive_id == drive_id

    @pytest.mark.asyncio
    async def test_unreadable_checkpoint_is_discarded_without_failing_setup(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        """A checkpoint whose state can't be deserialized is dropped, not raised."""
        coordinator = MockVehicleCoordinator()
        coordinator.set_telemetry(gear="park", odometer_m=1000.0, battery_soc=80.0)
        tracker, store = _make_tracker(mock_hass, analytics_db, coordinator)
        await store.async_save_checkpoint(
            f"{TEST_VIN}_123", {"schema": 99, "vin": TEST_VIN}, None, 0
        )

        await tracker.async_setup()

        assert tracker.active_drive is None
        assert (await store.async_load_checkpoint()) is None


class TestUnloadFlush:
    """Tests that unloading a tracker with an active drive flushes a checkpoint."""

    @pytest.mark.asyncio
    async def test_unload_flushes_checkpoint(
        self, mock_hass: Any, analytics_db: Any
    ) -> None:
        """async_unload persists a final checkpoint for an in-progress drive."""
        coordinator = MockVehicleCoordinator()
        tracker, store = _make_tracker(mock_hass, analytics_db, coordinator)
        start_odo = 900000.0
        coordinator.set_telemetry(gear="park", odometer_m=start_odo)
        await tracker.async_setup()

        coordinator.set_telemetry(gear="drive", odometer_m=start_odo, speed_mps=10.0)
        coordinator.set_telemetry(
            gear="drive", odometer_m=start_odo + 3000.0, speed_mps=12.0
        )
        assert (await store.async_load_checkpoint()) is None

        active = tracker.active_drive
        assert active is not None
        drive_id = f"{TEST_VIN}_{active['start_epoch']}"

        await tracker.async_unload()

        cp = await store.async_load_checkpoint()
        assert cp is not None
        assert cp.drive_id == drive_id
