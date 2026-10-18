"""Tests for registering the bundled cards as Lovelace resources."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from custom_components.rivian import (
    _BUNDLED_MODULES,
    _async_register_lovelace_resources,
    _bundled_module_versions,
)

STATIC = "/rivian_static"
URLS = {
    "rivian-overview-card.js": f"{STATIC}/rivian-overview-card.js?v=2",
    "rivian-drive-explorer-card.js": f"{STATIC}/rivian-drive-explorer-card.js?v=2",
}


class _LiveResources:
    """The slice of Lovelace's ResourceStorageCollection the registration uses."""

    def __init__(self, items: list[dict[str, Any]], loaded: bool = True) -> None:
        self._items = items
        self.loaded = loaded
        self.load_calls = 0

    async def async_load(self) -> None:
        self.load_calls += 1

    def async_items(self) -> list[dict[str, Any]]:
        return list(self._items)

    async def async_create_item(self, data: dict[str, Any]) -> None:
        self._items.append({"id": f"new{len(self._items)}", "url": data["url"]})

    async def async_update_item(self, item_id: str, data: dict[str, Any]) -> None:
        for item in self._items:
            if item["id"] == item_id:
                item["url"] = data["url"]

    async def async_delete_item(self, item_id: str) -> None:
        self._items = [i for i in self._items if i["id"] != item_id]


def _hass(lovelace: Any) -> SimpleNamespace:
    return SimpleNamespace(data={"lovelace": lovelace} if lovelace else {})


@pytest.mark.asyncio
async def test_live_collection_is_updated_so_no_restart_is_needed() -> None:
    """A direct file write is invisible once Lovelace has loaded its resources."""
    resources = _LiveResources(
        [{"id": "a", "url": f"{STATIC}/rivian-overview-card.js?v=1"}], loaded=False
    )
    lovelace = SimpleNamespace(resource_mode="storage", resources=resources)

    with patch("custom_components.rivian.Store") as store:
        assert await _async_register_lovelace_resources(_hass(lovelace), STATIC, URLS)
        store.assert_not_called()

    assert resources.load_calls == 1
    assert sorted(i["url"] for i in resources.async_items()) == sorted(URLS.values())


@pytest.mark.asyncio
async def test_yaml_resource_mode_falls_back_to_extra_js_urls() -> None:
    lovelace = SimpleNamespace(resource_mode="yaml", resources=_LiveResources([]))
    assert not await _async_register_lovelace_resources(_hass(lovelace), STATIC, URLS)


@pytest.mark.asyncio
async def test_storage_file_used_before_lovelace_is_set_up() -> None:
    saved: dict[str, Any] = {}

    class _Store:
        def __init__(self, _hass: Any, _version: int, _key: str) -> None:
            pass

        async def async_load(self) -> dict[str, Any]:
            return {
                "items": [
                    {"id": "a", "url": f"{STATIC}/rivian-overview-card.js"},
                    {"id": "b", "url": f"{STATIC}/plotly-graph-card.js?v=1"},
                    {"id": "c", "url": "/hacsfiles/other/card.js"},
                ]
            }

        async def async_save(self, data: dict[str, Any]) -> None:
            saved.update(data)

    with patch("custom_components.rivian.Store", _Store):
        assert await _async_register_lovelace_resources(_hass(None), STATIC, URLS)

    assert sorted(i["url"] for i in saved["items"]) == sorted(
        [*URLS.values(), "/hacsfiles/other/card.js"]
    )


@pytest.mark.asyncio
async def test_retired_modules_resources_are_removed() -> None:
    """The Plotly/series cards no longer ship; their resources are dropped."""
    resources = _LiveResources(
        [
            {"id": "a", "url": f"{STATIC}/rivian-series-card.js?v=1"},
            {"id": "b", "url": f"{STATIC}/mushroom.js?v=1"},
            {"id": "c", "url": f"{STATIC}/rivian-charging-sessions-card.js?v=1"},
            {"id": "d", "url": "/hacsfiles/lovelace-mushroom/mushroom.js"},
        ]
    )
    lovelace = SimpleNamespace(resource_mode="storage", resources=resources)

    assert await _async_register_lovelace_resources(_hass(lovelace), STATIC, URLS)

    assert sorted(i["url"] for i in resources.async_items()) == sorted(
        [*URLS.values(), "/hacsfiles/lovelace-mushroom/mushroom.js"]
    )


def test_module_version_changes_when_a_card_changes(tmp_path: Any) -> None:
    """A fixed ?v= let browsers keep serving a stale card after an update."""
    card = tmp_path / _BUNDLED_MODULES[0]
    card.write_text("v1")
    before = _bundled_module_versions(tmp_path)

    card.write_text("version two")
    after = _bundled_module_versions(tmp_path)

    assert set(before) == {_BUNDLED_MODULES[0]}  # missing cards are skipped
    assert before != after


def test_rivian_modules_share_one_combined_version(tmp_path: Any) -> None:
    """Cards import the shared bar module with their own ?v=, so all must match."""
    names = [n for n in _BUNDLED_MODULES if n.startswith("rivian-")]
    assert names == list(_BUNDLED_MODULES)  # no vendored modules any more
    for name in names[:3]:
        (tmp_path / name).write_text("a")

    before = _bundled_module_versions(tmp_path)
    assert len({before[n] for n in names[:3]}) == 1
    assert set(before) == set(names[:3])  # missing ones skipped

    (tmp_path / names[1]).write_text("changed and longer")
    after = _bundled_module_versions(tmp_path)
    assert len({after[n] for n in names[:3]}) == 1
    assert after[names[0]] != before[names[0]]  # any change refreshes them all


def test_shared_bar_modules_are_bundled() -> None:
    assert "rivian-vehicle-bar.js" in _BUNDLED_MODULES
    assert "rivian-vehicle-bar-card.js" in _BUNDLED_MODULES
