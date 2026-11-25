"""The services index (crowdcode.index): feeds, templates, probes, change history.

The integration test runs the real sweep and the real prober against a local
fixture server that plays both a feed and a seller, then changes its price,
then its payee, then goes down, then comes back.
"""

from __future__ import annotations

import base64
import json
import os
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

from crowdcode import index
from crowdcode.index_feeds import (
    BASE, SOLANA, TEMPO, USER_AGENT, caip2, fetch_bazaar, fetch_mpp, parse_bazaar_item,
    parse_mpp_catalog, route,
)
from crowdcode.index_probe import ProbeResult, ProbeTarget, probe, read_challenge, request_body

ROOT = Path(__file__).parents[1]
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
PAYEE_A = "0x" + "a1" * 20
PAYEE_B = "0x" + "b2" * 20


# --- Normalization -----------------------------------------------------------------


def test_networks_become_caip2_and_only_ours_count():
    assert caip2("base") == BASE
    assert caip2("eip155:8453") == BASE
    assert caip2("eip155:0x2105") == BASE
    assert caip2("solana") == SOLANA
    assert caip2("solana:5eykt4usfv8p8njdtrepy1vzqkqzkvdp") == SOLANA
    assert caip2("tempo") == TEMPO
    assert caip2("eip155:4217") == TEMPO
    assert caip2("base-sepolia") == "eip155:84532"
    assert caip2("") is None and caip2(None) is None


def test_route_templates_collapse_parameters_and_identifiers():
    colon = route("https://dripstack.com/api/v1/publications/:publicationSlug/:postSlug")
    braces = route("https://DripStack.com/api/v1/publications/{pub}/{post}/")
    assert colon.key("GET") == braces.key("GET") == "GET https://dripstack.com/api/v1/publications/{}/{}"
    assert colon.template == "https://dripstack.com/api/v1/publications/:publicationSlug/:postSlug"
    # Identifiers collapse wherever they come from.
    assert route("https://x.example/v1/items/12345").key_path == "/v1/items/{}"
    assert route("https://x.example/snap/2026-10-28").key_path == "/snap/{}"
    assert route("https://x.example/u/550e8400-e29b-41d4-a716-446655440000").key_path == "/u/{}"
    # A feed's own tool names are distinct resources...
    assert route("https://x.example/api/agent-intel/crosschain-gas-priority-fee").key_path.endswith("crosschain-gas-priority-fee")
    # ...but one URL per article from our own records is one resource.
    article = route("https://dripstack.com/api/v2/x402/sources/qualitystocks.substack.com/arteche-stock-deep-dive-a-hidden",
                    own_record=True)
    other = route("https://dripstack.com/api/v2/x402/sources/quoththeraven.substack.com/a-potentially-mispriced-microcap",
                  own_record=True)
    assert article.key("GET") == other.key("GET") == "GET https://dripstack.com/api/v2/x402/sources/{}/{}"


def bazaar_item(resource, *, payers=12, calls=40, method="POST", amount="10000", payee=PAYEE_A,
                network="eip155:8453", asset=USDC_BASE, body=None, scheme="exact"):
    info = {"type": "http", "method": method}
    if body is not None:
        info["body"] = body
        info["bodyType"] = "json"
    return {
        "resource": resource, "type": "http", "x402Version": 2, "description": "Search the web.",
        "serviceName": "Fixture Search",
        "accepts": [{"scheme": scheme, "network": network, "amount": amount, "asset": asset, "payTo": payee}],
        "extensions": {"bazaar": {"info": {"input": info}, "schema": {"properties": {"input": {"type": "object"}}}}},
        "quality": {"l30DaysUniquePayers": payers, "l30DaysTotalCalls": calls, "lastCalledAt": "2026-11-16T00:00:00Z"},
        "lastUpdated": "2026-11-16T00:00:00Z",
    }


def test_bazaar_listing_keeps_our_networks_and_marks_payable():
    listing = parse_bazaar_item({
        **bazaar_item("https://a.example/search", body={"query": "example"}),
        "accepts": [
            {"scheme": "exact", "network": "eip155:137", "amount": "5", "asset": "0xpolygon", "payTo": PAYEE_B},
            {"scheme": "exact", "network": "solana", "amount": "9000",
             "asset": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "payTo": "So1ana1111111111111111111111111111111111111"},
            {"scheme": "exact", "network": "base", "amount": "10000", "asset": USDC_BASE, "payTo": PAYEE_A},
        ],
    })
    assert listing.networks == {BASE, SOLANA}
    assert listing.payable
    assert (listing.price_atomic, listing.price_network) == (10000, BASE)  # Base first
    assert PAYEE_B not in listing.pay_to
    assert json.loads(listing.probe_body) == {"query": "example"}
    assert listing.payers_30d == 12 and listing.input_schema is not None
    # Another token, or a scheme the gateway does not sign, is listed but not payable.
    assert not parse_bazaar_item(bazaar_item("https://a.example/x", asset="0x" + "99" * 20)).payable
    assert not parse_bazaar_item(bazaar_item("https://a.example/x", scheme="batch-settlement")).payable


def test_mpp_catalog_endpoints_are_tempo_listings():
    listings = parse_mpp_catalog({"services": [{
        "id": "s", "name": "Fixture MPP", "serviceUrl": "https://mpp.example/", "description": "d",
        "endpoints": [
            {"method": "POST", "path": "/v1/search", "description": "Search",
             "payment": {"intent": "charge", "method": "tempo", "amount": "5000",
                         "currency": "0x20c000000000000000000000b9537d11c60e8b50"}},
            {"method": "GET", "path": "/v1/free", "description": "Free", "payment": None},
        ],
    }]})
    assert len(listings) == 1
    assert listings[0].url == "https://mpp.example/v1/search"
    assert listings[0].networks == {TEMPO} and listings[0].payable and listings[0].price_atomic == 5000


def test_merger_folds_instances_and_maps_our_records_onto_templates():
    merger = index.Merger()
    merger.add(parse_bazaar_item(bazaar_item("https://o.example/companies/123/news", method="GET", payers=4, calls=10)))
    merger.add(parse_bazaar_item(bazaar_item("https://o.example/companies/456/news", method="GET", payers=7, calls=5)))
    merger.add(parse_bazaar_item(bazaar_item("https://d.example/pubs/:pub/:post", method="GET", payers=3)))
    from crowdcode.index_feeds import Listing
    merger.add(Listing(source="gateway", method="GET", url="https://d.example/pubs/alpha/some-post",
                       service_id="svc_x", own_record=True))
    news = merger.resources["GET https://o.example/companies/{}/news"]
    assert (news.bazaar_payers, news.bazaar_calls) == (7, 15)
    assert news.probe_url == "https://o.example/companies/123/news"  # the busiest instance
    pubs = merger.resources["GET https://d.example/pubs/{}/{}"]
    assert pubs.sources == {"bazaar", "gateway"} and pubs.service_id == "svc_x"
    assert len(merger.resources) == 2


# --- Probes ------------------------------------------------------------------------


def test_probe_bodies_are_never_empty():
    assert request_body("GET", None, None) is None
    assert request_body("POST", None, None) == b"{}"
    assert request_body("POST", {}, None) == b"{}"
    assert json.loads(request_body("POST", {"query": "q"}, None)) == {"query": "q"}
    schema = {"schema": {"properties": {"body": {"required": ["text", "n"],
              "properties": {"text": {"type": "string"}, "n": {"type": "integer"}}}}}}
    assert json.loads(request_body("POST", None, schema)) == {"text": "test", "n": 1}
    for method in ("POST", "PUT", "PATCH"):
        assert request_body(method, None, None)


def test_media_generators_without_a_documented_body_are_asked_for_a_quote():
    assert json.loads(request_body("POST", None, None, "video")) == {"prompt": "A calm ocean at sunset, a short test clip"}
    assert json.loads(request_body("POST", None, None, "video", "kwaivgi/kling-v3.0-pro"))["model"] == "kwaivgi/kling-v3.0-pro"
    assert request_body("POST", None, None, "llm") == b"{}"
    assert json.loads(request_body("POST", {"q": 1}, None, "video")) == {"q": 1}  # the seller's own example wins


def test_synthesized_bodies_use_the_sellers_own_hints():
    # BlockRun's Bazaar listing: no example body, descriptions in info.bodyFields.
    blockrun = {
        "info": {"method": "POST", "bodyFields": {
            "model": {"type": "string", "description": "AI model ID (e.g., gpt-4o-mini, claude-haiku-4.5)"},
            "messages": {"type": "array", "description": "Array of chat messages with role and content"}}},
        "schema": {"properties": {"body": {"type": "object", "required": ["model", "messages"],
                                           "properties": {"model": {"type": "string"}, "messages": {"type": "array"}}}}},
    }
    assert json.loads(request_body("POST", None, blockrun)) == {
        "model": "gpt-4o-mini", "messages": [{"role": "user", "content": "Hello"}]}
    nested = {"schema": {"properties": {"body": {"required": ["items"], "properties": {"items": {
        "type": "array", "items": {"type": "object", "required": ["url"], "properties": {"url": {"type": "string",
        "example": "https://example.com"}}}}}}}}}
    assert json.loads(request_body("POST", None, nested)) == {"items": [{"url": "https://example.com"}]}


def x402_body(amount="10000", payee=PAYEE_A):
    return {"x402Version": 2, "accepts": [
        {"scheme": "exact", "network": "eip155:8453", "amount": amount, "asset": USDC_BASE, "payTo": payee}]}


def test_challenges_are_read_from_body_header_or_mpp():
    body = read_challenge(402, httpx.Headers({}), json.dumps(x402_body()).encode())
    assert body.ok and body.quoted_amount_atomic == 10000 and body.payee == PAYEE_A and body.network == BASE
    header = base64.b64encode(json.dumps(x402_body("20000")).encode()).decode()
    from_header = read_challenge(402, httpx.Headers({"payment-required": header}), b"")
    assert from_header.ok and from_header.quoted_amount_atomic == 20000
    request = base64.urlsafe_b64encode(json.dumps({
        "amount": "5000", "currency": "0x20C000000000000000000000b9537d11c60E8b50", "recipient": PAYEE_B,
        "methodDetails": {"chainId": 4217}}).encode()).decode().rstrip("=")
    mpp = read_challenge(402, httpx.Headers({"www-authenticate": f'Payment id="1", method="tempo", intent="charge", request="{request}"'}), b"")
    assert mpp.ok and mpp.network == TEMPO and mpp.payee == PAYEE_B and mpp.protocol == "mpp"
    assert read_challenge(404, httpx.Headers({}), b"").error_code == "http_404"
    assert read_challenge(400, httpx.Headers({}), b"{}").error_code == "probe_body_rejected"
    assert read_challenge(422, httpx.Headers({}), b"{}").error_code == "probe_body_rejected"
    assert read_challenge(200, httpx.Headers({}), b"{}").error_code == "no_challenge"
    redirect = read_challenge(301, httpx.Headers({"location": "https://new.example/x"}), b"")
    assert redirect.error_code == "redirect" and redirect.detail == "https://new.example/x"
    assert read_challenge(402, httpx.Headers({}), b"not json").error_code == "invalid_challenge"


def test_probe_refuses_private_hosts_without_calling():
    calls = []
    transport = httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(402))
    with httpx.Client(transport=transport) as client:
        result = probe(client, ProbeTarget(1, "POST", "https://127.0.0.1/x"))
    assert result.error_code == "not_public" and calls == []


def test_probe_sends_the_bot_user_agent_and_a_body():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(402, json=x402_body())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = probe(client, ProbeTarget(1, "POST", "https://seller.example/x"), allow=lambda _: True)
    assert result.ok
    assert seen[0].headers["user-agent"] == USER_AGENT == "CrowdCodeIndex/1 (+https://crowdcode.app/bot)"
    assert seen[0].content == b"{}" and seen[0].headers["content-type"] == "application/json"


def test_state_machine_flags_down_dead_and_revived():
    state = {"liveness": "unknown", "consecutive_failures": 0, "payees": [], "state_at": None}
    t = datetime(2026, 11, 1, tzinfo=UTC)
    ok = ProbeResult(True, 402, network=BASE, payee=PAYEE_A, pay_to=[PAYEE_A], quoted_amount_atomic=1, asset="usdc")
    down = ProbeResult(False, 503, "http_503")
    state, changes = index.apply_probe(state, ok, t)
    assert changes == [] and state["liveness"] == "alive"
    kinds = []
    for day in (1, 2, 3):
        state, changes = index.apply_probe(state, down, t + timedelta(days=day))
        kinds += [c[0] for c in changes]
    assert kinds == ["down", "dead"] and state["liveness"] == "dead"
    state, changes = index.apply_probe(state, ok, t + timedelta(days=4))
    assert [c[0] for c in changes] == ["revived"] and state["consecutive_failures"] == 0


# --- Integration: real sweep, real prober, real PostgreSQL --------------------------


class Fixture:
    """A feed and a seller on one local server."""

    def __init__(self) -> None:
        self.mode = {"price": "10000", "payee": PAYEE_A, "down": False}
        # Extra GET routes served verbatim: other feeds, model catalogs.
        self.pages: dict[str, object] = {}
        self.requests: list[dict] = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, payload, headers=None):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self):
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length) if length else b""
                path = self.path.split("?")[0]
                if path == "/feeds/bazaar":
                    return self._send(200, {"items": fixture.bazaar(), "pagination": {"total": 4}})
                if path in fixture.pages:
                    return self._send(200, fixture.pages[path])
                if path == "/feeds/mpp":
                    return self._send(200, {"services": []})
                fixture.requests.append({"method": self.command, "path": path, "body": body,
                                         "ua": self.headers.get("user-agent"),
                                         "ctype": self.headers.get("content-type")})
                if fixture.mode["down"]:
                    return self._send(503, {"error": "down"})
                return self._send(402, x402_body(fixture.mode["price"], fixture.mode["payee"]))

            do_GET = do_POST = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def bazaar(self):
        return [
            bazaar_item(f"{self.origin}/paid", body={"query": "example search query"}, payers=12),
            bazaar_item(f"{self.origin}/articles/:slug", method="GET", payers=5),
            bazaar_item(f"{self.origin}/junk", payers=2),
            bazaar_item(f"{self.origin}/self-test", payers=1),
        ]

    def feeds(self, fail_bazaar=False):
        def bazaar(client):
            if fail_bazaar:
                raise RuntimeError("feed down")
            return fetch_bazaar(client, f"{self.origin}/feeds/bazaar")
        return {"bazaar": bazaar, "mpp": lambda client: fetch_mpp(client, f"{self.origin}/feeds/mpp")}


class NoModel:
    def encode(self, texts):
        return None


# No network in tests beyond the local fixture server.
OFFLINE = {"embedder": NoModel(), "static_catalogs": (), "priors_fetcher": lambda client: ([], {})}


@pytest.fixture
def db():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")
    schema = "index_test_" + uuid4().hex
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(f"create schema {schema}")
        admin.execute(f"set search_path to {schema}")
        admin.execute((ROOT / "supabase/schema.sql").read_text())
        # Idempotent: applying it twice changes nothing.
        admin.execute((ROOT / "supabase/services-index.sql").read_text())
        admin.execute((ROOT / "supabase/services-index.sql").read_text())
        admin.execute((ROOT / "supabase/offerings-models.sql").read_text())
        admin.execute((ROOT / "supabase/offerings-models.sql").read_text())

    @contextmanager
    def connect():
        with psycopg.connect(url, row_factory=dict_row, options=f"-c search_path={schema}") as conn:
            yield conn

    try:
        yield connect
    finally:
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(f"drop schema {schema} cascade")


def test_migration_accepts_w3s_observations_table_first():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")
    schema = "index_w3_" + uuid4().hex
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f"create schema {schema}")
        try:
            conn.execute(f"set search_path to {schema}")
            conn.execute((ROOT / "supabase/schema.sql").read_text())
            conn.execute("""
                create table if not exists index_observations (
                  id bigserial primary key, resource_id bigint, endpoint text not null, method text,
                  at timestamptz not null default now(), kind text not null, ok boolean not null,
                  http_status int, network text, payee text, quoted_amount_atomic numeric, error_code text);
                alter table index_observations add column if not exists observer_wallet text;
            """)
            conn.execute((ROOT / "supabase/services-index.sql").read_text())
            conn.execute((ROOT / "supabase/offerings-models.sql").read_text())
            conn.execute("insert into index_observations (endpoint, kind, ok, observer_wallet) values ('https://x.example/a', 'observation', true, '0xabc')")
        finally:
            conn.execute(f"drop schema {schema} cascade")


def test_a_seller_changing_price_then_payee_then_going_down_is_flagged_and_revives(db):
    fixture = Fixture()
    t0 = datetime(2026, 11, 10, 6, 0, tzinfo=UTC)
    try:
        index.run_sweep(db, t0, feeds=fixture.feeds(), **OFFLINE)
        with db() as conn:
            rows = {r["route_template"]: r for r in conn.execute("select * from index_resources").fetchall()}
            paid = rows[f"{fixture.origin}/paid"]
            assert paid["tracked"] and paid["tracked_reason"] == "bazaar_payers"
            assert paid["networks"] == [BASE] and paid["payable"] and paid["sources"] == ["bazaar"]
            assert rows[f"{fixture.origin}/articles/:slug"]["tracked"]
            assert not rows[f"{fixture.origin}/junk"]["tracked"]  # listed, not tracked
            assert not rows[f"{fixture.origin}/self-test"]["tracked"]  # every listing is listed (C9)
            service = conn.execute("select * from services where id = %s", (paid["service_id"],)).fetchone()
            assert service["created_from_review"] is False
            assert service["metadata"]["listing_source"] == "index"

        def probe_day(day, **mode):
            fixture.mode.update(mode)
            return index.run_probes(db, t0 + timedelta(days=day), allow=lambda _: True, workers=2)

        assert probe_day(0)["probed"] == 2
        # At most one request per resource per day: a second run the same hour sends nothing.
        before = len(fixture.requests)
        assert index.run_probes(db, t0 + timedelta(hours=1), allow=lambda _: True)["probed"] == 0
        assert len(fixture.requests) == before

        # A seller that opted out is not probed at all.
        with db() as conn:
            conn.execute("insert into index_opt_outs (prefix, note) values (%s, 'test')", (f"{fixture.origin}/articles/",))
            conn.commit()
        probe_day(1, price="25000")
        with db() as conn:
            conn.execute("delete from index_opt_outs")
            conn.commit()
        probe_day(2, payee=PAYEE_B)
        probe_day(3, down=True)
        probe_day(4)
        probe_day(5)
        probe_day(6, down=False)

        with db() as conn:
            changes = conn.execute(
                "select kind, old_value, new_value from index_changes where resource_id = %s order by at, id",
                (paid["id"],),
            ).fetchall()
            assert [c["kind"] for c in changes] == ["price", "payee", "down", "dead", "revived"]
            assert changes[0]["old_value"].startswith("10000 ") and changes[0]["new_value"].startswith("25000 ")
            assert changes[1]["old_value"] == PAYEE_A and changes[1]["new_value"] == PAYEE_B
            signals = conn.execute("select * from index_resource_signals where resource_id = %s", (paid["id"],)).fetchone()
            assert [p["ok"] for p in signals["last_probes"]] == [True, False, False]
            assert signals["liveness"] == "alive" and signals["payees"] == [PAYEE_B]
            assert signals["payee_since"] == t0 + timedelta(days=2)
            assert int(signals["latest_price_atomic"]) == 25000

        # Every probe identified itself and never sent an empty POST body.
        posts = [r for r in fixture.requests if r["path"] == "/paid"]
        assert len(posts) == 7
        assert all(r["ua"] == USER_AGENT for r in fixture.requests)
        assert all(r["method"] == "POST" and r["body"] and json.loads(r["body"]) == {"query": "example search query"}
                   and r["ctype"] == "application/json" for r in posts)
        gets = [r for r in fixture.requests if r["path"].startswith("/articles/")]
        assert len(gets) == 6  # seven days, one skipped while opted out
        assert all(r["method"] == "GET" and r["body"] == b"" for r in gets)

        with db() as conn:
            health = index.health(conn, t0 + timedelta(days=6, minutes=5))
            assert health["tracked"] == 2 and health["probed_24h_share"] == 1.0, health
            assert health["bazaar_10_payers"] == {"urls": 1, "tracked": 1, "share": 1.0}
            assert {s["source"] for s in health["sources"]} >= {"bazaar", "mpp", "hosted", "gateway", "reviewed"}
            detail = index.resource_detail(conn, f"{fixture.origin}/paid", "POST")
            assert detail["resource"]["tracked"] and detail["changes"][0]["kind"] == "revived"
            # A concrete URL resolves to its template.
            assert index.resolve_resource(conn, f"{fixture.origin}/articles/some-article", "GET")["has_params"]
    finally:
        fixture.server.shutdown()


def test_a_failing_feed_does_not_stop_the_others_and_the_job_raises(db):
    fixture = Fixture()
    t0 = datetime(2026, 11, 10, 6, 0, tzinfo=UTC)
    try:
        index.run_sweep(db, t0, feeds=fixture.feeds(), **OFFLINE)
        with pytest.raises(index.IndexJobError, match="bazaar"):
            index.run_sweep(db, t0 + timedelta(days=1), feeds=fixture.feeds(fail_bazaar=True), **OFFLINE)
        with db() as conn:
            sweeps = {(r["source"], r["ok"]) for r in conn.execute(
                "select source, ok from index_sweeps where started_at >= %s", (t0 + timedelta(days=1),)).fetchall()}
            assert ("bazaar", False) in sweeps and ("mpp", True) in sweeps
            # Yesterday's Bazaar evidence is kept, not wiped, when today's read failed.
            paid = conn.execute("select * from index_resources where route_template = %s", (f"{fixture.origin}/paid",)).fetchone()
            assert paid["tracked"] and paid["bazaar_payers_30d"] == 12 and "bazaar" in paid["sources"]
            # A feed swept less than a day ago is not read again.
        assert index.run_sweep(db, t0 + timedelta(days=1, hours=2), feeds=fixture.feeds(), **OFFLINE)["due"] == ["bazaar"]
    finally:
        fixture.server.shutdown()


def test_hosted_services_and_gateway_purchases_are_tracked_and_observed(db):
    t0 = datetime(2026, 11, 10, 6, 0, tzinfo=UTC)
    with db() as conn:
        conn.execute("""
            insert into services (id, name, canonical_endpoint, canonical_origin, metadata) values
              ('svc_hosted', 'Hosted', 'https://crowdcode.app/s/fixture', 'https://crowdcode.app',
               '{"crowdcode_service_id": "x", "hosted_status": "live", "hosted_route": "POST /"}'),
              ('svc_drip1', 'Drip 1', 'https://dripstack.com/api/v2/x402/sources/a.substack.com/one-long-article-slug-here', 'https://dripstack.com', '{}'),
              ('svc_drip2', 'Drip 2', 'https://dripstack.com/api/v2/x402/sources/b.substack.com/another-long-article-slug', 'https://dripstack.com', '{}')
        """)
        # Bought under the hosted service's old URL, before a rename.
        conn.execute(
            "insert into reviews (service_id, rating, reason, receipt_key_id, gateway_attestation) values ('svc_hosted', 5, 'ok', 'k', %s)",
            (json.dumps({"receipt": {"endpoint": "https://crowdcode.app/s/old-name", "method": "POST", "network": "base",
                                     "payee": PAYEE_A, "amount_atomic": "1000", "response_status": 200,
                                     "settled_at": "2026-11-08T10:00:00Z"}}),),
        )
        receipt = lambda url, sid: {"receipt": {
            "endpoint": url, "method": "GET", "network": "base", "payee": PAYEE_A, "amount_atomic": "1000000",
            "token": USDC_BASE, "response_status": 200, "settled_at": "2026-11-09T10:00:00Z"}}
        for n, sid in enumerate(("svc_drip1", "svc_drip2", "svc_drip2")):
            url = conn.execute("select canonical_endpoint from services where id = %s", (sid,)).fetchone()["canonical_endpoint"]
            conn.execute(
                "insert into reviews (service_id, rating, reason, receipt_key_id, gateway_attestation) values (%s, 5, 'ok', 'k', %s)",
                (sid, json.dumps(receipt(url, sid))),
            )
        conn.commit()
    index.run_sweep(db, t0, feeds={}, **OFFLINE)
    with db() as conn:
        rows = {r["resource_key"]: r for r in conn.execute("select * from index_resources").fetchall()}
        hosted = rows["POST https://crowdcode.app/s/fixture"]
        assert hosted["tracked_reason"] == "hosted" and hosted["service_id"] == "svc_hosted"
        assert hosted["sources"] == ["hosted", "gateway"]
        drip = rows["GET https://dripstack.com/api/v2/x402/sources/{}/{}"]
        # Three purchases of two article URLs: one resource, linked to the most-bought service.
        assert drip["tracked_reason"] == "gateway_purchase" and drip["service_id"] == "svc_drip2"
        assert len(rows) == 2
    stats = index.run_purchases(db, t0)
    assert stats["purchases"] == 4
    with db() as conn:
        purchases = conn.execute("select * from index_observations where kind = 'purchase' and endpoint like '%%dripstack%%'").fetchall()
        assert {p["resource_id"] for p in purchases} == {drip["id"]}
        assert all(p["ok"] and p["network"] == BASE and int(p["quoted_amount_atomic"]) == 1000000 for p in purchases)
    assert index.run_purchases(db, t0)["purchases"] == 0


def test_hosted_manifests_give_each_route_a_probe_body_from_its_schema():
    from crowdcode.agent_listings import parse_manifest
    listing = parse_manifest("venice-proxy", {
        "name": "Venice", "url": "https://crowdcode.app/s/venice-proxy",
        "routes": [{"method": "POST", "url": "https://crowdcode.app/s/venice-proxy/chat/completions",
                    "input_schema": {"type": "object", "required": ["model", "messages"], "properties": {
                        "model": {"type": "string", "example": "venice-uncensored-1-2"},
                        "messages": {"type": "array", "example": [{"role": "user", "content": "Hello"}]},
                        "max_tokens": {"type": "integer", "example": 256}}}},
                   {"method": "GET", "url": "https://crowdcode.app/s/venice-proxy/health"}],
    }, "https://crowdcode.app")
    assert listing["route_bodies"] == {"https://crowdcode.app/s/venice-proxy/chat/completions": {
        "model": "venice-uncensored-1-2", "messages": [{"role": "user", "content": "Hello"}]}}



def test_model_names_normalize_across_sellers_and_leaderboards():
    from crowdcode.index_models import normalize_model, offering_model
    same = {
        "veo-3.1": ["google/veo-3.1", "fal-ai/veo3.1", "Veo 3.1", "/api/generate/veo-3.1/generate"],
        "kling-v3-pro": ["kwaivgi/kling-v3.0-pro", "/fal-ai/kling-video/v3/pro/text-to-video", "Kling 3.0 1080p (Pro)"],
        "seedance-2.5": ["bytedance/seedance-2.5", "/bytedance/seedance-2.5/text-to-video", "Dreamina Seedance 2.5"],
        "hailuo-3-max": ["minimax/hailuo-3-max", "/minimax/h3-max/text-to-video"],
        "flux-1.1-pro": ["/fal-ai/flux-pro/v1.1", "FLUX1.1 [pro] image generation"],
        "wan-3": ["alibaba/wan-3.0", "Wan 3.0"],
        "sora-2-pro": ["/api/generate/sora-2-pro/generate"],
        "gpt-6.2-astra": ["GPT-6.2 Astra Max", "openai/gpt-6.2-astra"],
    }
    for model, names in same.items():
        assert {normalize_model(n).model for n in names} == {model}, names
    assert normalize_model("google/veo-3.1").modality == "video"
    assert normalize_model("/fal-ai/flux-pro/v1.1").modality == "image"
    assert normalize_model("/stock/quote") is None
    # A route naming one model sells it; a description naming several is a router.
    assert offering_model("/api/generate/wan-2.6/t2v", None, None)[0].model == "wan-2.6"
    assert offering_model("/v1/videos", "Veo 3.1, Sora 2 and Kling in one API", None) is None


def test_leaderboards_become_priors_on_the_judge_scale():
    from crowdcode.index_models import parse_leaderboard, priors_rows
    page = """<table><tr><th></th><th>Range</th><th>Model</th><th>Elo</th></tr>
      <tr><td>1</td><td>1-2</td><td>Wan 3.0</td><td>1174±13 95% interval</td></tr>
      <tr><td>2</td><td>1-4</td><td>Utopai X</td><td>1168±13</td></tr>
      <tr><td>3</td><td>3-8</td><td>Dreamina Seedance 2.5</td><td>1156±13</td></tr>
      <tr><td>4</td><td>4-9</td><td>Kling 3.0 1080p (Pro)</td><td>1093±13</td></tr>
      <tr><td>5</td><td>4-9</td><td>Kling 3.0 720p (Pro)</td><td>1080±13</td></tr></table>"""
    priors = parse_leaderboard("text-to-video", "https://aa/video", "elo", page)
    assert [(p.model.model, p.score, p.rank) for p in priors] == [
        ("wan-3", 1174, 1), ("seedance-2.5", 1156, 2), ("kling-v3-pro", 1093, 3)]  # best row per model; unknown names skipped
    # Elo boards: 5 - min(2, gap to the leader / 150); never below 3.
    assert [p.prior_rating for p in priors] == [5.0, 4.88, 4.46]
    far = parse_leaderboard("text-to-video", "u", "elo", page.replace("1093", "700").replace("1080", "690"))
    assert far[-1].prior_rating == 3.0
    llm = parse_leaderboard("llm-intelligence-index", "u", "intelligence_index",
                            "<table><tr><td>GPT-6.2 Astra Max</td><td>62</td></tr><tr><td>Claude Opus 5</td><td>50</td></tr>"
                            "<tr><td>Qwen3.5 Max</td><td>40</td></tr></table>")
    assert [p.prior_rating for p in llm] == [5.0, 4.0, 3.0]  # no Elo: 3 + 2 x percentile
    row = priors_rows(priors, datetime(2026, 11, 18).date())[0]
    assert row[:7] == ("wan-3", "wan", "video", "artificialanalysis:text-to-video", "https://aa/video",
                       datetime(2026, 11, 18).date(), 1174)


def test_every_feed_is_listed_once_mpp_is_tracked_and_models_are_known(db):
    fixture = Fixture()
    o = fixture.origin
    t0 = datetime(2026, 11, 18, 6, 0, tzinfo=UTC)
    tempo_usdc = "0x20c000000000000000000000b9537d11c60e8b50"
    fixture.pages = {
        "/feeds/payai": {"items": [bazaar_item(f"{o}/paid", payers=None), bazaar_item(f"{o}/payai-only")],
                         "pagination": {"total": 2}},
        # thirdweb names no method: it joins the Bazaar's POST resource, not a new GET one.
        "/feeds/thirdweb": {"items": [{"resource": f"{o}/paid", "type": "http", "accepts": bazaar_item(f"{o}/paid")["accepts"],
                                        "metadata": {"source": "coinbase"}}], "pagination": {"total": 1}},
        "/feeds/dexter": {"items": [], "pagination": {"total": 0}},
        "/feeds/mpp2": {"services": [{
            "id": "studio", "name": "Studio", "serviceUrl": f"{o}/studio",
            "endpoints": [
                {"method": "POST", "path": "/api/generate/veo-3.1/generate", "description": "Veo 3.1 video generation",
                 "payment": {"intent": "charge", "method": "tempo", "currency": tempo_usdc}},
                {"method": "POST", "path": "/v1/videos", "description": "Submit video generation",
                 "payment": {"intent": "charge", "method": "tempo", "currency": tempo_usdc}},
                {"method": "GET", "path": "/v1/videos/models", "description": "Video models", "payment": None},
                {"method": "GET", "path": "/v1/videos/:id", "description": "Poll", "payment": None},
            ]}]},
        "/studio/v1/videos/models": {"data": [
            {"id": "google/veo-3.1", "pricing_skus": {"duration_seconds_with_audio": "0.40", "duration_seconds_without_audio": "0.20"}},
            {"id": "kwaivgi/kling-v3.0-pro", "pricing_skus": {"duration_seconds": "0.112"}}]},
    }
    from crowdcode.index_feeds import fetch_dexter, fetch_payai, fetch_thirdweb
    feeds = {
        **fixture.feeds(),
        "payai": lambda c: fetch_payai(c, f"{o}/feeds/payai"),
        "dexter": lambda c: fetch_dexter(c, f"{o}/feeds/dexter"),
        "thirdweb": lambda c: fetch_thirdweb(c, f"{o}/feeds/thirdweb"),
        "mpp": lambda c: fetch_mpp(c, f"{o}/feeds/mpp2"),
    }
    try:
        index.run_sweep(db, t0, feeds=feeds, **OFFLINE)
        with db() as conn:
            paid = conn.execute("select * from index_resources where route_template = %s", (f"{o}/paid",)).fetchall()
            assert len(paid) == 1 and paid[0]["method"] == "POST"
            assert paid[0]["sources"] == ["bazaar", "payai", "thirdweb"]
            assert conn.execute("select sources from index_resources where route_template = %s",
                                (f"{o}/payai-only",)).fetchone()["sources"] == ["payai"]
            studio = {r["route_template"].removeprefix(o): r for r in conn.execute(
                "select * from index_resources where 'mpp' = any(sources)").fetchall()}
            assert set(studio) == {"/studio/api/generate/veo-3.1/generate", "/studio/v1/videos"}  # free routes are not offerings
            assert all(r["tracked"] and r["tracked_reason"] == "curated" for r in studio.values())
            veo = studio["/studio/api/generate/veo-3.1/generate"]
            assert (veo["model"], veo["model_family"], veo["modality"]) == ("veo-3.1", "veo", "video")
            videos = studio["/studio/v1/videos"]
            assert (videos["model"], videos["modality"]) == (None, "video")  # several models, one modality
            offered = conn.execute(
                """
                select r.route_template, m.model, m.seller_model_id, m.source, m.usd_per_second
                from index_offering_models m join index_resources r on r.id = m.resource_id
                where m.modality = 'video' order by m.model, r.route_template
                """).fetchall()
            assert [(r["route_template"].removeprefix(o), r["model"], r["seller_model_id"], r["source"],
                     r["usd_per_second"] and float(r["usd_per_second"])) for r in offered] == [
                ("/studio/v1/videos", "kling-v3-pro", "kwaivgi/kling-v3.0-pro", "models_endpoint", 0.112),
                ("/studio/api/generate/veo-3.1/generate", "veo-3.1", None, "route", None),
                ("/studio/v1/videos", "veo-3.1", "google/veo-3.1", "models_endpoint", 0.2),
            ]
            health = index.health(conn, t0)
            assert health["listed_by_source"]["payai"] == 2 and health["tracked_by_reason"]["curated"] == 2
            assert health["models"]["by_modality"]["video"] == {"offerings": 2, "models": 2}
    finally:
        fixture.server.shutdown()


TREZA_SCHEMA = {"info": {"body": {"model": "minimax-h3", "prompt": "x"}}, "schema": {"properties": {"body": {"properties": {
    "model": {"enum": ["minimax-h3", "veo-3.1-lite", "kling-3.0-pro", "seedance-2.5", "veo-3.1", "not-a-model"],
              "description": "Prices: minimax-h3 (default) 5s $0.42, 10s $0.84; veo-3.1-lite 4s $0.28, 6s $0.42; "
                             "kling-3.0-pro 5s $1.18; seedance-2.5 5s $1.64; veo-3.1 4s $2.24, 6s $3.36."},
    "prompt": {"type": "string"}}}}}}


def test_a_router_s_schema_names_the_models_it_sells_and_their_clip_prices():
    from crowdcode.index_models import schema_models
    found = {m.model: (seller_id, price) for m, seller_id, price in schema_models(TREZA_SCHEMA)}
    assert set(found) == {"hailuo-3", "veo-3.1-lite", "kling-v3-pro", "seedance-2.5", "veo-3.1"}
    # veo-3.1's price is its own, not veo-3.1-lite's.
    assert found["veo-3.1"] == ("veo-3.1", {"clip_seconds": 4, "clip_usd": 2.24})
    assert found["veo-3.1-lite"] == ("veo-3.1-lite", {"clip_seconds": 4, "clip_usd": 0.28})
    assert found["hailuo-3"][0] == "minimax-h3"
    assert schema_models({"schema": {"properties": {"body": {"properties": {"prompt": {}}}}}}) == []


def test_update_models_takes_a_router_s_schema_between_the_catalog_and_the_route(db):
    with db() as conn:
        conn.execute((ROOT / "supabase/offerings-models.sql").read_text())
        conn.execute((ROOT / "supabase/offerings-models.sql").read_text())  # idempotent
        conn.execute(
            """insert into index_resources (resource_key, method, origin, route_template, probe_url, description,
                 input_schema) values ('POST https://treza.example/api/x402/video', 'POST', 'https://treza.example',
                 'https://treza.example/api/x402/video', 'https://treza.example/api/x402/video',
                 'Generate a video. 8 models including Google Veo 3.1, Kling 3.0 and Seedance 2.5', %s::jsonb)""",
            (json.dumps(TREZA_SCHEMA),))
        index.update_models(conn, [], complete=True)
        rows = {r["model"]: r for r in conn.execute(
            "select model, seller_model_id, source, usd_per_second, pricing from index_offering_models").fetchall()}
        assert set(rows) == {"hailuo-3", "veo-3.1-lite", "kling-v3-pro", "seedance-2.5", "veo-3.1"}
        assert {r["source"] for r in rows.values()} == {"schema"}
        assert float(rows["veo-3.1"]["usd_per_second"]) == pytest.approx(0.56)
        assert rows["veo-3.1"]["pricing"] == {"clip_seconds": 4, "clip_usd": 2.24}
        resource = conn.execute("select model, modality from index_resources").fetchone()
        assert (resource["model"], resource["modality"]) == (None, "video")


def test_a_route_crowdcode_hosts_is_payable_described_and_found_once_probed(db):
    """SRI-203: venice-proxy's image route was in the index as a blank row (no name, no description,
    payable false), so the tree, the classifier and the model reader skipped it, and search dropped it as
    unpayable as soon as a probe had quoted it on Base. Every image search missed it."""
    from crowdcode import discovery, offerings

    url = "https://crowdcode.app/s/venice-proxy/image/generate"
    description = ("Images from Venice's image models, uncensored ones included (lustify-v7 for adult "
                   "photorealism). safe_mode defaults to false (unblurred).")
    t0 = datetime(2026, 11, 25, 6, 0, tzinfo=UTC)
    with db() as conn:
        conn.execute(
            "insert into services (id, name, canonical_endpoint, canonical_origin, metadata) values "
            "('svc_venice_image', 'Venice, per request', %s, 'https://crowdcode.app', %s)",
            (url, json.dumps({"crowdcode_service_id": "f00d", "hosted_status": "live",
                              "hosted_route": "POST /image/generate", "description": description})))
        conn.commit()
    index.run_sweep(db, t0, feeds={}, **OFFLINE)
    with db() as conn:
        row = conn.execute("select * from index_resources where route_template = %s", (url,)).fetchone()
        assert row["payable"] and row["service_name"] == "Venice, per request" and row["description"] == description
        assert row["modality"] == "image" and row["tracked_reason"] == "hosted"
        # The probe quotes it on Base, as production's did ($0.012).
        index.record_probes(conn, [(ProbeTarget(row["id"], "POST", url), ProbeResult(
            True, 402, protocol="x402", network=BASE, payee=PAYEE_A, pay_to=[PAYEE_A],
            quoted_amount_atomic=12000, asset=USDC_BASE.lower()))], t0 + timedelta(minutes=5))
        conn.commit()
    # The next hourly sweep keeps it so.
    index.run_sweep(db, t0 + timedelta(hours=1), feeds={}, **OFFLINE)
    with db() as conn:
        found, *_ = offerings.load_offerings(conn)
    result = discovery.find_services("uncensored image generation", index=offerings.OfferingIndex(found),
                                     embedder=None, seed=1)
    assert result["source"] == "index" and result["services"][0]["endpoint"] == url, result
    assert result["services"][0]["networks"] == ["base"] and result["services"][0]["price_usd"] == pytest.approx(0.012)

    # Stopped by its owner: CrowdCode no longer lists it, and nothing says it is payable.
    with db() as conn:
        conn.execute("update services set metadata = metadata || '{\"hosted_status\": \"stopped\"}' "
                     "where id = 'svc_venice_image'")
        conn.commit()
    index.run_sweep(db, t0 + timedelta(hours=2), feeds={}, **OFFLINE)
    with db() as conn:
        assert not conn.execute("select payable from index_resources where route_template = %s", (url,)).fetchone()["payable"]
