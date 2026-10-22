"""The docs/SCORING.md §6.3 simulation, driven by the production functions.

Deterministic (seed=42). This is the regression guard on the properties the
design doc claims: adversaries pinned at exactly zero weight, trust
propagating to services the seed never reviewed, a wash-traded scam service
never lifted, and published scores that rank services by true quality.

Reviewers here are **owners** (v3): each gets one vote per service however many
rounds it reviews in. That changes what "accurate" can mean. In v2, 100 days of
reviewing piled up ~800 units of evidence per service, so the κ=2 prior was
negligible and the published score converged on the true quality. With one vote
per owner, a service has at most four units of trusted evidence here, and the
Bayesian shrinkage that is the whole point of κ keeps the published score
deliberately short of the truth. So the accuracy claim is tested where it
belongs — on the owner bucket means, which are what the score is computed from
— plus the ordering of the published scores, and the shrunk value each score
should take given how many owners actually voted.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from crowdcode.scoring import (
    KAPPA,
    MU0,
    ReviewRow,
    TrustRow,
    aggregate_owner_reviews,
    compute_score,
    compute_trust_consensus,
    effective_weight,
    updated_raw_trust,
)

NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)
ROUNDS = 100

N_REAL = 10
RELIABILITY = [1.0 - 0.75 * i / (N_REAL - 1) for i in range(N_REAL)] + [0.25]
SCAM = N_REAL
SEEDED = {0, 4, 9}
TARGETS = [1 + 4 * p for p in RELIABILITY]

REVIEWERS = (
    [("seed", "honest")]
    + [(f"hon{i}", "honest") for i in range(3)]
    + [(f"inv{i}", "inverter") for i in range(10)]
    + [(f"rand{i}", "random") for i in range(10)]
    + [(f"pump{i}", "pumper") for i in range(5)]
)


def _services_for(name: str, kind: str) -> list[int]:
    if name == "seed":
        return sorted(SEEDED)
    if kind == "pumper":
        return [SCAM]
    return list(range(len(RELIABILITY)))


def _rating_for(kind: str, works: bool, rng: random.Random) -> int:
    if kind == "honest":
        return 5 if works else 1
    if kind == "inverter":
        return 1 if works else 5
    if kind == "pumper":
        return 5
    return rng.choice([1, 5])


def _run():
    rng = random.Random(42)
    raw = {name: 0.0 for name, _ in REVIEWERS}
    trust: dict[str, TrustRow] = {
        name: (TrustRow(raw_trust=1.0, is_seed=True) if name == "seed"
               else TrustRow(raw_trust=0.0))
        for name, _ in REVIEWERS
    }
    reviews: list[list[ReviewRow]] = [[] for _ in RELIABILITY]
    max_adversary_weight = 0.0
    crossed: set[str] = set()

    # Each round is a distinct UTC day. In v2 that bought another bucket per
    # wallet; in v3 it buys nothing, which is the property under test. Future
    # synthetic timestamps keep decay at 1.0; decay is covered independently
    # in test_scoring.py.
    for round_index in range(ROUNDS):
        created_at = NOW + timedelta(days=round_index)
        for name, kind in REVIEWERS:
            for service in _services_for(name, kind):
                works = rng.random() < RELIABILITY[service]
                rating = _rating_for(kind, works, rng)
                if name != "seed":
                    loo = compute_trust_consensus(
                        reviews[service], trust, NOW, exclude_owner=name
                    )
                    raw[name] = updated_raw_trust(raw[name], loo.score, rating)
                    trust[name] = TrustRow(raw_trust=raw[name])
                reviews[service].append(
                    ReviewRow(
                        wallet=f"0xwallet-of-{name}",
                        rating=rating,
                        payment_verified=True,
                        signature_verified=True,
                        created_at=created_at,
                        payment_verification_level="response_attested",
                        owner_ref=name,
                        weight_exclusion=None,
                    )
                )
        for name, kind in REVIEWERS:
            weight = effective_weight(trust[name])
            if kind == "honest" and weight > 0:
                crossed.add(name)
            if kind in {"inverter", "random", "pumper"}:
                max_adversary_weight = max(max_adversary_weight, weight)

    scored = [compute_score(reviews[s], trust, NOW) for s in range(len(RELIABILITY))]
    finals = [result.score for result in scored]
    n_effs = [result.n_eff for result in scored]
    # The evidence the scores are computed from: one bucket per trusted owner.
    buckets = [
        [
            bucket
            for bucket in aggregate_owner_reviews(reviews[s], NOW)
            if effective_weight(trust[bucket.owner_ref]) > 0
        ]
        for s in range(len(RELIABILITY))
    ]
    return finals, trust, max_adversary_weight, crossed, n_effs, buckets


RESULT = _run()


def test_trusted_owner_votes_track_true_quality():
    """The evidence is accurate: each trusted owner's single vote is the mean
    of what it actually observed, which is the true expected honest rating."""
    _, _, _, _, _, buckets = RESULT
    errors = [
        abs(bucket.rating - TARGETS[service])
        for service in range(len(RELIABILITY))
        for bucket in buckets[service]
    ]
    mae = sum(errors) / len(errors)
    assert mae < 0.15, f"owner-vote MAE {mae:.3f}"


def test_published_scores_are_the_shrunk_posterior_of_those_votes():
    """And the published score is exactly the Bayesian shrinkage of them —
    deliberately short of the truth while few owners have voted, which is what
    n_eff is published for."""
    finals, _, _, _, n_effs, buckets = RESULT
    for service in range(len(RELIABILITY)):
        votes = buckets[service]
        assert n_effs[service] == len(votes), service
        expected = (sum(b.rating for b in votes) + KAPPA * MU0) / (len(votes) + KAPPA)
        assert abs(finals[service] - expected) < 1e-9, service
        # Shrinkage is towards the prior, never past the evidence.
        assert min(MU0, TARGETS[service]) - 0.3 <= finals[service] <= max(MU0, TARGETS[service]) + 0.3


def test_published_scores_rank_services_by_true_quality():
    finals, _, _, _, _, _ = RESULT
    ranked = sorted(range(N_REAL), key=lambda s: -finals[s])
    assert ranked == list(range(N_REAL)), finals


def test_no_adversary_ever_gains_weight():
    _, _, max_adversary_weight, _, _, _ = RESULT
    assert max_adversary_weight == 0.0


def test_honest_owners_earn_weight_despite_the_adversarial_majority():
    _, trust, _, crossed, _, _ = RESULT
    assert crossed == {"seed", "hon0", "hon1", "hon2"}
    for name in crossed:
        assert effective_weight(trust[name]) > 0


def test_trust_propagates_to_services_the_seed_never_reviewed():
    _, _, _, _, n_effs, buckets = RESULT
    for service in range(N_REAL):
        if service in SEEDED:
            continue
        # Three honest owners earned weight and voted here; the seed never did.
        assert n_effs[service] == 3, service
        # Their combined evidence is as accurate as on the seeded services.
        # (A single owner's 100-call mean has a standard error near 0.2 on a
        # coin-flip service, so the claim is about the evidence, not one vote.)
        consensus = sum(b.rating for b in buckets[service]) / len(buckets[service])
        assert abs(consensus - TARGETS[service]) < 0.25, service


def test_wash_traded_scam_service_is_not_lifted():
    finals, trust, _, _, n_effs, _ = RESULT
    # 5 pumper owners x 100 rounds = 500 five-star reviews on their own service,
    # every one of them a real judged hosted purchase, and none of them counted.
    for i in range(5):
        assert effective_weight(trust[f"pump{i}"]) == 0.0
    assert finals[SCAM] < 2.6, finals[SCAM]
    # Its score comes only from the honest owners who actually bought there.
    assert n_effs[SCAM] == 3
