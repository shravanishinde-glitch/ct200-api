"""Selection -> generate -> retrieve (with live staleness)."""
import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.llm import MAX_INPUT_CHARS, PROMPT_VERSION, GenerationFailed, LLMConfigError, get_llm, run_generation
from app.models import Selection
from app.queries import get_node
from app.staleness import assess_generation
from app.store import get_store
from app.versioning import node_text

router = APIRouter()


def _model_name() -> str:
    return os.getenv("LLM_MODEL", "openai/gpt-oss-120b")


def _snapshot(n) -> dict:
    return {"node_id": n.id, "logical_id": n.logical_id, "number": n.number, "heading": n.heading,
            "version_no": n.version.version_no, "content_hash": n.content_hash,
            "text": node_text(n.body, json.loads(n.tables_json))}


def _idem_key(sources: list) -> str:
    """Same exact section content (+ prompt + model) -> same key. Order of selection does not matter."""
    raw = "|".join(sorted(f"{s['node_id']}:{s['content_hash']}" for s in sources)) + f"|{PROMPT_VERSION}|{_model_name()}"
    return hashlib.sha256(raw.encode()).hexdigest()


@router.post("/selections/{selection_id}/generate")
def generate(selection_id: int, response: Response, force: bool = Query(False), db: Session = Depends(get_db),
             store=Depends(get_store), llm: Callable = Depends(get_llm)):
    """Generate 3-5 QA test cases for a selection.

    Duplicate policy: submitting the same selection again returns the EXISTING successful generation (200,
    reused=true) instead of paying for another LLM call and silently producing a different answer. ?force=true
    creates a NEW generation next to the old one (history is kept, nothing is overwritten)."""
    sel = db.get(Selection, selection_id)
    if sel is None:
        raise HTTPException(404, f"Selection {selection_id} not found")
    sources = [_snapshot(i.node) for i in sel.items]
    if sum(len(s["text"]) for s in sources) > MAX_INPUT_CHARS:
        raise HTTPException(413, f"Selection text exceeds {MAX_INPUT_CHARS} characters; select fewer sections.")
    key = _idem_key(sources)
    if not force:
        existing = [g for g in store.find({"selection_id": selection_id, "idempotency_key": key}) if g["status"] == "success"]
        if existing:
            response.status_code = 200
            return {**assess_generation(db, existing[0]), "reused": True}

    base = {"selection_id": selection_id, "selection_name": sel.name, "idempotency_key": key,
            "prompt_version": PROMPT_VERSION, "model": _model_name(), "sources": sources,
            "node_ids": [s["node_id"] for s in sources], "logical_ids": sorted({s["logical_id"] for s in sources}),
            "created_at": datetime.now(timezone.utc).isoformat()}
    try:
        cases, attempts, warnings, raw = run_generation(llm, sources)
    except LLMConfigError as e:
        raise HTTPException(503, f"LLM is not configured: {e}")
    except GenerationFailed as e:
        gid = store.insert({**base, "status": "failed", "test_cases": [], "attempts": e.attempts,
                            "warnings": [], "raw_output": e.last_raw})
        raise HTTPException(502, {"error": "LLM did not return valid test cases; nothing was saved as a result.",
                                  "generation_id": gid, "attempts": e.attempts})
    gid = store.insert({**base, "status": "success", "test_cases": cases, "attempts": attempts,
                        "warnings": warnings, "raw_output": raw})
    response.status_code = 201
    return {**assess_generation(db, store.get(gid)), "reused": False}


@router.get("/selections/{selection_id}/test-cases")
def test_cases_for_selection(selection_id: int, include_failed: bool = False, db: Session = Depends(get_db),
                             store=Depends(get_store)):
    if db.get(Selection, selection_id) is None:
        raise HTTPException(404, f"Selection {selection_id} not found")
    gens = store.find({"selection_id": selection_id})
    if not include_failed:
        gens = [g for g in gens if g["status"] == "success"]
    return {"selection_id": selection_id, "generations": [assess_generation(db, g) for g in gens]}


@router.get("/generations/{generation_id}")
def get_generation(generation_id: str, db: Session = Depends(get_db), store=Depends(get_store)):
    g = store.get(generation_id)
    if g is None:
        raise HTTPException(404, f"Generation {generation_id} not found")
    return {**assess_generation(db, g), "raw_output": g.get("raw_output")}


@router.get("/nodes/{node_id}/test-cases")
def test_cases_for_node(node_id: int, db: Session = Depends(get_db), store=Depends(get_store)):
    """Test cases derived from this section. Matches on the LOGICAL section, so a node id from v2 also finds
    test cases generated from v1 of that section (that is exactly the impact question after a re-ingest)."""
    n = get_node(db, node_id)
    doc = n.version.document.name
    out = []
    for g in store.find({"logical_ids": n.logical_id, "status": "success"}):
        src = next((s for s in g["sources"] if s["logical_id"] == n.logical_id), None)
        if src is None or get_node(db, src["node_id"]).version.document.name != doc:
            continue                                        # same logical id in a different document
        view = assess_generation(db, g, only_logical_id=n.logical_id)
        if view["test_cases"]:
            out.append(view)
    return {"node_id": node_id, "logical_id": n.logical_id, "generations": out}
