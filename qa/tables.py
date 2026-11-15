"""Normalise spreadsheet sheets into labelled tables: entities x periods -> cell address.

Two layouts are detected generically:
  A) periods across a header row (e.g. a row of years), entity labels in the column left of the year run;
     a sheet may stack several blocks (each with its own header row) and several side-by-side tables;
  B) periods down a column (a run of consecutive years), entity labels in the header row above.
The model only ever chooses among these labels; code resolves them to cells and reads values.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .tools import Ods, Xlsx

YEAR = re.compile(r"^\s*((?:19|20)\d\d)(?:\s*[-/]\s*\d{2,4})?(?:\s*\[.*\])?\s*$")
SCALE = [(re.compile(r"\bbillions?\b", re.I), 1e9), (re.compile(r"\bmillions?\b", re.I), 1e6), (re.compile(r"\bthousands?\b", re.I), 1e3)]


def clean_name(p: Path) -> str:
    return re.sub(r"^SRC_[0-9a-f]+__", "", Path(p).name)


def col_letter(i: int) -> str:
    s = ""
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _year(t: str | None) -> str | None:
    if t is None:
        return None
    m = YEAR.match(str(t))
    return m.group(0).strip() if m else None


@dataclass
class Table:
    id: str
    file: Path
    sheet: str
    title: str
    unit: str
    entities: list[str] = field(default_factory=list)
    periods: list[str] = field(default_factory=list)
    addr: dict = field(default_factory=dict)  # (entity, period) -> "A1"
    rows: dict = field(default_factory=dict)  # entity -> sheet row (layout A)
    raw: dict = field(default_factory=dict)  # (entity, period) -> cell text, for structure checks
    aggregates: dict = field(default_factory=dict)  # block -> [blocks it is the sum of]

    @property
    def scale(self) -> float:
        for rx, s in SCALE:
            if rx.search(self.unit or "") or rx.search(self.title or ""):
                return s
        return 1.0

    def describe(self) -> str:
        p = f"{self.periods[0]}..{self.periods[-1]}" if self.periods else "-"
        ent = ", ".join(self.entities[:8]) + (" ..." if len(self.entities) > 8 else "")
        return f"[{self.id}] {self.title[:160]} | unit: {self.unit or '?'} | periods {p} | entities ({len(self.entities)}): {ent}"


def _grid(path: Path):
    """{sheet: {(r,c): text}} using displayed text where the format provides it."""
    out = {}
    if path.suffix.lower() == ".ods":
        o = Ods(path)
        for sh in o.sheets():
            g = {}
            for (r, c), (stored, shown) in o._grid(sh).items():
                t = shown if shown else (None if stored is None else str(stored))
                if t not in (None, ""):
                    g[(r, c)] = t
            out[sh] = g
    else:
        x = Xlsx(path)
        for sh in x.sheets():
            ws = x.wb[sh]
            g = {}
            for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 400), max_col=min(ws.max_column, 120)):
                for c in row:
                    if c.value is not None and str(c.value).strip() != "":
                        v = c.value
                        g[(c.row, c.column)] = str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)
            out[sh] = g
    return out


def _title_above(g, r, c, lo=3):
    """Nearest text above the header row within a few columns to the left; the leftmost candidate wins
    (titles sit at the table's left edge, notes to their right)."""
    for rr in range(r - 1, 0, -1):
        cands = [cc for cc in range(max(1, c - lo), c + 1) if (rr, cc) in g and not _year(g[(rr, cc)])]
        if cands:
            return g[(rr, min(cands))]
    return ""


def _same(a, b) -> bool:
    if a == b:
        return True
    try:
        fa, fb = float(str(a).replace(",", "")), float(str(b).replace(",", ""))
        return abs(fa - fb) <= 1e-9 * max(1.0, abs(fa), abs(fb))
    except (TypeError, ValueError):
        return False


def _unit_text(unit_row: str, title: str) -> str:
    """Unit from the line above the header (e.g. 'Passenger journeys (millions)'), else from the title."""
    for t in (unit_row, title):
        m = re.findall(r"\(([^()]{1,40})\)", t or "")
        if m:
            return m[-1]
    return unit_row if unit_row and len(unit_row) <= 60 else ""


def tables_from_sheet(path: Path, sheet: str, g: dict) -> list[Table]:
    if not g:
        return []
    rows = sorted({r for r, _ in g})
    ncol = max(c for _, c in g)
    fname = clean_name(path)
    sheet_title = g.get((1, 1), "")
    found: list[Table] = []

    # ---- layout A: header rows with a run of consecutive years
    runs_by_row = {}
    for r in rows:
        cols = [c for c in range(1, ncol + 1) if _year(g.get((r, c)))]
        runs, cur = [], []
        for c in cols:
            if cur and c == cur[-1] + 1 and int(_year(g[(r, c)])[:4]) > int(_year(g[(r, cur[-1])])[:4]):
                cur.append(c)
            else:
                if len(cur) >= 3:
                    runs.append(cur)
                cur = [c]
        if len(cur) >= 3:
            runs.append(cur)
        if runs:
            runs_by_row[r] = runs
    tabs: dict[tuple, Table] = {}
    for r, runs in runs_by_row.items():
        for run in runs:
            lc = run[0] - 1
            key = (lc, run[0])  # same column span across stacked blocks -> same table
            if key not in tabs:
                title = _title_above(g, min(rr for rr in runs_by_row if any(x[0] == run[0] for x in runs_by_row[rr])), lc + 1)
                k = len([t for t in tabs]) + 1
                tid = f"{fname} | {sheet}" + (f" | table {k} (cols {col_letter(run[0])}-{col_letter(run[-1])})" if len({x[0] for rs in runs_by_row.values() for x in rs}) > 1 else "")
                tabs[key] = Table(tid, path, sheet, title or sheet_title, _unit_text(title, sheet_title), periods=[_year(g[(r, c)]) for c in run])
            t = tabs[key]
            block = g.get((r, lc), "") if lc >= 1 else ""
            # entity rows: until the next header row of this table, a blank label, or the end of the sheet
            rr, last = r + 1, max(rows)
            while rr <= last:
                if rr in runs_by_row and any(x[0] == run[0] for x in runs_by_row[rr]):
                    break
                lab = g.get((rr, lc)) if lc >= 1 else None
                if not lab:
                    break
                if all(g.get((rr, c)) is None for c in run):  # sub-heading without data
                    rr += 1
                    continue
                name = f"{block} / {lab}" if block else lab
                if name in t.entities:  # stacked duplicate (e.g. a repeated total): drop if identical, else suffix
                    if all(_same(g.get((rr, c)), g.get((t.rows[name], c))) for c in run):
                        rr += 1
                        continue
                    name = f"{name} (2)"
                t.entities.append(name)
                t.rows[name] = rr
                for i, c in enumerate(run):
                    t.addr[(name, t.periods[i])] = f"{col_letter(c)}{rr}"
                    t.raw[(name, t.periods[i])] = g.get((rr, c))
                rr += 1
    for t in tabs.values():
        detect_aggregates(t)
    found += [t for t in tabs.values() if t.entities]

    # ---- layout B: a column holding consecutive years down the rows
    if not found:
        for c in range(1, min(ncol, 6) + 1):
            yr_rows = [r for r in rows if _year(g.get((r, c)))]
            run = []
            for r in yr_rows:
                if run and r == run[-1] + 1 and int(_year(g[(r, c)])[:4]) > int(_year(g[(run[-1], c)])[:4]):
                    run.append(r)
                else:
                    if len(run) >= 3:
                        break
                    run = [r]
            if len(run) < 3:
                continue
            hr = run[0] - 1
            heads = {cc: g[(hr, cc)] for cc in range(c + 1, ncol + 1) if (hr, cc) in g}
            if len(heads) < 1:
                continue
            unit_row = g.get((hr - 1, 1), "")
            t = Table(f"{fname} | {sheet}", path, sheet, sheet_title, _unit_text(unit_row, sheet_title), periods=[_year(g[(r, c)]) for r in run])
            for cc, h in heads.items():
                if not any((r, cc) in g for r in run):
                    continue
                t.entities.append(h)
                for i, r in enumerate(run):
                    t.addr[(h, t.periods[i])] = f"{col_letter(cc)}{r}"
            if t.entities:
                found.append(t)
            break
    return found


def _f(x):
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return None


def detect_aggregates(t: Table) -> None:
    """A block whose every row equals the sum of the same rows in the other blocks (in every period checked) is an
    area/group total of those blocks, e.g. a region block summing its cities. Purely structural; recorded for the model."""
    blocks = {}
    for e in t.entities:
        if " / " in e:
            b, lab = e.split(" / ", 1)
            blocks.setdefault(b, set()).add(lab)
    if len(blocks) < 3:
        return
    periods = t.periods[-3:]
    for b, labs in blocks.items():
        others = [o for o in blocks if o != b]
        hit = checked = 0
        for lab in labs:
            parts = [o for o in others if lab in blocks[o]]
            if len(parts) < 2:
                continue
            for p in periods:
                tot = _f(t.raw.get((f"{b} / {lab}", p)))
                vals = [_f(t.raw.get((f"{o} / {lab}", p))) for o in parts]
                if tot is None or any(v is None for v in vals):
                    continue
                checked += 1
                hit += abs(tot - sum(vals)) <= 1e-6 * max(1.0, abs(tot))
        if checked >= 6 and hit >= 0.8 * checked:  # published totals can carry small source quirks
            t.aggregates[b] = others


def workbook_tables(path: Path) -> list[Table]:
    out = []
    for sh, g in _grid(path).items():
        try:
            out += tables_from_sheet(path, sh, g)
        except Exception:
            continue
    return out


def sheet_text(path: Path, sheet: str, max_chars: int = 3000) -> str:
    """Plain text of a (notes-like) sheet, for explanation questions."""
    g = _grid(path).get(sheet, {})
    lines = []
    for r in sorted({r for r, _ in g}):
        cells = [g[(r, c)] for c in sorted(c for rr, c in g if rr == r)]
        lines.append(f"r{r}: " + " | ".join(cells))
    return "\n".join(lines)[:max_chars]
