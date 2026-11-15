# Municipal QA (Zindi GenAI4Good, Cities)

Plan: `MUNICIPAL_QA_PLAN (1).md`. Model: see `MODEL.md` (`qwen3.5:2b`, 1,881,825,088 parameters, via Ollama).

## How an answer is produced (`qa/agent.py`)
1. **Sources are normalised by code**: workbook sheets become labelled tables (items x periods -> cell, with title and unit,
   `qa/tables.py`); CSV/geo files expose their columns; PDF/HTML/Markdown/JSON/XML become located passages (`qa/textqa.py`, BM25).
2. **The model chooses from enumerated options only** (Ollama JSON-schema output): the operation and source(s), then the
   items/periods/columns. Identifiers mentioned in the question are found in the data by code and offered as options.
   Invalid choices are fed back to the model and retried.
3. **Code computes** (stored vs displayed values per Context, changes, shares, ratios with unit scaling, day counts, areas in
   the stated CRS) and records evidence locators.
4. **The model writes the answer** from the computed facts (or cited passages); code checks every key number survived,
   with one repair attempt and a deterministic fallback.
Every model call goes through `qa/llm.py`: at most 15 calls and 12,000 generated tokens per question (enforced and audited).

## Run (Kaggle T4, the reference environment)
Data: private dataset with the challenge files (CSVs + sources archive). Kernel: `kaggle/kernel.py` (clones this repo) runs
`kaggle/run.py`, which installs Ollama, pulls the model, records `MODEL.json`, and runs the dev evaluation.
```
kaggle kernels push -p kaggle          # after git push; results: kaggle kernels output <kernel> --file-pattern '^qwen_dev.*json$'
```
Commands it runs (also usable on any Linux GPU box with Ollama):
```
python -m qa.run_dev --sources SRC --data DATA --model qwen3.5:2b [--think] --out qwen_dev.json   # Train+dev items (approved sources only)
python -m qa.submit  --sources SRC --data DATA --model qwen3.5:2b --out submission.md            # all 600 Test IDs; resumable
python -m qa.audit   --data DATA                                                                  # no Test IDs/questions in code
```
`qa.submit` writes `submission.md` in the `SampleSubmission.md` format, validates it, appends every answer to
`submission.md.jsonl` (resume after interruption) and writes `submission.md.audit.json` (budget audit, format errors).

## Data use
Training/development: only `Source_Set == Train` datasets in `AllowedDatasets.csv` (dft-bus-2026, hsy-energy-2024,
liverpool-tpo-2026). Test sources are inference-only: nothing is trained or tuned on them. `dev/dev_set.json` holds 51 items
built from Train sources (two-way verified); 19 are a sealed hold-out run only with `--split holdout`.

## Earlier steps
| Step | Module | Notes |
|---|---|---|
| 1 inventory | `qa/inventory.py` | 188/188 hashes ok |
| 2 schema cards | `qa/cards.py` | 158 cards |
| 3 dev set | `qa/devset.py` | 51 items, 19 hold-out |
| 4 family tools | `qa/tools.py`, `qa/gtfs.py`, `qa/layers.py`, `qa/maps.py`, `qa/ocr.py` | built; GTFS/layer tools not yet wired into the agent |

`python build.py` re-runs steps 1-3 from `data/` (extract sources, inventory, cards, dev set).
