import json
import os
import tempfile
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db, init_db
from app.ingest import ingest_pdf
from app.models import DocumentVersion, Node, Selection, SelectionItem
from app.queries import detail, get_document, get_node, resolve_version, source_status, summary
from app.versioning import diff_summary, node_text


@asynccontextmanager
async def lifespan(_app):
    init_db()
    yield


app = FastAPI(title="CT-200 Manual API", lifespan=lifespan)


def _text(n: Node) -> str:
    return node_text(n.body, json.loads(n.tables_json))


@app.get("/health")
def health():
    return {"status": "ok"}


# ---------- ingestion & versions ----------
@app.post("/documents/{name}/ingest")
def ingest(name: str, file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Upload a PDF. It becomes the next version of document `name`; earlier versions are never modified."""
    data = file.file.read()
    if not data.startswith(b"%PDF"):
        raise HTTPException(400, "Uploaded file is not a PDF")
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    try:
        tmp.write(data)
        tmp.close()
        try:
            version = ingest_pdf(db, name, tmp.name, source_name=file.filename or "")
        except Exception as exc:                      # parse failure or nothing found: nothing was stored
            db.rollback()
            raise HTTPException(422, f"Could not ingest PDF: {exc}")
    finally:
        os.unlink(tmp.name)
    count = len(db.scalars(select(Node).where(Node.version_id == version.id)).all())
    return {"document": name, "version_no": version.version_no, "version_id": version.id, "node_count": count,
            "parser_warnings": json.loads(version.warnings_json)}


@app.get("/documents/{name}/versions")
def list_versions(name: str, db: Session = Depends(get_db)):
    doc = get_document(db, name)
    return [{"version_no": v.version_no, "version_id": v.id, "source_file": v.source_file,
             "created_at": v.created_at.isoformat(), "parser_warnings": json.loads(v.warnings_json)}
            for v in doc.versions]


@app.get("/documents/{name}/versions/{version_no}/changes")
def version_changes(name: str, version_no: int, db: Session = Depends(get_db)):
    """What changed in `version_no` compared with the version right before it."""
    doc = get_document(db, name)
    v = resolve_version(db, doc, version_no)
    if version_no == 1:
        return {"version_no": 1, "compared_to": None, "changed": [], "new": [], "removed": [],
                "note": "First version: nothing to compare against."}
    prev = resolve_version(db, doc, version_no - 1)
    nodes = db.scalars(select(Node).where(Node.version_id == v.id).order_by(Node.order_index)).all()
    changed = []
    for n in nodes:
        if n.change_status == "changed":
            p = db.get(Node, n.prev_node_id)
            changed.append({**summary(n), "diff": diff_summary(_text(p), _text(n))})
    referenced = {n.prev_node_id for n in nodes if n.prev_node_id}
    prev_nodes = db.scalars(select(Node).where(Node.version_id == prev.id).order_by(Node.order_index)).all()
    return {"version_no": version_no, "compared_to": prev.version_no, "changed": changed,
            "new": [summary(n) for n in nodes if n.change_status == "new"],
            "removed": [summary(p) for p in prev_nodes if p.id not in referenced]}


# ---------- browsing ----------
@app.get("/documents/{name}/sections")
def top_level_sections(name: str, version: Optional[int] = Query(None, ge=1), db: Session = Depends(get_db)):
    """Top-level sections of the latest version (or the version given)."""
    v = resolve_version(db, get_document(db, name), version)
    rows = db.scalars(select(Node).where(Node.version_id == v.id, Node.parent_id.is_(None)).order_by(Node.order_index)).all()
    return {"version_no": v.version_no, "sections": [summary(n) for n in rows]}


@app.get("/documents/{name}/nodes")
def list_nodes(name: str, version: Optional[int] = Query(None, ge=1), number: Optional[str] = None,
               db: Session = Depends(get_db)):
    """Every section of a version in document order; optionally filter by section number (e.g. 4.2)."""
    v = resolve_version(db, get_document(db, name), version)
    q = select(Node).where(Node.version_id == v.id).order_by(Node.order_index)
    if number:
        q = q.where(Node.number == number)
    return {"version_no": v.version_no, "nodes": [summary(n) for n in db.scalars(q).all()]}


@app.get("/documents/{name}/search")
def search(name: str, q: str = Query(..., min_length=2), version: Optional[int] = Query(None, ge=1),
           db: Session = Depends(get_db)):
    """Case-insensitive search in headings and text (including table cells)."""
    v = resolve_version(db, get_document(db, name), version)
    needle = q.lower()
    hits = []
    for n in db.scalars(select(Node).where(Node.version_id == v.id).order_by(Node.order_index)):
        text = _text(n)
        in_heading, pos = needle in n.heading.lower(), text.lower().find(needle)
        if in_heading or pos >= 0:
            snippet = text[max(0, pos - 40): pos + len(q) + 60] if pos >= 0 else ""
            hits.append({**summary(n), "matched_in": "heading" if in_heading else "text", "snippet": snippet})
    return {"version_no": v.version_no, "query": q, "count": len(hits), "results": hits}


@app.get("/nodes/{node_id}")
def get_node_detail(node_id: int, db: Session = Depends(get_db)):
    return detail(db, get_node(db, node_id))


@app.get("/nodes/{node_id}/changes")
def node_changes(node_id: int, db: Session = Depends(get_db)):
    """Change status vs the previous version, a diff when changed, and this section's history across versions."""
    n = get_node(db, node_id)
    diff = None
    if n.prev_node_id and n.change_status == "changed":
        diff = diff_summary(_text(db.get(Node, n.prev_node_id)), _text(n))
    rows = db.execute(select(Node, DocumentVersion.version_no)
                      .join(DocumentVersion, Node.version_id == DocumentVersion.id)
                      .where(DocumentVersion.document_id == n.version.document_id, Node.logical_id == n.logical_id)
                      .order_by(DocumentVersion.version_no)).all()
    return {"node_id": n.id, "number": n.number, "version_no": n.version.version_no, "change_status": n.change_status,
            "previous_node_id": n.prev_node_id, "diff": diff,
            "history": [{"version_no": vn, "node_id": row.id, "number": row.number, "content_hash": row.content_hash,
                         "change_status": row.change_status} for row, vn in rows],
            "latest": source_status(db, n)}


# ---------- selections ----------
class SelectionIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    node_ids: list[int] = Field(min_length=1, max_length=200)


def _selection_out(db: Session, sel: Selection) -> dict:
    return {"id": sel.id, "name": sel.name, "created_at": sel.created_at.isoformat(),
            "items": [{**summary(i.node), "body": i.node.body, "tables": json.loads(i.node.tables_json),
                       "latest": source_status(db, i.node)} for i in sel.items]}


@app.post("/selections", status_code=201)
def create_selection(body: SelectionIn, db: Session = Depends(get_db)):
    """Pick sections. The selection is pinned to those exact section rows (and so to their versions)."""
    ids = list(dict.fromkeys(body.node_ids))
    found = {n.id for n in db.scalars(select(Node).where(Node.id.in_(ids)))}
    missing = [i for i in ids if i not in found]
    if missing:
        raise HTTPException(404, f"Node ids not found: {missing}")
    sel = Selection(name=body.name, items=[SelectionItem(node_id=i) for i in ids])
    db.add(sel)
    db.commit()
    return _selection_out(db, sel)


@app.get("/selections/{selection_id}")
def get_selection(selection_id: int, db: Session = Depends(get_db)):
    sel = db.get(Selection, selection_id)
    if sel is None:
        raise HTTPException(404, f"Selection {selection_id} not found")
    return _selection_out(db, sel)