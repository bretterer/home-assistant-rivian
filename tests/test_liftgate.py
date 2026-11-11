"""Signed opening: matching acknowledgements, no retries, and safe cleanup."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
import uuid

import pytest

from custom_components.rivian import coordinator, liftgate, liftgate_command_pb2 as wire
from custom_components.rivian.const import (
    ATTR_COORDINATOR,
    ATTR_USER,
    ATTR_VEHICLE,
    DOMAIN,
)
from homeassistant.exceptions import HomeAssistantError

COMMAND_ID = uuid.UUID("00112233-4455-6677-8899-aabbccddeeff")
RVM = "body.closures.open"
SECRET_MARKER = "private-data-must-not-appear"
# Produced independently with rivian-gen2-key-poc's encoder, fixed clock/UUID,
# and a synthetic HMAC key. This checks field numbers and signature coverage.
WIRE_VECTOR = (
    "UkoSSAoSYm9keS5jbG9zdXJlcy5vcGVuEAMaEAARIjNEVWZ3iJmqu8zd7v8iAwoBByoL"
    "CIDiz6oGEMCyzTs6DHZlaGljbGUtdGVzdGILCIDiz6oGEMCyzTuCARAAESIzRFVmd4iZqrvM"
    "3e7/igE1CiQIARIgc+BvCFDXbH7FbUrQse7sS2QqvHXiaioQ3jEKfjxKG70SDWlkZW50"
    "aXR5LXRlc3Q="
)


class Monitor:
    """The pinned client's subscription interface, with no connections."""

    def __init__(self) -> None:
        self._subscriptions: dict[str, Any] = {}
        self.wait_for_ack = AsyncMock(return_value=True)
        self._subscribe = AsyncMock()
        self.websocket = SimpleNamespace(send_json=AsyncMock())
        self.connected = True
        self.close = AsyncMock()

    @property
    def has_subscriptions(self) -> bool:
        return bool(self._subscriptions)


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    from rivian import utils

    monkeypatch.setattr(
        utils,
        "generate_ble_command_hmac",
        lambda data, *_: hmac.new(b"K" * 32, data, hashlib.sha256).digest(),
        raising=False,
    )
    monkeypatch.setattr(liftgate.time, "time_ns", lambda: 1_700_000_000_125_000_000)
    monitor = Monitor()
    api = SimpleNamespace(
        _ws_connect=AsyncMock(return_value=monitor), _ws_monitor=monitor
    )
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(liftgate, "_send", sent)
    return SimpleNamespace(api=api, monitor=monitor, send=sent)


def frame(state: wire.CommandState) -> dict[str, Any]:
    return {
        "payload": {
            "data": {
                "vehicleCommandStatePx": {
                    "payload": base64.b64encode(state.SerializeToString()).decode()
                }
            }
        }
    }


def callback(rig: SimpleNamespace) -> tuple[Any, uuid.UUID]:
    callback_fn, subscription = next(
        (cb, payload)
        for cb, payload in rig.monitor._subscriptions.values()
        if payload.get("operationName") == "vehicleCommandStatePx"
    )
    return callback_fn, uuid.UUID(subscription["variables"]["commandId"])


def completion(rig: SimpleNamespace, **changes: Any) -> wire.CommandState:
    _, command_id = callback(rig)
    fields = {
        "command_id": command_id.bytes,
        "rvm": RVM,
        "progress": 4,
        "data": wire.CommandResult(response_status=1),
    }
    fields.update(changes)
    return wire.CommandState(**fields)


async def open_gate(rig: SimpleNamespace, timeout: float = 1) -> None:
    await liftgate.open_liftgate(
        rig.api,
        "vehicle-test",
        identity_id="identity-test",
        vehicle_key="synthetic-vehicle-key",
        private_key=SECRET_MARKER,
        timeout=timeout,
    )


def test_signed_payload_matches_independent_wire_vector(rig: SimpleNamespace) -> None:
    encoded = liftgate._encode_command(
        "vehicle-test", "identity-test", "vehicle-key", "private-key", COMMAND_ID
    )
    assert encoded == WIRE_VECTOR
    envelope = wire.Envelope.FromString(base64.b64decode(encoded))
    operation = wire.Operation.FromString(envelope.composite.bytes_val)
    assert operation.payload == b"\x0a\x01\x07"  # only the upper liftgate
    assert operation.command_id == envelope.correlation_id == COMMAND_ID.bytes


async def test_subscribe_before_send_and_keep_other_subscriptions(
    rig: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    sentinel = (lambda _: None, {})
    rig.monitor._subscriptions["existing-telemetry"] = sentinel

    async def send(*args: Any) -> bool:
        rig.monitor._subscribe.assert_awaited_once()
        cb, _ = callback(rig)
        cb(frame(completion(rig)))
        return True

    rig.send.side_effect = send
    with caplog.at_level(logging.DEBUG):
        await open_gate(rig)

    rig.send.assert_awaited_once()
    assert rig.monitor._subscriptions == {"existing-telemetry": sentinel}
    rig.monitor.close.assert_not_awaited()
    assert rig.monitor.websocket.send_json.await_args.args[0]["type"] == "complete"
    assert "confirmed by vehicle" in caplog.text
    for private_value in [
        SECRET_MARKER,
        "vehicle-test",
        "identity-test",
        "synthetic-vehicle-key",
    ]:
        assert private_value not in caplog.text


@pytest.mark.parametrize("status", [0, 2, 3, 4, 5, 99])
async def test_non_success_completion_is_an_error_without_retry(
    rig: SimpleNamespace, status: int
) -> None:
    async def send(*args: Any) -> bool:
        cb, _ = callback(rig)
        cb(frame(completion(rig, data=wire.CommandResult(response_status=status))))
        return True

    rig.send.side_effect = send
    with pytest.raises(liftgate.LiftgateCommandError, match=f"status={status}"):
        await open_gate(rig)
    rig.send.assert_awaited_once()
    assert not rig.monitor.has_subscriptions
    rig.monitor.close.assert_awaited_once()


@pytest.mark.parametrize("operation_error", [True, False])
async def test_error_presence_overrides_success_and_redacts_server_message(
    rig: SimpleNamespace, operation_error: bool, caplog: pytest.LogCaptureFixture
) -> None:
    async def send(*args: Any) -> bool:
        cb, _ = callback(rig)
        state = completion(rig)
        error = state.operation_error if operation_error else state.data.error
        error.code = 3
        error.message = SECRET_MARKER
        cb(frame(state))
        return True

    rig.send.side_effect = send
    with pytest.raises(liftgate.LiftgateCommandError, match="error_code=3") as error:
        await open_gate(rig)
    assert SECRET_MARKER not in str(error.value) + caplog.text
    rig.send.assert_awaited_once()


@pytest.mark.parametrize(
    "kind", ["wrong_id", "wrong_rvm", "progress", "malformed", "graphql_error"]
)
async def test_unmatched_or_incomplete_messages_cannot_confirm_opening(
    rig: SimpleNamespace, kind: str
) -> None:
    async def send(*args: Any) -> bool:
        cb, _ = callback(rig)
        state = completion(rig)
        if kind == "wrong_id":
            state.command_id = uuid.uuid4().bytes
        elif kind == "wrong_rvm":
            state.rvm = "body.closures.close"
        elif kind == "progress":
            state.progress = 3
        cb(
            {"payload": {"errors": [{"message": SECRET_MARKER}]}}
            if kind == "graphql_error"
            else {"payload": {"data": {"vehicleCommandStatePx": {"payload": "!!!"}}}}
            if kind == "malformed"
            else frame(state)
        )
        return True

    rig.send.side_effect = send
    with pytest.raises(liftgate.LiftgateCommandError, match="Outcome unknown"):
        await open_gate(rig, timeout=0.01)
    rig.send.assert_awaited_once()
    assert not rig.monitor.has_subscriptions


async def test_completed_message_without_result_is_not_success(
    rig: SimpleNamespace,
) -> None:
    async def send(*args: Any) -> bool:
        cb, _ = callback(rig)
        state = completion(rig)
        state.ClearField("data")
        cb(frame(state))
        return True

    rig.send.side_effect = send
    with pytest.raises(liftgate.LiftgateCommandError, match="not confirmed"):
        await open_gate(rig)


async def test_cloud_acceptance_alone_is_not_success(rig: SimpleNamespace) -> None:
    with pytest.raises(
        liftgate.LiftgateCommandError, match="timed out.*Outcome unknown"
    ):
        await open_gate(rig, timeout=0.01)
    rig.send.assert_awaited_once()


async def test_cloud_rejection_never_retries(rig: SimpleNamespace) -> None:
    rig.send.return_value = False
    with pytest.raises(liftgate.LiftgateCommandError, match="acceptance not confirmed"):
        await open_gate(rig)
    rig.send.assert_awaited_once()


async def test_unavailable_subscription_sends_nothing(rig: SimpleNamespace) -> None:
    rig.monitor.wait_for_ack.return_value = False
    with pytest.raises(liftgate.LiftgateCommandError, match="command not sent"):
        await open_gate(rig)
    rig.send.assert_not_awaited()
    assert not rig.monitor.has_subscriptions


@pytest.mark.parametrize("stage", ["connect", "subscribe"])
async def test_setup_timeout_never_sends(rig: SimpleNamespace, stage: str) -> None:
    async def wait(*args: Any) -> None:
        await asyncio.Event().wait()

    target = rig.api._ws_connect if stage == "connect" else rig.monitor._subscribe
    target.side_effect = wait
    with pytest.raises(liftgate.LiftgateCommandError, match="command not sent"):
        await open_gate(rig, timeout=0.01)
    rig.send.assert_not_awaited()
    assert not rig.monitor.has_subscriptions


async def test_cleanup_failure_does_not_mask_confirmed_result(
    rig: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    async def send(*args: Any) -> bool:
        cb, _ = callback(rig)
        cb(frame(completion(rig)))
        return True

    rig.send.side_effect = send
    rig.monitor.websocket.send_json.side_effect = RuntimeError(SECRET_MARKER)
    await open_gate(rig)
    assert not rig.monitor.has_subscriptions
    assert "cleanup failed" in caplog.text
    assert SECRET_MARKER not in caplog.text


@pytest.mark.parametrize("stage", ["connect", "subscribe", "send"])
async def test_transport_error_is_sanitized_and_registration_removed(
    rig: SimpleNamespace, stage: str, caplog: pytest.LogCaptureFixture
) -> None:
    target = {
        "connect": rig.api._ws_connect,
        "subscribe": rig.monitor._subscribe,
        "send": rig.send,
    }[stage]
    target.side_effect = RuntimeError(SECRET_MARKER)
    with pytest.raises(liftgate.LiftgateCommandError) as error:
        await open_gate(rig)
    assert SECRET_MARKER not in str(error.value) + caplog.text
    assert not rig.monitor.has_subscriptions
    if stage == "send":
        rig.send.assert_awaited_once()
        assert "Outcome unknown" in str(error.value)
    else:
        rig.send.assert_not_awaited()
        assert "command not sent" in str(error.value)


@pytest.mark.parametrize("stage", ["connect", "subscribe", "send", "result"])
async def test_cancellation_cleans_up_without_a_second_command(
    rig: SimpleNamespace, stage: str
) -> None:
    blocked = asyncio.Event()

    async def wait(*args: Any) -> bool:
        blocked.set()
        await asyncio.Event().wait()
        return True

    if stage != "result":
        target = {
            "connect": rig.api._ws_connect,
            "subscribe": rig.monitor._subscribe,
            "send": rig.send,
        }[stage]
        target.side_effect = wait
    else:
        rig.send.side_effect = lambda *args: blocked.set() or True
    task = asyncio.create_task(open_gate(rig))
    await blocked.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not rig.monitor.has_subscriptions
    assert rig.send.await_count == (1 if stage in ("send", "result") else 0)


async def test_bad_key_fails_before_any_connection(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        liftgate, "_encode_command", MagicMock(side_effect=ValueError(SECRET_MARKER))
    )
    with pytest.raises(liftgate.LiftgateCommandError, match="command not sent"):
        await open_gate(rig)
    rig.api._ws_connect.assert_not_awaited()
    rig.send.assert_not_awaited()


@pytest.fixture
def routed_vehicle(monkeypatch: pytest.MonkeyPatch) -> coordinator.VehicleCoordinator:
    commands = SimpleNamespace(
        OPEN_LIFTGATE_UNLATCH_TAILGATE="OPEN_LIFTGATE_UNLATCH_TAILGATE",
        CLOSE_LIFTGATE="CLOSE_LIFTGATE",
        OPEN_FRUNK="OPEN_FRUNK",
        CLOSE_FRUNK="CLOSE_FRUNK",
        WAKE_VEHICLE="WAKE_VEHICLE",
    )
    monkeypatch.setattr(coordinator, "VehicleCommand", commands)
    coor = object.__new__(coordinator.VehicleCoordinator)
    coor.vehicle_id = "vehicle-test"
    coor.config_entry = SimpleNamespace(
        entry_id="entry", options={"private_key": SECRET_MARKER}
    )
    vehicle = {
        "model": "R1S",
        "supported_features": ["PVS_BD_CMD", "LIFTGATE_CMD"],
        "phone_identity_id": "identity-test",
        "public_key": "synthetic-vehicle-key",
    }
    user = SimpleNamespace(get_enrolled_phone_data=lambda _: ("phone-test", {}))
    coor.hass = SimpleNamespace(
        data={
            DOMAIN: {
                "entry": {
                    ATTR_VEHICLE: {coor.vehicle_id: vehicle},
                    ATTR_COORDINATOR: {ATTR_USER: user},
                }
            }
        }
    )
    coor.api = SimpleNamespace(send_vehicle_command=AsyncMock(return_value="legacy-id"))
    coor.data = {"powerState": {"value": "ready"}}
    monkeypatch.setattr(coordinator, "open_liftgate", AsyncMock())
    return coor


async def test_capable_r1s_uses_signed_open_only(routed_vehicle: Any) -> None:
    await routed_vehicle.send_vehicle_command("OPEN_LIFTGATE_UNLATCH_TAILGATE")
    coordinator.open_liftgate.assert_awaited_once_with(
        routed_vehicle.api,
        "vehicle-test",
        identity_id="identity-test",
        vehicle_key="synthetic-vehicle-key",
        private_key=SECRET_MARKER,
        timeout=90,
    )
    routed_vehicle.api.send_vehicle_command.assert_not_awaited()


@pytest.mark.parametrize(
    "command", ["CLOSE_LIFTGATE", "OPEN_FRUNK", "CLOSE_FRUNK", "WAKE_VEHICLE"]
)
async def test_working_commands_keep_the_existing_path(
    routed_vehicle: Any, command: str
) -> None:
    await routed_vehicle.send_vehicle_command(command)
    coordinator.open_liftgate.assert_not_awaited()
    routed_vehicle.api.send_vehicle_command.assert_awaited_once()
    assert (
        routed_vehicle.api.send_vehicle_command.await_args.kwargs["command"] == command
    )


@pytest.mark.parametrize(
    "model,features",
    [
        ("R1T", ["PVS_BD_CMD", "LIFTGATE_CMD"]),
        ("R2", ["PVS_BD_CMD", "LIFTGATE_CMD"]),
        (None, ["PVS_BD_CMD", "LIFTGATE_CMD"]),
        ("R1S", ["LIFTGATE_CMD"]),
        ("R1S", ["PVS_BD_CMD"]),
        ("R1S", []),
    ],
)
async def test_other_models_and_missing_features_keep_legacy(
    routed_vehicle: Any, model: str | None, features: list[str]
) -> None:
    vehicle = routed_vehicle.hass.data[DOMAIN]["entry"][ATTR_VEHICLE]["vehicle-test"]
    vehicle.update(model=model, supported_features=features)
    await routed_vehicle.send_vehicle_command("OPEN_LIFTGATE_UNLATCH_TAILGATE")
    coordinator.open_liftgate.assert_not_awaited()
    routed_vehicle.api.send_vehicle_command.assert_awaited_once()


async def test_sleep_uses_signed_wakeup_and_longer_timeout(routed_vehicle: Any) -> None:
    routed_vehicle.data["powerState"]["value"] = "sleep"
    await routed_vehicle.send_vehicle_command("OPEN_LIFTGATE_UNLATCH_TAILGATE")
    assert coordinator.open_liftgate.await_args.kwargs["timeout"] == 120
    routed_vehicle.api.send_vehicle_command.assert_not_awaited()


async def test_legacy_close_keeps_original_wake_and_refetches_vehicle(
    routed_vehicle: Any,
) -> None:
    routed_vehicle.data["powerState"]["value"] = "sleep"
    routed_vehicle._awake = asyncio.Event()
    routed_vehicle._awake.set()
    vehicles = routed_vehicle.hass.data[DOMAIN]["entry"][ATTR_VEHICLE]

    async def send(**kwargs: Any) -> str:
        if kwargs["command"] == "WAKE_VEHICLE":
            vehicles["vehicle-test"] = {
                **vehicles["vehicle-test"],
                "public_key": "updated-synthetic-vehicle-key",
            }
        return "legacy-id"

    routed_vehicle.api.send_vehicle_command.side_effect = send
    await routed_vehicle.send_vehicle_command("CLOSE_LIFTGATE")
    coordinator.open_liftgate.assert_not_awaited()
    calls = routed_vehicle.api.send_vehicle_command.await_args_list
    assert [call.kwargs["command"] for call in calls] == [
        "WAKE_VEHICLE",
        "CLOSE_LIFTGATE",
    ]
    assert calls[1].kwargs["vehicle_key"] == "updated-synthetic-vehicle-key"


async def test_signed_error_is_visible_and_never_falls_back(
    routed_vehicle: Any,
) -> None:
    coordinator.open_liftgate.side_effect = liftgate.LiftgateCommandError(
        "Outcome unknown"
    )
    with pytest.raises(HomeAssistantError, match="Outcome unknown"):
        await routed_vehicle.send_vehicle_command("OPEN_LIFTGATE_UNLATCH_TAILGATE")
    coordinator.open_liftgate.assert_awaited_once()
    routed_vehicle.api.send_vehicle_command.assert_not_awaited()
