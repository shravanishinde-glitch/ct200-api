"""Shared lookups used by the API (and later by staleness checks)."""
import json
from typing import Optional
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import Document, DocumentVersion, Node


def get_document(db: Session, name: str) -> Document:
    doc = db.scalar(select(Document).where(Document.name == name))
    if doc is None:
        raise HTTPException(404, f"Document '{name}' not found")
    return doc


def resolve_version(db: Session, doc: Document, version_no: Optional[int] = None) -> DocumentVersion:
    q = select(DocumentVersion).where(DocumentVersion.document_id == doc.id)
    q = q.where(DocumentVersion.version_no == version_no) if version_no is not None else q.order_by(DocumentVersion.version_no.desc())
    v = db.scalars(q).first()
    if v is None:
        raise HTTPException(404, f"Version {version_no} of '{doc.name}' not found" if version_no is not None
                            else f"'{doc.name}' has no versions")
    return v


def get_node(db: Session, node_id: int) -> Node:
    n = db.get(Node, node_id)
    if n is None:
        raise HTTPException(404, f"Node {node_id} not found")
    return n


def summary(n: Node) -> dict:
    return {"id": n.id, "logical_id": n.logical_id, "number": n.number, "heading": n.heading, "level": n.level,
            "parent_id": n.parent_id, "version_no": n.version.version_no, "change_status": n.change_status,
            "content_hash": n.content_hash}


def detail(db: Session, n: Node) -> dict:
    children = db.scalars(select(Node).where(Node.parent_id == n.id).order_by(Node.order_index)).all()
    path, cur = [], n
    while cur is not None:
        path.append({"id": cur.id, "number": cur.number, "heading": cur.heading})
        cur = db.get(Node, cur.parent_id) if cur.parent_id else None
    out = summary(n)
    out.update(document=n.version.document.name, page=n.page, body=n.body, tables=json.loads(n.tables_json),
               warnings=json.loads(n.warnings_json), prev_node_id=n.prev_node_id, path=list(reversed(path)),
               children=[summary(c) for c in children])
    return out


def latest_counterpart(db: Session, n: Node) -> Optional[Node]:
    """The same logical section in the newest version of the same document (None if it was removed)."""
    latest = db.scalars(select(DocumentVersion).where(DocumentVersion.document_id == n.version.document_id)
                        .order_by(DocumentVersion.version_no.desc())).first()
    if latest.id == n.version_id:
        return n
    return db.scalar(select(Node).where(Node.version_id == latest.id, Node.logical_id == n.logical_id))


def source_status(db: Session, n: Node) -> dict:
    """fresh = unchanged in the latest version, stale = text changed, removed = section no longer exists."""
    cur = latest_counterpart(db, n)
    if cur is None:
        return {"status": "removed", "latest_node_id": None}
    return {"status": "fresh" if cur.content_hash == n.content_hash else "stale", "latest_node_id": cur.id}