"""Tests for removing the keyless precondition controls once a phone is paired."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.rivian import button

VEHICLES = {
    "paired": {
        "id": "paired",
        "vin": "PAIRED",
        "phone_identity_id": "phone",
        "name": "R1T",
        "model": "R1T",
    },
    "keyless": {"id": "keyless", "vin": "KEYLESS", "name": "R1S", "model": "R1S"},
}


def _hass() -> tuple[SimpleNamespace, SimpleNamespace]:
    coordinators = {
        vehicle_id: MagicMock(
            has_paired_phone=lambda identity, paired=vehicle_id == "paired": paired,
            drivers_coordinator=MagicMock(
                get_device_details=lambda identity: {"isPaired": True}
            ),
        )
        for vehicle_id in VEHICLES
    }
    entry = SimpleNamespace(
        entry_id="entry",
        runtime_data=SimpleNamespace(
            vehicles=VEHICLES, vehicle_coordinators=coordinators
        ),
    )
    return SimpleNamespace(), entry


def _stale(module: Any, monkeypatch: pytest.MonkeyPatch) -> list[tuple[set, set]]:
    calls: list[tuple[set, set]] = []
    monkeypatch.setattr(
        module,
        "async_remove_stale_entities",
        lambda hass, platform, possible, created: calls.append(
            (set(possible), set(created))
        ),
    )
    return calls


async def test_keyless_button_removed_after_pairing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A paired vehicle's precondition button is removed; an unpaired one's kept."""
    calls = _stale(button, monkeypatch)
    hass, entry = _hass()

    await button.async_setup_entry(hass, entry, MagicMock())

    (possible, created) = calls[-1]
    assert "PAIRED-precondition_cabin" in possible - created
    assert "KEYLESS-precondition_cabin" in created
