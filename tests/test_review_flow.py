"""Tests for the hash-only signing payload and signature-mismatch recovery."""

from __future__ import annotations

import json

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from crowdcode.identity import ServiceIdentity
from crowdcode.payments import (
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
