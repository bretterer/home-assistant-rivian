"""Unit tests for road_snap.py: gap detection, Overpass parsing, and snapping."""

from __future__ import annotations

import math
from typing import Any

import pytest

from custom_components.rivian import road_snap
from custom_components.rivian.drive_track import DriveTrack, TrackPoint, haversine_m

REF_LAT: float = 40.0
_DEG_TO_M: float = 111_320.0


def _m_to_lat(m: float) -> float:
    return m / _DEG_TO_M


def _m_to_lon(m: float, lat_deg: float = REF_LAT) -> float:
    return m / (_DEG_TO_M * math.cos(math.radians(lat_deg)))


# -- find_gaps -----------------------------------------------------------------


class TestFindGaps:
    def test_finds_a_real_gap_and_excludes_small_jumps_and_glitches(self) -> None:
        track = DriveTrack()
        # Normal small hops.
        track.append(TrackPoint(t=0.0, lat=REF_LAT, lon=-105.0))
        track.append(
            TrackPoint(t=10.0, lat=REF_LAT + _m_to_lat(50), lon=-105.0)
        )  # ~50 m / 10 s: not a gap.
        # A real gap: far and slow.
        far_lat = REF_LAT + _m_to_lat(500)
        track.append(TrackPoint(t=100.0, lat=far_lat, lon=-105.0))  # +500m over 90s
        # A GPS glitch: far and fast (jumps back afterwards).
        track.append(
            TrackPoint(t=101.0, lat=far_lat + _m_to_lat(6000), lon=-105.0)
        )  # 6000m in 1s: glitch
        track.append(TrackPoint(t=110.0, lat=far_lat + _m_to_lat(10), lon=-105.0))

        gaps = road_snap.find_gaps(track)
        assert len(gaps) == 1
        gap = gaps[0]
        assert gap.start_index == 1
        assert gap.end_index == 2
        assert gap.distance_m > road_snap.GAP_MIN_DISTANCE_M
        assert gap.duration_s > road_snap.GAP_MIN_SECONDS

    def test_empty_and_single_point_tracks_have_no_gaps(self) -> None:
        assert road_snap.find_gaps(DriveTrack()) == []
        track = DriveTrack()
        track.append(TrackPoint(t=0.0, lat=REF_LAT, lon=-105.0))
        assert road_snap.find_gaps(track) == []


# -- bbox helpers ----------------------------------------------------------------


class TestBboxHelpers:
    def _make_gap(
        self, distance_m: float = 500.0, duration_s: float = 40.0
    ) -> road_snap.Gap:
        start = TrackPoint(t=1000.0, lat=REF_LAT, lon=-105.0)
        end = TrackPoint(
            t=1000.0 + duration_s, lat=REF_LAT + _m_to_lat(distance_m), lon=-105.0
        )
        return road_snap.Gap(0, 1, start, end, distance_m, duration_s)

    def test_gap_bbox_contains_both_endpoints_with_padding(self) -> None:
        gap = self._make_gap()
        south, west, north, east = road_snap.gap_bbox(gap)
        assert south < gap.start.lat < north
        assert south < gap.end.lat < north
        assert west < gap.start.lon < east

    def test_bbox_area_matches_expected_rectangle(self) -> None:
        # A bbox 0.01 deg tall by 0.01 deg wide near lat 40.
        bbox = (40.0, -105.0, 40.01, -104.99)
        area = road_snap.bbox_area_m2(bbox)
        height_m = 0.01 * _DEG_TO_M
        width_m = 0.01 * _DEG_TO_M * math.cos(math.radians(40.005))
        assert area == pytest.approx(height_m * width_m, rel=0.01)

    def test_bbox_too_large_exceeds_cap(self) -> None:
        # ~10km x 10km >> 25 km^2 cap.
        bbox = (40.0, -105.0, 40.09, -104.88)
        assert road_snap.bbox_area_m2(bbox) > road_snap.MAX_BBOX_AREA_M2

    def test_bbox_key_reuses_across_nearby_gaps(self) -> None:
        gap_a = self._make_gap()
        gap_b = self._make_gap(distance_m=520.0)
        key_a = road_snap.bbox_key(road_snap.gap_bbox(gap_a))
        key_b = road_snap.bbox_key(road_snap.gap_bbox(gap_b))
        assert key_a == key_b

    def test_bbox_key_differs_far_away(self) -> None:
        gap_a = self._make_gap()
        far_start = TrackPoint(t=1000.0, lat=45.0, lon=-100.0)
        far_end = TrackPoint(t=1040.0, lat=45.01, lon=-100.0)
        gap_far = road_snap.Gap(0, 1, far_start, far_end, 500.0, 40.0)
        key_a = road_snap.bbox_key(road_snap.gap_bbox(gap_a))
        key_far = road_snap.bbox_key(road_snap.gap_bbox(gap_far))
        assert key_a != key_far


# -- Overpass parsing --------------------------------------------------------------


class TestParseOverpass:
    def test_parses_ways_and_ignores_nodes_and_short_ways(self) -> None:
        data = {
            "elements": [
                {"type": "node", "lat": 1.0, "lon": 2.0},
                {
                    "type": "way",
                    "tags": {"highway": "residential", "oneway": "yes"},
                    "geometry": [
                        {"lat": 40.0, "lon": -105.0},
                        {"lat": 40.001, "lon": -105.0},
                    ],
                },
                {
                    "type": "way",
                    "tags": {"highway": "residential"},
                    "geometry": [{"lat": 41.0, "lon": -106.0}],  # too short
                },
            ]
        }
        ways = road_snap.parse_overpass(data)
        assert len(ways) == 1
        assert ways[0].oneway is True
        assert ways[0].nodes == [(40.0, -105.0), (40.001, -105.0)]

    def test_build_overpass_query_excludes_non_drivable_highways(self) -> None:
        query = road_snap.build_overpass_query((40.0, -105.0, 40.01, -104.99))
        assert "footway" in query
        assert "parking_aisle" in query

    def test_ways_json_roundtrip(self) -> None:
        ways = [
            road_snap.Way(nodes=[(40.0, -105.0), (40.001, -105.0)], oneway=True),
            road_snap.Way(nodes=[(41.0, -106.0), (41.001, -106.0)], oneway=False),
        ]
        restored = road_snap.ways_from_json(road_snap.ways_to_json(ways))
        assert len(restored) == 2
        assert restored[0].oneway is True
        assert restored[1].oneway is False


# -- snap_gap: synthetic grid road network -----------------------------------------


def _corner_ways(
    oneway: bool = False,
) -> tuple[list[road_snap.Way], tuple, tuple, tuple]:
    """Return an L-shaped road (A -east-> B -north-> C) plus its 3 nodes."""
    node_a = (REF_LAT, -105.0)
    node_b = (REF_LAT, -105.0 + _m_to_lon(400.0))
    node_c = (node_b[0] + _m_to_lat(300.0), node_b[1])
    way = road_snap.Way(nodes=[node_a, node_b, node_c], oneway=oneway)
    return [way], node_a, node_b, node_c


def _gap_near(
    point_a: tuple[float, float],
    point_b: tuple[float, float],
    duration_s: float,
    offset_m: float = 10.0,
) -> road_snap.Gap:
    """Build a Gap whose endpoints sit `offset_m` off the given road points."""
    start = TrackPoint(
        t=1000.0, lat=point_a[0] + _m_to_lat(offset_m), lon=point_a[1], alt_m=100.0
    )
    end = TrackPoint(
        t=1000.0 + duration_s,
        lat=point_b[0],
        lon=point_b[1] + _m_to_lon(offset_m),
        alt_m=130.0,
    )
    distance_m = haversine_m(start.lat, start.lon, end.lat, end.lon)
    return road_snap.Gap(0, 1, start, end, distance_m, duration_s)


class TestSnapGap:
    def test_fills_a_gap_along_an_l_shaped_road(self) -> None:
        ways, node_a, _node_b, node_c = _corner_ways()
        gap = _gap_near(node_a, node_c, duration_s=40.0)

        points = road_snap.snap_gap(gap, ways)

        assert points
        # Times strictly between the gap's own endpoints, in order.
        times = [p.t for p in points]
        assert times == sorted(times)
        assert all(gap.start.t < t < gap.end.t for t in times)
        # Altitude interpolated linearly between the gap's endpoints.
        assert all(100.0 <= p.alt_m <= 130.0 for p in points)
        # soc/odo are never filled in.
        assert all(p.soc is None and p.odo_m is None for p in points)
        # Roughly one point every RESAMPLE_STEP_M along a ~700m path.
        assert 8 <= len(points) <= 16

    def test_no_roads_returns_none(self) -> None:
        _ways, node_a, _node_b, node_c = _corner_ways()
        gap = _gap_near(node_a, node_c, duration_s=40.0)
        assert road_snap.snap_gap(gap, []) is None

    def test_snap_distance_guard_rejects_a_far_endpoint(self) -> None:
        ways, node_a, _node_b, node_c = _corner_ways()
        # 200 m off the road, well past SNAP_MAX_DISTANCE_M.
        gap = _gap_near(node_a, node_c, duration_s=40.0, offset_m=200.0)
        assert road_snap.snap_gap(gap, ways) is None

    def test_detour_ratio_guard_rejects_an_excessive_detour(self) -> None:
        # A long doglegged road whose path is far longer than the straight
        # line between the (nearby) gap endpoints.
        node_a = (REF_LAT, -105.0)
        node_b = (REF_LAT, -105.0 + _m_to_lon(3000.0))
        node_c = (node_a[0] + _m_to_lat(5.0), node_a[1] + _m_to_lon(5.0))
        way = road_snap.Way(nodes=[node_a, node_b, node_c], oneway=False)
        gap = _gap_near(node_a, node_c, duration_s=200.0)
        assert road_snap.snap_gap(gap, [way]) is None

    def test_implied_speed_guard_rejects_too_fast_a_path(self) -> None:
        ways, node_a, _node_b, node_c = _corner_ways()
        # Same ~700m path but only 1 second of gap duration: absurd speed.
        gap = _gap_near(node_a, node_c, duration_s=1.0)
        assert road_snap.snap_gap(gap, ways) is None

    def test_oneway_respected_forward_direction_succeeds(self) -> None:
        ways, node_a, _node_b, node_c = _corner_ways(oneway=True)
        gap = _gap_near(node_a, node_c, duration_s=40.0)
        assert road_snap.snap_gap(gap, ways) is not None

    def test_oneway_respected_reverse_direction_fails(self) -> None:
        ways, node_a, _node_b, node_c = _corner_ways(oneway=True)
        # Ask for the reverse direction (C -> A): no edges run that way.
        gap = _gap_near(node_c, node_a, duration_s=40.0)
        assert road_snap.snap_gap(gap, ways) is None


class TestAsyncFetchRoads:
    """async_fetch_roads: success, HTTP-error, and network-error paths."""

    class _FakeResponse:
        def __init__(self, status: int, payload: dict[str, Any]) -> None:
            self.status = status
            self._payload = payload

        async def json(self, content_type: Any = None) -> dict[str, Any]:
            return self._payload

        async def __aenter__(self) -> TestAsyncFetchRoads._FakeResponse:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

    class _FakeSession:
        def __init__(
            self, response: Any = None, raise_error: Exception | None = None
        ) -> None:
            self._response = response
            self._raise_error = raise_error

        def post(self, *args: Any, **kwargs: Any) -> Any:
            if self._raise_error is not None:
                raise self._raise_error
            return self._response

    @pytest.mark.asyncio
    async def test_success_returns_parsed_ways(self, monkeypatch: Any) -> None:
        payload = {
            "elements": [
                {
                    "type": "way",
                    "tags": {"highway": "residential"},
                    "geometry": [
                        {"lat": 40.0, "lon": -105.0},
                        {"lat": 40.001, "lon": -105.0},
                    ],
                }
            ]
        }
        session = self._FakeSession(response=self._FakeResponse(200, payload))
        monkeypatch.setattr(road_snap, "async_get_clientsession", lambda hass: session)
        road_snap._last_request_ts[0] = 0.0

        ways = await road_snap.async_fetch_roads(
            object(), (40.0, -105.0, 40.01, -104.99)
        )
        assert ways is not None
        assert len(ways) == 1

    @pytest.mark.asyncio
    async def test_non_200_status_returns_none(self, monkeypatch: Any) -> None:
        session = self._FakeSession(response=self._FakeResponse(500, {}))
        monkeypatch.setattr(road_snap, "async_get_clientsession", lambda hass: session)
        road_snap._last_request_ts[0] = 0.0

        ways = await road_snap.async_fetch_roads(
            object(), (40.0, -105.0, 40.01, -104.99)
        )
        assert ways is None

    @pytest.mark.asyncio
    async def test_network_error_returns_none(self, monkeypatch: Any) -> None:
        import aiohttp

        session = self._FakeSession(raise_error=aiohttp.ClientError("boom"))
        monkeypatch.setattr(road_snap, "async_get_clientsession", lambda hass: session)
        road_snap._last_request_ts[0] = 0.0

        ways = await road_snap.async_fetch_roads(
            object(), (40.0, -105.0, 40.01, -104.99)
        )
        assert ways is None
