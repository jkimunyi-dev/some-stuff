"""Layer-family tests: second-implementation cross-checks + invariants (no answer constants)."""
import os
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from qa.layers import Layer, circle_area, circle_overlap, diameter_from_circumference, key_diff, namespace_counts, timestamp_gap_seconds

SRC = Path(os.environ.get("QA_SOURCES", ""))
pytestmark = pytest.mark.skipif(not SRC.is_dir(), reason="QA_SOURCES not set")


def f(name):
    return next(SRC.glob(f"SRC_*__{name}"))


@pytest.fixture(scope="module")
def street():
    return Layer(f("strassenbaeume.geojson"), label="street")


@pytest.fixture(scope="module")
def park():
    return Layer(f("anlagenbaeume.geojson"), label="park")


def test_alternate_formats_same_records(street):
    gp = Layer(f("strassenbaeume.gpkg"))
    assert len(gp) == len(street)
    assert sorted(gp.gdf.gisid) == sorted(street.gdf.gisid)
    assert gp.value_counts("gattung").to_dict() == street.value_counts("gattung").to_dict()


def test_blank_counts_vs_pandas(street):
    for col in ("gattung", "gattung_deutsch", "art_bot"):
        s = street.gdf[col]
        want = int((s.isna() | (s.astype(str).str.strip() == "")).sum())
        assert street.null_count(col).value == want


def test_top_values_ties_and_share(street):
    r = street.top_values("gattung").value
    vc = street.gdf.gattung.replace("", pd.NA).dropna().value_counts()
    assert {x["value"] for x in r} == set(vc[vc == vc.max()].index)
    assert all(x["count"] == vc.max() for x in r)


def test_pooled_not_mean_of_percentages(street, park):
    r = Layer.pooled_share([street, park], "gattung", "Tilia").value
    assert r["num"] == sum(v["num"] for v in r["per_layer"].values())
    assert r["den"] == len(street) + len(park)
    assert 0 <= r["pct"] <= 100


def test_eligible_group_vs_sqlite(street):
    con = sqlite3.connect(":memory:")
    street.gdf.drop(columns="geometry").to_sql("t", con, index=False)
    rows = con.execute("""SELECT strname, COUNT(*) n, SUM(gattung='Tilia') k FROM t GROUP BY strname
                          HAVING n>=10 AND k*1.0/n <= 0.5 ORDER BY k*1.0/n DESC""").fetchall()
    if not rows:
        pytest.skip("no eligible street")
    from fractions import Fraction
    best = Fraction(rows[0][2], rows[0][1])
    want = {r[0] for r in rows if Fraction(r[2], r[1]) == best}
    got = Layer.eligible_group_extreme(street, "strname", "gattung", "Tilia", 10, 50.0).value
    assert {g["group"] for g in got} == want


def test_within_radius_vs_shapely(street, park):
    anchor = park.gdf.gisid.iloc[0]
    res = park.within_radius("gisid", anchor, 20, "EPSG:25833", others=[street])
    pa = park.proj("EPSG:25833")
    pt = pa.geometry.iloc[0]
    want = sum(int((L.proj("EPSG:25833").geometry.distance(pt) <= 20 + 1e-9).sum()) for L in (park, street))
    assert res.value == want >= 1


def test_radius_boundary_inclusive(park):
    g = park.proj("EPSG:25833")
    d = g.geometry.iloc[0].distance(g.geometry.iloc[1])
    a = park.gdf.gisid.iloc[0]
    assert park.within_radius("gisid", a, d, "EPSG:25833").value == park.within_radius("gisid", a, d - 1e-3, "EPSG:25833").value + 1


def test_circle_overlap_vs_buffer():
    from shapely.geometry import Point
    for c1, d1, c2, d2 in [((0, 0), 10, (4, 0), 8), ((0, 0), 10, (1, 0), 4), ((0, 0), 6, (9, 0), 6)]:
        want = Point(c1).buffer(d1 / 2, 2048).intersection(Point(c2).buffer(d2 / 2, 2048)).area
        assert abs(circle_overlap(c1, d1, c2, d2) - want) < 1e-2
    assert abs(circle_area(2) - 3.14159265) < 1e-6
    assert abs(diameter_from_circumference(3.14159265358979) - 1) < 1e-12


def test_bbox_violations_independent(park):
    g = park.proj("EPSG:25833")
    x0, x1 = g.geometry.x.quantile(.1), g.geometry.x.quantile(.9)
    y0, y1 = g.geometry.y.quantile(.1), g.geometry.y.quantile(.9)
    r = park.bbox_violations(x0, x1, y0, y1, "EPSG:25833", "gisid").value
    out = ((g.geometry.x < x0) | (g.geometry.x > x1) | (g.geometry.y < y0) | (g.geometry.y > y1))
    assert len(r) == int(out.sum())
    assert all(v["overshoot_m"]["x"] > 0 or v["overshoot_m"]["y"] > 0 for v in r)


def test_timestamp_gap_offset_aware():
    # 13:02:07.609+01:00 == 12:02:07.609Z, so the instants are 46 ms apart, not an hour
    assert timestamp_gap_seconds("2026-01-01T13:02:07.609+01:00", "2026-01-01T12:02:07.655Z") == pytest.approx(0.046, abs=1e-6)
    assert timestamp_gap_seconds("2026-01-01T13:00:00+01:00", "2026-01-01T12:00:00Z") == 0.0
    assert timestamp_gap_seconds("2026-01-01T13:00:00+01:00", "2026-01-01T13:00:00Z") == 3600.0


def test_key_diff_and_namespaces():
    a = pd.Series(["BK_1", "BK_2", "TLM3D_9", "BK_3"])
    b = pd.Series(["1", "2", "7"])
    r = key_diff(a, b, strip_prefix="BK_", only_with_prefix="BK_").value
    assert r == {"only_a": 1, "only_b": 1, "both": 2}
    assert namespace_counts(a) == {"BK": 3, "TLM3D": 1}


def test_zurich_registry_and_3d_views():
    reg = Layer(f("register.geojson"))
    inv = Layer(f("inventory.geojson"))
    # inventory columns x/y are projected (EPSG:2056) while geometry is WGS84: projecting must land near those values
    if {"x", "y"} <= set(inv.gdf.columns):
        g = inv.proj("EPSG:2056").head(200)
        sub = inv.gdf.head(200)
        ok = (g.geometry.x - pd.to_numeric(sub.x, errors="coerce")).abs().dropna()
        assert len(ok) and ok.median() < 5
    assert reg.proj("EPSG:2056").geometry.iloc[0].x > 1e6
    parts = Layer(f("tree-parts-view.geojson"))
    pid = parts.gdf.baumnummer.iloc[1]
    ext = parts.z_extent("baumnummer", pid).value
    assert ext["span"] == pytest.approx(ext["max"] - ext["min"]) and ext["span"] >= 0
    crown = Layer(f("crown-drawing-view.geojson"))
    bad = crown.invalid_geometries("EPSG:2056", "baumnummer").value
    assert isinstance(bad, list)
