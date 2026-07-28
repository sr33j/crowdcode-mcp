"""Exact-value tests for the canonical scoring math (docs/SCORING.md v1)."""

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
    ReviewRow,
    TrustRow,
    compute_score,
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


def review(wallet, rating, *, verified=True, age_days=0.0, level=None):
    return ReviewRow(
        wallet=wallet,
        rating=rating,
        payment_verified=verified,
        signature_verified=True,
        created_at=NOW - timedelta(days=age_days),
        payment_verification_level=level,
    )


def test_level_multipliers_gate_the_verified_upweight():
    # Proof-based and tx-hash-only verification weigh the same (both prove
    # exactly the on-chain transfer); unverified/signature_only weigh 1.
    from crowdcode.scoring import proof_multiplier

    assert proof_multiplier(review("0x1", 5, level="unverified")) == 1.0
    assert proof_multiplier(review("0x1", 5, level="signature_only")) == 1.0
    assert proof_multiplier(review("0x1", 5, level="onchain_verified")) == 2.0
    assert proof_multiplier(review("0x1", 5, level="response_attested")) == 2.0


def test_legacy_rows_without_a_level_fall_back_to_payment_verified():
    from crowdcode.scoring import proof_multiplier

    assert proof_multiplier(review("0x1", 5, verified=True, level=None)) == 2.0
    assert proof_multiplier(review("0x1", 5, verified=False, level=None)) == 1.0
    # The level wins over a stale boolean when both are present.
    assert (
        proof_multiplier(review("0x1", 5, verified=True, level="signature_only"))
        == 1.0
    )


def test_no_reviews_sits_at_the_prior():
    result = compute_score([], {}, NOW)
    assert result.score == MU0
    assert result.n_eff == 0.0
    assert is_unproven(result.n_eff)


def test_single_verified_seed_review():
    # weight = 1.0 (seed) * 2 (verified) * 1 (fresh) = 2.0
    result = compute_score([review("0xseed", 5)], {"0xseed": SEED}, NOW)
    assert result.n_eff == 2.0
    assert result.score == (2.0 * 5 + KAPPA * MU0) / (2.0 + KAPPA)
    assert result.score == 4.0
    assert not is_unproven(result.n_eff)


def test_unverified_review_carries_half_the_weight_of_a_verified_one():
    signed = compute_score(
        [review("0xseed", 5, verified=False)], {"0xseed": SEED}, NOW
    )
    assert signed.n_eff == 1.0


def test_decay_halves_weight_at_the_half_life():
    assert decay_factor(NOW - timedelta(days=180), NOW) == 0.5
    result = compute_score(
        [review("0xseed", 5, age_days=180)], {"0xseed": SEED}, NOW
    )
    assert result.n_eff == 1.0


def test_sub_theta_trust_rounds_to_zero_weight():
    assert effective_weight(TrustRow(raw_trust=THETA - 0.01)) == 0.0
    assert effective_weight(TrustRow(raw_trust=THETA)) == THETA


def test_unknown_and_slashed_wallets_carry_no_weight():
    assert effective_weight(None) == 0.0
    assert effective_weight(TrustRow(raw_trust=1.0, slashed=True)) == 0.0
    assert review_weight(review("0xnobody", 5), None, NOW) == 0.0


def test_many_zero_weight_wallets_cannot_move_the_score():
    # The core sybil property: 50 fresh wallets rating 5 leave the prior alone.
    reviews = [review(f"0x{i:040x}", 5) for i in range(50)]
    trust = {r.wallet: TrustRow(raw_trust=0.0) for r in reviews}
    result = compute_score(reviews, trust, NOW)
    assert result.score == MU0
    assert result.n_eff == 0.0


def test_weight_is_capped_even_when_raw_trust_exceeds_the_cap():
    assert effective_weight(TrustRow(raw_trust=42.0)) == TRUST_CAP


def test_leave_one_out_excludes_only_that_wallets_reviews():
    trust = {"0xseed": SEED, "0xother": TrustRow(raw_trust=1.0)}
    reviews = [review("0xseed", 5), review("0xother", 1)]
    full = compute_score(reviews, trust, NOW)
    loo = compute_score(reviews, trust, NOW, exclude_wallet="0xother")
    assert loo.n_eff == 2.0
    assert loo.score == compute_score([review("0xseed", 5)], trust, NOW).score
    assert full.n_eff == 4.0


def test_rating_of_three_never_moves_trust():
    assert trust_delta(4.5, 3) == 0.0
    assert updated_raw_trust(0.5, 4.5, 3) == 0.5


def test_agreeing_with_a_confident_consensus_earns_trust():
    # consensus 4.6 => p = 0.9; a 5-star review is a correct confident call.
    assert trust_delta(4.6, 5) > 0
    assert trust_delta(4.6, 1) < 0
    # Being wrong costs more than being right pays (log asymmetry).
    assert abs(trust_delta(4.6, 1)) > abs(trust_delta(4.6, 5))


def test_no_trust_moves_while_consensus_sits_at_the_prior():
    # p = 0.5 => log2(1) = 0: trust can only be earned on resources whose
    # consensus already moved, which is what anchors trust to the seeds.
    assert trust_delta(MU0, 5) == 0.0
    assert trust_delta(MU0, 1) == 0.0


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
