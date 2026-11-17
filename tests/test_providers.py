"""Providers: one rankings row per seller (SRI-118)."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from crowdcode.providers import group, provider_key, provider_name
from crowdcode.scoring import ReviewRow, compute_score

NOW = datetime(2026, 11, 17, tzinfo=UTC)
HOST = "https://api.financialdatasets.ai"


def vote(wallet, rating):
    return ReviewRow(wallet=wallet, rating=rating, payment_verified=True, signature_verified=True,
                     created_at=NOW, payment_verification_level="onchain_verified",
                     weight_exclusion=None, weight_basis="onchain")


def row(path, name, ratings, wallet="0xA"):
    result = compute_score([vote(wallet, r) for r in ratings], {}, NOW)
    endpoint = HOST + path
    return {"service_id": "svc" + path.replace("/", "_"), "name": name, "canonical_endpoint": endpoint,
            "provider_key": provider_key(endpoint), "score": result.score, "rated": result.rated,
            "avg_rating_weighted": result.avg_rating_weighted, "p_above_3": result.p_above_3,
            "score_alpha": result.alpha, "score_beta": result.beta, "n_eff": result.n_eff,
            "num_reviews": len(ratings), "num_weighted_reviews": len(ratings)}


def test_financial_datasets_is_one_provider_with_its_endpoints_underneath():
    # The 2026-11-17 purchases: one wallet, seven endpoints, eight reviews.
    rows = [
        row("/financial-metrics/snapshot", "api.financialdatasets.ai /financial-metrics/snapshot", [4, 4]),
        row("/financials/income-statements", "api.financialdatasets.ai /financials/income-statements", [5]),
        row("/earnings", "api.financialdatasets.ai /earnings", [5]),
        row("/insider-trades", "api.financialdatasets.ai /insider-trades", [4]),
        row("/institutional-holdings", "api.financialdatasets.ai /institutional-holdings", [3]),
        row("/macro/yield-curve/snapshot", "Financial Datasets", [5]),
        row("/macro/interest-rates/snapshot", "Financial Datasets", [4]),
        row("/prices", "api.financialdatasets.ai /prices", []),
    ]
    [provider] = group(rows)
    assert provider["name"] == "Financial Datasets"
    assert provider["num_endpoints"] == 8
    # One vote per wallet per endpoint: 7 votes, 4,5,5,4,3,5,4.
    pooled = compute_score([vote(f"0x{i}", r) for i, r in enumerate([4, 5, 5, 4, 3, 5, 4])], {}, NOW)
    assert provider["score"] == pytest.approx(pooled.score)
    assert provider["score"] == pytest.approx(30 / 7, abs=1e-9)  # min(4.29, 4.73)
    assert provider["avg_rating_weighted"] == pytest.approx(30 / 7)
    assert provider["endpoints"][0]["score"] == pytest.approx(3.75)
    assert provider["endpoints"][-1]["rated"] is False


def test_crowdcode_agents_are_each_their_own_provider():
    a = provider_key("https://crowdcode.app/s/venice-proxy/chat")
    b = provider_key("https://crowdcode.app/s/book-finder")
    assert a == "crowdcode.app/s/venice-proxy" and b == "crowdcode.app/s/book-finder"
    assert provider_key("https://www.exa.ai/search") == "exa.ai"


def test_an_unrated_provider_sorts_at_two_and_a_half():
    [provider] = group([row("/prices", "api.financialdatasets.ai /prices", [])])
    assert provider["rated"] is False and provider["score"] == 2.5
    assert provider["name"] == "api.financialdatasets.ai"


def test_the_name_is_a_real_one_when_any_endpoint_has_it():
    assert provider_name("api.exa.ai", [("api.exa.ai /search", 9), ("Exa Search", 1)]) == "Exa Search"
    assert provider_name("api.exa.ai", [("api.exa.ai /search", 9)]) == "api.exa.ai"
