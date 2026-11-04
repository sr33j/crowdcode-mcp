"""The directory search evaluation set, ranked offline with the real model (SRI-86).

The owner asked for "sell-side reports / investment memos" and the search did
not offer Drip, the best-rated research source CrowdCode lists. These tests
fail when a change to listings, matching or ranking loses recall on 60 real
intents (tests/search_eval/cases.json), scored over the committed snapshot of
the directory and catalogs, so a regression shows up in the pull request that
causes it rather than in an owner's search.
"""

from __future__ import annotations

import os

import pytest

from crowdcode import semantic
from crowdcode.search_eval import evaluate, load_cases
from tests.search_eval.snapshot import load_snapshot, offline_search

# On this snapshot: recall@3 0.458 and MRR 0.642 before SRI-86 (what
# production measured live that day), 0.689 and 0.800 after. The floors sit
# about two cases under that, so a snapshot refresh's noise passes and a real
# regression does not; raise them when a change earns it.
RECALL_FLOOR = 0.66
MRR_FLOOR = 0.77
DRIP = "https://dripstack.xyz/api/v1/publications/{publicationSlug}/{postSlug}"


@pytest.fixture(scope="module")
def snapshot():
    return load_snapshot()


@pytest.fixture(scope="module")
def search(snapshot):
    semantic.EMBEDDER.warm(wait=True)
    if not semantic.EMBEDDER.ready():
        if os.environ.get("CI"):
            pytest.fail(f"could not load {semantic.MODEL_REPO}@{semantic.MODEL_REVISION}")
        pytest.skip("the embedding model is not available offline")
    return offline_search(snapshot, embedder=semantic.EMBEDDER)


@pytest.fixture(scope="module")
def summary(search):
    return evaluate(load_cases(), search)


def test_every_expected_service_is_in_the_directory(snapshot):
    listed = {row["canonical_endpoint"] for row in snapshot["directory"]}
    cases = load_cases()
    assert 40 <= len(cases) <= 60
    assert len({c["id"] for c in cases}) == len(cases)
    missing = [(c["id"], e) for c in cases for e in c["expected"] if e not in listed]
    assert missing == []


def test_recall_and_mrr_do_not_regress(summary):
    print("\n" + summary.table())
    assert summary.recall_at_3 >= RECALL_FLOOR, summary.table()
    assert summary.mrr >= MRR_FLOOR, summary.table()


@pytest.mark.parametrize("query", [
    "sell-side research reports",
    "is there a service on CrowdCode that will get me really good sell-side reports / investment memos?",
])
def test_drip_answers_the_owners_sell_side_question(search, query):
    assert DRIP in search(query)[:3]


def test_the_tickets_other_examples(search):
    assert search("send an email")[0] == "https://stableemail.dev/api/send"
    speech = {"https://stablevoice.dev/api/speech", "https://agent402.tools/api/tts", "https://agent402.tools/api/tts-lite",
              "https://x402.forgemesh.io/speech-generator", "https://x402.forgemesh.io/tts"}
    assert speech & set(search("text to speech mp3")[:3])


def test_without_the_model_search_still_works(snapshot):
    keyword_only = evaluate(load_cases(), offline_search(snapshot, embedder=None))
    # Keywords over the new descriptions alone: no worse than before SRI-86.
    assert keyword_only.recall_at_3 >= 0.45
