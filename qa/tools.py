"""Step 4 - generic, tested tools. Every tool returns a Result(value, unit, scope, evidence).

No pack IDs, question IDs or answer constants. Evidence strings name the file and a verifiable locator.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pandas as pd


# ------------------------------------------------------------------ result + evidence
@dataclass
class Result:
    value: object
    unit: str = ""
    scope: str = ""
    evidence: list[str] = field(default_factory=list)

    def __repr__(self):
        return f"Result({self.value!r} {self.unit} | {self.scope} | ev={len(self.evidence)})"


class Recorder:
    """Collects evidence locators as tools touch the sources."""

    def __init__(self):
        self.items: list[str] = []

    def add(self, s: str):
        if s not in self.items:
            self.items.append(s)
        return s


def round_half_up(x: float, nd: int = 2) -> float:
    q = Decimal(1).scaleb(-nd)
    return float(Decimal(repr(float(x))).quantize(q, rounding=ROUND_HALF_UP))


def fmt(x: float, nd: int = 2) -> str:
    return f"{round_half_up(x, nd):,.{nd}f}"


# ------------------------------------------------------------------ spreadsheets
_T = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
_O = "{urn:oasis:names:tc:opendocument:xmlns:office:1.0}"
_X = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"


def col_to_idx(col: str) -> int:
    n = 0
    for ch in col.upper():
        n = n * 26 + ord(ch) - 64
    return n


def split_a1(a1: str):
    m = re.fullmatch(r"([A-Za-z]+)(\d+)", a1.strip())
    if not m:
        raise ValueError(f"bad A1 reference {a1!r}")
    return int(m.group(2)), col_to_idx(m.group(1))


class Ods:
    """ODS reader exposing stored value and displayed text for each cell."""

    def __init__(self, path: Path, rec: Recorder | None = None, label: str | None = None):
        self.path, self.rec, self.label = Path(path), rec, label or Path(path).name.split("__", 1)[-1]
        with zipfile.ZipFile(path) as z:
            self._root = ET.fromstring(z.read("content.xml"))
        self._cache: dict[str, dict] = {}

    def sheets(self):
        return [t.get(_T + "name") for t in self._root.iter(_T + "table")]

    def _grid(self, sheet: str):
        if sheet in self._cache:
            return self._cache[sheet]
        out = {}
        for tbl in self._root.iter(_T + "table"):
            if tbl.get(_T + "name") != sheet:
                continue
            r = 0
            for row in tbl.iter(_T + "table-row"):
                rep_r = int(row.get(_T + "number-rows-repeated", 1))
                if rep_r > 1000:
                    r += rep_r
                    continue
                cells, c = [], 0
                for el in row:
                    if el.tag not in (_T + "table-cell", _T + "covered-table-cell"):
                        continue
                    rep_c = int(el.get(_T + "number-columns-repeated", 1))
                    shown = " ".join("".join(p.itertext()) for p in el.findall(_X + "p"))
                    v = el.get(_O + "value")
                    stored = float(v) if v is not None else (shown or None)
                    if rep_c > 1000:
                        c += rep_c
                        continue
                    for _ in range(rep_c):
                        c += 1
                        cells.append((c, stored, shown))
                for k in range(rep_r):
                    for c, stored, shown in cells:
                        out[(r + 1 + k, c)] = (stored, shown)
                r += rep_r
            break
        else:
            raise KeyError(sheet)
        self._cache[sheet] = out
        return out

    def cell(self, sheet: str, a1: str, shown: bool = True):
        """shown=True -> displayed number parsed from text (the 'visibly formatted' convention); False -> stored value."""
        stored, text = self._grid(sheet).get(split_a1(a1), (None, ""))
        if self.rec:
            self.rec.add(f"{self.label}, sheet {sheet}, cell {a1.upper()}")
        if shown and text:
            t = text.replace(",", "").replace("%", "").strip()
            try:
                return float(t)
            except ValueError:
                return text
        return stored


class Xlsx:
    def __init__(self, path: Path, rec: Recorder | None = None, label: str | None = None):
        from openpyxl import load_workbook

        self.path, self.rec, self.label = Path(path), rec, label or Path(path).name.split("__", 1)[-1]
        self.wb = load_workbook(path, data_only=True)

    def sheets(self):
        return self.wb.sheetnames

    def cell(self, sheet: str, a1: str, shown: bool = False):
        c = self.wb[sheet][a1.upper()]
        if self.rec:
            self.rec.add(f"{self.label}, sheet {sheet}, cell {a1.upper()}")
        if shown and c.value is not None and isinstance(c.value, (int, float)):
            from openpyxl.styles.numbers import is_date_format  # noqa: F401

            nf = c.number_format
            m = re.search(r"0\.(0+)", nf)
            nd = len(m.group(1)) if m else 0
            return round_half_up(c.value, nd)
        return c.value


# ------------------------------------------------------------------ arithmetic helpers
def pct_change(old: float, new: float) -> float:
    return (new - old) / old * 100.0


def share(part: float, whole: float) -> float:
    return part / whole * 100.0


def days_between(a: str, b: str) -> int:
    """Calendar days from a to b, excluding the starting day (b - a)."""
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days


# ------------------------------------------------------------------ tabular
def read_table(path: Path, **kw) -> pd.DataFrame:
    path = Path(path)
    if path.suffix.lower() in (".csv", ".txt"):
        for enc in ("utf-8-sig", "latin-1"):
            try:
                first = path.open(encoding=enc).readline()
                sep = ";" if first.count(";") > first.count(",") else ","
                return pd.read_csv(path, sep=sep, dtype=str, keep_default_na=False, encoding=enc, **kw)
            except UnicodeDecodeError:
                continue
    raise ValueError(f"unsupported table {path}")


def duplicates(values) -> list:
    s = pd.Series(list(values))
    return sorted(s[s.duplicated(keep=False)].unique().tolist())


def set_difference(a, b):
    """Return (only_in_a, only_in_b) for two iterables of keys."""
    sa, sb = set(a), set(b)
    return sorted(sa - sb), sorted(sb - sa)


def value_counts_share(values, top: int = 5):
    s = pd.Series(list(values))
    vc = s.value_counts()
    return [(k, int(v), v / len(s) * 100) for k, v in vc.head(top).items()]


def is_blank(v, trim: bool = True) -> bool:
    if v is None or (isinstance(v, float) and v != v):
        return True
    return (str(v).strip() if trim else str(v)) == ""


# ------------------------------------------------------------------ vector / spatial
def read_vector(path: Path, layer: str | None = None):
    import geopandas as gpd

    return gpd.read_file(path, layer=layer)


def polygon_area(path: Path, id_field: str, id_value: str, crs: str, layer: str | None = None, rec: Recorder | None = None) -> Result:
    """Area of the (unrepaired) polygon whose id_field == id_value, computed in `crs` metres."""
    g = read_vector(path, layer)
    row = g[g[id_field].astype(str) == str(id_value)]
    if len(row) != 1:
        return Result(None, scope=f"{len(row)} features match {id_field}={id_value}")
    geom = row.to_crs(crs).geometry.iloc[0]
    name = Path(path).name.split("__", 1)[-1]
    ev = [f"{name}, properties.{id_field}={id_value}"]
    if rec:
        rec.add(ev[0])
    return Result(geom.area, "m2", f"{crs}; valid={geom.is_valid}", ev)


def geodesic_distance_m(lon1, lat1, lon2, lat2) -> float:
    from pyproj import Geod

    return Geod(ellps="WGS84").inv(lon1, lat1, lon2, lat2)[2]


def circle_overlap_area(c1, r1, c2, r2):
    """Area of intersection of two circles given centres (x,y) in metres and radii."""
    from shapely.geometry import Point

    a, b = Point(c1).buffer(r1, 1024), Point(c2).buffer(r2, 1024)
    return a.intersection(b).area


# ------------------------------------------------------------------ raster
def raster_class_areas(path: Path, mask_geom=None, nodata_values=(0,), all_touched: bool = False):
    """Sum of WGS84 geodesic cell areas (km2) per class for pixels whose centres fall in mask_geom.

    Implementation: per-row cell area from the ellipsoid (constant along a row on a lat/lon grid).
    """
    import numpy as np
    import rasterio
    from pyproj import Geod
    from rasterio.features import geometry_mask

    geod = Geod(ellps="WGS84")
    with rasterio.open(path) as d:
        arr = d.read(1)
        T = d.transform
        valid = ~np.isin(arr, nodata_values)
        if mask_geom is not None:
            inside = geometry_mask([mask_geom], out_shape=arr.shape, transform=T, invert=True, all_touched=all_touched)
            valid &= inside
        w, h = abs(T.a), abs(T.e)
        rows = np.arange(arr.shape[0])
        lat_top = T.f + T.e * rows
        lat_bot = lat_top + T.e
        row_area = np.array([abs(geod.polygon_area_perimeter([T.c, T.c + w, T.c + w, T.c], [a, a, b, b])[0])
                             for a, b in zip(lat_top, lat_bot)])
        out = {}
        for cls in np.unique(arr[valid]):
            m = valid & (arr == cls)
            out[int(cls)] = float((m.sum(axis=1) * row_area).sum() / 1e6)
        return out


# ------------------------------------------------------------------ GTFS
def gtfs_active_services(calendar: pd.DataFrame, calendar_dates: pd.DataFrame | None, date: str) -> set:
    d = dt.date.fromisoformat(date)
    ymd = d.strftime("%Y%m%d")
    dow = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"][d.weekday()]
    active = set()
    for r in calendar.itertuples():
        if r.start_date <= ymd <= r.end_date and getattr(r, dow) == "1":
            active.add(r.service_id)
    if calendar_dates is not None:
        for r in calendar_dates.itertuples():
            if r.date == ymd:
                (active.add if r.exception_type == "1" else active.discard)(r.service_id)
    return active


def gtfs_time_to_seconds(t: str) -> int:
    h, m, s = (int(x) for x in t.split(":"))
    return h * 3600 + m * 60 + s  # hours may exceed 24


def pathway_shortest(pathways: pd.DataFrame, src: str, dst: str, allowed_modes=None, use_time: str = "traversal_time"):
    """Dijkstra on GTFS pathways. Bidirectional links honoured; zero traversal times kept."""
    import networkx as nx

    G = nx.DiGraph()
    for r in pathways.itertuples():
        mode = int(r.pathway_mode)
        if allowed_modes is not None and mode not in allowed_modes:
            continue
        t = float(getattr(r, use_time) or 0)
        G.add_edge(r.from_stop_id, r.to_stop_id, weight=t)
        if r.is_bidirectional == "1":
            G.add_edge(r.to_stop_id, r.from_stop_id, weight=t)
    try:
        return nx.dijkstra_path_length(G, src, dst), nx.dijkstra_path(G, src, dst)
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return None, []
