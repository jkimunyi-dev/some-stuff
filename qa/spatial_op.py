"""Spatial operation for geographic layers, wrapping the tested qa.layers tools.

Tasks: radius_count, nearest, bbox_outside, circle_overlap, z_extent, invalid_geometries.
Anchors are features whose identifier appears verbatim in the question (found by code). The projected CRS comes from
the Context/question when stated (EPSG:xxxx); planar distances; radius boundary inclusive. No pack IDs or constants.
"""
from __future__ import annotations

import math
import re

from .layers import Layer, circle_overlap
from .query import _mask, value_hits
from .tools import Recorder, round_half_up

TASKS = ["radius_count", "nearest", "bbox_outside", "circle_overlap", "z_extent", "invalid_geometries"]


def stated_crs(*texts) -> str | None:
    for t in texts:
        m = re.search(r"EPSG[:\s]*(\d{4,5})", t or "", re.I)
        if m:
            return f"EPSG:{m.group(1)}"
    return None


class SpatialError(Exception):
    pass


_LAYERS: dict = {}


import threading
_LAYER_LOCK = threading.Lock()


def layer(path) -> Layer:
    with _LAYER_LOCK:  # load each layer once even when questions run in parallel
        if path not in _LAYERS:
            _LAYERS[path] = Layer(path)
    return _LAYERS[path]


def anchor_options(question: str, geo_sources) -> list[str]:
    out = []
    for s in geo_sources:
        df = layer(s.file).gdf.drop(columns="geometry")
        out += [f"{s.sid} :: {h}" for h in value_hits(df, question)]
    return out[:20]


def _parse_anchor(a: str, by_id):
    sid, rest = a.split(" :: ", 1)
    col, val = rest.split(" = ", 1)
    return by_id[sid], col, val


def run_spatial(b: dict, by_id: dict, crs: str, rec: Recorder):
    """Returns (facts, key numbers)."""
    task = b["task"]
    facts, keys = [], []
    f2 = lambda x: f"{round_half_up(x, 2):,.2f}"
    if task in ("radius_count", "nearest", "circle_overlap", "z_extent"):
        if not b.get("anchor"):
            raise SpatialError("this task needs an anchor feature")
        s, col, val = _parse_anchor(b["anchor"], by_id)
        L = layer(s.file)
    if task == "radius_count":
        r = float(b.get("radius_m") or 0)
        if r <= 0:
            raise SpatialError("radius_m must be positive")
        others = [layer(by_id[x].file) for x in (b.get("layers") or []) if by_id[x].file != s.file]
        res = L.within_radius(col, val, r, crs, others=others)
        facts.append(f"Records within {r:g} m of {col}={val} (planar {crs}, boundary included, anchor included): {res.value}")
        keys.append(str(res.value))
        flt = b.get("filter") or {}
        if flt.get("column"):
            sel = 0
            for Lx, idx in zip([L] + others, res.hit_index):
                df = Lx.gdf.loc[idx].drop(columns="geometry")
                if flt["column"] in df.columns:
                    sel += int(_mask(df, flt).sum())
            pct = 100 * sel / res.value if res.value else 0
            facts.append(f"Of these, records with {flt['column']} {flt['op']} {flt.get('value', '')}: {sel} ({f2(pct)}% of the {res.value} selected)")
            keys += [str(sel), f2(pct)]
        rec.add(f"{L.file}, {col}={val}, geometry; radius {r:g} m in {crs}")
    elif task == "nearest":
        tgt = by_id[(b.get("layers") or [s.sid])[0]]
        T = layer(tgt.file)
        g, gt = L.proj(crs), T.proj(crs)
        i = L.gdf.index[L.gdf[col].astype(str) == val][0]
        p = g.geometry.loc[i]
        d = gt.geometry.distance(p)
        if tgt.file == s.file:
            d = d.drop(index=i)
        flt = b.get("filter") or {}
        if flt.get("column") and flt["column"] in T.gdf.columns:
            d = d[_mask(T.gdf.drop(columns="geometry").loc[d.index], flt)]
        j = d.idxmin()
        idc = next((c for c in T.gdf.columns if re.search(r"(id|nummer|number|ref|code|name)$", str(c), re.I)), T.gdf.columns[0])
        facts.append(f"Nearest to {col}={val}: {idc}={T.gdf.loc[j, idc]} at {f2(d.loc[j])} m (planar {crs})")
        keys.append(f2(d.loc[j]))
        rec.add(f"{L.file}, {col}={val}; {T.file}, {idc}={T.gdf.loc[j, idc]}")
    elif task == "bbox_outside":
        bb = b.get("bbox") or {}
        tgt = by_id[(b.get("layers") or [next(iter(by_id))])[0]]
        T = layer(tgt.file)
        idc = next((c for c in T.gdf.columns if re.search(r"(id|nummer|number|ref|code)$", str(c), re.I)), T.gdf.columns[0])
        res = T.bbox_violations(float(bb["xmin"]), float(bb["xmax"]), float(bb["ymin"]), float(bb["ymax"]), crs, idc)
        for v in res.value[:10]:
            facts.append(f"{idc}={v['id']} is outside ({', '.join(v['edges'])} edge), by x {v['overshoot_m']['x']} m, y {v['overshoot_m']['y']} m")
        facts.insert(0, f"{len(res.value)} of {len(T)} features lie outside the inclusive box in {crs}")
        keys.append(str(len(res.value)))
        rec.add(f"{T.file}, geometry vs box")
    elif task == "circle_overlap":
        s2, col2, val2 = _parse_anchor(b["second_anchor"], by_id)
        dc = b.get("diameter_column")
        if not dc:
            raise SpatialError("circle_overlap needs the diameter column")
        L2 = layer(s2.file)
        g1, g2 = L.proj(crs), L2.proj(crs)
        i1 = L.gdf.index[L.gdf[col].astype(str) == val][0]
        i2 = L2.gdf.index[L2.gdf[col2].astype(str) == val2][0]
        d1, d2 = float(L.gdf.loc[i1, dc]), float(L2.gdf.loc[i2, dc])
        p1, p2 = g1.geometry.loc[i1], g2.geometry.loc[i2]
        a = circle_overlap((p1.x, p1.y), d1, (p2.x, p2.y), d2)
        facts.append(f"Circles of diameter {d1:g} m and {d2:g} m centred {f2(p1.distance(p2))} m apart overlap by {f2(a)} m² (planar {crs})")
        keys.append(f2(a))
        rec.add(f"{L.file}, {col}={val} and {col2}={val2}, {dc}")
    elif task == "z_extent":
        res = L.z_extent(col, val)
        if res.value is None:
            raise SpatialError("no Z coordinates for that feature")
        v = res.value
        facts.append(f"{col}={val}: Z from {f2(v['min'])} to {f2(v['max'])}, span {f2(v['span'])} ({v['n_features']} feature(s))")
        keys.append(f2(v["span"]))
        rec.add(f"{L.file}, {col}={val}, geometry Z")
    elif task == "invalid_geometries":
        tgt = by_id[(b.get("layers") or [next(iter(by_id))])[0]]
        T = layer(tgt.file)
        idc = next((c for c in T.gdf.columns if re.search(r"(id|nummer|number|ref|code)$", str(c), re.I)), T.gdf.columns[0])
        res = T.invalid_geometries(crs, idc)
        facts.append(f"{len(res.value)} invalid 2D geometries in {T.file} ({crs})" + (": " + "; ".join(f"{x['id']} ({x['reason']})" for x in res.value[:5]) if res.value else ""))
        keys.append(str(len(res.value)))
        rec.add(f"{T.file}, geometry validity")
    return facts, keys
