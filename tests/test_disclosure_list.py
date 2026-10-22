"""Disclosure filings are listed from members, not the retired /filings routes."""
import pytest

from ingest.ingest_client import DisclosureListError, HorizonsIngestClient


class _Resp:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data
        self.text = str(data)

    def json(self):
        return self._data


def _member(member_id, name):
    return {
        "id": member_id,
        "bioguide_id": member_id,
        "name": name,
        "chamber": "House",
        "jurisdiction": "federal",
    }


def test_list_pages_members_and_keeps_transaction_identity():
    hz = HorizonsIngestClient("https://example.test", "not-a-real-key")
    calls = []

    def fake_get(path, params=None):
        calls.append((path, params))
        if path == "/hcgi/api/members":
            if params["page"] == 1:
                return _Resp(
                    200,
                    {
                        "items": [_member("T000490", "David Taylor"), _member("P000608", "Scott Peters")],
                        "total": 3,
                        "totalPages": 2,
                        "page": 1,
                        "limit": 100,
                    },
                )
            return _Resp(
                200,
                {
                    "items": [_member("M001157", "Michael McCaul")],
                    "total": 3,
                    "totalPages": 2,
                    "page": 2,
                    "limit": 100,
                },
            )
        if path == "/hcgi/api/members/T000490/disclosures":
            return _Resp(
                200,
                {
                    "filings": [
                        {
                            "id": "f-taylor",
                            "member_id": "T000490",
                            "member_name": "David J. Taylor",
                            "member_party": "R",
                            "member_state": "OH",
                            "source_url": "https://disclosures-clerk.house.gov/ptr.pdf",
                            "filing_date": "10/15/2026",
                            "collectionId": "pbc_filings",
                            "created": "2026-10-01 00:00:00.000Z",
                        }
                    ],
                    "transactions": [
                        {
                            "id": "t1",
                            "filing_id": "f-taylor",
                            "line_index": 1,
                            "ticker": "FITB",
                            "ticker_confidence": "medium",
                            "asset_name": "Fifth Third",
                            "member_id": "T000490",
                            "collectionId": "pbc_tx",
                        },
                        {
                            "id": "t0",
                            "filing_id": "f-taylor",
                            "line_index": 0,
                            "ticker": "T",
                            "ticker_confidence": "medium",
                            "asset_name": "AT&T",
                            "member_id": "T000490",
                        },
                    ],
                },
            )
        if path == "/hcgi/api/members/P000608/disclosures":
            return _Resp(200, {"filings": [], "transactions": []})
        if path == "/hcgi/api/members/M001157/disclosures":
            return _Resp(
                200,
                {
                    "filings": [
                        {
                            "id": "f-mccaul",
                            "member_id": "M001157",
                            "member_name": "Michael T. McCaul",
                            "member_party": "R",
                            "member_state": "TX",
                            "filing_date": "10/14/2026",
                            "source_url": "https://disclosures-clerk.house.gov/mccaul.pdf",
                            "parse_status": "failed",
                        }
                    ],
                    "transactions": [],
                },
            )
        raise AssertionError(path)

    hz._get = fake_get
    filings = hz.list_disclosure_filings()
    transactions = hz.list_disclosure_transactions()
    assert [call[0] for call in calls].count("/hcgi/api/members") == 2
    # The second list call reuses the catalog. It does not fetch again.
    assert len(calls) == 5
    by_id = {row["id"]: row for row in filings}
    assert set(by_id) == {"f-taylor", "f-mccaul"}
    assert "collectionId" not in by_id["f-taylor"]
    taylor_tx = by_id["f-taylor"]["transactions"]
    assert [row["line_index"] for row in taylor_tx] == [0, 1]
    assert taylor_tx[0]["ticker"] == "T"
    assert taylor_tx[0]["ticker_confidence"] == "medium"
    assert taylor_tx[1]["ticker"] == "FITB"
    assert by_id["f-mccaul"]["filing_date"] == "10/14/2026"
    assert by_id["f-mccaul"]["transactions"] == []
    assert {(row["filing_id"], row["line_index"], row["ticker"]) for row in transactions} == {
        ("f-taylor", 0, "T"),
        ("f-taylor", 1, "FITB"),
    }


def test_pace_waits_when_the_rate_limit_window_is_nearly_spent(monkeypatch):
    hz = HorizonsIngestClient("https://example.test", "not-a-real-key")
    slept = []
    monkeypatch.setattr("ingest.ingest_client.time.sleep", lambda seconds: slept.append(seconds))

    class _Response:
        status_code = 200
        headers = {"ratelimit-remaining": "2", "ratelimit-reset": "15"}

    hz._note_rate_limit(_Response())
    hz._pace_get()
    assert slept == [15.0]
    assert hz._ratelimit_remaining is None

    hz._note_rate_limit(type("Plenty", (), {"headers": {"ratelimit-remaining": "40", "ratelimit-reset": "200"}})())
    hz._pace_get()
    assert slept == [15.0]


def test_list_retries_429_then_reads_the_filing(monkeypatch):
    hz = HorizonsIngestClient("https://example.test", "not-a-real-key")
    seen = {"disclosures": 0}
    monkeypatch.setattr("ingest.ingest_client.time.sleep", lambda _seconds: None)

    def fake_get(path, params=None):
        if path == "/hcgi/api/members":
            return _Resp(200, {"items": [_member("T000490", "David Taylor")], "total": 1, "totalPages": 1})
        seen["disclosures"] += 1
        if seen["disclosures"] == 1:
            return _Resp(429, {"message": "slow down"})
        return _Resp(
            200,
            {
                "filings": [
                    {
                        "id": "f-taylor",
                        "member_name": "David J. Taylor",
                        "source_url": "https://disclosures-clerk.house.gov/ptr.pdf",
                    }
                ],
                "transactions": [
                    {
                        "id": "t0",
                        "filing_id": "f-taylor",
                        "line_index": 0,
                        "ticker": "T",
                        "ticker_confidence": "medium",
                    }
                ],
            },
        )

    hz._get = fake_get
    filings = hz.list_disclosure_filings()
    assert seen["disclosures"] == 2
    assert filings[0]["transactions"][0]["ticker"] == "T"


def test_list_fails_when_members_route_404s():
    hz = HorizonsIngestClient("https://example.test", "not-a-real-key")
    hz._get = lambda path, params=None: _Resp(404, {"message": "Route not found"})
    with pytest.raises(DisclosureListError, match="404"):
        hz.list_disclosure_filings()


def test_list_fails_when_every_member_disclosure_404s():
    hz = HorizonsIngestClient("https://example.test", "not-a-real-key")

    def fake_get(path, params=None):
        if path == "/hcgi/api/members":
            return _Resp(200, {"items": [_member("T000490", "David Taylor")], "total": 1, "totalPages": 1})
        return _Resp(404, {"message": "Route not found"})

    hz._get = fake_get
    with pytest.raises(DisclosureListError, match="disclosures"):
        hz.list_disclosure_filings()


def test_list_retries_504_then_reads_the_filing(monkeypatch):
    hz = HorizonsIngestClient("https://example.test", "not-a-real-key")
    seen = {"disclosures": 0}
    monkeypatch.setattr("ingest.ingest_client.time.sleep", lambda _seconds: None)

    def fake_get(path, params=None):
        if path == "/hcgi/api/members":
            return _Resp(200, {"items": [_member("T000490", "David Taylor")], "total": 1, "totalPages": 1})
        seen["disclosures"] += 1
        if seen["disclosures"] == 1:
            return _Resp(504, {"message": "gateway timeout"})
        return _Resp(
            200,
            {
                "filings": [{"id": "f-taylor", "member_name": "David J. Taylor", "source_url": "https://example.test/ptr.pdf"}],
                "transactions": [{"id": "t0", "filing_id": "f-taylor", "line_index": 0, "ticker": "T", "ticker_confidence": "medium"}],
            },
        )

    hz._get = fake_get
    filings = hz.list_disclosure_filings()
    assert seen["disclosures"] == 2
    assert filings[0]["transactions"][0]["ticker"] == "T"


def test_list_fails_when_a_member_stays_unavailable(monkeypatch):
    hz = HorizonsIngestClient("https://example.test", "not-a-real-key")
    monkeypatch.setattr("ingest.ingest_client.time.sleep", lambda _seconds: None)

    def fake_get(path, params=None):
        if path == "/hcgi/api/members":
            return _Resp(
                200,
                {
                    "items": [_member("T000490", "David Taylor"), _member("S001200", "Someone")],
                    "total": 2,
                    "totalPages": 1,
                },
            )
        if path.endswith("/S001200/disclosures"):
            return _Resp(504, {"message": "gateway timeout"})
        return _Resp(
            200,
            {
                "filings": [{"id": "f-taylor", "source_url": "https://example.test/ptr.pdf"}],
                "transactions": [{"id": "t0", "filing_id": "f-taylor", "line_index": 0, "ticker": "T"}],
            },
        )

    hz._get = fake_get
    with pytest.raises(DisclosureListError, match="504"):
        hz.list_disclosure_filings()


def test_list_fails_when_a_member_stays_rate_limited(monkeypatch):
    hz = HorizonsIngestClient("https://example.test", "not-a-real-key")
    monkeypatch.setattr("ingest.ingest_client.time.sleep", lambda _seconds: None)

    def fake_get(path, params=None):
        if path == "/hcgi/api/members":
            return _Resp(200, {"items": [_member("M001227", "Someone")], "total": 1, "totalPages": 1})
        return _Resp(429, {"message": "slow down"})

    hz._get = fake_get
    with pytest.raises(DisclosureListError, match="429"):
        hz.list_disclosure_filings()
