"""Tests for when subscribing again after it failed, or asking to log in again."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.rivian import coordinator
from homeassistant.exceptions import ConfigEntryAuthFailed

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
ISSUE_ID = "rate_limited_entry"


def vehicle(
    backoff: float = 0, rate_limited: bool = False, unauthenticated: bool = False
) -> coordinator.VehicleCoordinator:
    """A vehicle coordinator whose client's web socket is in the given state."""
    coor = object.__new__(coordinator.VehicleCoordinator)
    coor.hass = MagicMock()
    coor.config_entry = SimpleNamespace(entry_id="entry")
    coor.api = SimpleNamespace(
        ws_backoff_remaining=backoff,
        ws_rate_limited=rate_limited,
        ws_unauthenticated=unauthenticated,
    )
    coor._rate_limited_since = None
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


def test_without_a_backoff() -> None:
    assert retry_delay(vehicle())[0] == coordinator.SUBSCRIBE_RETRY_DELAY


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
