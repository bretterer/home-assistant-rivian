"""Unit tests for long-term statistics writing, rewriting and clearing."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.rivian import statistics as statistics_mod
from custom_components.rivian.drive_models import DriveRecord

TEST_VIN = "7PDSGABA8NN000000"
UTC = timezone.utc


def _hour(y: int, m: int, d: int, h: int) -> datetime:
    return datetime(y, m, d, h, tzinfo=UTC)


def _drive(
    drive_id: str, start: datetime, distance_miles: float, energy_kwh: float
) -> DriveRecord:
    return DriveRecord(
        vin=TEST_VIN,
        drive_id=drive_id,
        start_time=start.isoformat(),
        end_time=start.isoformat(),
        distance_miles=distance_miles,
        duration_seconds=300.0,
        start_soc=80.0,
        end_soc=75.0,
        battery_capacity_kwh=135.0,
        energy_kwh=energy_kwh,
    )


class _FakeRecorderInstance:
    """Runs the target inline; records async_clear_statistics calls."""

    def __init__(self) -> None:
        self.cleared: list[list[str]] = []

    async def async_add_executor_job(self, target: Any, *args: Any) -> Any:
        return target(*args)

    def async_clear_statistics(self, statistic_ids: list[str]) -> None:
        self.cleared.append(list(statistic_ids))


class _FakeStore:
    """Minimal stand-in for DriveStore: just .vin and .async_drives_since()."""

    def __init__(self, vin: str, drives: list[DriveRecord]) -> None:
        self.vin = vin
        self._drives = drives

    async def async_drives_since(
        self, from_ts: float, now_ts: float | None = None
    ) -> list[DriveRecord]:
        return self._drives


@pytest.fixture
def hass_with_recorder(mock_hass: Any) -> Any:
    """A mock hass with "recorder" loaded, so statistics.py's guard doesn't skip."""
    mock_hass.config.components = ["recorder"]
    return mock_hass


@pytest.fixture
def fake_recorder(monkeypatch: pytest.MonkeyPatch) -> _FakeRecorderInstance:
    """Patch recorder.get_instance() to return a controllable fake instance.

    Also forces the pre-`mean_type` metadata branch: in this test environment
    `homeassistant.components.recorder.models` is a stub module, so the
    defensive `StatisticMeanType` import in statistics.py silently succeeds
    with a mock class that has no real enum members.
    """
    instance = _FakeRecorderInstance()
    monkeypatch.setattr(
        statistics_mod.recorder, "get_instance", MagicMock(return_value=instance)
    )
    monkeypatch.setattr(statistics_mod, "_HAS_STATISTIC_MEAN_TYPE", False)
    return instance


class TestAsyncRewriteStatistics:
    """async_rewrite_statistics: seeding, hour coverage, and the earlier-hours guarantee."""

    async def test_seeds_from_prior_row_and_leaves_earlier_hours_alone(
        self,
        hass_with_recorder: Any,
        fake_recorder: _FakeRecorderInstance,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ids = statistics_mod._stat_ids(TEST_VIN)
        hour0 = _hour(2026, 2, 1, 8)  # untouched, before the rewrite range
        hour1 = _hour(2026, 2, 2, 8)  # still has a remaining drive
        hour2 = _hour(2026, 2, 3, 8)  # had a row, now has no drives left

        all_rows: dict[str, list[dict[str, Any]]] = {
            ids.energy: [
                {"start": hour0, "sum": 10.0},
                {"start": hour1, "sum": 12.0},
                {"start": hour2, "sum": 12.0},
            ],
            ids.distance: [
                {"start": hour0, "sum": 100.0},
                {"start": hour1, "sum": 105.0},
                {"start": hour2, "sum": 105.0},
            ],
        }

        def fake_statistics_during_period(
            _hass: Any,
            start_time: datetime | None,
            end_time: datetime | None,
            statistic_ids: set[str],
            _period: str,
            _units: Any,
            _types: Any,
        ) -> dict[str, list[dict[str, Any]]]:
            result: dict[str, list[dict[str, Any]]] = {}
            for sid in statistic_ids:
                rows = all_rows.get(sid, [])
                result[sid] = [
                    r
                    for r in rows
                    if (start_time is None or r["start"] >= start_time)
                    and (end_time is None or r["start"] < end_time)
                ]
            return result

        monkeypatch.setattr(
            statistics_mod, "statistics_during_period", fake_statistics_during_period
        )
        written: dict[str, list[dict[str, Any]]] = {}

        def fake_add_external_statistics(_hass: Any, metadata: Any, rows: Any) -> None:
            written[metadata["statistic_id"]] = list(rows)

        monkeypatch.setattr(
            statistics_mod,
            "async_add_external_statistics",
            fake_add_external_statistics,
        )

        store = _FakeStore(TEST_VIN, [_drive("remaining", hour1, 5.0, 2.0)])
        await statistics_mod.async_rewrite_statistics(
            hass_with_recorder, store, hour1.timestamp()
        )

        energy_rows = written[ids.energy]
        distance_rows = written[ids.distance]
        efficiency_rows = written[ids.efficiency]

        # Only hour1 and hour2 were rewritten -- hour0 never appears.
        assert [r["start"] for r in energy_rows] == [hour1, hour2]

        # hour1 seeds from hour0's sum (10.0) plus the remaining drive's 2 kWh.
        assert energy_rows[0]["sum"] == pytest.approx(12.0)
        assert distance_rows[0]["sum"] == pytest.approx(105.0)
        assert efficiency_rows[0]["mean"] == pytest.approx(2.5)

        # hour2 lost its only drive: the running sum carries forward unchanged,
        # and the mean is left empty (a chart gap, not a false 0).
        assert energy_rows[1]["sum"] == pytest.approx(12.0)
        assert distance_rows[1]["sum"] == pytest.approx(105.0)
        assert efficiency_rows[1]["mean"] is None

    async def test_skips_when_recorder_not_loaded(self, mock_hass: Any) -> None:
        mock_hass.config.components = []
        store = _FakeStore(TEST_VIN, [])
        # Must return cleanly without touching recorder.get_instance at all.
        await statistics_mod.async_rewrite_statistics(mock_hass, store, 0.0)


class TestAsyncClearStatistics:
    """async_clear_statistics: clears the VIN's four stat ids, or skips without recorder."""

    def test_clears_the_vins_four_stat_ids(
        self, hass_with_recorder: Any, fake_recorder: _FakeRecorderInstance
    ) -> None:
        statistics_mod.async_clear_statistics(hass_with_recorder, TEST_VIN)
        assert len(fake_recorder.cleared) == 1
        ids = statistics_mod._stat_ids(TEST_VIN)
        assert set(fake_recorder.cleared[0]) == set(ids.as_tuple())

    def test_skips_when_recorder_not_loaded(self, mock_hass: Any) -> None:
        mock_hass.config.components = []
        # Must not raise, and must not touch recorder.get_instance.
        statistics_mod.async_clear_statistics(mock_hass, TEST_VIN)
