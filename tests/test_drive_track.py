"""Unit tests for the pure-Python GPS drive track module."""

from __future__ import annotations

import json
import math
import random

import pytest

from custom_components.rivian.drive_track import (
    TRACK_FORMAT_VERSION,
    DriveTrack,
    TrackPoint,
    haversine_m,
)


def _make_realistic_track(n: int, *, start_t: float = 1_700_000_000.0) -> DriveTrack:
    """Build a curved, southward/westward track with falling alt/soc.

    Includes negative deltas in every optional column so delta-encoding is
    exercised in both directions.
    """
    track = DriveTrack()
    lat, lon = 40.0, -105.0
    alt = 1800.0
    soc = 82.0
    odo = 12_345.0
    for i in range(n):
        # Curve that heads generally south and west.
        lat -= 0.0003 + 0.00005 * math.sin(i / 7.0)
        lon -= 0.0002 + 0.00004 * math.cos(i / 11.0)
        speed = max(0.0, 20.0 + 8.0 * math.sin(i / 5.0))
        alt += math.sin(i / 9.0) * 2.0 - 0.3  # net downhill
        soc -= 0.05 + 0.01 * math.sin(i / 3.0)  # net draining
        odo += speed * 1.0
        track.append(
            TrackPoint(
                t=start_t + i * 1.0,
                lat=lat,
                lon=lon,
                speed_mps=speed,
                alt_m=alt,
                soc=max(0.0, soc),
                odo_m=odo,
            )
        )
    return track


class TestHaversine:
    def test_one_degree_latitude_is_approximately_111195_m(self) -> None:
        dist = haversine_m(0.0, 0.0, 1.0, 0.0)
        assert dist == pytest.approx(111_195.0, rel=0.005)

    def test_zero_distance_for_identical_points(self) -> None:
        assert haversine_m(37.0, -122.0, 37.0, -122.0) == 0.0

    def test_symmetry(self) -> None:
        a = haversine_m(10.0, 20.0, 11.0, 21.0)
        b = haversine_m(11.0, 21.0, 10.0, 20.0)
        assert a == pytest.approx(b)


class TestAppendAndExtend:
    def test_append_accepts_increasing_timestamps(self) -> None:
        track = DriveTrack()
        assert track.append(TrackPoint(t=1.0, lat=37.0, lon=-122.0)) is True
        assert track.append(TrackPoint(t=2.0, lat=37.001, lon=-122.001)) is True
        assert len(track) == 2

    def test_append_rejects_non_increasing_timestamp(self) -> None:
        track = DriveTrack()
        track.append(TrackPoint(t=10.0, lat=37.0, lon=-122.0))
        assert track.append(TrackPoint(t=10.0, lat=37.1, lon=-122.1)) is False
        assert track.append(TrackPoint(t=5.0, lat=37.1, lon=-122.1)) is False
        assert len(track) == 1

    @pytest.mark.parametrize(
        "lat,lon",
        [
            (None, -122.0),
            (37.0, None),
            (float("nan"), -122.0),
            (37.0, float("nan")),
            (float("inf"), -122.0),
            (37.0, float("-inf")),
            (91.0, -122.0),
            (-91.0, -122.0),
            (37.0, 181.0),
            (37.0, -181.0),
            (0.0, 0.0),
        ],
    )
    def test_append_rejects_invalid_coordinates(
        self, lat: float | None, lon: float | None
    ) -> None:
        track = DriveTrack()
        assert track.append(TrackPoint(t=1.0, lat=lat, lon=lon)) is False
        assert len(track) == 0

    def test_extend_counts_only_accepted_points(self) -> None:
        track = DriveTrack()
        points = [
            TrackPoint(t=1.0, lat=37.0, lon=-122.0),
            TrackPoint(t=0.5, lat=37.0, lon=-122.0),  # rejected: not increasing
            TrackPoint(t=2.0, lat=0.0, lon=0.0),  # rejected: invalid coordinate
            TrackPoint(t=3.0, lat=37.1, lon=-122.1),
        ]
        assert track.extend(points) == 2
        assert len(track) == 2


class TestEmptyAndSingleton:
    def test_empty_track_bbox_is_none(self) -> None:
        track = DriveTrack()
        assert track.bbox() is None
        assert track.distance_m() == 0.0
        assert len(track.simplify(5.0)) == 0
        assert len(track.preview()) == 0

    def test_empty_track_encode_decode_round_trip(self) -> None:
        track = DriveTrack()
        decoded = DriveTrack.decode(track.encode())
        assert len(decoded) == 0
        assert decoded.bbox() is None

    def test_single_point_track(self) -> None:
        track = DriveTrack([TrackPoint(t=100.0, lat=10.0, lon=20.0, speed_mps=5.0)])
        assert track.bbox() == (10.0, 20.0, 10.0, 20.0)
        assert track.distance_m() == 0.0
        assert len(track.simplify(5.0)) == 1
        assert len(track.preview()) == 1
        decoded = DriveTrack.decode(track.encode())
        assert len(decoded) == 1
        assert decoded.points[0].lat == pytest.approx(10.0, abs=1e-5)
        assert decoded.points[0].lon == pytest.approx(20.0, abs=1e-5)
        assert decoded.points[0].speed_mps == pytest.approx(5.0, abs=0.1)


class TestEncodeDecodeRoundTrip:
    def test_realistic_track_round_trips_within_precision(self) -> None:
        track = _make_realistic_track(500)
        decoded = DriveTrack.decode(track.encode())
        assert len(decoded) == len(track)
        for original, restored in zip(track.points, decoded.points):
            assert restored.t == pytest.approx(original.t, abs=0.1)
            assert restored.lat == pytest.approx(original.lat, abs=1e-5)
            assert restored.lon == pytest.approx(original.lon, abs=1e-5)
            assert restored.speed_mps == pytest.approx(original.speed_mps, abs=0.1)
            assert restored.alt_m == pytest.approx(original.alt_m, abs=0.1)
            assert restored.soc == pytest.approx(original.soc, abs=0.01)
            assert restored.odo_m == pytest.approx(original.odo_m, abs=1.0)

    def test_negative_deltas_present_and_round_trip(self) -> None:
        # The realistic track heads south/west with falling altitude and SoC,
        # so consecutive deltas in lat, lon, alt, and soc should go negative.
        track = _make_realistic_track(50)
        raw = json.loads(track.encode())
        assert any(v is not None and v < 0 for v in raw["lat"][1:])
        assert any(v is not None and v < 0 for v in raw["lon"][1:])
        assert any(v is not None and v < 0 for v in raw["alt"][1:])
        assert any(v is not None and v < 0 for v in raw["soc"][1:])
        # Round trip should still hold.
        decoded = DriveTrack.decode(track.encode())
        assert decoded.points[-1].lat == pytest.approx(track.points[-1].lat, abs=1e-5)

    def test_nulls_in_middle_of_optional_column_round_trip(self) -> None:
        points = [
            TrackPoint(t=0.0, lat=10.0, lon=20.0, speed_mps=1.0, alt_m=100.0),
            TrackPoint(t=1.0, lat=10.001, lon=20.001, speed_mps=None, alt_m=None),
            TrackPoint(t=2.0, lat=10.002, lon=20.002, speed_mps=3.0, alt_m=102.0),
        ]
        track = DriveTrack(points)
        decoded = DriveTrack.decode(track.encode())
        assert decoded.points[0].speed_mps == pytest.approx(1.0, abs=0.1)
        assert decoded.points[1].speed_mps is None
        assert decoded.points[2].speed_mps == pytest.approx(3.0, abs=0.1)
        assert decoded.points[0].alt_m == pytest.approx(100.0, abs=0.1)
        assert decoded.points[1].alt_m is None
        assert decoded.points[2].alt_m == pytest.approx(102.0, abs=0.1)

    def test_entirely_null_column_round_trips_as_none(self) -> None:
        points = [
            TrackPoint(t=0.0, lat=10.0, lon=20.0, soc=None),
            TrackPoint(t=1.0, lat=10.001, lon=20.001, soc=None),
        ]
        track = DriveTrack(points)
        raw = json.loads(track.encode())
        assert raw["soc"] is None
        decoded = DriveTrack.decode(track.encode())
        assert all(p.soc is None for p in decoded.points)

    def test_decode_accepts_str_bytes_and_dict(self) -> None:
        track = _make_realistic_track(10)
        encoded_str = track.encode()
        encoded_bytes = encoded_str.encode("utf-8")
        encoded_dict = json.loads(encoded_str)

        for payload in (encoded_str, encoded_bytes, encoded_dict):
            decoded = DriveTrack.decode(payload)
            assert len(decoded) == 10

    def test_decode_unknown_version_raises(self) -> None:
        track = _make_realistic_track(5)
        raw = json.loads(track.encode())
        raw["v"] = TRACK_FORMAT_VERSION + 1
        with pytest.raises(ValueError):
            DriveTrack.decode(raw)

    def test_decode_mismatched_column_length_raises(self) -> None:
        track = _make_realistic_track(5)
        raw = json.loads(track.encode())
        raw["lat"].pop()
        with pytest.raises(ValueError):
            DriveTrack.decode(raw)

    def test_decode_non_list_column_raises(self) -> None:
        track = _make_realistic_track(5)
        raw = json.loads(track.encode())
        raw["lon"] = "not-a-list"
        with pytest.raises(ValueError):
            DriveTrack.decode(raw)

    def test_encoded_size_of_720_point_track_is_compact(self) -> None:
        track = _make_realistic_track(720)
        encoded = track.encode()
        size = len(encoded.encode("utf-8"))
        assert size < 25_000, f"encoded 720-point track is {size} bytes"


class TestSimplify:
    def test_keeps_endpoints(self) -> None:
        track = _make_realistic_track(200)
        simplified = track.simplify(50.0)
        assert simplified.points[0] == track.points[0]
        assert simplified.points[-1] == track.points[-1]

    def test_never_increases_point_count(self) -> None:
        track = _make_realistic_track(300)
        for tolerance in (0.0, 1.0, 5.0, 50.0, 500.0):
            simplified = track.simplify(tolerance)
            assert len(simplified) <= len(track)

    def test_straight_line_collapses_to_two_points(self) -> None:
        track = DriveTrack()
        for i in range(1000):
            track.append(TrackPoint(t=float(i), lat=10.0 + i * 0.0001, lon=20.0))
        simplified = track.simplify(1.0)
        assert len(simplified) == 2
        assert simplified.points[0] == track.points[0]
        assert simplified.points[-1] == track.points[-1]

    def test_l_shaped_track_keeps_corner(self) -> None:
        track = DriveTrack()
        t = 0.0
        # Head east along a fixed latitude.
        for i in range(50):
            track.append(TrackPoint(t=t, lat=10.0, lon=20.0 + i * 0.001))
            t += 1.0
        corner_lat, corner_lon = 10.0, 20.0 + 49 * 0.001
        # Then head north along a fixed longitude.
        for i in range(1, 50):
            track.append(TrackPoint(t=t, lat=10.0 + i * 0.001, lon=corner_lon))
            t += 1.0
        simplified = track.simplify(1.0)
        corner_points = [
            p
            for p in simplified.points
            if abs(p.lat - corner_lat) < 1e-9 and abs(p.lon - corner_lon) < 1e-9
        ]
        assert corner_points, "corner point should be retained"
        assert len(simplified) < len(track)

    def test_zero_length_pieces_do_not_crash(self) -> None:
        track = DriveTrack()
        t = 0.0
        for _ in range(20):
            track.append(TrackPoint(t=t, lat=10.0, lon=20.0))
            t += 1.0
        # A couple of points that move slightly then come back to a duplicate.
        track.append(TrackPoint(t=t, lat=10.01, lon=20.0))
        t += 1.0
        track.append(TrackPoint(t=t, lat=10.0, lon=20.0))
        simplified = track.simplify(5.0)
        assert simplified.points[0] == track.points[0]
        assert simplified.points[-1] == track.points[-1]

    def test_zero_and_one_point_tracks_return_same(self) -> None:
        empty = DriveTrack()
        assert empty.simplify(5.0).points == []
        single = DriveTrack([TrackPoint(t=1.0, lat=1.0, lon=1.0)])
        assert single.simplify(5.0).points == single.points


class TestPreview:
    def test_respects_max_points_on_wiggly_track(self) -> None:
        random.seed(42)
        track = DriveTrack()
        lat, lon = 40.0, -105.0
        t = 0.0
        for i in range(5000):
            lat += random.uniform(-0.0005, 0.0005)
            lon += random.uniform(-0.0005, 0.0005)
            t += 1.0
            track.append(TrackPoint(t=t, lat=lat, lon=lon))
        preview = track.preview(max_points=150)
        assert len(preview) <= 150
        assert preview.points[0] == track.points[0]
        assert preview.points[-1] == track.points[-1]

    def test_short_track_returns_copy(self) -> None:
        track = _make_realistic_track(10)
        preview = track.preview(max_points=150)
        assert len(preview) == 10
        assert preview is not track
        assert preview.points == track.points

    def test_preview_default_max_points(self) -> None:
        track = _make_realistic_track(2000)
        preview = track.preview()
        assert len(preview) <= 150


class TestPayloadAndPointsJson:
    def test_to_payload_shape_and_units(self) -> None:
        track = _make_realistic_track(20)
        payload = track.to_payload()
        assert set(payload.keys()) == {
            "t",
            "lat",
            "lon",
            "speed_mps",
            "alt_m",
            "soc",
            "odo_m",
        }
        for key in payload:
            assert len(payload[key]) == 20
        assert payload["t"][0] == pytest.approx(track.points[0].t)
        assert payload["lat"][0] == pytest.approx(track.points[0].lat, abs=1e-5)
        assert payload["speed_mps"][0] == pytest.approx(
            track.points[0].speed_mps, abs=0.01
        )

    def test_to_payload_allows_none_inside_lists(self) -> None:
        points = [
            TrackPoint(t=0.0, lat=1.0, lon=1.0, soc=None),
            TrackPoint(t=1.0, lat=1.001, lon=1.001, soc=50.0),
        ]
        track = DriveTrack(points)
        payload = track.to_payload()
        assert payload["soc"][0] is None
        assert payload["soc"][1] == pytest.approx(50.0)

    def test_points_json_round_trip(self) -> None:
        track = _make_realistic_track(30)
        encoded = track.to_points_json()
        decoded = DriveTrack.from_points_json(encoded)
        assert len(decoded) == len(track)
        for original, restored in zip(track.points, decoded.points):
            assert restored == original

    def test_points_json_round_trip_with_nulls(self) -> None:
        points = [
            TrackPoint(t=0.0, lat=1.0, lon=1.0, speed_mps=None, odo_m=None),
            TrackPoint(t=1.0, lat=1.001, lon=1.001, speed_mps=2.0, odo_m=99.0),
        ]
        track = DriveTrack(points)
        decoded = DriveTrack.from_points_json(track.to_points_json())
        assert decoded.points == track.points
