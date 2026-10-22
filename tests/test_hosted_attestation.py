"""Hosted gateway attestations end to end, including the manipulation attempts.

Every scenario drives the real `review_service` tool against PostgreSQL with a
real Ed25519 gateway key, so what is tested is the admission path a production
review actually takes: forged and mismatched receipts, two agents of one owner,
an owner that owns the payee, dust payments, an unpaid review, and replay.
"""
from __future__ import annotations

import base64
import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from eth_account import Account
from eth_account.messages import encode_defunct
from psycopg.rows import dict_row

from crowdcode import payments as payments_mod
from crowdcode import server as server_mod
from crowdcode import settings as settings_mod
from crowdcode.identity import build_identity, resolve_service
from crowdcode.payments import BASE_USDC_ADDRESS, ERC20_TRANSFER_TOPIC, canonical_review_payload
from crowdcode.receipts import canonical_payload, payload_sha256, verify_attestation
from crowdcode.reputation import load_trust_map, sync_seed_owners
from tests.test_shared_wallet_db import db  # noqa: F401 - fixture

KID = "oc-receipt-test-0001"
GATEWAY_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
PUBLIC_KEY = base64.urlsafe_b64encode(
    GATEWAY_KEY.public_key().public_bytes_raw()
).decode().rstrip("=")
RECEIPT_KEYS = f"{KID}:{PUBLIC_KEY}"

ENDPOINT = "https://api.provider.example/v1/search"
PAYEE = "0x" + "ab" * 20
FACILITATOR = "0x" + "fe" * 20
SEED_OWNER = "1" * 32
OTHER_OWNER = "2" * 32
JUDGE_TEXT = "Returned 12 ranked results in 1.4 s; two lacked the requested snippet field."


def _sign(payload: dict) -> str:
    return base64.urlsafe_b64encode(
        GATEWAY_KEY.sign(canonical_payload(payload).encode("utf-8"))
    ).decode().rstrip("=")


def receipt(
    *,
    tx: str,
    owner_ref: str = SEED_OWNER,
    payee_owner_ref: str | None = None,
    amount: str = "5000",
    endpoint: str = ENDPOINT,
    payee: str = PAYEE,
    settled_at: datetime | None = None,
    attempt_id: str = "11111111-1111-4111-a111-111111111111",
) -> dict:
    return {
        "type": "opencrowd.purchase-receipt.v1",
        "attempt_id": attempt_id,
        "owner_ref": owner_ref,
        "run_ref": "f" * 32,
        "listing_id": "svc_listing",
        "endpoint": endpoint,
        "method": "POST",
        "request_sha256": hashlib.sha256(b"request").hexdigest(),
        "response_sha256": hashlib.sha256(b"response").hexdigest(),
        "response_status": 200,
        "payment_reference": tx,
        "network": "base",
        "payee": payee.lower(),
        "payee_owner_ref": payee_owner_ref,
        "amount_atomic": amount,
        "token": BASE_USDC_ADDRESS,
        "settled_at": (settled_at or datetime.now(UTC)).isoformat(),
        "kid": KID,
    }


def attestation(
    rec: dict,
    *,
    reviewer_wallet: str,
    rating: int = 4,
    review_text: str = JUDGE_TEXT,
    issued_at: datetime | None = None,
    **overrides,
) -> dict:
    review = {
        "type": "opencrowd.review-attestation.v1",
        "receipt_sha256": payload_sha256(rec),
        "attempt_id": rec["attempt_id"],
        "owner_ref": rec["owner_ref"],
        "payee_owner_ref": rec["payee_owner_ref"],
        "payee": rec["payee"],
        "payment_reference": rec["payment_reference"],
        "network": rec["network"],
        "amount_atomic": rec["amount_atomic"],
        "endpoint": rec["endpoint"],
        "reviewer_wallet": reviewer_wallet.lower(),
        "rating": rating,
        "review_text_sha256": hashlib.sha256(review_text.strip().encode("utf-8")).hexdigest(),
        "delivery": "partial",
        "judge_model": "openai/gpt-5.6-luna",
        "judge_prompt_version": "opencrowd.judge.v1",
        "agent_rating": 5,
        "issued_at": (issued_at or datetime.now(UTC)).isoformat(),
        "kid": rec["kid"],
    }
    review.update(overrides)
    return {
        "receipt": rec,
        "receipt_signature": _sign(rec),
        "review": review,
        "review_signature": _sign(review),
    }


def _addr_topic(address: str) -> str:
    return "0x" + address.lower().removeprefix("0x").rjust(64, "0")


def _transfer_receipt(*, payer: str, payee: str = PAYEE, value: int = 5000) -> dict:
    return {
        "status": "0x1",
        "from": FACILITATOR,
        "blockNumber": "0x1867543",
        "logs": [
            {
                "address": BASE_USDC_ADDRESS,
                "topics": [ERC20_TRANSFER_TOPIC, _addr_topic(payer), _addr_topic(payee)],
                "data": hex(value),
            }
        ],
    }


@pytest.fixture
def hosted(db, monkeypatch):  # noqa: F811
    """The review backend with one configured gateway key and a seeded owner."""
    conn, schema, url = db
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("OPENCROWD_RECEIPT_KEYS", RECEIPT_KEYS)
    monkeypatch.setenv("CROWDCODE_MIN_WEIGHTED_AMOUNT_ATOMIC", "1000")
    # Settings must come from this test's environment, not a developer .env.
    monkeypatch.setattr(settings_mod, "load_dotenv", lambda *a, **k: None)

    @contextmanager
    def connect():
        with psycopg.connect(url, row_factory=dict_row, options=f"-c search_path={schema}") as c:
            yield c

    monkeypatch.setattr(server_mod, "connect", connect)
    monkeypatch.setattr(server_mod, "redact_texts", lambda texts, **_: texts)
    monkeypatch.setattr(server_mod, "redaction_enabled", lambda: True)
    sync_seed_owners(conn, [SEED_OWNER])

    def review(
        *,
        wallet,
        tx,
        att=None,
        rating=4,
        reason=JUDGE_TEXT,
        payer=None,
        value=5000,
        endpoint=ENDPOINT,
        payee=PAYEE,
    ):
        monkeypatch.setattr(
            payments_mod, "_rpc_transaction_receipt",
            lambda rpc, h: _transfer_receipt(payer=payer or wallet.address, payee=payee, value=value),
        )
        identity = build_identity(api_endpoint=endpoint, payment_provider="x402",
                                  payment_target_ref=payee)
        # Sign the identity the backend will resolve to, as a real client does.
        effective = resolve_service(conn, identity).identity or identity
        message = canonical_review_payload(identity=effective, rating=rating, reason=reason,
                                           payment_reference=tx)
        return server_mod.review_service(
            api_endpoint=endpoint, payment_provider="x402", payment_target_ref=payee,
            rating=rating, reason=reason, payment_reference=tx,
            reviewer_wallet=wallet.address,
            review_signature="0x" + wallet.sign_message(encode_defunct(text=message)).signature.hex(),
            gateway_attestation=att,
        )

    return conn, review


def wallet(seed: str) -> Account:
    return Account.from_key("0x" + seed * 32)


def tx_hash(seed: str) -> str:
    return "0x" + seed * 32


# --- the happy path -------------------------------------------------------


def test_an_attested_hosted_review_is_the_only_kind_that_scores(hosted):
    conn, review = hosted
    agent = wallet("42")
    tx = tx_hash("aa")
    result = review(wallet=agent, tx=tx, att=attestation(receipt(tx=tx), reviewer_wallet=agent.address))
    assert result["accepted"], result
    assert result["verified_source"] == "hosted_receipt"
    assert result["counts_towards_ranking"] is True
    assert result["weight_exclusion"] is None
    assert result["payment_verification_level"] == "response_attested"

    row = conn.execute("select * from reviews").fetchone()
    assert row["owner_ref"] == SEED_OWNER
    assert row["verified_source"] == "hosted_receipt"
    assert row["receipt_key_id"] == KID
    assert row["reason"] == JUDGE_TEXT
    # The evidence is retained verbatim so the score can be re-verified.
    assert verify_attestation(row["gateway_attestation"], keys={KID: GATEWAY_KEY.public_key().public_bytes_raw()}).ok

    score = server_mod.get_service_score(api_endpoint=ENDPOINT)
    assert score["num_weighted_reviews"] == 1
    assert score["num_community_reviews"] == 0
    assert score["n_eff"] == pytest.approx(1.0, abs=0.001)
    assert score["unproven"] is False
    assert score["recent_reviews"][0]["counts_towards_ranking"] is True


# --- manipulation attempts ------------------------------------------------


def test_a_forged_receipt_is_rejected_and_stores_nothing(hosted):
    """The core forgery: a well-formed attestation signed by the wrong key."""
    conn, review = hosted
    agent = wallet("42")
    tx = tx_hash("bb")
    forged = attestation(receipt(tx=tx), reviewer_wallet=agent.address)
    other_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32, 64)))
    forged["review_signature"] = base64.urlsafe_b64encode(
        other_key.sign(canonical_payload(forged["review"]).encode("utf-8"))
    ).decode().rstrip("=")

    result = review(wallet=agent, tx=tx, att=forged)
    assert result["accepted"] is False
    assert result["error_code"] == "attestation_rejected"
    assert "signature does not verify" in result["reason"]
    assert conn.execute("select count(*) as n from reviews").fetchone()["n"] == 0
    # And the forgery did not reserve the payment reference either.
    assert review(wallet=agent, tx=tx, att=attestation(receipt(tx=tx), reviewer_wallet=agent.address))["accepted"]


def test_an_unpublished_key_id_is_rejected(hosted):
    _, review = hosted
    agent = wallet("42")
    tx = tx_hash("cc")
    rec = receipt(tx=tx)
    rec["kid"] = "oc-receipt-not-ours"
    result = review(wallet=agent, tx=tx, att=attestation(rec, reviewer_wallet=agent.address))
    assert result["accepted"] is False
    assert "not a published OpenCrowd gateway key" in result["reason"]


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("rating", 5, "not the judge's rating"),
        ("review_text_sha256", "0" * 64, "not the judge's text"),
        ("endpoint", "https://api.provider.example/v1/other", "disagree about the endpoint"),
        ("payee", "0x" + "cd" * 20, "disagree about payee"),
        ("amount_atomic", "999999", "disagree about amount_atomic"),
        ("owner_ref", OTHER_OWNER, "disagree about owner_ref"),
        ("delivery", "excellent", "no delivery outcome"),
    ],
)
def test_an_edited_attestation_is_rejected(hosted, field, value, expected):
    """Every fact the weighting rests on is re-signed; editing one breaks it."""
    _, review = hosted
    agent = wallet("42")
    tx = tx_hash("dd")
    att = attestation(receipt(tx=tx), reviewer_wallet=agent.address, **{field: value})
    # Re-sign the edited review so only the cross-check can catch it.
    att["review_signature"] = _sign(att["review"])
    result = review(wallet=agent, tx=tx, att=att)
    assert result["accepted"] is False, result
    assert expected in result["reason"], result["reason"]


def test_a_receipt_for_another_service_cannot_be_aimed_at_this_one(hosted):
    _, review = hosted
    agent = wallet("42")
    tx = tx_hash("ee")
    elsewhere = receipt(tx=tx, endpoint="https://api.other.example/v1/thing")
    result = review(wallet=agent, tx=tx, att=attestation(elsewhere, reviewer_wallet=agent.address))
    assert result["accepted"] is False
    assert "different service endpoint" in result["reason"]


def test_an_attestation_issued_for_another_wallet_is_rejected(hosted):
    _, review = hosted
    agent, thief = wallet("42"), wallet("43")
    tx = tx_hash("ef")
    stolen = attestation(receipt(tx=tx), reviewer_wallet=agent.address)
    result = review(wallet=thief, tx=tx, att=stolen)
    assert result["accepted"] is False
    assert "different reviewer wallet" in result["reason"]


def test_a_stale_attestation_cannot_be_replayed_later(hosted):
    _, review = hosted
    agent = wallet("42")
    tx = tx_hash("f1")
    old = datetime.now(UTC) - timedelta(hours=3)
    result = review(wallet=agent, tx=tx,
                    att=attestation(receipt(tx=tx, settled_at=old), reviewer_wallet=agent.address,
                                    issued_at=old))
    assert result["accepted"] is False
    assert "stale" in result["reason"]


def test_two_agents_of_one_owner_are_one_vote(hosted):
    """The two-agents-of-one-owner scenario. Both reviews are genuine hosted
    purchases and both are stored; together they are one owner's single vote."""
    conn, review = hosted
    for seed, tx_seed, rating in (("42", "a1", 5), ("43", "a2", 5)):
        agent = wallet(seed)
        tx = tx_hash(tx_seed)
        rec = receipt(tx=tx, attempt_id=f"2222222{seed[0]}-2222-4222-a222-222222222222")
        outcome = review(wallet=agent, tx=tx, rating=rating,
                         att=attestation(rec, reviewer_wallet=agent.address, rating=rating))
        assert outcome["accepted"], outcome
    assert conn.execute("select count(*) as n from reviews").fetchone()["n"] == 2
    score = server_mod.get_service_score(api_endpoint=ENDPOINT)
    assert score["num_weighted_reviews"] == 2
    # Two weighted reviews, one trusted owner, one vote.
    assert score["n_eff"] == pytest.approx(1.0, abs=0.001)
    assert score["score"] == pytest.approx((5 + 2 * 3.0) / 3, abs=0.001)


def test_a_review_of_a_service_its_own_owner_is_paid_by_carries_no_weight(hosted):
    """The self-owned payee scenario: hosted services know both sides, so the
    gateway names the payee's owner and CrowdCode drops the vote."""
    conn, review = hosted
    agent = wallet("42")
    tx = tx_hash("b1")
    rec = receipt(tx=tx, owner_ref=SEED_OWNER, payee_owner_ref=SEED_OWNER)
    result = review(wallet=agent, tx=tx, rating=5,
                    att=attestation(rec, reviewer_wallet=agent.address, rating=5))
    assert result["accepted"] is True
    assert result["weight_exclusion"] == "self_dealing"
    assert result["counts_towards_ranking"] is False
    # Stored, published, and carrying nothing.
    assert conn.execute("select weight_exclusion from reviews").fetchone()["weight_exclusion"] == "self_dealing"
    score = server_mod.get_service_score(api_endpoint=ENDPOINT)
    assert score["num_reviews"] == 1
    assert score["num_weighted_reviews"] == 0
    assert score["n_eff"] == 0.0
    assert score["score"] == 3.0
    # A different owner's payee is fine.
    tx2 = tx_hash("b2")
    ok = review(wallet=wallet("43"), tx=tx2, rating=5,
                att=attestation(receipt(tx=tx2, payee_owner_ref=OTHER_OWNER,
                                        attempt_id="33333333-3333-4333-a333-333333333333"),
                                reviewer_wallet=wallet("43").address, rating=5))
    assert ok["weight_exclusion"] is None, ok


def test_a_dust_payment_buys_no_vote(hosted):
    _, review = hosted
    agent = wallet("42")
    tx = tx_hash("c1")
    result = review(wallet=agent, tx=tx, value=10,
                    att=attestation(receipt(tx=tx, amount="10"), reviewer_wallet=agent.address))
    assert result["accepted"] is True
    assert result["weight_exclusion"] == "dust_payment"
    assert server_mod.get_service_score(api_endpoint=ENDPOINT)["n_eff"] == 0.0


def test_an_unpaid_review_cannot_be_attested(hosted):
    """An unpaid review has no settlement, so it has no receipt to cite."""
    conn, _ = hosted
    agent = wallet("42")
    identity = build_identity(api_endpoint=ENDPOINT)
    identity = resolve_service(conn, identity).identity or identity
    message = canonical_review_payload(identity=identity, rating=1, reason="timed out",
                                       review_nonce="pur_unpaid_0001")
    tx = tx_hash("d1")
    result = server_mod.review_service(
        api_endpoint=ENDPOINT, rating=1, reason="timed out", review_nonce="pur_unpaid_0001",
        reviewer_wallet=agent.address,
        review_signature="0x" + agent.sign_message(encode_defunct(text=message)).signature.hex(),
        gateway_attestation=attestation(receipt(tx=tx), reviewer_wallet=agent.address,
                                        rating=1, review_text="timed out"),
    )
    assert result["accepted"] is False
    assert "unpaid review cannot carry a gateway purchase receipt" in result["reason"]
    # Without the attestation the same unpaid review is accepted as community.
    plain = server_mod.review_service(
        api_endpoint=ENDPOINT, rating=1, reason="timed out", review_nonce="pur_unpaid_0001",
        reviewer_wallet=agent.address,
        review_signature="0x" + agent.sign_message(encode_defunct(text=message)).signature.hex(),
    )
    assert plain["accepted"] and plain["counts_towards_ranking"] is False
    assert conn.execute("select count(*) as n from reviews").fetchone()["n"] == 1


def test_one_receipt_cannot_be_spent_twice(hosted):
    """Both the payment reference and the receipt hash are unique keys."""
    conn, review = hosted
    agent = wallet("42")
    tx = tx_hash("e1")
    rec = receipt(tx=tx)
    assert review(wallet=agent, tx=tx, att=attestation(rec, reviewer_wallet=agent.address))["accepted"]
    again = review(wallet=agent, tx=tx, att=attestation(rec, reviewer_wallet=agent.address))
    assert again["accepted"] is False
    assert again["error_code"] == "payment_reference_used"
    assert conn.execute("select count(*) as n from reviews").fetchone()["n"] == 1
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(
            """insert into reviews(service_id,rating,reason,gateway_attestation)
               select service_id, 5, 'replay', gateway_attestation from reviews""")


def test_trust_is_earned_per_owner_not_per_wallet(hosted):
    """A fresh owner starts at zero whatever wallets it brings, so its first
    hosted reviews are stored, verified and weightless until it earns trust."""
    conn, review = hosted
    for index, seed in enumerate(("51", "52", "53")):
        agent = wallet(seed)
        tx = tx_hash(f"7{index}")
        rec = receipt(tx=tx, owner_ref=OTHER_OWNER,
                      attempt_id=f"4444444{index}-4444-4444-a444-444444444444")
        outcome = review(wallet=agent, tx=tx, rating=5,
                         att=attestation(rec, reviewer_wallet=agent.address, rating=5))
        assert outcome["accepted"], outcome
    assert conn.execute("select count(*) as n from reviews").fetchone()["n"] == 3
    trust = load_trust_map(conn)
    assert trust[OTHER_OWNER].raw_trust == 0.0
    assert trust[OTHER_OWNER].is_seed is False
    score = server_mod.get_service_score(api_endpoint=ENDPOINT)
    assert score["num_weighted_reviews"] == 3
    assert score["n_eff"] == 0.0
    assert score["score"] == 3.0


def test_unsigned_placeholder_reviews_are_never_published(hosted):
    """payments.verify_payment_reference still admits an unsigned stripe/manual
    review. It must not reach any public output."""
    conn, _ = hosted
    accepted = server_mod.review_service(
        api_endpoint=ENDPOINT, payment_provider="stripe", payment_target_ref="acct_123",
        payment_reference="pi_123", rating=5, reason="Flawless, buy immediately",
    )
    assert accepted["accepted"] is True
    assert accepted["weight_exclusion"] == "unsigned_placeholder"
    stored = conn.execute("select weight_exclusion, signature_verified from reviews").fetchone()
    assert stored == {"weight_exclusion": "unsigned_placeholder", "signature_verified": False}

    score = server_mod.get_service_score(api_endpoint=ENDPOINT)
    assert score["num_reviews"] == 0
    assert score["recent_reviews"] == []
    detail, status = server_mod._service_detail_payload(accepted["service_id"])
    assert status == 200
    assert detail["num_reviews"] == 0
    assert detail["recent_reviews"] == []
    assert detail["histogram"] == {str(n): 0 for n in range(1, 6)}
    export = server_mod._knowledge_evidence_payload(50)
    assert all(service["reviews"] == [] for service in export["services"])
    assert "buy immediately" not in json.dumps(
        [score, detail, export, server_mod._top_services_payload(None)]
    )


def test_published_receipt_keys_expose_only_public_material(hosted, monkeypatch):
    from crowdcode.receipts import published_keys

    assert published_keys() == [{"kid": KID, "alg": "ed25519", "public_key": PUBLIC_KEY}]
    monkeypatch.setenv("OPENCROWD_RECEIPT_KEYS", "bad-entry:not-base64-$$$")
    with pytest.raises(ValueError):
        published_keys()


def test_without_configured_keys_nothing_can_be_weighted(hosted, monkeypatch):
    """A deployment that has published no gateway key must not accept an
    attestation at all, rather than trusting it blindly."""
    _, review = hosted
    monkeypatch.delenv("OPENCROWD_RECEIPT_KEYS")
    agent = wallet("42")
    tx = tx_hash("f9")
    result = review(wallet=agent, tx=tx, att=attestation(receipt(tx=tx), reviewer_wallet=agent.address))
    assert result["accepted"] is False
    assert "no published gateway receipt keys" in result["reason"]
