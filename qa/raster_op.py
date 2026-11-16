"""Raster operation for categorical land-cover grids (and other single-band rasters).

Tasks: class_areas, compare, coverage.
Cells are selected by their centres inside an optional boundary feature (rasterio geometry_mask, all_touched=False),
nodata excluded, WGS84 geodesic cell areas computed per row. The class legend is parsed from the pack's text; rounding
from the Context. No pack IDs or answer constants.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np

TASKS = ["class_areas", "compare", "coverage", "value_stats"]
import threading
_RASTER_LOCK = threading.Lock()  # one big raster in memory at a time


class RasterError(Exception):
    pass


def legend(text: str) -> dict[int, str]:
    out = {}
    for code, name in re.findall(r"\b(\d{1,3})\s+([A-Z][A-Za-z/\-]+(?: (?!and\b)[a-z/\-]+){0,3})", text):
        if code not in out and len(name) > 2:
            out[int(code)] = name.strip()
    return out


def decimals(context: str, what: str, default: int = 2) -> int:
    m = re.search(rf"{what}\w*\s+to\s+(\d+(?:\.\d+)?)", context, re.I)
    if not m:
        return default
    v = m.group(1)
    return len(v.split(".")[1]) if "." in v else 0


def _cell_areas(transform, height, crs) -> np.ndarray:
    """Area (m²) of one cell in each row. Geographic grids: WGS84 geodesic; projected grids: |a*e|."""
    if crs is not None and crs.is_projected:
        return np.full(height, abs(transform.a * transform.e))
    from pyproj import Geod
    geod = Geod(ellps="WGS84")
    res_x, res_y = transform.a, -transform.e
    top = transform.f
    out = np.empty(height)
    for i in range(height):
        t, b = top - i * res_y, top - (i + 1) * res_y
        out[i] = abs(geod.polygon_area_perimeter([0, res_x, res_x, 0], [b, b, t, t])[0])
    return out


def _read(path, boundary_geom=None):
    with _RASTER_LOCK:
        return _read_unlocked(path, boundary_geom)


def _read_unlocked(path, boundary_geom=None):
    import rasterio
    from rasterio.features import geometry_mask
    with rasterio.open(path) as d:
        arr = d.read(1)
        nod = d.nodata
        valid = np.ones(arr.shape, bool) if nod is None else (arr != nod)
        if boundary_geom is not None:
            inside = geometry_mask([boundary_geom], out_shape=arr.shape, transform=d.transform, invert=True, all_touched=False)
            valid &= inside
        return arr, valid, _cell_areas(d.transform, d.height, d.crs), d


def class_areas(path, boundary_geom=None, exclude=()):
    arr, valid, row_area, d = _read(path, boundary_geom)
    for c in exclude:
        valid &= arr != c
    res = {}
    for c in np.unique(arr[valid]):
        m = valid & (arr == c)
        res[int(c)] = (int(m.sum()), float((m.sum(axis=1) * row_area).sum()) / 1e6)
    return res


def run_raster(b: dict, rasters: dict, boundary_geom, boundary_label: str, leg: dict, context: str, rec):
    task = b["task"]
    nd_a, nd_s = decimals(context, "area", 2), decimals(context, "share", 2)
    fa = lambda x: f"{round(x, nd_a):,.{nd_a}f}"
    fs = lambda x: f"{round(x, nd_s):,.{nd_s}f}"
    name = lambda c: f"{c} {leg.get(c, '')}".strip()
    excl = [int(x) for x in b.get("exclude_classes") or []]
    cls = [int(x) for x in b.get("classes") or []]
    scope = f" inside {boundary_label}" if boundary_geom is not None else " in the whole raster window"
    facts, keys = [], []
    r1 = rasters.get(b.get("raster") or "")
    if r1 is None:
        raise RasterError("choose a raster")
    if task in ("class_areas", "compare"):
        import rasterio
        with rasterio.open(r1.file) as d:
            if d.dtypes[0].startswith("float"):
                raise RasterError("this raster holds continuous values (e.g. elevation), not classes; use task value_stats")
    if task == "value_stats":
        arr, valid, ra, d = _read(r1.file, boundary_geom)
        v = arr[valid].astype("float64")
        if not v.size:
            raise RasterError("no valid cells in scope")
        facts.append(f"{r1.label}{scope}: {v.size:,} valid cells; min {v.min():.2f}, max {v.max():.2f}, mean {v.mean():.2f} (raster units)")
        keys += [f"{v.min():.2f}", f"{v.max():.2f}", f"{v.mean():.2f}"]
        rec.add(f"{r1.label}, cell values{scope}")
        del arr, valid
        return facts, keys
    if task == "class_areas":
        res = class_areas(r1.file, boundary_geom, excl)
        tot_c, tot_a = sum(v[0] for v in res.values()), sum(v[1] for v in res.values())
        facts.append(f"{r1.label}{scope} (cell centres, nodata excluded{', excluding classes ' + str(excl) if excl else ''}): "
                     f"{tot_c:,} valid cells, {fa(tot_a)} km² (geodesic)")
        for c in (cls or sorted(res)):
            n, a = res.get(c, (0, 0.0))
            facts.append(f"  class {name(c)}: {n:,} cells, {fa(a)} km², {fs(100 * a / tot_a if tot_a else 0)}% of valid area")
            keys += [fa(a), fs(100 * a / tot_a if tot_a else 0)]
        rec.add(f"{r1.label}, cell values{scope}")
    elif task == "compare":
        r2 = rasters.get(b.get("second_raster") or "")
        if r2 is None:
            raise RasterError("compare needs second_raster")
        a1, v1, ra, d1 = _read(r1.file, boundary_geom)
        a2, v2, _, d2 = _read(r2.file, boundary_geom)
        if a1.shape != a2.shape:
            raise RasterError("rasters are on different grids")
        for c in excl:
            v1 &= a1 != c
            v2 &= a2 != c
        for c in (cls or sorted(set(np.unique(a1[v1])) | set(np.unique(a2[v2])))):
            A = float(((v1 & (a1 == c)).sum(axis=1) * ra).sum()) / 1e6
            B = float(((v2 & (a2 == c)).sum(axis=1) * ra).sum()) / 1e6
            facts.append(f"class {name(c)}: {fa(A)} km² in {r1.label} vs {fa(B)} km² in {r2.label}; change {fa(B - A)} km²"
                         + (f" ({fs(100 * (B - A) / A)}% of the older figure)" if A else ""))
            keys += [fa(B - A)] + ([fs(100 * (B - A) / A)] if A else [])
        joint = v1 & v2
        diff = joint & (a1 != a2)
        ja = float((joint.sum(axis=1) * ra).sum())
        da = float((diff.sum(axis=1) * ra).sum())
        facts.append(f"Jointly valid cells{scope}: {int(joint.sum()):,}; with a different label: {int(diff.sum()):,} "
                     f"({fs(100 * da / ja if ja else 0)}% of jointly valid area)")
        keys.append(fs(100 * da / ja if ja else 0))
        rec.add(f"{r1.label} and {r2.label}, cell values{scope}")
    elif task == "coverage":
        if boundary_geom is None:
            raise RasterError("coverage needs a boundary feature")
        from pyproj import Geod
        from shapely.geometry import box
        import rasterio
        with rasterio.open(r1.file) as d:
            bb = box(*d.bounds)
        geod = Geod(ellps="WGS84")
        whole = abs(geod.geometry_area_perimeter(boundary_geom)[0])
        part = abs(geod.geometry_area_perimeter(boundary_geom.intersection(bb))[0])
        facts.append(f"{fs(100 * part / whole)}% of {boundary_label} ({fa(whole / 1e6)} km², geodesic) lies inside the {r1.label} window "
                     f"({fa(part / 1e6)} km²)")
        keys.append(fs(100 * part / whole))
        rec.add(f"{r1.label} bounds; boundary {boundary_label}")
    return facts, keys
