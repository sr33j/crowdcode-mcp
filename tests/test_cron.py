"""Cron job behavior: sweep replay, summary watermark, and input tiering."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from crowdcode import cron
from crowdcode.scoring import MU0, TrustRow
from tests.test_reputation import FakeConn

NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)
SEED_WALLET = "0x" + "11" * 20
HONEST_WALLET = "0x" + "22" * 20
SYBIL_WALLET = "0x" + "33" * 20
SEED_OWNER = "1" * 32
HONEST_OWNER = "2" * 32
SYBIL_OWNER = "3" * 32


def _owner_row(owner_ref, raw_trust, *, is_seed=False, slashed_at=None):
    return {
        "owner_ref": owner_ref,
        "raw_trust": raw_trust,
        "is_seed": is_seed,
        "slashed_at": slashed_at,
    }


@contextmanager
def _fake_connect(conn):
    yield conn


def _sweep_review(review_id, owner, wallet, rating, *, minutes, exclusion=None, service="svc_1"):
    return {
        "id": review_id,
        "service_id": service,
        "reviewer_wallet": wallet,
        "rating": rating,
        "payment_verified": True,
        "signature_verified": True,
        "payment_verification_level": "response_attested",
        "created_at": NOW - timedelta(minutes=minutes),
        "user_id": review_id,
        "owner_ref": owner,
        "weight_exclusion": exclusion,
    }


def test_sweep_applies_one_trust_event_per_owner_and_service(monkeypatch):
    # The seed establishes a positive consensus. Two paid outcomes from the
    # honest owner — on different days, through different agent wallets —
    # average to neutral (3), so the nightly replay must apply no trust change
    # instead of treating them as two separate calls.
    reviews = [
        _sweep_review(1, SEED_OWNER, SEED_WALLET, 5, minutes=120),
        _sweep_review(2, HONEST_OWNER, HONEST_WALLET, 5, minutes=60),
        _sweep_review(3, HONEST_OWNER, "0x" + "44" * 20, 1, minutes=30),
    ]
    owners = [
        _owner_row(SEED_OWNER, 1.0, is_seed=True),
        _owner_row(HONEST_OWNER, 0.0),
    ]
    conn = FakeConn(
        {
            "select id, service_id, reviewer_wallet": reviews,
            "insert into wallet_users": [{"user_id": 1}],
            "insert into review_owners": [_owner_row(SEED_OWNER, 1.0, is_seed=True)],
            "select owner_ref, raw_trust": owners,
            "select id, score, n_eff from services": [
                {"id": "svc_1", "score": MU0, "n_eff": 0.0}
            ],
        }
    )
    monkeypatch.setattr(cron, "connect", lambda: _fake_connect(conn))
    cron.run_consistency_sweep(NOW)

    swept = {q[1][2]: q[1][0] for q in conn.executed("update review_owners set raw_trust")}
    assert swept[HONEST_OWNER] == 0.0
    # Two owners voted, but only the seed has trust, so the score rests on
    # exactly one vote. This is also the one-vote cap in action: the honest
    # owner's two reviews, from two wallets on two days, are one event.
    assert abs(conn.executed("set score = %s, n_eff")[0][1][1] - 1.0) < 0.01


def test_sweep_ignores_community_reviews_entirely(monkeypatch):
    reviews = [
        _sweep_review(1, SEED_OWNER, SEED_WALLET, 5, minutes=120),
        # 40 unattested five-star reviews, including some from the seed owner.
        *[
            _sweep_review(i + 2, SYBIL_OWNER, f"0x{i:040x}", 5, minutes=60, exclusion="community")
            for i in range(20)
        ],
        *[
            _sweep_review(i + 22, SEED_OWNER, f"0x{i + 100:040x}", 1, minutes=30, exclusion="community")
            for i in range(20)
        ],
    ]
    conn = FakeConn(
        {
            "select id, service_id, reviewer_wallet": reviews,
            "insert into wallet_users": [{"user_id": 1}],
            "insert into review_owners": [_owner_row(SEED_OWNER, 1.0, is_seed=True)],
            "select owner_ref, raw_trust": [
                _owner_row(SEED_OWNER, 1.0, is_seed=True),
                _owner_row(SYBIL_OWNER, 0.0),
            ],
            "select id, score, n_eff from services": [
                {"id": "svc_1", "score": MU0, "n_eff": 0.0}
            ],
        }
    )
    monkeypatch.setattr(cron, "connect", lambda: _fake_connect(conn))
    cron.run_consistency_sweep(NOW)
    score, n_eff = conn.executed("set score = %s, n_eff")[0][1][:2]
    assert abs(n_eff - 1.0) < 0.01
    assert abs(score - (5 + 2 * MU0) / 3) < 0.01
    assert {q[1][0] for q in conn.executed("update review_owners set raw_trust")} == {0.0}
    # Only owners behind weighted reviews are registered at all.
    assert {q[1][0] for q in conn.executed("insert into review_owners")} == {SEED_OWNER}


def test_sweep_backfills_user_ids(monkeypatch):
    conn = FakeConn(
        {
            "select id, service_id, reviewer_wallet": [
                {**_sweep_review(9, HONEST_OWNER, HONEST_WALLET, 5, minutes=0), "user_id": None}
            ],
            "insert into wallet_users": [{"user_id": 4}],
            "insert into review_owners": [_owner_row(HONEST_OWNER, 0.0)],
            "select owner_ref, raw_trust": [_owner_row(HONEST_OWNER, 0.0)],
            "select id, score, n_eff from services": [],
        }
    )
    monkeypatch.setattr(cron, "connect", lambda: _fake_connect(conn))
    cron.run_consistency_sweep(NOW)
    assert conn.executed("update reviews set user_id")[0][1] == (4, 9)
    # A five-star review with no trusted evidence must not earn reputation
    # from the optimistic public prior during the production replay.
    assert conn.executed("update review_owners set raw_trust")[0][1][0] == 0.0


def _summary_row(wallet, *, verified, rating=5, owner=None, exclusion="community"):
    return {
        "rating": rating,
        "reason": "worked",
        "task_context": None,
        "payment_verified": verified,
        "signature_verified": True,
        "payment_verification_level": "response_attested" if exclusion is None else "signature_only",
        "reviewer_wallet": wallet,
        "owner_ref": owner,
        "weight_exclusion": exclusion,
        "verified_source": "community" if exclusion else "hosted_receipt",
        "created_at": NOW,
    }


def test_summary_input_prefers_weighted_reviews():
    """The narrative comes from judged hosted evidence when any exists: a
    service's published summary must not be written by unweighted text."""
    trusted = _summary_row(SEED_WALLET, verified=True, owner=SEED_OWNER, exclusion=None)
    conn = FakeConn(
        {"from reviews": [trusted, _summary_row(SYBIL_WALLET, verified=True)]}
    )
    trust = {
        SEED_OWNER: TrustRow(raw_trust=1.0, is_seed=True),
        SYBIL_OWNER: TrustRow(raw_trust=0.0),
    }
    selected = cron._summary_input_reviews(conn, "svc_1", trust, NOW)
    assert selected == [trusted]


def test_summary_input_ignores_weighted_reviews_from_untrusted_owners():
    attested = _summary_row(SYBIL_WALLET, verified=True, owner=SYBIL_OWNER, exclusion=None)
    conn = FakeConn({"from reviews": [attested]})
    # A real receipt from an owner that has earned nothing still does not get
    # to write the narrative; it falls back to the cold-start tier.
    assert cron._summary_input_reviews(conn, "svc_1", {SYBIL_OWNER: TrustRow(raw_trust=0.0)}, NOW) == [attested]


def test_summary_input_includes_authenticated_unpaid_reviews_at_cold_start():
    verified = _summary_row(SYBIL_WALLET, verified=True)
    unpaid = _summary_row(SYBIL_WALLET, verified=False)
    conn = FakeConn({"from reviews": [verified, unpaid]})
    trust = {SYBIL_OWNER: TrustRow(raw_trust=0.0)}
    assert cron._summary_input_reviews(conn, "svc_1", trust, NOW) == [verified, unpaid]


def test_summary_input_is_empty_when_nothing_qualifies():
    unsigned = {**_summary_row(SYBIL_WALLET, verified=False), "signature_verified": False}
    conn = FakeConn({"from reviews": [unsigned]})
    trust = {SYBIL_OWNER: TrustRow(raw_trust=0.0)}
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


def test_reverification_rechecks_on_the_recorded_x402_network(monkeypatch):
    calls = []
    conn = FakeConn({"->>'verification_failure'": [
        _reverify_candidate(id=1, payment_verification_metadata={
            "network": "monad", "verification_failure": "rpc_unreachable"}),
        _reverify_candidate(id=2),  # legacy row: no network recorded
    ]})
    monkeypatch.setattr(cron, "connect", lambda: _fake_connect(conn))
    monkeypatch.setattr(
        cron,
        "check_payment_reference_onchain",
        lambda **kw: calls.append(kw["network"]) or (
            OnchainCheck(False, failure="rpc_unreachable"),
            {"verification_failure": "rpc_unreachable"},
        ),
    )
    monkeypatch.setattr(cron, "recompute_service_score", lambda *_: None)
    cron.run_payment_reverification(NOW)
    assert calls == ["monad", None]


# --- requested-services summary ---------------------------------------------

import pytest  # noqa: E402

from crowdcode import summaries  # noqa: E402
from crowdcode.redaction import RedactionUnavailable  # noqa: E402


def _request_rows(n):
    return [
        {"id": i, "created_at": NOW, "directory_match": "missing",
         "service_description": f"need service {i}", "task_context": None}
        for i in range(n)
    ]


def _patch_requests(monkeypatch, rows, redact):
    conn = FakeConn(
        {
            "information_schema.columns": [
                {"column_name": name}
                for name in ("id", "created_at", "directory_match",
                             "service_description", "task_context")
            ],
            "from service_requests": rows,
        }
    )
    monkeypatch.setattr(summaries, "connect", lambda: _fake_connect(conn))
    calls = []

    def fake_redact(texts, **kwargs):
        calls.append(kwargs)
        return redact(texts)

    monkeypatch.setattr(summaries, "redact_texts", fake_redact)
    return calls


def test_request_summary_input_uses_partial_batch_redaction(monkeypatch):
    calls = _patch_requests(
        monkeypatch,
        _request_rows(3),
        lambda texts: ["[R] " + texts[0], None, "[R] " + texts[2]],
    )
    requests = summaries.fetch_recent_requests()
    assert [r["text"] for r in requests] == ["[R] need service 0", "[R] need service 2"]
    assert calls == [{"fail_closed": False, "timeout": 60.0, "allow_partial": True}]


def test_request_summary_keeps_the_last_cache_when_redaction_is_down(monkeypatch):
    _patch_requests(monkeypatch, _request_rows(3), lambda texts: None)
    with pytest.raises(RedactionUnavailable):
        summaries.fetch_recent_requests()

    conn = FakeConn({})
    monkeypatch.setattr(cron, "connect", lambda: _fake_connect(conn))
    monkeypatch.setattr(cron, "fetch_recent_requests", summaries.fetch_recent_requests)
    with pytest.raises(RedactionUnavailable):
        cron.run_request_summary(NOW)
    assert conn.executed("app_cache") == []


def test_request_summary_with_no_requests_skips_the_redactor(monkeypatch):
    calls = _patch_requests(monkeypatch, [], lambda texts: None)
    assert summaries.fetch_recent_requests() == []
    assert calls == []
