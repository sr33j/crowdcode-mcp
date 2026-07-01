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

from crowdcode.identity import ServiceIdentity
from crowdcode.settings import get_settings

EVM_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
TX_HASH_RE = re.compile(r"^0x[a-fA-F0-9]{64}$")

TEMPO_RPC_URL = "https://rpc.tempo.xyz"
BASE_RPC_URL = "https://mainnet.base.org"


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


def reviewer_id_from_payment(payment_reference: str) -> str:
    settings = get_settings()
    material = f"{settings.reviewer_salt}:{payment_reference.strip()}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def canonical_review_payload(
    *,
    identity: ServiceIdentity,
    rating: int,
    reason: str,
    payment_reference: str,
) -> str:
    payload = {
        "type": "crowdcode.review.v1",
        "service_id": identity.service_id,
        "api_endpoint": identity.api_endpoint,
        "payment_provider": identity.payment_provider,
        "payment_target_ref": identity.payment_target_ref,
        "directory_slug": identity.directory_slug,
        "payment_reference": payment_reference.strip(),
        "rating": rating,
        "reason_hash": "sha256:" + hashlib.sha256(reason.strip().encode("utf-8")).hexdigest(),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


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
    payment_reference: str,
    payment_proof: str | None = None,
    payment_challenge: str | None = None,
    reviewer_wallet: str | None = None,
    review_signature: str | None = None,
    signature_scheme: str = "eip191",
) -> PaymentVerification:
    if not payment_reference or not payment_reference.strip():
        return PaymentVerification(False, "payment_reference is required")

    if identity.payment_provider in {"mppx", "x402"}:
        return _verify_signed_machine_payment(
            identity=identity,
            rating=rating,
            reason=reason,
            payment_reference=payment_reference,
            payment_proof=payment_proof,
            payment_challenge=payment_challenge,
            reviewer_wallet=reviewer_wallet,
            review_signature=review_signature,
            signature_scheme=signature_scheme,
        )

    payment_ok, payment_reason = verify_payment_reference(payment_reference)
    if not payment_ok:
        return PaymentVerification(False, payment_reason)

    return PaymentVerification(
        True,
        payment_reason,
        reviewer_id=reviewer_id_from_payment(payment_reference),
        metadata={"mode": "placeholder"},
    )


def _verify_signed_machine_payment(
    *,
    identity: ServiceIdentity,
    rating: int,
    reason: str,
    payment_reference: str,
    payment_proof: str | None,
    payment_challenge: str | None,
    reviewer_wallet: str | None,
    review_signature: str | None,
    signature_scheme: str,
) -> PaymentVerification:
    if not payment_proof:
        return PaymentVerification(False, "payment_proof is required for mppx and x402 reviews")
    if not reviewer_wallet:
        return PaymentVerification(False, "reviewer_wallet is required for mppx and x402 reviews")
    if not review_signature:
        return PaymentVerification(False, "review_signature is required for mppx and x402 reviews")
    if signature_scheme != "eip191":
        return PaymentVerification(False, "only eip191 signatures are supported for mppx and x402 reviews")

    wallet = _normalize_evm_address(reviewer_wallet)
    if wallet is None:
        return PaymentVerification(False, "reviewer_wallet must be an EVM 0x address")

    payload = canonical_review_payload(
        identity=identity,
        rating=rating,
        reason=reason,
        payment_reference=payment_reference,
    )
    recovered = _recover_eip191(payload, review_signature)
    if recovered is None:
        return PaymentVerification(False, "review_signature is invalid")
    if recovered.lower() != wallet.lower():
        return PaymentVerification(False, "review_signature does not match reviewer_wallet")

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
        payment_verified=True,
        signature_verified=True,
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
        return PaymentVerification(False, "mppx receipt status is not success")
    if receipt.get("method") != "tempo":
        return PaymentVerification(False, "mppx receipt method must be tempo")

    tx_hash = str(receipt.get("reference") or "").strip()
    if not TX_HASH_RE.match(tx_hash):
        return PaymentVerification(False, "mppx receipt reference must be a transaction hash")
    if payment_reference.strip() not in {tx_hash, f"mppx:tempo:{tx_hash}"}:
        return PaymentVerification(False, "payment_reference does not match mppx receipt reference")

    tx = _rpc_transaction(
        os.environ.get("MPPX_TEMPO_RPC_URL", TEMPO_RPC_URL),
        tx_hash,
    )
    if tx is None:
        return PaymentVerification(False, "could not verify mppx transaction on Tempo")

    tx_from = _normalize_evm_address(tx.get("from"))
    if tx_from is None:
        return PaymentVerification(False, "mppx transaction is missing sender")
    if tx_from.lower() != reviewer_wallet.lower():
        return PaymentVerification(False, "reviewer_wallet did not send the mppx payment")

    metadata: dict[str, Any] = {
        "provider": "mppx",
        "receipt": receipt,
        "transaction": {
            "hash": tx_hash,
            "from": tx_from,
            "chain_id": _hex_to_int(tx.get("chainId")),
            "block_number": _hex_to_int(tx.get("blockNumber")),
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
        amount = challenge.get("amount")
        if amount is not None:
            metadata["transaction"]["amount"] = str(amount)

    return PaymentVerification(True, "verified mppx payment and reviewer wallet", metadata=metadata)


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
    if payment_reference.strip() not in {tx_hash, f"x402:base:{tx_hash}"}:
        return PaymentVerification(False, "payment_reference does not match x402 transaction")

    network = str(proof.get("network") or proof.get("chain") or "base").lower()
    if network not in {"base", "eip155:8453", "8453"}:
        return PaymentVerification(False, "only x402 Base EVM payments are currently supported")

    tx = _rpc_transaction(os.environ.get("X402_BASE_RPC_URL", BASE_RPC_URL), tx_hash)
    if tx is None:
        return PaymentVerification(False, "could not verify x402 transaction on Base")

    tx_from = _normalize_evm_address(tx.get("from"))
    if tx_from is None:
        return PaymentVerification(False, "x402 transaction is missing sender")
    if tx_from.lower() != reviewer_wallet.lower():
        return PaymentVerification(False, "reviewer_wallet did not send the x402 payment")

    pay_to = _normalize_evm_address(proof.get("payTo") or proof.get("recipient"))
    target = _normalize_evm_address(identity.payment_target_ref)
    if target and pay_to and target.lower() != pay_to.lower():
        return PaymentVerification(False, "x402 payment recipient does not match payment_target_ref")

    metadata = {
        "provider": "x402",
        "proof": proof,
        "transaction": {
            "hash": tx_hash,
            "from": tx_from,
            "chain_id": _hex_to_int(tx.get("chainId")),
            "block_number": _hex_to_int(tx.get("blockNumber")),
        },
    }
    return PaymentVerification(True, "verified x402 payment and reviewer wallet", metadata=metadata)


def _reviewer_id_from_wallet(wallet: str) -> str:
    settings = get_settings()
    material = f"{settings.reviewer_salt}:wallet:{wallet.lower()}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


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


def _parse_payment_receipt(value: str) -> dict[str, Any] | None:
    cleaned = value.strip()
    if cleaned.lower().startswith("payment-receipt:"):
        cleaned = cleaned.split(":", 1)[1].strip()
    return _parse_json_or_base64_json(cleaned)


def _parse_json_or_base64_json(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
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


def _parse_mpp_challenge(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    cleaned = value.strip()
    match = re.search(r'request="([^"]+)"', cleaned)
    if match:
        cleaned = match.group(1)
    return _parse_json_or_base64_json(cleaned)


def _rpc_transaction(rpc_url: str, tx_hash: str) -> dict[str, Any] | None:
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_getTransactionByHash",
        "params": [tx_hash],
        "id": 1,
    }
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


def _hex_to_int(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        return int(value, 16)
    except ValueError:
        return None


def utc_now() -> datetime:
    return datetime.now(UTC)
