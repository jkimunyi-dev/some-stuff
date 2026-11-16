"""General table query: filters + optional group-by + one aggregate, over any record table
(CSV, geo attribute table, JSON record array, workbook sheet with a header row).

The model fills a JSON-schema-constrained query (columns are enums; filter values are checked against the data);
code executes it with pandas and reports counts, totals, shares and ties. No pack IDs or answer constants.
"""
from __future__ import annotations

import difflib
import json
import re
from pathlib import Path

import pandas as pd

FILTER_OPS = ["=", "!=", ">", ">=", "<", "<=", "contains", "not_contains", "is_blank", "not_blank"]
AGGS = ["count", "count_distinct", "sum", "mean", "median", "min", "max", "list_values", "duplicates"]


class QueryError(Exception):
    pass


# ---------------------------------------------------------------- loading record tables
def json_records(path: Path, max_bytes: int = 200_000_000):
    """Largest list of objects anywhere in the JSON -> (DataFrame, json path). GeoJSON features use their properties."""
    raw = json.loads(Path(path).read_bytes()[:max_bytes].decode("utf-8-sig", "ignore"))
    best = (None, "$", 0)

    def walk(o, p):
        nonlocal best
        if isinstance(o, list) and o and all(isinstance(x, dict) for x in o[:50]):
            if len(o) > best[2]:
                best = (o, p, len(o))
            for x in o[:3]:
                walk(x, p + "[]")
        elif isinstance(o, dict):
            for k, v in o.items():
                walk(v, f"{p}.{k}")

    walk(raw, "$")
    if best[0] is None:
        return None, None
    recs = best[0]
    if all(isinstance(r.get("properties"), dict) for r in recs[:20]):  # GeoJSON-like features
        recs = [{**r["properties"], **({"id": r["id"]} if "id" in r else {})} for r in recs]
    df = pd.json_normalize(recs, sep=".")
    for c in df.columns:  # list/dict cells (e.g. coordinates, tags) -> JSON text so they can be compared, counted and grouped
        if df[c].map(lambda x: isinstance(x, (list, dict))).any():
            df[c] = df[c].map(lambda x: json.dumps(x, ensure_ascii=False) if isinstance(x, (list, dict)) else x)
    return df, best[1]


def sheet_records(path: Path, sheet: str, max_scan: int = 12):
    """Workbook sheet with a header row (>=3 text cells followed by data rows) -> DataFrame, header row number."""
    head = pd.read_excel(path, sheet_name=sheet, header=None, nrows=max_scan + 5, dtype=str)
    for i in range(min(max_scan, len(head) - 1)):
        row = head.iloc[i]
        texts = [v for v in row if isinstance(v, str) and v.strip() and not re.fullmatch(r"[-\d.,%]+", v.strip())]
        if len(texts) >= 3 and head.iloc[i + 1].notna().sum() >= 2:
            df = pd.read_excel(path, sheet_name=sheet, header=i, dtype=str)
            df = df.loc[:, [c for c in df.columns if not str(c).startswith("Unnamed")]]
            return df.dropna(how="all"), i + 1
    return None, None


# ---------------------------------------------------------------- profiling (what the model sees)
def is_blank_series(s: pd.Series) -> pd.Series:
    if pd.api.types.is_numeric_dtype(s):  # numeric columns: only missing values are blank (no string conversion)
        return s.isna()
    return s.isna() | s.astype(str).str.strip().isin(["", "nan", "None", "NaN", "null"])


def to_num(s: pd.Series) -> pd.Series:
    if pd.api.types.is_numeric_dtype(s):
        return s.astype("float64")
    t = s.astype(str).str.strip().str.replace(" ", "", regex=False).str.replace(" ", "", regex=False)
    # decimal comma (e.g. 158,006) only when no dot is present and exactly one comma
    t = t.where(~(t.str.count(",") == 1) | t.str.contains(r"\.", regex=True), t.str.replace(",", ".", regex=False))
    t = t.str.replace(",", "", regex=False)
    return pd.to_numeric(t, errors="coerce")


def profile(df: pd.DataFrame, max_cols: int = 40) -> str:
    lines = [f"{len(df)} records"]
    if len(df) > 300_000:  # profile a sample of huge tables (min/max/top values are indicative only)
        df = df.sample(300_000, random_state=0)
        lines[0] += " (column profile from a 300,000-record sample)"
    for c in list(df.columns)[:max_cols]:
        s = df[c]
        blank = int(is_blank_series(s).sum())
        num = to_num(s[~is_blank_series(s)])
        nn = num.notna().mean() if len(num) else 0
        if nn > 0.9 and len(num):
            lines.append(f"- {c}: numeric, min {num.min():g}, max {num.max():g}, blanks {blank}")
        else:
            vc = s[~is_blank_series(s)].astype(str).value_counts()
            ex = ", ".join(f"{v[:30]!r} ({n})" for v, n in vc.head(6).items())
            t = col_type(df, c)
            lines.append(f"- {c}: {'date' if t == 'date' else 'JSON' if t == 'json' else 'text'}, {vc.size} distinct, blanks {blank}; top: {ex}")
    return "\n".join(lines)


def value_hits(df: pd.DataFrame, question: str, limit: int = 12) -> list[str]:
    """'column = value' for values that appear verbatim (whole token) in the question."""
    out, q = [], question
    toks = set(re.findall(r"[^\s,;:()?\"']{3,}", q))
    for c in df.columns:
        if df[c].dtype.kind in "fiub" and len(df) > 200_000:  # huge numeric columns (e.g. point clouds): no verbatim ids there
            continue
        col = df[c].dropna().astype(str).str.strip()
        if len(col) > 200_000:  # huge text columns: only test question tokens for membership (fast) instead of scanning values
            hit = col[col.isin(toks)].unique()[:20]
            out += [f"{c} = {v}" for v in hit]
            continue
        vals = set(col)
        if len(vals) > 200_000:
            continue
        for v in vals:
            if len(v) < 3 or v not in q:
                continue
            i = q.find(v)
            if (i > 0 and (q[i - 1].isalnum() or q[i - 1] in "/-_")) or (i + len(v) < len(q) and (q[i + len(v)].isalnum() or q[i + len(v)] in "/-_")):
                continue
            out.append(f"{c} = {v}")
    out.sort(key=lambda x: -len(x))
    return out[:limit]


# ---------------------------------------------------------------- strict type checking of a model-built query
_TYPES: dict = {}
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?")


def col_type(df: pd.DataFrame, c: str) -> str:
    """numeric | date | json | text (cached per table and column; decided on a sample of non-blank values)."""
    key = (id(df), len(df), c)
    if key not in _TYPES:
        s = df[c]
        if pd.api.types.is_numeric_dtype(s):
            t = "numeric"
        else:
            v = s[~is_blank_series(s)]
            v = v.sample(min(len(v), 5000), random_state=0) if len(v) > 5000 else v
            sv = v.astype(str).str.strip()
            if not len(sv):
                t = "text"
            elif to_num(sv).notna().mean() > 0.9:
                t = "numeric"
            elif sv.str.match(_ISO).mean() > 0.9:
                t = "date"
            elif sv.str[:1].isin(["[", "{"]).mean() > 0.5:
                t = "json"
            else:
                t = "text"
        _TYPES[key] = t
    return _TYPES[key]


def _norm(x: str) -> str:
    return re.sub(r"[^0-9a-zà-ÿ]+", "", str(x).lower())


def snap_value(s: pd.Series, v: str) -> str | None:
    """The single data value the model clearly meant (quotes/punctuation/case differences, truncation, small typos), else None."""
    vals = list(pd.Series(s[~is_blank_series(s)].astype(str).str.strip().unique()))
    if len(vals) > 200_000:
        return None
    nv = _norm(v)
    if not nv:
        return None
    same = [x for x in vals if _norm(x) == nv]
    if len(same) == 1:
        return same[0]
    if len(nv) >= 8:
        pre = [x for x in vals if _norm(x).startswith(nv)]
        if len(pre) == 1:
            return pre[0]
    if len(nv) < 5:  # short values: no fuzzy guessing ('1' must not become '14')
        return None
    close = difflib.get_close_matches(v.lower(), [x.lower() for x in vals[:20000]], n=2, cutoff=0.88)
    if len(close) == 1 or (len(close) == 2 and difflib.SequenceMatcher(None, v.lower(), close[0]).ratio()
                           - difflib.SequenceMatcher(None, v.lower(), close[1]).ratio() > 0.05):
        return next(x for x in vals if x.lower() == close[0])
    return None


def check_query(df: pd.DataFrame, q: dict) -> list[str]:
    """Validate every field of a query against the table's column types; fix what is unambiguous (snapped filter values) in
    place and return notes about the fixes. Raises QueryError listing ALL problems at once, each with what is allowed."""
    errs, notes = [], []
    cols = [str(c) for c in df.columns]

    def known(c, where):
        if c in df.columns:
            return True
        near = difflib.get_close_matches(str(c), cols, n=3, cutoff=0.5)
        errs.append(f"{where}: unknown column {c!r}" + (f"; did you mean {near}?" if near else ""))
        return False

    for i, f in enumerate(q.get("filters") or []):
        c, op = f.get("column"), f.get("op")
        v = str(f.get("value", "") or "").strip()
        m_ = re.fullmatch(r"(.+?)\s*\(\d[\d,]*\)", v)  # copied from the profile's top list: 'value' (count)
        if m_ and re.fullmatch(r"(['\"`]).*\1", m_.group(1).strip()):
            v = m_.group(1).strip()
        while len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"`":
            v = v[1:-1].strip()
        m_ = re.fullmatch(r"(<=|>=|<|>|=)\s*(-?[\d.,]+)", v)  # '<16' written into the value: move the operator out
        if m_ and op == "=":
            op, v = m_.group(1), m_.group(2)
            f["op"] = op
            notes.append(f"filter on {c}: operator {op} taken from the value")
        f["value"] = v
        w = f"filters[{i}] ({c} {op} {v!r})"
        if not known(c, w):
            continue
        if op in ("is_blank", "not_blank"):
            f["value"] = ""
            continue
        if not v:
            errs.append(f"{w}: op {op!r} needs a value (or use is_blank / not_blank)")
            continue
        t = col_type(df, c)
        if t == "numeric":
            if op in ("contains", "not_contains"):
                errs.append(f"{w}: {c} is a NUMERIC column; use =, !=, <, <=, >, >= with a plain number")
                continue
            y = pd.to_numeric(v.replace(",", "").replace(" ", ""), errors="coerce")
            if pd.isna(y):
                x = to_num(df[c])
                errs.append(f"{w}: {c} is a NUMERIC column (values {x.min():g} to {x.max():g}); the value must be one plain number such as "
                            f"{x.median():g}, not {v!r}. To select records by a word or label, filter a TEXT column instead")
                continue
            if pd.api.types.is_numeric_dtype(df[c]):
                f["value"] = str(y)
        elif t == "json":
            if op in ("=", "!="):
                s_ = df[c].astype(str).str.strip()
                if (s_ == v).any():
                    continue
                toks = sorted(re.findall(r"[\w.:-]{4,}", v), key=len, reverse=True)  # key text inside the JSON the model wrote
                toks = [x for x in toks if x not in ("id", "type", "name", "value")]
                if toks and s_.str.contains(re.escape(toks[0]), na=False).any():
                    f["op"], f["value"] = ("contains" if op == "=" else "not_contains"), toks[0]
                    notes.append(f"{c} holds JSON; '{op} {v[:40]}' changed to {f['op']} {toks[0]!r}")
                    continue
                errs.append(f"{w}: {c} holds JSON lists/objects; use contains / not_contains with the text to find inside them")
                continue
        if t == "date" and not _ISO.match(v):  # '16 January 2012', '03/04/2025' -> ISO (day first, as in European sources)
            d_ = pd.to_datetime(v, errors="coerce", dayfirst=True)
            if not pd.isna(d_):
                iso = d_.strftime("%Y-%m-%d")
                notes.append(f"date {v!r} written as {iso}")
                v = f["value"] = iso
        if t in ("numeric", "json"):
            pass
        elif op in (">", ">=", "<", "<="):
            if t == "date" and not _ISO.match(v):
                errs.append(f"{w}: {c} is a DATE column (YYYY-MM-DD); compare with a date written like 2024-03-31, not {v!r}")
                continue
            if t == "text" and pd.isna(pd.to_numeric(v.replace(",", ""), errors="coerce")):
                errs.append(f"{w}: {c} is a TEXT column; <, >, <=, >= need a number or a YYYY-MM-DD date. Use = or contains for text")
                continue
        if op in ("=", "!=") and t == "date" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            ss = df[c].astype(str).str.strip()
            if not (ss == v).any():
                if ss.str.startswith(v).any():  # date-only value on a date-time column: match the whole day
                    f["op"] = "starts_with" if op == "=" else "not_starts_with"
                    notes.append(f"{c} holds date-times; matching every time on {v}")
                elif op == "=":
                    errs.append(f"{w}: no record has {c} on {v}; dates in {c} run from {ss.min()} to {ss.max()}")
            continue
        if op in ("=", "!=") and t in ("text", "date"):
            ss = df[c].astype(str).str.strip().str.lower()
            if not (ss == v.lower()).any():
                fix = snap_value(df[c], v)
                if fix is not None:
                    notes.append(f"filter value {v!r} matched to the data value {fix!r} in {c}")
                    f["value"] = fix
                elif op == "=":
                    close = difflib.get_close_matches(v, list(df[c].astype(str).str.strip().unique())[:5000], n=5, cutoff=0.5)
                    errs.append(f"{w}: no record has {c} = {v!r}" + (f"; values that exist: {close}" if close else
                                f"; use contains with a shorter word, or another column"))
    agg, tgt, grp = q.get("aggregate"), q.get("target") or "", q.get("group_by") or ""
    if agg not in AGGS:
        errs.append(f"aggregate {agg!r} is not one of {AGGS}")
    if tgt and known(tgt, "target"):
        t = col_type(df, tgt)
        if agg in ("sum", "mean", "median") and t != "numeric":
            numc = [c for c in cols if col_type(df, c) == "numeric"][:12]
            errs.append(f"target: {agg} needs a NUMERIC column but {tgt} is {t.upper()}; numeric columns: {numc}")
        elif agg in ("min", "max") and t not in ("numeric", "date"):
            errs.append(f"target: {agg} needs a NUMERIC or DATE column but {tgt} is {t.upper()}; use count_distinct / list_values for text")
    elif agg and agg != "count" and not tgt:
        errs.append(f"target: aggregate {agg} needs a target column")
    if grp and known(grp, "group_by"):
        t = col_type(df, grp)
        if t == "json":
            errs.append(f"group_by: {grp} holds JSON lists/objects; group by a plain category column")
        else:
            nun = df[grp].nunique(dropna=False)
            if nun > 5000:
                errs.append(f"group_by: {grp} has {nun:,} distinct values; group by a category column (or filter) instead")
    if errs:
        raise QueryError("; ".join(errs))
    return notes


# ---------------------------------------------------------------- execution
def _mask(df: pd.DataFrame, f: dict) -> pd.Series:
    c, op, v = f["column"], f["op"], str(f.get("value", "")).strip()
    while len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"`":  # the model often wraps values in quotes
        v = v[1:-1].strip()
    s = df[c]
    if op == "is_blank":
        return is_blank_series(s)
    if op == "not_blank":
        return ~is_blank_series(s)
    if pd.api.types.is_numeric_dtype(s):  # numeric column: numeric comparisons only (fast on millions of rows)
        if op in ("contains", "not_contains"):
            raise QueryError(f"{c} is numeric; use =, !=, <, <=, >, >= with a number")
        y = pd.to_numeric(str(v).replace(",", ""), errors="coerce")
        if pd.isna(y):
            raise QueryError(f"{c} is numeric; the value {v!r} is not a number")
        x = s.astype("float64")
        m = {"=": (x - y).abs() < 1e-9, "!=": (x - y).abs() >= 1e-9, ">": x > y, ">=": x >= y, "<": x < y, "<=": x <= y}[op]
        if op == "=" and not m.any():
            raise QueryError(f"no record has {c} = {v!r} (range {x.min():g}..{x.max():g})")
        return m
    if op in ("contains", "not_contains"):
        m = s.astype(str).str.contains(re.escape(v), case=False, na=False)
        return m if op == "contains" else ~m
    if op in ("starts_with", "not_starts_with"):  # set by check_query (date-only value on a date-time column)
        m = s.astype(str).str.strip().str.startswith(v)
        return m if op == "starts_with" else ~m
    if op in (">", ">=", "<", "<="):
        x, y = to_num(s), pd.to_numeric(str(v).replace(",", ""), errors="coerce")
        if pd.isna(y):  # dates / strings compare as text (ISO dates sort correctly)
            x, y = s.astype(str).str.strip(), v
        return {">": x > y, ">=": x >= y, "<": x < y, "<=": x <= y}[op].fillna(False)
    # = / != : exact (trimmed, case-insensitive); numeric equality when both numeric
    ss = s.astype(str).str.strip()
    m = ss.str.lower() == v.lower()
    if not m.any():
        xn, yn = to_num(s), pd.to_numeric(v.replace(",", ""), errors="coerce")
        if not pd.isna(yn):
            m = (xn - yn).abs() < 1e-9
    if not m.any() and op == "=":
        close = difflib.get_close_matches(v, list(ss.unique())[:5000], n=5, cutoff=0.6)
        raise QueryError(f"no record has {c} = {v!r}" + (f"; closest values: {close}" if close else ""))
    return m if op == "=" else ~m


TOTAL_ROW = re.compile(r"^\s*(grand\s+|sub-?)?totals?\b|^\s*sum\s*$|^\s*all\s+(records|items|categories|types|sectors|areas)\s*$", re.I)


def total_rows(df: pd.DataFrame) -> pd.Series:
    """Rows whose label cell says Total/Totals/Grand total/Subtotal/Sum: aggregates over detail rows must not add them in."""
    m = pd.Series(False, index=df.index)
    for c in df.columns:
        s = df[c]
        if pd.api.types.is_object_dtype(s) or pd.api.types.is_string_dtype(s):
            m |= s.astype(str).str.match(TOTAL_ROW, na=False)
    return m


def run_query(df: pd.DataFrame, q: dict, name: str) -> tuple[list[str], list[str], str]:
    """Returns (facts, key numbers, evidence locator)."""
    note = ""
    if q.get("aggregate") in ("sum", "mean", "median", "min", "max", "count", "count_distinct") and len(df) <= 2_000_000:
        tm = total_rows(df)
        asked_total = any(TOTAL_ROW.match(str(f.get("value", ""))) for f in q.get("filters") or [])
        if tm.any() and not tm.all() and not asked_total:  # lesson 2: never add a Totals row into its own detail rows
            note = f" ({int(tm.sum())} total/subtotal row(s) excluded)"
            df = df[~tm]
    total = len(df)
    m = pd.Series(True, index=df.index)
    desc = []
    for f in q.get("filters") or []:
        if f["column"] not in df.columns:
            raise QueryError(f"unknown column {f['column']!r}")
        m &= _mask(df, f)
        desc.append(f"{f['column']} {f['op']} {f.get('value', '')}".strip())
    sub = df[m]
    agg, tgt, grp = q["aggregate"], q.get("target") or "", q.get("group_by") or ""
    where = f" where {' and '.join(desc)}" if desc else ""
    facts, keys = [], []
    fmt = lambda x: (f"{x:,.2f}" if abs(x - round(x)) > 1e-9 else f"{int(round(x)):,}") if isinstance(x, (int, float)) else str(x)

    def aggregate(frame):
        if agg == "count":
            return len(frame)
        col = frame[tgt]
        if agg == "count_distinct":
            return int(col[~is_blank_series(col)].astype(str).str.strip().nunique())
        if agg in ("sum", "mean", "median", "min", "max"):
            x = to_num(col)
            if x.notna().sum() == 0:
                if agg in ("min", "max"):
                    v = col[~is_blank_series(col)].astype(str)
                    return (v.min() if agg == "min" else v.max()) if len(v) else None
                raise QueryError(f"column {tgt!r} has no numeric values")
            return float(getattr(x, agg)())
        if agg == "list_values":
            return ", ".join(col.astype(str).head(15))
        if agg == "duplicates":
            v = col[~is_blank_series(col)].astype(str).str.strip()
            d = v[v.duplicated(keep=False)]
            return f"{d.nunique()} duplicated values in {len(d)} records" + (f": {sorted(d.unique())[:8]}" if len(d) else "")
        raise QueryError(f"unknown aggregate {agg}")

    if agg != "count" and tgt not in df.columns:
        raise QueryError(f"aggregate {agg} needs a target column")
    if grp:
        if grp not in df.columns:
            raise QueryError(f"unknown group_by column {grp!r}")
        nun = sub[grp].nunique(dropna=False)
        if nun > 5000:
            raise QueryError(f"group_by column {grp!r} has {nun:,} distinct values; group by a category column (or filter) instead")
        g = sub.assign(_g=sub[grp].where(~is_blank_series(sub[grp]), "(blank)").astype(str).str.strip())
        if agg == "count":  # vectorised paths for the common aggregates
            res = g.groupby("_g").size().astype(int).to_dict()
        elif agg in ("sum", "mean", "median", "min", "max") and to_num(g[tgt]).notna().any():
            res = getattr(g.assign(_v=to_num(g[tgt])).groupby("_g")["_v"], agg)().dropna().astype(float).to_dict()
        else:
            res = {k: aggregate(v) for k, v in g.groupby("_g")}
        asc = q.get("sort") == "asc"
        numv = lambda kv: kv[1] if isinstance(kv[1], (int, float)) else float("-inf")
        items = sorted(res.items(), key=numv, reverse=not asc)
        top = max(1, min(int(q.get("top") or 10), 20))
        cut = items[:top]
        if cut and isinstance(cut[-1][1], (int, float)):  # keep every tie at the cut-off value
            edge = cut[-1][1]
            cut = [kv for kv in items if isinstance(kv[1], (int, float)) and (kv[1] <= edge if asc else kv[1] >= edge)]
        facts.append(f"{name}: {agg}{'(' + tgt + ')' if tgt else ''} by {grp}{where} ({len(sub)} of {total} records):")
        for k, v in cut[:25]:
            share = f" ({100 * v / len(sub):.2f}% of {len(sub)})" if agg == "count" and len(sub) else ""
            facts.append(f"  {k}: {fmt(v)}{share}")
            if isinstance(v, (int, float)):
                keys.append(fmt(v))
        facts.append(f"  ({len(res)} groups in total)")
    else:
        v = aggregate(sub)
        if agg == "count":
            pct = f" ({100 * v / total:.2f}% of all {total} records)" if desc and total else f" (all records)"
            facts.append(f"{name}: number of records{where} = {fmt(v)}{pct}")
        else:
            facts.append(f"{name}: {agg} of {tgt}{where} = {fmt(v)} (over {len(sub)} of {total} records)")
        if isinstance(v, (int, float)):
            keys.append(fmt(v))
        if agg in ("sum", "mean", "median", "min", "max") and tgt:
            bad = int((to_num(sub[tgt]).isna() & ~is_blank_series(sub[tgt])).sum())
            if bad:
                facts.append(f"  ({bad} non-numeric values in {tgt} were skipped)")
        if agg in ("min", "max") and tgt and len(sub):  # identify the record(s) holding the extreme
            x = to_num(sub[tgt])
            if x.notna().any():
                ext = x.max() if agg == "max" else x.min()
                idcol = next((c for c in sub.columns if not pd.api.types.is_numeric_dtype(sub[c]) and
                              re.search(r"(^|_)(id|ref|reference|code|name|nummer|number)$", str(c), re.I)), None)
                if idcol:
                    facts.append(f"  record(s) with that {agg}: " + ", ".join(sub.loc[x == ext, idcol].astype(str).head(5)))
    if note and facts:
        facts[0] += note
    loc = f"{name}, columns {', '.join(dict.fromkeys([f['column'] for f in q.get('filters') or []] + [c for c in (tgt, grp) if c]))}{where}"
    return facts, keys, loc
