"""Step 12 compliance audit: the code must not contain Test IDs, Test question text or Test source IDs (no lookups).

    python -m qa.audit --data DATA_DIR [--root REPO_DIR]
Exit code 1 if anything is found. Data folders and run outputs are excluded (they legitimately hold Test material).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

SKIP_DIRS = {"data", "extracted", "runs", ".venv", ".git", "__pycache__", "sources", "dev"}
EXTS = {".py", ".ipynb"}  # code only: submission outputs and notes legitimately list Test IDs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    a = ap.parse_args()
    t = pd.read_csv(Path(a.data) / "Test.csv")
    man = pd.read_csv(Path(a.data) / "SourceManifest.csv")
    needles = set(t.ID) | {s for x in t.Source_IDs for s in json.loads(x)}
    needles |= {q[:60] for q in t.Question if len(q) >= 60}  # distinctive question openings
    test_src = set(man[man.Source_Set.str.lower() == "test"].Source_ID)
    hits = []
    for p in Path(a.root).rglob("*"):
        if not p.is_file() or p.suffix.lower() not in EXTS or any(part in SKIP_DIRS for part in p.parts):
            continue
        txt = p.read_text(encoding="utf-8", errors="ignore")
        for n in needles:
            if n in txt:
                hits.append((str(p.relative_to(a.root)), n[:70], "test-source" if n in test_src else "test-id/question"))
    for h in hits:
        print("FOUND", *h)
    print(f"audit: {len(hits)} hit(s) in code/docs")
    sys.exit(1 if hits else 0)


if __name__ == "__main__":
    main()
