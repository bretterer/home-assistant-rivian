"""Unit tests for the pure-stdlib routes.py grouping/variant/stats logic."""

from __future__ import annotations

from custom_components.rivian.routes import (
    MIN_ROUTE_DRIVES,
    RouteDriveInput,
    build_routes,
    coarsen_cells,
    compute_route_stats,
    jaccard,
    route_label,
    split_variants,
)

HOME = 1
WORK = 2
GYM = 3


def _drive(
    drive_id: str,
    start: int | None,
    end: int | None,
    sort_ts: float,
    duration_seconds: float | None = 600.0,
    moving_seconds: float | None = None,
    distance_miles: float | None = 10.0,
    energy_kwh: float | None = 3.0,
    temp_f: float | None = 60.0,
    cells: frozenset[int] | None = None,
    vin: str = "A",
) -> RouteDriveInput:
    return RouteDriveInput(
        drive_id=drive_id,
        start_place_id=start,
        end_place_id=end,
        sort_ts=sort_ts,
        duration_seconds=duration_seconds,
        moving_seconds=moving_seconds,
        distance_miles=distance_miles,
        energy_kwh=energy_kwh,
        temp_f=temp_f,
        cells=cells,
        vin=vin,
    )


ROUTE_A_CELLS = frozenset({1, 2, 3, 4, 5})
ROUTE_B_CELLS = frozenset({101, 102, 103, 104, 105})


class TestPairGrouping:
    """build_routes(): the (start, end) pair threshold and basic grouping."""

    def test_pair_below_threshold_is_not_a_route(self) -> None:
        drives = [
            _drive("d1", HOME, WORK, 0.0),
            _drive("d2", HOME, WORK, 100.0),
        ]
        assert build_routes(drives) == []

    def test_pair_at_threshold_becomes_a_route(self) -> None:
        drives = [
            _drive("d1", HOME, WORK, 0.0),
            _drive("d2", HOME, WORK, 100.0),
            _drive("d3", HOME, WORK, 200.0),
        ]
        routes = build_routes(drives)
        assert len(routes) == 1
        assert routes[0].start_place_id == HOME
        assert routes[0].end_place_id == WORK
        assert routes[0].drive_ids == ["A|d1", "A|d2", "A|d3"]
        assert routes[0].stats.count == 3

    def test_same_start_and_end_is_never_a_route(self) -> None:
        drives = [_drive(f"d{i}", HOME, HOME, float(i)) for i in range(5)]
        assert build_routes(drives) == []

    def test_missing_place_is_never_a_route(self) -> None:
        drives = [
            _drive("d1", HOME, None, 0.0),
            _drive("d2", None, WORK, 100.0),
            _drive("d3", HOME, WORK, 200.0),
        ]
        assert build_routes(drives) == []

    def test_reverse_pair_is_a_different_route(self) -> None:
        drives = [
            _drive("h1", HOME, WORK, 0.0),
            _drive("h2", HOME, WORK, 100.0),
            _drive("h3", HOME, WORK, 200.0),
            _drive("w1", WORK, HOME, 300.0),
            _drive("w2", WORK, HOME, 400.0),
            _drive("w3", WORK, HOME, 500.0),
        ]
        routes = build_routes(drives)
        assert len(routes) == 2
        pairs = {(r.start_place_id, r.end_place_id) for r in routes}
        assert pairs == {(HOME, WORK), (WORK, HOME)}

    def test_multiple_independent_pairs(self) -> None:
        drives = [_drive(f"hw{i}", HOME, WORK, float(i)) for i in range(4)]
        drives += [_drive(f"hg{i}", HOME, GYM, float(i)) for i in range(3)]
        drives += [_drive(f"wg{i}", WORK, GYM, float(i)) for i in range(2)]  # below min
        routes = build_routes(drives)
        pairs = {(r.start_place_id, r.end_place_id) for r in routes}
        assert pairs == {(HOME, WORK), (HOME, GYM)}


class TestJaccardAndCoarsen:
    def test_jaccard_identical_sets(self) -> None:
        assert jaccard(ROUTE_A_CELLS, ROUTE_A_CELLS) == 1.0

    def test_jaccard_disjoint_sets(self) -> None:
        assert jaccard(ROUTE_A_CELLS, ROUTE_B_CELLS) == 0.0

    def test_jaccard_partial_overlap(self) -> None:
        a = frozenset({1, 2, 3, 4})
        b = frozenset({1, 2, 5, 6})
        # intersection 2, union 6 -> 1/3
        assert round(jaccard(a, b), 4) == round(2 / 6, 4)

    def test_coarsen_cells_shrinks_the_set(self) -> None:
        # Cells within the same 2**3 x 2**3 block coarsen to one cell.
        base = [(0 << 21) | 0, (1 << 21) | 1, (7 << 21) | 7]
        coarsened = coarsen_cells(base)
        assert len(coarsened) == 1


class TestVariantSplit:
    """split_variants(): greedy Jaccard grouping, untracked join, tiny-variant folding."""

    def test_single_path_all_one_variant(self) -> None:
        drives = [
            _drive(f"d{i}", HOME, WORK, float(i), cells=ROUTE_A_CELLS) for i in range(5)
        ]
        variants = split_variants(drives)
        assert len(variants) == 1
        assert len(variants[0]) == 5

    def test_two_clearly_different_paths_split(self) -> None:
        drives = [
            _drive("a1", HOME, WORK, 0.0, cells=ROUTE_A_CELLS),
            _drive("a2", HOME, WORK, 1.0, cells=ROUTE_A_CELLS),
            _drive("a3", HOME, WORK, 2.0, cells=ROUTE_A_CELLS),
            _drive("b1", HOME, WORK, 3.0, cells=ROUTE_B_CELLS),
            _drive("b2", HOME, WORK, 4.0, cells=ROUTE_B_CELLS),
            _drive("b3", HOME, WORK, 5.0, cells=ROUTE_B_CELLS),
        ]
        variants = split_variants(drives)
        assert len(variants) == 2
        ids_by_variant = [[d.drive_id for d in v] for v in variants]
        assert ["a1", "a2", "a3"] in ids_by_variant
        assert ["b1", "b2", "b3"] in ids_by_variant

    def test_similar_paths_above_threshold_merge(self) -> None:
        # 4/5 cells shared -> jaccard 4/6 ~= 0.667 >= 0.6, should merge.
        near_a = frozenset({1, 2, 3, 4, 5})
        near_b = frozenset({1, 2, 3, 4, 6})
        drives = [
            _drive("a1", HOME, WORK, 0.0, cells=near_a),
            _drive("a2", HOME, WORK, 1.0, cells=near_a),
            _drive("b1", HOME, WORK, 2.0, cells=near_b),
        ]
        variants = split_variants(drives)
        assert len(variants) == 1
        assert len(variants[0]) == 3

    def test_untracked_drives_join_largest_variant(self) -> None:
        drives = [
            _drive("a1", HOME, WORK, 0.0, cells=ROUTE_A_CELLS),
            _drive("a2", HOME, WORK, 1.0, cells=ROUTE_A_CELLS),
            _drive("a3", HOME, WORK, 2.0, cells=ROUTE_A_CELLS),
            _drive("b1", HOME, WORK, 3.0, cells=ROUTE_B_CELLS),
            _drive("b2", HOME, WORK, 4.0, cells=ROUTE_B_CELLS),
            _drive("b3", HOME, WORK, 5.0, cells=ROUTE_B_CELLS),
            _drive("no_track", HOME, WORK, 6.0, cells=None),
        ]
        variants = split_variants(drives)
        # Both tracked variants meet MIN_ROUTE_DRIVES, so neither folds; the
        # untracked drive should join whichever was the largest (tie here ->
        # the first-created, "a").
        assert len(variants) == 2
        sizes = sorted(len(v) for v in variants)
        assert sizes == [3, 4]

    def test_tiny_variant_folds_into_largest(self) -> None:
        drives = [
            _drive(f"a{i}", HOME, WORK, float(i), cells=ROUTE_A_CELLS) for i in range(4)
        ]
        # Only 2 drives on route B -- below MIN_ROUTE_DRIVES, must fold back.
        drives += [
            _drive("b1", HOME, WORK, 10.0, cells=ROUTE_B_CELLS),
            _drive("b2", HOME, WORK, 11.0, cells=ROUTE_B_CELLS),
        ]
        variants = split_variants(drives)
        assert len(variants) == 1
        assert len(variants[0]) == 6

    def test_no_tracked_drives_at_all_is_one_variant(self) -> None:
        drives = [_drive(f"d{i}", HOME, WORK, float(i), cells=None) for i in range(3)]
        variants = split_variants(drives)
        assert len(variants) == 1
        assert len(variants[0]) == 3

    def test_build_routes_assigns_sequential_variant_numbers(self) -> None:
        drives = [
            _drive("a1", HOME, WORK, 0.0, cells=ROUTE_A_CELLS),
            _drive("a2", HOME, WORK, 1.0, cells=ROUTE_A_CELLS),
            _drive("a3", HOME, WORK, 2.0, cells=ROUTE_A_CELLS),
            _drive("b1", HOME, WORK, 3.0, cells=ROUTE_B_CELLS),
            _drive("b2", HOME, WORK, 4.0, cells=ROUTE_B_CELLS),
            _drive("b3", HOME, WORK, 5.0, cells=ROUTE_B_CELLS),
        ]
        routes = build_routes(drives)
        assert len(routes) == 2
        variants = sorted(r.variant for r in routes)
        assert variants == [1, 2]
        for r in routes:
            assert r.variant_count == 2


class TestRouteStats:
    """compute_route_stats(): fastest/avg/slowest, outlier exclusion, ranks."""

    def test_basic_stats(self) -> None:
        drives = [
            _drive("fast", HOME, WORK, 0.0, duration_seconds=500.0),
            _drive("mid", HOME, WORK, 1.0, duration_seconds=600.0),
            _drive("slow", HOME, WORK, 2.0, duration_seconds=700.0),
        ]
        stats = compute_route_stats(drives)
        assert stats.count == 3
        assert stats.fastest_drive_id == "A|fast"
        assert stats.fastest_seconds == 500.0
        assert stats.slowest_drive_id == "A|slow"
        assert stats.slowest_seconds == 700.0
        assert stats.avg_seconds == 600.0
        assert stats.drive_stats["A|fast"].rank == 1
        assert stats.drive_stats["A|mid"].rank == 2
        assert stats.drive_stats["A|slow"].rank == 3
        assert stats.drive_stats["A|mid"].vs_avg_pct == 0.0
        assert not any(d.outlier for d in stats.drive_stats.values())

    def test_outlier_excluded_from_stats_but_still_listed(self) -> None:
        drives = [
            _drive("d1", HOME, WORK, 0.0, duration_seconds=600.0),
            _drive("d2", HOME, WORK, 1.0, duration_seconds=620.0),
            _drive("d3", HOME, WORK, 2.0, duration_seconds=610.0),
            # Median ~610-620; 3x median is well over 1800s.
            _drive("stuck", HOME, WORK, 3.0, duration_seconds=5000.0),
        ]
        stats = compute_route_stats(drives)
        assert stats.count == 4
        assert "A|stuck" in stats.drive_stats
        assert stats.drive_stats["A|stuck"].outlier is True
        assert stats.drive_stats["A|stuck"].rank is None
        assert stats.slowest_drive_id != "A|stuck"
        assert stats.fastest_drive_id != "A|stuck"

    def test_moving_seconds_ignores_nulls(self) -> None:
        drives = [
            _drive("d1", HOME, WORK, 0.0, duration_seconds=600.0, moving_seconds=550.0),
            _drive("d2", HOME, WORK, 1.0, duration_seconds=620.0, moving_seconds=None),
            _drive("d3", HOME, WORK, 2.0, duration_seconds=610.0, moving_seconds=590.0),
        ]
        stats = compute_route_stats(drives)
        assert stats.fastest_moving_seconds == 550.0
        assert stats.slowest_moving_seconds == 590.0
        assert stats.avg_moving_seconds == (550.0 + 590.0) / 2

    def test_energy_weighted_efficiency(self) -> None:
        drives = [
            _drive(
                "d1",
                HOME,
                WORK,
                0.0,
                distance_miles=10.0,
                energy_kwh=2.0,
            ),
            _drive(
                "d2",
                HOME,
                WORK,
                1.0,
                distance_miles=20.0,
                energy_kwh=10.0,
            ),
        ]
        stats = compute_route_stats(drives)
        # (10 + 20) / (2 + 10) = 2.5
        assert stats.avg_efficiency_mi_kwh == 2.5

    def test_last_ts_is_the_most_recent_drive(self) -> None:
        drives = [
            _drive("d1", HOME, WORK, 10.0),
            _drive("d2", HOME, WORK, 999.0),
            _drive("d3", HOME, WORK, 500.0),
        ]
        stats = compute_route_stats(drives)
        assert stats.last_ts == 999.0


class TestRouteLabel:
    def test_single_variant_no_suffix(self) -> None:
        assert route_label("Home", "Work", 1) == "Home → Work"

    def test_variant_above_one_adds_suffix(self) -> None:
        assert route_label("Home", "Work", 2) == "Home → Work (via 2)"


def test_min_route_drives_constant_is_three() -> None:
    assert MIN_ROUTE_DRIVES == 3
