"""Signed R1S liftgate opening with the pinned rivian-python-client 2.3.1.

This compatibility adapter can go away when the client publishes its signed
Parallax helpers (upstream PRs #229/#235). It deliberately supports one operation.
The wire format is documented in walthowd/rivian-gen2-key-poc/PROTOCOL.md.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import TYPE_CHECKING, Any
import uuid

from google.protobuf.message import DecodeError

from . import liftgate_command_pb2 as wire

if TYPE_CHECKING:
    from rivian import Rivian

_LOGGER = logging.getLogger(__name__)
_OPEN_RVM = "body.closures.open"
# Packed repeated ClosureId field 1, containing only LIFTGATE (7).
_LIFTGATE_PAYLOAD = b"\x0a\x01\x07"
_UNKNOWN_OUTCOME = "Outcome unknown; check the vehicle before retrying"


class LiftgateCommandError(Exception):
    """Opening failed or could not be confirmed; never contains a raw response."""


def _encode_command(
    vehicle_id: str,
    identity_id: str,
    vehicle_key: str,
    private_key: str,
    command_id: uuid.UUID,
) -> str:
    """Sign the operation, including its target and correlation ID."""
    from rivian.utils import generate_ble_command_hmac

    if not all((vehicle_id, identity_id, vehicle_key, private_key)):
        raise ValueError("Missing enrolled key information")
    now = time.time_ns()
    stamp = wire.Timestamp(seconds=now // 1_000_000_000, nanos=now % 1_000_000_000)
    operation = wire.Operation(
        rvm=_OPEN_RVM,
        type=3,
        command_id=command_id.bytes,
        payload=_LIFTGATE_PAYLOAD,
        created_at=stamp,
        vehicle_id=vehicle_id,
    ).SerializeToString()
    envelope = wire.Envelope(
        composite=wire.Composite(bytes_val=operation),
        last_update=stamp,
        correlation_id=command_id.bytes,
        security_info=wire.SecurityInfo(
            signature=wire.Signature(
                type=1,
                value=generate_ble_command_hmac(operation, vehicle_key, private_key),
            ),
            mapped_identity=identity_id,
        ),
    )
    # The encoded request is signed and must not be logged.
    return base64.b64encode(envelope.SerializeToString()).decode("ascii")


def _decode_state(data: dict[str, Any]) -> wire.CommandState | None:
    """Read only command-state messages; malformed/unrelated messages are ignored."""
    payload: Any = data
    for key in ("payload", "data", "vehicleCommandStatePx", "payload"):
        if not isinstance(payload, dict):
            return None
        payload = payload.get(key)
    if not isinstance(payload, str) or not payload:
        return None
    try:
        return wire.CommandState.FromString(base64.b64decode(payload, validate=True))
    except (ValueError, DecodeError):
        return None


async def _send(api: Rivian, vehicle_id: str, payload: str) -> bool:
    """Use the existing authenticated session without logging raw GraphQL errors."""
    from rivian.rivian import BASE_HEADERS, GRAPHQL_GATEWAY

    headers = BASE_HEADERS | {
        "Csrf-Token": api._csrf_token,
        "A-Sess": api._app_session_token,
        "U-Sess": api._user_session_token,
    }
    body = {
        "operationName": "SendRemoteCommand",
        "query": (
            "mutation SendRemoteCommand($vehicleId: String!, $model: String!, "
            "$parallaxPayloadB64: String!) { sendParallaxPayload("
            "payload: $parallaxPayloadB64, meta: {vehicleId: $vehicleId "
            "model: $model isVehicleModelOp: true requiresWakeup: true}) { success } }"
        ),
        "variables": {
            "vehicleId": vehicle_id,
            "model": _OPEN_RVM,
            "parallaxPayloadB64": payload,
        },
    }
    async with api._session.post(
        GRAPHQL_GATEWAY, headers=headers, json=body
    ) as response:
        if response.status != 200:
            return False
        data = await response.json()
        if not isinstance(data, dict) or data.get("errors"):
            return False
        sent = (data.get("data") or {}).get("sendParallaxPayload")
        return isinstance(sent, dict) and sent.get("success") is True


async def open_liftgate(
    api: Rivian,
    vehicle_id: str,
    *,
    identity_id: str,
    vehicle_key: str,
    private_key: str,
    timeout: float = 90,
) -> None:
    """Subscribe first, send once, and require a matching successful completion."""
    command_id = uuid.uuid4()
    subscription_id = str(uuid.uuid4())
    result: asyncio.Future[wire.CommandState] = (
        asyncio.get_running_loop().create_future()
    )
    monitor = None
    submitted = False

    def on_state(data: dict[str, Any]) -> None:
        state = _decode_state(data)
        if (
            state is None
            or state.command_id != command_id.bytes
            or state.rvm != _OPEN_RVM
            or result.done()
        ):
            return
        _LOGGER.debug(
            "Liftgate opening progress=%d status=%d",
            state.progress,
            state.data.response_status,
        )
        if (
            state.progress == 4
            or state.HasField("operation_error")
            or state.data.HasField("error")
        ):
            result.set_result(state)

    try:
        payload = _encode_command(
            vehicle_id, identity_id, vehicle_key, private_key, command_id
        )
        async with asyncio.timeout(timeout):
            monitor = await api._ws_connect()
            if not await monitor.wait_for_ack():
                raise LiftgateCommandError(
                    "Liftgate status unavailable; command not sent"
                )
            subscription = {
                "operationName": "vehicleCommandStatePx",
                "query": (
                    "subscription vehicleCommandStatePx($vehicleId: String!, "
                    "$commandId: String!) { vehicleCommandStatePx("
                    "vehicleId: $vehicleId, commandId: $commandId) { payload } }"
                ),
                "variables": {"vehicleId": vehicle_id, "commandId": str(command_id)},
            }
            # In 2.3.1 start_subscription can leak its registration if setup is
            # cancelled. Own the ID here so finally can always remove just ours.
            monitor._subscriptions[subscription_id] = (on_state, subscription)
            await monitor._subscribe(subscription_id, subscription)
            submitted = True  # A transport failure from here can follow delivery.
            if not await _send(api, vehicle_id, payload):
                raise LiftgateCommandError(
                    f"Liftgate cloud acceptance not confirmed. {_UNKNOWN_OUTCOME}"
                )
            state = await result
            if state.HasField("operation_error") or state.data.HasField("error"):
                error = (
                    state.operation_error
                    if state.HasField("operation_error")
                    else state.data.error
                )
                raise LiftgateCommandError(
                    f"Liftgate opening error_code={error.code}. {_UNKNOWN_OUTCOME}"
                )
            if not state.HasField("data") or state.data.response_status != 1:
                raise LiftgateCommandError(
                    "Liftgate opening not confirmed "
                    f"(status={state.data.response_status}). {_UNKNOWN_OUTCOME}"
                )
            _LOGGER.info("Liftgate opening confirmed by vehicle")
    except asyncio.CancelledError:
        _LOGGER.warning(
            "Liftgate opening cancelled. %s",
            _UNKNOWN_OUTCOME if submitted else "Command not sent",
        )
        raise
    except TimeoutError:
        raise LiftgateCommandError(
            f"Liftgate opening timed out. {_UNKNOWN_OUTCOME}"
            if submitted
            else "Liftgate preparation timed out; command not sent"
        ) from None
    except LiftgateCommandError:
        raise
    except Exception:  # noqa: BLE001 -- sanitize errors at the credential boundary
        # Neither exception strings nor raw server messages are safe diagnostics.
        raise LiftgateCommandError(
            f"Liftgate transport failed. {_UNKNOWN_OUTCOME}"
            if submitted
            else "Liftgate preparation failed; command not sent"
        ) from None
    finally:
        if not result.done():
            result.cancel()
        # _ws_connect may have been cancelled before returning its monitor.
        monitor = monitor or api._ws_monitor
        if monitor is not None:
            registered = monitor._subscriptions.pop(subscription_id, None)
            try:
                async with asyncio.timeout(5):
                    if registered and monitor.connected and monitor.websocket:
                        await monitor.websocket.send_json(
                            {"id": subscription_id, "type": "complete"}
                        )
                    if not monitor.has_subscriptions:
                        await monitor.close()
            except Exception:  # noqa: BLE001 -- cleanup must not mask the outcome
                _LOGGER.warning("Liftgate status subscription cleanup failed")
