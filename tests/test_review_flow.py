"""Tests for the hash-only signing payload and signature-mismatch recovery."""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from crowdcode.identity import ResolvedService, ServiceIdentity
from crowdcode import payments as payments_mod
from crowdcode import settings as settings_mod
from crowdcode.payments import (
    BASE_USDC_ADDRESS,
    ERC20_TRANSFER_TOPIC,
    REASON_HASH_RE,
    PaymentVerification,
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
    assert verification.error_code == "payment_transfer_mismatch"
    assert not verification.retryable


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
    assert verification.error_code == "payment_transfer_mismatch"


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


def test_mppx_proof_without_token_pin_is_retryable_configuration_error(monkeypatch):
    # Missing token pinning is a server configuration outage. New reviews are
    # rejected and retried later; they are never admitted as signature-only.
    monkeypatch.setattr(settings_mod, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("MPPX_TEMPO_TOKEN_ADDRESS", raising=False)
    monkeypatch.setattr(payments_mod, "_rpc_transaction_receipt", _raise_if_rpc_called)
    verification = _verify_mppx("0x" + "cc" * 20)
    assert not verification.ok
    assert verification.error_code == "payment_verifier_misconfigured"
    assert verification.retryable


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


# Hex tx hashes are case-insensitive: a checksummed reference must match a
# lowercase proof hash (and vice versa), exactly as the proofless path already
# canonicalizes. Regression tests for github issue #2.

UPPER_TX_HASH = "0x" + TX_HASH[2:].upper()


def _verify_x402_case(monkeypatch, *, reference: str, proof_tx: str):
    identity = _x402_identity()
    reason = "fast and correct"
    monkeypatch.setattr(
        payments_mod,
        "_rpc_transaction_receipt",
        lambda rpc, h: _transfer_receipt(payer=ACCOUNT.address, payee=PAYEE),
    )
    return verify_review_payment(
        identity=identity,
        rating=5,
        reason=reason,
        payment_reference=reference,
        payment_proof=json.dumps({"transaction": proof_tx, "network": "base"}),
        reviewer_wallet=ACCOUNT.address,
        review_signature=_sign(
            canonical_review_payload(
                identity=identity, rating=5, reason=reason, payment_reference=reference
            )
        ),
    )


@pytest.mark.parametrize(
    ("reference", "proof_tx"),
    [
        (UPPER_TX_HASH, TX_HASH),
        (TX_HASH, UPPER_TX_HASH),
        (f"x402:base:{UPPER_TX_HASH}", TX_HASH),
    ],
)
def test_x402_proof_accepts_mixed_case_tx_hashes(monkeypatch, reference, proof_tx):
    verification = _verify_x402_case(monkeypatch, reference=reference, proof_tx=proof_tx)
    assert verification.ok, verification.reason
    assert verification.payment_verified
    assert verification.canonical_reference == TX_HASH
    assert verification.metadata["transaction"]["hash"] == TX_HASH


def test_mppx_proof_accepts_mixed_case_tx_hashes(monkeypatch):
    monkeypatch.setenv("MPPX_TEMPO_TOKEN_ADDRESS", TEMPO_TOKEN)
    monkeypatch.setattr(
        payments_mod,
        "_rpc_transaction_receipt",
        lambda rpc, h: _transfer_receipt(
            payer=ACCOUNT.address, payee=PAYEE, token=TEMPO_TOKEN
        ),
    )
    reason = "solid data"
    reference = f"mppx:tempo:{UPPER_TX_HASH}"
    verification = verify_review_payment(
        identity=IDENTITY,  # provider mppx, target PAYEE
        rating=5,
        reason=reason,
        payment_reference=reference,
        payment_proof=json.dumps(
            {"status": "success", "method": "tempo", "reference": TX_HASH}
        ),
        reviewer_wallet=ACCOUNT.address,
        review_signature=_sign(
            canonical_review_payload(
                identity=IDENTITY, rating=5, reason=reason, payment_reference=reference
            )
        ),
    )
    assert verification.ok, verification.reason
    assert verification.payment_verified
    assert verification.canonical_reference == TX_HASH
    assert verification.metadata["transaction"]["hash"] == TX_HASH


def test_proof_still_rejects_a_genuinely_different_tx_hash(monkeypatch):
    other_tx = "0x" + "ef" * 32
    verification = _verify_x402_case(monkeypatch, reference=other_tx, proof_tx=TX_HASH)
    assert not verification.ok
    assert verification.reason == "payment_reference does not match x402 transaction"


@pytest.mark.parametrize(
    ("identity", "proof"),
    [
        (IDENTITY, {"status": "success", "method": "solana", "reference": TX_HASH}),
        (_x402_identity(), {"transaction": TX_HASH, "network": "solana"}),
    ],
)
def test_solana_machine_payments_are_explicitly_unsupported(identity, proof):
    reason = "solid data"
    verification = verify_review_payment(
        identity=identity,
        rating=5,
        reason=reason,
        payment_reference=TX_HASH,
        payment_proof=json.dumps(proof),
        reviewer_wallet=ACCOUNT.address,
        review_signature=_signed_for(identity, reason),
    )
    assert not verification.ok
    assert verification.error_code == "unsupported_payment_chain"
    assert not verification.retryable


def _raise_if_rpc_called(rpc, tx_hash):
    raise AssertionError("chain RPC must not be called for proofless reviews")


def test_proofless_non_tx_reference_is_unsupported_without_rpc(monkeypatch):
    # There is no chain fact to verify, so new machine-payment reviews reject
    # the reference without consulting RPC or storing anything.
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
    assert not verification.ok
    assert verification.error_code == "unsupported_payment_reference"
    assert not verification.retryable
    assert verification.canonical_reference is None


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


def test_txonly_mixed_case_reference_verifies_and_canonicalizes(monkeypatch):
    verification = _verify_txonly(
        monkeypatch,
        _transfer_receipt(payer=ACCOUNT.address, payee=PAYEE),
        reference=UPPER_TX_HASH,
    )
    assert verification.ok, verification.reason
    assert verification.payment_verified is True
    assert verification.canonical_reference == TX_HASH


def test_txonly_wrong_token_is_rejected(monkeypatch):
    verification = _verify_txonly(
        monkeypatch,
        _transfer_receipt(payer=ACCOUNT.address, payee=PAYEE, token="0x" + "cc" * 20),
    )
    assert not verification.ok
    assert verification.error_code == "payment_transfer_mismatch"


def test_txonly_wrong_payee_is_rejected(monkeypatch):
    verification = _verify_txonly(
        monkeypatch,
        _transfer_receipt(payer=ACCOUNT.address, payee="0x" + "77" * 20),
    )
    assert not verification.ok
    assert verification.error_code == "payment_transfer_mismatch"


def test_txonly_failed_tx_is_rejected(monkeypatch):
    receipt = _transfer_receipt(payer=ACCOUNT.address, payee=PAYEE)
    receipt["status"] = "0x0"
    verification = _verify_txonly(monkeypatch, receipt)
    assert not verification.ok
    assert verification.error_code == "payment_transaction_failed"
    assert not verification.retryable


def test_txonly_rpc_unreachable_rejects_without_reserving_reference(monkeypatch):
    verification = _verify_txonly(monkeypatch, lambda rpc, h: None)
    assert not verification.ok
    assert verification.error_code == "payment_rpc_unavailable"
    assert verification.retryable
    assert verification.canonical_reference is None


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


def test_txonly_mppx_without_pin_is_retryable_configuration_error(monkeypatch):
    monkeypatch.setattr(settings_mod, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("MPPX_TEMPO_TOKEN_ADDRESS", raising=False)
    verification = _verify_txonly(
        monkeypatch, _raise_if_rpc_called, identity=IDENTITY
    )
    assert not verification.ok
    assert verification.error_code == "payment_verifier_misconfigured"
    assert verification.retryable


def test_canonical_payment_reference_strips_provider_prefixes():
    from crowdcode.payments import canonical_payment_reference

    assert canonical_payment_reference(TX_HASH) == TX_HASH
    assert canonical_payment_reference(f"x402:base:{TX_HASH}") == TX_HASH
    assert canonical_payment_reference(f"mppx:tempo:{TX_HASH}") == TX_HASH
    upper_hash = "0x" + TX_HASH[2:].upper()
    assert canonical_payment_reference(f"X402:BASE:{upper_hash}") == TX_HASH
    assert canonical_payment_reference("  receipt-ref-1 ") == "receipt-ref-1"


def test_signing_tool_rejects_malformed_hash():
    from crowdcode.server import get_review_signing_payload

    result = get_review_signing_payload(
        rating=5,
        reason_hash="not-a-hash",
        payment_reference="ref-1",
        api_endpoint="https://api.example.com/v1",
    )
    assert result["status"] == "rejected"
    assert result["error_code"] == "invalid_reason_hash"
    assert result["retryable"] is False
    assert result["ok"] is False
    assert result["reason"] == "reason_hash must look like sha256:<64 lowercase hex chars>"


def test_failed_payer_verification_cannot_probe_or_reserve_duplicate_reference(
    monkeypatch,
):
    from crowdcode import server as server_mod

    class NoQueryConn:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("dedup/storage query ran before payer verification")

    @contextmanager
    def fake_connect():
        yield NoQueryConn()

    monkeypatch.setattr(server_mod, "connect", fake_connect)
    monkeypatch.setattr(
        server_mod,
        "resolve_service",
        lambda _conn, _identity: ResolvedService(
            row={"id": IDENTITY.service_id}, identity=IDENTITY
        ),
    )
    monkeypatch.setattr(
        server_mod,
        "verify_review_payment",
        lambda **_kwargs: PaymentVerification(
            False,
            "reviewer wallet did not send the expected token",
            error_code="payment_transfer_mismatch",
        ),
    )

    result = server_mod.review_service(
        rating=5,
        reason="worked",
        payment_reference=TX_HASH,
        service_id=IDENTITY.service_id,
    )
    assert result["status"] == "rejected"
    assert result["error_code"] == "payment_transfer_mismatch"
    assert result["accepted"] is False


def test_backend_exception_is_stable_and_does_not_leak_details(monkeypatch):
    from crowdcode import server as server_mod

    def unavailable():
        raise RuntimeError("secret database hostname and credentials")

    monkeypatch.setattr(server_mod, "connect", unavailable)
    result = server_mod.get_service_score(service_id=IDENTITY.service_id)
    assert result["status"] == "unavailable"
    assert result["error_code"] == "backend_dependency_unavailable"
    assert result["retryable"] is True
    assert result["found"] is False
    assert "secret" not in result["reason"]
    assert result["correlation_id"]


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
