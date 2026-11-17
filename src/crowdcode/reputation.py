"""Database glue for the canonical scoring algorithm (docs/SCORING.md v3.1).

The write path (review_service) and the nightly consistency sweep both go
through these helpers so trust and stored scores can never diverge from
scoring.compute_score.

Trust earned from the cutover on is held per **owner**, not per wallet. A
hosted owner runs many agents with many wallets, and a wallet costs nothing to
create, so `review_owners` holds that raw trust. Hosted mainnet wallets are no
longer automatically trusted: `CROWDCODE_SEED_OWNERS` names the root of trust,
and every other owner earns its weight from the accuracy of its judged reviews.

`wallet_users` keeps the wallet trust that was already earned before the
cutover, and it is **frozen** there: `load_legacy_trust` reads it, nothing
writes it. That is what lets pre-cutover scores stand unchanged without
reopening the wallet-trust loop — a wallet cannot earn, lose, or be granted
trust any more, so the frozen snapshot can only ever weigh the finite set of
reviews that already cited it.
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


def load_legacy_trust(
    conn: psycopg.Connection, wallets: set[str] | None = None
) -> dict[str, TrustRow]:
    """The frozen pre-cutover wallet trust, keyed by wallet address.

    Read-only by construction: no code path writes `wallet_users.raw_trust` or
    `is_seed` any more, so this snapshot is fixed and can only weigh reviews
    that already existed when it was taken.
    """
    if wallets is not None and not wallets:
        return {}
    sql = "select wallet_address, raw_trust, is_seed, slashed_at from wallet_users"
    params: tuple[Any, ...] = ()
    if wallets is not None:
        sql += " where wallet_address = any(%s)"
        params = (list(wallets),)
    return {
        row["wallet_address"]: TrustRow(
            raw_trust=float(row["raw_trust"]),
            is_seed=bool(row["is_seed"]),
            slashed=row["slashed_at"] is not None,
        )
        for row in conn.execute(sql, params).fetchall()
    }


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


def wallet_owners(conn: psycopg.Connection, wallets: Iterable[str] | None = None) -> dict[str, str]:
    """Hosted owners known for wallets: an agent wallet that bought through
    the gateway is named, with its owner, by its verified receipts. Keyed by
    lowercase address. This is how an on-chain vote from a hosted agent's
    wallet is recognized as its owner's (docs/SCORING.md §3.2)."""
    sql = """
        select lower(reviewer_wallet) as wallet, min(owner_ref) as owner_ref
        from reviews
        where verified_source = 'hosted_receipt' and owner_ref is not null
          and reviewer_wallet is not null
    """
    params: tuple[Any, ...] = ()
    if wallets is not None:
        wanted = sorted({w.lower() for w in wallets if w})
        if not wanted:
            return {}
        sql += " and lower(reviewer_wallet) = any(%s)"
        params = (wanted,)
    sql += " group by 1"
    return {row["wallet"]: row["owner_ref"] for row in conn.execute(sql, params).fetchall()}


def payee_owner(conn: psycopg.Connection, payee: str | None) -> str | None:
    """The hosted owner a payee belongs to, when CrowdCode knows it: named as
    payee_owner_ref by a gateway receipt, or the payee is itself an agent
    wallet that bought through the gateway."""
    if not payee:
        return None
    row = conn.execute(
        """
        select payee_owner_ref as owner_ref from reviews
        where payee_owner_ref is not null and lower(payment_target_ref) = lower(%s)
        limit 1
        """,
        (payee,),
    ).fetchone()
    if row is not None:
        return row["owner_ref"]
    return wallet_owners(conn, [payee]).get(payee.lower())


def onchain_weight_exclusion(
    conn: psycopg.Connection,
    *,
    wallet: str,
    payee: str | None,
    amount: int | None,
    min_amount_atomic: int,
) -> str | None:
    """Why a paid review verified on chain carries no weight, or None
    (docs/SCORING.md §3.2). The payer must not be the payee, nor a wallet of
    the hosted account that owns the payee, and must have paid at least
    `min_amount_atomic`. One vote per wallet is applied by the score itself."""
    if amount is None or amount < min_amount_atomic:
        return "dust_payment"
    if not payee:
        return "community"
    if wallet.lower() == payee.lower():
        return "self_dealing"
    payer = wallet_owners(conn, [wallet]).get(wallet.lower())
    if payer is not None and payer == payee_owner(conn, payee):
        return "self_dealing"
    return None


def load_wallet_trust(conn: psycopg.Connection, wallets: set[str]) -> dict[str, TrustRow]:
    """Frozen wallet trust (wallet_users) keyed by lowercase address, for the
    on-chain votes it can only remove weight from."""
    if not wallets:
        return {}
    rows = conn.execute(
        """select lower(wallet_address) as wallet, raw_trust, is_seed, slashed_at
           from wallet_users where lower(wallet_address) = any(%s)""",
        (sorted({w.lower() for w in wallets}),),
    ).fetchall()
    return {
        row["wallet"]: TrustRow(
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
               payment_verification_level, created_at, owner_ref,
               weight_exclusion, weight_basis
        from reviews
        where service_id = %s
        """,
        (service_id,),
    ).fetchall()
    owners = wallet_owners(
        conn, {r["reviewer_wallet"] for r in rows if r.get("weight_basis") == "onchain" and r["reviewer_wallet"]}
    )
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
            weight_basis=row.get("weight_basis") or "none",
            payer_owner_ref=owners.get((row["reviewer_wallet"] or "").lower()),
        )
        for row in rows
    ]


# Signed failed purchases that count against a service (crowdcode.reliability
# SELLER_FAILURES, kept in step): each is a 1-star observation (v4).
FAILURE_DETAILS = ("seller_error", "seller_rejected", "no_receipt", "authorization_expired")


def load_failures(conn: psycopg.Connection, service_ids: list[str] | None = None) -> dict[str, list[datetime]]:
    """Times of signed failed purchases, by service. Empty when the services
    index is not installed (an older schema)."""
    sql = """
        select r.service_id, o.at
        from index_observations o
        join index_resources r on r.id = o.resource_id
        where o.kind = 'purchase' and not o.ok and o.detail = any(%s)
          and r.service_id is not null
    """
    params: list[Any] = [list(FAILURE_DETAILS)]
    if service_ids is not None:
        sql += " and r.service_id = any(%s)"
        params.append(service_ids)
    out: dict[str, list[datetime]] = {}
    installed = conn.execute(
        "select to_regclass('index_observations') is not null and to_regclass('index_resources') is not null as ok"
    ).fetchone()
    if not installed or not installed.get("ok"):
        return out
    for row in conn.execute(sql, params).fetchall():
        out.setdefault(row["service_id"], []).append(row["at"])
    return out


def _score_inputs(conn: psycopg.Connection, reviews: list[ReviewRow], extra_owners: set[str] = set()):
    owners = {r.owner_ref for r in reviews if r.weighted and r.owner_ref}
    owners |= {r.payer_owner_ref for r in reviews if r.onchain and r.payer_owner_ref}
    trust_map = load_trust_map(conn, owners | extra_owners)
    legacy_trust = load_legacy_trust(conn, {r.wallet for r in reviews if r.legacy and r.wallet})
    wallet_trust = load_wallet_trust(conn, {r.wallet for r in reviews if r.onchain and r.wallet})
    return trust_map, legacy_trust, wallet_trust


def store_score(conn: psycopg.Connection, service_id: str, result: ScoreResult, now: datetime) -> None:
    conn.execute(
        """
        update services
        set score = %s, score_mean = %s, n_eff = %s, n_eff_legacy = %s,
            n_eff_hosted = %s, n_eff_onchain = %s, n_eff_failures = %s,
            p_above_3 = %s, avg_rating_weighted = %s, score_alpha = %s,
            score_beta = %s, score_updated_at = %s
        where id = %s
        """,
        (result.score, result.mean, result.n_eff, result.n_eff_legacy,
         result.n_eff_hosted, result.n_eff_onchain, result.n_eff_failures,
         result.p_above_3, result.avg_rating_weighted, result.alpha, result.beta,
         now, service_id),
    )


def recompute_service_score(
    conn: psycopg.Connection, service_id: str, now: datetime
) -> ScoreResult:
    """Recompute and store the canonical (score, n_eff) for one service."""
    reviews = load_service_reviews(conn, service_id)
    trust_map, legacy_trust, wallet_trust = _score_inputs(conn, reviews)
    result = compute_score(reviews, trust_map, now, legacy_trust=legacy_trust,
                           wallet_trust=wallet_trust,
                           failures=load_failures(conn, [service_id]).get(service_id, []))
    store_score(conn, service_id, result, now)
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
    trust_map, legacy_trust, wallet_trust = _score_inputs(conn, reviews, {owner_ref})
    loo = compute_trust_consensus(
        reviews, trust_map, now, exclude_owner=owner_ref, legacy_trust=legacy_trust,
        wallet_trust=wallet_trust,
    )
    new_raw = updated_raw_trust(float(owner["raw_trust"]), loo.mean, rating)
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
