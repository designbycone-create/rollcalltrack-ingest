from ingest.judicial_events import classify_opinion_text, event_id


def test_event_id_is_stable_and_unique_per_type():
    url = "https://www.courtlistener.com/opinion/1/foo/"
    a = event_id(url, "nationwide_injunction")
    b = event_id(url, "nationwide_injunction")
    c = event_id(url, "vacatur_nonparty")
    assert a == b
    assert a != c
    assert len(a) == 32


def test_nationwide_injunction_explicit_text_writes():
    text = (
        "The court GRANTS a nationwide injunction restraining defendants "
        "from enforcing the rule against anyone, including non-parties."
    )
    out = classify_opinion_text(text)
    assert out["status"] == "write"
    assert out["event_type"] == "nationwide_injunction"
    assert out["classification"] == "explicit_text"
    assert out["disposition"] == "granted"


def test_bare_injunction_goes_to_review():
    text = "Plaintiffs' motion for a preliminary injunction is granted as to the named plaintiffs only."
    out = classify_opinion_text(text)
    assert out["status"] == "review"
    assert out["event_type"] is None
    assert "non-parties" in out["reason"] or "injunction" in out["reason"]


def test_vacatur_nonparty_explicit_writes():
    text = (
        "Pursuant to the APA the court VACATES the rule and sets it aside "
        "nationwide, not limited to the plaintiffs."
    )
    out = classify_opinion_text(text)
    assert out["status"] == "write"
    assert out["event_type"] == "vacatur_nonparty"


def test_empty_text_is_review():
    out = classify_opinion_text("")
    assert out["status"] == "review"
