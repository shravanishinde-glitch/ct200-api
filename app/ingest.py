import json
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import Document, DocumentVersion, Node
from app.parser.pdf_parser import parse_pdf
from app.versioning import match_versions


def ingest_pdf(db: Session, doc_name: str, pdf_path: str, source_name: str = "") -> DocumentVersion:
    """Parse a PDF and store it as a NEW version of `doc_name`. Earlier versions are never modified or deleted."""
    parsed = parse_pdf(pdf_path)

    doc = db.scalar(select(Document).where(Document.name == doc_name))
    if doc is None:
        doc = Document(name=doc_name)
        db.add(doc)
        db.flush()

    prev = db.scalars(select(DocumentVersion).where(DocumentVersion.document_id == doc.id)
                      .order_by(DocumentVersion.version_no.desc())).first()
    version = DocumentVersion(document_id=doc.id, version_no=(prev.version_no + 1) if prev else 1,
                              title=parsed["title"], source_file=source_name or pdf_path,
                              warnings_json=json.dumps(parsed["warnings"]))
    db.add(version)
    db.flush()

    new_nodes = parsed["nodes"]
    old_rows, old_dicts = [], []
    if prev:
        old_rows = list(db.scalars(select(Node).where(Node.version_id == prev.id).order_by(Node.order_index)))
        by_id = {n.id: n for n in old_rows}
        old_dicts = [{"logical_id": n.logical_id, "number": n.number, "heading": n.heading,
                      "parent_logical_id": by_id[n.parent_id].logical_id if n.parent_id else None,
                      "content_hash": n.content_hash} for n in old_rows]

    match = match_versions(old_dicts, new_nodes)["results"]

    by_number = {}
    for n, m in zip(new_nodes, match):
        parent = by_number.get(n["parent_number"]) if n["parent_number"] else None
        row = Node(version_id=version.id, logical_id=m["logical_id"], number=n["number"], heading=n["heading"],
                   level=n["level"], order_index=n["order_index"], parent_id=parent.id if parent else None,
                   body=n["body"], tables_json=json.dumps(n["tables"]), content_hash=n["content_hash"],
                   change_status=m["status"], prev_node_id=old_rows[m["old_index"]].id if m["old_index"] is not None else None,
                   warnings_json=json.dumps(n["warnings"]), page=n["page"])
        db.add(row)
        db.flush()                      # need row.id so children can point at it
        by_number[n["number"]] = row

    db.commit()
    return version