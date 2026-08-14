"""Offline tests for FJC Article III judge mapping.

Fixture rows are trimmed copies of the live 2026-08 FJC judges.csv export.
"""
from ingest.fjc import (
    full_name,
    iso_date,
    map_court_type,
    map_row,
    map_status,
    parse_csv,
    source_url_for,
)

ABELSON = {
    "nid": "13761857",
    "jid": "13761857",
    "Last Name": "Abelson",
    "First Name": "Adam",
    "Middle Name": "Ben",
    "Suffix": "",
    "Death Year": "",
    "Court Type (1)": "U.S. District Court",
    "Court Name (1)": "U.S. District Court for the District of Maryland",
    "Appointing President (1)": "Joseph R. Biden",
    "ABA Rating (1)": "Well Qualified",
    "Nomination Date (1)": "2024-05-14",
    "Confirmation Date (1)": "2024-09-10",
    "Senior Status Date (1)": "",
    "Termination (1)": "",
    "Other Federal Judicial Service (1)": (
        "U.S. Magistrate Judge, U.S. District Court for the "
        "District of Maryland, 2023-2024"
    ),
}

ALITO = {
    "nid": "1377101",
    "Last Name": "Alito",
    "First Name": "Samuel",
    "Middle Name": "A.",
    "Suffix": "Jr.",
    "Death Year": "",
    "Court Type (1)": "U.S. Court of Appeals",
    "Court Name (1)": "U.S. Court of Appeals for the Third Circuit",
    "Appointing President (1)": "George H.W. Bush",
    "ABA Rating (1)": "Well Qualified",
    "Nomination Date (1)": "1990-02-20",
    "Confirmation Date (1)": "1990-04-27",
    "Senior Status Date (1)": "",
    "Termination (1)": "Appointment to Another Judicial Position",
    "Court Type (2)": "Supreme Court",
    "Court Name (2)": "Supreme Court of the United States",
    "Appointing President (2)": "George W. Bush",
    "ABA Rating (2)": "Well Qualified",
    "Nomination Date (2)": "2005-11-10",
    "Confirmation Date (2)": "2006-01-31",
    "Senior Status Date (2)": "",
    "Termination (2)": "",
}

ADAMS_SENIOR = {
    "nid": "1377031",
    "Last Name": "Adams",
    "First Name": "Henry",
    "Middle Name": "Lee",
    "Suffix": "Jr.",
    "Death Year": "",
    "Court Type (1)": "U.S. District Court",
    "Court Name (1)": "U.S. District Court for the Middle District of Florida",
    "Appointing President (1)": "William J. Clinton",
    "ABA Rating (1)": "Qualified",
    "Nomination Date (1)": "1993-10-29",
    "Confirmation Date (1)": "1993-11-20",
    "Senior Status Date (1)": "2010-04-08",
    "Termination (1)": "",
}

ABRUZZO_DEAD = {
    "nid": "1376976",
    "Last Name": "Abruzzo",
    "First Name": "Matthew",
    "Middle Name": "T.",
    "Suffix": " ",
    "Death Year": "1971",
    "Court Type (1)": "U.S. District Court",
    "Court Name (1)": "U.S. District Court for the Eastern District of New York",
    "Appointing President (1)": "Franklin D. Roosevelt",
    "ABA Rating (1)": "",
    "Nomination Date (1)": "1936-02-03",
    "Confirmation Date (1)": "1936-02-12",
    "Senior Status Date (1)": "1966-02-15",
    "Termination (1)": "Death",
}

ADAMS_RETIRED_THEN_DEAD = {
    "nid": "1377011",
    "Last Name": "Adams",
    "First Name": "Arlin",
    "Middle Name": "M.",
    "Death Year": "2015",
    "Court Type (1)": "U.S. Court of Appeals",
    "Court Name (1)": "U.S. Court of Appeals for the Third Circuit",
    "Appointing President (1)": "Richard M. Nixon",
    "ABA Rating (1)": "Exceptionally Well Qualified",
    "Nomination Date (1)": "1969-09-22",
    "Confirmation Date (1)": "1969-10-01",
    "Senior Status Date (1)": "",
    "Termination (1)": "Retirement",
}

RESIGNED_LIVING = {
    "nid": "9990001",
    "Last Name": "Example",
    "First Name": "Living",
    "Death Year": "",
    "Court Type (1)": "U.S. District Court",
    "Court Name (1)": "U.S. District Court for the District of Columbia",
    "Appointing President (1)": "Barack Obama",
    "ABA Rating (1)": "Qualified",
    "Nomination Date (1)": "01/15/2012",
    "Confirmation Date (1)": "03/20/2012",
    "Senior Status Date (1)": "",
    "Termination (1)": "Resignation",
}

RECESS_LIVING = {
    "nid": "9990002",
    "Last Name": "Recess",
    "First Name": "Unconfirmed",
    "Death Year": "",
    "Court Type (1)": "U.S. District Court",
    "Court Name (1)": "U.S. District Court for the Northern District of Georgia",
    "Appointing President (1)": "Harry S Truman",
    "Nomination Date (1)": "1950-01-05",
    "Confirmation Date (1)": "",
    "Senior Status Date (1)": "",
    "Termination (1)": "Recess Appointment-Not Confirmed",
}

CIT = {
    "nid": "9990003",
    "Last Name": "Trade",
    "First Name": "Judge",
    "Death Year": "",
    "Court Type (1)": "Other",
    "Court Name (1)": "U.S. Court of International Trade",
    "Appointing President (1)": "Ronald Reagan",
    "Nomination Date (1)": "1983-06-01",
    "Confirmation Date (1)": "1983-07-01",
    "Senior Status Date (1)": "",
    "Termination (1)": "",
}


def test_iso_date_accepts_iso_and_us():
    assert iso_date("2024-05-14") == "2024-05-14"
    assert iso_date("05/14/2024") == "2024-05-14"
    assert iso_date("  ") is None
    assert iso_date("not-a-date") is None


def test_judge_id_is_fjc_nid_not_jid():
    rec = map_row(ABELSON)
    assert rec["judge_id"] == "13761857"
    assert rec["source_url"] == source_url_for("13761857")
    assert rec["source_url"] == "https://www.fjc.gov/node/13761857"


def test_abelson_is_active_district_not_magistrate():
    rec = map_row(ABELSON)
    assert rec.get("_review") is None
    assert rec["full_name"] == "Adam Ben Abelson"
    assert rec["court"] == "U.S. District Court for the District of Maryland"
    assert rec["court_type"] == "district_court"
    assert rec["appointing_president"] == "Joseph R. Biden"
    assert rec["nomination_date"] == "2024-05-14"
    assert rec["confirmation_date"] == "2024-09-10"
    assert rec["aba_rating"] == "Well Qualified"
    assert rec["status"] == "active"


def test_alito_uses_most_recent_slot():
    rec = map_row(ALITO)
    assert rec["court"] == "Supreme Court of the United States"
    assert rec["court_type"] == "supreme_court"
    assert rec["appointing_president"] == "George W. Bush"
    assert rec["nomination_date"] == "2005-11-10"
    assert rec["confirmation_date"] == "2006-01-31"
    assert rec["aba_rating"] == "Well Qualified"
    assert rec["status"] == "active"
    assert rec["full_name"] == "Samuel A. Alito Jr."


def test_senior_status_without_termination():
    rec = map_row(ADAMS_SENIOR)
    assert rec["status"] == "senior_status"
    assert rec["court_type"] == "district_court"


def test_death_year_beats_senior_and_retirement():
    dead = map_row(ABRUZZO_DEAD)
    assert dead["status"] == "deceased"
    retired_then_dead = map_row(ADAMS_RETIRED_THEN_DEAD)
    assert retired_then_dead["status"] == "deceased"
    assert retired_then_dead["court_type"] == "circuit_court"


def test_resignation_without_death_is_resigned():
    rec = map_row(RESIGNED_LIVING)
    assert rec["status"] == "resigned"
    assert rec["nomination_date"] == "2012-01-15"
    assert rec["confirmation_date"] == "2012-03-20"


def test_recess_not_confirmed_living_goes_to_review():
    rec = map_row(RECESS_LIVING)
    assert rec["_review"] is True
    assert "Recess Appointment-Not Confirmed" in rec["notes"]


def test_impeachment_maps_to_resigned():
    status = map_status(None, {"termination": "Impeachment & Conviction"})
    assert status == "resigned"


def test_international_trade_is_district_court():
    rec = map_row(CIT)
    assert rec["court_type"] == "district_court"
    assert rec["status"] == "active"


def test_court_type_historical_circuit():
    assert (
        map_court_type("U.S. Circuit Court (1869-1911)", "U.S. Circuit Court for the Third Circuit")
        == "circuit_court"
    )
    assert map_court_type("Other", "Court of Claims") == "other"
    assert map_court_type("Other", "U.S. Customs Court") == "district_court"
    assert (
        map_court_type("Other", "U.S. Court of Customs and Patent Appeals")
        == "circuit_court"
    )


def test_missing_nid_is_review():
    rec = map_row({"First Name": "No", "Last Name": "Id"})
    assert rec["_review"] is True


def test_full_name_skips_blank_middle_and_suffix():
    assert full_name(ABELSON) == "Adam Ben Abelson"
    assert (
        full_name(
            {
                "First Name": "Ronnie",
                "Middle Name": " ",
                "Last Name": "Abrams",
                "Suffix": " ",
            }
        )
        == "Ronnie Abrams"
    )


def test_parse_csv_respects_max_records():
    text = (
        "nid,Last Name,First Name\n"
        "1,A,Ann\n"
        "2,B,Bob\n"
        "3,C,Cara\n"
    )
    assert len(parse_csv(text, max_records=2)) == 2
    assert parse_csv(text, max_records=2)[0]["nid"] == "1"


if __name__ == "__main__":
    for fn in [
        test_iso_date_accepts_iso_and_us,
        test_judge_id_is_fjc_nid_not_jid,
        test_abelson_is_active_district_not_magistrate,
        test_alito_uses_most_recent_slot,
        test_senior_status_without_termination,
        test_death_year_beats_senior_and_retirement,
        test_resignation_without_death_is_resigned,
        test_recess_not_confirmed_living_goes_to_review,
        test_impeachment_maps_to_resigned,
        test_international_trade_is_district_court,
        test_court_type_historical_circuit,
        test_missing_nid_is_review,
        test_full_name_skips_blank_middle_and_suffix,
        test_parse_csv_respects_max_records,
    ]:
        fn()
        print("ok", fn.__name__)
    print("all passed")
