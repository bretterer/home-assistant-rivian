"""Exercise the adapter with the actual pinned client, outside the HA stubs."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import textwrap


def test_real_pinned_client_and_http_contract() -> None:
    """Use real crypto/monitor classes, synthetic keys, and mocked HTTP/WebSocket."""
    # conftest.py replaces rivian in sys.modules. A fresh interpreter checks the
    # actual installed dependency instead; every socket connection is blocked.
    program = r"""
        import asyncio
        import base64
        import importlib
        from importlib.metadata import version
        import socket
        import sys
        import types
        from unittest.mock import AsyncMock, Mock
        import uuid

        def offline(*args, **kwargs):
            raise AssertionError("Network is disabled for this compatibility test")

        socket.socket.connect = socket.socket.connect_ex = offline
        socket.create_connection = offline

        from cryptography.hazmat.primitives.asymmetric import ec
        from rivian import Rivian
        from rivian.rivian import BASE_HEADERS, GRAPHQL_GATEWAY
        from rivian.utils import (
            encode_private_key, encode_public_key, generate_ble_command_hmac
        )
        from rivian.ws_monitor import WebSocketMonitor

        assert version("rivian-python-client") == "2.3.1"
        package = types.ModuleType("liftgate_under_test")
        package.__path__ = [sys.argv[1]]
        sys.modules[package.__name__] = package
        gate = importlib.import_module("liftgate_under_test.liftgate")
        wire = importlib.import_module("liftgate_under_test.liftgate_command_pb2")
        private_key = encode_private_key(ec.derive_private_key(1, ec.SECP256R1()))
        vehicle_key = encode_public_key(ec.derive_private_key(2, ec.SECP256R1()).public_key())

        async def run():
            session = Mock()
            api = Rivian(session=session)
            api._csrf_token = "synthetic-csrf"
            api._app_session_token = "synthetic-app-session"
            api._user_session_token = "synthetic-user-session"
            monitor = WebSocketMonitor(api, "invalid://offline", AsyncMock())
            monitor._ws = types.SimpleNamespace(closed=False, send_json=AsyncMock())
            monitor.connection_ack.set()
            telemetry = (lambda data: None, {"operationName": "existing"})
            monitor._subscriptions["existing-telemetry"] = telemetry
            api._ws_monitor = monitor
            api._ws_connect = AsyncMock(return_value=monitor)
            response = types.SimpleNamespace(status=200)

            async def acknowledge():
                callback, subscription = next(
                    (cb, payload) for cb, payload in monitor._subscriptions.values()
                    if payload.get("operationName") == "vehicleCommandStatePx"
                )
                cmd = uuid.UUID(subscription["variables"]["commandId"])
                assert subscription["variables"]["vehicleId"] == "synthetic-vehicle"
                state = wire.CommandState(
                    command_id=cmd.bytes, rvm="body.closures.open", progress=4,
                    data=wire.CommandResult(response_status=1)
                )
                callback({"payload": {"data": {"vehicleCommandStatePx": {
                    "payload": base64.b64encode(state.SerializeToString()).decode()
                }}}})
                return {"data": {"sendParallaxPayload": {"success": True}}}

            response.json = AsyncMock(side_effect=acknowledge)
            # Special methods need to live on the type, not its instance.
            class ResponseContext:
                async def __aenter__(self):
                    return response
                async def __aexit__(self, *args):
                    return False
            session.post.return_value = ResponseContext()
            await gate.open_liftgate(
                api, "synthetic-vehicle", identity_id="synthetic-identity",
                vehicle_key=vehicle_key, private_key=private_key, timeout=1
            )
            session.post.assert_called_once()
            request = session.post.call_args
            assert request.args == (GRAPHQL_GATEWAY,)
            assert request.kwargs["headers"] == BASE_HEADERS | {
                "Csrf-Token": "synthetic-csrf", "A-Sess": "synthetic-app-session",
                "U-Sess": "synthetic-user-session"
            }
            body = request.kwargs["json"]
            assert "requiresWakeup: true" in body["query"]
            envelope = wire.Envelope.FromString(base64.b64decode(
                body["variables"]["parallaxPayloadB64"]
            ))
            operation_bytes = envelope.composite.bytes_val
            operation = wire.Operation.FromString(operation_bytes)
            assert operation.rvm == "body.closures.open" and operation.type == 3
            assert operation.payload == b"\x0a\x01\x07"
            assert operation.command_id == envelope.correlation_id
            assert operation.vehicle_id == "synthetic-vehicle"
            assert envelope.security_info.mapped_identity == "synthetic-identity"
            assert envelope.security_info.signature.value == generate_ble_command_hmac(
                operation_bytes, vehicle_key, private_key
            )
            assert monitor._subscriptions == {"existing-telemetry": telemetry}
            assert [call.args[0]["type"] for call in monitor.websocket.send_json.await_args_list] == ["subscribe", "complete"]

            # GraphQL partial errors, nulls, and non-boolean success cannot pass.
            response.json.side_effect = None
            for payload in [
                {"errors": [{"message": "synthetic-secret"}], "data": {"sendParallaxPayload": {"success": True}}},
                {"data": {"sendParallaxPayload": None}},
                {"data": {"sendParallaxPayload": {"success": False}}},
                {"data": {"sendParallaxPayload": {"success": "true"}}},
                None,
            ]:
                response.json.return_value = payload
                assert not await gate._send(api, "synthetic-vehicle", "synthetic-payload")
            response.status = 401
            assert not await gate._send(api, "synthetic-vehicle", "synthetic-payload")

        asyncio.run(run())
    """
    integration = Path(__file__).resolve().parents[1] / "custom_components" / "rivian"
    subprocess.run(
        [sys.executable, "-c", textwrap.dedent(program), str(integration)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
