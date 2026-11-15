"""Run the generative model end to end over Train + dev items (APPROVED training/dev sources only) and score.

    python -m qa.run_dev --sources SRC_DIR --data DATA_DIR [--split dev|holdout|all] [--limit N] [--model M]

Approved for training/development per AllowedDatasets.csv: Source_Set == Train (dft-bus-2026, hsy-energy-2024, liverpool-tpo-2026).
Each item receives its WHOLE approved dataset as the pack (as Test questions receive several files), not just the right file.
Test sources are never read here. The sealed hold-out runs only with --split holdout.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import pandas as pd

from .agent import answer
from .llm import LLM
from .score import score_items
from .tables import clean_name


def items(data: Path, sources: Path, split: str) -> list[dict]:
    man = pd.read_csv(data / "SourceManifest.csv")
    allowed = pd.read_csv(data / "AllowedDatasets.csv")
    train_sets = [json.loads(r.Source_IDs) for r in allowed[allowed.Source_Set == "Train"].itertuples()]
    ok_ids = {s for ids in train_sets for s in ids}
    path_of = {p.name.split("__")[0]: p for p in sources.glob("SRC_*")}
    name_to_id = {r.Original_File: r.Source_ID for r in man.itertuples() if r.Source_ID in ok_ids}
    out = []
    if split in ("dev", "all"):
        tr = pd.read_csv(data / "Train.csv")
        for r in tr.itertuples():
            ids = [i for i in json.loads(r.Source_IDs) if i in ok_ids]
            out.append(dict(id=r.ID, domain=r.Domain, question=r.Question, context=r.Context, reference=r.Answer,
                            evidence_ref=json.loads(r.Evidence), files=[path_of[i] for i in ids if i in path_of]))
    dev = json.loads((Path(__file__).resolve().parent.parent / "dev" / "dev_set.json").read_text(encoding="utf-8"))
    for d in dev:
        if split != "all" and d["split"] != ("holdout" if split == "holdout" else "dev"):
            continue
        sid = name_to_id.get(d["evidence"][0].split(",")[0].strip())
        pack = next((ids for ids in train_sets if sid in ids), None)
        if pack:
            out.append(dict(id=d["id"], domain=d["domain"], question=d["question"], context=d["context"], reference=d["answer"],
                            evidence_ref=d["evidence"], files=[path_of[i] for i in pack if i in path_of]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--split", choices=["dev", "holdout", "all"], default="dev")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--model", default="qwen2.5:3b-instruct")
    ap.add_argument("--url", default="http://127.0.0.1:11434", help="Ollama URL, or an OpenAI-compatible vLLM base ending in /v1")
    ap.add_argument("--think", action="store_true", help="let a thinking-capable model think during plan/bind calls")
    ap.add_argument("--workers", type=int, default=1, help="questions answered concurrently (match OLLAMA_NUM_PARALLEL)")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    its = items(Path(a.data), Path(a.sources), a.split)
    if a.limit:
        its = its[: a.limit]
    llm = LLM(a.model, url=a.url)
    llm.think_plan = a.think
    rows = []
    def one(it):
        t0 = time.time()
        r = answer(llm, it["question"], it["context"], it["files"])
        row = dict(id=it["id"], domain=it["domain"], question=it["question"], reference=it["reference"], evidence_ref=it["evidence_ref"],
                   pack=[clean_name(f) for f in it["files"]], answer=r["answer"], evidence=r["evidence"], facts=r["facts"],
                   trace=r["trace"], error=r["error"], ledger=r["ledger"])
        print(f"{it['id'][:12]} {r['ledger']} op={r['trace'].get('plan', {}).get('op')} err={r['error']} ({time.time() - t0:.0f}s)", flush=True)
        return row

    if a.workers > 1:  # independent questions in parallel; each keeps its own ledger (budgets are per question)
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(a.workers) as pool:
            rows = list(pool.map(one, its))
    else:
        rows = [one(it) for it in its]
    s = score_items(rows)
    s["budget_violations"] = [r["id"] for r in rows if r["ledger"]["calls"] > 15 or r["ledger"]["tokens"] > 12000]
    s["errors"] = sum(1 for r in rows if r["error"])
    print(json.dumps(s, indent=1))
    for r in rows:  # full per-item view in the log: question, reference, answer, verdict
        print(f"\n### {r['id'][:12]} [{r['domain']}] score={r.get('score')}\nQ: {r['question']}\nREF: {r['reference']}\nANS: {r['answer']}\nTRACE: {json.dumps(r['trace'], ensure_ascii=False)[:700]}\nERR: {r['error']}")
    if a.out:
        Path(a.out).write_text(json.dumps({"summary": s, "rows": rows}, indent=1, ensure_ascii=False, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
