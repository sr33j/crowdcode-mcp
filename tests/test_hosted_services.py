"""CrowdCode's hosted services, pushed into the index by CrowdCode (hosted_services.py)."""
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4
import os

import psycopg
from psycopg.rows import dict_row
import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

import crowdcode.server as server
from crowdcode.hosted_services import SyncError, parse_services, sync_hosted_services
from crowdcode.identity import build_identity, create_service_from_identity, resolve_service

SCHEMA = Path(__file__).parents[1] / "supabase/schema.sql"
ORIGIN = "https://crowdcode.app"
PAYEE = "0x" + "ab" * 20
VENICE_ID = "04885f2d-b06c-44f4-babe-ca6193c70b3c"
CROWDCODE_ID = "6c6235b0-fb24-4cfe-88f1-cc9f4c66d299"
GONE_ID = "3bde75d7-25c1-4e7c-abc5-3db30f625bed"


@pytest.fixture
def db():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")
    schema = "hosted_test_" + uuid4().hex
    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
        conn.execute(f"create schema {schema}")
        conn.execute(f"set search_path to {schema}")
        conn.execute(SCHEMA.read_text())
        try:
            yield conn
        finally:
            conn.execute(f"drop schema {schema} cascade")


def _register(conn, endpoint):
    with conn.transaction():
        return create_service_from_identity(conn, build_identity(api_endpoint=endpoint, payment_provider="x402", payment_target_ref=PAYEE)).row


def _service(sid, slug, status="live", routes=(("POST", "/chat/completions"),), aliases=(), name=None):
    return {"id": sid, "slug": slug, "status": status, "aliases": list(aliases), "payee": PAYEE, "name": name or slug,
            "description": f"{slug}, hosted on CrowdCode", "routes": [{"method": m, "path": p} for m, p in routes]}


def _sync(conn, services):
    with conn.transaction():
        return sync_hosted_services(conn, parse_services({"services": services}, ORIGIN), origin=ORIGIN)


def test_a_renamed_service_keeps_its_reviews_and_moves_to_its_current_url(db):
    # Reviewed under its old slug, before the rename (SRI-75).
    old = _register(db, f"{ORIGIN}/s/venice-reseller/chat/completions")
    db.execute("insert into reviews(service_id, rating, reason) values (%s, 4, 'fast and cheap')", (old["id"],))
    stats = _sync(db, [_service(VENICE_ID, "venice-proxy", aliases=["venice-reseller"], name="Venice, per request")])
    assert stats == {"created": 0, "updated": 1, "moved": 1, "deleted": 0, "skipped": 0}
    row = db.execute("select * from services where id = %s", (old["id"],)).fetchone()
    assert row["canonical_endpoint"] == f"{ORIGIN}/s/venice-proxy/chat/completions"
    assert row["name"] == "Venice, per request"
    assert row["metadata"]["crowdcode_service_id"] == VENICE_ID
    assert row["metadata"]["hosted_status"] == "live"
    assert db.execute("select count(*) as n from reviews where service_id = %s", (old["id"],)).fetchone()["n"] == 1
    # A review naming either URL lands on the same row.
    for url in [f"{ORIGIN}/s/venice-proxy/chat/completions", f"{ORIGIN}/s/venice-reseller/chat/completions"]:
        assert resolve_service(db, build_identity(api_endpoint=url, payment_provider="x402", payment_target_ref=PAYEE)).row["id"] == old["id"]
    # Pushing the same list again changes nothing.
    assert _sync(db, [_service(VENICE_ID, "venice-proxy", aliases=["venice-reseller"], name="Venice, per request")])["moved"] == 0


def test_a_live_service_is_listed_without_waiting_for_a_review_and_a_deleted_one_leaves(db, monkeypatch):
    gone = _register(db, f"{ORIGIN}/s/hash-echo/hash")
    stats = _sync(db, [
        _service(CROWDCODE_ID, "crowdcode", routes=[("POST", "/agents"), ("GET", "/")], name="CrowdCode"),
        _service(GONE_ID, "hash-echo", status="deleted", routes=[("GET", "/hash")]),
        # Never reviewed and not live: nothing to list.
        _service(str(uuid4()), "stopped-one", status="stopped"),
    ])
    assert stats["created"] == 2 and stats["skipped"] == 1
    rows = db.execute("select canonical_endpoint, metadata->>'hosted_status' as status from services order by canonical_endpoint").fetchall()
    assert [(r["canonical_endpoint"], r["status"]) for r in rows] == [
        (f"{ORIGIN}/s/crowdcode", "live"), (f"{ORIGIN}/s/crowdcode/agents", "live"), (f"{ORIGIN}/s/hash-echo/hash", "deleted")]

    @contextmanager
    def connect():
        yield db
    monkeypatch.setattr(server, "connect", connect)
    listed = server._top_services_payload(None)["services"]
    assert sorted(s["canonical_endpoint"] for s in listed) == [f"{ORIGIN}/s/crowdcode", f"{ORIGIN}/s/crowdcode/agents"]
    assert {s["crowdcode_service_id"] for s in listed} == {CROWDCODE_ID}
    assert gone["id"] not in {s["service_id"] for s in listed}
    detail, status = server._service_detail_payload(listed[0]["service_id"])
    assert status == 200 and detail["service"]["crowdcode_service_id"] == CROWDCODE_ID

    # A hosted service no longer on CrowdCode's list leaves too; its reviews stay.
    assert _sync(db, [])["deleted"] == 2
    assert server._top_services_payload(None)["services"] == []


def test_the_pushed_list_is_checked_and_urls_are_built_from_slugs(db):
    for bad in [{"services": "x"}, {"services": [{"id": "nope", "slug": "a-b", "status": "live"}]},
                {"services": [{"id": VENICE_ID, "slug": "Bad Slug", "status": "live"}]},
                {"services": [{"id": VENICE_ID, "slug": "ok-slug", "status": "running"}]},
                {"services": [{"id": VENICE_ID, "slug": "ok-slug", "status": "live", "payee": "https://evil.example"}]}]:
        with pytest.raises(SyncError):
            parse_services(bad, ORIGIN)
    [parsed] = parse_services({"services": [{**_service(VENICE_ID, "ok-slug"), "url": "https://evil.example/s/x",
        "routes": [{"method": "POST", "path": "/x?y=1"}, {"method": "DELETE", "path": "/x"}, {"method": "get", "path": "/ok/"}]}]}, ORIGIN)
    assert parsed["base"] == f"{ORIGIN}/s/ok-slug"
    assert parsed["routes"] == [("GET", "/ok", None)]


def test_the_endpoint_needs_the_configured_token(monkeypatch):
    app = Starlette(routes=[Route("/api/internal/hosted-services/sync", server.hosted_services_sync, methods=["POST"])])
    client = TestClient(app)
    monkeypatch.delenv("CROWDCODE_HOSTED_SYNC_TOKEN", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    server.get_settings.cache_clear() if hasattr(server.get_settings, "cache_clear") else None
    assert client.post("/api/internal/hosted-services/sync", json={"services": []}).status_code == 404
    monkeypatch.setenv("CROWDCODE_HOSTED_SYNC_TOKEN", "s3cret-token")
    server.get_settings.cache_clear() if hasattr(server.get_settings, "cache_clear") else None
    assert client.post("/api/internal/hosted-services/sync", json={"services": []}).status_code == 401
    assert client.post("/api/internal/hosted-services/sync", json={"services": []}, headers={"authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/api/internal/hosted-services/sync", json={"services": "x"}, headers={"authorization": "Bearer s3cret-token"}).status_code == 400


def test_each_route_is_listed_under_its_own_description(db):
    # SRI-203: an image route under the service's text read like its chat route, and was matched as chat.
    service = _service(VENICE_ID, "venice-proxy", routes=[("POST", "/chat/completions"), ("POST", "/image/generate")])
    service["routes"][1]["description"] = "Images from Venice's image models, uncensored ones included."
    assert _sync(db, [service])["created"] == 2
    rows = {r["canonical_endpoint"]: r["metadata"]["description"] for r in db.execute("select canonical_endpoint, metadata from services").fetchall()}
    assert rows == {f"{ORIGIN}/s/venice-proxy/chat/completions": "venice-proxy, hosted on CrowdCode",
                    f"{ORIGIN}/s/venice-proxy/image/generate": "Images from Venice's image models, uncensored ones included."}
