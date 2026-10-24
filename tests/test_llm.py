import json
import pytest
from app.llm import GenerationFailed, extract_json, run_generation

SOURCES = [{"node_id": 7, "logical_id": "L-4.2", "number": "4.2", "heading": "Error Codes", "text": "E3 auto-deflates within 2 seconds"},
           {"node_id": 9, "logical_id": "L-4.3", "number": "4.3", "heading": "Alarm Thresholds", "text": "E3 cannot be silenced"}]


def tc(i, ids=(7,)):
    return {"title": f"Case {i}", "priority": "high", "preconditions": [], "steps": ["do it"],
            "expected_result": "E3 shown", "source_node_ids": list(ids)}


def good(n=3, **kw):
    return json.dumps({"test_cases": [tc(i, **kw) for i in range(n)]})


def scripted(*replies):
    calls = []
    def llm(messages):
        calls.append(messages)
        r = replies[min(len(calls) - 1, len(replies) - 1)]
        if isinstance(r, Exception):
            raise r
        return r
    llm.calls = calls
    return llm


def test_extract_json_handles_fences_and_chatter():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! Here you go: {"a": 1} hope it helps') == {"a": 1}
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_valid_output_first_try():
    cases, attempts, warnings, _ = run_generation(scripted(good(4)), SOURCES)
    assert len(cases) == 4 and attempts == [{"n": 1, "ok": True}] and warnings == []
    assert cases[0]["id"] == "TC-1"


def test_malformed_then_repaired_feeds_error_back():
    llm = scripted("this is not json", good(3))
    cases, attempts, _, _ = run_generation(llm, SOURCES)
    assert len(cases) == 3 and [a["ok"] for a in attempts] == [False, True]
    assert "rejected" in llm.calls[1][-1]["content"]            # the repair prompt carried the error


def test_too_few_cases_is_invalid_and_gives_up_after_max_attempts():
    llm = scripted(good(2))
    with pytest.raises(GenerationFailed) as e:
        run_generation(llm, SOURCES)
    assert len(e.value.attempts) == 3 and len(llm.calls) == 3


def test_too_many_cases_trimmed_to_five_with_warning():
    cases, _, warnings, _ = run_generation(scripted(good(7)), SOURCES)
    assert len(cases) == 5 and any("kept the first 5" in w for w in warnings)


def test_unknown_cited_ids_dropped_and_empty_citation_falls_back_to_whole_selection():
    cases, _, warnings, _ = run_generation(scripted(good(3, ids=(999,))), SOURCES)
    assert cases[0]["source_node_ids"] == [7, 9] and warnings


def test_provider_errors_are_retried():
    cases, attempts, _, _ = run_generation(scripted(TimeoutError("slow"), good(3)), SOURCES)
    assert len(cases) == 3 and "provider error" in attempts[0]["error"]
