"""Unit tests: Train reference answers reproduced by qa.tools. Set QA_SOURCES to the extracted sources/ folder."""
import os
from pathlib import Path

import pytest

from qa.tools import Ods, Xlsx, Recorder, pct_change, share, days_between, round_half_up, polygon_area, read_table, duplicates, set_difference

SRC = Path(os.environ.get("QA_SOURCES", ""))
pytestmark = pytest.mark.skipif(not SRC.is_dir(), reason="QA_SOURCES not set")


def f(name):
    return next(SRC.glob(f"SRC_*__{name}"))


def test_helsinki_energy():
    rec = Recorder()
    x = Xlsx(f("Paakaupunkiseudun_energiankulutus_1990_ja_2000-2024.xlsx"), rec)
    g = lambda c: x.cell("Tulokset", c)  # stored values
    assert round_half_up(g("AA9") - g("Z9")) == 1964.06
    assert round_half_up(pct_change(g("Z9"), g("AA9"))) == 8.07
    assert round_half_up(g("AA11") - g("Z11")) == 31.02
    assert round_half_up(share(g("AA24"), g("AA22"))) == 45.45
    assert round_half_up(share(g("AA37"), g("AA35"))) == 51.09
    assert round_half_up(share(g("AA37"), g("AA35")) - share(g("AA24"), g("AA22"))) == 5.64
    assert round_half_up(g("AA22") - g("Z22")) == 1424.36
    assert round_half_up(pct_change(g("Z22"), g("AA22"))) == 10.75
    assert len(rec.items) > 5 and "sheet Tulokset" in rec.items[0]


def test_bus01_displayed_vs_2019():
    o = Ods(f("bus01.ods"))
    v = lambda c: o.cell("BUS01a", c, shown=True)
    assert v("A23") == 2019 and v("A29") == 2025  # rows are year-ending-March labels
    assert round_half_up(share(v("H29"), v("H23"))) == 82.85   # London
    assert round_half_up(share(v("T29"), v("T23"))) == 87.60   # England outside London


def test_bus_km_per_journey():
    km_bn = Ods(f("bus03.ods")).cell("BUS03a_km", "E29", shown=True)      # billion passenger-km
    journeys_m = Ods(f("bus01.ods")).cell("BUS01a", "P29", shown=True)   # million journeys
    assert round_half_up(km_bn * 1000 / journeys_m) == 6.07


def test_tpo453_days():
    df = read_table(f("trees-dataset-2026-06-23.csv"))
    r = df[df["reference"] == "TPO453"].iloc[0]
    print(r.get("made_date"), r.get("confirmed_date"))
    assert days_between(str(r["made_date"])[:10], str(r["confirmed_date"])[:10]) == 35


def test_liverpool_reference_match():
    import geopandas as gpd

    csv = read_table(f("tpo-trees.csv"))
    gp = gpd.read_file(f("trees.gpkg"))
    a, b = set(csv["reference"]), set(gp["reference"])
    assert a == b and not duplicates(csv["reference"]) and not duplicates(gp["reference"])


def test_zone_area():
    r = polygon_area(f("zones-2026-06-23.geojson"), "reference", "1912/A1", "EPSG:27700")
    assert round_half_up(r.value / 10_000) == 3.48


def test_devset_deterministic_and_two_way_agree(tmp_path):
    import json
    from qa.devset import build

    a, b = build(SRC), build(SRC)  # build() asserts method A == method B for every reference
    assert a == b and len(a) >= 45
    hold = [i for i in a if i["split"] == "holdout"]
    assert 0.2 < len(hold) / len(a) < 0.45
    committed = json.load(open(Path(__file__).parent.parent / "dev" / "dev_set.json", encoding="utf-8"))
    assert committed == a


def test_raster_areas_match_polygon_geodesic_area():
    """Cape Town: summed geodesic cell areas must equal the polygon's geodesic area (validates the cell-area method)."""
    import geopandas as gpd
    from pyproj import Geod
    from qa.tools import raster_class_areas

    m = gpd.read_file(f("municipalities.geojson"))
    r = m[m.shapeName.str.contains("Cape")].iloc[0]
    tot = sum(raster_class_areas(f("cape_town_2021.tif"), r.geometry).values())
    poly = abs(Geod(ellps="WGS84").geometry_area_perimeter(r.geometry)[0]) / 1e6
    assert abs(tot - poly) < 0.5


def test_gtfs_active_services_two_ways():
    from qa.tools import gtfs_active_services

    cal, cd = read_table(f("calendar.txt")), read_table(f("calendar_dates.txt"))
    date = "2026-11-04"  # Wednesday
    got = gtfs_active_services(cal, cd, date)
    base = {r.service_id for r in cal.itertuples() if r.start_date <= "20261104" <= r.end_date and r.wednesday == "1"}
    adds = {r.service_id for r in cd.itertuples() if r.date == "20261104" and r.exception_type == "1"}
    rem = {r.service_id for r in cd.itertuples() if r.date == "20261104" and r.exception_type == "2"}
    assert got == (base | adds) - rem
