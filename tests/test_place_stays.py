"""Stays at a place: pairing a drive that ends there with the next one leaving."""

from __future__ import annotations

from custom_components.rivian.places import Stay, StayDrive, place_stays

P = 5


def _d(
    start: float,
    end: float,
    sp: int | None,
    ep: int | None,
    micro: bool = False,
    so: float | None = None,
    eo: float | None = None,
    s: tuple[float | None, float | None] = (None, None),
    e: tuple[float | None, float | None] = (None, None),
) -> StayDrive:
    return StayDrive(
        start_ts=start,
        end_ts=end,
        start_place_id=sp,
        end_place_id=ep,
        is_micro=micro,
        start_odometer_mi=so,
        end_odometer_mi=eo,
        start_lat=s[0],
        start_lon=s[1],
        end_lat=e[0],
        end_lon=e[1],
    )


def test_arrival_then_departure_is_one_stay() -> None:
    assert place_stays([_d(0, 100, 1, P), _d(4000, 4100, P, 1)], P) == [Stay(100, 4000)]


def test_micro_drive_inside_the_place_does_not_end_the_stay() -> None:
    drives = [
        _d(0, 100, 1, P),
        _d(500, 560, P, P, micro=True),
        _d(4000, 4100, P, 1),
    ]
    assert place_stays(drives, P) == [Stay(100, 4000)]


def test_next_drive_elsewhere_gives_arrival_only() -> None:
    assert place_stays([_d(0, 100, 1, P), _d(4000, 4100, 2, 1)], P) == [Stay(100, None)]


def test_currently_parked_gives_arrival_only() -> None:
    assert place_stays([_d(0, 100, 1, P)], P) == [Stay(100, None)]


def test_departure_without_arrival() -> None:
    assert place_stays([_d(0, 100, 1, 2), _d(4000, 4100, P, 1)], P) == [
        Stay(None, 4000)
    ]


def test_odometer_jump_breaks_the_stay() -> None:
    here = (37.0, -122.0)
    drives = [
        _d(0, 100, 1, P, eo=1000.0, e=here),
        _d(4000, 4100, P, 1, so=1006.0, s=here),
    ]
    assert place_stays(drives, P) == [Stay(100, None), Stay(None, 4000)]


def test_late_first_report_keeps_the_stay() -> None:
    # The first fix arrives ~1.66 mi east of where the car was parked; the
    # odometer moved 2.4 mi, within 1.5 * 1.66 + 0.5.
    drives = [
        _d(0, 100, 1, P, eo=1000.0, e=(37.0, -122.0)),
        _d(4000, 4100, P, 1, so=1002.4, s=(37.0, -122.03)),
    ]
    assert place_stays(drives, P) == [Stay(100, 4000)]


def test_missing_odometer_trusts_place_ids() -> None:
    drives = [_d(0, 100, 1, P, eo=None), _d(4000, 4100, P, 1, so=1500.0)]
    assert place_stays(drives, P) == [Stay(100, 4000)]


def test_other_places_ignored() -> None:
    assert place_stays([_d(0, 100, 1, 2), _d(4000, 4100, 2, 1)], P) == []


# -- AnalyticsDatabase.place_stays ---------------------------------------------

VIN_A = "7PDSGABA8NN000001"
VIN_B = "7PDSGABA8NN000002"
DEMO = "DEMO0R2EAGLE00001"
HOME = (37.0, -122.0)
SHOP = (37.2, -122.3)


def _drive(vin: str, drive_id: str, day: int, hour: int, minute: int, start, end):  # type: ignore[no-untyped-def]
    from custom_components.rivian.drive_models import DriveRecord

    return DriveRecord(
        vin=vin,
        drive_id=drive_id,
        start_time=f"2026-02-{day:02d}T{hour:02d}:{minute:02d}:00Z",
        end_time=f"2026-02-{day:02d}T{hour:02d}:{minute + 30:02d}:00Z",
        distance_miles=20.0,
        duration_seconds=1800.0,
        start_soc=80.0,
        end_soc=75.0,
        battery_capacity_kwh=135.0,
        energy_kwh=6.0,
        start_lat=start[0],
        start_lon=start[1],
        end_lat=end[0],
        end_lon=end[1],
    )


def _seed(db) -> int:  # type: ignore[no-untyped-def]
    db.upsert_drives(
        VIN_A,
        [
            _drive(VIN_A, "a1", 1, 8, 0, HOME, SHOP),  # arrive 08:30
            _drive(VIN_A, "a2", 1, 9, 10, SHOP, HOME),  # leave 09:10
        ],
    )
    db.upsert_drives(VIN_B, [_drive(VIN_B, "b1", 2, 8, 0, HOME, SHOP)])
    db.create_place("real", *HOME, "Home")
    return db.create_place("real", *SHOP, "Shop")


def test_db_place_stays_pairs_per_vehicle(analytics_db) -> None:  # type: ignore[no-untyped-def]
    shop = _seed(analytics_db)
    stays = analytics_db.place_stays("real", shop)
    by_vin = {s["vin"]: s for s in stays}
    assert len(stays) == 2
    a = by_vin[VIN_A]
    assert a["depart_ts"] - a["arrive_ts"] == 2400
    assert by_vin[VIN_B]["arrive_ts"] is not None
    assert by_vin[VIN_B]["depart_ts"] is None
    only_a = analytics_db.place_stays("real", shop, [VIN_A])
    assert [s["vin"] for s in only_a] == [VIN_A]


def test_db_place_stays_hidden_or_unknown_is_none(analytics_db) -> None:  # type: ignore[no-untyped-def]
    shop = _seed(analytics_db)
    assert analytics_db.place_stays("real", 9999) is None
    assert analytics_db.place_stays("demo", shop) is None
    analytics_db.update_place("real", shop, hidden=True)
    assert analytics_db.place_stays("real", shop) is None


def test_db_place_stays_demo_dataset_is_separate(analytics_db) -> None:  # type: ignore[no-untyped-def]
    import json

    from custom_components.rivian.analytics_db import DEMO_VEHICLES_META_KEY

    analytics_db.set_meta(
        DEMO_VEHICLES_META_KEY, json.dumps([{"vin": DEMO, "name": "Demo"}])
    )
    analytics_db.upsert_drives(DEMO, [_drive(DEMO, "d1", 3, 8, 0, HOME, SHOP)])
    shop = _seed(analytics_db)
    vins = {s["vin"] for s in analytics_db.place_stays("real", shop)}
    assert DEMO not in vins
    assert vins == {VIN_A, VIN_B}
    # Asking for a demo VIN against a real place yields nothing for it.
    assert analytics_db.place_stays("real", shop, [DEMO]) == []
