"""Tests for the tonneau sensors, added once a power tonneau reports its state."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from custom_components.rivian import binary_sensor

TONNEAU_KEYS = {"closure_tonneau_closed", "closure_tonneau_locked"}


class FakeCoordinator:
    """Holds vehicle state and calls its listeners when it changes."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.listeners: list[Callable[[], None]] = []
        self.charging_coordinator = MagicMock()
        self.drivers_coordinator = MagicMock()

    def get(self, key: str) -> Any | None:
        return (self.data.get(key) or {}).get("value")

    def async_add_listener(self, update_callback: Callable[[], None]) -> Callable:
        self.listeners.append(update_callback)
        return lambda: self.listeners.remove(update_callback)

    def update(self, **values: Any) -> None:
        self.data |= {key: {"value": value} for key, value in values.items()}
        for listener in list(self.listeners):
            listener()


def setup(
    coordinator: FakeCoordinator, features: list[str], model: str = "R1T"
) -> tuple[list[Any], MagicMock, MagicMock]:
    """Set up the platform for one vehicle; return the added entities and mocks."""
    vehicle = {
        "id": "V",
        "vin": "VIN1",
        "name": "Truck",
        "model": model,
        "supported_features": features,
    }
    entry = MagicMock(entry_id="E")
    entry.runtime_data = SimpleNamespace(
        vehicles={"V": vehicle}, vehicle_coordinators={"V": coordinator}
    )
    hass = MagicMock()
    added: list[Any] = []
    remove_stale = MagicMock()
    with patch.object(binary_sensor, "async_remove_stale_entities", remove_stale):
        asyncio.run(
            binary_sensor.async_setup_entry(hass, entry, lambda e: added.extend(e))
        )
    return added, entry, remove_stale


def tonneau_keys(entities: list[Any]) -> set[str]:
    return {
        e.entity_description.key
        for e in entities
        if e.entity_description.key in TONNEAU_KEYS
    }


def test_tonneau_command_does_not_add_them() -> None:
    """The tonneau command only gates the cover; the sensors wait for a state."""
    coordinator = FakeCoordinator({})
    added, _, _ = setup(coordinator, ["TONNEAU_CMD"])
    assert not tonneau_keys(added)

    coordinator.update(closureTonneauClosed="closed", closureTonneauLocked="locked")
    assert tonneau_keys(added) == TONNEAU_KEYS


def test_added_at_setup_when_already_reported() -> None:
    """A Gen 1 tonneau that already reports its state gets the sensors at setup."""
    coordinator = FakeCoordinator(
        {
            "closureTonneauClosed": {"value": "closed"},
            "closureTonneauLocked": {"value": "unlocked"},
        }
    )
    added, _, _ = setup(coordinator, [])
    assert tonneau_keys(added) == TONNEAU_KEYS
    assert not coordinator.listeners


def test_added_once_each_is_reported() -> None:
    """Each sensor is added the first time its state arrives, then it stops."""
    coordinator = FakeCoordinator({"closureTonneauClosed": {"value": None}})
    added, entry, _ = setup(coordinator, [])
    assert not tonneau_keys(added)
    assert len(coordinator.listeners) == 1

    coordinator.update(doorFrontLeftClosed="closed")
    assert not tonneau_keys(added)

    coordinator.update(closureTonneauClosed="closed")
    assert tonneau_keys(added) == {"closure_tonneau_closed"}

    coordinator.update(closureTonneauClosed="open", closureTonneauLocked="locked")
    assert tonneau_keys(added) == TONNEAU_KEYS
    # Each is added only once
    assert sum(e.entity_description.key in TONNEAU_KEYS for e in added) == 2
    assert not coordinator.listeners

    # Unloading the entry after the listener stopped doesn't remove it again
    stop_listening = entry.async_on_unload.call_args.args[0]
    stop_listening()


def test_never_added_without_a_tonneau() -> None:
    """A truck without a tonneau reports it with no state, so never gets them."""
    coordinator = FakeCoordinator({})
    added, entry, _ = setup(coordinator, [])
    coordinator.update(closureTonneauClosed=None, doorFrontLeftClosed="closed")
    assert not tonneau_keys(added)

    entry.async_on_unload.call_args.args[0]()
    assert not coordinator.listeners


def test_never_added_for_an_unspecified_state() -> None:
    """An unspecified state, which the client passes on as 0, isn't a value."""
    coordinator = FakeCoordinator({})
    added, _, _ = setup(coordinator, [])
    coordinator.update(closureTonneauClosed=0, closureTonneauLocked=0)
    assert not tonneau_keys(added)
    assert len(coordinator.listeners) == 1


def test_not_removed_as_stale_before_reported() -> None:
    """A sleeping truck's registered tonneau sensors aren't deleted at setup."""
    _, _, remove_stale = setup(FakeCoordinator({}), [])
    possible = set(remove_stale.call_args.args[2])
    assert "VIN1-door_front_left_closed" in possible
    assert not {f"VIN1-{key}" for key in TONNEAU_KEYS} & possible


def test_removed_as_stale_for_other_models() -> None:
    """An R1S can never get the tonneau sensors, so leftovers are removed."""
    _, _, remove_stale = setup(FakeCoordinator({}), ["PVS_BD_ST8"], model="R1S")
    possible = set(remove_stale.call_args.args[2])
    assert {f"VIN1-{key}" for key in TONNEAU_KEYS} <= possible
