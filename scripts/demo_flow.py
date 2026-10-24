"""End-to-end demo: ingest v1 -> select -> generate -> ingest v2 -> see staleness.

Start the API first:   uvicorn app.main:app --reload
Then:                  python scripts/demo_flow.py [http://127.0.0.1:8000]
Needs GROQ_API_KEY set in the SERVER's environment (.env). Use a fresh DB (delete ct200.db) for a clean run.
"""
import json
import sys
import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
c = httpx.Client(base_url=BASE, timeout=120)


def show(title, obj):
    print(f"\n=== {title}\n{json.dumps(obj, indent=2)[:1800]}")


def ingest(path):
    with open(path, "rb") as f:
        r = c.post("/documents/ct200/ingest", files={"file": (path, f, "application/pdf")})
    r.raise_for_status()
    return r.json()


def node(number, version):
    return c.get("/documents/ct200/nodes", params={"number": number, "version": version}).json()["nodes"][0]["id"]


r1 = ingest("data/ct200_manual.pdf")
show("1. Ingested v1", {k: r1[k] for k in ("version_no", "node_count")} | {"parser_warnings": r1["parser_warnings"]})

ids = [node("4.2", 1), node("1.1", 1)]            # 4.2 changes in v2, 1.1 does not
sel = c.post("/selections", json={"name": "alarms + intended use", "node_ids": ids}).json()
show("2. Selection (pinned to v1 rows)", {"id": sel["id"], "items": [(i["number"], i["version_no"]) for i in sel["items"]]})

gen = c.post(f"/selections/{sel['id']}/generate")
show(f"3. Generate -> HTTP {gen.status_code}", {"overall_staleness": gen.json().get("overall_staleness"),
     "titles": [t["title"] for t in gen.json().get("test_cases", [])], "detail": gen.json().get("detail")})
gen.raise_for_status()
again = c.post(f"/selections/{sel['id']}/generate").json()
print(f"\n=== 4. Same selection again -> reused={again['reused']} (no second LLM call)")

r2 = ingest("data/ct200_manual_v2.pdf")
show("5. Ingested v2 (v1 untouched)", {"version_no": r2["version_no"], "node_count": r2["node_count"]})
show("6. What changed v1 -> v2", {k: [x["number"] for x in v] for k, v in c.get("/documents/ct200/versions/2/changes").json().items() if isinstance(v, list)})

g = c.get(f"/selections/{sel['id']}/test-cases").json()["generations"][0]
show("7. SAME generation, now with live staleness", {
    "overall": g["overall_staleness"],
    "per_test_case": [(t["id"], t["staleness"]["status"]) for t in g["test_cases"]],
    "per_source": [(s["number"], s["current"]["status"], s["current"]["reason"]) for s in g["sources"]]})

n42_v2 = node("4.2", 2)
res = c.get(f"/nodes/{n42_v2}/test-cases").json()
show("8. Impact lookup by the v2 node id of 4.2", {"generations_affected": len(res["generations"]),
     "statuses": [t["staleness"]["status"] for x in res["generations"] for t in x["test_cases"]]})
show("9. Diff for that node", c.get(f"/nodes/{n42_v2}/changes").json()["diff"])
