"""Step 2 - schema cards and guide index. Deterministic, code-computed, generic.

card_for_file(path)  -> compact text profile of one file
pack_card(source_ids, ...) -> text card for a pack (all files + guide files in full)
Usage: python -m qa.cards --data DATA --sources SRC --out cards.json
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import zipfile
from pathlib import Path

import pandas as pd

GUIDE_RE = re.compile(r"(sourceguide|readingguide|productnotes|readme|attribution|datasetmetadata|derivation|"
                      r"publisher_csw|sourcenote|metadata|levels|dictionary)", re.I)
GUIDE_EXT = {".txt", ".md", ".json", ".xml", ".html"}
GUIDE_MAX = 4000  # chars of a guide kept in the card
GTFS = {"stops", "routes", "trips", "stop_times", "calendar", "calendar_dates", "agency", "shapes",
        "pathways", "levels", "transfers"}


def _short(v, n=40):
    s = str(v)
    return s if len(s) <= n else s[: n - 1] + "…"


def is_guide(path: Path) -> bool:
    name = path.name.split("__", 1)[-1]
    return path.suffix.lower() in GUIDE_EXT and bool(GUIDE_RE.search(name)) and path.stat().st_size < 400_000


def df_card(df: pd.DataFrame, max_cols=30) -> str:
    lines = [f"rows={len(df)} cols={len(df.columns)}"]
    for c in list(df.columns)[:max_cols]:
        s = df[c].map(lambda x: json.dumps(x, default=str) if isinstance(x, (dict, list)) else x)
        nn = int((s.astype(str).str.strip() == "").sum() + s.isna().sum())
        ex = [_short(x, 25) for x in s.dropna().astype(str).unique()[:3]]
        lines.append(f"  {_short(c, 30)} | blank/null={nn} | uniq={s.nunique()} | ex={ex}")
    if len(df.columns) > max_cols:
        lines.append(f"  ... {len(df.columns) - max_cols} more columns")
    return "\n".join(lines)


def read_csv_any(p: Path, nrows=None) -> pd.DataFrame:
    for enc in ("utf-8-sig", "latin-1"):
        try:
            first = p.open(encoding=enc).readline()
            sep = ";" if first.count(";") > first.count(",") else ","
            return pd.read_csv(p, sep=sep, dtype=str, encoding=enc, nrows=nrows, on_bad_lines="skip")
        except Exception:  # noqa: BLE001
            continue
    raise ValueError("csv unreadable")


def card_xlsx(p: Path) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(p, read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        out.append(f"sheet {ws.title!r} {ws.max_row}x{ws.max_column}")
        for i, row in enumerate(ws.iter_rows(max_row=4, values_only=True), 1):
            cells = [_short(c, 18) for c in row[:10] if c is not None]
            if cells:
                out.append(f"  r{i}: {cells}")
    return "\n".join(out)


def card_ods(p: Path) -> str:
    import xml.etree.ElementTree as ET

    t = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
    tx = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
    with zipfile.ZipFile(p) as z:
        root = ET.fromstring(z.read("content.xml"))
    out = []
    for tbl in root.iter(t + "table"):
        rows = [r for r in tbl.iter(t + "table-row")]
        out.append(f"sheet {tbl.get(t + 'name')!r} ~{len(rows)} rows")
        for i, r in enumerate(rows[:3], 1):
            vals = ["".join("".join(pp.itertext()) for pp in c.findall(tx + "p")) for c in r][:8]
            vals = [_short(v, 18) for v in vals if v]
            if vals:
                out.append(f"  r{i}: {vals}")
    return "\n".join(out)


def card_vector(p: Path) -> str:
    import pyogrio

    out = []
    for name, geom in pyogrio.list_layers(p):
        info = pyogrio.read_info(p, layer=name)
        out.append(f"layer {name!r} geom={geom} features={info['features']} crs={info.get('crs')}")
        df = pyogrio.read_dataframe(p, layer=name, max_features=200, read_geometry=False)
        out.append(df_card(df))
    return "\n".join(out)


def card_json(p: Path) -> str:
    if p.stat().st_size > 40_000_000:
        return f"large json {p.stat().st_size:,} B (not profiled)"
    obj = json.loads(p.read_text(encoding="utf-8-sig"))

    def walk(o, depth=0):
        pad = "  " * depth
        if isinstance(o, dict):
            return "\n".join(f"{pad}{_short(k, 30)}: {walk(v, depth + 1) if depth < 2 and isinstance(v, (dict, list)) else _short(v, 50)}"
                             for k, v in list(o.items())[:14])
        if isinstance(o, list):
            return f"list[{len(o)}] first=" + (walk(o[0], depth + 1) if o and depth < 2 else "")
        return _short(o)

    return walk(obj)


def card_tif(p: Path) -> str:
    import rasterio

    with rasterio.open(p) as d:
        return (f"raster {d.width}x{d.height} bands={d.count} dtype={d.dtypes[0]} crs={d.crs} nodata={d.nodata} "
                f"res={d.res} bounds={tuple(round(b, 4) for b in d.bounds)}")


def card_laz(p: Path) -> str:
    import laspy

    with laspy.open(p) as f:
        h = f.header
        return f"points={h.point_count} dims={[d.name for d in h.point_format.dimensions][:14]} mins={list(h.mins)} maxs={list(h.maxs)}"


def card_sqlite(p: Path) -> str:
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    out = []
    for (name,) in con.execute("select name from sqlite_master where type='table'").fetchall():
        cols = [r[1] for r in con.execute(f"pragma table_info([{name}])")]
        n = con.execute(f"select count(*) from [{name}]").fetchone()[0]
        out.append(f"table {name}: rows={n} cols={cols[:15]}")
    return "\n".join(out)


def card_pdf(p: Path) -> str:
    from pypdf import PdfReader

    r = PdfReader(str(p))
    t = (r.pages[0].extract_text() or "")[:200].replace("\n", " ")
    return f"pdf pages={len(r.pages)} p1={t!r}"


def card_ifc(p: Path) -> str:
    try:
        import ifcopenshell

        m = ifcopenshell.open(str(p))
        return f"ifc schema={m.schema} " + " ".join(f"{t}={len(m.by_type(t))}" for t in ("IfcSpace", "IfcWall", "IfcSlab", "IfcBuildingStorey"))
    except ImportError:
        txt = p.read_text(errors="replace")
        return "ifc (ifcopenshell missing) " + " ".join(f"{t}={len(re.findall(t.upper() + '\\(', txt))}" for t in ("IfcSpace", "IfcWall", "IfcSlab"))


def card_for_file(path: Path) -> str:
    ext, stem = path.suffix.lower(), path.name.split("__", 1)[-1].rsplit(".", 1)[0]
    try:
        if ext == ".csv" or (ext == ".txt" and stem in GTFS):
            return df_card(read_csv_any(path))
        if ext == ".xlsx":
            return card_xlsx(path)
        if ext == ".ods":
            return card_ods(path)
        if ext in (".geojson", ".gpkg", ".gml"):
            return card_vector(path)
        if ext == ".json":
            return card_json(path)
        if ext == ".tif":
            return card_tif(path)
        if ext == ".laz":
            return card_laz(path)
        if ext == ".sqlite":
            return card_sqlite(path)
        if ext == ".pdf":
            return card_pdf(path)
        if ext == ".ifc":
            return card_ifc(path)
        if ext == ".zip":
            return "zip members: " + ", ".join(zipfile.ZipFile(path).namelist()[:10])
        if ext in (".jpg", ".png"):
            return "image (needs OCR)"
        return f"{ext} {path.stat().st_size:,} B"
    except Exception as e:  # noqa: BLE001
        return f"card failed: {type(e).__name__}: {str(e)[:100]}"


def guide_text(path: Path) -> str:
    t = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() in (".html", ".xml"):
        t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"[ \t]+", " ", t).strip()[:GUIDE_MAX]


def pack_card(source_ids: list[str], sources_dir: Path, manifest: pd.DataFrame) -> dict:
    by_id = manifest.set_index("Source_ID")
    data_cards, guides = [], []
    for sid in source_ids:
        name = by_id.loc[sid, "Original_File"]
        path = sources_dir / Path(by_id.loc[sid, "File"]).name
        if is_guide(path):
            guides.append(f"[{sid} {name}]\n{guide_text(path)}")
        else:
            data_cards.append(f"[{sid} {name}]\n{card_for_file(path)}")
    return {"source_ids": source_ids, "files": data_cards, "guides": guides}


def build_cards(data_dir: Path, sources_dir: Path) -> dict:
    manifest = pd.read_csv(data_dir / "SourceManifest.csv", keep_default_na=False)
    packs = {}
    for name in ("Train", "Test"):
        df = pd.read_csv(data_dir / f"{name}.csv", keep_default_na=False)
        for case, g in df.groupby("Case_ID"):
            ids = sorted({s for x in g.Source_IDs for s in json.loads(x)})
            packs[f"{name}:{case}"] = {"n_questions": len(g), "domain": g.Domain.iloc[0], **pack_card(ids, sources_dir, manifest)}
    return packs


def card_text(pack: dict) -> str:
    return "\n\n".join(pack["files"] + pack["guides"])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--sources", required=True)
    ap.add_argument("--out", default="cards.json")
    a = ap.parse_args()
    packs = build_cards(Path(a.data), Path(a.sources))
    Path(a.out).write_text(json.dumps(packs, indent=1, ensure_ascii=False), encoding="utf-8")
    sizes = sorted(((len(card_text(p)) // 4, k) for k, p in packs.items()), reverse=True)
    print(f"packs={len(packs)} | approx tokens: max={sizes[0][0]} median={sizes[len(sizes)//2][0]}")
    print("largest:", sizes[:5])
