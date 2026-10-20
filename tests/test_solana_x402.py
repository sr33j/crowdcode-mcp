"""x402 on Solana: SPL USDC transfer verification, case-sensitive references,
and ed25519 payer linkage.

The fixture is the getTransaction (jsonParsed, maxSupportedTransactionVersion
0) result for a real x402 `exact` payment on Solana mainnet settled by the
PayAI facilitator: the facilitator (CjNF…eKww) is the fee payer, and the
payer (2b1J…wSLP) signs an SPL transferChecked of 0.012 USDC into the payee's
(53Jh…uC7t) token account, alongside two compute-budget instructions and a
memo. The payer's key is not available, so linkage tests rewrite the payer to
a deterministic test key ("_with_payer").

Set CROWDCODE_LIVE_SOLANA_RPC=1 to also re-fetch it from SOLANA_RPC_URL
(default https://api.mainnet-beta.solana.com).
"""

from __future__ import annotations

import base64
import copy
import json
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from eth_account import Account
from eth_account.messages import encode_defunct

from crowdcode import payments as payments_mod
from crowdcode.identity import ServiceIdentity
from crowdcode.payments import (
    SOLANA_USDC_MINT,
    canonical_payment_reference,
    canonical_review_payload,
    check_payment_reference_onchain,
    check_solana_payment_onchain,
    payer_link_message,
    solana_reference_signature,
    unlinked_reference_key,
    verify_review_payment,
    x402_network,
    x402_reference_network,
)
from crowdcode.solana import (
    SOLANA_RPC_URL,
    b58decode,
    b58encode,
    find_usdc_transfers,
    is_solana_address,
    is_solana_signature,
    rpc_get_transaction,
    verify_ed25519,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "solana_x402_settlement_transaction.json").read_text()
)
SIG = "5eHrTHo8wSNNNf857kCqzX15YW1AaETvyDW5bAyv3EBZp57PFhXMGj4zoi9mYi9XGKEA5bCzNFTdm2mdaeLwH5uj"
PAYER = "2b1JD7tcidGtAYCVm1dDKTgVHRoihSJ1uqz3jf9WwSLP"
PAYEE = "53JhuF8bgxvUQ59nDG6kWs4awUQYCS3wswQmUsV5uC7t"
FEE_PAYER = "CjNFTjvBhbJJd2B5ePPMHRLx1ELZpa8dwQgGL727eKww"
AMOUNT = 12_000  # 0.012 USDC
REFERENCE = f"x402:solana:{SIG}"

REVIEWER = Account.from_key("0x" + "42" * 32)
PAYER_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
TEST_PAYER = b58encode(PAYER_KEY.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))

IDENTITY = ServiceIdentity(
    service_id="svc_0123456789abcdef0123",
    api_endpoint="https://api.solana-seller.example/v1/search",
    payment_provider="x402",
    payment_target_ref=PAYEE,
)


def _with_payer(tx: dict, payer: str) -> dict:
    """The recorded transaction with its payer (signer, authority, and source
    token account owner) replaced by ``payer``."""
    return json.loads(json.dumps(tx).replace(PAYER, payer))


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ.get("DATABASE_URL") or "postgresql://unused")
    for name in ("SOLANA_RPC_URL", "X402_SOLANA_USDC_MINT"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def chain(monkeypatch):
    """Serve a transaction for SIG and record the RPC URLs asked."""
    state = {"tx": FIXTURE, "calls": []}

    def get(rpc_url, signature):
        state["calls"].append(rpc_url)
        return state["tx"] if signature == SIG else None

    monkeypatch.setattr(payments_mod, "_solana_transaction", get)
    monkeypatch.setattr(payments_mod, "_rpc_transaction_receipt",
                        lambda *_: pytest.fail("EVM RPC used for a Solana payment"))
    return state


def _sign_review(identity=IDENTITY, reference=REFERENCE, rating=5, reason="fresh results"):
    message = canonical_review_payload(
        identity=identity, rating=rating, reason=reason, payment_reference=reference
    )
    return message, REVIEWER.sign_message(encode_defunct(text=message)).signature.hex()


def _payer_signature(payload: str, wallet: str = REVIEWER.address, key=PAYER_KEY) -> str:
    return b58encode(key.sign(payer_link_message(reviewer_wallet=wallet, payload=payload).encode()))


def _verify(reference=REFERENCE, identity=IDENTITY, **extra):
    _, signature = _sign_review(identity, reference)
    return verify_review_payment(
        identity=identity, rating=5, reason="fresh results", payment_reference=reference,
        reviewer_wallet=REVIEWER.address, review_signature=signature, **extra,
    )


# --- primitives -------------------------------------------------------------

def test_fixture_is_a_facilitator_paid_usdc_transfer_checked():
    message = FIXTURE["transaction"]["message"]
    assert FIXTURE["transaction"]["signatures"][0] == SIG
    assert message["accountKeys"][0]["pubkey"] == FEE_PAYER  # facilitator pays fees
    assert FIXTURE["meta"]["err"] is None
    (transfer,) = [ix for ix in message["instructions"] if ix.get("program") == "spl-token"]
    assert transfer["parsed"]["type"] == "transferChecked"
    assert transfer["parsed"]["info"]["mint"] == SOLANA_USDC_MINT
    assert transfer["parsed"]["info"]["authority"] == PAYER


def test_base58_roundtrip_and_shapes():
    for raw in (b"\x00\x00\x01", bytes(range(32)), b"\xff" * 64):
        assert b58decode(b58encode(raw)) == raw
    assert b58decode("") is None
    assert is_solana_address(PAYER) and is_solana_address(SOLANA_USDC_MINT)
    assert is_solana_signature(SIG) and not is_solana_address(SIG)
    for bad in ("", "0x" + "ab" * 32, SIG + "0", SIG[:-1] + "l", None, 5):
        assert not is_solana_signature(bad)


def test_ed25519_rfc8032_vector_and_rejections():
    # RFC 8032 §7.1 TEST 1 (empty message).
    public = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
    signature = bytes.fromhex(
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
        "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
    )
    assert verify_ed25519(b58encode(public), b"", b58encode(signature))
    assert not verify_ed25519(b58encode(public), b"x", b58encode(signature))
    assert not verify_ed25519(b58encode(public), b"", b58encode(signature[:-1] + b"\x00"))
    assert not verify_ed25519("not-base58-0OIl", b"", b58encode(signature))


@pytest.mark.parametrize("value", ["solana", "SOLANA", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp", " solana "])
def test_solana_network_aliases(value):
    assert x402_network(value) == "solana"


def test_solana_references_stay_case_sensitive():
    assert canonical_payment_reference(REFERENCE) == SIG
    assert canonical_payment_reference("X402:SOLANA:" + SIG) == SIG
    assert canonical_payment_reference(SIG) == SIG
    assert canonical_payment_reference(REFERENCE.lower()) != SIG
    assert x402_reference_network(REFERENCE) == "solana"
    assert solana_reference_signature(REFERENCE) == SIG
    assert solana_reference_signature(SIG) == SIG
    assert solana_reference_signature("x402:base:0x" + "ab" * 32) is None


def test_payer_link_message_binds_payload_and_reviewer():
    payload, _ = _sign_review()
    message = payer_link_message(reviewer_wallet=REVIEWER.address, payload=payload)
    lines = message.split("\n")
    assert lines[0] == "CrowdCode review payer link v1"
    assert lines[1] == "reviewer_wallet:" + REVIEWER.address.lower()
    assert lines[2].startswith("payload_sha256:") and len(lines[2]) == 15 + 64
    assert message == payer_link_message(reviewer_wallet=REVIEWER.address.lower(), payload=payload)
    assert message != payer_link_message(reviewer_wallet="0x" + "11" * 20, payload=payload)


# --- chain check --------------------------------------------------------------

def test_recorded_transfer_is_found_for_the_payee():
    transfers = find_usdc_transfers(FIXTURE, payee=PAYEE)
    assert transfers.by_payer == {PAYER: AMOUNT}
    assert transfers.payee_net_delta == AMOUNT
    assert transfers.fee_payer == FEE_PAYER
    assert transfers.slot == 459255525
    # The payer's own account is the source, not a destination.
    assert find_usdc_transfers(FIXTURE, payee=PAYER).by_payer == {}


def test_recorded_settlement_verifies_on_solana(chain):
    check, meta = check_solana_payment_onchain(payment_target_ref=PAYEE, signature=SIG)
    assert check.ok, check.failure
    assert check.transfer == {"by_payer": {PAYER: AMOUNT}, "to": PAYEE,
                              "token": SOLANA_USDC_MINT, "fee_payer": FEE_PAYER}
    assert meta == {"network": "solana", "token_pinned": True, "amount_checked": False,
                    "verification_failure": None}
    assert chain["calls"] == [SOLANA_RPC_URL]


def test_solana_rpc_url_comes_from_the_environment(monkeypatch, chain):
    monkeypatch.setenv("SOLANA_RPC_URL", "https://solana.example/rpc")
    assert check_solana_payment_onchain(payment_target_ref=PAYEE, signature=SIG)[0].ok
    assert chain["calls"] == ["https://solana.example/rpc"]


def _mutate(path_fn):
    tx = copy.deepcopy(FIXTURE)
    path_fn(tx)
    return tx


@pytest.mark.parametrize(
    ("mutation", "failure"),
    [
        (lambda tx: tx["meta"].update(err={"InstructionError": [2, {"Custom": 1}]}), "tx_failed"),
        # A look-alike token: same instruction shape, different mint.
        (lambda tx: [b.update(mint="So11111111111111111111111111111111111111112")
                     for b in tx["meta"]["preTokenBalances"] + tx["meta"]["postTokenBalances"]],
         "no_matching_transfer"),
        # Token-2022 cannot move classic USDC; never trust it.
        (lambda tx: [ix.update(programId="TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
                     for ix in tx["transaction"]["message"]["instructions"]],
         "no_matching_transfer"),
    ],
)
def test_failed_or_unpinned_transfers_do_not_verify(chain, mutation, failure):
    chain["tx"] = _mutate(mutation)
    check, meta = check_solana_payment_onchain(payment_target_ref=PAYEE, signature=SIG)
    assert not check.ok and check.failure == failure == meta["verification_failure"]


def test_other_payee_payer_and_unknown_payee(chain):
    other = TEST_PAYER
    assert check_solana_payment_onchain(payment_target_ref=other, signature=SIG)[0].failure == "no_matching_transfer"
    assert check_solana_payment_onchain(payment_target_ref=PAYEE, signature=SIG, payer=other)[0].failure == "payer_mismatch"
    assert check_solana_payment_onchain(payment_target_ref="0x" + "11" * 20, signature=SIG)[0].failure == "payee_unknown"
    assert chain["calls"] == [SOLANA_RPC_URL, SOLANA_RPC_URL]


def test_missing_transaction_is_retryable(chain):
    chain["tx"] = None
    check, _ = check_solana_payment_onchain(payment_target_ref=PAYEE, signature=SIG)
    assert check.failure == "rpc_unreachable"
    verification = _verify()
    assert not verification.ok and verification.retryable
    assert verification.error_code == "payment_rpc_unavailable"


def test_evm_check_never_handles_solana():
    check, meta = check_payment_reference_onchain(
        provider="x402", payment_target_ref=PAYEE, reviewer_wallet=REVIEWER.address,
        tx_hash=SIG, network="solana",
    )
    assert check.failure == "unsupported_chain" and meta["network"] is None


# --- review admission ---------------------------------------------------------

def test_unlinked_solana_payment_is_observed_at_signature_only(chain):
    verification = _verify()
    assert verification.ok, verification.reason
    assert verification.signature_verified and not verification.payment_verified
    assert verification.payment_verification_level == "signature_only"
    assert verification.amount is None
    # Never reserves the public reference: a reviewer-scoped key instead.
    assert not verification.claims_reference
    assert verification.canonical_reference == unlinked_reference_key(REVIEWER.address, SIG)
    assert verification.payment_verification_metadata["verification_failure"] == "payer_unlinked"
    assert verification.payment_verification_metadata["payer_linked"] is False
    assert verification.metadata["transaction"]["payers"] == {PAYER: AMOUNT}
    assert verification.metadata["review_payload"]["payment_reference"] == REFERENCE


@pytest.mark.parametrize("reference", [REFERENCE, SIG, "X402:Solana:" + SIG])
def test_payer_signature_links_the_payment(chain, reference):
    chain["tx"] = _with_payer(FIXTURE, TEST_PAYER)
    payload, _ = _sign_review(reference=reference)
    verification = _verify(reference, payer_signature=_payer_signature(payload))
    assert verification.ok, verification.reason
    assert verification.payment_verified and verification.claims_reference
    assert verification.payment_verification_level == "onchain_verified"
    assert verification.amount == AMOUNT
    assert verification.canonical_reference == SIG
    assert verification.metadata["transaction"]["from"] == TEST_PAYER
    assert verification.metadata["payer_link"]["payer"] == TEST_PAYER
    assert verification.payment_verification_metadata == {
        "network": "solana", "token_pinned": True, "amount_checked": False,
        "verification_failure": None, "proof_present": False,
        "source": "payment_reference", "payer_linked": True,
    }
    # payer_wallet is optional, but must name the signer when given.
    named = _verify(reference, payer_signature=_payer_signature(payload), payer_wallet=TEST_PAYER)
    assert named.ok and named.payment_verified


def test_invalid_payer_signature_is_rejected_not_downgraded(chain):
    chain["tx"] = _with_payer(FIXTURE, TEST_PAYER)
    payload, _ = _sign_review()
    other_key = Ed25519PrivateKey.from_private_bytes(b"\x07" * 32)
    for bad in (
        _payer_signature(payload, key=other_key),  # not the payer
        _payer_signature(payload, wallet="0x" + "11" * 20),  # another reviewer
        _payer_signature(payload.replace('"rating":5', '"rating":4')),  # another review
    ):
        verification = _verify(payer_signature=bad)
        assert not verification.ok and verification.error_code == "payer_signature_invalid"
        assert verification.expected_payer_message == payer_link_message(
            reviewer_wallet=REVIEWER.address, payload=payload)
    malformed = _verify(payer_signature="0x" + "ab" * 64)
    assert not malformed.ok and "base58" in malformed.reason


def test_payer_wallet_must_have_paid(chain):
    payload, _ = _sign_review()
    verification = _verify(payer_signature=_payer_signature(payload), payer_wallet=TEST_PAYER)
    assert not verification.ok and verification.error_code == "payment_transfer_mismatch"
    assert not _verify(payer_wallet=TEST_PAYER).ok  # payer_wallet alone proves nothing


def test_failed_chain_checks_reject_even_without_linkage(chain):
    chain["tx"] = _mutate(lambda tx: tx["meta"].update(err={"InstructionError": [2, "x"]}))
    verification = _verify()
    assert not verification.ok and verification.error_code == "payment_transaction_failed"
    chain["tx"] = FIXTURE
    wrong_payee = ServiceIdentity(**{**IDENTITY.__dict__, "payment_target_ref": TEST_PAYER})
    verification = _verify(identity=wrong_payee)
    assert not verification.ok and verification.error_code == "payment_transfer_mismatch"


def _payment_response(**overrides) -> str:
    body = {"success": True, "transaction": SIG, "network": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
            "payer": TEST_PAYER, **overrides}
    return base64.b64encode(json.dumps(body).encode()).decode()


def test_payment_response_header_verifies_with_linkage(chain):
    chain["tx"] = _with_payer(FIXTURE, TEST_PAYER)
    payload, _ = _sign_review()
    verification = _verify(payment_proof=_payment_response(), payer_signature=_payer_signature(payload))
    assert verification.ok and verification.payment_verified, verification.reason
    assert verification.payment_verification_metadata["source"] == "payment_proof"
    priced = _verify(payment_proof=_payment_response(amount=str(AMOUNT)),
                     payer_signature=_payer_signature(payload))
    assert priced.ok and priced.payment_verification_metadata["amount_checked"]


@pytest.mark.parametrize(
    "proof",
    [
        _payment_response(transaction=SIG.lower()),  # base58 is case-sensitive
        _payment_response(network="eip155:8453"),
        _payment_response(amount=str(AMOUNT + 1)),
        "not a proof",
    ],
)
def test_mismatched_payment_response_is_rejected(chain, proof):
    assert not _verify(payment_proof=proof).ok


def test_payer_signature_is_only_for_x402_solana(chain):
    base_ref = "0x" + "ab" * 32
    base_identity = ServiceIdentity(**{**IDENTITY.__dict__, "payment_target_ref": "0x" + "11" * 20})
    verification = _verify(reference=base_ref, identity=base_identity, payer_signature=SIG)
    assert not verification.ok and "only used for x402 Solana" in verification.reason
    mppx = ServiceIdentity(**{**IDENTITY.__dict__, "payment_provider": "mppx"})
    assert not _verify(identity=mppx).ok


@pytest.mark.skipif(not os.environ.get("CROWDCODE_LIVE_SOLANA_RPC"), reason="live RPC opt-in")
def test_live_rpc_matches_fixture():
    tx = rpc_get_transaction(os.environ.get("SOLANA_RPC_URL") or SOLANA_RPC_URL, SIG)
    assert tx is not None
    assert find_usdc_transfers(tx, payee=PAYEE).by_payer == {PAYER: AMOUNT}


def test_cdp_solana_sign_message_vector():
    # Recorded from Coinbase CDP `cdp.solana.signMessage({ address, message })`
    # (cdp-sdk 1.55.0) with an unfunded probe account: CDP signs the raw UTF-8
    # bytes and returns a base58 signature — exactly what payer_signature is.
    address = "DP3RL5cQLovdirSF7vE1eHB9oSm2now49DCPMUDjbMpj"
    message = payer_link_message(
        reviewer_wallet="0x0000000000000000000000000000000000000001", payload="")
    message = message.rsplit(":", 1)[0] + ":" + "ab" * 32
    signature = ("654zhkVUMgG2YoBLy1Yrvdw5AN3giEP7WGC78F7kod96C9vRMh8PDnny1CTQ8xz3"
                 "y6NsXa7XZR1XUaTtiFEgCN8q")
    assert verify_ed25519(address, message.encode(), signature)
