"""The search evaluation set, ranked offline with the real model (SRI-86, SRI-115 W10).

The owner asked for "sell-side reports / investment memos" and the search did
not offer Drip, the best-rated research source CrowdCode lists. These tests
fail when a change to listings, matching or ranking loses recall on 80 real
intents (tests/search_eval/cases.json), scored over the committed snapshot of
the index, directory and catalogs, so a regression shows up in the pull request
that causes it rather than in an owner's search.

Two paths are scored:

- **the index search** (C13, the default since SRI-115 W10): every offering in
  the services index, ranked by a seeded Thompson draw of value per dollar.
  Scored against `expected` plus the judged `also` (search_eval.py), averaged
  over several seeds, since a ranking that explores is a distribution.
- **the live fallback** (the federated search the index replaced, still used
  when the index has no match or has not loaded): the original 60 cases,
  against `expected` alone, with the floors it has always had.
"""

from __future__ import annotations

import os

import pytest

from crowdcode import semantic
from crowdcode.search_eval import evaluate, load_cases, route_key
from tests.search_eval.snapshot import load_snapshot, offline_search

# The live federated search on this snapshot: recall@3 0.661 and MRR 0.779 against
# `expected` (0.689 / 0.800 on the 2026-11-04 snapshot it was tuned on).
LIVE_RECALL_FLOOR = 0.63
LIVE_MRR_FLOOR = 0.75
# The index search, mean over SEEDS, against `expected` plus `also`: recall@3 0.740,
# MRR 0.837, hit@3 0.907 on the 80 cases over 47,831 offerings (the live search: 0.533,
# 0.765, 0.838 against the same labels; on the original 60, 0.726 / 0.828 / 0.893 against
# the live search's 0.589 / 0.833 / 0.950; quality_first trades a little of the labels' MRR
# for the better model). The floors leave room for a snapshot refresh.
INDEX_RECALL_FLOOR = 0.70
INDEX_MRR_FLOOR = 0.80
INDEX_HIT_FLOOR = 0.87
SEEDS = (20261117, 1, 2, 3, 4)
DRIP = "https://dripstack.xyz/api/v1/publications/{publicationSlug}/{postSlug}"
OWNER = "is there a service on CrowdCode that will get me really good sell-side reports / investment memos?"


@pytest.fixture(scope="module")
def snapshot():
    return load_snapshot()


@pytest.fixture(scope="module")
def model():
    semantic.EMBEDDER.warm(wait=True)
    if not semantic.EMBEDDER.ready():
        if os.environ.get("CI"):
            pytest.fail(f"could not load {semantic.MODEL_REPO}@{semantic.MODEL_REVISION}")
        pytest.skip("the embedding model is not available offline")
    return semantic.EMBEDDER


@pytest.fixture(scope="module")
def search(snapshot, model):
    return offline_search(snapshot, embedder=model)


@pytest.fixture(scope="module")
def live(snapshot, model):
    return offline_search(snapshot, embedder=model, live=True)


def original(cases):
    return [c for c in cases if c.get("source") != "index"]


def test_every_expected_service_is_in_the_index_or_the_directory(snapshot):
    cases = load_cases()
    assert 75 <= len(cases) <= 100
    assert len({c["id"] for c in cases}) == len(cases)
    known = {route_key(r["route_template"]) for r in snapshot["index"]["resources"]}
    known |= {route_key(r["probe_url"]) for r in snapshot["index"]["resources"]}
    known |= {route_key(r["canonical_endpoint"]) for r in snapshot["directory"] if r.get("canonical_endpoint")}
    # `also` may name results only the live catalogs returned; `expected` must be findable.
    missing = [(c["id"], e) for c in cases for e in c["expected"] if route_key(e) not in known]
    assert missing == []


def test_index_search_recall_and_mrr_do_not_regress(snapshot, model):
    cases = load_cases()
    summaries = [evaluate(cases, offline_search(snapshot, embedder=model, seed=seed)) for seed in SEEDS]
    recall = sum(s.recall_at_3 for s in summaries) / len(summaries)
    mrr = sum(s.mrr for s in summaries) / len(summaries)
    hit = sum(s.hit_at_3 for s in summaries) / len(summaries)
    print("\n" + summaries[0].table())
    print(f"mean over {len(SEEDS)} seeds: recall@3 {recall:.3f}  MRR {mrr:.3f}  hit@3 {hit:.3f}")
    assert recall >= INDEX_RECALL_FLOOR, summaries[0].table()
    assert mrr >= INDEX_MRR_FLOOR, summaries[0].table()
    assert hit >= INDEX_HIT_FLOOR, summaries[0].table()


def test_index_search_beats_the_live_search_it_replaced(snapshot, model, live):
    cases = load_cases()
    old = evaluate(cases, live)
    new = [evaluate(cases, offline_search(snapshot, embedder=model, seed=seed)) for seed in SEEDS]
    assert sum(s.recall_at_3 for s in new) / len(new) > old.recall_at_3
    assert sum(s.mrr for s in new) / len(new) > old.mrr


def test_live_fallback_recall_and_mrr_do_not_regress(live):
    summary = evaluate(original(load_cases()), live, widened=False)
    print("\n" + summary.table())
    assert summary.recall_at_3 >= LIVE_RECALL_FLOOR, summary.table()
    assert summary.mrr >= LIVE_MRR_FLOOR, summary.table()


@pytest.mark.parametrize("query", ["sell-side research reports", OWNER])
def test_the_live_fallback_still_offers_drip_for_the_owners_question(live, query):
    assert route_key(DRIP) in [route_key(e) for e in live(query)[:3]]


def test_the_index_offers_drip_for_the_owners_question(snapshot, model):
    # Among ~48k offerings Drip's own words match the question only weakly (it sells newsletter posts);
    # CrowdCode's directory listing keeps it in the first screen of every draw.
    for seed in SEEDS:
        ranked = [route_key(e) for e in offline_search(snapshot, embedder=model, seed=seed)(OWNER)]
        assert route_key(DRIP) in ranked[:6], (seed, ranked[:6])


def test_the_tickets_other_examples(snapshot, model):
    send = next(c for c in load_cases() if c["id"] == "send-email")
    senders = {route_key(e) for e in [*send["expected"], *send.get("also", [])]}
    for seed in SEEDS:
        search = offline_search(snapshot, embedder=model, seed=seed)
        top = [route_key(e) for e in search("send an email")[:3]]
        # The draw may lead with another sender; whatever leads sends email, and StableEmail is there.
        assert top[0] in senders and route_key("https://stableemail.dev/api/send") in top, (seed, top)
        speech = {"https://stablevoice.dev/api/speech", "https://agent402.tools/api/tts", "https://agent402.tools/api/tts-lite",
                  "https://x402.forgemesh.io/speech-generator", "https://x402.forgemesh.io/tts"}
        assert speech & set(search("text to speech mp3")[:3]), seed


def test_a_video_request_gets_video_generators_not_transcript_tools(snapshot, model):
    """C13's example: "generate a 10 second video" is answered by sellers of video models."""
    case = next(c for c in load_cases() if c["id"] == "video-10s")
    generators = {route_key(e) for e in [*case["expected"], *case.get("also", [])]}
    for seed in SEEDS:
        top = [route_key(e) for e in offline_search(snapshot, embedder=model, seed=seed)("generate a 10 second video")[:5]]
        assert sum(1 for e in top if e in generators) >= 3, (seed, top)
        assert not any("transcri" in e for e in top), (seed, top)


def test_without_the_model_search_still_works(snapshot):
    cases = load_cases()
    keyword_only = evaluate(cases, offline_search(snapshot, embedder=None))
    # Keywords alone over the index (the model not yet loaded): still useful, if well below the model.
    assert keyword_only.recall_at_3 >= 0.35, keyword_only.table()
    live_keywords = evaluate(original(cases), offline_search(snapshot, embedder=None, live=True), widened=False)
    # The live fallback with keywords only: no worse than before SRI-86.
    assert live_keywords.recall_at_3 >= 0.45


def test_a_cinematic_video_gets_top_model_sellers_not_a_cheap_clip(snapshot, model):
    """C13 amendment: Veo, Kling, Sora or Seedance sellers first; never a $0.01 480p clip or a prompt writer."""
    case = next(c for c in load_cases() if c["id"] == "video-cinematic")
    premium = {route_key(e) for e in case["expected"]}
    for seed in SEEDS:
        top = [route_key(e) for e in offline_search(snapshot, embedder=model, seed=seed)(case["query"])[:3]]
        assert set(top) <= premium, (seed, top)
        assert not any("480p" in e or "prompt" in e for e in top), (seed, top)
