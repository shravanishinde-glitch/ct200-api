import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app import models  # noqa: F401
from app.llm import get_llm
from app.main import app
from app.store import JsonFileStore, get_store

V1 = "data/ct200_manual.pdf"
V2 = "data/ct200_manual_v2.pdf"


class FakeLLM:
    """Cites whichever node ids appear in the prompt, so it behaves like a well-behaved model."""
    def __init__(self):
        self.calls = 0
        self.mode = "good"

    def __call__(self, messages):
        self.calls += 1
        if self.mode == "garbage":
            return "I cannot help with that."
        import re
        ids = [int(x) for x in re.findall(r"\[NODE (\d+)\]", messages[1]["content"])]
        cases = [{"title": f"Case {i}", "priority": "high", "preconditions": [], "steps": ["step"],
                  "expected_result": "expected", "source_node_ids": ids} for i in range(3)]
        return json.dumps({"test_cases": cases})


@pytest.fixture()
def env(tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    store, llm = JsonFileStore(str(tmp_path / "gen.json")), FakeLLM()

    def override_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_llm] = lambda: llm
    yield TestClient(app), llm
    app.dependency_overrides.clear()


def upload(c, path):
    with open(path, "rb") as f:
        return c.post("/documents/ct200/ingest", files={"file": ("m.pdf", f, "application/pdf")})


def nid(c, number, version):
    return c.get("/documents/ct200/nodes", params={"number": number, "version": version}).json()["nodes"][0]["id"]


def make_selection(c, numbers, version=1):
    ids = [nid(c, n, version) for n in numbers]
    return c.post("/selections", json={"name": "s", "node_ids": ids}).json()["id"], ids


def test_generate_stores_and_retrieves_fresh_before_reingest(env):
    c, llm = env
    upload(c, V1)
    sid, _ = make_selection(c, ["4.2"])
    r = c.post(f"/selections/{sid}/generate")
    assert r.status_code == 201 and len(r.json()["test_cases"]) == 3 and r.json()["overall_staleness"] == "fresh"
    got = c.get(f"/selections/{sid}/test-cases").json()["generations"]
    assert len(got) == 1 and got[0]["test_cases"][0]["staleness"]["status"] == "fresh"


def test_same_selection_twice_reuses_unless_forced(env):
    c, llm = env
    upload(c, V1)
    sid, _ = make_selection(c, ["4.2"])
    first = c.post(f"/selections/{sid}/generate").json()
    again = c.post(f"/selections/{sid}/generate")
    assert again.status_code == 200 and again.json()["reused"] and again.json()["generation_id"] == first["generation_id"]
    assert llm.calls == 1
    forced = c.post(f"/selections/{sid}/generate", params={"force": True})
    assert forced.status_code == 201 and forced.json()["generation_id"] != first["generation_id"] and llm.calls == 2
    assert len(c.get(f"/selections/{sid}/test-cases").json()["generations"]) == 2     # history kept


def test_garbage_llm_output_gives_502_saves_nothing_usable_and_is_not_reused(env):
    c, llm = env
    upload(c, V1)
    sid, _ = make_selection(c, ["4.2"])
    llm.mode = "garbage"
    r = c.post(f"/selections/{sid}/generate")
    assert r.status_code == 502 and len(r.json()["detail"]["attempts"]) == 3
    assert c.get(f"/selections/{sid}/test-cases").json()["generations"] == []          # failed runs are hidden by default
    assert len(c.get(f"/selections/{sid}/test-cases", params={"include_failed": True}).json()["generations"]) == 1
    llm.mode = "good"
    assert c.post(f"/selections/{sid}/generate").status_code == 201                    # a failure never blocks a retry


def test_staleness_flips_after_v2_and_only_for_changed_sections(env):
    c, _ = env
    upload(c, V1)
    sid, _ = make_selection(c, ["4.2", "1.1"])         # 4.2 changes in v2, 1.1 does not
    gid = c.post(f"/selections/{sid}/generate").json()["generation_id"]
    assert upload(c, V2).json()["version_no"] == 2
    g = c.get(f"/generations/{gid}").json()
    by_num = {s["number"]: s["current"] for s in g["sources"]}
    assert by_num["1.1"]["status"] == "fresh"
    assert by_num["4.2"]["status"] == "stale" and by_num["4.2"]["latest_version"] == 2
    assert g["overall_staleness"] == "stale" and g["test_cases"][0]["staleness"]["status"] == "stale"
    assert g["sources"][0]["version_no"] == 1          # the generation is still anchored to the v1 text it was made from


def test_v2_node_id_finds_test_cases_generated_from_v1(env):
    c, _ = env
    upload(c, V1)
    sid, _ = make_selection(c, ["4.2"])
    c.post(f"/selections/{sid}/generate")
    upload(c, V2)
    res = c.get(f"/nodes/{nid(c, '4.2', 2)}/test-cases").json()
    assert len(res["generations"]) == 1
    assert res["generations"][0]["test_cases"][0]["staleness"]["status"] == "stale"
    assert c.get(f"/nodes/{nid(c, '1.1', 2)}/test-cases").json()["generations"] == []


def test_unknown_selection_is_404(env):
    c, _ = env
    assert c.post("/selections/999/generate").status_code == 404
