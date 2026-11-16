"""Tests for the departure schedule actions' permission checks."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.rivian import services
from homeassistant.exceptions import Unauthorized, UnknownUser

DEVICE_ID = "vehicle-device"
ENTITIES = {
    DEVICE_ID: ["switch.r1t_departure_schedule_weekdays", "sensor.r1t_battery"],
    "other-device": ["switch.other_vehicle_departure_schedule"],
}


def _user(allowed: set[str] | None = None) -> SimpleNamespace:
    allowed = allowed or set()
    return SimpleNamespace(
        permissions=SimpleNamespace(
            check_entity=lambda entity_id, policy: entity_id in allowed
        )
    )


def _call(user_id: str | None) -> SimpleNamespace:
    # keyed by the module's own constants, which the test harness may mock
    return SimpleNamespace(
        context=SimpleNamespace(user_id=user_id),
        data={
            services.ATTR_DEVICE_ID: DEVICE_ID,
            services.ATTR_SCHEDULE_ID: "schedule",
        },
    )


@pytest.fixture
def hass(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Home Assistant stand-in with the registries the checks use."""
    monkeypatch.setattr(
        services,
        "er",
        SimpleNamespace(
            async_get=lambda hass: "registry",
            async_entries_for_device=lambda registry, device_id: [
                SimpleNamespace(entity_id=entity_id)
                for entity_id in ENTITIES.get(device_id, [])
            ],
        ),
        raising=False,
    )
    return SimpleNamespace(auth=SimpleNamespace(async_get_user=AsyncMock()))


@pytest.fixture
def coordinator(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    coordinator = MagicMock()
    coordinator.delete_departure_schedule = AsyncMock()
    monkeypatch.setattr(
        services, "_get_coordinator", lambda hass, device_id: coordinator
    )
    return coordinator


def _handlers(hass: SimpleNamespace) -> dict[str, Any]:
    handlers: dict[str, Any] = {}
    hass.services = SimpleNamespace(
        async_register=lambda domain, service, handler, schema: handlers.__setitem__(
            service, handler
        )
    )
    services.async_setup_services(hass)
    return handlers


async def test_user_without_vehicle_control_is_denied(
    hass: SimpleNamespace, coordinator: MagicMock
) -> None:
    hass.auth.async_get_user.return_value = _user(
        {"switch.other_vehicle_departure_schedule"}
    )
    delete = _handlers(hass)[services.SERVICE_DELETE_DEPARTURE_SCHEDULE]

    with pytest.raises(Unauthorized):
        await delete(_call("user"))
    coordinator.delete_departure_schedule.assert_not_awaited()


async def test_user_with_vehicle_control_is_allowed(
    hass: SimpleNamespace, coordinator: MagicMock
) -> None:
    hass.auth.async_get_user.return_value = _user(
        {"switch.r1t_departure_schedule_weekdays"}
    )
    delete = _handlers(hass)[services.SERVICE_DELETE_DEPARTURE_SCHEDULE]

    await delete(_call("user"))
    coordinator.delete_departure_schedule.assert_awaited_once_with("schedule")


async def test_unknown_user_is_denied(
    hass: SimpleNamespace, coordinator: MagicMock
) -> None:
    hass.auth.async_get_user.return_value = None
    delete = _handlers(hass)[services.SERVICE_DELETE_DEPARTURE_SCHEDULE]

    with pytest.raises(UnknownUser):
        await delete(_call("missing"))
    coordinator.delete_departure_schedule.assert_not_awaited()


async def test_call_without_user_is_trusted(
    hass: SimpleNamespace, coordinator: MagicMock
) -> None:
    """Automations and scripts call without a user and are not checked."""
    delete = _handlers(hass)[services.SERVICE_DELETE_DEPARTURE_SCHEDULE]

    await delete(_call(None))
    hass.auth.async_get_user.assert_not_awaited()
    coordinator.delete_departure_schedule.assert_awaited_once_with("schedule")


@pytest.mark.parametrize(
    "service",
    [
        services.SERVICE_CREATE_DEPARTURE_SCHEDULE,
        services.SERVICE_UPDATE_DEPARTURE_SCHEDULE,
        services.SERVICE_DELETE_DEPARTURE_SCHEDULE,
    ],
)
async def test_every_schedule_action_checks_permission(
    hass: SimpleNamespace, coordinator: MagicMock, service: str
) -> None:
    hass.auth.async_get_user.return_value = _user()
    handler = _handlers(hass)[service]

    with pytest.raises(Unauthorized):
        await handler(_call("user"))
    assert not coordinator.method_calls


class _TranslatedError(Exception):
    """Stands in for Home Assistant's errors, keeping their translation."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(kwargs.get("translation_key"))
        self.translation_key = kwargs.get("translation_key")
        self.translation_placeholders = kwargs.get("translation_placeholders")


@pytest.fixture
def devices(monkeypatch: pytest.MonkeyPatch) -> dict[str, SimpleNamespace]:
    """Device registry stand-in; tests add devices by id."""
    registry: dict[str, SimpleNamespace] = {}
    monkeypatch.setattr(
        services,
        "dr",
        SimpleNamespace(async_get=lambda hass: SimpleNamespace(async_get=registry.get)),
        raising=False,
    )
    monkeypatch.setattr(services, "ServiceValidationError", _TranslatedError)
    return registry


def _loaded(hass: SimpleNamespace, coordinator: MagicMock) -> None:
    hass.data = {
        services.DOMAIN: {
            "entry": {
                services.ATTR_COORDINATOR: {
                    services.ATTR_VEHICLE: {"vid": coordinator}
                },
                services.ATTR_VEHICLE: {"vid": {"vin": "VIN"}},
            }
        }
    }


@pytest.mark.parametrize("identifier", ["vid", "VIN"])
def test_vehicle_found_by_id_or_vin(
    hass: SimpleNamespace, devices: dict[str, SimpleNamespace], identifier: str
) -> None:
    coordinator = MagicMock()
    _loaded(hass, coordinator)
    devices["device"] = SimpleNamespace(
        identifiers={(services.DOMAIN, identifier)}, config_entries={"entry"}
    )
    assert services._get_coordinator(hass, "device") is coordinator


async def test_unknown_device_is_invalid_not_unauthorized(
    hass: SimpleNamespace, devices: dict[str, SimpleNamespace]
) -> None:
    """The vehicle lookup runs before the permission check."""
    _loaded(hass, MagicMock())
    hass.auth.async_get_user.return_value = _user()
    delete = _handlers(hass)[services.SERVICE_DELETE_DEPARTURE_SCHEDULE]

    with pytest.raises(_TranslatedError) as err:
        await delete(_call("user"))
    assert err.value.translation_key == "invalid_device"
    assert err.value.translation_placeholders == {"device_id": DEVICE_ID}


def test_wallbox_device_is_invalid(
    hass: SimpleNamespace, devices: dict[str, SimpleNamespace]
) -> None:
    """A device of a loaded entry that isn't a vehicle is a wallbox."""
    _loaded(hass, MagicMock())
    devices["wallbox"] = SimpleNamespace(
        identifiers={(services.DOMAIN, "serial")}, config_entries={"entry"}
    )
    with pytest.raises(_TranslatedError) as err:
        services._get_coordinator(hass, "wallbox")
    assert err.value.translation_key == "invalid_device"


def test_vehicle_of_unloaded_entry(
    hass: SimpleNamespace, devices: dict[str, SimpleNamespace]
) -> None:
    _loaded(hass, MagicMock())
    devices["device"] = SimpleNamespace(
        identifiers={(services.DOMAIN, "other")}, config_entries={"unloaded"}
    )
    with pytest.raises(_TranslatedError) as err:
        services._get_coordinator(hass, "device")
    assert err.value.translation_key == "vehicle_not_loaded"
