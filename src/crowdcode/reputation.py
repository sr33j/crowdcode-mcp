"""Database glue for the canonical scoring algorithm (docs/SCORING.md v3).

The write path (review_service) and the nightly consistency sweep both go
through these helpers so trust and stored scores can never diverge from
scoring.compute_score.

Trust is held per **owner**, not per wallet. A hosted owner runs many agents
with many wallets, and a wallet costs nothing to create, so `review_owners`
holds the raw trust and `wallet_users` keeps only the reviewer identity it
always did. Hosted mainnet wallets are no longer automatically trusted:
`CROWDCODE_SEED_OWNERS` names the root of trust, and every other owner earns
its weight from the accuracy of its judged reviews.
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

OWNER_REF_RE = re.compile(r"^[0-9a-f]{32}$")


def ensure_user(conn: psycopg.Connection, wallet: str) -> dict[str, Any]:
    """Get-or-create the wallet_users row for a normalized wallet address.

    A wallet carries no ranking weight of its own any more, so no wallet is
    ever promoted here. Weight belongs to the owner named by a verified
    gateway attestation (`ensure_owner`).
    """
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


def ensure_owner(conn: psycopg.Connection, owner_ref: str) -> dict[str, Any]:
    """Get-or-create the review_owners row for a verified owner reference."""
    if not OWNER_REF_RE.match(owner_ref):
        raise ValueError("owner_ref must be a 32-character hex reference")
    row = conn.execute(
        """
        insert into review_owners (owner_ref)
        values (%s)
        on conflict (owner_ref) do nothing
        returning owner_ref, is_seed, raw_trust, slashed_at
        """,
        (owner_ref,),
    ).fetchone()
    if row is not None:
        return row
    return conn.execute(
        "select owner_ref, is_seed, raw_trust, slashed_at from review_owners where owner_ref = %s",
        (owner_ref,),
    ).fetchone()


def load_trust_map(
    conn: psycopg.Connection, owners: set[str] | None = None
) -> dict[str, TrustRow]:
    """Owner trust, keyed by owner_ref — the key compute_score() expects."""
    if owners is not None and not owners:
        return {}
    sql = "select owner_ref, raw_trust, is_seed, slashed_at from review_owners"
    params: tuple[Any, ...] = ()
    if owners is not None:
        sql += " where owner_ref = any(%s)"
        params = (list(owners),)
    rows = conn.execute(sql, params).fetchall()
    return {
        row["owner_ref"]: TrustRow(
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
               payment_verification_level, created_at, owner_ref, weight_exclusion
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
            owner_ref=row.get("owner_ref"),
            weight_exclusion=row.get("weight_exclusion"),
        )
        for row in rows
    ]


def recompute_service_score(
    conn: psycopg.Connection, service_id: str, now: datetime
) -> ScoreResult:
    """Recompute and store the canonical (score, n_eff) for one service."""
    reviews = load_service_reviews(conn, service_id)
    trust_map = load_trust_map(
        conn, {r.owner_ref for r in reviews if r.weighted and r.owner_ref}
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
    owner_ref: str,
    service_id: str,
    rating: int,
    now: datetime,
) -> float | None:
    """Apply the proper-scoring-rule trust update for one incoming review.

    The update is computed against the leave-one-out consensus (this owner's
    own reviews excluded). Seeds and slashed owners never move. Returns the
    new raw trust, or None when no update applies.
    """
    owner = conn.execute(
        """
        select owner_ref, raw_trust, is_seed, slashed_at
        from review_owners
        where owner_ref = %s
        for update
        """,
        (owner_ref,),
    ).fetchone()
    if owner is None or owner["is_seed"] or owner["slashed_at"] is not None:
        return None

    reviews = load_service_reviews(conn, service_id)
    trust_map = load_trust_map(
        conn, {r.owner_ref for r in reviews if r.weighted and r.owner_ref} | {owner_ref}
    )
    loo = compute_trust_consensus(reviews, trust_map, now, exclude_owner=owner_ref)
    new_raw = updated_raw_trust(float(owner["raw_trust"]), loo.score, rating)
    if new_raw != float(owner["raw_trust"]):
        conn.execute(
            "update review_owners set raw_trust = %s, trust_updated_at = %s where owner_ref = %s",
            (new_raw, now, owner_ref),
        )
    return new_raw


def sync_seed_owners(conn: psycopg.Connection, owners: Iterable[str]) -> None:
    """Pin the explicitly configured root owners at trust 1.0.

    Without at least one seed, nothing is ever weighted: a fresh owner's trust
    is 0, below theta, and the leave-one-out consensus of a service with no
    weighted evidence sits at the prior, where the scoring rule pays exactly
    zero. That is the EigenTrust seed-vector property, so the seed list is a
    deliberate operator decision, never derived from the hosted wallet
    registry. An empty list never demotes existing seeds.
    """
    seeds = sorted({owner.strip().lower() for owner in owners if owner and owner.strip()})
    if any(OWNER_REF_RE.match(owner) is None for owner in seeds):
        raise ValueError("CROWDCODE_SEED_OWNERS must contain only comma-separated 32-character hex owner references")
    if not seeds:
        return
    for owner in seeds:
        conn.execute(
            """
            insert into review_owners (owner_ref, is_seed, raw_trust, trust_updated_at)
            values (%s, true, 1.0, now())
            on conflict (owner_ref)
            do update set is_seed = true, raw_trust = 1.0, trust_updated_at = now()
            """,
            (owner,),
        )
    conn.execute(
        "update review_owners set is_seed = false where is_seed and owner_ref != all(%s)",
        (seeds,),
    )
