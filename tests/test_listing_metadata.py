"""Listings described from the service's own metadata (SRI-86)."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from crowdcode import listing_metadata as lm
from tests.test_shared_wallet_db import db  # noqa: F401  isolated PostgreSQL schema fixture

DRIP = "https://dripstack.xyz/api/v1/publications/{publicationSlug}/{postSlug}"


def response(status=200, body=None, headers=None, content_type="application/json"):
    content = body if isinstance(body, (bytes, str)) else json.dumps(body or {})
    return httpx.Response(status, headers={"content-type": content_type, **(headers or {})},
                          content=content.encode() if isinstance(content, str) else content)


class Web:
    """A fake internet: (method, url) -> response; anything else is unreachable."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def __call__(self, method, url):
        self.calls.append((method, url))
        return self.pages.get((method, url)) or self.pages.get(("ANY", url))


def describe(endpoint, pages, **kwargs):
    web = Web(pages)
    return lm.describe_endpoint(endpoint, fetch=web, origins=lm.OriginCache(web), **kwargs), web


def test_x402_v2_resource_description_wins_over_origin_text():
    found, _ = describe("https://api.example.com/v1/speech", {
        ("POST", "https://api.example.com/v1/speech"): response(402, {
            "x402Version": 2,
            "resource": {"url": "https://api.example.com/v1/speech", "description": "Convert text to speech and return an MP3 file."},
            "accepts": [{"network": "eip155:8453", "payTo": "0x1"}],
            "extensions": {"bazaar": {"info": {"input": {"type": "http", "method": "POST",
                                                         "body": {"text": {"type": "string"}, "voice": {}}}}}},
        }),
        ("ANY", "https://api.example.com/llms.txt"): response(200, "# Example\n\n> Forty tools for agents, one bill.", content_type="text/plain"),
    })
    assert found.description("Example TTS") == ("x402", "https://api.example.com/v1/speech",
                                                "Convert text to speech and return an MP3 file.")
    # The host's summary is about all forty tools: it does not describe this one.
    assert "Forty tools" not in found.listing_text()
    assert "fields: text voice" in found.listing_text()


def test_x402_v1_accepts_description_and_base64_header():
    payload = {"x402Version": 1, "accepts": [{"description": "Current weather for any city, as JSON.", "payTo": "0x1",
                                              "outputSchema": {"input": {"queryParams": {"city": {}}}}}]}
    header = base64.b64encode(json.dumps(payload).encode()).decode()
    found, _ = describe("https://w.example.com/weather", {
        ("POST", "https://w.example.com/weather"): response(402, b"Payment required", {"payment-required": header},
                                                            content_type="text/plain"),
    })
    assert found.description(None)[2] == "Current weather for any city, as JSON."
    assert found.fields == ["city"]


def test_mpp_challenge_description():
    request = base64.urlsafe_b64encode(json.dumps({"amount": "1000", "recipient": "0x2",
                                                   "description": "Search the web and return ranked results."}).encode()).decode().rstrip("=")
    found, _ = describe("https://s.example.com/search", {
        ("POST", "https://s.example.com/search"): response(402, {}, {"www-authenticate": f'Payment method="tempo", request="{request}"'}),
    })
    assert found.description(None)[:2] == ("mpp", "https://s.example.com/search")


def test_templated_endpoint_is_described_from_its_openapi_document_without_a_probe():
    document = {"openapi": "3.1.0", "servers": [{"url": "https://dripstack.com"}],
                "info": {"title": "Drip", "summary": "Premium financial newsletter and podcast API with micropayment access.",
                         "description": "Data feed and API for tracking smart money — what top investors are buying, selling, and thinking."},
                "paths": {"/api/v2/stock-picks": {"get": {"summary": "Stock picks"}}}}
    found, web = describe(DRIP, {("ANY", "https://dripstack.xyz/openapi.json"): response(200, document)})
    assert not any(url == DRIP for _, url in web.calls)  # a path template is not a URL anyone can call
    assert found.description("Drip — Paid Financial Newsletter Research")[2].startswith("Premium financial newsletter")
    assert "smart money" in found.listing_text()


def test_openapi_operation_matches_path_parameters_and_server_prefix():
    document = {"servers": [{"url": "https://api.example.com/v2"}], "info": {"title": "T"},
                "paths": {"/report/stock/{ticker}": {"get": {"summary": "Analyst report",
                                                             "description": "Sell-side style analyst report for one stock.",
                                                             "parameters": [{"name": "ticker", "in": "path"}]}}}}
    found = lm.Found()
    lm.read_openapi(found, document, "https://api.example.com/openapi.json", "https://api.example.com/v2/report/stock/AAPL")
    assert found.description(None)[:2] == ("openapi operation", "https://api.example.com/openapi.json")
    assert "Sell-side style analyst report" in found.description(None)[2]
    assert found.fields == ["ticker"]


def test_landing_page_meta_description_and_boilerplate_is_refused():
    page = '<html><head><title>X</title><meta property="og:description" content="Book flights &amp; compare fares across airlines."></head></html>'
    found, _ = describe("https://t.example.com/flights", {
        ("POST", "https://t.example.com/flights"): response(402, b"Payment required", content_type="text/plain"),
        ("ANY", "https://t.example.com/"): response(200, page, content_type="text/html; charset=utf-8"),
    })
    assert found.description(None)[0] == "landing page"
    assert found.description(None)[2] == "Book flights & compare fares across airlines."
    for text in ("Payment required", "Not Found", "x402", "{\"error\": \"nope\"}", "Too short."):
        assert not lm.usable_description(text)
    assert not lm.usable_description("Example Weather Service Name Here", "Example Weather Service Name Here")


def test_mpp_catalog_endpoint_description():
    catalog = {"services": [{"serviceUrl": "https://parallelmpp.dev", "description": "Parallel's web APIs.",
                             "endpoints": [{"path": "/api/search", "description": "Web search for agents with excerpts."}]}]}
    found, _ = describe("https://parallelmpp.dev/api/search", {}, mpp_catalog=catalog, mpp=True)
    assert found.description(None)[:2] == ("mpp.dev endpoint", lm.MPP_CATALOG_URL)


def test_safe_fetch_rechecks_every_redirect_hop(monkeypatch):
    public = {"https://public.example.com/a": True, "https://internal.example.com/b": False}
    monkeypatch.setattr(lm, "public_endpoint", lambda url: public.get(url, False))

    def handler(request):
        return httpx.Response(308, headers={"location": "https://internal.example.com/b"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert lm.safe_fetch(client, "GET", "https://public.example.com/a") is None


def test_safe_fetch_follows_public_redirects_and_caps_size(monkeypatch):
    monkeypatch.setattr(lm, "public_endpoint", lambda url: True)

    def handler(request):
        if request.url.host == "old.example.com":
            return httpx.Response(308, headers={"location": "https://new.example.com/openapi.json"})
        if request.url.path == "/huge":
            return httpx.Response(200, content=b"x" * (lm.MAX_BYTES + 1))
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        got = lm.safe_fetch(client, "GET", "https://old.example.com/openapi.json")
        assert got.status_code == 200 and got.json() == {"ok": True}
        assert lm.safe_fetch(client, "GET", "https://new.example.com/huge") is None


class Conn:
    def __init__(self, rows):
        self.rows = rows
        self.updates = []
        self.info = type("Info", (), {"transaction_status": __import__("psycopg").pq.TransactionStatus.IDLE})()

    def execute(self, sql, params=None):
        if sql.strip().startswith("update"):
            self.updates.append(params)
        self._last = sql
        return self

    def fetchall(self):
        return self.rows

    def transaction(self):
        from contextlib import nullcontext
        return nullcontext()

    def commit(self):
        pass


def row(service_id, endpoint, metadata=None, name="svc"):
    return {"id": service_id, "name": name, "canonical_endpoint": endpoint, "payment_provider": "x402",
            "metadata": metadata or {}}


def test_fill_writes_only_what_the_service_said_and_never_replaces_a_declared_description():
    now = datetime(2026, 11, 4, tzinfo=timezone.utc)
    rows = [
        row("new", "https://a.example.com/speech"),
        row("agent", "https://crowdcode.app/s/word-count/count",
            {"description": "Counts words.", "listing_source": "crowdcode-agent-service"}),
        row("curated", "https://b.example.com/x", {"description": "Hand-written listing."}),
        row("recent-empty", "https://c.example.com/x", {"listing_checked_at": (now - timedelta(days=1)).isoformat()}),
        row("stale-empty", "https://d.example.com/x", {"listing_checked_at": (now - timedelta(days=8)).isoformat()}),
    ]
    web = Web({("POST", "https://a.example.com/speech"): response(402, {"resource": {"description": "Turn text into natural speech audio."}})})
    conn = Conn(rows)
    stats = lm.fill_descriptions(conn, fetch=web, mpp_catalog={}, now=now)
    assert stats == {"candidates": 2, "described": 1, "nothing_found": 1, "unchanged": 0}
    written = {service_id: patch.obj for patch, service_id, _ in conn.updates}
    assert set(written) == {"new", "stale-empty"}
    assert written["new"]["description"] == "Turn text into natural speech audio."
    assert written["new"]["description_source"] == "x402"
    assert written["new"]["listing_checked_at"] == now.isoformat()
    assert "description" not in written["stale-empty"]  # nothing found, nothing invented


def test_dry_run_writes_nothing():
    conn = Conn([row("new", "https://a.example.com/speech")])
    web = Web({("POST", "https://a.example.com/speech"): response(402, {"resource": {"description": "Turn text into natural speech audio."}})})
    seen = []
    lm.fill_descriptions(conn, fetch=web, mpp_catalog={}, apply=False, report=seen.append)
    assert conn.updates == []
    assert seen[0]["description"] == "Turn text into natural speech audio."


@pytest.mark.parametrize("snippets", [["Same words.", "Same words. And more about it."]])
def test_contained_snippets_are_kept_once(snippets):
    found = lm.Found()
    for text in snippets:
        found.add("x402", "u", text)
    assert [text for _, _, text in found.snippets] == ["Same words. And more about it."]


def test_fill_merges_into_metadata_in_postgres(db):  # noqa: F811
    from psycopg.types.json import Jsonb

    conn, _, _ = db
    corrections = {"payee_corrections": [{"script": "scripts/fix_machine_payees.py"}]}
    for service_id, endpoint, metadata in [
        ("svc_new", "https://a.example.com/speech", corrections),
        ("svc_curated", "https://b.example.com/x", {"description": "Hand-written listing."}),
    ]:
        conn.execute("insert into services (id, name, canonical_endpoint, payment_provider, metadata) values (%s, %s, %s, 'x402', %s)",
                     (service_id, service_id, endpoint, Jsonb(metadata)))
    web = Web({
        ("POST", "https://a.example.com/speech"): response(402, {"resource": {"description": "Turn text into natural speech audio."}}),
        ("POST", "https://b.example.com/x"): response(402, {"resource": {"description": "What the 402 says about it."}}),
    })
    stats = lm.fill_descriptions(conn, fetch=web, mpp_catalog={})
    assert stats["candidates"] == 1  # the curated listing is never looked at
    rows = {r["id"]: r["metadata"] for r in conn.execute("select id, metadata from services").fetchall()}
    assert rows["svc_new"]["payee_corrections"] == corrections["payee_corrections"]
    assert rows["svc_new"]["description"] == "Turn text into natural speech audio."
    assert rows["svc_new"]["description_source"] == "x402"
    assert rows["svc_curated"] == {"description": "Hand-written listing."}
    # The directory's search reads both columns.
    from crowdcode.discovery import directory_rows
    listed = {r["id"]: r for r in directory_rows(conn)}
    assert listed["svc_new"]["description"] == "Turn text into natural speech audio."
    assert listed["svc_new"]["listing_text"] == "Turn text into natural speech audio."


def test_the_web_service_only_describes_listings_never_looked_at():
    now = datetime(2026, 11, 4, tzinfo=timezone.utc)
    rows = [row("new", "https://a.example.com/x"),
            row("stale", "https://d.example.com/x", {"listing_checked_at": (now - timedelta(days=40)).isoformat(),
                                                     "description": "Old words.", "description_source": "x402"})]
    stats = lm.fill_descriptions(Conn(rows), fetch=Web({}), mpp_catalog={}, now=now, only_unchecked=True)
    assert stats["candidates"] == 1  # the monthly refresh is the cron's
    assert lm.fill_descriptions(Conn(rows), fetch=Web({}), mpp_catalog={}, now=now)["candidates"] == 2


def test_origin_documents_are_kept_parsed_and_compact():
    document = {"info": {"summary": "S"}, "components": {"schemas": {"Huge": {"x": "y" * 10_000}}},
                "paths": {"/a/{id}": {"post": {"summary": "Make a thing", "description": "Makes a thing from a prompt.",
                                               "parameters": [{"name": "id"}],
                                               "requestBody": {"content": {"application/json": {"schema": {"properties": {"prompt": {}}}}}}}}}}
    compact = lm.compact_openapi(document)
    assert "components" not in compact and len(json.dumps(compact)) < 600
    found = lm.Found()
    lm.read_openapi(found, compact, "u", "https://h.example/a/42")
    assert found.description(None)[2] == "Make a thing. Makes a thing from a prompt."
    assert found.fields == ["id", "prompt"]
    web = Web({("ANY", "https://h.example/llms.txt"): response(200, "> One line.", content_type="text/plain")})
    cache = lm.OriginCache(web)
    assert cache.get("https://h.example/llms.txt", lm.llms_summary) == "One line."
    assert cache.get("https://h.example/llms.txt", lm.llms_summary) == "One line."
    assert len(web.calls) == 1
