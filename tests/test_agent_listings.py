"""Agent services are listed under their own name and description, not "crowdcode.app"."""
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4
import os

import httpx
import psycopg
from psycopg.rows import dict_row
import pytest

from crowdcode import server
from crowdcode.agent_listings import (
    DESCRIPTION_MAX,
    NAME_MAX,
    agent_service_slug,
    fetch_listing,
    parse_manifest,
    refresh_agent_listings,
)
from crowdcode.identity import build_identity, create_service_from_identity

SCHEMA = Path(__file__).parents[1] / "supabase/schema.sql"
PAYEE = "0x" + "ab" * 20
ORIGIN = "https://crowdcode.app"
VENICE = {
    "name": "Venice, per request",
    "description": "Venice's open-weight models, paid per request in USDC on Base with x402.",
    "url": "https://crowdcode.app/s/venice-reseller",
    "routes": [],
}


@pytest.mark.parametrize(
    "endpoint, slug",
    [
        ("https://crowdcode.app/s/venice-reseller/chat/completions", "venice-reseller"),
        ("https://www.crowdcode.app/s/text-stats/stats", "text-stats"),
        ("https://crowdcode.app/s/sweep-wordcount", "sweep-wordcount"),
        ("https://CROWDCODE.app:443/s/abc/x", "abc"),
        ("http://crowdcode.app/s/venice-reseller/chat", None),
        ("https://crowdcode.app.evil.example/s/venice-reseller", None),
        ("https://evil.example/s/venice-reseller", None),
        ("https://crowdcode.app:8443/s/venice-reseller", None),
        ("https://crowdcode.app/x402/venice/chat/completions", None),
        ("https://crowdcode.app/s/UPPER/x", None),
        ("https://crowdcode.app/s/ab", None),
        (None, None),
    ],
)
def test_recognises_only_agent_service_endpoints(endpoint, slug):
    assert agent_service_slug(endpoint, ORIGIN) == slug


def test_reads_the_manifest_of_the_service_asked_for_and_nothing_else():
    assert parse_manifest("venice-reseller", VENICE, ORIGIN) == {
        "name": "Venice, per request",
        "description": VENICE["description"],
    }
    # A manifest that is some other service's, or none at all, is not a listing.
    assert parse_manifest("text-stats", VENICE, ORIGIN) is None
    assert parse_manifest("venice-reseller", {**VENICE, "url": "https://evil.example/s/venice-reseller"}, ORIGIN) is None
    assert parse_manifest("venice-reseller", {**VENICE, "name": "  "}, ORIGIN) is None
    assert parse_manifest("venice-reseller", ["not", "a", "manifest"], ORIGIN) is None
    # One line of printable text, bounded.
    noisy = parse_manifest("venice-reseller", {**VENICE, "name": "Venice\n‮\tper\x00request" + "x" * 300,
                                                "description": "d" * 5000}, ORIGIN)
    assert "\n" not in noisy["name"] and "\x00" not in noisy["name"] and "‮" not in noisy["name"]
    assert noisy["name"].startswith("Venice per request")
    assert len(noisy["name"]) == NAME_MAX and len(noisy["description"]) == DESCRIPTION_MAX
    assert "description" not in parse_manifest("venice-reseller", {**VENICE, "description": None}, ORIGIN)


def test_fetches_the_free_manifest_and_treats_any_other_answer_as_none():
    answers = {
        "/s/venice-reseller": httpx.Response(200, json=VENICE),
        "/s/stopped-one": httpx.Response(503, json={"error": "service_unavailable"}),
        "/s/paid-root": httpx.Response(402, json={}),
        "/s/garbled": httpx.Response(200, text="<html>"),
    }
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return answers[request.url.path]

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert fetch_listing("venice-reseller", origin=ORIGIN, client=client)["name"] == "Venice, per request"
        for slug in ("stopped-one", "paid-root", "garbled"):
            assert fetch_listing(slug, origin=ORIGIN, client=client) is None
    assert seen[0] == "https://crowdcode.app/s/venice-reseller"


@pytest.fixture
def db():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")
    schema = "listings_test_" + uuid4().hex
    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
        conn.execute(f"create schema {schema}")
        conn.execute(f"set search_path to {schema}")
        conn.execute(SCHEMA.read_text())
        try:
            yield conn
        finally:
            conn.execute(f"drop schema {schema} cascade")


def _register(conn, endpoint, **extra):
    with conn.transaction():
        return create_service_from_identity(
            conn, build_identity(api_endpoint=endpoint, payment_provider="x402", payment_target_ref=PAYEE, **extra)
        ).row


def test_an_agent_service_is_listed_under_its_own_name_and_description(db, monkeypatch):
    venice = _register(db, "https://crowdcode.app/s/venice-reseller/chat/completions")
    stopped = _register(db, "https://crowdcode.app/s/stopped-one/run")
    # Registered before this change, under the host every agent service shares.
    legacy = _register(db, "https://crowdcode.app/s/text-stats/stats")
    db.execute("update services set name = 'crowdcode.app' where id = %s", (legacy["id"],))
    elsewhere = _register(db, "https://api.other.example/v1/search")
    with db.transaction():
        curated = create_service_from_identity(db, build_identity(
            api_endpoint="https://crowdcode.app/s/curated-one/x", payment_provider="x402",
            payment_target_ref="0x" + "cd" * 20, directory_slug="curated", service_name="Curated name")).row

    # Before anything is fetched it is at least its own slug, not the shared host.
    assert venice["name"] == "venice-reseller"
    assert stopped["name"] == "stopped-one"
    assert elsewhere["name"] == "api.other.example"

    fetched = []

    def fetch(slug):
        fetched.append(slug)
        return parse_manifest(slug, VENICE, ORIGIN) if slug == "venice-reseller" else None

    stats = refresh_agent_listings(db, fetch=fetch, origin=ORIGIN)
    assert sorted(fetched) == ["stopped-one", "text-stats", "venice-reseller"]
    assert stats == {"candidates": 3, "updated": 2, "unchanged": 0, "unavailable": 2}
    rows = {r["id"]: r for r in db.execute("select id, name, metadata from services").fetchall()}
    assert rows[venice["id"]]["name"] == "Venice, per request"
    assert rows[venice["id"]]["metadata"]["description"] == VENICE["description"]
    assert rows[venice["id"]]["metadata"]["listing_source"] == "crowdcode-agent-service"
    # A service with nothing to say keeps what it had; others are not touched.
    assert rows[stopped["id"]]["name"] == "stopped-one"
    # One with nothing to say that was still named after the host gets its slug.
    assert rows[legacy["id"]]["name"] == "text-stats"
    assert rows[elsewhere["id"]]["name"] == "api.other.example"
    assert rows[curated["id"]]["name"] == "Curated name"

    # Idempotent, and a redeploy's new name follows.
    assert refresh_agent_listings(db, fetch=fetch, origin=ORIGIN)["unchanged"] == 1
    renamed = {**VENICE, "name": "Venice models, per request", "description": None}
    refresh_agent_listings(db, fetch=lambda slug: parse_manifest(slug, renamed, ORIGIN), origin=ORIGIN,
                           service_ids=[venice["id"]])
    row = db.execute("select name, metadata from services where id = %s", (venice["id"],)).fetchone()
    assert row["name"] == "Venice models, per request" and "description" not in row["metadata"]

    # The public read API carries the description beside the name.
    refresh_agent_listings(db, fetch=fetch, origin=ORIGIN)

    @contextmanager
    def connect():
        yield db

    monkeypatch.setattr(server, "connect", connect)
    listed = {s["service_id"]: s for s in server._top_services_payload(None)["services"]}
    assert listed[venice["id"]]["name"] == "Venice, per request"
    assert listed[venice["id"]]["description"] == VENICE["description"]
    assert listed[elsewhere["id"]]["description"] is None
    detail, status = server._service_detail_payload(venice["id"])
    assert status == 200
    assert detail["service"]["name"] == "Venice, per request"
    assert detail["service"]["description"] == VENICE["description"]
