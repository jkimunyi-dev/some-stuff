"""Step 1 - inventory: parse-probe every source file and write inventory.csv.

Generic: no question IDs, pack IDs or answer constants. Works on Colab, Linux and Windows.
Usage:  python -m qa.inventory --data DATA_DIR --sources SOURCES_DIR --out inventory.csv
DATA_DIR holds Train.csv, Test.csv, SourceManifest.csv; SOURCES_DIR holds SRC_<id>__<name> files.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
import zipfile
from pathlib import Path

import pandas as pd

BIG_JSON = 40_000_000  # do not fully parse larger JSON; peek only
TEXT_EXT = {".txt", ".md", ".csv", ".json", ".geojson", ".xml", ".html", ".xsd", ".gml"}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


# ----------------------------------------------------------------- probes
# each probe returns (status, detail, notes); status in parsed|partial|unsupported


def probe_csv(p: Path):
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            sep = ";" if p.open(encoding=enc).readline().count(";") > p.open(encoding=enc).readline().count(",") else ","
            df = pd.read_csv(p, sep=sep, dtype=str, encoding=enc, keep_default_na=False, on_bad_lines="skip")
            return "parsed", f"rows={len(df)} cols={len(df.columns)} sep={sep!r} enc={enc}", "cols: " + ",".join(map(str, df.columns[:15]))
        except Exception as e:  # noqa: BLE001
            err = str(e)[:80]
    return "unsupported", "", f"csv read failed: {err}"


def probe_xlsx(p: Path):
    from openpyxl import load_workbook

    wb = load_workbook(p, read_only=True, data_only=True)
    sheets = ", ".join(f"{ws.title}({ws.max_row}x{ws.max_column})" for ws in wb.worksheets)
    return "parsed", f"sheets={len(wb.worksheets)}", sheets[:300]


def probe_ods(p: Path):
    import xml.etree.ElementTree as ET

    ns = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
    with zipfile.ZipFile(p) as z:
        root = ET.fromstring(z.read("content.xml"))
    names = [t.get(ns + "name") for t in root.iter(ns + "table")]
    return "parsed", f"sheets={len(names)}", ", ".join(names)[:300]


def probe_vector(p: Path):
    import pyogrio

    layers = pyogrio.list_layers(p)
    parts, crs = [], ""
    total = 0
    for name, _geom in layers:
        info = pyogrio.read_info(p, layer=name)
        total += int(info["features"])
        crs = crs or str(info.get("crs"))
        parts.append(f"{name}:{info['features']}")
    return "parsed", f"layers={len(layers)} features={total} crs={crs}", " ".join(parts)[:300]


def probe_json(p: Path):
    size = p.stat().st_size
    if size > BIG_JSON:
        head = p.read_text(encoding="utf-8", errors="replace")[:300].replace("\n", " ")
        return "partial", f"bytes={size}", f"too big to fully parse; head: {head}"
    obj = json.loads(p.read_text(encoding="utf-8-sig"))
    if isinstance(obj, dict):
        return "parsed", f"dict keys={len(obj)}", "keys: " + ",".join(list(obj)[:12])
    return "parsed", f"list len={len(obj)}", ""


def probe_text(p: Path):
    t = p.read_text(encoding="utf-8", errors="replace")
    return "parsed", f"chars={len(t)} lines={t.count(chr(10)) + 1}", t[:120].replace("\n", " ")


def probe_xml(p: Path):
    from lxml import etree

    root = etree.parse(str(p), etree.XMLParser(huge_tree=True, recover=True)).getroot()
    return "parsed", f"root={etree.QName(root).localname} children={len(root)}", ""


def probe_pdf(p: Path):
    from pypdf import PdfReader

    r = PdfReader(str(p))
    n = len(r.pages)
    chars = sum(len((r.pages[i].extract_text() or "").strip()) for i in range(min(n, 5)))
    scanned = chars < 50 * min(n, 5)
    status = "partial" if scanned else "parsed"
    return status, f"pages={n} text_chars_first5={chars} scanned={scanned}", "needs OCR (tesseract)" if scanned else ""


def probe_docx(p: Path):
    import re

    with zipfile.ZipFile(p) as z:
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    return "parsed", f"chars={len(re.sub(r'<[^>]+>', '', xml))}", ""


def probe_tif(p: Path):
    import rasterio

    with rasterio.open(p) as d:
        return "parsed", f"{d.width}x{d.height} bands={d.count} dtype={d.dtypes[0]} crs={d.crs}", f"nodata={d.nodata}"


def probe_laz(p: Path):
    import laspy

    with laspy.open(p) as f:
        h = f.header
        return "parsed", f"points={h.point_count} version={h.version}", f"crs={h.parse_crs()}"[:200]


def probe_ifc(p: Path):
    try:
        import ifcopenshell

        m = ifcopenshell.open(str(p))
        return "parsed", f"schema={m.schema} spaces={len(m.by_type('IfcSpace'))}", ""
    except ImportError:
        head = p.read_text(encoding="utf-8", errors="replace")
        return "partial", f"lines={head.count(chr(10))}", "ifcopenshell missing: text-only (pip install ifcopenshell)"


def probe_sqlite(p: Path):
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    tabs = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
    counts = []
    for t in tabs[:30]:
        try:
            counts.append(f"{t}:{con.execute(f'select count(*) from [{t}]').fetchone()[0]}")
        except sqlite3.Error:
            counts.append(f"{t}:?")
    con.close()
    return "parsed", f"tables={len(tabs)}", " ".join(counts)[:300]


def probe_image(p: Path):
    from PIL import Image

    with Image.open(p) as im:
        return "partial", f"{im.size[0]}x{im.size[1]} {im.mode}", "image: needs OCR (tesseract)"


def probe_zip(p: Path):
    with zipfile.ZipFile(p) as z:
        names = z.namelist()
    return "parsed", f"members={len(names)}", ", ".join(names[:8])[:300]


PROBES = {
    ".csv": probe_csv, ".xlsx": probe_xlsx, ".ods": probe_ods,
    ".geojson": probe_vector, ".gpkg": probe_vector, ".gml": probe_vector,
    ".json": probe_json, ".txt": probe_text, ".md": probe_text, ".html": probe_text,
    ".xml": probe_xml, ".xsd": probe_xml, ".pdf": probe_pdf, ".docx": probe_docx,
    ".tif": probe_tif, ".laz": probe_laz, ".ifc": probe_ifc, ".sqlite": probe_sqlite,
    ".jpg": probe_image, ".png": probe_image, ".zip": probe_zip,
}
GTFS_TXT = {"stops", "routes", "trips", "stop_times", "calendar", "calendar_dates", "agency", "shapes",
            "pathways", "levels", "transfers"}


def probe(path: Path):
    ext = path.suffix.lower()
    fn = PROBES.get(ext)
    stem = path.name.split("__", 1)[-1].rsplit(".", 1)[0]
    if ext == ".txt" and stem in GTFS_TXT:
        fn = probe_csv  # GTFS tables are CSV
    if fn is None:
        return "unsupported", "", f"no reader for {ext}"
    try:
        return fn(path)
    except Exception as e:  # noqa: BLE001
        return "unsupported", "", f"{type(e).__name__}: {str(e)[:150]}"


# ----------------------------------------------------------------- driver
def build_inventory(data_dir: Path, sources_dir: Path, verify_hash: bool = True) -> pd.DataFrame:
    manifest = pd.read_csv(data_dir / "SourceManifest.csv", keep_default_na=False)
    refs = {"Train": {}, "Test": {}}
    for name in ("Train", "Test"):
        for s in pd.read_csv(data_dir / f"{name}.csv", keep_default_na=False).Source_IDs:
            for sid in json.loads(s):
                refs[name][sid] = refs[name].get(sid, 0) + 1
    rows = []
    for m in manifest.itertuples():
        path = sources_dir / Path(m.File).name
        row = {"source_id": m.Source_ID, "original_file": m.Original_File, "ext": Path(m.Original_File).suffix.lower(),
               "source_set": m.Source_Set, "bytes": int(m.Bytes),
               "train_q_refs": refs["Train"].get(m.Source_ID, 0), "test_q_refs": refs["Test"].get(m.Source_ID, 0)}
        if not path.exists():
            rows.append({**row, "file_ok": "missing", "status": "unsupported", "detail": "", "notes": "file missing"})
            continue
        ok = "size_mismatch" if path.stat().st_size != row["bytes"] else "ok"
        if ok == "ok" and verify_hash:
            ok = "ok" if sha256(path) == m.SHA256 else "hash_mismatch"
        t0 = time.time()
        status, detail, notes = probe(path)
        rows.append({**row, "file_ok": ok, "status": status, "detail": detail, "notes": notes,
                     "probe_s": round(time.time() - t0, 2)})
    return pd.DataFrame(rows)


def summarize(inv: pd.DataFrame) -> str:
    out = [f"files={len(inv)}  file_ok={inv.file_ok.value_counts().to_dict()}  status={inv.status.value_counts().to_dict()}"]
    w = inv.assign(w=inv.test_q_refs + inv.train_q_refs)
    tot = w.w.sum()
    out.append("question-weighted parsed share: %.1f%%  (parsed+partial: %.1f%%)" % (
        100 * w[w.status == "parsed"].w.sum() / tot, 100 * w[w.status != "unsupported"].w.sum() / tot))
    bad = inv[inv.status != "parsed"][["source_id", "original_file", "status", "notes", "test_q_refs"]]
    out.append("\nNOT fully parsed:\n" + (bad.to_string(index=False) if len(bad) else "none"))
    return "\n".join(out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--sources", required=True)
    ap.add_argument("--out", default="inventory.csv")
    ap.add_argument("--no-hash", action="store_true")
    a = ap.parse_args()
    inv = build_inventory(Path(a.data), Path(a.sources), verify_hash=not a.no_hash)
    inv.to_csv(a.out, index=False)
    print(summarize(inv))
