"""Steps 5-6 (first cut): model plans -> code validates and runs tools -> model writes the answer from tool results.

Generic: no pack IDs, question IDs or answer constants. Conventions that Context fixes (displayed vs stored values, rounding)
are parsed from the Context text by code, not left to the model. Every model call goes through qa.llm.LLM (budget ledger).
"""
from __future__ import annotations

import ast
import json
import math
import re
from pathlib import Path

from .cards import card_for_file, guide_text, is_guide
from .llm import LLM, BudgetExceeded, Ledger
from .tools import Ods, Recorder, Xlsx, days_between, pct_change, round_half_up, share, split_a1, col_to_idx, read_table

SKIP_SHEET = re.compile(r"cover|contents|notes|about", re.I)


def clean_name(p: Path) -> str:
    return re.sub(r"^SRC_[0-9a-f]+__", "", Path(p).name)


# ------------------------------------------------------------------ digests (what the model sees about each file)
def _idx_to_col(i: int) -> str:
    s = ""
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _is_year(v) -> bool:
    try:
        f = float(str(v).strip())
        return f == int(f) and 1900 <= f <= 2100
    except ValueError:
        return False


def _sheet_digest(name: str, grid: dict, max_rows: int = 45, max_cols: int = 6) -> str:
    """grid: {(row, col): text}. Shows addressed cells for the top-left block and a year->column map for wide sheets."""
    lines = [f"sheet '{name}'"]
    rows = sorted({r for r, _ in grid})
    ncols = max((c for _, c in grid), default=0)
    for r in rows[:max_rows]:
        cells = [f"{_idx_to_col(c)}{r}={str(grid[(r, c)])[:28]!r}" for c in range(1, max_cols + 1) if grid.get((r, c)) not in (None, "")]
        if cells:
            lines.append("  " + " ".join(cells))
    if ncols > max_cols:
        for r in rows[:12]:
            cols = [c for c in range(1, ncols + 1) if _is_year(grid.get((r, c)))]
            if len(cols) < 5:
                continue
            runs, cur = [], [cols[0]]  # a sheet may repeat its year header (a second table further right): report each run
            for c in cols[1:]:
                if c - cur[-1] <= 2:
                    cur.append(c)
                else:
                    runs.append(cur)
                    cur = [c]
            runs.append(cur)
            for k, run in enumerate(runs, 1):
                m = ", ".join(f"{str(grid[(r, c)]).split('.')[0]}:{_idx_to_col(c)}" for c in run[-8:])
                lines.append(f"  year->column, row {r}, table {k} (cols {_idx_to_col(run[0])}-{_idx_to_col(run[-1])}, last 8 years): {m}")
            break
    return "\n".join(lines)


def digest(path: Path) -> str:
    suf = Path(path).suffix.lower()
    name = clean_name(path)
    try:
        if suf == ".xlsx":
            x = Xlsx(path)
            parts = []
            for sh in x.sheets():
                if SKIP_SHEET.search(sh):
                    continue
                ws = x.wb[sh]
                grid = {(c.row, c.column): c.value for row in ws.iter_rows(min_row=1, max_row=60, max_col=70) for c in row if c.value is not None}
                parts.append(_sheet_digest(sh, grid))
            return f"FILE {name}\n" + "\n".join(parts[:4])
        if suf == ".ods":
            o = Ods(path)
            parts = []
            for sh in o.sheets():
                if SKIP_SHEET.search(sh):
                    continue
                g = {k: (v[1] or v[0]) for k, v in o._grid(sh).items() if k[0] <= 60 and k[1] <= 70 and (v[1] or v[0]) not in (None, "")}
                parts.append(_sheet_digest(sh, g, max_rows=34, max_cols=7))
            return f"FILE {name}\n" + "\n".join(parts[:6])
        if is_guide(path):
            return f"GUIDE {name}\n" + guide_text(path)[:2500]
        return f"FILE {name}\n" + card_for_file(path)[:2500]
    except Exception as e:  # a digest failure must not kill the run
        return f"FILE {name} (digest failed: {type(e).__name__})"


# ------------------------------------------------------------------ tools
TOOL_HELP = """Tools (JSON steps; each has a unique "id"; later steps may use earlier ids as variables in "expr"):
- {"id":"a","tool":"cell","args":{"file":"<file name>","sheet":"<sheet>","ref":"<A1 address>"}}  -> number from a workbook cell
- {"id":"t","tool":"csv_value","args":{"file":"<csv>","where_col":"<column>","where_val":"<value>","col":"<column to read>"}}  -> text from the row where where_col==where_val
- {"id":"p","tool":"polygon_area_ha","args":{"file":"<geojson>","id_field":"<property>","id_value":"<value>","crs":"EPSG:xxxx"}}  -> area in hectares (planar, in that CRS)
- {"id":"r","tool":"calc","args":{"expr":"<formula>"}}  formulas use earlier ids, + - * /, and functions
    pct_change(old,new), share(part,whole), days(date_from,date_to)  (dates are ISO strings from csv_value; days = to - from)
Reply with ONLY JSON: {"steps":[...],"final":"<id of the step holding the main result>"}"""


class ToolError(Exception):
    pass


def _find_file(files: list[Path], want: str) -> Path:
    w = want.lower().strip()
    for f in files:
        if clean_name(f).lower() == w:
            return f
    hits = [f for f in files if w in clean_name(f).lower() or clean_name(f).lower() in w]
    if len(hits) == 1:
        return hits[0]
    raise ToolError(f"file not found/ambiguous: {want!r}")


_SAFE = {"pct_change": pct_change, "share": share, "days": days_between, "abs": abs, "round": round}


def safe_eval(expr: str, env: dict):
    tree = ast.parse(expr, mode="eval")
    for n in ast.walk(tree):
        if not isinstance(n, (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Call, ast.Name, ast.Load, ast.Constant, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.USub, ast.UAdd, ast.Pow)):
            raise ToolError(f"disallowed syntax in expr: {type(n).__name__}")
        if isinstance(n, ast.Call) and not (isinstance(n.func, ast.Name) and n.func.id in _SAFE):
            raise ToolError("disallowed function")
    return eval(compile(tree, "<expr>", "eval"), {"__builtins__": {}}, {**_SAFE, **env})


class Runner:
    def __init__(self, files: list[Path], context: str):
        self.files, self.rec = files, Recorder()
        self.displayed = bool(re.search(r"visibly formatted|as displayed|displayed value|as shown", context, re.I))
        self.vals: dict = {}
        self._wb: dict = {}

    def _book(self, f: Path):
        if f not in self._wb:
            self._wb[f] = Ods(f, self.rec) if f.suffix.lower() == ".ods" else Xlsx(f, self.rec)
        return self._wb[f]

    def run(self, plan: dict) -> dict:
        steps = plan.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ToolError("plan has no steps")
        for s in steps:
            sid, tool, a = s.get("id"), s.get("tool"), s.get("args", {})
            if not sid or sid in self.vals:
                raise ToolError(f"bad/duplicate id {sid!r}")
            if tool == "cell":
                f = _find_file(self.files, a["file"])
                bk = self._book(f)
                v = bk.cell(a["sheet"], a["ref"], shown=self.displayed)
                if v is None:
                    raise ToolError(f"empty cell {a['sheet']}!{a['ref']}")
                self.vals[sid] = v
            elif tool == "csv_value":
                f = _find_file(self.files, a["file"])
                df = read_table(f)
                m = df[df[a["where_col"]] == str(a["where_val"])]
                if len(m) != 1:
                    raise ToolError(f"{len(m)} rows where {a['where_col']}=={a['where_val']}")
                self.vals[sid] = m.iloc[0][a["col"]]
                self.rec.add(f"{clean_name(f)}, {a['where_col']}={a['where_val']}, column {a['col']}")
            elif tool == "polygon_area_ha":
                from .tools import polygon_area
                f = _find_file(self.files, a["file"])
                r = polygon_area(f, a["id_field"], str(a["id_value"]), a["crs"], rec=self.rec)
                if r.value is None:
                    raise ToolError(r.scope)
                self.vals[sid] = r.value / 10_000
            elif tool == "calc":
                self.vals[sid] = safe_eval(str(a["expr"]), {k: v for k, v in self.vals.items()})
            else:
                raise ToolError(f"unknown tool {tool!r}")
        final = plan.get("final") or steps[-1]["id"]
        if final not in self.vals:
            raise ToolError(f"final id {final!r} missing")
        return self.vals


# ------------------------------------------------------------------ prompts
PLAN_SYS = ("You plan data calculations. Use ONLY the tools described. Read exact values from the files shown; never invent numbers. "
            "Follow the Context's conventions. Output JSON only.")


def _extract_json(text: str):
    text = re.sub(r"```(?:json)?", "", text)
    i = text.find("{")
    if i < 0:
        raise ValueError("no JSON object")
    depth = 0
    for j in range(i, len(text)):
        depth += text[j] == "{"
        depth -= text[j] == "}"
        if depth == 0:
            return json.loads(text[i:j + 1])
    raise ValueError("unterminated JSON")


def build_context_block(files: list[Path]) -> str:
    return "\n\n".join(digest(f) for f in files)


def fmt_val(v) -> str:
    return f"{round_half_up(v, 2):,.2f}" if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v)


def answer_with_tools(llm: LLM, question: str, context: str, files: list[Path], think_plan: bool = False, retries: int = 2) -> dict:
    led = Ledger()
    blk = build_context_block(files)
    user = f"{TOOL_HELP}\n\nFILES\n{blk}\n\nCONTEXT: {context}\n\nQUESTION: {question}\n\nJSON plan:"
    msgs = [{"role": "system", "content": PLAN_SYS}, {"role": "user", "content": user}]
    plan, run, err, runner, out = None, None, None, None, {"text": ""}
    for attempt in range(retries + 1):
        try:
            out = llm.chat(msgs, led, think=think_plan, max_tokens=2500 if think_plan else 900, json_mode=True, tag=f"plan{attempt}")
            plan = _extract_json(out["text"])
            runner = Runner(files, context)
            run = runner.run(plan)
            err = None
            break
        except BudgetExceeded:
            err = "budget"
            break
        except Exception as e:  # invalid JSON / tool error -> feed back and retry
            err = f"{type(e).__name__}: {e}"
            msgs += [{"role": "assistant", "content": out["text"]},{"role": "user", "content": f"That failed: {err}. Fix the plan and reply with JSON only."}]
    if err or run is None:
        return {"answer": "", "evidence": [], "plan": plan, "error": err, "ledger": led.summary()}
    results = {k: fmt_val(v) for k, v in run.items()}
    comp = (f"Write the final answer in 1-3 plain sentences. Use ONLY these computed results, quoted exactly as given (already rounded). "
            f"Restate what each value is, with units.\nQUESTION: {question}\nCONTEXT: {context}\nRESULTS: {json.dumps(results, ensure_ascii=False)}\nMain result: {results[plan.get('final') or plan['steps'][-1]['id']]}")
    try:
        out = llm.chat([{"role": "user", "content": comp}], led, think=False, max_tokens=300, tag="compose")
        ans = out["text"].strip()
    except BudgetExceeded:
        ans = ""
    return {"answer": ans, "evidence": runner.rec.items, "plan": plan, "results": results, "error": None, "ledger": led.summary()}


def answer_baseline(llm: LLM, question: str, context: str, files: list[Path]) -> dict:
    """No tools: model answers from the digests alone (reference point for how much the tools add)."""
    led = Ledger()
    prompt = f"FILES\n{build_context_block(files)}\n\nCONTEXT: {context}\n\nQUESTION: {question}\n\nAnswer concisely with exact numbers and name the file and cells/columns you used."
    try:
        out = llm.chat([{"role": "user", "content": prompt}], led, think=False, max_tokens=500, tag="direct")
        txt = out["text"].strip()
    except BudgetExceeded:
        txt = ""
    return {"answer": txt, "evidence": [txt], "plan": None, "error": None, "ledger": led.summary()}
