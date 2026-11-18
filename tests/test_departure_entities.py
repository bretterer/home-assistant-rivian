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

    def _add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self.listener = listener
        return MagicMock()

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


def test_deleted_schedule_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The coordinator only passes on confirmed lists, so one missing is gone."""
    harness = _Harness(monkeypatch, registered=set())
    harness.start(["a", "b"])

    harness.set(["b"])
    assert harness.registry.unique_ids() == {PREFIX + "b"}

    harness.set(["a", "b"])  # a schedule with the same id comes back
    assert harness.added.count("a") == 2


def test_schedule_deleted_while_stopped_is_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(monkeypatch, registered={"a", "gone"})
    harness.start(["a"])

    assert harness.added == ["a"]
    assert harness.registry.unique_ids() == {PREFIX + "a"}


def test_no_removal_before_schedules_arrive(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _Harness(monkeypatch, registered={"a"})
    harness.start(None)

    assert harness.registry.unique_ids() == {PREFIX + "a"}


def test_unchanged_list_is_not_rescanned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Vehicle messages update listeners too; the same list isn't processed again."""
    harness = _Harness(monkeypatch, registered=set())
    harness.start(["a"])
    harness.registry.entries["switch.stray"] = PREFIX + "stray"

    assert harness.listener
    harness.listener()  # the same list object

    assert PREFIX + "stray" in harness.registry.unique_ids()


@pytest.mark.parametrize(
    ("unit", "temperature"), [("°C", 21), ("°F", 69.8)], ids=["celsius", "fahrenheit"]
)
def test_summary_temperature_in_the_users_unit(unit: str, temperature: float) -> None:
    """Schedule temperatures are shown in the user's unit, as the actions take them."""
    schedule = {
        "id": "S1",
        "departureSettings": {"comfortSettings": {"cabinTempCelsius": 21}},
    }
    summary = helpers.departure_schedule_summary(schedule, unit)
    assert summary["temperature"] == temperature


def test_summary_departure_time() -> None:
    """The departure time is named as the actions take it, as HH:MM."""
    schedule = {"id": "S1", "occurrence": {"days": ["Wednesday"], "startsAtMin": 732}}
    summary = helpers.departure_schedule_summary(schedule, "°C")
    assert summary["departure_time"] == "12:12"
    assert "time" not in summary
