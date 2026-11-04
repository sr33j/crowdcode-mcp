"""Semantic matching for find_service (SRI-86), with a stand-in model."""

from __future__ import annotations

import threading

import numpy as np

from crowdcode import semantic


class BagOfWords:
    """A deterministic toy model: one dimension per vocabulary word."""

    VOCAB = ["research", "report", "analyst", "investor", "newsletter", "speech", "audio", "email", "send", "search", "web"]
    SYNONYMS = {"sell": "analyst", "memo": "report", "voice": "speech", "mp3": "audio", "tts": "speech"}

    def encode(self, texts, use_multiprocessing=False):
        rows = []
        for text in texts:
            vector = np.zeros(len(self.VOCAB), dtype=np.float32)
            for word in text.lower().replace("-", " ").split():
                word = self.SYNONYMS.get(word.strip(".,?"), word.strip(".,?"))
                for index, known in enumerate(self.VOCAB):
                    if word.startswith(known):
                        vector[index] += 1
            rows.append(vector)
        return np.vstack(rows)


def embedder():
    model = semantic.Embedder(loader=BagOfWords)
    model.warm(wait=True)
    return model


def test_document_is_name_description_listing_text_and_endpoint_words():
    text = semantic.document("Drip", "Financial newsletter research.", "Financial newsletter research. | Stock picks.",
                             "https://dripstack.xyz/api/v1/publications/{publicationSlug}/{postSlug}")
    assert text == "Drip. Financial newsletter research. | Stock picks. dripstack publications"
    assert semantic.endpoint_words("https://api.tickersfeed.net/sec/:symbol/filings") == "tickersfeed net sec filings"


def test_encode_returns_unit_vectors_and_caches():
    model = embedder()
    vectors = model.encode(["sell-side research memo", "sell-side research memo", "unrelated words"])
    assert vectors.shape == (3, len(BagOfWords.VOCAB))
    assert np.allclose(np.linalg.norm(vectors[:2], axis=1), 1.0)
    assert np.allclose(vectors[2], 0.0)  # nothing known: a zero vector, never a division by zero
    similarities = model.similarities("sell-side research memo", ["Analyst research reports", "Send an email"])
    assert similarities[0] > 0.8 and similarities[1] == 0.0


def test_without_the_model_nothing_is_embedded_and_loading_starts_in_the_background():
    started = threading.Event()
    release = threading.Event()

    def slow_loader():
        started.set()
        release.wait(5)
        return BagOfWords()

    model = semantic.Embedder(loader=slow_loader)
    assert model.encode(["web search"]) is None  # never blocks a search
    assert started.wait(5)
    assert model.similarities("web search", ["Search the web"]) is None
    release.set()
    for _ in range(100):
        if model.ready():
            break
        threading.Event().wait(0.02)
    assert model.similarities("web search", ["Search the web"])[0] > 0.9


def test_a_failed_load_is_retried_later_not_on_every_search(monkeypatch):
    calls = []

    def broken():
        calls.append(1)
        raise OSError("no network")

    model = semantic.Embedder(loader=broken)
    model.warm(wait=True)
    model.warm(wait=True)
    assert calls == [1] and not model.ready()


def test_disabled_by_environment(monkeypatch):
    monkeypatch.setenv("CROWDCODE_SEMANTIC_SEARCH", "0")
    assert not semantic.enabled()
    monkeypatch.setenv("CROWDCODE_SEMANTIC_SEARCH", "1")
    assert semantic.enabled()
