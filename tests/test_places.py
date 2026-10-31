"""Unit tests for the pure-stdlib places.py clustering/assignment logic."""

from __future__ import annotations

from custom_components.rivian.places import (
    AutoPlaceState,
    Cluster,
    DriveEndpointInput,
    Endpoint,
    ExistingPlace,
    assign,
    cluster_endpoints,
    drive_endpoints,
    place_label,
)

HOME = (37.0, -122.0)
WORK = (37.5, -122.5)


def _row(
    drive_id: str,
    start: tuple[float, float] | None,
    end: tuple[float, float] | None,
    start_ts: float,
    end_ts: float,
) -> DriveEndpointInput:
    return DriveEndpointInput(
        drive_id=drive_id,
        start_lat=start[0] if start else None,
        start_lon=start[1] if start else None,
        start_ts=start_ts,
        end_lat=end[0] if end else None,
        end_lon=end[1] if end else None,
        end_ts=end_ts,
    )


class TestDriveEndpoints:
    """drive_endpoints(): the parked-position rule and basic point extraction."""

    def test_simple_round_trip_produces_four_endpoints(self) -> None:
        rows = [
            _row("d1", HOME, WORK, 0.0, 100.0),
            _row("d2", WORK, HOME, 200.0, 300.0),
        ]
        endpoints = drive_endpoints(rows)
        assert [(e.kind, e.drive_id) for e in endpoints] == [
            ("start", "d1"),
            ("end", "d1"),
            ("start", "d2"),
            ("end", "d2"),
        ]
        # d2's start uses its own recorded start (== previous drive's end).
        assert endpoints[2].lat == WORK[0]
        assert endpoints[2].lon == WORK[1]

    def test_start_far_from_previous_end_uses_own_start(self) -> None:
        """A start > DAY_GAP_MAX_M from the previous drive's end stays as recorded."""
        far_start = (38.0, -123.0)  # far from WORK
        rows = [
            _row("d1", HOME, WORK, 0.0, 100.0),
            _row("d2", far_start, HOME, 200.0, 300.0),
        ]
        endpoints = drive_endpoints(rows)
        start_d2 = next(
            e for e in endpoints if e.drive_id == "d2" and e.kind == "start"
        )
        assert start_d2.lat == far_start[0]
        assert start_d2.lon == far_start[1]

    def test_start_near_previous_end_snaps_to_it(self) -> None:
        """A start within DAY_GAP_MAX_M of the previous drive's end snaps to it.

        This is the Sep 24 regression the plan references: the first live fix
        can arrive ~1.2 km down the road from where the car actually parked.
        """
        near_start = (37.5009, -122.5009)  # ~130 m from WORK
        rows = [
            _row("d1", HOME, WORK, 0.0, 100.0),
            _row("d2", near_start, HOME, 200.0, 300.0),
        ]
        endpoints = drive_endpoints(rows)
        start_d2 = next(
            e for e in endpoints if e.drive_id == "d2" and e.kind == "start"
        )
        assert start_d2.lat == WORK[0]
        assert start_d2.lon == WORK[1]

    def test_missing_end_coords_skip_endpoint_and_dont_poison_next_start(self) -> None:
        """A drive with no end coords contributes no end endpoint; the next
        drive's start-rule looks further back for a previous end, not at it."""
        rows = [
            _row("d1", HOME, WORK, 0.0, 100.0),
            _row("d2", WORK, None, 200.0, 300.0),
            _row("d3", (37.5005, -122.5005), HOME, 400.0, 500.0),
        ]
        endpoints = drive_endpoints(rows)
        kinds = [(e.kind, e.drive_id) for e in endpoints]
        assert ("end", "d2") not in kinds
        start_d3 = next(
            e for e in endpoints if e.drive_id == "d3" and e.kind == "start"
        )
        # Still snaps to d1's end (WORK), the most recent *known* end.
        assert start_d3.lat == WORK[0]
        assert start_d3.lon == WORK[1]

    def test_missing_start_coords_skip_endpoint(self) -> None:
        rows = [_row("d1", None, WORK, 0.0, 100.0)]
        endpoints = drive_endpoints(rows)
        assert len(endpoints) == 1
        assert endpoints[0].kind == "end"


def _endpoint(
    lat: float, lon: float, t: float, kind: str = "end", drive_id: str = "d"
) -> Endpoint:
    return Endpoint(lat=lat, lon=lon, t=t, kind=kind, drive_id=drive_id)


class TestClusterEndpoints:
    """cluster_endpoints(): threshold, median centroid, zone priority, stability."""

    def test_below_min_visits_is_dropped(self) -> None:
        endpoints = [_endpoint(HOME[0], HOME[1], float(i)) for i in range(2)]
        clusters = cluster_endpoints(endpoints, [], [])
        assert clusters == []

    def test_two_stops_are_two_visits_not_four(self) -> None:
        # Each stop is an arrival plus the next departure from the same spot.
        endpoints = [
            _endpoint(HOME[0], HOME[1], float(i), kind="end" if i % 2 == 0 else "start")
            for i in range(4)
        ]
        assert cluster_endpoints(endpoints, [], []) == []

    def test_at_min_visits_becomes_a_cluster(self) -> None:
        endpoints = [_endpoint(HOME[0], HOME[1], float(i)) for i in range(3)]
        clusters = cluster_endpoints(endpoints, [], [])
        assert len(clusters) == 1
        assert clusters[0].visit_count == 3
        assert clusters[0].place_id is None

    def test_centroid_is_the_running_median_not_mean(self) -> None:
        """The centroid tracks the median of a tight group of points."""
        tight = [
            (37.00000, -122.00000),
            (37.00010, -122.00010),
            (37.00005, -122.00005),
        ]
        endpoints = [
            _endpoint(lat, lon, float(i)) for i, (lat, lon) in enumerate(tight)
        ]
        clusters = cluster_endpoints(endpoints, [], [])
        assert len(clusters) == 1
        lats = sorted(lat for lat, _lon in tight)
        assert clusters[0].lat == lats[1]  # median of 3 values

    def test_two_separate_spots_form_two_clusters(self) -> None:
        endpoints = [
            _endpoint(HOME[0], HOME[1], 0.0),
            _endpoint(HOME[0], HOME[1], 1.0),
            _endpoint(HOME[0], HOME[1], 2.0),
            _endpoint(WORK[0], WORK[1], 3.0),
            _endpoint(WORK[0], WORK[1], 4.0),
            _endpoint(WORK[0], WORK[1], 5.0),
        ]
        clusters = cluster_endpoints(endpoints, [], [])
        assert len(clusters) == 2

    def test_zone_place_absorbs_points_without_creating_a_cluster(self) -> None:
        """Points inside a zone/user place never become an auto cluster."""
        zone = ExistingPlace(
            place_id=1, lat=HOME[0], lon=HOME[1], radius_m=150, source="zone"
        )
        endpoints = [_endpoint(HOME[0], HOME[1], float(i)) for i in range(5)]
        clusters = cluster_endpoints(endpoints, [zone], [])
        assert clusters == []

    def test_hidden_fixed_place_still_absorbs_its_points(self) -> None:
        """A hidden zone/user place still owns its points (plan's explicit requirement)."""
        hidden_zone = ExistingPlace(
            place_id=1,
            lat=HOME[0],
            lon=HOME[1],
            radius_m=150,
            source="zone",
            hidden=True,
        )
        endpoints = [_endpoint(HOME[0], HOME[1], float(i)) for i in range(5)]
        clusters = cluster_endpoints(endpoints, [hidden_zone], [])
        assert clusters == []

    def test_existing_auto_place_id_is_reused_across_a_rebuild(self) -> None:
        """A rebuild must not change an existing auto place's id."""
        existing = AutoPlaceState(
            place_id=42,
            lat=HOME[0],
            lon=HOME[1],
            radius_m=150,
            hidden=False,
            name=None,
            category=None,
            geocode_name="Some Cafe, Main St",
            geocoded_ts=123.0,
        )
        endpoints = [_endpoint(HOME[0], HOME[1], float(i)) for i in range(4)]
        clusters = cluster_endpoints(endpoints, [], [existing])
        assert len(clusters) == 1
        assert clusters[0].place_id == 42
        assert clusters[0].geocode_name == "Some Cafe, Main St"

    def test_hidden_flag_survives_a_rebuild(self) -> None:
        existing = AutoPlaceState(
            place_id=7,
            lat=HOME[0],
            lon=HOME[1],
            radius_m=150,
            hidden=True,
            name=None,
            category=None,
            geocode_name=None,
            geocoded_ts=None,
        )
        endpoints = [_endpoint(HOME[0], HOME[1], float(i)) for i in range(4)]
        clusters = cluster_endpoints(endpoints, [], [existing])
        assert clusters[0].hidden is True

    def test_new_cluster_does_not_reuse_an_unrelated_existing_auto_place(self) -> None:
        existing = AutoPlaceState(
            place_id=9,
            lat=WORK[0],
            lon=WORK[1],
            radius_m=150,
            hidden=False,
            name=None,
            category=None,
            geocode_name=None,
            geocoded_ts=None,
        )
        endpoints = [_endpoint(HOME[0], HOME[1], float(i)) for i in range(4)]
        clusters = cluster_endpoints(endpoints, [], [existing])
        assert len(clusters) == 1
        assert clusters[0].place_id is None


class TestAssign:
    """assign(): nearest non-hidden containing place; hidden places label nothing."""

    def test_assigns_to_nearest_containing_place(self) -> None:
        home_place = ExistingPlace(
            place_id=1, lat=HOME[0], lon=HOME[1], radius_m=150, source="zone"
        )
        work_place = ExistingPlace(
            place_id=2, lat=WORK[0], lon=WORK[1], radius_m=150, source="auto"
        )
        assert assign(HOME, [home_place, work_place]) == 1
        assert assign(WORK, [home_place, work_place]) == 2

    def test_point_outside_every_radius_is_unassigned(self) -> None:
        home_place = ExistingPlace(
            place_id=1, lat=HOME[0], lon=HOME[1], radius_m=150, source="zone"
        )
        far_away = (10.0, 10.0)
        assert assign(far_away, [home_place]) is None

    def test_hidden_place_leaves_the_point_unlabeled(self) -> None:
        hidden_place = ExistingPlace(
            place_id=1,
            lat=HOME[0],
            lon=HOME[1],
            radius_m=150,
            source="auto",
            hidden=True,
        )
        assert assign(HOME, [hidden_place]) is None

    def test_hidden_place_does_not_shadow_a_visible_one(self) -> None:
        hidden_place = ExistingPlace(
            place_id=1,
            lat=HOME[0],
            lon=HOME[1],
            radius_m=500,
            source="auto",
            hidden=True,
        )
        visible_place = ExistingPlace(
            place_id=2,
            lat=HOME[0],
            lon=HOME[1],
            radius_m=150,
            source="user",
            hidden=False,
        )
        assert assign(HOME, [hidden_place, visible_place]) == 2


class TestPlaceLabel:
    def test_name_wins_over_geocode_name(self) -> None:
        assert place_label("Home", "123 Main St", 1) == "Home"

    def test_geocode_name_used_when_no_name(self) -> None:
        assert place_label(None, "123 Main St", 1) == "123 Main St"

    def test_falls_back_to_numbered_placeholder(self) -> None:
        assert place_label(None, None, 7) == "Place #7"


class TestMergeScenario:
    """A merge (DB-level) is covered in test_analytics_db.py; here we only check
    that cluster/assign logic doesn't itself need to know about merges -- a
    merged-away place simply stops appearing in `places` passed to assign()."""

    def test_assign_ignores_a_place_not_in_the_list(self) -> None:
        # Simulates a merge: place 1 was deleted, so it's no longer passed in.
        remaining = ExistingPlace(
            place_id=2, lat=HOME[0], lon=HOME[1], radius_m=150, source="user"
        )
        assert assign(HOME, [remaining]) == 2


def test_cluster_result_dataclass_defaults() -> None:
    c = Cluster(lat=1.0, lon=2.0, visit_count=3)
    assert c.place_id is None
    assert c.hidden is False
    assert c.name is None
