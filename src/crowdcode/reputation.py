"""Database glue for the canonical scoring algorithm (docs/SCORING.md v1).

The write path (review_service) and the nightly consistency sweep both go
through these helpers so trust and stored scores can never diverge from
scoring.compute_score.
"""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any, Iterable

import psycopg

from crowdcode.scoring import (
    ReviewRow,
    ScoreResult,
    TrustRow,
    compute_score,
    compute_trust_consensus,
    updated_raw_trust,
)


def ensure_user(conn: psycopg.Connection, wallet: str) -> dict[str, Any]:
    """Get-or-create the users row for a normalized wallet address."""
    # Only the operator-installed view can grant hosted trust. A client cannot
    # claim to be OpenCrowd in its review payload to promote an arbitrary wallet.
    if wallet in load_opencrowd_seed_wallets(conn):
        return conn.execute(
            """
            insert into wallet_users (wallet_address, is_seed, raw_trust, trust_updated_at)
            values (%s, true, 1.0, now())
            on conflict (wallet_address) do update
            set is_seed = true, raw_trust = 1.0, trust_updated_at = now()
            returning user_id, wallet_address, is_seed, raw_trust, slashed_at
            """,
            (wallet,),
        ).fetchone()
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
               payment_verification_level, created_at
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
            payment_verification_level=row.get("payment_verification_level"),
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
    loo = compute_trust_consensus(reviews, trust_map, now, exclude_wallet=wallet)
    new_raw = updated_raw_trust(float(user["raw_trust"]), loo.score, rating)
    if new_raw != float(user["raw_trust"]):
        conn.execute(
            "update wallet_users set raw_trust = %s, trust_updated_at = %s where user_id = %s",
            (new_raw, now, user["user_id"]),
        )
    return new_raw


def load_opencrowd_seed_wallets(conn: psycopg.Connection) -> set[str]:
    """Optional operator-owned bridge to the hosted agent wallet registry.

    Standalone CrowdCode installations have no bridge and retain explicit seeds.
    """
    bridge = conn.execute(
        "select to_regclass('opencrowd_seed_wallets') as relation"
    ).fetchone()
    if not bridge or bridge["relation"] is None:
        return set()
    return {
        row["wallet_address"]
        for row in conn.execute("select wallet_address from opencrowd_seed_wallets").fetchall()
    }


def sync_seed_wallets(conn: psycopg.Connection, wallets: Iterable[str]) -> None:
    """Pin explicit operator and registered hosted wallets at trust 1.0.

    Hosted seeds survive explicit-list reconciliation. An empty explicit list
    never demotes existing seeds. Slashed wallets remain excluded by scoring.
    """
    explicit = {w.strip().lower() for w in wallets if w and w.strip()}
    if any(re.fullmatch(r"0x[0-9a-f]{40}", wallet) is None for wallet in explicit):
        raise ValueError("CROWDCODE_SEED_WALLETS must contain only comma-separated EVM addresses")
    seeds = sorted(explicit | load_opencrowd_seed_wallets(conn))
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
    if explicit:
        conn.execute(
            "update wallet_users set is_seed = false where is_seed and wallet_address != all(%s)",
            (seeds,),
        )
