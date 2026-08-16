from ingest.jcs_scores import lookup_ideology, rescale_jcs


def test_rescale_jcs():
    assert rescale_jcs(0.41) == 41
    assert rescale_jcs(-1) == -100
    assert rescale_jcs(1.2) == 100


def test_lookup_missing_is_not_party_score():
    out = lookup_ideology({"scores": {}}, "13761857")
    assert out["ideology_score"] is None
    assert out["ideology_prior_only"] is False


def test_lookup_known_id():
    snap = {"scores": {"1377101": {"jcs": 0.7, "source": "martin_quinn"}}}
    out = lookup_ideology(snap, "1377101")
    assert out["ideology_score"] == 70
    assert out["ideology_prior_only"] is False
