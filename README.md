# Municipal QA (Zindi GenAI4Good, Cities)

Plan: `MUNICIPAL_QA_PLAN (1).md`. Code in `qa/`, tests in `tests/`, dev set in `dev/`.

| Step | Module | Status |
|---|---|---|
| 1 inventory | `qa/inventory.py` | done: 188/188 hashes ok, 184 parsed, 4 partial (OCR/ifcopenshell, needs Colab install) |
| 2 schema cards | `qa/cards.py` | done: 158 cards |
| 3 dev set | `qa/devset.py`, `dev/dev_set.json` | 51 items, two-way verified, 19 hold-out (sealed) |
| 4 tools | `qa/tools.py`, `qa/maps.py`, `qa/gtfs.py` | core primitives + Train tests; GTFS family done (10 tests); other top families still to do |
| 5+ planner, agent loop, retrieval | - | need GPU (Colab) |

Run: `Steps1_4_Pipeline.ipynb` on Colab (installs in cell 0), or locally
`python -m pytest tests` with `QA_SOURCES=<extracted sources/>`.

## Standalone build

```
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt   # Linux/Colab: .venv/bin/pip
python build.py            # extract sources -> steps 1-3 -> pytest (outputs in runs/, dev/)
```
Self-contained: paths resolve relative to `build.py`; needs only `data/`. Verified locally: 188/188 files hash ok, 158 cards, 51 dev items, 9 tests pass. 4 sources stay `partial` until tesseract/ifcopenshell are installed (Colab).
