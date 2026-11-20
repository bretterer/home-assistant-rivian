"""Tests for sending vehicle commands over Parallax."""

from __future__ import annotations

from collections.abc import Iterator
import importlib
import sys
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from custom_components.rivian import coordinator
from homeassistant.exceptions import HomeAssistantError

# Plain strings stand in for VehicleCommand, which conftest mocks
SIDE_BIN = "RELEASE_LEFT_SIDE_BIN"
FEATURES = {"UNLOCK": "PVS_BD_CMD", SIDE_BIN: "PVS_BD_CMD"}
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
    coor.config_entry.runtime_data = SimpleNamespace(
        vehicles={
            "vehicle": {
                "name": "W4LT3R",
                "model": "R1S",
                "supported_features": features,
                "phone_identity_id": "identity",
                "public_key": "vehicle_key",
            }
        },
        user_coordinator=user,
    )
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
    encode.assert_called_once_with("UNLOCK", None, model="R1S")
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


class TranslatedError(Exception):
    """Stands in for Home Assistant's errors, keeping their translation."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__()
        self.translation_key = kwargs.get("translation_key")
        self.translation_placeholders = kwargs.get("translation_placeholders")


@pytest.mark.parametrize(
    ("states", "key", "placeholders"),
    [
        (
            [{"progress": "complete", "status": "not_supported", "error": None}],
            "command_failed",
            {"rvm": "body.locks.unlock", "reason": "not_supported"},
        ),
        ([], "command_unconfirmed", {"rvm": "body.locks.unlock"}),
    ],
)
async def test_parallax_errors_are_translated(
    states: list[dict[str, Any]], key: str, placeholders: dict[str, str]
) -> None:
    """Failures raise translated errors naming the RVM."""
    coor = vehicle(["PVS_BD_CMD"], states=states)
    with (
        patch.object(coordinator, "HomeAssistantError", TranslatedError),
        pytest.raises(TranslatedError) as err,
    ):
        await send(coor, "UNLOCK")
    assert err.value.translation_key == key
    assert err.value.translation_placeholders == placeholders


async def test_side_bin_timeout_is_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    """A released side bin that reports a timeout is logged, not raised."""
    timeout = [
        {
            "progress": "complete",
            "status": "timeout",
            "error": {
                "code": 1,
                "message": "Command execution timed out in vehicle services",
            },
        }
    ]
    await send(vehicle(["PVS_BD_CMD"], states=timeout), SIDE_BIN)
    assert (
        "RELEASE_LEFT_SIDE_BIN sent to W4LT3R, but the side bin may not have fully"
        " opened" in caplog.text
    )
    with pytest.raises(HomeAssistantError):
        await send(vehicle(["PVS_BD_CMD"], states=timeout), "UNLOCK")


@pytest.fixture
def real_parallax() -> Iterator[ModuleType]:
    """The installed rivian.parallax, which conftest otherwise mocks."""
    with patch.dict(sys.modules):
        for name in [
            n for n in sys.modules if n == "rivian" or n.startswith("rivian.")
        ]:
            del sys.modules[name]
        yield importlib.import_module("rivian.parallax")


async def test_r1t_tailgate_with_the_real_client(real_parallax: ModuleType) -> None:
    """The client opens an R1T's tailgate over Parallax for the liftgate command."""
    body_pb2 = importlib.import_module("rivian.parallax.proto.body_pb2")
    command = "OPEN_LIFTGATE_UNLATCH_TAILGATE"
    coor = vehicle(["PVS_BD_CMD"])
    coor.config_entry.runtime_data.vehicles["vehicle"]["model"] = "R1T"
    with (
        patch.object(coordinator, "decode_command_state_message", lambda m: m),
        patch.object(
            coordinator,
            "vehicle_command_feature",
            real_parallax.vehicle_command_feature,
        ),
        patch.object(
            coordinator, "encode_vehicle_command", real_parallax.encode_vehicle_command
        ),
    ):
        await coor.send_vehicle_command(command)
    args = coor.api.send_parallax_command.await_args.args
    assert args[1:] == (
        "body.closures.open",
        real_parallax.encode_closures(body_pb2.TAILGATE),
    )
