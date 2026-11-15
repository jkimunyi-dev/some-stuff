# Municipal QA (GenAI for Good, Cities Edition) — Findings, Rules and Implementation Plan

Prepared 14 Nov 2026. Challenge closes **13 Dec 2026**. Tags used below:
**[rules]** = challenge rules text you pasted, **[staff]** = Zindi staff replies you pasted, **[run]** = your notebook output,
**[card]** = Qwen3.5-2B model card, **[web]** = pages I checked, **[unverified]** = needs confirming before relying on it.

---

## 1. Readiness verdict

**Yes, we have enough to start implementing Steps 0–4 now.** The rules and staff replies settle the main design questions:
per-source readers, calculators, retrieval tools, non-generative OCR and embedder/reranker models are allowed, and prompts may be tuned on
Test question wording. Hard-coded answers, Test ID lookups and any learned adaptation on Test material are prohibited.

Steps 5 onward are conditional on a few open items (Section 8). None of them blocks Steps 0–4.

Biggest finding: the problem is **not the model**. Your run shows all 9 Train answers wrong and the code agent falling back to the
text baseline on 9 of 9 questions. Deterministic tools, schema cards, automatic evidence and a working execution loop are where the score is.

---

## 2. What we have

### 2.1 Data and structure [run]
- Train: 9 questions with references. Test: 600 questions (buildings_energy 261, urban_greenery 191, transport 148) in **64 packs**.
- Sources: 188 files in the zip; 22 Train sources, 161 Test sources, **0 overlap**. Hash/size check: 0 files wrong or missing.
- Each Test pack shares files and a Context string that encodes the calculation conventions (rounding, displayed vs stored values, CRS, service day, etc.).
  There are 58 distinct Context strings.
- Many questions are traps whose correct answer is "the source does not support that" (e.g. `[z]` is not zero; empty `min_transfer_time` is not 0 s).
- File-format references across Test questions (counts are per question reference, not distinct files): .txt 732, .json 546, .md 306, .geojson 304,
  .csv 263, .pdf 182, .xlsx 168, .tif 160, .xml 117, .html 116, .xsd/.gml/.gpkg 64 each, .zip 44, .laz 38, .jpg 30, .ifc 24, .ods 24, .sqlite 24.

### 2.2 Largest Test packs (from your notebook output)
| Qs | Domain | Pack (main files) | Tools needed |
|---|---|---|---|
| 36 | transport | Accra / Burgos / Kenya survey reports (.html, .pdf) | PDF/HTML text + table extraction, OCR if scanned, retrieval |
| 32 | urban_greenery | Zürich trees (register/inventory/crown-drawing/position-symbol/tree-parts geojson, .xml) | geopandas, counts/joins/nulls |
| 32 | urban_greenery | Berlin trees (anlagenbaeume/strassenbaeume .gml/.geojson/.gpkg/.xsd) | multi-layer reads, genus/street aggregation |
| 32 | buildings_energy | Lille RNB cohort (Records/BuildingDetails/Buildings/Collection json, EPSG:2154) | JSON flatten/join, planar geometry |
| 32 | transport | GTFS metropolitan snapshot (routes, stop_times, pathways, transfers, calendar_dates, shapes...) | GTFS loader, pathway graph routing, calendar logic, WGS84 ellipsoid distances |
| 28 | urban_greenery | eThekwini / Cape Town WorldCover-style rasters (.tif, ProductNotes, ReadingGuide) | rasterio, class areas, vector coverage |
| 24 | buildings_energy | eThekwini electricity workbooks + data dictionary PDF + catalogue JSON | openpyxl, period labels, percentage change |
| 24 | buildings_energy | ENB2012 workbook + CSV + metadata | pandas joins, tolerance comparisons |
| 24 | transport | Bike-sharing day.csv / hour.csv | pandas, date logic, leap-day checks |
| 24 | buildings_energy | IFC model + Estonian requirements workbook | ifcopenshell, footprint/bounding-box area |
| 24 | buildings_energy | EightClients electricity CSV | time-label logic, tariffs, simultaneous peaks |
| 24 | buildings_energy | Urban building stock CSV + ReadMe xlsx | key uniqueness, units from workbook |

The remaining ~52 packs are smaller. Montréal public trees, Dublin trees, Cape Town/Durban sets and others appear in the contexts.

### 2.3 Scoring [rules]
- Per element: correctness c in {0, 0.5, 1} and evidence e in {0, 0.5, 1}. Element score = `0.8c + 0.2·c·e`.
- Question = mean of elements. Aggregation: within case, across cases within domain, then **equal weight across the 3 domains**.
- Evidence earns credit only if the element is correct. A right number with the wrong unit or scope is incorrect.
- The public leaderboard scores a hidden subset. Final ranking uses the better full-Test score of your two selected submissions.
- Leaderboard snapshot you pasted: #1 about 0.969, top 5 above 0.91, #10 about 0.748. Reaching the top tier needs mostly deterministic correctness.

### 2.4 Our results so far [run]
- Baseline (text-only RAG, Qwen3-1.7B, thinking off): 600 answers in about 19 minutes; **none of the 9 Train answers matches its reference**.
  Examples: invented "12.3 GWh" figures; three answers just describe a GeoJSON file.
- Code agent on Train: `calls 1`, `code: (fallback)` on all 9, so every attempt failed or printed nothing.
- Evidence strings ("excerpt used for the answer") are generic and earn almost nothing.
- Starter Train regression targets we can already use as unit tests (from Train references): zone 1912/A1 = **3.48 ha** (EPSG:27700); London **82.85%** and
  England outside London **87.60%** of YE-March-2019 journeys (bus01 cells H23, T23, H29, T29, displayed values); Helsinki-region total rise **1,964.06 GWh / 8.07%**, residential **+31.02 GWh**;
  residential share Helsinki **45.45%**, Espoo **51.09%**, gap **5.64 pp**; bus km per journey about **6.07** (2024 NTS stage length); TPO453 made 20 Apr 2018, confirmed 25 May 2018, **35 days**;
  largest rise Helsinki **1,424.36 GWh / 10.75%**.

### 2.5 Files produced in this session
- `judge_eval.py`: local evaluator. Train mode is reference-based (organiser formula). Test mode is reference-free with programmatic flags. **It sends text to an external endpoint, so default to Train only until Section 8 item 4 is cleared.**
- `qa_runtime.py`: budget-enforcing Ollama wrapper (15 calls / 12,000 tokens including thinking), Qwen3.5-2B sampling profiles, thinking-loop fallback, per-question ledger. Mock-tested only.
- This plan.

### 2.6 The provided dataset: what each file is and does
All files come from the challenge Data page. Sizes are as listed there.

| File | What it is | How we use it |
|---|---|---|
| `Train.csv` (9.7 KB) | 9 development questions with checked **reference answers and evidence**. Columns: `ID`, `Domain`, `Source_IDs` (JSON list of source IDs), `Question`, `Context`, `Answer`, `Evidence` (JSON list). | Regression tests, calibrating the judge, few-shot examples, learning the evidence style. Too small for tuning on its own. |
| `Test.csv` (385.3 KB) | The **600 questions to answer**. Same columns as Train **minus** `Answer` and `Evidence`. Which questions are scored on the public leaderboard is hidden. | Every ID must be answered, including questions reserved for final evaluation. Test is inference-only (see Section 4). |
| `SourceMaterials.zip` (437.5 MB) | The 188 source files behind Train and Test: reports, spreadsheets, registers, maps, rasters, point clouds, IFC models, GTFS feeds, SQLite files, scans. Extracted into a `sources/` folder, each file named `SRC_<id>__<original name>`. | The only evidence base. **Note:** the Data page lists the archive under the odd filename `SRC_535ab8cab643__3dbag_page_a.json`. Your notebook renames it to `SourceMaterials.zip` before extracting, and the extraction worked (188 files, 0 size mismatches). |
| `SourceManifest.csv` (121.8 KB) | One row per source file. Columns: `Source_ID`, `File` (path inside the zip), `Source_Set` (train/test), `Original_File`, `Publisher`, `Licence`, `Licence_URL`, `Source_URL`, `Attribution`, `Derivation`, `SHA256`, `Bytes`, `Rights_Notes`, `Archive`. | Maps IDs to files, checks hashes and sizes, and supplies licence/attribution information for the code-review declaration. |
| `SampleSubmission.md` (70.3 KB) | The required Markdown format, with every Test ID but no reference answers. | Template for the output file. (I have not seen its contents; the format in Section 10 comes from the notebook's `save_submission`. Check it against the template.) |
| `AllowedDatasets.csv` (31.7 KB) | Approved datasets and whether each is training/development or **inference-only** (Test sources are inference-only). Updated on Mondays as additions are approved. | Not yet reviewed. Read it before using any external data. |
| `StarterNotebook.ipynb` (42.4 KB) | Organiser walkthrough: loading data, source exploration, a Qwen3-1.7B answer through Ollama, and a submission writer. The organisers state it is not a complete reader for every format. | Our starting point. We extend it with tools, evidence logging and budget control. |

Other things in your folder: three use-case PDFs (Rivne energy, Skopje green infrastructure, Pristina transit) that give background on the broader challenge but are not question sources.

**How the sources are organised**
- A **case pack** is the set of sources referenced by a group of questions (`Source_IDs`). Test has 64 packs, and questions in a pack share files and Context.
- Each pack typically contains **data files** (CSV, workbooks, GeoJSON/GML/GPKG, rasters, IFC, GTFS text files) and **guidance files** such as `SourceGuide.txt/.md`, `ReadingGuide.md`, `ProductNotes.txt`, `Readme`, `DatasetMetadata.json`, `DERIVATION.json`, `Attribution.txt`, `publisher_csw.xml`, data dictionaries. The guidance files carry the caveats that "cannot conclude" questions depend on.
  The starter notebook's display code **skips** these files (a regex hides SourceGuide, Attribution, DatasetMetadata, DERIVATION, publisher_csw). Do not skip them when answering.
- The `Context` column is part of the question. It fixes the snapshot, units, rounding, CRS and conventions.
- Train and Test sources are disjoint (22 vs 161 sources, 0 overlap), so Train cannot leak into Test.

**How to submit on Zindi**
1. Run the pipeline for all 600 Test IDs and keep the per-question ledger (calls and tokens).
2. Build the file with `save_submission`: one UTF-8 `.md` file, `# Submission` at the top, then for each ID in **Test.csv order**:
   `## <ID>`, a blank line, the answer in natural English, a blank line, `### Evidence`, then bullet lines `- <evidence>`.
   The function rejects wrong or missing IDs, an answer containing a `## ` line, and evidence containing line breaks. Evidence should name the source file and a verifiable location (sheet/cell, column, record or feature ID, page) and, for negative answers, the note or scope that shows the gap. The notebook's template style is `SRC_... | original locator | supported claim`; staff accept plain-language citations.
3. Upload the `.md` on the competition page (**Submit**). Limits: **1 submission per day, 20 in total**. A submission that fails processing does not count against the limits and receives no score.
4. Scoring: an LLM judge compares each required answer element with checked references. The public leaderboard shows a score on a hidden subset of Test questions, and the final ranking is computed on the full Test set.
5. Before the challenge closes (13 Dec 2026), **select two submissions** for final evaluation. If you do not, your two best public scores are chosen automatically.
6. If you finish in the provisional top 100, you get an email asking for code and have 48 hours to send it (Section 4.4).

---

## 3. What we found

### 3.1 Model
- **Qwen3.5-2B** [card]: thinking is **off by default** and must be requested; recommended thinking sampling is temperature 1.0, top_p 0.95, top_k 20, presence_penalty 1.5; "precise coding" uses 0.6 / 0.95 / 20 / 0.0.
  The 2B variant is **more prone to thinking loops**, so greedy (temperature 0) is a poor choice. Reproducibility must come from fixed seeds.
  About 2.3B total per Artificial Analysis [web]. The card gives only "2B", so confirm via `/api/show` and document the checkpoint.
- **Granite 4.2 3B** [web: IBM docs]: dense, Apache 2.0, native `<think>` reasoning toggled by `enable_thinking`, tool calling, 128K context.
  Exact parameter count not published on that page. No Ollama 4.2 tag listed (only a 4.1 page), so likely llama.cpp or vLLM. Candidate for a bake-off.
- Out by the rules: Qwen3.5-4B, Phi-4-mini (3.8B), Gemma 3 4B, Bonsai-8B, Gemma 3n/4 E2B (about 5B raw), and any quantized bigger model, because quantization does not reduce the parameter count [rules][staff].
- Higher precision (Q8_0 / FP16) is a free improvement on a T4 or a 4090.

### 3.2 Ollama structured output [web: ollama issue 14645]
`format` has been reported ignored for Qwen3.5 when thinking is off, and occasionally even with thinking on; `/api/generate` enforced it in several tests where `/api/chat` did not;
a fix is reported in 0.31.2 but not universally. **Do not depend on it**: validate JSON in code and retry (retries count against the 15 calls).

### 3.3 Evaluator models [web: OpenRouter compare]
| | DeepSeek V4.1 Flash 1017 | GLM 5.3 Flash |
|---|---|---|
| Input / output per 1M tokens | $0.038 / $0.50 | $0.0399 / $0.0784 |
| Intelligence Index | 28.6 | 27.8 |
| Throughput p50 | 160 tok/s | 64 tok/s |
Reasoning, tool-calling, structured output and data-retention policy were **not shown** on the page. [unverified]
DeepSeek's output price is above the $0.20 limit you set. Total cost for 600 judgements is under about $1 either way, so choose on reliability.

### 3.4 Claims from the pasted chat that we are not adopting
Quantized 4B/8B models, merging, frankenmerge, speculative decoding with a draft model, and its specific benchmark figures (unverified). Its sampling numbers differ from the model card; follow the card and test.

---

## 4. Rules

### 4.1 Allowed
| Item | Source |
|---|---|
| One openly available generative model of the **3B class** (staff accept about 3.1–3.2B total); use total, not active, parameters; document exact checkpoint | [rules][staff] |
| Max **15 model calls and 12,000 generated tokens per question**, including thinking, retries, self-checks | [rules] |
| Non-generative tools: retrieval, OCR (Tesseract), parsing, indexing, embeddings, rerankers, spatial analysis, calculations | [rules][staff] |
| Decoder-based embedding/reranker models, **only** to produce vectors or relevance scores | [staff] |
| `apt-get install` of system tools (e.g. tesseract-ocr, poppler-utils) during review setup; document versions and commands | [staff] |
| Reusable readers, calculators, retrieval tools and prompts built after inspecting Test sources and Test question wording | [staff] |
| Prebuilt artifacts (OCR text, parsed files, chunks, indexes) with scripts and settings to regenerate them | [staff] |
| Quantization of the chosen model | [rules] |
| Plain-language citations if they identify the source and a verifiable location (page, cell, record, feature) | [staff] |
| Non-ASCII UTF-8 characters in answers and evidence | [staff] |
| Additional datasets only after organiser approval (proposal deadline **30 Nov 2026**) | [rules] |
| Fine-tuning or adaptation using **Train and AllowedDatasets sources only** (see 4.3) | [rules] |

### 4.2 Not allowed
| Item | Source |
|---|---|
| More than one generative model in the answering pipeline | [rules] |
| Any model over the 3B class, including via quantization | [rules][staff] |
| Hard-coded answers; Test ID lookups | [staff] |
| Training, fine-tuning, distillation, self-training, pseudo-labelling or other learned adaptation on Test questions, Test sources, their derivatives or answers generated from them | [rules] |
| AutoML tools | [rules] |
| Closed-source or paid tools; paid services or free trials that need a credit card; non-public packages | [rules] |
| Custom packages inside the submission notebook | [rules] |
| Sharing code privately outside your team; multiple accounts; membership in several teams | [rules] |
| Generative OCR/vision models (e.g. VLM-based OCR) or an LLM extractor as an extra model in the pipeline | [rules] (our reading) |
| Using shared generative preprocessing without charging it to every question that uses it | [staff] |
| Exceeding 1 submission per day or 20 in total | [rules] |

### 4.3 Conditions and cautions
- **Shared generative preprocessing** counts in full against each question that uses it (a call shared by 5 questions is charged 5 times). Keep a per-question call log [staff].
- **Fine-tuning** is allowed only on non-Test data, and you must supply training code and instructions. Synthetic data modelled on Test question wording or templates would be a Test derivative.
- **Negative answers** earn evidence credit by citing the note, definition, field or missing value that shows the gap [staff].
- **Spirit of the challenge**: Zindi may disqualify for violating its spirit. Keep the generative model genuinely in the loop (planning, parameter extraction, answer writing, verification), not a pipeline that is entirely templates.
- **Judge models** are dev-only and must not be part of the answer path. Keep them out of the submitted entrypoint.

### 4.4 Logistics [rules][staff]
- Closes **13 Dec 2026**. Select two final submissions before then; otherwise the two best public scores are auto-selected.
- Provisional top 100: code request by email, **48 hours** to respond. Code may be requested earlier from anyone, with 24 hours.
- Review environment: isolated **Ubuntu 24.04** container, **RTX 4090 (24 GB)**, time limit based on the generated-token budget; **internet only for package installation during setup**; keep every needed file available locally; README with the regeneration command; template repo to be provided.
- Document seeds, model versions, settings, dependencies, data sources and licences. Release original code under an approved permissive licence.
- Submission: one UTF-8 `.md` following `SampleSubmission.md`, every Test ID once, in order, each with `### Evidence`. A valid but blank answer scores 0.
- Panel evaluation and funding stages: notification before 28 Feb 2027. This phase has no cash prize.

---

## 5. Non-generative toolbox ("use it everywhere it helps")

| Area | Tool | Notes |
|---|---|---|
| PDF text/tables | pdfplumber, pypdf, poppler `pdftotext` | prefer permissive licences (PyMuPDF is AGPL, so check before using) |
| Scanned PDFs / images | Tesseract via pytesseract/ocrmypdf, poppler `pdftoppm` | staff confirmed; document versions |
| HTML/XML | lxml, BeautifulSoup | strip boilerplate, keep tables |
| Spreadsheets | openpyxl (stored vs displayed values, number formats), odfpy or your ODS parser | Context often says "displayed" vs "stored" |
| Tabular | pandas, duckdb, sqlite3 | joins, uniqueness, null handling |
| Vector/spatial | geopandas, pyogrio, shapely, pyproj (`Geod` for WGS84 ellipsoid) | respect CRS from Context (EPSG:27700, 2154) |
| Raster/point cloud | rasterio, laspy | class-cell areas, pixel selection per ReadingGuide |
| BIM | ifcopenshell | IfcSpace footprint, bounding box |
| Transit | own GTFS loader plus networkx/Dijkstra on pathways | mode filters, traversal time 0, 27:49 times, service-day exceptions, zoneinfo |
| Retrieval | rank_bm25, sentence-transformers with an embedder and a reranker | embeddings/rerankers only, no generation |
| Question understanding | regex/rule classifier, embedding similarity to tool descriptions | must be generic, never ID-keyed |
| Arithmetic | Python `decimal`, explicit rounding rules from Context | |
| Evidence | instrumented `read`/`cell` wrappers that log file + sheet/cell/column/feature | auto-builds citations |

Unconfirmed: small discriminative classifiers/NLI models as helper tools. They don't generate text, but ask staff before depending on them.

---

## 6. Implementation plan with verification and greenlight gates

Each step ends in a gate. **Do not start the next step until the gate is met.** Thresholds are proposals; adjust and record any change.

### Step 0 — Environment and compliance harness
**Do:** pin Python, Ollama (or llama.cpp/vLLM) and package versions; load Qwen3.5-2B; install `qa_runtime.py`; create a run folder on Drive; choose the engine; add the audit script that reads ledgers.
**Verify:** `/api/show` parameter count recorded; with `think: True`, `message.thinking` is non-empty; Ollama `eval_count` covers thinking tokens (compare with output length); the wrapper's mocked tests pass; a fresh Colab runtime reproduces setup from the README commands.
**Greenlight:** all checks pass and the parameter count and checkpoint are written to `MODEL.md`.

### Step 1 — Inventory
**Do:** parse-probe every file in the 22 Train and 161 Test sources. Write `inventory.csv`: source id, extension, parse status, rows/sheets/features/pages/CRS, scanned-vs-text PDF flag, notes.
**Verify:** manifest hashes match; every file is classified as parsed, partly parsed or unsupported-with-reason.
**Greenlight:** 100% classified, and parsing succeeds for at least 95% of files weighted by the questions that reference them. Unsupported files are listed and a decision is recorded for each.

### Step 2 — Schema cards and guide index
**Do:** generate a compact, code-computed profile per file (columns, dtypes, sample rows, uniques, null counts, sheet names, CRS, GTFS table relationships) and an index of guide/readme/metadata/notes files per pack. Include all guides listed in a question's `Source_IDs` by default.
**Verify:** cards regenerate deterministically; each pack's card fits the context budget (about 1.5k tokens); manual spot check of 10 cards against the raw files.
**Greenlight:** cards exist for all 64 packs and the spot check finds no errors.

### Step 3 — Dev set and references
**Do:** Train's 9 questions plus at least 40 new questions you write about the **Train sources** (Liverpool TPO, UK bus workbooks, Helsinki energy), covering conclusions-not-supported traps, and with the same Context style. Compute every reference two independent ways. No Test content.
**Verify:** the two computations agree; hold out about 30% as untouched.
**Greenlight:** at least 45 dev questions with agreed references; the hold-out is sealed.

### Step 4 — Tool library per source family
**Do:** implement tools for the largest packs first (Section 2.2), each as a function with explicit parameters, returning value, unit, scope and **the source locations it touched**. Add unit tests from Train references (Section 2.4) and edge cases (displayed vs stored, CRS, `blank location_type = 0`, times past 24:00, leap day, ties).
**Verify:** unit tests pass; reruns are bit-identical; each Test-pack tool is cross-checked with a second implementation on a sample (e.g. pandas vs sqlite, shapely vs geopandas); invariants checked (shares sum to 100%, counts non-negative, areas plausible).
**Greenlight (per family):** tests pass, cross-check agrees, and no tool contains pack IDs, question IDs or answer constants. Proceed once the top 6 families are green; continue with the rest in parallel with later steps.

### Step 5 — Planner and evidence recorder
**Do:** the model reads the question, Context and schema card and emits a small JSON plan (tool name plus arguments). Code validates it against the tool registry, runs it, and builds evidence from the recorder. For "cannot conclude" answers, cite the note/definition/scope location.
**Verify:** on dev paraphrases, correct tool selection and arguments; 100% of tool-derived numbers carry a location; the audit script finds no ID-keyed logic.
**Greenlight:** tool-selection accuracy of at least 90% on the hold-out, and evidence present for all computed values.

### Step 6 — Agent loop, thinking and budget
**Do:** use `qa_runtime.ask` and `ask_with_fallback` for every call. Thinking on for planning/code, off for the final answer, with the model-card sampling profiles. A majority vote across seeds on **computed values** (not prose); validate-and-retry JSON in code. Log every call in a per-question ledger.
**Verify:** the ledger audit reports calls and tokens per question; thinking-loop rate and fallback rate are measured.
**Greenlight:** **zero** questions over 15 calls or 12,000 tokens on a full 600-question run (hard requirement); fallback and loop rates documented and acceptable (proposal: under 5% each); reruns with the same seeds reproduce.

### Step 7 — Retrieval for text-heavy packs
**Do:** run OCR where needed, chunk with source-location metadata, BM25 plus embedder plus reranker retrieval, and always include the pack's guide/notes.
**Verify:** recall@k on dev questions that rely on text; OCR sample checked by eye.
**Greenlight:** answer-bearing passage in the top-k for at least 90% of dev text questions.

### Step 8 — Answer composer and submission validator
**Do:** answers restate each sub-question in order, give exact values with units and the Context's rounding, then the caveat. Validate with `save_submission` plus extra checks (UTF-8, IDs in order, one evidence block each). Keep answers plain and reasonably short, since the Zindi scorer has failed on some submissions for unclear reasons.
**Verify:** element-coverage check (each question clause answered); a dry run of the file through the validator.
**Greenlight:** validator passes on all 600 and element coverage is complete.

### Step 9 — Evaluator (layered)
**Do:** Layer 1 deterministic checks (numbers must appear in tool output, cited cells must exist, unit/rounding match Context, every clause answered). Layer 2 judge (`judge_eval.py` or an extended version) with tool access and code-verified quotes. Layer 3 manual spot check of 20–30 answers per domain. Failure taxonomy per question: tool failed, retrieval miss, wrong convention, wrong unit, insufficient-info mishandled, format.
**Verify:** **planted-error test**: take known-correct answers, corrupt the number/unit/scope, and confirm the judge catches at least 95%; judge agrees with your manual labels on Train and dev at least 90% at element level.
**Greenlight:** both thresholds met. Until the data-transmission question (Section 8, item 4) is cleared, run the external judge on Train/dev only.

### Step 10 — Model bake-off
**Do:** compare Qwen3.5-2B (thinking on vs off) and Granite 4.2 3B, each at Q8/FP16 and the default quant, on the dev set. Measure plan validity, final accuracy, tokens, loop rate and runtime.
**Verify:** same seeds and prompts per candidate; ledgers kept.
**Greenlight:** one model chosen on dev metrics; exact checkpoint and parameter count documented.

### Step 11 — Zindi submission policy
**Do:** submit only when the local dev score improved and the Layer 1 checks are clean. Log every submission (date, config, local score, public score).
**Verify:** maintain a counter against 20 total and 1 per day.
**Greenlight:** each submission requires validator pass plus a local improvement. Keep at least 5 submissions in reserve for the last week. Select two finals by 12 Dec: one safe, one ambitious.

### Step 12 — Reproducibility package and dry run
**Do:** README with the single regeneration command; pinned dependencies; apt package list; local weights; seeds; ledgers; source/licence declaration; permissive licence; no judge code in the entrypoint.
**Verify:** run end-to-end in a clean Ubuntu 24.04 GPU container with network only for package installation; run the ledger audit; grep the repo for Test IDs and answer constants; confirm no Test-derived training.
**Greenlight:** clean run reproduces your submission within tolerance, audit shows zero budget violations, and the grep is clean.

### Step 13 — Optional: fine-tuning
Only after Steps 0–12 are green and after confirming in Section 8 items 5–6. Train/AllowedDatasets data only; ship training code.

### Suggested timeline
| Dates | Work |
|---|---|
| 14–20 Nov | Steps 0–3; Step 4 for the first 3 families |
| 21–27 Nov | Step 4 remaining top families; Steps 5–6 |
| 28 Nov–4 Dec | Steps 7–9 (30 Nov: dataset-proposal deadline, not planning to use external data) |
| 5–9 Dec | Step 10, tuning, submissions |
| 10–12 Dec | Step 12 dry run; select two finals |
| 13 Dec | Challenge closes |

---

## 7. Pre-submission compliance checklist
- [ ] One generative model only; checkpoint and total parameter count documented
- [ ] Ledger shows at most 15 calls and 12,000 generated tokens for **every** question, including shared preprocessing and retries
- [ ] No hard-coded answers; no Test ID lookups; repo grep for Test IDs is clean
- [ ] No training, tuning or pseudo-labelling using Test questions, sources or generated answers
- [ ] OCR and retrieval tools are non-generative; embedders/rerankers only emit vectors or scores
- [ ] No judge model in the answer path
- [ ] Submission is UTF-8 Markdown, every ID once and in order, each with Evidence
- [ ] Evidence names a file and a verifiable location; negative answers cite the scope note
- [ ] Seeds fixed, versions pinned, licences and attributions declared
- [ ] No paid tool or credit-card-gated service in the reproducible pipeline

---

## 8. Open items to confirm with Zindi (defaults until answered)
| # | Question | Why it matters | Default until answered |
|---|---|---|---|
| 1 | May we read guide files not listed in a question's Source_IDs (e.g. the Dublin trees guide)? Asked in the comments, unanswered | Missing caveats cost correctness | Use only listed sources |
| 2 | Will fresh review cases come from the same datasets or new ones? | Decides how generic the tools must be | Build generic readers with fallbacks |
| 3 | May the reproduction run `ollama pull`, or must weights be bundled? Internet is for package installation only | The notebook currently pulls weights | Bundle weights locally |
| 4 | Does sending Test questions/sources to an external API (OpenRouter providers) count as unauthorised sharing of challenge data? | Data-sharing and licence terms | Train/dev only for the external judge |
| 5 | May a larger teacher model generate synthetic training data from Train sources? | Gate for any fine-tuning | No fine-tuning |
| 6 | May small discriminative classifiers (not generative) be used as helpers? | Router/NLI options | Rules and embedding similarity only |
| 7 | Does a draft model for speculative decoding count as a second generative model? | Speed only | Don't use |
| 8 | The full rules say teams are groups of **at least two** people; the Zindi page allows up to four. Does a solo participant qualify for panel evaluation? | Eligibility | Check your own status |
| 9 | Scorer failures: what causes "failed to score"? Zindi is investigating | Wasted daily submissions | Keep answers short and plain; validate locally |
| 10 | AllowedDatasets.csv: which sources are listed for training vs inference? Not yet reviewed | Gate for external data | Use none |
| 11 | Exact parameter count of the chosen checkpoint (Qwen3.5-2B and Granite 4.2 3B) | Required documentation | Read from `/api/show` or the checkpoint metadata |

---

## 9. Risk register
| Risk | Impact | Mitigation |
|---|---|---|
| Small model writes unreliable free-form code | Low accuracy | JSON plans over tested tools; validate and retry |
| Thinking loops consume the budget | Budget violation | `qa_runtime` caps, fallback, loop-rate monitoring |
| Tools overfit to current packs | Fails on fresh cases | Generic readers; test with paraphrases and variant schemas |
| Judge hallucination | False confidence | Layer 1 deterministic checks; code-verified quotes; planted-error test |
| Test data leakage to third parties | Rules breach | Train-only judging until cleared |
| Ollama quirks (`format`, thinking) | Unstable runs | Pin version; validate in code; consider llama.cpp |
| Scorer failures | Lost daily slots | Local validation; early format check |
| Colab session limits | Lost progress | Checkpoint to Drive every 10 questions |

---

## 10. Appendix
- Submission format: `# Submission`, then for each ID `## <ID>`, the answer, `### Evidence`, bullet lines. Answers must not contain a line starting with `## `.
- Domain weights: transport, buildings_energy and urban_greenery count equally, regardless of question counts.
- Test question counts: buildings_energy 261, urban_greenery 191, transport 148; total 600.
- Context conventions seen so far: displayed vs stored cell values; two-decimal rounding for percentages and pp changes; EPSG:27700 (Liverpool) and EPSG:2154 (Lille); WGS84 ellipsoidal distances with inclusive radius boundaries (GTFS); service day 4 Nov 2026; blank `location_type` counts as 0; step-free model permits modes 1, 5, 6, 7.
