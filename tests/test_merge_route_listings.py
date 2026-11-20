"""SRI-135: per-job listings are folded into their route, keeping every review.

Reproduces production on 2026-11-20: BlockRun's video listing (the submit
endpoint, from the index, no reviews) and seven listings, one per clip
collected, each with the one review its purchase got.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from crowdcode import reliability
from crowdcode.identity import build_identity, resolve_service
from scripts import merge_route_listings as merge
from tests.test_shared_wallet_db import db  # noqa: F401 - fixture

SUBMIT = "https://blockrun.ai/api/v1/videos/generations"
PAYEE = "0x" + "ab" * 20
JOBS = [f"v1_{seed}KAoQPb2qLgY324q5iQGvatY6grOPvcyPgNiDe9pMj9C0q5B0RUP7H_G4jIhr" for seed in ("iK", "0M", "MJ", "CI", "qb", "KH", "sC")]


def _seed(conn):
    conn.execute("insert into services(id,name,canonical_origin,canonical_endpoint) values('svc_submit','BlockRun.AI /api/v1/videos/generations','https://blockrun.ai',%s)", (SUBMIT,))
    for index, job in enumerate(JOBS):
        service = f"svc_job{index}"
        conn.execute("""insert into services(id,name,canonical_origin,canonical_endpoint,payment_provider,payment_target_ref,created_from_review)
            values(%s,'blockrun.ai','https://blockrun.ai',%s,'x402',%s,true)""", (service, f"{SUBMIT}/{job}", PAYEE))
        conn.execute("insert into service_identifiers(service_id,identifier_type,identifier_value) values(%s,'api_endpoint',%s)", (service, f"{SUBMIT}/{job}"))
        conn.execute("insert into service_identifiers(service_id,identifier_type,identifier_value) values(%s,'payment_target',%s)", (service, f"x402:{PAYEE}"))
        conn.execute("insert into reviews(service_id,rating,reason,payment_reference) values(%s,5,'Returned a 10-second video.',%s)", (service, "0x" + f"{index:02x}" * 32))
    # One unrelated listing whose lower-case slug is a service, not a record.
    conn.execute("insert into services(id,name,canonical_endpoint) values('svc_market','m','https://payanagent.com/x402/kh71792ee7x7w59dtrgbq7zz898an9cn')")


def _connect(conn):
    @contextmanager
    def connect():
        yield conn
    return connect


def test_folds_every_clip_listing_into_the_video_endpoint_keeping_its_reviews(db, monkeypatch):
    conn, _, _ = db
    _seed(conn)
    monkeypatch.setattr(merge, "connect", _connect(conn))
    monkeypatch.setattr(reliability, "recompute", lambda *a, **k: {})
    planned = merge.plan(conn)
    assert planned == [{"route": f"{SUBMIT}/{{id}}", "submit": SUBMIT, "target": "svc_submit", "target_endpoint": SUBMIT,
                        "rename_to": None, "merged": [f"svc_job{i}" for i in range(7)],
                        "merged_endpoints": [f"{SUBMIT}/{job}"[:120] for job in JOBS]}]
    # A dry run writes nothing.
    assert merge.main_with([]) == 0
    assert conn.execute("select count(*) as n from services").fetchone()["n"] == 9
    assert merge.main_with(["--apply"]) == 0
    assert [r["id"] for r in conn.execute("select id from services order by id").fetchall()] == ["svc_market", "svc_submit"]
    assert conn.execute("select count(*) as n, min(service_id) as s, max(service_id) as t from reviews").fetchone() == {"n": 7, "s": "svc_submit", "t": "svc_submit"}
    # The next review of any clip, or of the submit, resolves to the one listing.
    for endpoint in (f"{SUBMIT}/{JOBS[0]}", f"{SUBMIT}/v1_NewJobAbc123{'x' * 30}", SUBMIT):
        resolved = resolve_service(conn, build_identity(api_endpoint=endpoint, payment_provider="x402", payment_target_ref=PAYEE))
        assert resolved.error is None and resolved.row["id"] == "svc_submit", endpoint
    # The gateway's reliability lookup for a new job's collect URL finds it too.
    conn.execute((Path(__file__).parents[1] / "supabase/services-index.sql").read_text())
    assert reliability._service_for_url(conn, f"{SUBMIT}/v1_NewJobAbc123{'x' * 30}", "GET")[0] == "svc_submit"
    # Run again: nothing left to fold.
    assert merge.plan(conn) == []


def test_a_single_record_listing_is_renamed_to_its_route(db, monkeypatch):
    conn, _, _ = db
    conn.execute("insert into services(id,name,canonical_endpoint) values('svc_chart','c','https://astro.dutchdata.io/api/full_chart/dt/1779285600000')")
    conn.execute("insert into reviews(service_id,rating,reason,payment_reference) values('svc_chart',4,'ok','0x' || repeat('cd',32))")
    monkeypatch.setattr(merge, "connect", _connect(conn))
    monkeypatch.setattr(reliability, "recompute", lambda *a, **k: {})
    assert merge.main_with(["--apply"]) == 0
    row = conn.execute("select canonical_endpoint from services where id='svc_chart'").fetchone()
    assert row["canonical_endpoint"] == "https://astro.dutchdata.io/api/full_chart/dt/{id}"
    resolved = resolve_service(conn, build_identity(api_endpoint="https://astro.dutchdata.io/api/full_chart/dt/1779372000000"))
    assert resolved.row["id"] == "svc_chart"
