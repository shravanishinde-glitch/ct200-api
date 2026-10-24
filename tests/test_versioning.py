from app.parser.pdf_parser import parse_pdf
from app.versioning import match_versions, diff_summary, node_text

V1 = "data/ct200_manual.pdf"
V2 = "data/ct200_manual_v2.pdf"


def _as_old(nodes):
    num2l, old = {}, []
    for n in nodes:
        lid = f"L-{n['number']}"
        num2l[n["number"]] = lid
        old.append({"logical_id": lid, "number": n["number"], "heading": n["heading"],
                    "parent_logical_id": num2l.get(n["parent_number"]), "content_hash": n["content_hash"]})
    return old


def test_v1_to_v2_changed_and_new_nodes():
    v1, v2 = parse_pdf(V1)["nodes"], parse_pdf(V2)["nodes"]
    r = match_versions(_as_old(v1), v2)
    status = {n["number"]: x["status"] for n, x in zip(v2, r["results"])}
    assert {k for k, v in status.items() if v == "changed"} == {"2.1.1.1", "3.2", "4.2", "4.3"}
    assert {k for k, v in status.items() if v == "new"} == {"5.3"}
    assert r["removed"] == []


def test_page_break_shift_is_not_a_change():
    v1, v2 = parse_pdf(V1)["nodes"], parse_pdf(V2)["nodes"]
    r = match_versions(_as_old(v1), v2)
    status = {n["number"]: x["status"] for n, x in zip(v2, r["results"])}
    assert status["3.1"] == "unchanged" and status["6.1"] == "unchanged"


def test_duplicate_heading_matches_by_number_not_title():
    v1 = parse_pdf(V1)["nodes"]
    r = match_versions(_as_old(v1), v1)
    lids = {n["number"]: x["logical_id"] for n, x in zip(v1, r["results"])}
    assert lids["4.2"] != lids["7.1"]          # both are titled "Error Codes"


def test_renumbered_section_is_matched_by_heading_fallback():
    old = [{"logical_id": "L-1", "number": "1", "heading": "Overview", "parent_logical_id": None, "content_hash": "a"},
           {"logical_id": "L-1.1", "number": "1.1", "heading": "Intended Use", "parent_logical_id": "L-1", "content_hash": "b"}]
    new = [{"number": "1", "heading": "Overview", "parent_number": None, "content_hash": "a"},
           {"number": "1.2", "heading": "Intended Use", "parent_number": "1", "content_hash": "b"}]
    r = match_versions(old, new)["results"]
    assert r[1]["logical_id"] == "L-1.1" and r[1]["status"] == "unchanged" and r[1]["via"] == "heading"


def test_removed_section_is_reported():
    old = [{"logical_id": "L-1", "number": "1", "heading": "A", "parent_logical_id": None, "content_hash": "a"},
           {"logical_id": "L-2", "number": "2", "heading": "Gone", "parent_logical_id": None, "content_hash": "b"}]
    new = [{"number": "1", "heading": "A", "parent_number": None, "content_hash": "a"}]
    assert match_versions(old, new)["removed"] == [1]


def test_numeric_change_is_high_severity_wording_change_is_low():
    assert diff_summary("auto-deflates within 2 seconds", "auto-deflates within 1.5 seconds")["severity"] == "high"
    assert diff_summary("the device shows a clear icon", "the device displays a clear icon")["severity"] == "low"