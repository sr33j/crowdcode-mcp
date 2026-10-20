"""x402 on Monad (eip155:143): chain routing, token pinning, and a recorded
mainnet settlement.

The fixture is the eth_getTransactionReceipt result for a real x402 payment
to agent402.tools (/v1/videos/generations), settled by the Monad x402
facilitator (molandak) with USDC transferWithAuthorization: the tx sender is
the facilitator signer and the USDC Transfer log carries the real payer.

Set CROWDCODE_LIVE_MONAD_RPC=1 to also re-fetch it from X402_MONAD_RPC_URL
(default https://rpc.monad.xyz).
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

from crowdcode import payments as payments_mod
from crowdcode.identity import ServiceIdentity
from crowdcode.payments import (
    BASE_RPC_URL,
    MONAD_RPC_URL,
    MONAD_USDC_ADDRESS,
    canonical_payment_reference,
    check_payment_reference_onchain,
    verify_review_payment,
    x402_network,
    x402_reference_network,
)

RECEIPT = json.loads(
    (Path(__file__).parent / "fixtures" / "monad_x402_settlement_receipt.json").read_text()
)
TX = "0x1e24b5403debdc44f5da23a9cb5f2d59ac0d6620dae6f172382a8a58cb181c5c"
PAYER = "0x8443b3E7B593ac30b0AD759802767E7AA17C987d"
PAYEE = "0xaBF4FAbd7c416fB67202E5f9002389Fc75e2a9D0"
FACILITATOR = "0x7f6a2850669202519f0fe8aa912451238820db86"
AMOUNT = 200_000  # 0.20 USDC
# The PAYMENT-RESPONSE header the seller returned, as the client received it.
PAYMENT_RESPONSE = base64.b64encode(json.dumps({
    "success": True,
    "payer": PAYER,
    "transaction": TX,
    "network": "eip155:143",
}).encode()).decode()

IDENTITY = ServiceIdentity(
    service_id="svc_0123456789abcdef0123",
    api_endpoint="https://agent402.tools/v1/videos/generations",
    payment_provider="x402",
    payment_target_ref=PAYEE,
)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ.get("DATABASE_URL") or "postgresql://unused")
    for name in ("X402_USDC_ADDRESS", "X402_MONAD_USDC_ADDRESS",
                 "X402_BASE_RPC_URL", "X402_MONAD_RPC_URL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def rpc_calls(monkeypatch):
    """Serve the recorded receipt and record which RPC URL was asked."""
    calls: list[str] = []

    def receipt(rpc_url, tx_hash):
        calls.append(rpc_url)
        return RECEIPT if tx_hash == TX else None

    monkeypatch.setattr(payments_mod, "_rpc_transaction_receipt", receipt)
    return calls


def _verify(reference, proof=None):
    # The real payer's key is not available; accept its signature so the
    # on-chain half of the check runs against the recorded settlement.
    return verify_review_payment(
        identity=IDENTITY, rating=5, reason="video generated", payment_reference=reference,
        payment_proof=proof, reviewer_wallet=PAYER, review_signature="0xsig",
    )


@pytest.fixture(autouse=True)
def _payer_signature(monkeypatch):
    monkeypatch.setattr(payments_mod, "_recover_eip191", lambda *_: PAYER.lower())


def test_fixture_is_a_facilitator_settlement_of_monad_usdc():
    assert RECEIPT["transactionHash"] == TX
    assert RECEIPT["status"] == "0x1"
    assert RECEIPT["from"] == FACILITATOR
    assert RECEIPT["to"] == MONAD_USDC_ADDRESS


@pytest.mark.parametrize("value", ["monad", "MONAD", "eip155:143", "143", " eip155:143 "])
def test_monad_network_aliases(value):
    assert x402_network(value) == "monad"


@pytest.mark.parametrize("value", ["base", "eip155:8453", "8453"])
def test_base_network_aliases(value):
    assert x402_network(value) == "base"


@pytest.mark.parametrize("value", ["solana:devnet", "eip155:1", "", None, 143.5])
def test_unknown_networks_are_unsupported(value):
    assert x402_network(value) is None


def test_monad_reference_prefix_canonicalizes_to_the_tx_hash():
    assert canonical_payment_reference(f"x402:monad:{TX}") == TX
    assert canonical_payment_reference("X402:MONAD:0x" + TX[2:].upper()) == TX
    assert x402_reference_network(f"x402:monad:{TX}") == "monad"
    assert x402_reference_network(f"x402:base:{TX}") == "base"
    assert x402_reference_network(TX) is None


def test_recorded_settlement_verifies_on_monad(rpc_calls):
    check, meta = check_payment_reference_onchain(
        provider="x402", payment_target_ref=PAYEE, reviewer_wallet=PAYER,
        tx_hash=TX, network="eip155:143",
    )
    assert check.ok, check.failure
    assert check.transfer == {
        "from": PAYER.lower(), "to": PAYEE.lower(), "value": AMOUNT, "token": MONAD_USDC_ADDRESS,
    }
    assert check.block_number == 113171598
    assert meta["network"] == "monad" and meta["token_pinned"]
    assert rpc_calls == [MONAD_RPC_URL]


def test_monad_rpc_url_comes_from_the_environment(monkeypatch, rpc_calls):
    monkeypatch.setenv("X402_MONAD_RPC_URL", "https://monad.example/rpc")
    check, _ = check_payment_reference_onchain(
        provider="x402", payment_target_ref=PAYEE, reviewer_wallet=PAYER, tx_hash=TX, network="monad",
    )
    assert check.ok
    assert rpc_calls == ["https://monad.example/rpc"]


def test_monad_settlement_does_not_verify_as_base(rpc_calls):
    # Base is pinned to Base USDC, so the same logs fail there: the chain
    # table pins a token per network.
    check, meta = check_payment_reference_onchain(
        provider="x402", payment_target_ref=PAYEE, reviewer_wallet=PAYER, tx_hash=TX,
    )
    assert not check.ok and check.failure == "no_matching_transfer"
    assert meta["network"] == "base"
    assert rpc_calls == [BASE_RPC_URL]


def test_unknown_x402_network_fails_without_rpc(rpc_calls):
    check, meta = check_payment_reference_onchain(
        provider="x402", payment_target_ref=PAYEE, reviewer_wallet=PAYER, tx_hash=TX, network="solana",
    )
    assert not check.ok and check.failure == "unsupported_chain"
    assert meta["network"] is None
    assert rpc_calls == []


@pytest.mark.parametrize("reference", [TX, f"x402:monad:{TX}", f"x402:monad:0x{TX[2:].upper()}"])
def test_payment_response_header_verifies_on_monad(rpc_calls, reference):
    verification = _verify(reference, PAYMENT_RESPONSE)
    assert verification.ok, verification.reason
    assert verification.payment_verified
    assert verification.payment_verification_level == "onchain_verified"
    assert verification.amount == AMOUNT
    assert verification.canonical_reference == TX
    assert verification.payment_verification_metadata["network"] == "monad"
    assert verification.metadata["network"] == "monad"
    assert verification.metadata["transaction"]["token"] == MONAD_USDC_ADDRESS
    assert rpc_calls == [MONAD_RPC_URL]


def test_monad_reference_without_proof_verifies_on_monad(rpc_calls):
    verification = _verify(f"x402:monad:{TX}")
    assert verification.ok, verification.reason
    assert verification.payment_verified
    assert verification.payment_verification_metadata["network"] == "monad"
    assert verification.payment_verification_metadata["source"] == "payment_reference"
    assert verification.canonical_reference == TX
    assert rpc_calls == [MONAD_RPC_URL]


def test_bare_hash_without_proof_still_means_base(rpc_calls):
    verification = _verify(TX)
    assert not verification.ok
    assert verification.error_code == "payment_transfer_mismatch"
    assert rpc_calls == [BASE_RPC_URL]


def test_proof_without_network_inherits_the_monad_reference_prefix(rpc_calls):
    verification = _verify(f"x402:monad:{TX}", json.dumps({"transaction": TX}))
    assert verification.ok, verification.reason
    assert rpc_calls == [MONAD_RPC_URL]


def test_base_reference_with_a_monad_proof_is_rejected(rpc_calls):
    verification = _verify(f"x402:base:{TX}", PAYMENT_RESPONSE)
    assert not verification.ok
    assert verification.reason == "payment_reference does not match x402 transaction"
    assert rpc_calls == []


def test_monad_underpayment_is_rejected(rpc_calls):
    proof = json.dumps({"transaction": TX, "network": "eip155:143", "amount": str(AMOUNT + 1)})
    verification = _verify(TX, proof)
    assert not verification.ok
    assert verification.reason == "x402 payment amount is below the challenge price"


def test_monad_rpc_outage_is_retryable_and_names_monad(monkeypatch):
    monkeypatch.setattr(payments_mod, "_rpc_transaction_receipt", lambda *_: None)
    verification = _verify(f"x402:monad:{TX}")
    assert not verification.ok and verification.retryable
    assert verification.reason == "could not verify x402 transaction on Monad"


@pytest.mark.skipif(
    not os.environ.get("CROWDCODE_LIVE_MONAD_RPC"),
    reason="set CROWDCODE_LIVE_MONAD_RPC=1 to hit the Monad RPC",
)
def test_live_monad_rpc_matches_the_recorded_settlement():
    rpc_url = os.environ.get("X402_MONAD_RPC_URL") or MONAD_RPC_URL
    live = payments_mod._rpc_transaction_receipt(rpc_url, TX)
    assert live is not None, "Monad RPC unreachable"
    assert {k: live[k] for k in ("status", "from", "to", "blockNumber", "logs")} == {
        k: RECEIPT[k] for k in ("status", "from", "to", "blockNumber", "logs")
    }
    check, meta = check_payment_reference_onchain(
        provider="x402", payment_target_ref=PAYEE, reviewer_wallet=PAYER, tx_hash=TX, network="monad",
    )
    assert check.ok and check.transfer["value"] == AMOUNT and meta["network"] == "monad"
