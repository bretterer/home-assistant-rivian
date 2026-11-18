"""Tests for the vehicle coordinator.

Covers schedule writes, phone pairing and the cleanup timer for temporary
precondition schedules.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from rivian.exceptions import RivianApiException

from custom_components.rivian import coordinator as coordinator_module
from custom_components.rivian.coordinator import VehicleCoordinator
from homeassistant.exceptions import HomeAssistantError


def _coordinator(data: dict[str, Any]) -> VehicleCoordinator:
    coordinator = object.__new__(VehicleCoordinator)
    coordinator.vehicle_id = "vehicle"
    coordinator.data = data
    return coordinator


def _schedule_coordinator() -> VehicleCoordinator:
    coordinator = _coordinator({})
    coordinator._departure_stale = False
    coordinator._expect_empty_schedules = False
    coordinator._empty_list_hold = None
    coordinator._departure_failure_logged = False
    coordinator._departure_subscription = 0
    return coordinator


def _received(coordinator: VehicleCoordinator, schedules: list[Any]) -> None:
    coordinator._process_departure_schedules(
        {"payload": {"data": {"vehicleDepartureSchedules": schedules}}}
    )


async def test_failed_refresh_after_an_accepted_write_isnt_an_error() -> None:
    """Rivian applied it, so reporting a failure would invite a duplicate retry."""
    coordinator = _schedule_coordinator()
    coordinator._departure_refreshed = asyncio.Event()
    coordinator._subscribe_departure_schedules = AsyncMock(
        side_effect=ConnectionError("web socket closed")
    )
    response = MagicMock()
    response.json = AsyncMock(
        return_value={"data": {"createDepartureSchedule": {"success": True}}}
    )

    async def request() -> MagicMock:
        return response

    await coordinator._departure_schedule_mutation("createDepartureSchedule", request())

    assert coordinator._departure_stale  # the next read refreshes it


async def test_unreadable_response_is_an_unknown_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An error page instead of JSON may follow a write Rivian applied."""
    monkeypatch.setattr(coordinator_module, "HomeAssistantError", _TranslatedError)
    coordinator = _schedule_coordinator()
    response = MagicMock()
    response.json = AsyncMock(side_effect=ValueError("not JSON"))

    async def request() -> MagicMock:
        return response

    with pytest.raises(_TranslatedError) as err:
        await coordinator._departure_schedule_mutation(
            "createDepartureSchedule", request()
        )
    assert err.value.translation_key == "schedule_unconfirmed"


@pytest.mark.parametrize("action", ["update", "delete"])
async def test_schedule_changes_need_the_list(
    monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    """Before the list arrives, a schedule isn't 'not found': it's out of sync."""
    monkeypatch.setattr(coordinator_module, "HomeAssistantError", _TranslatedError)
    coordinator = _schedule_coordinator()
    coordinator._departure_lock = asyncio.Lock()
    coordinator._departure_schedules = None
    coordinator._ensure_fresh_departure_schedules = AsyncMock()

    with pytest.raises(_TranslatedError) as err:
        if action == "update":
            await coordinator.update_departure_schedule("S1", {"isEnabled": True})
        else:
            await coordinator.delete_departure_schedule("S1")
    assert err.value.translation_key == "schedules_out_of_sync"


@pytest.mark.parametrize(
    ("previous", "expect_empty", "held"),
    [
        ([{"id": "a"}], False, True),
        ([], False, True),
        (None, False, True),
        ([{"id": "a"}], True, False),
    ],
    ids=["after-schedules", "again", "first", "deleting-the-last"],
)
def test_empty_schedule_list_held_back_after_schedules(
    monkeypatch: pytest.MonkeyPatch,
    previous: list[Any] | None,
    expect_empty: bool,
    held: bool,
) -> None:
    """An empty list may be brief, so it's held back, whatever came before.

    Only one expected after deleting the last schedule is used straight away.
    """
    timers = _Timers()
    monkeypatch.setattr(coordinator_module, "async_call_later", timers)
    coordinator = _schedule_coordinator()
    coordinator.hass = MagicMock()
    # confirmed by an earlier list, as before a resubscribe
    coordinator._departure_refreshed = asyncio.Event()
    coordinator._departure_refreshed.set()
    coordinator._departure_schedules = previous
    coordinator._expect_empty_schedules = expect_empty
    coordinator._precondition_pending = []
    coordinator._precondition_ids = {}
    coordinator.async_update_listeners = MagicMock()

    _received(coordinator, [])

    assert coordinator._departure_schedules == (previous if held else [])
    assert coordinator._departure_refreshed.is_set() is not held
    assert coordinator._departure_stale is held
    assert len(timers.calls) == (1 if held else 0)


def _held_coordinator(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[VehicleCoordinator, _Timers]:
    timers = _Timers()
    monkeypatch.setattr(coordinator_module, "async_call_later", timers)
    coordinator = _schedule_coordinator()
    coordinator.hass = MagicMock()
    coordinator._departure_refreshed = asyncio.Event()
    coordinator._departure_schedules = [{"id": "a"}]
    coordinator._precondition_pending = []
    coordinator._precondition_ids = {}
    coordinator.async_update_listeners = MagicMock()
    return coordinator, timers


def test_held_empty_list_dropped_for_the_full_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full list arriving in time replaces the held back empty one."""
    coordinator, timers = _held_coordinator(monkeypatch)

    _received(coordinator, [])
    _received(coordinator, [{"id": "a"}])

    timers.calls[0][2].assert_called_once()  # the hold is cancelled
    assert coordinator._departure_schedules == [{"id": "a"}]
    assert coordinator._departure_refreshed.is_set()
    assert not coordinator._departure_stale
    coordinator.async_update_listeners.assert_not_called()


def test_held_empty_list_used_if_nothing_follows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no other list by the end of the hold, they're really all gone."""
    coordinator, timers = _held_coordinator(monkeypatch)

    _received(coordinator, [])
    timers.calls[0][1](None)  # the hold ends

    assert coordinator._departure_schedules == []
    assert coordinator._departure_refreshed.is_set()
    assert not coordinator._departure_stale


class _TranslatedError(Exception):
    """Stands in for Home Assistant's errors, keeping their translation key."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args)
        self.translation_key = kwargs.get("translation_key")


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (RivianApiException("timed out"), "schedule_unconfirmed"),
        (
            RivianApiException("rejected", {"errors": [{"message": "Bad input"}]}),
            "schedule_rejected",
        ),
    ],
    ids=["timeout", "graphql-error"],
)
async def test_schedule_write_rejected_only_with_graphql_errors(
    monkeypatch: pytest.MonkeyPatch, error: Exception, key: str
) -> None:
    """Without GraphQL errors, such as after a timeout, the outcome is unknown."""
    monkeypatch.setattr(coordinator_module, "HomeAssistantError", _TranslatedError)
    coordinator = _schedule_coordinator()

    async def request() -> None:
        raise error

    with pytest.raises(_TranslatedError) as err:
        await coordinator._departure_schedule_mutation(
            "createDepartureSchedule", request()
        )
    assert err.value.translation_key == key


async def test_failed_schedule_write_marks_cache_unconfirmed() -> None:
    """A write that errors or times out may still have been applied by Rivian."""
    coordinator = _schedule_coordinator()

    async def request() -> None:
        raise RivianApiException("timed out")

    with pytest.raises(HomeAssistantError):
        await coordinator._departure_schedule_mutation(
            "updateDepartureSchedule", request()
        )
    assert coordinator._departure_stale


async def test_cancelled_schedule_write_marks_cache_unconfirmed() -> None:
    """A write cancelled mid-request has an unknown outcome."""
    coordinator = _schedule_coordinator()
    started = asyncio.Event()

    async def request() -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        coordinator._departure_schedule_mutation("updateDepartureSchedule", request())
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert coordinator._departure_stale


async def test_payload_during_schedule_write_does_not_confirm_cache() -> None:
    """A schedule payload that lands mid-request may predate the write."""
    coordinator = _schedule_coordinator()

    async def request() -> None:
        coordinator._departure_stale = False  # as an authoritative payload would
        raise RivianApiException("timed out")

    with pytest.raises(HomeAssistantError):
        await coordinator._departure_schedule_mutation(
            "updateDepartureSchedule", request()
        )
    assert coordinator._departure_stale


async def test_schedule_refresh_during_resubscribe_is_not_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refreshed list that arrives while resubscribing ends the wait at once."""
    monkeypatch.setattr(coordinator_module, "DEPARTURE_REFRESH_TIMEOUT", 60)
    coordinator = _schedule_coordinator()
    coordinator._departure_refreshed = asyncio.Event()
    coordinator._departure_schedules = []
    coordinator._precondition_pending = []
    coordinator._precondition_ids = {}
    coordinator.async_update_listeners = MagicMock()

    async def subscribe(force: bool = False) -> None:
        coordinator._process_departure_schedules(
            {"payload": {"data": {"vehicleDepartureSchedules": [{"id": "a"}]}}}
        )

    coordinator._subscribe_departure_schedules = subscribe
    response = MagicMock()
    response.json = AsyncMock(
        return_value={"data": {"updateDepartureSchedule": {"success": True}}}
    )

    async def request() -> MagicMock:
        return response

    # a lost notification would wait until the timeout, which is far longer
    await asyncio.wait_for(
        coordinator._departure_schedule_mutation("updateDepartureSchedule", request()),
        5,
    )
    assert coordinator._departure_refreshed.is_set()
    assert not coordinator._departure_stale


def _failing_subscription(subscription: int) -> tuple[VehicleCoordinator, AsyncMock]:
    coordinator = _coordinator({})
    coordinator._departure_subscribe_lock = asyncio.Lock()
    coordinator._departure_subscription = subscription
    coordinator._departure_failure_logged = False
    coordinator._empty_list_hold = None
    coordinator._departure_stale = False
    unsubscribe = AsyncMock()
    coordinator._unsub_departure_handler = unsubscribe
    coordinator.hass = MagicMock()
    coordinator.tasks = []
    coordinator.config_entry = MagicMock(
        async_create_task=lambda hass, coro, eager_start=True: coordinator.tasks.append(
            asyncio.ensure_future(coro)
        )
    )
    return coordinator, unsubscribe


async def test_failed_departure_subscription_is_retried() -> None:
    """A subscription Rivian ended is dropped, so the next refresh subscribes again."""
    coordinator, unsubscribe = _failing_subscription(1)

    coordinator._departure_subscription_failed(1, {"payload": [{"message": "nope"}]})
    await asyncio.gather(*coordinator.tasks)

    unsubscribe.assert_awaited_once()
    assert coordinator._unsub_departure_handler is None


async def test_earlier_subscription_error_keeps_the_new_one() -> None:
    """An error from a replaced subscription doesn't end the one that replaced it."""
    coordinator, unsubscribe = _failing_subscription(2)

    coordinator._departure_subscription_failed(1, {"payload": [{"message": "nope"}]})
    await asyncio.gather(*coordinator.tasks)

    unsubscribe.assert_not_awaited()
    assert coordinator._unsub_departure_handler is unsubscribe


async def test_repeated_subscription_failures_warn_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Retried each refresh, a failure warns once until schedules arrive again."""
    coordinator, _ = _failing_subscription(1)

    with caplog.at_level("DEBUG"):
        coordinator._departure_subscription_failed(1, {"payload": []})
        coordinator._departure_subscription_failed(1, {"payload": []})
    await asyncio.gather(*coordinator.tasks)

    assert [r.levelname for r in caplog.records].count("WARNING") == 1


def test_hold_from_a_replaced_subscription_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only an empty list from the current subscription can confirm the cache."""
    coordinator, timers = _held_coordinator(monkeypatch)

    _received(coordinator, [])
    coordinator._departure_subscription += 1  # resubscribed meanwhile
    timers.calls[0][1](None)  # the old hold ends

    assert coordinator._departure_schedules == [{"id": "a"}]
    assert not coordinator._departure_refreshed.is_set()


@pytest.mark.parametrize(
    ("result", "key"),
    [
        (
            {"data": {"createDepartureSchedule": {"success": False}}},
            "schedule_rejected",
        ),
        ({"data": {"createDepartureSchedule": None}}, "schedule_unconfirmed"),
        ({"data": None}, "schedule_unconfirmed"),
    ],
    ids=["failed", "no-result", "no-data"],
)
async def test_only_an_explicit_failure_is_rejected(
    monkeypatch: pytest.MonkeyPatch, result: dict[str, Any], key: str
) -> None:
    """No result, such as partial data with an error, may follow a write."""
    monkeypatch.setattr(coordinator_module, "HomeAssistantError", _TranslatedError)
    coordinator = _schedule_coordinator()
    response = MagicMock()
    response.json = AsyncMock(return_value=result)

    async def request() -> MagicMock:
        return response

    with pytest.raises(_TranslatedError) as err:
        await coordinator._departure_schedule_mutation(
            "createDepartureSchedule", request()
        )
    assert err.value.translation_key == key


async def test_update_keeps_settings_it_doesnt_name() -> None:
    """A temperature-only update sends the schedule's other settings unchanged."""
    coordinator = _schedule_coordinator()
    coordinator._departure_lock = asyncio.Lock()
    coordinator._departure_schedules = [
        {
            "id": "S1",
            "name": "Weekdays",
            "isEnabled": True,
            "occurrence": {
                "__typename": "RepeatsWeekly",
                "days": ["Monday"],
                "startsAtMin": 450,
            },
            "departureSettings": {
                "shouldOverrideChargeSchedule": False,
                "comfortSettings": {
                    "cabinTempCelsius": 21,
                    "frontDefogDefrost": "Defrost",
                    "surfaceHeatVentLevels": {"frontLeftSeat": "Heat2"},
                },
            },
        }
    ]
    coordinator._ensure_fresh_departure_schedules = AsyncMock()
    coordinator._departure_schedule_mutation = AsyncMock()
    coordinator.api = MagicMock()

    await coordinator.update_departure_schedule(
        "S1", {"departureSettings": {"comfortSettings": {"cabinTempCelsius": 23}}}
    )

    sent = coordinator.api.update_departure_schedule.call_args.args[2]
    comfort = sent["departureSettings"]["comfortSettings"]
    assert comfort == {
        "cabinTempCelsius": 23,
        "frontDefogDefrost": "Defrost",
        "surfaceHeatVentLevels": {"frontLeftSeat": "Heat2"},
    }
    assert sent["repeatsWeekly"] == {"days": ["Monday"], "startsAtMin": 450}
    assert sent["name"] == "Weekdays"


class _Drivers:
    def __init__(self, device: dict[str, Any] | None) -> None:
        self.device = device

    def get_device_details(self, identity_id: str) -> dict[str, Any] | None:
        return self.device


@pytest.mark.parametrize(
    ("identity_id", "device", "paired"),
    [
        (None, None, False),
        ("identity", None, False),
        ("identity", {"isPaired": False}, False),
        ("identity", {"isPaired": True}, True),
        ("identity", {"isPaired": True, "isEnabled": True}, True),
        ("identity", {"isPaired": True, "isEnabled": False}, False),
    ],
    ids=[
        "not-enrolled",
        "enrolled-no-device",
        "enrolled-unpaired",
        "paired",
        "paired-enabled",
        "paired-disabled",
    ],
)
def test_has_paired_phone(
    identity_id: str | None, device: dict[str, Any] | None, paired: bool
) -> None:
    """Keyless controls depend on completed pairing, not on enrollment alone."""
    coordinator = _coordinator({})
    coordinator.drivers_coordinator = _Drivers(device)
    assert coordinator.has_paired_phone(identity_id) is paired


class _Timers:
    """Capture async_call_later calls in place of Home Assistant's scheduler."""

    def __init__(self) -> None:
        self.calls: list[tuple[float, Any, MagicMock]] = []

    def __call__(self, hass: Any, delay: float, action: Any) -> MagicMock:
        cancel = MagicMock()
        self.calls.append((delay, action, cancel))
        return cancel

    @property
    def delay(self) -> float:
        return self.calls[-1][0]


@pytest.fixture
def timers(monkeypatch: pytest.MonkeyPatch) -> _Timers:
    timers = _Timers()
    monkeypatch.setattr(coordinator_module, "async_call_later", timers)
    return timers


async def test_overlapping_departure_subscribes_open_one() -> None:
    """A refresh during a mutation's resubscribe doesn't open a second one."""
    coordinator = _coordinator({})
    coordinator._unsub_departure_handler = None
    coordinator._departure_subscribe_lock = asyncio.Lock()
    coordinator._departure_subscription = 0
    coordinator._empty_list_hold = None
    coordinator._shutting_down = False

    async def subscribe(vehicle_id: str, callback: Any, **kwargs: Any) -> AsyncMock:
        await asyncio.sleep(0)
        return AsyncMock()

    coordinator.api = MagicMock(
        subscribe_for_departure_schedules=AsyncMock(side_effect=subscribe)
    )

    await asyncio.gather(
        coordinator._subscribe_departure_schedules(),
        coordinator._subscribe_departure_schedules(),
    )

    coordinator.api.subscribe_for_departure_schedules.assert_awaited_once()
