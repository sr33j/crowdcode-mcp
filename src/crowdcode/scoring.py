"""Canonical scoring & reputation math (docs/SCORING.md v3.1).

Every published score — the MCP get_service_score tool, /api/services,
/api/services/top, /api/services/{id} — comes from compute_score() here, either
directly or via the stored services.score/n_eff columns that the review write
path and the nightly consistency sweep refresh with it. There is deliberately
no second implementation of the formula (a SQL twin would drift).

All functions are pure: rows in, numbers out, no database imports.

v3 narrows what counts **going forward**. A review written from the cutover on
carries ranking weight only if it is backed by a hosted OpenCrowd gateway
receipt and a platform-judge verdict; everything else — CLI wallets,
crowdcode-mcp users — is still stored, read and displayed, as community
evidence with weight exactly zero. That weight is held by the reviewing
**owner**, not the wallet: one owner runs many hosted agents with many wallets,
so trust and the one-vote cap are keyed on ``owner_ref``.

Reviews that already existed at the cutover keep the weight they had. They were
earned under v2's rules — wallet trust, the on-chain proof multiplier, and the
wallet/service/UTC-day bucket — and those rules are preserved verbatim for
exactly those rows, so a score built over months of real paid calls does not
evaporate the day the rules change. Three things keep that from being a hole:

* `weight_basis` is assigned once, by the backfill. The write path can only
  ever produce `hosted` or `none`, so no new review can become legacy.
* The wallet trust behind legacy rows is **frozen** at its cutover snapshot and
  never updated again, so the wallet-trust loop that v3 closed cannot be
  reopened by reviewing more.
* Legacy evidence decays on the same 180-day half-life it always did. The old
  scores stand today and fade on their original schedule as judged evidence
  accumulates underneath them.

So a published score is the sum of two eras: frozen legacy evidence, and
trusted owner votes. `ScoreResult` reports each separately.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping

ALGORITHM = "crowdcode-scoring-v3.1"

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

# Hosted reviews all weigh the same. In v2 the multiplier priced how much a
# payment claim proved (1x signed, 2x on-chain); for a hosted review there is
# nothing left to price, because it either clears the receipt-and-judge gate or
# carries no weight at all. Keeping it at 1.0 makes the hosted half of n_eff
# readable: it is the number of trusted owner votes behind a score.
WEIGHTED_MULTIPLIER = 1.0

# v2's multipliers, preserved for rows written before the cutover so their
# scores reproduce exactly. Never applied to anything written since.
LEGACY_PROOF_VERIFIED = 2.0
LEGACY_PROOF_SIGNED = 1.0
LEGACY_LEVEL_MULTIPLIERS = {
    "unverified": LEGACY_PROOF_SIGNED,
    "signature_only": LEGACY_PROOF_SIGNED,
    "onchain_verified": LEGACY_PROOF_VERIFIED,
    "response_attested": LEGACY_PROOF_VERIFIED,
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
    # None on rows predating the level column. 'response_attested' is the
    # hosted gateway receipt level; nothing else can be weighted.
    payment_verification_level: str | None = None
    # The hosted owner behind the review, from its verified attestation.
    owner_ref: str | None = None
    # None when the review carries ranking weight; otherwise why it does not
    # ('community', 'self_dealing', 'dust_payment', 'unsigned_placeholder').
    weight_exclusion: str | None = "community"
    # Which era's rules weigh this row: 'legacy' for the pre-cutover corpus
    # (v2 wallet trust, frozen), 'hosted' for a verified judged purchase,
    # 'none' for everything else. Assigned once; the write path never writes
    # 'legacy'.
    weight_basis: str = "none"

    @property
    def weighted(self) -> bool:
        """Carries weight under the v3 hosted rules."""
        return (
            self.weight_basis == "hosted"
            and self.weight_exclusion is None
            and self.owner_ref is not None
        )

    @property
    def legacy(self) -> bool:
        """Carries the weight it had before the cutover, under v2's rules."""
        return self.weight_basis == "legacy" and self.wallet is not None


@dataclass(frozen=True)
class TrustRow:
    raw_trust: float
    is_seed: bool = False
    slashed: bool = False


@dataclass(frozen=True)
class ScoreResult:
    score: float
    n_eff: float
    # The two eras behind n_eff: frozen pre-cutover evidence, and trusted
    # owner votes. They are on different scales (v2 priced proof up to 2x;
    # a hosted vote is exactly 1), so they are published separately rather
    # than left to be read as one number.
    n_eff_legacy: float = 0.0
    n_eff_hosted: float = 0.0


@dataclass(frozen=True)
class LegacyReviewBucket:
    """One wallet's aggregate pre-cutover opinion of one service on one UTC day.

    v2's influence cap, preserved verbatim so the scores it produced reproduce
    exactly. Nothing written since the cutover can land in one of these.
    """

    wallet: str
    day: date
    rating: float
    evidence: float
    created_at: datetime


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


def legacy_multiplier(review: ReviewRow) -> float:
    """v2's proof multiplier, for pre-cutover rows only."""
    level = review.payment_verification_level
    if level in LEGACY_LEVEL_MULTIPLIERS:
        return LEGACY_LEVEL_MULTIPLIERS[level]
    return LEGACY_PROOF_VERIFIED if review.payment_verified else LEGACY_PROOF_SIGNED


def proof_multiplier(review: ReviewRow) -> float:
    """Weight multiplier for one review, by era: v2's proof pricing for the
    frozen legacy corpus, a flat 1 for a verified judged purchase, and zero
    for everything else. Community reviews are evidence a reader can see,
    never evidence the ranking moves on."""
    if review.legacy:
        return legacy_multiplier(review)
    return WEIGHTED_MULTIPLIER if review.weighted else 0.0


def review_weight(
    review: ReviewRow, trust: TrustRow | None, now: datetime
) -> float:
    """One review's weight. `trust` is the owner's for a hosted review and the
    wallet's frozen trust for a legacy one — the caller knows which key to
    look up, because the eras use different identities."""
    multiplier = proof_multiplier(review)
    if multiplier == 0.0:
        return 0.0
    weight = effective_weight(trust)
    if weight == 0.0:
        return 0.0
    return weight * multiplier * decay_factor(review.created_at, now)


def aggregate_legacy_reviews(
    reviews: Iterable[ReviewRow], now: datetime
) -> list[LegacyReviewBucket]:
    """v2's wallet/service/UTC-day aggregation, for pre-cutover rows only.

    Kept byte-for-byte equivalent to the v2 implementation: the rating is the
    proof-and-decay-weighted average within the bucket and the evidence is
    capped at the strongest single review, so the scores this produced before
    the cutover reproduce exactly afterwards.
    """

    grouped: dict[tuple[str, date], list[tuple[ReviewRow, float]]] = {}
    for review in reviews:
        if not review.legacy:
            continue
        assert review.wallet is not None
        day = review.created_at.astimezone(UTC).date()
        evidence = legacy_multiplier(review) * decay_factor(review.created_at, now)
        grouped.setdefault((review.wallet, day), []).append((review, evidence))

    buckets: list[LegacyReviewBucket] = []
    for (wallet, day), items in grouped.items():
        total = sum(evidence for _, evidence in items)
        if total <= 0:
            continue
        buckets.append(
            LegacyReviewBucket(
                wallet=wallet,
                day=day,
                rating=sum(review.rating * evidence for review, evidence in items)
                / total,
                evidence=max(evidence for _, evidence in items),
                created_at=max(review.created_at for review, _ in items),
            )
        )
    return sorted(buckets, key=lambda bucket: (bucket.created_at, bucket.wallet))


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
    legacy_trust: Mapping[str, TrustRow] | None = None,
    exclude_owner: str | None = None,
    prior_mean: float = MU0,
) -> ScoreResult:
    """score = (sum(w*r) + kappa*mu0) / (sum(w) + kappa); n_eff = sum(w).

    Two eras contribute to the same sum. `trust_map` is keyed by owner_ref and
    weighs hosted reviews; `legacy_trust` is keyed by wallet address and is the
    frozen pre-cutover snapshot that weighs legacy rows. Omitting
    `legacy_trust` scores the hosted era alone.

    exclude_owner omits one owner's own hosted reviews; reputation callers use
    compute_trust_consensus(), which also selects the neutral internal prior.
    """
    num = KAPPA * prior_mean
    den = KAPPA
    n_eff_hosted = 0.0
    n_eff_legacy = 0.0

    hosted = [
        review
        for review in reviews
        if review.weighted and (exclude_owner is None or review.owner_ref != exclude_owner)
    ]
    for bucket in aggregate_owner_reviews(hosted, now):
        weight = effective_weight(trust_map.get(bucket.owner_ref)) * bucket.evidence
        if weight == 0.0:
            continue
        num += weight * bucket.rating
        den += weight
        n_eff_hosted += weight

    if legacy_trust is not None:
        for bucket in aggregate_legacy_reviews(reviews, now):
            weight = effective_weight(legacy_trust.get(bucket.wallet)) * bucket.evidence
            if weight == 0.0:
                continue
            num += weight * bucket.rating
            den += weight
            n_eff_legacy += weight

    return ScoreResult(
        score=num / den,
        n_eff=n_eff_hosted + n_eff_legacy,
        n_eff_legacy=n_eff_legacy,
        n_eff_hosted=n_eff_hosted,
    )


def compute_trust_consensus(
    reviews: Iterable[ReviewRow],
    trust_map: Mapping[str, TrustRow],
    now: datetime,
    *,
    exclude_owner: str,
    legacy_trust: Mapping[str, TrustRow] | None = None,
) -> ScoreResult:
    """Leave-one-out evidence for reputation, retaining the neutral v1 prior.

    Legacy evidence is included. A new owner has to be able to earn trust
    somewhere, and before the cutover almost every service in the directory was
    scored by wallets rather than owners; without the old consensus to agree or
    disagree with, trust could only ever be earned on the handful of services a
    seed owner had personally bought from. The residual — an owner whose own
    pre-cutover wallet reviews moved a consensus can earn a little trust by
    agreeing with them — is bounded by the frozen legacy corpus and by the
    per-event cap of about 0.0185 raw trust.
    """
    return compute_score(
        reviews, trust_map, now,
        legacy_trust=legacy_trust,
        exclude_owner=exclude_owner,
        prior_mean=TRUST_MU0,
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
