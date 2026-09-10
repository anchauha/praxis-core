# Standards assistant

A local chat interface for planning lessons against the Indiana Academic
Standards index. FastAPI serves the UI and streams chat through a local Ollama.

The home page is now a **lesson planner**. Select standards, save the eleven-field
context, generate a structured lesson with local Ollama, inspect validation
findings, edit the wording and save a revision. Saved sessions reload in the same
browser. General chat is available at `/chat`; it still answers without retrieval.

The planner exposes 711 standards from the 24 manifest-enabled courses. The full
archive remains 774 records from 26 courses, including two explicit course holds.
See [the inference baseline](C:/code/praxis-core/research/inference_baseline.md)
for API details, behavior, validation limits and the conservative prompt budget.

## Run it

Ollama must be running (`ollama serve`) with at least one model pulled.

```bash
uv sync
uv run uvicorn backend.main:app --reload
```

Then open http://127.0.0.1:8000.

## Configuration

Every setting has a working default. To override, create `.env` beside
`pyproject.toml` (see `.env.example`) or set the variable in the environment.

| Variable | Default | Notes |
| --- | --- | --- |
| `CATPC_OLLAMA_HOST` | `http://127.0.0.1:11434` | Where Ollama listens. |
| `CATPC_DEFAULT_MODEL` | `qwen3.5:9b` | Preselected in the model picker. |
| `CATPC_NUM_CTX` | `8192` | Context window, in tokens. |
| `CATPC_SYSTEM_PROMPT` | see `backend/config.py` | Replaced wholesale when set. |
| `CATPC_REQUEST_TIMEOUT` | `600` | Seconds for a full generation. |
| `CATPC_STORE_PATH` | `data/catpc.sqlite` | Event store. Not in version control. |
| `CATPC_COURSE_MANIFEST` | `../standards_extract/course_manifest.json` | Source/version/serving decisions; required by the planner. |
| `CATPC_PLANNING_NUM_PREDICT` | `2048` | Output-token limit for structured lessons. |

`CATPC_NUM_CTX` is the setting most likely to need attention. Ollama sizes its
CUDA compute buffer from it, so on an 8 GB card a 9B model fails to load at the
default context with `cudaMalloc failed: out of memory`. 8192 fits a 9B model on
an RTX 4060 Laptop; lower it if loading still fails, raise it on a larger card.
It is also the budget that retrieved standards will have to fit inside.

## Layout

```
backend/
  main.py           FastAPI routes; converts Ollama's NDJSON into SSE
  ollama_client.py  async HTTP wrapper around /api/chat, /api/tags, /api/version
  corpus.py         read-only summary of the standards index
  config.py         settings
  schemas.py        chat API plus the planning, evidence and lesson contracts
  validation.py     checks a plan against the context and evidence it came from
  store.py          SQLite event store
  retrieval.py      manifest-filtered exact lookup and lexical search
  planning.py       shared context/evidence preparation, prompt and validation
  planning_api.py   browser-owned sessions, generation and revision routes
tests/
  test_store.py     original standalone store tests
  test_contract_integrity.py  provenance, citations, DPO inputs and seed permissions
  test_planning.py  corpus/API/store integration and structured Ollama requests
frontend/
  planner.html / planner.js / planner.css  planning form and lesson editor
  index.html        one page: rail, thread, composer
  app.js            SSE client, markdown rendering, KaTeX typesetting
  styles.css
data/
  standards_index.json   generated; do not edit by hand
```

`data/standards_index.json` is a build artifact copied byte-for-byte from
`standards_extract/standards_index.json`. Regenerate both by running `standards_extract/indexer.py` with a Python environment
containing PyMuPDF. Rebuild the manifest only for an intentional source change,
and review its serving/version decisions before regenerating the index.
It currently holds 774 standards from all 26 courses in
`standards_extract/course_manifest.json`, across three published versions
(2023, 2022 and 2020). Two of those courses are extracted but have `served`
set to false: Quantitative Reasoning (repeated codes) and Environmental Science
(version confirmation). Each one's `notes` field says why. Planning retrieval filters
by the manifest; the archived index includes all 26 courses. Review
metadata now matches the reviewed manifest; extraction review does not certify
that a lesson teaches or assesses a standard.

### API

| Route | Purpose |
| --- | --- |
| `GET /api/health` | Whether Ollama answers, and its version. |
| `GET /api/models` | Installed models, split local and cloud. |
| `GET /api/corpus` | Per-subject counts from the standards index. |
| `POST /api/chat` | Streams `start`, `thinking`, `token`, `done`, `error` as SSE. |

`POST /api/chat` takes `{messages, model, think, temperature}`. The system prompt
is prepended server-side, so the client never sends it. Every turn replays the
full message list. The SQLite store exists, but this route does not yet read or write planning sessions, context/evidence snapshots, generations or preferences.

Models without a thinking mode reject Ollama's `think` field outright, so the
client drops it and retries once when that is the only thing that failed.

## The event store

`store.py` keeps sessions, planning events and export manifests in SQLite.
Events are written once and never edited; an `UPDATE` on the events table is
refused by a trigger. A generation links immutable context and evidence snapshots and stores the exact
prompt messages it was given, so `store.replay(generation_id, owner_id)` rebuilds a past response's
inputs without consulting a corpus that has since changed. That replay is what
makes an SFT row or a DPO pair reproducible.

Four consents are recorded separately per session: classroom use, research
analysis, model training and public sharing. `training_exclusions()` checks both
the inputs and the output, so an asset cleared for classroom use is still
excluded from a training export unless it is cleared for that too.

Session and event reads are scoped by `owner_id`, and a session belonging to someone else
returns nothing rather than an error. Splits are assigned per family, above the
level of a single session, so a paraphrase cannot land on the other side of one.

`redact_session()` is the only path that deletes an event. It exists because a
contributor may withdraw, and it leaves a tombstone in `redactions` recording
that it happened. It removes material from future exports and from retrieval; it
cannot remove what a model has already learnt from it.

The store has five tables: sessions, events, splits, export manifests and
redactions. Its JSON contracts are now version 0.2.0; the SQL layout is unchanged.
Legacy missing origins/permissions remain unknown, and immutable events are not
rewritten. Synthetic session training flags record developer authorization;
they are not participant consent.

`append_preference()` requires distinct existing candidates in the same
owner/session, matching saved context/evidence, and identical nonempty prompts
and prompt versions. `preference_exclusions()` rechecks older pairs and current
permissions before export. It excludes ties, missing label provenance and
incomplete AI-judge metadata. Rejected answers may contain intentional quality
errors; chosen answers must pass the recorded-validation gate. These are
structural/permission gates; curated exporters and label audits remain to build.

Seed rights prose is separate from `training_use: unknown | allowed | denied`.
Both context seeds and evidence excerpts need `allowed` plus a recorded
`training_permission_basis` for training. Unknown/denied values are excluded.

`validate_plan()` binds each standard key to its exact standard evidence ref,
rejects duplicate refs, and returns `source_verification_status` independently
of `alignment_status`. Automated checks leave pedagogical alignment unverified.
It returns a report without mutating the plan; the planning integration attaches
the report/source status. Model drafts cannot assign either verification field.

Run the tests from the app directory with:

```bash
.venv/Scripts/python.exe -X utf8 -B tests/test_store.py
.venv/Scripts/python.exe -X utf8 -B -m unittest discover -s tests -p "test_*.py"
```

## Notes

- Fonts come from Google Fonts and KaTeX from jsDelivr, so first paint needs a
  network connection. Both degrade to system fonts and raw LaTeX offline.
- The planning view was checked at desktop and 390-pixel mobile widths. General chat received no new layout changes.
