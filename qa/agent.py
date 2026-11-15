"""Question answering: the model chooses, code computes.

  call 1  (plan)     model picks an operation and the source(s) from enumerated lists       [JSON schema enforced]
  call 2  (gloss)    only if labels are non-English: model glosses them into English         [JSON schema enforced]
  call 3  (bind)     model picks entities/periods/columns/keys from the chosen source's real labels
  code               resolves labels -> cells/records, reads stored or displayed values per Context, computes
  call 4  (write)    model writes the answer from the computed facts; code checks every key number survived,
                     else falls back to a deterministic sentence built from the facts.
Retries feed the validation error back to the model. Every call goes through qa.llm (15 calls / 12k tokens per question).
Generic: no pack IDs, question IDs or answer constants.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from .cards import guide_text, is_guide
from .llm import LLM, BudgetExceeded, Ledger
from .score import numbers
from .tables import Table, clean_name, sheet_text, workbook_tables
from .transit_op import MODELS as TRANSIT_MODELS, TASKS as TRANSIT_TASKS, TransitError, feed_for, run_transit, stop_options, trip_options
from .gtfs import LOCATION_TYPES
from .spatial_op import TASKS as SPATIAL_TASKS, SpatialError, anchor_options, run_spatial, stated_crs
from .query import AGGS, FILTER_OPS, QueryError, json_records, profile, run_query, sheet_records, value_hits
from .textqa import TEXT_SUFFIXES, Index
from .tools import Ods, Recorder, Xlsx, days_between, read_table, round_half_up

CRS = {"British National Grid (EPSG:27700)": "EPSG:27700", "WGS 84 longitude/latitude (EPSG:4326)": "EPSG:4326",
       "Web Mercator (EPSG:3857)": "EPSG:3857", "Lambert-93 France (EPSG:2154)": "EPSG:2154",
       "ETRS89 / UTM zone 33N (EPSG:25833)": "EPSG:25833", "ETRS89 / UTM zone 32N (EPSG:25832)": "EPSG:25832",
       "Swiss CH1903+ / LV95 (EPSG:2056)": "EPSG:2056", "ETRS89 / LAEA Europe (EPSG:3035)": "EPSG:3035",
       "Irish Transverse Mercator (EPSG:2157)": "EPSG:2157", "GDA94 / MGA zone 55 (EPSG:28355)": "EPSG:28355"}
AREA_UNITS = {"hectares": 1e4, "square metres": 1.0, "square kilometres": 1e6, "acres": 4046.8564224}

OPS = {
    "value": "look up the value(s) of named item(s) in given year(s)/period(s) of a table",
    "change": "HOW MUCH an item rose/fell/changed FROM one period TO another (difference and percent change); with several items also says which changed most",
    "percent_of": "the SAME item in a later period expressed AS A PERCENTAGE OF ITS OWN earlier period (e.g. 'journeys in 2025 as a percentage of 2019', 'how close to its 2019 level')",
    "share": "what share/percentage OF A TOTAL one part makes up IN ONE period (e.g. 'what share of total energy use in 2024 was residential', 'compare two cities' residential shares')",
    "ratio": "divide one total by another, possibly from two different tables (e.g. 'average distance per journey' = passenger-kilometres / journeys)",
    "record_lookup": "find ONE record by an identifier mentioned in the question (e.g. an order or zone reference) and report its fields; optionally days between two of its dates",
    "count_blank": "how many records have an empty/missing value in a column, and the total number of records",
    "table_query": "count / sum / average / min / max / distinct / duplicates over the RECORDS of a table, optionally filtered (e.g. value > 0, category = X) and grouped (e.g. per street, per type, top N)",
    "compare_keys": "compare identifier values between two files (values present in only one file, duplicates in each)",
    "area": "area of one polygon identified in the question, computed in a stated coordinate system",
    "transit": "public-transport timetable (GTFS) logic: trips active on a date (calendar exceptions), modelled walking route times through station pathways (step-free / without lifts), nearest entrance to a platform, a trip's time at a stop as local date and UTC, wheelchair access of a stop and its parent",
    "spatial": "distances between mapped features: how many features lie within N metres of a named feature, the nearest feature, features outside a coordinate box, overlap of two crown circles, height (Z) span of a 3D feature, invalid geometries",
    "explain": "the question asks whether the data SUPPORTS something or what a symbol/flag means (e.g. whether '[z]' entries can be treated as zero); no calculation",
    "text_answer": "the answer is stated in words in a report, guide, readme or metadata document (e.g. publisher, licence, definitions, survey findings, caveats)",
}
EXAMPLES = """EXAMPLES (question -> operation):
- "How much did X's consumption rise from 2020 to 2022, in GWh and percent?" -> change
- "Which of A, B and C grew most from 2019 to 2021?" -> change
- "What share of X's total in 2021 was transport?" -> share
- "Compare X in 2024 with 2018 as a percentage of the earlier year." -> percent_of
- "Average load per trip from the displayed tonne-km and trip totals?" -> ratio
- "What were the start and end dates of record R17, and how many days apart?" -> record_lookup
- "What is the area of polygon P-4 in hectares?" -> area
- "How many trips run on 3 March after exceptions?" / "Fastest step-free route from entrance E to platform 2?" -> transit
- "How many trees lie within 20 m of tree T-9, including it?" / "Which entrance is closest to platform 2?" -> spatial
- "How many rows have no end_date?" -> count_blank
- "How many trees have height 0, and which genus is most common on street S?" -> table_query
- "What is the total cost of the areas of type T, and how many records are there per category?" -> table_query
- "Can we treat the '[x]' entries as zero?" -> explain
- "Which licence does the publisher metadata give, and who publishes it?" -> text_answer"""


# ------------------------------------------------------------------ sources
@dataclass
class Source:
    sid: str
    kind: str  # table | csv | geo | text
    file: Path
    desc: str
    table: Table | None = None
    columns: list[str] = field(default_factory=list)
    sheet: str | None = None  # record table read from a workbook sheet
    json_path: str | None = None  # record table read from a JSON array


def _geo_fields(p: Path):
    import pyogrio
    info = pyogrio.read_info(p)
    return list(info["fields"]), str(info.get("crs") or ""), int(info.get("features") or 0), str(info.get("geometry_type") or "")


_SRC_CACHE: dict = {}
_IDX_CACHE: dict = {}


def text_index(files: list[Path]) -> Index:
    key = tuple(sorted(map(str, files)))
    if key not in _IDX_CACHE:  # one index per pack, reused by every question of that pack
        _IDX_CACHE[key] = Index(files)
    return _IDX_CACHE[key]


def build_sources(files: list[Path]) -> list[Source]:
    key = tuple(sorted(map(str, files)))
    if key not in _SRC_CACHE:
        _SRC_CACHE[key] = _build_sources(files)
    return _SRC_CACHE[key]


def _build_sources(files: list[Path]) -> list[Source]:
    out: list[Source] = []

    def add(kind, f, desc, **kw):
        out.append(Source(f"S{len(out) + 1}", kind, f, desc, **kw))

    for f in sorted(files, key=lambda p: clean_name(p).lower()):
        suf, name = f.suffix.lower(), clean_name(f)
        try:
            if suf in (".xlsx", ".ods"):
                tabs_here = workbook_tables(f)
                for t in tabs_here:
                    add("table", f, t.describe(), table=t)
                with_tables = {t.sheet for t in tabs_here}
                try:
                    sheets = pd.ExcelFile(f).sheet_names
                except Exception:
                    sheets = []
                for sh in sheets:  # sheets without a period layout may still be record tables with a header row
                    if sh in with_tables or SKIP_REC_SHEET.search(sh):
                        continue
                    try:
                        df, hr = sheet_records(f, sh)
                    except Exception:
                        df = None
                    if df is not None and len(df) >= 3:
                        _DF_CACHE[(f, sh)] = df
                        add("csv", f, f"[{name} | sheet {sh}] record table, {len(df)} rows, columns: {', '.join(map(str, df.columns[:25]))}",
                            columns=[str(c) for c in df.columns], sheet=sh)
            elif suf in (".csv", ".tsv") or (suf == ".txt" and not is_guide(f) and f.stat().st_size < 50_000_000 and "," in f.open(encoding="utf-8-sig", errors="ignore").readline()):
                df = read_table(f, nrows=200)
                wkt = r"^\s*((?:MULTI)?(?:POINT|POLYGON|LINESTRING))"
                pts = next((c for c in df.columns if df[c].astype(str).str.match(wkt, case=False).any()), None)
                gtyp = df[pts].astype(str).str.extract(wkt, flags=re.I)[0].dropna().iloc[0].lower() if pts else ""
                kind = "CSV" + (f" with {gtyp} geometries as WKT in column {pts}" if pts else "")
                add("csv", f, f"[{name}] {kind} table with {len(df.columns)} columns: {', '.join(df.columns[:25])}", columns=list(df.columns))
            elif suf in (".geojson", ".gpkg", ".shp", ".gml"):
                flds, crs, n, gt = _geo_fields(f)
                fmt = {".gpkg": "GeoPackage", ".geojson": "GeoJSON", ".shp": "Shapefile", ".gml": "GML"}[suf]
                add("geo", f, f"[{name}] {fmt} geographic layer of {gt or '?'} geometries, {n} features, CRS {crs or '?'}, fields: {', '.join(flds[:25])}", columns=flds)
            elif suf == ".json" and f.stat().st_size < 200_000_000:
                try:
                    df, jp = json_records(f)
                except Exception:
                    df, jp = None, None
                if df is not None and len(df) >= 3 and len(df.columns) >= 2:
                    _DF_CACHE[(f, jp)] = df
                    add("csv", f, f"[{name}] JSON records at {jp}: {len(df)} records, columns: {', '.join(map(str, df.columns[:25]))}",
                        columns=[str(c) for c in df.columns], json_path=jp)
                add("text", f, f"[{name}] JSON document (metadata/text)")
            elif is_guide(f) or suf in TEXT_SUFFIXES:
                snippet = re.sub(r"\s+", " ", guide_text(f))[:300] if is_guide(f) else ""
                add("text", f, f"[{name}] document/notes" + (f": {snippet}" if snippet else ""))
        except Exception as e:
            if suf in TEXT_SUFFIXES:  # e.g. a guide whose first line contains a comma: index it as text instead
                add("text", f, f"[{name}] document/notes")
            else:
                add("text", f, f"[{name}] (could not be read as data: {type(e).__name__})")
    return out


# ------------------------------------------------------------------ helpers
class Fail(Exception):
    pass


def _fmt(v, nd=2) -> str:
    return f"{round_half_up(v, nd):,.{nd}f}"


def _num(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _enum_schema(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or list(props)}


def _ask_json(llm: LLM, led: Ledger, msgs: list[dict], schema: dict, tag: str, validate, tries: int = 3, max_tokens: int = 600, think: bool = False):
    """Call with an enforced JSON schema, parse, validate; feed errors back. Returns validated value."""
    last = None
    for k in range(tries):
        out = llm.chat(msgs, led, think=think, profile="precise" if think else "extract", max_tokens=max_tokens + (800 if think else 0),
                       json_mode=schema, tag=f"{tag}{k}")
        try:
            obj = json.loads(out["text"])
            return validate(obj)
        except (Fail, ValueError, KeyError, TypeError) as e:
            last = f"{type(e).__name__}: {e}"
            msgs = msgs + [{"role": "assistant", "content": out["text"]}, {"role": "user", "content": f"That is invalid: {last}. Answer again with corrected JSON."}]
    raise Fail(f"{tag} failed after {tries} tries: {last}")


_DF_CACHE: dict = {}
SKIP_REC_SHEET = re.compile(r"cover|contents|^notes?$|about|readme|metadata", re.I)


def load_df(s: Source) -> pd.DataFrame:
    if s.sheet or s.json_path:
        key = (s.file, s.sheet or s.json_path)
        if key not in _DF_CACHE:
            _DF_CACHE[key] = sheet_records(s.file, s.sheet)[0] if s.sheet else json_records(s.file)[0]
        return _DF_CACHE[key]
    if s.file not in _DF_CACHE:
        if s.kind == "geo":
            import geopandas as gpd
            _DF_CACHE[s.file] = pd.DataFrame(gpd.read_file(s.file).drop(columns="geometry"))
        else:
            _DF_CACHE[s.file] = read_table(s.file)
    return _DF_CACHE[s.file]


def id_candidates(question: str, s: Source, limit: int = 15) -> list[str]:
    """'column = value' pairs whose value occurs verbatim (as a whole token) in the question. Code finds them; the model picks."""
    df, q, out = load_df(s), question, []
    for c in df.columns:
        for v in set(df[c].dropna().astype(str).str.strip()):
            if len(v) < 3 or v not in q or not re.search(r"\d", v):
                continue
            i = q.find(v)
            tok = lambda ch: ch.isalnum() or ch in "/-_"  # an identifier embedded in a longer token is not a match
            if (i > 0 and tok(q[i - 1])) or (i + len(v) < len(q) and tok(q[i + len(v)])):
                continue
            out.append(f"{c} = {v}")
    out.sort(key=lambda x: -len(x.split(" = ", 1)[1]))
    return out[:limit]


# ------------------------------------------------------------------ executor
class Exec:
    def __init__(self, context: str):
        self.rec = Recorder()
        self.displayed = bool(re.search(r"visibly formatted|as displayed|displayed (value|number)|as shown|visible (value|number)", context, re.I))
        self._books = {}

    def cell(self, t: Table, entity: str, period: str):
        a1 = t.addr.get((entity, period))
        if a1 is None:
            raise Fail(f"no cell for {entity!r} in {period!r}")
        if t.file not in self._books:
            self._books[t.file] = Ods(t.file, self.rec) if t.file.suffix.lower() == ".ods" else Xlsx(t.file, self.rec)
        v = self._books[t.file].cell(t.sheet, a1, shown=self.displayed)
        return v, a1

    def num(self, t, e, p):
        v, a1 = self.cell(t, e, p)
        n = _num(v)
        if n is None:
            raise Fail(f"{e} in {p} is {v!r} (cell {a1}), not a number")
        return n, a1


def _strip_cells(s: str) -> str:
    s = re.sub(r"\s*\(cells? [A-Z]{1,3}\d+\)", "", s)
    s = re.sub(r";\s*cells? [A-Z]{1,3}\d+", "", s)
    return s.rstrip(".") + "."


def _label(e: str, gloss: dict) -> str:
    parts = [gloss.get(x, x) for x in e.split(" / ")]
    return " / ".join(parts)


def run_table_op(op, b, srcs, ex: Exec, gloss: dict):
    """Returns (facts list[str], key numbers list[str])."""
    t = srcs[0].table
    unit = t.unit or ""
    facts, keys = [], []
    L = lambda e: _label(e, gloss)
    if op == "value":
        for e in b["entities"]:
            for p in b["periods"]:
                v, a1 = ex.cell(t, e, p)
                n = _num(v)
                s = _fmt(n) if n is not None and not float(n).is_integer() else (f"{int(n):,}" if n is not None else repr(v))
                facts.append(f"{L(e)} in {p}: {s} {unit} (cell {a1})")
                if n is not None:
                    keys.append(s)
    elif op in ("change", "percent_of"):
        f, to = b["period_from"], b["period_to"]
        rows = []
        for e in b["entities"]:
            v0, a0 = ex.num(t, e, f)
            v1, a1 = ex.num(t, e, to)
            if op == "change":
                d, pc = v1 - v0, (v1 - v0) / v0 * 100
                rows.append((e, d, pc))
                facts.append(f"{L(e)}: {_fmt(v0)} in {f} (cell {a0}) -> {_fmt(v1)} in {to} (cell {a1}); change {_fmt(d)} {unit}, {_fmt(pc)}%")
                keys += [_fmt(d), _fmt(pc)]
            else:
                r = v1 / v0 * 100
                rows.append((e, r, None))
                facts.append(f"{L(e)}: {to} value {_fmt(v1, 0) if v1.is_integer() else _fmt(v1)} (cell {a1}) is {_fmt(r)}% of the {f} value {_fmt(v0, 0) if v0.is_integer() else _fmt(v0)} (cell {a0})")
                keys.append(_fmt(r))
        if len(rows) > 1:
            if op == "change":
                bd = max(rows, key=lambda x: x[1])
                bp = max(rows, key=lambda x: x[2])
                facts.append(f"Largest absolute change: {L(bd[0])} ({_fmt(bd[1])} {unit}). Largest percentage change: {L(bp[0])} ({_fmt(bp[2])}%).")
            else:
                close = min(rows, key=lambda x: abs(x[1] - 100))
                facts.append(f"Closest to the earlier level (100%): {L(close[0])} at {_fmt(close[1])}%.")
    elif op == "share":
        p = b["period"]
        res = []
        for pr in b["pairs"]:
            vp, ap = ex.num(t, pr["part"], p)
            vw, aw = ex.num(t, pr["whole"], p)
            s = vp / vw * 100
            res.append((pr["part"], s))
            facts.append(f"{L(pr['part'])} = {_fmt(vp)} {unit} (cell {ap}) is {_fmt(s)}% of {L(pr['whole'])} = {_fmt(vw)} {unit} (cell {aw}) in {p}")
            keys.append(_fmt(s))
        if len(res) == 2:
            hi, lo = max(res, key=lambda x: x[1]), min(res, key=lambda x: x[1])
            gap = round_half_up(hi[1], 10) - round_half_up(lo[1], 10)
            facts.append(f"{L(hi[0])} is higher by {_fmt(gap)} percentage points.")
            keys.append(_fmt(gap))
    elif op == "ratio":
        tn, td = srcs[0].table, srcs[-1].table
        vn, an = ex.num(tn, b["numerator"]["entity"], b["numerator"]["period"])
        vd, ad = ex.num(td, b["denominator"]["entity"], b["denominator"]["period"])
        r = (vn * tn.scale) / (vd * td.scale)
        facts.append(f"{L(b['numerator']['entity'])} {b['numerator']['period']}: {vn:g} ({tn.unit}, {tn.title[:80]}; cell {an}) divided by "
                     f"{L(b['denominator']['entity'])} {b['denominator']['period']}: {vd:g} ({td.unit}, {td.title[:80]}; cell {ad}) = {_fmt(r)} (unit scales applied: {tn.scale:g} / {td.scale:g})")
        keys.append(_fmt(r))
    return facts, keys


def run_record_op(op, b, srcs, ex: Exec):
    facts, keys = [], []
    s = srcs[0]
    df = load_df(s)
    name = clean_name(s.file) + (f" sheet {s.sheet}" if s.sheet else "") + (f" {s.json_path}" if s.json_path else "")
    if op == "record_lookup":
        kc, kv = b["key_column"], str(b["key_value"]).strip()
        m = df[df[kc].astype(str).str.strip() == kv]
        if len(m) == 0:
            m = df[df[kc].astype(str).str.strip().str.lower() == kv.lower()]
        if len(m) != 1:
            raise Fail(f"{len(m)} records where {kc} == {kv!r}")
        r = m.iloc[0]
        flds = list(dict.fromkeys(b.get("fields", []) + b.get("days_between", [])))
        for fcol in flds:
            v = r[fcol]
            pretty = v
            try:
                d = pd.to_datetime(v, dayfirst=not re.match(r"^\d{4}-", str(v)))
                pretty = f"{v} ({d.day} {d.strftime('%B %Y')})"
            except Exception:
                pass
            facts.append(f"{kv}: {fcol} = {pretty}")
        ex.rec.add(f"{name}, {kc}={kv}, {' and '.join(flds)}")
        db = [x for x in (b.get("days_between") or []) if _is_date(r[x])]
        if len(db) != 2 and b.get("want_days"):  # the question asks for days: use the chosen date fields, in the order chosen
            db = [x for x in flds if _is_date(r[x])][:2]
        if len(db) == 2:
            a, c = (pd.to_datetime(r[x], dayfirst=not re.match(r"^\d{4}-", str(r[x]))).date().isoformat() for x in db)
            n = days_between(a, c)
            facts.append(f"Days from {db[0]} to {db[1]} (excluding the starting day): {n}")
            keys.append(str(n))
    elif op == "count_blank":
        c = b["column"]
        blank = int(df[c].map(lambda v: v is None or (isinstance(v, float) and v != v) or str(v).strip() == "").sum())
        facts.append(f"{name}: {blank} of {len(df)} records have a blank {c}")
        keys += [str(blank), str(len(df))]
        ex.rec.add(f"{name}, {c} column")
    elif op == "compare_keys":
        s2 = srcs[-1]
        df2 = load_df(s2)
        a = df[b["column_a"]].astype(str).str.strip()
        c = df2[b["column_b"]].astype(str).str.strip()
        only_a, only_b = sorted(set(a) - set(c)), sorted(set(c) - set(a))
        da, dc = sorted(a[a.duplicated()].unique()), sorted(c[c.duplicated()].unique())
        facts.append(f"{name}.{b['column_a']} ({len(a)} rows) vs {clean_name(s2.file)}.{b['column_b']} ({len(c)} rows): "
                     f"{len(only_a)} values only in the first {only_a[:5]}, {len(only_b)} only in the second {only_b[:5]}; "
                     f"duplicates: {len(da)} in the first {da[:5]}, {len(dc)} in the second {dc[:5]}")
        keys += [str(len(only_a)), str(len(only_b))]
        ex.rec.add(f"{name}, {b['column_a']} column; {clean_name(s2.file)}, {b['column_b']}")
    return facts, keys


def _is_date(v) -> bool:
    v = str(v).strip()
    if not re.match(r"^\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}", v):
        return False
    try:
        pd.to_datetime(v, dayfirst=not re.match(r"^\d{4}-", v))
        return True
    except (ValueError, TypeError):
        return False


def run_area(b, s: Source, ex: Exec):
    import geopandas as gpd
    g = gpd.read_file(s.file)
    m = g[g[b["id_field"]].astype(str).str.strip() == str(b["id_value"]).strip()]
    if len(m) != 1:
        raise Fail(f"{len(m)} features where {b['id_field']} == {b['id_value']!r}")
    crs = CRS[b["crs"]]
    geom = m.to_crs(crs).geometry.iloc[0]
    unit = b["unit"]
    v = geom.area / AREA_UNITS[unit]
    ex.rec.add(f"{clean_name(s.file)}, properties.{b['id_field']}={b['id_value']}")
    return [f"Area of {b['id_field']}={b['id_value']}: {_fmt(v)} {unit} (computed in {crs}, geometry {'valid' if geom.is_valid else 'INVALID'}, not repaired)"], [_fmt(v)]


def explain_context(srcs: list[Source], all_srcs: list[Source]) -> str:
    """Text the model needs for 'what does the data support' questions: table header lines, notes sheets, guides."""
    parts, seen = [], set()
    for s in srcs:
        if s.kind == "table" and s.file not in seen:
            seen.add(s.file)
            parts.append(f"--- {clean_name(s.file)}, sheet {s.table.sheet}, header lines:\n" + sheet_text(s.file, s.table.sheet, 1200).split("\nr8")[0][:1200])
            for sh in ("Notes", "notes", "NOTES"):
                txt = sheet_text(s.file, sh, 2500)
                if txt:
                    parts.append(f"--- {clean_name(s.file)}, sheet {sh}:\n{txt}")
    for s in all_srcs:
        if s.kind == "text" and is_guide(s.file):
            parts.append(f"--- {clean_name(s.file)}:\n{guide_text(s.file)[:1500]}")
    return "\n".join(parts)[:7000]


AREA_WORDS = re.compile(r"\b(metropolitan|region|regional|area|capital|whole|combined|overall)\b", re.I)


_MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august", "september",
                                       "october", "november", "december"], 1)}


def iso_dates(text: str) -> list[str]:
    out = re.findall(r"\b(\d{4}-\d{2}-\d{2})\b", text)
    for d, mo, y in re.findall(r"\b(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})\b", text):
        if mo.lower() in _MONTHS:
            out.append(f"{int(y):04d}-{_MONTHS[mo.lower()]:02d}-{int(d):02d}")
    return list(dict.fromkeys(out))


def narrow(t: Table, question: str, gloss: dict, op: str):
    """Code-side narrowing of the options offered to the model, using facts detectable in the question and the table:
    years mentioned; blocks (places) named; total rows when no component is named; for flat tables, items sharing a word."""
    E, P = list(t.entities), list(t.periods)
    t._named_comps = []
    ql = question.lower()
    yrs = [p for p in P if re.search(rf"(?<!\d){re.escape(p[:4])}(?!\d)", question)]
    if yrs:
        P = yrs
    blocks = list(dict.fromkeys(e.split(" / ", 1)[0] for e in E if " / " in e))
    if blocks:
        named_b = [b for b in blocks if re.search(rf"(?<!\w){re.escape(b.lower())}(?!\w)", ql)]
        # "<place> metropolitan area/region": the place is part of an aggregate block -> the aggregate is meant
        area_of = [a for a, parts in t.aggregates.items() for b in named_b if b in parts and
                   re.search(rf"(?<!\w){re.escape(b.lower())}(?:'s)?\s+(?:\w+[\s-]+){{0,1}}(metropolitan|region|regional|area|capital)", ql)]
        if area_of:
            named_b = list(dict.fromkeys(area_of))
        if named_b:
            keep = set(named_b) | (set(t.aggregates) if AREA_WORDS.search(question) and not area_of else set())
            E = [e for e in E if e.split(" / ", 1)[0] in keep]
        totals = set(t.totals.values())
        comps = {e.split(" / ", 1)[1] for e in E if " / " in e and e not in totals}
        qstems = {w[:6] for w in re.findall(r"[a-z0-9]+", ql) if len(w) >= 4}
        def named_comp(c):  # the label itself, or every (stemmed) word of its English gloss, appears in the question
            if re.search(rf"(?<!\w){re.escape(c.lower())}(?!\w)", ql):
                return True
            gw = [w[:6] for w in re.findall(r"[a-z0-9]+", (gloss.get(c) or "").lower()) if len(w) >= 4]
            return bool(gw) and all(w in qstems for w in gw)
        t._named_comps = [c for c in comps if named_comp(c)]
        comp_named = bool(t._named_comps)
        if totals and not comp_named and op != "share":
            E = [e for e in E if e in totals] or E
    else:
        words = lambda e: set(re.findall(r"[a-z0-9]+", re.sub(r"\[.*?\]", "", e.lower()))) - {"and", "of", "the", "with", "or"}
        qt = set(re.findall(r"[a-z0-9]+", ql))
        cand = [e for e in E if words(e) & qt]
        if cand:
            E = cand
    return E, P


def table_fit(s: Source, query: str) -> float:
    """How well a table's measure line ('Passenger journeys (millions)') matches the query: matched words minus half the
    unmatched ones, on 6-letter stems. Short measure lines discriminate; long titles do not."""
    from .textqa import tokens
    if s.kind != "table":
        return 0.0
    stem = lambda w: w[:6]
    m = {stem(w) for w in tokens(re.sub(r"\(.*?\)", "", s.table.measure or s.table.title))}
    q = {stem(w) for w in tokens(query)}
    return len(m & q) - 0.5 * len(m - q)


def best_table(pool: list[Source], query: str) -> Source | None:
    tabs = [s for s in pool if s.kind == "table"]
    return max(tabs, key=lambda s: table_fit(s, query)) if tabs else None


def rank_sources(pool: list[Source], query: str, k: int) -> list[Source]:
    import math
    from collections import Counter
    from .textqa import tokens
    docs = [tokens(s.desc) for s in pool]
    df = Counter(t for d in docs for t in set(d))
    avg = sum(map(len, docs)) / max(1, len(docs))
    q, N = tokens(query), len(docs)
    def score(d):
        tf = Counter(d)
        return sum(math.log(1 + (N - df[t] + 0.5) / (df[t] + 0.5)) * tf[t] * 2.5 / (tf[t] + 1.5 * (0.25 + 0.75 * len(d) / avg)) for t in q if t in tf)
    keep = sorted(range(N), key=lambda i: -score(docs[i]))[:k]
    return [pool[i] for i in sorted(keep)]  # keep original order (and ids) among the kept


def _one_line(s: str) -> str:
    return " ".join(str(s).split()).replace("|", "/")


def format_evidence(items: list[str], facts: list[str], files: list[Path]) -> list[str]:
    """Recorder locators -> 'SOURCE_ID | locator | supported claim' (the claim is the computed fact that used the locator)."""
    sid = {clean_name(f): f.name.split("__")[0] for f in files}
    out = []
    for it in items:
        name = next((n for n in sorted(sid, key=len, reverse=True) if it.startswith(n)), it.split(",")[0].strip())
        cell = re.search(r"cell ([A-Z]{1,3}\d+)", it)
        claim = next((f for f in facts if cell and cell.group(1) in f), facts[0] if facts else "")
        out.append(f"{sid.get(name, name)} | {_one_line(it)} | {_one_line(_strip_cells(claim))[:180]}")
    return out


# ------------------------------------------------------------------ main entry
SYS = ("You are a careful municipal data analyst. You never invent values: you only choose among the options offered. "
       "Read the question and the Context carefully and choose exactly what they ask for.")


def answer(llm: LLM, question: str, context: str, files: list[Path]) -> dict:
    led = Ledger()
    trace: dict = {}
    try:
        srcs = build_sources(files)
        if not srcs:
            raise Fail("no readable sources")
        named = {s.file for s in srcs if clean_name(s.file).lower() in question.lower()}
        pool = [s for s in srcs if s.file in named] if named else srcs  # a file named in the question restricts the choice
        if named:
            trace["named_files"] = sorted(clean_name(f) for f in named)
        if len(pool) > 15:  # large packs: show the 15 sources most similar to the question (BM25 over descriptions)
            pool = rank_sources(pool, f"{question} {context}", 15)
            trace["ranked_sources"] = [s.desc[:70] for s in pool]
        by_id = {s.sid: s for s in pool}
        catalog = "\n".join(f"{s.sid}: {s.desc}" for s in pool)
        ops_txt = "\n".join(f"- {k}: {v}" for k, v in OPS.items())
        # ---- call 1: operation + sources
        sch = _enum_schema({"what_is_asked": {"type": "string", "maxLength": 220, "description": "one short sentence: the quantity asked and, for a ratio, 'numerator: ...; denominator: ...'"},
                            "operation": {"type": "string", "enum": list(OPS)},
                            "sources": {"type": "array", "items": {"type": "string", "enum": list(by_id)}, "minItems": 1, "maxItems": 2}})
        msg = [{"role": "system", "content": SYS},
               {"role": "user", "content": f"QUESTION: {question}\nCONTEXT: {context}\n\nOPERATIONS:\n{ops_txt}\n\n{EXAMPLES}\n\nSOURCES:\n{catalog}\n\n"
                                           "Choose the operation that answers the main calculation in the question, and the source(s) holding the data "
                                           "(two sources only for ratio or compare_keys across files). Match the measure exactly (e.g. passenger vs vehicle, "
                                           "current vs constant prices). If several tables hold the same measure, prefer the main summary table with the fewest items. Reply as JSON."}]

        fit_rejected = trace.setdefault("fit_pushback", [])

        def v1(o):
            if o["operation"] not in OPS:
                raise Fail("unknown operation")
            ss = [by_id[x] for x in o["sources"]]
            kinds = {s.kind for s in ss}
            need = {"value": {"table"}, "change": {"table"}, "percent_of": {"table"}, "share": {"table"}, "ratio": {"table"},
                    "record_lookup": {"csv", "geo"}, "count_blank": {"csv", "geo"}, "compare_keys": {"csv", "geo"}, "area": {"geo"}, "explain": None,
                    "table_query": {"csv", "geo"}, "spatial": {"geo"}, "transit": None,
                    "text_answer": None}[o["operation"]]
            if need and not kinds <= need:
                raise Fail(f"operation {o['operation']} needs a source of kind {sorted(need)}, got {sorted(kinds)}")
            need_fmt = [(fmt, [x.sid for x in pool if f"] {fmt}" in x.desc]) for word, fmt in
                        (("geopackage", "GeoPackage"), ("geojson", "GeoJSON"), ("shapefile", "Shapefile"), ("csv", "CSV"))
                        if word in question.lower() and any(f"] {fmt}" in x.desc for x in pool)]
            missing_fmt = [(f, c) for f, c in need_fmt if not any(f"] {f}" in x.desc for x in ss)]
            if missing_fmt:
                raise Fail("the question names these formats, so choose one source of each: "
                           + "; ".join(f"{f} from {', '.join(c)}" for f, c in need_fmt))
            if o["operation"] in ("value", "change", "percent_of", "share") and ss[0].kind == "table":
                norm = lambda x: " ".join(re.findall(r"[a-z0-9]+", x.lower()))
                qn = norm(question)
                phrase = [x for x in pool if x.kind == "table" and norm(re.sub(r"\(.*?\)", "", x.table.measure or "")) and
                          len(norm(re.sub(r"\(.*?\)", "", x.table.measure)).split()) >= 2 and norm(re.sub(r"\(.*?\)", "", x.table.measure)) in qn]
                key = f"phrase:{ss[0].sid}"
                if phrase and ss[0] not in phrase and key not in fit_rejected:
                    fit_rejected.append(key)
                    raise Fail(f"the question names the measure of {', '.join(f'{x.sid} ({x.table.measure})' for x in phrase[:4])}; "
                               f"{ss[0].sid} measures '{ss[0].table.measure}'. Use one of those unless the question really needs {ss[0].sid}.")
            for gword in ("point", "polygon", "line"):
                if re.search(rf"\b{gword}s?\b", question.lower()):
                    geo_srcs = [x for x in ss if x.kind in ("csv", "geo") and "geometr" in x.desc.lower()]
                    good = [x for x in pool if x.kind in ("csv", "geo") and re.search(rf"\b(multi)?{gword}", x.desc.lower())]
                    bad = [x for x in geo_srcs if not re.search(rf"\b(multi)?{gword}", x.desc.lower()) and x.kind == "csv"]
                    if bad and good:
                        raise Fail(f"the question mentions {gword} data but {bad[0].sid} holds other geometries; "
                                   f"sources with {gword} geometries: {', '.join(x.sid for x in good)}")
            wa = o.get("what_is_asked", "")
            if o["operation"] == "ratio" and "denominator" not in wa.lower():  # 2. ask the two measures explicitly
                ms = _enum_schema({"numerator": {"type": "string", "maxLength": 120}, "denominator": {"type": "string", "maxLength": 120}})
                mm = [{"role": "user", "content": f"QUESTION: {question}\nThis needs one quantity divided by another. Name the measure of the "
                                                  "numerator and of the denominator in a few words each. Reply as JSON."}]
                try:
                    r = _ask_json(llm, led, mm, ms, "measures", lambda x: x, tries=1, max_tokens=120)
                    wa = f"numerator: {r['numerator']}; denominator: {r['denominator']}"
                    o["what_is_asked"] = wa
                except Fail:
                    pass
            if o["operation"] == "ratio" and "denominator" in wa.lower():  # fit check only on the model's own ratio sides
                num, den = re.split(r"denominator", wa, flags=re.I, maxsplit=1)
                sides = [num, den]
                for side, chosen in zip(sides, ss):
                    best = best_table(pool, side)
                    if best and best is not chosen:
                        key = f"fit:{chosen.sid}->{best.sid}"
                        if key not in fit_rejected and table_fit(best, side) >= table_fit(chosen, side) + 1.0:
                            fit_rejected.append(key)  # one push-back; the model may insist
                            raise Fail(f"{best.sid} ({best.table.id}) matches '{side.strip()[:80]}' better than {chosen.sid} "
                                       f"({chosen.table.id}). Use {best.sid} for that side unless it is really the wrong measure.")
            if o["operation"] == "transit" and feed_for(files) is None:
                raise Fail("transit needs a GTFS feed (stops.txt, trips.txt, ...) in the sources; choose another operation")
            if o["operation"] in ("record_lookup", "area") and not id_candidates(question, ss[0]):
                raise Fail(f"no identifier from the question occurs in {clean_name(ss[0].file)}; choose the source that contains it")
            return o
        plan = _ask_json(llm, led, msg, sch, "plan", v1, think=getattr(llm, "think_plan", False))
        op, ss = plan["operation"], [by_id[x] for x in plan["sources"]]
        trace["plan"] = {"op": op, "sources": [s.desc[:90] for s in ss], "what_is_asked": plan.get("what_is_asked", "")}
        ex = Exec(context)
        gloss: dict = {}
        # ---- call 2: gloss non-English labels of chosen tables
        tabs = [s.table for s in ss if s.kind == "table"]
        labels = sorted({part for t in tabs for e in t.entities for part in e.split(" / ")})
        if tabs and any(re.search(r"[^\x00-\x7f]", x) for x in labels) and len(labels) <= 150:
            gs = _enum_schema({x: {"type": "string"} for x in labels})
            ctx_names = "; ".join(sorted({f"table '{t.title}' in file '{clean_name(t.file)}'" for t in tabs}))
            aggs = "; ".join(f"block '{b}' is the total of {', '.join(o)}" for t in tabs for b, o in t.aggregates.items())
            if aggs:
                ctx_names += f" (structure found in the data: {aggs})"
            gm = [{"role": "user", "content": f"These are row/block labels from {ctx_names}. Translate each label to short English. "
                                              "Keep place names (cities, regions) unchanged. If a label is an abbreviation, expand it if the file name "
                                              "or title makes its meaning clear. Labels:\n" + "\n".join(labels) + "\nReply as JSON {label: english}."}]
            try:
                gloss = _ask_json(llm, led, gm, gs, "gloss", lambda o: {k: str(v) for k, v in o.items() if k in labels}, tries=1, max_tokens=900)
            except Fail:
                gloss = {}
            trace["gloss"] = gloss
        # ---- call 3: bind arguments to real labels
        facts, keys = [], []
        if op in ("value", "change", "percent_of", "share", "ratio", "explain") and tabs:
            t0 = ss[0].table
            show = lambda t: "\n".join(f"  {e}" + (f"   (= {_label(e, gloss)})" if gloss and _label(e, gloss) != e else "") for e in t.entities)
            E, P = narrow(t0, question, gloss, op)
            trace["narrowed"] = {"items": len(E), "of": len(t0.entities), "periods": P if len(P) <= 6 else len(P)}
            R = {"reasoning": {"type": "string", "maxLength": 200, "description": "one short sentence naming the items and periods the question asks for"}}
            if op == "value" or op == "explain":
                bs = _enum_schema({**R, "entities": {"type": "array", "items": {"type": "string", "enum": E}, "minItems": 1, "maxItems": 8},
                                   "periods": {"type": "array", "items": {"type": "string", "enum": P}, "minItems": 1, "maxItems": 6}})
            elif op in ("change", "percent_of"):
                bs = _enum_schema({**R, "entities": {"type": "array", "items": {"type": "string", "enum": E}, "minItems": 1, "maxItems": 8},
                                   "period_from": {"type": "string", "enum": P}, "period_to": {"type": "string", "enum": P}})
            elif op == "share":
                bs = _enum_schema({**R, "pairs": {"type": "array", "minItems": 1, "maxItems": 4, "items": _enum_schema({"part": {"type": "string", "enum": E}, "whole": {"type": "string", "enum": E}})},
                                   "period": {"type": "string", "enum": P}})
            else:  # ratio
                t1 = ss[-1].table
                E1, P1 = narrow(t1, question, gloss, op)
                bs = _enum_schema({**R, "numerator": _enum_schema({"entity": {"type": "string", "enum": E}, "period": {"type": "string", "enum": P}}),
                                   "denominator": _enum_schema({"entity": {"type": "string", "enum": E1}, "period": {"type": "string", "enum": P1}})})
            notes = [f"Block '{b}' is the TOTAL of blocks {', '.join(o)} (the whole area/group)." for b, o in t0.aggregates.items()]
            if t0.totals:
                notes.append("Total row of each block: " + "; ".join(t0.totals.values()))
                notes.append("Unless the question names a specific component (e.g. one sector, fuel or category), a place's consumption/value "
                             "means its TOTAL row, not a component row.")
            qt = set(re.findall(r"[a-z0-9]+", question.lower()))
            def _w(e):
                return set(re.findall(r"[a-z0-9]+", re.sub(r"\[.*?\]", "", _label(e, gloss).lower()))) - {"and", "of", "the"}
            named = [e for e in E if _w(e) and (_w(e) <= qt or len(_w(e) & qt) >= 2)]
            if named:
                notes.append("Items whose names appear in the question: " + "; ".join(named[:10]))
            show = lambda t, ents=None: "\n".join(f"  {e}" + (f"   (= {_label(e, gloss)})" if gloss and _label(e, gloss) != e else "") for e in (ents or t.entities))
            listing = f"TABLE {t0.id}\nTitle: {t0.title}\nUnit: {t0.unit}\n" + "".join(n + "\n" for n in notes) + f"Items:\n{show(t0, E)}\nPeriods: {', '.join(P)}"
            if op == "ratio" and ss[-1] is not ss[0]:
                t1 = ss[-1].table
                E1, P1 = narrow(t1, question, gloss, op)
                listing = f"NUMERATOR {listing}\n\nDENOMINATOR TABLE {t1.id}\nTitle: {t1.title}\nUnit: {t1.unit}\nItems:\n{show(t1, E1)}\nPeriods: {', '.join(P1)}"
            how = {"value": "Pick the item(s) and period(s) the question asks about.",
                   "explain": "Pick the item(s) and period(s) whose values the question is about.",
                   "change": "Pick every item the question compares, and the start (period_from) and end (period_to) periods.",
                   "percent_of": "Pick the item(s); period_from is the earlier/base period, period_to is the later period.",
                   "share": "For each part/whole pair pick the part item and the matching whole (total) item of the SAME group, and the period.",
                   "ratio": "Pick the numerator item and period, and the denominator item and period."}[op]
            bm = [{"role": "system", "content": SYS},
                  {"role": "user", "content": f"QUESTION: {question}\nCONTEXT: {context}\n\n{listing}\n\n{how} Choose ONLY the items the question "
                                              "names (usually 1-3); never add others. An area-wide or regional total is the block named for that area, not one of its cities. "
                                              "Keep all chosen items in the same block unless the question names different places. "
                                              "'The rest of X' or 'X excluding Y' means an item such as 'X outside Y' when one is listed. "
                                              "Use labels exactly as listed. Reply as JSON."}]

            rejected = trace.setdefault("share_pushback", [])

            def vb(o):
                o.pop("reasoning", None)
                if "entities" in o:
                    o["entities"] = list(dict.fromkeys(o["entities"]))
                    if op in ("change", "value", "percent_of") and getattr(t0, "_named_comps", None):
                        for blk in dict.fromkeys(e.split(" / ", 1)[0] for e in o["entities"] if " / " in e):
                            for c in t0._named_comps:  # the question also asks about a named component: include its row
                                cand = f"{blk} / {c}"
                                if cand in t0.entities and cand not in o["entities"]:
                                    o["entities"].append(cand)
                if op == "share":
                    for pr in o["pairs"]:
                        blk = pr["part"].split(" / ", 1)[0] if " / " in pr["part"] else ""
                        own = t0.totals.get(blk)
                        if pr["part"] == pr["whole"] and own and pr["part"] != own:
                            pr["whole"] = own  # a component's share is of its own block total
                        elif pr["part"] == pr["whole"] == own and getattr(t0, "_named_comps", None):
                            cand = f"{blk} / {t0._named_comps[0]}"  # total chosen twice: the part is the named component
                            if cand in t0.entities:
                                pr["part"] = cand
                if op in ("change", "percent_of") and o["period_from"][:4] > o["period_to"][:4]:
                    o["period_from"], o["period_to"] = o["period_to"], o["period_from"]  # from = earlier/base, to = later
                if op == "change" and o["period_from"] == o["period_to"]:
                    raise Fail("period_from and period_to must differ")
                if op == "percent_of" and o["period_from"] == o["period_to"]:
                    raise Fail("the two periods must differ")
                if op == "share":
                    for pr in o["pairs"]:
                        if pr["part"] == pr["whole"]:
                            raise Fail("part and whole must differ")
                        blk = pr["part"].split(" / ", 1)[0] if " / " in pr["part"] else ""
                        own = t0.totals.get(blk)
                        key = f"{pr['part']}|{pr['whole']}"
                        if own and pr["whole"] != own and pr["part"] != own and key not in rejected:
                            rejected.append(key)  # one push-back; if the model insists, accept its choice
                            wb = pr["whole"].split(" / ", 1)[0] if " / " in pr["whole"] else ""
                            lab = pr["part"].split(" / ", 1)[-1]
                            alt = f"'{wb} / {lab}' with '{pr['whole']}'" if f"{wb} / {lab}" in E else None
                            raise Fail(f"part and whole come from different blocks. Use either '{pr['part']}' with its own block total '{own}'"
                                       + (f", or {alt} if the question is about the {wb} block as a whole" if alt else "") + ".")
                if op == "explain":
                    return o
                # dry-run: every value must resolve to a number
                run_table_op(op, o, ss, Exec(context), gloss)
                return o
            b = _ask_json(llm, led, bm, bs, "bind", vb, think=getattr(llm, "think_plan", False))
            trace["bind"] = b
            if op == "explain":
                for e in b["entities"]:
                    for p in b["periods"]:
                        v, a1 = ex.cell(t0, e, p)
                        facts.append(f"{_label(e, gloss)} in {p}: {v!r} (cell {a1})")
            else:
                facts, keys = run_table_op(op, b, ss, ex, gloss)
        elif op == "transit":
            fd = feed_for(files)
            stops = stop_options(fd, f"{question} {context}") or ["(none)"]
            trips = trip_options(fd, f"{question} {context}")
            dates = iso_dates(f"{question} {context}")
            bs = _enum_schema({"reasoning": {"type": "string", "maxLength": 200},
                               "task": {"type": "string", "enum": TRANSIT_TASKS},
                               "date": {"type": "string", "maxLength": 10}, "flag": {"type": "string", "enum": ["", "bikes_prohibited", "bikes_allowed", "wheelchair_accessible"]},
                               "stop_a": {"type": "string", "enum": [""] + stops}, "stop_b": {"type": "string", "enum": [""] + stops},
                               "models": {"type": "array", "items": {"type": "string", "enum": TRANSIT_MODELS}, "maxItems": 3},
                               "both_directions": {"type": "boolean"},
                               "candidate_type": {"type": "string", "enum": [v for k, v in LOCATION_TYPES.items() if k]},
                               "same_station": {"type": "boolean"},
                               "trip": {"type": "string", "enum": [""] + trips}, "which": {"type": "string", "enum": ["first", "last"]}})
            bm = [{"role": "system", "content": SYS},
                  {"role": "user", "content": f"QUESTION: {question}\nCONTEXT: {context}\n\nStops matching the question (id | name | type): {'; '.join(stops)}\n"
                                              f"Trips named in the question/Context: {', '.join(trips) or '(none)'}\nDates found (ISO): {', '.join(dates) or '(none)'}\n"
                                              "Pick the task. active_trips: date (YYYY-MM-DD; a named day like Boxing Day must be converted) and optional flag. "
                                              "route_time: stop_a (start) and stop_b (end), the models the question asks for, both_directions if it asks for the way back too. "
                                              "nearest_stop: stop_a is the target, candidate_type, same_station. trip_time_at_stop: trip, stop_a (or empty with which=last "
                                              "for the destination), date = service day. accessibility: stop_a. Reply as JSON."}]

            def vt(o):
                o.pop("reasoning", None)
                try:
                    run_transit(o, fd, context, Recorder())
                except (TransitError, KeyError, ValueError, IndexError) as e:
                    raise Fail(f"{type(e).__name__}: {e}")
                return o
            b = _ask_json(llm, led, bm, bs, "bind", vt, think=getattr(llm, "think_plan", False))
            trace["bind"] = b
            facts, keys = run_transit(b, fd, context, ex.rec)
        elif op == "spatial":
            geo = [x for x in pool if x.kind == "geo"]
            gid = {x.sid: x for x in geo}
            opts = anchor_options(question, geo) or ["(none)"]
            cols = sorted({str(c) for x in geo for c in x.columns})
            crs_fixed = stated_crs(context, question)
            fsch = _enum_schema({"column": {"type": "string", "enum": [""] + cols}, "op": {"type": "string", "enum": FILTER_OPS},
                                 "value": {"type": "string", "maxLength": 60}})
            bs = _enum_schema({"reasoning": {"type": "string", "maxLength": 200},
                               "task": {"type": "string", "enum": SPATIAL_TASKS},
                               "anchor": {"type": "string", "enum": [""] + opts}, "second_anchor": {"type": "string", "enum": [""] + opts},
                               "radius_m": {"type": "number"},
                               "layers": {"type": "array", "items": {"type": "string", "enum": list(gid)}, "maxItems": 4},
                               "crs": {"type": "string", "enum": list(CRS.values())},
                               "filter": fsch, "diameter_column": {"type": "string", "enum": [""] + cols},
                               "bbox": _enum_schema({k: {"type": "number"} for k in ("xmin", "xmax", "ymin", "ymax")})})
            listing = "\n".join(f"{x.sid}: {x.desc}" for x in geo)
            bm = [{"role": "system", "content": SYS},
                  {"role": "user", "content": f"QUESTION: {question}\nCONTEXT: {context}\n\nLAYERS:\n{listing}\n\nFeatures named in the question "
                                              f"(layer :: column = value): {'; '.join(opts)}\n"
                                              + (f"Coordinate system fixed by the Context: {crs_fixed}.\n" if crs_fixed else "")
                                              + "Pick the task, the anchor feature (and second_anchor for circle_overlap), radius_m for radius_count, "
                                                "the layers to search (for radius_count include every layer the question says to use), an optional filter "
                                                "for a subset the question asks about (else column ''), diameter_column for circle_overlap, bbox numbers for "
                                                "bbox_outside (else 0). Reply as JSON."}]

            def vs(o):
                o.pop("reasoning", None)
                if (o.get("filter") or {}).get("column", "") == "":
                    o["filter"] = {}
                try:
                    run_spatial(o, gid, crs_fixed or o["crs"], Recorder())
                except (SpatialError, KeyError, ValueError, IndexError, TypeError) as e:
                    raise Fail(f"{type(e).__name__}: {e}")
                return o
            b = _ask_json(llm, led, bm, bs, "bind", vs, think=getattr(llm, "think_plan", False))
            trace["bind"] = b
            facts, keys = run_spatial(b, gid, crs_fixed or b["crs"], ex.rec)
        elif op == "table_query":
            s0 = ss[0]
            df = load_df(s0)
            C = [str(c) for c in df.columns]
            hits = value_hits(df, question)
            name = clean_name(s0.file) + (f" sheet {s0.sheet}" if s0.sheet else "")
            fsch = _enum_schema({"column": {"type": "string", "enum": C}, "op": {"type": "string", "enum": FILTER_OPS},
                                 "value": {"type": "string", "maxLength": 120}})
            bs = _enum_schema({"reasoning": {"type": "string", "maxLength": 200, "description": "one short sentence: which records, which measure"},
                               "filters": {"type": "array", "items": fsch, "maxItems": 4},
                               "group_by": {"type": "string", "enum": [""] + C}, "aggregate": {"type": "string", "enum": AGGS},
                               "target": {"type": "string", "enum": [""] + C}, "sort": {"type": "string", "enum": ["desc", "asc"]},
                               "top": {"type": "integer", "minimum": 1, "maximum": 20}})
            hint = ("Values from the question found in the data: " + "; ".join(hits) + "\n") if hits else ""
            bm = [{"role": "system", "content": SYS},
                  {"role": "user", "content": f"QUESTION: {question}\nCONTEXT: {context}\n\nTABLE {name}\nColumns:\n{profile(df)}\n{hint}\n"
                                              "Build the query: filters select the records the question is about (op is_blank/not_blank need no value; "
                                              "use '=' with a value exactly as it appears in the data); aggregate is what to compute "
                                              "(count needs no target); group_by only if the question asks per category or for the most/least common; "
                                              "top = how many groups to report. Reply as JSON."}]

            def vq(o):
                o.pop("reasoning", None)
                for f_ in o.get("filters") or []:
                    if f_["op"] in ("is_blank", "not_blank"):
                        f_["value"] = ""
                if o["aggregate"] != "count" and not o.get("target"):
                    if o.get("group_by") and o["aggregate"] in ("count_distinct", "list_values"):
                        o["target"] = o["group_by"]
                    else:
                        raise Fail(f"aggregate {o['aggregate']} needs a target column")
                try:
                    run_query(df, o, name)
                except QueryError as e:
                    raise Fail(str(e))
                return o
            b = _ask_json(llm, led, bm, bs, "bind", vq, think=getattr(llm, "think_plan", False))
            trace["bind"] = b
            facts, keys, loc = run_query(df, b, name)
            ex.rec.add(loc)
        elif op in ("record_lookup", "count_blank", "compare_keys", "area") or (op == "explain" and not tabs):
            s0 = ss[0]
            C = s0.columns
            ids = id_candidates(question, s0) if op in ("record_lookup", "area") else []
            if op == "record_lookup":
                bs = _enum_schema({"record": {"type": "string", "enum": ids},
                                   "fields": {"type": "array", "items": {"type": "string", "enum": C}, "minItems": 1, "maxItems": 6},
                                   "days_between": {"type": "array", "items": {"type": "string", "enum": C}, "maxItems": 2}})
                how = ("Pick the record identifier (column = value) the question is about, the fields to report, and, if the question asks for days "
                       "between two dates, the two date fields in chronological order (else an empty list).")
            elif op == "count_blank":
                bs = _enum_schema({"column": {"type": "string", "enum": C}})
                how = "Pick the column whose empty values must be counted."
            elif op == "compare_keys":
                C2 = ss[-1].columns
                bs = _enum_schema({"column_a": {"type": "string", "enum": C}, "column_b": {"type": "string", "enum": C2}})
                how = f"Pick the key column in the first file ({clean_name(s0.file)}) and in the second file ({clean_name(ss[-1].file)})."
            elif op == "area":
                bs = _enum_schema({"feature": {"type": "string", "enum": ids},
                                   "crs": {"type": "string", "enum": list(CRS)}, "unit": {"type": "string", "enum": list(AREA_UNITS)}})
                how = "Pick the feature identifier (column = value) the question is about, the coordinate system the question or Context asks for, and the area unit."
            else:
                bs, how = None, ""
            sample = ""
            if s0.kind in ("csv", "geo"):
                try:
                    df = read_table(s0.file, nrows=3) if s0.kind == "csv" else __import__("geopandas").read_file(s0.file, rows=3).drop(columns="geometry")
                    sample = "Example rows:\n" + df.head(3).to_csv(index=False)[:1500]
                except Exception:
                    pass
            if bs:
                bm = [{"role": "system", "content": SYS},
                      {"role": "user", "content": f"QUESTION: {question}\nCONTEXT: {context}\n\nFILE {clean_name(s0.file)} columns: {', '.join(C)}\n{sample}\n{how} Reply as JSON."}]

                def vb(o):
                    o["want_days"] = bool(re.search(r"\bdays?\b|elapsed|how long", question, re.I))
                    for k_in, (kc, kv) in (("record", ("key_column", "key_value")), ("feature", ("id_field", "id_value"))):
                        if k_in in o:
                            o[kc], o[kv] = o.pop(k_in).split(" = ", 1)
                    if op == "area":
                        run_area(o, s0, Exec(context))
                    else:
                        run_record_op(op, o, ss, Exec(context))
                    return o
                b = _ask_json(llm, led, bm, bs, "bind", vb, think=getattr(llm, "think_plan", False))
                trace["bind"] = b
                facts, keys = run_area(b, s0, ex) if op == "area" else run_record_op(op, b, ss, ex)
        # ---- retrieval for text answers and explanations
        found = []
        if op in ("text_answer", "explain"):
            chosen = [s.file for s in ss if s.kind == "text"]
            others = [s.file for s in pool if s.kind == "text" and s.file not in chosen]
            found = text_index(chosen).search(f"{question} {context}", k=6) if chosen else []
            if len(found) < 3 and others:
                found += text_index(others).search(f"{question} {context}", k=6 - len(found))
            trace["passages"] = [f"{clean_name(x.file)} | {x.locator}" for x in found]
        # ---- call 4: write the answer
        ctx_txt = explain_context(ss, srcs) if op == "explain" else ""
        if found:
            ctx_txt += "\n\nPASSAGES:\n" + "\n".join(f"[P{i + 1}] ({clean_name(x.file)}, {x.locator}) {x.text[:900]}" for i, x in enumerate(found))
        wm = [{"role": "system", "content": "You write short, exact answers for municipal briefings."},
              {"role": "user", "content": f"QUESTION: {question}\nCONTEXT: {context}\n\nCOMPUTED FACTS (exact; copy numbers verbatim, do not recompute or round differently):\n"
                                          + "\n".join(f"- {x}" for x in facts) + (f"\n\nSOURCE TEXT:\n{ctx_txt}" if ctx_txt else "")
                                          + "\n\nAnswer every part of the question in 1-3 plain sentences, with units. If the question asks yes/no, start with Yes or No. "
                                            "If the data does not support what is asked, say so and why. Do not mention cells or files."
                                          + (" Use only the passages; put the passage number you relied on in brackets, e.g. [P2]." if found else "")}]
        try:
            out = llm.chat(wm, led, think=False, profile="write", max_tokens=350, tag="write")
            ans = out["text"].strip()
        except BudgetExceeded:
            ans = ""
        miss = lambda a: [k for k in keys if not any(abs(float(k.replace(",", "")) - c) < 1e-9 for c in numbers(a))]
        missing = miss(ans)
        if keys and missing:
            try:
                out = llm.chat(wm + [{"role": "assistant", "content": ans}, {"role": "user", "content": f"Your answer dropped or changed these exact numbers: {', '.join(missing)}. Rewrite it so each appears exactly as given."}],
                               led, think=False, profile="write", max_tokens=350, tag="write_fix")
                if not miss(out["text"]):
                    ans, missing = out["text"].strip(), []
            except BudgetExceeded:
                pass
        if keys and missing:
            trace["write_fallback"] = {"missing": missing, "model_answer": ans}
            ans = " ".join(_strip_cells(x) for x in facts)
        cited = [int(n) - 1 for n in re.findall(r"\[P(\d+)\]", ans) if 0 < int(n) <= len(found)] or ([0] if found else [])
        ans = re.sub(r"\s*\[P\d+\](?:\s*,\s*\[P\d+\])*", "", ans).strip()
        evidence = format_evidence(ex.rec.items, facts, files) + [
            f"{found[i].src_id} | {clean_name(found[i].file)}, {found[i].locator} | {_one_line(found[i].text)[:160]}" for i in dict.fromkeys(cited)]
        if not ans:
            raise Fail("empty answer")
        return {"answer": ans, "evidence": evidence, "facts": facts, "trace": trace, "error": None, "ledger": led.summary()}
    except (Fail, BudgetExceeded) as e:
        return {"answer": "", "evidence": [], "facts": [], "trace": trace, "error": f"{type(e).__name__}: {e}", "ledger": led.summary()}
