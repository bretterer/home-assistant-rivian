"""Tests for the Outside temperature sensor and its Open-Meteo lookups."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import MethodType
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.rivian import sensor
from custom_components.rivian.config_flow import CONF_WEATHER_LOOKUPS
from custom_components.rivian.coordinator import VehicleCoordinator
from custom_components.rivian.weather import OpenMeteoWeatherClient

T0 = datetime(2026, 10, 12, 18, 0, tzinfo=UTC)
VEHICLE = {"id": "vehicle-1", "vin": "VIN1", "name": "Truck", "model": "R1T"}
# Made-up coordinates; 0.01 degrees of latitude is about 1.1 km
START = (0.12345, 0.98765)


class Clock:
    """A controllable utcnow."""

    def __init__(self) -> None:
        self.now = T0

    def advance(self, minutes: float) -> None:
        self.now += timedelta(minutes=minutes)


class Harness:
    """An Outside temperature entity with its lookups and timers captured."""

    def __init__(self, location: tuple[float, float] | None) -> None:
        self.data: dict[str, Any] = {}
        self.move(location)
        coordinator = MagicMock()
        # The real lookup: gnssLocation has no "value" key, so get() can't read it
        coordinator.data = self.data
        coordinator.get = MethodType(VehicleCoordinator.get, coordinator)
        self.client = MagicMock()
        self.client.async_get_current_temperature = AsyncMock(return_value=41.3)
        self.tasks: list[Any] = []
        self.timers: list[tuple[timedelta, Any, MagicMock]] = []
        self.entity = sensor.RivianOutsideTemperatureEntity(
            coordinator,
            MagicMock(),
            sensor.OUTSIDE_TEMPERATURE_SENSOR,
            VEHICLE,
            self.client,
        )
        self.entity.entity_id = "sensor.truck_outside_temperature"
        self.entity.hass = MagicMock()
        self.entity.hass.async_create_background_task = lambda coro, name: (
            self.tasks.append(coro)
        )

    def move(self, location: tuple[float, float] | None) -> None:
        self.data["gnssLocation"] = (
            None
            if location is None
            else {"latitude": location[0], "longitude": location[1]}
        )

    def call_later(self, hass: Any, delay: timedelta, action: Any) -> MagicMock:
        cancel = MagicMock()
        self.timers.append((delay, action, cancel))
        return cancel

    async def run_tasks(self) -> None:
        while self.tasks:
            await self.tasks.pop(0)

    @property
    def requests(self) -> list[tuple[float, float]]:
        return [
            call.args for call in self.client.async_get_current_temperature.mock_calls
        ]


@pytest.fixture
def clock() -> Any:
    clock = Clock()
    with patch.object(sensor.dt_util, "utcnow", lambda: clock.now):
        yield clock


async def _added(harness: Harness) -> Harness:
    await harness.entity.async_added_to_hass()
    await harness.run_tasks()
    return harness


@pytest.fixture
def make(clock: Clock) -> Any:
    """Build a harness whose timers are captured."""
    harnesses: list[Harness] = []

    def _make(location: tuple[float, float] | None = START) -> Harness:
        harness = Harness(location)
        harnesses.append(harness)
        return harness

    def _call_later(hass: Any, delay: timedelta, action: Any) -> MagicMock:
        return harnesses[-1].call_later(hass, delay, action)

    with patch.object(sensor, "async_call_later", _call_later):
        yield _make


async def test_first_location_is_looked_up(make: Any) -> None:
    """The first known location is looked up once and the value shown."""
    harness = await _added(make())
    assert harness.requests == [START]
    assert harness.entity.native_value == 41.3
    assert harness.entity.available
    # The next refresh is 15 minutes after the lookup
    assert [delay for delay, _, _ in harness.timers] == [timedelta(minutes=15)]


async def test_no_location_no_request(make: Any, clock: Clock) -> None:
    """Without a location nothing is sent, and the entity stays unavailable."""
    harness = await _added(make(None))
    assert harness.requests == []
    assert not harness.entity.available
    # The location arrives with a coordinator update
    harness.move(START)
    harness.entity._handle_coordinator_update()
    await harness.run_tasks()
    assert harness.requests == [START]


async def test_short_move_is_not_looked_up(make: Any, clock: Clock) -> None:
    """Moving under a kilometre sends nothing."""
    harness = await _added(make())
    clock.advance(10)
    harness.move((START[0] + 0.005, START[1]))  # about 560 m
    harness.entity._handle_coordinator_update()
    await harness.run_tasks()
    assert harness.requests == [START]


async def test_long_move_waits_for_the_minimum_gap(make: Any, clock: Clock) -> None:
    """Moving over a kilometre looks up again, but not within 5 minutes."""
    harness = await _added(make())
    far = (START[0] + 0.02, START[1])  # about 2.2 km
    clock.advance(4)
    harness.move(far)
    harness.entity._handle_coordinator_update()
    await harness.run_tasks()
    assert harness.requests == [START]

    clock.advance(1)
    harness.entity._handle_coordinator_update()
    await harness.run_tasks()
    assert harness.requests == [START, far]
    # The pending refresh was replaced by one counted from the new lookup
    assert harness.timers[0][2].called
    assert len(harness.timers) == 2


async def test_refresh_looks_up_again(make: Any, clock: Clock) -> None:
    """The refresh looks the same spot up again and schedules the next one."""
    harness = await _added(make())
    harness.client.async_get_current_temperature.return_value = 39.0
    clock.advance(15)
    _, refresh, _ = harness.timers[-1]
    refresh(clock.now)
    await harness.run_tasks()
    assert harness.requests == [START, START]
    assert harness.entity.native_value == 39.0
    assert len(harness.timers) == 2


async def test_refresh_without_location_checks_again(make: Any, clock: Clock) -> None:
    """A refresh with no location sends nothing but keeps checking."""
    harness = await _added(make())
    harness.move(None)
    clock.advance(15)
    harness.timers[-1][1](clock.now)
    await harness.run_tasks()
    assert harness.requests == [START]
    assert len(harness.timers) == 2


async def test_value_turns_unavailable_after_an_hour(make: Any, clock: Clock) -> None:
    """Failed lookups keep the last value for an hour, then it's unavailable."""
    harness = await _added(make())
    harness.client.async_get_current_temperature.return_value = None
    for _ in range(3):
        clock.advance(15)
        harness.timers[-1][1](clock.now)
        await harness.run_tasks()
    assert harness.entity.available
    assert harness.entity.native_value == 41.3
    clock.advance(15.1)
    assert not harness.entity.available


async def test_removal_cancels_the_refresh(make: Any) -> None:
    """Removing the entity cancels the scheduled refresh."""
    harness = make()
    removers: list[Any] = []
    harness.entity.async_on_remove = removers.append
    await _added(harness)
    for remove in removers:
        remove()
    assert harness.timers[-1][2].called


async def test_removal_mid_lookup_schedules_nothing(make: Any) -> None:
    """Removing the entity during a lookup cancels it, with no timer left."""
    harness = make()
    removers: list[Any] = []
    harness.entity.async_on_remove = removers.append
    started = asyncio.Event()

    async def _never_answers(*_: Any) -> float:
        started.set()
        await asyncio.Event().wait()
        return 0.0

    harness.client.async_get_current_temperature = _never_answers
    tasks: list[asyncio.Task[None]] = []
    harness.entity.hass.async_create_background_task = lambda coro, name: (
        tasks.append(asyncio.get_running_loop().create_task(coro)) or tasks[-1]
    )
    await harness.entity.async_added_to_hass()
    await started.wait()
    harness.entity.async_write_ha_state = MagicMock()
    for remove in removers:
        remove()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(tasks[0], 1)
    assert harness.timers == []
    harness.entity.async_write_ha_state.assert_not_called()


async def test_failed_lookup_keeps_refreshing(make: Any, clock: Clock) -> None:
    """A lookup that raises still schedules the next refresh."""
    harness = make()
    harness.client.async_get_current_temperature.side_effect = RuntimeError("boom")
    await _added(harness)
    assert harness.entity.native_value is None
    assert [delay for delay, _, _ in harness.timers] == [timedelta(minutes=15)]


def test_distance() -> None:
    """The distance is in metres."""
    assert sensor._distance_m(START, START) == 0
    assert 1100 < sensor._distance_m(START, (START[0] + 0.01, START[1])) < 1125


class _Response:
    status = 200

    async def json(self) -> dict[str, Any]:
        return {"current": {"temperature_2m": 41.26}}

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


async def test_client_rounds_coordinates() -> None:
    """Only coordinates rounded to 2 decimals (about 1 km) are sent."""
    session = MagicMock()
    session.get = MagicMock(return_value=_Response())
    client = OpenMeteoWeatherClient(session=session)
    assert await client.async_get_current_temperature(*START) == 41.3
    params = session.get.call_args.kwargs["params"]
    assert (params["latitude"], params["longitude"]) == (0.12, 0.99)


async def test_disabled_client_sends_nothing() -> None:
    """A disabled client returns nothing without a request."""
    session = MagicMock()
    client = OpenMeteoWeatherClient(session=session, enabled=False)
    assert await client.async_get_current_temperature(*START) is None
    session.get.assert_not_called()


def _entry(options: dict[str, Any]) -> MagicMock:
    entry = MagicMock()
    entry.options = options
    coordinator = MagicMock()
    coordinator.get = lambda key: None
    entry.runtime_data.vehicles = {"vehicle-1": VEHICLE}
    entry.runtime_data.vehicle_coordinators = {"vehicle-1": coordinator}
    entry.runtime_data.wallbox_coordinator.data = []
    return entry


@pytest.mark.parametrize("enabled", [False, True])
async def test_setup_follows_the_option(enabled: bool) -> None:
    """The sensor exists only with the option on; otherwise it's removed."""
    added: list[Any] = []
    stale_calls: list[tuple[set[str], set[str | None]]] = []

    def _remove_stale(hass: Any, platform: str, possible: Any, created: Any) -> None:
        stale_calls.append((set(possible), set(created)))

    with (
        patch.object(sensor, "OpenMeteoWeatherClient") as client_cls,
        patch.object(sensor, "async_remove_stale_entities", _remove_stale),
    ):
        await sensor.async_setup_entry(
            MagicMock(), _entry({CONF_WEATHER_LOOKUPS: enabled}), added.extend
        )

    outside = [e for e in added if isinstance(e, sensor.RivianOutsideTemperatureEntity)]
    assert len(outside) == (1 if enabled else 0)
    assert client_cls.called == enabled
    unique_id = "VIN1-outside_temperature"
    # Offered for removal every time; only removed when it wasn't created
    calls = [call for call in stale_calls if unique_id in call[0]]
    assert len(calls) == 1
    assert (unique_id in calls[0][1]) == enabled
