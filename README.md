# Municipal QA (Zindi GenAI4Good, Cities)

Plan: `MUNICIPAL_QA_PLAN (1).md`. Code in `qa/`, tests in `tests/`, dev set in `dev/`.

| Step | Module | Status |
|---|---|---|
| 1 inventory | `qa/inventory.py` | done: 188/188 hashes ok, 184 parsed, 4 partial (OCR/ifcopenshell, needs Colab install) |
| 2 schema cards | `qa/cards.py` | done: 158 cards |
| 3 dev set | `qa/devset.py`, `dev/dev_set.json` | 51 items, two-way verified, 19 hold-out (sealed) |
| 4 tools | `qa/tools.py` | core primitives + Train-reference tests; family tools still to do |
| 5+ planner, agent loop, retrieval | - | need GPU (Colab) |

Run: `Steps1_4_Pipeline.ipynb` on Colab (installs in cell 0), or locally
`python -m pytest tests` with `QA_SOURCES=<extracted sources/>`.
