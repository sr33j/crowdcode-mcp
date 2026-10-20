"""PostgreSQL tests for x402 Solana reviews: front-running protection for
unlinked observations, deletion replay keys, and per-family payee slots."""

from __future__ import annotations

import json
import time
from contextlib import contextmanager

import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from eth_account import Account
from eth_account.messages import encode_defunct
from psycopg.rows import dict_row

from crowdcode import payments, server
from crowdcode.payments import (
    BASE_USDC_ADDRESS,
    ERC20_TRANSFER_TOPIC,
    payer_link_message,
    reason_hash,
    unlinked_reference_key,
)
from crowdcode.review_management import management_message
from crowdcode.solana import b58encode
from tests.test_shared_wallet_db import db  # noqa: F401  isolated PostgreSQL schema fixture
from tests.test_solana_x402 import FIXTURE, PAYEE, SIG, _with_payer

PAYER_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
PAYER = b58encode(PAYER_KEY.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
TX = _with_payer(FIXTURE, PAYER)
REFERENCE = f"x402:solana:{SIG}"
ENDPOINT = "https://api.solana-seller.example/v1/search"
EVM_PAYEE = "0x" + "ab" * 20
PAYER_REVIEWER = Account.from_key("0x" + "42" * 32)
ATTACKER = Account.from_key("0x" + "43" * 32)
BYSTANDER = Account.from_key("0x" + "44" * 32)


@pytest.fixture
def backend(db, monkeypatch):  # noqa: F811
    conn, schema, url = db

    @contextmanager
    def connect():
        with psycopg.connect(url, row_factory=dict_row, options=f"-c search_path={schema}") as c:
            yield c

    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("SOLANA_RPC_URL", raising=False)
    monkeypatch.setattr(server, "connect", connect)
    monkeypatch.setattr(server, "redact_texts", lambda texts, **_: texts)
    monkeypatch.setattr(server, "redaction_enabled", lambda: True)
    monkeypatch.setattr(payments, "_solana_transaction", lambda _url, sig: TX if sig == SIG else None)
    return conn


def review(account, *, payee=PAYEE, reference=REFERENCE, link=False, rating=5, reason="fresh results"):
    args = dict(api_endpoint=ENDPOINT, payment_provider="x402", payment_target_ref=payee,
                payment_reference=reference)
    message = server.get_review_signing_payload(**args, rating=rating, reason_hash=reason_hash(reason))["message"]
    extra = {}
    if link:
        text = payer_link_message(reviewer_wallet=account.address, payload=message)
        extra["payer_signature"] = b58encode(PAYER_KEY.sign(text.encode()))
    return server.review_service(
        **args, rating=rating, reason=reason, reviewer_wallet=account.address,
        review_signature=account.sign_message(encode_defunct(text=message)).signature.hex(), **extra,
    )


def delete(account, review_id):
    expires_at = int(time.time()) + 240
    signature = account.sign_message(encode_defunct(text=management_message(
        "delete", account.address, expires_at, review_id=review_id))).signature.hex()
    return server.delete_my_review(review_id=review_id, reviewer_wallet=account.address,
                                   expires_at=expires_at, authorization=signature)


def rows(conn):
    return conn.execute(
        """select reviewer_wallet, payment_reference, payment_reference_canonical,
                  payment_verification_level, payment_verified, amount
           from reviews order by id""").fetchall()


def test_unlinked_observation_cannot_front_run_the_payer(backend):
    conn = backend
    attacker = review(ATTACKER)
    assert attacker["accepted"], attacker
    assert attacker["payment_verification_level"] == "signature_only"
    assert not attacker["payment_verified"]
    # The endpoint is registered without a payee: nothing linked a payment.
    service = conn.execute("select payment_provider, payment_target_ref from services").fetchone()
    assert service == {"payment_provider": None, "payment_target_ref": None}
    assert review(ATTACKER)["error_code"] == "payment_reference_used"  # one per wallet

    payer = review(PAYER_REVIEWER, link=True)
    assert payer["accepted"] and payer["payment_verified"], payer
    assert payer["payment_verification_level"] == "onchain_verified"
    assert payer["service_id"] == attacker["service_id"]
    assert review(PAYER_REVIEWER, link=True)["error_code"] == "payment_reference_used"
    # Once linked, nobody else can attach the payment, linked or not.
    assert review(BYSTANDER)["error_code"] == "payment_reference_used"

    assert rows(conn) == [
        {"reviewer_wallet": ATTACKER.address.lower(), "payment_reference": None,
         "payment_reference_canonical": unlinked_reference_key(ATTACKER.address, SIG),
         "payment_verification_level": "signature_only", "payment_verified": False, "amount": None},
        {"reviewer_wallet": PAYER_REVIEWER.address.lower(), "payment_reference": REFERENCE,
         "payment_reference_canonical": SIG,
         "payment_verification_level": "onchain_verified", "payment_verified": True, "amount": 12000},
    ]
    # The first verified payment registered the Solana payee.
    service = conn.execute("select payment_provider, payment_target_ref from services").fetchone()
    assert service == {"payment_provider": "x402", "payment_target_ref": PAYEE}
    metadata = conn.execute("select payment_proof from reviews where payment_verified").fetchone()["payment_proof"]
    assert metadata["payer_link"]["payer"] == PAYER
    assert json.loads(json.dumps(metadata["review_payload"]))["payment_reference"] == REFERENCE


def test_deleting_an_unlinked_observation_does_not_block_the_payer(backend):
    attacker = review(ATTACKER)
    assert delete(ATTACKER, attacker["review_id"])["deleted"]
    assert review(ATTACKER)["error_code"] == "review_deleted"
    payer = review(PAYER_REVIEWER, link=True)
    assert payer["accepted"] and payer["payment_verified"], payer
    assert delete(PAYER_REVIEWER, payer["review_id"])["deleted"]
    assert review(PAYER_REVIEWER, link=True)["error_code"] == "review_deleted"
    assert review(BYSTANDER)["error_code"] == "review_deleted"


def test_bad_payer_signature_stores_nothing(backend):
    conn = backend
    # A payer link signed for one reviewer cannot be reused by another.
    args = dict(api_endpoint=ENDPOINT, payment_provider="x402", payment_target_ref=PAYEE,
                payment_reference=REFERENCE)
    message = server.get_review_signing_payload(**args, rating=5, reason_hash=reason_hash("ok"))["message"]
    forged = b58encode(PAYER_KEY.sign(payer_link_message(
        reviewer_wallet=PAYER_REVIEWER.address, payload=message).encode()))
    rejected = server.review_service(
        **args, rating=5, reason="ok", reviewer_wallet=BYSTANDER.address,
        review_signature=BYSTANDER.sign_message(encode_defunct(text=message)).signature.hex(),
        payer_signature=forged,
    )
    assert rejected["error_code"] == "payer_signature_invalid" and not rejected["accepted"]
    assert rejected["expected_payer_message"].startswith("CrowdCode review payer link v1\n")
    assert conn.execute("select count(*) as n from reviews").fetchone()["n"] == 0


def _base_review(monkeypatch, account):
    topic = lambda address: "0x" + "0" * 24 + address[2:].lower()  # noqa: E731
    monkeypatch.setattr(payments, "_rpc_transaction_receipt", lambda *_: {
        "status": "0x1", "blockNumber": "0x10", "logs": [{
            "address": BASE_USDC_ADDRESS,
            "topics": [ERC20_TRANSFER_TOPIC, topic(account.address), topic(EVM_PAYEE)],
            "data": hex(200000),
        }],
    })
    return review(account, payee=EVM_PAYEE, reference="0x" + "55" * 32)


def test_evm_service_gains_one_solana_payee_at_first_linked_payment(backend, monkeypatch):
    conn = backend
    base = _base_review(monkeypatch, BYSTANDER)
    assert base["accepted"] and base["payment_verified"], base
    # Unlinked observations never register a payee.
    assert review(ATTACKER)["accepted"]
    assert conn.execute("select count(*) as n from service_identifiers where identifier_value = %s",
                        (f"x402:{PAYEE}",)).fetchone()["n"] == 0
    solana = review(PAYER_REVIEWER, link=True)
    assert solana["accepted"] and solana["payment_verified"], solana
    assert solana["service_id"] == base["service_id"]
    identifiers = {r["identifier_value"] for r in conn.execute(
        "select identifier_value from service_identifiers where identifier_type = 'payment_target'")}
    assert identifiers == {f"x402:{EVM_PAYEE}", f"x402:{PAYEE}"}
    # The canonical EVM payee is unchanged, and the Solana slot is now taken.
    service = conn.execute("select payment_target_ref from services").fetchone()
    assert service["payment_target_ref"] == EVM_PAYEE
    other = server.get_review_signing_payload(
        api_endpoint=ENDPOINT, payment_provider="x402", payment_target_ref=PAYER,
        payment_reference=REFERENCE, rating=5, reason_hash=reason_hash("x"))
    assert not other["ok"] and other["reason"] == "service identity conflict"
