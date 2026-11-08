"""Tests for the pet mode switch."""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.rivian import switch

SWITCH = next(d for d in switch.SWITCHES if d.key == "pet_mode")


def test_switch_follows_status() -> None:
    """The switch is on only while pet mode is on."""
    coor = MagicMock()
    for status, on in (
        ("on", True),
        ("off", False),
        ("disabled", False),
        ("faulty", False),
    ):
        coor.get.side_effect = {"petModeStatus": status}.get
        assert SWITCH.is_on(coor) is on
    assert SWITCH.supported_feature == "PET_COMFORT_CONTROL"
