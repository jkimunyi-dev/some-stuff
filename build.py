"""One-command, self-contained build: extract sources, run steps 1-4, run tests.

    python build.py            # everything
    python build.py --skip-tests

Everything is resolved relative to this file, so the repo folder can live anywhere
(local, Colab, Drive). Inputs: data/ (CSVs + the sources zip). Outputs: runs/.
"""
import argparse
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# Overridable for Colab/Drive: QA_DATA (dir with CSVs + sources zip), QA_EXTRACT, QA_RUNS
DATA = Path(os.environ.get("QA_DATA", ROOT / "data"))
EXTRACT = Path(os.environ.get("QA_EXTRACT", ROOT / "extracted"))
SRC = EXTRACT / "sources"
RUNS = Path(os.environ.get("QA_RUNS", ROOT / "runs"))
sys.path.insert(0, str(ROOT))


def extract_sources() -> None:
    if SRC.is_dir() and any(SRC.iterdir()):
        print(f"[extract] reuse {SRC} ({len(list(SRC.iterdir()))} files)")
        return
    cands = [p for p in DATA.iterdir() if p.stat().st_size > 50_000_000 and zipfile.is_zipfile(p)]
    if not cands:
        sys.exit("no sources zip found in data/")
    print(f"[extract] {cands[0].name} -> {EXTRACT}")
    zipfile.ZipFile(cands[0]).extractall(EXTRACT)
    if not SRC.is_dir():
        sys.exit(f"expected {SRC} after extraction")
    print(f"[extract] {len(list(SRC.iterdir()))} source files")


def step1() -> None:
    from qa.inventory import build_inventory, summarize
    inv = build_inventory(DATA, SRC)
    inv.to_csv(RUNS / "inventory.csv", index=False)
    print("[step1]", summarize(inv))


def step2() -> None:
    from qa.cards import build_cards
    packs = build_cards(DATA, SRC)
    (RUNS / "cards.json").write_text(json.dumps(packs, indent=1, ensure_ascii=False), encoding="utf-8")
    failed = [k for k, v in packs.items() if any("card failed" in f for f in v["files"])]
    print(f"[step2] {len(packs)} cards; failed: {failed}")


def step_ocr() -> None:
    """Full OCR text of every scanned/image source -> runs/ocr.json (skipped if tesseract missing)."""
    import pandas as pd
    from qa.ocr import available, ocr_file

    if not available():
        print("[ocr] tesseract not available; skipped")
        return
    inv = pd.read_csv(RUNS / "inventory.csv", keep_default_na=False)
    todo = inv[inv.notes.str.contains("needs OCR")]
    out = {}
    for r in todo.itertuples():
        path = next(SRC.glob(f"{r.source_id}__*"))
        out[r.source_id] = {"file": r.original_file, "text": ocr_file(path, max_pages=20)}
    (RUNS / "ocr.json").write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    print("[ocr]", {k: len(v["text"]) for k, v in out.items()})
    # large scanned sheets (maps): tiled OCR with word boxes, cached per source
    from qa.maps import cached_words

    for r in todo[todo.ext.isin([".jpg", ".png"]) & (todo.bytes > 1_000_000)].itertuples():
        path = next(SRC.glob(f"{r.source_id}__*"))
        w = cached_words(path, RUNS / f"map_words_{r.source_id}.json")
        print(f"[maps] {r.original_file}: {len(w)} words")


def step3() -> None:
    from qa.devset import build
    items = build(SRC)
    text = json.dumps(items, indent=1, ensure_ascii=False)
    (RUNS / "dev_set.json").write_text(text, encoding="utf-8")
    (ROOT / "dev").mkdir(exist_ok=True)
    (ROOT / "dev" / "dev_set.json").write_text(text, encoding="utf-8")
    print(f"[step3] {len(items)} dev items")


def tests() -> int:
    env = dict(os.environ, QA_SOURCES=str(SRC))
    return subprocess.call([sys.executable, "-m", "pytest", "tests", "-q"], cwd=ROOT, env=env)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-tests", action="store_true")
    a = ap.parse_args()
    RUNS.mkdir(exist_ok=True)
    extract_sources()
    step1()
    step_ocr()
    step2()
    step3()
    if not a.skip_tests:
        sys.exit(tests())


if __name__ == "__main__":
    main()
