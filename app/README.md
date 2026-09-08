# Standards assistant

A local chat interface for planning lessons against the Indiana Academic
Standards index. FastAPI serves the UI and streams chat through a local Ollama.

**Retrieval is not built yet.** The assistant answers from the model's own
knowledge; the standards index is loaded only to report what it contains. The
opening screen says so, and the system prompt tells the model to flag any
standard code as unverified.

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
tests/
  test_store.py     runs standalone or under pytest
frontend/
  index.html        one page: rail, thread, composer
  app.js            SSE client, markdown rendering, KaTeX typesetting
  styles.css
data/
  standards_index.json   generated; do not edit by hand
```

`data/standards_index.json` is a build artifact copied byte-for-byte from
`standards_extract/standards_index.json`. Regenerate both by running
`standards_extract/build_manifest.py` then `standards_extract/indexer.py`.
It currently holds 774 standards from all 26 courses in
`standards_extract/course_manifest.json`, across three published versions
(2023, 2022 and 2020). Two of those courses are extracted but have `served`
set to false; each one's `notes` field says why.

### API

| Route | Purpose |
| --- | --- |
| `GET /api/health` | Whether Ollama answers, and its version. |
| `GET /api/models` | Installed models, split local and cloud. |
| `GET /api/corpus` | Per-subject counts from the standards index. |
| `POST /api/chat` | Streams `start`, `thinking`, `token`, `done`, `error` as SSE. |

`POST /api/chat` takes `{messages, model, think, temperature}`. The system prompt
is prepended server-side, so the client never sends it. Every turn replays the
full message list — there is no session state on the server.

Models without a thinking mode reject Ollama's `think` field outright, so the
client drops it and retries once when that is the only thing that failed.

## The event store

`store.py` keeps sessions, planning events and export manifests in SQLite.
Events are written once and never edited; an `UPDATE` on the events table is
refused by a trigger. A generation stores the exact context and evidence it was
given, so `store.replay(generation_id, owner_id)` rebuilds a past response's
inputs without consulting a corpus that has since changed. That replay is what
makes an SFT row or a DPO pair reproducible.

Four consents are recorded separately per session: classroom use, research
analysis, model training and public sharing. `training_exclusions()` checks both
the inputs and the output, so an asset cleared for classroom use is still
excluded from a training export unless it is cleared for that too.

Every read is scoped by `owner_id`, and a session belonging to someone else
returns nothing rather than an error. Splits are assigned per family, above the
level of a single session, so a paraphrase cannot land on the other side of one.

`redact_session()` is the only path that deletes an event. It exists because a
contributor may withdraw, and it leaves a tombstone in `redactions` recording
that it happened. It removes material from future exports and from retrieval; it
cannot remove what a model has already learnt from it.

Run the tests with:

```bash
.venv/Scripts/python.exe tests/test_store.py
```

## Notes

- Fonts come from Google Fonts and KaTeX from jsDelivr, so first paint needs a
  network connection. Both degrade to system fonts and raw LaTeX offline.
- The narrow-viewport layout is written but has not been checked in a browser.
