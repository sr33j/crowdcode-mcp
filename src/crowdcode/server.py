from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx
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
    canonical_review_payload,
    canonical_review_payload_from_hash,
    utc_now,
    verify_review_payment,
)
from crowdcode.redaction import (
    RedactionUnavailable,
    redact_texts,
    redaction_enabled,
)
from crowdcode.scoring import as_float
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
_REQUESTS_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _json_ready(row: dict[str, Any]) -> dict[str, Any]:
    clean = dict(row)
    created_at = clean.get("created_at")
    if created_at is not None:
        clean["created_at"] = created_at.isoformat()
    return clean


@mcp.tool()
def request_service(
    service_description: str,
    task_context: str | None = None,
) -> dict[str, Any]:
    """Capture an unmet, reusable service request for future directory coverage.

    Use this only when no fitting paid or external service exists. The request
    should describe a specific service capability with clear inputs and expected
    outputs or state changes, while staying general enough to apply to multiple
    users. For example: "Accepts a GitHub repository URL and failing CI logs,
    then opens a pull request with the focused fix." Avoid vague wishes,
    one-off local tasks, or descriptions tied to private user details.
    """
    service_description = service_description.strip()
    task_context = task_context.strip() if task_context else None

    if not service_description:
        return {"accepted": False, "reason": "service_description is required"}

    # Ingest enforcement (fail-closed): free text is redacted before storage
    # so raw PII/secrets from clients that bypass crowdcode-mcp never land
    # in the shared database.
    try:
        redacted = redact_texts([service_description, task_context], fail_closed=True)
    except RedactionUnavailable:
        return {
            "accepted": False,
            "reason": "redaction service unavailable; retry shortly",
        }
    if redacted is not None:
        service_description = redacted[0] or service_description
        task_context = redacted[1]

    with connect() as conn:
        row = conn.execute(
            """
            insert into service_requests (service_description, task_context, redacted_at)
            values (%s, %s, %s)
            returning id, directory_match
            """,
            (
                service_description,
                task_context,
                utc_now() if redaction_enabled() else None,
            ),
        ).fetchone()
        conn.commit()

    return {
        "accepted": True,
        "request_id": row["id"],
        "directory_match": row["directory_match"],
    }


@mcp.tool()
def get_service_score(
    service_id: str | None = None,
    api_endpoint: str | None = None,
    payment_provider: str | None = None,
    payment_target_ref: str | None = None,
    directory_slug: str | None = None,
) -> dict[str, Any]:
    """Return the simple average rating for a service."""
    try:
        identity = build_identity(
            service_id=service_id,
            api_endpoint=api_endpoint,
            payment_provider=payment_provider,
            payment_target_ref=payment_target_ref,
            directory_slug=directory_slug,
        )
    except ValueError as exc:
        return {
            "service_id": service_id,
            "found": False,
            "avg_rating": None,
            "num_reviews": 0,
            "recent_reviews": [],
            "reason": str(exc),
        }

    with connect() as conn:
        resolved = resolve_service(conn, identity)
        if resolved.error:
            return {
                "service_id": identity.service_id,
                "found": False,
                "avg_rating": None,
                "num_reviews": 0,
                "recent_reviews": [],
                "reason": resolved.error,
            }
        service = resolved.row
        if service is None:
            return {
                "service_id": identity.service_id,
                "found": False,
                "avg_rating": None,
                "num_reviews": 0,
                "recent_reviews": [],
                "reason": "service not found",
            }

        score = conn.execute(
            """
            select avg(rating) as avg_rating, count(*)::int as num_reviews
            from reviews
            where service_id = %s
            """,
            (service["id"],),
        ).fetchone()
        recent_reviews = conn.execute(
            """
            select rating, reason, task_context, created_at
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

    num_reviews = score["num_reviews"]
    return {
        "service_id": service["id"],
        "service_name": service["name"],
        "canonical_endpoint": service.get("canonical_endpoint"),
        "payment_provider": service.get("payment_provider"),
        "payment_target_ref": service.get("payment_target_ref"),
        "directory_slug": service.get("directory_slug"),
        "found": True,
        "avg_rating": as_float(score["avg_rating"]),
        "num_reviews": num_reviews,
        "recent_reviews": [_json_ready(row) for row in recent_reviews],
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
    """Create a review after a v1 payment-reference check."""
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
            return failure

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
            row = conn.execute(
                """
                insert into reviews (
                  service_id, rating, reason, payment_reference, task_context,
                  reviewer_id, payment_provider, payment_target_ref,
                  payment_proof, payment_verified, payment_verified_at,
                  reviewer_wallet, review_signature, signature_scheme,
                  signature_verified, redacted_at
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                ),
            ).fetchone()
            conn.commit()
        except psycopg.errors.UniqueViolation:
            conn.rollback()
            return {"accepted": False, "reason": "payment_reference already used"}

    return {
        "accepted": True,
        "reason": "review accepted",
        "service_id": service["id"],
        "service_created": service_created,
        "review_id": row["id"],
        "verification": verification.reason,
        "payment_verified": verification.payment_verified,
        "signature_verified": verification.signature_verified,
    }


def _json_error(message: str, status_code: int = 500) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status_code)


def _validate_requests_table(table_name: str) -> str:
    if not _REQUESTS_TABLE_RE.match(table_name):
        raise ValueError("CROWDCODE_REQUESTS_TABLE must be a simple table name")
    return table_name


def _table_columns(conn: psycopg.Connection, table_name: str) -> set[str]:
    rows = conn.execute(
        """
        select column_name
        from information_schema.columns
        where table_schema = 'public'
          and table_name = %s
        """,
        (table_name,),
    ).fetchall()
    return {row["column_name"] for row in rows}


def _fetch_recent_requests(limit: int = 100) -> list[dict[str, Any]]:
    settings = get_settings()
    table_name = _validate_requests_table(settings.requests_table)

    with connect() as conn:
        columns = _table_columns(conn, table_name)
        if not columns:
            raise RuntimeError(f"request table not found: {table_name}")

        text_columns = [
            column
            for column in (
                "service_description",
                "description",
                "request",
                "prompt",
                "task_context",
                "title",
            )
            if column in columns
        ]
        if not text_columns:
            raise RuntimeError(f"request table has no supported text columns: {table_name}")

        selected = ["id"] if "id" in columns else []
        selected += ["created_at"] if "created_at" in columns else []
        selected += ["directory_match"] if "directory_match" in columns else []
        selected += text_columns
        order_clause = "created_at desc" if "created_at" in columns else "1 desc"
        sql = f"""
            select {", ".join(selected)}
            from {table_name}
            order by {order_clause}
            limit %s
        """
        rows = conn.execute(sql, (limit,)).fetchall()

    requests: list[dict[str, Any]] = []
    for row in rows:
        parts = [str(row[column]).strip() for column in text_columns if row.get(column)]
        text = " ".join(parts)
        if not text:
            continue
        created_at = row.get("created_at")
        requests.append(
            {
                "id": row.get("id"),
                "created_at": created_at.isoformat() if created_at is not None else None,
                "directory_match": row.get("directory_match"),
                "text": text[:1500],
            }
        )

    # Egress backstop before this text reaches OpenRouter and the public
    # frontend: redact pre-enforcement rows; drop everything if the redactor
    # is configured but down (never leak on failure).
    redacted = redact_texts([item["text"] for item in requests], fail_closed=False)
    if redacted is None:
        return []
    for index, item in enumerate(requests):
        item["text"] = redacted[index] or ""
    return [item for item in requests if item["text"]]


def _fallback_project_ideas(requests: list[dict[str, Any]], reason: str) -> dict[str, Any]:
    ideas: dict[str, dict[str, Any]] = {}
    for item in requests:
        words = re.findall(r"[A-Za-z0-9]+", item["text"].lower())
        key_words = [word for word in words if len(word) > 3][:5]
        key = " ".join(key_words) or "general requests"
        title = " ".join(word.capitalize() for word in key_words[:4]) or "General Requests"
        idea = ideas.setdefault(
            key,
            {
                "title": title,
                "summary": item["text"][:260],
                "request_count": 0,
                "example_requests": [],
                "tags": key_words[:4],
            },
        )
        idea["request_count"] += 1
        if len(idea["example_requests"]) < 3:
            idea["example_requests"].append(item["text"][:220])

    ordered = sorted(
        ideas.values(),
        key=lambda idea: (-idea["request_count"], idea["title"]),
    )[:12]
    return {
        "ok": True,
        "source": "fallback",
        "fallback_reason": reason,
        "generated_at": int(time.time()),
        "source_request_count": len(requests),
        "ideas": ordered,
    }


def _extract_json_object(content: str) -> dict[str, Any]:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")
        if start < 0 or end < start:
            raise
        parsed = json.loads(content[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("OpenRouter response was not a JSON object")
    return parsed


def _summarize_project_ideas(requests: list[dict[str, Any]]) -> dict[str, Any]:
    settings = get_settings()
    if not requests:
        return {
            "ok": True,
            "source": "empty",
            "generated_at": int(time.time()),
            "source_request_count": 0,
            "ideas": [],
        }
    if not settings.openrouter_api_key:
        return _fallback_project_ideas(requests, "OPENROUTER_API_KEY is not set")

    compact_requests = [
        {
            "id": request["id"],
            "created_at": request["created_at"],
            "text": request["text"][:900],
        }
        for request in requests
    ]
    prompt = {
        "task": "Cluster the latest CrowdCode service requests into distinct project ideas.",
        "requirements": [
            "Return only a JSON object.",
            "Merge similar requests into one idea.",
            "Sort ideas by request_count descending, then practical usefulness.",
            "Use concise product-style titles.",
            "Include 2-5 short tags per idea.",
            "Do not invent details not grounded in the requests.",
        ],
        "schema": {
            "ideas": [
                {
                    "title": "string",
                    "summary": "string",
                    "request_count": "integer",
                    "example_requests": ["string"],
                    "tags": ["string"],
                }
            ]
        },
        "requests": compact_requests,
    }

    try:
        with httpx.Client(timeout=90) as client:
            response = client.post(
                f"{settings.openrouter_base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.openrouter_api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": settings.openrouter_site_url,
                    "X-Title": settings.openrouter_app_name,
                },
                json={
                    "model": settings.openrouter_model,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "You aggregate user request logs into concrete "
                                "software project ideas. Respond with valid JSON only."
                            ),
                        },
                        {"role": "user", "content": json.dumps(prompt)},
                    ],
                    "response_format": {"type": "json_object"},
                    "temperature": 0.2,
                },
            )
            response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        parsed = _extract_json_object(content)
    except Exception as exc:
        return _fallback_project_ideas(requests, f"OpenRouter summarization failed: {exc}")

    ideas = parsed.get("ideas", [])
    if not isinstance(ideas, list):
        return _fallback_project_ideas(requests, "OpenRouter JSON did not include ideas[]")

    clean_ideas: list[dict[str, Any]] = []
    for idea in ideas[:20]:
        if not isinstance(idea, dict):
            continue
        title = str(idea.get("title", "")).strip()
        summary = str(idea.get("summary", "")).strip()
        if not title or not summary:
            continue
        examples = idea.get("example_requests", [])
        tags = idea.get("tags", [])
        clean_ideas.append(
            {
                "title": title[:120],
                "summary": summary[:700],
                "request_count": int(idea.get("request_count") or 1),
                "example_requests": [
                    str(example).strip()[:260]
                    for example in examples
                    if str(example).strip()
                ][:3],
                "tags": [str(tag).strip()[:40] for tag in tags if str(tag).strip()][:5],
            }
        )

    return {
        "ok": True,
        "source": "openrouter",
        "model": settings.openrouter_model,
        "generated_at": int(time.time()),
        "source_request_count": len(requests),
        "ideas": clean_ideas,
    }


def _project_ideas_payload(refresh: bool = False) -> dict[str, Any]:
    settings = get_settings()
    now = time.time()
    if (
        not refresh
        and _PROJECT_IDEAS_CACHE["payload"] is not None
        and _PROJECT_IDEAS_CACHE["expires_at"] > now
    ):
        payload = dict(_PROJECT_IDEAS_CACHE["payload"])
        payload["cached"] = True
        return payload

    requests = _fetch_recent_requests(limit=100)
    payload = _summarize_project_ideas(requests)
    payload["cached"] = False
    _PROJECT_IDEAS_CACHE["payload"] = payload
    _PROJECT_IDEAS_CACHE["expires_at"] = now + settings.project_ideas_cache_seconds
    return payload


def _top_services_payload(limit: int = 10) -> dict[str, Any]:
    with connect() as conn:
        rows = conn.execute(
            """
            select
              s.id as service_id,
              s.name,
              s.directory_slug,
              s.canonical_endpoint,
              s.payment_provider,
              avg(r.rating)::float as avg_rating,
              count(r.id)::int as num_reviews,
              (
                (avg(r.rating) * count(r.id)) + (4.0 * 5)
              ) / (count(r.id) + 5) as rank_score
            from services s
            join reviews r on r.service_id = s.id
            group by s.id, s.name, s.directory_slug, s.canonical_endpoint,
                     s.payment_provider
            order by rank_score desc, num_reviews desc, avg_rating desc, s.name asc
            limit %s
            """,
            (limit,),
        ).fetchall()

    return {
        "ok": True,
        "services": [
            {
                "service_id": row["service_id"],
                "name": row["name"],
                "directory_slug": row.get("directory_slug"),
                "canonical_endpoint": row.get("canonical_endpoint"),
                "payment_provider": row.get("payment_provider"),
                "avg_rating": as_float(row["avg_rating"]),
                "num_reviews": row["num_reviews"],
                "rank_score": as_float(row["rank_score"]),
            }
            for row in rows
        ],
    }


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


def create_app() -> Starlette:
    settings = get_settings()
    mcp_app = mcp.streamable_http_app()
    app = Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/api/project-ideas", project_ideas, methods=["GET"]),
            Route("/api/services/top", top_services, methods=["GET"]),
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
