"""Tests for the auto cabin ventilation, Gear Guard streaming and parked energy sensors."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from custom_components.rivian import binary_sensor, const, coordinator

SENSORS = {d.key: d for ds in const.SENSORS.values() for d in ds}
BINARY_SENSORS = {d.key: d for ds in const.BINARY_SENSORS.values() for d in ds}
DAILY_LIMIT_RVM = "gearguard_streaming.privacy.gearguard_streaming_daily_limit"
PARKED_ENERGY_RVM = "energy_edge_compute.graphs.parked_energy_distributions"


@pytest.fixture
def coor() -> coordinator.VehicleCoordinator:
    """A vehicle coordinator whose data updates are kept."""
    coor = coordinator.VehicleCoordinator(
        hass=MagicMock(), config_entry=MagicMock(), client=MagicMock(), vehicle_id="V"
    )
    coor.data = {}

    def set_data(data: dict[str, Any]) -> None:
        coor.data = data

    coor.async_set_updated_data = set_data
    coor.charging_coordinator = MagicMock()
    return coor


def parallax(
    coor: coordinator.VehicleCoordinator, rvm: str, decoded: dict[str, Any]
) -> None:
    """Deliver a Parallax message that decodes to `decoded`."""
    message = {
        "payload": {
            "data": {"parallaxMessages": {"rvm": rvm, "payload": "", "timestamp": 0}}
        }
    }
    decoded = {"timestamp": datetime(2026, 10, 12, tzinfo=UTC)} | decoded
    with patch.object(coordinator, "decode_parallax_message", return_value=decoded):
        coor._process_parallax_data(message)


def sensor_value(coor: coordinator.VehicleCoordinator, key: str) -> Any:
    """Return a sensor's value, as the sensor entity converts it."""
    description = SENSORS[key]
    return description.value_lambda(coor.get(description.field))


@pytest.mark.parametrize("enabled", [True, False])
def test_cabin_ventilation(coor: coordinator.VehicleCoordinator, enabled: bool) -> None:
    """The binary sensor is on while auto cabin ventilation is enabled."""
    parallax(
        coor,
        "comfort.cabin.cabin_ventilation_setting",
        {"cabinVentilationEnabled": enabled},
    )
    entity = binary_sensor.RivianBinarySensorEntity(
        coor,
        MagicMock(),
        BINARY_SENSORS["cabin_ventilation_enabled"],
        {"vin": "VIN", "id": "V", "name": "R1T", "model": "R1T"},
    )

    assert entity.is_on is enabled


@pytest.mark.parametrize(
    ("consent", "state"),
    [
        ("unknown", "not_set"),
        ("not_applicable", "not_applicable"),
        ("consented", "consented"),
        ("not_consented", "not_consented"),
    ],
)
def test_gear_guard_streaming_consent(
    coor: coordinator.VehicleCoordinator, consent: str, state: str
) -> None:
    """The consent is one of the sensor's states, never HA's own "unknown"."""
    parallax(
        coor,
        "gearguard_streaming.privacy.gearguard_streaming_in_vehicle_consent",
        {"gearGuardStreamingConsent": consent},
    )

    assert sensor_value(coor, "gear_guard_streaming_consent") == state
    assert state in SENSORS["gear_guard_streaming_consent"].options
    assert "unknown" not in SENSORS["gear_guard_streaming_consent"].options


def test_gear_guard_streaming_limit(coor: coordinator.VehicleCoordinator) -> None:
    """The reset time (epoch seconds) is a UTC time, and cleared once not hit."""
    parallax(
        coor,
        DAILY_LIMIT_RVM,
        {
            "gearGuardStreamingDailyLimit": "hit",
            "gearGuardStreamingLimitResetTime": 1760227200,
        },
    )

    assert coor.get("gearGuardStreamingDailyLimit") == "hit"
    assert sensor_value(coor, "gear_guard_streaming_limit_reset_time") == datetime(
        2025, 10, 12, tzinfo=UTC
    )

    parallax(coor, DAILY_LIMIT_RVM, {"gearGuardStreamingDailyLimit": "not_hit"})

    assert coor.get("gearGuardStreamingDailyLimit") == "not_hit"
    assert coor.get("gearGuardStreamingLimitResetTime") is None
    assert {"hit", "not_hit"} <= set(
        SENSORS["gear_guard_streaming_daily_limit"].options
    )


def test_parked_energy(coor: coordinator.VehicleCoordinator) -> None:
    """Each window's total energy is used, from the decoded window."""
    parallax(
        coor,
        PARKED_ENERGY_RVM,
        {
            "parkedEnergyLast24Hours": {
                "totalEnergy": 6.1,
                "thermalEnergy": 3.1,
                "duration": 1440,
            },
            "parkedEnergyLast8Hours": {
                "totalEnergy": 4.0,
                "thermalEnergy": 2.8,
                "duration": 480,
            },
            "parkedEnergyLastParkSession": {
                "totalEnergy": 0.3,
                "thermalEnergy": 0.2,
                "duration": 57,
            },
        },
    )

    assert sensor_value(coor, "parked_energy_last_24_hours") == 6.1
    assert sensor_value(coor, "parked_energy_last_8_hours") == 4.0
    assert sensor_value(coor, "parked_energy_park_session") == 0.3


@pytest.mark.parametrize(
    "key",
    [
        "parked_energy_last_24_hours",
        "parked_energy_last_8_hours",
        "parked_energy_park_session",
    ],
)
def test_parked_energy_is_not_an_energy_meter(key: str) -> None:
    """A window's total goes down as well as up, so it's a plain measurement."""
    description = SENSORS[key]

    assert description.device_class is None
    assert description.state_class == const.SensorStateClass.MEASUREMENT
