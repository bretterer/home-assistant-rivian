"""Support for Rivian Gear Guard live cameras."""

from __future__ import annotations

import base64
import json
import logging
from typing import Any, Final

from aiohttp import ClientError, ClientWebSocketResponse, WSMsgType
from rivian.exceptions import RivianApiException
from webrtc_models import RTCIceCandidateInit

from homeassistant.components.camera import (
    Camera,
    CameraEntityDescription,
    CameraEntityFeature,
    WebRTCAnswer,
    WebRTCCandidate,
    WebRTCSendMessage,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ATTR_COORDINATOR, ATTR_VEHICLE, DOMAIN
from .coordinator import VehicleCoordinator
from .entity import RivianVehicleControlEntity

_LOGGER = logging.getLogger(__name__)

# The Rivian app's viewer id on the Kinesis signaling channel
CLIENT_ID: Final = "ConsumerViewer"

# Camera -> model keys it exists on, matched as substrings of the model
CAMERAS: Final[dict[str, tuple[str, ...]]] = {
    "front": ("R1", "R2"),
    "rear": ("R1", "R2"),
    "left": ("R1", "R2"),
    "right": ("R1", "R2"),
    "interior": ("R2",),  # the R1's cabin camera is only for driver monitoring
    "bed": ("R1T",),
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the Gear Guard cameras."""
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    vehicles: dict[str, dict[str, Any]] = data[ATTR_VEHICLE]
    coordinators: dict[str, VehicleCoordinator] = data[ATTR_COORDINATOR][ATTR_VEHICLE]

    async_add_entities(
        RivianGearGuardCamera(
            coordinators[vehicle_id],
            entry,
            CameraEntityDescription(
                key=f"gear_guard_{camera}",
                translation_key=f"gear_guard_{camera}",
            ),
            vehicle,
            camera,
        )
        for vehicle_id, vehicle in vehicles.items()
        if vehicle.get("phone_identity_id")
        and "LIVE_CAM" in vehicle.get("supported_features", [])
        for camera, models in CAMERAS.items()
        if any(model in vehicle["model"] for model in models)
    )


def _signal(action: str, payload: dict[str, Any], recipient: str = "") -> str:
    """Return a Kinesis Video signaling message."""
    return json.dumps(
        {
            "action": action,
            "messagePayload": base64.b64encode(json.dumps(payload).encode()).decode(),
            "recipientClientId": recipient,
            "senderClientId": CLIENT_ID,
        }
    )


class RivianGearGuardCamera(RivianVehicleControlEntity, Camera):
    """A Gear Guard camera, streamed live over WebRTC."""

    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(
        self,
        coordinator: VehicleCoordinator,
        config_entry: ConfigEntry,
        description: CameraEntityDescription,
        vehicle: dict[str, Any],
        camera: str,
    ) -> None:
        """Create a Gear Guard camera."""
        super().__init__(coordinator, config_entry, description, vehicle)
        Camera.__init__(self)
        self._camera = camera
        # session id -> (signaling socket, the vehicle's client id once it answers)
        self._sessions: dict[str, tuple[ClientWebSocketResponse, str]] = {}
        # Browser candidates that arrive while the stream is still starting
        self._pending: dict[str, list[RTCIceCandidateInit]] = {}

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return None; the cameras only stream, so there's no thumbnail."""
        return None

    async def async_handle_async_webrtc_offer(
        self, offer_sdp: str, session_id: str, send_message: WebRTCSendMessage
    ) -> None:
        """Start a live stream and pass the browser's offer to the vehicle."""
        if self._sessions or self._pending:
            raise HomeAssistantError("A live stream is already open")
        self._pending[session_id] = []
        try:
            try:
                config = await self.coordinator.start_gear_guard_live_stream(
                    self._camera
                )
            except RivianApiException as err:
                raise HomeAssistantError(str(err)) from err
            if not config:
                raise HomeAssistantError(
                    "The vehicle didn't start the stream; it streams one session at "
                    "a time, up to an hour a day"
                )
            # The stream's own TURN servers (config["iceServers"]) go unused: HA
            # asks for the browser's ICE servers before there is an offer
            try:
                socket = await async_get_clientsession(self.hass).ws_connect(
                    config["endpoint"]
                )
            except ClientError as err:
                raise HomeAssistantError(f"Couldn't reach the stream: {err}") from err
            self._sessions[session_id] = (socket, "")
            await socket.send_str(
                _signal("SDP_OFFER", {"type": "offer", "sdp": offer_sdp})
            )
            for candidate in self._pending.get(session_id, []):
                await socket.send_str(_signal("ICE_CANDIDATE", candidate.to_dict()))
        finally:
            self._pending.pop(session_id, None)
        self.hass.async_create_background_task(
            self._relay(session_id, socket, send_message),
            f"{self.entity_id} Gear Guard signaling",
        )

    async def _relay(
        self,
        session_id: str,
        socket: ClientWebSocketResponse,
        send_message: WebRTCSendMessage,
    ) -> None:
        """Pass the vehicle's answer and ICE candidates to the browser."""
        try:
            async for message in socket:
                if message.type != WSMsgType.TEXT or not message.data:
                    continue
                data = json.loads(message.data)
                _LOGGER.debug("Gear Guard signaling: %s", data.get("messageType"))
                if not (raw := data.get("messagePayload")):
                    continue
                payload = json.loads(base64.b64decode(raw))
                if data.get("messageType") == "SDP_ANSWER":
                    self._sessions[session_id] = (
                        socket,
                        data.get("senderClientId", ""),
                    )
                    send_message(WebRTCAnswer(payload["sdp"]))
                elif data.get("messageType") == "ICE_CANDIDATE":
                    send_message(
                        WebRTCCandidate(
                            RTCIceCandidateInit(
                                payload["candidate"],
                                sdp_mid=payload.get("sdpMid"),
                                sdp_m_line_index=payload.get("sdpMLineIndex"),
                            )
                        )
                    )
        finally:
            _LOGGER.debug("Gear Guard signaling closed: %s", socket.close_code)
            self._sessions.pop(session_id, None)
            await socket.close()

    async def async_on_webrtc_candidate(
        self, session_id: str, candidate: RTCIceCandidateInit
    ) -> None:
        """Pass a browser ICE candidate to the vehicle."""
        if session := self._sessions.get(session_id):
            socket, vehicle = session
            if not socket.closed:
                await socket.send_str(
                    _signal("ICE_CANDIDATE", candidate.to_dict(), vehicle)
                )
        elif session_id in self._pending:
            self._pending[session_id].append(candidate)

    @callback
    def close_webrtc_session(self, session_id: str) -> None:
        """Close a session's signaling socket."""
        self._pending.pop(session_id, None)
        if session := self._sessions.pop(session_id, None):
            self.hass.async_create_task(session[0].close())

    async def async_will_remove_from_hass(self) -> None:
        """Close any open stream."""
        self._pending.clear()
        for socket, _ in self._sessions.values():
            await socket.close()
        self._sessions.clear()
        await super().async_will_remove_from_hass()
