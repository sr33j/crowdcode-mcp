from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from crowdcode.settings import get_settings

WINDOW_SECONDS = 86400

AGENTCASH_INSTALL_COMMAND = (
    "claude mcp add --scope user agentcash -- npx -y agentcash@latest"
)


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    retry_after_seconds: int | None
    limit: dict[str, Any]
    # Actions left in the window AFTER the current one succeeds; None when the
    # limit is disabled.
    remaining: int | None


def identity_id_from_wallet(wallet: str) -> str:
    """Salted, non-reversible identity for a wallet address.

    Same derivation as review reviewer_id so a wallet maps to one identity
    across reviews and service requests.
    """
    settings = get_settings()
    material = f"{settings.reviewer_salt}:wallet:{wallet.lower()}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def check_request_limit(
    conn: Any,
    requester_id: str,
    max_per_day: int,
    now: datetime,
) -> RateLimitResult:
    return _check_window(
        conn,
        """
        select count(*)::int as n, min(created_at) as oldest
        from service_requests
        where requester_id = %s and created_at > %s
        """,
        (requester_id, now - timedelta(seconds=WINDOW_SECONDS)),
        scope="requester_daily",
        max_per_day=max_per_day,
        now=now,
    )


def rate_limit_payload(result: RateLimitResult, human_reason: str, retry_tool: str) -> dict[str, Any]:
    retry_after = result.retry_after_seconds
    return {
        "accepted": False,
        "reason": f"rate limit exceeded: {human_reason}",
        "rate_limited": True,
        "retry_after_seconds": retry_after,
        "limit": result.limit,
        "next_step": {
            "action": "wait_and_retry",
            "summary": (
                f"The limit resets on a rolling 24h window; retry {retry_tool} "
                f"in about {retry_after} seconds."
            ),
            "command": None,
            "link": None,
            "retry": {"tool": retry_tool, "after_seconds": retry_after, "with": {}},
        },
    }


def _check_window(
    conn: Any,
    sql: str,
    params: tuple[Any, ...],
    *,
    scope: str,
    max_per_day: int,
    now: datetime,
) -> RateLimitResult:
    limit = {"scope": scope, "max": max_per_day, "window_seconds": WINDOW_SECONDS}
    if max_per_day <= 0:
        return RateLimitResult(True, None, limit, remaining=None)

    row = conn.execute(sql, params).fetchone()
    count = int(row["n"] or 0)
    if count < max_per_day:
        return RateLimitResult(True, None, limit, remaining=max_per_day - count - 1)

    oldest = row.get("oldest")
    if oldest is None:
        retry_after = WINDOW_SECONDS
    else:
        retry_after = max(
            1,
            math.ceil(
                (oldest + timedelta(seconds=WINDOW_SECONDS) - now).total_seconds()
            ),
        )
    return RateLimitResult(False, retry_after, limit, remaining=0)
