"""Tests for the one-time Rivian dashboard creation on first setup."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components import rivian as integration


class FakeStore:
    """In-memory stand-in for homeassistant.helpers.storage.Store."""

    data: dict[str, Any] = {}

    def __init__(self, hass: Any, version: int, key: str) -> None:
        self.key = key

    async def async_load(self) -> Any:
        return FakeStore.data.get(self.key)

    async def async_save(self, value: Any) -> None:
        FakeStore.data[self.key] = value


def _hass(mode: str = "storage", dashboards: dict | None = None) -> MagicMock:
    hass = MagicMock()
    hass.data = {
        "lovelace": MagicMock(mode=mode, dashboards=dashboards or {}),
    }
    return hass


@pytest.fixture(autouse=True)
def _fresh_store():
    FakeStore.data = {}
    with patch.object(integration, "Store", FakeStore):
        yield


async def _run(hass: MagicMock) -> AsyncMock:
    create = AsyncMock(return_value=True)
    with patch.object(integration, "async_create_efficiency_dashboard", create):
        await integration._async_auto_create_dashboard(hass)
    return create


async def test_created_once_on_first_setup() -> None:
    hass = _hass()
    create = await _run(hass)
    create.assert_awaited_once()
    kwargs = create.await_args.kwargs
    assert kwargs["url_path"] == "rivian-dashboard"
    assert kwargs["title"] == "Rivian"
    assert FakeStore.data[integration._DASHBOARD_AUTOCREATE_STORE] == {"created": True}

    # A second run (e.g. next restart) does nothing.
    create2 = await _run(hass)
    create2.assert_not_awaited()


async def test_skipped_when_flag_set() -> None:
    FakeStore.data[integration._DASHBOARD_AUTOCREATE_STORE] = {"created": True}
    create = await _run(_hass())
    create.assert_not_awaited()


async def test_existing_dashboard_skips_creation_but_sets_flag() -> None:
    hass = _hass(dashboards={"rivian-dashboard": object()})
    create = await _run(hass)
    create.assert_not_awaited()
    assert FakeStore.data[integration._DASHBOARD_AUTOCREATE_STORE] == {"created": True}


async def test_existing_dashboard_in_storage_file_skips_creation() -> None:
    FakeStore.data["lovelace_dashboards"] = {
        "items": [{"url_path": "rivian-dashboard"}]
    }
    create = await _run(_hass())
    create.assert_not_awaited()
    assert FakeStore.data[integration._DASHBOARD_AUTOCREATE_STORE] == {"created": True}


async def test_yaml_mode_notifies_and_sets_flag() -> None:
    hass = _hass(mode="yaml")
    from homeassistant.components import persistent_notification

    with patch.object(
        persistent_notification, "async_create", MagicMock(), create=True
    ) as notify:
        create = await _run(hass)
    create.assert_not_awaited()
    notify.assert_called_once()
    assert notify.call_args.kwargs["title"] == "Rivian dashboard"
    assert "YAML" in notify.call_args.args[1]
    assert FakeStore.data[integration._DASHBOARD_AUTOCREATE_STORE] == {"created": True}


async def test_failure_warns_and_does_not_raise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    create = AsyncMock(side_effect=RuntimeError("boom"))
    with (
        caplog.at_level(logging.WARNING),
        patch.object(integration, "async_create_efficiency_dashboard", create),
    ):
        await integration._async_auto_create_dashboard(_hass())
    assert "Could not create the Rivian dashboard" in caplog.text
    # Flag stays unset so the next startup retries.
    assert integration._DASHBOARD_AUTOCREATE_STORE not in FakeStore.data


async def test_schedule_runs_once_per_instance() -> None:
    hass = MagicMock()
    hass.data = {}
    hass.is_running = True
    with patch.object(integration, "_async_auto_create_dashboard", AsyncMock()):
        integration._schedule_dashboard_autocreate(hass)
        integration._schedule_dashboard_autocreate(hass)
    hass.async_create_background_task.assert_called_once()
    hass.async_create_background_task.call_args.args[0].close()


async def test_schedule_waits_for_start_when_not_running() -> None:
    hass = MagicMock()
    hass.data = {}
    hass.is_running = False
    integration._schedule_dashboard_autocreate(hass)
    hass.bus.async_listen_once.assert_called_once()
    hass.async_create_background_task.assert_not_called()
    # The listener must be an HA @callback: a plain function is run in a
    # worker thread, where creating the task fails on a real instance.
    listener = hass.bus.async_listen_once.call_args.args[1]
    assert getattr(listener, "_hass_callback", False) is True
