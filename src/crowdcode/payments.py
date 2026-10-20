from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from eth_account import Account
from eth_account.messages import encode_defunct

from crowdcode.identity import MACHINE_PAYMENT_PROVIDERS, ServiceIdentity
from crowdcode.rate_limit import identity_id_from_wallet
from crowdcode.settings import get_settings
from crowdcode.solana import (
    SOLANA_COMMITMENT,
    SOLANA_RPC_URL,
    SOLANA_USDC_MINT,
    find_usdc_transfers,
    is_solana_address,
    is_solana_signature,
    rpc_get_transaction,
    verify_ed25519,
)

EVM_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
TX_HASH_RE = re.compile(r"^0x[a-fA-F0-9]{64}$")

# keccak256("Transfer(address,address,uint256)")
ERC20_TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)

TEMPO_RPC_URL = "https://rpc.tempo.xyz"
BASE_RPC_URL = "https://mainnet.base.org"
MONAD_RPC_URL = "https://rpc.monad.xyz"

# Token pinning (docs/SCORING.md §3.4): a verified payment must move the
# expected token, otherwise a self-minted worthless ERC-20 buys the verified
# multiplier. x402 on Base and Monad is pinned to native Circle USDC by
# default; the Tempo token for mppx must be configured via
# MPPX_TEMPO_TOKEN_ADDRESS — without it, new mppx reviews fail closed with a
# retryable configuration error.
BASE_USDC_ADDRESS = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
MONAD_USDC_ADDRESS = "0x754704bc059f8c67012fed69bc8a327a5aafb603"


@dataclass(frozen=True)
class X402Chain:
    network: str
    label: str
    aliases: frozenset[str]
    rpc_env: str
    default_rpc_url: str
    token_env: str
    default_token: str
    # "evm" (EIP-3009 USDC Transfer log, payer = reviewer_wallet) or "svm"
    # (SPL transfer, payer linked by an ed25519 payer_signature).
    kind: str = "evm"


# Chains x402 payments may settle on. `network` is the short name used in
# x402:<network>:<tx> references and stored in verification metadata so the
# cron re-verify pass checks the same chain.
X402_CHAINS = {
    "base": X402Chain(
        network="base",
        label="Base",
        aliases=frozenset({"base", "eip155:8453", "8453"}),
        rpc_env="X402_BASE_RPC_URL",
        default_rpc_url=BASE_RPC_URL,
        token_env="X402_USDC_ADDRESS",
        default_token=BASE_USDC_ADDRESS,
    ),
    "monad": X402Chain(
        network="monad",
        label="Monad",
        aliases=frozenset({"monad", "eip155:143", "143"}),
        rpc_env="X402_MONAD_RPC_URL",
        default_rpc_url=MONAD_RPC_URL,
        token_env="X402_MONAD_USDC_ADDRESS",
        default_token=MONAD_USDC_ADDRESS,
    ),
    # Aliases are matched lowercased (x402_network); the reference itself, a
    # base58 transaction signature, stays case-sensitive.
    "solana": X402Chain(
        network="solana",
        label="Solana",
        aliases=frozenset({"solana", "solana:5eykt4usfv8p8njdtrepy1vzqkqzkvdp"}),
        rpc_env="SOLANA_RPC_URL",
        default_rpc_url=SOLANA_RPC_URL,
        token_env="X402_SOLANA_USDC_MINT",
        default_token=SOLANA_USDC_MINT,
        kind="svm",
    ),
}
DEFAULT_X402_NETWORK = "base"
SOLANA_NETWORK = "solana"


def x402_network(value: Any) -> str | None:
    """Map an x402 network label (name, CAIP-2 id, or chain id) to its short
    name in X402_CHAINS; None when the chain is unsupported."""
    cleaned = str(value).strip().lower()
    for chain in X402_CHAINS.values():
        if cleaned in chain.aliases:
            return chain.network
    return None

# What the payment check actually proved (docs/SCORING.md §3.4).
LEVEL_UNVERIFIED = "unverified"
LEVEL_SIGNATURE_ONLY = "signature_only"
LEVEL_ONCHAIN_VERIFIED = "onchain_verified"
LEVEL_RESPONSE_ATTESTED = "response_attested"

# Levels that count as a verified purchase (payment_verified boolean).
VERIFIED_LEVELS = frozenset({LEVEL_ONCHAIN_VERIFIED, LEVEL_RESPONSE_ATTESTED})

# Why an on-chain check failed. RPC-unreachable and token-not-pinned failures
# reject new reviews without reserving the reference; the constants also
# support re-verification of legacy signature-only rows.
FAILURE_RPC_UNREACHABLE = "rpc_unreachable"
FAILURE_TX_FAILED = "tx_failed"
FAILURE_NO_MATCHING_TRANSFER = "no_matching_transfer"
FAILURE_TOKEN_NOT_PINNED = "token_not_pinned"
FAILURE_PAYEE_UNKNOWN = "payee_unknown"
# Solana only: the transfer verified but no payer_signature linked the payer
# to reviewer_wallet, so the review is admitted at signature_only.
FAILURE_PAYER_UNLINKED = "payer_unlinked"
# Solana only: payer_wallet sent nothing to the payee in this transaction.
FAILURE_PAYER_MISMATCH = "payer_mismatch"
RETRYABLE_FAILURES = frozenset({FAILURE_RPC_UNREACHABLE})

# Dedup/storage form of payment_reference: the same settlement tx must not
# yield two reviews under 0x<hash> and x402:base:<hash> spellings.
_REFERENCE_PREFIX_RE = re.compile(
    r"^(?:x402:(?:" + "|".join(X402_CHAINS) + r"):|mppx:tempo:)", re.IGNORECASE
)
_X402_REFERENCE_NETWORK_RE = re.compile(r"^x402:([a-z0-9]+):", re.IGNORECASE)


def canonical_payment_reference(reference: str) -> str:
    """Signatures are always verified against the reference exactly as
    submitted (the client signed that string); this canonical form exists
    only for dedup, storage, and on-chain lookup. Only EVM hashes are
    lowercased: Solana base58 signatures are case-sensitive."""
    canonical = _REFERENCE_PREFIX_RE.sub("", reference.strip())
    return canonical.lower() if TX_HASH_RE.match(canonical) else canonical


# An unlinked Solana observation is stored under a reviewer-scoped key rather
# than the bare signature: it must never reserve the payment the real payer
# may still claim with a payer_signature (front-running protection).
UNLINKED_REFERENCE_PREFIX = "unlinked:"


def unlinked_reference_key(reviewer_wallet: str, canonical: str) -> str:
    return f"{UNLINKED_REFERENCE_PREFIX}{reviewer_wallet.lower()}:{canonical}"


def solana_reference_signature(reference: str | None) -> str | None:
    """The base58 transaction signature named by x402:solana:<sig> (prefix
    case-insensitive) or a bare signature; None for anything else."""
    if reference is None:
        return None
    cleaned = reference.strip()
    if cleaned[:12].lower() == "x402:solana:":
        cleaned = cleaned[12:]
    return cleaned if is_solana_signature(cleaned) else None


PAYER_LINK_MESSAGE_HEADER = "CrowdCode review payer link v1"


def payer_link_message(*, reviewer_wallet: str, payload: str) -> str:
    """The text a Solana payer signs (ed25519, raw UTF-8 bytes) to link its
    payment to a review: it commits to the exact canonical review payload the
    reviewer signed and to the reviewer wallet, so it cannot be replayed onto
    another review or another reviewer (spec/CANONICAL_PAYLOAD.md)."""
    return "\n".join([
        PAYER_LINK_MESSAGE_HEADER,
        f"reviewer_wallet:{reviewer_wallet.strip().lower()}",
        "payload_sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    ])


def x402_reference_network(reference: str) -> str | None:
    """The chain named by an x402:<network>:<tx> reference prefix, or None for
    a bare transaction hash (which settles on DEFAULT_X402_NETWORK unless a
    payment_proof names another chain)."""
    match = _X402_REFERENCE_NETWORK_RE.match(reference.strip())
    return match.group(1).lower() if match else None


@dataclass(frozen=True)
class PaymentVerification:
    ok: bool
    reason: str
    reviewer_id: str | None = None
    metadata: dict[str, Any] | None = None
    reviewer_wallet: str | None = None
    review_signature: str | None = None
    signature_scheme: str | None = None
    payment_verified: bool = False
    signature_verified: bool = False
    # Token base units actually transferred (ERC-20 Transfer value) when a
    # payment proof verified; persisted to reviews.amount.
    amount: int | None = None
    # True when the failure was specifically the EIP-191 signature not
    # matching the server-side canonical payload — lets review_service return
    # the expected message so clients can re-sign after an identity
    # resolution race.
    signature_mismatch: bool = False
    # True when the failure was the absence of a signing wallet/signature —
    # lets review_service attach an install-wallet next_step without
    # string-matching the reason.
    missing_wallet: bool = False
    # What this verification actually proved; payment_verified stays the
    # derived boolean (level in VERIFIED_LEVELS) for compatibility.
    payment_verification_level: str = LEVEL_UNVERIFIED
    # Compact machine-readable trail: proof_present, source, network,
    # token_pinned, amount_checked, verification_failure. Persisted to
    # reviews.payment_verification_metadata (cron keys retries off it).
    payment_verification_metadata: dict[str, Any] | None = None
    # payment_reference with any provider prefix stripped; persisted to
    # reviews.payment_reference_canonical for dedup.
    canonical_reference: str | None = None
    error_code: str | None = None
    retryable: bool = False
    # False for an unlinked Solana observation: the review is stored without
    # payment_reference and must not reserve the globally unique reference.
    claims_reference: bool = True
    # Set when payer_signature did not verify, so review_service can return
    # the exact payer message to sign.
    expected_payer_message: str | None = None


def reviewer_id_from_payment(payment_reference: str) -> str:
    settings = get_settings()
    material = f"{settings.reviewer_salt}:{payment_reference.strip()}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


REASON_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def reason_hash(reason: str) -> str:
    return "sha256:" + hashlib.sha256(reason.strip().encode("utf-8")).hexdigest()


def canonical_review_payload_from_hash(
    *,
    identity: ServiceIdentity,
    rating: int,
    reason_hash: str,
    payment_reference: str | None = None,
    review_nonce: str | None = None,
) -> str:
    payload = {
        "type": "crowdcode.review.v1",
        "service_id": identity.service_id,
        "api_endpoint": identity.api_endpoint,
        "payment_provider": identity.payment_provider,
        "payment_target_ref": identity.payment_target_ref,
        "directory_slug": identity.directory_slug,
        "payment_reference": payment_reference.strip() if payment_reference is not None else None,
        "rating": rating,
        "reason_hash": reason_hash,
    }
    if payment_reference is None:
        payload["type"] = "crowdcode.review.v2"
        payload["review_nonce"] = review_nonce
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def canonical_review_payload(
    *,
    identity: ServiceIdentity,
    rating: int,
    reason: str,
    payment_reference: str | None = None,
    review_nonce: str | None = None,
) -> str:
    return canonical_review_payload_from_hash(
        identity=identity,
        rating=rating,
        reason_hash=reason_hash(reason),
        payment_reference=payment_reference,
        review_nonce=review_nonce,
    )


def verify_payment_reference(payment_reference: str) -> tuple[bool, str]:
    """Compatibility gate for providers without strong verification yet."""
    if not payment_reference or not payment_reference.strip():
        return False, "payment_reference is required"

    return True, "verified by v1 placeholder"


def verify_review_payment(
    *,
    identity: ServiceIdentity,
    rating: int,
    reason: str,
    payment_reference: str | None = None,
    review_nonce: str | None = None,
    payment_proof: str | None = None,
    payment_challenge: str | None = None,
    reviewer_wallet: str | None = None,
    review_signature: str | None = None,
    signature_scheme: str = "eip191",
    payer_signature: str | None = None,
    payer_wallet: str | None = None,
) -> PaymentVerification:
    if (payer_signature is not None or payer_wallet is not None) and (
        identity.payment_provider != "x402" or solana_reference_signature(payment_reference) is None
    ):
        return PaymentVerification(
            False, "payer_signature and payer_wallet are only used for x402 Solana payments"
        )
    if payment_reference is not None and not payment_reference.strip():
        return PaymentVerification(False, "omit payment_reference when no payment is claimed")
    if payment_reference is None:
        if payment_proof is not None or payment_challenge is not None:
            return PaymentVerification(False, "payment evidence requires payment_reference")
        if not review_nonce or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", review_nonce):
            return PaymentVerification(False, "review_nonce must be an 8-128 character unique identifier for an unpaid review")
    elif review_nonce is not None:
        return PaymentVerification(False, "review_nonce is only used when payment_reference is omitted")
    elif identity.payment_provider is None:
        return PaymentVerification(False, "payment_provider is required when claiming payment")

    if payment_reference is None or identity.payment_provider in MACHINE_PAYMENT_PROVIDERS:
        return _verify_signed_machine_payment(
            identity=identity,
            rating=rating,
            reason=reason,
            payment_reference=payment_reference,
            review_nonce=review_nonce,
            payment_proof=payment_proof,
            payment_challenge=payment_challenge,
            reviewer_wallet=reviewer_wallet,
            review_signature=review_signature,
            signature_scheme=signature_scheme,
            payer_signature=payer_signature,
            payer_wallet=payer_wallet,
        )

    payment_ok, payment_reason = verify_payment_reference(payment_reference)
    if not payment_ok:
        return PaymentVerification(False, payment_reason)

    return PaymentVerification(
        True,
        payment_reason,
        reviewer_id=reviewer_id_from_payment(payment_reference),
        metadata={"mode": "placeholder"},
        payment_verification_level=LEVEL_UNVERIFIED,
        payment_verification_metadata={
            "proof_present": False,
            "source": None,
            "verification_failure": None,
        },
        canonical_reference=canonical_payment_reference(payment_reference),
    )


def _verify_signed_machine_payment(
    *,
    identity: ServiceIdentity,
    rating: int,
    reason: str,
    payment_reference: str | None,
    review_nonce: str | None,
    payment_proof: str | None,
    payment_challenge: str | None,
    reviewer_wallet: str | None,
    review_signature: str | None,
    signature_scheme: str,
    payer_signature: str | None = None,
    payer_wallet: str | None = None,
) -> PaymentVerification:
    if not reviewer_wallet:
        return PaymentVerification(
            False,
            "reviewer_wallet is required for signed reviews",
            missing_wallet=True,
        )
    if not review_signature:
        return PaymentVerification(
            False,
            "review_signature is required for signed reviews",
            missing_wallet=True,
        )
    if signature_scheme != "eip191":
        return PaymentVerification(False, "only eip191 signatures are supported for signed reviews")

    wallet = _normalize_evm_address(reviewer_wallet)
    if wallet is None:
        return PaymentVerification(False, "reviewer_wallet must be an EVM 0x address")

    payload = canonical_review_payload(
        identity=identity,
        rating=rating,
        reason=reason,
        payment_reference=payment_reference,
        review_nonce=review_nonce,
    )
    recovered = _recover_eip191(payload, review_signature)
    if recovered is None:
        return PaymentVerification(
            False, "review_signature is invalid", signature_mismatch=True
        )
    if recovered.lower() != wallet.lower():
        return PaymentVerification(
            False,
            "review_signature does not match reviewer_wallet",
            signature_mismatch=True,
        )

    if payment_reference is None:
        return PaymentVerification(
            True,
            "reviewer signature verified; payment not verified",
            reviewer_id=_reviewer_id_from_wallet(wallet),
            metadata={"review_payload": json.loads(payload), "signature_recovered_wallet": recovered},
            reviewer_wallet=wallet,
            review_signature=review_signature.strip(),
            signature_scheme=signature_scheme,
            signature_verified=True,
            payment_verification_level=LEVEL_SIGNATURE_ONLY,
            payment_verification_metadata={"proof_present": False, "source": None, "verification_failure": None},
        )

    canonical = canonical_payment_reference(payment_reference)

    solana_signature = (
        solana_reference_signature(payment_reference)
        if identity.payment_provider == "x402"
        else None
    )
    if solana_signature is not None:
        payment = _verify_solana_x402_payment(
            identity=identity,
            payment_reference=payment_reference,
            signature=solana_signature,
            payment_proof=payment_proof,
            reviewer_wallet=wallet,
            payload=payload,
            payer_signature=payer_signature,
            payer_wallet=payer_wallet,
        )
        if not payment.ok:
            return payment
        metadata = dict(payment.metadata or {})
        metadata["review_payload"] = json.loads(payload)
        metadata["signature_recovered_wallet"] = recovered
        return PaymentVerification(
            True,
            payment.reason,
            reviewer_id=_reviewer_id_from_wallet(wallet),
            metadata=metadata,
            reviewer_wallet=wallet,
            review_signature=review_signature.strip(),
            signature_scheme=signature_scheme,
            payment_verified=payment.payment_verification_level in VERIFIED_LEVELS,
            signature_verified=True,
            amount=payment.amount,
            payment_verification_level=payment.payment_verification_level,
            payment_verification_metadata=payment.payment_verification_metadata,
            canonical_reference=payment.canonical_reference,
            claims_reference=payment.claims_reference,
        )

    # Proof is optional, but a supported-chain transaction hash is mandatory.
    # New machine-payment reviews never downgrade a definitive chain failure
    # to signature_only: doing so would let an attacker reserve another
    # payer's public transaction hash before the real payer reviews it.
    if not payment_proof:
        if not TX_HASH_RE.match(canonical):
            return PaymentVerification(
                False,
                "payment_reference must be an EVM transaction hash for x402 Base/Monad or "
                "mppx Tempo, or x402:solana:<signature> for x402 Solana",
                error_code="unsupported_payment_reference",
            )
        network = None
        if identity.payment_provider == "x402":
            network = x402_reference_network(payment_reference) or DEFAULT_X402_NETWORK
        check, verification_meta = check_payment_reference_onchain(
            provider=identity.payment_provider,
            payment_target_ref=identity.payment_target_ref,
            reviewer_wallet=wallet,
            tx_hash=canonical,
            network=network,
        )
        verification_meta["proof_present"] = False
        verification_meta["source"] = "payment_reference"
        if not check.ok or check.transfer is None:
            return _chain_failure(identity.payment_provider, check, verification_meta["network"])
        metadata: dict[str, Any] = {
            "review_payload": json.loads(payload),
            "signature_recovered_wallet": recovered,
            "proof": None,
            "transaction": {
                "hash": canonical,
                "from": check.transfer["from"],
                "to": check.transfer["to"],
                "value": str(check.transfer["value"])
                if check.transfer["value"] is not None
                else None,
                "token": check.transfer["token"],
                "block_number": check.block_number,
            },
        }
        return PaymentVerification(
            True,
            "signature verified; payment verified on-chain from payment_reference",
            reviewer_id=_reviewer_id_from_wallet(wallet),
            metadata=metadata,
            reviewer_wallet=wallet,
            review_signature=review_signature.strip(),
            signature_scheme=signature_scheme,
            payment_verified=True,
            signature_verified=True,
            amount=check.transfer["value"],
            payment_verification_level=LEVEL_ONCHAIN_VERIFIED,
            payment_verification_metadata=verification_meta,
            canonical_reference=canonical,
        )

    if identity.payment_provider == "mppx":
        payment = _verify_mppx_payment(
            identity=identity,
            payment_reference=payment_reference,
            payment_proof=payment_proof,
            payment_challenge=payment_challenge,
            reviewer_wallet=wallet,
        )
    elif identity.payment_provider == "x402":
        payment = _verify_x402_payment(
            identity=identity,
            payment_reference=payment_reference,
            payment_proof=payment_proof,
            reviewer_wallet=wallet,
        )
    else:
        payment = PaymentVerification(False, "unsupported payment provider")

    if not payment.ok:
        return payment

    metadata = dict(payment.metadata or {})
    metadata["review_payload"] = json.loads(payload)
    metadata["signature_recovered_wallet"] = recovered

    return PaymentVerification(
        True,
        payment.reason,
        reviewer_id=_reviewer_id_from_wallet(wallet),
        metadata=metadata,
        reviewer_wallet=wallet,
        review_signature=review_signature.strip(),
        signature_scheme=signature_scheme,
        payment_verified=payment.payment_verification_level in VERIFIED_LEVELS,
        signature_verified=True,
        amount=payment.amount,
        payment_verification_level=payment.payment_verification_level,
        payment_verification_metadata=payment.payment_verification_metadata,
        canonical_reference=canonical,
    )


def _verify_mppx_payment(
    *,
    identity: ServiceIdentity,
    payment_reference: str,
    payment_proof: str,
    payment_challenge: str | None,
    reviewer_wallet: str,
) -> PaymentVerification:
    receipt = _parse_payment_receipt(payment_proof)
    if receipt is None:
        return PaymentVerification(False, "payment_proof must be a valid mppx Payment-Receipt")
    if receipt.get("status") != "success":
        return PaymentVerification(
            False, "mppx receipt status is not success", error_code="payment_failed"
        )
    if receipt.get("method") != "tempo":
        return PaymentVerification(
            False,
            "only mppx Tempo payments are supported",
            error_code="unsupported_payment_chain",
        )

    tx_hash = str(receipt.get("reference") or "").strip()
    if not TX_HASH_RE.match(tx_hash):
        return PaymentVerification(False, "mppx receipt reference must be a transaction hash")
    # Hex tx hashes are case-insensitive: compare (and look up) in lowercase
    # so a checksummed reference matches, like the proofless path does.
    tx_hash = tx_hash.lower()
    if payment_reference.strip().lower() not in {tx_hash, f"mppx:tempo:{tx_hash}"}:
        return PaymentVerification(False, "payment_reference does not match mppx receipt reference")

    check, verification_meta = check_payment_reference_onchain(
        provider="mppx",
        payment_target_ref=identity.payment_target_ref,
        reviewer_wallet=reviewer_wallet,
        tx_hash=tx_hash,
    )
    verification_meta["proof_present"] = True
    verification_meta["source"] = "payment_proof"

    if not check.ok or check.transfer is None:
        return _chain_failure("mppx", check, verification_meta["network"])

    transfer = check.transfer
    metadata: dict[str, Any] = {
        "provider": "mppx",
        "receipt": receipt,
        "transaction": {
            "hash": tx_hash,
            "from": transfer["from"],
            "to": transfer["to"],
            "value": str(transfer["value"]) if transfer["value"] is not None else None,
            "token": transfer["token"],
            "block_number": check.block_number,
        },
    }

    challenge = _parse_mpp_challenge(payment_challenge)
    if challenge is not None:
        metadata["challenge"] = challenge
        recipient = _normalize_evm_address(challenge.get("recipient"))
        if recipient:
            metadata["transaction"]["recipient"] = recipient
            target = _normalize_evm_address(identity.payment_target_ref)
            if target and target.lower() != recipient.lower():
                return PaymentVerification(False, "mppx payment recipient does not match payment_target_ref")
        amount = _amount_to_int(challenge.get("amount"))
        if amount is not None:
            metadata["transaction"]["amount"] = str(amount)
            if transfer["value"] is not None and transfer["value"] < amount:
                return PaymentVerification(
                    False, "mppx payment amount is below the challenge price"
                )
            verification_meta["amount_checked"] = True

    return PaymentVerification(
        True,
        "verified mppx payment and reviewer wallet",
        metadata=metadata,
        amount=transfer["value"],
        payment_verification_level=LEVEL_ONCHAIN_VERIFIED,
        payment_verification_metadata=verification_meta,
    )


def _verify_x402_payment(
    *,
    identity: ServiceIdentity,
    payment_reference: str,
    payment_proof: str,
    reviewer_wallet: str,
) -> PaymentVerification:
    proof = _parse_json_or_base64_json(payment_proof)
    if proof is None:
        return PaymentVerification(False, "payment_proof must be valid x402 JSON")

    tx_hash = (
        proof.get("transaction")
        or proof.get("transactionHash")
        or proof.get("txHash")
        or proof.get("reference")
    )
    if not isinstance(tx_hash, str) or not TX_HASH_RE.match(tx_hash):
        return PaymentVerification(False, "x402 payment_proof must include a transaction hash")
    # Hex tx hashes are case-insensitive: compare (and look up) in lowercase
    # so a checksummed reference matches, like the proofless path does.
    tx_hash = tx_hash.lower()

    # The proof names the chain; a proof without one inherits the reference
    # prefix (x402:monad:<tx>) and otherwise defaults to Base.
    proof_network = proof.get("network") or proof.get("chain")
    if proof_network is None:
        network = x402_reference_network(payment_reference) or DEFAULT_X402_NETWORK
    else:
        network = x402_network(proof_network)
    if network == SOLANA_NETWORK:
        # A Solana proof's transaction is a base58 signature, never a 0x hash;
        # it is verified by _verify_solana_x402_payment via payment_reference.
        return PaymentVerification(
            False,
            "payment_reference must be x402:solana:<signature> for a Solana x402 proof",
            error_code="unsupported_payment_reference",
        )
    if network not in X402_CHAINS:
        return PaymentVerification(
            False,
            "only x402 Base, Monad and Solana USDC payments are supported",
            error_code="unsupported_payment_chain",
        )
    if payment_reference.strip().lower() not in {tx_hash, f"x402:{network}:{tx_hash}"}:
        return PaymentVerification(False, "payment_reference does not match x402 transaction")

    check, verification_meta = check_payment_reference_onchain(
        provider="x402",
        payment_target_ref=identity.payment_target_ref,
        reviewer_wallet=reviewer_wallet,
        tx_hash=tx_hash,
        network=network,
    )
    verification_meta["proof_present"] = True
    verification_meta["source"] = "payment_proof"

    if not check.ok or check.transfer is None:
        return _chain_failure("x402", check, network)

    transfer = check.transfer
    required = _amount_to_int(
        proof.get("maxAmountRequired") or proof.get("amount")
    )
    if (
        required is not None
        and transfer["value"] is not None
        and transfer["value"] < required
    ):
        return PaymentVerification(
            False, "x402 payment amount is below the challenge price"
        )
    verification_meta["amount_checked"] = (
        required is not None and transfer["value"] is not None
    )

    metadata = {
        "provider": "x402",
        "network": network,
        "proof": proof,
        "transaction": {
            "hash": tx_hash,
            "from": transfer["from"],
            "to": transfer["to"],
            "value": str(transfer["value"]) if transfer["value"] is not None else None,
            "token": transfer["token"],
            "block_number": check.block_number,
        },
    }
    return PaymentVerification(
        True,
        "verified x402 payment and reviewer wallet",
        metadata=metadata,
        amount=transfer["value"],
        payment_verification_level=LEVEL_ONCHAIN_VERIFIED,
        payment_verification_metadata=verification_meta,
    )


@dataclass(frozen=True)
class OnchainCheck:
    ok: bool
    failure: str | None = None
    transfer: dict[str, Any] | None = None
    block_number: int | None = None


def _chain_failure(
    provider: str, check: OnchainCheck, network: str | None
) -> PaymentVerification:
    chain = X402_CHAINS.get(network or "") if provider == "x402" else None
    network = chain.label if chain else "Tempo"
    if check.failure == FAILURE_RPC_UNREACHABLE:
        return PaymentVerification(
            False,
            f"could not verify {provider} transaction on {network}",
            error_code="payment_rpc_unavailable",
            retryable=True,
        )
    if check.failure == FAILURE_TOKEN_NOT_PINNED:
        return PaymentVerification(
            False,
            f"{provider} payment token is not configured",
            error_code="payment_verifier_misconfigured",
            retryable=True,
        )
    if check.failure == FAILURE_PAYEE_UNKNOWN:
        return PaymentVerification(
            False,
            "service payment recipient is not registered",
            error_code="payment_payee_unknown",
        )
    if check.failure == FAILURE_TX_FAILED:
        return PaymentVerification(
            False,
            f"{provider} transaction did not succeed on {network}",
            error_code="payment_transaction_failed",
        )
    if chain is not None and chain.kind == "svm":
        return PaymentVerification(
            False,
            "transaction does not transfer pinned USDC to the registered service payee"
            if check.failure == FAILURE_NO_MATCHING_TRANSFER
            else "payer_wallet did not send pinned USDC to the registered service payee",
            error_code="payment_transfer_mismatch",
        )
    return PaymentVerification(
        False,
        "reviewer wallet did not send the expected token to the registered service payee",
        error_code="payment_transfer_mismatch",
    )


def _verify_solana_x402_payment(
    *,
    identity: ServiceIdentity,
    payment_reference: str,
    signature: str,
    payment_proof: str | None,
    reviewer_wallet: str,
    payload: str,
    payer_signature: str | None,
    payer_wallet: str | None,
) -> PaymentVerification:
    """x402 exact on Solana: an SPL transfer of pinned USDC from the payer's
    token account to the service payee's token account.

    The facilitator is the fee payer and the payer is a base58 key, so the
    EVM reviewer_wallet can never be matched against the transfer. Linkage
    is an ed25519 payer_signature over payer_link_message(). Without it the
    transfer is only *observed*: the review is admitted at signature_only and
    does not reserve the reference, so nobody can claim (or block) a public
    Solana payment they did not send.
    """
    proof = None
    required = None
    if payment_proof:
        proof = _parse_json_or_base64_json(payment_proof)
        if proof is None:
            return PaymentVerification(False, "payment_proof must be valid x402 JSON")
        proof_tx = (
            proof.get("transaction")
            or proof.get("transactionHash")
            or proof.get("txHash")
            or proof.get("reference")
        )
        proof_network = proof.get("network") or proof.get("chain")
        if proof_network is not None and x402_network(proof_network) != SOLANA_NETWORK:
            return PaymentVerification(
                False, "payment_proof network does not match the Solana payment_reference"
            )
        # Base58 is case-sensitive: compare exactly.
        if not isinstance(proof_tx, str) or proof_tx.strip() != signature:
            return PaymentVerification(False, "payment_reference does not match x402 transaction")
        required = _amount_to_int(proof.get("maxAmountRequired") or proof.get("amount"))

    payer_sig = payer_signature.strip() if payer_signature else None
    payer = payer_wallet.strip() if payer_wallet else None
    if payer is not None and not is_solana_address(payer):
        return PaymentVerification(False, "payer_wallet must be a base58 Solana address")
    if payer is not None and payer_sig is None:
        return PaymentVerification(False, "payer_wallet requires payer_signature")
    if payer_sig is not None and not is_solana_signature(payer_sig):
        return PaymentVerification(
            False, "payer_signature must be a base58 ed25519 signature (64 bytes)"
        )

    check, verification_meta = check_solana_payment_onchain(
        payment_target_ref=identity.payment_target_ref,
        signature=signature,
        payer=payer,
    )
    verification_meta["proof_present"] = proof is not None
    verification_meta["source"] = "payment_proof" if proof is not None else "payment_reference"
    if not check.ok or check.transfer is None:
        return _chain_failure("x402", check, SOLANA_NETWORK)

    by_payer: dict[str, int] = check.transfer["by_payer"]
    message = payer_link_message(reviewer_wallet=reviewer_wallet, payload=payload)
    linked = None
    if payer_sig is not None:
        candidates = [payer] if payer is not None else sorted(by_payer)
        linked = next(
            (c for c in candidates if verify_ed25519(c, message.encode("utf-8"), payer_sig)),
            None,
        )
        if linked is None:
            # Supplied linkage evidence must verify; it never downgrades.
            return PaymentVerification(
                False,
                "payer_signature does not match a payer of this transaction",
                error_code="payer_signature_invalid",
                expected_payer_message=message,
            )

    value = by_payer[linked] if linked is not None else max(by_payer.values())
    if required is not None and value < required:
        return PaymentVerification(False, "x402 payment amount is below the challenge price")
    verification_meta["amount_checked"] = required is not None
    verification_meta["payer_linked"] = linked is not None
    transaction = {
        "signature": signature,
        "payers": by_payer if linked is None else None,
        "from": linked,
        "to": check.transfer["to"],
        "value": str(value),
        "token": check.transfer["token"],
        "slot": check.block_number,
        "fee_payer": check.transfer["fee_payer"],
        "commitment": SOLANA_COMMITMENT,
    }
    transaction = {k: v for k, v in transaction.items() if v is not None}
    metadata: dict[str, Any] = {
        "provider": "x402",
        "network": SOLANA_NETWORK,
        "proof": proof,
        "transaction": transaction,
    }
    if linked is None:
        verification_meta["verification_failure"] = FAILURE_PAYER_UNLINKED
        return PaymentVerification(
            True,
            "signature verified; Solana payment observed on-chain but not linked to "
            "reviewer_wallet (no payer_signature): weighted as unverified payment",
            metadata=metadata,
            payment_verification_level=LEVEL_SIGNATURE_ONLY,
            payment_verification_metadata=verification_meta,
            canonical_reference=unlinked_reference_key(reviewer_wallet, signature),
            claims_reference=False,
        )
    metadata["payer_link"] = {
        "scheme": "ed25519",
        "payer": linked,
        "payer_signature": payer_sig,
        "message": message,
    }
    return PaymentVerification(
        True,
        "verified x402 Solana payment and payer_signature linking payer to reviewer wallet",
        metadata=metadata,
        amount=value,
        payment_verification_level=LEVEL_ONCHAIN_VERIFIED,
        payment_verification_metadata=verification_meta,
        canonical_reference=signature,
    )


def check_solana_payment_onchain(
    *,
    payment_target_ref: str | None,
    signature: str,
    payer: str | None = None,
) -> tuple[OnchainCheck, dict[str, Any]]:
    """getTransaction, require meta.err == null, and find pinned-USDC SPL
    transfers into token accounts owned by the service payee. ``payer``
    (a base58 owner) restricts the match to transfers out of its accounts.

    On success ``transfer`` holds ``by_payer`` (owner -> base units), the
    payee (``to``), the mint (``token``) and the fee payer."""
    chain = X402_CHAINS[SOLANA_NETWORK]
    rpc_url = os.environ.get(chain.rpc_env) or chain.default_rpc_url
    mint = os.environ.get(chain.token_env) or chain.default_token
    token = mint if is_solana_address(mint) else None
    payee = payment_target_ref.strip() if payment_target_ref else None
    meta: dict[str, Any] = {
        "network": SOLANA_NETWORK,
        "token_pinned": token is not None,
        "amount_checked": False,
    }

    def done(check: OnchainCheck) -> tuple[OnchainCheck, dict[str, Any]]:
        meta["verification_failure"] = check.failure
        return check, meta

    if token is None:
        return done(OnchainCheck(False, failure=FAILURE_TOKEN_NOT_PINNED))
    if payee is None or not is_solana_address(payee):
        return done(OnchainCheck(False, failure=FAILURE_PAYEE_UNKNOWN))
    tx = _solana_transaction(rpc_url, signature)
    if tx is None:
        return done(OnchainCheck(False, failure=FAILURE_RPC_UNREACHABLE))
    meta_block = tx.get("meta")
    if not isinstance(meta_block, dict) or meta_block.get("err") is not None:
        return done(OnchainCheck(False, failure=FAILURE_TX_FAILED))
    transfers = find_usdc_transfers(tx, payee=payee, mint=token)
    by_payer = transfers.by_payer
    if payer is not None:
        by_payer = {payer: by_payer[payer]} if payer in by_payer else {}
        if not by_payer:
            return done(OnchainCheck(False, failure=FAILURE_PAYER_MISMATCH if transfers.by_payer else FAILURE_NO_MATCHING_TRANSFER))
    if not by_payer:
        return done(OnchainCheck(False, failure=FAILURE_NO_MATCHING_TRANSFER))
    return done(OnchainCheck(
        True,
        transfer={
            "by_payer": by_payer,
            "to": payee,
            "token": token,
            "fee_payer": transfers.fee_payer,
        },
        block_number=transfers.slot,
    ))


def _solana_transaction(rpc_url: str, signature: str) -> dict[str, Any] | None:
    return rpc_get_transaction(rpc_url, signature)


def _x402_chain_config(network: str) -> tuple[str, str | None]:
    chain = X402_CHAINS[network]
    rpc_url = os.environ.get(chain.rpc_env) or chain.default_rpc_url
    token = _normalize_evm_address(
        os.environ.get(chain.token_env) or chain.default_token
    )
    return rpc_url, token


def _mppx_chain_config() -> tuple[str, str | None]:
    rpc_url = os.environ.get("MPPX_TEMPO_RPC_URL", TEMPO_RPC_URL)
    token = _normalize_evm_address(os.environ.get("MPPX_TEMPO_TOKEN_ADDRESS"))
    return rpc_url, token


def check_payment_reference_onchain(
    *,
    provider: str,
    payment_target_ref: str | None,
    reviewer_wallet: str,
    tx_hash: str,
    network: str | None = None,
) -> tuple[OnchainCheck, dict[str, Any]]:
    """The single on-chain payment check, shared by the proof paths, the
    tx-hash-only path, and the cron re-verify pass: fetch the receipt, require
    status 1, and find an ERC-20 Transfer of the pinned token from
    reviewer_wallet to the service payee. Also returns the compact
    verification-metadata dict (caller fills in proof_present/source).

    ``network`` selects the x402 chain (an X402_CHAINS key or alias; Base when
    omitted) and is recorded in the metadata so re-verification reuses it.
    """
    if provider == "x402":
        network = x402_network(network or DEFAULT_X402_NETWORK)
        # Solana payers are base58 keys linked by payer_signature, not the
        # EVM reviewer_wallet: they are checked by check_solana_payment_onchain.
        if network is not None and X402_CHAINS[network].kind != "evm":
            network = None
    if provider == "x402" and network is not None:
        rpc_url, token = _x402_chain_config(network)
    elif provider == "mppx":
        rpc_url, token = _mppx_chain_config()
        network = "tempo"
    else:
        check = OnchainCheck(
            False,
            failure="unsupported_chain" if provider == "x402" else "unsupported_provider",
        )
        return check, {
            "network": None,
            "token_pinned": False,
            "amount_checked": False,
            "verification_failure": check.failure,
        }

    recipient = _normalize_evm_address(payment_target_ref)
    check = _check_onchain_transfer(
        rpc_url=rpc_url,
        tx_hash=tx_hash,
        sender=reviewer_wallet,
        recipient=recipient,
        token=token,
    )
    return check, {
        "network": network,
        "token_pinned": token is not None,
        "amount_checked": False,
        "verification_failure": check.failure,
    }


def _check_onchain_transfer(
    *,
    rpc_url: str,
    tx_hash: str,
    sender: str,
    recipient: str | None,
    token: str | None,
) -> OnchainCheck:
    """Both the token pin and the payee are required: without the pin a
    self-minted worthless ERC-20 buys the verified multiplier, and without a
    known payee any transfer out of the reviewer's wallet would count."""
    if token is None:
        return OnchainCheck(False, failure=FAILURE_TOKEN_NOT_PINNED)
    if recipient is None:
        return OnchainCheck(False, failure=FAILURE_PAYEE_UNKNOWN)
    receipt = _rpc_transaction_receipt(rpc_url, tx_hash)
    if receipt is None:
        return OnchainCheck(False, failure=FAILURE_RPC_UNREACHABLE)
    if _hex_to_int(receipt.get("status")) != 1:
        return OnchainCheck(False, failure=FAILURE_TX_FAILED)
    transfer = _find_erc20_transfer(
        receipt, sender=sender, recipient=recipient, token=token
    )
    if transfer is None:
        return OnchainCheck(False, failure=FAILURE_NO_MATCHING_TRANSFER)
    return OnchainCheck(
        True,
        transfer=transfer,
        block_number=_hex_to_int(receipt.get("blockNumber")),
    )


def _reviewer_id_from_wallet(wallet: str) -> str:
    return identity_id_from_wallet(wallet)


def _normalize_evm_address(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not EVM_ADDRESS_RE.match(candidate):
        return None
    return "0x" + candidate[2:].lower()


def _recover_eip191(message: str, signature: str) -> str | None:
    try:
        recovered = Account.recover_message(
            encode_defunct(text=message),
            signature=signature.strip(),
        )
    except Exception:
        return None
    return _normalize_evm_address(recovered)


def _parse_payment_receipt(value: str | dict | None) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not value:
        return None
    cleaned = value.strip()
    if cleaned.lower().startswith("payment-receipt:"):
        cleaned = cleaned.split(":", 1)[1].strip()
    return _parse_json_or_base64_json(cleaned)


def _parse_json_or_base64_json(value: str | dict | None) -> dict[str, Any] | None:
    if not value:
        return None
    if isinstance(value, dict):
        return value
    cleaned = value.strip()
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    try:
        padded = cleaned + "=" * (-len(cleaned) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8")
        parsed = json.loads(decoded)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _parse_mpp_challenge(value: str | dict | None) -> dict[str, Any] | None:
    if not value:
        return None
    if isinstance(value, dict):
        return value
    cleaned = value.strip()
    match = re.search(r'request="([^"]+)"', cleaned)
    if match:
        cleaned = match.group(1)
    return _parse_json_or_base64_json(cleaned)


def _rpc_call(rpc_url: str, method: str, params: list[Any]) -> dict[str, Any] | None:
    payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
    request = urllib.request.Request(
        rpc_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "user-agent": "crowdcode-mcp",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    result = body.get("result")
    return result if isinstance(result, dict) else None


def _rpc_transaction(rpc_url: str, tx_hash: str) -> dict[str, Any] | None:
    return _rpc_call(rpc_url, "eth_getTransactionByHash", [tx_hash])


def _rpc_transaction_receipt(rpc_url: str, tx_hash: str) -> dict[str, Any] | None:
    return _rpc_call(rpc_url, "eth_getTransactionReceipt", [tx_hash])


def _topic_to_address(topic: Any) -> str | None:
    """An indexed address topic is a 32-byte word; the address is the low 20."""
    if not isinstance(topic, str):
        return None
    cleaned = topic.strip()
    if not cleaned.startswith("0x") or len(cleaned) != 66:
        return None
    return _normalize_evm_address("0x" + cleaned[-40:])


def _find_erc20_transfer(
    receipt: dict[str, Any],
    *,
    sender: str,
    recipient: str | None = None,
    token: str | None = None,
) -> dict[str, Any] | None:
    """Return the first ERC-20 Transfer log matching the given constraints.

    x402 (EIP-3009) and MPP/Tempo payments are gasless: a facilitator submits
    the transaction, so the transaction's ``from`` is the relayer, never the
    payer. The payer is the ``from`` of the ERC-20 ``Transfer`` event, which is
    what this inspects.
    """
    logs = receipt.get("logs")
    if not isinstance(logs, list):
        return None
    sender_n = _normalize_evm_address(sender)
    recipient_n = _normalize_evm_address(recipient) if recipient else None
    token_n = _normalize_evm_address(token) if token else None
    for log in logs:
        if not isinstance(log, dict):
            continue
        topics = log.get("topics")
        if not isinstance(topics, list) or len(topics) < 3:
            continue
        if str(topics[0]).lower() != ERC20_TRANSFER_TOPIC:
            continue
        log_from = _topic_to_address(topics[1])
        log_to = _topic_to_address(topics[2])
        log_token = _normalize_evm_address(log.get("address"))
        if sender_n and log_from != sender_n:
            continue
        if recipient_n and log_to != recipient_n:
            continue
        if token_n and log_token != token_n:
            continue
        return {
            "from": log_from,
            "to": log_to,
            "value": _hex_to_int(log.get("data")),
            "token": log_token,
        }
    return None


def _amount_to_int(value: Any) -> int | None:
    """Parse a challenge/proof amount (int, decimal string, or hex) into token
    base units; None when absent or unparseable."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        try:
            return int(cleaned, 16) if cleaned.lower().startswith("0x") else int(cleaned)
        except ValueError:
            return None
    return None


def _hex_to_int(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        return int(value, 16)
    except ValueError:
        return None


def utc_now() -> datetime:
    return datetime.now(UTC)
