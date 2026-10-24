"""Tests for the covers' opening and closing states."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.rivian import cover

DESCRIPTIONS = {
    description.key: description
    for descriptions in cover.COVERS.values()
    for description in descriptions
}


def coordinator(values: dict[str, Any]) -> MagicMock:
    coor = MagicMock()
    coor.get.side_effect = values.get
    return coor


@pytest.mark.parametrize(
    ("key", "field", "value", "opening", "closing"),
    [
        ("frunk", "closureFrunkNextAction", "opening", True, False),
        ("frunk", "closureFrunkNextAction", "closing", False, True),
        ("frunk", "closureFrunkNextAction", "open_allowed", False, False),
        ("frunk", "closureFrunkNextAction", "close_allowed", False, False),
        ("frunk", "closureFrunkNextAction", None, False, False),
        ("liftgate", "closureLiftgateNextAction", "opening", True, False),
        (
            "liftgate",
            "closureLiftgateNextAction",
            "opening_pause_not_allowed",
            True,
            False,
        ),
        ("liftgate", "closureLiftgateNextAction", "closing", False, True),
        (
            "liftgate",
            "closureLiftgateNextAction",
            "closing_pause_not_allowed",
            False,
            True,
        ),
        ("liftgate", "closureLiftgateNextAction", "close_allowed", False, False),
        ("liftgate", "closureLiftgateNextAction", None, False, False),
        ("windows", "windowsNextAction", "opening", True, False),
        ("windows", "windowsNextAction", "closing", False, True),
        # "moving" doesn't say which way
        ("windows", "windowsNextAction", "moving", False, False),
        ("windows", "windowsNextAction", "close_allowed", False, False),
        ("windows", "windowsNextAction", None, False, False),
    ],
)
def test_motion_follows_next_action(
    key: str, field: str, value: str | None, opening: bool, closing: bool
) -> None:
    description = DESCRIPTIONS[key]
    coor = coordinator({field: value})
    assert description.is_opening(coor) is opening
    assert description.is_closing(coor) is closing
