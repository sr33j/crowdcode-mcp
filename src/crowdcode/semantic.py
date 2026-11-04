"""Semantic matching for `find_service` (SRI-86).

Keyword matching cannot see that "sell-side research reports" and "premium
financial newsletter research ... what top investors are buying" are the same
need, so the directory never offered Drip for it. Every candidate — a listing
or a catalog result — is embedded as one short document (name, description,
listing text, host and path words) and compared with the query by cosine
similarity, which `discovery` blends with keyword overlap and quality.

The model is **minishlab/potion-base-8M** (Model2Vec, MIT), a static
distillation of bge-base-en-v1.5, pinned to one revision and run in this
process:

- an embedding is a token lookup and a mean, so a query costs about 0.2 ms on
  the backend's half CPU and the whole directory embeds in about a second;
- it adds about 65 MB of memory to a 512 MB instance using ~180 MB;
- there is no API key, bill or third party on the search path, and the same
  vectors come out in CI and in production, so the evaluation set CI runs is
  the ranking production serves;
- on the evaluation set it comes within a few points of bge-small-en-v1.5,
  which needs onnxruntime and several times the memory, and of hosted models
  that would put a network call and a new secret on every search.

Loading is best effort and off the request path: until the model is ready,
or if it cannot be fetched, or with ``CROWDCODE_SEMANTIC_SEARCH=0``,
``encode`` returns None and search ranks by keywords alone, exactly as before.
"""

from __future__ import annotations

import os
import re
import threading
import time
import traceback
from collections import OrderedDict
from typing import Any, Callable, Sequence
from urllib.parse import urlsplit

import numpy as np

MODEL_REPO = "minishlab/potion-base-8M"
MODEL_REVISION = "bf8b056651a2c21b8d2565580b8569da283cab23"
DOCUMENT_MAX = 1200
CACHE_SIZE = 8192
RETRY_SECONDS = 300.0

_PATH_NOISE = frozenset("api apis v1 v2 v3 v4 v5 x402 mpp www https http com dev xyz io app".split())


def enabled() -> bool:
    return os.environ.get("CROWDCODE_SEMANTIC_SEARCH", "1").strip().lower() not in ("0", "false", "off", "no")


def endpoint_words(endpoint: str | None) -> str:
    """Host and path as words: `stablevoice.dev/api/speech` -> "stablevoice speech"."""
    if not endpoint:
        return ""
    parts = urlsplit(endpoint)
    words = re.findall(r"[a-z][a-z0-9]+", f"{parts.hostname or ''} {parts.path}".lower().replace("{", " {"))
    path_params = set(re.findall(r"\{([a-z0-9_]+)\}|:([a-z0-9_]+)", parts.path.lower()))
    params = {w for pair in path_params for w in pair if w}
    return " ".join(dict.fromkeys(w for w in words if w not in _PATH_NOISE and w not in params))


def document(name: str | None, description: str | None = None, listing_text: str | None = None,
             endpoint: str | None = None, slug: str | None = None) -> str:
    """The text a listing is matched on."""
    parts = [" ".join(str(p).split()).rstrip(". ") for p in (name, slug, description, listing_text, endpoint_words(endpoint)) if p]
    kept: list[str] = []
    for index, text in enumerate(parts):
        lowered = text.lower()
        # Said again inside a longer part (a description repeated in the listing text): once is enough.
        if len(text) >= 20 and any(lowered in other.lower() and (len(other) > len(text) or j < index)
                           for j, other in enumerate(parts) if j != index):
            continue
        kept.append(text)
    return ". ".join(kept)[:DOCUMENT_MAX]


class Embedder:
    """The model, loaded once in the background, and a cache of what it embedded."""

    def __init__(self, loader: Callable[[], Any] | None = None) -> None:
        self._loader = loader or _load_model
        self._model: Any = None
        self._lock = threading.Lock()
        self._loading = False
        self._failed_at: float | None = None
        self._cache: OrderedDict[str, np.ndarray] = OrderedDict()

    def ready(self) -> bool:
        return self._model is not None

    def warm(self, wait: bool = False) -> None:
        """Start loading the model if nothing has; ``wait`` blocks until it is done."""
        if self._model is not None:
            return
        with self._lock:
            if self._loading or (self._failed_at and time.monotonic() - self._failed_at < RETRY_SECONDS):
                start = False
            else:
                self._loading = start = True
        if start:
            if wait:
                self._load()
            else:
                threading.Thread(target=self._load, name="semantic-model", daemon=True).start()

    def _load(self) -> None:
        try:
            model = self._loader()
            with self._lock:
                self._model = model
                self._failed_at = None
        except Exception:
            traceback.print_exc()
            with self._lock:
                self._failed_at = time.monotonic()
        finally:
            with self._lock:
                self._loading = False

    def encode(self, texts: Sequence[str]) -> np.ndarray | None:
        """Unit vectors, one row per text; None while the model is unavailable."""
        if self._model is None:
            self.warm()
            return None
        missing = [t for t in dict.fromkeys(texts) if t not in self._cache]
        if missing:
            vectors = np.asarray(self._model.encode(list(missing), use_multiprocessing=False), dtype=np.float32)
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            vectors = vectors / np.where(norms == 0, 1.0, norms)
            with self._lock:
                for text, vector in zip(missing, vectors):
                    self._cache[text] = vector
                while len(self._cache) > CACHE_SIZE:
                    self._cache.popitem(last=False)
        with self._lock:
            rows = []
            for text in texts:
                vector = self._cache.get(text)
                if vector is None:  # evicted by a concurrent search between the two steps
                    return None
                self._cache.move_to_end(text)
                rows.append(vector)
        return np.vstack(rows) if rows else np.zeros((0, 1), dtype=np.float32)

    def similarities(self, query: str, documents: Sequence[str]) -> list[float] | None:
        """Cosine similarity of the query to each document, or None without the model."""
        if not documents:
            return []
        vectors = self.encode([query, *documents])
        if vectors is None:
            return None
        return [float(x) for x in vectors[1:] @ vectors[0]]


def _load_model() -> Any:
    from huggingface_hub import snapshot_download
    from model2vec import StaticModel

    path = snapshot_download(MODEL_REPO, revision=MODEL_REVISION)
    return StaticModel.from_pretrained(path, force_download=False)


EMBEDDER = Embedder()
