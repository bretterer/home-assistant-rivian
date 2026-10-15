"""Tests for the vehicle coordinator.

Covers schedule writes, phone pairing and the cleanup timer for temporary
precondition schedules.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from rivian.exceptions import RivianApiException

from custom_components.rivian import coordinator as coordinator_module
from custom_components.rivian.coordinator import (
    PRECONDITION_RETRY_SECONDS,
    VehicleCoordinator,
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
