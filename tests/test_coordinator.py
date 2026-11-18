"""Tests for the vehicle coordinator.

Covers schedule writes, phone pairing and the cleanup timer for temporary
precondition schedules.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from rivian.exceptions import RivianApiException

from custom_components.rivian import coordinator as coordinator_module
from custom_components.rivian.const import (
    PRECONDITION_LEAD_MINUTES_MINIMUM,
    WEEK_DAYS_ORDERED,
)
from custom_components.rivian.coordinator import (
    PRECONDITION_CLEANUP_GRACE_SECONDS,
    PRECONDITION_RETRY_SECONDS,
    VehicleCoordinator,
    precondition_departure,
)
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


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 11, 14, 12, 51, 24), datetime(2026, 11, 14, 13, 12)),
        (datetime(2026, 11, 14, 12, 51, 0), datetime(2026, 11, 14, 13, 11)),
        (datetime(2026, 11, 14, 12, 51, 0, 1), datetime(2026, 11, 14, 13, 12)),
        (datetime(2026, 11, 14, 23, 45, 30), datetime(2026, 11, 15, 0, 6)),
    ],
    ids=["rounds-up", "whole-minute", "microsecond", "next-day"],
)
def test_precondition_departure_never_shortens_lead(
    now: datetime, expected: datetime
) -> None:
    """Never round down: a lead just under 15 minutes failed to precondition."""
    depart = precondition_departure(now, 20)
    assert depart == expected
    assert depart - now >= timedelta(minutes=20)


def _precondition_coordinator(
    owned: dict[str, float] | None = None,
    pending: list[dict[str, Any]] | None = None,
    data: dict[str, Any] | None = None,
) -> tuple[VehicleCoordinator, list]:
    coordinator = _coordinator(data or {})
    coordinator._precondition_ids = dict(owned or {})
    coordinator._precondition_pending = list(pending or [])
    coordinator._precondition_cleanup_lock = asyncio.Lock()
    coordinator._precondition_lock = asyncio.Lock()
    coordinator._departure_stale = False
    coordinator._expect_empty_schedules = False
    coordinator._departure_refreshed = asyncio.Event()
    coordinator._departure_schedules = [{"id": sid} for sid in owned or {}]
    coordinator._arm_precondition_timer = MagicMock()
    coordinator.update_departure_schedule = AsyncMock()
    coordinator._ensure_precondition_loaded = AsyncMock()
    coordinator._save_precondition_state = AsyncMock()
    coordinator._reconcile_and_cleanup = AsyncMock()
    created: list[dict[str, Any]] = []
    coordinator.create_departure_schedule = AsyncMock(side_effect=created.append)
    return coordinator, created


@pytest.mark.parametrize(("lead", "minutes"), [(15, 20), (10, 20), (30, 30)])
async def test_precondition_now_enforces_minimum_lead(
    monkeypatch: pytest.MonkeyPatch, lead: float, minutes: int
) -> None:
    """A lead restored from before the minimum was raised is clamped up."""
    now = datetime(2026, 11, 14, 12, 51, 24, tzinfo=UTC)
    monkeypatch.setattr(coordinator_module.dt_util, "now", lambda: now)
    coordinator, created = _precondition_coordinator()

    await coordinator.precondition_now(lead)

    depart = precondition_departure(now, minutes)
    occurrence = created[0]["repeatsWeekly"]
    assert occurrence["startsAtMin"] == depart.hour * 60 + depart.minute
    assert minutes >= PRECONDITION_LEAD_MINUTES_MINIMUM
    # cleanup waits for the rounded-up departure, not just the nominal lead
    assert coordinator._precondition_pending[0]["expiry"] == (
        depart.timestamp() + PRECONDITION_CLEANUP_GRACE_SECONDS
    )


async def test_precondition_again_moves_the_upcoming_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second press updates HA's schedule that hasn't departed, not a new one."""
    now = datetime(2026, 11, 14, 12, 51, 24, tzinfo=UTC)
    monkeypatch.setattr(coordinator_module.dt_util, "now", lambda: now)
    monkeypatch.setattr(coordinator_module.time, "time", now.timestamp)
    departs_soon = now.timestamp() + 10 * 60 + PRECONDITION_CLEANUP_GRACE_SECONDS
    coordinator, created = _precondition_coordinator(owned={"S1": departs_soon})

    await coordinator.precondition_now(30, 22.5)

    assert not created
    depart = precondition_departure(now, 30)
    coordinator.update_departure_schedule.assert_awaited_once_with(
        "S1",
        {
            "isEnabled": True,
            "repeatsWeekly": {
                "days": [WEEK_DAYS_ORDERED[depart.weekday()]],
                "startsAtMin": depart.hour * 60 + depart.minute,
            },
            "departureSettings": {"comfortSettings": {"cabinTempCelsius": 22.5}},
        },
    )
    # cleaned up after the new departure
    assert coordinator._precondition_ids["S1"] == (
        depart.timestamp() + PRECONDITION_CLEANUP_GRACE_SECONDS
    )
    coordinator._save_precondition_state.assert_awaited()


@pytest.mark.parametrize(
    ("target", "expected"),
    [(22.3, 22.5), (63.5, 29), (10.0, 16), (None, 21)],
    ids=["set", "hi", "lo", "missing"],
)
async def test_precondition_defaults_to_the_set_temperature(
    monkeypatch: pytest.MonkeyPatch, target: float | None, expected: float
) -> None:
    """Without a temperature, use the driver's set temperature, clamped, like the app."""
    now = datetime(2026, 11, 14, 12, 51, 24, tzinfo=UTC)
    monkeypatch.setattr(coordinator_module.dt_util, "now", lambda: now)
    data = {} if target is None else {"hvacTargetTemperature": {"value": target}}
    coordinator, created = _precondition_coordinator(data=data)

    await coordinator.precondition_now()

    comfort = created[0]["departureSettings"]["comfortSettings"]
    assert comfort["cabinTempCelsius"] == expected
    # and the default lead
    depart = precondition_departure(now, 20)
    occurrence = created[0]["repeatsWeekly"]
    assert occurrence["startsAtMin"] == depart.hour * 60 + depart.minute


async def test_cleanup_follows_a_departure_moved_in_the_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A schedule moved later in the app isn't deleted at its original time."""
    zone = ZoneInfo("America/Denver")
    monkeypatch.setattr(
        coordinator_module.dt_util, "as_local", lambda value: value.astimezone(zone)
    )
    monkeypatch.setattr(
        coordinator_module.dt_util,
        "utc_from_timestamp",
        lambda timestamp: datetime.fromtimestamp(timestamp, UTC),
    )
    # created to depart Wednesday 12:12, moved to 13:30 in the app
    departure = datetime(2026, 11, 18, 12, 12, tzinfo=zone)
    moved = datetime(2026, 11, 18, 13, 30, tzinfo=zone)
    now = departure + timedelta(minutes=10)
    monkeypatch.setattr(coordinator_module.time, "time", now.timestamp)
    schedule = {
        "id": "S1",
        "occurrence": {"days": ["Wednesday"], "startsAtMin": 13 * 60 + 30},
    }
    coordinator = _cleanup_coordinator(
        owned={"S1": departure.timestamp() + PRECONDITION_CLEANUP_GRACE_SECONDS},
        schedules=[schedule],
    )

    await coordinator._cleanup_precondition_schedules()

    coordinator.delete_departure_schedule.assert_not_awaited()
    assert coordinator._precondition_ids["S1"] == (
        moved.timestamp() + PRECONDITION_CLEANUP_GRACE_SECONDS
    )
    coordinator._save_precondition_state.assert_awaited()


def _denver(monkeypatch: pytest.MonkeyPatch) -> ZoneInfo:
    zone = ZoneInfo("America/Denver")
    monkeypatch.setattr(
        coordinator_module.dt_util, "as_local", lambda value: value.astimezone(zone)
    )
    monkeypatch.setattr(
        coordinator_module.dt_util,
        "utc_from_timestamp",
        lambda timestamp: datetime.fromtimestamp(timestamp, UTC),
    )
    return zone


def test_unchanged_departed_schedule_keeps_its_departure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One that has departed unchanged isn't moved to next week, so it's cleaned up."""
    zone = _denver(monkeypatch)
    departure = datetime(2026, 11, 18, 12, 12, tzinfo=zone)
    schedule = {"occurrence": {"days": ["Wednesday"], "startsAtMin": 12 * 60 + 12}}

    result = coordinator_module.precondition_schedule_departure(
        schedule, departure.timestamp(), departure.timestamp() + 3600
    )

    assert result == departure


def test_edited_schedule_that_has_passed_is_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Edited to a time that has passed since, such as while HA was down.

    It has departed, so it's cleaned up rather than kept for another week.
    """
    zone = _denver(monkeypatch)
    departure = datetime(2026, 11, 18, 12, 12, tzinfo=zone)  # a Wednesday
    now = datetime(2026, 11, 19, 9, 0, tzinfo=zone)  # the next day
    schedule = {"occurrence": {"days": ["Wednesday"], "startsAtMin": 13 * 60}}

    result = coordinator_module.precondition_schedule_departure(
        schedule, departure.timestamp(), now.timestamp()
    )

    assert result == datetime(2026, 11, 18, 13, 0, tzinfo=zone)
    assert result.timestamp() < now.timestamp()


def test_edited_schedule_takes_its_next_occurrence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Moved to a day that has passed this week, it departs next week, not then."""
    zone = _denver(monkeypatch)
    departure = datetime(2026, 11, 18, 12, 12, tzinfo=zone)  # a Wednesday
    now = departure - timedelta(minutes=10)
    schedule = {"occurrence": {"days": ["Tuesday"], "startsAtMin": 13 * 60}}

    result = coordinator_module.precondition_schedule_departure(
        schedule, departure.timestamp(), now.timestamp()
    )

    assert result == datetime(2026, 11, 24, 13, 0, tzinfo=zone)


def test_repeated_wall_time_keeps_its_instant(monkeypatch: pytest.MonkeyPatch) -> None:
    """01:30 happens twice as the clocks go back; the recorded one is kept."""
    zone = _denver(monkeypatch)
    second = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=1)
    schedule = {"occurrence": {"days": ["Sunday"], "startsAtMin": 90}}

    result = coordinator_module.precondition_schedule_departure(
        schedule, second.timestamp(), second.timestamp() - 600
    )

    assert result is not None
    assert result.timestamp() == second.timestamp()


async def test_schedule_moved_just_before_deletion_is_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rechecked as it stands just before deleting, one moved later isn't deleted."""
    zone = _denver(monkeypatch)
    departure = datetime(2026, 11, 18, 12, 12, tzinfo=zone)
    now = departure + timedelta(minutes=5)
    monkeypatch.setattr(coordinator_module.time, "time", now.timestamp)
    coordinator = _cleanup_coordinator(
        owned={"S1": departure.timestamp() + PRECONDITION_CLEANUP_GRACE_SECONDS}
    )
    keep = coordinator._precondition_moved("S1")

    moved = {"occurrence": {"days": ["Wednesday"], "startsAtMin": 13 * 60}}
    assert keep(moved)
    assert coordinator._precondition_ids["S1"] == (
        datetime(2026, 11, 18, 13, 0, tzinfo=zone).timestamp()
        + PRECONDITION_CLEANUP_GRACE_SECONDS
    )
    unchanged = {"occurrence": {"days": ["Wednesday"], "startsAtMin": 13 * 60}}
    coordinator._precondition_ids["S1"] = (
        departure.timestamp() + PRECONDITION_CLEANUP_GRACE_SECONDS
    )
    unchanged["occurrence"]["startsAtMin"] = 12 * 60 + 12
    assert not keep(unchanged)


async def test_precondition_needs_the_schedule_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the list, HA's upcoming schedule can't be found, so it's refused."""
    monkeypatch.setattr(coordinator_module, "HomeAssistantError", _TranslatedError)
    coordinator, created = _precondition_coordinator()
    coordinator._departure_schedules = None

    with pytest.raises(_TranslatedError) as err:
        await coordinator.precondition_now()

    assert err.value.translation_key == "schedules_out_of_sync"
    assert not created


async def test_rejected_precondition_does_not_block_the_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A creation Rivian rejected leaves no record that would refuse a retry."""
    now = datetime(2026, 11, 14, 12, 51, 24, tzinfo=UTC)
    monkeypatch.setattr(coordinator_module.dt_util, "now", lambda: now)
    coordinator, _ = _precondition_coordinator()
    rejected = HomeAssistantError("rejected")
    rejected.translation_key = "schedule_rejected"
    coordinator.create_departure_schedule = AsyncMock(side_effect=rejected)

    with pytest.raises(HomeAssistantError):
        await coordinator.precondition_now()

    assert coordinator._precondition_pending == []


async def test_unconfirmed_precondition_keeps_its_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a timeout the schedule may exist, so its record stays to adopt it."""
    now = datetime(2026, 11, 14, 12, 51, 24, tzinfo=UTC)
    monkeypatch.setattr(coordinator_module.dt_util, "now", lambda: now)
    coordinator, _ = _precondition_coordinator()
    unconfirmed = HomeAssistantError("unconfirmed")
    unconfirmed.translation_key = "schedule_unconfirmed"

    async def create(schedule: dict[str, Any]) -> None:
        coordinator._arm_precondition_timer.reset_mock()
        raise unconfirmed

    coordinator.create_departure_schedule = AsyncMock(side_effect=create)

    with pytest.raises(HomeAssistantError):
        await coordinator.precondition_now()

    assert len(coordinator._precondition_pending) == 1
    # retried on its own timer, as vehicle updates keep putting off a refresh
    coordinator._arm_precondition_timer.assert_called_once()


async def test_concurrent_preconditions_create_one_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second call waits for the first, then moves its schedule."""
    now = datetime(2026, 11, 14, 12, 51, 24, tzinfo=UTC)
    monkeypatch.setattr(coordinator_module.dt_util, "now", lambda: now)
    monkeypatch.setattr(coordinator_module.time, "time", now.timestamp)
    coordinator, created = _precondition_coordinator()

    async def create(schedule: dict[str, Any]) -> None:
        await asyncio.sleep(0)
        created.append(schedule)
        # Rivian's list then has it, and the reconcile adopts its id
        coordinator._departure_schedules = [{"id": "S1", "name": schedule["name"]}]
        record = coordinator._precondition_pending.pop()
        coordinator._precondition_ids["S1"] = record["expiry"]

    coordinator.create_departure_schedule = AsyncMock(side_effect=create)

    await asyncio.gather(coordinator.precondition_now(), coordinator.precondition_now())

    assert len(created) == 1
    coordinator.update_departure_schedule.assert_awaited_once()


def test_precondition_departure_across_a_dst_change() -> None:
    """The lead is real time: 01:50 plus 20 minutes on spring-forward is 03:10."""
    zone = ZoneInfo("America/Denver")
    now = datetime(2026, 3, 8, 1, 50, tzinfo=zone)

    depart = precondition_departure(now, 20)

    assert (depart.hour, depart.minute) == (3, 10)
    # same-zone subtraction is wall-clock, so compare in UTC
    assert depart.astimezone(UTC) - now.astimezone(UTC) == timedelta(minutes=20)


async def test_precondition_after_hass_schedule_was_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HA's schedule missing from the list was deleted (in the app): a new one."""
    now = datetime(2026, 11, 14, 12, 51, 24, tzinfo=UTC)
    monkeypatch.setattr(coordinator_module.dt_util, "now", lambda: now)
    monkeypatch.setattr(coordinator_module.time, "time", now.timestamp)
    departs_soon = now.timestamp() + 10 * 60 + PRECONDITION_CLEANUP_GRACE_SECONDS
    coordinator, created = _precondition_coordinator(owned={"S1": departs_soon})
    coordinator._departure_schedules = [{"id": "other"}]

    await coordinator.precondition_now()

    coordinator.update_departure_schedule.assert_not_awaited()
    assert len(created) == 1


def test_expiries_not_synced_from_an_unconfirmed_list() -> None:
    """A list that may predate HA's own move of a schedule doesn't undo it."""
    coordinator = _cleanup_coordinator(
        owned={"S1": 2000.0},
        schedules=[{"id": "S1", "occurrence": {"days": ["Monday"], "startsAtMin": 0}}],
    )
    coordinator._departure_stale = True

    assert not coordinator._sync_precondition_expiries()
    assert coordinator._precondition_ids == {"S1": 2000.0}


async def test_unreadable_stored_entries_are_skipped() -> None:
    """A malformed store doesn't fail the vehicle's setup."""
    coordinator = _coordinator({})
    coordinator._precondition_loaded = False
    coordinator._precondition_load_lock = asyncio.Lock()
    coordinator._precondition_ids = {}
    coordinator._precondition_pending = []
    coordinator._precondition_store = MagicMock(
        async_load=AsyncMock(
            return_value={
                "owned": {"good": 100.0, "bad": "soon"},
                "pending": [
                    {"name": "HA Precondition ab12cd", "expiry": 200},
                    {"name": "HA Precondition ef34ab", "expiry": None},
                    "junk",
                ],
            }
        )
    )

    await coordinator._ensure_precondition_loaded()

    assert coordinator._precondition_ids == {"good": 100.0}
    assert coordinator._precondition_pending == [
        {"name": "HA Precondition ab12cd", "expiry": 200.0, "missing": False}
    ]


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


async def test_precondition_after_departure_creates_a_new_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once HA's schedule has departed, a press creates a new one."""
    now = datetime(2026, 11, 14, 12, 51, 24, tzinfo=UTC)
    monkeypatch.setattr(coordinator_module.dt_util, "now", lambda: now)
    monkeypatch.setattr(coordinator_module.time, "time", now.timestamp)
    departed = now.timestamp() - 60 + PRECONDITION_CLEANUP_GRACE_SECONDS
    coordinator, created = _precondition_coordinator(owned={"S1": departed})

    await coordinator.precondition_now()

    coordinator.update_departure_schedule.assert_not_awaited()
    assert len(created) == 1


@pytest.mark.parametrize("created_anyway", [True, False])
async def test_unknown_creation_decided_by_a_fresh_list(
    monkeypatch: pytest.MonkeyPatch, created_anyway: bool
) -> None:
    """A record left by an unknown outcome doesn't block presses until it departs.

    A fresh list shows whether Rivian created the schedule: if so it's adopted and
    moved, if not the record is dropped and a new one created.
    """
    now = datetime(2026, 11, 14, 12, 51, 24, tzinfo=UTC)
    monkeypatch.setattr(coordinator_module.dt_util, "now", lambda: now)
    monkeypatch.setattr(coordinator_module.time, "time", now.timestamp)
    expiry = now.timestamp() + 20 * 60 + PRECONDITION_CLEANUP_GRACE_SECONDS
    name = "HA Precondition abc123"
    coordinator, created = _precondition_coordinator(
        pending=[{"name": name, "expiry": expiry}]
    )
    coordinator._departure_lock = asyncio.Lock()

    async def fresh_list() -> None:
        coordinator._departure_schedules = (
            [{"id": "S1", "name": name}] if created_anyway else []
        )

    coordinator._ensure_fresh_departure_schedules = AsyncMock(side_effect=fresh_list)

    await coordinator.precondition_now()

    coordinator._ensure_fresh_departure_schedules.assert_awaited_once()
    if created_anyway:
        assert not created
        coordinator.update_departure_schedule.assert_awaited_once()
        assert "S1" in coordinator._precondition_ids
    else:
        assert len(created) == 1
        coordinator.update_departure_schedule.assert_not_awaited()
        # kept, so a late appearance is still adopted and cleaned up, but marked,
        # so it doesn't prompt another refresh
        old = next(r for r in coordinator._precondition_pending if r["name"] == name)
        assert old["missing"]


@pytest.mark.parametrize(
    ("target", "sent"),
    [(21.0, 21), (21.2, 21), (21.3, 21.5)],
    ids=["whole", "rounds-down", "rounds-up"],
)
async def test_button_sends_temperature_like_the_action(
    monkeypatch: pytest.MonkeyPatch, target: float, sent: float
) -> None:
    """The same rounding and type as the action, so 21.0 °C is sent as 21."""
    now = datetime(2026, 11, 14, 12, 51, 24, tzinfo=UTC)
    monkeypatch.setattr(coordinator_module.dt_util, "now", lambda: now)
    coordinator, created = _precondition_coordinator(
        data={"hvacTargetTemperature": {"value": target}}
    )

    await coordinator.precondition_now()

    value = created[0]["departureSettings"]["comfortSettings"]["cabinTempCelsius"]
    assert value == sent
    assert type(value) is type(sent)


async def test_precondition_refreshes_an_unconfirmed_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A list that may predate HA's own change is refreshed before deciding."""
    now = datetime(2026, 11, 14, 12, 51, 24, tzinfo=UTC)
    monkeypatch.setattr(coordinator_module.dt_util, "now", lambda: now)
    monkeypatch.setattr(coordinator_module.time, "time", now.timestamp)
    departs_soon = now.timestamp() + 10 * 60 + PRECONDITION_CLEANUP_GRACE_SECONDS
    coordinator, created = _precondition_coordinator(owned={"S1": departs_soon})
    coordinator._departure_lock = asyncio.Lock()
    coordinator._departure_stale = True
    coordinator._departure_schedules = []  # predates HA's schedule

    async def fresh_list() -> None:
        coordinator._departure_schedules = [{"id": "S1"}]
        coordinator._departure_stale = False

    coordinator._ensure_fresh_departure_schedules = AsyncMock(side_effect=fresh_list)

    await coordinator.precondition_now()

    assert not created
    coordinator.update_departure_schedule.assert_awaited_once()


def test_missing_id_not_forgotten_from_an_unconfirmed_list() -> None:
    """A list that may predate HA's own change doesn't make it forget a schedule."""
    coordinator = _cleanup_coordinator(
        owned={"temp": time.time() - 1}, schedules=[{"id": "other"}]
    )
    coordinator._departure_stale = True

    asyncio.run(coordinator._cleanup_precondition_schedules())

    assert "temp" in coordinator._precondition_ids


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


def _cleanup_coordinator(
    owned: dict[str, float] | None = None,
    pending: list[dict[str, Any]] | None = None,
    schedules: list[dict[str, Any]] | None = None,
) -> VehicleCoordinator:
    coordinator = _coordinator({})
    coordinator.hass = SimpleNamespace(is_stopping=False)
    coordinator._precondition_ids = dict(owned or {})
    coordinator._precondition_pending = list(pending or [])
    coordinator._precondition_loaded = True
    coordinator._precondition_cleanup_lock = asyncio.Lock()
    coordinator._departure_stale = False
    coordinator._expect_empty_schedules = False
    coordinator._empty_list_hold = None
    coordinator._precondition_timer = None
    coordinator._reconcile_task = None
    coordinator._shutting_down = False
    coordinator._departure_schedules = schedules
    coordinator._save_precondition_state = AsyncMock()
    coordinator.delete_departure_schedule = AsyncMock()
    coordinator._schedule_reconcile = MagicMock()
    return coordinator


async def test_cleanup_timer_restored_for_unexpired_schedule(timers: _Timers) -> None:
    """After a restart mid-run, cleanup waits for the expiry on its own timer.

    Vehicle payloads restart the coordinator's refresh timer, so cleanup must not
    depend on a refresh ever firing.
    """
    expiry = time.time() + 600
    coordinator = _cleanup_coordinator(
        owned={"temp": expiry}, schedules=[{"id": "temp"}]
    )

    await coordinator._reconcile_and_cleanup()

    coordinator.delete_departure_schedule.assert_not_awaited()
    assert 590 < timers.delay <= 600
    timers.calls[-1][1](None)  # the timer fires at expiry
    coordinator._schedule_reconcile.assert_called_once()


async def test_cleanup_deletes_expired_schedule_and_stops_timer(
    timers: _Timers,
) -> None:
    coordinator = _cleanup_coordinator(
        owned={"temp": time.time() - 1}, schedules=[{"id": "temp"}]
    )

    await coordinator._reconcile_and_cleanup()

    coordinator.delete_departure_schedule.assert_awaited_once()
    assert coordinator.delete_departure_schedule.await_args.args == ("temp",)
    assert coordinator._precondition_ids == {}
    assert timers.calls == []


async def test_missing_schedule_kept_until_expiry(timers: _Timers) -> None:
    """A schedule list that briefly leaves out an owned schedule doesn't drop it.

    Rivian's subscription has been seen sending an empty list for about a second
    before the full list again. Forgetting the schedule then left it on the
    vehicle, repeating weekly, with no cleanup timer.
    """
    expiry = time.time() + 600
    coordinator = _cleanup_coordinator(owned={"temp": expiry}, schedules=[])

    await coordinator._reconcile_and_cleanup()

    assert coordinator._precondition_ids == {"temp": expiry}
    assert 590 < timers.delay <= 600

    coordinator._departure_schedules = [{"id": "temp"}]
    coordinator._precondition_ids["temp"] = time.time() - 1  # now expired
    await coordinator._reconcile_and_cleanup()

    coordinator.delete_departure_schedule.assert_awaited_once()
    assert coordinator.delete_departure_schedule.await_args.args == ("temp",)
    assert coordinator._precondition_ids == {}


async def test_schedule_gone_at_expiry_is_forgotten(timers: _Timers) -> None:
    """A schedule deleted elsewhere is dropped once expired, without deleting."""
    coordinator = _cleanup_coordinator(
        owned={"temp": time.time() - 1}, schedules=[{"id": "other"}]
    )

    await coordinator._reconcile_and_cleanup()

    coordinator.delete_departure_schedule.assert_not_awaited()
    assert coordinator._precondition_ids == {}
    coordinator._save_precondition_state.assert_awaited_once()
    assert timers.calls == []


async def test_failed_cleanup_deletion_is_retried(timers: _Timers) -> None:
    coordinator = _cleanup_coordinator(
        owned={"temp": time.time() - 1}, schedules=[{"id": "temp"}]
    )
    coordinator.delete_departure_schedule.side_effect = HomeAssistantError("offline")

    await coordinator._reconcile_and_cleanup()

    assert coordinator._precondition_ids.keys() == {"temp"}
    assert timers.delay == PRECONDITION_RETRY_SECONDS


async def test_unmatched_creation_is_retried(timers: _Timers) -> None:
    """A creation whose schedule id has not appeared yet is reconciled again soon."""
    coordinator = _cleanup_coordinator(
        pending=[{"name": "HA Precondition abc123", "expiry": time.time() + 900}],
        schedules=[],
    )

    await coordinator._reconcile_and_cleanup()

    assert timers.delay == PRECONDITION_RETRY_SECONDS


async def test_cleanup_timer_retries_before_schedules_arrive(timers: _Timers) -> None:
    """Without an authoritative schedule list yet, owned state still gets a timer."""
    coordinator = _cleanup_coordinator(owned={"temp": time.time() - 1})

    await coordinator._reconcile_and_cleanup()

    coordinator.delete_departure_schedule.assert_not_awaited()
    assert timers.delay == PRECONDITION_RETRY_SECONDS


async def test_rearming_cancels_previous_cleanup_timer(timers: _Timers) -> None:
    coordinator = _cleanup_coordinator(
        owned={"temp": time.time() + 600}, schedules=[{"id": "temp"}]
    )

    await coordinator._reconcile_and_cleanup()
    await coordinator._reconcile_and_cleanup()

    first_cancel = timers.calls[0][2]
    first_cancel.assert_called_once()
    assert len(timers.calls) == 2


async def test_unload_during_cleanup_does_not_rearm_timer(
    timers: _Timers, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unloading while a deletion is in flight leaves no timer or reconcile behind."""
    coordinator = _cleanup_coordinator(
        owned={"temp": time.time() - 1}, schedules=[{"id": "temp"}]
    )
    deleting = asyncio.Event()

    async def delete(schedule_id: str, **kwargs: Any) -> None:
        deleting.set()
        await asyncio.Event().wait()

    coordinator.delete_departure_schedule = AsyncMock(side_effect=delete)
    coordinator._unsubscribe = AsyncMock()
    coordinator.config_entry = MagicMock()
    del coordinator._schedule_reconcile  # use the real method
    monkeypatch.setattr(
        coordinator_module.RivianDataUpdateCoordinator,
        "async_shutdown",
        AsyncMock(),
        raising=False,
    )
    coordinator._reconcile_task = asyncio.create_task(
        coordinator._reconcile_and_cleanup()
    )
    await deleting.wait()

    await coordinator.async_shutdown()
    for _ in range(3):  # let a cancelled task finish unwinding
        await asyncio.sleep(0)

    assert coordinator._reconcile_task.cancelled()
    assert timers.calls == []
    coordinator._schedule_reconcile()
    coordinator.config_entry.async_create_task.assert_not_called()


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
