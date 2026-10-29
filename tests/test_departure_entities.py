"""Tests for keeping one entity per departure schedule."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.rivian import helpers

PREFIX = "VIN-departure_schedule_"


class _Registry:
    def __init__(self, entries: dict[str, str]) -> None:
        # entity_id -> unique_id
        self.entries = dict(entries)

    def async_remove(self, entity_id: str) -> None:
        del self.entries[entity_id]

    def unique_ids(self) -> set[str]:
        return set(self.entries.values())


class _Harness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, registered: set[str]) -> None:
        self.registry = _Registry({f"switch.{sid}": PREFIX + sid for sid in registered})
        self.timers: list[tuple[float, Callable[[Any], None], MagicMock]] = []
        self.added: list[str] = []
        self.unloads: list[Callable[[], None]] = []
        self.listener: Callable[[], None] | None = None
        self.coordinator = SimpleNamespace(
            departure_schedules=None, async_add_listener=self._add_listener
        )

        monkeypatch.setattr(
            helpers,
            "er",
            SimpleNamespace(
                async_get=lambda hass: self.registry,
                async_entries_for_config_entry=lambda registry, entry_id: [
                    SimpleNamespace(domain="switch", unique_id=uid, entity_id=eid)
                    for eid, uid in registry.entries.items()
                ],
            ),
        )
        monkeypatch.setattr(helpers, "async_call_later", self._call_later)

    def _add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self.listener = listener
        return MagicMock()

    def _call_later(
        self, hass: Any, delay: float, action: Callable[[Any], None]
    ) -> MagicMock:
        cancel = MagicMock()
        self.timers.append((delay, action, cancel))
        return cancel

    def _add_entities(self, entities: Any) -> None:
        for schedule_id in entities:
            self.added.append(schedule_id)
            self.registry.entries[f"switch.{schedule_id}"] = PREFIX + schedule_id

    def start(self, schedule_ids: list[str] | None) -> None:
        self.set(schedule_ids, notify=False)
        helpers.track_departure_schedule_entities(
            MagicMock(),
            SimpleNamespace(entry_id="entry", async_on_unload=self.unloads.append),
            self.coordinator,
            "VIN",
            "switch",
            "departure_schedule",
            lambda schedule_id: schedule_id,
            self._add_entities,
        )

    def set(self, schedule_ids: list[str] | None, notify: bool = True) -> None:
        self.coordinator.departure_schedules = (
            None if schedule_ids is None else [{"id": sid} for sid in schedule_ids]
        )
        if notify:
            assert self.listener
            self.listener()

    def fire_removal(self) -> None:
        self.timers[-1][1](None)


def test_brief_empty_list_keeps_entities(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rivian has sent an empty list for about a second before the full list.

    Removing the registry entry then loses the user's changes to the entity, such
    as its name and area, and adds it back as a new entity.
    """
    harness = _Harness(monkeypatch, registered=set())
    harness.start(["a"])
    assert harness.added == ["a"]

    harness.set([])
    assert harness.registry.unique_ids() == {PREFIX + "a"}
    assert len(harness.timers) == 1

    harness.set(["a"])
    harness.timers[0][2].assert_called_once()  # removal cancelled
    assert harness.registry.unique_ids() == {PREFIX + "a"}
    assert harness.added == ["a"]  # not added again


def test_deleted_schedule_removed_after_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _Harness(monkeypatch, registered=set())
    harness.start(["a", "b"])

    harness.set(["b"])
    assert harness.timers[-1][0] == helpers.DEPARTURE_SCHEDULE_REMOVE_DELAY
    harness.fire_removal()

    assert harness.registry.unique_ids() == {PREFIX + "b"}

    harness.set(["a", "b"])  # a schedule with the same id comes back
    assert harness.added.count("a") == 2


def test_removal_uses_the_list_when_it_fires(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only schedules still missing when the delay ends are removed."""
    harness = _Harness(monkeypatch, registered=set())
    harness.start(["a", "b"])

    harness.set([])
    harness.coordinator.departure_schedules = [{"id": "a"}]  # no listener call yet
    harness.fire_removal()

    assert harness.registry.unique_ids() == {PREFIX + "a"}


def test_schedule_deleted_while_stopped_is_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(monkeypatch, registered={"a", "gone"})
    harness.start(["a"])

    assert harness.added == ["a"]
    assert harness.registry.unique_ids() == {PREFIX + "a", PREFIX + "gone"}
    harness.fire_removal()
    assert harness.registry.unique_ids() == {PREFIX + "a"}


def test_no_removal_before_schedules_arrive(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _Harness(monkeypatch, registered={"a"})
    harness.start(None)

    assert harness.timers == []
    assert harness.registry.unique_ids() == {PREFIX + "a"}


def test_unload_cancels_pending_removal(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _Harness(monkeypatch, registered=set())
    harness.start(["a"])
    harness.set([])

    for unload in harness.unloads:
        unload()

    harness.timers[-1][2].assert_called_once()
