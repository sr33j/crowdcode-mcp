from __future__ import annotations

import json
import threading
import time
from typing import Any

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
    resolve_service,
)
from crowdcode.payments import (
    REASON_HASH_RE,
    _normalize_evm_address,
    canonical_review_payload,
    canonical_review_payload_from_hash,
    utc_now,
    verify_review_payment,
)
from crowdcode.rate_limit import (
    AGENTCASH_INSTALL_COMMAND,
    check_request_limit,
    check_review_limit,
    identity_id_from_wallet,
    rate_limit_payload,
)
from crowdcode.redaction import (
    RedactionUnavailable,
    redact_texts,
    redaction_enabled,
)
from crowdcode.reputation import (
    apply_review_trust_update,
    ensure_user,
    recompute_service_score,
    sync_seed_wallets,
)
from crowdcode.scoring import (
    ALGORITHM as SCORE_ALGORITHM,
    as_float,
    is_unproven,
)
from crowdcode.summaries import (
    fetch_recent_requests,
    summarize_project_ideas,
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

_PROJECT_IDEAS_CACHE: dict[str, Any] = {
    "expires_at": 0.0,
    "payload": None,
}
_PROJECT_IDEAS_REFRESH_LOCK = threading.Lock()


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


@mcp.tool()
def request_service(
    service_description: str,
    task_context: str | None = None,
    requester_wallet: str | None = None,
) -> dict[str, Any]:
    """Record unmet paid-service demand for future directory coverage.

    Use this only when you were actively trying to BUY a capability — you had
    the task, a wallet, and spend authority, and would have paid concrete
    money right then if the service existed — and no fitting paid service
    (x402/mppx/Stripe) could be found. "A provider could charge for this" is
    not enough; a free tool that would merely have been convenient is not a
    service request. Describe the paid API call you wanted to make: the input
    you would have sent, the output or state change you were paying for, and
    roughly what a call was worth to the task, phrased generally enough to
    serve multiple users.

    Requires requester_wallet (an EVM 0x address identifying who is asking;
    crowdcode-mcp attaches your local wallet automatically). Requests are
    rate-limited per wallet identity per day.

    Good: "Accepts a GitHub repository URL and failing CI logs, then opens a
    pull request with the focused fix — worth ~$1-5 per fix." / "Resolves a
    citation like 'Smith et al. 2019' to the actual paper, or reports that it
    does not exist — worth ~$0.10 per lookup." / "Semantic search over
    paywalled full-text academic PDFs with page-level citations — worth
    ~$0.25 per query."

    Bad: free tools you would only use if they cost nothing, wishes about
    your own runtime or agent harness ("cleaner context", "more memory",
    local compute/IDE features), one-off local tasks ("fix my CI"), or
    descriptions tied to private user details.
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
                "install agentcash (or set X402_PRIVATE_KEY), then retry request_service.",
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
def get_service_score(
    service_id: str | None = None,
    api_endpoint: str | None = None,
    payment_provider: str | None = None,
    payment_target_ref: str | None = None,
    directory_slug: str | None = None,
) -> dict[str, Any]:
    """Return the simple average rating for a service.

    Check this before paying for, provisioning, or calling any paid agent
    service — especially x402 and mppx/MPP services. Identify the service by
    service_id, api_endpoint, payment target, or directory_slug.
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

        score = conn.execute(
            """
            select
              avg(rating) as avg_rating,
              count(*)::int as num_reviews,
              count(*) filter (where payment_verified)::int as num_verified_reviews,
              avg(rating) filter (where payment_verified) as verified_avg_rating
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
            select rating, reason, task_context, payment_verified, created_at
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
        "found": True,
        "score": canonical_score,
        "n_eff": n_eff,
        "unproven": is_unproven(n_eff or 0.0),
        "score_algorithm": SCORE_ALGORITHM,
        "avg_rating": as_float(score["avg_rating"]),
        "num_reviews": score["num_reviews"],
        "num_verified_reviews": score["num_verified_reviews"],
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
    return {
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
def get_review_signing_payload(
    rating: int,
    reason_hash: str,
    payment_reference: str,
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
    if not REASON_HASH_RE.match(reason_hash):
        return {
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
        identity = build_identity(
            service_id=service["id"],
            service_name=service["name"],
            api_endpoint=identity.api_endpoint or service.get("canonical_endpoint"),
            payment_provider=identity.payment_provider or service.get("payment_provider"),
            payment_target_ref=identity.payment_target_ref or service.get("payment_target_ref"),
            directory_slug=identity.directory_slug or service.get("directory_slug"),
        )

    return {
        "ok": True,
        "signature_scheme": "eip191",
        "message": canonical_review_payload_from_hash(
            identity=identity,
            rating=rating,
            reason_hash=reason_hash,
            payment_reference=payment_reference,
        ),
    }


@mcp.tool()
def review_service(
    rating: int,
    reason: str,
    payment_reference: str,
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
    """Create a review after paying for a service (x402/mppx/Stripe/manual).

    Call this after EVERY paid x402/mppx use — success, slow response, or
    failure. A bad outcome is not a reason to skip the review; it IS the
    review: rate 1-2 with the failure in the reason.

    Rate against the ORIGINAL task you were solving: was the response
    relevant and did it actually help answer that question? Rating scale:
    5 = excellent (clear schema, relevant output that answered the original
    question, fast, clean receipt — would reuse confidently); 4 = helped but
    a real schema/docs/latency/output caveat; 3 = paid but
    thin/confusing/needed guesswork, or not actually relevant/helpful for
    the task — a technically valid answer that did not help is a 3 at best;
    2 = paid but poor (client error, unclear failure, hard to use);
    1 = paid and broken (server error, unusable output, misleading challenge,
    timeout). A service that simply worked well AND helped is a 5 — do not
    hedge to 4 without a concrete caveat.

    For mppx/x402, take the identity and
    proofs from the ACTUAL payment, not a directory listing:
    - payment_reference: the settlement tx hash (x402) or Payment-Receipt
      `reference` (mppx). Unique — one review per payment.
    - payment_proof: the base64 response header STRING — `payment-response` for
      x402, `Payment-Receipt` for mppx. Not the tx hash, not decoded JSON.
      Strongly recommended but OPTIONAL: without it the review is stored as
      unverified and carries half weight in scoring.
    - payment_target_ref: the real payee (the 402 challenge recipient / on-chain
      Transfer `to`), not a bazaar/directory advertised payTo.
    - reviewer_wallet: the wallet that SENT the payment (the ERC-20 Transfer
      `from`). For gasless x402/mppx the tx sender is a facilitator, not the
      payer.
    """
    reason = reason.strip()
    payment_reference = payment_reference.strip()
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

        existing = conn.execute(
            "select id from reviews where payment_reference = %s",
            (payment_reference,),
        ).fetchone()
        if existing is not None:
            return {"accepted": False, "reason": "payment_reference already used"}

        effective_identity = identity
        if service is not None:
            effective_identity = build_identity(
                service_id=service["id"],
                service_name=service["name"],
                api_endpoint=identity.api_endpoint or service.get("canonical_endpoint"),
                payment_provider=identity.payment_provider or service.get("payment_provider"),
                payment_target_ref=identity.payment_target_ref or service.get("payment_target_ref"),
                directory_slug=identity.directory_slug or service.get("directory_slug"),
            )

        verification = verify_review_payment(
            identity=effective_identity,
            rating=rating,
            reason=reason,
            payment_reference=payment_reference,
            payment_proof=payment_proof,
            payment_challenge=payment_challenge,
            reviewer_wallet=reviewer_wallet,
            review_signature=review_signature,
            signature_scheme=signature_scheme,
        )
        if not verification.ok:
            failure: dict[str, Any] = {
                "accepted": False,
                "reason": verification.reason,
            }
            if verification.missing_wallet:
                failure["next_step"] = _next_step(
                    "install_wallet",
                    "mppx/x402 reviews need a signing wallet. crowdcode-mcp >= 0.2.0 "
                    "signs automatically with your local agentcash wallet or "
                    "X402_PRIVATE_KEY — install agentcash, then retry review_service.",
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

        settings = get_settings()
        if (
            service is not None
            and verification.reviewer_id
            and settings.review_rate_limit_per_day > 0
        ):
            limit_check = check_review_limit(
                conn,
                verification.reviewer_id,
                service["id"],
                settings.review_rate_limit_per_day,
                utc_now(),
            )
            if not limit_check.allowed:
                return rate_limit_payload(
                    limit_check,
                    f"{settings.review_rate_limit_per_day} review(s) per service "
                    "per reviewer per 24 hours",
                    retry_tool="review_service",
                )

        # Ingest enforcement (fail-closed). Runs AFTER signature verification:
        # the signature covers the hash of the reason exactly as received
        # (already redacted when sent via crowdcode-mcp — this is a no-op
        # then); storage gets the re-redacted text either way.
        try:
            redacted = redact_texts([reason, task_context], fail_closed=True)
        except RedactionUnavailable:
            return {
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
            created = create_service_from_identity(conn, identity)
            if created.error:
                return {"accepted": False, "reason": created.error}
            service = created.row
            service_created = created.created

        try:
            # Write-path scoring (docs/SCORING.md §8.1): the review insert,
            # the reviewer's trust update, and the service's stored score all
            # land in one transaction.
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
                  signature_verified, redacted_at, user_id, amount
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                ),
            ).fetchone()

            now = utc_now()
            if verification.reviewer_wallet:
                apply_review_trust_update(
                    conn, verification.reviewer_wallet, service["id"], rating, now
                )
            recompute_service_score(conn, service["id"], now)
            conn.commit()
        except psycopg.errors.UniqueViolation:
            conn.rollback()
            return {"accepted": False, "reason": "payment_reference already used"}

    result = {
        "accepted": True,
        "reason": "review accepted",
        "service_id": service["id"],
        "service_created": service_created,
        "review_id": row["id"],
        "verification": verification.reason,
        "payment_verified": verification.payment_verified,
        "signature_verified": verification.signature_verified,
        "verified_purchase": verification.payment_verified,
    }
    if (
        not verification.payment_verified
        and effective_identity.payment_provider in {"mppx", "x402"}
    ):
        result["next_step"] = _next_step(
            "supply_payment_proof",
            "Review accepted as UNVERIFIED (half weight in scoring). Next time "
            "include payment_proof — the base64 payment-response header (x402) "
            "or Payment-Receipt header (mppx) — for verified-purchase status.",
        )
    return result


def _json_error(message: str, status_code: int = 500) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status_code)


def _refresh_project_ideas_cache() -> None:
    try:
        settings = get_settings()
        requests = fetch_recent_requests(limit=100)
        payload = summarize_project_ideas(requests)
        payload["cached"] = False
        _PROJECT_IDEAS_CACHE["payload"] = payload
        _PROJECT_IDEAS_CACHE["expires_at"] = (
            time.time() + settings.project_ideas_cache_seconds
        )
    except Exception:
        pass
    finally:
        _PROJECT_IDEAS_REFRESH_LOCK.release()


def _cron_project_ideas_payload(ttl_seconds: int) -> dict[str, Any] | None:
    """Read the requested-services summary the cron job wrote to app_cache.
    Unlike the in-process cache this survives free-tier cold starts."""
    try:
        with connect() as conn:
            row = conn.execute(
                """
                select payload, generated_at
                from app_cache
                where key = 'project_ideas'
                  and generated_at > now() - make_interval(secs => %s)
                """,
                (ttl_seconds,),
            ).fetchone()
    except Exception:
        return None
    if row is None or not isinstance(row["payload"], dict):
        return None
    payload = dict(row["payload"])
    payload["source"] = "cron"
    return payload


def _project_ideas_payload(refresh: bool = False) -> dict[str, Any]:
    settings = get_settings()
    now = time.time()
    cached = _PROJECT_IDEAS_CACHE["payload"]

    if not refresh and cached is not None:
        # Serve the cache immediately; if it has expired, rebuild it in the
        # background so no visitor ever waits on summarization.
        if _PROJECT_IDEAS_CACHE["expires_at"] <= now and _PROJECT_IDEAS_REFRESH_LOCK.acquire(
            blocking=False
        ):
            threading.Thread(target=_refresh_project_ideas_cache, daemon=True).start()
        payload = dict(cached)
        payload["cached"] = True
        return payload

    if not refresh:
        payload = _cron_project_ideas_payload(settings.project_ideas_cache_seconds)
        if payload is not None:
            payload["cached"] = False
            _PROJECT_IDEAS_CACHE["payload"] = payload
            _PROJECT_IDEAS_CACHE["expires_at"] = (
                now + settings.project_ideas_cache_seconds
            )
            return payload

    requests = fetch_recent_requests(limit=100)
    payload = summarize_project_ideas(requests)
    payload["cached"] = False
    _PROJECT_IDEAS_CACHE["payload"] = payload
    _PROJECT_IDEAS_CACHE["expires_at"] = now + settings.project_ideas_cache_seconds
    return payload


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
          count(r.id) filter (where r.payment_verified)::int as num_verified_reviews
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
            return {"ok": False, "error": "service not found"}, 404

        counts = conn.execute(
            """
            select
              count(*)::int as num_reviews,
              count(*) filter (where payment_verified)::int as num_verified_reviews
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
            select rating, reason, task_context, payment_verified, created_at
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
        "histogram": histogram,
        "summary": _redacted_summary(service.get("review_summary")),
        "recent_reviews": [_json_ready(row) for row in recent_reviews],
    }, 200


async def health(_: Request) -> JSONResponse:
    try:
        with connect() as conn:
            conn.execute("select 1").fetchone()
    except Exception as exc:
        return _json_error(f"database unavailable: {exc}", status_code=503)
    return JSONResponse({"ok": True, "service": "crowdcode-backend"})


async def project_ideas(request: Request) -> JSONResponse:
    refresh = request.query_params.get("refresh") in {"1", "true", "yes"}
    try:
        payload = await run_in_threadpool(_project_ideas_payload, refresh)
    except Exception as exc:
        return _json_error(str(exc))
    return JSONResponse(payload)


async def top_services(_: Request) -> JSONResponse:
    try:
        payload = await run_in_threadpool(_top_services_payload, 10)
    except Exception as exc:
        return _json_error(str(exc))
    return JSONResponse(payload)


async def all_services(_: Request) -> JSONResponse:
    try:
        payload = await run_in_threadpool(_top_services_payload, None)
    except Exception as exc:
        return _json_error(str(exc))
    return JSONResponse(payload, headers={"Cache-Control": "public, max-age=60"})


async def service_detail(request: Request) -> JSONResponse:
    service_id = request.path_params["service_id"]
    try:
        payload, status_code = await run_in_threadpool(
            _service_detail_payload, service_id
        )
    except Exception as exc:
        return _json_error(str(exc))
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
            Route("/api/project-ideas", project_ideas, methods=["GET"]),
            Route("/api/services", all_services, methods=["GET"]),
            Route("/api/services/top", top_services, methods=["GET"]),
            Route("/api/services/{service_id}", service_detail, methods=["GET"]),
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
