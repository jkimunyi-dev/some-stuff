"""GTFS family tests: invariants + second-implementation cross-checks (sqlite vs pandas). No answer constants.
Set QA_SOURCES to the extracted sources/ folder."""
import os
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from qa.gtfs import Feed

SRC = Path(os.environ.get("QA_SOURCES", ""))
pytestmark = pytest.mark.skipif(not SRC.is_dir() or not list(SRC.glob("SRC_*__pathways.txt")), reason="GTFS sources not available")


@pytest.fixture(scope="module")
def feed():
    return Feed(SRC)


def test_blank_location_type_is_zero(feed):
    s = feed.stops()
    assert (s.loc_type[feed.t("stops").location_type.str.strip() == ""] == 0).all()
    assert s.loc_type.isin([0, 1, 2, 3, 4]).all()


def test_breakdown_partitions_rows(feed):
    r = feed.stop_breakdown().value
    assert r["stop_or_platform_with_platform_code"] + r["stop_or_platform_without_platform_code"] == r["stop_or_platform_total"]
    assert r["stop_or_platform_total"] + r["station"] + r["entrance/exit"] + r["generic node"] + r["boarding area"] == r["rows"]
    assert r["rows"] == len(feed.t("stops"))


def test_active_trips_matches_sqlite(feed):
    con = sqlite3.connect(":memory:")
    for n in ("calendar", "calendar_dates", "trips"):
        feed.t(n).to_sql(n, con, index=False)
    for date, dow in (("2026-11-04", "wednesday"), ("2026-12-26", "saturday")):
        ymd = date.replace("-", "")
        sql = f"""
        WITH base AS (SELECT service_id FROM calendar WHERE start_date<='{ymd}' AND end_date>='{ymd}' AND {dow}='1'),
             added AS (SELECT service_id FROM calendar_dates WHERE date='{ymd}' AND exception_type='1'),
             removed AS (SELECT service_id FROM calendar_dates WHERE date='{ymd}' AND exception_type='2'),
             act AS (SELECT service_id FROM base WHERE service_id NOT IN (SELECT service_id FROM removed)
                     UNION SELECT service_id FROM added)
        SELECT COUNT(*) FROM trips WHERE service_id IN (SELECT service_id FROM act)"""
        assert feed.active_trips(date).value == con.execute(sql).fetchone()[0]
    # a date with removal exceptions must count fewer trips once exceptions apply
    assert feed.active_trips("2026-12-26").value < feed.active_trips("2026-12-26", use_exceptions=False).value


def test_active_bike_subset_never_exceeds_total(feed):
    tot = feed.active_trips("2026-11-04").value
    assert 0 <= feed.active_trips("2026-11-04", bikes_allowed="2").value <= tot


def test_zero_and_blank_kept(feed):
    pw = feed.t("pathways")
    assert pw.traversal_time.map(type).eq(str).all()


def test_path_modes_monotone(feed):
    """Restricting modes can only lengthen or break a route; never shorten it."""
    pw = feed.t("pathways")
    pairs = pw[pw.is_bidirectional == "1"].head(40)
    for r in pairs.itertuples():
        full = feed.path_route(r.from_stop_id, r.to_stop_id, None).value
        sf = feed.path_route(r.from_stop_id, r.to_stop_id, {1, 5, 6, 7}).value
        nolift = feed.path_route(r.from_stop_id, r.to_stop_id, {1, 5, 6, 7}, exclude_modes={5}).value
        if full["reachable"] and sf["reachable"]:
            assert sf["seconds"] >= full["seconds"]
        if sf["reachable"] and nolift["reachable"]:
            assert nolift["seconds"] >= sf["seconds"]
        if not sf["reachable"]:
            assert not nolift["reachable"]
        assert all(l["mode"] != 5 for l in nolift["legs"])


def test_nearest_is_true_minimum(feed):
    s = feed.stops()
    plat = s[(s.loc_type == 0) & (s.parent_station != "")].iloc[0]
    par = plat.parent_station
    res = feed.nearest(plat.stop_id, 2, within_parent=par)
    if res.value:
        from qa.tools import geodesic_distance_m
        cands = s[(s.loc_type == 2) & (s.parent_station == par)]
        ds = [geodesic_distance_m(float(plat.stop_lon), float(plat.stop_lat), float(c.stop_lon), float(c.stop_lat)) for c in cands.itertuples()]
        assert abs(res.value["metres"] - round(min(ds), 2)) < 0.011


def test_radius_boundary_inclusive(feed):
    s = feed.stops()
    a = s.iloc[0]
    from qa.tools import geodesic_distance_m
    d = geodesic_distance_m(float(a.stop_lon), float(a.stop_lat), float(s.iloc[1].stop_lon), float(s.iloc[1].stop_lat))
    n_at = feed.within_radius(float(a.stop_lon), float(a.stop_lat), d).value
    n_below = feed.within_radius(float(a.stop_lon), float(a.stop_lat), d - 1e-6).value
    assert n_at == n_below + 1


def test_utc_time_past_midnight_and_dst(feed):
    assert feed.to_utc("2026-11-04", "25:30:00", "Australia/Melbourne").value == "2026-11-04 14:30:00"  # UTC+11, +25.5h from local midnight
    # DST start day in Melbourne (first Sunday of Oct 2026): offsets measured from noon-12h
    assert feed.to_utc("2026-10-04", "04:00:00", "Australia/Melbourne").value == "2026-10-03 17:00:00"


def test_wheelchair_blank_inherits_parent_not_zero(feed):
    s = feed.t("stops")
    blank = s[(s.wheelchair_boarding.str.strip() == "") & (s.parent_station != "")]
    if len(blank):
        r = feed.wheelchair_status(blank.iloc[0].stop_id).value
        assert r["inherited"] is True
