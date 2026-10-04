"""Tests for the stale-dashboard repair check run at integration setup."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from custom_components.rivian import _async_check_dashboard_staleness
from custom_components.rivian.const import DASHBOARD_SCHEMA_VERSION

STALE_EXPR = "hass.states['sensor.x']?.attributes?.recent_drives || []"
CURRENT_EXPR = "(window.__rivianAnalytics?.['VIN']?.drives || [])"


def _dashboard(expr: str, schema_version: int | None = None) -> dict[str, Any]:
    config: dict[str, Any] = {
        "views": [{"cards": [{"type": "custom:plotly-graph", "fn": expr}]}]
    }
    if schema_version is not None:
        config["schema_version"] = schema_version
    return {"config": config}


async def _issue_raised(stores: dict[str, Any]) -> bool:
    """Run the check against the given stored dashboards; return whether it flagged one."""

    class MockStore:
        def __init__(self, _hass, _version, key):
            self.key = key

        async def async_load(self):
            return stores.get(self.key)

    create, delete = MagicMock(), MagicMock()
    with (
        patch("custom_components.rivian.Store", side_effect=MockStore),
        patch("custom_components.rivian.async_create_issue", create),
        patch("custom_components.rivian.async_delete_issue", delete),
    ):
        await _async_check_dashboard_staleness(MagicMock())

    raised = create.called
    assert raised != delete.called, "exactly one of create/delete must run"
    return raised


def _registered(*ids: str) -> dict[str, Any]:
    return {"lovelace_dashboards": {"items": [{"id": i} for i in ids]}}


@pytest.mark.asyncio
async def test_unversioned_dashboard_reading_removed_attributes_is_flagged() -> None:
    """Dashboards generated before schema_version existed are the ones that break."""
    stores = _registered("rivian_efficiency")
    stores["lovelace.rivian_efficiency"] = _dashboard(STALE_EXPR)
    assert await _issue_raised(stores)


@pytest.mark.asyncio
async def test_unrelated_user_dashboard_is_not_flagged() -> None:
    stores = _registered("home")
    stores["lovelace.home"] = _dashboard("states['sensor.outside_temp'].state")
    assert not await _issue_raised(stores)


@pytest.mark.asyncio
async def test_current_generated_dashboard_is_not_flagged() -> None:
    stores = _registered("rivian_efficiency")
    stores["lovelace.rivian_efficiency"] = _dashboard(
        CURRENT_EXPR, DASHBOARD_SCHEMA_VERSION
    )
    assert not await _issue_raised(stores)


@pytest.mark.asyncio
async def test_older_schema_version_is_flagged() -> None:
    stores = _registered("rivian_efficiency")
    stores["lovelace.rivian_efficiency"] = _dashboard(
        CURRENT_EXPR, DASHBOARD_SCHEMA_VERSION - 1
    )
    assert await _issue_raised(stores)


@pytest.mark.asyncio
async def test_stale_default_overview_is_flagged() -> None:
    """The Overview dashboard isn't in lovelace_dashboards but must still be checked."""
    stores = _registered()
    stores["lovelace"] = _dashboard(STALE_EXPR)
    assert await _issue_raised(stores)
