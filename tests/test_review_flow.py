"""Tests for the hash-only signing payload and signature-mismatch recovery."""

from __future__ import annotations

import json

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from crowdcode.identity import ServiceIdentity
from crowdcode import payments as payments_mod
from crowdcode import settings as settings_mod
from crowdcode.payments import (
    BASE_USDC_ADDRESS,
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


@pytest.fixture(autouse=True)
def _default_token_pinning(monkeypatch):
    """Token pinning must be decided by each test, never by whatever the
    developer happens to have in .env."""
    monkeypatch.delenv("X402_USDC_ADDRESS", raising=False)


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


def _transfer_receipt(
    *, payer: str, payee: str, value: int = 1000, token: str = BASE_USDC_ADDRESS
) -> dict:
    """A receipt whose tx sender is a facilitator but whose Transfer event
    proves the real payer — mirrors a gasless x402/mppx settlement."""
    return {
        "status": "0x1",
        "from": FACILITATOR,  # tx sender is the relayer, not the payer
        "blockNumber": "0x1867543",
        "logs": [
            {
                "address": token,
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
    assert verification.payment_verification_level == "onchain_verified"
    assert verification.payment_verification_metadata["proof_present"] is True
    assert verification.payment_verification_metadata["source"] == "payment_proof"
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
    assert verification.reason == "reviewer_wallet did not send the x402 payment in USDC"


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


def test_x402_rejects_a_transfer_of_a_different_token(monkeypatch):
    # Token pinning (docs/SCORING.md §3.4): without it a self-minted worthless
    # ERC-20 would buy the verified-purchase weight multiplier.
    identity = _x402_identity()
    reason = "fast and correct"
    monkeypatch.setattr(
        payments_mod,
        "_rpc_transaction_receipt",
        lambda rpc, h: _transfer_receipt(
            payer=ACCOUNT.address, payee=PAYEE, token="0x" + "cc" * 20
        ),
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
    assert "USDC" in verification.reason


def test_x402_rejects_an_underpayment_and_records_the_amount(monkeypatch):
    identity = _x402_identity()
    reason = "fast and correct"
    monkeypatch.setattr(
        payments_mod,
        "_rpc_transaction_receipt",
        lambda rpc, h: _transfer_receipt(
            payer=ACCOUNT.address, payee=PAYEE, value=1000
        ),
    )

    def verify(max_amount: str):
        return verify_review_payment(
            identity=identity,
            rating=5,
            reason=reason,
            payment_reference=TX_HASH,
            payment_proof=json.dumps(
                {
                    "transaction": TX_HASH,
                    "network": "base",
                    "maxAmountRequired": max_amount,
                }
            ),
            reviewer_wallet=ACCOUNT.address,
            review_signature=_signed_for(identity, reason),
        )

    under = verify("5000")
    assert not under.ok
    assert "below the challenge price" in under.reason

    paid = verify("1000")
    assert paid.ok, paid.reason
    assert paid.amount == 1000


TEMPO_TOKEN = "0x20c000000000000000000000b9537d11c60e8b50"


def _verify_mppx(token: str):
    reason = "solid data"
    receipt_proof = json.dumps(
        {"status": "success", "method": "tempo", "reference": TX_HASH}
    )
    return verify_review_payment(
        identity=IDENTITY,  # provider mppx, target PAYEE
        rating=5,
        reason=reason,
        payment_reference=TX_HASH,
        payment_proof=receipt_proof,
        reviewer_wallet=ACCOUNT.address,
        review_signature=_signed_for(IDENTITY, reason),
    )


def test_mppx_proof_without_token_pin_caps_at_signature_only(monkeypatch):
    # Unpinned (MPPX_TEMPO_TOKEN_ADDRESS unset): pinning is symmetric across
    # the proof and tx-only paths, so nothing upgrades to onchain_verified —
    # but a missing pin is server config, never the client's fault, so the
    # review is accepted at signature_only (not rejected) and the chain is
    # never consulted. Stub load_dotenv too — otherwise a get_settings() call
    # mid-verification re-reads the developer's .env and re-pins the token.
    monkeypatch.setattr(settings_mod, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("MPPX_TEMPO_TOKEN_ADDRESS", raising=False)
    monkeypatch.setattr(payments_mod, "_rpc_transaction_receipt", _raise_if_rpc_called)
    verification = _verify_mppx("0x" + "cc" * 20)
    assert verification.ok, verification.reason
    assert verification.payment_verified is False
    assert verification.payment_verification_level == "signature_only"
    assert verification.payment_verification_metadata["token_pinned"] is False
    assert (
        verification.payment_verification_metadata["verification_failure"]
        == "token_not_pinned"
    )


def test_mppx_pins_the_configured_tempo_token(monkeypatch):
    monkeypatch.setenv("MPPX_TEMPO_TOKEN_ADDRESS", TEMPO_TOKEN)
    monkeypatch.setattr(
        payments_mod,
        "_rpc_transaction_receipt",
        lambda rpc, h: _transfer_receipt(
            payer=ACCOUNT.address, payee=PAYEE, token=TEMPO_TOKEN
        ),
    )
    paid = _verify_mppx(TEMPO_TOKEN)
    assert paid.ok, paid.reason
    assert paid.amount == 1000

    monkeypatch.setattr(
        payments_mod,
        "_rpc_transaction_receipt",
        lambda rpc, h: _transfer_receipt(
            payer=ACCOUNT.address, payee=PAYEE, token="0x" + "cc" * 20
        ),
    )
    wrong = _verify_mppx("0x" + "cc" * 20)
    assert not wrong.ok
    assert "expected token" in wrong.reason


def _raise_if_rpc_called(rpc, tx_hash):
    raise AssertionError("chain RPC must not be called for proofless reviews")


def test_proofless_non_tx_reference_accepted_unverified_without_rpc(monkeypatch):
    # No payment_proof and a payment_reference that is not a tx hash: the
    # review passes on signature alone, marked signature_only, and never
    # touches the chain RPC (there is nothing to look up).
    monkeypatch.setattr(payments_mod, "_rpc_transaction_receipt", _raise_if_rpc_called)
    reason = "worked well, forgot to save the receipt"
    reference = "receipt-ref-1"
    verification = verify_review_payment(
        identity=IDENTITY,
        rating=5,
        reason=reason,
        payment_reference=reference,
        payment_proof=None,
        reviewer_wallet=ACCOUNT.address,
        review_signature=_sign(
            canonical_review_payload(
                identity=IDENTITY, rating=5, reason=reason, payment_reference=reference
            )
        ),
    )
    assert verification.ok, verification.reason
    assert verification.payment_verified is False
    assert verification.signature_verified is True
    assert verification.payment_verification_level == "signature_only"
    assert verification.reviewer_id == payments_mod._reviewer_id_from_wallet(
        ACCOUNT.address
    )
    assert verification.metadata["proof"] is None
    assert verification.canonical_reference == reference


def test_proofless_review_still_rejects_bad_signature(monkeypatch):
    monkeypatch.setattr(payments_mod, "_rpc_transaction_receipt", _raise_if_rpc_called)
    reason = "worked well"
    wrong_identity = ServiceIdentity(
        service_id=None,
        api_endpoint=IDENTITY.api_endpoint,
        payment_provider=IDENTITY.payment_provider,
        payment_target_ref=IDENTITY.payment_target_ref,
    )
    verification = verify_review_payment(
        identity=IDENTITY,
        rating=5,
        reason=reason,
        payment_reference=TX_HASH,
        payment_proof=None,
        reviewer_wallet=ACCOUNT.address,
        review_signature=_signed_for(wrong_identity, reason),
    )
    assert not verification.ok
    assert verification.signature_mismatch


def test_missing_wallet_sets_missing_wallet_flag():
    verification = verify_review_payment(
        identity=IDENTITY,
        rating=5,
        reason="x",
        payment_reference=TX_HASH,
        payment_proof=None,
        reviewer_wallet=None,
        review_signature=None,
    )
    assert not verification.ok
    assert verification.missing_wallet
    assert "reviewer_wallet is required" in verification.reason


def test_missing_signature_sets_missing_wallet_flag():
    verification = verify_review_payment(
        identity=IDENTITY,
        rating=5,
        reason="x",
        payment_reference=TX_HASH,
        payment_proof=None,
        reviewer_wallet=ACCOUNT.address,
        review_signature=None,
    )
    assert not verification.ok
    assert verification.missing_wallet
    assert "review_signature is required" in verification.reason


def test_supplied_but_failing_proof_stays_hard_rejection(monkeypatch):
    # A proof that fails on-chain verification must NOT downgrade to the
    # unverified-accepted path.
    identity = _x402_identity()
    reason = "fast and correct"
    monkeypatch.setattr(
        payments_mod, "_rpc_transaction_receipt", lambda rpc, h: None
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
    assert verification.reason == "could not verify x402 transaction on Base"


# --- tx-hash-only verification (no payment_proof) -------------------------
#
# The proof header is unsigned client JSON: every load-bearing fact comes
# from the chain. A settlement tx hash in payment_reference therefore earns
# the same onchain_verified level as a proof — and a failed tx-only check
# falls back to signature_only instead of rejecting.


def _verify_txonly(
    monkeypatch,
    receipt,
    *,
    identity=None,
    reference=TX_HASH,
):
    identity = identity or _x402_identity()
    reason = "fast and correct"
    monkeypatch.setattr(
        payments_mod,
        "_rpc_transaction_receipt",
        receipt if callable(receipt) else (lambda rpc, h: receipt),
    )
    return verify_review_payment(
        identity=identity,
        rating=5,
        reason=reason,
        payment_reference=reference,
        payment_proof=None,
        reviewer_wallet=ACCOUNT.address,
        review_signature=_sign(
            canonical_review_payload(
                identity=identity, rating=5, reason=reason, payment_reference=reference
            )
        ),
    )


def test_txonly_matching_transfer_upgrades_to_onchain_verified(monkeypatch):
    verification = _verify_txonly(
        monkeypatch, _transfer_receipt(payer=ACCOUNT.address, payee=PAYEE)
    )
    assert verification.ok, verification.reason
    assert verification.payment_verified is True
    assert verification.payment_verification_level == "onchain_verified"
    assert verification.amount == 1000
    assert verification.canonical_reference == TX_HASH
    meta = verification.payment_verification_metadata
    assert meta["proof_present"] is False
    assert meta["source"] == "payment_reference"
    assert meta["network"] == "base"
    assert meta["token_pinned"] is True
    assert meta["amount_checked"] is False  # no challenge amount without a proof
    assert meta["verification_failure"] is None
    assert verification.metadata["transaction"]["from"] == ACCOUNT.address.lower()


def test_txonly_prefixed_reference_verifies_and_canonicalizes(monkeypatch):
    verification = _verify_txonly(
        monkeypatch,
        _transfer_receipt(payer=ACCOUNT.address, payee=PAYEE),
        reference=f"x402:base:{TX_HASH}",
    )
    assert verification.ok, verification.reason
    assert verification.payment_verified is True
    assert verification.canonical_reference == TX_HASH


def test_txonly_wrong_token_falls_back_to_signature_only(monkeypatch):
    verification = _verify_txonly(
        monkeypatch,
        _transfer_receipt(payer=ACCOUNT.address, payee=PAYEE, token="0x" + "cc" * 20),
    )
    assert verification.ok, verification.reason
    assert verification.payment_verified is False
    assert verification.payment_verification_level == "signature_only"
    assert (
        verification.payment_verification_metadata["verification_failure"]
        == "no_matching_transfer"
    )


def test_txonly_wrong_payee_falls_back_to_signature_only(monkeypatch):
    verification = _verify_txonly(
        monkeypatch,
        _transfer_receipt(payer=ACCOUNT.address, payee="0x" + "77" * 20),
    )
    assert verification.ok, verification.reason
    assert verification.payment_verified is False
    assert verification.payment_verification_level == "signature_only"
    assert (
        verification.payment_verification_metadata["verification_failure"]
        == "no_matching_transfer"
    )


def test_txonly_failed_tx_falls_back_to_signature_only(monkeypatch):
    receipt = _transfer_receipt(payer=ACCOUNT.address, payee=PAYEE)
    receipt["status"] = "0x0"
    verification = _verify_txonly(monkeypatch, receipt)
    assert verification.ok, verification.reason
    assert verification.payment_verified is False
    assert (
        verification.payment_verification_metadata["verification_failure"]
        == "tx_failed"
    )


def test_txonly_rpc_unreachable_is_recorded_as_retryable(monkeypatch):
    # An unreachable RPC accepts at signature_only — never rejects — and
    # records rpc_unreachable so the nightly cron can upgrade it later.
    verification = _verify_txonly(monkeypatch, lambda rpc, h: None)
    assert verification.ok, verification.reason
    assert verification.payment_verified is False
    assert verification.payment_verification_level == "signature_only"
    failure = verification.payment_verification_metadata["verification_failure"]
    assert failure == "rpc_unreachable"
    assert failure in payments_mod.RETRYABLE_FAILURES


def test_txonly_mppx_with_pinned_token_verifies(monkeypatch):
    monkeypatch.setenv("MPPX_TEMPO_TOKEN_ADDRESS", TEMPO_TOKEN)
    verification = _verify_txonly(
        monkeypatch,
        _transfer_receipt(payer=ACCOUNT.address, payee=PAYEE, token=TEMPO_TOKEN),
        identity=IDENTITY,  # provider mppx
    )
    assert verification.ok, verification.reason
    assert verification.payment_verified is True
    assert verification.payment_verification_level == "onchain_verified"
    assert verification.payment_verification_metadata["network"] == "tempo"


def test_txonly_mppx_without_pin_stays_signature_only_without_rpc(monkeypatch):
    monkeypatch.setattr(settings_mod, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("MPPX_TEMPO_TOKEN_ADDRESS", raising=False)
    verification = _verify_txonly(
        monkeypatch, _raise_if_rpc_called, identity=IDENTITY
    )
    assert verification.ok, verification.reason
    assert verification.payment_verified is False
    assert verification.payment_verification_level == "signature_only"
    assert (
        verification.payment_verification_metadata["verification_failure"]
        == "token_not_pinned"
    )


def test_canonical_payment_reference_strips_provider_prefixes():
    from crowdcode.payments import canonical_payment_reference

    assert canonical_payment_reference(TX_HASH) == TX_HASH
    assert canonical_payment_reference(f"x402:base:{TX_HASH}") == TX_HASH
    assert canonical_payment_reference(f"mppx:tempo:{TX_HASH}") == TX_HASH
    assert canonical_payment_reference("  receipt-ref-1 ") == "receipt-ref-1"


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


def test_project_ideas_public_payload_reads_cron_cache_only(monkeypatch):
    from crowdcode import server as server_mod

    cached = {
        "ok": True,
        "source": "cron",
        "cached": True,
        "stale": False,
        "ideas": [{"title": "Cached"}],
    }
    monkeypatch.setattr(
        server_mod,
        "_cron_project_ideas_payload",
        lambda _ttl: cached,
    )

    assert server_mod._project_ideas_payload() is cached
