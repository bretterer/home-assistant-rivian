"""Tests for GraphQL position fixes alongside Parallax's."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.rivian import coordinator

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
# What Rivian sends when a subscription asks for a field it doesn't accept
VALIDATION_ERROR = {
    "id": "subscription",
    "type": "error",
    "payload": [
        {
            "message": 'Cannot query field "gnssLocation" on type "VehicleState".',
            "locations": [{"line": 1, "column": 70}],
        }
    ],
}


@pytest.fixture
def coor() -> coordinator.VehicleCoordinator:
    """A vehicle coordinator whose data is set directly."""
    coor = coordinator.VehicleCoordinator(
        hass=MagicMock(), config_entry=MagicMock(), client=MagicMock(), vehicle_id="V"
    )
    coor.data = {}

    def set_data(data: dict[str, Any]) -> None:
        coor.data = data

    coor.async_set_updated_data = set_data
    coor.charging_coordinator = MagicMock()
    return coor


def graphql_fix(
    coor: coordinator.VehicleCoordinator,
    at: datetime,
    lat: float = 40.1,
    lon: float = -111.9,
    **extra: Any,
) -> None:
    """Deliver a GraphQL position fix taken at `at`."""
    location = {
        "latitude": lat,
        "longitude": lon,
        "timeStamp": at.isoformat().replace("+00:00", "Z"),
        "isAuthorized": True,
    } | extra
    coor._process_graphql_location(
        {
            "type": "next",
            "payload": {"data": {"vehicleState": {"gnssLocation": location}}},
        }
    )


def parallax_fix(
    coor: coordinator.VehicleCoordinator,
    at: datetime,
    lat: float = 40.2,
    speed: float = 12.5,
) -> None:
    """Deliver a dynamics.vehicle.gnss fix taken at `at`."""
    decoded = {
        "timestamp": at,
        "gnssLocation": {"latitude": lat, "longitude": -111.8, "timeStamp": at},
        "gnssSpeed": speed,
        "gnssBearing": 90.0,
    }
    message = {
        "payload": {
            "data": {
                "parallaxMessages": {
                    "rvm": "dynamics.vehicle.gnss",
                    "payload": "",
                    "timestamp": at.timestamp() * 1000,
                }
            }
        }
    }
    with patch.object(coordinator, "decode_parallax_message", return_value=decoded):
        coor._process_parallax_data(message)


def test_newer_graphql_fix_is_used(coor: coordinator.VehicleCoordinator) -> None:
    parallax_fix(coor, T0)

    graphql_fix(coor, T0 + timedelta(seconds=5), lat=40.1234567)

    assert coor.data["gnssLocation"] == {
        "latitude": 40.123457,
        "longitude": -111.9,
        "timeStamp": T0 + timedelta(seconds=5),
    }


def test_stale_graphql_fix_is_ignored(coor: coordinator.VehicleCoordinator) -> None:
    """Some vehicles send an old GraphQL position (#274); Parallax's stays."""
    parallax_fix(coor, T0)

    graphql_fix(coor, T0 - timedelta(hours=3))

    assert coor.data["gnssLocation"]["latitude"] == 40.2
    assert coor.data["gnssLocation"]["timeStamp"] == T0


def test_parallax_keeps_speed_and_bearing_current(
    coor: coordinator.VehicleCoordinator,
) -> None:
    """GraphQL's newer position stays, but Parallax's speed and bearing update."""
    parallax_fix(coor, T0, speed=10.0)
    graphql_fix(coor, T0 + timedelta(seconds=55))

    parallax_fix(coor, T0 + timedelta(seconds=50), lat=40.3, speed=20.0)

    assert coor.data["gnssLocation"]["timeStamp"] == T0 + timedelta(seconds=55)
    assert coor.data["gnssSpeed"]["value"] == 20.0


def test_older_parallax_speed_is_ignored(coor: coordinator.VehicleCoordinator) -> None:
    parallax_fix(coor, T0, speed=10.0)

    parallax_fix(coor, T0 - timedelta(minutes=1), lat=40.3, speed=20.0)

    assert coor.data["gnssSpeed"]["value"] == 10.0
    assert coor.data["gnssLocation"]["latitude"] == 40.2


def test_newer_parallax_fix_replaces_graphql(
    coor: coordinator.VehicleCoordinator,
) -> None:
    """When GraphQL's fixes stop, Parallax's take over again."""
    graphql_fix(coor, T0)

    parallax_fix(coor, T0 + timedelta(minutes=1), lat=40.3)

    assert coor.data["gnssLocation"]["latitude"] == 40.3


@pytest.mark.parametrize(
    "message",
    [
        # A field that fails GraphQL validation ends the subscription like this
        {
            "type": "error",
            "payload": [
                {"message": 'Cannot query field "gnssLocation" on type "VehicleState".'}
            ],
        },
        {"type": "next", "payload": {"errors": [{"message": "Internal error"}]}},
        {"type": "next", "payload": {"data": None}},
        {"type": "next", "payload": {"data": {"vehicleState": None}}},
        {"type": "next", "payload": {"data": {"vehicleState": {"gnssLocation": None}}}},
        {"type": "complete"},
    ],
)
def test_unexpected_messages_are_ignored(
    coor: coordinator.VehicleCoordinator, message: dict[str, Any]
) -> None:
    """The position also comes from Parallax, so these aren't errors."""
    parallax_fix(coor, T0)
    before = coor.data

    with patch.object(coor, "async_set_update_error", create=True) as set_update_error:
        coor._process_graphql_location(message)

    assert coor.data == before
    assert coor._error_count == 0
    set_update_error.assert_not_called()


@pytest.mark.parametrize(
    "extra",
    [
        {"latitude": None},
        {"longitude": "not a number"},
        {"timeStamp": None},
        {"timeStamp": "not a time"},
        {"timeStamp": "2026-01-01T12:05:00"},  # no timezone to compare with
        {"isAuthorized": False},
    ],
)
def test_unusable_fixes_are_ignored(
    coor: coordinator.VehicleCoordinator, extra: dict[str, Any]
) -> None:
    parallax_fix(coor, T0)

    graphql_fix(coor, T0 + timedelta(minutes=5), **extra)

    assert coor.data["gnssLocation"]["latitude"] == 40.2


def subscribing_vehicle(
    location_unsubscribe: AsyncMock | Exception | None,
    error_first: bool = False,
) -> coordinator.VehicleCoordinator:
    """A vehicle coordinator whose subscriptions start, but maybe not GraphQL's position."""
    coor = coordinator.VehicleCoordinator(
        hass=MagicMock(), config_entry=MagicMock(), client=MagicMock(), vehicle_id="V"
    )
    coor.data = None
    coor.last_update_success = True
    coor.get_charging_schedule_data = AsyncMock()
    coor.hass = MagicMock()
    coor.config_entry = MagicMock()
    coor.config_entry.async_create_task = lambda hass, coro, eager_start=True: (
        asyncio.ensure_future(coro)
    )
    unsubscribe = AsyncMock()

    async def subscribe(
        vehicle_id: str, properties: set[str], callback: Any, on_error: Any = None
    ) -> AsyncMock | None:
        if properties == coordinator.GRAPHQL_LOCATION_FIELDS:
            coor.location_on_error = on_error
            if error_first:
                on_error(VALIDATION_ERROR)
            if isinstance(location_unsubscribe, Exception):
                raise location_unsubscribe
            return location_unsubscribe
        coor._initial.set()
        coor.data = {"activeDriverName": {"value": "Driver"}}
        return unsubscribe

    coor.api = MagicMock(
        subscribe_for_vehicle_updates=AsyncMock(side_effect=subscribe),
        subscribe_for_parallax_messages=AsyncMock(return_value=unsubscribe),
        _ws_monitor=None,
    )
    return coor


async def test_position_is_its_own_subscription() -> None:
    location_unsubscribe = AsyncMock()
    coor = subscribing_vehicle(location_unsubscribe)

    assert await coor._async_update_data() == {"activeDriverName": {"value": "Driver"}}

    requested = [
        call.kwargs["properties"]
        for call in coor.api.subscribe_for_vehicle_updates.await_args_list
    ]
    assert requested == [
        coordinator.VEHICLE_STATE_API_FIELDS,
        coordinator.GRAPHQL_LOCATION_FIELDS,
    ]
    assert (
        not coordinator.VEHICLE_STATE_API_FIELDS & coordinator.GRAPHQL_LOCATION_FIELDS
    )

    await coor._unsubscribe()
    location_unsubscribe.assert_awaited_once()


async def test_setup_carries_on_without_the_position_subscription() -> None:
    coor = subscribing_vehicle(None)

    assert await coor._async_update_data() == {"activeDriverName": {"value": "Driver"}}
    assert coor._unsub_location is None


async def test_unsubscribing_twice_ends_the_position_subscription_once() -> None:
    location_unsubscribe = AsyncMock()
    coor = subscribing_vehicle(location_unsubscribe)
    await coor._async_update_data()

    await asyncio.gather(coor._unsubscribe(), coor._unsubscribe())

    location_unsubscribe.assert_awaited_once()


async def test_setup_carries_on_when_the_position_subscription_raises() -> None:
    coor = subscribing_vehicle(RuntimeError("boom"))

    assert await coor._async_update_data() == {"activeDriverName": {"value": "Driver"}}
    assert coor._unsub_location is None


async def test_failed_position_subscription_warns_once_and_ends(
    caplog: pytest.LogCaptureFixture,
) -> None:
    location_unsubscribe = AsyncMock()
    coor = subscribing_vehicle(location_unsubscribe)
    await coor._async_update_data()

    coor.location_on_error(VALIDATION_ERROR)
    coor.location_on_error(VALIDATION_ERROR)  # again, e.g. after a reconnect
    await asyncio.sleep(0)

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert 'Cannot query field "gnssLocation"' in warnings[0].getMessage()
    assert "Parallax" in warnings[0].getMessage()
    location_unsubscribe.assert_awaited_once()
    assert coor._unsub_location is None


async def test_failed_position_subscription_is_not_subscribed_again() -> None:
    coor = subscribing_vehicle(AsyncMock())
    await coor._async_update_data()
    coor.location_on_error(VALIDATION_ERROR)
    await asyncio.sleep(0)
    coor.api.subscribe_for_vehicle_updates.reset_mock()

    await coor._subscribe_location()

    coor.api.subscribe_for_vehicle_updates.assert_not_awaited()


async def test_error_before_the_unsubscribe_handle_still_ends_it() -> None:
    location_unsubscribe = AsyncMock()
    coor = subscribing_vehicle(location_unsubscribe, error_first=True)

    await coor._async_update_data()
    await asyncio.sleep(0)

    location_unsubscribe.assert_awaited_once()
    assert coor._unsub_location is None


async def test_unexpected_error_message_still_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    coor = subscribing_vehicle(AsyncMock())
    await coor._async_update_data()

    coor.location_on_error({"type": "error", "payload": "unexpected"})
    await asyncio.sleep(0)

    assert any(r.levelname == "WARNING" for r in caplog.records)
    assert coor._location_failed
