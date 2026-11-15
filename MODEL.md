# Model card for this pipeline

| Item | Value | How it was verified |
|---|---|---|
| Generative model | `qwen3.5:2b` (Ollama library tag) | `ollama pull` on the Kaggle T4 kernel |
| Total parameters | **1,881,825,088** (Ollama `parameter_size` 1.9B) | `/api/show` → `model_info["general.parameter_count"]` |
| Format / quantization | GGUF, `Q8_0` | `/api/show` → `details` |
| Family / runner | `qwen35` / llama.cpp (via Ollama) | `/api/show` |
| Capabilities | completion, vision, tools, thinking | `/api/show` → `capabilities` |
| Digest, Ollama version | recorded per run in `MODEL.json` (kernel output) | `/api/tags`, `/api/version` |

Rules check: one generative model; 1.88B total parameters, below the 3B class (staff accept ~3.1-3.2B).
Quantization does not change the parameter count. No other generative model is in the answer path
(retrieval is BM25; OCR is Tesseract; everything else is deterministic code).

Budget per question: at most 15 model calls and 12,000 generated tokens including thinking and retries, enforced
in `qa/llm.py` (raises `BudgetExceeded`) and audited after every run (`budget_violations` in the run summary).

Sampling: fixed seed 1234. Schema-constrained choices use temperature 0 (`extract` profile); answer writing uses
temperature 0.2; when thinking is enabled for planning, the `precise` profile (0.6 / 0.95 / 20) is used.

Alternative evaluated: `qwen2.5:3b-instruct-q8_0` (3,085,938,688 parameters). On the same pipeline and dev items it
scored 0.296 versus 0.287 for qwen3.5:2b; qwen3.5:2b was chosen by the user and is the default.
