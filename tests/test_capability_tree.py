"""The capability tree (crowdcode.capability_tree, contracts C7, C10, C13, C14).

Pure tests cover the tiers and the ranking, the split rule, the media nodes'
fixed leaves and their models, demand, and the token budgets. The integration
test runs the real job against PostgreSQL with a scripted model.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

from crowdcode import capability_tree as ct
from crowdcode import media_models
from crowdcode import value_ranking as vr

ROOT = Path(__file__).parents[1]
NOW = datetime(2026, 11, 17, 6, 0, tzinfo=UTC)
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"


def resource(rid: int, sid: str | None, *, payers: int = 3, calls: int | None = None, price: int | None = 10_000,
             name: str | None = None, liveness: str = "alive", sources: list[str] | None = None,
             description: str | None = None, route: str | None = None) -> dict:
    origin = f"https://{sid or f'r{rid}'}.example"
    return {
        "id": rid, "method": "POST", "origin": origin,
        "route_template": route or f"{origin}/v1/r{rid}", "probe_url": route or f"{origin}/v1/r{rid}",
        "service_id": sid, "service_name": None, "service_label": name or f"{sid or 'listing'} r{rid}",
        "description": description or f"Endpoint {rid}", "input_schema": None, "payable": True,
        "networks": ["eip155:8453"], "liveness": liveness, "sources": sources or ["bazaar"],
        "bazaar_payers_30d": payers, "bazaar_calls_30d": calls if calls is not None else payers * 2,
        "price_atomic": price, "price_asset": USDC, "quoted_price_atomic": None, "quoted_asset": None,
    }


def judged(sid: str, total: int = 5, delivered: int = 5, state: str = "unverified", reasons=None) -> dict:
    return {"service_id": sid, "state": state, "rule_version": "reliability-v1",
            "reasons": reasons or [f"{delivered} of {total} judged purchases delivered in 30 days"],
            "inputs": {"judged_30d": {"total": total, "delivered": delivered, "partial": 0}}}


def view(*rows: dict) -> ct.ReliabilityView:
    return ct.ReliabilityView(states={r["service_id"]: r for r in rows}, available=True, rule_version="reliability-v1")


def leaf(node: str, name: str, *facets: str) -> dict:
    return {"id": f"{node}.{name}", "node": node, "slug": name, "label": name.replace("-", " ").title(),
            "when_to_look": f"When the task needs {name}.", "facets": list(facets)}


def fixed(node: str, **facets: list[str]) -> list[dict]:
    return [{**leaf(node, slug_, *facets.get(slug_, [])), "label": label, "when_to_look": when}
            for slug_, label, when in ct.FIXED_LEAVES[node]]


def assign(node: str, facet: str) -> list[dict]:
    return [{"node": node, "facet": facet}]


def node_of(tree: dict, node_id: str) -> dict:
    return next(n for n in tree["nodes"] if n["id"] == node_id)


def build(resources, assignments, leaves, proposals=(), reliability=None, version=3):
    return ct.assemble(resources=list(resources), assignments=assignments, leaves=list(leaves),
                       proposals=list(proposals), reliability=reliability or view(), built_at=NOW, version=version)


# --- C10: tiers ----------------------------------------------------------------------


def est(**kw) -> vr.Estimate:
    row = resource(1, "s", **{k: v for k, v in kw.items() if k != "state"})
    return vr.estimate(row, kw.get("state"))


def test_tiers_follow_c10():
    # Outside evidence: ≥ 10 payers who come back is popular; 3-9 payers is new with a Beta(3,1) prior.
    popular = est(payers=12, calls=30)
    assert (popular.tier, popular.alpha, popular.beta) == ("popular", 8.0, 1.0)
    assert "12 payers on the x402 Bazaar" in popular.reason
    few = est(payers=5)
    assert (few.tier, few.alpha, few.beta) == ("new", 3.0, 1.0)
    # ≥ 10 payers who rarely come back get the 3-9 band's prior, not popular.
    assert est(payers=20, calls=21).tier == "new"
    nobody = est(payers=0, calls=0)
    assert (nobody.tier, nobody.alpha, nobody.beta) == ("new", 1.0, 1.0)
    # The curated catalog with no payer data is popular, unless its probe is failing.
    curated = est(payers=0, calls=0, sources=["mpp"])
    assert (curated.tier, curated.alpha) == ("popular", 3.0) and "mpp.dev's curated catalog" in curated.reason
    assert est(payers=0, calls=0, sources=["mpp"], liveness="failing").tier == "new"
    # Judged purchases: ≥ 5 with a posterior mean ≥ 0.85 is proven; partial counts as delivered (C11).
    proven = est(payers=0, state={"inputs": {"judged_30d": {"total": 6, "delivered": 5, "partial": 1}}})
    assert proven.tier == "proven" and (proven.alpha, proven.beta) == (7.0, 1.0)
    # Bad: v1's unreliable (with its reason), dead, or ≥ 4 judged with a mean under 0.5.
    assert est(state={"state": "unreliable", "reasons": ["payee changed"]}).reason == "payee changed"
    assert est(liveness="dead").tier == "bad"
    assert est(payers=0, state={"inputs": {"judged_30d": {"total": 5, "delivered": 1}}}).tier == "bad"
    # reliability-v2's own estimate is used as it is.
    v2 = est(state={"alpha": 9, "beta": 2, "tier": "proven", "quality": 4.5, "reasons": ["v2 says so"], "inputs": {}})
    assert (v2.tier, v2.alpha, v2.reason, v2.source, v2.quality) == ("proven", 9.0, "v2 says so", "reliability-v2", 0.9)
    # Without a v2 row an offering gets v2's default quality, so values compare on one scale.
    assert est(payers=5).quality == 0.6 == est(state={"alpha": 3, "beta": 1, "tier": "new", "quality": 3}).quality


# --- C13: ranking --------------------------------------------------------------------


def offering(oid: str, tier_kw: dict, price: float | None) -> dict:
    return {"offering_id": oid, "name": oid, "price_usd": price, "payers": 1, "estimate": est(**tier_kw)}


def test_picks_are_known_offerings_by_value_and_exploration_is_thompson():
    pool = [
        offering("pop_cheap", {"payers": 12, "calls": 30}, 0.01),
        offering("pop_dear", {"payers": 50, "calls": 200}, 0.10),
        offering("new_cheap", {"payers": 0, "calls": 0}, 0.002),
        offering("new_dear", {"payers": 0, "calls": 0}, 1.00),
        offering("bad", {"liveness": "dead"}, 0.0001),
    ]
    ranked = vr.rank(pool, seed=vr.seed_for("t"))
    assert [p["offering_id"] for p in ranked.picks] == ["pop_cheap", "pop_dear"]  # value per dollar
    assert all(p["tier"] == "popular" and p["role"] == "pick" for p in ranked.picks)
    explore = [e["offering_id"] for e in ranked.explore]
    assert explore[0] == "new_cheap" and "bad" not in explore and "pop_cheap" not in explore
    assert ranked.explore[0]["chosen_share"] > 0.5 and ranked.eligible == 4
    # Reproducible for a seed.
    again = vr.rank(pool, seed=vr.seed_for("t"))
    assert [e["offering_id"] for e in again.explore] == explore
    # Nothing known: no picks, only exploration.
    assert vr.rank(pool[2:], seed=1).picks == []


# --- The split rule (non-media nodes) ------------------------------------------------


def finance_fixture(**extra):
    resources = [resource(1, "svc_x", payers=40, calls=100), resource(2, "svc_x2", payers=30, calls=90, price=20_000),
                 resource(3, "svc_y", payers=20, calls=60), resource(4, "svc_u", payers=4),
                 resource(5, "svc_v", payers=1), resource(6, "svc_bad", payers=500, calls=900, liveness="dead")]
    assignments = {"r:1": assign("finance", "token-prices"), "r:2": assign("finance", "candles"),
                   "r:3": assign("finance", "wallet-balances"), "r:4": assign("finance", "defi-yields"),
                   "r:5": assign("finance", "token-prices"), "r:6": assign("finance", "defi-yields"),
                   "p:p_defi": assign("finance", "defi-yields")}
    leaves = [leaf("finance", "prices", "token-prices"), leaf("finance", "candles", "candles"),
              leaf("finance", "wallets", "wallet-balances"), leaf("finance", "defi", "defi-yields")]
    proposals = [{"id": "p_defi", "title": "Yield Aggregator", "demand": {"requests": 5, "requesters": 3},
                  "price": {"median": 0.02}}]
    return build(resources, assignments, leaves, proposals, **extra)


def test_a_leaf_splits_only_where_a_different_best_pick_wins():
    tree = finance_fixture()
    finance = node_of(tree, "finance")
    served = {leaf["id"]: leaf for leaf in finance["leaves"]}
    assert set(served) == {"finance.prices", "finance.candles", "finance.wallets", "finance.other"}
    winners = [leaf["picks"][0]["offering_id"] for leaf in finance["leaves"] if not leaf["remainder"]]
    assert len(winners) == len(set(winners))
    # defi has no proven or popular offering: merged up into the remainder, with its demand.
    other = served["finance.other"]
    assert other["remainder"] and other["empty"] and [c["id"] for c in other["covers"]] == ["finance.defi"]
    assert other["demand"][0]["title"] == "Yield Aggregator"
    # The dead seller is never offered, even with 500 payers.
    assert [e["offering_id"] for e in other["explore"]] == ["svc_u"]
    assert finance["best_pick"]["tier"] == "popular" and finance["tiers"]["bad"] == 1


def test_one_winner_or_none_keeps_the_node_whole():
    resources = [resource(1, "svc_a", payers=40, calls=100), resource(2, "svc_b", payers=2)]
    tree = build(resources, {"r:1": assign("search", "web-search"), "r:2": assign("search", "news")},
                 [leaf("search", "web", "web-search"), leaf("search", "news", "news")])
    search = node_of(tree, "search")
    assert [leaf["id"] for leaf in search["leaves"]] == ["search.all"]
    assert {c["id"] for c in search["leaves"][0]["covers"]} == {"search.web", "search.news"}
    nothing = node_of(build([resource(2, "svc_b", payers=2)], {"r:2": assign("search", "news")},
                            [leaf("search", "news", "news")]), "search")
    assert nothing["empty"] and nothing["first_to_try"]["offering_id"] == "svc_b"


def test_every_popular_offering_sits_on_a_leaf_even_before_it_is_classified():
    resources = [resource(1, "svc_a", payers=40, calls=100), resource(2, None, payers=0, sources=["mpp"])]
    tree = build(resources, {}, [])
    developer = node_of(tree, "developer")
    assert {p["offering_id"] for p in developer["leaves"][0]["picks"]} == {"svc_a", "res_2"}
    assert tree["stats"]["known_on_tree"] == tree["stats"]["known_offerings"] == 2


def test_without_reliability_tiers_come_from_outside_evidence():
    tree = finance_fixture(reliability=ct.ReliabilityView(error="OperationalError"))
    assert not tree["reliability"]["available"]
    assert "Reliability data is unavailable" in ct.render_l0(tree)
    assert node_of(tree, "finance")["best_pick"]["tier"] == "popular"


def test_a_covered_candidate_leaf_id_still_resolves():
    tree = finance_fixture()
    assert ct.find_entry(tree, "finance.defi")[1]["id"] == "finance.other"
    assert ct.find_entry(tree, "finance")[1] is None
    assert ct.find_entry(tree, "nope") is None


def test_explicit_capability_on_a_proposal_wins():
    proposals = [{"id": "p1", "title": "Flights", "capability": "travel", "demand": {"requests": 2}}]
    tree = build([resource(1, "svc_a")], {"r:1": assign("search", "web-search"), "p:p1": assign("search", "web-search")},
                 [leaf("search", "general", "web-search")], proposals)
    assert node_of(tree, "travel")["leaves"][0]["demand"][0]["id"] == "p1"
    assert node_of(tree, "search")["leaves"][0]["demand"] == []


# --- Media: the owner's split, with models -----------------------------------------


def video_parts():
    resources = [
        resource(1, None, payers=0, sources=["mpp"], price=400_000, name="StableStudio veo-3.1",
                 route="https://stablestudio.dev/api/generate/veo-3.1/generate", description="Veo 3.1 video generation"),
        resource(2, None, payers=0, sources=["mpp"], price=250_000, name="StableStudio veo-3.1-fast",
                 route="https://stablestudio.dev/api/generate/veo-3.1-fast/generate", description="Veo 3.1 Fast video generation"),
        resource(3, "svc_treza", payers=9, price=300_000, name="Treza video",
                 description="Generate a video, 4 to 15 seconds: veo-3.1, kling-3.0-pro, seedance-2.5, wan-2.7"),
        resource(4, None, payers=0, sources=["mpp"], price=500_000, name="StableStudio sora-2",
                 route="https://stablestudio.dev/api/generate/sora-2/generate", description="Sora 2 video generation"),
        resource(5, "svc_transcript", payers=7, price=10_000, name="YouTube transcript",
                 description="Transcript of a YouTube video"),
        resource(6, "svc_render", payers=1, price=50_000, name="Template renderer", description="Render a video template"),
        # Cheap and popular, but 480p: below the media minimums.
        resource(7, "svc_wan480", payers=11, calls=22, price=10_000, name="cleared.ink /wan/t2v/480p",
                 route="https://cleared.ink/wan/t2v/480p", description="Wan 2.6 text-to-video job (480p)"),
        # A prompt writer that names a model: not a generator.
        resource(8, "svc_prompts", payers=3, price=5_000, name="kino402 /v1/prompts/launch-teaser",
                 route="https://kino402.com/v1/prompts/launch-teaser", description="Launch teaser prompt for seedance-1.5-pro"),
        # A second seller of the top model.
        resource(9, "svc_aispace", payers=6, price=650_000, name="aispace video queue",
                 description="Async text-to-video on Venice: Seedance 2.5"),
    ]
    assignments = {f"r:{i}": assign("video", "text-to-video") for i in (1, 2, 3, 4, 7, 8, 9)}
    assignments.update({"r:5": assign("video", "video-transcript"), "r:6": assign("video", "template-render"),
                        "p:p_vid": assign("video", "text-to-video")})
    leaves = fixed("video", generate=["text-to-video"], edit=["template-render"], transcribe=["video-transcript"])
    proposals = [{"id": "p_vid", "title": "Exact Video Generator", "demand": {"requests": 3, "requesters": 2}}]
    return resources, assignments, leaves, proposals


def video_fixture(priors=None):
    resources, assignments, leaves, proposals = video_parts()
    return ct.assemble(resources=resources, assignments=assignments, leaves=leaves, proposals=proposals,
                       reliability=view(), built_at=NOW, version=3, model_priors=priors)


def prior(model, rating, rank=1):
    return {"model": model, "prior_rating": rating, "rank": rank, "source": "artificialanalysis:text-to-video",
            "source_url": "https://artificialanalysis.ai/video/leaderboard/text-to-video", "as_of": "2026-11-17"}


# W1's 3-5 scale (5 - Elo gap / 150).
PRIORS = {"wan-2.6": prior("wan-2.6", 5.0, 1), "seedance-2.5": prior("seedance-2.5", 4.88, 2),
          "sora-2": prior("sora-2", 4.7, 3), "veo-3.1": prior("veo-3.1", 4.3, 5),
          "kling-v3-pro": prior("kling-v3-pro", 3.84, 8), "veo-3.1-fast": prior("veo-3.1-fast", 3.6, 9)}


def test_media_nodes_serve_the_fixed_split_with_models_and_their_sellers():
    tree = video_fixture(PRIORS)
    video = node_of(tree, "video")
    assert [leaf["id"] for leaf in video["leaves"]] == ["video.generate", "video.edit", "video.transcribe"]
    assert video["ranking"] == "quality_first" and video["leaves"][2]["ranking"] == "value_first"
    generate = video["leaves"][0]
    models = [m["model"] for m in generate["models"]]
    # Best-rated first; the prompt writer's model is not a model on sale here.
    assert models[:4] == ["wan-2.6", "seedance-2.5", "sora-2", "veo-3.1"] and "seedance-1.5-pro" not in models
    assert generate["not_generators"] == 1 and generate["below_minimums"] == 1
    assert generate["models"][0]["below_minimums"] == 1 and generate["models"][0]["picks"] == []
    assert generate["demand"][0]["title"] == "Exact Video Generator"
    assert all(leaf["demand_count"] == len(leaf["demand"]) for leaf in video["leaves"])


def test_quality_first_picks_the_best_model_and_explores_its_other_sellers():
    tree = video_fixture(PRIORS)
    generate = node_of(tree, "video")["leaves"][0]
    picks = [(p["model"], p["name"]) for p in generate["picks"]]
    # wan-2.6 (5.0) has no seller that meets the minimums: Seedance 2.5 (4.88) is the best eligible model;
    # Sora 2 (4.7) is inside the 0.25 band; Veo 3.1 (4.3) is not, and no cheap low-rated model is offered.
    assert {m for m, _ in picks} == {"seedance-2.5", "sora-2"}
    assert picks[0] == ("seedance-2.5", "Treza video")  # equal delivery estimates: the cheaper seller of the band
    explore = [(e["model"], e["name"]) for e in generate["explore"]]
    assert explore == [("seedance-2.5", "aispace video queue")]  # the top model's other seller
    names = {p["name"] for p in generate["picks"] + generate["explore"]}
    assert not names & {"cleared.ink /wan/t2v/480p", "kino402 /v1/prompts/launch-teaser", "StableStudio veo-3.1-fast"}
    assert node_of(tree, "video")["best_pick"]["model"] == "seedance-2.5"
    text = ct.render_entry(tree, "video.generate")
    assert "Pick: Seedance 2.5 via Treza video, $0.30, new, rated 4.9/5" in text
    assert "Quality first: use the best model" in text
    assert "best: Seedance 2.5 via" in ct.render_l0(tree)


def test_a_model_view_names_its_sellers_by_delivery_then_price():
    tree = video_fixture(PRIORS)
    veo = next(m for m in node_of(tree, "video")["leaves"][0]["models"] if m["model"] == "veo-3.1")
    assert veo["sellers"] == 2 and veo["quality_prior"]["rating"] == 4.3
    # Both have Beta(3,1); the cheaper seller first.
    assert [p["name"] for p in veo["picks"]] == ["Treza video"]
    assert [e["name"] for e in veo["explore"]] == ["StableStudio veo-3.1"]
    text = ct.render_entry(tree, "video.generate.veo-3.1")
    assert "rated 4.3/5, #5 on text-to-video (2026-11-17)" in text and "Pick: Veo 3.1 via Treza video" in text


def test_value_first_ranks_only_offerings_of_quality_three_or_more():
    import dataclasses
    good = offering("good", {"payers": 12, "calls": 30}, 0.01)
    poor = offering("poor", {"payers": 12, "calls": 30}, 0.001)
    poor["estimate"] = dataclasses.replace(poor["estimate"], quality=2.0 / 5)
    ranked = vr.rank_value_first([good, poor], seed=1)
    assert [p["offering_id"] for p in ranked.picks] == ["good"] and ranked.eligible == 1


def test_media_minimums_read_the_offering_s_own_words():
    assert vr.meets_minimums("Wan Text-to-Video Job (480p)", "video") == (False, "at most 480p (video needs 720p)")
    assert vr.meets_minimums("a four-second 720p clip", "video")[0]
    assert not vr.meets_minimums("3s clip at 1080p", "video")[0]
    assert vr.meets_minimums("4 to 15 seconds, 16:9", "video")[0]
    assert not vr.meets_minimums("512x512 avatar", "image")[0] and vr.meets_minimums("up to 4K", "image")[0]
    assert vr.meets_minimums("Generate a video", "video") == (True, None)  # silence passes


def test_stored_c9_models_are_the_only_source_once_the_index_has_them():
    resources, assignments, leaves, proposals = video_parts()
    stored = {3: [{"model": "kling-v3-pro", "family": "kling", "modality": "video"}]}
    tree = ct.assemble(resources=resources, assignments=assignments, leaves=leaves, proposals=proposals,
                       reliability=view(), built_at=NOW, version=3, offering_models=stored)
    generate = node_of(tree, "video")["leaves"][0]
    assert [m["model"] for m in generate["models"]] == ["kling-v3-pro"]
    # Before the first sweep, W1's patterns read the seller's own words, model enums included.
    schema = {"schema": {"properties": {"body": {"properties": {"model": {"enum": ["sora-2-pro", "veo-3.1-fast"]}}}}}}
    assert [m["model"] for m in media_models.models_for(
        {"id": 9, "route_template": "https://x.example/v1/video", "input_schema": schema}, None)] == ["sora-2-pro", "veo-3.1-fast"]
    assert media_models.models_for({"id": 9, "route_template": "https://x.example/allowance"}, None) == []
    assert media_models.label("veo-3.1-fast") == "Veo 3.1 Fast" and media_models.label("gpt-image-1") == "GPT Image 1"


# --- Token budgets -----------------------------------------------------------------


def big_tree() -> dict:
    """Every node full: eight long-named leaves (the media nodes their fixed
    three), each with popular sellers, new ones and many models."""
    resources, assignments, leaves, proposals = [], {}, [], []
    names = ["veo-3.1", "veo-3.1-fast", "kling-v3-pro", "seedance-2.5", "sora-2-pro", "wan-2.6", "grok-imagine-video",
             "hailuo-2.3", "flux-1.1-pro", "gpt-image-1", "imagen-4", "seedream-4", "eleven-v3", "suno-v5"]
    rid = 0
    for node in ct.NODES:
        if not node.hint:
            continue
        slugs = ([s for s, _, _ in ct.FIXED_LEAVES[node.id]] if node.id in ct.FIXED_LEAVES
                 else [f"situation-number-{k}-with-a-long-name" for k in range(ct.MAX_LEAVES_PER_NODE)])
        leaves.extend({**leaf(node.id, s, f"facet-{k}"), "label": f"A rather long leaf label {k}"}
                      for k, s in enumerate(slugs))
        for k in range(len(slugs)):
            for j in range(12):
                rid += 1
                resources.append(resource(rid, f"svc_{rid}", payers=(40 - j) if j < 4 else 2, calls=200,
                                          name=f"An Extremely Verbose Seller Name {rid} /api/v1/{'x' * 30}",
                                          description=" ".join(names[(rid + i) % len(names)] for i in range(6))))
                assignments[f"r:{rid}"] = assign(node.id, f"facet-{k}")
            proposals.append({"id": f"p_{node.id}_{k}", "title": "A long proposal title " * 3,
                              "demand": {"requests": 9, "requesters": 4}})
            assignments[f"p:p_{node.id}_{k}"] = assign(node.id, f"facet-{k}")
    return build(resources, assignments, leaves, proposals, version=12)


def _tiktoken():
    try:
        import tiktoken
        return tiktoken.get_encoding("o200k_base")
    except Exception:
        return None


def test_l0_and_every_entry_fit_their_token_budgets():
    tree = big_tree()
    row = {"version": 12, "published": True, "tree": tree}
    enc = _tiktoken()
    l0 = ct.render_l0(tree)
    assert ct.token_bound(l0) <= ct.L0_TOKENS
    assert all(f"- {n.id}:" in l0 for n in ct.NODES if n.hint)  # one line per node, none dropped
    assert f"Hard rule: {ct.MEDIA_RULE}" in l0
    if enc:
        assert len(enc.encode(l0)) <= ct.L0_TOKENS
    ids = [n["id"] for n in tree["nodes"]] + [l["id"] for n in tree["nodes"] for l in n["leaves"]] + [
        m["id"] for n in tree["nodes"] for l in n["leaves"] for m in l.get("models") or []]
    assert len(ids) > 150 and any(".generate." in i for i in ids)
    for entry in ids:
        body = json.dumps(ct.entry_payload(row, entry), ensure_ascii=False)
        assert ct.token_bound(body) <= ct.ENTRY_TOKENS, entry
        if enc:
            assert len(enc.encode(body)) <= ct.ENTRY_TOKENS, entry


def test_the_token_bound_is_an_upper_bound_on_this_kind_of_text():
    enc = _tiktoken()
    if enc is None:
        pytest.skip("tiktoken is not installed")
    tree = big_tree()
    for text in [ct.render_l0(tree), *(ct.render_entry(tree, n["id"]) for n in tree["nodes"])]:
        assert len(enc.encode(text)) <= ct.token_bound(text)


def test_a_leaf_rendering_names_tiers_reasons_exploration_and_demand():
    text = ct.render_entry(finance_fixture(), "finance.prices")
    assert "Pick: svc_x r1, $0.01, popular" in text and "Why: popular: 40 payers on the x402 Bazaar in 30 days" in text
    assert "Explore: svc_v r5, $0.01, new" in text and "% of samples)" in text
    other = ct.render_entry(finance_fixture(), "finance.other")
    assert "No proven or popular offering yet" in other and "Demand (1): Yield Aggregator (5 requests)" in other


def test_a_version_stored_before_tiers_renders_as_it_did():
    legacy = {"format": "capability-tree-v1", "version": 5, "built_at": NOW.isoformat(), "policy": ct.FREE_FIRST_POLICY,
              "reliability": {"available": True}, "nodes": [
                  {"id": "search", "label": "Web search", "kind": "capability", "when_to_look": "Look.",
                   "when_not_to_buy": "Don't.", "best_pick": None, "empty": True, "leaves": [
                       {"id": "search.all", "label": "All web search", "when_to_look": "Look.", "covers": [],
                        "remainder": False, "picks": [], "candidates": [
                            {"service_id": "s", "name": "Exa", "price_usd": 0.007, "payers": 147, "liveness": "alive"}],
                        "candidate_count": 1, "demand": [], "demand_count": 0, "demand_requests": 0, "empty": True}]}]}
    assert "- search: Look. → nothing reliable yet" in ct.render_l0(legacy)
    text = ct.render_entry(legacy, "search")
    assert "Nothing reliable yet" in text and "Unverified candidates (1): Exa ($0.007, 147 payers)" in text
    assert f"Hard rule: {ct.MEDIA_RULE}" in ct.render_l0(legacy)


class ScriptedModel:
    """Answers like the model would: classifies by keywords, groups facets per node."""

    def __init__(self, fail: bool = False):
        self.calls: list[dict] = []
        self.fail = fail

    def __call__(self, system: str, payload: dict, usage: dict) -> dict:
        self.calls.append(payload)
        if self.fail:
            raise httpx.ConnectError("down")
        usage.update({"prompt_tokens": 1000, "completion_tokens": 200})
        if "items" in payload and "rule" in payload and "task" in payload:  # the media review
            return {"r": {str(item["i"]): ("llm:prompt-writing" if "prompt" in json.dumps(item).lower() else "keep")
                          for item in payload["items"]}}
        if "items" in payload:
            out = {}
            for item in payload["items"]:
                text = json.dumps(item).lower()
                if "video" in text:
                    out[str(item["i"])] = "video:text-to-video"
                elif "search" in text:
                    out[str(item["i"])] = "search:web-search|nonsense:x"
                elif "skip" in text:
                    continue
                else:
                    out[str(item["i"])] = "developer:misc-tools"
            return {"r": out}
        facets = [f["f"] for f in payload["facets"]]
        existing = payload["existing_leaves"]
        target = existing[0]["slug"] if existing else "general"
        return {"new_leaves": [] if existing else [{"slug": "general", "label": "General", "when_to_look": "When anything."}],
                "map": {f: target for f in facets}}


def items(*texts: str) -> list[ct.Item]:
    return [ct.Item(f"r:{i}", {"d": t}, f"fp{i}") for i, t in enumerate(texts)]


def test_an_item_the_model_skips_twice_is_stored_unclassified_not_resent(db):
    seed(db)
    with db() as conn:
        conn.execute("update index_resources set description = 'skip this one' where service_id = 'svc_dns'")
        conn.commit()
    model = ScriptedModel()
    report = ct.run(db, NOW, chat=model, reliability=ct.ReliabilityView(), settings=Settings())
    assert report["classify"]["unclassifiable"] == 1
    # One batch, then the retry of the skipped item alone.
    assert [len(c["items"]) for c in model.calls if "items" in c] == [4, 1]
    calls = len(model.calls)
    assert ct.run(db, NOW + timedelta(hours=1), chat=model, reliability=ct.ReliabilityView(),
                  settings=Settings())["pending"] == 0
    assert len(model.calls) == calls


def test_classify_batch_keeps_valid_pairs_and_leaves_the_rest_pending():
    spend = ct.Spend(model="gpt-5-mini", remaining_usd=1.0)
    result = ct.classify_batch(ScriptedModel(), spend, items("make a video", "web search", "skip me"), {})
    assert result == {"r:0": [{"node": "video", "facet": "text-to-video"}],
                      "r:1": [{"node": "search", "facet": "web-search"}]}  # the unknown node is dropped
    assert spend.calls == 1 and spend.usd == pytest.approx((1000 * 0.25 + 200 * 2.0) / 1e6)


def test_the_budget_stops_calls_before_they_are_made():
    model = ScriptedModel()
    spend = ct.Spend(model="gpt-5-mini", remaining_usd=0.0000001)
    with pytest.raises(ct.BudgetExhausted):
        ct.classify_batch(model, spend, items("a video"), {})
    assert model.calls == []
    # An unknown model is costed at the most expensive rate.
    assert ct.Spend(model="mystery", remaining_usd=1).price() == ct.UNKNOWN_MODEL_PRICE
    assert ct.Spend(model="gpt-5-mini-2025-08-07", remaining_usd=1).price() == ct.MODEL_PRICES["gpt-5-mini"]
    # A failed call is None (its items stay pending), not an empty answer.
    assert ct.classify_batch(ScriptedModel(fail=True), ct.Spend(model="gpt-5-mini", remaining_usd=1), items("x"), {}) is None


def test_slugs_and_schema_keys():
    assert ct.slug("Risk, compliance & verification checks", 30) == "risk-compliance-verification"
    assert ct._input_keys({"info": {"queryParams": {"q": "x"}}, "schema": {"properties": {
        "body": {"properties": {"prompt": {}, "seconds": {}}}}}}) == ["prompt", "seconds", "q"]


# --- Reliability (C3) --------------------------------------------------------------


def test_reliability_is_read_from_service_reliability_and_a_failure_is_unverified(db):
    seed(db)
    with db() as conn:
        conn.execute((ROOT / "supabase/reliability.sql").read_text())
        for sid, state in (("svc_vid", "reliable"), ("svc_find", "unreliable"), ("svc_dns", "unverified")):
            conn.execute("insert into service_reliability (service_id, state, rule_version, computed_at, reasons, inputs) "
                         "values (%s, %s, 'reliability-v1', now(), '[\"r\"]', '{}')", (sid, state))
        conn.commit()
        conn.execute("""update service_reliability set inputs = '{"judged_30d": {"total": 6, "delivered": 6},
                        "alpha": 9, "beta": 1, "tier": "proven", "big": "not loaded"}' where service_id = 'svc_vid'""")
        conn.commit()
    loaded = ct.load_reliability(db)
    assert loaded.available and loaded.rule_version == "reliability-v1"
    assert (loaded.state("svc_vid"), loaded.state("svc_find"), loaded.state("svc_dns")) == ("reliable", "unreliable", "unverified")
    # Only what the estimate reads comes back; v2's fields inside `inputs` are picked up.
    assert loaded.states["svc_vid"]["inputs"] == {"judged_30d": {"total": 6, "delivered": 6}, "alpha": 9, "beta": 1,
                                                  "tier": "proven"}
    assert vr.estimate({"liveness": "alive"}, loaded.states["svc_vid"]).source == "reliability-v2"

    @contextmanager
    def broken():
        raise psycopg.OperationalError("down")
        yield
    down = ct.load_reliability(broken)
    assert not down.available and down.states == {} and down.error == "OperationalError"


# --- The job, against PostgreSQL ---------------------------------------------------


@pytest.fixture
def db():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")
    schema = "tree_test_" + uuid4().hex
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(f"create schema {schema}")
        admin.execute(f"set search_path to {schema}")
        admin.execute((ROOT / "supabase/schema.sql").read_text())
        admin.execute((ROOT / "supabase/services-index.sql").read_text())
        # Idempotent: applying it twice changes nothing.
        admin.execute((ROOT / "supabase/capability-tree.sql").read_text())
        admin.execute((ROOT / "supabase/capability-tree.sql").read_text())

    @contextmanager
    def connect():
        with psycopg.connect(url, row_factory=dict_row, options=f"-c search_path={schema}") as conn:
            yield conn

    try:
        yield connect
    finally:
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(f"drop schema {schema} cascade")


def seed(connect, n: int = 3) -> None:
    with connect() as conn:
        for i, (sid, text) in enumerate([("svc_vid", "Generate a video from a prompt"),
                                         ("svc_find", "Web search for agents"),
                                         ("svc_dns", "DNS lookups")][:n]):
            conn.execute("insert into services (id, name, created_from_review) values (%s, %s, false)",
                         (sid, f"{sid} service"))
            conn.execute(
                """
                insert into index_resources (resource_key, method, origin, route_template, probe_url, payable,
                  networks, price_atomic, price_asset, bazaar_payers_30d, description, tracked, tracked_reason,
                  service_id)
                values (%s, 'POST', %s, %s, %s, true, '{eip155:8453}', 10000, %s, %s, %s, true, 'bazaar_payers', %s)
                """,
                (f"POST https://{sid}.example/v1", f"https://{sid}.example", f"https://{sid}.example/v1",
                 f"https://{sid}.example/v1", USDC, 10 + i, text, sid),
            )
        conn.execute(
            "insert into app_cache (key, payload, generated_at) values ('service_proposals', %s::jsonb, now())",
            (json.dumps({"version": 2, "proposals": [
                {"id": "p_vid", "title": "Validated Prompt Video", "problem": "video", "demand": {"requests": 3}}]}),),
        )
        conn.commit()


class Settings:
    openai_api_key = "test"
    openai_model = "gpt-5-mini"


def test_the_job_classifies_once_builds_versions_and_publishes(db, monkeypatch):
    monkeypatch.setenv("CROWDCODE_TREE_AUTO_PUBLISH", "1")  # a database with no gate
    seed(db)
    model = ScriptedModel()
    reliability = view(judged("svc_vid", 6, 6))
    report = ct.run(db, NOW, chat=model, reliability=reliability, settings=Settings())
    assert report["classify"]["classified"] == 4 and report["built"]["version"] == 1
    assert report["built"]["tiers"]["proven"] == 1 and report["spend"]["usd"] > 0

    with db() as conn:
        # The media nodes' fixed leaves exist, and the video facet was placed on one of them.
        assert {r["id"] for r in conn.execute("select id from capability_leaves where node = 'video'")} == {
            "video.generate", "video.edit", "video.transcribe"}
        current = ct.latest(conn, published_only=True)
        assert current["version"] == 1 and current["published"]
        summary = ct.summary_payload(current)
        video = next(n for n in summary["nodes"] if n["id"] == "video")
        assert video["best_pick"]["service_id"] == "svc_vid" and video["best_pick"]["tier"] == "proven"
        assert ct.token_bound(summary["l0"]) <= ct.L0_TOKENS
        # Search has only a new seller: no pick, and the agent is told what to try.
        search = ct.entry_payload(current, "search")
        assert "No proven or popular offering yet" in search["text"] and "Explore: svc_find service" in search["text"]
        assert "Validated Prompt Video" in ct.entry_payload(current, "video")["text"]
        spent = ct.spent_today(conn, NOW)
        assert spent == pytest.approx(report["spend"]["usd"])

    # Nothing new an hour later: no model call, no new version.
    calls = len(model.calls)
    later = ct.run(db, NOW + timedelta(hours=1), chat=model, reliability=reliability, settings=Settings())
    assert len(model.calls) == calls and later["built"] is None

    # New judged purchases change a tier and rebuild at once.
    changed = view(judged("svc_vid", 6, 6), judged("svc_find", 5, 5))
    assert ct.run(db, NOW + timedelta(hours=2), chat=model, reliability=changed,
                  settings=Settings())["built"]["trigger"] == "changed"

    # C3 unreachable: keep the version built with it rather than publish one without.
    down = ct.run(db, NOW + timedelta(hours=3), chat=model, reliability=ct.ReliabilityView(error="ConnectError"),
                  settings=Settings())
    assert down["built"] is None and "keeping" in down["skipped"]

    # The W8 seam: without auto-publish a build is only a candidate until published.
    monkeypatch.setenv("CROWDCODE_TREE_AUTO_PUBLISH", "0")
    nightly = ct.run(db, NOW + timedelta(hours=26), chat=model, reliability=changed, settings=Settings())
    assert nightly["built"]["trigger"] == "nightly"
    with db() as conn:
        assert ct.latest(conn, published_only=True)["version"] == 2
        candidate = ct.latest(conn, published_only=False)
        assert candidate["version"] == 3 and not candidate["published"]
        assert ct.publish(conn, 3, by="w8 gate", now=NOW)
        conn.commit()
        assert ct.latest(conn, published_only=True)["version"] == 3


def test_a_model_proposed_leaf_under_a_media_node_is_replaced_by_the_fixed_split(db):
    with db() as conn:
        conn.execute("insert into capability_leaves (id, node, slug, label, facets) values "
                     "('video.short-form', 'video', 'short-form', 'Short form', '{text-to-video}')")
        assert ct.ensure_fixed_leaves(conn, NOW) == 1
        rows = {r["id"]: r for r in conn.execute("select id, facets from capability_leaves")}
        assert set(rows) == {f"{n}.{s}" for n, fx in ct.FIXED_LEAVES.items() for s, _, _ in fx}
        assert ct.ensure_fixed_leaves(conn, NOW) == 0


def test_a_model_outage_is_reported_and_the_tree_still_builds(db, monkeypatch):
    monkeypatch.setenv("CROWDCODE_TREE_AUTO_PUBLISH", "1")
    seed(db)
    with pytest.raises(RuntimeError, match="model call"):
        ct.run(db, NOW, chat=ScriptedModel(fail=True), reliability=ct.ReliabilityView(), settings=Settings())
    with db() as conn:
        assert ct.latest(conn, published_only=True)["version"] == 1


def test_the_daily_cap_holds_across_runs(db, monkeypatch):
    seed(db)
    monkeypatch.setenv("CROWDCODE_TREE_DAILY_BUDGET_USD", "0.0001")
    model = ScriptedModel()
    report = ct.run(db, NOW, chat=model, reliability=ct.ReliabilityView(), settings=Settings())
    # The first call fits nowhere in a $0.0001 day: nothing is sent.
    assert model.calls == [] and report["built"]["classified_resources"] == 0


def test_old_versions_are_pruned_but_the_published_one_is_kept(db, monkeypatch):
    seed(db, 1)
    monkeypatch.setenv("CROWDCODE_TREE_AUTO_PUBLISH", "1")
    with db() as conn:
        ct.store_version(conn, {"nodes": []}, fingerprint="a", trigger="first", now=NOW, stats={})
        monkeypatch.setenv("CROWDCODE_TREE_AUTO_PUBLISH", "0")
        for i in range(ct.KEEP_VERSIONS + 3):
            ct.store_version(conn, {"nodes": []}, fingerprint=str(i), trigger="changed", now=NOW, stats={})
        versions = [r["version"] for r in conn.execute("select version from capability_tree_versions order by 1")]
        assert 1 in versions and len(versions) == ct.KEEP_VERSIONS + 1


# --- W8: the gate's publish seam and test fixtures ---------------------------------


def _store(conn, n: int, monkeypatch) -> list[int]:
    monkeypatch.setenv("CROWDCODE_TREE_AUTO_PUBLISH", "0")
    tree = video_fixture()
    return [ct.store_version(conn, tree, fingerprint=f"f{i}", trigger="nightly", now=NOW + timedelta(hours=i),
                             stats={"built": i}) for i in range(n)]


def test_the_gate_publishes_only_a_newer_real_candidate_and_keeps_its_evidence(db, monkeypatch):
    with db() as conn:
        v1, v2, v3 = _store(conn, 3, monkeypatch)
        assert ct.latest(conn, published_only=True) is None
        evidence = {"baseline": None, "candidate": v2, "tasks": [{"task": "free_math", "outcome": "pass"}]}
        assert ct.publish_gated(conn, v2, by="tree-gate 2026-11-18", evidence=evidence, now=NOW) == "published"
        current = ct.latest(conn, published_only=True)
        assert current["version"] == v2 and current["published_by"] == "tree-gate 2026-11-18"
        assert current["stats"]["gate"]["tasks"][0]["task"] == "free_math" and current["stats"]["built"] == 1
        # A retry is not an error; an older version is never served again by the gate.
        assert ct.publish_gated(conn, v2, by="again", evidence={}, now=NOW) == "already_published"
        assert ct.publish_gated(conn, v1, by="gate", evidence={}, now=NOW) == "not_newer"
        assert ct.publish_gated(conn, 99, by="gate", evidence={}, now=NOW) == "not_found"
        # Oversized evidence keeps the comparison and drops the task list.
        huge = {"decision": "publish", "tasks": [{"detail": "x" * 500}] * 40}
        assert ct.publish_gated(conn, v3, by="gate", evidence=huge, now=NOW) == "published"
        gate = ct.get_version(conn, v3)["stats"]["gate"]
        assert gate == {"truncated": True, "decision": "publish"}


def test_a_fixture_is_worse_advice_on_the_same_shape_and_is_never_published(db, monkeypatch):
    with db() as conn:
        [v1] = _store(conn, 1, monkeypatch)
        base = ct.get_version(conn, v1)
        fixture = ct.store_fixture(conn, v1, "buy_first", now=NOW + timedelta(days=1))
        row = ct.get_version(conn, fixture)
        assert fixture == v1 + 1 and not row["published"]
        assert row["trigger"] == f"fixture:buy_first:v{v1}"
        # Same fingerprint and build time: the hourly cron neither rebuilds for it nor counts it as fresh.
        assert row["fingerprint"] == base["fingerprint"] and row["built_at"] == base["built_at"]
        # Every node and leaf is still there; only the policy changed.
        assert [n["id"] for n in row["tree"]["nodes"]] == [n["id"] for n in base["tree"]["nodes"]]
        l0 = ct.render_l0(row["tree"])
        assert l0.splitlines()[1].startswith("Buy first") and "Free first" not in l0
        assert row["tree"]["version"] == fixture
        assert ct.summary_payload(row)["trigger"].startswith("fixture:")
        assert ct.latest(conn, published_only=False)["version"] == fixture
        assert ct.publish_gated(conn, fixture, by="gate", evidence={}, now=NOW) == "fixture"
        assert ct.latest(conn, published_only=True) is None
    dropped = ct.degrade(base["tree"], "drop_free_first")
    assert "free-first" not in [n["id"] for n in dropped["nodes"]]
    assert "Free first" not in ct.render_l0(dropped) and len(dropped["nodes"]) == len(base["tree"]["nodes"]) - 1
    with pytest.raises(ValueError, match="unknown fixture"):
        ct.degrade({"nodes": []}, "nonsense")


def test_the_publish_endpoint_needs_the_gate_token_and_a_well_formed_body(db, monkeypatch):
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from crowdcode import server

    with db() as conn:
        [v1, v2] = _store(conn, 2, monkeypatch)
        conn.commit()
    monkeypatch.setattr(server, "connect", db)
    client = TestClient(Starlette(routes=[Route("/api/internal/tree/publish", server.tree_publish, methods=["POST"])]))
    body = {"version": v2, "by": "tree-gate test", "evidence": {"decision": "publish"}}
    monkeypatch.delenv("CROWDCODE_TREE_GATE_TOKEN", raising=False)
    assert client.post("/api/internal/tree/publish", json=body).status_code == 404
    monkeypatch.setenv("CROWDCODE_TREE_GATE_TOKEN", "gate-token-0123456789abcdef0123456789")
    auth = {"authorization": "Bearer gate-token-0123456789abcdef0123456789"}
    assert client.post("/api/internal/tree/publish", json=body).status_code == 401
    assert client.post("/api/internal/tree/publish", json=body, headers={"authorization": "Bearer nope"}).status_code == 401
    for bad in [{**body, "version": "2"}, {**body, "version": True}, {**body, "by": " "}, {**body, "evidence": []}, []]:
        assert client.post("/api/internal/tree/publish", json=bad, headers=auth).status_code == 400
    ok = client.post("/api/internal/tree/publish", json=body, headers=auth)
    assert ok.status_code == 200 and ok.json() == {"ok": True, "result": "published", "version": v2}
    older = client.post("/api/internal/tree/publish", json={**body, "version": v1}, headers=auth)
    assert older.status_code == 409 and older.json()["result"] == "not_newer"
    with db() as conn:
        assert ct.latest(conn, published_only=True)["version"] == v2


# --- The standing media rule (C14) ---------------------------------------------------


def test_the_media_rule_is_in_l0_and_on_every_media_node_and_leaf():
    tree = video_fixture()
    rule = "deliver a real file from a model. Never substitute SVG, HTML, CSS or canvas animation, or a slideshow"
    assert rule in ct.render_l0(tree)
    assert tree["hard_rules"] == [ct.MEDIA_RULE]
    for entry in ("video", "video.generate", "video.generate.veo-3.1", "video.edit", "image", "audio"):
        assert f"Hard rule: {ct.MEDIA_RULE}" in ct.render_entry(tree, entry), entry
    assert "Hard rule" not in ct.render_entry(tree, "search")
    summary = ct.summary_payload({"version": 3, "published": True, "tree": tree})
    assert {n["id"] for n in summary["nodes"] if n.get("hard_rule")} == set(ct.MEDIA_NODES)


def test_a_version_stored_before_the_rule_still_shows_it():
    tree = video_fixture()
    del tree["hard_rules"]
    for node in tree["nodes"]:
        node.pop("hard_rule", None)
    assert f"Hard rule: {ct.MEDIA_RULE}" in ct.render_l0(tree)
    assert f"Hard rule: {ct.MEDIA_RULE}" in ct.render_entry(tree, "video")
    assert "Hard rule" not in ct.render_entry(tree, "finance")


def test_the_curated_catalog_and_model_sellers_are_classified_before_the_long_tail(db):
    seed(db)
    with db() as conn:
        conn.execute("""insert into index_resources (resource_key, method, origin, route_template, probe_url, payable,
                          sources, bazaar_payers_30d, description) values
                        ('POST https://studio.example/veo', 'POST', 'https://studio.example', 'https://studio.example/veo',
                         'https://studio.example/veo', true, '{mpp}', null, 'Veo 3.1 video generation')""")
        conn.commit()
        resources = ct.load_resources(conn)
        items = ct._pending_items(conn, resources, [], ct.ReliabilityView())
    # The curated listing, with no payers at all, comes before sellers with 10-12 payers.
    assert items[0].payload.get("url", "").startswith("studio.example/veo")


def test_the_media_review_moves_non_generators_out_once_and_new_items_are_marked(db):
    seed(db, 1)  # svc_vid: "Generate a video from a prompt" ... classified below
    with db() as conn:
        conn.execute("""insert into index_resources (resource_key, method, origin, route_template, probe_url, payable,
                          description, service_id) values ('POST https://kino.example/v1/prompts/teaser', 'POST',
                          'https://kino.example', 'https://kino.example/v1/prompts/teaser', 'https://kino.example/v1/prompts/teaser',
                          true, 'Writes a launch teaser script for seedance', null)""")
        rid = conn.execute("select id from index_resources where origin = 'https://kino.example'").fetchone()["id"]
        old = [{"node": "video", "facet": "text-to-video"}]  # classified before the rule: no check mark
        conn.execute("insert into capability_classifications (item_key, fingerprint, assignments) values (%s, 'x', %s::jsonb)",
                     (f"r:{rid}", json.dumps(old)))
        conn.commit()
    model = ScriptedModel()
    spend = ct.Spend(model="gpt-5-mini", remaining_usd=1.0)
    stats = ct.review_media(db, model, spend, NOW)
    assert stats["pending"] == 1 and stats["moved"] == 1
    with db() as conn:
        moved = conn.execute("select assignments from capability_classifications where item_key = %s",
                             (f"r:{rid}",)).fetchone()["assignments"]
    assert moved == [{"node": "llm", "facet": "prompt-writing", "check": ct.GENERATOR_CHECK}]
    # Checked once: a second pass sends nothing.
    calls = len(model.calls)
    assert ct.review_media(db, model, spend, NOW)["pending"] == 0 and len(model.calls) == calls
    # A new classification is made under the rule and marked as such.
    ct.run(db, NOW, chat=model, reliability=ct.ReliabilityView(), settings=Settings())
    with db() as conn:
        rows = conn.execute("select assignments from capability_classifications where item_key like 'r:%'").fetchall()
    assert all(a.get("check") == ct.GENERATOR_CHECK for r in rows for a in r["assignments"])
    assert ct.GENERATOR_RULE in json.dumps([c for c in model.calls if "nodes" in c and "rules" in c][0]["rules"])


def test_only_the_gate_publishes_unless_auto_publish_is_set(db, monkeypatch):
    monkeypatch.delenv("CROWDCODE_TREE_AUTO_PUBLISH", raising=False)
    seed(db)
    ct.run(db, NOW, chat=ScriptedModel(), reliability=ct.ReliabilityView(), settings=Settings())
    with db() as conn:
        assert ct.latest(conn, published_only=True) is None and ct.latest(conn, published_only=False)["version"] == 1
