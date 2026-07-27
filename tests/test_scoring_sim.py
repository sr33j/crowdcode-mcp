"""The docs/SCORING.md §6.3 simulation, driven by the production functions.

Deterministic (seed=42). This is the regression guard on the properties the
design doc claims: accurate scores under an adversarial majority, adversaries
pinned at exactly zero weight, trust propagating to services the seed never
reviewed, and a wash-traded scam service staying at its true (bad) score.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from crowdcode.scoring import (
    ReviewRow,
    TrustRow,
    compute_score,
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

    # Decay-neutral: every review is stamped at NOW so the assertions test the
    # trust mechanics, not the 180-day half-life (covered in test_scoring.py).
    for _ in range(ROUNDS):
        for name, kind in REVIEWERS:
            for service in _services_for(name, kind):
                works = rng.random() < RELIABILITY[service]
                rating = _rating_for(kind, works, rng)
                if name != "seed":
                    loo = compute_score(
                        reviews[service], trust, NOW, exclude_wallet=name
                    )
                    raw[name] = updated_raw_trust(raw[name], loo.score, rating)
                    trust[name] = TrustRow(raw_trust=raw[name])
                reviews[service].append(
                    ReviewRow(
                        wallet=name,
                        rating=rating,
                        payment_verified=True,
                        signature_verified=True,
                        created_at=NOW,
                    )
                )
        for name, kind in REVIEWERS:
            weight = effective_weight(trust[name])
            if kind == "honest" and weight > 0:
                crossed.add(name)
            if kind in {"inverter", "random", "pumper"}:
                max_adversary_weight = max(max_adversary_weight, weight)

    finals = [compute_score(reviews[s], trust, NOW).score for s in range(len(RELIABILITY))]
    return finals, trust, max_adversary_weight, crossed


RESULT = _run()


def test_scores_track_true_quality():
    finals, _, _, _ = RESULT
    mae = sum(abs(finals[s] - TARGETS[s]) for s in range(len(RELIABILITY))) / len(
        RELIABILITY
    )
    assert mae < 0.15, f"MAE {mae:.3f}, finals={finals}"


def test_no_adversary_ever_gains_weight():
    _, _, max_adversary_weight, _ = RESULT
    assert max_adversary_weight == 0.0


def test_honest_wallets_earn_weight_despite_the_adversarial_majority():
    _, trust, _, crossed = RESULT
    assert crossed == {"seed", "hon0", "hon1", "hon2"}
    for name in crossed:
        assert effective_weight(trust[name]) > 0


def test_trust_propagates_to_services_the_seed_never_reviewed():
    finals, _, _, _ = RESULT
    for service in range(N_REAL):
        if service in SEEDED:
            continue
        assert abs(finals[service] - TARGETS[service]) < 0.25


def test_wash_traded_scam_service_is_not_lifted():
    finals, trust, _, _ = RESULT
    # 5 pumpers x 100 rounds = 500 five-star reviews on their own service.
    assert finals[SCAM] < 2.5, finals[SCAM]
    assert abs(finals[SCAM] - TARGETS[SCAM]) < 0.25
    for i in range(5):
        assert effective_weight(trust[f"pump{i}"]) == 0.0
