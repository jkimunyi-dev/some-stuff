"""Run the single generative model over Train + dev items (APPROVED training/dev sources only) and score with Layer 1.

    python -m qa.run_dev --sources SRC_DIR --data DATA_DIR --mode tools|baseline [--split dev|holdout|all] [--limit N]

Approved for training/development per AllowedDatasets.csv: Source_Set == Train (dft-bus-2026, hsy-energy-2024, liverpool-tpo-2026).
Test sources are inference-only and are never read here. The sealed hold-out is only run with --split holdout (final check).
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import pandas as pd

from .agent import answer_baseline, answer_with_tools, clean_name
from .llm import LLM
from .score import score_items


def items(data: Path, sources: Path, split: str) -> list[dict]:
    man = pd.read_csv(data / "SourceManifest.csv")
    allowed = pd.read_csv(data / "AllowedDatasets.csv")
    ok_ids = {s for r in allowed[allowed.Source_Set == "Train"].itertuples() for s in json.loads(r.Source_IDs)}
    by_name = {clean_name(p): p for p in sources.glob("SRC_*")}
    out = []
    if split in ("dev", "all"):
        tr = pd.read_csv(data / "Train.csv")
        for r in tr.itertuples():
            ids = [i for i in json.loads(r.Source_IDs) if i in ok_ids]
            files = [p for p in sources.glob("SRC_*") if p.name.split("__")[0][4:] in ids]
            out.append(dict(id=r.ID, domain=r.Domain, question=r.Question, context=r.Context, reference=r.Answer,
                            evidence_ref=json.loads(r.Evidence), files=files))
    dev = json.loads((Path(__file__).resolve().parent.parent / "dev" / "dev_set.json").read_text(encoding="utf-8"))
    for d in dev:
        if split != "all" and d["split"] != ("holdout" if split == "holdout" else "dev"):
            continue
        name = d["evidence"][0].split(",")[0].strip()
        if name in by_name:
            out.append(dict(id=d["id"], domain=d["domain"], question=d["question"], context=d["context"], reference=d["answer"],
                            evidence_ref=d["evidence"], files=[by_name[name]]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--mode", choices=["tools", "baseline"], default="tools")
    ap.add_argument("--split", choices=["dev", "holdout", "all"], default="dev")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--model", default="qwen3.5:2b")
    ap.add_argument("--think-plan", action="store_true")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    its = items(Path(a.data), Path(a.sources), a.split)
    if a.limit:
        its = its[: a.limit]
    llm = LLM(a.model)
    rows = []
    for i, it in enumerate(its):
        t0 = time.time()
        if a.mode == "tools":
            r = answer_with_tools(llm, it["question"], it["context"], it["files"], think_plan=a.think_plan)
        else:
            r = answer_baseline(llm, it["question"], it["context"], it["files"])
        row = dict(id=it["id"], domain=it["domain"], question=it["question"], reference=it["reference"], evidence_ref=it["evidence_ref"],
                   answer=r["answer"], evidence=r["evidence"], plan=r.get("plan"), error=r.get("error"), ledger=r["ledger"])
        rows.append(row)
        print(f"[{i + 1}/{len(its)}] {it['id']} {r['ledger']} err={r.get('error')} ({time.time() - t0:.0f}s)", flush=True)
    s = score_items(rows)
    over = [r["id"] for r in rows if r["ledger"]["calls"] > 15 or r["ledger"]["tokens"] > 12000]
    s["budget_violations"] = over
    s["errors"] = sum(1 for r in rows if r["error"])
    print(json.dumps(s, indent=1))
    if a.out:
        Path(a.out).write_text(json.dumps({"summary": s, "rows": rows}, indent=1, ensure_ascii=False, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
