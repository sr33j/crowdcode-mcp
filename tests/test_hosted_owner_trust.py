"""Owner trust against PostgreSQL: who earns weight, and who no longer does.

Replaces tests/test_hosted_reputation.py, which pinned every registered hosted
mainnet wallet at trust 1.0. That is the behaviour this suite now forbids.
"""
from datetime import UTC, datetime, timedelta
from pathlib import Path

from crowdcode.cron import replay_scores
from crowdcode.reputation import ensure_owner, ensure_user, load_trust_map, sync_seed_owners
from crowdcode.scoring import effective_weight
from tests.test_shared_wallet_db import db

SEED_OWNER = "a" * 32
OTHER_OWNER = "b" * 32
HOSTED_WALLET = "0x" + "11" * 20
NOW = datetime(2026, 10, 22, 12, 0, 0, tzinfo=UTC)


def service(conn, service_id="svc_1"):
    conn.execute(
        "insert into services(id,name,score,n_eff) values(%s,%s,3.0,0) on conflict do nothing",
        (service_id, service_id),
    )
    return service_id


def review(conn, service_id, owner_ref, rating, *, exclusion=None, at=NOW, wallet=None):
    conn.execute(
        """insert into reviews(service_id,rating,reason,reviewer_wallet,signature_verified,
             payment_verified,payment_verification_level,owner_ref,weight_exclusion,
             verified_source,created_at)
           values(%s,%s,'judged',%s,true,true,'response_attested',%s,%s,%s,%s)""",
        (service_id, rating, wallet or f"0xagent{owner_ref[:6]}", owner_ref, exclusion,
         "community" if exclusion else "hosted_receipt", at),
    )


def test_registering_a_hosted_wallet_grants_no_trust(db):
    conn, _, _ = db
    # The old bridge view is gone; a first review from a hosted mainnet wallet
    # creates an ordinary, weightless wallet_users row.
    assert ensure_user(conn, HOSTED_WALLET)["is_seed"] is False
    assert ensure_user(conn, HOSTED_WALLET)["raw_trust"] == 0
    assert (Path(__file__).parents[1] / "supabase/hosted-only-reviews.sql").read_text().count(
        "drop view if exists public.opencrowd_seed_wallets"
    ) == 1


def test_only_configured_seed_owners_start_with_weight(db):
    conn, _, _ = db
    ensure_owner(conn, OTHER_OWNER)
    sync_seed_owners(conn, [SEED_OWNER])
    trust = load_trust_map(conn)
    assert effective_weight(trust[SEED_OWNER]) == 1.0
    assert effective_weight(trust[OTHER_OWNER]) == 0.0


def test_a_new_owner_earns_weight_by_agreeing_with_trusted_evidence(db):
    conn, _, _ = db
    svc = service(conn)
    sync_seed_owners(conn, [SEED_OWNER])
    # The seed establishes a confident consensus over several days, then the
    # new owner agrees with it. Trust can only be earned where the consensus
    # has already moved, which is what anchors it to the seed.
    for day in range(40):
        review(conn, svc, SEED_OWNER, 5, at=NOW - timedelta(days=60 - day))
        review(conn, svc, OTHER_OWNER, 5, at=NOW - timedelta(days=60 - day) + timedelta(hours=1))
    replay_scores(conn, NOW)
    trust = load_trust_map(conn)
    assert trust[OTHER_OWNER].raw_trust > 0
    # And a service with two trusted-or-pending owner votes still reports
    # exactly two votes, not eighty.
    assert conn.execute("select n_eff from services where id=%s", (svc,)).fetchone()["n_eff"] <= 2.0


def test_community_reviews_never_earn_trust_or_move_a_score(db):
    conn, _, _ = db
    svc = service(conn)
    sync_seed_owners(conn, [SEED_OWNER])
    review(conn, svc, SEED_OWNER, 5)
    for i in range(50):
        review(conn, svc, OTHER_OWNER, 1, exclusion="community", wallet=f"0xcli{i:034x}")
    replay_scores(conn, NOW)
    row = conn.execute("select score, n_eff from services where id=%s", (svc,)).fetchone()
    assert row["n_eff"] == 1.0
    assert abs(row["score"] - (5 + 2 * 3.0) / 3) < 1e-9
    assert load_trust_map(conn).get(OTHER_OWNER) is None


def test_slashing_still_overrides_a_seed_owner(db):
    conn, _, _ = db
    sync_seed_owners(conn, [SEED_OWNER])
    conn.execute("update review_owners set slashed_at=now() where owner_ref=%s", (SEED_OWNER,))
    sync_seed_owners(conn, [SEED_OWNER])
    assert effective_weight(load_trust_map(conn)[SEED_OWNER]) == 0.0
