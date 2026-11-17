"""Tiers and value ranking for offerings (C10's estimate, C13's ranking).

Shared by the capability tree (W6) and meant for `/api/services/find` (W10):
one definition of "best-known" and "worth exploring".

- **Delivery** is `Beta(α, β)`. When W4's reliability-v2 has stored `alpha`,
  `beta` and `tier` for a service, those are used as they are. Until then the
  prior is computed here from C10's outside evidence (Bazaar payers and calls,
  the mpp.dev catalog), and v1's judged purchases are added (delivered: α+1;
  not delivered: β+1; `partial` counts as delivered, C11).
- **Tier**, by C10: `bad` (refused), `proven`, `popular`, or `new`.
- **Value per dollar** is delivery × quality / price. Quality is v2's shrunk
  judge mean when present, else neutral (1.0) until C9's leaderboard priors
  exist.
- **Ranking** (C13): best-known picks are `proven` and `popular` offerings by
  posterior-mean value; exploration candidates are the others most often
  chosen by a Thompson sample (seeded, so a build is reproducible), in
  proportion to their uncertainty.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Iterable

import numpy as np

from crowdcode import reliability

TIERS = ("proven", "popular", "new", "bad")
DRAWS = 400
MIN_PRICE_USD = 0.0005  # a free or sub-cent offering still has a finite value
PROVEN_MIN_JUDGED = 5
PROVEN_MIN_MEAN = 0.85
BAD_MIN_JUDGED = 4
BAD_BELOW_MEAN = 0.5


@dataclass
class Estimate:
    alpha: float
    beta: float
    prior: str  # where the prior came from, in words
    tier: str
    reason: str
    judged: int = 0
    delivered: int = 0
    quality: float = float(reliability.QUALITY_DEFAULT_PRIOR) / 5  # 0..1, from v2's 1-5 scale
    source: str = "computed"  # computed here, or "reliability-v2"

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)


def prior(payers: int | None, calls: int | None, curated: bool) -> tuple[float, float, str, str]:
    """C10's prior from outside evidence, by reliability-v2's own rule (W4):
    (alpha, beta, source in words, kind: bazaar_strong | bazaar | curated | none)."""
    p = reliability.prior_for({"bazaar_payers_30d": payers, "bazaar_calls_30d": calls, "curated": curated})
    return float(p["alpha"]), float(p["beta"]), str(p["source"]), str(p["kind"])


def _judged(state_row: dict[str, Any] | None) -> tuple[int, int]:
    judged = ((state_row or {}).get("inputs") or {}).get("judged_30d") or (state_row or {}).get("judged_30d") or {}
    if not isinstance(judged, dict):
        return 0, 0
    total = int(judged.get("total") or 0)
    delivered = int(judged.get("delivered") or 0) + int(judged.get("partial") or 0)
    return total, min(delivered, total)


def estimate(offering: dict[str, Any], state_row: dict[str, Any] | None) -> Estimate:
    """The delivery estimate and tier for one offering.

    `offering`: bazaar_payers_30d, bazaar_calls_30d, sources, liveness.
    `state_row`: the service's `service_reliability` row (v1 or v2), or None.
    """
    row = state_row or {}
    inputs = row.get("inputs") if isinstance(row.get("inputs"), dict) else {}
    # reliability-v2's columns, else the same keys inside `inputs`.
    v2 = {k: row[k] if row.get(k) is not None else inputs.get(k) for k in ("alpha", "beta", "tier", "quality")}
    if v2["alpha"] is not None and v2["beta"] is not None and v2["tier"] in TIERS:
        reasons = [str(r) for r in row.get("reasons") or [] if str(r).strip()]
        # v2's quality is the shrunk judge rating on 1-5; the value uses it as a share.
        quality = (float(v2["quality"]) / 5 if v2["quality"] is not None
                   else float(reliability.QUALITY_DEFAULT_PRIOR) / 5)
        total, delivered = _judged(row)
        return Estimate(float(v2["alpha"]), float(v2["beta"]), str(inputs.get("prior_source") or "reliability-v2"),
                        str(v2["tier"]), reasons[0] if reasons else f"{v2['tier']} (reliability-v2)",
                        total, delivered, quality, "reliability-v2")

    curated = "mpp" in (offering.get("sources") or [])
    a, b, source, kind = prior(offering.get("bazaar_payers_30d"), offering.get("bazaar_calls_30d"), curated)
    total, delivered = _judged(row)
    alpha, beta = a + delivered, b + (total - delivered)
    mean = alpha / (alpha + beta)
    liveness = offering.get("liveness") or "unknown"
    v1_reasons = [str(r) for r in row.get("reasons") or [] if str(r).strip()]
    if row.get("state") == "unreliable" or liveness == "dead" or (total >= BAD_MIN_JUDGED and mean < BAD_BELOW_MEAN):
        reason = v1_reasons[0] if row.get("state") == "unreliable" and v1_reasons else (
            "dead: its last probes all failed" if liveness == "dead"
            else f"{delivered} of {total} judged purchases delivered")
        return Estimate(alpha, beta, source, "bad", reason, total, delivered)
    if total >= PROVEN_MIN_JUDGED and mean >= PROVEN_MIN_MEAN:
        return Estimate(alpha, beta, source, "proven",
                        f"{delivered} of {total} judged purchases delivered (estimate {mean:.2f})", total, delivered)
    healthy = liveness != "failing"
    # Popular: the strong outside prior (≥ 10 payers who come back), or the
    # curated catalog, with no failing probe.
    if healthy and kind in ("bazaar_strong", "curated"):
        return Estimate(alpha, beta, source, "popular", f"popular: {source}"
                        + (f"; {delivered} of {total} judged delivered" if total else ""), total, delivered)
    why = source if kind != "none" else "no payers or judged purchases yet"
    if liveness == "failing":
        why += "; its latest probe failed"
    return Estimate(alpha, beta, source, "new", f"new: {why}"
                    + (f"; {delivered} of {total} judged delivered" if total else ""), total, delivered)


@dataclass
class Ranked:
    picks: list[dict[str, Any]] = field(default_factory=list)  # best-known, by posterior-mean value
    explore: list[dict[str, Any]] = field(default_factory=list)  # Thompson exploration candidates
    eligible: int = 0  # offerings not `bad`


def _price(offering: dict[str, Any], fallback: float) -> float:
    price = offering.get("price_usd")
    return max(float(price) if price is not None else fallback, MIN_PRICE_USD)


def seed_for(*parts: str) -> int:
    return int.from_bytes(hashlib.sha256("|".join(parts).encode()).digest()[:8], "big")


def rank(offerings: Iterable[dict[str, Any]], *, seed: int, picks: int = 3, explore: int = 3,
         draws: int = DRAWS) -> Ranked:
    """C13 over `offerings` (each with `estimate`: Estimate and `price_usd`).

    Picks: `proven`/`popular` offerings by E[delivery] × quality / price.
    Exploration: the rest, by how often a Thompson sample of delivery × quality
    / price makes them the best choice of all; ties by the sample's 90th
    percentile, so an uncertain cheap offering is preferred over a known-poor one.
    """
    pool = [o for o in offerings if o["estimate"].tier != "bad"]
    out = Ranked(eligible=len(pool))
    if not pool:
        return out
    known_prices = sorted(float(o["price_usd"]) for o in pool if o.get("price_usd") is not None)
    fallback = known_prices[len(known_prices) // 2] if known_prices else 0.05
    prices = np.array([_price(o, fallback) for o in pool])
    quality = np.array([o["estimate"].quality for o in pool])
    alpha = np.array([o["estimate"].alpha for o in pool])
    beta = np.array([o["estimate"].beta for o in pool])
    mean_value = alpha / (alpha + beta) * quality / prices
    rng = np.random.default_rng(seed)
    samples = rng.beta(alpha, beta, size=(draws, len(pool))) * quality / prices
    wins = np.bincount(samples.argmax(axis=1), minlength=len(pool)) / draws
    upper = np.quantile(samples, 0.9, axis=0)

    def entry(i: int, role: str) -> dict[str, Any]:
        e = pool[i]["estimate"]
        return {**{k: v for k, v in pool[i].items() if k != "estimate"},
                "tier": e.tier, "reason": e.reason, "alpha": round(e.alpha, 2), "beta": round(e.beta, 2),
                "delivery_estimate": round(e.mean, 3), "judged": e.judged, "delivered": e.delivered,
                "value_per_dollar": round(float(mean_value[i]), 2), "chosen_share": round(float(wins[i]), 3),
                "role": role}

    known = [i for i, o in enumerate(pool) if o["estimate"].tier in ("proven", "popular")]
    known.sort(key=lambda i: (-mean_value[i], -(pool[i].get("payers") or 0), str(pool[i].get("offering_id"))))
    out.picks = [entry(i, "pick") for i in known[:picks]]
    chosen = set(known[:picks])
    rest = [i for i in range(len(pool)) if i not in chosen]
    rest.sort(key=lambda i: (-wins[i], -upper[i], str(pool[i].get("offering_id"))))
    out.explore = [entry(i, "explore") for i in rest[:explore]]
    return out
