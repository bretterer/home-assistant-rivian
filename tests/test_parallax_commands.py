"""Tests for sending vehicle commands over Parallax."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from custom_components.rivian import coordinator
from custom_components.rivian.const import (
    ATTR_COORDINATOR,
    ATTR_USER,
    ATTR_VEHICLE,
    DOMAIN,
)
from homeassistant.exceptions import HomeAssistantError

# Plain strings stand in for VehicleCommand, which conftest mocks
FEATURES = {"UNLOCK": "PVS_BD_CMD"}
DONE_STATES = [
    {"progress": "executing", "status": None, "error": None},
    {"progress": "complete", "status": "success", "error": None},
]


def vehicle(
    features: list[str],
    power_state: str = "ready",
    states: list[dict[str, Any]] | None = None,
) -> coordinator.VehicleCoordinator:
    """A vehicle coordinator for an awake R1S with the given supported features.

    The vehicle reports `states` for a Parallax command, by default its success.
    """
    coor = object.__new__(coordinator.VehicleCoordinator)
    coor.vehicle_id = "vehicle"
    coor.data = {"powerState": {"value": power_state}}
    coor.config_entry = MagicMock(entry_id="entry", options={"private_key": "private"})
    user = MagicMock()
    user.get_enrolled_phone_data.return_value = ("phone", {})
    coor.hass = MagicMock()
    coor.hass.data = {
        DOMAIN: {
            "entry": {
                ATTR_VEHICLE: {
                    "vehicle": {
                        "model": "R1S",
                        "supported_features": features,
                        "phone_identity_id": "identity",
                        "public_key": "vehicle_key",
                    }
                },
                ATTR_COORDINATOR: {ATTR_USER: user},
            }
        }
    }
    callbacks = []

    async def subscribe(vehicle_id: str, command_id: str, callback: Any) -> Any:
        callbacks.append(callback)
        return coor.unsubscribe

    async def send_parallax(*args: Any, command_id: str, **kwargs: Any) -> str:
        for state in DONE_STATES if states is None else states:
            callbacks[-1](state)
        return command_id

    coor.unsubscribe = AsyncMock()
    coor.api = SimpleNamespace(
        subscribe_for_parallax_command_state=subscribe,
        send_parallax_command=AsyncMock(side_effect=send_parallax),
        send_vehicle_command=AsyncMock(return_value="id"),
    )
    return coor


async def send(coor: coordinator.VehicleCoordinator, command: str) -> MagicMock:
    """Send a command with the library's Parallax table stubbed."""
    with (
        patch.object(coordinator, "decode_command_state_message", lambda m: m),
        patch.object(coordinator, "PARALLAX_COMMAND_TIMEOUT", 0.01),
        patch.object(coordinator, "vehicle_command_feature", FEATURES.get),
        patch.object(
            coordinator,
            "encode_vehicle_command",
            return_value=("body.locks.unlock", b"\x08\x01"),
        ) as encode,
    ):
        await coor.send_vehicle_command(command)
    return encode


async def test_parallax_when_available() -> None:
    """A command whose category is available over Parallax is sent that way."""
    coor = vehicle(["PVS_BD_CMD"])
    encode = await send(coor, "UNLOCK")
    encode.assert_called_once_with("UNLOCK", None)
    coor.api.send_parallax_command.assert_awaited_once_with(
        "vehicle",
        "body.locks.unlock",
        b"\x08\x01",
        identity_id="identity",
        vehicle_key="vehicle_key",
        private_key="private",
        command_id=ANY,
    )
    coor.api.send_vehicle_command.assert_not_awaited()
    coor.unsubscribe.assert_awaited_once()


@pytest.mark.parametrize(
    ("features", "command"),
    [
        ([], "UNLOCK"),
        (["PVS_COMF_CMD"], "UNLOCK"),
        (["PVS_BD_CMD"], "HONK"),
    ],
)
async def test_legacy_otherwise(features: list[str], command: str) -> None:
    """Other commands, and vehicles without the feature, stay on the legacy API."""
    coor = vehicle(features)
    await send(coor, command)
    coor.api.send_parallax_command.assert_not_awaited()
    assert coor.api.send_vehicle_command.await_args.kwargs["command"] == command


async def test_parallax_skips_the_wake() -> None:
    """A sleeping vehicle isn't woken first; the cloud wakes it for Parallax."""
    coor = vehicle(["PVS_BD_CMD"], power_state="sleep")
    await send(coor, "UNLOCK")
    coor.api.send_vehicle_command.assert_not_awaited()


async def test_refused_parallax_command_raises() -> None:
    """A command Rivian refuses fails the action."""
    coor = vehicle(["PVS_BD_CMD"])
    coor.api.send_parallax_command.side_effect = None
    coor.api.send_parallax_command.return_value = None
    with pytest.raises(HomeAssistantError):
        await send(coor, "UNLOCK")
    coor.unsubscribe.assert_awaited_once()


@pytest.mark.parametrize(
    "states",
    [
        [{"progress": "complete", "status": "not_supported", "error": None}],
        [{"progress": "executing", "status": None, "error": {"code": 3}}],
        [{"progress": "waking_up", "status": None, "error": None}],
        [],
    ],
)
async def test_parallax_command_not_carried_out_raises(
    states: list[dict[str, Any]],
) -> None:
    """A command the vehicle fails, or doesn't confirm in time, fails the action."""
    coor = vehicle(["PVS_BD_CMD"], states=states)
    with pytest.raises(HomeAssistantError):
        await send(coor, "UNLOCK")
    coor.api.send_parallax_command.assert_awaited_once()
    coor.unsubscribe.assert_awaited_once()


async def test_parallax_command_sent_unconfirmed_without_a_subscription() -> None:
    """Without the state subscription, the command is still sent, unconfirmed."""
    coor = vehicle(["PVS_BD_CMD"], states=[])
    coor.unsubscribe = None
    await send(coor, "UNLOCK")
    coor.api.send_parallax_command.assert_awaited_once()
