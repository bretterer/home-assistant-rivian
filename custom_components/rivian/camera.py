"""Support for Rivian Gear Guard live cameras."""

from __future__ import annotations

import base64
import functools
import json
import logging
from pathlib import Path
from typing import Any, Final

from aiohttp import ClientError, ClientWebSocketResponse, WSMsgType
from rivian.exceptions import RivianApiException
from webrtc_models import RTCIceCandidateInit

from homeassistant.components.camera import (
    DOMAIN as CAMERA_DOMAIN,
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
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import RivianConfigEntry, VehicleCoordinator
from .entity import RivianVehicleEntity
from .helpers import async_remove_stale_entities

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
# Live view also needs V_GGVS (the app's GEAR_GUARD_STREAMING, Connect+'s Gear
# Guard Live Cam): a LIVE_CAM vehicle without it refuses the stream
# (VEHICLE_COMMAND_FORBIDDEN). The interior camera needs its own feature too.
STREAM_FEATURES: Final[set[str]] = {"LIVE_CAM", "V_GGVS"}
CAMERA_FEATURES: Final[dict[str, str]] = {"interior": "INTERIOR_CAMERA"}


def vehicle_cameras(vehicle: dict[str, Any]) -> list[str]:
    """Return the cameras a vehicle can stream."""
    features = set(vehicle.get("supported_features", []))
    if not vehicle.get("phone_identity_id") or not STREAM_FEATURES <= features:
        return []
    return [
        camera
        for camera, models in CAMERAS.items()
        if any(model in vehicle["model"] for model in models)
        and (camera not in CAMERA_FEATURES or CAMERA_FEATURES[camera] in features)
    ]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RivianConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Gear Guard cameras."""
    vehicles = entry.runtime_data.vehicles
    coordinators = entry.runtime_data.vehicle_coordinators

    cameras = [
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
        for camera in vehicle_cameras(vehicle)
    ]
    # Remove cameras a vehicle no longer gets, e.g. once Connect+ lapses
    async_remove_stale_entities(
        hass,
        CAMERA_DOMAIN,
        (
            f"{vehicle['vin']}-gear_guard_{camera}"
            for vehicle in vehicles.values()
            # Keep them if the features didn't load, and while there's no phone key,
            # like the other entities that need one
            if vehicle.get("supported_features") and vehicle.get("phone_identity_id")
            for camera in CAMERAS
        ),
        (camera.unique_id for camera in cameras),
    )
    async_add_entities(cameras)


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


# Shown until a stream starts, as the cameras have no still images
PLACEHOLDER = Path(__file__).parent / "placeholder.png"


class RivianGearGuardCamera(RivianVehicleEntity, Camera):
    """A Gear Guard camera, streamed live over WebRTC.

    Not limited to the vehicle control zones, which are about commands: live
    view is most useful away from home.
    """

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
        self.content_type = "image/png"
        self._camera = camera
        # session id -> (signaling socket, the vehicle's client id once it answers)
        self._sessions: dict[str, tuple[ClientWebSocketResponse, str]] = {}
        # Browser candidates that arrive while the stream is still starting
        self._pending: dict[str, list[RTCIceCandidateInit]] = {}

    @property
    def available(self) -> bool:
        """Return whether the vehicle is parked, which live view needs."""
        return super().available and self._get_value("gearStatus") == "park"

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a placeholder; the cameras only stream."""
        return await self.hass.async_add_executor_job(self.placeholder_image)

    @classmethod
    @functools.cache
    def placeholder_image(cls) -> bytes:
        """Return the placeholder image."""
        return PLACEHOLDER.read_bytes()

    async def async_handle_async_webrtc_offer(
        self, offer_sdp: str, session_id: str, send_message: WebRTCSendMessage
    ) -> None:
        """Start a live stream and pass the browser's offer to the vehicle."""
        if self._sessions or self._pending:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="live_stream_open"
            )
        self._pending[session_id] = []
        socket: ClientWebSocketResponse | None = None
        try:
            try:
                config = await self.coordinator.start_gear_guard_live_stream(
                    self._camera
                )
            except RivianApiException as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="live_stream_failed",
                    translation_placeholders={"error": str(err)},
                ) from err
            if not config:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="live_stream_refused"
                )
            # The viewer may have closed while the stream was starting
            if session_id not in self._pending:
                return
            # The stream's own TURN servers (config["iceServers"]) go unused: HA
            # asks for the browser's ICE servers before there is an offer
            try:
                socket = await async_get_clientsession(self.hass).ws_connect(
                    config["endpoint"]
                )
                await socket.send_str(
                    _signal("SDP_OFFER", {"type": "offer", "sdp": offer_sdp})
                )
                for candidate in self._pending.get(session_id, []):
                    await socket.send_str(_signal("ICE_CANDIDATE", candidate.to_dict()))
            except ClientError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="live_stream_unreachable",
                    translation_placeholders={"error": str(err)},
                ) from err
            if session_id not in self._pending:
                await socket.close()
                return
        except BaseException:
            # Nothing relays or closes the socket until the relay starts
            if socket:
                await socket.close()
            raise
        finally:
            self._pending.pop(session_id, None)
        self._sessions[session_id] = (socket, "")
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
