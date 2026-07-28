"""Cron job behavior: sweep replay, summary watermark, and input tiering."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from crowdcode import cron
from crowdcode.reputation import apply_review_trust_update
from crowdcode.scoring import MU0, TrustRow
from tests.test_reputation import FakeConn

NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)
SEED_WALLET = "0x" + "11" * 20
HONEST_WALLET = "0x" + "22" * 20
SYBIL_WALLET = "0x" + "33" * 20


def _user_row(wallet, raw_trust, *, is_seed=False, slashed_at=None, user_id=1):
    return {
        "user_id": user_id,
        "wallet_address": wallet,
        "raw_trust": raw_trust,
        "is_seed": is_seed,
        "slashed_at": slashed_at,
    }


@contextmanager
def _fake_connect(conn):
    yield conn


def test_sweep_replay_reproduces_the_write_path_trust(monkeypatch):
    # One seed 5-star, then an honest 5-star on the same service: the sweep's
    # from-scratch replay must land on the same trust the incremental write
    # path would have produced for that second review.
    reviews = [
        {
            "id": 1,
            "service_id": "svc_1",
            "reviewer_wallet": SEED_WALLET,
            "rating": 5,
            "payment_verified": True,
            "signature_verified": True,
            "created_at": NOW - timedelta(hours=2),
            "user_id": 1,
        },
        {
            "id": 2,
            "service_id": "svc_1",
            "reviewer_wallet": HONEST_WALLET,
            "rating": 5,
            "payment_verified": True,
            "signature_verified": True,
            "created_at": NOW - timedelta(hours=1),
            "user_id": 2,
        },
    ]
    users = [
        _user_row(SEED_WALLET, 1.0, is_seed=True, user_id=1),
        _user_row(HONEST_WALLET, 0.0, user_id=2),
    ]
    conn = FakeConn(
        {
            "select id, service_id, reviewer_wallet": reviews,
            "select user_id, wallet_address": users,
            "select id, score, n_eff from services": [
                {"id": "svc_1", "score": MU0, "n_eff": 0.0}
            ],
        }
    )
    monkeypatch.setattr(cron, "connect", lambda: _fake_connect(conn))
    cron.run_consistency_sweep(NOW)

    swept = {q[1][2]: q[1][0] for q in conn.executed("update wallet_users set raw_trust")}
    assert swept[2] > 0  # the honest wallet earned trust from the seed's consensus

    # Same review through the incremental write path.
    write_conn = FakeConn(
        {
            "from wallet_users\n        where wallet_address": [
                {
                    "user_id": 2,
                    "raw_trust": 0.0,
                    "is_seed": False,
                    "slashed_at": None,
                }
            ],
            "from reviews": [
                {
                    "reviewer_wallet": r["reviewer_wallet"],
                    "rating": r["rating"],
                    "payment_verified": r["payment_verified"],
                    "signature_verified": r["signature_verified"],
                    "created_at": r["created_at"],
                }
                for r in reviews[:1]
            ],
            "from wallet_users": [
                {
                    "wallet_address": SEED_WALLET,
                    "raw_trust": 1.0,
                    "is_seed": True,
                    "slashed_at": None,
                }
            ],
        }
    )
    incremental = apply_review_trust_update(
        write_conn, HONEST_WALLET, "svc_1", 5, NOW - timedelta(hours=1)
    )
    assert abs(swept[2] - incremental) < 1e-12


def test_sweep_backfills_user_ids(monkeypatch):
    conn = FakeConn(
        {
            "select id, service_id, reviewer_wallet": [
                {
                    "id": 9,
                    "service_id": "svc_1",
                    "reviewer_wallet": HONEST_WALLET,
                    "rating": 5,
                    "payment_verified": True,
                    "signature_verified": True,
                    "created_at": NOW,
                    "user_id": None,
                }
            ],
            "insert into wallet_users": [{"user_id": 4}],
            "select user_id, wallet_address": [_user_row(HONEST_WALLET, 0.0, user_id=4)],
            "select id, score, n_eff from services": [],
        }
    )
    monkeypatch.setattr(cron, "connect", lambda: _fake_connect(conn))
    cron.run_consistency_sweep(NOW)
    assert conn.executed("update reviews set user_id")[0][1] == (4, 9)


def _summary_row(wallet, *, verified, rating=5):
    return {
        "rating": rating,
        "reason": "worked",
        "task_context": None,
        "payment_verified": verified,
        "signature_verified": True,
        "reviewer_wallet": wallet,
        "created_at": NOW,
    }


def test_summary_input_prefers_trusted_reviews():
    trusted = _summary_row(SEED_WALLET, verified=True)
    conn = FakeConn(
        {"from reviews": [trusted, _summary_row(SYBIL_WALLET, verified=True)]}
    )
    trust = {
        SEED_WALLET: TrustRow(raw_trust=1.0, is_seed=True),
        SYBIL_WALLET: TrustRow(raw_trust=0.0),
    }
    selected = cron._summary_input_reviews(conn, "svc_1", trust, NOW)
    assert selected == [trusted]


def test_summary_input_falls_back_to_verified_reviews_at_cold_start():
    verified = _summary_row(SYBIL_WALLET, verified=True)
    conn = FakeConn(
        {"from reviews": [verified, _summary_row(SYBIL_WALLET, verified=False)]}
    )
    trust = {SYBIL_WALLET: TrustRow(raw_trust=0.0)}
    assert cron._summary_input_reviews(conn, "svc_1", trust, NOW) == [verified]


def test_summary_input_is_empty_when_nothing_qualifies():
    conn = FakeConn({"from reviews": [_summary_row(SYBIL_WALLET, verified=False)]})
    trust = {SYBIL_WALLET: TrustRow(raw_trust=0.0)}
    assert cron._summary_input_reviews(conn, "svc_1", trust, NOW) == []


def test_service_summaries_skip_when_nothing_is_stale(monkeypatch):
    conn = FakeConn({})
    monkeypatch.setattr(cron, "connect", lambda: _fake_connect(conn))
    cron.run_service_summaries(NOW)
    assert conn.executed("update services") == []


# --- payment re-verification (rpc_unreachable retry) -----------------------

from crowdcode.payments import OnchainCheck  # noqa: E402

PAYEE = "0x" + "44" * 20
CANONICAL_TX = "0x" + "cd" * 32


def _reverify_candidate(**overrides):
    row = {
        "id": 42,
        "service_id": "svc_1",
        "payment_provider": "x402",
        "payment_target_ref": PAYEE,
        "reviewer_wallet": HONEST_WALLET,
        "payment_reference_canonical": CANONICAL_TX,
        "payment_verification_metadata": {
            "proof_present": False,
            "source": "payment_reference",
            "verification_failure": "rpc_unreachable",
        },
    }
    row.update(overrides)
    return row


def _run_reverification(monkeypatch, candidates, check, chain_failure=None):
    conn = FakeConn({"->>'verification_failure'": candidates})
    monkeypatch.setattr(cron, "connect", lambda: _fake_connect(conn))
    monkeypatch.setattr(
        cron,
        "check_payment_reference_onchain",
        lambda **kw: (
            check,
            {
                "network": "base",
                "token_pinned": True,
                "amount_checked": False,
                "verification_failure": chain_failure,
            },
        ),
    )
    recomputed = []
    monkeypatch.setattr(
        cron,
        "recompute_service_score",
        lambda _conn, service_id, _now: recomputed.append(service_id),
    )
    cron.run_payment_reverification(NOW)
    return conn, recomputed


def test_reverification_upgrades_when_the_transfer_now_verifies(monkeypatch):
    check = OnchainCheck(
        True,
        transfer={"from": HONEST_WALLET, "to": PAYEE, "value": 1000, "token": "0xt"},
        block_number=7,
    )
    conn, recomputed = _run_reverification(
        monkeypatch, [_reverify_candidate()], check
    )

    update = conn.executed("set payment_verification_level")[0]
    params = update[1]
    assert params[0] == "onchain_verified"
    assert params[2] == 1000  # coalesced into amount
    meta = params[3].obj
    assert meta["verification_failure"] is None
    assert meta["reverify_attempts"] == 1
    assert meta["upgraded_by"] == "cron_reverification"
    assert params[4] == 42
    assert recomputed == ["svc_1"]
    assert conn.commits == 1


def test_reverification_records_definitive_failures_so_they_stop_retrying(
    monkeypatch,
):
    check = OnchainCheck(False, failure="no_matching_transfer")
    conn, recomputed = _run_reverification(
        monkeypatch,
        [_reverify_candidate()],
        check,
        chain_failure="no_matching_transfer",
    )

    assert conn.executed("set payment_verification_level") == []
    update = conn.executed("set payment_verification_metadata")[0]
    meta = update[1][0].obj
    # A definitive chain "no" replaces rpc_unreachable: the row is no longer
    # a retry candidate for the nightly pass.
    assert meta["verification_failure"] == "no_matching_transfer"
    assert meta["reverify_attempts"] == 1
    assert recomputed == []


def test_reverification_skips_rows_whose_reference_is_not_a_tx_hash(monkeypatch):
    def _explode(**kw):
        raise AssertionError("on-chain check must not run for non-tx references")

    conn = FakeConn(
        {
            "->>'verification_failure'": [
                _reverify_candidate(payment_reference_canonical="receipt-ref-1")
            ]
        }
    )
    monkeypatch.setattr(cron, "connect", lambda: _fake_connect(conn))
    monkeypatch.setattr(cron, "check_payment_reference_onchain", _explode)
    cron.run_payment_reverification(NOW)
    assert conn.executed("update reviews") == []
