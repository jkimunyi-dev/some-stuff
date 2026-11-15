"""Step 8 - answer every Test question and write the Zindi submission file (format of SampleSubmission.md).

    python -m qa.submit --sources SRC_DIR --data DATA_DIR --out submission.md [--model M] [--limit N]

* Test sources are used for INFERENCE ONLY (AllowedDatasets.csv); nothing here trains or tunes on them.
* Checkpoint/resume: every answered question is appended to <out>.jsonl, so an interrupted run continues where it stopped.
* The per-question ledger (calls, generated tokens) is kept in the jsonl and audited: no question may exceed 15 calls / 12,000 tokens.
* validate() enforces the format: '# Submission', every Test ID exactly once in Test.csv order, answer non-empty and free of
  '## ' lines, a '### Evidence' block with single-line '- SOURCE_ID | locator | claim' bullets, UTF-8.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

import pandas as pd

from .agent import answer
from .llm import LLM, MAX_CALLS, MAX_TOKENS

FALLBACK = "The supplied sources could not be processed reliably for this question, so no figure is given."


def files_for(source_ids: list[str], sources: Path) -> list[Path]:
    idx = {p.name.split("__")[0]: p for p in sources.glob("SRC_*")}
    return [idx[i] for i in source_ids if i in idx]


def clean_answer(a: str) -> str:
    a = (a or "").strip()
    a = re.sub(r"(?m)^#+\s*", "", a)  # no markdown headings inside an answer
    return a or FALLBACK


def clean_ev(e: str) -> str:
    return " ".join(str(e).split())


def render(rows: list[dict], order: list[str]) -> str:
    by = {r["id"]: r for r in rows}
    out = ["# Submission", ""]
    for qid in order:
        r = by.get(qid, {})
        ev = [clean_ev(e) for e in r.get("evidence") or []] or ["NONE | no locator | no computed evidence for this answer"]
        out += [f"## {qid}", "", clean_answer(r.get("answer", "")), "", "### Evidence", *[f"- {e}" for e in ev[:8]], ""]
    return "\n".join(out)


def validate(md: str, order: list[str]) -> list[str]:
    errs = []
    if not md.startswith("# Submission"):
        errs.append("must start with '# Submission'")
    ids = re.findall(r"(?m)^## (.+)$", md)
    if ids != order:
        errs.append(f"IDs not exactly Test.csv order (got {len(ids)}, expected {len(order)}; missing {len(set(order) - set(ids))}, extra {len(set(ids) - set(order))})")
    blocks = re.split(r"(?m)^## ", md)[1:]
    for b in blocks:
        qid, _, body = b.partition("\n")
        if "### Evidence" not in body:
            errs.append(f"{qid}: no Evidence block")
            continue
        ans, _, ev = body.partition("### Evidence")
        if not ans.strip():
            errs.append(f"{qid}: empty answer")
        bullets = [l for l in ev.strip().splitlines() if l.strip()]
        if not bullets or any(not l.startswith("- ") for l in bullets):
            errs.append(f"{qid}: evidence must be '- ' bullet lines")
    try:
        md.encode("utf-8")
    except UnicodeError:
        errs.append("not UTF-8 encodable")
    return errs


def audit(rows: list[dict]) -> dict:
    over = [r["id"] for r in rows if r["ledger"]["calls"] > MAX_CALLS or r["ledger"]["tokens"] > MAX_TOKENS]
    return {"answered": len(rows), "errors": sum(1 for r in rows if r.get("error")),
            "max_calls": max((r["ledger"]["calls"] for r in rows), default=0),
            "max_tokens": max((r["ledger"]["tokens"] for r in rows), default=0), "budget_violations": over}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default="qwen3.5:2b")
    ap.add_argument("--url", default="http://127.0.0.1:11434", help="Ollama URL, or an OpenAI-compatible vLLM base ending in /v1")
    ap.add_argument("--think", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=1)
    a = ap.parse_args()
    test = pd.read_csv(Path(a.data) / "Test.csv")
    order = list(test.ID)
    ck = Path(a.out + ".jsonl")
    done = {}
    if ck.exists():
        for ln in ck.read_text(encoding="utf-8").splitlines():
            r = json.loads(ln)
            done[r["id"]] = r
    llm = LLM(a.model, url=a.url)
    llm.think_plan = a.think
    todo = [r for r in test.itertuples() if r.ID not in done]
    if a.limit:
        todo = todo[: a.limit]
    import threading
    from concurrent.futures import ThreadPoolExecutor
    lock = threading.Lock()

    def one(r):
        t0 = time.time()
        try:
            res = answer(llm, r.Question, r.Context, files_for(json.loads(r.Source_IDs), Path(a.sources)))
        except Exception as e:  # never lose the run to one question
            res = {"answer": "", "evidence": [], "error": f"crash: {type(e).__name__}: {e}", "ledger": {"calls": 0, "tokens": 0, "seconds": 0}, "trace": {}}
        row = {"id": r.ID, "domain": r.Domain, "question": r.Question, "answer": res["answer"], "evidence": res["evidence"], "error": res["error"],
               "ledger": res["ledger"], "op": res["trace"].get("plan", {}).get("op"), "facts": res.get("facts", []),
               "trace": {k: v for k, v in res["trace"].items() if k in ("plan", "bind", "passages", "named_files")}}
        with lock:
            done[r.ID] = row
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            fh.flush()
            print(f"[{len(done)}/{len(order)}] {r.ID[:8]} {r.Domain} op={row['op']} {res['ledger']} err={res['error']} ({time.time() - t0:.0f}s)", flush=True)
            if len(done) % 25 == 0:
                prog.update(list(done.values()), len(order))

    from .agent import clear_caches
    from .progress import Progress, token_from_env_or_kaggle
    prog = Progress(token_from_env_or_kaggle(), label=f"submit {a.model}")
    prog.update(list(done.values()), len(order), note="started")
    packs: dict = {}
    for r in todo:  # pack by pack: sources load once per pack and memory is freed before the next pack
        packs.setdefault(r.Source_IDs, []).append(r)
    with ck.open("a", encoding="utf-8") as fh:
        for i, (k, rs) in enumerate(packs.items(), 1):
            with ThreadPoolExecutor(max(1, a.workers)) as pool:
                list(pool.map(one, rs))
            clear_caches()
            prog.update(list(done.values()), len(order), note=f"pack {i}/{len(packs)} done")
            print(f"--- pack {i}/{len(packs)} done ({len(rs)} questions); {len(done)}/{len(order)} answered so far", flush=True)
    rows = list(done.values())
    md = render(rows, order)
    errs = validate(md, order)
    Path(a.out).write_text(md, encoding="utf-8")
    rep = audit(rows)
    prog.update(rows, len(order), note="finished")
    rep["unanswered_or_fallback"] = sum(1 for q in order if not (done.get(q) or {}).get("answer"))
    rep["format_errors"] = errs[:20]
    print(json.dumps(rep, indent=1))
    Path(a.out + ".audit.json").write_text(json.dumps(rep, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
