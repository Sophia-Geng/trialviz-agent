# TrialViz Agent: ClinicalTrials.gov Query-to-Visualization Backend

A backend service that turns a natural-language question about clinical trials into a **structured visualization spec**. The data comes from the [ClinicalTrials.gov Data API v2](https://clinicaltrials.gov/data-api/api). Every number in the output is counted by deterministic code from real trial records, and every bar, time bucket, node and edge carries **deep citations** back to the records that produced it.

```
POST /visualize  {"query": "Compare phases for trials involving Pembrolizumab vs Nivolumab."}
  -> {"visualization": {"type": "grouped_bar_chart", "title": ..., "encoding": ..., "data": [...]}, "meta": {...}}
```

## Contents

1. [Overview and architecture](#1-overview-and-architecture)
2. [Quick start](#2-quick-start)
3. [Request schema](#3-request-schema)
4. [Response schema](#4-response-schema)
5. [Supported questions and visualizations](#5-supported-questions-and-visualizations)
6. [Key design decisions and tradeoffs](#6-key-design-decisions-and-tradeoffs)
7. [Handling real-world API data](#7-handling-real-world-api-data)
8. [Testing and validation](#8-testing-and-validation)
9. [Example runs](#9-example-runs)
10. [Limitations and future work](#10-limitations-and-future-work)
11. [AI tools usage](#11-ai-tools-usage)

---

## 1. Overview and architecture

The LLM does exactly one job: it turns the question into a constrained **QueryPlan**, a small JSON object made of enums and entity strings. It never sees or produces any number about trials. Fetching, normalizing, counting, building the chart and collecting citations are all done by plain Python code.

```
Request ─► [1] Pydantic request validation
             │
             ▼
          [2] Planner (OpenAI Structured Outputs, strict JSON schema) ──► QueryPlan
             │      ▲
             │      └─ business-rule violations fed back once; still invalid / LLM down ─► keyword fallback
             ▼
          [2b] Grounding: each search term is counted on the live API; a term with 0 trials (typo) is sent
             │     back for correction, and the correction is kept only if the API confirms it finds trials
             ▼
          [3] Plan rules + explicit request fields override + scope guard
             │
             ├─ answer_mode = unsupported ─► visualization: null + reason
             ▼
          [4] ClinicalTrials.gov client: paging, field selection, 1 req/s, retry, disk cache
             │
             ▼
          [5] Dimension registry: extract + normalize (phase, dates, drugs via MeSH, countries, ...)
             │
             ▼
          [6] Aggregation engine: group_by / partition / co_occurrence (citations collected while counting)
             │
             ▼
          [7] Spec builder: visualization {type, title, encoding, data} + meta
```

| Module | Responsibility |
|---|---|
| `app/schemas/` | Request, QueryPlan and response models (Pydantic v2). The response is a discriminated union on `visualization.type` |
| `app/planner/` | Prompt (generated from the dimension registry), OpenAI adapter, rule retry, search-term grounding against the API, keyword fallback |
| `app/validation/plan_rules.py` | Business rules, merging of explicit request fields, scope guard |
| `app/ctgov/` | API client and query-parameter builder |
| `app/data/dimensions.py` | **Dimension registry, the single extension point** (17 dimensions) |
| `app/data/normalize.py` | Normalization of raw values |
| `app/aggregate/engine.py` | Counting and co-occurrence, plus citations |
| `app/viz/builder.py` | Chart specs and render hints |
| `app/pipeline.py` | Executes a validated plan end to end |
| `app/main.py` | FastAPI app |

## 2. Quick start

Requires Python 3.11+.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # then set OPENAI_API_KEY (and optionally OPENAI_MODEL, default gpt-4.1)

uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000/docs> for the interactive API, or:

```bash
curl -X POST http://127.0.0.1:8000/visualize \
  -H "Content-Type: application/json" \
  -d '{"query": "How has the number of trials for this drug changed per year since 2015?", "drug_name": "Pembrolizumab"}'
```

**Optional demo viewer:** <http://127.0.0.1:8000/demo> renders the specs in the browser, both the files in `examples/` and live questions typed into the page. Click a bar, point, node or edge to list its citations, with links to the trial records. It is a single static page (`demo/index.html`, Vega-Lite + vis-network from a CDN) that uses only the documented response schema. It doubles as a check that a frontend can render every chart type without guessing.

Without `OPENAI_API_KEY` the service still runs. It uses the keyword fallback planner and says so in `meta.notes`.

| Command | Purpose |
|---|---|
| `pytest` | Run the test suite (260 tests, offline, uses recorded real API responses) |
| `ruff check . && ruff format --check .` | Lint and format check, config in `pyproject.toml` (`pip install -r requirements-dev.txt`) |
| `python scripts/run_examples.py` | Regenerate `examples/` through the HTTP API (needs the API key) |
| `python scripts/eval_planner.py [models...]` | Planner evaluation over 25 labeled questions, writes `docs/planner_eval.md` (needs the API key) |
| `python scripts/run_plan_demo.py` | Run hand-written QueryPlans against the live API without the LLM |
| `python scripts/record_fixtures.py` | Re-record `tests/fixtures/` from the live API |

Configuration (environment variables or `.env`):

| Variable | Default | Meaning |
|---|---|---|
| `OPENAI_API_KEY` | – | Enables the LLM planner |
| `OPENAI_MODEL` | `gpt-4.1` | Any chat model with Structured Outputs; `gpt-5*` models get `reasoning_effort=low` instead of `temperature=0` |
| `CTGOV_MIN_INTERVAL_S` | `1.0` | Minimum seconds between API requests |
| `CACHE_ENABLED` / `CACHE_DIR` | `true` / `.cache` | On-disk cache for API and LLM responses |

## 3. Request schema

`POST /visualize`, JSON body. The full JSON Schema is served at `GET /schema` (`request` key).

| Field | Type | Required | Validation / meaning |
|---|---|---|---|
| `query` | string | **yes** | 1–500 characters, trimmed |
| `drug_name` | string \| string[] | no | A list of 2–4 distinct names requests a comparison (one cohort each) |
| `condition` | string \| string[] | no | Same as `drug_name`. Comparing drugs and conditions at the same time is rejected |
| `sponsor` | string | no | Lead sponsor (`query.lead`) |
| `country` | string | no | Country of at least one site (`query.locn`) |
| `trial_phase` | enum[] | no | `EARLY_PHASE1`, `PHASE1`, `PHASE2`, `PHASE3`, `PHASE4`, `NA` |
| `status` | enum[] | no | API overall status, e.g. `RECRUITING`, `COMPLETED`, `ACTIVE_NOT_RECRUITING` (all 14 values) |
| `start_year` | int | no | 1990 to current year + 5. Trials starting in or after this year |
| `end_year` | int | no | Same range, ≥ `start_year` |
| `viz_type` | enum | no | Force a chart type (`bar_chart`, `grouped_bar_chart`, `time_series`, `network_graph`, `metric`, ...). It must still pass the plan rules, otherwise the response is 422 `PLAN_ERROR` |
| `max_records` | int | no | Default 10000, 1–20000. Upper bound on studies fetched per cohort |
| `top_n` | int | no | Default 15, 1–50. Maximum categories shown (networks: 2 × `top_n` nodes) |

Unknown fields are rejected. **Explicit fields take precedence** over values the planner reads from `query`. Every conflict is reported in `meta.notes`, e.g. `Using intervention 'pembrolizumab' from the request instead of 'keytruda' read from the query.`

## 4. Response schema

Full JSON Schema: `GET /schema` (`response` key). Every response has two parts:

```jsonc
{
  "visualization": { "type": "...", "title": "...", "encoding": { }, "data": [ ] },  // null only if answer_mode = "unsupported"
  "meta": { ... }
}
```

### 4.1 Data rows use fixed keys

Data rows always use the same keys (`category`, `series`, `trial_count`, `year`, `bin_start`, ...), whatever the dimension. That gives each chart type a static schema, so a renderer never has to guess keys. The human-readable axis title (e.g. "Phase", "Country (recruiting sites)") is in `encoding.<channel>.label`. Each channel is `{"field": <row key>, "type": "nominal" | "ordinal" | "quantitative" | "temporal", "label": <axis title>}`.

### 4.2 Visualization types

| `type` | `encoding` | `data` |
|---|---|---|
| `bar_chart` | `x` (category), `y` (trial_count) | `[{category, trial_count, citation_count, citations}]` |
| `grouped_bar_chart` | `x`, `y`, `series` | `[{category, series, trial_count, citation_count, citations}]`. Every category × series pair is present, with 0 when empty |
| `time_series` | `x` (year, temporal), `y` | `[{year, trial_count, estimated_count, incomplete, citation_count, citations}]`, ascending, gaps filled with 0. `estimated_count` = trials whose start date is ESTIMATED (planned). `incomplete: true` marks the current and future years, whose counts will still grow: draw them dashed so a drop there is not read as a decline |
| `network_graph` | `nodes: {id, label, group: "type", size: "trial_count"}`, `edges: {source, target, weight, directed: false}` | `{nodes: [{id, label, type, trial_count, degree, citation_count, citations}], edges: [{source, target, weight, citation_count, citations}]}` |
| `metric` | `value` | `[{value, label, citation_count, citations}]`, exactly one row |
| `histogram` | `x` (bin_start), `x2` (bin_end), `y` | `[{bin_start, bin_end, trial_count, citation_count, citations}]`, bins `[x, x2)`, ascending, empty bins included. `meta.binning.method` is `log` (1-2-5 edges: 0, 1, 2, 5, 10, 20, ... for long-tailed enrollment / site counts) or `linear` (nice equal widths) |
| `scatter_plot` | `x`, `y` | `[{nct_id, x, y, label, citations}]`, one point per trial (up to 2000). `citations` holds the two raw values behind x and y |

Ordinal categories (phase) come in their natural order: Early Phase 1 → Phase 1 → Phase 1/2 → Phase 2 → Phase 2/3 → Phase 3 → Phase 4 → Not Applicable → Unknown. Nominal categories are sorted by `trial_count` descending and cut to `top_n`. `meta.sort` always says which order applies.

Example (`examples/02_bar_phase_distribution.json`, one row shown):

```json
{
  "type": "bar_chart",
  "title": "Trials by phase: Alzheimer's disease",
  "encoding": {
    "x": {"field": "category", "type": "ordinal", "label": "Phase"},
    "y": {"field": "trial_count", "type": "quantitative", "label": "Trials"}
  },
  "data": [
    {
      "category": "Phase 2",
      "trial_count": 600,
      "citation_count": 600,
      "citations": [
        {
          "nct_id": "NCT07094516",
          "field": "protocolSection.designModule.phases",
          "excerpt": "[\"PHASE2\"]",
          "title": "A Clinical Trial to Learn About the Effects of VHB937 in People With Early Alzheimer's Disease",
          "url": "https://clinicaltrials.gov/study/NCT07094516"
        }
      ]
    }
  ]
}
```

Network example (`examples/05_network_sponsor_drug.json`, one node and one edge):

```json
{
  "nodes": [{"id": "lead_sponsor:national cancer institute (nci)", "label": "National Cancer Institute (NCI)",
             "type": "lead_sponsor", "trial_count": 197, "degree": 13, "citation_count": 197, "citations": ["..."]}],
  "edges": [{"source": "lead_sponsor:national cancer institute (nci)", "target": "drug:aldesleukin", "weight": 54,
             "citation_count": 54,
             "citations": [{"nct_id": "NCT00513604",
                            "field": "protocolSection.sponsorCollaboratorsModule.leadSponsor.name | derivedSection.interventionBrowseModule.meshes[0].term",
                            "excerpt": "National Cancer Institute (NCI) | aldesleukin", "...": "..."}]}]
}
```

Node ids are `<dimension>:<normalized value>`, so the same name in two dimensions never collides. Edge weight = number of distinct trials in which both endpoints appear.

### 4.3 Citations

| Field | Meaning |
|---|---|
| `nct_id` | Trial ID |
| `field` | Path of the supporting field in the API response, **including the array index** (e.g. `...interventions[2].name`). Network edges join both endpoints' paths with ` \| ` |
| `excerpt` | **Exact raw value of that field as returned by the API, before normalization** (e.g. `["PHASE1","PHASE2"]` behind the "Phase 1/2" bar, or `MK-3475` behind the "pembrolizumab" node) |
| `title` | Brief title, for context only. It is not the evidence |
| `url` | `https://clinicaltrials.gov/study/<nct_id>` |

Each datum carries at most 5 citations. `citation_count` is the total number of supporting trials (for counts this equals the value itself).

### 4.4 `meta`

| Field | Meaning |
|---|---|
| `answer_mode` | `visualization`, `single_value` (metric) or `unsupported` |
| `query_interpretation` | One sentence saying what was counted |
| `filters_applied` | Final filters and the exact API parameters sent, per cohort for comparisons |
| `plan` | The validated QueryPlan (for transparency and debugging) |
| `units`, `sort`, `time_granularity` | Render hints, e.g. `{"y": "trials"}`, `{"field": "trial_count", "order": "desc"}`, `"year"` |
| `grouping` | `{group_by, split_by, multi_valued}`. `multi_valued: true` means one trial can appear in several bars (countries, drugs, conditions), so bars may sum to more than the trial count |
| `record_counts` | `{total_matching, fetched, truncated, count_method}`. `count_method` is `full_scan` (counted over all fetched records) or `api_total` (metric: the API's exact `totalCount`) |
| `notes` | Assumptions, truncation, removed Unknown values, cohort overlap, planner retries or fallback, scope limits |
| `source` | `{name, api, api_version, data_timestamp, retrieved_at}` from the API's `/version` endpoint |

### 4.5 Errors

Every error has the same shape: `{"error": {"code": ..., "message": ..., "details": [...]}}`.

| HTTP | `code` | When |
|---|---|---|
| 422 | `VALIDATION_ERROR` | Request body invalid (`details` lists each field) |
| 422 | `PLAN_ERROR` | Explicit fields make the plan impossible, e.g. `viz_type: "time_series"` for a phase question |
| 502 | `UPSTREAM_ERROR` | ClinicalTrials.gov unreachable after retries |

## 5. Supported questions and visualizations

Every question type runs through the same pipeline. A question type is just a combination of a chart type and dimensions from the registry:

| Question (from the assignment appendix) | Plan | Output |
|---|---|---|
| How has the number of trials for *[drug]* changed per year since 2015? | `group_by=start_year` | `time_series` |
| How many trials started each year for *[condition]*? | `group_by=start_year` | `time_series` |
| How are *[condition]* trials distributed across phases? | `group_by=phase` | `bar_chart` |
| What are the most common intervention types for *[drug/condition]*? | `group_by=intervention_type` | `bar_chart` |
| Compare phases for trials involving *Drug A* vs *Drug B* | 2 cohorts, `group_by=phase` | `grouped_bar_chart` |
| Compare sponsor categories across two conditions | 2 cohorts, `group_by=sponsor_class` | `grouped_bar_chart` |
| Which countries have the most recruiting trials for *[condition]*? | `group_by=recruiting_country` | `bar_chart` |
| Show a network of sponsors ↔ drugs for *[condition]* trials | `network=(lead_sponsor, drug)` | `network_graph` |
| Which drugs frequently co-occur in combination studies? | `network=(drug, drug)` | `network_graph` |
| What is the distribution of enrollment sizes in *[condition]* trials? | `measure.x=enrollment` | `histogram` |
| Is there a relationship between enrollment and duration? | `measure=(enrollment, duration_months)` | `scatter_plot` |
| How many trials are recruiting for *X*? | `answer_mode=single_value` | `metric` |
| Is *X* safe / better than *Y*? (not in the registry) | `answer_mode=unsupported` | `visualization: null` + reason and an answerable rephrasing |

The registry has 17 dimensions, and they combine freely (e.g. `split_by` for two categorical dimensions in one population, or `condition ↔ drug`, `investigator ↔ sponsor`, `site ↔ sponsor` networks):

| Kind | Dimensions |
|---|---|
| Categorical | `phase`, `overall_status`, `study_type`, `lead_sponsor`, `sponsor_class`, `intervention_type`, `intervention_name`, `drug`, `condition`, `country`, `recruiting_country`, `site`, `investigator` |
| Temporal | `start_year` |
| Numeric | `enrollment`, `duration_months`, `site_count` |

## 6. Key design decisions and tradeoffs

**1. The LLM plans, code counts.** The LLM never writes a number into the output, so hallucination can only happen when the question is *interpreted*, and that step is constrained in four layers. (a) OpenAI Structured Outputs in strict mode: dimensions and chart types are enums, so the JSON always parses. (b) Business rules (`plan_rules.check`): e.g. `time_series` needs a temporal dimension, networks need two categorical endpoints, comparisons need 2–4 cohorts. Violations are sent back to the model for **one corrective retry**. (c) **Grounding against the registry**: the prompt tells the model to copy names as written and never "translate" a drug from memory. A plan can still be well formed but useless, e.g. a misspelled "pembrolizmab" that matches no trial. So every search term is counted on the API (one cached request each). Only terms with zero hits are sent back for correction, and the correction is accepted only if the API confirms that fewer terms come back empty. (d) A keyword fallback, if the plan is still invalid or the LLM is unavailable. Retry, grounding corrections and fallback are all reported in `meta.notes`. The final plan is returned in `meta.plan`, so the interpretation can always be audited.

**2. One extension point: the dimension registry.** A dimension declares its API fields, its kind, and an extractor that returns `(normalized key, label, raw excerpt, field path)`. The prompt's dimension list is generated from the registry, so the planner can only ask for what the pipeline can execute. The few-shot plans in the prompt are real `QueryPlanLLM` objects, and tests check them against the rules. Adding a question type usually means adding one registry entry.

**3. Strict drug search, measured against the website.** Plain `query.intr=pembrolizumab` applies concept expansion and returns 3005 trials (the website shows the same 3005). We spot-checked trials that are in this set but not in the strict one. In their full records, no field mentions pembrolizumab, Keytruda or MK-3475 (e.g. NCT03719105, a lymphoma chemotherapy trial). The system therefore searches drugs with `AREA[InterventionName]"x" OR AREA[InterventionMeshTerm]"x"`, which gives 2659 trials. `InterventionName` already matches registered synonyms: adding MK-3475 / Keytruda explicitly did not change the count, and searching by brand name returns exactly the same trial set as the generic name (Keytruda = pembrolizumab: 2659 trials, 0 differing; Opdivo = nivolumab: 1766, 0 differing). The planner therefore copies drug names as the user wrote them and never "translates" them from memory. Condition search (`query.cond`) keeps the API's expansion (breast cancer ↔ breast neoplasms), which is the behavior we want.

**4. Drug identity via MeSH, with batch-level resolution.** Registered names are inconsistent ("MK3475", "Pembrolizumab KEYTRUDA®", "Pembrolizumab 200 mg IV"). The `drug` dimension uses the API's intervention MeSH terms, so those all become one node `pembrolizumab`, and placebo never appears. Salt forms are merged (fludarabine phosphate → fludarabine). MeSH target annotations such as "CTLA-4 Antigen" are dropped. Without MeSH, cleaned names are used and combinations are split ("CBA-1535+Pembrolizumab"). Drug regimens registered under a non-drug type exist (NCT02704156 lists *"Device: Cyberknife plus Pembrolizumab and Trametinib"*). For those, the system extracts *tentative* names and keeps only the ones that match a drug confirmed elsewhere in the same batch. This recovers the drug without letting device names in.

The hard part is that **MeSH terms belong to the whole trial, not to one intervention**. A trial with a drug arm and a mindfulness arm lists "Mindfulness" next to the drug. A blocklist cannot keep up with that, so a drug must be backed by evidence:
- A MeSH term counts as a drug if it is a supplementary chemical concept (MeSH id `C…`, e.g. pembrolizumab `C582435`), if every intervention of the trial is a drug (placebo arms aside), or if a drug-type intervention's name or registered other names mention it (`otherNames` holds "MK-3475", "KEYTRUDA®").
- A registered name without MeSH support is trusted only if it looks like a drug: a [WHO INN stem](https://www.who.int/teams/health-product-and-policy-standards/inn) (-mab, -nib, -platin, -statin, -gliflozin, ...) or a development code (HMPL-013, MK-0616). Any other name is tentative and needs confirmation elsewhere in the batch.

An audit of the 10000-trial unscoped sample (example 11) shows the effect. Distinct drug keys fell from 3319 to 2051, and entries such as "test", "app", "mindfulness", "tablets", "time", "quality of life" and "experimental group" are gone from the top 300.

**5. Phase is single-valued.** `["PHASE2","PHASE3"]` becomes one bar, "Phase 2/3", so phase bars sum exactly to the number of trials. The website's "Phase 3" filter instead includes Phase 2/3 trials. We checked this on Alzheimer's: website Phase 3 = our Phase 3 (317) + Phase 2/3 (68).

**6. Multi-valued dimensions count each trial once per value.** A trial with 40 US sites counts once for the United States. Bars can then sum to more than the trial count, and the response says so (`meta.grouping.multi_valued` plus a note).

**7. Accuracy over latency: full scans.** The API has no aggregation endpoint: `/stats/field/values` covers the whole registry and cannot be filtered. All counts are therefore computed client-side over **every** matching record (default cap 10000 per cohort, 1000 per page, only the needed `fields`). Every example except 11 is a full scan with `truncated: false`. Example 11 is an unscoped question over 180k trials and shows the truncated case. If the cap is hit, `record_counts.truncated` is true and a note says the values come from the first N records in API order (not a random sample). Metric answers use the API's exact `totalCount` instead.

**8. Rate limiting and caching.** The rate limit is undocumented, but about 10 requests in 5 seconds return 429. The client therefore serializes requests (1 req/s), honors `Retry-After`, retries 429/5xx with backoff, and does not retry 4xx. API and LLM responses are cached on disk by request hash. That keeps development off the API and makes example runs reproducible.

**9. "Is a visualization needed?"** The planner chooses `answer_mode`. `single_value` returns a `metric` card for "how many" questions. `unsupported` covers efficacy, safety, eligibility and off-topic questions: it returns `visualization: null` with a reason and a rephrasing the system can answer, instead of a misleading chart.

**10. Scope guard.** If a question has no drug, condition, sponsor or country (e.g. "Which drugs co-occur in combination studies?"), the search is limited to interventional trials from the last 5 years instead of the whole registry (611k studies), and a note says so.

**11. Explicit beats inferred.** Structured request fields override the planner. A list in `drug_name` / `condition` always becomes a comparison, and an explicit `viz_type` that contradicts the question is rejected (422) rather than silently changed.

## 7. Handling real-world API data

All of these come from inspecting recorded real responses (`tests/fixtures/`):

| Issue | Handling |
|---|---|
| Phase missing in 6–24% of trials (mostly observational) | "Unknown" bar, kept for phase. In rankings, Unknown is dropped with a note |
| Combined phases | Single label "Phase 1/2" / "Phase 2/3" (§6.5) |
| Dates as `YYYY`, `YYYY-MM` or `YYYY-MM-DD`; ACTUAL vs ESTIMATED | All parsed. Unparseable dates are counted in a note. `estimated_count` per year |
| Site-level recruiting status exists only for active trials | `recruiting_country` uses sites with `status = RECRUITING`, and falls back to the trial's overall status when no site status exists |
| Anonymous sites ("Research Site") and placeholder officials ("Medical Director", "Pfizer CT.gov Call Center") | Filtered out of `site` / `investigator`. Investigators are limited to PIs and study chairs |
| Inconsistent drug names, synonyms, salts, combinations, placebo | §6.4 |
| Non-drugs inside drug data: MeSH terms from non-drug arms ("Mindfulness", "Transcranial Magnetic Stimulation"), procedures ("Biopsy", "Magnetic Resonance Imaging"), dosage forms ("Tablets"), controls registered as DRUG ("Control Group", saline), mis-typed entries ("App", "Test" as DRUG), split-name fragments ("Drug 1 and Drug 2" → "1") | Evidence-based drug identity (§6.4) plus procedure / dosage-form / fragment filters. Found by auditing the top drug keys of a 10000-trial sample, not by guessing. Salt stripping never reduces a name to a bare salt ("sodium chloride" ≠ "sodium") |
| Conditions are free text: 10019 distinct spellings in 10000 trials ("Type 2 Diabetes" / "Diabetes Mellitus, Type 2", "Postoperative Pain" / "Pain, Postoperative", "COVID-19" / "Covid19", "Healthy" / "Healthy Volunteers" / "Healthy Participants") | `condition` uses the API's condition MeSH terms (75% of trials), so labels are MeSH headings such as "Breast Neoplasms". Without MeSH it falls back to the free text, and healthy-volunteer spellings are merged. Result: 3777 distinct values |
| Wrong MeSH mappings in the source: ClinicalTrials.gov tags ~100 of the sampled pain / anesthesia trials with "Agnosia" (a perception disorder), mostly from "Analgesia"; no such trial names agnosia | Listed explicitly (`KNOWN_CONDITION_MISMAPS`): the term is kept only if the free text names it, otherwise it is dropped and "Analgesia" is restored. String similarity cannot detect this generically: "analgesia → agnosia" scores the same 0.62 as the valid "depression → depressive disorder" |
| One investigator, several spellings ("Sean van Diepen, MD" / "Sean van Diepen, MD, MSc") | Grouped by the name before the first comma, without titles |
| Sponsor names with different legal suffixes ("Celgene" / "Celgene Corporation", "Daiichi Sankyo" / "Daiichi Sankyo Co., Ltd.") | Audited on 13782 trials: only 5 groups (6 of 4939 names), since the registry already standardizes sponsors. Grouped by name without legal-form suffix. The name itself is never fuzzy-matched, so "Merck Sharp & Dohme" and "Merck KGaA" stay apart. Subsidiaries of one group (Eisai Inc. / Eisai Co., Ltd.) are merged on purpose |
| Intervention MeSH present for ~90% of drug-searched trials but only 50–67% of condition-searched ones | Fallback to cleaned registered names is required, not optional |
| Cohort overlap (a trial uses both compared drugs) | Counted in each cohort, overlap size noted (174 trials for pembrolizumab vs nivolumab) |
| Search characters that break the query syntax (`" [ ] ( )`) | Stripped. Multi-word values are quoted (`AREA[InterventionName]"trastuzumab deruxtecan"`) |
| No matching trials | Empty `data` plus a note suggesting a spelling check, rather than an invented answer |

## 8. Testing and validation

**Automated tests** (`pytest`, 260 tests, fully offline):

- **Hand-countable aggregation**: small synthetic datasets where the expected counts can be worked out by hand (phase buckets sum to the total, multi-valued dedupe, citations point to raw values, time-series gap filling, top-N truncation, grouped-bar zero fill, network weights and pruning).
- **Real-data extraction**: every dimension runs on 500 recorded real studies (5 queries). Checks: no crashes, one count per distinct value, every value has an excerpt and a field path. Drug synonyms never form their own node, and ≥ 95% of trials matched by the strict drug search map to that drug.
- **Schemas**: request validation edge cases. The local `Phase` / `OverallStatus` enums equal the API's `/studies/enums`. The plan schema passes the OpenAI SDK's strict-schema conversion. The response union round-trips.
- **Planner**: the few-shot plans pass the rules. A scripted fake LLM covers valid plans, a corrective retry (violations are fed back), fallback after two invalid plans, fallback on LLM errors, and rejection of an impossible explicit `viz_type`. A fake API counter covers grounding: a zero-hit term is corrected, a correction the API does not confirm is rejected, and terms with hits trigger no extra LLM call.
- **Client**: an `httpx.MockTransport` covers page tokens (and that later pages drop `countTotal`), `max_records` truncation, 429 retry, no retry on 4xx, giving up after the maximum attempts, and the cache.
- **HTTP**: `TestClient` end to end with a fake API client over fixtures, covering error shapes and `/schema`.

**Manual cross-check against the public website** (done by hand on 2026-11-10, details in [`docs/validation_log.md`](docs/validation_log.md)):

| Check | Website | This system | Result |
|---|---|---|---|
| Alzheimer's disease, total trials | 4299 | 4299 | ✅ |
| Same, Phase 3 filter | 385 | Phase 3 (317) + Phase 2/3 (68) = 385 | ✅ confirms the combined-phase handling |
| Breast cancer, recruiting | 2451 | 2451 | ✅ |
| Intervention "pembrolizumab" | 3005 | API default search 3005; strict search 2659 | ✅ same data; strict search is intentional (§6.3) |
| NCT02704156 interventions | "Device: Cyberknife plus Pembrolizumab and Trametinib" | same raw value and type; resolved to `pembrolizumab` | ✅ |
| NCT00065767 phase | Phase 2/3 | "Phase 2/3" | ✅ |

**Plausibility check: networks rediscover known regimens.** The code knows nothing about diseases or treatment regimens. Still, the unscoped drug ↔ drug network ([`11_network_combinations_unscoped`](examples/11_network_combinations_unscoped.json), 10000 interventional trials started since 2021) separates into clusters that are standard regimens, with edge weights in shared trials:

| Cluster in the graph | Known regimen |
|---|---|
| cyclophosphamide – fludarabine (27, the strongest edge) | lymphodepletion before CAR-T therapy |
| fluorouracil – oxaliplatin (17), fluorouracil – leucovorin (13), irinotecan, capecitabine | FOLFOX / FOLFIRI (colorectal cancer) |
| rituximab, cyclophosphamide, doxorubicin, vincristine, prednisone | R-CHOP (lymphoma) |
| carboplatin – paclitaxel (24), carboplatin – pembrolizumab (14), pemetrexed, bevacizumab | platinum chemotherapy ± immunotherapy (lung cancer) |
| azacitidine – venetoclax (14), cytarabine | acute myeloid leukemia |
| propofol – dexmedetomidine, separate from the rest | anesthesia studies |

Errors in drug normalization or counting would not produce clusters this clean. Inspecting this same network is also how the last non-drug leaks were found and fixed (§7: "Biopsy", "Control Group", split fragments such as "1"). Two of the examples cross-check each other as well. A live question by brand name ("Keytruda vs Opdivo") returned exactly the bars of example 03 (generic names), and the NCT ID sets are identical (§6.3).

**Planner evaluation** ([`docs/planner_eval.md`](docs/planner_eval.md), `python scripts/eval_planner.py`). 25 labeled questions run through the full planner for each model. They cover all appendix question types, the other chart types, the metric and unsupported answer modes, tricky phrasing (brand names, "top 5", an implicit "right now", a typo, a Chinese question, a vague "trends in ...") and an explicit `drug_name` list. A case passes only if every labeled field matches **and** every search term in the final plan finds at least one trial on the live API:

| Model | Passed | Rule retry | Search-term correction | Keyword fallback | Median latency |
|---|---|---|---|---|---|
| `gpt-4.1` (default) | 25/25 | 0 | 1 | 0 | 1.2 s |
| `gpt-4.1-mini` | 25/25 | 0 | 0 | 0 | 1.5 s |
| `gpt-5.4-mini` | 25/25 | 0 | 1 | 0 | 1.3 s |

The first version of this evaluation also reported 25/25, but it only checked plan structure. Checking whether the plans actually find data exposed two real failures that every model shared:
- **Typo**: "pembrolizmab" was copied verbatim, as the prompt says, and matches 0 trials. This led to the grounding step (§6.1c): gpt-4.1 and gpt-5.4-mini now correct it after the API reports zero hits (gpt-4.1-mini corrected it on its own).
- **Chinese question**: "乳腺癌" was copied verbatim. It matches 3 trials versus 17004 for "breast cancer", so it was never zero and grounding could not catch it. The fix is a prompt rule: entity names are written in English, as translation and never substitution. Brand names in the same evaluation ("Keytruda vs Opdivo") are still kept as written.

## 9. Example runs

All files in [`examples/`](examples/) are real outputs from `scripts/run_examples.py`: the request was sent to `POST /visualize`, the plan came from `gpt-4.1`, and the data came live from ClinicalTrials.gov on 2026-11-10. Each file contains the request, the status code and the full response.

| File | Request | Result |
|---|---|---|
| [`01_time_series_drug_trend`](examples/01_time_series_drug_trend.json) | "How has the number of trials for this drug changed per year since 2015?" + `drug_name: "Pembrolizumab"` | `time_series`, 2015–2027: 116 trials in 2015, then 185–263 per year (2027 holds only estimated starts) |
| [`02_bar_phase_distribution`](examples/02_bar_phase_distribution.json) | "How are Alzheimer's disease trials distributed across phases?" | `bar_chart`, 4299 trials, Phase 2 = 600, Not Applicable = 1578 |
| [`03_grouped_bar_drug_comparison`](examples/03_grouped_bar_drug_comparison.json) | "Compare phases for trials involving Pembrolizumab vs Nivolumab." | `grouped_bar_chart`, 174 overlapping trials noted |
| [`04_bar_recruiting_countries`](examples/04_bar_recruiting_countries.json) | "Which countries have the most recruiting trials for breast cancer?" | `bar_chart`, top 15 of 78: United States 915, China 648, France 192 |
| [`05_network_sponsor_drug`](examples/05_network_sponsor_drug.json) | "Show a network of sponsors ↔ drugs for melanoma trials." | `network_graph`, 30 nodes: NCI–aldesleukin 54, BMS–ipilimumab 52, BMS–nivolumab 39, Merck–pembrolizumab 27 |
| [`06_network_drug_combinations`](examples/06_network_drug_combinations.json) | "Which drugs frequently co-occur with nivolumab in combination studies?" | `network_graph`, nivolumab–ipilimumab 555 trials |
| [`07_metric_single_value`](examples/07_metric_single_value.json) | "How many trials are currently recruiting for ALS?" | `metric`: 173 recruiting trials (the API's exact `totalCount`), 5 example citations |
| [`08_unsupported_question`](examples/08_unsupported_question.json) | "Is pembrolizumab safe for pregnant women?" | `visualization: null`, reason plus an answerable rephrasing |
| [`09_histogram_enrollment`](examples/09_histogram_enrollment.json) | "What is the distribution of enrollment sizes in phase 3 breast cancer trials?" | `histogram`, 1566 trials, log bins, mode at 200–500 participants (501 trials) |
| [`10_scatter_enrollment_duration`](examples/10_scatter_enrollment_duration.json) | "Is there a relationship between enrollment and trial duration for completed melanoma trials?" | `scatter_plot`, 1610 points (143 trials lack a value) |
| [`11_network_combinations_unscoped`](examples/11_network_combinations_unscoped.json) | "Which drugs are most often combined in combination studies?" | `network_graph`. No entity given, so the scope guard limits the search to interventional trials since 2021. 180456 match, the first 10000 are used (`truncated: true`, noted). The clusters match known regimens (§8) |
| [`12_network_condition_drug`](examples/12_network_condition_drug.json) | "Show a network of conditions and drugs for trials sponsored by Pfizer since 2020." | `network_graph` with MeSH condition headings: COVID-19 – nirmatrelvir/ritonavir 19, Breast Neoplasms – fulvestrant 12, Multiple Myeloma – elranatamab 12, Carcinoma, Non-Small-Cell Lung – sasanlimab 7 (871 trials, full scan) |

## 10. Limitations and future work

- **Large result sets.** Above `max_records`, values come from the first N records in API order. Planned improvement: per-bucket `countTotal` requests for low-cardinality dimensions (phase, status, year), which give exact counts at one request per bucket. In the longer run, a nightly bulk download indexed locally.
- **Entity resolution.** Sponsors are merged only across legal suffixes. Differently named subsidiaries (Genentech vs Roche) are not grouped. MeSH can list a drug class and a specific drug side by side (e.g. "interleukin-2" and "aldesleukin").
- **`drug` misses about 1–3% of trials in our samples.** This happens when MeSH is present but incomplete (NCT04507906, "Nivolumab Combined with Anlotinib", is tagged only with anlotinib). Merging raw names with MeSH would bring synonym duplicates back. The fix is to map raw names onto MeSH through its synonym table. The evidence rule also has a cost. An old drug without an INN stem (e.g. aspirin) that appears in a mixed trial without a matching MeSH term is only kept if another trial in the batch confirms it. MeSH can also list a class or target next to the drug ("immune checkpoint inhibitors", "interleukin-2" next to "aldesleukin"). Cohort counts are not affected: they come from the API search itself.
- **No eligibility, outcome or result data.** These fields exist in the API but are not modeled as dimensions. Such questions are classified as unsupported.
- **Planner evaluation size.** 25 labeled questions (§8) are enough to find failure classes, not to estimate accuracy precisely. A few hundred questions, ideally from real users, and repeated runs for the non-deterministic `gpt-5*` models would be the next step. Grounding only catches terms with **zero** hits. A term that is valid but too narrow (like "乳腺癌", with 3 hits) needs either the prompt rule or a comparison against expected registry volume.
- **Single-turn only.** Ambiguous questions are resolved by the planner's best guess (visible in `meta.plan` and `query_interpretation`), not by asking a clarifying question.
- **Synchronous fetching.** A large comparison can take tens of seconds at 1 req/s on a cold cache. Caching makes repeated questions fast. Background jobs or streaming would help in production.

## 11. AI tools usage

- **Tools**: Claude Code (Anthropic Claude) was used as a pair-programming assistant for planning, API exploration, implementation, tests and this README. The runtime planner uses OpenAI `gpt-4.1` through the company-provided key.
- **Designed and directed by the author**: scope and priorities, review of the plan against the assignment, the decision to drop the default drug search after reviewing the evidence, and the decision to recover drugs registered under non-drug intervention types.
- **Cross-checking correctness from several angles (author)**: AI explanations were not accepted at face value. Asking where the data behind a claim actually was led to re-checking full API records. That showed the first explanation of the drug-search mismatch was wrong: the cause was concept expansion in the search, not trials mentioning the drug only in their descriptions. Asking "is this certain?" about the missed-drug cases led to inspecting each record, which found three distinct causes instead of the one first assumed (§10). The author also ran every manual check against the public website (§8), including a count that first did not match and was then reproduced exactly.
- **Generated with AI and then iterated**: most code and tests. Several design changes came from inspecting real data rather than from the first draft. Examples: the strict drug search (concept expansion over-matched about 13%); MeSH-based drug identity; tentative drug names resolved at batch level; salt-form and MeSH-target filtering found in the first network output; the rule that unsupported-question rephrasings must be answerable, after the model suggested an eligibility question. Later changes came from systematic checks rather than single observations. Auditing the top values of every dimension over 10000 trials led to evidence-based drug identity, MeSH conditions and the "Agnosia" correction. Re-running the planner evaluation with a check that plans actually find trials led to API grounding of search terms and the English-names rule.
- **How correctness was validated**: hand-countable unit tests, tests over recorded real API responses, enum parity with the live API, a manual comparison with the public website (all checks matched), and inspection of the real example outputs.
