"""Step 3 - dev set built ONLY from Train sources (Liverpool TPO, UK bus workbooks, Helsinki energy).

Every reference is computed two independent ways (A: qa.tools; B: a separate raw implementation) and must agree.
~30% (by stable hash of the id) is flagged hold-out and must not be used for tuning.
Usage: python -m qa.devset --sources SRC_DIR --out dev_set.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path

import pandas as pd

from qa.tools import Ods, Recorder, Xlsx, days_between, fmt, pct_change, read_table, round_half_up, share

HEL_CTX = ("Use the stored numeric cell values, including their decimal precision; the workbook's display format rounds them to "
           "whole GWh. Keep intermediate calculations unrounded. Report GWh values and percentages to two decimal places unless "
           "the question specifies otherwise.")
BUS_CTX = ("Use the numbers as they are visibly formatted in the supplied workbooks, rather than extra decimal places stored inside "
           "their cells. Keep intermediate calculations unrounded. Report percentages and percentage-point changes to two decimal places "
           "unless the question specifies otherwise.")
TPO_CTX = ("Use the supplied frozen Liverpool tree-preservation files, selecting the exact named file and its recorded fields. "
           "Different formats must not be assumed to be row-for-row copies. Preserve source references, versions, nulls and geometry; "
           "do not replace the supplied records with a live register.")


def find(src: Path, name: str) -> Path:
    return next(src.glob(f"SRC_*__{name}"))


def split_of(qid: str) -> str:
    return "holdout" if int(hashlib.sha256(qid.encode()).hexdigest(), 16) % 10 < 3 else "dev"


# --------------------------------------------------------------- Helsinki
HEL = "Paakaupunkiseudun_energiankulutus_1990_ja_2000-2024.xlsx"
CITY_HDR = {"the Helsinki metropolitan area": 2, "Helsinki": 15, "Espoo": 28, "Vantaa": 41, "Kauniainen": 54}


def hel_blocks_A(x: Xlsx):
    """{city: {'total_row','sector_total_row','resid_row','col': {year: letter}}} using header rows (A: openpyxl cells)."""
    from openpyxl.utils import get_column_letter

    ws = x.wb["Tulokset"]
    cols = {ws.cell(2, c).value: get_column_letter(c) for c in range(2, 28)}
    out = {}
    for city, h in CITY_HDR.items():
        r_total = next(r for r in range(h + 1, h + 12) if ws.cell(r, 1).value == "Yhteensä")
        r_res = next(r for r in range(r_total + 1, r_total + 8) if ws.cell(r, 1).value == "Asuminen")
        out[city] = {"total": r_total, "resid": r_res}
    return out, cols


def hel_B(path: Path):
    """B: pandas raw grid read, independent of openpyxl cell addressing."""
    df = pd.read_excel(path, sheet_name="Tulokset", header=None)
    years = {}
    for c, v in df.iloc[1].items():  # sheet repeats the year header further right (a second table); keep the first
        if isinstance(v, (int, float)) and v == v and 1989 < v < 2030:
            years.setdefault(int(v), c)
    res = {}
    for city, h in CITY_HDR.items():
        h0 = h - 1  # 0-based
        sub = df.iloc[h0 + 1: h0 + 12, 0]
        tot = h0 + 1 + list(sub).index("Yhteensä")
        res_i = tot + 1 + list(df.iloc[tot + 1: tot + 8, 0]).index("Asuminen")
        res[city] = (tot, res_i)
    return df, years, res


def helsinki_items(src: Path):
    p = find(src, HEL)
    rec = Recorder()
    x = Xlsx(p, rec)
    blocks, cols = hel_blocks_A(x)
    df, years, resB = hel_B(p)
    items = []
    cities = list(CITY_HDR)
    for city in cities:
        for y0, y1 in [(2023, 2024), (2019, 2024), (2000, 2010)]:
            b = blocks[city]
            a0, a1 = x.cell("Tulokset", f"{cols[y0]}{b['total']}"), x.cell("Tulokset", f"{cols[y1]}{b['total']}")
            tb, _ = resB[city]
            b0, b1 = float(df.iat[tb, years[y0]]), float(df.iat[tb, years[y1]])
            assert abs(a0 - b0) < 1e-9 and abs(a1 - b1) < 1e-9
            q = f"For the briefing, how much did total energy consumption in {city} change from {y0} to {y1}, in GWh and percent?"
            ans = f"Total energy consumption in {city} changed by {fmt(a1 - a0)} GWh, or {fmt(pct_change(a0, a1))}%, from {y0} to {y1}."
            items.append(dict(domain="buildings_energy", question=q, context=HEL_CTX, answer=ans,
                              evidence=[f"{HEL}, sheet Tulokset, cells {cols[y0]}{b['total']} and {cols[y1]}{b['total']}"]))
        for y in (2000, 2024):
            b = blocks[city]
            ra, ta = x.cell("Tulokset", f"{cols[y]}{b['resid']}"), x.cell("Tulokset", f"{cols[y]}{b['total']}")
            tb, rb = resB[city]
            assert abs(ra / ta - float(df.iat[rb, years[y]]) / float(df.iat[tb, years[y]])) < 1e-12
            q = f"What share of {city}'s total energy use in {y} was residential (Asuminen)?"
            ans = f"Residential energy use was {fmt(share(ra, ta))}% of {city}'s total in {y}."
            items.append(dict(domain="buildings_energy", question=q, context=HEL_CTX, answer=ans,
                              evidence=[f"{HEL}, sheet Tulokset, cells {cols[y]}{b['resid']} and {cols[y]}{b['total']}"]))
    # which city largest GWh rise 2023->2024 (three main cities)
    three = ["Helsinki", "Espoo", "Vantaa"]
    rise = {c: x.cell("Tulokset", f"{cols[2024]}{blocks[c]['total']}") - x.cell("Tulokset", f"{cols[2023]}{blocks[c]['total']}") for c in three}
    riseB = {c: float(df.iat[resB[c][0], years[2024]]) - float(df.iat[resB[c][0], years[2023]]) for c in three}
    assert max(rise, key=rise.get) == max(riseB, key=riseB.get)
    w = max(rise, key=rise.get)
    items.append(dict(domain="buildings_energy", context=HEL_CTX,
                      question="Which of Helsinki, Espoo and Vantaa had the largest GWh increase in total energy consumption from 2023 to 2024, and by how much?",
                      answer=f"{w} had the largest increase, {fmt(rise[w])} GWh.", evidence=[f"{HEL}, sheet Tulokset, total rows for each city, columns {cols[2023]} and {cols[2024]}"]))
    return items


# --------------------------------------------------------------- Liverpool TPO
def tpo_items(src: Path):
    import csv
    import datetime as dt

    p = find(src, "trees-dataset-2026-06-23.csv")
    df = read_table(p)
    raw = list(csv.DictReader(open(p, encoding="utf-8-sig")))
    ok = df[(df.made_date != "") & (df.confirmed_date != "")].reset_index(drop=True)
    items = []
    for ref in ok.reference.iloc[::45].head(8):
        r = ok[ok.reference == ref].iloc[0]
        a = days_between(r.made_date[:10], r.confirmed_date[:10])
        rb = next(d for d in raw if d["reference"] == ref)
        b = (dt.datetime.strptime(rb["confirmed_date"][:10], "%Y-%m-%d") - dt.datetime.strptime(rb["made_date"][:10], "%Y-%m-%d")).days
        assert a == b
        fm = lambda s: dt.date.fromisoformat(s[:10]).strftime("%-d %B %Y") if False else dt.date.fromisoformat(s[:10]).strftime("%d %B %Y").lstrip("0")
        items.append(dict(domain="urban_greenery", context=TPO_CTX,
                          question=f"I'm checking {ref} in trees-dataset-2026-06-23.csv. What were its made and confirmed dates, and how many calendar days elapsed between them, excluding the starting day?",
                          answer=f"{ref} was made on {fm(r.made_date)} and confirmed on {fm(r.confirmed_date)}. The elapsed time was {a} calendar days, excluding the starting day.",
                          evidence=[f"trees-dataset-2026-06-23.csv, reference={ref}, made_date and confirmed_date"]))
    n_blank = int((df.confirmed_date == "").sum())
    n_blank_b = sum(1 for d in raw if not d["confirmed_date"].strip())
    assert n_blank == n_blank_b
    items.append(dict(domain="urban_greenery", context=TPO_CTX,
                      question="In trees-dataset-2026-06-23.csv, how many orders have no confirmed_date, and how many records are there in total?",
                      answer=f"{n_blank} of {len(df)} records have a blank confirmed_date.",
                      evidence=["trees-dataset-2026-06-23.csv, confirmed_date column"]))
    return items


def zone_items(src: Path):
    import geopandas as gpd
    from pyproj import Transformer

    p = find(src, "zones-2026-06-23.geojson")
    g = gpd.read_file(p)
    tr = Transformer.from_crs(g.crs, "EPSG:27700", always_xy=True)
    items = []
    for i in range(0, len(g), 90):
        row = g.iloc[i]
        a = g.iloc[[i]].to_crs("EPSG:27700").geometry.iloc[0].area
        # B: shoelace on exterior minus holes, via manual coordinate transform
        geom = row.geometry

        def ring_area(ring):
            xs, ys = tr.transform(*zip(*[(c[0], c[1]) for c in ring.coords]))
            return abs(sum(xs[k] * ys[k + 1] - xs[k + 1] * ys[k] for k in range(len(xs) - 1))) / 2

        polys = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
        b = sum(ring_area(pl.exterior) - sum(ring_area(h) for h in pl.interiors) for pl in polys)
        assert abs(a - b) / max(a, 1) < 1e-6, (a, b)
        ref = row["reference"]
        items.append(dict(domain="urban_greenery", context="Organiser-authored budget request; calculate the valid published polygon without repairing or replacing its boundary.",
                          question=f"For an early budget estimate, give the area of Liverpool zone {ref} in hectares, rounded to two decimal places. Use its actual published geometry in zones-2026-06-23.geojson, calculate in British National Grid metres, and do not repair or replace the boundary.",
                          answer=f"Zone {ref} has an area of {fmt(a / 10000)} hectares, calculated from its published geometry in British National Grid metres.",
                          evidence=[f"zones-2026-06-23.geojson, properties.reference={ref}"]))
    return items


# --------------------------------------------------------------- UK bus (BUS01a)
def bus_items(src: Path):
    p = find(src, "bus01.ods")
    o = Ods(p)
    # B: independent lxml XPath read of displayed text
    from lxml import etree

    with zipfile.ZipFile(p) as z:
        root = etree.fromstring(z.read("content.xml"))
    ns = {"t": "urn:oasis:names:tc:opendocument:xmlns:table:1.0", "x": "urn:oasis:names:tc:opendocument:xmlns:text:1.0"}
    tbl = root.xpath("//t:table[@t:name='BUS01a']", namespaces=ns)[0]
    rows = []
    for r in tbl.xpath("t:table-row", namespaces=ns):
        cells = []
        for c in r.xpath("t:table-cell", namespaces=ns):
            rep = int(c.get(f"{{{ns['t']}}}number-columns-repeated", 1))
            txt = "".join(c.xpath("x:p//text()", namespaces=ns))
            cells += [txt] * min(rep, 50)
        rep_r = int(r.get(f"{{{ns['t']}}}number-rows-repeated", 1))
        rows += [cells] * min(rep_r, 50)
    colname = {"H": "London", "P": "England", "T": "England outside London"}
    items = []
    # find year rows via col A in A
    yr_rows = {int(float(o.cell("BUS01a", f"A{r}"))): r for r in range(10, 40) if isinstance(o.cell("BUS01a", f"A{r}"), float)}
    for col, name in colname.items():
        for y0, y1 in [(2019, 2025), (2022, 2025)]:
            if y0 not in yr_rows or y1 not in yr_rows:
                continue
            a0, a1 = o.cell("BUS01a", f"{col}{yr_rows[y0]}"), o.cell("BUS01a", f"{col}{yr_rows[y1]}")
            ci = ord(col) - 65
            b0, b1 = float(rows[yr_rows[y0] - 1][ci].replace(",", "")), float(rows[yr_rows[y1] - 1][ci].replace(",", ""))
            assert (a0, a1) == (b0, b1)
            items.append(dict(domain="transport", context=BUS_CTX,
                              question=f"Compare bus passenger journeys in {name} for the year ending March {y1} with the year ending March {y0}, as a percentage of the earlier year.",
                              answer=f"Journeys in {name} in the year ending March {y1} were {fmt(share(a1, a0))}% of the year-ending-March {y0} total.",
                              evidence=[f"bus01.ods, sheet BUS01a, cells {col}{yr_rows[y1]} and {col}{yr_rows[y0]}"]))
    items.append(dict(domain="transport", context=BUS_CTX,
                      question="Can we publish a zero share of operating revenue for either the metropolitan or non-metropolitan group in the year ending March 2022 using bus04ii.ods? Someone treated [z] entries as zeros.",
                      answer="No. The workbook uses [z] to mean not applicable, not a measured zero, so it does not support assigning a zero share.",
                      evidence=["bus04ii.ods, BUS04aii row 3 and cells C26:D26; Notes, note 4"]))
    return items


def build(src: Path):
    items = helsinki_items(src) + tpo_items(src) + zone_items(src) + bus_items(src)
    for i, it in enumerate(items):
        it["id"] = f"dev-{i:03d}"
        it["split"] = split_of(it["id"])
    return items


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", required=True)
    ap.add_argument("--out", default="dev_set.json")
    a = ap.parse_args()
    items = build(Path(a.sources))
    Path(a.out).write_text(json.dumps(items, indent=1, ensure_ascii=False), encoding="utf-8")
    s = pd.Series([i["split"] for i in items]).value_counts().to_dict()
    print(len(items), "items", s, pd.Series([i["domain"] for i in items]).value_counts().to_dict())
