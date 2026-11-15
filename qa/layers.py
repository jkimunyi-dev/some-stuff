"""Step 4 - vector / register ("layer") family tools. Generic over any GeoDataFrame-readable layer.

Conventions come in as parameters (CRS, radius, rounding). Blank == None, NaN or whitespace-only (never silently 0).
Every tool returns qa.tools.Result with evidence naming file + field/id.
"""
from __future__ import annotations

import datetime as dt
import math
import re
from fractions import Fraction
from pathlib import Path

import pandas as pd

from .tools import Recorder, Result, is_blank, round_half_up, read_vector


class Layer:
    def __init__(self, path: Path, rec: Recorder | None = None, layer: str | None = None, label: str | None = None):
        self.rec = rec or Recorder()
        self.path = Path(path)
        self.file = re.sub(r"^SRC_[0-9a-f]+__", "", self.path.name)
        self.label = label or self.file
        self.gdf = read_vector(self.path, layer)
        self._proj: dict[str, object] = {}

    def __len__(self):
        return len(self.gdf)

    def _ev(self, what: str) -> str:
        return self.rec.add(f"{self.file} | {what}")

    # ---- blanks / counts
    def blank_mask(self, col: str) -> pd.Series:
        return self.gdf[col].map(is_blank)

    def null_count(self, col: str) -> Result:
        n = int(self.blank_mask(col).sum())
        return Result(n, "records", f"{self.label}.{col}: null/empty/whitespace; total {len(self)}", [self._ev(f"field {col}")])

    def distinct(self, col: str) -> Result:
        v = self.gdf.loc[~self.blank_mask(col), col].astype(str)
        return Result(v.nunique(), "labels", f"distinct non-blank {col} values (labels, not biological taxa)", [self._ev(f"field {col}")])

    def value_counts(self, col: str, where: pd.Series | None = None, include_blank: bool = False) -> pd.Series:
        g = self.gdf if where is None else self.gdf[where]
        v = g[col] if include_blank else g.loc[~g[col].map(is_blank), col]
        return v.astype(str).value_counts()

    def top_values(self, col: str, where: pd.Series | None = None) -> Result:
        """Most frequent value(s); ALL ties returned."""
        vc = self.value_counts(col, where)
        if vc.empty:
            return Result([], "", "no non-blank values", [])
        top = vc[vc == vc.max()]
        denom = len(self.gdf if where is None else self.gdf[where])
        return Result([{"value": k, "count": int(c), "share_pct": round_half_up(100 * c / denom)} for k, c in top.items()],
                      "records", f"{self.label}.{col}; share of {denom} selected records (blanks in denominator)", [self._ev(f"field {col}")])

    def mask_eq(self, col: str, value: str) -> pd.Series:
        return self.gdf[col].astype(str) == value

    # ---- shares across layers (pooled, never mean of percentages)
    @staticmethod
    def pooled_share(layers: list["Layer"], col: str, value: str) -> Result:
        num = sum(int((l.gdf[col].astype(str) == value).sum()) for l in layers)
        den = sum(len(l) for l in layers)
        per = {l.label: {"num": int((l.gdf[col].astype(str) == value).sum()), "den": len(l),
                         "pct": round_half_up(100 * (l.gdf[col].astype(str) == value).sum() / len(l))} for l in layers}
        return Result({"num": num, "den": den, "pct": round_half_up(100 * num / den), "per_layer": per}, "%",
                      "pooled = sum numerators / sum denominators", [l._ev(f"field {col}") for l in layers])

    def group_share_table(self, group_col: str, col: str, value: str) -> pd.DataFrame:
        g = self.gdf.assign(_hit=(self.gdf[col].astype(str) == value))
        t = g.groupby(group_col).agg(n=("_hit", "size"), k=("_hit", "sum")).reset_index()
        t["pct"] = 100 * t.k / t.n
        return t

    def eligible_group_extreme(self, group_col: str, col: str, value: str, min_n: int, max_share_pct: float | None = None,
                               pick: str = "max") -> Result:
        t = self.group_share_table(group_col, col, value)
        t = t[(t.n >= min_n)]
        if max_share_pct is not None:
            t = t[t.pct <= max_share_pct]
        if t.empty:
            return Result([], "", "no eligible group", [])
        fr = [Fraction(int(k), int(n)) for k, n in zip(t.k, t.n)]  # exact ratios: ties are real ties
        best = max(fr) if pick == "max" else min(fr)
        w = t[[f == best for f in fr]]
        return Result([{"group": r[group_col], "num": int(r.k), "den": int(r.n), "pct": round_half_up(r.pct)} for _, r in w.iterrows()],
                      "%", f"groups with n>={min_n}" + (f" and share<={max_share_pct}%" if max_share_pct is not None else ""),
                      [self._ev(f"fields {group_col}, {col}")])

    # ---- geometry in a stated CRS
    def proj(self, crs: str):
        if crs not in self._proj:
            self._proj[crs] = self.gdf.to_crs(crs) if self.gdf.crs else self.gdf
        return self._proj[crs]

    def row_by(self, col: str, value: str) -> pd.Series:
        m = self.gdf[self.gdf[col].astype(str) == str(value)]
        if len(m) != 1:
            raise KeyError(f"{len(m)} rows match {col}={value} in {self.label}")
        self._ev(f"{col}={value}")
        return m.iloc[0]

    def within_radius(self, anchor_col: str, anchor_value: str, radius_m: float, crs: str, others: list["Layer"] | None = None,
                      include_anchor: bool = True) -> Result:
        """Count points within radius (boundary included) of the anchor point, planar in `crs`, including other layers."""
        pa = self.proj(crs)
        idx = self.gdf.index[self.gdf[anchor_col].astype(str) == str(anchor_value)]
        assert len(idx) == 1, f"{len(idx)} anchors"
        ax, ay = pa.geometry.loc[idx[0]].x, pa.geometry.loc[idx[0]].y
        tot, ids, ev = 0, [], [self._ev(f"{anchor_col}={anchor_value}")]
        for L in [self] + list(others or []):
            g = L.proj(crs)
            d2 = (g.geometry.x - ax) ** 2 + (g.geometry.y - ay) ** 2
            hit = d2 <= radius_m ** 2 + 1e-9  # inclusive
            if L is self and not include_anchor:
                hit.loc[idx[0]] = False
            tot += int(hit.sum())
            ids.append(L.gdf.index[hit])
            ev.append(L._ev("geometry"))
        res = Result(tot, "records", f"planar {crs}, radius {radius_m} m inclusive, anchor {'included' if include_anchor else 'excluded'}", ev)
        res.hit_index = ids  # list of Index per layer for follow-up filtering
        return res

    def bbox_violations(self, xmin: float, xmax: float, ymin: float, ymax: float, crs: str, id_col: str) -> Result:
        g = self.proj(crs)
        out = []
        for i, geom in g.geometry.items():
            x, y = geom.x, geom.y
            dx = (xmin - x) if x < xmin else ((x - xmax) if x > xmax else 0.0)
            dy = (ymin - y) if y < ymin else ((y - ymax) if y > ymax else 0.0)
            if dx or dy:
                edges = ([("west" if x < xmin else "east")] if dx else []) + ([("south" if y < ymin else "north")] if dy else [])
                out.append({"id": self.gdf.loc[i, id_col], "edges": edges, "overshoot_m": {"x": round_half_up(dx, 3), "y": round_half_up(dy, 3)}})
        return Result(out, "points", f"inclusive box in {crs}", [self._ev(f"geometry, {id_col}")])

    def invalid_geometries(self, crs: str, id_col: str) -> Result:
        from shapely.validation import explain_validity

        g = self.proj(crs)
        bad = [{"id": self.gdf.loc[i, id_col], "reason": explain_validity(geom)} for i, geom in g.geometry.items() if geom is not None and not geom.is_valid]
        return Result(bad, "features", f"2D validity in {crs}; rows must not be dropped quietly", [self._ev("geometry")])

    def z_extent(self, id_col: str, value: str, where: pd.Series | None = None) -> Result:
        import shapely

        g = self.gdf if where is None else self.gdf[where]
        m = g[g[id_col].astype(str) == value]
        zs = [c[2] for geom in m.geometry for c in shapely.get_coordinates(geom, include_z=True)]
        if not zs:
            return Result(None, "", "no Z", [])
        return Result({"min": min(zs), "max": max(zs), "span": max(zs) - min(zs), "n_features": len(m)}, "m (source Z units)",
                      "max(Z)-min(Z) of supplied geometry", [self._ev(f"{id_col}={value}, geometry Z")])


# ---------------------------------------------------------------- stand-alone helpers
def circle_area(diameter: float) -> float:
    return math.pi * (diameter / 2) ** 2


def circle_overlap(c1, d1, c2, d2) -> float:
    """Intersection area of two discs from centres (x, y) metres and DIAMETERS; analytic (lens formula)."""
    r1, r2 = d1 / 2, d2 / 2
    d = math.dist(c1, c2)
    if d >= r1 + r2:
        return 0.0
    if d <= abs(r1 - r2):
        return math.pi * min(r1, r2) ** 2
    a1 = math.acos((d * d + r1 * r1 - r2 * r2) / (2 * d * r1))
    a2 = math.acos((d * d + r2 * r2 - r1 * r1) / (2 * d * r2))
    return r1 * r1 * a1 + r2 * r2 * a2 - 0.5 * math.sqrt((-d + r1 + r2) * (d + r1 - r2) * (d - r1 + r2) * (d + r1 + r2))


def diameter_from_circumference(c: float) -> float:
    return c / math.pi


def key_diff(a: pd.Series, b: pd.Series, strip_prefix: str | None = None, only_with_prefix: str | None = None) -> Result:
    """Exact string key comparison. Optionally strip exactly one leading prefix from `a` (restricted to keys carrying it)."""
    ak = a.astype(str)
    if only_with_prefix:
        ak = ak[ak.str.startswith(only_with_prefix)]
    if strip_prefix:
        ak = ak.map(lambda s: s[len(strip_prefix):] if s.startswith(strip_prefix) else s)
    bs, as_ = set(b.astype(str)), set(ak)
    return Result({"only_a": len(as_ - bs), "only_b": len(bs - as_), "both": len(as_ & bs)}, "distinct keys",
                  "exact string equality; a key difference is not evidence of planting/felling", [])


def namespace_counts(ids: pd.Series, pattern: str = r"^([A-Za-z0-9]+)_") -> dict:
    return ids.astype(str).map(lambda s: (re.match(pattern, s) or [None, "(none)"])[1]).value_counts().to_dict()


def timestamp_gap_seconds(a: str, b: str) -> float:
    """Absolute separation between two ISO-8601 timestamps with offsets (Z accepted)."""
    pa, pb = (dt.datetime.fromisoformat(x.replace("Z", "+00:00")) for x in (a, b))
    return abs((pa - pb).total_seconds())
