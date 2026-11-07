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
