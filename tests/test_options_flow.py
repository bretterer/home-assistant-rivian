"""Tests for the options flow's two pages."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.rivian import config_flow
from custom_components.rivian.config_flow import (
    ANALYTICS_OPTIONS_SCHEMA,
    OPTIONS_SCHEMA,
)
from custom_components.rivian.const import CONF_DRIVE_ANALYTICS

COMPONENT = Path(__file__).parent.parent / "custom_components" / "rivian"


def _keys(schema) -> set[str]:
    return {str(key) for key in schema.schema}


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({CONF_DRIVE_ANALYTICS: True}, "analytics"),
        ({CONF_DRIVE_ANALYTICS: False}, None),
        ({}, None),  # off by default
    ],
)
async def test_analytics_page_only_while_analytics_on(options, expected) -> None:
    """The analytics settings page follows the main page only when it's on."""
    assert await config_flow._analytics_next_step(options) == expected


def test_main_page_keeps_only_the_toggles() -> None:
    """The analytics settings live on their own page, not the main one."""
    assert _keys(OPTIONS_SCHEMA) == {
        "vehicle_image_style",
        "vehicle_control",
        "zone",
        "drive_analytics",
        "weather_lookups",
    }
    assert _keys(ANALYTICS_OPTIONS_SCHEMA) == {
        "analytics_all_users",
        "analytics_retention_days",
        "track_capture",
        "track_retention_days",
        "track_full_detail_days",
    }


@pytest.mark.parametrize("path", ["strings.json", "translations/en.json"])
def test_each_page_is_translated(path: str) -> None:
    """Each page's labels are under that page's step."""
    steps = json.loads((COMPONENT / path).read_text(encoding="utf-8"))["options"][
        "step"
    ]
    assert set(steps["init"]["data"]) == _keys(OPTIONS_SCHEMA)
    assert set(steps["analytics"]["data"]) == _keys(ANALYTICS_OPTIONS_SCHEMA)
    assert set(steps["analytics"]["data_description"]) <= _keys(
        ANALYTICS_OPTIONS_SCHEMA
    )
