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

        # Estimate range added: ~3.5 km/kWh (~2.17 mi/kWh) is a typical Rivian average
        if total_kwh > 0:
            result["rangeAddedThisSession"] = round(total_kwh * 3.5, 1)

        # Derive charge rate (km/h) from current power (kW)
        if "power" in result:
            p = result["power"]
            result["kilometersChargedPerHour"] = round(p * 3.5, 1) if p > 0 else 0.0

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


def decode_odometer(payload_b64: str) -> dict[str, Any]:
    """Decode dynamics.vehicle.odometer.

    Returns dict with keys:
        - vehicleMileage: float (meters)
    """
    if not payload_b64:
        return {}
    try:
        data = base64.b64decode(payload_b64)
        fields = _decode_protobuf_fields(data)
        result: dict[str, Any] = {}

        for field_num, wire_type, value in fields:
            if field_num == 1 and wire_type == 0:
                # Value is distance in miles; HA expects meters (1 mile = 1609.344 meters)
                result["vehicleMileage"] = round(value * 1609.344, 1)

        return result
    except Exception:
        _LOGGER.debug("Failed to decode odometer payload", exc_info=True)
        return {}


TIRE_POSITION_MAP = {
    1: "FrontLeft",
    2: "FrontRight",
    3: "RearLeft",
    4: "RearRight",
}


def decode_tires(payload_b64: str) -> dict[str, Any]:
    """Decode dynamics.tires.state.

    Returns dict with keys:
        - tirePressureFrontLeft, tirePressureFrontRight, etc. (bar)
        - tirePressureStatusFrontLeft, etc. ("Ok")
    """
    if not payload_b64:
        return {}
    try:
        data = base64.b64decode(payload_b64)
        fields = _decode_protobuf_fields(data)
        result: dict[str, Any] = {}

        for field_num, wire_type, value in fields:
            if field_num == 2 and wire_type == 2:  # Repeated nested tire state
                inner = _decode_protobuf_fields(value)
                pos = None
                status = None
                pressure = None
                for in_num, in_type, in_val in inner:
                    if in_num == 1 and in_type == 0:
                        pos = in_val
                    elif in_num == 2 and in_type == 0:
                        status = "Ok" if in_val == 1 else "Warning"
                    elif in_num == 3 and in_type == 1:  # Double (bar)
                        pressure = round(in_val, 2)

                if pos and pos in TIRE_POSITION_MAP:
                    suffix = TIRE_POSITION_MAP[pos]
                    if pressure is not None:
                        result[f"tirePressure{suffix}"] = pressure
                    if status is not None:
                        result[f"tirePressureStatus{suffix}"] = status

        return result
    except Exception:
        _LOGGER.debug("Failed to decode tires payload", exc_info=True)
        return {}


CLOSURE_MAP = {
    1: "doorFrontLeftClosed",
    2: "doorFrontRightClosed",
    3: "doorRearLeftClosed",
    4: "doorRearRightClosed",
    5: "closureFrunkClosed",
    6: "closureSideBinLeftClosed",
    7: "closureLiftgateClosed",
}


def decode_closures(payload_b64: str) -> dict[str, Any]:
    """Decode body.closures.states.

    Returns dict with keys:
        - doorFrontLeftClosed, doorFrontRightClosed, closureFrunkClosed, etc. ("closed" / "open")
    """
    if not payload_b64:
        return {}
    try:
        data = base64.b64decode(payload_b64)
        fields = _decode_protobuf_fields(data)
        result: dict[str, Any] = {}

        for field_num, wire_type, value in fields:
            if field_num == 1 and wire_type == 2:  # Repeated nested closure state
                inner = _decode_protobuf_fields(value)
                cid = None
                state_val = None
                for in_num, in_type, in_val in inner:
                    if in_num == 1 and in_type == 0:
                        cid = in_val
                    elif in_num == 2 and in_type == 0:
                        state_val = in_val

                if cid and cid in CLOSURE_MAP and state_val is not None:
                    # 1 = open, 2 = closed
                    result[CLOSURE_MAP[cid]] = "closed" if state_val == 2 else "open"

        return result
    except Exception:
        _LOGGER.debug("Failed to decode closures payload", exc_info=True)
        return {}


LOCK_MAP = {
    1: "doorFrontLeftLocked",
    2: "doorFrontRightLocked",
    3: "doorRearLeftLocked",
    4: "doorRearRightLocked",
    5: "closureFrunkLocked",
    7: "closureLiftgateLocked",
}


def decode_locks(payload_b64: str) -> dict[str, Any]:
    """Decode body.locks.states.

    Returns dict with keys:
        - doorFrontLeftLocked, closureFrunkLocked, etc. ("locked" / "unlocked")
    """
    if not payload_b64:
        return {}
    try:
        data = base64.b64decode(payload_b64)
        fields = _decode_protobuf_fields(data)
        result: dict[str, Any] = {}

        for field_num, wire_type, value in fields:
            if field_num == 1 and wire_type == 2:  # Repeated nested lock state
                inner = _decode_protobuf_fields(value)
                lid = None
                state_val = None
                for in_num, in_type, in_val in inner:
                    if in_num == 1 and in_type == 0:
                        lid = in_val
                    elif in_num == 2 and in_type == 0:
                        state_val = in_val

                if lid and lid in LOCK_MAP and state_val is not None:
                    # 1 = unlocked, 2 = locked
                    result[LOCK_MAP[lid]] = "locked" if state_val == 2 else "unlocked"

        return result
    except Exception:
        _LOGGER.debug("Failed to decode locks payload", exc_info=True)
        return {}


def decode_cabin_temperatures(payload_b64: str) -> dict[str, Any]:
    """Decode comfort.cabin.cabin_temperatures.

    Returns dict with keys:
        - cabinClimateInteriorTemperature: float (Celsius)
    """
    if not payload_b64:
        return {}
    try:
        data = base64.b64decode(payload_b64)
        fields = _decode_protobuf_fields(data)
        result: dict[str, Any] = {}

        for field_num, wire_type, value in fields:
            if field_num == 4 and wire_type == 5:  # interior temp (float, Celsius)
                result["cabinClimateInteriorTemperature"] = round(value, 1)

        return result
    except Exception:
        _LOGGER.debug("Failed to decode cabin temperatures payload", exc_info=True)
        return {}


POWER_STATE_MAP = {
    1: "sleep",
    2: "standby",
    3: "ready",
    4: "go",
}


def decode_power_state(payload_b64: str) -> dict[str, Any]:
    """Decode vehicle.power.state.

    Returns dict with keys:
        - powerState: str ("sleep", "standby", "ready", "go")
    """
    if not payload_b64:
        return {}
    try:
        data = base64.b64decode(payload_b64)
        fields = _decode_protobuf_fields(data)
        result: dict[str, Any] = {}

        for field_num, wire_type, value in fields:
            if field_num == 1 and wire_type == 0:
                result["powerState"] = POWER_STATE_MAP.get(value, "standby")

        return result
    except Exception:
        _LOGGER.debug("Failed to decode power state payload", exc_info=True)
        return {}


def decode_gnss(payload_b64: str) -> dict[str, Any]:
    """Decode dynamics.vehicle.gnss.

    Returns dict with keys:
        - gnssLocation: {"latitude": float, "longitude": float, "timeStamp": str}
        - gnssAltitude: float (meters)
    """
    if not payload_b64:
        return {}
    try:
        data = base64.b64decode(payload_b64)
        fields = _decode_protobuf_fields(data)
        lat = None
        lon = None
        alt = None
        for field_num, wire_type, value in fields:
            if field_num == 1 and wire_type == 1:  # latitude (double)
                lat = round(value, 6)
            elif field_num == 2 and wire_type == 1:  # longitude (double)
                lon = round(value, 6)
            elif field_num == 3 and wire_type == 1:  # altitude (double)
                alt = round(value, 1)

        result: dict[str, Any] = {}
        if lat is not None and lon is not None:
            from datetime import datetime, timezone

            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f%z")
            result["gnssLocation"] = {
                "latitude": lat,
                "longitude": lon,
                "timeStamp": now_iso,
            }
        if alt is not None:
            result["gnssAltitude"] = alt
        return result
    except Exception:
        _LOGGER.debug("Failed to decode gnss payload", exc_info=True)
        return {}


def decode_preconditioning(payload_b64: str) -> dict[str, Any]:
    """Decode comfort.cabin.cabin_preconditioning_status.

    Returns dict with keys:
        - cabinPreconditioningStatus: str ("active", "initiate", "off")
    """
    if not payload_b64:
        return {"cabinPreconditioningStatus": "off"}
    try:
        data = base64.b64decode(payload_b64)
        fields = _decode_protobuf_fields(data)
        status_val = None
        for field_num, wire_type, value in fields:
            if field_num == 1 and wire_type == 0:
                status_val = value

        if status_val == 4:
            return {"cabinPreconditioningStatus": "active"}
        elif status_val in (1, 2):
            return {"cabinPreconditioningStatus": "initiate"}
        return {"cabinPreconditioningStatus": "off"}
    except Exception:
        _LOGGER.debug("Failed to decode preconditioning payload", exc_info=True)
        return {}


def decode_defrost(payload_b64: str) -> dict[str, Any]:
    """Decode comfort.cabin.defrost_defog_status.

    Returns dict with keys:
        - defrostDefogStatus: str ("Defrost", "Off")
    """
    if not payload_b64:
        return {}
    try:
        data = base64.b64decode(payload_b64)
        fields = _decode_protobuf_fields(data)
        result: dict[str, Any] = {}
        for field_num, wire_type, value in fields:
            if field_num == 1 and wire_type == 0:
                result["defrostDefogStatus"] = "Defrost" if value == 2 else "Off"
        return result
    except Exception:
        _LOGGER.debug("Failed to decode defrost payload", exc_info=True)
        return {}


def decode_charging_graph_global(payload_b64: str) -> dict[str, Any]:
    """Decode energy_edge_compute.graphs.charging_graph_global (APK: k70/k).

    Returns dict with keys:
        - startTime: str (ISO format timestamp of session start)
        - timeElapsed: int (seconds elapsed since session start)
        - power: float (kW, latest segment power)
    """
    if not payload_b64:
        return {}
    try:
        data = base64.b64decode(payload_b64)
        outer = _decode_protobuf_fields(data)
        segments = []
        for field_num, wire_type, value in outer:
            if field_num == 1 and wire_type == 2:
                inner = _decode_protobuf_fields(value)
                seg: dict[str, Any] = {}
                for in_num, in_wt, in_val in inner:
                    if in_num == 1 and in_wt == 0:
                        seg["soc"] = in_val
                    elif in_num == 2 and in_wt == 5:
                        seg["power"] = round(in_val, 2)
                    elif in_num == 3 and in_wt == 0:
                        seg["start_ms"] = in_val
                    elif in_num == 4 and in_wt == 0:
                        seg["end_ms"] = in_val
                    elif in_num == 6 and in_wt == 0:
                        seg["state"] = in_val
                segments.append(seg)

        if not segments:
            return {}

        first_seg = segments[0]
        last_seg = segments[-1]
        result: dict[str, Any] = {}

        if "start_ms" in first_seg:
            from datetime import datetime, timezone

            st = datetime.fromtimestamp(first_seg["start_ms"] / 1000, timezone.utc)
            result["startTime"] = st.strftime("%Y-%m-%dT%H:%M:%S.%f%z")

        if "start_ms" in first_seg and "end_ms" in last_seg:
            result["timeElapsed"] = max(
                0, int((last_seg["end_ms"] - first_seg["start_ms"]) / 1000)
            )

        if "power" in last_seg:
            result["power"] = last_seg["power"]
        else:
            result["power"] = 0.0
            result["kilometersChargedPerHour"] = 0.0

        return result
    except Exception:
        _LOGGER.debug(
            "Failed to decode charging_graph_global payload", exc_info=True
        )
        return {}


# Map of RVM topic -> decoder function
RVM_DECODERS: dict[str, callable] = {
    "energy.high_voltage.battery_state": decode_battery_state,
    "energy_edge_compute.graphs.charge_session_breakdown": decode_charge_session_breakdown,
    "energy_edge_compute.graphs.charging_graph_global": decode_charging_graph_global,
    "charging.session.status": decode_charging_session_status,
    "charging.session.time_estimation": decode_time_estimation,
    "dynamics.vehicle.odometer": decode_odometer,
    "dynamics.tires.state": decode_tires,
    "dynamics.vehicle.gnss": decode_gnss,
    "body.closures.states": decode_closures,
    "body.locks.states": decode_locks,
    "comfort.cabin.cabin_temperatures": decode_cabin_temperatures,
    "comfort.cabin.cabin_preconditioning_status": decode_preconditioning,
    "comfort.cabin.defrost_defog_status": decode_defrost,
    "vehicle.power.state": decode_power_state,
}

# Full list of Parallax RVMs subscribed for vehicle & charging telemetry
PARALLAX_RVMS: list[str] = list(RVM_DECODERS.keys())
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
