import pytest
from app.parser.pdf_parser import parse_pdf, normalize

V1 = "data/ct200_manual.pdf"
V2 = "data/ct200_manual_v2.pdf"

@pytest.fixture(scope="module")
def v1():
    return parse_pdf(V1)

def get(r, number, heading=None):
    return [n for n in r["nodes"] if n["number"] == number and (heading is None or n["heading"] == heading)]

def test_duplicate_heading_distinct_nodes_and_parents(v1):
    errs = [n for n in v1["nodes"] if n["heading"] == "Error Codes"]
    assert len(errs) == 2
    assert {n["number"] for n in errs} == {"4.2", "7.1"}
    assert {n["parent_number"] for n in errs} == {"4", "7"}

def test_skipped_level_attaches_to_nearest_ancestor_and_warns(v1):
    n = get(v1, "2.1.1.1")[0]
    assert n["parent_number"] == "2.1" and n["level"] == 4
    assert any("skipped level" in w for w in n["warnings"])

def test_out_of_order_sections_keep_correct_parent(v1):
    order = [n["number"] for n in v1["nodes"] if n["parent_number"] == "3"]
    assert order == ["3.1", "3.2", "3.4", "3.3"]

def test_small_bold_heading_detected_despite_body_sized_font(v1):
    assert get(v1, "3.2") and get(v1, "2.1.1.1")      # both are 11pt bold, same as body

def test_cross_reference_is_not_a_heading(v1):
    assert len(v1["nodes"]) == 27
    assert "see 2.1, 4.3" in get(v1, "3.3")[0]["body"]

def test_tables_attached_to_correct_section(v1):
    assert len(get(v1, "2.1")[0]["tables"][0]) == 8     # header + 7 rows
    assert len(get(v1, "4.2")[0]["tables"][0]) == 6     # header + E1..E5

def test_ligatures_and_nonbreaking_hyphens_normalized():
    assert normalize("ﬁts the CT\u2011200") == "fits the CT-200"

def test_v2_same_text_different_page_break_same_hash(v1):
    v2 = parse_pdf(V2)
    for num in ["1.1", "3.1", "6.1", "8.1"]:
        assert get(v1, num)[0]["content_hash"] == get(v2, num)[0]["content_hash"]
    assert get(v1, "4.2")[0]["content_hash"] != get(v2, "4.2")[0]["content_hash"]
    