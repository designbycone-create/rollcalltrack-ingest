"""Senate roll-call ingest from senate.gov LIS XML.

Hostinger cannot fetch senate.gov, so this job pulls the menu (and optional
per-vote detail) and posts through HorizonsIngestClient.submit_votes.
Payload fields match the House rows already stored on GET /hcgi/api/votes.
"""
from datetime import date
from pathlib import Path

import pytest

from ingest.ingest_client import HorizonsIngestClient, _LIST_GET_INTERVAL_SECONDS
from ingest.senate_votes import (
    BROWSER_USER_AGENT,
    HORIZONS_BATCH_SIZE,
    POST_INTERVAL_SECONDS,
    SenateVoteFetchError,
    _detail_datetime,
    current_senate_session,
    detail_url,
    fetch_xml,
    menu_url,
    parse_detail,
    parse_menu,
    resolve_sessions,
    run,
    run_sessions,
)

MENU_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<vote_summary>
  <congress>119</congress>
  <session>2</session>
  <congress_year>2026</congress_year>
  <votes>
    <vote>
      <vote_number>00256</vote_number>
      <vote_date>30-Sep</vote_date>
      <issue>PN1129</issue>
      <question>On the Nomination
         </question>
      <result>Confirmed</result>
      <vote_tally>
        <yeas>47</yeas>
        <nays>41</nays>
      </vote_tally>
      <title>Confirmation: Keith Sonderling, of F.L., to be Secretary of Labor</title>
    </vote>
    <vote>
      <vote_number>00256</vote_number>
      <vote_date>30-Sep</vote_date>
      <issue>PN1129</issue>
      <question>On the Nomination</question>
      <result>Confirmed</result>
      <vote_tally>
        <yeas>47</yeas>
        <nays>41</nays>
      </vote_tally>
      <title>Confirmation: Keith Sonderling, of F.L., to be Secretary of Labor</title>
    </vote>
    <vote>
      <vote_number>00001</vote_number>
      <vote_date>07-Jan</vote_date>
      <issue>S. 5</issue>
      <question>On Passage of the Bill</question>
      <result>Passed</result>
      <vote_tally>
        <yeas>60</yeas>
        <nays>40</nays>
      </vote_tally>
      <title>A bill to do something.</title>
    </vote>
  </votes>
</vote_summary>
"""

DETAIL_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<roll_call_vote>
  <congress>119</congress>
  <session>2</session>
  <congress_year>2026</congress_year>
  <vote_number>256</vote_number>
  <vote_date>September 30, 2026,  09:29 PM</vote_date>
  <question>On the Nomination</question>
  <vote_result>Nomination Confirmed</vote_result>
  <members>
    <member>
      <member_full>Alsobrooks (D-MD)</member_full>
      <last_name>Alsobrooks</last_name>
      <first_name>Angela</first_name>
      <party>D</party>
      <state>MD</state>
      <vote_cast>Nay</vote_cast>
      <lis_member_id>S428</lis_member_id>
    </member>
    <member>
      <member_full>Sanders (I-VT)</member_full>
      <party>I</party>
      <state>VT</state>
      <vote_cast>Present</vote_cast>
      <lis_member_id>S313</lis_member_id>
    </member>
    <member>
      <member_full>Absent (D-CA)</member_full>
      <vote_cast>Not Voting</vote_cast>
      <lis_member_id>S000</lis_member_id>
    </member>
    <member>
      <member_full>No Id</member_full>
      <vote_cast>Yea</vote_cast>
      <lis_member_id></lis_member_id>
    </member>
  </members>
</roll_call_vote>
"""

LIS = {"S428": "A000000", "S313": "S000033"}


class _Resp:
    def __init__(self, status, content, headers=None):
        self.status_code = status
        self.content = content
        self.headers = headers or {}


class _Session:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def get(self, url, timeout=None, allow_redirects=True, headers=None):
        self.calls.append(
            {
                "url": url,
                "timeout": timeout,
                "allow_redirects": allow_redirects,
                "headers": headers or {},
            }
        )
        return self.resp


def test_urls_cover_session_1_and_session_2():
    assert menu_url(119, 2) == (
        "https://www.senate.gov/legislative/LIS/roll_call_lists/vote_menu_119_2.xml"
    )
    assert menu_url(119, 1) == (
        "https://www.senate.gov/legislative/LIS/roll_call_lists/vote_menu_119_1.xml"
    )
    assert detail_url(119, 2, "00256") == (
        "https://www.senate.gov/legislative/LIS/roll_call_votes/"
        "vote1192/vote_119_2_00256.xml"
    )
    assert detail_url(119, 1, 1) == (
        "https://www.senate.gov/legislative/LIS/roll_call_votes/"
        "vote1191/vote_119_1_00001.xml"
    )


def test_current_session_switches_on_january_3():
    assert current_senate_session(date(2026, 10, 22)) == (119, 2)
    assert current_senate_session(date(2025, 6, 1)) == (119, 1)
    assert current_senate_session(date(2026, 1, 2)) == (119, 1)
    assert current_senate_session(date(2026, 1, 3)) == (119, 2)
    assert current_senate_session(date(2027, 1, 2)) == (119, 2)


def test_parse_menu_uses_congress_year_and_house_payload_shape():
    votes = parse_menu(MENU_XML)
    by_roll = {v["roll_number"]: v for v in votes}
    assert set(by_roll) == {"256", "1"}
    vote = by_roll["256"]
    assert vote["id"] == "s-119-2026-256"
    assert vote["rc_id"] == "s-119-2026-256"
    assert vote["congress"] == 119
    assert vote["chamber"] == "Senate"
    assert vote["session"] == "2"
    assert vote["roll_number"] == "256"
    assert vote["bill_label"] == "PN1129"
    assert vote["title"].startswith("Confirmation: Keith Sonderling")
    assert vote["description"] == vote["title"]
    assert vote["result"] == "Confirmed"
    assert vote["vote_type"] == "On the Nomination"
    assert vote["question"] == "On the Nomination"
    assert vote["description"] == vote["title"]
    assert vote["date"] == "2026-09-30"
    assert vote["tally_yea"] == 47
    assert vote["tally_nay"] == 41
    assert vote["tally_present"] == 0
    assert vote["tally_not_voting"] == 0
    assert vote["jurisdiction"] == "federal"
    assert vote["state_code"] == ""
    assert vote["source_url"] == detail_url(119, 2, 256)
    assert "member_votes" not in vote


def test_session_1_id_uses_that_documents_congress_year():
    raw = (
        MENU_XML.replace(b"<session>2</session>", b"<session>1</session>")
        .replace(b"<congress_year>2026</congress_year>", b"<congress_year>2025</congress_year>")
    )
    vote = next(v for v in parse_menu(raw) if v["roll_number"] == "256")
    assert vote["session"] == "1"
    assert vote["rc_id"] == "s-119-2025-256"
    assert vote["date"] == "2025-09-30"
    assert "/vote1191/vote_119_1_00256.xml" in vote["source_url"]


def test_fetch_xml_rejects_redirects_and_non_xml():
    session = _Session(
        _Resp(302, b"<html>blocked</html>", {"Location": "https://www.senate.gov/", "Content-Type": "text/html"})
    )
    with pytest.raises(SenateVoteFetchError, match="redirect"):
        fetch_xml(menu_url(119, 2), session=session)
    assert session.calls[0]["allow_redirects"] is False
    assert "Mozilla/5.0" in session.calls[0]["headers"]["User-Agent"]
    assert "bot" not in session.calls[0]["headers"]["User-Agent"].lower()
    assert BROWSER_USER_AGENT.startswith("Mozilla/5.0")

    html = _Session(_Resp(200, b"<!DOCTYPE html><html><body>no</body></html>", {"Content-Type": "text/html"}))
    with pytest.raises(SenateVoteFetchError, match="non-XML"):
        fetch_xml(menu_url(119, 2), session=html)

    junk = _Session(_Resp(200, b'{"votes":[]}', {"Content-Type": "application/json"}))
    with pytest.raises(SenateVoteFetchError, match="non-XML"):
        fetch_xml(menu_url(119, 2), session=junk)


def test_fetch_xml_returns_senate_xml():
    session = _Session(_Resp(200, MENU_XML, {"Content-Type": "text/xml; charset=UTF-8"}))
    body = fetch_xml(menu_url(119, 2), session=session)
    assert body.startswith(b"<?xml")
    assert parse_menu(body)[0]["chamber"] == "Senate"


def test_parse_detail_maps_member_positions_and_clock_time():
    summary = next(v for v in parse_menu(MENU_XML) if v["roll_number"] == "256")
    enriched = parse_detail(DETAIL_XML, summary, lis_to_bioguide=LIS)
    assert enriched["date"] == "2026-09-30T21:29:00-04:00"
    assert enriched["result"] == "Confirmed"
    assert enriched["question"] == "On the Nomination"
    assert enriched["vote_type"] == "On the Nomination"
    assert enriched["description"] == enriched["title"]
    assert enriched["tally_yea"] == 47
    assert enriched["tally_nay"] == 41
    assert enriched["tally_present"] == 1
    assert enriched["tally_not_voting"] == 1
    members = enriched["member_votes"]
    assert [m["bioguide_id"] for m in members] == ["A000000", "S000033"]
    nay = members[0]
    assert nay["vote"] == "nay"
    assert nay["rc_id"] == "s-119-2026-256"
    assert nay["name"] == "Alsobrooks (D-MD)"
    assert nay["jurisdiction"] == "federal"
    assert nay["state_code"] == ""
    assert members[1]["vote"] == "present"


# Roll 225 (2026-08-07) is an en-bloc confirmation. The live menu nests 74
# matters under <en_bloc>; this fixture keeps the first two. The live detail
# clock is "August 7, 2026, 01:23 PM".
EN_BLOC_MENU = b"""<?xml version="1.0" encoding="UTF-8"?>
<vote_summary>
  <congress>119</congress>
  <session>2</session>
  <congress_year>2026</congress_year>
  <votes>
    <vote>
      <vote_number>00225</vote_number>
      <vote_date>07-Aug</vote_date>
      <en_bloc>
        <matter>
          <issue>PN961-1</issue>
          <question>On the Nomination</question>
          <result>Confirmed</result>
        </matter>
        <matter>
          <issue>PN937-5</issue>
          <question>On the Nomination</question>
          <result>Confirmed</result>
        </matter>
      </en_bloc>
      <vote_tally>
        <yeas>51</yeas>
        <nays>47</nays>
      </vote_tally>
      <title>Confirmation: En Bloc nominations provided for under the provisions of S. Res. 817</title>
    </vote>
  </votes>
</vote_summary>
"""

EN_BLOC_DETAIL = b"""<?xml version="1.0" encoding="UTF-8"?>
<roll_call_vote>
  <congress>119</congress>
  <session>2</session>
  <congress_year>2026</congress_year>
  <vote_number>225</vote_number>
  <vote_date>August 7, 2026,  01:23 PM</vote_date>
  <question>On the Nomination</question>
  <vote_result>Nomination Confirmed</vote_result>
  <vote_title>Confirmation: En Bloc nominations provided for under the provisions of S. Res. 817</vote_title>
  <members>
    <member>
      <member_full>Alsobrooks (D-MD)</member_full>
      <vote_cast>Nay</vote_cast>
      <lis_member_id>S428</lis_member_id>
    </member>
  </members>
</roll_call_vote>
"""


def test_en_bloc_menu_uses_first_matter_and_counts_the_issue():
    vote = parse_menu(EN_BLOC_MENU)[0]
    assert vote["roll_number"] == "225"
    assert vote["rc_id"] == "s-119-2026-225"
    assert vote["bill_label"] == "En bloc (2)"
    assert vote["question"] == "On the Nomination"
    assert vote["vote_type"] == "On the Nomination"
    assert vote["result"] == "Confirmed"
    assert vote["description"] == vote["title"]
    assert vote["title"].startswith("Confirmation: En Bloc nominations")
    assert vote["date"] == "2026-08-07"
    assert vote["tally_yea"] == 51
    assert vote["tally_nay"] == 47


def test_en_bloc_detail_prefers_vote_result_and_eastern_offset():
    summary = parse_menu(EN_BLOC_MENU)[0]
    enriched = parse_detail(EN_BLOC_DETAIL, summary, lis_to_bioguide=LIS)
    assert enriched["question"] == "On the Nomination"
    assert enriched["vote_type"] == "On the Nomination"
    assert enriched["result"] == "Nomination Confirmed"
    assert enriched["bill_label"] == "En bloc (2)"
    assert enriched["description"] == enriched["title"]
    assert enriched["description"].startswith("Confirmation: En Bloc nominations")
    assert enriched["date"] == "2026-08-07T13:23:00-04:00"
    assert enriched["member_votes"][0]["vote"] == "nay"


def test_detail_datetime_uses_eastern_offset_across_dst():
    assert _detail_datetime("August 7, 2026, 01:23 PM") == "2026-08-07T13:23:00-04:00"
    assert _detail_datetime("September 30, 2026, 09:29 PM") == "2026-09-30T21:29:00-04:00"
    assert _detail_datetime("January 7, 2026, 01:23 PM") == "2026-01-07T13:23:00-05:00"
    assert _detail_datetime("November 3, 2026, 01:23 PM") == "2026-11-03T13:23:00-05:00"
    assert _detail_datetime("") is None
    assert _detail_datetime("not a date") is None


class _Client:
    def __init__(self):
        self.batches = []
        self.logs = []
        self.configured = True

    def submit_votes(self, records):
        self.batches.append(records)
        return {"ok": True, "written": len(records)}

    def submit_ingestion_log(self, source, found, parsed, failed, note=""):
        self.logs.append(
            {"source": source, "found": found, "parsed": parsed, "failed": failed, "note": note}
        )
        return {"ok": True}


def test_run_is_idempotent_and_fetches_detail_by_stable_url():
    fetched = []

    def fake_fetch(url, session=None):
        fetched.append(url)
        if url.endswith("vote_menu_119_2.xml"):
            return MENU_XML
        if url.endswith("vote_119_2_00256.xml"):
            return DETAIL_XML
        raise SenateVoteFetchError(f"unexpected {url}")

    client = _Client()
    kwargs = dict(
        congress=119,
        session=2,
        dry_run=False,
        fetch_details=True,
        client=client,
        today=date(2026, 10, 22),
        fetcher=fake_fetch,
        lis_to_bioguide=LIS,
        max_records=1,
        include_records=True,
    )
    first = run(**kwargs)
    second = run(**kwargs)
    assert first["failed"] == 0
    assert first["parsed"] == 1
    assert first["submitted"] == 1
    assert first["records"][0]["rc_id"] == "s-119-2026-256"
    assert first["records"][0]["member_votes"][0]["vote"] == "nay"
    assert client.batches[0][0]["rc_id"] == client.batches[1][0]["rc_id"]
    assert client.batches[0][0]["source_url"] == client.batches[1][0]["source_url"]
    assert client.batches[0] == client.batches[1]
    assert fetched.count(detail_url(119, 2, 256)) == 2
    assert all(not u.endswith("vote_119_2_00001.xml") for u in fetched)
    assert client.logs[0]["source"] == "senate_votes"


def test_run_counts_submitted_from_the_response():
    client = _Client()

    def submit_votes(records):
        client.batches.append(records)
        return {"ok": True, "written": len(records), "submitted": 0}

    client.submit_votes = submit_votes
    result = run(
        congress=119,
        session=2,
        dry_run=False,
        fetch_details=False,
        client=client,
        today=date(2026, 10, 22),
        menu_xml=MENU_XML,
        days_back=30,
    )
    assert len(client.batches) == 1
    assert result["submitted"] == 0


def test_days_back_drops_old_rolls_without_calling_them_failures():
    result = run(
        congress=119,
        session=2,
        days_back=7,
        dry_run=True,
        fetch_details=False,
        today=date(2026, 10, 22),
        menu_xml=MENU_XML,
    )
    assert result["found"] == 0
    assert result["parsed"] == 0
    assert result["failed"] == 0
    assert result["submitted"] == 0


def test_menu_redirect_is_a_failure_not_an_empty_success():
    def fake_fetch(url, session=None):
        raise SenateVoteFetchError("Senate vote URL redirected (302)")

    result = run(
        congress=119,
        session=2,
        dry_run=True,
        fetch_details=False,
        fetcher=fake_fetch,
    )
    assert result["failed"] == 1
    assert result["parsed"] == 0
    assert result["submitted"] == 0
    assert "redirect" in result["error"]


def test_dry_run_does_not_post():
    client = _Client()
    result = run(
        congress=119,
        session=2,
        dry_run=True,
        fetch_details=False,
        client=client,
        today=date(2026, 10, 22),
        menu_xml=MENU_XML,
        days_back=30,
        include_records=True,
    )
    assert result["submitted"] == 0
    assert client.batches == []
    assert {r["roll_number"] for r in result["records"]} == {"256"}
    assert result["records"][0]["chamber"] == "Senate"


def test_submit_votes_accepts_federal_senate_shape(monkeypatch):
    hz = HorizonsIngestClient("https://example.com", "key")
    seen = {}

    def fake_post(body, path="/hcgi/api/ingest/disclosures"):
        seen["body"] = body
        seen["path"] = path
        return _IngestResp()

    monkeypatch.setattr(hz, "_post_ingest", fake_post)
    record = parse_detail(
        DETAIL_XML,
        next(v for v in parse_menu(MENU_XML) if v["roll_number"] == "256"),
        lis_to_bioguide=LIS,
    )
    out = hz.submit_votes([record])
    assert seen["path"] == "/hcgi/api/ingest/votes"
    posted = seen["body"]["votes"][0]
    assert posted["chamber"] == "Senate"
    assert posted["jurisdiction"] == "federal"
    assert posted["state_code"] == ""
    assert posted["rc_id"] == "s-119-2026-256"
    assert posted["member_votes"][0]["bioguide_id"] == "A000000"
    assert out["written"] == 1

    state = {
        "id": "de-2025-1",
        "rc_id": "LSRC1",
        "congress": 2025,
        "chamber": "house",
        "session": "2025",
        "roll_number": "1",
        "source_url": "https://legiscan.com/DE/rollcall/HB1/id/1",
        "jurisdiction": "state",
        "state_code": "DE",
        "member_votes": [{"bioguide_id": "LS1", "vote": "yea"}],
    }
    hz.submit_votes([state])
    assert seen["body"]["votes"][0]["jurisdiction"] == "state"

    with pytest.raises(ValueError):
        hz.submit_votes([{**record, "chamber": "senate"}])
    with pytest.raises(ValueError):
        hz.submit_votes([{**record, "jurisdiction": "federal", "state_code": "MD"}])
    with pytest.raises(ValueError):
        hz.submit_votes([{**record, "source_url": ""}])


def test_submit_votes_logs_batch_status_and_written(monkeypatch, caplog):
    import logging

    hz = HorizonsIngestClient("https://example.com", "key")

    class _Counted(_IngestResp):
        status_code = 201

        def json(self):
            return {"ok": True, "written": 4, "submitted": 3}

    monkeypatch.setattr(hz, "_post_ingest", lambda body, path="/hcgi/api/ingest/disclosures": _Counted())
    record = parse_detail(
        DETAIL_XML,
        next(v for v in parse_menu(MENU_XML) if v["roll_number"] == "256"),
        lis_to_bioguide=LIS,
    )
    with caplog.at_level(logging.INFO):
        out = hz.submit_votes([record])
    assert out["submitted"] == 3
    assert "Votes batch HTTP 201 written=4 submitted=3" in caplog.text


def test_ingestion_log_keeps_senate_votes_source(monkeypatch):
    hz = HorizonsIngestClient("https://example.com", "key")
    seen = {}

    def fake_post(body, path="/hcgi/api/ingest/disclosures"):
        seen["body"] = body
        return _IngestResp()

    monkeypatch.setattr(hz, "_post_ingest", fake_post)
    hz.submit_ingestion_log("senate_votes", 2, 2, 0, note="menu")
    assert seen["body"]["payload"]["source"] == "senate_votes"


class _IngestResp:
    status_code = 200
    text = ""

    def json(self):
        return {"ok": True, "written": 1}

    def raise_for_status(self):
        return None


def test_source_all_runs_senate_votes(monkeypatch):
    import sys

    import ingest.main as mainmod

    seen = {}
    house_called = {"n": 0}

    def house_run(*_a, **_k):
        house_called["n"] += 1
        return {"found": 0, "parsed": 0, "failed": 0}

    monkeypatch.setattr(mainmod.house, "run", house_run)
    monkeypatch.setattr(
        mainmod.senate, "run", lambda *_a, **_k: {"found": 0, "parsed": 0, "failed": 0}
    )
    monkeypatch.setattr(
        mainmod.senate_votes,
        "run",
        lambda **k: seen.update(k) or {"found": 1, "parsed": 1, "failed": 0},
    )
    monkeypatch.setattr(
        sys, "argv", ["ingest.main", "--source", "all", "--dry-run"]
    )
    mainmod.main()
    assert house_called["n"] == 1
    assert seen["dry_run"] is True
    assert seen["fetch_details"] is True
    assert seen["congress"] is None
    assert seen["session"] is None
    assert seen["days_back"] is None
    assert seen["max_records"] == 5


def test_senate_votes_cli_passes_congress_and_session(monkeypatch):
    import sys

    import ingest.main as mainmod

    seen = {}
    monkeypatch.setattr(
        mainmod.senate_votes,
        "run",
        lambda **k: seen.update(k) or {"found": 0, "parsed": 0, "failed": 0},
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ingest.main",
            "--source",
            "senate_votes",
            "--congress",
            "118",
            "--session",
            "1",
            "--dry-run",
        ],
    )
    mainmod.main()
    assert seen["congress"] == 118
    assert seen["session"] == 1
    assert seen["dry_run"] is True


def _session_menu(session: int, year: int, rolls: int, en_bloc: int | None = None) -> bytes:
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<vote_summary>",
        "<congress>119</congress>",
        f"<session>{session}</session>",
        f"<congress_year>{year}</congress_year>",
        "<votes>",
    ]
    for n in range(1, rolls + 1):
        num = f"{n:05d}"
        if n == en_bloc:
            parts.append(
                f"""
    <vote>
      <vote_number>{num}</vote_number>
      <vote_date>15-Jan</vote_date>
      <en_bloc>
        <matter>
          <issue>PN1</issue>
          <question>On the Nomination</question>
          <result>Confirmed</result>
        </matter>
        <matter>
          <issue>PN2</issue>
          <question>On the Nomination</question>
          <result>Confirmed</result>
        </matter>
      </en_bloc>
      <vote_tally><yeas>51</yeas><nays>47</nays></vote_tally>
      <title>Confirmation: En Bloc nominations</title>
    </vote>
"""
            )
        else:
            parts.append(
                f"""
    <vote>
      <vote_number>{num}</vote_number>
      <vote_date>07-Jan</vote_date>
      <issue>S. {n}</issue>
      <question>On Passage of the Bill</question>
      <result>Passed</result>
      <vote_tally><yeas>60</yeas><nays>40</nays></vote_tally>
      <title>A bill number {n}.</title>
    </vote>
"""
            )
    parts.append("</votes></vote_summary>")
    return "".join(parts).encode()


SESSION1_MENU = _session_menu(1, 2025, 26, en_bloc=12)

NORMAL_2025_DETAIL = b"""<?xml version="1.0" encoding="UTF-8"?>
<roll_call_vote>
  <congress>119</congress>
  <session>1</session>
  <congress_year>2025</congress_year>
  <vote_number>1</vote_number>
  <vote_date>January 7, 2025,  01:23 PM</vote_date>
  <question>On the Cloture Motion</question>
  <vote_result>Bill Passed</vote_result>
  <members>
    <member>
      <member_full>Alsobrooks (D-MD)</member_full>
      <vote_cast>Nay</vote_cast>
      <lis_member_id>S428</lis_member_id>
    </member>
  </members>
</roll_call_vote>
"""

EN_BLOC_2025_DETAIL = b"""<?xml version="1.0" encoding="UTF-8"?>
<roll_call_vote>
  <congress>119</congress>
  <session>1</session>
  <congress_year>2025</congress_year>
  <vote_number>12</vote_number>
  <vote_date>January 15, 2025,  01:23 PM</vote_date>
  <question>On the Nomination</question>
  <vote_result>Nomination Confirmed</vote_result>
  <en_bloc>
    <matter>
      <issue>PN1</issue>
      <question>On the Nomination</question>
      <result>Confirmed</result>
    </matter>
    <matter>
      <issue>PN2</issue>
      <question>On the Nomination</question>
      <result>Confirmed</result>
    </matter>
  </en_bloc>
  <members>
    <member>
      <member_full>Alsobrooks (D-MD)</member_full>
      <vote_cast>Nay</vote_cast>
      <lis_member_id>S428</lis_member_id>
    </member>
  </members>
</roll_call_vote>
"""


def _fetch_119(url, session=None):
    if url.endswith("vote_menu_119_1.xml"):
        return SESSION1_MENU
    if url.endswith("vote_menu_119_2.xml"):
        return MENU_XML
    if url.endswith("vote_119_1_00012.xml"):
        return EN_BLOC_2025_DETAIL
    if "/vote1191/" in url:
        return NORMAL_2025_DETAIL
    if "/vote1192/" in url:
        return DETAIL_XML
    raise SenateVoteFetchError(f"unexpected {url}")


def _by_roll(batches):
    found = {}
    for batch in batches:
        for rec in batch:
            found[rec["roll_number"]] = rec
    return found


def test_resolve_sessions_keeps_current_session_only():
    today = date(2026, 10, 22)
    assert resolve_sessions("current", today=today) == [(119, 2)]
    assert resolve_sessions(None, today=today) == [(119, 2)]
    assert resolve_sessions("1", today=today) == [(119, 1)]
    assert resolve_sessions("2", today=today) == [(119, 2)]
    assert resolve_sessions("all", today=today) == [(119, 1), (119, 2)]
    assert resolve_sessions("1", congress=118, today=today) == [(118, 1)]
    assert resolve_sessions("ALL", today=today) == [(119, 1), (119, 2)]
    with pytest.raises(ValueError, match="senate_session"):
        resolve_sessions("3", today=today)


def test_post_interval_stays_inside_the_origin_window():
    assert HORIZONS_BATCH_SIZE == 25
    assert POST_INTERVAL_SECONDS == _LIST_GET_INTERVAL_SECONDS
    # 100th request falls outside the 5-minute window that started at the first.
    assert (100 - 1) * POST_INTERVAL_SECONDS >= 300


def test_session_1_backfill_reuses_parser_and_is_idempotent():
    client = _Client()
    sleeps = []
    kwargs = dict(
        senate_session="1",
        dry_run=False,
        fetch_details=True,
        client=client,
        today=date(2026, 10, 22),
        fetcher=_fetch_119,
        lis_to_bioguide=LIS,
        include_records=True,
        sleeper=sleeps.append,
    )
    first = run_sessions(**kwargs)
    second = run_sessions(**kwargs)

    assert first["found"] == 26
    assert first["parsed"] == 26
    assert first["failed"] == 0
    assert first["submitted"] == 26
    assert first["sessions"] == [
        {"congress": 119, "session": 1, "found": 26, "parsed": 26, "failed": 0, "submitted": 26}
    ]
    assert [len(batch) for batch in client.batches[:2]] == [HORIZONS_BATCH_SIZE, 1]
    assert sleeps == [POST_INTERVAL_SECONDS, POST_INTERVAL_SECONDS]
    assert client.batches[0] == client.batches[2]
    assert client.batches[1] == client.batches[3]
    posted = _by_roll(client.batches[:2])
    assert set(posted) == {str(n) for n in range(1, 27)}
    assert client.batches[0][0]["roll_number"] == "26"
    for roll, rec in posted.items():
        assert rec["id"] == f"s-119-2025-{roll}"
        assert rec["rc_id"] == rec["id"]
        assert rec["session"] == "1"
        assert rec["chamber"] == "Senate"
        assert rec["source_url"] == detail_url(119, 1, roll)
        assert rec["member_votes"][0]["bioguide_id"] == "A000000"
        assert rec["member_votes"][0]["vote"] == "nay"
        assert rec["member_votes"][0]["rc_id"] == rec["rc_id"]

    plain = posted["1"]
    assert plain["question"] == "On Passage of the Bill"
    assert plain["vote_type"] == "On Passage of the Bill"
    assert plain["result"] == "Passed"
    assert plain["date"] == "2025-01-07T13:23:00-05:00"
    assert plain["description"] == plain["title"]

    bloc = posted["12"]
    assert bloc["bill_label"] == "En bloc (2)"
    assert bloc["question"] == "On the Nomination"
    assert bloc["vote_type"] == "On the Nomination"
    assert bloc["result"] == "Nomination Confirmed"
    assert bloc["date"] == "2025-01-15T13:23:00-05:00"
    assert bloc["description"] == bloc["title"]
    assert first["records"][0]["rc_id"] == second["records"][0]["rc_id"]
    assert second["records"] == first["records"]


def test_session_1_backfill_does_not_fetch_the_current_session(monkeypatch):
    fetched = []

    def fake_fetch(url, session=None):
        fetched.append(url)
        return _fetch_119(url, session=session)

    run_sessions(
        senate_session="1",
        dry_run=True,
        fetch_details=True,
        today=date(2026, 10, 22),
        fetcher=fake_fetch,
        lis_to_bioguide=LIS,
        max_records=1,
        sleeper=lambda _s: pytest.fail("dry-run must not pace POSTs"),
    )
    assert fetched[0].endswith("vote_menu_119_1.xml")
    assert not any(url.endswith("vote_menu_119_2.xml") for url in fetched)
    assert any(url.endswith("vote_119_1_00026.xml") for url in fetched)


def test_all_sessions_posts_2025_then_2026_without_colliding():
    client = _Client()
    sleeps = []
    fetched = []

    def fake_fetch(url, session=None):
        fetched.append(url)
        return _fetch_119(url, session=session)

    result = run_sessions(
        senate_session="all",
        dry_run=False,
        fetch_details=True,
        client=client,
        today=date(2026, 10, 22),
        fetcher=fake_fetch,
        lis_to_bioguide=LIS,
        sleeper=sleeps.append,
    )
    assert [row["session"] for row in result["sessions"]] == [1, 2]
    assert result["found"] == 26 + 2
    assert result["submitted"] == 28
    assert result["failed"] == 0
    ids = [rec["rc_id"] for batch in client.batches for rec in batch]
    assert ids[:26] == [f"s-119-2025-{n}" for n in range(26, 0, -1)]
    assert set(ids[26:]) == {"s-119-2026-256", "s-119-2026-1"}
    assert len(ids) == len(set(ids))
    # One gap inside the 26-roll session, then one gap before session 2.
    assert sleeps == [POST_INTERVAL_SECONDS, POST_INTERVAL_SECONDS]
    assert fetched[0].endswith("vote_menu_119_1.xml")
    assert "vote_menu_119_2.xml" in fetched[27]


def test_current_choice_on_a_session_2_day_skips_2025():
    fetched = []

    def fake_fetch(url, session=None):
        fetched.append(url)
        return _fetch_119(url, session=session)

    result = run_sessions(
        senate_session="current",
        dry_run=True,
        fetch_details=False,
        today=date(2026, 10, 22),
        fetcher=fake_fetch,
        sleeper=lambda _s: pytest.fail("dry-run must not pace POSTs"),
    )
    assert result["sessions"] == [
        {"congress": 119, "session": 2, "found": 2, "parsed": 2, "failed": 0, "submitted": 0}
    ]
    assert fetched == [menu_url(119, 2)]


def test_failed_batch_still_paces_the_next_post():
    client = _Client()
    sleeps = []

    def submit_votes(records):
        client.batches.append(records)
        if len(client.batches) == 1:
            raise RuntimeError("origin 429")
        return {"submitted": len(records)}

    client.submit_votes = submit_votes
    result = run_sessions(
        senate_session="1",
        dry_run=False,
        fetch_details=False,
        client=client,
        today=date(2026, 10, 22),
        menu_xml=SESSION1_MENU,
        sleeper=sleeps.append,
    )
    assert [len(batch) for batch in client.batches] == [25, 1]
    assert sleeps == [POST_INTERVAL_SECONDS]
    assert result["failed"] == 25
    assert result["submitted"] == 1
    assert result["parsed"] == 26


def test_dry_run_backfill_does_not_post():
    client = _Client()
    result = run_sessions(
        senate_session="all",
        dry_run=True,
        fetch_details=False,
        client=client,
        today=date(2026, 10, 22),
        fetcher=_fetch_119,
        sleeper=lambda _s: pytest.fail("dry-run must not pace POSTs"),
    )
    assert client.batches == []
    assert client.logs == []
    assert result["submitted"] == 0
    assert result["found"] == 28
    assert "error" not in result


def test_all_continues_after_a_session_1_menu_failure():
    client = _Client()
    sleeps = []

    def fake_fetch(url, session=None):
        if url.endswith("vote_menu_119_1.xml"):
            raise SenateVoteFetchError("Senate vote URL redirected (302)")
        return _fetch_119(url, session=session)

    result = run_sessions(
        senate_session="all",
        dry_run=False,
        fetch_details=False,
        client=client,
        today=date(2026, 10, 22),
        fetcher=fake_fetch,
        sleeper=sleeps.append,
    )
    assert result["sessions"][0]["failed"] == 1
    assert result["sessions"][0]["submitted"] == 0
    assert result["sessions"][1]["session"] == 2
    assert result["sessions"][1]["parsed"] == 2
    assert result["submitted"] == 2
    assert "session 1:" in result["error"]
    assert sleeps == [POST_INTERVAL_SECONDS]
    assert client.logs[0]["source"] == "senate_votes"
    assert "session=2" in client.logs[-1]["note"]


def test_senate_session_backfill_dry_run_is_not_capped(monkeypatch):
    import sys

    import ingest.main as mainmod

    seen = {}

    def house_run(*_a, **_k):
        raise AssertionError("session backfill must not run disclosures")

    monkeypatch.setattr(mainmod.house, "run", house_run)
    monkeypatch.setattr(mainmod.senate, "run", house_run)
    monkeypatch.setattr(
        mainmod.senate_votes,
        "run",
        lambda **_k: (_ for _ in ()).throw(AssertionError("backfill uses run_sessions")),
    )
    monkeypatch.setattr(
        mainmod.senate_votes,
        "run_sessions",
        lambda **k: seen.update(k) or {"found": 0, "parsed": 0, "failed": 0},
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ingest.main",
            "--source",
            "senate_votes",
            "--senate-session",
            "1",
            "--dry-run",
        ],
    )
    mainmod.main()
    assert seen["senate_session"] == "1"
    assert seen["congress"] is None
    assert seen["days_back"] is None
    assert seen["max_records"] is None
    assert seen["dry_run"] is True
    assert seen["fetch_details"] is True


def test_source_all_ignores_senate_session_backfill(monkeypatch):
    import sys

    import ingest.main as mainmod

    seen = {}

    monkeypatch.setattr(
        mainmod.house, "run", lambda *_a, **_k: {"found": 0, "parsed": 0, "failed": 0}
    )
    monkeypatch.setattr(
        mainmod.senate, "run", lambda *_a, **_k: {"found": 0, "parsed": 0, "failed": 0}
    )
    monkeypatch.setattr(
        mainmod.senate_votes,
        "run_sessions",
        lambda **_k: (_ for _ in ()).throw(AssertionError("--source all must not backfill")),
    )
    monkeypatch.setattr(
        mainmod.senate_votes,
        "run",
        lambda **k: seen.update(k) or {"found": 1, "parsed": 1, "failed": 0},
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ingest.main",
            "--source",
            "all",
            "--senate-session",
            "all",
            "--session",
            "1",
            "--days-back",
            "3",
            "--dry-run",
        ],
    )
    mainmod.main()
    assert seen["congress"] is None
    assert seen["session"] is None
    assert seen["days_back"] is None
    assert seen["max_records"] == 5
    assert seen["dry_run"] is True


def test_senate_session_current_dry_run_stays_capped(monkeypatch):
    import sys

    import ingest.main as mainmod

    seen = {}
    monkeypatch.setattr(
        mainmod.senate_votes,
        "run_sessions",
        lambda **k: seen.update(k) or {"found": 0, "parsed": 0, "failed": 0},
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ingest.main",
            "--source",
            "senate_votes",
            "--senate-session",
            "current",
            "--dry-run",
        ],
    )
    mainmod.main()
    assert seen["senate_session"] == "current"
    assert seen["max_records"] == 5


def _dispatch_plan(event, session, dry_run, *, ingest_live=""):
    import os
    import subprocess

    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["DISPATCH_EVENT"] = event
    env["DISPATCH_SESSION"] = session
    env["DISPATCH_DRY_RUN"] = dry_run
    env["DISPATCH_PRINT_ONLY"] = "1"
    env["INGEST_LIVE"] = ingest_live
    out = subprocess.check_output(
        ["bash", str(root / "scripts" / "dispatch_ingest.sh")],
        cwd=root,
        env=env,
        text=True,
    )
    lines = {}
    for line in out.splitlines():
        if line.startswith(("ingest_live=", "argv=")):
            key, value = line.split("=", 1)
            lines[key] = value
    return lines


def test_scheduled_ingest_workflow_stays_on_current_session():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/ingest.yml").read_text(encoding="utf-8")
    assert 'cron: "0 9 * * *"' in workflow
    assert "timeout-minutes: 90" in workflow
    assert "senate_session:" in workflow
    assert "dry_run:" in workflow
    assert '- "1"' in workflow
    assert "\n          - all\n" in workflow
    # The schedule path is the script's event check, not a dispatch input.
    script = (root / "scripts" / "dispatch_ingest.sh").read_text(encoding="utf-8")
    assert 'event" = "schedule"' in script or '[ "$event" = "schedule" ]' in script
    plan = _dispatch_plan("schedule", "1", "true", ingest_live="")
    assert plan["argv"] == "python -m ingest.main --source all --live"
    assert plan["ingest_live"] == "1"
    assert "--senate-session" not in plan["argv"]
    ps1 = (root / "scripts/run_daily_ingest.ps1").read_text(encoding="utf-8")
    assert "--source all --live" in ps1
    assert '"-m", "ingest.main", "--source", "all", "--live"' in ps1
    assert "senate-session" not in ps1
    assert "senate_session" not in ps1


def test_dry_run_true_never_posts_and_ambiguous_values_stay_safe():
    """Run 39839118977: choice true/false was stored as false and posted.

    The workflow must pass the raw event string, and only the token live
    may add --live. true, false, empty, and any other value stay dry-run
    even if the step environment already has INGEST_LIVE=1.
    """
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/ingest.yml").read_text(encoding="utf-8")
    dry = workflow.split("dry_run:", 1)[1].split("jobs:", 1)[0]
    assert "type: choice" in dry
    assert "default: dry-run" in dry
    assert "- dry-run" in dry
    assert "- live" in dry
    for banned in ("- true", "- false", '- "true"', '- "false"', "- 'true'", "- 'false'"):
        assert banned not in dry
    assert "${{ inputs.dry_run }}" not in workflow
    assert "${{ github.event.inputs.dry_run }}" in workflow
    assert "INGEST_LIVE:" not in workflow
    assert "bash scripts/dispatch_ingest.sh" in workflow

    session_one = "python -m ingest.main --source senate_votes --senate-session 1 --dry-run"
    for raw in ("true", "false", "True", "FALSE", "", "dry-run", "yes", "no", "1", "0", "live "):
        plan = _dispatch_plan("workflow_dispatch", "1", raw, ingest_live="1")
        assert plan["argv"] == session_one, raw
        assert plan["ingest_live"] == "", raw
        assert "--live" not in plan["argv"]

    live = _dispatch_plan("workflow_dispatch", "1", "live", ingest_live="")
    assert live["argv"] == (
        "python -m ingest.main --source senate_votes --senate-session 1 --live"
    )
    assert live["ingest_live"] == "1"
    assert "--dry-run" not in live["argv"]

    missing = _dispatch_plan("workflow_dispatch", "", "", ingest_live="1")
    assert missing["argv"] == "python -m ingest.main --source all --dry-run"
    assert missing["ingest_live"] == ""
