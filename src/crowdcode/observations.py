"""Paid-call observations from crowdcode-mcp (C5, SRI-115 ticket 4).

A crowdcode-mcp client sees a paid call made by any MCP tool (AgentCash or
another wallet) through the payment receipt in the tool output, and reports
it as one signed record with no content at all:

    {endpoint, method, network, payment_reference, amount_atomic, status,
     response_bytes, observed_at}

The record is signed (EIP-191) by the local wallet that paid. This module
checks the signature, proves on-chain that the signer's payment settled to a
payee the service itself advertises, and stores one `kind = 'observation'`
row in `index_observations`. The status is self-reported, so an observation
is evidence that *someone else paid* (external payers) and that the endpoint
was up (liveness); it is never evidence of delivery (C3).

What keeps it honest:
- The transfer must come from the signer (Base, Tempo) or from a Solana payer
  that signed the same message with ed25519, so nobody can report another
  wallet's payment.
- The payee must be one the endpoint's service is registered with or that the
  endpoint's own 402 advertises, so paying your own second wallet proves
  nothing about a service.
- One row per settlement (unique payment_reference), and a per-wallet daily
  limit.

Nothing here holds a database connection across an RPC or a probe.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import psycopg

from crowdcode import advertised
from crowdcode.identity import build_identity, normalize_api_endpoint, resolve_service
from crowdcode.payments import (
    TX_HASH_RE,
    X402_CHAINS,
    _find_erc20_transfer,
    _hex_to_int,
    _mppx_chain_config,
    _normalize_evm_address,
    _recover_eip191,
    _rpc_transaction_receipt,
    _x402_chain_config,
    utc_now,
)
from crowdcode.solana import (
    SOLANA_MAINNET_CAIP2,
    _account_keys,
    _token_accounts,
    find_usdc_transfers,
    is_solana_address,
    is_solana_signature,
    rpc_get_transaction,
    verify_ed25519,
)

logger = logging.getLogger(__name__)

MESSAGE_HEADER = "CrowdCode observation v1"
FIELDS = ("endpoint", "method", "network", "payment_reference", "amount_atomic",
          "status", "response_bytes", "observed_at")
# Anything else in a submission is refused: an observation never carries content.
ALLOWED_KEYS = frozenset({"observation", "signature", "payer_wallet", "payer_signature"})

BASE_CAIP2 = "eip155:8453"
TEMPO_CAIP2 = "eip155:4217"
NETWORKS = {
    "base": BASE_CAIP2, "eip155:8453": BASE_CAIP2, "8453": BASE_CAIP2,
    "tempo": TEMPO_CAIP2, "eip155:4217": TEMPO_CAIP2, "4217": TEMPO_CAIP2,
    "solana": SOLANA_MAINNET_CAIP2, SOLANA_MAINNET_CAIP2.lower(): SOLANA_MAINNET_CAIP2,
}
METHOD_RE = re.compile(r"^[A-Z]{3,7}$")
MAX_AGE = timedelta(days=7)
MAX_SKEW = timedelta(minutes=10)
MAX_BYTES = 10**12
DEFAULT_LIMIT_PER_DAY = 200


@dataclass
class Outcome:
    http_status: int
    body: dict[str, Any] = field(default_factory=dict)


def _reject(status: int, code: str, reason: str, *, retryable: bool = False) -> Outcome:
    return Outcome(status, {"accepted": False, "error_code": code, "reason": reason,
                            "retryable": retryable})


def observation_message(record: dict[str, Any]) -> str:
    """The exact text the wallet signs: one `key:value` line per field, in a
    fixed order, values as submitted (null is empty). crowdcode-mcp builds the
    same string (packages/mcp/src/observations.ts)."""
    lines = [MESSAGE_HEADER]
    for key in FIELDS:
        value = record.get(key)
        lines.append(f"{key}:{'' if value is None else value}")
    return "\n".join(lines)


def _optional_int(value: Any, name: str, maximum: int) -> tuple[int | None, str | None]:
    if value is None:
        return None, None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        return None, f"{name} must be a non-negative integer or null"
    return value, None


@dataclass(frozen=True)
class Parsed:
    record: dict[str, Any]
    endpoint: str
    method: str | None
    network: str
    reference: str
    amount_atomic: int | None
    status: int | None
    response_bytes: int | None
    observed_at: datetime


def parse_record(record: Any, now: datetime) -> tuple[Parsed | None, str | None]:
    if not isinstance(record, dict):
        return None, "observation must be an object"
    extra = set(record) - set(FIELDS)
    if extra:
        return None, f"unexpected observation fields: {', '.join(sorted(extra))}"
    raw_endpoint = record.get("endpoint")
    if not isinstance(raw_endpoint, str) or len(raw_endpoint) > 2048:
        return None, "endpoint is required"
    if "?" in raw_endpoint or "#" in raw_endpoint:
        return None, "endpoint must not include a query or fragment"
    try:
        endpoint = normalize_api_endpoint(raw_endpoint)
    except ValueError as exc:
        return None, str(exc)
    if endpoint is None or not endpoint.startswith("https://"):
        return None, "endpoint must be an https URL"
    method = record.get("method")
    if method is not None and (not isinstance(method, str) or not METHOD_RE.match(method)):
        return None, "method must be an upper-case HTTP method or null"
    network = NETWORKS.get(str(record.get("network") or "").strip().lower())
    if network is None:
        return None, "network must be base, tempo or solana (or its CAIP-2 id)"
    reference = record.get("payment_reference")
    if not isinstance(reference, str):
        return None, "payment_reference is required"
    reference = reference.strip()
    if network == SOLANA_MAINNET_CAIP2:
        if not is_solana_signature(reference):
            return None, "a Solana payment_reference is the base58 transaction signature"
    elif not TX_HASH_RE.match(reference):
        return None, "payment_reference must be the 0x settlement transaction hash"
    else:
        reference = reference.lower()
    amount, error = _optional_int(record.get("amount_atomic"), "amount_atomic", 10**18)
    if error:
        return None, error
    status, error = _optional_int(record.get("status"), "status", 599)
    if error:
        return None, error
    if status is not None and status < 100:
        return None, "status must be an HTTP status code or null"
    response_bytes, error = _optional_int(record.get("response_bytes"), "response_bytes", MAX_BYTES)
    if error:
        return None, error
    observed_raw = record.get("observed_at")
    try:
        observed_at = datetime.fromisoformat(str(observed_raw).replace("Z", "+00:00"))
    except ValueError:
        return None, "observed_at must be an ISO 8601 timestamp"
    if observed_at.tzinfo is None:
        return None, "observed_at must include a time zone"
    if observed_at > now + MAX_SKEW or observed_at < now - MAX_AGE:
        return None, "observed_at must be within the last 7 days"
    return Parsed(record, endpoint, method, network, reference, amount, status,
                  response_bytes, observed_at), None


@dataclass(frozen=True)
class Settlement:
    ok: bool
    payee: str | None = None
    amount: int | None = None
    payer: str | None = None
    code: str | None = None
    reason: str | None = None
    retryable: bool = False


def _evm_settlement(network: str, reference: str, signer: str) -> Settlement:
    if network == TEMPO_CAIP2:
        rpc_url, token = _mppx_chain_config()
    else:
        rpc_url, token = _x402_chain_config("base")
    if token is None:
        return Settlement(False, code="payment_verifier_misconfigured",
                          reason="the payment token is not configured", retryable=True)
    receipt = _rpc_transaction_receipt(rpc_url, reference)
    if receipt is None:
        # Not yet visible to the RPC (or the RPC is down): the client retries.
        return Settlement(False, code="payment_not_found",
                          reason="settlement transaction not found yet", retryable=True)
    if _hex_to_int(receipt.get("status")) != 1:
        return Settlement(False, code="payment_transaction_failed",
                          reason="settlement transaction did not succeed")
    transfer = _find_erc20_transfer(receipt, sender=signer, token=token)
    if transfer is None:
        return Settlement(False, code="payment_transfer_mismatch",
                          reason="the transaction has no USDC transfer from the signing wallet")
    return Settlement(True, payee=transfer["to"], amount=transfer.get("value"), payer=signer)


def _solana_settlement(reference: str, message: str, payer_wallet: Any,
                       payer_signature: Any) -> Settlement:
    if not is_solana_address(payer_wallet) or not is_solana_signature(payer_signature):
        return Settlement(False, code="payer_signature_required",
                          reason="a Solana observation needs payer_wallet and its ed25519 "
                                 "payer_signature over the same message")
    if not verify_ed25519(payer_wallet, message.encode("utf-8"), payer_signature):
        return Settlement(False, code="payer_signature_invalid",
                          reason="payer_signature does not verify for payer_wallet")
    chain = X402_CHAINS["solana"]
    rpc_url = os.environ.get(chain.rpc_env) or chain.default_rpc_url
    mint = os.environ.get(chain.token_env) or chain.default_token
    tx = rpc_get_transaction(rpc_url, reference)
    if tx is None:
        return Settlement(False, code="payment_not_found",
                          reason="settlement transaction not found yet", retryable=True)
    if not isinstance(tx.get("meta"), dict) or tx["meta"].get("err") is not None:
        return Settlement(False, code="payment_transaction_failed",
                          reason="settlement transaction did not succeed")
    # The payee is whoever received pinned USDC from the payer in this
    # transaction: every token-account owner whose balance went up.
    accounts = _token_accounts(tx, _account_keys(tx), mint)
    for owner in sorted({o for o, pre, post in accounts.values() if post > pre and o != payer_wallet}):
        amount = find_usdc_transfers(tx, payee=owner, mint=mint).by_payer.get(payer_wallet)
        if amount:
            return Settlement(True, payee=owner, amount=amount, payer=payer_wallet)
    return Settlement(False, code="payment_transfer_mismatch",
                      reason="the transaction has no USDC transfer from payer_wallet")


def _provider(network: str) -> str:
    return "mppx" if network == TEMPO_CAIP2 else "x402"


def confirm_payee(connect: Any, endpoint: str, network: str, payee: str) -> tuple[bool, str | None]:
    """Whether `payee` belongs to the service at `endpoint`: registered to it,
    or advertised by the endpoint's own 402. Returns (confirmed, service_id)."""
    try:
        identity = build_identity(api_endpoint=endpoint, payment_provider=_provider(network),
                                  payment_target_ref=payee, strict=True)
    except ValueError:
        return False, None
    # Teaches the service a payee its own 402 advertises (a cooled-down probe).
    advertised.learn_advertised_payee(connect, identity)
    with connect() as conn:
        resolved = resolve_service(conn, identity)
    if resolved.row is not None:
        return True, resolved.row["id"]
    if resolved.error:
        return False, None
    # Not a service CrowdCode knows yet: ask the endpoint itself what it accepts.
    offers, _ = advertised._advertised(f"endpoint:{endpoint}", endpoint)
    return advertised.advertises(offers, payee), None


def _resource_id(conn: Any, endpoint: str, method: str | None) -> int | None:
    """W1's index_resources row for this URL, when that module is deployed."""
    try:
        from crowdcode.index import resolve_resource  # type: ignore[attr-defined]
    except ImportError:
        return None
    try:
        row = resolve_resource(conn, endpoint, method)
    except Exception:  # the index is optional for an observation; never fail on it
        logger.warning("index resource lookup failed for an observation")
        return None
    return row["id"] if row else None


def _limit_per_day() -> int:
    try:
        return int(os.environ.get("CROWDCODE_OBSERVATION_LIMIT_PER_DAY", DEFAULT_LIMIT_PER_DAY))
    except ValueError:
        return DEFAULT_LIMIT_PER_DAY


def submit_observation(body: Any, connect: Any, now: datetime | None = None) -> Outcome:
    now = now or utc_now()
    if not isinstance(body, dict):
        return _reject(400, "invalid_observation", "body must be a JSON object")
    extra = set(body) - ALLOWED_KEYS
    if extra:
        return _reject(400, "invalid_observation", f"unexpected fields: {', '.join(sorted(extra))}")
    parsed, error = parse_record(body.get("observation"), now)
    if parsed is None:
        return _reject(400, "invalid_observation", error or "invalid observation")
    signature = body.get("signature")
    message = observation_message(parsed.record)
    signer = _recover_eip191(message, signature) if isinstance(signature, str) else None
    if signer is None:
        return _reject(401, "signature_invalid", "signature must be an EIP-191 signature of the observation message")

    limit = _limit_per_day()
    with connect() as conn:
        existing = conn.execute(
            "select id, observer_wallet from index_observations "
            "where kind = 'observation' and payment_reference = %s",
            (parsed.reference,),
        ).fetchone()
        count = conn.execute(
            "select count(*)::int as n from index_observations "
            "where kind = 'observation' and observer_wallet = %s and at > %s",
            (signer, now - timedelta(days=1)),
        ).fetchone()["n"]
    if existing is not None:
        if existing["observer_wallet"] == signer:
            return Outcome(200, {"accepted": True, "duplicate": True, "observation_id": existing["id"]})
        return _reject(409, "payment_reference_used", "this settlement was already reported by another wallet")
    if limit > 0 and count >= limit:
        return _reject(429, "rate_limited", f"at most {limit} observations per wallet per 24 hours",
                       retryable=True)

    if parsed.network == SOLANA_MAINNET_CAIP2:
        settlement = _solana_settlement(parsed.reference, message, body.get("payer_wallet"),
                                        body.get("payer_signature"))
    else:
        settlement = _evm_settlement(parsed.network, parsed.reference, signer)
    if not settlement.ok:
        return _reject(503 if settlement.retryable else 422, settlement.code or "payment_unverified",
                       settlement.reason or "payment could not be verified", retryable=settlement.retryable)

    confirmed, service_id = confirm_payee(connect, parsed.endpoint, parsed.network, settlement.payee or "")
    if not confirmed:
        return _reject(422, "payee_not_advertised",
                       "the payment did not go to a payee this endpoint's service uses or advertises")

    ok = parsed.status is None or 200 <= parsed.status < 400
    try:
        with connect() as conn:
            row = conn.execute(
                """
                insert into index_observations (
                  resource_id, endpoint, method, at, kind, ok, http_status, network,
                  payee, quoted_amount_atomic, error_code, observer_wallet,
                  payment_reference, response_bytes
                )
                values (%s, %s, %s, %s, 'observation', %s, %s, %s, %s, %s, %s, %s, %s, %s)
                returning id
                """,
                (
                    _resource_id(conn, parsed.endpoint, parsed.method),
                    parsed.endpoint, parsed.method, min(parsed.observed_at, now), ok,
                    parsed.status, parsed.network, settlement.payee,
                    # The amount that actually settled, not the client's claim.
                    settlement.amount if settlement.amount is not None else parsed.amount_atomic,
                    None if ok else f"http_{parsed.status}",
                    signer, parsed.reference, parsed.response_bytes,
                ),
            ).fetchone()
            conn.commit()
    except psycopg.errors.UniqueViolation:
        return Outcome(200, {"accepted": True, "duplicate": True})
    logger.info(json.dumps({"event": "observation_recorded", "observation_id": row["id"],
                            "endpoint": parsed.endpoint, "network": parsed.network,
                            "service_id": service_id, "ok": ok}))
    return Outcome(200, {
        "accepted": True,
        "observation_id": row["id"],
        "service_id": service_id,
        "network": parsed.network,
        "payee": settlement.payee,
        "amount_atomic": settlement.amount,
        "counts_as": "an external payer and liveness evidence; never delivery evidence",
    })
