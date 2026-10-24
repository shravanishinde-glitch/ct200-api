"""Staleness / impact detection for generated test cases. Pure classification + a DB lookup.

A generation stores, for every source section, a SNAPSHOT (node id, logical id, content hash, text).
At retrieval time each snapshot is compared with the same logical section in the LATEST version:

  fresh           - latest text hash == hash the test case was generated from
  possibly_stale  - text changed, but only plain wording changed (no numbers, no safety-relevant words)
  stale           - a number changed, or a safety-relevant word was added/removed (not, must, within, ...)
  source_removed  - the section no longer exists in the latest version

A test case takes the WORST status of the sections it cites. Nothing is ever auto-cleared: any non-fresh
state means "a human should re-read this", and `possibly_stale` just says it is probably cosmetic.

Known limits (also in APPROACH.md): the classifier is a heuristic over tokens, not semantics. A change such as
"before" -> "after", unit changes written as words ("two" -> "three"), or a reworded condition that flips
meaning without touching the keyword list is classified `possibly_stale` although it is really critical.
A section that was split or merged is seen as removed/new.
"""
import json
import re
from typing import Optional
from sqlalchemy.orm import Session

from app.models import Node
from app.queries import latest_counterpart
from app.versioning import diff_summary, node_text

SEVERITY_ORDER = {"fresh": 0, "possibly_stale": 1, "stale": 2, "source_removed": 3}
# words whose addition/removal can flip a requirement even though no number changed
CRITICAL_WORDS = {"not", "no", "never", "only", "must", "shall", "should", "may", "cannot", "unless", "except",
                  "always", "within", "before", "after", "least", "most", "minimum", "maximum", "exceeds",
                  "above", "below", "than", "disables", "disabled", "enabled", "silenced"}
_WORD = re.compile(r"[A-Za-z]+")


def classify_change(old_text: str, new_text: str) -> dict:
    """Return {'status', 'reason', 'diff'} for a snapshot text vs the current text of the same section."""
    d = diff_summary(old_text, new_text)
    if d["numeric_change"]:
        return {"status": "stale", "diff": d,
                "reason": f"numbers changed: removed {d['numbers_removed']}, added {d['numbers_added']}"}
    touched = {w.lower() for chunk in d["added"] + d["removed"] for w in _WORD.findall(chunk)}
    critical = sorted(touched & CRITICAL_WORDS)
    if critical:
        return {"status": "stale", "diff": d, "reason": f"safety-relevant wording changed: {critical}"}
    return {"status": "possibly_stale", "diff": d, "reason": "wording changed; no numbers or safety-relevant words differ"}


def assess_source(db: Session, src: dict) -> dict:
    """Staleness of ONE source snapshot against the latest version of the same logical section."""
    base = {"node_id": src["node_id"], "number": src["number"], "heading": src["heading"],
            "generated_from_version": src["version_no"]}
    row = db.get(Node, src["node_id"])
    cur = latest_counterpart(db, row) if row is not None else None
    if cur is None:
        return {**base, "status": "source_removed", "reason": "section no longer exists in the latest version",
                "latest_node_id": None, "latest_version": None}
    base.update(latest_node_id=cur.id, latest_version=cur.version.version_no)
    if cur.content_hash == src["content_hash"]:
        return {**base, "status": "fresh", "reason": "section text identical in the latest version"}
    res = classify_change(src["text"], node_text(cur.body, json.loads(cur.tables_json)))
    return {**base, "status": res["status"], "reason": res["reason"], "diff": res["diff"]}


def worst(statuses: list) -> str:
    return max(statuses, key=lambda s: SEVERITY_ORDER[s]) if statuses else "fresh"


def assess_generation(db: Session, gen: dict, only_logical_id: Optional[str] = None) -> dict:
    """Public view of a stored generation with live staleness attached to every test case."""
    by_node = {s["node_id"]: assess_source(db, s) for s in gen.get("sources", [])}
    logical_of = {s["node_id"]: s["logical_id"] for s in gen.get("sources", [])}
    cases = []
    for tc in gen.get("test_cases", []):
        if only_logical_id and only_logical_id not in {logical_of.get(i) for i in tc["source_node_ids"]}:
            continue
        srcs = [by_node[i] for i in tc["source_node_ids"] if i in by_node]
        cases.append({**tc, "staleness": {"status": worst([s["status"] for s in srcs]), "sources": srcs}})
    return {
        "generation_id": gen["_id"], "selection_id": gen["selection_id"], "selection_name": gen.get("selection_name"),
        "status": gen["status"], "created_at": gen["created_at"], "model": gen.get("model"),
        "prompt_version": gen.get("prompt_version"), "warnings": gen.get("warnings", []),
        "attempts": gen.get("attempts", []),
        "overall_staleness": worst([c["staleness"]["status"] for c in cases]) if cases else None,
        "sources": [{k: v for k, v in s.items() if k != "text"} | {"current": by_node[s["node_id"]]}
                    for s in gen.get("sources", [])],
        "test_cases": cases,
    }
