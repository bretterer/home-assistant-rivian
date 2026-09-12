"""Unit tests for Rivian real-time drive tracker lifecycle and telemetry engine."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.rivian.drive_models import MPGE_FACTOR, DriveState, DriveStatus
from custom_components.rivian.drive_storage import DriveStore
from custom_components.rivian.drive_tracker import DriveTracker, get_speed_bin_key

TEST_VIN = "7PDSGABA8NN000000"
TEST_VEHICLE_ID = "01894b9f-0000-0000-0000-000000000000"


class MockVehicleCoordinator:
    """Mock VehicleCoordinator providing controllable vehicle telemetry feeds."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self._listeners: list[Any] = []

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
    ) -> None:
        """Update coordinator data and broadcast to listeners."""
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
                "timeStamp": datetime.now(timezone.utc).isoformat(),
            },
            "powerState": {"value": power_state},
        }
        if charger_state is not None:
            self.data["chargerState"] = {"value": charger_state}
        if charger_power is not None:
            self.data["chargerPower"] = {"value": charger_power}
        if battery_temp_f is not None:
            self.data["batteryTemperature"] = {"value": battery_temp_f}
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
        self, mock_hass: Any
    ) -> tuple[DriveTracker, MockVehicleCoordinator, DriveStore, AsyncMock]:
        """Create and initialize a DriveTracker instance with mock coordinator and weather client."""
        coordinator = MockVehicleCoordinator()
        store = DriveStore(mock_hass, TEST_VIN)
        weather_client = AsyncMock()
        weather_client.async_get_current_temperature = AsyncMock(return_value=72.0)

        vehicle_info = {
            "vin": TEST_VIN,
            "id": TEST_VEHICLE_ID,
            "name": "reggie",
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
        assert len(store.drives) == 0

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
        assert len(store.drives) == 1
        assert store.drives[0].drive_id == finalized_record.drive_id


class TestGPSLockGateAndWeatherSampling:
    """Tests for GPS lock gate validation and route weather sampling."""

    @pytest.fixture
    def setup_tracker(
        self, mock_hass: Any
    ) -> tuple[DriveTracker, MockVehicleCoordinator, AsyncMock]:
        """Create and initialize tracker with weather mock."""
        coordinator = MockVehicleCoordinator()
        store = DriveStore(mock_hass, TEST_VIN)
        weather_client = AsyncMock()
        weather_client.async_get_current_temperature = AsyncMock(return_value=68.5)

        vehicle_info = {
            "vin": TEST_VIN,
            "id": TEST_VEHICLE_ID,
            "name": "reggie",
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
        self, mock_hass: Any
    ) -> tuple[DriveTracker, MockVehicleCoordinator]:
        """Create tracker for binning tests."""
        coordinator = MockVehicleCoordinator()
        store = DriveStore(mock_hass, TEST_VIN)
        weather_client = AsyncMock()

        vehicle_info = {
            "vin": TEST_VIN,
            "id": TEST_VEHICLE_ID,
            "name": "reggie",
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
    async def test_state_listener_notifications(self, mock_hass: Any) -> None:
        """Test external listeners receive state update callbacks."""
        coordinator = MockVehicleCoordinator()
        store = DriveStore(mock_hass, TEST_VIN)
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
        tracker.async_add_listener(lambda state: updates.append(state))

        # Shift to drive
        coordinator.set_telemetry(gear="drive", speed_mps=10.0)
        assert len(updates) >= 1
        assert updates[-1].is_driving is True
        assert updates[-1].status == "Driving"

    @pytest.mark.asyncio
    async def test_clean_unload(self, mock_hass: Any) -> None:
        """Test async_unload clears listeners and cancels debounce timer."""
        coordinator = MockVehicleCoordinator()
        store = DriveStore(mock_hass, TEST_VIN)
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
    async def test_idle_time_binning_at_traffic_lights(self, mock_hass: Any) -> None:
        """Test idle time at 0 mph accumulates into 0-9 bin seconds without distance."""
        coordinator = MockVehicleCoordinator()
        store = DriveStore(mock_hass, TEST_VIN)
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
    async def test_neutral_gear_behavior(self, mock_hass: Any) -> None:
        """Test neutral gear while parked vs neutral gear while driving."""
        coordinator = MockVehicleCoordinator()
        store = DriveStore(mock_hass, TEST_VIN)
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
    async def test_repeated_rapid_park_toggles(self, mock_hass: Any) -> None:
        """Test rapid drive -> park -> drive -> park toggling within debounce window."""
        coordinator = MockVehicleCoordinator()
        store = DriveStore(mock_hass, TEST_VIN)
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
        assert len(store.drives) == 1

    @pytest.mark.asyncio
    async def test_weather_sampling_integrated_temp_on_finalize(
        self, mock_hass: Any
    ) -> None:
        """Test finalized drive computes distance-weighted integrated temperature from samples."""
        coordinator = MockVehicleCoordinator()
        store = DriveStore(mock_hass, TEST_VIN)
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
    async def test_dcfc_session_recorded_and_persisted(self, mock_hass: Any) -> None:
        """Verify DC fast charging sessions (>22 kW) are sampled and saved to DriveStore."""
        coordinator = MockVehicleCoordinator()
        store = DriveStore(mock_hass, TEST_VIN)
        tracker = DriveTracker(
            hass=mock_hass,
            entry=MagicMock(),
            coordinator=coordinator,  # type: ignore[arg-type]
            vehicle_info={"vin": TEST_VIN, "id": TEST_VEHICLE_ID, "battery_capacity": 135.0},
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

        # Allow scheduled coroutines to execute in event loop
        await asyncio.sleep(0)

        # Verify persisted DCFC session in DriveStore
        dcfc_sessions = store.get_dcfc_sessions()
        assert len(dcfc_sessions) == 1
        session = dcfc_sessions[0]
        assert session.is_dcfc is True
        assert session.start_soc == 20.0
        assert session.end_soc == 50.0
        assert session.max_power_kw == 150.0
        assert session.avg_power_kw == pytest.approx((150.0 + 135.0 + 100.0) / 3.0, rel=1e-2)
        assert len(session.samples) == 3
        # Energy added: (50 - 20) * 135 / 100 = 40.5 kWh
        assert session.energy_added_kwh == pytest.approx(40.5, rel=1e-2)

    @pytest.mark.asyncio
    async def test_l2_charging_discarded_under_22kw(self, mock_hass: Any) -> None:
        """Verify AC Level 1 / Level 2 charging sessions (<= 22 kW) are discarded."""
        coordinator = MockVehicleCoordinator()
        store = DriveStore(mock_hass, TEST_VIN)
        tracker = DriveTracker(
            hass=mock_hass,
            entry=MagicMock(),
            coordinator=coordinator,  # type: ignore[arg-type]
            vehicle_info={"vin": TEST_VIN, "id": TEST_VEHICLE_ID, "battery_capacity": 135.0},
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

        # Verify nothing persisted to DCFC history
        assert len(store.get_dcfc_sessions()) == 0

    @pytest.mark.asyncio
    async def test_charging_pauses_vampire_drain(self, mock_hass: Any) -> None:
        """Verify active charging resets/pauses vampire drain idle tracking."""
        coordinator = MockVehicleCoordinator()
        store = DriveStore(mock_hass, TEST_VIN)
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
    async def test_dcfc_session_finalized_on_shift_to_drive(self, mock_hass: Any) -> None:
        """Verify active DCFC session is cleanly finalized when shifting into drive."""
        coordinator = MockVehicleCoordinator()
        store = DriveStore(mock_hass, TEST_VIN)
        tracker = DriveTracker(
            hass=mock_hass,
            entry=MagicMock(),
            coordinator=coordinator,  # type: ignore[arg-type]
            vehicle_info={"vin": TEST_VIN, "id": TEST_VEHICLE_ID, "battery_capacity": 135.0},
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

        # Allow scheduled coroutines to execute in event loop
        await asyncio.sleep(0)

        # Verify DCFC session was finalized and recorded
        dcfc_sessions = store.get_dcfc_sessions()
        assert len(dcfc_sessions) == 1
        assert dcfc_sessions[0].start_soc == 30.0
        assert dcfc_sessions[0].end_soc == 75.0
        assert dcfc_sessions[0].max_power_kw == 150.0


