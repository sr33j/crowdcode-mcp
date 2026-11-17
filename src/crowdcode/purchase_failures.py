"""Paid calls the seller broke before settlement (SRI-115 Phase 2, C10).

A judged purchase is delivery evidence because it settled and the hosted
gateway issued a receipt. A seller that answers its paid request with a 5xx,
rejects a request it quoted, answers 200 with no settlement receipt, or never
settles the authorization it was sent leaves no receipt and no judgement, so
until now it left no evidence either: one of every four test purchases on
2026-11-17 ended that way.

The gateway now sends each such call once, as a flat record signed with its
receipt key (the Ed25519 keys published at /api/receipt-keys, the same ones
that sign purchase receipts and judge attestations):

    POST /api/purchase-failures
    {"payload": {type, attempt_id, owner_ref, endpoint, method, network, payee,
                 amount_atomic, asset, http_status, failure, observed_at,
                 traffic, kid},
     "signature": "<base64url Ed25519 over the canonical payload>"}

It is stored as an ``index_observations`` row with ``kind = 'purchase'``,
``ok = false``, ``review_id`` null and ``detail`` the failure, keyed by
``attempt_ref`` so a resend is a no-op. reliability-v2 counts it as beta+1.
The gateway decides what is the seller's doing (purchase-failures.ts in the
app); this end checks the signature, the shape, and the four values it may
carry, and nothing it cannot verify.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from crowdcode.receipts import _flat, _verify, load_receipt_keys

logger = logging.getLogger(__name__)

FAILURE_TYPE = "opencrowd.purchase_failure.v1"
#: The values reliability-v2 counts (agreed with W4). Nothing else is accepted.
FAILURES = ("seller_error", "seller_rejected", "no_receipt", "authorization_expired")
_NETWORKS = {
    "base": "eip155:8453",
    "solana": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
    "tempo": "eip155:4217",
}
_OWNER_REF = re.compile(r"^[0-9a-f]{64}$")
MAX_AGE = timedelta(days=30)
MAX_BYTES = 8192


@dataclass
class Outcome:
    http_status: int
    body: dict[str, Any] = field(default_factory=dict)


def _reject(status: int, code: str, reason: str) -> Outcome:
    return Outcome(status, {"accepted": False, "error_code": code, "reason": reason})


def _parse(payload: dict[str, Any], now: datetime) -> tuple[dict[str, Any] | None, str | None]:
    if payload.get("type") != FAILURE_TYPE:
        return None, "payload is not an OpenCrowd purchase failure"
    if payload.get("failure") not in FAILURES:
        return None, f"failure must be one of {', '.join(FAILURES)}"
    attempt = payload.get("attempt_id")
    if not isinstance(attempt, str) or not re.fullmatch(r"[0-9a-f-]{36}", attempt):
        return None, "attempt_id must be the gateway's attempt uuid"
    endpoint = payload.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint.startswith("https://") or len(endpoint) > 2000:
        return None, "endpoint must be an https URL"
    method = payload.get("method")
    if method not in ("GET", "POST"):
        return None, "method must be GET or POST"
    network = _NETWORKS.get(str(payload.get("network")))
    if network is None:
        return None, "network must be base, solana or tempo"
    owner = payload.get("owner_ref")
    if not isinstance(owner, str) or not _OWNER_REF.match(owner):
        return None, "owner_ref is missing"
    status = payload.get("http_status")
    if status is not None and (not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599):
        return None, "http_status must be an HTTP status or null"
    amount = str(payload.get("amount_atomic") or "")
    if not amount.isdigit():
        return None, "amount_atomic must be atomic units"
    traffic = payload.get("traffic")
    if traffic not in ("test", "owner"):
        return None, "traffic must be test or owner"
    try:
        at = datetime.fromisoformat(str(payload.get("observed_at")).replace("Z", "+00:00"))
    except ValueError:
        return None, "observed_at must be an ISO timestamp"
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    if at > now + timedelta(minutes=5) or now - at > MAX_AGE:
        return None, "observed_at is outside the accepted window"
    payee = payload.get("payee")
    asset = payload.get("asset")
    return {
        "attempt": attempt, "endpoint": endpoint, "method": method, "network": network, "status": status,
        "amount": int(amount), "failure": payload["failure"], "traffic": traffic, "at": at,
        "payee": payee.lower() if isinstance(payee, str) and payee.startswith("0x") else payee,
        "asset": asset.lower() if isinstance(asset, str) and asset.startswith("0x") else asset,
    }, None


def submit_failure(body: Any, connect: Any, *, keys: dict[str, bytes] | None = None, now: datetime | None = None) -> Outcome:
    now = now or datetime.now(UTC)
    if not isinstance(body, dict) or set(body) - {"payload", "signature"}:
        return _reject(400, "invalid_failure", "body must be {payload, signature}")
    payload, signature = body.get("payload"), body.get("signature")
    if not _flat(payload):
        return _reject(400, "invalid_failure", "payload must be a flat object")
    keys = load_receipt_keys() if keys is None else keys
    if not keys:
        return _reject(503, "receipt_keys_unconfigured", "this deployment has no gateway receipt keys")
    problem = _verify(payload, signature, keys)
    if problem:
        return _reject(401, "signature_invalid", problem)
    parsed, error = _parse(payload, now)
    if parsed is None:
        return _reject(400, "invalid_failure", error or "invalid failure record")
    try:
        with connect() as conn:
            from crowdcode.index import resolve_resource

            resource = resolve_resource(conn, parsed["endpoint"], parsed["method"])
            row = conn.execute(
                """
                insert into index_observations (resource_id, endpoint, method, at, kind, ok, http_status, network,
                  payee, quoted_amount_atomic, asset, protocol, detail, attempt_ref, traffic)
                values (%s, %s, %s, %s, 'purchase', false, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (attempt_ref) where kind = 'purchase' and attempt_ref is not null do nothing
                returning id
                """,
                (resource["id"] if resource else None, parsed["endpoint"], parsed["method"], min(parsed["at"], now),
                 parsed["status"], parsed["network"], parsed["payee"], parsed["amount"], parsed["asset"],
                 "mpp" if parsed["network"] == _NETWORKS["tempo"] else "x402", parsed["failure"], parsed["attempt"],
                 parsed["traffic"]),
            ).fetchone()
            conn.commit()
    except psycopg.errors.UndefinedColumn:
        return _reject(503, "schema_not_applied", "supabase/judge-v2-evidence.sql has not been applied")
    if row is None:
        return Outcome(200, {"accepted": True, "duplicate": True})
    logger.info(json.dumps({"event": "purchase_failure_recorded", "observation_id": row["id"],
                            "endpoint": parsed["endpoint"], "failure": parsed["failure"], "traffic": parsed["traffic"]}))
    return Outcome(200, {"accepted": True, "observation_id": row["id"],
                         "counts_as": "one seller-attributable failure (C10: beta+1)"})


def reread_partials(connect: Any, limit: int = 2000) -> dict[str, int]:
    """C11's back-fill: each judge.v1 ``partial`` re-read once, by the judge's own text."""
    from crowdcode.rubric import reread_partial

    done = {"delivered": 0, "not_delivered": 0}
    with connect() as conn:
        exists = conn.execute(
            "select 1 from information_schema.columns where table_name = 'reviews' and column_name = 'delivery_reread'"
            " and table_schema = any(current_schemas(false))"
        ).fetchone()
        if not exists:
            print("partial re-read: supabase/judge-v2-evidence.sql not applied yet; skipped")
            return done
        rows = conn.execute(
            """
            select id, reason from reviews
            where delivery_reread is null and receipt_key_id is not null
              and gateway_attestation->'review'->>'delivery' = 'partial'
            order by id limit %s
            """,
            (limit,),
        ).fetchall()
        for row in rows:
            delivery, note = reread_partial(row["reason"])
            conn.execute("update reviews set delivery_reread = %s, delivery_reread_note = %s where id = %s and delivery_reread is null",
                         (delivery, note, row["id"]))
            done[delivery] += 1
        conn.commit()
    print(f"partial re-read: {json.dumps(done)}")
    return done
