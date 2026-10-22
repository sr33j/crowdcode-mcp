"""Exact-value tests for the canonical scoring math (docs/SCORING.md v3)."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from crowdcode.scoring import (
    ETA,
    KAPPA,
    MU0,
    THETA,
    TRUST_CAP,
    TRUST_FLOOR,
    TRUST_MU0,
    WEIGHTED_MULTIPLIER,
    ReviewRow,
    TrustRow,
    aggregate_owner_reviews,
    compute_score,
    compute_trust_consensus,
    decay_factor,
    effective_weight,
    implied_p,
    is_unproven,
    review_weight,
    trust_delta,
    updated_raw_trust,
)

NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)
SEED = TrustRow(raw_trust=1.0, is_seed=True)


def review(owner, rating, *, verified=True, age_days=0.0, level="response_attested",
           exclusion=None, wallet=None):
    """A weighted hosted review by default; `exclusion` makes it community."""
    return ReviewRow(
        wallet=wallet or f"0xwallet-of-{owner}",
        rating=rating,
        payment_verified=verified,
        signature_verified=True,
        created_at=NOW - timedelta(days=age_days),
        payment_verification_level=level,
        owner_ref=owner,
        weight_exclusion=exclusion,
    )


def test_only_attested_hosted_reviews_carry_weight():
    """The whole point of v3: a review either cleared the hosted-receipt gate
    or it weighs exactly nothing, whatever its payment proved."""
    from crowdcode.scoring import proof_multiplier

    assert proof_multiplier(review("owner", 5)) == WEIGHTED_MULTIPLIER == 1.0
    for exclusion in ("community", "self_dealing", "dust_payment", "unsigned_placeholder"):
        excluded = review("owner", 5, exclusion=exclusion)
        assert excluded.weighted is False
        assert proof_multiplier(excluded) == 0.0
        assert review_weight(excluded, SEED, NOW) == 0.0
    # An on-chain-verified community review (a CLI wallet paying for real) is
    # still only community evidence.
    legacy = review("owner", 5, level="onchain_verified", exclusion="community")
    assert proof_multiplier(legacy) == 0.0
    # A row with no owner cannot be weighted even if nothing excluded it.
    assert ReviewRow("0xw", 5, True, True, NOW, "response_attested", None, None).weighted is False


def test_no_reviews_sits_at_the_prior():
    result = compute_score([], {}, NOW)
    assert result.score == MU0 == 3.0
    assert result.n_eff == 0.0
    assert is_unproven(result.n_eff)


def test_single_weighted_seed_review():
    # weight = 1.0 (seed) * 1 (weighted) * 1 (fresh) = 1.0, so n_eff reads as
    # "one trusted owner vote".
    result = compute_score([review("seed", 5)], {"seed": SEED}, NOW)
    assert result.n_eff == 1.0
    assert result.score == (1.0 * 5 + KAPPA * MU0) / (1.0 + KAPPA)
    assert math.isclose(result.score, 11.0 / 3.0)
    assert not is_unproven(result.n_eff)


def test_one_owner_gets_one_vote_per_service_however_many_calls():
    """Replaces the v2 wallet/UTC-day cap. Days, agents and wallets no longer
    multiply influence: an owner has one vote on a service, full stop."""
    reviews = [
        review("seed", 1, wallet="0xagent-a"),
        review("seed", 5, wallet="0xagent-b"),
        ReviewRow("0xagent-c", 5, True, True,
                  datetime(2026, 7, 24, 23, 59, tzinfo=UTC), "response_attested", "seed", None),
    ]
    buckets = aggregate_owner_reviews(reviews, NOW)
    assert len(buckets) == 1
    assert buckets[0].owner_ref == "seed"
    assert buckets[0].evidence == 1.0
    result = compute_score(reviews, {"seed": SEED}, NOW)
    assert result.n_eff == 1.0
    assert 3.6 < buckets[0].rating < 3.7  # decay-weighted mean of 1, 5 and 5


def test_repeated_purchases_sharpen_one_vote_rather_than_stacking():
    reviews = [review("seed", 1)] + [review("seed", 5) for _ in range(20)]
    bucket = aggregate_owner_reviews(reviews, NOW)[0]
    assert bucket.evidence == 1.0
    assert math.isclose(bucket.rating, (1 + 20 * 5) / 21)


def test_two_owners_are_two_votes():
    reviews = [review("one", 5), review("two", 5)]
    trust = {"one": SEED, "two": SEED}
    assert len(aggregate_owner_reviews(reviews, NOW)) == 2
    assert compute_score(reviews, trust, NOW).n_eff == 2.0


def test_community_reviews_never_reach_a_score():
    """Two agents of one owner, a thousand CLI wallets: all community, all zero."""
    reviews = [review(f"owner{i}", 5, exclusion="community") for i in range(1000)]
    trust = {r.owner_ref: SEED for r in reviews}
    result = compute_score(reviews, trust, NOW)
    assert result.score == MU0
    assert result.n_eff == 0.0
    assert aggregate_owner_reviews(reviews, NOW) == []


def test_decay_halves_weight_at_the_half_life():
    assert decay_factor(NOW - timedelta(days=180), NOW) == 0.5
    result = compute_score([review("seed", 5, age_days=180)], {"seed": SEED}, NOW)
    assert result.n_eff == 0.5


def test_sub_theta_trust_rounds_to_zero_weight():
    assert effective_weight(TrustRow(raw_trust=THETA - 0.01)) == 0.0
    assert effective_weight(TrustRow(raw_trust=THETA)) == THETA


def test_unknown_and_slashed_owners_carry_no_weight():
    assert effective_weight(None) == 0.0
    assert effective_weight(TrustRow(raw_trust=1.0, slashed=True)) == 0.0
    assert review_weight(review("nobody", 5), None, NOW) == 0.0


def test_many_zero_trust_owners_cannot_move_the_score():
    # The core sybil property, now priced per owner: 50 fresh hosted owners
    # with real receipts and real judge scores still leave the prior alone.
    reviews = [review(f"owner{i:032x}", 5) for i in range(50)]
    trust = {r.owner_ref: TrustRow(raw_trust=0.0) for r in reviews}
    result = compute_score(reviews, trust, NOW)
    assert result.score == MU0
    assert result.n_eff == 0.0


def test_weight_is_capped_even_when_raw_trust_exceeds_the_cap():
    assert effective_weight(TrustRow(raw_trust=42.0)) == TRUST_CAP


def test_leave_one_out_excludes_only_that_owners_reviews():
    trust = {"seed": SEED, "other": TrustRow(raw_trust=1.0)}
    reviews = [review("seed", 5), review("other", 1)]
    full = compute_score(reviews, trust, NOW)
    loo = compute_score(reviews, trust, NOW, exclude_owner="other")
    assert loo.n_eff == 1.0
    assert loo.score == compute_score([review("seed", 5)], trust, NOW).score
    assert full.n_eff == 2.0


def test_rating_of_three_never_moves_trust():
    assert trust_delta(4.5, 3) == 0.0
    assert updated_raw_trust(0.5, 4.5, 3) == 0.5


def test_agreeing_with_a_confident_consensus_earns_trust():
    # consensus 4.6 => p = 0.9; a 5-star review is a correct confident call.
    assert trust_delta(4.6, 5) > 0
    assert trust_delta(4.6, 1) < 0
    # Being wrong costs more than being right pays (log asymmetry).
    assert abs(trust_delta(4.6, 1)) > abs(trust_delta(4.6, 5))


def test_no_trust_moves_while_consensus_sits_at_the_trust_prior():
    # p = 0.5 => log2(1) = 0: trust can only be earned on resources whose
    # consensus already moved, which is what anchors trust to the seeds.
    assert trust_delta(TRUST_MU0, 5) == 0.0
    assert trust_delta(TRUST_MU0, 1) == 0.0


def test_optimistic_public_prior_cannot_bootstrap_untrusted_owners():
    reviews = [review(f"owner{i}", 5) for i in range(50)]
    trust = {r.owner_ref: TrustRow(raw_trust=0.0) for r in reviews}
    assert compute_score(reviews, trust, NOW, prior_mean=4.0).score == 4.0
    for owner in trust:
        loo = compute_trust_consensus(reviews, trust, NOW, exclude_owner=owner)
        assert loo.score == 3.0
        assert loo.n_eff == 0.0
        assert updated_raw_trust(0.0, loo.score, 5) == 0.0


def test_trust_consensus_keeps_its_neutral_prior_with_real_seed_evidence():
    reviews = [review("seed", 5), review("other", 1)]
    trust = {"seed": SEED, "other": TrustRow(raw_trust=0.5)}
    public = compute_score(reviews, trust, NOW, exclude_owner="other")
    loo = compute_trust_consensus(reviews, trust, NOW, exclude_owner="other")
    assert math.isclose(public.score, 11.0 / 3.0)
    assert math.isclose(loo.score, 11.0 / 3.0)
    assert math.isclose(trust_delta(loo.score, 5), ETA * math.log2(implied_p(loo.score) / 0.5))


def test_an_accurate_reviewer_gains_and_a_coin_flipper_loses():
    loo = 4.0
    p = implied_p(loo)  # 0.75: the service really does work 75% of the time
    honest = p * trust_delta(loo, 5) + (1 - p) * trust_delta(loo, 1)
    flipper = 0.5 * trust_delta(loo, 5) + 0.5 * trust_delta(loo, 1)
    inverter = (1 - p) * trust_delta(loo, 5) + p * trust_delta(loo, 1)
    assert honest > 0
    assert flipper < 0
    assert inverter < flipper


def test_p_clamp_bounds_any_single_update():
    bound = ETA * abs(math.log2(0.05 / 0.5))
    for loo in (1.0, 3.0, 5.0):
        for rating in (1, 5):
            assert abs(trust_delta(loo, rating)) <= bound + 1e-12


def test_raw_trust_is_clamped_to_the_cap_and_the_floor():
    assert updated_raw_trust(TRUST_CAP, 4.9, 5) == TRUST_CAP
    assert updated_raw_trust(TRUST_FLOOR, 4.9, 1) == TRUST_FLOOR
    # No reflecting wall at zero: a wrong call from zero goes negative.
    assert updated_raw_trust(0.0, 4.9, 1) < 0.0
