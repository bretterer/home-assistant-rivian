"""Signed command routing and no-retry regression tests."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.rivian import coordinator as module


class Commands(type):
    def __getattr__(cls, name):
        return name


class VehicleCommand(metaclass=Commands):
    pass


@pytest.fixture
def vehicle(monkeypatch):
    monkeypatch.setattr(module, "VehicleCommand", VehicleCommand)
    coor = object.__new__(module.VehicleCoordinator)
    coor.vehicle_id = "vehicle"
    coor.config_entry = SimpleNamespace(entry_id="entry", options={})
    coor.api = SimpleNamespace(send_vehicle_command=AsyncMock(return_value=None))
    coor.data = {"powerState": {"value": "ready"}}
    details = {
        "supported_features": [],
        "phone_identity_id": "identity",
        "public_key": "synthetic",
    }
    user = SimpleNamespace(
        get_enrolled_phone_data=MagicMock(return_value=("phone", {}))
    )
    coor.hass = SimpleNamespace(
        data={
            module.DOMAIN: {
                "entry": {
                    module.ATTR_VEHICLE: {"vehicle": details},
                    module.ATTR_COORDINATOR: {module.ATTR_USER: user},
                }
            }
        }
    )
    execute = AsyncMock()
    monkeypatch.setattr(module, "execute_command", execute)
    return coor, details, execute


@pytest.mark.parametrize(
    "feature,command",
    [
        ("PVS_COMF_CMD", "CABIN_PRECONDITIONING_SET_TEMP"),
        ("PVS_ENRG_CMD", "CHARGING_LIMITS"),
    ],
)
async def test_control_requires_its_feature(vehicle, monkeypatch, feature, command):
    coor, details, execute = vehicle
    encoder = MagicMock(return_value=("example.command", b"payload"))
    monkeypatch.setattr(module, "CONTROL_COMMAND_FEATURES", {command: feature})
    monkeypatch.setattr(module, "encode_control_command", encoder)
    params = {"value": 21}
    await coor.send_vehicle_command(command, params)
    execute.assert_not_awaited()
    encoder.assert_not_called()
    coor.api.send_vehicle_command.assert_awaited_once()
    coor.api.send_vehicle_command.reset_mock()
    details["supported_features"] = [feature]
    await coor.send_vehicle_command(command, params)
    encoder.assert_called_once_with(command, params)
    assert execute.await_args.args == (
        coor.api,
        "vehicle",
        "example.command",
        b"payload",
    )
    coor.api.send_vehicle_command.assert_not_awaited()


@pytest.mark.parametrize("power,timeout", [("ready", 90), ("sleep", 120)])
async def test_signed_lock_does_not_issue_separate_wake(
    vehicle, monkeypatch, power, timeout
):
    coor, details, execute = vehicle
    coor.data["powerState"]["value"] = power
    details["supported_features"] = ["PVS_BD_CMD"]
    monkeypatch.setattr(module, "encode_lock", lambda: b"")
    await coor.send_vehicle_command("LOCK_ALL_CLOSURES_FEEDBACK")
    assert execute.await_args.args == (coor.api, "vehicle", "body.locks.lock", b"")
    assert execute.await_args.kwargs["timeout"] == timeout
    coor.api.send_vehicle_command.assert_not_awaited()


async def test_operation_error_never_falls_back_to_legacy(vehicle):
    coor, details, execute = vehicle
    details["supported_features"] = ["PVS_SEC_CMD"]
    execute.side_effect = module.ParallaxExecutionError("outcome unknown")
    with pytest.raises(module.HomeAssistantError, match="outcome unknown"):
        await coor.send_vehicle_command("DISABLE_GEAR_GUARD_VIDEO")
    execute.assert_awaited_once()
    coor.api.send_vehicle_command.assert_not_awaited()


@pytest.mark.parametrize(
    "command,rvm,encoder_name,feature",
    [
        ("OPEN_FRUNK", "body.closures.open", "encode_frunk", "PVS_BD_CMD"),
        ("CLOSE_FRUNK", "body.closures.close", "encode_frunk", "PVS_BD_CMD"),
        ("OPEN_ALL_WINDOWS", "body.closures.open", "encode_windows", "PVS_BD_CMD"),
        ("CLOSE_ALL_WINDOWS", "body.closures.close", "encode_windows", "PVS_BD_CMD"),
        ("LOCK_ALL_CLOSURES_FEEDBACK", "body.locks.lock", "encode_lock", "PVS_BD_CMD"),
        ("UNLOCK_ALL_CLOSURES", "body.locks.unlock", "encode_unlock", "PVS_BD_CMD"),
        (
            "ENABLE_GEAR_GUARD_VIDEO",
            "security.video_monitoring.enable_video_monitoring",
            None,
            "PVS_SEC_CMD",
        ),
        (
            "DISABLE_GEAR_GUARD_VIDEO",
            "security.video_monitoring.disable_video_monitoring",
            None,
            "PVS_SEC_CMD",
        ),
        ("PANIC_ON", "security.alarm.enable_panic", None, "PVS_SEC_CMD"),
        ("PANIC_OFF", "security.alarm.disable_panic", None, "PVS_SEC_CMD"),
    ],
)
async def test_body_and_security_routing(
    vehicle, monkeypatch, command, rvm, encoder_name, feature
):
    coor, details, execute = vehicle
    payload = b"closure" if encoder_name else b""
    if encoder_name:
        monkeypatch.setattr(module, encoder_name, lambda: payload)
    await coor.send_vehicle_command(command)
    execute.assert_not_awaited()
    coor.api.send_vehicle_command.assert_awaited_once()
    coor.api.send_vehicle_command.reset_mock()
    details["supported_features"] = [feature]
    await coor.send_vehicle_command(command)
    execute.assert_awaited_once()
    assert execute.await_args.args == (coor.api, "vehicle", rvm, payload)
    coor.api.send_vehicle_command.assert_not_awaited()


@pytest.mark.parametrize(
    "command,rvm",
    [
        ("OPEN_LIFTGATE_UNLATCH_TAILGATE", "body.closures.open"),
        ("CLOSE_LIFTGATE", "body.closures.close"),
    ],
)
@pytest.mark.parametrize(
    "features,signed",
    [
        ([], False),
        (["PVS_BD_CMD"], False),
        (["LIFTGATE_CMD"], False),
        (["PVS_BD_CMD", "LIFTGATE_CMD"], True),
    ],
)
async def test_liftgate_requires_both_features(
    vehicle, monkeypatch, command, rvm, features, signed
):
    coor, details, execute = vehicle
    details["supported_features"] = features
    monkeypatch.setattr(module, "encode_liftgate", lambda: bytes.fromhex("0a0107"))
    await coor.send_vehicle_command(command)
    if signed:
        execute.assert_awaited_once()
        assert execute.await_args.args == (
            coor.api,
            "vehicle",
            rvm,
            bytes.fromhex("0a0107"),
        )
        coor.api.send_vehicle_command.assert_not_awaited()
    else:
        execute.assert_not_awaited()
        coor.api.send_vehicle_command.assert_awaited_once()


async def test_explicit_wake_keeps_existing_command_path(vehicle):
    coor, details, execute = vehicle
    details["supported_features"] = ["PVS_BD_CMD", "LIFTGATE_CMD"]
    await coor.send_vehicle_command("WAKE_VEHICLE")
    execute.assert_not_awaited()
    coor.api.send_vehicle_command.assert_awaited_once()
