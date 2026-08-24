"""Offline mapping tests for FOIA.gov agency_component ingest."""
from ingest.foia_federal import map_component, unique_slug


ABMC = {
    "type": "agency_component",
    "id": "403a8fdc-18bf-4ae5-99cb-8185367399c7",
    "attributes": {
        "title": "American Battle Monuments Commission",
        "abbreviation": "ABMC",
        "email": ["foia@abmc.gov"],
        "portal_submission_format": "email",
        "status": True,
        "website": {"uri": "http://www.abmc.gov/foia"},
        "submission_web": None,
        "reading_rooms": [{"uri": "https://www.abmc.gov/foia/foia-reading-room"}],
        "submission_address": {
            "address_line1": "2300 Clarendon Blvd",
            "locality": "Arlington",
            "administrative_area": "VA",
            "postal_code": "22201",
            "country_code": "US",
        },
        "telephone": "571-842-0020",
    },
    "relationships": {
        "agency": {"data": {"type": "agency", "id": "agency-1"}}
    },
}

INCLUDED = {
    "agency-1": {
        "type": "agency",
        "id": "agency-1",
        "attributes": {"name": "American Battle Monuments Commission", "abbreviation": "ABMC"},
    }
}


def test_map_component_federal_office():
    rec = map_component(ABMC, INCLUDED)
    assert rec["jurisdiction"] == "federal"
    assert rec["state_code"] is None
    assert unique_slug(rec["_abbr"], rec["foia_gov_id"], set()) == "abmc"
    assert rec["office_name"] == "American Battle Monuments Commission"
    assert rec["abbreviation"] == "ABMC"
    assert rec["foia_email"] == "foia@abmc.gov"
    assert rec["reading_room_url"] == "https://www.abmc.gov/foia/foia-reading-room"
    assert rec["website"] == "http://www.abmc.gov/foia"
    assert rec["request_url"] == "https://www.foia.gov/request"
    assert rec["foia_gov_id"] == "403a8fdc-18bf-4ae5-99cb-8185367399c7"
    assert rec["source_url"].endswith("403a8fdc-18bf-4ae5-99cb-8185367399c7")
    assert rec["parent_agency"] == "American Battle Monuments Commission"


def test_inactive_component_skipped():
    dead = {
        **ABMC,
        "attributes": {**ABMC["attributes"], "status": False},
    }
    assert map_component(dead, INCLUDED) is None


def test_unique_slug_collision():
    used = {"fbi"}
    assert unique_slug("FBI", "aaa-bbb", used) == "fbi-aaabbb"
    assert unique_slug("FBI", "ccc", set()) == "fbi"


def test_slug_strips_unsafe_chars():
    used: set[str] = set()
    assert unique_slug("DOJ/OIG", "id1", used) == "doj-oig"
    assert unique_slug("U.S.C.", "id2", used) == "u-s-c"
    assert unique_slug("***", "403a8fdc-18bf", used).startswith("office-")


def test_single_letter_abbr_is_not_a_url_slug():
    used: set[str] = set()
    slug = unique_slug("J", "403a8fdc-18bf-4ae5", used)
    assert slug.startswith("office-")
    assert len(slug) >= 8
