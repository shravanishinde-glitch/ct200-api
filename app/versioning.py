"""Version matching and diffing. Pure Python (no DB) so it can be unit-tested on its own.

Strategy (in order):
  A. Same section number AND similar heading (>= 0.6)  -> same logical node.
  B. Still unmatched: same parent logical node AND very similar heading (>= 0.85)
     -> same logical node (handles renumbering such as 5.3 -> 5.4).
  C. Otherwise the node is NEW in this version. Old nodes nobody claimed are REMOVED.
Same logical node + same content hash = unchanged; different hash = changed.
"""
import difflib
import re
from collections import Counter

NUMBER_HEADING_MIN = 0.6
FALLBACK_HEADING_MIN = 0.85
NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def heading_sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _new_logical_id(number: str, taken: set) -> str:
    base = f"L-{number}"
    lid, i = base, 1
    while lid in taken:
        i += 1
        lid = f"{base}-{i}"
    return lid


def match_versions(old_nodes: list, new_nodes: list) -> dict:
    """old_nodes: [{logical_id, number, heading, parent_logical_id, content_hash}]
    new_nodes: [{number, heading, parent_number, content_hash}] in document order (parents first).
    Returns {"results": [{logical_id, status, old_index}] aligned with new_nodes, "removed": [old indexes]}."""
    old_by_number = {o["number"]: i for i, o in enumerate(old_nodes)}
    used_old = set()
    results = [None] * len(new_nodes)

    # Pass A: number + heading
    for i, n in enumerate(new_nodes):
        j = old_by_number.get(n["number"])
        if j is not None and j not in used_old and heading_sim(old_nodes[j]["heading"], n["heading"]) >= NUMBER_HEADING_MIN:
            results[i] = {"old_index": j, "logical_id": old_nodes[j]["logical_id"], "via": "number"}
            used_old.add(j)

    taken = {o["logical_id"] for o in old_nodes}
    num_to_logical = {}
    # Pass B/C in document order so parents get their logical id before their children
    for i, n in enumerate(new_nodes):
        if results[i] is None:
            parent_logical = num_to_logical.get(n["parent_number"]) if n["parent_number"] else None
            best, best_sim = None, 0.0
            for j, o in enumerate(old_nodes):
                if j in used_old or o["parent_logical_id"] != parent_logical:
                    continue
                s = heading_sim(o["heading"], n["heading"])
                if s >= FALLBACK_HEADING_MIN and s > best_sim:
                    best, best_sim = j, s
            if best is not None:
                results[i] = {"old_index": best, "logical_id": old_nodes[best]["logical_id"], "via": "heading"}
                used_old.add(best)
            else:
                lid = _new_logical_id(n["number"], taken)
                taken.add(lid)
                results[i] = {"old_index": None, "logical_id": lid, "via": "new"}
        r = results[i]
        num_to_logical[n["number"]] = r["logical_id"]
        if r["old_index"] is None:
            r["status"] = "new"
        else:
            same = old_nodes[r["old_index"]]["content_hash"] == n["content_hash"]
            r["status"] = "unchanged" if same else "changed"

    removed = [j for j in range(len(old_nodes)) if j not in used_old]
    return {"results": results, "removed": removed}


def node_text(body: str, tables: list) -> str:
    """Body plus table cells flattened to words, used for diffing."""
    parts = [body or ""]
    for t in tables or []:
        for row in t:
            parts.append(" | ".join(" ".join(str(c or "").split()) for c in row))
    return " ".join(p for p in parts if p).strip()


def diff_summary(old_text: str, new_text: str) -> dict:
    a, b = old_text.split(), new_text.split()
    added, removed = [], []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if op in ("replace", "delete"):
            removed.append(" ".join(a[i1:i2]))
        if op in ("replace", "insert"):
            added.append(" ".join(b[j1:j2]))
    old_nums, new_nums = Counter(NUM_RE.findall(old_text)), Counter(NUM_RE.findall(new_text))
    numeric = old_nums != new_nums
    return {
        "added": added, "removed": removed,
        "numeric_change": numeric,
        "numbers_removed": sorted((old_nums - new_nums).elements()),
        "numbers_added": sorted((new_nums - old_nums).elements()),
        "severity": "high" if numeric else "low",
    }