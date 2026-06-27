from __future__ import annotations

from typing import Any

import psycopg
from mcp.server.fastmcp import FastMCP
from psycopg.types.json import Jsonb

from crowdcode.db import connect
from crowdcode.payments import verify_payment_reference
from crowdcode.scoring import as_float, confidence_for_count
from crowdcode.settings import get_settings

mcp = FastMCP("CrowdCode")


def _json_ready(row: dict[str, Any]) -> dict[str, Any]:
    clean = dict(row)
    created_at = clean.get("created_at")
    if created_at is not None:
        clean["created_at"] = created_at.isoformat()
    return clean


@mcp.tool()
def get_service_score(service_id: str) -> dict[str, Any]:
    """Return the simple average rating for a service."""
    service_id = service_id.strip()

    with connect() as conn:
        service = conn.execute(
            "select id, name from services where id = %s",
            (service_id,),
        ).fetchone()
        if service is None:
            return {
                "service_id": service_id,
                "found": False,
                "avg_rating": None,
                "num_reviews": 0,
                "confidence": "low",
                "recent_reviews": [],
                "reason": "service not found",
            }

        score = conn.execute(
            """
            select avg(rating) as avg_rating, count(*)::int as num_reviews
            from reviews
            where service_id = %s
            """,
            (service_id,),
        ).fetchone()
        recent_reviews = conn.execute(
            """
            select rating, reason, task_context, created_at
            from reviews
            where service_id = %s
            order by created_at desc
            limit 5
            """,
            (service_id,),
        ).fetchall()

    num_reviews = score["num_reviews"]
    return {
        "service_id": service_id,
        "service_name": service["name"],
        "found": True,
        "avg_rating": as_float(score["avg_rating"]),
        "num_reviews": num_reviews,
        "confidence": confidence_for_count(num_reviews),
        "recent_reviews": [_json_ready(row) for row in recent_reviews],
    }


@mcp.tool()
def review_service(
    service_id: str,
    rating: int,
    reason: str,
    payment_reference: str,
    task_context: str | None = None,
    payment_protocol: str = "auto",
    payment_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a review after a v1 payment-reference check."""
    service_id = service_id.strip()
    reason = reason.strip()
    payment_reference = payment_reference.strip()
    task_context = task_context.strip() if task_context else None

    if rating < 1 or rating > 5:
        return {"accepted": False, "reason": "rating must be between 1 and 5"}
    if not reason:
        return {"accepted": False, "reason": "reason is required"}

    with connect() as conn:
        service = conn.execute(
            "select id, stripe_payee_ref from services where id = %s",
            (service_id,),
        ).fetchone()
        if service is None:
            return {"accepted": False, "reason": "service not found"}

        verification = verify_payment_reference(
            service_id=service_id,
            payment_reference=payment_reference,
            service_payment_ref=service["stripe_payee_ref"],
            payment_protocol=payment_protocol,
            payment_evidence=payment_evidence,
        )
        if not verification.ok:
            return {"accepted": False, "reason": verification.reason}

        existing = conn.execute(
            "select id from reviews where payment_reference = %s",
            (payment_reference,),
        ).fetchone()
        if existing is not None:
            return {"accepted": False, "reason": "payment_reference already used"}

        try:
            row = conn.execute(
                """
                insert into reviews (
                  service_id,
                  rating,
                  reason,
                  payment_reference,
                  task_context,
                  reviewer_id,
                  payment_protocol,
                  payment_rail,
                  payment_status,
                  payment_amount,
                  payment_currency,
                  payment_metadata
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                returning id
                """,
                (
                    service_id,
                    rating,
                    reason,
                    payment_reference,
                    task_context,
                    verification.reviewer_id,
                    verification.protocol,
                    verification.rail,
                    verification.payment_status,
                    verification.payment_amount,
                    verification.payment_currency,
                    Jsonb(verification.metadata or {}),
                ),
            ).fetchone()
            conn.commit()
        except psycopg.errors.UniqueViolation:
            conn.rollback()
            return {"accepted": False, "reason": "payment_reference already used"}

    return {
        "accepted": True,
        "reason": "review accepted",
        "review_id": row["id"],
        "verification": verification.reason,
        "payment_protocol": verification.protocol,
        "payment_rail": verification.rail,
    }


@mcp.tool()
def request_service(
    service_description: str,
    task_context: str | None = None,
) -> dict[str, Any]:
    """Capture unmet demand as a plain request row."""
    service_description = service_description.strip()
    task_context = task_context.strip() if task_context else None

    if not service_description:
        return {"accepted": False, "reason": "service_description is required"}

    with connect() as conn:
        row = conn.execute(
            """
            insert into service_requests (
              service_description, task_context, directory_match
            )
            values (%s, %s, 'missing')
            returning id, directory_match
            """,
            (service_description, task_context),
        ).fetchone()
        conn.commit()

    return {
        "accepted": True,
        "request_id": row["id"],
        "directory_match": row["directory_match"],
    }


@mcp.tool()
def list_service_requests(filter: str = "missing", limit: int = 20) -> list[dict[str, Any]]:
    """List recent service requests for the simple demand board."""
    filter = filter.strip() if filter else "missing"
    limit = max(1, min(limit, 100))

    with connect() as conn:
        if filter == "all":
            rows = conn.execute(
                """
                select id, service_description, task_context, directory_match, created_at
                from service_requests
                order by created_at desc
                limit %s
                """,
                (limit,),
            ).fetchall()
            return [_json_ready(row) for row in rows]

        rows = conn.execute(
            """
            select id, service_description, task_context, directory_match, created_at
            from service_requests
            where directory_match = %s
            order by created_at desc
            limit %s
            """,
            (filter, limit),
        ).fetchall()
        return [_json_ready(row) for row in rows]


def main() -> None:
    settings = get_settings()
    mcp.run(transport=settings.mcp_transport)


if __name__ == "__main__":
    main()
