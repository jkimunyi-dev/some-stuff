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
    return pd.json_normalize(recs, sep="."), best[1]


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
    return s.isna() | s.astype(str).str.strip().isin(["", "nan", "None", "NaN", "null"])


def to_num(s: pd.Series) -> pd.Series:
    t = s.astype(str).str.strip().str.replace(" ", "", regex=False).str.replace(" ", "", regex=False)
    # decimal comma (e.g. 158,006) only when no dot is present and exactly one comma
    t = t.where(~(t.str.count(",") == 1) | t.str.contains(r"\.", regex=True), t.str.replace(",", ".", regex=False))
    t = t.str.replace(",", "", regex=False)
    return pd.to_numeric(t, errors="coerce")


def profile(df: pd.DataFrame, max_cols: int = 40) -> str:
    lines = [f"{len(df)} records"]
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
            lines.append(f"- {c}: {vc.size} distinct, blanks {blank}; top: {ex}")
    return "\n".join(lines)


def value_hits(df: pd.DataFrame, question: str, limit: int = 12) -> list[str]:
    """'column = value' for values that appear verbatim (whole token) in the question."""
    out, q = [], question
    for c in df.columns:
        vals = set(df[c].dropna().astype(str).str.strip())
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


# ---------------------------------------------------------------- execution
def _mask(df: pd.DataFrame, f: dict) -> pd.Series:
    c, op, v = f["column"], f["op"], str(f.get("value", "")).strip()
    s = df[c]
    if op == "is_blank":
        return is_blank_series(s)
    if op == "not_blank":
        return ~is_blank_series(s)
    if op in ("contains", "not_contains"):
        m = s.astype(str).str.contains(re.escape(v), case=False, na=False)
        return m if op == "contains" else ~m
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


def run_query(df: pd.DataFrame, q: dict, name: str) -> tuple[list[str], list[str], str]:
    """Returns (facts, key numbers, evidence locator)."""
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
        g = sub.assign(_g=sub[grp].where(~is_blank_series(sub[grp]), "(blank)").astype(str).str.strip())
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
                idcol = next((c for c in sub.columns if re.search(r"(^|_)(id|ref|reference|code|name|nummer|number)$", str(c), re.I)), None)
                if idcol:
                    facts.append(f"  record(s) with that {agg}: " + ", ".join(sub.loc[x == ext, idcol].astype(str).head(5)))
    loc = f"{name}, columns {', '.join(dict.fromkeys([f['column'] for f in q.get('filters') or []] + [c for c in (tgt, grp) if c]))}{where}"
    return facts, keys, loc
