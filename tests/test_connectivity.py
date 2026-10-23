"""Tests for the Wi-Fi and cellular diagnostic sensors."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from custom_components.rivian import const, coordinator

DESCRIPTIONS = {d.key: d for ds in const.SENSORS.values() for d in ds}
INTEGRATION = Path(__file__).resolve().parents[1] / "custom_components" / "rivian"

# What rivian-python-client 2.3.1 decodes vehicle.network.state's enums to
# (_CONNECTIVITY_LEVEL_MAP, _WIFI_SECURITY_MAP and _WPA_STATUS_MAP in
# rivian/parallax/vehicle.py). The suite stubs the client, so they're copied.
CONNECTIVITY_LEVELS = ["level_0", "level_1", "level_2", "level_3", "level_4"]
DECODER_VALUES = {
    "wifi_antenna_bars": CONNECTIVITY_LEVELS,
    "cellular_antenna_bars": CONNECTIVITY_LEVELS,
    "wifi_secure_status": [
        "open",
        "wpa_personal",
        "wpa_enterprise",
        "wpa2_personal",
        "wpa2_enterprise",
        "wpa3_personal",
        "wpa3_enterprise",
        "enhanced_open",
    ],
    "wifi_wpa_status": [
        "not_connected",
        "connected",
        "scanning",
        "connecting",
        "disconnecting",
        "connection_failed",
        "disabled",
        "scan_failure",
    ],
}

# A decoded frame from a vehicle on Wi-Fi, and each sensor's resulting state
NETWORK_STATE = {
    "networkActiveInterface": "wifi",
    "networkAntennaBars": "level_3",
    "wifiWpaStatus": "connected",
    "wifiSsid": "Garage",
    "wifiAntennaBars": "level_3",
    "wifiSignal": -61,
    "wifiLinkSpeed": 260,
    "wifiFreq": 5200,
    "wifiBandwidth": 80,
    "wifiConnStatus": "success",
    "wifiSecured": True,
    "wifiUserEnabled": True,
    "wifiSecureStatus": "wpa2_personal",
    "cellularCarrier": "Carrier",
    "cellularMode": "LTE",
    "cellularAntennaBars": "level_2",
    "cellularSignalStrength": -95,
}
STATES = {
    "wifi_signal": -61,
    "wifi_antenna_bars": "level_3",
    "wifi_frequency": 5200,
    "wifi_link_speed": 260,
    "wifi_secure_status": "wpa2_personal",
    "wifi_wpa_status": "connected",
    "wifi_ssid": "Garage",
    "cellular_antenna_bars": "level_2",
    "cellular_carrier": "Carrier",
    "cellular_mode": "LTE",
    "cellular_signal_strength": -95,
}


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


def network_state(coor: coordinator.VehicleCoordinator, **changes: Any) -> None:
    """Deliver a vehicle.network.state message, with `changes` to the frame."""
    message = {
        "payload": {
            "data": {
                "parallaxMessages": {
                    "rvm": "vehicle.network.state",
                    "payload": "",
                    "timestamp": 0,
                }
            }
        }
    }
    decoded = {"timestamp": datetime(2026, 10, 12, tzinfo=UTC)} | NETWORK_STATE
    with patch.object(
        coordinator, "decode_parallax_message", return_value=decoded | changes
    ):
        coor._process_parallax_data(message)


def state(coor: coordinator.VehicleCoordinator, key: str) -> Any:
    """The sensor's state, computed from its field as the entity does."""
    description = DESCRIPTIONS[key]
    if (value := coor.get(description.field)) is None:
        return None
    return description.value_lambda(value) if description.value_lambda else value


def test_every_sensor_gets_its_value(coor: coordinator.VehicleCoordinator) -> None:
    """Each sensor reads a field of the frame, and is diagnostic."""
    network_state(coor)

    assert {key: state(coor, key) for key in STATES} == STATES
    for key in STATES:
        assert DESCRIPTIONS[key].entity_category == const.EntityCategory.DIAGNOSTIC


def test_ssid_is_disabled_by_default() -> None:
    assert DESCRIPTIONS["wifi_ssid"].entity_registry_enabled_default is False


@pytest.mark.parametrize(
    ("key", "field"),
    [
        ("wifi_signal", "wifiSignal"),
        ("cellular_signal_strength", "cellularSignalStrength"),
    ],
)
def test_no_reading_is_unknown(
    coor: coordinator.VehicleCoordinator, key: str, field: str
) -> None:
    """-255 dBm means there's no reading, not a very weak signal."""
    network_state(coor, **{field: -255})

    assert coor.get(field) == -255
    assert state(coor, key) is None


@pytest.mark.parametrize("freq", [0, 2, 5, 999])
def test_frequency_below_1000_is_unknown(
    coor: coordinator.VehicleCoordinator, freq: int
) -> None:
    """Unset (0) or a band number isn't a frequency, so it isn't recorded as MHz."""
    network_state(coor, wifiFreq=freq)

    assert coor.get("wifiFreq") == freq
    assert state(coor, "wifi_frequency") is None


@pytest.mark.parametrize("freq", [1000, 2437, 5200, 5955])
def test_frequency_is_kept(coor: coordinator.VehicleCoordinator, freq: int) -> None:
    """Real Wi-Fi frequencies pass through unchanged."""
    network_state(coor, wifiFreq=freq)

    assert state(coor, "wifi_frequency") == freq


@pytest.mark.parametrize(("key", "values"), DECODER_VALUES.items())
def test_options_are_the_decoder_values(key: str, values: list[str]) -> None:
    """Every value the decoder can emit is a known, translated state."""
    options = DESCRIPTIONS[key].options
    assert sorted(options) == sorted(values)
    for path in ("strings.json", "translations/en.json"):
        translations = json.loads((INTEGRATION / path).read_text(encoding="utf-8"))
        assert set(translations["entity"]["sensor"][key]["state"]) == set(options)


def test_every_sensor_has_a_name_and_icon() -> None:
    icons = json.loads((INTEGRATION / "icons.json").read_text(encoding="utf-8"))
    for path in ("strings.json", "translations/en.json"):
        translations = json.loads((INTEGRATION / path).read_text(encoding="utf-8"))
        for key in STATES:
            assert translations["entity"]["sensor"][key]["name"], (path, key)
    assert STATES.keys() <= icons["entity"]["sensor"].keys()
