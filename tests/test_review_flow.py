"""Tests for the hash-only signing payload and signature-mismatch recovery."""

from __future__ import annotations

import json

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from crowdcode.identity import ServiceIdentity
from crowdcode import payments as payments_mod
from crowdcode.payments import (
    ERC20_TRANSFER_TOPIC,
    REASON_HASH_RE,
    canonical_review_payload,
    canonical_review_payload_from_hash,
    reason_hash,
    verify_review_payment,
)

IDENTITY = ServiceIdentity(
    service_id="svc_0123456789abcdef0123",
    api_endpoint="https://api.example.com/v1",
    payment_provider="mppx",
    payment_target_ref="0x" + "11" * 20,
)

ACCOUNT = Account.from_key("0x" + "42" * 32)


def _sign(message: str) -> str:
    return ACCOUNT.sign_message(encode_defunct(text=message)).signature.hex()


def test_payload_from_hash_matches_reason_payload():
    reason = "  Great service — très rapide 🚀  "
    assert canonical_review_payload_from_hash(
        identity=IDENTITY,
        rating=5,
        reason_hash=reason_hash(reason),
        payment_reference="ref-1",
    ) == canonical_review_payload(
        identity=IDENTITY, rating=5, reason=reason, payment_reference="ref-1"
    )


def test_reason_hash_format():
    value = reason_hash("anything")
    assert REASON_HASH_RE.match(value)
    assert not REASON_HASH_RE.match("sha256:XYZ")
    assert not REASON_HASH_RE.match("md5:" + "0" * 32)
    assert not REASON_HASH_RE.match("sha256:" + "0" * 63)


def _verify(reason: str, signed_message: str, wallet: str | None = None):
    return verify_review_payment(
        identity=IDENTITY,
        rating=5,
        reason=reason,
        payment_reference="0x" + "ab" * 32,
        payment_proof="Payment-Receipt: bogus",
        reviewer_wallet=wallet or ACCOUNT.address,
        review_signature=_sign(signed_message),
    )


def test_matching_signature_passes_signature_stage():
    reason = "solid service"
    message = canonical_review_payload(
        identity=IDENTITY,
        rating=5,
        reason=reason,
        payment_reference="0x" + "ab" * 32,
    )
    verification = _verify(reason, message)
    # Fails later, at the receipt stage — signature itself was accepted.
    assert not verification.ok
    assert not verification.signature_mismatch
    assert "Payment-Receipt" in verification.reason


def test_mismatched_signature_sets_flag():
    reason = "solid service"
    wrong_identity = ServiceIdentity(
        service_id=None,
        api_endpoint=IDENTITY.api_endpoint,
        payment_provider=IDENTITY.payment_provider,
        payment_target_ref=IDENTITY.payment_target_ref,
    )
    wrong_message = canonical_review_payload(
        identity=wrong_identity,
        rating=5,
        reason=reason,
        payment_reference="0x" + "ab" * 32,
    )
    verification = _verify(reason, wrong_message)
    assert not verification.ok
    assert verification.signature_mismatch
    assert verification.reason == "review_signature does not match reviewer_wallet"


def test_garbage_signature_sets_flag():
    verification = verify_review_payment(
        identity=IDENTITY,
        rating=5,
        reason="x",
        payment_reference="0x" + "ab" * 32,
        payment_proof="Payment-Receipt: bogus",
        reviewer_wallet=ACCOUNT.address,
        review_signature="0x1234",
    )
    assert not verification.ok
    assert verification.signature_mismatch
    assert verification.reason == "review_signature is invalid"


TX_HASH = "0x" + "cd" * 32
PAYEE = "0x" + "11" * 20  # matches IDENTITY.payment_target_ref
FACILITATOR = "0x" + "99" * 20  # gasless relayer / settler (never the payer)


def _addr_topic(addr: str) -> str:
    return "0x" + "0" * 24 + addr[2:].lower()


def _transfer_receipt(*, payer: str, payee: str, value: int = 1000) -> dict:
    """A receipt whose tx sender is a facilitator but whose Transfer event
    proves the real payer — mirrors a gasless x402/mppx settlement."""
    return {
        "status": "0x1",
        "from": FACILITATOR,  # tx sender is the relayer, not the payer
        "blockNumber": "0x1867543",
        "logs": [
            {
                "address": "0x" + "cc" * 20,
                "topics": [
                    ERC20_TRANSFER_TOPIC,
                    _addr_topic(payer),
                    _addr_topic(payee),
                ],
                "data": hex(value),
            }
        ],
    }


def _x402_identity() -> ServiceIdentity:
    return ServiceIdentity(
        service_id=IDENTITY.service_id,
        api_endpoint=IDENTITY.api_endpoint,
        payment_provider="x402",
        payment_target_ref=PAYEE,
    )


def _signed_for(identity: ServiceIdentity, reason: str) -> str:
    return _sign(
        canonical_review_payload(
            identity=identity, rating=5, reason=reason, payment_reference=TX_HASH
        )
    )


def test_x402_gasless_payment_verifies_via_transfer_event(monkeypatch):
    identity = _x402_identity()
    reason = "fast and correct"
    monkeypatch.setattr(
        payments_mod,
        "_rpc_transaction_receipt",
        lambda rpc, h: _transfer_receipt(payer=ACCOUNT.address, payee=PAYEE),
    )
    verification = verify_review_payment(
        identity=identity,
        rating=5,
        reason=reason,
        payment_reference=TX_HASH,
        payment_proof=json.dumps({"transaction": TX_HASH, "network": "base"}),
        reviewer_wallet=ACCOUNT.address,
        review_signature=_signed_for(identity, reason),
    )
    assert verification.ok, verification.reason
    assert verification.payment_verified
    assert verification.metadata["transaction"]["from"] == ACCOUNT.address.lower()


def test_x402_rejects_when_payer_is_not_reviewer_wallet(monkeypatch):
    identity = _x402_identity()
    reason = "fast and correct"
    other = "0x" + "77" * 20
    monkeypatch.setattr(
        payments_mod,
        "_rpc_transaction_receipt",
        lambda rpc, h: _transfer_receipt(payer=other, payee=PAYEE),
    )
    verification = verify_review_payment(
        identity=identity,
        rating=5,
        reason=reason,
        payment_reference=TX_HASH,
        payment_proof=json.dumps({"transaction": TX_HASH, "network": "base"}),
        reviewer_wallet=ACCOUNT.address,
        review_signature=_signed_for(identity, reason),
    )
    assert not verification.ok
    assert verification.reason == "reviewer_wallet did not send the x402 payment"


def test_x402_accepts_dict_payment_proof(monkeypatch):
    # Some MCP transports coerce a JSON-object-shaped string into a dict; the
    # verifier must tolerate a dict payment_proof, not only a string.
    identity = _x402_identity()
    reason = "fast and correct"
    monkeypatch.setattr(
        payments_mod,
        "_rpc_transaction_receipt",
        lambda rpc, h: _transfer_receipt(payer=ACCOUNT.address, payee=PAYEE),
    )
    verification = verify_review_payment(
        identity=identity,
        rating=5,
        reason=reason,
        payment_reference=TX_HASH,
        payment_proof={"transaction": TX_HASH, "network": "base"},
        reviewer_wallet=ACCOUNT.address,
        review_signature=_signed_for(identity, reason),
    )
    assert verification.ok, verification.reason
    assert verification.payment_verified


def test_mppx_gasless_payment_verifies_via_transfer_event(monkeypatch):
    reason = "solid data"
    monkeypatch.setattr(
        payments_mod,
        "_rpc_transaction_receipt",
        lambda rpc, h: _transfer_receipt(payer=ACCOUNT.address, payee=PAYEE),
    )
    receipt_proof = json.dumps(
        {"status": "success", "method": "tempo", "reference": TX_HASH}
    )
    verification = verify_review_payment(
        identity=IDENTITY,  # provider mppx, target PAYEE
        rating=5,
        reason=reason,
        payment_reference=TX_HASH,
        payment_proof=receipt_proof,
        reviewer_wallet=ACCOUNT.address,
        review_signature=_signed_for(IDENTITY, reason),
    )
    assert verification.ok, verification.reason
    assert verification.payment_verified
    assert verification.metadata["provider"] == "mppx"


def test_signing_tool_rejects_malformed_hash():
    from crowdcode.server import get_review_signing_payload

    result = get_review_signing_payload(
        rating=5,
        reason_hash="not-a-hash",
        payment_reference="ref-1",
        api_endpoint="https://api.example.com/v1",
    )
    assert result == {
        "ok": False,
        "reason": "reason_hash must look like sha256:<64 lowercase hex chars>",
    }
