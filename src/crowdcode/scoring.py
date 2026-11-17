"""Canonical scoring & reputation math (docs/SCORING.md v4).

v4 (SRI-118, owner decision 2026-11-17) changes what is published, not how
evidence is weighed: the score is **5 × P(quality > 3)**. Each weighted vote's
rating r becomes a goodness s = (r − 1) / 4, the votes update a uniform
Beta(1, 1) prior to Beta(α, β) with α = 1 + Σ w·s and β = 1 + Σ w·(1 − s), and
"better than a 3" is θ > ½, so the score is 5 × (1 − I_½(α, β)). With no
evidence it is 2.5; one 5★ vote is 3.75; eight votes averaging 4.25 are 4.77.
The posterior mean 1 + 4·α/(α+β) is exactly v3.1's shrunk mean (κ = 2,
μ₀ = 3), so it is still published (`mean`, `services.score_mean`) and still
drives owner trust, which therefore did not move.

v4 also weighs a third kind of evidence: a paid review verified on chain
outside the hosted gateway (`weight_basis = 'onchain'`), one vote per paying
wallet per service, with the guards in review_service and below.

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
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping

ALGORITHM = "crowdcode-scoring-v4"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


# The prior on θ, the probability that a weighted vote is good: uniform.
PRIOR_ALPHA = 1.0
PRIOR_BETA = 1.0
# The same prior read as pseudo-reviews at a mean rating: the posterior mean
# is (Σ w·r + KAPPA·MU0) / (Σ w + KAPPA), v3.1's shrunk mean.
KAPPA = PRIOR_ALPHA + PRIOR_BETA
MU0 = 1.0 + 4.0 * PRIOR_ALPHA / KAPPA

# What one vote weighs before owner trust and decay, by kind of evidence
# (docs/SCORING.md §3.2). Config, not code: the operator can change them with
# the environment, and a consistency sweep republishes every score.
#   hosted judged purchase (gateway receipt + platform judge)
HOSTED_WEIGHT = _env_float("CROWDCODE_WEIGHT_HOSTED", 1.0)
#   paid review verified on chain, outside the hosted gateway
ONCHAIN_WEIGHT = _env_float("CROWDCODE_WEIGHT_ONCHAIN", 1.0)
#   unverified community review: published, never weighed
COMMUNITY_WEIGHT = 0.0
#   a signed failed purchase: a 1-star observation of this weight
FAILURE_WEIGHT = _env_float("CROWDCODE_WEIGHT_FAILURE", 1.0)
FAILURE_RATING = 1.0
# A paying wallet nobody has penalized counts in full. Trust can only take
# weight away from it (slashing, or earned negative trust); see onchain_trust.
ONCHAIN_DEFAULT_TRUST = _env_float("CROWDCODE_ONCHAIN_DEFAULT_TRUST", 1.0)
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
WEIGHTED_MULTIPLIER = HOSTED_WEIGHT

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
# Below this much evidence a score is labelled "early" (shown with its raw
# average and count) rather than hidden.
EARLY_N_EFF = 1.0


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
    # (crowdcode.review_sql.WEIGHT_EXCLUSIONS: 'community', 'self_dealing',
    # 'dust_payment', 'unsigned_placeholder', 'platform_defect').
    weight_exclusion: str | None = "community"
    # Which era's rules weigh this row: 'legacy' for the pre-cutover corpus
    # (v2 wallet trust, frozen), 'hosted' for a verified judged purchase,
    # 'none' for everything else. Assigned once; the write path never writes
    # 'legacy'.
    weight_basis: str = "none"
    # The hosted owner a paying wallet is known to belong to (from its
    # verified gateway receipts), if any. An on-chain vote from such a wallet
    # is that owner's vote, so an owner never votes twice on one service.
    payer_owner_ref: str | None = None

    @property
    def onchain(self) -> bool:
        """A paid review verified on chain outside the hosted gateway that
        passed the write-path guards (payer is not the payee or its owner,
        not dust)."""
        return (
            self.weight_basis == "onchain"
            and self.weight_exclusion is None
            and self.wallet is not None
        )

    @property
    def voter(self) -> str | None:
        """Whose vote an on-chain review is: its owner's, else its wallet's."""
        if self.payer_owner_ref:
            return f"owner:{self.payer_owner_ref}"
        return f"wallet:{self.wallet.lower()}" if self.wallet else None

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
    n_eff_onchain: float = 0.0
    # Signed failed purchases, each a 1-star observation (decayed).
    n_eff_failures: float = 0.0
    # P(θ > ½) over every vote and failure: how likely the service is to be
    # better than a 3.
    p_above_3: float = 0.5
    # Σ w·r / Σ w over every vote and failure; None with no evidence.
    avg_rating_weighted: float | None = None
    # The posterior expected rating of the review votes alone,
    # 1 + 4·α/(α+β): v3.1's shrunk mean. Owner trust and the payment floors
    # (score_mean) read this, so v4 did not move them.
    mean: float = MU0
    # Beta(α, β) over every vote and failure. α + β − 2 is the total
    # evidence, which is what lets endpoints be pooled into a provider.
    alpha: float = PRIOR_ALPHA
    beta: float = PRIOR_BETA

    @property
    def rated(self) -> bool:
        """Any weighted evidence at all. Without it a service is "unrated";
        its `score` (2.5) is only a sort key."""
        return self.avg_rating_weighted is not None


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


@dataclass(frozen=True)
class OnchainVoteBucket:
    """One paying wallet's (or its owner's) single opinion of one service."""

    voter: str
    wallet: str
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


def onchain_trust(wallet: TrustRow | None, owner: TrustRow | None = None) -> float:
    """Trust behind an on-chain vote. A wallet is free to create, so the vote
    is bounded by the payment, the payee checks and one vote per wallet, not
    by earned trust: an unknown wallet counts ONCHAIN_DEFAULT_TRUST. Trust
    only removes weight: a slashed wallet or owner, or one whose trust has
    gone negative (penalized for disagreeing with consensus), weighs zero."""
    for row in (wallet, owner):
        if row is None:
            continue
        if row.slashed:
            return 0.0
        if row.is_seed:
            continue
        if row.raw_trust < 0.0:
            return 0.0
    return ONCHAIN_DEFAULT_TRUST


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


def aggregate_onchain_reviews(
    reviews: Iterable[ReviewRow], now: datetime
) -> list[OnchainVoteBucket]:
    """Collapse on-chain reviews into one vote per paying wallet (or its known
    owner) per service, like owner buckets: decay-weighted mean rating,
    evidence capped at the strongest single review."""
    grouped: dict[str, list[tuple[ReviewRow, float]]] = {}
    for review in reviews:
        if not review.onchain or review.voter is None:
            continue
        grouped.setdefault(review.voter, []).append(
            (review, decay_factor(review.created_at, now))
        )
    buckets: list[OnchainVoteBucket] = []
    for voter, items in grouped.items():
        total = sum(evidence for _, evidence in items)
        if total <= 0:
            continue
        latest = max(items, key=lambda item: item[0].created_at)[0]
        buckets.append(
            OnchainVoteBucket(
                voter=voter,
                wallet=str(latest.wallet).lower(),
                rating=sum(review.rating * evidence for review, evidence in items) / total,
                evidence=max(evidence for _, evidence in items),
                created_at=latest.created_at,
            )
        )
    return sorted(buckets, key=lambda bucket: (bucket.created_at, bucket.voter))


def goodness(rating: float) -> float:
    """A rating on 1..5 as the probability-scale goodness s ∈ [0, 1]."""
    return min(1.0, max(0.0, (rating - 1.0) / 4.0))


def regularized_incomplete_beta(x: float, a: float, b: float) -> float:
    """I_x(a, b), by the continued fraction (Numerical Recipes betai/betacf).
    Equal to scipy.special.betainc(a, b, x) to ~1e-12 for the a, b > 0 a
    score produces."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
        + a * math.log(x) + b * math.log1p(-x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _beta_cf(x, a, b) / a
    return 1.0 - front * _beta_cf(1.0 - x, b, a) / b


def _beta_cf(x: float, a: float, b: float) -> float:
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 500):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-15:
            break
    return h


def p_above_three(alpha: float, beta: float) -> float:
    """P(θ > ½ | Beta(α, β)) = 1 − I_½(α, β)."""
    return 1.0 - regularized_incomplete_beta(0.5, alpha, beta)


def score_from_votes(votes: Iterable[tuple[float, float]]) -> tuple[float, float, float, float]:
    """(score, p_above_3, alpha, beta) for (weight, rating) votes.

    score = min(weighted average rating, 5 × P(θ > ½)) (owner, 2026-11-17):
    the probability keeps a little evidence from looking excellent, the
    average keeps a lot of evidence from looking better than it is. With no
    evidence the score is 5 × ½ = 2.5, a sort key for an unrated service."""
    alpha, beta = PRIOR_ALPHA, PRIOR_BETA
    total = weighted = 0.0
    for weight, rating in votes:
        good = goodness(rating)
        alpha += weight * good
        beta += weight * (1.0 - good)
        total += weight
        weighted += weight * rating
    return (*score_from_sums(alpha, beta, weighted / total if total > 0 else None)[:2], alpha, beta)


def score_from_sums(alpha: float, beta: float, average: float | None) -> tuple[float, float]:
    """(score, p_above_3) from a Beta posterior and the weighted average it
    was built from. Endpoints pool into a provider by summing α − 1, β − 1."""
    p = p_above_three(alpha, beta)
    probability_score = 5.0 * p
    if average is None:
        return probability_score, p
    return min(average, probability_score), p


def compute_score(
    reviews: Iterable[ReviewRow],
    trust_map: Mapping[str, TrustRow],
    now: datetime,
    *,
    legacy_trust: Mapping[str, TrustRow] | None = None,
    wallet_trust: Mapping[str, TrustRow] | None = None,
    exclude_owner: str | None = None,
    failures: Iterable[datetime] = (),
) -> ScoreResult:
    """score = min(Σ w·r / Σ w, 5 × P(θ > ½)) over every weighted vote and
    every signed failed purchase; n_eff = Σ w over the review votes.

    `failures` are the times of signed failed purchases (seller_error,
    seller_rejected, no_receipt, authorization_expired). Each is a 1-star
    observation, weight 1 decaying like a review. They do not enter `mean`.

    Three kinds of vote, each with its own one-vote rule:

    * hosted judged purchases, one per owner, weighed by owner trust
      (`trust_map`, keyed by owner_ref);
    * paid reviews verified on chain, one per paying wallet, or per owner when
      the wallet is a known owner's; `wallet_trust` (frozen wallet_users,
      keyed by lowercase address) and `trust_map` can only remove their weight
      (onchain_trust). An owner whose hosted vote already counts does not get
      a second one this way;
    * the frozen pre-cutover corpus, weighed by `legacy_trust` (omit it to
      leave the legacy era out).

    exclude_owner omits one owner's votes of every kind; reputation callers
    use compute_trust_consensus().
    """
    reviews = list(reviews)
    votes: list[tuple[float, float]] = []
    n_eff_hosted = 0.0
    n_eff_legacy = 0.0
    n_eff_onchain = 0.0
    voted_owners: set[str] = set()

    hosted = [
        review
        for review in reviews
        if review.weighted and (exclude_owner is None or review.owner_ref != exclude_owner)
    ]
    for bucket in aggregate_owner_reviews(hosted, now):
        weight = effective_weight(trust_map.get(bucket.owner_ref)) * bucket.evidence
        if weight == 0.0:
            continue
        votes.append((weight, bucket.rating))
        n_eff_hosted += weight
        voted_owners.add(bucket.owner_ref)

    onchain = [
        review
        for review in reviews
        if review.onchain and (exclude_owner is None or review.payer_owner_ref != exclude_owner)
    ]
    for bucket in aggregate_onchain_reviews(onchain, now):
        owner = bucket.voter.removeprefix("owner:") if bucket.voter.startswith("owner:") else None
        if owner is not None and owner in voted_owners:
            continue
        trust = onchain_trust(
            (wallet_trust or {}).get(bucket.wallet),
            trust_map.get(owner) if owner else None,
        )
        weight = ONCHAIN_WEIGHT * trust * bucket.evidence
        if weight == 0.0:
            continue
        votes.append((weight, bucket.rating))
        n_eff_onchain += weight

    if legacy_trust is not None:
        for bucket in aggregate_legacy_reviews(reviews, now):
            weight = effective_weight(legacy_trust.get(bucket.wallet)) * bucket.evidence
            if weight == 0.0:
                continue
            votes.append((weight, bucket.rating))
            n_eff_legacy += weight

    _, _, review_alpha, review_beta = score_from_votes(votes)
    failure_votes = [(FAILURE_WEIGHT * decay_factor(at, now), FAILURE_RATING) for at in failures]
    failure_votes = [vote for vote in failure_votes if vote[0] > 0]
    every = votes + failure_votes
    score, p, alpha, beta = score_from_votes(every)
    total = sum(weight for weight, _ in every)
    return ScoreResult(
        score=score,
        n_eff=n_eff_hosted + n_eff_legacy + n_eff_onchain,
        n_eff_legacy=n_eff_legacy,
        n_eff_hosted=n_eff_hosted,
        n_eff_onchain=n_eff_onchain,
        n_eff_failures=sum(weight for weight, _ in failure_votes),
        p_above_3=p,
        avg_rating_weighted=(sum(w * r for w, r in every) / total) if total > 0 else None,
        mean=1.0 + 4.0 * review_alpha / (review_alpha + review_beta),
        alpha=alpha,
        beta=beta,
    )


def compute_trust_consensus(
    reviews: Iterable[ReviewRow],
    trust_map: Mapping[str, TrustRow],
    now: datetime,
    *,
    exclude_owner: str,
    legacy_trust: Mapping[str, TrustRow] | None = None,
    wallet_trust: Mapping[str, TrustRow] | None = None,
) -> ScoreResult:
    """Leave-one-out evidence for reputation. Trust updates read its `mean`,
    the posterior expected rating, which is v3.1's neutral shrunk consensus
    exactly (prior 3.0, strength 2): v4 changed the published score, not this.

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
        wallet_trust=wallet_trust,
        exclude_owner=exclude_owner,
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


def is_early(n_eff: float) -> bool:
    return n_eff < EARLY_N_EFF


def score_from_stored(score: float | None) -> float | None:
    """P(>3) from a stored v4 score."""
    return None if score is None else score / 5.0
