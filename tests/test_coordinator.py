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
    PRECONDITION_PENDING_MAX_AGE_SECONDS,
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
    return coordinator


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
    monkeypatch.setattr(coordinator_module, "DEPARTURE_REFRESH_TIMEOUT", 0.5)
    coordinator = _schedule_coordinator()
    coordinator._departure_refreshed = asyncio.Event()
    coordinator._departure_schedules = []
    coordinator._precondition_pending = []
    coordinator._precondition_ids = {}
    coordinator.async_update_listeners = MagicMock()

    async def subscribe(force: bool = False) -> None:
        coordinator._process_departure_schedules(
            {"payload": {"data": {"vehicleDepartureSchedules": []}}}
        )

    coordinator._subscribe_departure_schedules = subscribe
    response = MagicMock()
    response.json = AsyncMock(
        return_value={"data": {"updateDepartureSchedule": {"success": True}}}
    )

    async def request() -> MagicMock:
        return response

    start = time.monotonic()
    await coordinator._departure_schedule_mutation("updateDepartureSchedule", request())
    assert time.monotonic() - start < 0.4
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


async def test_precondition_needs_the_schedule_list() -> None:
    """Without the list, HA's upcoming schedule can't be found, so it's refused."""
    coordinator, created = _precondition_coordinator()
    coordinator._departure_schedules = None

    with pytest.raises(HomeAssistantError):
        await coordinator.precondition_now()

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


async def test_precondition_while_one_is_being_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A press while HA's schedule has no id yet is refused, not duplicated."""
    now = datetime(2026, 11, 14, 12, 51, 24, tzinfo=UTC)
    monkeypatch.setattr(coordinator_module.dt_util, "now", lambda: now)
    monkeypatch.setattr(coordinator_module.time, "time", now.timestamp)
    expiry = now.timestamp() + 20 * 60 + PRECONDITION_CLEANUP_GRACE_SECONDS
    coordinator, created = _precondition_coordinator(
        pending=[{"name": "Home Assistant abc123", "expiry": expiry}]
    )

    with pytest.raises(HomeAssistantError):
        await coordinator.precondition_now()

    assert not created
    coordinator.update_departure_schedule.assert_not_awaited()


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
    ],
    ids=["not-enrolled", "enrolled-no-device", "enrolled-unpaired", "paired"],
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

    coordinator.delete_departure_schedule.assert_awaited_once_with("temp")
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

    coordinator.delete_departure_schedule.assert_awaited_once_with("temp")
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


async def test_expired_schedule_kept_through_a_brief_empty_list(
    timers: _Timers,
) -> None:
    """An empty list may come just before the full one, so the id is kept."""
    coordinator = _cleanup_coordinator(owned={"temp": time.time() - 1}, schedules=[])

    await coordinator._reconcile_and_cleanup()

    assert coordinator._precondition_ids.keys() == {"temp"}
    # the full list arrives with the schedule, which is then deleted
    coordinator._departure_schedules = [{"id": "temp"}]
    await coordinator._reconcile_and_cleanup()
    coordinator.delete_departure_schedule.assert_awaited_once_with("temp")


async def test_long_expired_schedule_forgotten_with_an_empty_list(
    timers: _Timers,
) -> None:
    """With no schedules left at all, an id is dropped once well past its expiry."""
    expiry = time.time() - PRECONDITION_PENDING_MAX_AGE_SECONDS - 1
    coordinator = _cleanup_coordinator(owned={"temp": expiry}, schedules=[])

    await coordinator._reconcile_and_cleanup()

    assert coordinator._precondition_ids == {}


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

    async def delete(schedule_id: str) -> None:
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

    async def subscribe(vehicle_id: str, callback: Any) -> AsyncMock:
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
