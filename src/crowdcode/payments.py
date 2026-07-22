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

# keccak256("Transfer(address,address,uint256)")
ERC20_TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)

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
    # True when the failure was specifically the EIP-191 signature not
    # matching the server-side canonical payload — lets review_service return
    # the expected message so clients can re-sign after an identity
    # resolution race.
    signature_mismatch: bool = False


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
        "reason_hash": reason_hash,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def canonical_review_payload(
    *,
    identity: ServiceIdentity,
    rating: int,
    reason: str,
    payment_reference: str,
) -> str:
    return canonical_review_payload_from_hash(
        identity=identity,
        rating=rating,
        reason_hash=reason_hash(reason),
        payment_reference=payment_reference,
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
        return PaymentVerification(
            False, "review_signature is invalid", signature_mismatch=True
        )
    if recovered.lower() != wallet.lower():
        return PaymentVerification(
            False,
            "review_signature does not match reviewer_wallet",
            signature_mismatch=True,
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

    chain_receipt = _rpc_transaction_receipt(
        os.environ.get("MPPX_TEMPO_RPC_URL", TEMPO_RPC_URL),
        tx_hash,
    )
    if chain_receipt is None:
        return PaymentVerification(False, "could not verify mppx transaction on Tempo")
    if _hex_to_int(chain_receipt.get("status")) != 1:
        return PaymentVerification(False, "mppx transaction did not succeed on Tempo")

    # MPP/Tempo payments are settled by a facilitator, so the tx sender is the
    # settler, not the payer. Verify the payer via the Transfer event `from`.
    target = _normalize_evm_address(identity.payment_target_ref)
    transfer = _find_erc20_transfer(
        chain_receipt, sender=reviewer_wallet, recipient=target
    )
    if transfer is None:
        return PaymentVerification(False, "reviewer_wallet did not send the mppx payment")

    metadata: dict[str, Any] = {
        "provider": "mppx",
        "receipt": receipt,
        "transaction": {
            "hash": tx_hash,
            "from": transfer["from"],
            "to": transfer["to"],
            "value": str(transfer["value"]) if transfer["value"] is not None else None,
            "token": transfer["token"],
            "block_number": _hex_to_int(chain_receipt.get("blockNumber")),
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

    chain_receipt = _rpc_transaction_receipt(
        os.environ.get("X402_BASE_RPC_URL", BASE_RPC_URL), tx_hash
    )
    if chain_receipt is None:
        return PaymentVerification(False, "could not verify x402 transaction on Base")
    if _hex_to_int(chain_receipt.get("status")) != 1:
        return PaymentVerification(False, "x402 transaction did not succeed on Base")

    # x402 on Base uses EIP-3009 (gasless): a facilitator submits the tx, so the
    # payer is the `from` of the USDC Transfer event, not the tx sender.
    target = _normalize_evm_address(identity.payment_target_ref)
    transfer = _find_erc20_transfer(
        chain_receipt, sender=reviewer_wallet, recipient=target
    )
    if transfer is None:
        return PaymentVerification(False, "reviewer_wallet did not send the x402 payment")

    metadata = {
        "provider": "x402",
        "proof": proof,
        "transaction": {
            "hash": tx_hash,
            "from": transfer["from"],
            "to": transfer["to"],
            "value": str(transfer["value"]) if transfer["value"] is not None else None,
            "token": transfer["token"],
            "block_number": _hex_to_int(chain_receipt.get("blockNumber")),
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


def _hex_to_int(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        return int(value, 16)
    except ValueError:
        return None


def utc_now() -> datetime:
    return datetime.now(UTC)
