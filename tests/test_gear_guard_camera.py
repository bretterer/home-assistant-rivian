"""Tests for the Gear Guard live cameras."""

from __future__ import annotations

import asyncio
import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from webrtc_models import RTCIceCandidateInit

from custom_components.rivian import camera, coordinator
from homeassistant.exceptions import HomeAssistantError


def test_signal() -> None:
    """Signaling messages carry a base64 JSON payload from the app's viewer id."""
    message = json.loads(camera._signal("ICE_CANDIDATE", {"candidate": "c"}, "car"))
    assert message["action"] == "ICE_CANDIDATE"
    assert message["recipientClientId"] == "car"
    assert message["senderClientId"] == "ConsumerViewer"
    assert json.loads(base64.b64decode(message["messagePayload"])) == {"candidate": "c"}


def test_cameras_by_model() -> None:
    """Every model has the four sides; the R1T adds the bed, the R2 the cabin."""

    def cameras(model: str) -> set[str]:
        return {
            name
            for name, models in camera.CAMERAS.items()
            if any(key in model for key in models)
        }

    assert cameras("R1S") == {"front", "rear", "left", "right"}
    assert cameras("R1T") == cameras("R1S") | {"bed"}
    assert cameras("R2") == cameras("R1S") | {"interior"}


async def test_early_candidates_follow_the_offer() -> None:
    """Browser candidates sent while the stream starts reach the vehicle after the offer."""
    entity = object.__new__(camera.RivianGearGuardCamera)
    entity._camera, entity._sessions, entity._pending = "front", {}, {}
    entity.entity_id = "camera.gear_guard_front"
    entity.hass = MagicMock()
    entity.hass.async_create_background_task.side_effect = lambda coro, _: coro.close()
    socket = MagicMock(send_str=AsyncMock())
    started = asyncio.Event()

    async def start(_camera: str) -> dict[str, str]:
        await started.wait()
        return {"endpoint": "wss://kinesis"}

    entity.coordinator = MagicMock(start_gear_guard_live_stream=start)
    with patch.object(
        camera,
        "async_get_clientsession",
        return_value=MagicMock(ws_connect=AsyncMock(return_value=socket)),
    ):
        offer = asyncio.create_task(
            entity.async_handle_async_webrtc_offer("v=0", "session", MagicMock())
        )
        await asyncio.sleep(0)
        await entity.async_on_webrtc_candidate(
            "session", RTCIceCandidateInit("candidate:1", sdp_mid="0")
        )
        started.set()
        await offer
    actions = [json.loads(c.args[0])["action"] for c in socket.send_str.await_args_list]
    assert actions == ["SDP_OFFER", "ICE_CANDIDATE"]


async def test_start_retries_with_a_fresh_start() -> None:
    """A start the vehicle misses (e.g. while waking) is retried."""
    coor = object.__new__(coordinator.VehicleCoordinator)
    coor.vehicle_id = "vehicle"
    coor.data = {"gearGuardVideoStatus": {"value": "enabled"}}
    coor._phone_key = MagicMock(return_value={})  # type: ignore[method-assign]
    coor.api = MagicMock(
        start_gear_guard_live_stream=AsyncMock(side_effect=[None, {"endpoint": "e"}])
    )
    assert await coor.start_gear_guard_live_stream("front") == {"endpoint": "e"}
    assert coor.api.start_gear_guard_live_stream.await_count == 2


async def test_relay_passes_answer_and_candidates() -> None:
    """The vehicle's answer and candidates reach the browser; the session then ends."""
    entity = object.__new__(camera.RivianGearGuardCamera)
    entity._sessions, entity._pending = {}, {}

    def message(kind: str, payload: dict[str, object]) -> MagicMock:
        data = {
            "messageType": kind,
            "senderClientId": "car",
            "messagePayload": base64.b64encode(json.dumps(payload).encode()).decode(),
        }
        return MagicMock(type=camera.WSMsgType.TEXT, data=json.dumps(data))

    messages = [
        message("SDP_ANSWER", {"type": "answer", "sdp": "v=0"}),
        message("ICE_CANDIDATE", {"candidate": "c", "sdpMid": "0", "sdpMLineIndex": 0}),
    ]
    socket = MagicMock(close=AsyncMock())
    socket.__aiter__.return_value = messages
    entity._sessions["session"] = (socket, "")
    send_message = MagicMock()
    await entity._relay("session", socket, send_message)
    assert send_message.call_count == 2
    assert "session" not in entity._sessions
    socket.close.assert_awaited_once()


async def test_refused_stream_fails_at_once() -> None:
    """A stream the vehicle refuses fails without waiting or retrying."""
    entity = object.__new__(camera.RivianGearGuardCamera)
    entity._camera, entity._sessions, entity._pending = "front", {}, {}
    entity.coordinator = MagicMock(
        start_gear_guard_live_stream=AsyncMock(
            side_effect=camera.RivianApiException("refused")
        )
    )
    with pytest.raises(HomeAssistantError):
        await entity.async_handle_async_webrtc_offer("v=0", "session", MagicMock())
    assert entity.coordinator.start_gear_guard_live_stream.await_count == 1
    assert not entity._pending
