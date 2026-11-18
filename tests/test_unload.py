"""Tests for unloading the integration."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

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


class _Store:
    """A vehicle's precondition store."""

    def __init__(self, data: dict[str, Any] | None) -> None:
        self.data = data
        self.removed = False

    async def async_load(self) -> dict[str, Any] | None:
        return self.data

    async def async_remove(self) -> None:
        self.removed = True


class _Client:
    """The Rivian client, deleting schedules and sending the list on subscribe."""

    def __init__(self, schedules: list[dict[str, Any]] | None, fail: set[str]) -> None:
        # None sends no list at all
        self.schedules = schedules
        self.fail = fail
        self.deleted: list[str] = []
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
        if self.schedules is not None:
            callback({"payload": {"data": {"vehicleDepartureSchedules": []}}})
            if self.schedules:
                callback(
                    {"payload": {"data": {"vehicleDepartureSchedules": self.schedules}}}
                )
        return AsyncMock()

    async def close(self) -> None:
        self.closed = True


def _removal(
    monkeypatch: pytest.MonkeyPatch,
    stores: dict[str, _Store],
    client: _Client,
) -> tuple[SimpleNamespace, SimpleNamespace, MagicMock]:
    device = SimpleNamespace(
        identifiers={(DOMAIN, vehicle_id) for vehicle_id in stores}
        | {(DOMAIN, "VIN1"), ("other", "x")}
    )
    monkeypatch.setattr(
        rivian,
        "dr",
        SimpleNamespace(
            async_get=lambda hass: None,
            async_entries_for_config_entry=lambda registry, entry_id: [device],
        ),
    )
    monkeypatch.setattr(
        rivian,
        "precondition_store",
        lambda hass, entry, identifier: stores.get(identifier, _Store(None)),
    )
    get_client = MagicMock(return_value=client)
    monkeypatch.setattr(rivian, "get_rivian_api_from_entry", get_client)
    entry = SimpleNamespace(entry_id="entry", options={})
    return SimpleNamespace(), entry, get_client


async def test_removal_deletes_owned_precondition_schedules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing would delete them once the integration is gone."""
    store = _Store(
        {
            "owned": {"S1": 100.0},
            "pending": [{"name": "HA Precondition ab12cd", "expiry": 200.0}],
        }
    )
    client = _Client(
        schedules=[
            {"id": "S1", "name": "HA Precondition 12ab34"},
            {"id": "S2", "name": "HA Precondition ab12cd"},
            {"id": "S3", "name": "Weekdays"},
        ],
        fail=set(),
    )
    hass, entry, _ = _removal(monkeypatch, {"V1": store}, client)

    await rivian.async_remove_entry(hass, entry)

    # the pending one is found by its unique name; the user's schedule is kept
    assert sorted(client.deleted) == ["S1", "S2"]
    assert store.removed
    assert client.closed


async def test_store_kept_if_a_schedule_cant_be_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting the integration up again then picks the cleanup back up."""
    store = _Store({"owned": {"S1": 100.0}, "pending": []})
    client = _Client(schedules=[{"id": "S1", "name": "HA Precondition"}], fail={"S1"})
    hass, entry, _ = _removal(monkeypatch, {"V1": store}, client)

    await rivian.async_remove_entry(hass, entry)

    assert client.deleted == ["S1"]
    assert not store.removed


async def test_removal_without_precondition_stores_needs_no_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hass, entry, get_client = _removal(monkeypatch, {}, _Client([], set()))

    await rivian.async_remove_entry(hass, entry)

    get_client.assert_not_called()


@pytest.mark.parametrize("schedules", [None, []], ids=["no-list", "empty-list"])
async def test_pending_schedule_needs_a_list_to_remove_the_store(
    monkeypatch: pytest.MonkeyPatch,
    schedules: list[dict[str, Any]] | None,
) -> None:
    """Without a list, a pending schedule may exist, so its store is kept.

    An empty list doesn't count, as a timeout or disconnect can follow the brief
    one Rivian sends before the full list.
    """
    monkeypatch.setattr(rivian, "DEPARTURE_SCHEDULES_WAIT", 0.01)
    store = _Store(
        {"owned": {}, "pending": [{"name": "HA Precondition ab12cd", "expiry": 1.0}]}
    )
    client = _Client(schedules=schedules, fail=set())
    hass, entry, _ = _removal(monkeypatch, {"V1": store}, client)

    await rivian.async_remove_entry(hass, entry)

    assert client.deleted == []
    assert not store.removed


async def test_schedule_already_deleted_in_the_app_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One no longer in the list isn't deleted, and doesn't keep the store."""
    store = _Store({"owned": {"S1": 100.0}, "pending": []})
    client = _Client(schedules=[{"id": "S3", "name": "Weekdays"}], fail=set())
    hass, entry, _ = _removal(monkeypatch, {"V1": store}, client)

    await rivian.async_remove_entry(hass, entry)

    assert client.deleted == []
    assert store.removed


async def test_store_with_nothing_left_is_removed_without_a_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A store everything was already cleaned up from needs no list."""
    store = _Store({"owned": {}, "pending": []})
    client = _Client(schedules=None, fail=set())
    hass, entry, _ = _removal(monkeypatch, {"V1": store}, client)

    await rivian.async_remove_entry(hass, entry)

    assert store.removed


async def test_unnamed_schedule_isnt_taken_for_a_pending_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pending record without a name doesn't match a schedule without one."""
    store = _Store({"owned": {}, "pending": [{"expiry": 1.0}]})
    client = _Client(schedules=[{"id": "S9"}], fail=set())
    hass, entry, _ = _removal(monkeypatch, {"V1": store}, client)

    await rivian.async_remove_entry(hass, entry)

    assert client.deleted == []


async def test_unreadable_store_doesnt_stop_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One store failing to load still lets another vehicle's be cleaned up."""

    class _Broken(_Store):
        async def async_load(self) -> dict[str, Any] | None:
            raise ValueError("corrupt")

    good = _Store({"owned": {"S1": 100.0}, "pending": []})
    client = _Client(schedules=[{"id": "S1", "name": "HA Precondition"}], fail=set())
    hass, entry, _ = _removal(monkeypatch, {"V1": _Broken(None), "V2": good}, client)

    await rivian.async_remove_entry(hass, entry)

    assert client.deleted == ["S1"]
    assert good.removed


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


def test_store_is_per_account_and_survives_adding_it_again() -> None:
    """Same account, same store, even as a new entry; another account, another."""
    from custom_components.rivian.coordinator import precondition_store

    def entry(entry_id: str, username: str) -> SimpleNamespace:
        return SimpleNamespace(entry_id=entry_id, data={"username": username})

    first = precondition_store(SimpleNamespace(), entry("e1", "Me@x.com"), "V1")
    again = precondition_store(SimpleNamespace(), entry("e2", "me@x.com "), "V1")
    other = precondition_store(SimpleNamespace(), entry("e3", "you@x.com"), "V1")

    assert first.key == again.key
    assert first.key != other.key
    assert "x.com" not in first.key
