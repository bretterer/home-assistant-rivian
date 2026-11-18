"""Tests for unloading the integration."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from custom_components import rivian
from custom_components.rivian.const import (
    ATTR_API,
    ATTR_COORDINATOR,
    ATTR_VEHICLE,
    DOMAIN,
)


async def test_vehicles_stop_before_the_client_closes() -> None:
    """Background work, such as a precondition cleanup, doesn't use a closed client."""
    order: list[str] = []
    vehicle = SimpleNamespace(
        async_shutdown=AsyncMock(side_effect=lambda: order.append("vehicle"))
    )
    api = SimpleNamespace(close=AsyncMock(side_effect=lambda: order.append("api")))
    entry = SimpleNamespace(entry_id="entry")
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_unload_platforms=AsyncMock(return_value=True)
        ),
        data={
            DOMAIN: {
                "entry": {
                    ATTR_API: api,
                    ATTR_COORDINATOR: {ATTR_VEHICLE: {"V": vehicle}},
                }
            }
        },
    )

    assert await rivian.async_unload_entry(hass, entry)

    assert order == ["vehicle", "api"]
    assert "entry" not in hass.data[DOMAIN]


class _Client:
    """The Rivian client: sends each vehicle's schedules on subscribe, and deletes."""

    def __init__(
        self,
        schedules: dict[str, list[dict[str, Any]] | None],
        fail: set[str] | None = None,
    ) -> None:
        # a vehicle mapped to None sends no list at all
        self.schedules = schedules
        self.fail = fail or set()
        self.deleted: list[str] = []
        self.disenrolled: list[str] = []
        self.closed = False

    async def create_csrf_token(self) -> None:
        pass

    async def delete_departure_schedule(self, vehicle_id: str, schedule_id: str) -> Any:
        self.deleted.append(schedule_id)
        success = schedule_id not in self.fail
        result = {"data": {"deleteDepartureSchedule": {"success": success}}}
        return SimpleNamespace(json=AsyncMock(return_value=result))

    async def subscribe_for_departure_schedules(
        self, vehicle_id: str, callback: Any
    ) -> AsyncMock:
        if (schedules := self.schedules.get(vehicle_id)) is not None:
            # Rivian can send an empty list just before the full one
            callback({"payload": {"data": {"vehicleDepartureSchedules": []}}})
            if schedules:
                callback(
                    {"payload": {"data": {"vehicleDepartureSchedules": schedules}}}
                )
        return AsyncMock()

    async def disenroll_phone(self, identity_id: str) -> None:
        self.disenrolled.append(identity_id)

    async def close(self) -> None:
        self.closed = True


def _removal(
    monkeypatch: pytest.MonkeyPatch,
    client: _Client,
    user_data: bool = True,
    public_key: str | None = None,
) -> tuple[SimpleNamespace, SimpleNamespace]:
    """Remove an entry whose account has the client's vehicles."""
    monkeypatch.setattr(rivian, "DEPARTURE_SCHEDULES_WAIT", 0.01)
    monkeypatch.setattr(rivian, "get_rivian_api_from_entry", lambda hass, entry: client)

    class _User:
        def __init__(self, **kwargs: Any) -> None:
            self.last_update_success = user_data

        async def async_refresh(self) -> None:
            pass

        def get_vehicles(self) -> dict[str, Any]:
            return {vehicle_id: {} for vehicle_id in client.schedules}

        def get_enrolled_phone_data(self, key: str) -> Any:
            return ("phone", {"V1": "identity"})

    monkeypatch.setattr(rivian, "UserCoordinator", _User)
    entry = SimpleNamespace(entry_id="entry", options={"public_key": public_key})
    return SimpleNamespace(), entry


async def test_removal_deletes_precondition_schedules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing would delete them once the integration is gone; others are kept."""
    client = _Client(
        {
            "V1": [
                {"id": "S1", "name": "HA Precondition Now"},
                {"id": "U1", "name": "Weekdays"},
            ],
            "V2": [{"id": "S2", "name": "HA Precondition Now"}],
        }
    )
    hass, entry = _removal(monkeypatch, client)

    await rivian.async_remove_entry(hass, entry)

    assert sorted(client.deleted) == ["S1", "S2"]
    assert client.closed


@pytest.mark.parametrize(
    "schedules", [None, [{"id": "U1", "name": "Weekdays"}]], ids=["no-list", "none"]
)
async def test_one_vehicle_doesnt_stop_the_next(
    monkeypatch: pytest.MonkeyPatch, schedules: list[dict[str, Any]] | None
) -> None:
    """Whether its list can't be fetched or it has none of HA's schedules."""
    client = _Client(
        {"V1": schedules, "V2": [{"id": "S2", "name": "HA Precondition Now"}]}
    )
    hass, entry = _removal(monkeypatch, client)

    await rivian.async_remove_entry(hass, entry)

    assert client.deleted == ["S2"]


async def test_failed_deletion_is_logged_and_the_phone_still_disenrolled(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client = _Client({"V1": [{"id": "S1", "name": "HA Precondition Now"}]}, fail={"S1"})
    hass, entry = _removal(monkeypatch, client, public_key="key")

    await rivian.async_remove_entry(hass, entry)

    assert "delete it in the Rivian app" in caplog.text
    assert client.disenrolled == ["identity"]


async def test_removal_without_user_data_says_what_to_delete(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client = _Client({"V1": [{"id": "S1", "name": "HA Precondition Now"}]})
    hass, entry = _removal(monkeypatch, client, user_data=False)

    await rivian.async_remove_entry(hass, entry)

    assert client.deleted == []
    assert "HA Precondition Now" in caplog.text
    assert client.closed


async def test_unload_stops_if_platforms_dont_unload() -> None:
    """Nothing is shut down or closed if the platforms couldn't unload."""
    vehicle = SimpleNamespace(async_shutdown=AsyncMock())
    api = SimpleNamespace(close=AsyncMock())
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_unload_platforms=AsyncMock(return_value=False)
        ),
        data={
            DOMAIN: {
                "entry": {
                    ATTR_API: api,
                    ATTR_COORDINATOR: {ATTR_VEHICLE: {"V": vehicle}},
                }
            }
        },
    )

    assert not await rivian.async_unload_entry(hass, SimpleNamespace(entry_id="entry"))

    vehicle.async_shutdown.assert_not_awaited()
    api.close.assert_not_awaited()
    assert "entry" in hass.data[DOMAIN]


async def test_one_vehicle_failing_to_stop_still_closes_the_client() -> None:
    order: list[str] = []
    failing = SimpleNamespace(
        vehicle_id="V1", async_shutdown=AsyncMock(side_effect=RuntimeError("ws"))
    )
    other = SimpleNamespace(
        vehicle_id="V2",
        async_shutdown=AsyncMock(side_effect=lambda: order.append("V2")),
    )
    api = SimpleNamespace(close=AsyncMock(side_effect=lambda: order.append("api")))
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_unload_platforms=AsyncMock(return_value=True)
        ),
        data={
            DOMAIN: {
                "entry": {
                    ATTR_API: api,
                    ATTR_COORDINATOR: {ATTR_VEHICLE: {"V1": failing, "V2": other}},
                }
            }
        },
    )

    assert await rivian.async_unload_entry(hass, SimpleNamespace(entry_id="entry"))

    assert order == ["V2", "api"]
