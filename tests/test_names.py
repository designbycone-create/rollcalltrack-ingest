"""Offline unit tests for name normalization / member-match keys."""
from ingest.names import (
    STATE_CODES,
    first_names_compatible,
    member_record_compatible,
    normalized_lookup_names,
    parse_filer_name,
    parse_state_district,
    normalize_state,
    state_display_name,
    state_for_member_lookup,
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


def test_usps_codes_map_to_full_names_including_dc_and_territories():
    # Horizons /members/lookup compares state to the stored full name.
    expected = {
        "OH": "Ohio",
        "MI": "Michigan",
        "CA": "California",
        "DC": "District of Columbia",
        "AS": "American Samoa",
        "GU": "Guam",
        "MP": "Northern Mariana Islands",
        "PR": "Puerto Rico",
        "VI": "Virgin Islands",
    }
    assert set(STATE_CODES) >= set(expected)
    assert len(STATE_CODES) == 56  # 50 states + DC + 5 territories
    for code, full in expected.items():
        assert state_for_member_lookup(code) == full
        assert state_for_member_lookup(code.lower()) == full
        assert state_for_member_lookup(full) == full
        assert state_display_name(code) == full
    assert state_for_member_lookup("not-a-state") is None


def test_lookup_names_drop_titles_suffixes_and_middle_names():
    # House Clerk: "Richard Dean Dr McCormick". Full name is first + last.
    assert normalized_lookup_names("Richard Dean Dr McCormick") == (
        "Richard McCormick",
        "McCormick",
    )
    assert normalized_lookup_names("Hon. David J. Taylor, Jr.") == (
        "David Taylor",
        "Taylor",
    )
    assert normalized_lookup_names("Jane Public III") == ("Jane Public", "Public")
    assert normalized_lookup_names("Dr. Hon. Laurel M. Lee") == ("Laurel Lee", "Lee")


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
        test_lookup_hit_must_be_the_same_person,
    ]:
        fn()
        print("ok", fn.__name__)
    print("all passed")
