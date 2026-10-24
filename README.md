# CT-200 Manual API

FastAPI service that turns the CardioTrack CT-200 manual (PDF) into a **versioned section tree**, lets you
**select sections**, generates **QA test-case ideas with an LLM**, and tells you at retrieval time whether those
test cases are still **in sync with the current manual** (staleness / impact detection).

Design decisions, edge cases and known weaknesses: see [APPROACH.md](APPROACH.md).

## Stack
FastAPI + Pydantic, SQLAlchemy + SQLite (tree, versions, selections), MongoDB **or** a JSON-file store (LLM output),
pdfplumber (PDF parsing), Groq (LLM, any provider can be swapped in `app/llm.py::groq_complete`).

## Setup
```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                    # then put your GROQ_API_KEY in it
uvicorn app.main:app --reload                           # http://127.0.0.1:8000/docs
```

| Env var | Default | Meaning |
|---|---|---|
| `GROQ_API_KEY` | – | Needed only for `/generate`. Without it, generate returns 503 (everything else works). |
| `LLM_MODEL` | `openai/gpt-oss-120b` | Groq model name. |
| `MONGO_URI` | empty | If set, generations go to MongoDB (db `ct200`, collection `generations`). If empty, the JSON-file store is used. |
| `GEN_STORE_PATH` | `./generations.json` | Location of the JSON-file store. |
| `SQLITE_URL` | `sqlite:///./ct200.db` | Relational DB. |

## Tests
```bash
pytest -q
```
No network, API key or MongoDB needed: the LLM is replaced by a scripted fake and generations go to a temp JSON file.
Parser irregularity tests: `tests/test_parser.py` (duplicate headings, skipped level, out-of-order sections,
body-sized bold headings, cross-reference lines, tables, ligatures). Versioning: `tests/test_versioning.py`,
`tests/test_ingest.py`. LLM failure handling: `tests/test_llm.py`. Generation + staleness over the API:
`tests/test_generation_api.py`, `tests/test_staleness.py`.

## The v1 -> v2 re-ingestion flow (curl)
```bash
# 1. ingest v1 (creates document "ct200", version 1)
curl -F file=@data/ct200_manual.pdf http://127.0.0.1:8000/documents/ct200/ingest

# 2. find node ids and pick sections (4.2 changes in v2, 1.1 does not)
curl "http://127.0.0.1:8000/documents/ct200/nodes?number=4.2&version=1"
curl -X POST http://127.0.0.1:8000/selections -H 'content-type: application/json' \
     -d '{"name":"alarms","node_ids":[<id of 4.2>, <id of 1.1>]}'

# 3. generate test cases (same call twice = reuse; add ?force=true for a new generation)
curl -X POST http://127.0.0.1:8000/selections/1/generate

# 4. re-ingest the modified manual: SAME document name => becomes version 2, version 1 stays intact
curl -F file=@data/ct200_manual_v2.pdf http://127.0.0.1:8000/documents/ct200/ingest

# 5. what changed, and the staleness of the earlier generation
curl http://127.0.0.1:8000/documents/ct200/versions/2/changes
curl http://127.0.0.1:8000/selections/1/test-cases          # per-test-case + per-source staleness
curl http://127.0.0.1:8000/nodes/<v2 id of 4.2>/test-cases  # impact lookup from the NEW node
```
Or run all of it: `python scripts/demo_flow.py` (server running, fresh `ct200.db`).

## Endpoints
| Method & path | Purpose |
|---|---|
| `POST /documents/{name}/ingest` | Upload PDF -> next version of `{name}` (old versions never modified) |
| `GET /documents/{name}/versions` | Versions + parser warnings |
| `GET /documents/{name}/versions/{n}/changes` | changed / new / removed vs previous version, with diff summaries |
| `GET /documents/{name}/sections?version=` | Top-level sections (latest by default) |
| `GET /documents/{name}/nodes?version=&number=` | All nodes of a version (filter by section number) |
| `GET /documents/{name}/search?q=&version=` | Search headings, text and table cells |
| `GET /nodes/{id}` | Node with children, full text, tables, path, content hash |
| `GET /nodes/{id}/changes` | Changed across versions? diff summary, history, status in latest |
| `POST /selections` / `GET /selections/{id}` | Version-pinned selection (points at exact node rows) |
| `POST /selections/{id}/generate[?force=true]` | LLM test cases (201 new / 200 reused / 502 invalid LLM output / 503 no key) |
| `GET /selections/{id}/test-cases` | Generations for a selection, **with live staleness** |
| `GET /nodes/{id}/test-cases` | Test cases derived from a section (matches across versions via logical id) |
| `GET /generations/{gid}` | One generation incl. attempts and raw LLM output |

## Staleness values
`fresh` · `possibly_stale` (wording only) · `stale` (a number or a safety-relevant word changed) · `source_removed`.
A test case gets the worst status among the sections it cites. Details and limits in APPROACH.md.
