"""Tests for removing the keyless precondition controls once a phone is paired."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.rivian import button, number
from custom_components.rivian.const import ATTR_COORDINATOR, ATTR_VEHICLE, DOMAIN

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
    entry = SimpleNamespace(entry_id="entry")
    hass = SimpleNamespace(
        data={
            DOMAIN: {
                "entry": {
                    ATTR_VEHICLE: VEHICLES,
                    ATTR_COORDINATOR: {ATTR_VEHICLE: coordinators},
                }
            }
        }
    )
    return hass, entry


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


@pytest.mark.parametrize(
    ("module", "keys"),
    [
        (button, ["precondition_cabin"]),
        (number, ["precondition_lead_time", "precondition_temperature"]),
    ],
    ids=["button", "number"],
)
async def test_keyless_controls_removed_after_pairing(
    monkeypatch: pytest.MonkeyPatch, module: Any, keys: list[str]
) -> None:
    """A paired vehicle's keyless controls are removed; an unpaired one's are kept."""
    calls = _stale(module, monkeypatch)
    hass, entry = _hass()

    await module.async_setup_entry(hass, entry, MagicMock())

    (possible, created) = calls[-1]
    for key in keys:
        assert f"PAIRED-{key}" in possible - created
        assert f"KEYLESS-{key}" in created
