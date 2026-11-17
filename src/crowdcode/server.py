from __future__ import annotations

import hmac
import json
import logging
import os
import re
from importlib.metadata import version
from datetime import UTC, datetime
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

from crowdcode.advertised import learn_advertised_payee
from crowdcode.agent_listings import agent_service_slug, refresh_in_background
from crowdcode.db import connect
from crowdcode.hosted_services import SyncError, parse_services, sync_hosted_services
from crowdcode.demand import RequestExtras, validate_request_extras, validate_request_fields
from crowdcode.observations import submit_observation
from crowdcode import demand_miner, reliability
from crowdcode.request_screen import REJECT_REASONS, SCREEN_VERSION, screen_request
from crowdcode.discovery import find_services, warm_search
from crowdcode.listing_metadata import fill_in_background
from crowdcode.identity import (
    ServiceIdentity,
    build_identity,
    create_service_from_identity,
    register_machine_payment_alias,
    resolve_service,
)
from crowdcode.payments import (
    LEVEL_RESPONSE_ATTESTED,
    REASON_HASH_RE,
    PaymentVerification,
    _normalize_evm_address,
    canonical_payment_reference,
    canonical_review_payload,
    canonical_review_payload_from_hash,
    unlinked_reference_key,
    utc_now,
    verify_review_payment,
)
from crowdcode.rate_limit import (
    AGENTCASH_INSTALL_COMMAND,
    check_request_limit,
    identity_id_from_wallet,
    rate_limit_payload,
)
from crowdcode.networks import EMPTY_NETWORKS, fetch_service_networks
from crowdcode.redaction import (
    BATCH_TIMEOUT,
    RedactionUnavailable,
    redact_texts,
    redaction_enabled,
)
from crowdcode.review_management import authorize, review_replay_key, lock_review_writes
from crowdcode.receipts import evaluate_attestation, published_keys
from crowdcode.reputation import (
    ensure_owner,
    ensure_user,
    recompute_service_score,
    sync_seed_owners,
)

logger = logging.getLogger(__name__)
from crowdcode.scoring import (
    ALGORITHM as SCORE_ALGORITHM,
    as_float,
    is_unproven,
)
from crowdcode.proposals import CACHE_KEY as PROPOSALS_CACHE_KEY
from crowdcode.proposals import PAYLOAD_VERSION as PROPOSALS_VERSION
from crowdcode.proposals import as_project_ideas
from crowdcode.proposals import empty_payload as empty_proposals
from crowdcode.review_sql import (
    directory_listed,
    legacy_reviews,
    public_reviews,
    review_counts,
    weighted_reviews,
)
from crowdcode.summaries import SUMMARY_VERSION
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

# Which reviews are published and weighed: crowdcode.review_sql, shared with
# the cron. Static SQL, interpolated by name.
PUBLIC_REVIEWS = public_reviews()
WEIGHTED_REVIEWS = weighted_reviews()
LEGACY_REVIEWS = legacy_reviews()
REVIEW_COUNTS = review_counts()


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
                )
            elif func.__name__ == "get_review_signing_payload":
                payload["ok"] = False
            else:
                payload["accepted"] = False
            return payload
    return wrapped


def _requests_v2_strict() -> bool:
    return os.environ.get("CROWDCODE_REQUESTS_V2_STRICT", "1").strip().lower() not in ("0", "false", "no")


@mcp.tool()
@_stable_tool
def request_service(
    input: str,
    output: str,
    problem: str,
    price_per_call: float,
    builds_on: str | None = None,
    frequency: str | None = None,
    capability: str | None = None,
    requester_wallet: str | None = None,
) -> dict[str, Any]:
    """Record a concrete paid service that would have fixed an observed failure,
    a poor result, or excessive cost in a real task.

    Call it before your final answer when a reusable paid API call would have
    fixed what went wrong. No purchase or spending authority is needed.
    Builders read these fields to decide what to build:
    - input: the clear input the user will give, e.g. "scanned annual-report
      PDFs, up to 200 pages".
    - output: the exact output they want back, e.g. "one CSV per financial
      table with source-page citations and uncertain cells flagged".
    - problem: what failed, was poor or cost too much, e.g. "ordinary OCR
      drops columns and misaligns totals".
    - builds_on: what it could be built from today: an official API, a
      licensed dataset, existing paid services combined, or a human-ops team.
      Required; "nothing exists" is rejected.
    - frequency: one_off, per_task_like_this or recurring (daily or weekly).
    - price_per_call: what you would pay per call in USD.
    - capability: optional capability tree node id; mapped when omitted.
    Describe a reusable service, not your task's answer: requests that restate
    the task, say "N/A" or lack builds_on are rejected. No names, URLs, IDs or
    private data. If nothing qualifies, submit nothing. Requests stay private:
    CrowdCode publishes only aggregated, generalized proposals.
    requester_wallet is attached by the local client. Older clients' `kind`
    and `unlocks` are ignored.
    """
    fields, error = validate_request_fields(input, output, problem, price_per_call)
    if fields is None:
        return {"accepted": False, "error_code": "invalid_request_fields", "reason": error}
    legacy = builds_on is None and frequency is None
    if legacy and not _requests_v2_strict():
        # Rollout only: a client that predates C6 sends none of the new fields.
        # It is still screened below; once every client sends them, strict mode
        # (the default) rejects it for lacking builds_on.
        extras = RequestExtras(builds_on="", frequency=None, capability=None)
    else:
        extras, error = validate_request_extras(builds_on, frequency, capability)
        if extras is None:
            return {"accepted": False, "error_code": "invalid_request_fields", "reason": error}

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
    texts = [fields.input, fields.output, fields.problem, extras.builds_on]
    try:
        redacted = redact_texts(texts, fail_closed=True)
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
        # Store exactly what the redactor returned, even an emptied field:
        # falling back to the original would store the unredacted text.
        texts = [clean or "" for clean in redacted]

    settings = get_settings()
    # The rate limit is checked before the model call (so a throttled wallet
    # costs nothing) and again at insert; no connection is held across the call.
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

    screen = screen_request({key: value for key, value in {
        "input": texts[0], "output": texts[1], "problem": texts[2],
        "builds_on": texts[3], "frequency": extras.frequency,
        "price_per_call_usd": fields.price_per_call,
    }.items() if value})
    if screen.rejected:
        logger.info("service request rejected by screen: %s", screen.verdict)
        return {
            "accepted": False,
            "error_code": f"request_{screen.verdict}",
            "reason": REJECT_REASONS[screen.verdict],
            "screen": {"verdict": screen.verdict, "note": screen.note, "version": SCREEN_VERSION},
        }

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
              request_input, request_output, request_problem, price_per_call_usd,
              fields_source, requester_id, requester_wallet, redacted_at,
              builds_on, frequency, capability, screen_verdict, screened_at
            )
            values (%s, %s, %s, %s, 'agent', %s, %s, %s, %s, %s, %s, %s, %s)
            returning id
            """,
            (
                *texts[:3],
                fields.price_per_call,
                requester_id,
                wallet,
                utc_now() if redaction_enabled() else None,
                texts[3] or None,
                extras.frequency,
                extras.capability,
                screen.verdict,
                utc_now(),
            ),
        ).fetchone()
        conn.commit()

    return {
        "accepted": True,
        "request_id": row["id"],
        "recorded": {"input": texts[0], "output": texts[1],
                     "problem": texts[2], "builds_on": texts[3],
                     "frequency": extras.frequency, "price_per_call": fields.price_per_call,
                     "capability": extras.capability},
        "screen": screen.verdict,
        "requests_remaining_today": limit_check.remaining,
        "published_as": "aggregated proposals only; this request stays private",
    }


@mcp.tool()
@_stable_tool
def find_service(
    query: str,
    networks: list[str] | None = None,
    balances: dict[str, float] | None = None,
    candidates: list[dict[str, Any]] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Find the best paid service for a capability across Base, Solana and Tempo.

    `query`: the capability in a few words ("web search", "text to speech",
    "seedance video"), not the task's subject. Searches every offering in
    CrowdCode's index (the x402 Bazaar, mpp.dev and the other catalogs, plus
    CrowdCode's own services), within the capability or model asked for, and
    ranks them by a seeded draw of delivery x quality per dollar on the
    payer's best network (adding one transfer between its own networks when
    that network's balance is short). Each result carries its `tier`
    (proven, popular, new; bad is left out) and `why`; `explore: true` marks
    one the draw tried ahead of the best-known option. `networks`: networks
    the payer can pay on (default base, solana, tempo). `balances`: available
    USDC per network, e.g. {"base": 1.2, "solana": 0.4, "tempo": 0}.
    `candidates`: endpoints found elsewhere (e.g. AgentCash search) to rank
    together, each {endpoint, method?, name?, price_usd?, network?, protocol?,
    source?}. Inspect the chosen endpoint for its live quote before paying.
    """
    return find_services(query, networks=networks, balances=balances,
                         candidates=candidates, limit=limit, conn_factory=connect)


def _learn_payees(identity: ServiceIdentity) -> None:
    """Let a payee the service's own 402 advertises join its payee set.

    Runs before resolution, holding no connection while it asks the service
    (crowdcode.advertised). A service that collects at two Base addresses, as
    Exa does, otherwise refuses every purchase paid to the second.
    """
    try:
        learn_advertised_payee(connect, identity)
    except Exception:
        logger.exception("learning a service's advertised payees failed")


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
            # Validated on the way in: a payee that is not an address must never
            # be stored as one. See normalize_payment_target.
            strict=True,
        )
    except ValueError as exc:
        return _score_not_found(service_id, str(exc))

    _learn_payees(identity)
    with connect() as conn:
        resolved = resolve_service(conn, identity)
        if resolved.error:
            return _score_not_found(identity.service_id, resolved.error)
        service = resolved.row
        if service is None:
            return _score_not_found(identity.service_id, "service not found")
        assert resolved.identity is not None

        score = conn.execute(
            f"""
            select {REVIEW_COUNTS}
            from reviews
            where service_id = %s
            """,
            (service["id"],),
        ).fetchone()
        stored = conn.execute(
            "select score, n_eff, n_eff_legacy, n_eff_hosted, review_summary from services where id = %s",
            (service["id"],),
        ).fetchone()

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
        # Weighted evidence is what the score is computed from; community
        # reviews are counted context with no ranking weight.
        "num_weighted_reviews": score["num_weighted_reviews"],
        "num_legacy_reviews": score["num_legacy_reviews"],
        "num_community_reviews": score["num_community_reviews"],
        "weighted_avg_rating": as_float(score["weighted_avg_rating"]),
        # The two eras behind n_eff: frozen pre-cutover evidence, and trusted
        # owner votes from judged hosted purchases.
        "n_eff_legacy": as_float(stored["n_eff_legacy"]) if stored else 0.0,
        "n_eff_hosted": as_float(stored["n_eff_hosted"]) if stored else 0.0,
        "verified_avg_rating": as_float(score["verified_avg_rating"]),
        # Deprecated alias of the canonical score, kept so pre-0.3.0 clients
        # silently upgrade instead of reading a dead formula.
        "weighted_rating": canonical_score,
        # Pros, cons and things to watch out for, generated from the reviews.
        # The reviews themselves are private and never returned.
        "summary": _public_summary(stored["review_summary"] if stored else None),
    }


SUMMARY_KEYS = ("pros", "cons", "watch_out")


def _public_summary(summary: Any) -> dict[str, Any] | None:
    """The published form of a cron-generated review summary.

    Only current-format summaries are served (an older one was written under
    a prompt that could repeat task details; the cron regenerates it). Its
    inputs were redacted, but re-redact on the way out anyway; drop the
    summary entirely if the redactor is configured but down."""
    if not isinstance(summary, dict) or str(summary.get("version")) != str(SUMMARY_VERSION):
        return None
    keys = SUMMARY_KEYS
    texts: list[str | None] = []
    for key in keys:
        items = summary.get(key)
        if isinstance(items, list):
            texts.extend(str(item) for item in items)
    redacted = redact_texts(texts, fail_closed=False)
    if redacted is None:
        return None
    clean = {key: summary.get(key) for key in ("n_reviews", "through_date", "generated_at")}
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
        "num_weighted_reviews": 0,
        "num_legacy_reviews": 0,
        "num_community_reviews": 0,
        "weighted_avg_rating": None,
        "n_eff_legacy": 0,
        "n_eff_hosted": 0,
        "verified_avg_rating": None,
        "weighted_rating": None,
        "summary": None,
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
            # Validated on the way in: a payee that is not an address must never
            # be stored as one. See normalize_payment_target.
            strict=True,
        )
    except ValueError as exc:
        return {"ok": False, "reason": str(exc)}

    _learn_payees(identity)
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
            """select service_id, payment_reference, review_nonce, payment_reference_canonical
               from reviews where id = %s and lower(reviewer_wallet) = %s for update""",
            (review_id, wallet),
        ).fetchone()
        if row is None:
            return {"accepted": True, "deleted": False, "review_id": review_id,
                    "reason": "No matching review owned by this wallet"}
        key = review_replay_key(wallet, row["payment_reference"], row["review_nonce"],
                                stored_canonical=row["payment_reference_canonical"])
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
    payer_signature: str | None = None,
    payer_wallet: str | None = None,
    gateway_attestation: str | dict | None = None,
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

    Only reviews carrying a hosted OpenCrowd gateway attestation
    (gateway_attestation, signed by the gateway that made the paid call and
    containing the platform judge's score) count towards a service's ranking.
    Every other review is accepted and counted as community evidence with no
    ranking weight; there is nothing to opt into and nothing to supply.
    Review text is private: CrowdCode publishes only counts, scores and a
    generated summary (pros, cons, things to watch out for), never the text.

    When claiming payment, use the actual settlement reference and payee.
    x402 Base/Monad USDC and mppx Tempo claims require a verified on-chain transfer
    from reviewer_wallet to the service payee. x402 Solana USDC is referenced as
    x402:solana:<base58 signature> with the Solana payee as payment_target_ref;
    it counts as verified only with payer_signature, the Solana payer's base58
    ed25519 signature over the payer link message (spec/CANONICAL_PAYLOAD.md),
    optionally naming payer_wallet. Without it the payment is recorded as
    observed and the review is weighted as unverified. Optional payment_proof
    is the original base64 response header, not decoded JSON. Invalid supplied
    payment evidence is rejected; it is never downgraded into an unpaid review.
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
            # Validated on the way in: a payee that is not an address must never
            # be stored as one. See normalize_payment_target.
            strict=True,
        )
    except ValueError as exc:
        return {"accepted": False, "reason": str(exc)}

    if rating < 1 or rating > 5:
        return {"accepted": False, "reason": "rating must be between 1 and 5"}
    if not reason:
        return {"accepted": False, "reason": "reason is required"}

    _learn_payees(identity)
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
            payer_signature=payer_signature,
            payer_wallet=payer_wallet,
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
            if verification.expected_payer_message:
                failure["expected_payer_message"] = verification.expected_payer_message
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

        # The hosted gateway attestation decides ranking weight. It is checked
        # after the payment claim and before anything is reserved: a forged or
        # mismatched attestation is rejected outright rather than silently
        # downgraded, exactly like a failing payment proof.
        attested = None
        weight_exclusion = "community"
        if gateway_attestation is not None:
            outcome = evaluate_attestation(
                gateway_attestation,
                api_endpoint=effective_identity.api_endpoint,
                payment_target_ref=effective_identity.payment_target_ref,
                payment_reference=payment_reference,
                rating=rating,
                reason=reason,
                reviewer_wallet=verification.reviewer_wallet,
                min_amount_atomic=get_settings().min_weighted_amount_atomic,
            )
            if not outcome.ok:
                return {"accepted": False, "status": "rejected", "retryable": False,
                        "error_code": "attestation_rejected", "reason": outcome.reason}
            attested = outcome.attestation
            weight_exclusion = outcome.weight_exclusion
        elif not verification.signature_verified:
            # The legacy unsigned admission path (stripe/manual placeholders).
            # Stored for continuity, never published, never weighted.
            weight_exclusion = "unsigned_placeholder"

        # Only a successfully verified payer may claim the globally unique
        # canonical transaction reference. This check intentionally runs
        # after signature/on-chain verification to prevent public tx-hash
        # front-running. The unique index remains the concurrency backstop.
        canonical_reference = canonical_payment_reference(payment_reference) if payment_reference is not None else None
        # An unlinked Solana observation is stored under a reviewer-scoped key
        # and without payment_reference, so it can neither reserve nor (via
        # deletion) block the payment for the payer who later links it.
        own_reference = (
            unlinked_reference_key(verification.reviewer_wallet, canonical_reference)
            if canonical_reference is not None and verification.reviewer_wallet
            else None
        )
        stored_reference = payment_reference if verification.claims_reference else None
        stored_canonical = verification.canonical_reference or canonical_reference
        lock_review_writes(conn)
        replay_keys = [review_replay_key(verification.reviewer_wallet, payment_reference, review_nonce)]
        if not verification.claims_reference:
            replay_keys.append(review_replay_key(
                verification.reviewer_wallet, None, None, stored_canonical=stored_canonical))
        if conn.execute("select replay_key from deleted_review_keys where replay_key = any(%s)", (replay_keys,)).fetchone():
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
               or payment_reference_canonical = %s
            """,
            (canonical_reference, canonical_reference, payment_reference, own_reference),
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
            created = create_service_from_identity(
                conn, identity, payment_verified=stored_reference is not None
            )
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

            # A verified hosted purchase is the signed-receipt tier the level
            # column always reserved: `response_attested` binds the payment to
            # the specific request and response, which nothing else does.
            level = LEVEL_RESPONSE_ATTESTED if attested else verification.payment_verification_level
            payment_verified = verification.payment_verified or attested is not None
            if attested is not None:
                ensure_owner(conn, attested.owner_ref)
            row = conn.execute(
                """
                insert into reviews (
                  service_id, rating, reason, payment_reference, task_context,
                  reviewer_id, payment_provider, payment_target_ref,
                  payment_proof, payment_verified, payment_verified_at,
                  reviewer_wallet, review_signature, signature_scheme,
                  signature_verified, redacted_at, user_id, amount,
                  payment_verification_level, payment_verification_metadata,
                  payment_reference_canonical, review_nonce,
                  verified_source, weight_exclusion, owner_ref, payee_owner_ref,
                  receipt_key_id, gateway_attestation, weight_basis
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                returning id
                """,
                (
                    service["id"],
                    rating,
                    reason,
                    stored_reference,
                    task_context,
                    verification.reviewer_id,
                    effective_identity.payment_provider,
                    effective_identity.payment_target_ref,
                    Jsonb(verification.metadata or {}),
                    payment_verified,
                    utc_now() if payment_verified else None,
                    verification.reviewer_wallet,
                    verification.review_signature,
                    verification.signature_scheme,
                    verification.signature_verified,
                    utc_now() if redaction_enabled() else None,
                    user_id,
                    verification.amount,
                    level,
                    Jsonb(verification.payment_verification_metadata or {}),
                    stored_canonical,
                    review_nonce,
                    "hosted_receipt" if attested else "community",
                    weight_exclusion,
                    attested.owner_ref if attested else None,
                    attested.payee_owner_ref if attested else None,
                    attested.kid if attested else None,
                    Jsonb(_attestation_record(gateway_attestation)) if attested else None,
                    # Only the pre-cutover backfill writes 'legacy'; a review
                    # submitted now is weighed by the hosted rules or not at all.
                    "hosted" if (attested and weight_exclusion is None) else "none",
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

    if service_created and agent_service_slug(service.get("canonical_endpoint")):
        # Named after its slug for now; the agent's declared name and
        # description follow from its manifest, off the request path.
        refresh_in_background([service["id"]])
    elif service_created:
        # A review names an endpoint, not what it does: read that from the
        # service's own 402, OpenAPI or landing page, off the request path.
        fill_in_background([service["id"]])
    if attested is not None:
        # A judged purchase is delivery evidence (C3): refresh the service's
        # reliability now rather than at the next hourly recompute.
        reliability.recompute_in_background(connect, service["id"])
    return _review_accepted(service["id"], row["id"], verification, service_created,
                            weight_exclusion, attested is not None)


def _attestation_record(raw: Any) -> dict[str, Any]:
    """The verified attestation, stored verbatim so any published score can be
    re-derived — and re-verified — from the evidence it rests on."""
    if isinstance(raw, str):
        raw = json.loads(raw)
    return raw if isinstance(raw, dict) else {}


def _review_accepted(service_id: str, review_id: int, verification: PaymentVerification,
                     service_created: bool = False, weight_exclusion: str | None = "community",
                     attested: bool = False) -> dict[str, Any]:
    return {
        "accepted": True,
        "reason": "review accepted",
        "service_id": service_id,
        "service_created": service_created,
        "review_id": review_id,
        "verification": verification.reason,
        "payment_verified": verification.payment_verified or attested,
        "payment_verification_level": LEVEL_RESPONSE_ATTESTED if attested
            else verification.payment_verification_level,
        "signature_verified": verification.signature_verified,
        "verified_purchase": verification.payment_verified or attested,
        # What the reviewer's own submission earned: weighted hosted evidence,
        # or community evidence and why.
        "verified_source": "hosted_receipt" if attested else "community",
        "counts_towards_ranking": weight_exclusion is None,
        "weight_exclusion": weight_exclusion,
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


def _cached_proposals(ttl_seconds: int) -> dict[str, Any] | None:
    """Read the service proposals written by the trusted cron job."""
    try:
        with connect() as conn:
            row = conn.execute(
                """
                select payload, generated_at,
                       generated_at > now() - make_interval(secs => %s) as fresh
                from app_cache
                where key = %s
                """,
                (ttl_seconds, PROPOSALS_CACHE_KEY),
            ).fetchone()
    except Exception:
        return None
    if row is None or not isinstance(row["payload"], dict):
        return None
    payload = dict(row["payload"])
    if payload.get("version") != PROPOSALS_VERSION:
        return None
    payload["source"] = "cron"
    payload["cached"] = True
    payload["stale"] = not bool(row["fresh"])
    return payload


def _service_proposals_payload() -> dict[str, Any]:
    """Serve cron output only; public requests never execute an LLM, and raw
    request text is never part of it."""
    settings = get_settings()
    payload = _cached_proposals(settings.project_ideas_cache_seconds)
    if payload is not None:
        return payload
    return {**empty_proposals(utc_now()), "generated_at": None, "source": "cron",
            "cached": True, "stale": True}


def _project_ideas_payload() -> dict[str, Any]:
    """The pre-v2 shape, derived from the proposals for older clients."""
    return as_project_ideas(_service_proposals_payload())


def _has_reliability(conn: Any) -> bool:
    return bool(conn.execute("select to_regclass('service_reliability') is not null as ok").fetchone()["ok"])


def _top_services_payload(limit: int | None = 10) -> dict[str, Any]:
    # Canonical score (docs/SCORING.md v1): stored on the services row,
    # refreshed by the review write path and the nightly consistency sweep.
    # The LEFT JOIN keeps zero-review services visible at the prior
    # (score 3.0, n_eff 0 => displayed as "unproven", never as a rating).
    sql = f"""
        select
          s.id as service_id,
          s.name,
          s.directory_slug,
          s.canonical_endpoint,
          s.payment_provider,
          s.metadata->>'description' as description,
          s.metadata->>'crowdcode_service_id' as crowdcode_service_id,
          s.score,
          s.n_eff,
          s.n_eff_legacy,
          s.n_eff_hosted,
          (s.review_summary->>'version' = '{SUMMARY_VERSION}') as has_summary,
          {{reliability_columns}}
          {review_counts('r')}
        from services s
        left join reviews r on r.service_id = s.id
        {{reliability_join}}
        -- A hosted service its owner deleted leaves the rankings (hosted_services.py).
        where coalesce(s.metadata->>'hosted_status', '') <> 'deleted'
          and {directory_listed('s')}
        group by s.id
        order by s.score desc, s.n_eff desc, num_weighted_reviews desc, num_reviews desc, s.name asc
        """
    with connect() as conn:
        # reliability-v2's tier and score, where supabase/reliability.sql is applied.
        has_reliability = _has_reliability(conn)
        sql = sql.replace("{reliability_columns}", (
            "max(sr.tier) as tier, max(sr.score) as reliability_score, max(sr.state) as reliability_state,"
            if has_reliability else "")).replace("{reliability_join}", (
            "left join service_reliability sr on sr.service_id = s.id" if has_reliability else ""))
        if limit is None:
            rows = conn.execute(sql).fetchall()
        else:
            rows = conn.execute(sql + " limit %s", (limit,)).fetchall()
        networks = fetch_service_networks(conn)

    services = [
        {
            "service_id": row["service_id"],
            "name": row["name"],
            "description": row.get("description"),
            "directory_slug": row.get("directory_slug"),
            "canonical_endpoint": row.get("canonical_endpoint"),
            "payment_provider": row.get("payment_provider"),
            "crowdcode_service_id": row.get("crowdcode_service_id"),
            "score": as_float(row["score"]),
            "n_eff": as_float(row["n_eff"]),
            "unproven": is_unproven(as_float(row["n_eff"]) or 0.0),
            "has_summary": bool(row["has_summary"]),
            "avg_rating": as_float(row["avg_rating"]),
            "num_reviews": row["num_reviews"],
            "num_verified_reviews": row["num_verified_reviews"],
            "num_onchain_verified_reviews": row["num_onchain_verified_reviews"],
            "num_signature_only_reviews": row["num_signature_only_reviews"],
            "num_weighted_reviews": row["num_weighted_reviews"],
            "num_legacy_reviews": row["num_legacy_reviews"],
            "num_community_reviews": row["num_community_reviews"],
            "weighted_avg_rating": as_float(row["weighted_avg_rating"]),
            "n_eff_legacy": as_float(row["n_eff_legacy"]) or 0.0,
            "n_eff_hosted": as_float(row["n_eff_hosted"]) or 0.0,
            # Deprecated alias of the canonical score (pre-scoring-v1 field).
            "rank_score": as_float(row["score"]),
            # reliability-v2 (C10): the tier and the one score CrowdCode's pages show
            # (delivery estimate x quality). `score` above stays the v3.1 review
            # score, the quality input, because clients validate it as 2-5.
            "tier": row.get("tier") or "new",
            "reliability_score": as_float(row.get("reliability_score")),
            "reliability_state": row.get("reliability_state") or "unverified",
            **networks.get(row["service_id"], EMPTY_NETWORKS),
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
                   payment_provider, score, n_eff, n_eff_legacy, n_eff_hosted,
                   review_summary, metadata->>'description' as description,
                   metadata->>'crowdcode_service_id' as crowdcode_service_id
            from services
            where id = %s
            """,
            (service_id,),
        ).fetchone()
        verdict = conn.execute(
            "select tier, score as reliability_score, state as reliability_state from service_reliability "
            "where service_id = %s", (service_id,),
        ).fetchone() if service is not None and _has_reliability(conn) else None
        if verdict:
            service = {**service, **verdict}
        if service is None:
            return {
                "ok": False,
                "status": "not_found",
                "error_code": "service_not_found",
                "retryable": False,
                "error": "service not found",
            }, 404

        counts = conn.execute(
            f"""
            select {REVIEW_COUNTS}
            from reviews
            where service_id = %s
            """,
            (service_id,),
        ).fetchone()
        histogram_rows = conn.execute(
            f"""
            select rating, count(*)::int as n
            from reviews
            where service_id = %s and {PUBLIC_REVIEWS}
            group by rating
            """,
            (service_id,),
        ).fetchall()
        weighted_histogram_rows = conn.execute(
            f"""
            select rating, count(*)::int as n
            from reviews
            where service_id = %s and ({WEIGHTED_REVIEWS} or {LEGACY_REVIEWS})
            group by rating
            """,
            (service_id,),
        ).fetchall()
        networks = fetch_service_networks(conn, service_id).get(service_id, EMPTY_NETWORKS)

    histogram = {str(rating): 0 for rating in range(1, 6)}
    for row in histogram_rows:
        histogram[str(row["rating"])] = row["n"]
    weighted_histogram = {str(rating): 0 for rating in range(1, 6)}
    for row in weighted_histogram_rows:
        weighted_histogram[str(row["rating"])] = row["n"]

    n_eff = as_float(service["n_eff"]) or 0.0
    return {
        "ok": True,
        "service": {
            "service_id": service["id"],
            "name": service["name"],
            "description": service.get("description"),
            "directory_slug": service.get("directory_slug"),
            "canonical_endpoint": service.get("canonical_endpoint"),
            "payment_provider": service.get("payment_provider"),
            "crowdcode_service_id": service.get("crowdcode_service_id"),
        },
        **networks,
        "score": as_float(service["score"]),
        "n_eff": n_eff,
        "unproven": is_unproven(n_eff),
        "score_algorithm": SCORE_ALGORITHM,
        "tier": service.get("tier") or "new",
        "reliability_score": as_float(service.get("reliability_score")),
        "reliability_state": service.get("reliability_state") or "unverified",
        "num_reviews": counts["num_reviews"],
        "num_verified_reviews": counts["num_verified_reviews"],
        "num_onchain_verified_reviews": counts["num_onchain_verified_reviews"],
        "num_signature_only_reviews": counts["num_signature_only_reviews"],
        "num_weighted_reviews": counts["num_weighted_reviews"],
        "num_legacy_reviews": counts["num_legacy_reviews"],
        "num_community_reviews": counts["num_community_reviews"],
        "weighted_avg_rating": as_float(counts["weighted_avg_rating"]),
        "n_eff_legacy": as_float(service["n_eff_legacy"]) or 0.0,
        "n_eff_hosted": as_float(service["n_eff_hosted"]) or 0.0,
        "histogram": histogram,
        "weighted_histogram": weighted_histogram,
        # The reviews behind these numbers stay private; the summary is the
        # only published form of their content.
        "summary": _public_summary(service.get("review_summary")),
    }, 200


def _knowledge_evidence_payload() -> dict[str, Any]:
    """Every service's evidence in one call: the input to the OpenCrowd
    knowledge-tree generator. Per-service score, n_eff, review counts, the
    rating histogram, the median verified paid amount (a real price
    observation) and the generated summary. Review text is never exported."""
    with connect() as conn:
        services = conn.execute(
            f"""
            select id, name, directory_slug, canonical_endpoint,
                   payment_provider, score, n_eff, n_eff_legacy, n_eff_hosted,
                   review_summary
            from services
            where {directory_listed()}
            order by score desc, n_eff desc, name asc
            """
        ).fetchall()
        counts = conn.execute(
            f"""
            select service_id,
                   count(*)::int as num_reviews,
                   count(*) filter (where payment_verified)::int as num_verified_reviews,
                   count(*) filter (where {WEIGHTED_REVIEWS})::int as num_weighted_reviews,
                   count(*) filter (where {LEGACY_REVIEWS})::int as num_legacy_reviews,
                   count(*) filter (where rating = 1)::int as r1,
                   count(*) filter (where rating = 2)::int as r2,
                   count(*) filter (where rating = 3)::int as r3,
                   count(*) filter (where rating = 4)::int as r4,
                   count(*) filter (where rating = 5)::int as r5,
                   percentile_cont(0.5) within group (order by amount)
                     filter (where payment_verified and amount > 0) as median_paid_amount
            from reviews
            where {PUBLIC_REVIEWS}
            group by service_id
            """
        ).fetchall()
    by_service = {row["service_id"]: row for row in counts}

    out = []
    for service in services:
        n_eff = as_float(service["n_eff"]) or 0.0
        row = by_service.get(service["id"]) or {}
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
                "num_reviews": row.get("num_reviews", 0),
                "num_verified_reviews": row.get("num_verified_reviews", 0),
                "num_weighted_reviews": row.get("num_weighted_reviews", 0),
                "num_legacy_reviews": row.get("num_legacy_reviews", 0),
                "histogram": {str(n): row.get(f"r{n}", 0) for n in range(1, 6)},
                "median_paid_amount": as_float(row.get("median_paid_amount")),
                "summary": _public_summary(service.get("review_summary")),
            }
        )
    return {
        "ok": True,
        "score_algorithm": SCORE_ALGORITHM,
        "services": out,
        "stats": {
            "num_services": len(out),
            "total_reviews": sum(s["num_reviews"] for s in out),
        },
    }


async def knowledge_evidence(request: Request) -> JSONResponse:
    """Token-gated evidence export (see _knowledge_evidence_payload), gated so
    only the OpenCrowd generator (hosted or a configured local run) pulls it.
    It carries summaries and counts, never review text."""
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
        payload = await run_in_threadpool(_knowledge_evidence_payload)
    except Exception:
        return _unexpected_json_error("knowledge_evidence")
    return JSONResponse(payload, headers={"Cache-Control": "private, max-age=300"})


async def receipt_keys(_: Request) -> JSONResponse:
    """The gateway receipt keys this deployment trusts (docs/SCORING.md §3.6).

    Only reviews signed by one of these carry ranking weight, so publishing
    them is what makes that claim checkable: anyone can take a published score,
    pull the attestation it rests on, and verify the signature themselves.
    Public keys only."""
    try:
        keys = published_keys()
    except ValueError:
        return _json_error("receipt keys are misconfigured", status_code=503,
                           error_code="receipt_keys_unavailable", retryable=True)
    return JSONResponse({"ok": True, "alg": "ed25519",
                         "canonical_payload": "json.dumps(payload, sort_keys=True, separators=(\",\", \":\"))",
                         "keys": keys},
                        headers={"Cache-Control": "public, max-age=300"})


async def hosted_services_sync(request: Request) -> JSONResponse:
    """CrowdCode's control plane pushes its hosted services (hosted_services.py)."""
    token = get_settings().hosted_sync_token
    if not token:
        return JSONResponse({"ok": False, "error": "hosted service sync is not enabled"}, status_code=404)
    supplied = request.headers.get("authorization", "")
    if not hmac.compare_digest(supplied.encode(), f"Bearer {token}".encode()):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return _json_error("invalid JSON body", status_code=400, error_code="invalid_request")
    try:
        services = parse_services(body)
    except SyncError as exc:
        return _json_error(str(exc), status_code=400, error_code="invalid_request")

    def apply() -> dict[str, int]:
        with connect() as conn:
            with conn.transaction():
                stats = sync_hosted_services(conn, services)
        return stats

    try:
        stats = await run_in_threadpool(apply)
    except Exception:
        return _unexpected_json_error("hosted_services_sync")
    # Names and descriptions from the manifests too, as at registration.
    refresh_in_background()
    return JSONResponse({"ok": True, **stats})


async def demand_miner_batch(request: Request) -> JSONResponse:
    """CrowdCode's reconciler posts one batch of run summaries (demand_miner.py, C8)."""
    token = get_settings().demand_miner_token
    if not token:
        return JSONResponse({"ok": False, "error": "the demand miner is not enabled"}, status_code=404)
    supplied = request.headers.get("authorization", "")
    if not hmac.compare_digest(supplied.encode(), f"Bearer {token}".encode()):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return _json_error("invalid JSON body", status_code=400, error_code="invalid_request")
    try:
        result = await run_in_threadpool(lambda: demand_miner.mine(body, connect, utc_now()))
    except demand_miner.MinerError as exc:
        return _json_error(str(exc), status_code=400, error_code="invalid_request")
    except RedactionUnavailable:
        return _json_error("redaction service unavailable; retry shortly", status_code=503,
                           error_code="redaction_unavailable", retryable=True)
    except Exception:
        return _unexpected_json_error("demand_miner_batch")
    return JSONResponse(result)


FEED_REDACT_MAX_TEXTS = 40
FEED_REDACT_MAX_CHARS = 4000


async def feed_redact(request: Request) -> JSONResponse:
    """CrowdCode's reconciler redacts the text of the public home-page feed.

    crowdcode.app's feed (SRI-117) shows runs only from owners who chose to
    share them, and only after the same PII redactor the reviews and the
    proposals go through. The redactor is a private sidecar of this backend,
    so the reconciler asks here, with the token it already holds for the
    demand miner. Fail-closed: if the sidecar cannot be reached this answers
    503 and nothing is published; ``redacted`` is false only on a deployment
    with no redactor configured, and the reconciler then publishes nothing.
    """
    token = get_settings().demand_miner_token
    if not token:
        return JSONResponse({"ok": False, "error": "feed redaction is not enabled"}, status_code=404)
    supplied = request.headers.get("authorization", "")
    if not hmac.compare_digest(supplied.encode(), f"Bearer {token}".encode()):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return _json_error("invalid JSON body", status_code=400, error_code="invalid_request")
    texts = body.get("texts") if isinstance(body, dict) else None
    if (not isinstance(texts, list) or len(texts) > FEED_REDACT_MAX_TEXTS
            or not all(isinstance(t, str) and len(t) <= FEED_REDACT_MAX_CHARS for t in texts)):
        return _json_error(
            f"texts must be a list of at most {FEED_REDACT_MAX_TEXTS} strings of at most {FEED_REDACT_MAX_CHARS} characters",
            status_code=400, error_code="invalid_request")
    try:
        redacted = await run_in_threadpool(
            lambda: redact_texts(list(texts), fail_closed=True, timeout=BATCH_TIMEOUT))
    except RedactionUnavailable:
        return _json_error("redaction service unavailable; retry shortly", status_code=503,
                           error_code="redaction_unavailable", retryable=True)
    except Exception:
        return _unexpected_json_error("feed_redact")
    if redacted is None:
        return _json_error("redaction service unavailable; retry shortly", status_code=503,
                           error_code="redaction_unavailable", retryable=True)
    return JSONResponse({"ok": True, "texts": [t or "" for t in redacted],
                         "redacted": redaction_enabled()})


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
        redact_texts(["readiness"], fail_closed=True, timeout=2.0, attempts=1)
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


def cron_status_summary(payload: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    """What `/api/cron/status` says: the last runs and how healthy they are.

    `consecutive_failures` counts failed runs since the last good one; the
    reconciler opens an incident at 2, or when no run has finished for 3 hours.
    Only job names, outcomes, durations and error classes are stored, so the
    whole thing is public.
    """
    runs = [run for run in (payload or {}).get("runs", []) if isinstance(run, dict)]
    consecutive = 0
    for run in runs:
        if run.get("ok"):
            break
        consecutive += 1
    last_success = next((run.get("finished_at") for run in runs if run.get("ok")), None)
    last_finished = runs[0].get("finished_at") if runs else None
    age = None
    if last_finished:
        try:
            age = round((now - datetime.fromisoformat(last_finished)).total_seconds() / 60, 1)
        except ValueError:
            age = None
    return {
        "ok": bool(runs) and bool(runs[0].get("ok")),
        "last_finished_at": last_finished,
        "last_success_at": last_success,
        "minutes_since_last_run": age,
        "consecutive_failures": consecutive,
        "runs": runs[:24],
    }


def _cron_status_payload() -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute(
            "select payload from app_cache where key = 'cron_status'"
        ).fetchone()
    payload = row["payload"] if row and isinstance(row["payload"], dict) else None
    return cron_status_summary(payload, datetime.now(UTC))


async def cron_status(_: Request) -> JSONResponse:
    try:
        payload = await run_in_threadpool(_cron_status_payload)
    except Exception:
        return _unexpected_json_error("cron_status")
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


def _index_health_payload() -> dict[str, Any]:
    from crowdcode import index as services_index
    with connect() as conn:
        return services_index.health(conn, datetime.now(UTC))


async def index_health(_: Request) -> JSONResponse:
    """The services index: tracked count, share probed in 24 h, feed sweep ages (C1)."""
    try:
        payload = await run_in_threadpool(_index_health_payload)
    except Exception:
        return _unexpected_json_error("index_health")
    return JSONResponse(payload, headers={"Cache-Control": "public, max-age=60"})


def _index_resource_payload(url: str, method: str | None) -> dict[str, Any] | None:
    from crowdcode import index as services_index
    with connect() as conn:
        return services_index.resource_detail(conn, url, method)


async def index_resource(request: Request) -> JSONResponse:
    """`GET /api/index/resource?url=<endpoint>[&method=POST]`: one indexed resource,
    its probe evidence and its change history."""
    url = (request.query_params.get("url") or "").strip()
    method = (request.query_params.get("method") or "").strip().upper() or None
    if not url or len(url) > 2000 or (method and method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}):
        return JSONResponse({"ok": False, "error_code": "invalid_request",
                             "error": "url is required; method must be an HTTP method"}, status_code=400)
    try:
        payload = await run_in_threadpool(_index_resource_payload, url, method)
    except Exception:
        return _unexpected_json_error("index_resource")
    if payload is None:
        return JSONResponse({"ok": False, "error_code": "not_indexed", "error": "no indexed resource matches this URL"},
                            status_code=404)
    return JSONResponse(payload, headers={"Cache-Control": "public, max-age=60"})


_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def _service_reliability_payload(service_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        return reliability.for_service(conn, service_id)


async def service_reliability(request: Request) -> JSONResponse:
    """`GET /api/services/{service_id}/reliability`: reliability-v2's tier, posterior, score, reasons and inputs (C3, C10)."""
    service_id = request.path_params["service_id"]
    try:
        payload = await run_in_threadpool(_service_reliability_payload, service_id)
    except Exception:
        return _unexpected_json_error("service_reliability")
    if payload is None:
        return JSONResponse({"ok": False, "error_code": "not_found", "error": "no such service"}, status_code=404)
    return JSONResponse({"ok": True, **payload}, headers={"Cache-Control": "public, max-age=60"})


def _reliability_listing_payload(state: str | None, tier: str | None, limit: int, offset: int) -> dict[str, Any]:
    with connect() as conn:
        return reliability.listing(conn, state=state, tier=tier, limit=limit, offset=offset)


async def reliability_lookup(request: Request) -> JSONResponse:
    """`GET /api/reliability?url=<endpoint>&method=<M>` (what the gateway asks before paying), or
    `GET /api/reliability?state=reliable` or `?tier=proven` `[&limit=&offset=]` (the bulk list) (C3, C10)."""
    params = request.query_params
    url = (params.get("url") or "").strip()
    state = (params.get("state") or "").strip().lower()
    tier = (params.get("tier") or "").strip().lower()
    method = (params.get("method") or "").strip().upper() or None
    if url:
        if len(url) > 2000 or (method and method not in _HTTP_METHODS):
            return JSONResponse({"ok": False, "error_code": "invalid_request",
                                 "error": "url must be at most 2000 characters; method must be an HTTP method"},
                                status_code=400)
        try:
            payload = await run_in_threadpool(reliability.cached_for_url, connect, url, method)
        except Exception:
            return _unexpected_json_error("reliability_lookup")
        return JSONResponse({"ok": True, **payload}, headers={"Cache-Control": "public, max-age=30"})
    if state or tier:
        if (state and state not in reliability.STATES) or (tier and tier not in reliability.TIERS):
            return JSONResponse({"ok": False, "error_code": "invalid_request",
                                 "error": "state must be reliable, unverified or unreliable; "
                                          "tier must be bad, proven, popular or new"}, status_code=400)
        try:
            limit = min(max(int(params.get("limit") or 500), 1), 1000)
            offset = max(int(params.get("offset") or 0), 0)
        except ValueError:
            return JSONResponse({"ok": False, "error_code": "invalid_request",
                                 "error": "limit and offset must be integers"}, status_code=400)
        try:
            payload = await run_in_threadpool(_reliability_listing_payload, state or None, tier or None, limit, offset)
        except Exception:
            return _unexpected_json_error("reliability_list")
        return JSONResponse(payload, headers={"Cache-Control": "public, max-age=60"})
    return JSONResponse({"ok": False, "error_code": "invalid_request", "error": "pass url (and method), state or tier"},
                        status_code=400)

def _tree_row(which: str | None) -> dict[str, Any] | None:
    from crowdcode import capability_tree
    with connect() as conn:
        if which in (None, "", "current"):
            return capability_tree.latest(conn, published_only=True)
        if which == "candidate":
            return capability_tree.latest(conn, published_only=False)
        return capability_tree.get_version(conn, int(which))


def _tree_payload(which: str | None, entry: str | None, full: bool) -> tuple[dict[str, Any], int]:
    from crowdcode import capability_tree
    row = _tree_row(which)
    if row is None:
        return {"ok": False, "error_code": "no_tree", "error": "no capability tree has been built yet"}, 404
    if entry is None:
        if full:
            return {"ok": True, "version": row["version"], "published": row["published"],
                    "l0": capability_tree.render_l0(row["tree"]), "tree": row["tree"]}, 200
        return capability_tree.summary_payload(row), 200
    payload = (capability_tree.full_entry if full else capability_tree.entry_payload)(row, entry)
    if payload is None:
        return {"ok": False, "error_code": "unknown_node",
                "error": "no node or leaf with this id in this version"}, 404
    return payload, 200


async def capability_tree_route(request: Request) -> JSONResponse:
    """C7. `GET /api/tree[?version=current|candidate|<n>][&format=full]` and
    `GET /api/tree/{node_or_leaf}`: the version, the top level and the L0
    rendering (<= 800 tokens), or one node or leaf (<= 600 tokens)."""
    which = (request.query_params.get("version") or "").strip().lower() or None
    if which not in (None, "current", "candidate") and not (which.isdigit() and len(which) < 9):
        return _json_error("version must be current, candidate or a version number",
                           status_code=400, error_code="invalid_request")
    entry = request.path_params.get("entry")
    if entry is not None and (len(entry) > 80 or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", entry.lower())):
        return _json_error("unknown node or leaf id", status_code=404, error_code="unknown_node")
    full = (request.query_params.get("format") or "").strip().lower() == "full"
    try:
        payload, status_code = await run_in_threadpool(_tree_payload, which, entry, full)
    except Exception:
        return _unexpected_json_error("capability_tree")
    headers = {"Cache-Control": "public, max-age=300"} if status_code == 200 else None
    return JSONResponse(payload, status_code=status_code, headers=headers)


async def tree_publish(request: Request) -> JSONResponse:
    """W8's test gate: `POST /api/internal/tree/publish {version, by, evidence}`
    serves a candidate that did at least as well as the current version as
    `current` (C7, "Publishing"). Bearer `CROWDCODE_TREE_GATE_TOKEN`."""
    token = get_settings().tree_gate_token
    if not token:
        return JSONResponse({"ok": False, "error": "tree publishing is not enabled"}, status_code=404)
    supplied = request.headers.get("authorization", "")
    if not hmac.compare_digest(supplied.encode(), f"Bearer {token}".encode()):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return _json_error("invalid JSON body", status_code=400, error_code="invalid_request")
    version, by, evidence = (body.get("version"), body.get("by"), body.get("evidence")) if isinstance(body, dict) else (None, None, None)
    if not isinstance(version, int) or isinstance(version, bool) or version < 1 or not isinstance(by, str) or not by.strip() \
            or not isinstance(evidence, dict):
        return _json_error("version (a positive integer), by and evidence (an object) are required",
                           status_code=400, error_code="invalid_request")

    def apply() -> str:
        from crowdcode import capability_tree
        with connect() as conn:
            with conn.transaction():
                return capability_tree.publish_gated(conn, version, by=by.strip(), evidence=evidence,
                                                     now=datetime.now(UTC))

    try:
        result = await run_in_threadpool(apply)
    except Exception:
        return _unexpected_json_error("tree_publish")
    status_code = {"published": 200, "already_published": 200, "not_found": 404}.get(result, 409)
    return JSONResponse({"ok": status_code == 200, "result": result, "version": version}, status_code=status_code)


def _gate_authorized(request: Request) -> JSONResponse | None:
    """The tree gate's and the evolution loop's shared bearer token (W8, C14)."""
    token = get_settings().tree_gate_token
    if not token:
        return JSONResponse({"ok": False, "error": "tree evolution is not enabled"}, status_code=404)
    supplied = request.headers.get("authorization", "")
    if not hmac.compare_digest(supplied.encode(), f"Bearer {token}".encode()):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    return None


async def _json_body(request: Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def _evolve_chat(model: str | None):
    from crowdcode import capability_tree
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("no model configured")
    chosen = (model or os.environ.get("CROWDCODE_TREE_PROPOSER_MODEL", "") or settings.openai_model).strip()
    return chosen, capability_tree.openai_chat(settings, chosen)


def _record_evolve_spend(spend: Any) -> None:
    from crowdcode import capability_tree, tree_policy
    if spend.usd > 0:
        with connect() as conn:
            capability_tree.record_spend(conn, datetime.now(UTC), spend.usd, key=tree_policy.EVOLVE_SPEND_KEY)
            conn.commit()


async def tree_candidate(request: Request) -> JSONResponse:
    """C14. `POST /api/internal/tree/candidate {base_version, label, policy, meta?}` stores a
    given policy on a base version as an unpublished candidate (no model call)."""
    if (denied := _gate_authorized(request)) is not None:
        return denied
    body = await _json_body(request)
    if body is None or not isinstance(body.get("base_version"), int) or not isinstance(body.get("label"), str):
        return _json_error("base_version, label and policy are required", status_code=400, error_code="invalid_request")

    def apply() -> int:
        from crowdcode import tree_policy
        with connect() as conn:
            with conn.transaction():
                return tree_policy.store_candidate(conn, body["base_version"], body.get("policy"), label=body["label"],
                                                   now=datetime.now(UTC), meta=body.get("meta") if isinstance(body.get("meta"), dict) else None)

    try:
        version = await run_in_threadpool(apply)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        return _json_error(str(exc)[:600], status_code=422, error_code="invalid_policy")
    except Exception:
        return _unexpected_json_error("tree_candidate")
    return JSONResponse({"ok": True, "version": version})


async def tree_propose(request: Request) -> JSONResponse:
    """C14. `POST /api/internal/tree/propose {base_version, parent_version?, label, cases, directive?, model?}`:
    the evolution proposer edits the parent's policy from held-in results and the edit is stored as a
    candidate on the base version. Costed against CROWDCODE_TREE_EVOLVE_DAILY_BUDGET_USD."""
    if (denied := _gate_authorized(request)) is not None:
        return denied
    body = await _json_body(request)
    if body is None or not isinstance(body.get("base_version"), int) or not isinstance(body.get("label"), str) \
            or not isinstance(body.get("cases", []), list):
        return _json_error("base_version, label and cases are required", status_code=400, error_code="invalid_request")

    def run() -> dict[str, Any]:
        from crowdcode import capability_tree, tree_policy
        now = datetime.now(UTC)
        model, chat = _evolve_chat(body.get("model") if isinstance(body.get("model"), str) else None)
        with connect() as conn:
            base = capability_tree.get_version(conn, body["base_version"])
            parent = capability_tree.get_version(conn, body["parent_version"]) if isinstance(body.get("parent_version"), int) else base
            spend = tree_policy.evolve_spend(model, conn, now)
        if base is None or parent is None:
            raise LookupError("no such base or parent version")
        try:
            policy, rationale = tree_policy.propose_policy(
                chat, spend, parent=tree_policy.policy_of(parent["tree"]), tree=base["tree"],
                cases=[c for c in body.get("cases", []) if isinstance(c, dict)],
                directive=body.get("directive") if isinstance(body.get("directive"), str) else None)
        finally:
            _record_evolve_spend(spend)
        with connect() as conn:
            with conn.transaction():
                version = tree_policy.store_candidate(conn, body["base_version"], policy, label=body["label"], now=now,
                                                      meta={"parent": parent["version"], "proposer": model, "rationale": rationale})
        return {"ok": True, "version": version, "policy": policy, "rationale": rationale, "spend": spend.summary()}

    try:
        payload = await run_in_threadpool(run)
    except LookupError as exc:
        return _json_error(str(exc), status_code=404, error_code="not_found")
    except ValueError as exc:
        return _json_error(str(exc)[:600], status_code=422, error_code="invalid_policy")
    except Exception as exc:
        from crowdcode.capability_tree import BudgetExhausted
        if isinstance(exc, BudgetExhausted):
            return _json_error("the day's evolution budget is spent", status_code=429, error_code="budget_exhausted")
        return _unexpected_json_error("tree_propose")
    return JSONResponse(payload)


async def eval_judge(request: Request) -> JSONResponse:
    """C14. `POST /api/internal/eval/judge {task, rubric, answer, files?}` grades an open-ended
    evaluation task. Costed against the same daily evolution budget."""
    if (denied := _gate_authorized(request)) is not None:
        return denied
    body = await _json_body(request)
    if body is None or not all(isinstance(body.get(k), str) for k in ("task", "rubric", "answer")):
        return _json_error("task, rubric and answer are required", status_code=400, error_code="invalid_request")

    def run() -> dict[str, Any]:
        from crowdcode import tree_policy
        model, chat = _evolve_chat(os.environ.get("CROWDCODE_EVAL_JUDGE_MODEL") or None)
        with connect() as conn:
            spend = tree_policy.evolve_spend(model, conn, datetime.now(UTC))
        try:
            verdict = tree_policy.judge_answer(chat, spend, task=body["task"], rubric=body["rubric"], answer=body["answer"],
                                               files=[f for f in body.get("files") or [] if isinstance(f, dict)])
        finally:
            _record_evolve_spend(spend)
        return {"ok": True, **verdict, "model": model, "usd": round(spend.usd, 6)}

    try:
        payload = await run_in_threadpool(run)
    except ValueError as exc:
        return _json_error(str(exc), status_code=502, error_code="judge_failed", retryable=True)
    except Exception as exc:
        from crowdcode.capability_tree import BudgetExhausted
        if isinstance(exc, BudgetExhausted):
            return _json_error("the day's evolution budget is spent", status_code=429, error_code="budget_exhausted")
        return _unexpected_json_error("eval_judge")
    return JSONResponse(payload)


async def service_proposals(_: Request) -> JSONResponse:
    try:
        payload = await run_in_threadpool(_service_proposals_payload)
    except Exception:
        return _unexpected_json_error("service_proposals")
    return JSONResponse(payload, headers={"Cache-Control": "public, max-age=300"})


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


async def find_services_route(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return _json_error("invalid JSON body", status_code=400, error_code="invalid_request")
    if not isinstance(body, dict) or not isinstance(body.get("query"), str) or not body["query"].strip():
        return _json_error("query is required", status_code=400, error_code="invalid_request")
    try:
        payload = await run_in_threadpool(
            find_services, body["query"], networks=body.get("networks"), balances=body.get("balances"),
            candidates=body.get("candidates"), limit=body.get("limit"), conn_factory=connect,
            seed=body.get("seed"), detail=body.get("detail"),
        )
    except Exception:
        return _unexpected_json_error("find_services")
    return JSONResponse(payload)


async def observations_route(request: Request) -> JSONResponse:
    """C5: one signed, on-chain-verified paid call reported by crowdcode-mcp."""
    raw = await request.body()
    if len(raw) > 8192:
        return _json_error("observation too large", status_code=413, error_code="invalid_observation")
    try:
        body = json.loads(raw)
    except Exception:
        return _json_error("invalid JSON body", status_code=400, error_code="invalid_observation")
    try:
        outcome = await run_in_threadpool(submit_observation, body, lambda: connect())
    except Exception:
        return _unexpected_json_error("observations")
    return JSONResponse(outcome.body, status_code=outcome.http_status)


async def purchase_failures_route(request: Request) -> JSONResponse:
    """C10: a paid call the seller broke before settlement, signed by the hosted gateway's receipt key."""
    from crowdcode.purchase_failures import MAX_BYTES, submit_failure

    raw = await request.body()
    if len(raw) > MAX_BYTES:
        return _json_error("failure record too large", status_code=413, error_code="invalid_failure")
    try:
        body = json.loads(raw)
    except Exception:
        return _json_error("invalid JSON body", status_code=400, error_code="invalid_failure")
    try:
        outcome = await run_in_threadpool(submit_failure, body, lambda: connect())
    except Exception:
        return _unexpected_json_error("purchase_failures")
    return JSONResponse(outcome.body, status_code=outcome.http_status)


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


def _sync_seed_owners_at_startup() -> None:
    """Best-effort seed sync (docs/SCORING.md §3.5): pin the root owners from
    CROWDCODE_SEED_OWNERS at trust 1.0. Failures must not stop the server —
    the cron run repeats the sync."""
    settings = get_settings()
    try:
        with connect() as conn:
            sync_seed_owners(conn, settings.seed_owners)
            conn.commit()
    except Exception as exc:
        print(f"seed owner sync failed: {exc}")


def create_app() -> Starlette:
    settings = get_settings()
    _sync_seed_owners_at_startup()
    # Agent-service names and descriptions, refreshed at every deploy as well
    # as nightly, so a fix reaches the listings without waiting for the cron.
    refresh_in_background()
    # Search by meaning: load the model and embed the directory before the
    # first search needs them; then describe listings never looked at
    # (the cron revisits the rest) and embed what that changed.
    warm_search()
    fill_in_background(then=warm_search)
    mcp_app = mcp.streamable_http_app()
    app = Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/ready", ready, methods=["GET"]),
            Route("/api/receipt-keys", receipt_keys, methods=["GET"]),
            Route("/api/project-ideas", project_ideas, methods=["GET"]),
            Route("/api/service-proposals", service_proposals, methods=["GET"]),
            Route("/api/cron/status", cron_status, methods=["GET"]),
            Route("/api/index/health", index_health, methods=["GET"]),
            Route("/api/index/resource", index_resource, methods=["GET"]),
            Route("/api/tree", capability_tree_route, methods=["GET"]),
            Route("/api/tree/{entry}", capability_tree_route, methods=["GET"]),
            Route("/api/services", all_services, methods=["GET"]),
            Route("/api/services/top", top_services, methods=["GET"]),
            Route("/api/services/find", find_services_route, methods=["POST"]),
            Route("/api/observations", observations_route, methods=["POST"]),
            Route("/api/purchase-failures", purchase_failures_route, methods=["POST"]),
            Route("/api/reliability", reliability_lookup, methods=["GET"]),
            Route("/api/services/{service_id}/reliability", service_reliability, methods=["GET"]),
            Route("/api/services/{service_id}", service_detail, methods=["GET"]),
            Route("/api/knowledge/evidence", knowledge_evidence, methods=["GET"]),
            Route("/api/internal/hosted-services/sync", hosted_services_sync, methods=["POST"]),
            Route("/api/internal/demand-miner/batch", demand_miner_batch, methods=["POST"]),
            Route("/api/internal/feed/redact", feed_redact, methods=["POST"]),
            Route("/api/internal/tree/publish", tree_publish, methods=["POST"]),
            Route("/api/internal/tree/candidate", tree_candidate, methods=["POST"]),
            Route("/api/internal/tree/propose", tree_propose, methods=["POST"]),
            Route("/api/internal/eval/judge", eval_judge, methods=["POST"]),
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
