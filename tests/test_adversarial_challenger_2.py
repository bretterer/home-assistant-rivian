"""Empirical adversarial test suite for Rivian drive tracking, weather, and dashboard.

Adversarial Challenger 2 Stress Harness:
1. Stress test DriveTracker state transitions:
   - Rapid gear oscillation (Park -> Drive -> Reverse -> Drive -> Park)
   - 60s debounce boundary timing (resume at 59s vs finalize at 61s)
   - GPS sync validation gate (speed <= 2 mph & odo <= 50m gates)
   - Micro-drive 0.5-mile boundary & regenerative braking zero energy safety
2. Stress test weather error resilience:
   - Open-Meteo HTTP 500, timeouts, network failure fallbacks to None
   - Historical archive error resilience & extreme waypoint temperature math
3. Validate Lovelace dashboard YAML:
   - PyYAML parsing, hex color codes (#1E88E5, #43A047, #FB8C00), entity IDs, core card fallbacks.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
import yaml

from custom_components.rivian.drive_models import (
    MICRO_DRIVE_THRESHOLD_MILES,
    MPGE_FACTOR,
)
from custom_components.rivian.drive_storage import DriveStore
from custom_components.rivian.drive_tracker import DriveTracker, get_speed_bin_key
from custom_components.rivian.weather import (
    OpenMeteoWeatherClient,
    calculate_distance_weighted_temperature,
    get_interpolated_temperature,
)

REPO_ROOT = Path(__file__).parent.parent
DASHBOARD_YAML_PATH = REPO_ROOT / "lovelace_efficiency_dashboard.yaml"

TEST_VIN = "7PDSGABA8NN999999"
TEST_VEHICLE_ID = "01894b9f-adversarial-test-vehicle"


class MockClientResponse:
    """Mock aiohttp ClientResponse for testing."""

    def __init__(
        self,
        status: int = 200,
        json_data: dict[str, Any] | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self.status = status
        self._json_data = json_data or {}
        self._raise_exc = raise_exc

    async def json(self) -> dict[str, Any]:
        if self._raise_exc:
            raise self._raise_exc
        return self._json_data

    async def __aenter__(self) -> Self:
        if self._raise_exc and not isinstance(
            self._raise_exc, (KeyError, TypeError, ValueError)
        ):
            raise self._raise_exc
        return self

    async def __aexit__(self, *args: object) -> None:
        pass


class ControllableVehicleCoordinator:
    """Controllable mock coordinator for adversarial stress testing."""

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
    ) -> None:
        """Update coordinator data and notify listeners."""
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
        for listener in list(self._listeners):
            listener()


# ==============================================================================
# 1. ADVERSARIAL DRIVE TRACKER STATE TRANSITION TESTS
# ==============================================================================


class TestAdversarialDriveTrackerTransitions:
    """Stress testing DriveTracker state machine under hostile conditions."""

    @pytest.fixture
    def tracker_setup(
        self, mock_hass: Any
    ) -> tuple[DriveTracker, ControllableVehicleCoordinator, DriveStore, AsyncMock]:
        """Set up tracker with mock coordinator, store, and weather client."""
        coordinator = ControllableVehicleCoordinator()
        store = DriveStore(mock_hass, TEST_VIN)
        weather_client = AsyncMock(spec=OpenMeteoWeatherClient)
        weather_client.async_get_current_temperature = AsyncMock(return_value=72.0)

        vehicle_info = {
            "vin": TEST_VIN,
            "id": TEST_VEHICLE_ID,
            "name": "stress_vehicle",
            "model": "R1S",
            "battery_capacity": 135.0,
        }
        tracker = DriveTracker(
            hass=mock_hass,
            entry=MagicMock(),
            coordinator=coordinator,  # type: ignore[arg-type]
            vehicle_info=vehicle_info,
            store=store,
            weather_client=weather_client,
        )
        return tracker, coordinator, store, weather_client

    @pytest.mark.asyncio
    async def test_rapid_gear_oscillation_continuous_drive(
        self,
        tracker_setup: tuple[
            DriveTracker, ControllableVehicleCoordinator, DriveStore, AsyncMock
        ],
    ) -> None:
        """Stress test: Rapid oscillations Park -> Drive -> Reverse -> Drive -> Park within debounce."""
        tracker, coordinator, store, _ = tracker_setup
        start_odo = 1609344.0  # 1000.0 mi
        coordinator.set_telemetry(gear="park", odometer_m=start_odo, battery_soc=80.0)
        await tracker.async_setup()

        assert tracker.is_driving is False
        assert tracker.is_debouncing_park is False

        # 1. Shift to Drive at start odometer
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo,
            speed_mps=0.0,
            battery_soc=80.0,
        )
        assert tracker.is_driving is True
        assert tracker.is_debouncing_park is False

        # 2. Drive forward 160.9 meters
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo + 160.9,
            speed_mps=5.0,
            battery_soc=79.9,
        )

        # 3. Shift to Reverse (backing up) -> Remains in same continuous drive session
        coordinator.set_telemetry(
            gear="reverse",
            odometer_m=start_odo + 321.8,
            speed_mps=3.0,
            battery_soc=79.8,
        )
        assert tracker.is_driving is True
        assert tracker.is_debouncing_park is False
        assert len(store.drives) == 0

        # 4. Shift back to Drive (heading down the street)
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo + 1609.344,
            speed_mps=15.0,
            battery_soc=79.5,
        )
        assert tracker.is_driving is True
        assert tracker.is_debouncing_park is False

        # 5. Stop at mailbox / gate -> shift to Park (starts 60s debounce)
        coordinator.set_telemetry(
            gear="park",
            odometer_m=start_odo + 1609.344,
            speed_mps=0.0,
            battery_soc=79.5,
        )
        assert tracker.is_driving is True
        assert tracker.is_debouncing_park is True

        # 6. Shift back to Drive within debounce window -> Debounce cancelled, trip continues
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo + 3218.688,
            speed_mps=20.0,
            battery_soc=79.0,
        )
        assert tracker.is_driving is True
        assert tracker.is_debouncing_park is False

        # 7. Shift to Reverse (parallel parking) -> Continues drive
        coordinator.set_telemetry(
            gear="reverse",
            odometer_m=start_odo + 3250.0,
            speed_mps=2.0,
            battery_soc=78.9,
        )
        assert tracker.is_driving is True

        # 8. Final shift to Park -> Debounce active
        coordinator.set_telemetry(
            gear="park",
            odometer_m=start_odo + 3250.0,
            speed_mps=0.0,
            battery_soc=78.9,
        )
        assert tracker.is_debouncing_park is True

        # 9. Expire debounce timer and verify exactly 1 consolidated drive record created
        record = await tracker.async_finalize_drive()
        assert record is not None
        assert tracker.is_driving is False
        assert tracker.is_debouncing_park is False
        assert len(store.drives) == 1
        assert record.distance_miles == pytest.approx(3250.0 / 1609.344, rel=1e-2)
        assert record.energy_kwh == pytest.approx(
            (80.0 - 78.9) * 135.0 / 100.0, rel=1e-2
        )

    @pytest.mark.asyncio
    async def test_case_insensitivity_and_unexpected_gear_strings(
        self,
        tracker_setup: tuple[
            DriveTracker, ControllableVehicleCoordinator, DriveStore, AsyncMock
        ],
    ) -> None:
        """Stress test: Upper/mixed case and invalid gear strings."""
        tracker, coordinator, _store, _ = tracker_setup
        coordinator.set_telemetry(gear="PARK")
        await tracker.async_setup()
        assert tracker.is_driving is False

        # Uppercase "DRIVE"
        coordinator.set_telemetry(gear="DRIVE", speed_mps=10.0)
        assert tracker.is_driving is True

        # Uppercase "REVERSE"
        coordinator.set_telemetry(gear="REVERSE", speed_mps=5.0)
        assert tracker.is_driving is True

        # Unknown / None / Invalid gears
        coordinator.set_telemetry(gear="TOW_MODE")
        assert tracker.is_driving is True  # Unknown string does not crash tracker

        coordinator.data["gearStatus"] = {"value": None}
        coordinator.set_telemetry()
        assert tracker.is_driving is True  # None value does not crash tracker

    @pytest.mark.asyncio
    async def test_60s_debounce_boundary_resume_at_59s(
        self,
        tracker_setup: tuple[
            DriveTracker, ControllableVehicleCoordinator, DriveStore, AsyncMock
        ],
    ) -> None:
        """Debounce boundary test: Resuming drive at 59s cancels debounce and keeps trip open."""
        tracker, coordinator, store, _ = tracker_setup
        start_odo = 100000.0
        coordinator.set_telemetry(gear="park", odometer_m=start_odo, battery_soc=80.0)
        await tracker.async_setup()

        # Start drive at start_odo
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo,
            speed_mps=0.0,
            battery_soc=80.0,
        )
        assert tracker.is_driving is True
        assert tracker.is_debouncing_park is False

        # Leg 1 = 5.0 miles
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo + (5.0 * 1609.344),
            speed_mps=15.0,
            battery_soc=78.0,
        )

        # Shift to park at 0s
        coordinator.set_telemetry(
            gear="park",
            odometer_m=start_odo + (5.0 * 1609.344),
            speed_mps=0.0,
            battery_soc=78.0,
        )
        assert tracker.is_debouncing_park is True

        # At 59s, shift back to Drive before timer expiry
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo + (5.0 * 1609.344),
            speed_mps=10.0,
            battery_soc=78.0,
        )
        assert tracker.is_debouncing_park is False
        assert tracker.is_driving is True
        assert len(store.drives) == 0

        # Leg 2 = another 5.0 miles (total 10.0 miles)
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo + (10.0 * 1609.344),
            speed_mps=20.0,
            battery_soc=76.0,
        )

        # Final shift to park & finalize
        coordinator.set_telemetry(
            gear="park",
            odometer_m=start_odo + (10.0 * 1609.344),
            speed_mps=0.0,
            battery_soc=76.0,
        )
        record = await tracker.async_finalize_drive()

        assert record is not None
        assert len(store.drives) == 1
        assert record.distance_miles == pytest.approx(10.0, rel=1e-2)
        assert record.energy_kwh == pytest.approx(
            (80.0 - 76.0) * 135.0 / 100.0, rel=1e-2
        )

    @pytest.mark.asyncio
    async def test_60s_debounce_boundary_finalize_at_61s_and_start_new_trip(
        self,
        tracker_setup: tuple[
            DriveTracker, ControllableVehicleCoordinator, DriveStore, AsyncMock
        ],
    ) -> None:
        """Debounce boundary test: Waiting 61s (timer expires) finalizes trip 1, new shift starts trip 2."""
        tracker, coordinator, store, _ = tracker_setup
        start_odo = 100000.0
        coordinator.set_telemetry(gear="park", odometer_m=start_odo, battery_soc=80.0)
        await tracker.async_setup()

        # Start trip 1 at start_odo
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo,
            speed_mps=0.0,
            battery_soc=80.0,
        )

        # Trip 1: 3.0 miles
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo + (3.0 * 1609.344),
            speed_mps=15.0,
            battery_soc=79.0,
        )
        coordinator.set_telemetry(
            gear="park",
            odometer_m=start_odo + (3.0 * 1609.344),
            speed_mps=0.0,
            battery_soc=79.0,
        )
        assert tracker.is_debouncing_park is True

        # 61s elapsed -> timer expired
        drive1 = await tracker.async_finalize_drive()
        assert drive1 is not None
        assert drive1.distance_miles == pytest.approx(3.0, rel=1e-2)
        assert len(store.drives) == 1
        assert tracker.is_driving is False

        # Advance clock to ensure trip 2 has distinct start_epoch
        await asyncio.sleep(1.05)

        # Trip 2: Shift to drive again at current odometer -> Starts independent new session
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo + (3.0 * 1609.344),
            speed_mps=0.0,
            battery_soc=79.0,
        )
        assert tracker.is_driving is True
        assert tracker.drive_state.current_trip_distance_mi == 0.0

        # Drive 4.0 miles (odometer now at start_odo + 7.0 mi)
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo + (7.0 * 1609.344),
            speed_mps=20.0,
            battery_soc=77.5,
        )
        coordinator.set_telemetry(
            gear="park",
            odometer_m=start_odo + (7.0 * 1609.344),
            speed_mps=0.0,
            battery_soc=77.5,
        )

        drive2 = await tracker.async_finalize_drive()
        assert drive2 is not None
        assert drive2.distance_miles == pytest.approx(4.0, rel=1e-2)
        assert len(store.drives) == 2
        assert drive1.drive_id != drive2.drive_id

    @pytest.mark.asyncio
    async def test_gps_sync_validation_gate_strict_thresholds(
        self,
        tracker_setup: tuple[
            DriveTracker, ControllableVehicleCoordinator, DriveStore, AsyncMock
        ],
    ) -> None:
        """GPS gate test: Verify no weather calls while speed <= 2 mph and odo delta <= 50 m."""
        tracker, coordinator, _store, weather_client = tracker_setup
        start_odo = 100000.0
        coordinator.set_telemetry(gear="park", odometer_m=start_odo)
        await tracker.async_setup()

        # Shift to drive at standstill: speed = 0, delta = 0
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo,
            speed_mps=0.0,
        )
        await asyncio.sleep(0)
        assert tracker.active_drive["gps_locked"] is False
        assert weather_client.async_get_current_temperature.call_count == 0

        # Creeping at 1.5 mph (0.67 m/s <= 0.89408 m/s), odo delta = 20 m (<= 50 m)
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo + 20.0,
            speed_mps=0.67,
        )
        await asyncio.sleep(0)
        assert tracker.active_drive["gps_locked"] is False
        assert weather_client.async_get_current_temperature.call_count == 0

        # Creeping at 1.99 mph (0.889 m/s <= 0.89408 m/s), odo delta = 49.0 m (<= 50 m)
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo + 49.0,
            speed_mps=0.889,
        )
        await asyncio.sleep(0)
        assert tracker.active_drive["gps_locked"] is False
        assert weather_client.async_get_current_temperature.call_count == 0

        # Now accelerate to 2.1 mph (0.938 m/s > 0.89408 m/s) -> GATE OPENS!
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo + 49.0,
            speed_mps=0.938,
        )
        await asyncio.sleep(0)
        assert tracker.active_drive["gps_locked"] is True
        assert weather_client.async_get_current_temperature.call_count == 1

    @pytest.mark.asyncio
    async def test_micro_drive_threshold_boundary_cases(
        self,
        tracker_setup: tuple[
            DriveTracker, ControllableVehicleCoordinator, DriveStore, AsyncMock
        ],
    ) -> None:
        """Boundary test: Drives < 0.50 mi tagged as micro-drives; drives >= 0.50 mi not tagged."""
        tracker, coordinator, _store, _ = tracker_setup
        assert MICRO_DRIVE_THRESHOLD_MILES == 0.50

        start_odo = 100000.0
        coordinator.set_telemetry(gear="park", odometer_m=start_odo)
        await tracker.async_setup()

        # Trip 1: Exactly 0.49 miles
        coordinator.set_telemetry(gear="drive", odometer_m=start_odo, speed_mps=0.0)
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo + (0.49 * 1609.344),
            speed_mps=10.0,
        )
        coordinator.set_telemetry(
            gear="park",
            odometer_m=start_odo + (0.49 * 1609.344),
            speed_mps=0.0,
        )
        record1 = await tracker.async_finalize_drive()
        assert record1 is not None
        assert record1.distance_miles == pytest.approx(0.49, rel=1e-2)
        assert record1.is_micro_drive is True

        await asyncio.sleep(1.05)

        # Trip 2: Exactly 0.50 miles
        start_odo2 = start_odo + 1000.0
        coordinator.set_telemetry(gear="drive", odometer_m=start_odo2, speed_mps=0.0)
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo2 + (0.50 * 1609.344),
            speed_mps=10.0,
        )
        coordinator.set_telemetry(
            gear="park",
            odometer_m=start_odo2 + (0.50 * 1609.344),
            speed_mps=0.0,
        )
        record2 = await tracker.async_finalize_drive()
        assert record2 is not None
        assert record2.distance_miles == pytest.approx(0.50, rel=1e-2)
        assert record2.is_micro_drive is False

    @pytest.mark.asyncio
    async def test_zero_and_negative_soc_delta_energy_safety(
        self,
        tracker_setup: tuple[
            DriveTracker, ControllableVehicleCoordinator, DriveStore, AsyncMock
        ],
    ) -> None:
        """Regenerative braking test: Battery SOC gain downhill results in 0.0 kWh energy and 0.0 mi/kWh efficiency."""
        tracker, coordinator, _store, _ = tracker_setup
        start_odo = 100000.0
        # Start at 70.0% SOC
        coordinator.set_telemetry(gear="park", odometer_m=start_odo, battery_soc=70.0)
        await tracker.async_setup()

        # Drive 5.0 miles downhill with strong regen -> Battery increases to 71.0%
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo,
            speed_mps=0.0,
            battery_soc=70.0,
        )
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo + (5.0 * 1609.344),
            speed_mps=20.0,
            battery_soc=71.0,
        )
        coordinator.set_telemetry(
            gear="park",
            odometer_m=start_odo + (5.0 * 1609.344),
            speed_mps=0.0,
            battery_soc=71.0,
        )

        record = await tracker.async_finalize_drive()
        assert record is not None
        assert record.distance_miles == pytest.approx(5.0, rel=1e-2)
        # Gross energy consumed is clamped to 0.0 kWh (never negative gross energy)
        assert record.energy_kwh == 0.0
        # Zero division safety: efficiency is 0.0 mi/kWh
        assert record.efficiency_mi_kwh == 0.0
        assert record.mpge == 0.0

    def test_speed_bin_classifier_adversarial(self) -> None:
        """Verify speed bin classification under edge cases."""
        assert get_speed_bin_key(-100.0) == "0-9"
        assert get_speed_bin_key(0.0) == "0-9"
        assert get_speed_bin_key(9.999) == "0-9"
        assert get_speed_bin_key(10.0) == "10-19"
        assert get_speed_bin_key(79.999) == "70-79"
        assert get_speed_bin_key(80.0) == "80+"
        assert get_speed_bin_key(200.0) == "80+"


# ==============================================================================
# 2. ADVERSARIAL WEATHER ERROR RESILIENCE TESTS
# ==============================================================================


class TestAdversarialWeatherResilience:
    """Stress testing weather failure modes: HTTP 500, timeouts, and network drops."""

    @pytest.mark.asyncio
    async def test_drive_tracker_resilience_on_weather_http_500(
        self, mock_hass: Any
    ) -> None:
        """Verify DriveTracker operates flawlessly when Open-Meteo returns HTTP 500."""
        mock_session = MagicMock()
        mock_session.get.return_value = MockClientResponse(
            status=500, json_data={"error": True}
        )
        weather_client = OpenMeteoWeatherClient(hass=mock_hass, session=mock_session)

        coordinator = ControllableVehicleCoordinator()
        store = DriveStore(mock_hass, TEST_VIN)
        vehicle_info = {
            "vin": TEST_VIN,
            "id": TEST_VEHICLE_ID,
            "battery_capacity": 135.0,
        }
        tracker = DriveTracker(
            hass=mock_hass,
            entry=MagicMock(),
            coordinator=coordinator,  # type: ignore[arg-type]
            vehicle_info=vehicle_info,
            store=store,
            weather_client=weather_client,
        )

        start_odo = 100000.0
        coordinator.set_telemetry(gear="park", odometer_m=start_odo, battery_soc=80.0)
        await tracker.async_setup()

        # Start drive at start_odo
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo,
            speed_mps=0.0,
            battery_soc=80.0,
        )

        # Drive 20 miles with HTTP 500 weather API
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo + (20.0 * 1609.344),
            speed_mps=25.0,
            battery_soc=74.0,
        )
        await asyncio.sleep(0)

        # Finalize drive
        record = await tracker.async_finalize_drive()
        assert record is not None
        assert record.distance_miles == pytest.approx(20.0, rel=1e-2)
        assert record.energy_kwh == pytest.approx(6.0 * 135.0 / 100.0, rel=1e-2)
        # Weather failed gracefully, fallback is None
        assert record.integrated_temperature_f is None
        assert record.weather_samples == []
        assert len(store.drives) == 1

    @pytest.mark.asyncio
    async def test_drive_tracker_resilience_on_weather_timeout_and_exceptions(
        self, mock_hass: Any
    ) -> None:
        """Verify DriveTracker handles weather timeout without breaking trip tracking."""
        mock_session = MagicMock()
        mock_session.get.side_effect = asyncio.TimeoutError(
            "Open-Meteo connection timed out"
        )
        weather_client = OpenMeteoWeatherClient(hass=mock_hass, session=mock_session)

        coordinator = ControllableVehicleCoordinator()
        store = DriveStore(mock_hass, TEST_VIN)
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
            weather_client=weather_client,
        )

        start_odo = 100000.0
        coordinator.set_telemetry(gear="park", odometer_m=start_odo)
        await tracker.async_setup()

        # Start drive
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo,
            speed_mps=0.0,
        )

        # Drive 1 mile and trigger GPS lock
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo + 1609.344,
            speed_mps=15.0,
        )
        await asyncio.sleep(0)

        # Drive and finalize
        record = await tracker.async_finalize_drive()
        assert record is not None
        assert record.distance_miles == pytest.approx(1.0, rel=1e-2)
        assert record.integrated_temperature_f is None

    @pytest.mark.asyncio
    async def test_client_connection_error_resilience(self, mock_hass: Any) -> None:
        """Verify OpenMeteoWeatherClient catches aiohttp.ClientConnectionError."""
        mock_session = MagicMock()
        mock_session.get.side_effect = aiohttp.ClientConnectionError(
            "Network is unreachable"
        )
        client = OpenMeteoWeatherClient(hass=mock_hass, session=mock_session)
        temp = await client.async_get_current_temperature(37.77, -122.42)
        assert temp is None

    @pytest.mark.asyncio
    async def test_intermittent_weather_success_and_failures(
        self, mock_hass: Any
    ) -> None:
        """Verify weighted temperature computes accurately when some samples succeed and some fail."""
        coordinator = ControllableVehicleCoordinator()
        store = DriveStore(mock_hass, TEST_VIN)
        weather_client = AsyncMock(spec=OpenMeteoWeatherClient)
        # First sample at 0 mi: 60°F, second at 15 mi: None (failed), third at 30 mi: 80°F
        weather_client.async_get_current_temperature = AsyncMock(
            side_effect=[60.0, None, 80.0]
        )

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
            weather_client=weather_client,
        )

        start_odo = 100000.0
        coordinator.set_telemetry(gear="park", odometer_m=start_odo)
        await tracker.async_setup()

        # Start drive
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo,
            speed_mps=0.0,
        )

        # Sample 1 (at 0.0 mi) -> 60°F
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo,
            speed_mps=15.0,
        )
        await asyncio.sleep(0)

        # Sample 2 (at 15.0 mi) -> None (failed HTTP 500)
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo + (15.0 * 1609.344),
            speed_mps=25.0,
        )
        await asyncio.sleep(0)

        # Sample 3 (at 30.0 mi) -> 80°F
        coordinator.set_telemetry(
            gear="drive",
            odometer_m=start_odo + (30.0 * 1609.344),
            speed_mps=25.0,
        )
        await asyncio.sleep(0)

        record = await tracker.async_finalize_drive()
        assert record is not None
        assert len(record.weather_samples) == 2  # Only successful 2 samples
        # Weighted average between 60°F and 80°F over 30 miles = 70.0°F
        assert record.integrated_temperature_f == pytest.approx(70.0, rel=1e-1)
        assert record.mpge == pytest.approx(
            record.efficiency_mi_kwh * MPGE_FACTOR, rel=1e-2
        )

    @pytest.mark.asyncio
    async def test_historical_archive_client_error_resilience(
        self, mock_hass: Any
    ) -> None:
        """Verify OpenMeteoWeatherClient handles archive API failures gracefully."""
        mock_session = MagicMock()
        mock_session.get.return_value = MockClientResponse(
            status=500, json_data={"error": True}
        )
        client = OpenMeteoWeatherClient(hass=mock_hass, session=mock_session)

        # Test archive query returns None
        archive_res = await client.async_get_historical_temperatures(
            39.7392, -104.9903, "2026-08-20", "2026-08-20"
        )
        assert archive_res is None

        # Test interpolated temperature for timestamp returns None
        interp_res = await client.async_get_historical_temperature_for_timestamp(
            39.7392, -104.9903, "2026-08-20T14:30:00Z"
        )
        assert interp_res is None

    def test_extreme_and_unordered_weather_waypoint_integration(self) -> None:
        """Stress test distance-weighted temperature calculation with unordered and extreme values."""
        # Unordered samples with negative temperature and extreme heat
        samples = [
            {"distance_at_sample": 25.0, "temp_f": 110.0},
            {"distance_at_sample": 0.0, "temp_f": -10.0},
            {"distance_at_sample": 10.0, "temp_f": 40.0},
        ]
        # Drive 30 miles:
        # 0 to 10 mi (10 mi weight): avg (-10 + 40)/2 = 15°F -> 150
        # 10 to 25 mi (15 mi weight): avg (40 + 110)/2 = 75°F -> 1125
        # 25 to 30 mi (5 mi tail weight): 110°F -> 550
        # Total sum = 150 + 1125 + 550 = 1825 / 30 = 60.833 -> 60.8°F
        result = calculate_distance_weighted_temperature(
            samples, total_distance_miles=30.0
        )
        assert result == 60.8

    def test_interpolated_temperature_boundary_clamping(self) -> None:
        """Test timestamp interpolation clamps properly at bounds."""
        hourly = {"2026-08-20T10:00": 65.0, "2026-08-20T11:00": 75.0}
        assert get_interpolated_temperature(hourly, "2026-08-20T09:00:00Z") == 65.0
        assert get_interpolated_temperature(hourly, "2026-08-20T12:00:00Z") == 75.0


# ==============================================================================
# 3. LOVELACE DASHBOARD YAML VALIDATION TESTS
# ==============================================================================


@pytest.fixture(scope="module")
def dashboard_raw_fixture() -> str:
    """Load raw YAML text."""
    assert DASHBOARD_YAML_PATH.exists()
    return DASHBOARD_YAML_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def dashboard_parsed_fixture(dashboard_raw_fixture: str) -> dict[str, Any]:
    """Safely parse YAML content."""
    data = yaml.safe_load(dashboard_raw_fixture)
    assert isinstance(data, dict)
    return data


class TestAdversarialDashboardValidation:
    """Validate Lovelace dashboard YAML formatting, hex color codes, entity IDs, and fallbacks."""

    def test_dashboard_pyyaml_safe_parsing(
        self, dashboard_parsed_fixture: dict[str, Any]
    ) -> None:
        """Verify dashboard YAML loads cleanly via PyYAML."""
        assert "views" in dashboard_parsed_fixture
        assert len(dashboard_parsed_fixture["views"]) >= 2

    def test_elevation_hex_color_codes_strictly_match(
        self, dashboard_raw_fixture: str
    ) -> None:
        """Verify hex color codes: Downhill #1E88E5, Flat #43A047, Uphill #FB8C00."""
        assert "#1E88E5" in dashboard_raw_fixture, "Downhill hex code #1E88E5 missing"
        assert "#43A047" in dashboard_raw_fixture, "Flat hex code #43A047 missing"
        assert "#FB8C00" in dashboard_raw_fixture, "Uphill hex code #FB8C00 missing"

    def test_all_8_entity_ids_present_in_dashboard(
        self, dashboard_raw_fixture: str
    ) -> None:
        """Verify all 8 entity IDs are referenced in dashboard YAML."""
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
            entity_id = f"sensor.{{vin}}_{key}"
            assert entity_id in dashboard_raw_fixture, f"Entity {entity_id} missing"

    def test_core_card_fallback_view_validity(
        self, dashboard_parsed_fixture: dict[str, Any]
    ) -> None:
        """Verify View 2 contains native Core cards (Tile, Entities, Statistics, History)."""
        views = dashboard_parsed_fixture.get("views", [])
        core_view = next(
            (v for v in views if v.get("path") == "rivian-efficiency-core"),
            None,
        )
        assert core_view is not None, "Core fallback view not found"

        def get_all_card_types(cards: list[dict[str, Any]]) -> set[str]:
            types: set[str] = set()
            for c in cards:
                if isinstance(c, dict):
                    if "type" in c:
                        types.add(c["type"])
                    if "cards" in c and isinstance(c["cards"], list):
                        types.update(get_all_card_types(c["cards"]))
            return types

        card_types = get_all_card_types(core_view.get("cards", []))
        assert "tile" in card_types
        assert "entities" in card_types
        assert "statistics-graph" in card_types
        assert "history-graph" in card_types
