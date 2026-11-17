"""Search and ranking over the services index (SRI-115 C13, crowdcode/offerings.py).

These run on keyword matching alone (no model), over small hand-made indexes,
so each test pins one rule: what joins the matching set, how the seeded draw
ranks it, what is left out, and what the response says.
"""

from __future__ import annotations

import json

import pytest

from crowdcode import discovery, offerings
from crowdcode.offerings import Offering, OfferingIndex

USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
BASE = "eip155:8453"
SOLANA = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"


def offering(id: int, name: str, description: str, *, price: float | None = 0.01, tier: str = "new",
             alpha: float = 1.0, beta: float = 1.0, leaves: tuple[str, ...] = (), networks=("base",),
             model: str | None = None, family: str | None = None, quality: float = 3.0, evidenced: bool | None = None,
             endpoint: str | None = None) -> Offering:
    o = Offering(id=id, method="POST", endpoint=endpoint or f"https://s{id}.example/api", name=name, description=description,
                 networks=tuple(networks), payable=True, price_usd=price, tier=tier, alpha=alpha, beta=beta,
                 quality=quality, leaves=leaves, model=model, model_family=family, reason=f"reason {id}")
    if model:
        o.models = {model: (family, None)}
    o.evidenced = evidenced if evidenced is not None else (tier in ("proven", "popular") or alpha + beta > 2)
    return o


def search(index: OfferingIndex, query: str, **kwargs) -> dict:
    return discovery.find_services(query, index=index, embedder=None, **kwargs)


def endpoints(result: dict) -> list[str]:
    return [s["endpoint"] for s in result["services"]]


VIDEO = [
    offering(1, "Veo video", "Generate a video from a text prompt with Veo 3.1", price=0.4, tier="popular", alpha=8,
             leaves=("video.generation",), model="veo-3.1", family="veo"),
    offering(2, "Seedance video", "Generate a video from a text prompt with Seedance 2.0", price=0.3,
             leaves=("video.generation",), model="seedance-2.0", family="seedance"),
    offering(3, "YouTube transcript", "Get the transcript of a video from a YouTube link", price=0.001,
             tier="popular", alpha=8, leaves=("video.transcription",)),
    offering(4, "Another video generator", "Generate a short video clip from a text prompt", price=0.2,
             tier="proven", alpha=40, beta=2, leaves=("video.generation",)),
]


def test_a_seed_reproduces_its_ranking_and_is_returned():
    index = OfferingIndex(VIDEO)
    first = search(index, "generate a video", seed=7)
    assert first["seed"] == 7 and first["source"] == "index"
    assert endpoints(search(index, "generate a video", seed=7)) == endpoints(first)
    unseeded = search(index, "generate a video")
    assert isinstance(unseeded["seed"], int)
    assert endpoints(search(index, "generate a video", seed=unseeded["seed"])) == endpoints(unseeded)


def test_a_capability_is_answered_by_its_leaf_not_by_a_neighbour_that_shares_words():
    result = search(OfferingIndex(VIDEO), "generate a video", seed=1)
    assert result["capability"] == "video.generation"
    top = endpoints(result)[:3]
    assert "https://s3.example/api" not in top  # the transcript tool says "video" too
    assert set(top) == {"https://s1.example/api", "https://s2.example/api", "https://s4.example/api"}


def test_naming_a_model_answers_with_its_sellers():
    result = search(OfferingIndex(VIDEO), "seedance video", seed=3)
    assert result["model"] == "seedance"
    assert endpoints(result)[0] == "https://s2.example/api"
    exact = search(OfferingIndex(VIDEO), "veo-3.1 please", seed=3)
    assert exact["model"] == "veo-3.1" and endpoints(exact)[0] == "https://s1.example/api"


def test_bad_and_unpayable_offerings_are_left_out_and_counted():
    rows = [
        offering(1, "Weather", "current weather forecast", tier="bad"),
        offering(2, "Weather two", "current weather forecast", networks=("tempo",)),
        offering(3, "Weather three", "current weather forecast"),
    ]
    result = search(OfferingIndex(rows), "weather forecast", networks=["base", "solana"], seed=1)
    assert endpoints(result) == ["https://s3.example/api"]
    assert result["left_out"] == {"bad": 1, "not_payable": 1}


def test_value_is_per_dollar_including_what_the_call_costs_the_agent():
    rows = [
        offering(1, "Search A", "web search results", price=0.01, tier="popular", alpha=8),
        offering(2, "Search B", "web search results", price=0.5, tier="popular", alpha=8),
    ]
    wins = sum(endpoints(search(OfferingIndex(rows), "web search", seed=s))[0] == "https://s1.example/api" for s in range(50))
    assert wins >= 45  # same evidence, a fiftieth of the price
    # Two cents of overhead per call: $0.001 against $0.01 is not a tenfold difference in value.
    cheap = offering(3, "Search C", "web search results", price=0.001, tier="popular", alpha=8)
    pricier_better = offering(4, "Search D", "web search results", price=0.01, tier="proven", alpha=80, beta=1, quality=4.8)
    wins = sum(endpoints(search(OfferingIndex([cheap, pricier_better]), "web search", seed=s))[0] == "https://s4.example/api"
               for s in range(50))
    assert wins >= 35


def test_the_best_known_option_usually_comes_first_however_many_unknowns_match():
    # Equal quality, so only the delivery draw separates them.
    proven = offering(1, "Known search", "web search results", price=0.01, tier="proven", alpha=40, beta=2)
    unknowns = [offering(i, f"Search {i}", "web search results", price=0.01, evidenced=False) for i in range(2, 80)]
    index = OfferingIndex([proven, *unknowns])
    firsts = [endpoints(search(index, "web search", seed=s))[0] for s in range(100)]
    assert firsts.count("https://s1.example/api") >= 70
    # ...and unknowns are still tried, a few at a time.
    assert len(set(firsts)) > 1
    for seed in range(20):
        top = endpoints(search(index, "web search", seed=seed, limit=10))
        # At most EXPLORE_SLOTS offerings with no evidence are drawn against it.
        assert "https://s1.example/api" in top[:offerings.EXPLORE_SLOTS + 1]


def test_explore_marks_a_result_its_draw_lifted_above_a_better_known_one():
    rows = [
        offering(1, "Known", "web search results", price=0.01, tier="proven", alpha=20, beta=4),
        offering(2, "Unknown", "web search results", price=0.01, evidenced=False),
    ]
    index = OfferingIndex(rows)
    seen = set()
    for seed in range(200):
        services = search(index, "web search", seed=seed)["services"]
        first = services[0]
        if first["endpoint"] == "https://s2.example/api":
            assert first.get("explore") is True
            seen.add("explored")
        else:
            assert "explore" not in first
            seen.add("exploited")
    assert seen == {"explored", "exploited"}


def test_results_are_compact():
    rows = [offering(i, f"Text to speech {i}", "Convert text to natural speech audio, many voices and formats, MP3 or WAV.",
                     tier="popular", alpha=8, leaves=("audio.speech-synthesis",),
                     endpoint=f"https://speech-provider-number-{i}.example.com/api/v1/audio/speech")
            for i in range(1, 9)]
    result = search(OfferingIndex(rows), "text to speech", seed=1, limit=5)
    assert len(result["services"]) == 5
    # C13: about 800 tokens for five results, counted as UTF-8 bytes / 3 like the tree (C7).
    assert len(json.dumps(result).encode()) / 3 <= 800
    first = result["services"][0]
    assert {"endpoint", "method", "name", "tier", "why", "price_usd", "networks"} <= set(first)
    assert "quality" not in first and "offers" not in first
    full = search(OfferingIndex(rows), "text to speech", seed=1, limit=5, detail="full")["services"][0]
    assert {"delivery", "quality", "match", "best"} <= set(full)


def test_a_query_the_index_cannot_match_goes_to_the_live_catalogs():
    class Catalogs:
        def cdp_search(self, query):
            return {"resources": [{"resource": "https://live.example/thing", "description": "obscure thing",
                                   "accepts": [{"network": BASE, "asset": USDC_BASE, "amount": "1000", "scheme": "exact"}]}]}

        def mpp_catalog(self):
            return {"services": []}

    index = OfferingIndex([offering(1, "Weather", "current weather forecast")])
    result = discovery.find_services("obscure thing", index=index, embedder=None, catalogs=Catalogs())
    assert result["source"] == "live"
    assert endpoints(result) == ["https://live.example/thing"]


def test_a_callers_candidate_outside_the_index_is_ranked_as_new():
    index = OfferingIndex([offering(1, "Weather", "current weather forecast", tier="popular", alpha=8)])
    result = search(index, "weather forecast", seed=2, candidates=[
        {"endpoint": "https://agentcash.example/weather", "name": "Weather forecast", "price_usd": 0.002, "network": "base",
         "source": "agentcash"}])
    found = {s["endpoint"]: s for s in result["services"]}
    assert found["https://agentcash.example/weather"]["tier"] == "new"
    assert "not in CrowdCode's index" in found["https://agentcash.example/weather"]["why"]
    assert "https://s1.example/api" in found


def test_the_prior_is_c10s():
    assert offerings.prior(12, 40, False)[:2] == (8.0, 1.0)
    assert offerings.prior(12, 12, False)[:2] == (3.0, 1.0)  # >= 10 payers, < 1.5 calls each (W4's clarification)
    assert offerings.prior(4, None, False)[:2] == (3.0, 1.0)
    assert offerings.prior(None, None, True)[:2] == (3.0, 1.0)
    assert offerings.prior(1, 1, False)[:2] == (1.0, 1.0)


def _rows(resource: dict, **extra) -> dict:
    base = {"id": 1, "method": "POST", "route_template": "https://x.example/a", "probe_url": "https://x.example/a",
            "has_params": False, "service_name": "X", "description": "does x", "schema_text": "", "sources": ["bazaar"],
            "networks": [BASE, SOLANA], "payable": True, "protocol": "x402", "price_atomic": "20000",
            "price_asset": USDC_BASE, "quoted_price_atomic": None, "quoted_asset": None, "quoted_network": None,
            "bazaar_payers_30d": 15, "bazaar_calls_30d": 60, "tracked": True, "liveness": "alive", "service_id": "svc_1"}
    base.update(resource)
    return {"resources": [base], **extra}


def test_offerings_read_reliability_v2_when_it_is_there():
    v2 = offerings.offerings_from_rows(_rows({}, reliability={"svc_1": {
        "state": "reliable", "tier": "proven", "alpha": 30.0, "beta": 2.0, "quality": 4.4,
        "reason": "proven: 28 of 29 judged purchases delivered"}}))[0]
    assert (v2.tier, v2.alpha, v2.beta, v2.quality) == ("proven", 30.0, 2.0, 4.4)
    assert v2.reason == "28 of 29 judged purchases delivered"
    assert v2.price_usd == pytest.approx(0.02) and v2.networks == ("base", "solana")


def test_without_a_v2_answer_the_tier_follows_the_shared_estimate():
    """value_ranking.estimate (W6) is the one definition the tree and search share."""
    popular = offerings.offerings_from_rows(_rows({}, reliability={"svc_1": {"state": "unverified", "judged": {}}}))[0]
    assert (popular.tier, popular.alpha, popular.beta) == ("popular", 8.0, 1.0)
    # C11: `partial` counts as delivered.
    judged = offerings.offerings_from_rows(_rows({}, reliability={"svc_1": {
        "state": "unverified", "judged": {"total": 6, "delivered": 2, "partial": 4, "not_delivered": 0}}}))[0]
    assert (judged.alpha, judged.beta) == (14.0, 1.0) and judged.tier == "proven"
    blocked = offerings.offerings_from_rows(_rows({}, reliability={"svc_1": {
        "state": "unreliable", "reason": "blocked by a CrowdCode operator on 2026-11-17: fixture"}}))[0]
    assert blocked.tier == "bad"


def test_an_offering_paid_through_crowdcode_but_listed_nowhere_is_payable_on_an_unknown_network():
    o = offerings.offerings_from_rows(_rows({"sources": ["gateway", "reviewed"], "networks": [], "payable": False,
                                             "price_atomic": None, "price_asset": None,
                                             "description": "translate documents"}))[0]
    assert o.payable and o.networks == (offerings.UNKNOWN_NETWORK,)
    result = search(OfferingIndex([o]), "translate documents", seed=1)
    assert result["source"] == "index"
    assert result["services"][0]["networks"] == []
