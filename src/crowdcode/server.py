from __future__ import annotations

import hmac
import json
import logging
import os
from importlib.metadata import version
from functools import wraps
from typing import Any
from uuid import uuid4

import psycopg
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from psycopg.types.json import Jsonb
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from crowdcode.db import connect
from crowdcode.identity import (
    build_identity,
    create_service_from_identity,
    register_machine_payment_alias,
    resolve_service,
)
from crowdcode.payments import (
    REASON_HASH_RE,
    PaymentVerification,
    _normalize_evm_address,
    canonical_payment_reference,
    canonical_review_payload,
    canonical_review_payload_from_hash,
    utc_now,
    verify_review_payment,
)
from crowdcode.rate_limit import (
    AGENTCASH_INSTALL_COMMAND,
    check_request_limit,
    identity_id_from_wallet,
    rate_limit_payload,
)
from crowdcode.redaction import (
    RedactionUnavailable,
    redact_texts,
    redaction_enabled,
)
from crowdcode.review_management import authorize, review_replay_key, lock_review_writes
from crowdcode.reputation import (
    ensure_user,
    recompute_service_score,
    sync_seed_wallets,
)

logger = logging.getLogger(__name__)
from crowdcode.scoring import (
    ALGORITHM as SCORE_ALGORITHM,
    as_float,
    is_unproven,
)
from crowdcode.settings import (
    get_mcp_allowed_hosts,
    get_mcp_allowed_origins,
    get_mcp_host,
    get_mcp_port,
    get_settings,
)

mcp = FastMCP(
    "CrowdCode",
    host=get_mcp_host(),
    port=get_mcp_port(),
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(get_mcp_allowed_hosts()),
        allowed_origins=list(get_mcp_allowed_origins()),
    ),
)

def _json_ready(row: dict[str, Any]) -> dict[str, Any]:
    clean = dict(row)
    created_at = clean.get("created_at")
    if created_at is not None:
        clean["created_at"] = created_at.isoformat()
    return clean


def _next_step(
    action: str,
    summary: str,
    *,
    command: str | None = None,
    link: str | None = None,
    retry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Machine-actionable CTA attached to responses: what to do, the literal
    command or link that does it, and what to retry afterward."""
    return {
        "action": action,
        "summary": summary,
        "command": command,
        "link": link,
        "retry": retry,
    }


def _classify_tool_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Attach the additive v0.5 status envelope to every tool response."""
    if "status" in payload:
        payload.setdefault("error_code", None)
        payload.setdefault("retryable", False)
        return payload

    reason = str(payload.get("reason") or "")
    if payload.get("found") is False:
        status = "not_found" if reason == "service not found" else "rejected"
    elif payload.get("accepted") is False or payload.get("ok") is False:
        status = "rejected"
    else:
        status = "ok"
    payload["status"] = status
    payload.setdefault("error_code", None if status == "ok" else "request_rejected")
    retry = payload.get("next_step")
    payload.setdefault(
        "retryable",
        isinstance(retry, dict) and retry.get("retry") is not None,
    )
    return payload


def _stable_tool(func):
    """Convert unexpected dependency failures into stable, non-leaking results."""
    @wraps(func)
    def wrapped(*args, **kwargs):
        try:
            return _classify_tool_payload(func(*args, **kwargs))
        except Exception:
            correlation_id = uuid4().hex
            logger.exception(
                "CrowdCode tool failed tool=%s correlation_id=%s",
                func.__name__,
                correlation_id,
            )
            payload: dict[str, Any] = {
                "status": "unavailable",
                "error_code": "backend_dependency_unavailable",
                "retryable": True,
                "reason": "CrowdCode is temporarily unavailable",
                "correlation_id": correlation_id,
            }
            if func.__name__ == "get_service_score":
                payload.update(
                    found=False,
                    score=None,
                    n_eff=0,
                    avg_rating=None,
                    num_reviews=0,
                    summary=None,
                    recent_reviews=[],
                )
            elif func.__name__ == "get_review_signing_payload":
                payload["ok"] = False
            else:
                payload["accepted"] = False
            return payload
    return wrapped


@mcp.tool()
@_stable_tool
def request_service(
    service_description: str,
    task_context: str | None = None,
    requester_wallet: str | None = None,
) -> dict[str, Any]:
    """Record concrete paid-service demand discovered while completing a real task.

    Before the final answer, reflect on observed failures, poor results,
    excessive cost, or avoidable detours, even if no purchase was attempted.
    Describe a reusable service: exact input, paid deliverable and acceptance
    criteria, actual use case and obstacle, and why the result is worth paying
    for. An existing service qualifies only with a specific deficiency.
    No actual purchase or spending authority is required; never invent a
    budget or claim the user authorized payment. Skip local runtime wishes,
    generic run-Python tasks, web search that already worked well, and ordinary
    agent errors without a concrete sellable remedy. If nothing qualifies,
    submit nothing. Submit each distinct gap once per task, respecting limits.
    Example: scanned annual reports to reconciled tables with page citations
    and confidence flags, after OCR dropped columns and forced manual checks.
    Requires requester_wallet, attached automatically by the local client.
    Never include secrets or private user data.
    """
    service_description = service_description.strip()
    task_context = task_context.strip() if task_context else None

    if not service_description:
        return {"accepted": False, "reason": "service_description is required"}

    wallet = _normalize_evm_address(requester_wallet) if requester_wallet else None
    if wallet is None:
        return {
            "accepted": False,
            "reason": "requester_wallet is required (an EVM 0x address identifying who is asking)",
            "next_step": _next_step(
                "install_wallet",
                "Service requests need a wallet identity for rate limiting. "
                "crowdcode-mcp >= 0.2.0 attaches your local wallet automatically — "
                "install agentcash, then retry request_service.",
                command=AGENTCASH_INSTALL_COMMAND,
            ),
        }
    requester_id = identity_id_from_wallet(wallet)

    # Ingest enforcement (fail-closed): free text is redacted before storage
    # so raw PII/secrets from clients that bypass crowdcode-mcp never land
    # in the shared database. Wallet fields are structured identifiers and
    # never pass through redaction.
    try:
        redacted = redact_texts([service_description, task_context], fail_closed=True)
    except RedactionUnavailable:
        return {
            "status": "unavailable",
            "error_code": "redaction_unavailable",
            "retryable": True,
            "accepted": False,
            "reason": "redaction service unavailable; retry shortly",
            "next_step": _next_step(
                "retry_redaction",
                "The redaction sidecar is temporarily unavailable; retry the same call shortly.",
                retry={"tool": "request_service", "after_seconds": 30, "with": {}},
            ),
        }
    if redacted is not None:
        service_description = redacted[0] or service_description
        task_context = redacted[1]

    settings = get_settings()
    with connect() as conn:
        limit_check = check_request_limit(
            conn, requester_id, settings.request_rate_limit_per_day, utc_now()
        )
        if not limit_check.allowed:
            return rate_limit_payload(
                limit_check,
                f"{settings.request_rate_limit_per_day} service requests per wallet per 24 hours",
                retry_tool="request_service",
            )

        row = conn.execute(
            """
            insert into service_requests (
              service_description, task_context, requester_id, requester_wallet,
              redacted_at
            )
            values (%s, %s, %s, %s, %s)
            returning id, directory_match
            """,
            (
                service_description,
                task_context,
                requester_id,
                wallet,
                utc_now() if redaction_enabled() else None,
            ),
        ).fetchone()
        conn.commit()

    return {
        "accepted": True,
        "request_id": row["id"],
        "directory_match": row["directory_match"],
        "requests_remaining_today": limit_check.remaining,
    }


@mcp.tool()
@_stable_tool
def get_service_score(
    service_id: str | None = None,
    api_endpoint: str | None = None,
    payment_provider: str | None = None,
    payment_target_ref: str | None = None,
    directory_slug: str | None = None,
) -> dict[str, Any]:
    """Return the canonical trust-weighted score for a service.

    Check this before purchasing a third-party paid API, remote service, or
    provisioned infrastructure. Do not use CrowdCode to gate trades, swaps,
    transfers, deposits, withdrawals, staking, lending, escrow settlement, or
    purchases of financial instruments. Identify the service by service_id,
    api_endpoint, payment target, or directory_slug.
    """
    try:
        identity = build_identity(
            service_id=service_id,
            api_endpoint=api_endpoint,
            payment_provider=payment_provider,
            payment_target_ref=payment_target_ref,
            directory_slug=directory_slug,
        )
    except ValueError as exc:
        return _score_not_found(service_id, str(exc))

    with connect() as conn:
        resolved = resolve_service(conn, identity)
        if resolved.error:
            return _score_not_found(identity.service_id, resolved.error)
        service = resolved.row
        if service is None:
            return _score_not_found(identity.service_id, "service not found")
        assert resolved.identity is not None

        score = conn.execute(
            """
            select
              avg(rating) as avg_rating,
              count(*)::int as num_reviews,
              count(*) filter (where payment_verified)::int as num_verified_reviews,
              avg(rating) filter (where payment_verified) as verified_avg_rating,
              count(*) filter (where payment_verification_level
                in ('onchain_verified', 'response_attested'))::int
                as num_onchain_verified_reviews,
              count(*) filter (where payment_verification_level
                in ('unverified', 'signature_only'))::int
                as num_signature_only_reviews
            from reviews
            where service_id = %s
            """,
            (service["id"],),
        ).fetchone()
        stored = conn.execute(
            "select score, n_eff, review_summary from services where id = %s",
            (service["id"],),
        ).fetchone()
        recent_reviews = conn.execute(
            """
            select rating, reason, task_context, payment_verified,
                   payment_verification_level as verification_level, created_at
            from reviews
            where service_id = %s
            order by created_at desc
            limit 5
            """,
            (service["id"],),
        ).fetchall()

    # Egress backstop: rows written before enforcement may contain raw text;
    # redact on the way out, dropping free text if the redactor is down.
    review_texts: list[str | None] = []
    for review in recent_reviews:
        review_texts.append(review.get("reason"))
        review_texts.append(review.get("task_context"))
    redacted = redact_texts(review_texts, fail_closed=False)
    for index, review in enumerate(recent_reviews):
        if redacted is None:
            review["reason"] = None
            review["task_context"] = None
        else:
            review["reason"] = redacted[index * 2]
            review["task_context"] = redacted[index * 2 + 1]

    canonical_score = as_float(stored["score"]) if stored else None
    n_eff = as_float(stored["n_eff"]) if stored else 0.0
    return {
        "service_id": service["id"],
        "service_name": service["name"],
        "canonical_endpoint": service.get("canonical_endpoint"),
        "payment_provider": service.get("payment_provider"),
        "payment_target_ref": service.get("payment_target_ref"),
        "directory_slug": service.get("directory_slug"),
        "resolved_identity": {
            "service_id": resolved.identity.service_id,
            "api_endpoint": resolved.identity.api_endpoint,
            "payment_provider": resolved.identity.payment_provider,
            "payment_target_ref": resolved.identity.payment_target_ref,
            "directory_slug": resolved.identity.directory_slug,
        },
        "found": True,
        "score": canonical_score,
        "n_eff": n_eff,
        "unproven": is_unproven(n_eff or 0.0),
        "score_algorithm": SCORE_ALGORITHM,
        "avg_rating": as_float(score["avg_rating"]),
        "num_reviews": score["num_reviews"],
        "num_verified_reviews": score["num_verified_reviews"],
        "num_onchain_verified_reviews": score["num_onchain_verified_reviews"],
        "num_signature_only_reviews": score["num_signature_only_reviews"],
        "verified_avg_rating": as_float(score["verified_avg_rating"]),
        # Deprecated alias of the canonical score, kept so pre-0.3.0 clients
        # silently upgrade instead of reading a dead formula.
        "weighted_rating": canonical_score,
        "summary": _redacted_summary(stored["review_summary"] if stored else None),
        "recent_reviews": [_json_ready(row) for row in recent_reviews],
    }


def _redacted_summary(summary: Any) -> dict[str, Any] | None:
    """Egress backstop for the cron-generated review summary: its inputs were
    redacted, but re-redact on the way out anyway; drop the summary entirely
    if the redactor is configured but down (never leak on failure)."""
    if not isinstance(summary, dict):
        return None
    keys = ("strengths", "failure_modes", "caveats")
    texts: list[str | None] = []
    for key in keys:
        items = summary.get(key)
        if isinstance(items, list):
            texts.extend(str(item) for item in items)
    redacted = redact_texts(texts, fail_closed=False)
    if redacted is None:
        return None
    clean = dict(summary)
    cursor = 0
    for key in keys:
        items = summary.get(key)
        if not isinstance(items, list):
            clean[key] = []
            continue
        clean[key] = [
            redacted[cursor + offset] or "" for offset in range(len(items))
        ]
        clean[key] = [item for item in clean[key] if item]
        cursor += len(items)
    return clean


def _score_not_found(service_id: str | None, reason: str) -> dict[str, Any]:
    not_found = reason == "service not found"
    return {
        "status": "not_found" if not_found else "rejected",
        "error_code": "service_not_found" if not_found else "service_identity_invalid",
        "retryable": False,
        "service_id": service_id,
        "found": False,
        "score": None,
        "n_eff": 0,
        "unproven": True,
        "score_algorithm": SCORE_ALGORITHM,
        "avg_rating": None,
        "num_reviews": 0,
        "num_verified_reviews": 0,
        "verified_avg_rating": None,
        "weighted_rating": None,
        "summary": None,
        "recent_reviews": [],
        "reason": reason,
    }


@mcp.tool()
@_stable_tool
def get_review_signing_payload(
    rating: int,
    reason_hash: str,
    payment_reference: str | None = None,
    review_nonce: str | None = None,
    service_id: str | None = None,
    api_endpoint: str | None = None,
    payment_provider: str | None = None,
    payment_target_ref: str | None = None,
    directory_slug: str | None = None,
) -> dict[str, Any]:
    """Return the exact EIP-191 message to sign before reviewing.

    Send only a hash of the review text, never the text itself: compute
    reason_hash locally as "sha256:" + sha256(reason.strip() utf-8 bytes) in
    lowercase hex, over the exact reason string you will later pass to
    review_service. (The crowdcode-mcp package builds this payload entirely
    locally and does not call this tool.)

    Sign the returned `message` VERBATIM (byte-for-byte) with the payer wallet:
    the same self-custody wallet that sent the payment, whose key you hold. A
    custodial or login-only wallet that cannot sign an arbitrary message will
    not work.
    """
    reason_hash = reason_hash.strip().lower()
    if payment_reference is None and review_nonce is None:
        review_nonce = str(uuid4())
    if not REASON_HASH_RE.match(reason_hash):
        return {
            "status": "rejected",
            "error_code": "invalid_reason_hash",
            "retryable": False,
            "ok": False,
            "reason": "reason_hash must look like sha256:<64 lowercase hex chars>",
        }
    try:
        identity = build_identity(
            service_id=service_id,
            api_endpoint=api_endpoint,
            payment_provider=payment_provider,
            payment_target_ref=payment_target_ref,
            directory_slug=directory_slug,
        )
    except ValueError as exc:
        return {"ok": False, "reason": str(exc)}

    with connect() as conn:
        resolved = resolve_service(conn, identity)
        if resolved.error:
            return {"ok": False, "reason": resolved.error}
        service = resolved.row

    if service is not None:
        assert resolved.identity is not None
        identity = resolved.identity

    return {
        "ok": True,
        "signature_scheme": "eip191",
        **({"review_nonce": review_nonce} if payment_reference is None else {}),
        "message": canonical_review_payload_from_hash(
            identity=identity,
            rating=rating,
            reason_hash=reason_hash,
            payment_reference=payment_reference,
            review_nonce=review_nonce,
        ),
    }


@mcp.tool()
@_stable_tool
def list_my_reviews(
    reviewer_wallet: str, expires_at: int, authorization: str,
    before_id: int = 0, limit: int = 25,
) -> dict[str, Any]:
    """List this wallet's reviews, newest first. Local clients sign ownership automatically.

    Follow next_before_id for older pages. No other wallet's records can be
    fetched with this proof. History remains available when local CrowdCode is off.
    """
    if not 1 <= limit <= 100 or not 0 <= before_id <= 9007199254740991:
        return {"ok": False, "error_code": "invalid_pagination"}
    try:
        wallet = authorize("list", reviewer_wallet, expires_at, authorization,
                           before_id=before_id, limit=limit)
    except ValueError as exc:
        return {"ok": False, "error_code": "invalid_authorization", "reason": str(exc)}
    with connect() as conn:
        rows = conn.execute(
            """select r.id as review_id, r.service_id, s.name as service_name,
                      r.rating, r.reason, r.task_context, r.created_at,
                      r.payment_verified, r.payment_verification_level
               from reviews r join services s on s.id = r.service_id
               where lower(r.reviewer_wallet) = %s
                 and (%s = 0 or r.id < %s)
               order by r.id desc limit %s""",
            (wallet, before_id, before_id, limit + 1),
        ).fetchall()
    page = rows[:limit]
    texts = [value for row in page for value in (row["reason"], row["task_context"])]
    clean = redact_texts(texts, fail_closed=True) if texts else []
    if clean is not None:
        for index, row in enumerate(page):
            row["reason"], row["task_context"] = clean[index * 2:index * 2 + 2]
    return {"ok": True, "reviews": [_json_ready(row) for row in page],
            "next_before_id": page[-1]["review_id"] if len(rows) > limit else None}


@mcp.tool()
@_stable_tool
def delete_my_review(
    review_id: int, reviewer_wallet: str, expires_at: int, authorization: str,
) -> dict[str, Any]:
    """Delete one review explicitly selected by its owner. Local clients sign automatically.

    Removes review content, clears derived summaries, and recalculates scores.
    Only a hashed replay key remains, preventing old signed retries from
    restoring the review. Repeating deletion is harmless. Available while off.
    """
    if not 1 <= review_id <= 9007199254740991:
        return {"accepted": False, "error_code": "invalid_review_id"}
    try:
        wallet = authorize("delete", reviewer_wallet, expires_at, authorization, review_id=review_id)
    except ValueError as exc:
        return {"accepted": False, "error_code": "invalid_authorization", "reason": str(exc)}
    with connect() as conn:
        lock_review_writes(conn)
        row = conn.execute(
            """select service_id, payment_reference, review_nonce from reviews
               where id = %s and lower(reviewer_wallet) = %s for update""",
            (review_id, wallet),
        ).fetchone()
        if row is None:
            return {"accepted": True, "deleted": False, "review_id": review_id,
                    "reason": "No matching review owned by this wallet"}
        key = review_replay_key(wallet, row["payment_reference"], row["review_nonce"])
        conn.execute("insert into deleted_review_keys(replay_key) values (%s) on conflict do nothing", (key,))
        conn.execute("delete from reviews where id = %s", (review_id,))
        # Trust can affect scores and narrative selection for other services.
        # Clear all derived narratives; a generation guards in-flight LLM jobs.
        conn.execute("""update services set review_summary = null, last_summarized_at = null,
                        review_revision = review_revision + 1""")
        from crowdcode.cron import replay_scores
        replay_scores(conn, utc_now())
        conn.commit()
    return {"accepted": True, "deleted": True, "review_id": review_id}


@mcp.tool()
@_stable_tool
def review_service(
    rating: int,
    reason: str,
    payment_reference: str | None = None,
    review_nonce: str | None = None,
    service_id: str | None = None,
    task_context: str | None = None,
    service_name: str | None = None,
    api_endpoint: str | None = None,
    payment_provider: str | None = None,
    payment_target_ref: str | None = None,
    directory_slug: str | None = None,
    payment_proof: str | dict | None = None,
    payment_challenge: str | dict | None = None,
    reviewer_wallet: str | None = None,
    review_signature: str | None = None,
    signature_scheme: str = "eip191",
) -> dict[str, Any]:
    """Review a service experience, whether or not payment occurred.

    Use one review flow for paid, free, and failed interactions. Omit
    payment_reference and payment_proof when no payment is claimed; supply a
    stable review_nonce (8-128 letters, digits, underscores or hyphens) for
    retry-safe submission. Reviewer wallet signatures remain required for
    unpaid reviews. Payment is explicitly marked unverified in those reviews.

    Rate usefulness for the original task from 1 (unusable) to 5 (excellent).
    Describe the observed outcome and distinguish provider faults from caller
    errors, insufficient funds, and uncertain failures. Do not invent a payment
    reference or blame the provider for a client-side failure.

    When claiming payment, use the actual settlement reference and payee.
    x402 Base USDC and mppx Tempo claims require a verified on-chain transfer
    from reviewer_wallet to the service payee. Optional payment_proof is the
    original base64 response header, not decoded JSON. Invalid supplied payment
    evidence is rejected; it is never downgraded into an unpaid review.
    """
    reason = reason.strip()
    payment_reference = payment_reference.strip() if payment_reference is not None else None
    task_context = task_context.strip() if task_context else None

    # payment_proof / payment_challenge are opaque strings end to end, but some
    # MCP transports coerce a JSON-object-shaped string argument into a dict.
    # Accept that and re-serialize so downstream string parsing still works.
    if isinstance(payment_proof, dict):
        payment_proof = json.dumps(payment_proof)
    if isinstance(payment_challenge, dict):
        payment_challenge = json.dumps(payment_challenge)

    try:
        identity = build_identity(
            service_id=service_id,
            service_name=service_name,
            api_endpoint=api_endpoint,
            payment_provider=payment_provider,
            payment_target_ref=payment_target_ref,
            directory_slug=directory_slug,
        )
    except ValueError as exc:
        return {"accepted": False, "reason": str(exc)}

    if rating < 1 or rating > 5:
        return {"accepted": False, "reason": "rating must be between 1 and 5"}
    if not reason:
        return {"accepted": False, "reason": "reason is required"}

    with connect() as conn:
        resolved = resolve_service(conn, identity)
        if resolved.error:
            return {"accepted": False, "reason": resolved.error}
        service = resolved.row
        service_created = False

        effective_identity = identity
        if service is not None:
            assert resolved.identity is not None
            effective_identity = resolved.identity

        verification = verify_review_payment(
            identity=effective_identity,
            rating=rating,
            reason=reason,
            payment_reference=payment_reference,
            review_nonce=review_nonce,
            payment_proof=payment_proof,
            payment_challenge=payment_challenge,
            reviewer_wallet=reviewer_wallet,
            review_signature=review_signature,
            signature_scheme=signature_scheme,
        )
        if not verification.ok:
            status = (
                "unavailable"
                if verification.retryable
                else "unsupported"
                if verification.error_code
                in {"unsupported_payment_chain", "unsupported_payment_reference"}
                else "rejected"
            )
            failure: dict[str, Any] = {
                "status": status,
                "error_code": verification.error_code or "payment_rejected",
                "retryable": verification.retryable,
                "accepted": False,
                "reason": verification.reason,
            }
            if verification.missing_wallet:
                failure["next_step"] = _next_step(
                    "install_wallet",
                    "mppx/x402 reviews need a signing wallet. crowdcode-mcp >= 0.2.0 "
                    "signs automatically with your local agentcash wallet — "
                    "install agentcash, then retry review_service.",
                    command=AGENTCASH_INSTALL_COMMAND,
                )
            if verification.signature_mismatch:
                # The signed message did not match the server-side canonical
                # payload — usually a service_id resolution race. Return the
                # resolved identity and the exact message to re-sign (identity
                # fields plus the reason hash only; no private data).
                failure["resolved_identity"] = {
                    "service_id": effective_identity.service_id,
                    "api_endpoint": effective_identity.api_endpoint,
                    "payment_provider": effective_identity.payment_provider,
                    "payment_target_ref": effective_identity.payment_target_ref,
                    "directory_slug": effective_identity.directory_slug,
                }
                failure["expected_message"] = canonical_review_payload(
                    identity=effective_identity,
                    rating=rating,
                    reason=reason,
                    payment_reference=payment_reference,
                    review_nonce=review_nonce,
                )
                failure["next_step"] = _next_step(
                    "resign_expected_message",
                    "Sign expected_message VERBATIM with the same wallet, then retry "
                    "review_service using the fields from resolved_identity.",
                    retry={
                        "tool": "review_service",
                        "after_seconds": 0,
                        "with": {"review_signature": "<signature of expected_message>"},
                    },
                )
            return failure

        # Only a successfully verified payer may claim the globally unique
        # canonical transaction reference. This check intentionally runs
        # after signature/on-chain verification to prevent public tx-hash
        # front-running. The unique index remains the concurrency backstop.
        canonical_reference = canonical_payment_reference(payment_reference) if payment_reference is not None else None
        lock_review_writes(conn)
        replay_key = review_replay_key(verification.reviewer_wallet, payment_reference, review_nonce)
        if conn.execute("select replay_key from deleted_review_keys where replay_key = %s", (replay_key,)).fetchone():
            return {"accepted": False, "status": "rejected", "retryable": False,
                    "error_code": "review_deleted", "reason": "This review was deleted; do not resubmit it"}
        if review_nonce and service is not None:
            replay = _unpaid_review_replay(conn, verification, review_nonce, service["id"], rating)
            if replay is not None:
                return replay
        existing = conn.execute(
            """
            select id from reviews
            where payment_reference_canonical = %s
               or (
                 payment_reference_canonical ~* '^0x[0-9a-f]{64}$'
                 and lower(payment_reference_canonical) = lower(%s)
               )
               or payment_reference = %s
            """,
            (canonical_reference, canonical_reference, payment_reference),
        ).fetchone()
        if existing is not None:
            return {
                "status": "rejected",
                "error_code": "payment_reference_used",
                "retryable": False,
                "accepted": False,
                "reason": "payment_reference already used",
            }

        # Ingest enforcement (fail-closed). Runs AFTER signature verification:
        # the signature covers the hash of the reason exactly as received
        # (already redacted when sent via crowdcode-mcp — this is a no-op
        # then); storage gets the re-redacted text either way.
        try:
            redacted = redact_texts([reason, task_context], fail_closed=True)
        except RedactionUnavailable:
            return {
                "status": "unavailable",
                "error_code": "redaction_unavailable",
                "retryable": True,
                "accepted": False,
                "reason": "redaction service unavailable; retry shortly",
                "next_step": _next_step(
                    "retry_redaction",
                    "The redaction sidecar is temporarily unavailable; retry the "
                    "same call shortly.",
                    retry={"tool": "review_service", "after_seconds": 30, "with": {}},
                ),
            }
        if redacted is not None:
            reason = redacted[0] or reason
            task_context = redacted[1]

        if service is None:
            created = create_service_from_identity(conn, identity, payment_verified=payment_reference is not None)
            if created.error:
                return {"accepted": False, "reason": created.error}
            service = created.row
            service_created = created.created

        try:
            # Store each authenticated experience in the same review history. Score influence is aggregated
            # into one wallet/service/UTC-day bucket by compute_score(); trust
            # is replayed authoritatively by the nightly consistency sweep.
            user_id = None
            if verification.reviewer_wallet:
                user = ensure_user(conn, verification.reviewer_wallet)
                user_id = user["user_id"]

            row = conn.execute(
                """
                insert into reviews (
                  service_id, rating, reason, payment_reference, task_context,
                  reviewer_id, payment_provider, payment_target_ref,
                  payment_proof, payment_verified, payment_verified_at,
                  reviewer_wallet, review_signature, signature_scheme,
                  signature_verified, redacted_at, user_id, amount,
                  payment_verification_level, payment_verification_metadata,
                  payment_reference_canonical, review_nonce
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                returning id
                """,
                (
                    service["id"],
                    rating,
                    reason,
                    payment_reference,
                    task_context,
                    verification.reviewer_id,
                    effective_identity.payment_provider,
                    effective_identity.payment_target_ref,
                    Jsonb(verification.metadata or {}),
                    verification.payment_verified,
                    utc_now() if verification.payment_verified else None,
                    verification.reviewer_wallet,
                    verification.review_signature,
                    verification.signature_scheme,
                    verification.signature_verified,
                    utc_now() if redaction_enabled() else None,
                    user_id,
                    verification.amount,
                    verification.payment_verification_level,
                    Jsonb(verification.payment_verification_metadata or {}),
                    verification.canonical_reference or canonical_reference,
                    review_nonce,
                ),
            ).fetchone()

            # A pair that resolution authorized and the chain confirmed is now
            # a registered rail for this service (mppx and x402 can share one
            # payee), so future pair-only lookups resolve directly.
            if not service_created and verification.payment_verified:
                register_machine_payment_alias(
                    conn, service["id"], effective_identity
                )

            # Trust is replayed once per wallet/service/UTC-day bucket by the
            # nightly sweep. The write path still refreshes the affected score
            # immediately using the wallet's current trust.
            recompute_service_score(conn, service["id"], utc_now())
            conn.commit()
        except ValueError as exc:
            conn.rollback()
            return {"accepted": False, "reason": str(exc)}
        except psycopg.errors.UniqueViolation:
            conn.rollback()
            if review_nonce:
                replay = _unpaid_review_replay(conn, verification, review_nonce, service["id"], rating)
                if replay is not None:
                    return replay
            return {
                "status": "rejected",
                "error_code": "payment_reference_used",
                "retryable": False,
                "accepted": False,
                "reason": "payment_reference already used",
            }

    return _review_accepted(service["id"], row["id"], verification, service_created)


def _review_accepted(service_id: str, review_id: int, verification: PaymentVerification,
                     service_created: bool = False) -> dict[str, Any]:
    return {
        "accepted": True,
        "reason": "review accepted",
        "service_id": service_id,
        "service_created": service_created,
        "review_id": review_id,
        "verification": verification.reason,
        "payment_verified": verification.payment_verified,
        "payment_verification_level": verification.payment_verification_level,
        "signature_verified": verification.signature_verified,
        "verified_purchase": verification.payment_verified,
    }


def _unpaid_review_replay(conn: Any, verification: PaymentVerification,
                         nonce: str, service_id: str, rating: int) -> dict[str, Any] | None:
    existing = conn.execute(
        """select id, service_id, rating, payment_proof from reviews
           where lower(reviewer_wallet) = lower(%s) and review_nonce = %s""",
        (verification.reviewer_wallet, nonce),
    ).fetchone()
    if existing is None:
        return None
    original = existing["payment_proof"].get("review_payload", {})
    incoming = (verification.metadata or {}).get("review_payload", {})
    if (existing["service_id"] == service_id and existing["rating"] == rating
            and original.get("reason_hash") == incoming.get("reason_hash")):
        return _review_accepted(service_id, existing["id"], verification)
    return {"accepted": False, "status": "rejected", "retryable": False,
            "error_code": "review_nonce_used", "reason": "review_nonce already used for a different review"}


def _json_error(
    message: str,
    status_code: int = 500,
    *,
    error_code: str = "internal_error",
    retryable: bool = False,
) -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "status": "unavailable" if retryable else "rejected",
            "error": message,
            "error_code": error_code,
            "retryable": retryable,
        },
        status_code=status_code,
    )


def _unexpected_json_error(scope: str) -> JSONResponse:
    correlation_id = uuid4().hex
    logger.exception("HTTP handler failed scope=%s correlation_id=%s", scope, correlation_id)
    response = _json_error(
        "CrowdCode is temporarily unavailable",
        status_code=503,
        error_code="backend_dependency_unavailable",
        retryable=True,
    )
    response.headers["x-correlation-id"] = correlation_id
    return response


def _cron_project_ideas_payload(ttl_seconds: int) -> dict[str, Any] | None:
    """Read the requested-services summary written by the trusted cron job."""
    try:
        with connect() as conn:
            row = conn.execute(
                """
                select payload, generated_at,
                       generated_at > now() - make_interval(secs => %s) as fresh
                from app_cache
                where key = 'project_ideas'
                """,
                (ttl_seconds,),
            ).fetchone()
    except Exception:
        return None
    if row is None or not isinstance(row["payload"], dict):
        return None
    payload = dict(row["payload"])
    payload["source"] = "cron"
    payload["cached"] = True
    payload["stale"] = not bool(row["fresh"])
    return payload


def _project_ideas_payload() -> dict[str, Any]:
    """Serve cron output only; public requests never execute an LLM."""
    settings = get_settings()
    payload = _cron_project_ideas_payload(settings.project_ideas_cache_seconds)
    if payload is not None:
        return payload
    return {
        "ok": True,
        "source": "cron",
        "cached": True,
        "stale": True,
        "generated_at": None,
        "source_request_count": 0,
        "ideas": [],
    }


def _top_services_payload(limit: int | None = 10) -> dict[str, Any]:
    # Canonical score (docs/SCORING.md v1): stored on the services row,
    # refreshed by the review write path and the nightly consistency sweep.
    # The LEFT JOIN keeps zero-review services visible at the prior
    # (score 3.0, n_eff 0 => displayed as "unproven", never as a rating).
    sql = """
        select
          s.id as service_id,
          s.name,
          s.directory_slug,
          s.canonical_endpoint,
          s.payment_provider,
          s.score,
          s.n_eff,
          (s.review_summary is not null) as has_summary,
          avg(r.rating)::float as avg_rating,
          count(r.id)::int as num_reviews,
          count(r.id) filter (where r.payment_verified)::int as num_verified_reviews,
          count(r.id) filter (where r.payment_verification_level
            in ('onchain_verified', 'response_attested'))::int
            as num_onchain_verified_reviews,
          count(r.id) filter (where r.payment_verification_level
            in ('unverified', 'signature_only'))::int
            as num_signature_only_reviews
        from services s
        left join reviews r on r.service_id = s.id
        group by s.id
        order by s.score desc, s.n_eff desc, num_reviews desc, s.name asc
        """
    with connect() as conn:
        if limit is None:
            rows = conn.execute(sql).fetchall()
        else:
            rows = conn.execute(sql + " limit %s", (limit,)).fetchall()

    services = [
        {
            "service_id": row["service_id"],
            "name": row["name"],
            "directory_slug": row.get("directory_slug"),
            "canonical_endpoint": row.get("canonical_endpoint"),
            "payment_provider": row.get("payment_provider"),
            "score": as_float(row["score"]),
            "n_eff": as_float(row["n_eff"]),
            "unproven": is_unproven(as_float(row["n_eff"]) or 0.0),
            "has_summary": bool(row["has_summary"]),
            "avg_rating": as_float(row["avg_rating"]),
            "num_reviews": row["num_reviews"],
            "num_verified_reviews": row["num_verified_reviews"],
            "num_onchain_verified_reviews": row["num_onchain_verified_reviews"],
            "num_signature_only_reviews": row["num_signature_only_reviews"],
            # Deprecated alias of the canonical score (pre-scoring-v1 field).
            "rank_score": as_float(row["score"]),
        }
        for row in rows
    ]
    return {
        "ok": True,
        "services": services,
        "stats": {
            "num_services": len(services),
            "total_reviews": sum(s["num_reviews"] for s in services),
        },
    }


def _service_detail_payload(service_id: str) -> tuple[dict[str, Any], int]:
    with connect() as conn:
        service = conn.execute(
            """
            select id, name, directory_slug, canonical_endpoint,
                   payment_provider, score, n_eff, review_summary
            from services
            where id = %s
            """,
            (service_id,),
        ).fetchone()
        if service is None:
            return {
                "ok": False,
                "status": "not_found",
                "error_code": "service_not_found",
                "retryable": False,
                "error": "service not found",
            }, 404

        counts = conn.execute(
            """
            select
              count(*)::int as num_reviews,
              count(*) filter (where payment_verified)::int as num_verified_reviews,
              count(*) filter (where payment_verification_level
                in ('onchain_verified', 'response_attested'))::int
                as num_onchain_verified_reviews,
              count(*) filter (where payment_verification_level
                in ('unverified', 'signature_only'))::int
                as num_signature_only_reviews
            from reviews
            where service_id = %s
            """,
            (service_id,),
        ).fetchone()
        histogram_rows = conn.execute(
            """
            select rating, count(*)::int as n
            from reviews
            where service_id = %s
            group by rating
            """,
            (service_id,),
        ).fetchall()
        recent_reviews = conn.execute(
            """
            select rating, reason, task_context, payment_verified,
                   payment_verification_level as verification_level, created_at
            from reviews
            where service_id = %s
            order by created_at desc
            limit 5
            """,
            (service_id,),
        ).fetchall()

    # Same egress backstop as get_service_score: re-redact free text on the
    # way out, dropping it if the redactor is down.
    review_texts: list[str | None] = []
    for review in recent_reviews:
        review_texts.append(review.get("reason"))
        review_texts.append(review.get("task_context"))
    redacted = redact_texts(review_texts, fail_closed=False)
    for index, review in enumerate(recent_reviews):
        if redacted is None:
            review["reason"] = None
            review["task_context"] = None
        else:
            review["reason"] = redacted[index * 2]
            review["task_context"] = redacted[index * 2 + 1]

    histogram = {str(rating): 0 for rating in range(1, 6)}
    for row in histogram_rows:
        histogram[str(row["rating"])] = row["n"]

    n_eff = as_float(service["n_eff"]) or 0.0
    return {
        "ok": True,
        "service": {
            "service_id": service["id"],
            "name": service["name"],
            "directory_slug": service.get("directory_slug"),
            "canonical_endpoint": service.get("canonical_endpoint"),
            "payment_provider": service.get("payment_provider"),
        },
        "score": as_float(service["score"]),
        "n_eff": n_eff,
        "unproven": is_unproven(n_eff),
        "score_algorithm": SCORE_ALGORITHM,
        "num_reviews": counts["num_reviews"],
        "num_verified_reviews": counts["num_verified_reviews"],
        "num_onchain_verified_reviews": counts["num_onchain_verified_reviews"],
        "num_signature_only_reviews": counts["num_signature_only_reviews"],
        "histogram": histogram,
        "summary": _redacted_summary(service.get("review_summary")),
        "recent_reviews": [_json_ready(row) for row in recent_reviews],
    }, 200


KNOWLEDGE_EXPORT_DEFAULT_REVIEWS = 50
KNOWLEDGE_EXPORT_MAX_REVIEWS = 500


def _knowledge_evidence_payload(max_reviews: int) -> dict[str, Any]:
    """Full review evidence for every service, in one call. This is the input
    to the OpenCrowd knowledge-tree generator (system prompt / skills folder
    derived from CrowdCode reviews): per-service score, n_eff, the nightly
    digest, and the raw redacted reviews with task context, rating, and the
    verified paid amount (a real price observation)."""
    with connect() as conn:
        services = conn.execute(
            """
            select id, name, directory_slug, canonical_endpoint,
                   payment_provider, score, n_eff, review_summary
            from services
            order by score desc, n_eff desc, name asc
            """
        ).fetchall()
        reviews = conn.execute(
            """
            select service_id, rating, reason, task_context, payment_verified,
                   payment_verification_level as verification_level, amount,
                   created_at
            from reviews
            order by service_id, created_at desc
            """
        ).fetchall()

    grouped: dict[str, list[dict[str, Any]]] = {}
    for review in reviews:
        bucket = grouped.setdefault(review["service_id"], [])
        if len(bucket) < max_reviews:
            bucket.append(dict(review))

    # Same egress backstop as the detail endpoint: re-redact free text on the
    # way out; drop it if the redactor is configured but down.
    kept = [review for bucket in grouped.values() for review in bucket]
    texts: list[str | None] = []
    for review in kept:
        texts.append(review.get("reason"))
        texts.append(review.get("task_context"))
    redacted = redact_texts(texts, fail_closed=False)
    for index, review in enumerate(kept):
        if redacted is None:
            review["reason"] = None
            review["task_context"] = None
        else:
            review["reason"] = redacted[index * 2]
            review["task_context"] = redacted[index * 2 + 1]
        review["amount"] = as_float(review.get("amount"))
        review.pop("service_id", None)

    out = []
    for service in services:
        n_eff = as_float(service["n_eff"]) or 0.0
        service_reviews = grouped.get(service["id"], [])
        out.append(
            {
                "service_id": service["id"],
                "name": service["name"],
                "directory_slug": service.get("directory_slug"),
                "canonical_endpoint": service.get("canonical_endpoint"),
                "payment_provider": service.get("payment_provider"),
                "score": as_float(service["score"]),
                "n_eff": n_eff,
                "unproven": is_unproven(n_eff),
                "num_reviews": len(service_reviews),
                "num_verified_reviews": sum(
                    1 for r in service_reviews if r.get("payment_verified")
                ),
                "summary": _redacted_summary(service.get("review_summary")),
                "reviews": [_json_ready(r) for r in service_reviews],
            }
        )
    return {
        "ok": True,
        "score_algorithm": SCORE_ALGORITHM,
        "max_reviews_per_service": max_reviews,
        "services": out,
        "stats": {
            "num_services": len(out),
            "total_reviews": sum(s["num_reviews"] for s in out),
        },
    }


async def knowledge_evidence(request: Request) -> JSONResponse:
    """Token-gated full evidence export (see _knowledge_evidence_payload).
    Reviews are public data, but the bulk export is gated so only the
    OpenCrowd generator (hosted or a configured local run) pulls it."""
    token = get_settings().knowledge_export_token
    if not token:
        return JSONResponse(
            {"ok": False, "error": "knowledge export is not enabled"},
            status_code=404,
        )
    supplied = request.headers.get("authorization", "")
    if not hmac.compare_digest(supplied.encode(), f"Bearer {token}".encode()):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        max_reviews = int(
            request.query_params.get("max_reviews", KNOWLEDGE_EXPORT_DEFAULT_REVIEWS)
        )
    except ValueError:
        max_reviews = KNOWLEDGE_EXPORT_DEFAULT_REVIEWS
    max_reviews = max(1, min(KNOWLEDGE_EXPORT_MAX_REVIEWS, max_reviews))
    try:
        payload = await run_in_threadpool(_knowledge_evidence_payload, max_reviews)
    except Exception:
        return _unexpected_json_error("knowledge_evidence")
    return JSONResponse(payload, headers={"Cache-Control": "private, max-age=300"})


async def health(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "service": "crowdcode-backend",
                         "version": version("crowdcode-mcp"),
                         "commit": os.environ.get("RENDER_GIT_COMMIT")})


async def ready(_: Request) -> JSONResponse:
    try:
        with connect() as conn:
            conn.execute("select 1").fetchone()
    except Exception:
        return _unexpected_json_error("ready_database")
    if not redaction_enabled():
        return _json_error(
            "redaction service is not configured",
            status_code=503,
            error_code="redaction_unavailable",
            retryable=True,
        )
    try:
        redact_texts(["readiness"], fail_closed=True, timeout=2.0)
    except RedactionUnavailable:
        return _json_error(
            "redaction service is unavailable",
            status_code=503,
            error_code="redaction_unavailable",
            retryable=True,
        )
    return JSONResponse({"ok": True, "ready": True, "service": "crowdcode-backend"})


async def project_ideas(_: Request) -> JSONResponse:
    try:
        payload = await run_in_threadpool(_project_ideas_payload)
    except Exception:
        return _unexpected_json_error("project_ideas")
    return JSONResponse(payload)


async def top_services(_: Request) -> JSONResponse:
    try:
        payload = await run_in_threadpool(_top_services_payload, 10)
    except Exception:
        return _unexpected_json_error("top_services")
    return JSONResponse(payload)


async def all_services(_: Request) -> JSONResponse:
    try:
        payload = await run_in_threadpool(_top_services_payload, None)
    except Exception:
        return _unexpected_json_error("all_services")
    return JSONResponse(payload, headers={"Cache-Control": "public, max-age=60"})


async def service_detail(request: Request) -> JSONResponse:
    service_id = request.path_params["service_id"]
    try:
        payload, status_code = await run_in_threadpool(
            _service_detail_payload, service_id
        )
    except Exception:
        return _unexpected_json_error("service_detail")
    headers = {"Cache-Control": "public, max-age=60"} if status_code == 200 else None
    return JSONResponse(payload, status_code=status_code, headers=headers)


def _sync_seed_wallets_at_startup() -> None:
    """Best-effort seed sync (docs/SCORING.md §3.5): pin the operator wallets
    from CROWDCODE_SEED_WALLETS at trust 1.0. Failures must not stop the
    server — the cron run repeats the sync."""
    settings = get_settings()
    if not settings.seed_wallets:
        return
    try:
        with connect() as conn:
            sync_seed_wallets(conn, settings.seed_wallets)
            conn.commit()
    except Exception as exc:
        print(f"seed wallet sync failed: {exc}")


def create_app() -> Starlette:
    settings = get_settings()
    _sync_seed_wallets_at_startup()
    mcp_app = mcp.streamable_http_app()
    app = Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/ready", ready, methods=["GET"]),
            Route("/api/project-ideas", project_ideas, methods=["GET"]),
            Route("/api/services", all_services, methods=["GET"]),
            Route("/api/services/top", top_services, methods=["GET"]),
            Route("/api/services/{service_id}", service_detail, methods=["GET"]),
            Route("/api/knowledge/evidence", knowledge_evidence, methods=["GET"]),
            *mcp_app.routes,
        ],
        lifespan=mcp_app.router.lifespan_context,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )
    return app


def main() -> None:
    settings = get_settings()
    if settings.mcp_transport == "stdio":
        mcp.run(transport=settings.mcp_transport)
        return

    import uvicorn

    uvicorn.run(
        create_app(),
        host=settings.host,
        port=settings.port,
    )


if __name__ == "__main__":
    main()
