"""Providers: one row per seller, with its endpoints underneath (SRI-118).

The directory lists one `services` row per endpoint, so Financial Datasets,
reviewed at seven of its routes on 2026-11-17, showed as seven rankings rows,
some named "Financial Datasets" and some "api.financialdatasets.ai /earnings".
A provider is the host an endpoint is served from, except that every CrowdCode
agent shares crowdcode.app and is its own provider under `/s/<slug>`.

A provider's score pools its endpoints' evidence exactly: every endpoint's
score is a Beta(α, β) posterior built from a uniform prior plus its votes, so
the provider's is Beta(1 + Σ(α − 1), 1 + Σ(β − 1)), and its weighted average
is the evidence-weighted mean of theirs (crowdcode.scoring.score_from_sums).
One vote per paying wallet or owner still holds per endpoint; buying three of
a provider's products is three purchases, and three votes.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable
from urllib.parse import urlsplit

from crowdcode.agent_listings import agent_service_slug
from crowdcode.scoring import PRIOR_ALPHA, PRIOR_BETA, as_float, is_early, is_unproven, score_from_sums


def provider_key(endpoint: str | None, origin: str | None = None) -> str | None:
    slug = agent_service_slug(endpoint)
    if slug:
        return f"crowdcode.app/s/{slug}"
    url = endpoint or origin
    if not url:
        return None
    host = (urlsplit(url if "://" in url else f"https://{url}").hostname or "").lower()
    return host.removeprefix("www.") or None


def _generated(name: str, key: str) -> bool:
    """Names the index or a first review made up from the URL, not a name."""
    lowered = name.strip().lower()
    host = key.split("/", 1)[0]
    return lowered in (key, host, f"www.{host}") or lowered.startswith((f"{host} ", f"www.{host} ", f"{host}/"))


def provider_name(key: str, names: Iterable[tuple[str | None, int]]) -> str:
    """The provider's canonical name: the most-reviewed real name any of its
    endpoints carries, else its host."""
    counts: Counter[str] = Counter()
    for name, reviews in names:
        if name and not _generated(name, key):
            counts[name.strip()] += 1 + max(0, int(reviews or 0))
    if not counts:
        return key
    return sorted(counts.items(), key=lambda item: (-item[1], len(item[0]), item[0]))[0][0]


def _path(endpoint: str | None) -> str:
    if not endpoint:
        return "/"
    return urlsplit(endpoint).path or "/"


def group(services: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Providers from the /api/services rows (which carry score_alpha,
    score_beta, avg_rating_weighted and n_eff), best first."""
    by_key: dict[str, list[dict[str, Any]]] = {}
    for row in services:
        key = row.get("provider_key")
        if key:
            by_key.setdefault(key, []).append(row)
    providers = []
    for key, rows in by_key.items():
        alpha, beta, evidence, weighted = PRIOR_ALPHA, PRIOR_BETA, 0.0, 0.0
        for row in rows:
            a = as_float(row.get("score_alpha"))
            b = as_float(row.get("score_beta"))
            if a is None or b is None:
                continue
            alpha += a - PRIOR_ALPHA
            beta += b - PRIOR_BETA
            amount = (a - PRIOR_ALPHA) + (b - PRIOR_BETA)
            average = as_float(row.get("avg_rating_weighted"))
            if amount > 0 and average is not None:
                evidence += amount
                weighted += amount * average
        average = weighted / evidence if evidence > 0 else None
        score, p = score_from_sums(alpha, beta, average)
        n_eff = sum(as_float(row.get("n_eff")) or 0.0 for row in rows)
        name = provider_name(key, ((row.get("name"), row.get("num_reviews")) for row in rows))
        endpoints = sorted(
            (
                {
                    "service_id": row["service_id"],
                    "path": _path(row.get("canonical_endpoint")),
                    "name": row.get("name"),
                    "score": row.get("score"),
                    "rated": row.get("rated"),
                    "avg_rating_weighted": row.get("avg_rating_weighted"),
                    "p_above_3": row.get("p_above_3"),
                    "n_eff": row.get("n_eff"),
                    "num_reviews": row.get("num_reviews"),
                    "num_weighted_reviews": row.get("num_weighted_reviews"),
                }
                for row in rows
            ),
            key=lambda e: (-(e["score"] or 0.0), e["path"]),
        )
        providers.append({
            "provider_key": key,
            "name": name,
            "score": score,
            "rated": average is not None,
            "avg_rating_weighted": average,
            "p_above_3": p,
            "n_eff": n_eff,
            "unproven": is_unproven(n_eff),
            "early": is_early(evidence),
            "num_endpoints": len(rows),
            "num_reviews": sum(int(row.get("num_reviews") or 0) for row in rows),
            "num_weighted_reviews": sum(int(row.get("num_weighted_reviews") or 0) for row in rows),
            "endpoints": endpoints,
        })
    providers.sort(key=lambda p: (-p["score"], -p["n_eff"], -p["num_reviews"], p["name"].lower()))
    return providers

