"""Tests for the defrost/defog switch."""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.rivian import switch

SWITCH = next(d for d in switch.SWITCHES if d.key == "defrost_defog")


def test_switch_follows_status() -> None:
    """Any defrost or defog state is on; off and no data are off."""
    coor = MagicMock()
    for status, on in (
        ("off", False),
        ("defog", True),
        ("defrost", True),
        ("defog_defrost", True),
        (None, False),
    ):
        coor.get.side_effect = {"defrostDefogStatus": status}.get
        assert SWITCH.is_on(coor) is on
