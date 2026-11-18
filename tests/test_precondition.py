"""Tests for keyless preconditioning through a temporary departure schedule."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from custom_components.rivian import coordinator as coordinator_module
from custom_components.rivian.const import (
    PRECONDITION_LEAD_MINUTES_MINIMUM,
    PRECONDITION_SCHEDULE_NAME,
    WEEK_DAYS_ORDERED,
)
from custom_components.rivian.coordinator import (
    PRECONDITION_CLEANUP_GRACE_SECONDS,
    PRECONDITION_RETRY_SECONDS,
    VehicleCoordinator,
    is_precondition_schedule,
    precondition_current_departure,
    precondition_departure,
)

ZONE = ZoneInfo("America/Denver")
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=ZONE)  # a Wednesday


class _TranslatedError(Exception):
    """Stands in for Home Assistant's errors, keeping their translation key."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args)
        self.translation_key = kwargs.get("translation_key")


class _Timers:
    """Capture async_call_later calls in place of Home Assistant's scheduler."""

    def __init__(self) -> None:
        self.calls: list[tuple[float, Any, MagicMock]] = []

    def __call__(self, hass: Any, delay: float, action: Any) -> MagicMock:
        cancel = MagicMock()
        self.calls.append((delay, action, cancel))
        return cancel


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Home Assistant's clock, in Denver, at NOW unless moved."""
    clock = SimpleNamespace(now=NOW)
    monkeypatch.setattr(coordinator_module.dt_util, "now", lambda: clock.now)
    monkeypatch.setattr(coordinator_module.time, "time", lambda: clock.now.timestamp())
    monkeypatch.setattr(
        coordinator_module.dt_util, "as_local", lambda value: value.astimezone(ZONE)
    )
    monkeypatch.setattr(
        coordinator_module.dt_util,
        "utc_from_timestamp",
        lambda timestamp: datetime.fromtimestamp(timestamp, UTC),
    )
    return clock


@pytest.fixture
def timers(monkeypatch: pytest.MonkeyPatch) -> _Timers:
    timers = _Timers()
    monkeypatch.setattr(coordinator_module, "async_call_later", timers)
    return timers


def _schedule(
    schedule_id: str, departure: datetime, name: str = PRECONDITION_SCHEDULE_NAME
) -> dict[str, Any]:
    return {
        "id": schedule_id,
        "name": name,
        "occurrence": {
            "days": [WEEK_DAYS_ORDERED[departure.weekday()]],
            "startsAtMin": departure.hour * 60 + departure.minute,
        },
    }


def _coordinator(
    schedules: list[dict[str, Any]] | None, data: dict[str, Any] | None = None
) -> tuple[VehicleCoordinator, list[dict[str, Any]]]:
    coordinator = object.__new__(VehicleCoordinator)
    coordinator.vehicle_id = "vehicle"
    coordinator.data = data or {}
    coordinator.hass = SimpleNamespace(is_stopping=False)
    coordinator._departure_schedules = schedules
    coordinator._departure_stale = False
    coordinator._departure_lock = asyncio.Lock()
    coordinator._precondition_lock = asyncio.Lock()
    coordinator._precondition_cleanup_lock = asyncio.Lock()
    coordinator._precondition_cleanup_failed = False
    coordinator._precondition_timer = None
    coordinator._reconcile_task = None
    coordinator._shutting_down = False
    coordinator._schedule_reconcile = MagicMock()
    coordinator.update_departure_schedule = AsyncMock()
    coordinator.delete_departure_schedule = AsyncMock(return_value=True)
    created: list[dict[str, Any]] = []
    coordinator.create_departure_schedule = AsyncMock(side_effect=created.append)
    return coordinator, created


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
def test_departure_never_shortens_the_lead(now: datetime, expected: datetime) -> None:
    """Never round down: a lead just under 15 minutes failed to precondition."""
    depart = precondition_departure(now, 20)
    assert depart == expected
    assert depart - now >= timedelta(minutes=20)


def test_departure_across_a_dst_change() -> None:
    """The lead is real time: 01:50 plus 20 minutes on spring-forward is 03:10."""
    now = datetime(2026, 3, 8, 1, 50, tzinfo=ZONE)

    depart = precondition_departure(now, 20)

    assert (depart.hour, depart.minute) == (3, 10)
    # same-zone subtraction is wall-clock, so compare in UTC
    assert depart.astimezone(UTC) - now.astimezone(UTC) == timedelta(minutes=20)


@pytest.mark.parametrize(
    ("name", "ours"),
    [
        ("HA Precondition Now", True),
        ("ha precondition now ", True),
        ("HA Precondition 3fa9c1", False),
        ("HA Precondition Nowhere", False),
        ("Weekdays", False),
        (None, False),
    ],
)
def test_precondition_schedules_are_recognized_by_name(
    name: str | None, ours: bool
) -> None:
    assert is_precondition_schedule({"name": name}) is ours


@pytest.mark.parametrize(
    ("departs_in", "current"),
    [
        (timedelta(minutes=20), True),
        (timedelta(minutes=61), True),  # the minute is rounded up
        (timedelta(minutes=-1), True),  # within the grace period
        (timedelta(minutes=-5), False),  # departed
        (timedelta(hours=3), False),  # moved further out in the app
    ],
    ids=["upcoming", "longest-lead", "just-departed", "departed", "moved-out"],
)
def test_current_departure(
    clock: SimpleNamespace, departs_in: timedelta, current: bool
) -> None:
    departure = NOW + departs_in

    result = precondition_current_departure(_schedule("S1", departure), NOW.timestamp())

    assert (result == departure) if current else result is None


def test_repeated_wall_time_is_current(clock: SimpleNamespace) -> None:
    """01:30 happens twice as the clocks go back; the coming one is current."""
    second = datetime(2026, 11, 1, 1, 30, tzinfo=ZONE, fold=1)
    # 10 real minutes before; wall-clock arithmetic would lose the fold
    clock.now = datetime.fromtimestamp(second.timestamp() - 600, ZONE)

    result = precondition_current_departure(
        _schedule("S1", second), clock.now.timestamp()
    )

    assert result is not None
    assert result.timestamp() == second.timestamp()


async def test_press_creates_a_schedule(clock: SimpleNamespace) -> None:
    """Named so it's recognized later, at the driver's set temperature."""
    coordinator, created = _coordinator(
        [], data={"hvacTargetTemperature": {"value": 21.0}}
    )

    await coordinator.precondition_now()

    (schedule,) = created
    assert schedule["name"] == PRECONDITION_SCHEDULE_NAME
    assert schedule["isEnabled"]
    depart = precondition_departure(NOW, 20)
    assert schedule["repeatsWeekly"]["days"] == [WEEK_DAYS_ORDERED[depart.weekday()]]
    assert schedule["repeatsWeekly"]["startsAtMin"] == depart.hour * 60 + depart.minute
    temperature = schedule["departureSettings"]["comfortSettings"]["cabinTempCelsius"]
    assert temperature == 21
    assert type(temperature) is int  # like the action sends it


@pytest.mark.parametrize(
    ("target", "expected"),
    [(22.3, 22.5), (63.5, 29), (10.0, 16), (None, 21)],
    ids=["set", "hi", "lo", "missing"],
)
async def test_default_temperature(
    clock: SimpleNamespace, target: float | None, expected: float
) -> None:
    """The driver's set temperature, clamped, like the app; 21 °C without one."""
    data = {} if target is None else {"hvacTargetTemperature": {"value": target}}
    coordinator, created = _coordinator([], data=data)

    await coordinator.precondition_now()

    comfort = created[0]["departureSettings"]["comfortSettings"]
    assert comfort["cabinTempCelsius"] == expected


@pytest.mark.parametrize(("lead", "minutes"), [(15, 20), (30, 30), (90, 60)])
async def test_lead_is_clamped(clock: SimpleNamespace, lead: int, minutes: int) -> None:
    coordinator, created = _coordinator([])

    await coordinator.precondition_now(lead)

    depart = precondition_departure(NOW, minutes)
    assert created[0]["repeatsWeekly"]["startsAtMin"] == (
        depart.hour * 60 + depart.minute
    )
    assert minutes >= PRECONDITION_LEAD_MINUTES_MINIMUM


async def test_press_again_moves_the_current_schedule(clock: SimpleNamespace) -> None:
    """Rather than add a second one that conditions again and can't be stopped."""
    current = _schedule("S1", NOW + timedelta(minutes=10))
    coordinator, created = _coordinator(
        [current, _schedule("U1", NOW + timedelta(minutes=5), "Weekdays")]
    )

    await coordinator.precondition_now(30, 22.5)

    assert not created
    depart = precondition_departure(NOW, 30)
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


async def test_press_after_departure_creates_a_new_one(
    clock: SimpleNamespace,
) -> None:
    """The departed one is left for cleanup."""
    coordinator, created = _coordinator([_schedule("S1", NOW - timedelta(hours=1))])

    await coordinator.precondition_now()

    coordinator.update_departure_schedule.assert_not_awaited()
    assert len(created) == 1


async def test_press_needs_the_schedule_list(
    clock: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without it, HA's current schedule can't be found, so another would be made."""
    monkeypatch.setattr(coordinator_module, "HomeAssistantError", _TranslatedError)
    coordinator, created = _coordinator(None)

    with pytest.raises(_TranslatedError) as err:
        await coordinator.precondition_now()

    assert err.value.translation_key == "schedules_out_of_sync"
    assert not created


async def test_press_refreshes_an_unconfirmed_list(clock: SimpleNamespace) -> None:
    """A list predating HA's own change could create a second schedule."""
    coordinator, created = _coordinator([])
    coordinator._departure_stale = True

    async def fresh_list() -> None:
        coordinator._departure_schedules = [
            _schedule("S1", NOW + timedelta(minutes=10))
        ]
        coordinator._departure_stale = False

    coordinator._ensure_fresh_departure_schedules = AsyncMock(side_effect=fresh_list)

    await coordinator.precondition_now()

    assert not created
    coordinator.update_departure_schedule.assert_awaited_once()


async def test_concurrent_presses_create_one_schedule(clock: SimpleNamespace) -> None:
    """The second waits for the first, then moves its schedule."""
    coordinator, created = _coordinator([])

    async def create(schedule: dict[str, Any]) -> None:
        await asyncio.sleep(0)
        created.append(schedule)
        # the refreshed list has it
        coordinator._departure_schedules = [
            _schedule("S1", NOW + timedelta(minutes=20))
        ]

    coordinator.create_departure_schedule = AsyncMock(side_effect=create)

    await asyncio.gather(coordinator.precondition_now(), coordinator.precondition_now())

    assert len(created) == 1
    coordinator.update_departure_schedule.assert_awaited_once()


async def test_cleanup_deletes_departed_schedules(
    clock: SimpleNamespace, timers: _Timers
) -> None:
    """Only HA's departed ones; it then waits for the current one to depart."""
    current = NOW + timedelta(minutes=10)
    coordinator, _ = _coordinator(
        [
            _schedule("S1", current),
            _schedule("S2", NOW - timedelta(hours=1)),
            _schedule("U1", NOW - timedelta(hours=1), "Weekdays"),
        ]
    )

    await coordinator._reconcile_and_cleanup()

    coordinator.delete_departure_schedule.assert_awaited_once()
    assert coordinator.delete_departure_schedule.await_args.args == ("S2",)
    ((delay, _, _),) = timers.calls
    assert delay == pytest.approx(
        (current - NOW).total_seconds() + PRECONDITION_CLEANUP_GRACE_SECONDS
    )


async def test_cleanup_rechecks_before_deleting(
    clock: SimpleNamespace, timers: _Timers
) -> None:
    """One moved back into range in the app just before deleting is kept."""
    coordinator, _ = _coordinator([_schedule("S1", NOW - timedelta(hours=1))])

    await coordinator._reconcile_and_cleanup()

    keep = coordinator.delete_departure_schedule.await_args.kwargs["keep"]
    assert keep(_schedule("S1", NOW + timedelta(minutes=30)))
    assert not keep(_schedule("S1", NOW - timedelta(hours=1)))


async def test_failed_deletion_is_retried(
    clock: SimpleNamespace, timers: _Timers
) -> None:
    coordinator, _ = _coordinator([_schedule("S1", NOW - timedelta(hours=1))])
    coordinator.delete_departure_schedule.side_effect = RuntimeError("offline")

    await coordinator._reconcile_and_cleanup()

    assert timers.calls[-1][0] == PRECONDITION_RETRY_SECONDS


async def test_no_timer_without_precondition_schedules(
    clock: SimpleNamespace, timers: _Timers
) -> None:
    coordinator, _ = _coordinator(
        [_schedule("U1", NOW + timedelta(minutes=10), "Weekdays")]
    )

    await coordinator._reconcile_and_cleanup()

    coordinator.delete_departure_schedule.assert_not_awaited()
    assert timers.calls == []


async def test_timer_runs_the_next_pass(
    clock: SimpleNamespace, timers: _Timers
) -> None:
    coordinator, _ = _coordinator([_schedule("S1", NOW + timedelta(minutes=10))])

    await coordinator._reconcile_and_cleanup()
    timers.calls[-1][1](None)

    coordinator._schedule_reconcile.assert_called_once()


async def test_unload_during_cleanup_does_not_rearm(
    clock: SimpleNamespace, timers: _Timers, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unloading while a deletion is in flight leaves no timer or pass behind."""
    coordinator, _ = _coordinator([_schedule("S1", NOW - timedelta(hours=1))])
    deleting = asyncio.Event()

    async def delete(schedule_id: str, **kwargs: Any) -> None:
        deleting.set()
        await asyncio.Event().wait()

    coordinator.delete_departure_schedule = AsyncMock(side_effect=delete)
    coordinator._unsubscribe = AsyncMock()
    coordinator._empty_list_hold = None
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
    for _ in range(3):  # let the cancelled task finish unwinding
        await asyncio.sleep(0)

    assert coordinator._reconcile_task.cancelled()
    assert timers.calls == []
    coordinator._schedule_reconcile()
    coordinator.config_entry.async_create_task.assert_not_called()
