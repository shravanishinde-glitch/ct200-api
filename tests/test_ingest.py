import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from app.db import Base
from app import models
from app.models import Node, DocumentVersion
from app.ingest import ingest_pdf

V1 = "data/ct200_manual.pdf"
V2 = "data/ct200_manual_v2.pdf"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    yield s
    s.close()


def nodes_of(db, version):
    return list(db.scalars(select(Node).where(Node.version_id == version.id).order_by(Node.order_index)))


def test_ingest_v1_creates_tree_with_distinct_duplicate_headings(db):
    v1 = ingest_pdf(db, "ct200", V1)
    nodes = nodes_of(db, v1)
    assert v1.version_no == 1 and len(nodes) == 27
    errs = [n for n in nodes if n.heading == "Error Codes"]
    assert len(errs) == 2 and errs[0].id != errs[1].id
    assert {db.get(Node, e.parent_id).number for e in errs} == {"4", "7"}


def test_reingest_keeps_v1_and_flags_changes(db):
    v1 = ingest_pdf(db, "ct200", V1)
    v2 = ingest_pdf(db, "ct200", V2)
    assert v2.version_no == 2
    assert len(nodes_of(db, v1)) == 27            # version 1 untouched
    n2 = {n.number: n for n in nodes_of(db, v2)}
    n1 = {n.number: n for n in nodes_of(db, v1)}
    assert n2["4.2"].change_status == "changed" and n2["4.2"].logical_id == n1["4.2"].logical_id
    assert n2["4.2"].prev_node_id == n1["4.2"].id
    assert n2["5.3"].change_status == "new" and n2["5.3"].prev_node_id is None
    assert n2["1.1"].change_status == "unchanged"


def test_versions_are_not_duplicated_as_documents(db):
    ingest_pdf(db, "ct200", V1)
    ingest_pdf(db, "ct200", V2)
    assert len(list(db.scalars(select(DocumentVersion)))) == 2