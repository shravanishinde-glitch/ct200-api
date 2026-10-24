from app.staleness import classify_change, worst


def test_changed_number_is_stale():
    r = classify_change("auto-deflates within 2 seconds", "auto-deflates within 1.5 seconds")
    assert r["status"] == "stale" and "numbers changed" in r["reason"]


def test_pure_wording_change_is_only_possibly_stale():
    r = classify_change("the device shows a clear icon", "the device displays a clear icon")
    assert r["status"] == "possibly_stale"


def test_negation_flip_without_numbers_is_stale():
    # no digit changed, but the requirement reversed: must not be treated like a cosmetic edit
    r = classify_change("audible alarm can be silenced", "audible alarm cannot be silenced")
    assert r["status"] == "stale"


def test_worst_status_wins():
    assert worst(["fresh", "possibly_stale", "stale"]) == "stale"
    assert worst(["stale", "source_removed"]) == "source_removed"
    assert worst([]) == "fresh"
