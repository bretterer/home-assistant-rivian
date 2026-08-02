"""Decoder for Rivian Parallax protobuf payloads.

Decodes base64-encoded protobuf binary payloads from the Parallax WebSocket
subscription into structured Python dicts that match the field names expected
by the existing ChargingCoordinator and its sensors.

Reference: https://github.com/kaedenbrinkman/rivian-api (RivDocs)
APK message classes mapped via cq/f.smali, l70/*, k70/*, f70/*, g70/*
"""

from __future__ import annotations

import base64
import logging
import struct
from typing import Any

_LOGGER = logging.getLogger(__name__)


def _decode_varint(data: bytes, offset: int) -> tuple[int, int]:
    """Decode a protobuf varint, return (value, new_offset)."""
    result = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        result |= (byte & 0x7F) << shift
        shift += 7
        offset += 1
        if not (byte & 0x80):
            break
    return result, offset


def _decode_protobuf_fields(data: bytes) -> list[tuple[int, int, Any]]:
    """Decode raw protobuf bytes into a list of (field_number, wire_type, value) tuples."""
    fields = []
    i = 0
    while i < len(data):
        tag, i = _decode_varint(data, i)
        field_num = tag >> 3
        wire_type = tag & 0x07

        if wire_type == 0:  # Varint
            value, i = _decode_varint(data, i)
            fields.append((field_num, wire_type, value))
        elif wire_type == 1:  # 64-bit (double)
            if i + 8 <= len(data):
                value = struct.unpack("<d", data[i : i + 8])[0]
                i += 8
                fields.append((field_num, wire_type, value))
            else:
                break
        elif wire_type == 2:  # Length-delimited
            length, i = _decode_varint(data, i)
            if i + length <= len(data):
                value = data[i : i + length]
                i += length
                fields.append((field_num, wire_type, value))
            else:
                break
        elif wire_type == 5:  # 32-bit (float)
            if i + 4 <= len(data):
                value = struct.unpack("<f", data[i : i + 4])[0]
                i += 4
                fields.append((field_num, wire_type, value))
            else:
                break
        else:
            _LOGGER.debug("Unknown wire type %d for field %d", wire_type, field_num)
            break

    return fields


def decode_battery_state(payload_b64: str) -> dict[str, Any]:
    """Decode energy.high_voltage.battery_state (APK: l70/p).

    Returns dict with keys:
        - soc: float (percentage, 0-100)
        - pack_energy_kwh: float
        - range_km: float (if present)
    """
    if not payload_b64:
        return {}
    try:
        data = base64.b64decode(payload_b64)
        fields = _decode_protobuf_fields(data)
        result: dict[str, Any] = {}

        for field_num, wire_type, value in fields:
            if field_num == 1 and wire_type == 2:
                # Nested charge_state message (l70/k)
                inner_fields = _decode_protobuf_fields(value)
                for inner_num, inner_wt, inner_val in inner_fields:
                    if inner_num == 1 and inner_wt == 1:  # soc (double)
                        result["soc"] = round(inner_val, 2)
                    elif inner_num == 2 and inner_wt == 1:  # pack_energy_kwh (double)
                        result["pack_energy_kwh"] = round(inner_val, 2)
                    elif inner_num == 3 and inner_wt == 5:  # range_km (float)
                        result["range_km"] = round(inner_val, 1)

        return result
    except Exception:
        _LOGGER.debug("Failed to decode battery_state payload", exc_info=True)
        return {}


def decode_charge_session_breakdown(payload_b64: str) -> dict[str, Any]:
    """Decode energy_edge_compute.graphs.charge_session_breakdown (APK: k70/b).

    Returns dict with keys matching legacy getLiveSessionData field names:
        - totalChargedEnergy: float (kWh total)
        - power: float (kW, current charge rate)
        - timeElapsed: int (seconds, estimated)
        - rangeAddedThisSession: float (km, estimated from energy)
    """
    if not payload_b64:
        return {}
    try:
        data = base64.b64decode(payload_b64)
        fields = _decode_protobuf_fields(data)
        result: dict[str, Any] = {}

        total_kwh = 0.0
        pack_kwh = 0.0

        for field_num, wire_type, value in fields:
            if field_num == 1 and wire_type == 5:  # totalKwh (float)
                total_kwh = round(value, 4)
                result["totalChargedEnergy"] = total_kwh
            elif field_num == 2 and wire_type == 5:  # packKwh (float)
                pack_kwh = round(value, 4)
            elif field_num == 9 and wire_type == 5:  # currentPower (float, kW)
                result["power"] = round(value, 2)
            elif field_num == 7 and wire_type == 0:  # timeRemainingMins or elapsed secs
                result["_time_field_7"] = value
            elif field_num == 10 and wire_type == 0:  # charge power integer (kW)
                if "power" not in result:
                    result["power"] = float(value)
            elif field_num == 13 and wire_type == 0:  # chargingState enum
                result["_charging_state"] = value

        # Estimate range added: ~3.5 km/kWh is a reasonable Rivian average
        if total_kwh > 0:
            result["rangeAddedThisSession"] = round(total_kwh * 3.5, 1)

        return result
    except Exception:
        _LOGGER.debug(
            "Failed to decode charge_session_breakdown payload", exc_info=True
        )
        return {}


def decode_charging_session_status(payload_b64: str) -> dict[str, Any]:
    """Decode charging.session.status (APK: f70/v).

    Returns dict with keys:
        - plugConnectionStatus: int (enum)
        - displayStatus: int (enum)
        - evseType: int (enum)
    """
    if not payload_b64:
        return {}
    try:
        data = base64.b64decode(payload_b64)
        fields = _decode_protobuf_fields(data)
        result: dict[str, Any] = {}

        for field_num, wire_type, value in fields:
            if field_num == 1 and wire_type == 0:
                result["plugConnectionStatus"] = value
            elif field_num == 2 and wire_type == 0:
                result["displayStatus"] = value
            elif field_num == 3 and wire_type == 0:
                result["evseType"] = value

        return result
    except Exception:
        _LOGGER.debug(
            "Failed to decode charging.session.status payload", exc_info=True
        )
        return {}


def decode_time_estimation(payload_b64: str) -> dict[str, Any]:
    """Decode charging.session.time_estimation (APK: g70/e0).

    Returns dict with keys:
        - timeToEndOfCharge: int (seconds remaining)
    """
    if not payload_b64:
        return {}
    try:
        data = base64.b64decode(payload_b64)
        fields = _decode_protobuf_fields(data)
        result: dict[str, Any] = {}

        for field_num, wire_type, value in fields:
            if field_num == 1 and wire_type == 0:
                result["timeToEndOfCharge"] = value

        return result
    except Exception:
        _LOGGER.debug("Failed to decode time_estimation payload", exc_info=True)
        return {}


# Map of RVM topic -> decoder function
RVM_DECODERS: dict[str, callable] = {
    "energy.high_voltage.battery_state": decode_battery_state,
    "energy_edge_compute.graphs.charge_session_breakdown": decode_charge_session_breakdown,
    "charging.session.status": decode_charging_session_status,
    "charging.session.time_estimation": decode_time_estimation,
}

# RVMs that the ChargingCoordinator should subscribe to for live charging data
CHARGING_RVMS: list[str] = [
    "energy.high_voltage.battery_state",
    "energy_edge_compute.graphs.charge_session_breakdown",
    "energy_edge_compute.graphs.charging_graph_global",
    "charging.session.status",
    "charging.session.time_estimation",
    "charging.session.notification",
    "charging.session.soc_slider",
    "charging.session.remote_command",
]


def decode_parallax_message(rvm: str, payload_b64: str) -> dict[str, Any] | None:
    """Decode a Parallax message payload given its RVM topic.

    Returns a dict of decoded fields, or None if no decoder exists for this RVM.
    """
    if decoder := RVM_DECODERS.get(rvm):
        return decoder(payload_b64)
    return None
