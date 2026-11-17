"""reliability-v2 (SRI-115 C10): the prior, the posterior, quality, the one score
and every tier, then the stored results, the url lookup and the block list
against PostgreSQL."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from crowdcode import reliability
from crowdcode.reliability import evaluate, prior_for, quality_for

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
PAYEE = "0x6d6e695b09861467c7d462f5aaf31cf3540b9192"
OTHER = "0xb98ef29eb2be19ae646a8fc0248255b90a332dbc"
STRANGER = "0x1111111111111111111111111111111111111111"


def iso(dt: datetime) -> str:
    return dt.isoformat()


def probe(days_ago: float, ok: bool = True, error_code: str | None = None, status: int | None = None) -> dict:
    return {"at": iso(NOW - timedelta(days=days_ago)), "ok": ok, "http_status": status or (402 if ok else 503),
            "error_code": None if ok else (error_code or "http_503"), "quoted_amount_atomic": "7000" if ok else None,
            "asset": USDC if ok else None}


def endpoint(**over) -> dict:
    ep = {
        "resource_id": 1, "method": "POST", "url": "https://api.example.com/search",
        "last_probes": [probe(0.1), probe(1.1), probe(2.1)],
        "last_paid_ok_at": None,
        "latest_price_atomic": "7000", "latest_price_asset": USDC, "median_price_7d_atomic": "7000",
        "payees": [PAYEE], "payee_since": iso(NOW - timedelta(days=30)),
        "payee_changes_7d": [], "bazaar_payers_30d": 147, "bazaar_calls_30d": 13321, "curated": False, "models": [],
    }
    ep.update(over)
    return ep


def offering(**over) -> dict:
    """A popular offering with no judged purchases yet."""
    inputs = {
        "as_of": iso(NOW), "tracked": True, "blocked": None, "payee_rotates": False,
        "registered_payees": [{"payee": PAYEE, "since": iso(NOW - timedelta(days=60))}],
        "endpoints": [endpoint()],
        "outside": {"bazaar_payers_30d": 147, "bazaar_calls_30d": 13321, "curated": False},
        "judged_30d": judged(0, 0),
        "failures_30d": {"total": 0, "test": 0, "by": {}},
        "quality": {"v31_score": None, "n_eff": 0, "prior_rating": 3, "prior_source": "the default prior"},
        "external_payers_30d": 0,
    }
    inputs.update(over)
    return inputs


def judged(delivered: int, not_delivered: int, **extra) -> dict:
    return {"delivered": delivered, "not_delivered": not_delivered, "unresolved": 0, "reread": 0, "test": 0,
            "unlabelled": 0, "owner_buyers": 1, "last_at": iso(NOW - timedelta(days=1)), **extra}


def tier(inputs: dict) -> str:
    return evaluate(inputs)["tier"]


# --- The prior -------------------------------------------------------------------------


def test_the_prior_comes_from_outside_evidence():
    strong = prior_for({"bazaar_payers_30d": 10, "bazaar_calls_30d": 15})
    assert (strong["alpha"], strong["beta"], strong["kind"]) == (8, 1, "bazaar_strong")
    assert strong["source"] == "10 payers on the x402 Bazaar in 30 days, 1.5 calls per payer"
    # Ten payers who mostly called once: still >= 3 payers, so Beta(3,1), not Beta(8,1).
    low_repeat = prior_for({"bazaar_payers_30d": 10, "bazaar_calls_30d": 14})
    assert (low_repeat["alpha"], low_repeat["beta"], low_repeat["kind"]) == (3, 1, "bazaar")
    assert prior_for({"bazaar_payers_30d": 3, "bazaar_calls_30d": 3})["kind"] == "bazaar"
    assert prior_for({"bazaar_payers_30d": 2, "bazaar_calls_30d": 50})["kind"] == "none"
    curated = prior_for({"curated": True})
    assert (curated["alpha"], curated["beta"], curated["source"]) == (3, 1, "listed in mpp.dev's curated catalog")
    none = prior_for({})
    assert (none["alpha"], none["beta"], none["source"]) == (1, 1, "no outside evidence")


def test_quality_is_the_v31_score_reshrunk_to_the_models_prior():
    # v3.1 = (sum(w*r) + 2*3) / (n_eff + 2). Ratings of 5 over n_eff 2: v3.1 = 4.0.
    assert quality_for({"v31_score": 4.0, "n_eff": 2, "prior_rating": 3}) == 4
    # The same ratings shrunk to a leaderboard prior of 4.6 instead: (2*4.6 + 2*5) / 4 = 4.8.
    assert float(quality_for({"v31_score": 4.0, "n_eff": 2, "prior_rating": 4.6})) == pytest.approx(4.8)
    assert float(quality_for({"v31_score": None, "n_eff": 0, "prior_rating": 4.2})) == pytest.approx(4.2)
    assert quality_for({}) == 3


# --- Tiers -----------------------------------------------------------------------------


def test_popular_needs_a_strong_or_curated_prior_and_healthy_probes():
    r = evaluate(offering())
    assert (r["tier"], r["state"]) == ("popular", "reliable")
    assert r["reasons"][0] == ("popular: 147 payers on the x402 Bazaar in 30 days, 90.6 calls per payer, "
                               "and its probes are healthy")
    assert r["posterior"]["alpha"] == 8 and r["posterior"]["beta"] == 1
    assert r["reasons"][1] == ("delivery estimate Beta(8, 1), mean 0.89: prior Beta(8, 1) (147 payers on the "
                               "x402 Bazaar in 30 days, 90.6 calls per payer), plus 0 delivered and 0 not "
                               "delivered judged purchases and 0 paid calls that failed before settlement in 30 days")
    curated = offering(outside={"bazaar_payers_30d": None, "bazaar_calls_30d": None, "curated": True})
    assert tier(curated) == "popular"
    # Healthy means the latest conclusive probe was a valid 402.
    failing = offering(endpoints=[endpoint(last_probes=[probe(0.1, ok=False), probe(1.1), probe(2.1)])])
    r = evaluate(failing)
    assert r["tier"] == "new" and r["reasons"][0].startswith("new: 147 payers on the x402 Bazaar in 30 days, 90.6 "
                                                             "calls per payer, but its probes are not healthy")
    unprobed = evaluate(offering(endpoints=[endpoint(last_probes=[])]))
    assert unprobed["tier"] == "new"
    assert unprobed["reasons"][0].startswith("new: 147 payers on the x402 Bazaar in 30 days, 90.6 calls per payer, "
                                             "but it has not been probed yet (probes run once a day)")
    rejected = probe(0.1, ok=False, error_code="probe_body_rejected", status=400)
    assert tier(offering(endpoints=[endpoint(last_probes=[rejected, probe(1.1)])])) == "popular"


def test_new_is_everything_healthy_without_strong_evidence():
    r = evaluate(offering(outside={"bazaar_payers_30d": 4, "bazaar_calls_30d": 4, "curated": False}))
    assert (r["tier"], r["state"]) == ("new", "unverified")
    assert r["reasons"][0] == ("new: 4 payers on the x402 Bazaar in 30 days, 1.0 calls per payer; popular needs 10 "
                               "or more Bazaar payers with 1.5 calls per payer, or mpp.dev's curated catalog; proven "
                               "needs 5 judged purchases with a mean of 0.85 (0 so far)")


def test_proven_needs_five_judged_and_a_mean_of_085():
    weak = {"bazaar_payers_30d": 0, "bazaar_calls_30d": 0, "curated": False}  # Beta(1,1)
    assert tier(offering(outside=weak, judged_30d=judged(5, 0))) == "proven"  # Beta(6,1): 0.857
    assert tier(offering(outside=weak, judged_30d=judged(4, 0))) == "new"  # only 4 judged
    r = evaluate(offering(outside=weak, judged_30d=judged(6, 1)))  # Beta(7,2): 0.78
    assert r["tier"] == "new"
    r = evaluate(offering(judged_30d=judged(5, 0)))  # Beta(13,1)
    assert (r["tier"], r["state"]) == ("proven", "reliable")
    assert r["reasons"][0] == ("proven: 5 of 5 judged purchases in 30 days delivered, and the delivery "
                               "estimate's mean 0.93 is at least 0.85")


def test_seller_failures_before_settlement_count_against_delivery():
    weak = {"bazaar_payers_30d": 0, "bazaar_calls_30d": 0, "curated": False}
    ok = offering(outside=weak, judged_30d=judged(6, 0))  # Beta(7,1): proven
    assert tier(ok) == "proven"
    failing = deepcopy(ok)
    failing["failures_30d"] = {"total": 1, "test": 1, "by": {"seller_error": 1}}  # Beta(7,2): 0.78
    r = evaluate(failing)
    assert r["tier"] == "new" and r["posterior"]["beta"] == 2 and r["posterior"]["failed_calls"] == 1
    assert "1 paid call that failed before settlement in 30 days (1 of the failed calls were CrowdCode test purchases)" in r["reasons"][1]


def test_unresolved_partials_count_neither_way_and_rereads_are_labelled():
    r = evaluate(offering(judged_30d=judged(4, 0, unresolved=9, reread=3, test=2, unlabelled=1)))
    assert r["posterior"]["alpha"] == 12 and r["posterior"]["beta"] == 1
    assert ("(2 of the judged purchases were CrowdCode test purchases; 1 from before test purchases were labelled; "
            "3 old partial verdicts re-read under the new rubric; 9 partial verdicts not counted until they are re-read)"
            ) in r["reasons"][1]


def test_the_one_score_is_delivery_times_quality():
    r = evaluate(offering(judged_30d=judged(10, 0),
                          quality={"v31_score": 4.4, "n_eff": 3, "prior_rating": 3, "prior_source": "the default prior"}))
    # Beta(18, 1) = 0.947; quality 4.4: score 4.17.
    assert r["quality"] == pytest.approx(4.4)
    assert r["score"] == pytest.approx(18 / 19 * 4.4, abs=1e-4)
    assert ("score 4.17 = delivery 0.95 × quality 4.40: judge ratings 4.40 over 3.0 weighted reviews (review score "
            "v3.1), shrunk to 3.0 from the default prior") in r["reasons"]
    model = offering(quality={"v31_score": None, "n_eff": 0, "prior_rating": 4.6,
                              "prior_source": "veo-3.1 on Artificial Analysis video arena (2026-11-17)"})
    r = evaluate(model)
    assert r["quality"] == pytest.approx(4.6)
    assert ("score 4.09 = delivery 0.89 × quality 4.60: no weighted ratings yet, shrunk to 4.6 from veo-3.1 on "
            "Artificial Analysis video arena (2026-11-17)") in r["reasons"]


# --- bad ---------------------------------------------------------------------------------


def test_blocked_is_bad_with_the_operators_note_first():
    r = evaluate(offering(judged_30d=judged(50, 0), blocked={"note": "pays out to a wallet reported for fraud",
                                                             "by": "ops", "at": iso(NOW - timedelta(hours=2))}))
    assert (r["tier"], r["state"]) == ("bad", "unreliable")
    assert r["reasons"][0] == "blocked by a CrowdCode operator on 2026-11-20: pays out to a wallet reported for fraud"


def test_dead_is_bad_unless_a_paid_call_succeeded_since():
    failed = [probe(0.1, ok=False), probe(1.1, ok=False, error_code="timeout"), probe(2.1, ok=False, error_code="http_404")]
    r = evaluate(offering(endpoints=[endpoint(last_probes=failed)]))
    assert r["tier"] == "bad"
    assert r["reasons"][0] == "the last 3 daily probes all failed (http_503, timeout, http_404; latest 2026-11-20 09:36 UTC)"
    alive = endpoint(last_probes=failed, last_paid_ok_at=iso(NOW - timedelta(days=1)))
    r = evaluate(offering(endpoints=[alive]))
    assert r["tier"] == "new"
    assert any("but a paid call succeeded on 2026-11-19 12:00 UTC, so it is not counted as down" in x for x in r["reasons"])
    # Inconclusive probes never make it dead.
    rejected = [probe(d, ok=False, error_code="probe_body_rejected", status=400) for d in (0.1, 1.1, 2.1)]
    r = evaluate(offering(endpoints=[endpoint(last_probes=rejected)]))
    assert r["tier"] == "new" and any("probes inconclusive" in x for x in r["reasons"])


def test_a_price_above_twice_its_median_is_bad():
    assert tier(offering(endpoints=[endpoint(latest_price_atomic="14000")])) == "popular"
    r = evaluate(offering(endpoints=[endpoint(latest_price_atomic="14001")]))
    assert r["tier"] == "bad"
    assert r["reasons"][0] == "latest price $0.014001 is more than 2× its 7-day median of $0.007"


def test_payee_changes_to_unadvertised_addresses_are_bad_and_advertised_or_rotating_ones_are_not():
    change = {"at": iso(NOW - timedelta(days=2)), "from": [PAYEE], "to": [STRANGER]}
    r = evaluate(offering(endpoints=[endpoint(payees=[STRANGER], payee_changes_7d=[change])]))
    assert r["tier"] == "bad"
    assert r["reasons"][0] == (f"payee changed on 2026-11-18 12:00 UTC to {STRANGER}, "
                               "which the service's own 402 had not advertised before")
    old = dict(change, at=iso(NOW - timedelta(days=8)))
    assert tier(offering(endpoints=[endpoint(payees=[STRANGER], payee_changes_7d=[old])])) == "popular"
    registered = [{"payee": PAYEE, "since": iso(NOW - timedelta(days=60))},
                  {"payee": OTHER, "since": iso(NOW - timedelta(days=30))}]
    added = {"at": iso(NOW - timedelta(days=2)), "from": [PAYEE], "to": [OTHER, PAYEE]}
    assert tier(offering(registered_payees=registered, endpoints=[endpoint(payee_changes_7d=[added])])) == "popular"
    late = [registered[0], {"payee": OTHER, "since": iso(NOW - timedelta(days=1))}]
    assert tier(offering(registered_payees=late, endpoints=[endpoint(payee_changes_7d=[added])])) == "bad"
    r = evaluate(offering(payee_rotates=True, endpoints=[endpoint(payee_changes_7d=[change])]))
    assert r["tier"] == "popular"
    assert "the payee rotates per request (confirmed by an operator), so payee changes are not counted" in r["reasons"]


def test_low_delivery_is_bad_once_there_are_four_judged():
    weak = {"bazaar_payers_30d": 0, "bazaar_calls_30d": 0, "curated": False}
    r = evaluate(offering(outside=weak, judged_30d=judged(1, 3)))  # Beta(2,4): 0.33
    assert r["tier"] == "bad"
    assert "delivery estimate mean 0.33 is below 0.5 after 4 judged purchases" in r["reasons"]
    assert tier(offering(outside=weak, judged_30d=judged(0, 3))) == "new"  # 3 judged: too few
    # The prior counts: Beta(8,1) + 2 delivered, 2 not delivered = Beta(10,3), 0.77.
    assert tier(offering(judged_30d=judged(2, 2))) == "popular"


# --- untracked, several endpoints, limits ------------------------------------------------


def test_untracked_is_new_unless_blocked():
    r = evaluate({"as_of": iso(NOW), "tracked": False, "endpoints": []})
    assert (r["tier"], r["state"], r["score"]) == ("new", "unverified", 1.5)
    assert r["reasons"][1] == reliability.NOT_TRACKED or reliability.NOT_TRACKED in r["reasons"]
    r = evaluate({"as_of": iso(NOW), "tracked": False, "endpoints": [], "blocked": {"note": "fixture", "at": iso(NOW)}})
    assert r["tier"] == "bad" and r["reasons"][0] == "blocked by a CrowdCode operator on 2026-11-20: fixture"


def test_several_endpoints_dead_only_if_all_are_and_popular_only_if_all_healthy():
    down = [probe(0.1, ok=False), probe(1.1, ok=False), probe(2.1, ok=False)]
    second = endpoint(resource_id=2, url="https://api.example.com/contents", last_probes=down)
    assert tier(offering(endpoints=[endpoint(), second])) == "new"
    assert tier(offering(endpoints=[endpoint(last_probes=down), second])) == "bad"


def test_reasons_are_bounded_strings_and_the_rule_is_deterministic():
    many = offering(endpoints=[endpoint(resource_id=i, url=f"https://api.example.com/{i}") for i in range(12)])
    r = evaluate(many)
    assert len(r["reasons"]) == reliability.MAX_REASONS
    assert all(isinstance(x, str) and len(x) < 400 for x in r["reasons"])
    copy = deepcopy(many)
    assert evaluate(copy) == r and copy == many  # pure: inputs untouched


# --- PostgreSQL ------------------------------------------------------------------------


@pytest.fixture
def db():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")
    schema = "reliability_test_" + uuid4().hex
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(f"create schema {schema}")
        admin.execute(f"set search_path to {schema}")
        admin.execute((ROOT / "supabase/schema.sql").read_text())
        admin.execute((ROOT / "supabase/services-index.sql").read_text())
        admin.execute((ROOT / "supabase/observations-and-requests-v3.sql").read_text())
        admin.execute((ROOT / "supabase/rotating-payees.sql").read_text())
        # Idempotent: applying it twice changes nothing.
        admin.execute((ROOT / "supabase/reliability.sql").read_text())
        admin.execute((ROOT / "supabase/reliability.sql").read_text())

    @contextmanager
    def connect():
        with psycopg.connect(url, row_factory=dict_row, options=f"-c search_path={schema}") as conn:
            yield conn

    try:
        yield connect
    finally:
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(f"drop schema {schema} cascade")


def _w1_w2_columns(conn) -> None:
    """What W1 (C9 models) and W2 (C11 re-reads, failure records) add, as they named it."""
    conn.execute("""
        alter table reviews add column if not exists delivery_reread text, add column if not exists delivery_reread_note text;
        alter table index_observations add column if not exists attempt_ref text, add column if not exists traffic text;
        create table if not exists index_offering_models (resource_id bigint, model text, model_family text,
          modality text, seller_model_id text, source text, pricing jsonb, usd_per_second numeric,
          updated_at timestamptz, primary key (resource_id, model));
        create table if not exists model_quality_priors (model text primary key, model_family text, modality text,
          source text, source_url text, as_of date, elo numeric, rank int, prior_rating numeric);
    """)


def _seed(conn, *, v2: bool = False) -> None:
    conn.execute("""
        insert into services (id, name, canonical_endpoint, canonical_origin, payment_provider, payment_target_ref, score, n_eff) values
          ('svc_exa', 'Exa search', 'https://api.exa.ai/search', 'https://api.exa.ai', 'x402', %(payee)s, 4.4, 3),
          ('svc_new', 'New', 'https://new.example.com/q', 'https://new.example.com', 'x402', %(payee)s, 3.0, 0),
          ('svc_hosted', 'Renamed hosted', 'https://crowdcode.app/s/new-name', 'https://crowdcode.app', 'x402', %(payee)s, 3.0, 0),
          ('svc_fixture', 'Blocked fixture', 'https://fixture.example.com/x', 'https://fixture.example.com', 'x402', %(payee)s, 3.0, 0),
          ('svc_video', 'Video seller', 'https://video.example.com/veo', 'https://video.example.com', 'x402', %(payee)s, 3.0, 0)
    """, {"payee": PAYEE})
    conn.execute("""
        insert into service_identifiers (service_id, identifier_type, identifier_value) values
          ('svc_exa', 'payment_target', 'x402:' || %(other)s),
          ('svc_hosted', 'api_endpoint', 'https://crowdcode.app/s/old-name')
    """, {"other": OTHER})
    for rid, sid, url, tracked, payers, calls, sources in (
            (1, "svc_exa", "https://api.exa.ai/search", True, 147, 13321, ["bazaar", "gateway"]),
            (2, "svc_new", "https://new.example.com/q", True, 4, 4, ["bazaar"]),
            (3, None, "https://quiet.example.com/q", False, 1, 1, ["bazaar"]),
            (4, "svc_video", "https://video.example.com/veo", True, None, None, ["mpp"])):
        conn.execute(
            """
            insert into index_resources (id, resource_key, method, origin, route_template, probe_url, tracked,
              tracked_reason, service_id, quoted_price_atomic, quoted_asset, quoted_network, payees, payee_since,
              bazaar_payers_30d, bazaar_calls_30d, sources)
            values (%s, %s, 'POST', %s, %s, %s, %s, %s, %s, 7000, %s, 'eip155:8453', %s, %s, %s, %s, %s)
            """,
            (rid, f"POST {url}", url.rsplit("/", 1)[0], url, url, tracked, "gateway_purchase" if tracked else None,
             sid, USDC, [PAYEE, OTHER], NOW - timedelta(days=20), payers, calls, sources),
        )
        conn.execute("insert into index_resource_urls (source, method, url, resource_id) values ('bazaar', 'POST', %s, %s)",
                     (url, rid))
    for rid, url in ((1, "https://api.exa.ai/search"), (4, "https://video.example.com/veo")):
        for days in (0.2, 1.2):
            conn.execute(
                """
                insert into index_observations (resource_id, endpoint, method, at, kind, ok, http_status, network,
                  payee, quoted_amount_atomic, asset, pay_to)
                values (%s, %s, 'POST', %s, 'probe', true, 402, 'eip155:8453', %s, 7000, %s, %s)
                """,
                (rid, url, NOW - timedelta(days=days), PAYEE, USDC, [PAYEE, OTHER]),
            )
    conn.execute("insert into index_changes (resource_id, at, kind, old_value, new_value) values (1, %s, 'payee', %s, %s)",
                 (NOW - timedelta(days=1), PAYEE, f"{OTHER},{PAYEE}"))
    attestation = lambda delivery, traffic=None: json.dumps({"review": {"delivery": delivery, **({"traffic": traffic} if traffic else {})},
                                                             "receipt": {"endpoint": "https://api.exa.ai/search"}})
    for i, (delivery, traffic) in enumerate([("delivered", "test"), ("delivered", None), ("partial", None),
                                             ("delivered", "owner"), ("delivered", "owner"), ("partial", None)]):
        conn.execute(
            "insert into reviews (service_id, rating, reason, receipt_key_id, gateway_attestation, owner_ref, created_at) "
            "values ('svc_exa', 5, 'ok', 'k', %s, %s, %s)",
            (attestation(delivery, traffic), f"owner{i % 2}", NOW - timedelta(days=i + 1)),
        )
    conn.execute(
        "insert into reviews (service_id, rating, reason, receipt_key_id, gateway_attestation, weight_exclusion, created_at) "
        "values ('svc_exa', 1, 'ours', 'k', %s, 'platform_defect', %s)",
        (attestation("not_delivered"), NOW - timedelta(days=2)),
    )
    conn.execute(
        "insert into reviews (service_id, rating, reason, receipt_key_id, gateway_attestation, created_at) "
        "values ('svc_exa', 1, 'old', 'k', %s, %s)",
        (attestation("not_delivered"), NOW - timedelta(days=40)),
    )
    if v2:
        _w1_w2_columns(conn)
        # W2: one partial re-read as delivered; one seller failure before settlement; one settled
        # purchase with a non-2xx response, which is not a failure record.
        conn.execute("update reviews set delivery_reread = 'delivered' where id = (select min(id) from reviews "
                     "where gateway_attestation->'review'->>'delivery' = 'partial')")
        conn.execute("""
            insert into index_observations (resource_id, endpoint, method, at, kind, ok, http_status, detail, attempt_ref, traffic)
            values (1, 'https://api.exa.ai/search', 'POST', %(at)s, 'purchase', false, 502, 'seller_error', 'a-1', 'test'),
                   (1, 'https://api.exa.ai/search', 'POST', %(at)s, 'purchase', false, 400, 'funding', 'a-2', 'owner'),
                   (1, 'https://api.exa.ai/search', 'POST', %(at)s, 'purchase', false, 500, null, null, null)
        """, {"at": NOW - timedelta(days=3)})
        # W1: the video seller sells one model with a leaderboard prior.
        conn.execute("insert into index_offering_models (resource_id, model, model_family, modality, source) "
                     "values (4, 'veo-3.1', 'veo', 'video', 'route')")
        conn.execute("insert into model_quality_priors (model, model_family, modality, source, as_of, elo, rank, prior_rating) "
                     "values ('veo-3.1', 'veo', 'video', 'Artificial Analysis video arena', '2026-11-17', 1250, 2, 4.6)")


def test_recompute_stores_tiers_and_rewrites_only_what_changed(db):
    with db() as conn:
        _seed(conn)
        conn.commit()
    stats = reliability.recompute(db, NOW)
    # svc_hosted is untracked and unreviewed, so it has no row; svc_exa, svc_new, svc_video are tracked.
    assert stats == {"services": 3, "written": 3, "removed": 0, "bad": 0, "proven": 0, "popular": 2, "new": 1}
    with db() as conn:
        exa = reliability.for_service(conn, "svc_exa")
        assert (exa["tier"], exa["state"], exa["rule_version"]) == ("popular", "reliable", "reliability-v2")
        # 4 delivered (the platform defect and the 40-day-old ones excluded); 2 partials unresolved.
        assert (exa["posterior"]["alpha"], exa["posterior"]["beta"]) == (12, 1)
        assert exa["inputs"]["judged_30d"]["unresolved"] == 2
        assert exa["quality"] == pytest.approx(4.4) and exa["score"] == pytest.approx(12 / 13 * 4.4, abs=1e-4)
        row = conn.execute("select tier, alpha, beta, quality, score from service_reliability where service_id = 'svc_exa'").fetchone()
        assert (row["tier"], int(row["alpha"]), int(row["beta"])) == ("popular", 12, 1)
        # Anyone can recompute the answer from what is published.
        again = evaluate(exa["inputs"])
        assert (again["tier"], again["reasons"]) == (exa["tier"], exa["reasons"])
        video = reliability.for_service(conn, "svc_video")
        assert video["tier"] == "popular" and video["reasons"][0].startswith("popular: listed in mpp.dev's curated catalog")
        new = reliability.for_service(conn, "svc_new")
        assert new["tier"] == "new"
        untracked = reliability.for_service(conn, "svc_hosted")
        assert (untracked["tier"], untracked["state"]) == ("new", "unverified")
        assert untracked["reasons"][0] == reliability.NOT_TRACKED
        assert reliability.for_service(conn, "svc_nope") is None
        listed = reliability.listing(conn, tier="popular")
        assert listed["tier_counts"] == {"bad": 0, "proven": 0, "popular": 2, "new": 1}
        assert listed["counts"] == {"reliable": 2, "unverified": 1, "unreliable": 0}
        assert [s["service_id"] for s in listed["services"]] == ["svc_exa", "svc_video"]  # by score
        assert [s["service_id"] for s in reliability.by_state(conn, "reliable")["services"]] == ["svc_exa", "svc_video"]
    assert reliability.recompute(db, NOW + timedelta(hours=1))["written"] == 0


def test_w1_and_w2_evidence_once_their_columns_exist(db):
    with db() as conn:
        _seed(conn, v2=True)
        conn.commit()
    reliability.recompute(db, NOW)
    with db() as conn:
        exa = reliability.for_service(conn, "svc_exa")
        # One partial re-read as delivered: 5 delivered. One seller failure: beta + 1.
        # The 'funding' failure (not the seller's) and the plain failed purchase do not count.
        assert (exa["posterior"]["alpha"], exa["posterior"]["beta"]) == (13, 2)
        assert exa["inputs"]["failures_30d"] == {"total": 1, "test": 1, "by": {"seller_error": 1}}
        assert exa["inputs"]["judged_30d"]["reread"] == 1 and exa["inputs"]["judged_30d"]["unresolved"] == 1
        video = reliability.for_service(conn, "svc_video")
        assert video["inputs"]["endpoints"][0]["models"][0]["model"] == "veo-3.1"
        assert video["quality"] == pytest.approx(4.6)
        assert any("shrunk to 4.6 from veo-3.1 on Artificial Analysis video arena (2026-11-17)" in r for r in video["reasons"])


def test_url_lookup_resolves_aliases_and_unknown_urls(db):
    with db() as conn:
        _seed(conn)
        conn.commit()
    reliability.recompute(db, NOW)
    with db() as conn:
        exa = reliability.for_url(conn, "https://api.exa.ai/search/", "POST")
        assert exa["tier"] == "popular" and exa["service_id"] == "svc_exa" and exa["resolved"]["resource_id"] == 1
        alias = reliability.for_url(conn, "https://crowdcode.app/s/old-name", "POST")
        assert alias["service_id"] == "svc_hosted" and alias["tier"] == "new"
        unknown = reliability.for_url(conn, "https://nobody.example.org/thing", "POST")
        assert (unknown["tier"], unknown["state"], unknown["service_id"]) == ("new", "unverified", None)
        assert unknown["reasons"][0] == "not in CrowdCode's index"
        assert unknown["posterior"]["prior"]["source"] == "no outside evidence"
        # Listed but not tracked: evaluated from what the index knows (1 Bazaar payer: Beta(1,1)).
        quiet = reliability.for_url(conn, "https://quiet.example.com/q", "POST")
        assert quiet["service_id"] is None and quiet["tier"] == "new" and quiet["resolved"]["resource_id"] == 3
        assert reliability.NOT_TRACKED in quiet["reasons"]
        assert quiet["posterior"]["prior"]["kind"] == "none"


def test_blocking_forces_bad_with_the_note_and_is_audited(db, monkeypatch):
    with db() as conn:
        _seed(conn)
        conn.commit()
    reliability.recompute(db, NOW)
    import scripts.block_service as script
    monkeypatch.setattr(script, "connect", db)
    note = "CrowdCode reliability fixture: blocked on purpose"
    assert script.main(["--block", "svc_exa", "--note", note, "--by", "w4-test"]) == 0
    assert script.main(["--block", "svc_fixture", "--note", note, "--by", "w4-test"]) == 0
    assert script.main(["--block", "svc_missing", "--note", note, "--by", "w4-test"]) == 2
    assert script.main(["--block", "svc_exa", "--note", "x", "--by", "w4-test"]) == 2
    with db() as conn:
        exa = reliability.for_service(conn, "svc_exa")
        assert (exa["tier"], exa["state"]) == ("bad", "unreliable") and exa["reasons"][0].endswith(note)
        fixture = reliability.for_url(conn, "https://fixture.example.com/x", "POST")
        assert fixture["tier"] == "bad" and fixture["service_id"] == "svc_fixture"
        assert fixture["reasons"][0] == f"blocked by a CrowdCode operator on {datetime.now(UTC):%Y-%m-%d}: {note}"
    assert script.main(["--unblock", "svc_exa", "--note", "fixture done", "--by", "w4-test"]) == 0
    assert script.main(["--unblock", "svc_exa", "--note", "fixture done", "--by", "w4-test"]) == 1
    assert script.main(["--unblock", "svc_fixture", "--note", "fixture done", "--by", "w4-test"]) == 0
    with db() as conn:
        assert reliability.for_service(conn, "svc_exa")["tier"] != "bad"
        assert reliability.for_service(conn, "svc_fixture")["tier"] == "new"
        assert conn.execute("select count(*)::int as n from service_reliability where service_id = 'svc_fixture'").fetchone()["n"] == 0
        audit = conn.execute("select service_id, action from service_block_audit order by id").fetchall()
        assert [(a["service_id"], a["action"]) for a in audit] == [
            ("svc_exa", "block"), ("svc_fixture", "block"), ("svc_exa", "unblock"), ("svc_fixture", "unblock")]


def test_the_cron_runs_the_rule_after_the_probes():
    from crowdcode import cron
    names = [job.__name__ for job in cron.jobs()]
    assert names.index("run_reliability") == names.index("run_index_probes") + 1


def test_the_directory_carries_the_tier_and_the_one_score_and_keeps_v31_as_score(db, monkeypatch):
    with db() as conn:
        _seed(conn)
        conn.commit()
    reliability.recompute(db, NOW)
    from crowdcode import server
    monkeypatch.setattr(server, "connect", db)
    rows = {r["service_id"]: r for r in server._top_services_payload(None)["services"]}
    exa = rows["svc_exa"]
    assert exa["tier"] == "popular" and exa["reliability_state"] == "reliable"
    assert exa["reliability_score"] == pytest.approx(12 / 13 * 4.4, abs=1e-4)
    assert exa["score"] == pytest.approx(4.4)  # v3.1: clients validate it as 2-5
    detail, status = server._service_detail_payload("svc_exa")
    assert status == 200 and detail["tier"] == "popular" and detail["reliability_score"] == exa["reliability_score"]
    assert rows["svc_new"]["tier"] == "new"


def test_a_listed_untracked_video_offering_is_evaluated_from_its_payers_and_model(db):
    with db() as conn:
        _seed(conn, v2=True)
        conn.execute(
            """
            insert into index_resources (id, resource_key, method, origin, route_template, probe_url, tracked,
              bazaar_payers_30d, bazaar_calls_30d, sources)
            values (5, 'POST https://clips.example.com/veo', 'POST', 'https://clips.example.com',
              'https://clips.example.com/veo', 'https://clips.example.com/veo', false, 40, 120, '{bazaar}')
            """)
        conn.execute("insert into index_offering_models (resource_id, model, model_family, modality, source) "
                     "values (5, 'veo-3.1', 'veo', 'video', 'route')")
        conn.commit()
    with db() as conn:
        r = reliability.for_url(conn, "https://clips.example.com/veo", "POST")
    assert (r["tier"], r["service_id"]) == ("new", None)
    assert r["posterior"]["alpha"] == 8 and r["quality"] == pytest.approx(4.6)
    assert r["score"] == pytest.approx(8 / 9 * 4.6, abs=1e-4)
    assert r["reasons"][0] == ("new: 40 payers on the x402 Bazaar in 30 days, 3.0 calls per payer, but CrowdCode "
                               "does not probe it yet; proven needs 5 judged purchases with a mean of 0.85 (0 so far)")
    assert any("shrunk to 4.6 from veo-3.1 on Artificial Analysis video arena (2026-11-17)" in x for x in r["reasons"])
    assert r["inputs"]["listed"]["models"][0]["model"] == "veo-3.1"
