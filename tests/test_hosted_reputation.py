"""Hosted trust bridge behavior against PostgreSQL, including first reviews."""
from datetime import UTC, datetime
from pathlib import Path

from crowdcode.cron import replay_scores
from crowdcode.reputation import ensure_user, sync_seed_wallets, load_trust_map
from crowdcode.scoring import effective_weight
from tests.test_shared_wallet_db import db

HOSTED = "0x" + "11" * 20
OPERATOR = "0x" + "22" * 20
EXTERNAL = "0x" + "33" * 20


def bridge(conn, schema):
    conn.execute("create table agents(wallet_address text, mode text, wallet_kind text, state text)")
    migration = (Path(__file__).parents[1] / "supabase/opencrowd-wallets.sql").read_text()
    conn.execute(migration.replace("public.", schema + ".").replace("opencrowd.", schema + "."))


def test_hosted_first_review_seed_sync_and_replay(db):
    conn, schema, _ = db
    bridge(conn, schema)
    conn.execute("insert into agents values(%s, 'mainnet', 'agent_eoa', 'stopped')", (HOSTED,))
    assert ensure_user(conn, HOSTED)["is_seed"] is True
    assert ensure_user(conn, EXTERNAL)["raw_trust"] == 0
    # A raw-trust reset or operator-list reconciliation must not remove hosted trust.
    conn.execute("update wallet_users set raw_trust=0")
    sync_seed_wallets(conn, [OPERATOR])
    replay_scores(conn, datetime.now(UTC))
    weights = load_trust_map(conn)
    assert weights[HOSTED].raw_trust == 1
    assert effective_weight(weights[HOSTED]) == 1
    assert effective_weight(weights[EXTERNAL]) == 0
    assert effective_weight(weights[OPERATOR]) == 1


def test_hosted_bridge_excludes_legacy_testnet_and_invalid_wallets(db):
    conn, schema, _ = db
    bridge(conn, schema)
    for wallet, mode, kind in [
        (HOSTED, 'mainnet', 'legacy_eoa'),
        (EXTERNAL, 'demo', 'agent_eoa'),
        ('invalid', 'mainnet', 'agent_eoa'),
    ]:
        conn.execute("insert into agents values(%s,%s,%s,'stopped')", (wallet, mode, kind))
    sync_seed_wallets(conn, [])
    assert conn.execute("select count(*) as n from wallet_users").fetchone()["n"] == 0


def test_deleted_hosted_wallet_keeps_trust_but_slashing_still_wins(db):
    conn, schema, _ = db
    bridge(conn, schema)
    conn.execute("insert into agents values(%s, 'mainnet', 'agent_eoa', 'deleted')", (HOSTED,))
    sync_seed_wallets(conn, [])
    conn.execute("update wallet_users set slashed_at=now() where wallet_address=%s", (HOSTED,))
    ensure_user(conn, HOSTED)
    sync_seed_wallets(conn, [OPERATOR])
    replay_scores(conn, datetime.now(UTC))
    assert effective_weight(load_trust_map(conn)[HOSTED]) == 0
