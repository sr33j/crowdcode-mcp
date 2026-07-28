"""Canonical scoring & reputation math (docs/SCORING.md v1).

Every published score — the MCP get_service_score tool, /api/services,
/api/services/top, /api/services/{id} — comes from compute_score() here, either
directly or via the stored services.score/n_eff columns that the review write
path and the nightly consistency sweep refresh with it. There is deliberately
no second implementation of the formula (a SQL twin would drift).

All functions are pure: rows in, numbers out, no database imports.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping

ALGORITHM = "crowdcode-scoring-v1"

# Score prior (Bayesian shrinkage): kappa pseudo-reviews at the prior mean.
KAPPA = 2.0
MU0 = 3.0

# Reviewer trust. raw trust lives in [TRUST_FLOOR, TRUST_CAP]; effective
# weight is 0 below THETA (the round-bad-actors-to-zero mechanism) and never
# exceeds TRUST_CAP. Seeds are pinned at 1.0.
ETA = 0.02
THETA = 0.1
TRUST_CAP = 1.0
TRUST_FLOOR = -5.0
P_CLAMP = (0.05, 0.95)

# Review weight multipliers, keyed by payment_verification_level. Proof-based
# and tx-hash-only verification weigh the same on purpose: both prove exactly
# one fact (an on-chain transfer from reviewer wallet to service payee in the
# pinned token) — the proof header is unsigned client JSON and adds nothing.
# response_attested is reserved for future signed receipts; it inherits the
# verified weight until it actually proves more.
PROOF_VERIFIED = 2.0
PROOF_SIGNED = 1.0
LEVEL_MULTIPLIERS = {
    "unverified": PROOF_SIGNED,
    "signature_only": PROOF_SIGNED,
    "onchain_verified": PROOF_VERIFIED,
    "response_attested": PROOF_VERIFIED,
}
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
    # None on rows predating the level column; proof_multiplier falls back to
    # the payment_verified boolean for those.
    payment_verification_level: str | None = None


@dataclass(frozen=True)
class TrustRow:
    raw_trust: float
    is_seed: bool = False
    slashed: bool = False


@dataclass(frozen=True)
class ScoreResult:
    score: float
    n_eff: float


def effective_weight(trust: TrustRow | None) -> float:
    """Wallet weight: seeds pinned to 1.0; slashed, unknown, and sub-theta
    wallets contribute exactly zero (n small residual trusts must never
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
    level = review.payment_verification_level
    if level in LEVEL_MULTIPLIERS:
        return LEVEL_MULTIPLIERS[level]
    return PROOF_VERIFIED if review.payment_verified else PROOF_SIGNED


def review_weight(
    review: ReviewRow, trust: TrustRow | None, now: datetime
) -> float:
    weight = effective_weight(trust)
    if weight == 0.0:
        return 0.0
    return weight * proof_multiplier(review) * decay_factor(review.created_at, now)


def compute_score(
    reviews: Iterable[ReviewRow],
    trust_map: Mapping[str, TrustRow],
    now: datetime,
    *,
    exclude_wallet: str | None = None,
) -> ScoreResult:
    """score = (sum(w*r) + kappa*mu0) / (sum(w) + kappa); n_eff = sum(w).

    exclude_wallet computes the leave-one-out consensus used for trust
    updates: a wallet must never earn trust from agreement with a consensus
    its own reviews created.
    """
    num = KAPPA * MU0
    den = KAPPA
    n_eff = 0.0
    for review in reviews:
        if review.wallet is None:
            continue
        if exclude_wallet is not None and review.wallet == exclude_wallet:
            continue
        weight = review_weight(review, trust_map.get(review.wallet), now)
        if weight == 0.0:
            continue
        num += weight * review.rating
        den += weight
        n_eff += weight
    return ScoreResult(score=num / den, n_eff=n_eff)


def implied_p(loo_score: float) -> float:
    """Success probability implied by the leave-one-out consensus."""
    lo, hi = P_CLAMP
    return min(hi, max(lo, (loo_score - 1.0) / 4.0))


def trust_delta(loo_score: float, rating: int) -> float:
    """Proper scoring rule: trust moves by the information the review carried
    beyond the current consensus. Ratings of 3 make no prediction."""
    if rating == 3:
        return 0.0
    p = implied_p(loo_score)
    likelihood = p if rating >= 4 else 1.0 - p
    return ETA * math.log2(likelihood / 0.5)


def updated_raw_trust(raw: float, loo_score: float, rating: int) -> float:
    return min(TRUST_CAP, max(TRUST_FLOOR, raw + trust_delta(loo_score, rating)))


def is_unproven(n_eff: float) -> bool:
    return n_eff < UNPROVEN_N_EFF
