"""Offline unit tests for name normalization / member-match keys."""
from ingest.names import (
    first_names_compatible,
    member_lookup_attempts,
    member_record_compatible,
    parse_filer_name,
    parse_state_district,
    normalize_state,
    state_display_name,
)


def test_house_middle_name_variants():
    p = parse_filer_name("Donald Sternoff Beyer")
    assert "donald beyer" in p.variants
    assert "donald sternoff beyer" in p.variants
    assert p.first == "Donald" and p.last == "Beyer"


def test_middle_initial_not_hyphenated_as_last():
    p = parse_filer_name("Robert B. Aderholt")
    assert "robert aderholt" in p.variants
    assert "robert b-aderholt" not in p.variants


def test_compound_last_name():
    p = parse_filer_name("April McClain Delaney")
    assert "april mcclain delaney" in p.variants
    assert "april mcclain-delaney" in p.variants


def test_senate_office_expands_name():
    p = parse_filer_name("Shelley M Capito", office="Capito, Shelley Moore (Senator)")
    assert p.full == "Shelley Moore Capito"
    assert "shelley moore capito" in p.variants
    assert "shelley capito" in p.variants


def test_title_stripped():
    p = parse_filer_name("Hon. Alma Shealey Adams")
    assert p.first == "Alma"
    assert "alma adams" in p.variants


def test_state_district():
    assert parse_state_district("AL04") == ("AL", 4)
    assert parse_state_district("CA33") == ("CA", 33)
    assert normalize_state("California") == "CA"
    assert normalize_state("al") == "AL"
    assert state_display_name("NJ") == "New Jersey"
    assert state_display_name("DC") == "District of Columbia"
    assert state_display_name("OH") == "Ohio"
    assert state_display_name("MI") == "Michigan"
    assert state_display_name("CA") == "California"
    # Filings sometimes already store the full name. Lookup must still send it.
    assert state_display_name("Ohio") == "Ohio"
    assert state_display_name("california") == "California"
    assert state_display_name("PR") == "Puerto Rico"
    assert state_display_name("VI") == "Virgin Islands"
    assert state_display_name("GU") == "Guam"
    assert state_display_name("AS") == "American Samoa"
    assert state_display_name("MP") == "Northern Mariana Islands"


def test_embedded_title_and_nickname_quotes():
    p = parse_filer_name('Richard Dean Dr McCormick')
    assert p.first == "Richard" and p.last == "McCormick"
    assert "dr" not in [t.lower() for t in p.middle_tokens]
    chuck = parse_filer_name('Charles J. "Chuck" Fleischmann')
    assert chuck.first == "Charles" and chuck.last == "Fleischmann"
    assert first_names_compatible("Josh", "Joshua")
    assert first_names_compatible("Rohit", "Ro")
    assert first_names_compatible("Thomas", "Tommy")
    assert not first_names_compatible("Josh", "John")


def test_suffixes_and_titles_leave_first_and_last():
    honored = parse_filer_name("Hon. David J. Taylor, Jr.")
    assert honored.first == "David" and honored.last == "Taylor"
    third = parse_filer_name("Robert Smith III")
    assert third.first == "Robert" and third.last == "Smith"
    doctor = parse_filer_name("Richard Dean Dr McCormick")
    assert doctor.first == "Richard" and doctor.last == "McCormick"
    assert "dr" not in [t.lower() for t in doctor.middle_tokens]


def test_lookup_attempts_full_name_then_last_then_bioguide():
    # House Clerk: "Richard Dean Dr McCormick". State on the filing is a USPS code.
    mccormick = member_lookup_attempts(
        "Richard Dean Dr McCormick",
        state="GA",
        bioguide_id="M001218",
    )
    assert mccormick == [
        {"name": "Richard McCormick", "state": "Georgia"},
        {"name": "McCormick", "state": "Georgia"},
        {"bioguide_id": "M001218"},
    ]
    taylor = member_lookup_attempts(
        "David J. Taylor",
        state="OH",
        bioguide_id="T000490",
    )
    assert taylor[0] == {"name": "David Taylor", "state": "Ohio"}
    assert taylor[1] == {"name": "Taylor", "state": "Ohio"}
    assert taylor[2] == {"bioguide_id": "T000490"}
    peters = member_lookup_attempts("Peters", state="MI", bioguide_id="P000608")
    assert peters[0] == {"name": "Peters", "state": "Michigan"}
    assert peters[-1] == {"bioguide_id": "P000608"}
    assert all(attempt.get("name") != "Peters Peters" for attempt in peters)
    lee = member_lookup_attempts(
        "Lee",
        state_district="CA12",
        bioguide_id="L000597",
    )
    assert lee[0]["state"] == "California"
    assert lee[-1] == {"bioguide_id": "L000597"}
    # Territories use the full name the members table stores.
    assert member_lookup_attempts("Eleanor Norton", state="DC")[0]["state"] == (
        "District of Columbia"
    )
    assert member_lookup_attempts("Stacey Plaskett", state="VI")[0]["state"] == (
        "Virgin Islands"
    )


def test_lookup_hit_must_be_the_same_person():
    assert not member_record_compatible(
        "Richard W. Allen",
        {"id": "M001218", "bioguide_id": "M001218", "name": "Richard McCormick", "state": "Georgia"},
        state="GA",
    )
    assert not member_record_compatible(
        "Richard W. Allen",
        {"id": "LS25179", "bioguide_id": "LS25179", "name": "Gary Richardson", "state": "GA"},
        state="GA",
    )
    assert member_record_compatible(
        "Rohit Khanna",
        {"id": "K000389", "bioguide_id": "K000389", "name": "Ro Khanna", "state": "California"},
        state="CA",
    )
    assert member_record_compatible(
        "Josh Gottheimer",
        {"id": "G000583", "bioguide_id": "G000583", "name": "Josh Gottheimer", "state": "New Jersey"},
        state="NJ",
    )


if __name__ == "__main__":
    for fn in [
        test_house_middle_name_variants,
        test_middle_initial_not_hyphenated_as_last,
        test_compound_last_name,
        test_senate_office_expands_name,
        test_title_stripped,
        test_state_district,
        test_embedded_title_and_nickname_quotes,
        test_suffixes_and_titles_leave_first_and_last,
        test_lookup_attempts_full_name_then_last_then_bioguide,
        test_lookup_hit_must_be_the_same_person,
    ]:
        fn()
        print("ok", fn.__name__)
    print("all passed")
