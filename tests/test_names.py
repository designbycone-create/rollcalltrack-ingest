"""Offline unit tests for name normalization / member-match keys."""
from ingest.names import parse_filer_name, parse_state_district, normalize_state


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


if __name__ == "__main__":
    for fn in [
        test_house_middle_name_variants,
        test_middle_initial_not_hyphenated_as_last,
        test_compound_last_name,
        test_senate_office_expands_name,
        test_title_stripped,
        test_state_district,
    ]:
        fn()
        print("ok", fn.__name__)
    print("all passed")
