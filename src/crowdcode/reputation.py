"""Database glue for the canonical scoring algorithm (docs/SCORING.md v1).

The write path (review_service) and the nightly consistency sweep both go
through these helpers so trust and stored scores can never diverge from
scoring.compute_score.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

import psycopg

from crowdcode.scoring import (
    ReviewRow,
    ScoreResult,
    TrustRow,
    compute_score,
    updated_raw_trust,
)


def ensure_user(conn: psycopg.Connection, wallet: str) -> dict[str, Any]:
    """Get-or-create the users row for a normalized wallet address."""
    row = conn.execute(
        """
        insert into wallet_users (wallet_address)
        values (%s)
        on conflict (wallet_address) do nothing
        returning user_id, wallet_address, is_seed, raw_trust, slashed_at
        """,
        (wallet,),
    ).fetchone()
    if row is not None:
        return row
    return conn.execute(
        """
        select user_id, wallet_address, is_seed, raw_trust, slashed_at
        from wallet_users
        where wallet_address = %s
        """,
        (wallet,),
    ).fetchone()


def load_trust_map(
    conn: psycopg.Connection, wallets: set[str] | None = None
) -> dict[str, TrustRow]:
    if wallets is not None and not wallets:
        return {}
    sql = "select wallet_address, raw_trust, is_seed, slashed_at from wallet_users"
    params: tuple[Any, ...] = ()
    if wallets is not None:
        sql += " where wallet_address = any(%s)"
        params = (list(wallets),)
    rows = conn.execute(sql, params).fetchall()
    return {
        row["wallet_address"]: TrustRow(
            raw_trust=float(row["raw_trust"]),
            is_seed=bool(row["is_seed"]),
            slashed=row["slashed_at"] is not None,
        )
        for row in rows
    }


def load_service_reviews(
    conn: psycopg.Connection, service_id: str
) -> list[ReviewRow]:
    rows = conn.execute(
        """
        select reviewer_wallet, rating, payment_verified, signature_verified,
               created_at
        from reviews
        where service_id = %s
        """,
        (service_id,),
    ).fetchall()
    return [
        ReviewRow(
            wallet=row["reviewer_wallet"],
            rating=int(row["rating"]),
            payment_verified=bool(row["payment_verified"]),
            signature_verified=bool(row["signature_verified"]),
            created_at=row["created_at"],
        )
        for row in rows
    ]


def recompute_service_score(
    conn: psycopg.Connection, service_id: str, now: datetime
) -> ScoreResult:
    """Recompute and store the canonical (score, n_eff) for one service."""
    reviews = load_service_reviews(conn, service_id)
    trust_map = load_trust_map(
        conn, {r.wallet for r in reviews if r.wallet is not None}
    )
    result = compute_score(reviews, trust_map, now)
    conn.execute(
        """
        update services
        set score = %s, n_eff = %s, score_updated_at = %s
        where id = %s
        """,
        (result.score, result.n_eff, now, service_id),
    )
    return result


def apply_review_trust_update(
    conn: psycopg.Connection,
    wallet: str,
    service_id: str,
    rating: int,
    now: datetime,
) -> float | None:
    """Apply the proper-scoring-rule trust update for one incoming review.

    The update is computed against the leave-one-out consensus (this wallet's
    own reviews excluded). Seeds and slashed wallets never move. Returns the
    new raw trust, or None when no update applies.
    """
    user = conn.execute(
        """
        select user_id, raw_trust, is_seed, slashed_at
        from wallet_users
        where wallet_address = %s
        for update
        """,
        (wallet,),
    ).fetchone()
    if user is None or user["is_seed"] or user["slashed_at"] is not None:
        return None

    reviews = load_service_reviews(conn, service_id)
    trust_map = load_trust_map(
        conn, {r.wallet for r in reviews if r.wallet is not None} | {wallet}
    )
    loo = compute_score(reviews, trust_map, now, exclude_wallet=wallet)
    new_raw = updated_raw_trust(float(user["raw_trust"]), loo.score, rating)
    if new_raw != float(user["raw_trust"]):
        conn.execute(
            "update wallet_users set raw_trust = %s, trust_updated_at = %s where user_id = %s",
            (new_raw, now, user["user_id"]),
        )
    return new_raw


def sync_seed_wallets(conn: psycopg.Connection, wallets: Iterable[str]) -> None:
    """Make the users table match CROWDCODE_SEED_WALLETS exactly: listed
    wallets are seeds pinned at trust 1.0; previously-seeded wallets no longer
    listed are demoted (keeping their raw trust). A missing/empty setting is a
    no-op rather than a mass demotion — an unset env var must not unseat the
    trust anchors."""
    seeds = sorted({w.strip().lower() for w in wallets if w and w.strip()})
    if not seeds:
        return
    for wallet in seeds:
        conn.execute(
            """
            insert into wallet_users (wallet_address, is_seed, raw_trust, trust_updated_at)
            values (%s, true, 1.0, now())
            on conflict (wallet_address)
            do update set is_seed = true, raw_trust = 1.0, trust_updated_at = now()
            """,
            (wallet,),
        )
    conn.execute(
        "update wallet_users set is_seed = false where is_seed and wallet_address != all(%s)",
        (seeds,),
    )
