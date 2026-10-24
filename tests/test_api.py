import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app import models  # noqa: F401
from app.main import app

V1 = "data/ct200_manual.pdf"
V2 = "data/ct200_manual_v2.pdf"


@pytest.fixture()
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    def override():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


def upload(client, path, name="ct200"):
    with open(path, "rb") as f:
        return client.post(f"/documents/{name}/ingest", files={"file": ("manual.pdf", f, "application/pdf")})


def node_id(client, number, version=None):
    params = {"number": number, **({"version": version} if version else {})}
    return client.get("/documents/ct200/nodes", params=params).json()["nodes"][0]["id"]


def test_ingest_v1_and_browse_top_level(client):
    r = upload(client, V1)
    assert r.status_code == 200 and r.json()["version_no"] == 1 and r.json()["node_count"] == 27
    top = client.get("/documents/ct200/sections").json()["sections"]
    assert [s["number"] for s in top] == ["1", "2", "3", "4", "5", "6", "7", "8"]


def test_non_pdf_is_rejected_and_nothing_stored(client):
    r = client.post("/documents/ct200/ingest", files={"file": ("x.pdf", b"not a pdf", "application/pdf")})
    assert r.status_code == 400
    assert client.get("/documents/ct200/versions").status_code == 404


def test_search_finds_both_error_codes_sections(client):
    upload(client, V1)
    hits = client.get("/documents/ct200/search", params={"q": "error codes"}).json()["results"]
    assert {h["number"] for h in hits if h["heading"] == "Error Codes"} == {"4.2", "7.1"}


def test_node_detail_has_table_children_and_path(client):
    upload(client, V1)
    d = client.get(f"/nodes/{node_id(client, '4.2')}").json()
    assert d["tables"][0][0][0] == "Code" and [p["number"] for p in d["path"]] == ["4", "4.2"]
    parent = client.get(f"/nodes/{node_id(client, '4')}").json()
    assert "4.2" in [c["number"] for c in parent["children"]]


def test_version_changes_report(client):
    upload(client, V1)
    assert upload(client, V2).json()["version_no"] == 2
    ch = client.get("/documents/ct200/versions/2/changes").json()
    assert {c["number"] for c in ch["changed"]} == {"2.1.1.1", "3.2", "4.2", "4.3"}
    assert {c["number"] for c in ch["new"]} == {"5.3"} and ch["removed"] == []
    assert all(c["diff"]["severity"] == "high" for c in ch["changed"])


def test_selection_stays_pinned_to_v1_after_v2_arrives(client):
    upload(client, V1)
    v1_node = node_id(client, "4.2", version=1)
    sel = client.post("/selections", json={"name": "alarms", "node_ids": [v1_node]}).json()
    original_body = sel["items"][0]["body"]
    upload(client, V2)
    again = client.get(f"/selections/{sel['id']}").json()["items"][0]
    assert again["version_no"] == 1 and again["body"] == original_body
    assert again["latest"]["status"] == "stale"


def test_selection_with_unknown_node_is_404(client):
    upload(client, V1)
    assert client.post("/selections", json={"name": "x", "node_ids": [999999]}).status_code == 404