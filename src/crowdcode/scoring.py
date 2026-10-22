"""Canonical scoring & reputation math (docs/SCORING.md v3).

Every published score — the MCP get_service_score tool, /api/services,
/api/services/top, /api/services/{id} — comes from compute_score() here, either
directly or via the stored services.score/n_eff columns that the review write
path and the nightly consistency sweep refresh with it. There is deliberately
no second implementation of the formula (a SQL twin would drift).

All functions are pure: rows in, numbers out, no database imports.

v3 narrows what counts. Only a review backed by a hosted OpenCrowd gateway
receipt and a platform-judge score carries ranking weight (``ReviewRow.weight_
exclusion is None``); every other review — CLI wallets, crowdcode-mcp users,
legacy rows — is still stored, read and displayed, as community evidence with
weight exactly zero. Weight is held by the reviewing **owner**, not the wallet:
one owner runs many hosted agents with many wallets, so trust and the one-vote
cap are keyed on ``owner_ref``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping

ALGORITHM = "crowdcode-scoring-v3"

# Score prior (Bayesian shrinkage): kappa pseudo-reviews at the prior mean.
KAPPA = 2.0
MU0 = 3.0
# Reputation still uses a neutral consensus prior. An optimistic public prior
# must not let fresh wallets earn trust without evidence from trusted wallets.
TRUST_MU0 = 3.0

# Owner trust. raw trust lives in [TRUST_FLOOR, TRUST_CAP]; effective weight
# is 0 below THETA (the round-bad-actors-to-zero mechanism) and never exceeds
# TRUST_CAP. Seeds are pinned at 1.0. Trust is held per owner, not per wallet:
# an owner spinning up fresh hosted agents gains no influence from doing so.
ETA = 0.02
THETA = 0.1
TRUST_CAP = 1.0
TRUST_FLOOR = -5.0
P_CLAMP = (0.05, 0.95)

# Weighted reviews all weigh the same. In v2 the multiplier priced how much a
# payment claim proved (1x signed, 2x on-chain); in v3 a review either clears
# the hosted-receipt gate or carries no weight at all, so there is nothing left
# to price. Keeping the multiplier at 1.0 also makes n_eff readable: it is the
# number of trusted owner votes behind a score.
WEIGHTED_MULTIPLIER = 1.0
DECAY_HALF_LIFE_DAYS = 180.0

# Display threshold: below this effective evidence a resource is "unproven at
# the prior", never a starred rating.
UNPROVEN_N_EFF = 0.5


def as_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


@dataclass(frozen=True)
class ReviewRow:
    wallet: str | None
    rating: int
    payment_verified: bool
    signature_verified: bool
    created_at: datetime
    # None on rows predating the level column. 'response_attested' is the
    # hosted gateway receipt level; nothing else can be weighted.
    payment_verification_level: str | None = None
    # The hosted owner behind the review, from its verified attestation.
    owner_ref: str | None = None
    # None when the review carries ranking weight; otherwise why it does not
    # ('community', 'self_dealing', 'dust_payment', 'unsigned_placeholder').
    weight_exclusion: str | None = "community"

    @property
    def weighted(self) -> bool:
        return self.weight_exclusion is None and self.owner_ref is not None


@dataclass(frozen=True)
class TrustRow:
    raw_trust: float
    is_seed: bool = False
    slashed: bool = False


@dataclass(frozen=True)
class ScoreResult:
    score: float
    n_eff: float


@dataclass(frozen=True)
class OwnerReviewBucket:
    """One owner's single aggregate opinion of one service.

    v2 capped influence per wallet per UTC day; v3 caps it per owner outright —
    one vote per owner per service, however many agents, wallets, days or calls
    produced it. Every purchase remains stored and readable; the rating is the
    decay-weighted mean of that owner's weighted reviews and the evidence is
    capped at the strongest single one, so buying a service a hundred times
    sharpens the owner's one vote instead of multiplying it.
    """

    owner_ref: str
    rating: float
    evidence: float
    created_at: datetime


def effective_weight(trust: TrustRow | None) -> float:
    """Owner weight: seeds pinned to 1.0; slashed, unknown, and sub-theta
    owners contribute exactly zero (n small residual trusts must never
    aggregate into influence)."""
    if trust is None or trust.slashed:
        return 0.0
    if trust.is_seed:
        return TRUST_CAP
    if trust.raw_trust < THETA:
        return 0.0
    return min(TRUST_CAP, trust.raw_trust)


def decay_factor(created_at: datetime, now: datetime) -> float:
    age_days = (now - created_at).total_seconds() / 86400.0
    if age_days <= 0:
        return 1.0
    return 0.5 ** (age_days / DECAY_HALF_LIFE_DAYS)


def proof_multiplier(review: ReviewRow) -> float:
    """Weight multiplier for one review: constant for weighted ones, zero
    otherwise. Community reviews are evidence a reader can see, never
    evidence the ranking moves on."""
    return WEIGHTED_MULTIPLIER if review.weighted else 0.0


def review_weight(
    review: ReviewRow, trust: TrustRow | None, now: datetime
) -> float:
    if not review.weighted:
        return 0.0
    weight = effective_weight(trust)
    if weight == 0.0:
        return 0.0
    return weight * WEIGHTED_MULTIPLIER * decay_factor(review.created_at, now)


def aggregate_owner_reviews(
    reviews: Iterable[ReviewRow], now: datetime
) -> list[OwnerReviewBucket]:
    """Collapse every weighted review into one bucket per owner.

    The rating is the decay-weighted mean of that owner's weighted reviews and
    the evidence is capped at the strongest single one, so repeated purchases
    sharpen one vote rather than stacking several. Unweighted reviews are
    dropped here, which is why community evidence can never reach a score.
    """

    grouped: dict[str, list[tuple[ReviewRow, float]]] = {}
    for review in reviews:
        if not review.weighted:
            continue
        assert review.owner_ref is not None
        grouped.setdefault(review.owner_ref, []).append(
            (review, WEIGHTED_MULTIPLIER * decay_factor(review.created_at, now))
        )

    buckets: list[OwnerReviewBucket] = []
    for owner_ref, items in grouped.items():
        total = sum(evidence for _, evidence in items)
        if total <= 0:
            continue
        buckets.append(
            OwnerReviewBucket(
                owner_ref=owner_ref,
                rating=sum(review.rating * evidence for review, evidence in items)
                / total,
                evidence=max(evidence for _, evidence in items),
                created_at=max(review.created_at for review, _ in items),
            )
        )
    return sorted(buckets, key=lambda bucket: (bucket.created_at, bucket.owner_ref))


def compute_score(
    reviews: Iterable[ReviewRow],
    trust_map: Mapping[str, TrustRow],
    now: datetime,
    *,
    exclude_owner: str | None = None,
    prior_mean: float = MU0,
) -> ScoreResult:
    """score = (sum(w*r) + kappa*mu0) / (sum(w) + kappa); n_eff = sum(w).

    `trust_map` is keyed by owner_ref. exclude_owner omits one owner's own
    reviews; reputation callers use compute_trust_consensus(), which also
    selects the neutral internal prior.
    """
    num = KAPPA * prior_mean
    den = KAPPA
    n_eff = 0.0
    eligible = [
        review
        for review in reviews
        if review.weighted and (exclude_owner is None or review.owner_ref != exclude_owner)
    ]
    for bucket in aggregate_owner_reviews(eligible, now):
        weight = effective_weight(trust_map.get(bucket.owner_ref)) * bucket.evidence
        if weight == 0.0:
            continue
        num += weight * bucket.rating
        den += weight
        n_eff += weight
    return ScoreResult(score=num / den, n_eff=n_eff)


def compute_trust_consensus(
    reviews: Iterable[ReviewRow],
    trust_map: Mapping[str, TrustRow],
    now: datetime,
    *,
    exclude_owner: str,
) -> ScoreResult:
    """Leave-one-out evidence for reputation, retaining the neutral v1 prior."""
    return compute_score(
        reviews, trust_map, now,
        exclude_owner=exclude_owner, prior_mean=TRUST_MU0,
    )


def implied_p(loo_score: float) -> float:
    """Success probability implied by the leave-one-out consensus."""
    lo, hi = P_CLAMP
    return min(hi, max(lo, (loo_score - 1.0) / 4.0))


def trust_delta(loo_score: float, rating: float) -> float:
    """Proper scoring rule: trust moves by the information the review carried
    beyond the current consensus. Ratings of 3 make no prediction."""
    if 2 < rating < 4:
        return 0.0
    p = implied_p(loo_score)
    likelihood = p if rating >= 4 else 1.0 - p
    return ETA * math.log2(likelihood / 0.5)


def updated_raw_trust(raw: float, loo_score: float, rating: float) -> float:
    return min(TRUST_CAP, max(TRUST_FLOOR, raw + trust_delta(loo_score, rating)))


def is_unproven(n_eff: float) -> bool:
    return n_eff < UNPROVEN_N_EFF
