"""Tests for when subscribing again after it failed, or asking to log in again."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.rivian import coordinator, repairs
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
ISSUE_ID = "rate_limited_entry"


def vehicle(
    backoff: float = 0,
    rate_limited: bool = False,
    unauthenticated: bool = False,
    data: dict[str, Any] | None = None,
    subscribed: bool = False,
) -> coordinator.VehicleCoordinator:
    """A vehicle coordinator whose client's web socket is in the given state."""
    coor = object.__new__(coordinator.VehicleCoordinator)
    coor.hass = MagicMock()
    coor.config_entry = MagicMock(entry_id="entry")
    unsubscribe = AsyncMock() if subscribed else None
    coor.api = SimpleNamespace(
        ws_backoff_remaining=backoff,
        ws_rate_limited=rate_limited,
        ws_unauthenticated=unauthenticated,
        subscribe_for_vehicle_updates=AsyncMock(return_value=unsubscribe),
        subscribe_for_parallax_messages=AsyncMock(return_value=unsubscribe),
    )
    coor.vehicle_id = "vehicle"
    coor.data = data
    coor.last_update_success = True
    coor._unsub_handler = coor._unsub_parallax = None
    coor._initial = asyncio.Event()
    coor._rate_limited_since = None
    coor._subscribe_failures = 0
    coor.get_charging_schedule_data = AsyncMock()
    return coor


def retry_delay(
    coor: coordinator.VehicleCoordinator, now: datetime = T0
) -> tuple[float, MagicMock]:
    """Return the retry delay and the mocked async_create_issue."""
    with (
        patch.object(coordinator.dt_util, "utcnow", return_value=now),
        patch.object(coordinator, "async_create_issue") as create_issue,
    ):
        return coor._subscribe_retry_delay("failed"), create_issue


def test_waits_out_the_backoff() -> None:
    assert retry_delay(vehicle(backoff=37.5))[0] == 38.5


def test_without_a_backoff_backs_off_here() -> None:
    """Each retry connects, so the delay doubles, up to the max."""
    coor = vehicle()

    delays = [retry_delay(coor)[0] for _ in range(6)]

    assert delays == [60, 120, 240, 480, 900, 900]


def test_unauthenticated_asks_to_log_in_again() -> None:
    with pytest.raises(ConfigEntryAuthFailed):
        retry_delay(vehicle(unauthenticated=True))


def test_rate_limited_offers_then_requires_logging_in_again() -> None:
    coor = vehicle(backoff=120, rate_limited=True)

    delay, create_issue = retry_delay(coor)
    assert delay == 121
    assert coor._rate_limited_since == T0
    create_issue.assert_not_called()

    # After a while, a repair issue offers to log in again
    delay, create_issue = retry_delay(coor, T0 + coordinator.RATE_LIMIT_ISSUE_AFTER)
    assert delay == 121
    create_issue.assert_called_once()
    assert create_issue.call_args.args[2] == ISSUE_ID
    assert create_issue.call_args.kwargs["data"] == {"entry_id": "entry"}

    # Then it's required
    with pytest.raises(ConfigEntryAuthFailed):
        retry_delay(coor, T0 + coordinator.RATE_LIMIT_REAUTH_AFTER)


def test_other_failures_keep_when_the_rate_limit_started() -> None:
    """A failure that isn't a rate limit doesn't end the rate limited stretch."""
    coor = vehicle(backoff=120, rate_limited=True)
    retry_delay(coor)

    coor.api = vehicle(backoff=45).api
    assert retry_delay(coor, T0 + timedelta(minutes=10))[0] == 46
    assert coor._rate_limited_since == T0


async def test_setup_finishes_without_data_when_subscribing_fails() -> None:
    coor = vehicle(backoff=37)

    with patch.object(coordinator, "async_call_later") as call_later:
        assert await coor._async_update_data() == {}

    # Parallax isn't tried once the web socket failed
    coor.api.subscribe_for_parallax_messages.assert_not_called()
    call_later.assert_called_once_with(coor.hass, 38, coor._retry_subscribe)
    coor.config_entry.async_on_unload.assert_called_once_with(call_later.return_value)


async def test_retry_after_setup_fails_with_retry_after() -> None:
    coor = vehicle(backoff=37, data={})

    with pytest.raises(UpdateFailed) as err:
        await coor._async_update_data()

    assert err.value.retry_after == 38


async def test_setup_asks_to_log_in_again_when_unauthenticated() -> None:
    coor = vehicle(unauthenticated=True)

    with pytest.raises(ConfigEntryAuthFailed):
        await coor._async_update_data()


async def test_subscribing_resets_and_clears_the_issue() -> None:
    coor = vehicle(data={}, subscribed=True)
    coor._rate_limited_since = T0
    coor._subscribe_failures = 3
    coor._initial.set()

    with patch.object(coordinator, "async_delete_issue") as delete_issue:
        assert await coor._async_update_data() == {}

    delete_issue.assert_called_once_with(coor.hass, coordinator.DOMAIN, ISSUE_ID)
    assert coor._rate_limited_since is None
    assert coor._subscribe_failures == 0


async def test_repair_starts_logging_in_again() -> None:
    flow = await repairs.async_create_fix_flow(
        MagicMock(), ISSUE_ID, {"entry_id": "entry"}
    )
    flow.hass = MagicMock()
    entry = flow.hass.config_entries.async_get_entry.return_value

    assert (await flow.async_step_init())["type"] == "form"
    entry.async_start_reauth.assert_not_called()

    result = await flow.async_step_confirm({})

    flow.hass.config_entries.async_get_entry.assert_called_once_with("entry")
    entry.async_start_reauth.assert_called_once_with(flow.hass)
    assert result["type"] == "create_entry"


async def test_repair_for_a_removed_entry_just_closes() -> None:
    flow = await repairs.async_create_fix_flow(
        MagicMock(), ISSUE_ID, {"entry_id": "entry"}
    )
    flow.hass = MagicMock()
    flow.hass.config_entries.async_get_entry.return_value = None

    assert (await flow.async_step_confirm({}))["type"] == "create_entry"


async def test_other_repairs_only_confirm() -> None:
    flow = await repairs.async_create_fix_flow(
        MagicMock(), "state_values_changed", None
    )

    assert not isinstance(flow, repairs.RateLimitedRepairFlow)
