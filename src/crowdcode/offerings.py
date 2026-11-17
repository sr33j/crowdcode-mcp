"""Search and ranking over the whole services index (SRI-115 contract C13).

`/api/services/find` used to run a live federated search (the Bazaar's top
12, mpp.dev, CrowdCode's reviewed listings) on every request. It now searches
every offering in the index (C1, C9: listed and tracked, from every source) in
this process, and ranks the ones that match by value per dollar:

1. **Match.** Every offering is one short document (name, description, the
   input schema's field names, CrowdCode's listing text, host and path words),
   matched like the directory always was (`discovery.match_strength`): shared
   words and the pinned static embedding (semantic.py). A CSR word index and
   one matrix product keep a query over ~48k offerings at a few milliseconds.
   CrowdCode's listed services are matched among themselves too, as the
   directory was, so a reviewed service is not lost among 48k listings.
2. **What was asked for.** A query that names a model (`veo 3.1`, `seedance`,
   C9) is answered by that model's sellers. Otherwise the strongest matches and
   the leaves' own labels agree on a capability leaf (C7); offerings classified
   into another leaf are not part of the answer ("generate a video" is not
   answered by a transcript tool that mentions videos). Untracked offerings,
   which C7 does not classify, take the leaf of their nearest classified ones.
3. **Value per dollar** (C13). Within the matching set, each offering's
   delivery is drawn from its posterior ``Beta(alpha, beta)`` (C10, read through
   W6's `value_ranking.estimate`, so the tree and search share one definition),
   multiplied by its quality on [0, 1] and by its fit (the match, sharpened),
   and divided by its effective price: the price on the caller's best network,
   a transfer when that balance is short, and what the call costs the agent
   itself. Every offering with evidence competes; only the EXPLORE_SLOTS most
   relevant with none are drawn against them, or a hundred uniform draws would
   put an unknown first on nearly every request. ``bad`` is never offered. The
   draw is seeded per request, so one seed always reproduces one list;
   ``explore`` marks a result the draw lifted above an offering whose posterior
   mean would have ranked first.

Nothing here touches the database on the request path: `SharedIndex` loads the
index in the background every 15 minutes and swaps it in whole.
"""

from __future__ import annotations

import bisect
import hashlib
import math
import re
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Sequence

import numpy as np

from crowdcode import semantic, value_ranking
from crowdcode.review_sql import directory_listed

# --- Networks and prices -------------------------------------------------------------

_CAIP_NETWORKS = {
    "eip155:8453": "base",
    "solana:5eykt4usfv8p8njdtrepy1vzqkqzkvdp": "solana",
    "eip155:4217": "tempo",
}
# The USDC each network's payers hold (Tempo: USDC.e), lower-cased.
_USDC = frozenset({
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    "epjfwdd5aufqssqem2qn1xzybapc8g4wegggkzwytdt1v",
    "0x20c000000000000000000000b9537d11c60e8b50",
})
USDC_DECIMALS = 6
# An offering CrowdCode knows is payable but whose network no catalog names: any of the caller's.
UNKNOWN_NETWORK = "unknown"

# --- Ranking constants -----------------------------------------------------------------

# Admission, as the directory search was tuned (discovery.SEMANTIC_*): a listing joins
# on half the query's words, or on a similarity within the gap of the best and above the floor.
KEYWORD_ADMIT = 0.5
# Offerings considered per query after admission, strongest match first.
CANDIDATE_LIMIT = 400
# The matching set: within MATCH_BAND of the strongest match and at least MATCH_FLOOR.
MATCH_BAND = 0.2
MATCH_FLOOR = 0.35
# Below this the index has nothing for the query and the live catalogs are asked instead.
INDEX_FLOOR = 0.35
# The strongest VOTERS classified matches vote for a leaf; it must hold LEAF_SHARE of the vote.
VOTERS = 30
LEAF_SHARE = 0.4
# With leaf labels to compare: a leaf needs this share of the vote, and its label's similarity to
# the query counts LEAF_LABEL_WEIGHT times as much as its share.
LEAF_MIN_SHARE = 0.15
LEAF_LABEL_WEIGHT = 2.0
# A leaf the matches barely voted for still wins on a label this close to the query.
LEAF_LABEL_ALONE = 0.6
LEAF_LABEL_MARGIN = 0.15
# An unclassified offering whose neighbours do not agree takes the leaf whose label it is closest to, this close.
INFER_LABEL_SIMILARITY = 0.5
# An offering with no leaf of its own joins a detected leaf's set only on a match this close to the best.
UNCLASSIFIED_BAND = 0.05
# What one call costs the agent besides the price: inspecting the endpoint, building the request and
# reading the answer take model tokens and turns (a few cents on the hosted models). It is part of
# the effective price value is divided by, so a $0.001 seller is not "ten times better value" than a
# $0.01 one when either call really costs about five cents. Chosen on tests/search_eval.
CALL_OVERHEAD_USD = 0.05
# A listed offering's match is scaled so the best listed match counts as the best overall, by at most this.
LISTED_SCALE = 2.0
# Value is delivery x quality x fit per dollar; fit is the match raised to this power.
FIT_POWER = 12.0
# Offerings with no evidence at all (a Beta(1,1) prior, no judged purchase, no review) drawn per request.
EXPLORE_SLOTS = 2
# Untracked offerings have no classification (C7 classifies tracked ones); each takes the leaf its
# INFER_NEIGHBOURS most similar classified offerings agree on, when they are this similar.
INFER_NEIGHBOURS = 7
INFER_MIN_SIMILARITY = 0.6
INFER_SHARE = 0.6
# Moving USDC between one's own networks (Relay) costs about $0.02-0.03 (discovery.TRANSFER_COST_USD).
TRANSFER_COST_USD = 0.03
# Results carry the first reason, capped for the agent's context.
REASON_MAX = 80
# Description characters kept per offering (~50k of them live in memory).
DESCRIPTION_STORED = 200
NAME_MAX = 48
DESCRIPTION_MAX = 60

# C13 amendment (2026-11-17): where outputs differ in quality more than in price, quality decides.
QUALITY_FIRST_NODES = frozenset({"video", "image", "audio", "llm"})
QUALITY_FIRST_MODALITIES = frozenset({"video", "image", "audio", "music", "llm"})
# Leaves whose offerings must return the artifact itself (a file, or a job that yields one).
ARTIFACT_LEAVES = frozenset({"video.generate", "video.edit", "image.generate", "image.edit", "audio.tts", "audio.music",
                             "llm.conversational-chat"})
LEAF_MODALITY = {"video.generate": {"video"}, "video.edit": {"video"}, "image.generate": {"image"},
                 "image.edit": {"image"}, "audio.tts": {"audio"}, "audio.music": {"music"},
                 "llm.conversational-chat": {"llm"}}
_ARTIFACT_WORDS = {"video": "video|t2v|i2v|clip|movie|animat", "image": "image|img|photo|picture|t2i|i2i",
                   "audio": "audio|speech|tts|voice|sound|music|song", "llm": "chat|completion|llm|inference|messages"}
_EDIT = re.compile(r"edit|image2image|img2img|i2i|inpaint|outpaint|retouch|upscal|remove", re.I)
# Price breaks ties only within a quality band this wide (±0.25).
QUALITY_BAND = 0.5
# value_first ranks only offerings at least this good on the judge's 1-5 scale.
VALUE_MIN_QUALITY = 3.0

TIERS = ("proven", "popular", "new", "bad")
_GOOD_TIERS = frozenset({"proven", "popular"})

NEUTRAL_QUALITY = 3.0  # the judge's 1-5 scale; an offering nobody rated sits in the middle


def prior(payers: int | None, calls: int | None, curated: bool) -> tuple[float, float, str]:
    """C10's outside-evidence prior, by reliability-v2's own rule: ``(alpha, beta, kind)``."""
    from crowdcode import reliability

    found = reliability.prior_for({"bazaar_payers_30d": payers, "bazaar_calls_30d": calls, "curated": curated})
    return float(found["alpha"]), float(found["beta"]), str(found.get("kind") or "none")


def quality_unit(rating: float | None) -> float:
    """A 1-5 quality as a share of the best: ``(q - 1) / 4``, never quite zero."""
    if rating is None or not math.isfinite(rating):
        rating = NEUTRAL_QUALITY
    return min(1.0, max(0.02, (rating - 1.0) / 4.0))


def network_name(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return _CAIP_NETWORKS.get(text) or (text if text in ("base", "solana", "tempo") else None)


def usd(amount: Any, asset: Any) -> float | None:
    """A USDC atomic amount in dollars; None for another asset or no amount."""
    if amount is None or str(asset or "").lower() not in _USDC:
        return None
    try:
        value = Decimal(str(amount)) / (Decimal(10) ** USDC_DECIMALS)
    except Exception:
        return None
    return float(value) if value >= 0 else None


# --- Text -------------------------------------------------------------------------------

_STOPWORDS = frozenset(
    "a an and any api apis are as at be best buy by call check current data details does find "
    "fetch for from get give how i info information in into is it latest list look lookup me my "
    "name names need of on or paid please query request return service services some that the "
    "this to tool use using via want what which with".split()
)


def tokens(text: str) -> list[str]:
    """The query's subject words (discovery.tokens)."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    return [w for w in dict.fromkeys(words) if len(w) >= 3 and w not in _STOPWORDS]


def schema_words(schema: Any, limit: int = 120) -> str:
    """The input's field names and their short descriptions, from a listing's input schema."""
    if not isinstance(schema, dict):
        return ""
    names: list[str] = []
    info = schema.get("info") if isinstance(schema.get("info"), dict) else schema
    for key in ("bodyFields", "queryParams", "body", "query"):
        block = info.get(key) if isinstance(info, dict) else None
        if isinstance(block, dict):
            for name, spec in block.items():
                names.append(str(name))
                if isinstance(spec, dict) and isinstance(spec.get("description"), str):
                    names.append(spec["description"][:60])
    properties = ((schema.get("schema") or {}).get("properties") or {}).get("body") if isinstance(schema.get("schema"), dict) else None
    if isinstance(properties, dict) and isinstance(properties.get("properties"), dict):
        names.extend(str(n) for n in properties["properties"])
    text = " ".join(dict.fromkeys(n.replace("_", " ") for n in names if n))
    return text[:limit]


# --- The offerings ------------------------------------------------------------------------


@dataclass(slots=True)
class Offering:
    id: int
    method: str
    endpoint: str
    name: str | None = None
    description: str | None = None
    schema_text: str = ""
    # CrowdCode's own listing text for the service, when its directory lists it.
    listing_text: str | None = None
    # CrowdCode's directory lists the service (reviewed, or one of our hosted services).
    listed: bool = False
    networks: tuple[str, ...] = ()
    protocol: str | None = None
    payable: bool = False
    price_usd: float | None = None
    payers: int | None = None
    calls: int | None = None
    sources: tuple[str, ...] = ()
    tracked: bool = False
    liveness: str = "unknown"
    service_id: str | None = None
    leaves: tuple[str, ...] = ()
    # Inferred from the nearest classified offerings when it has no classification of its own.
    inferred: tuple[str, ...] = ()
    model: str | None = None
    model_family: str | None = None
    modality: str | None = None
    # The kinds of artifact its models produce (C9: video, image, audio, music, llm, embedding).
    modalities: tuple[str, ...] = ()
    # Every model the offering sells, with each one's quality prior (C9): {model: (family, prior_rating)}.
    models: dict[str, tuple[str | None, float | None]] | None = None
    # Reliability (C10): the tier, the delivery posterior, quality on the judge's 1-5 scale, the first reason.
    tier: str = "new"
    alpha: float = 1.0
    beta: float = 1.0
    quality: float | None = None
    reason: str | None = None
    # Anything beyond a flat prior: payers, the curated catalog, judged purchases or CrowdCode reviews.
    evidenced: bool = False
    # The index row it was built from, hashed: an unchanged row keeps its Offering across refreshes.
    fingerprint: int = 0

    def document(self) -> str:
        listing = " ".join(filter(None, [self.listing_text, self.schema_text]))
        return semantic.document(self.name, self.description, listing or None, self.endpoint)

    @property
    def key(self) -> str:
        return f"r:{self.id}"

    @property
    def capabilities(self) -> tuple[str, ...]:
        return self.leaves or self.inferred

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)


@dataclass
class Match:
    offering: Offering
    keyword: float
    similarity: float | None
    strength: float


class OfferingIndex:
    """Every offering, its document, an inverted word index and the embedding matrix."""

    def __init__(self, offerings: Sequence[Offering], embedder: semantic.Embedder | None = None,
                 vectors: np.ndarray | None = None, built_at: float | None = None,
                 leaf_text: dict[str, str] | None = None, leaf_vectors: np.ndarray | None = None) -> None:
        self.offerings = list(offerings)
        # Each leaf's label and when-to-look (C7), for telling which one a query asks for.
        self.leaf_ids = sorted(leaf_text or {})
        self.leaf_vectors = leaf_vectors
        if self.leaf_vectors is None and embedder is not None and self.leaf_ids:
            self.leaf_vectors = embedder.encode_uncached([leaf_text[i] for i in self.leaf_ids])
        self.built_at = built_at or time.time()
        self._ids = np.fromiter((o.id for o in self.offerings), dtype=np.int64, count=len(self.offerings))
        self._listed = np.fromiter((o.listed for o in self.offerings), dtype=bool, count=len(self.offerings))
        self.by_endpoint: dict[tuple[str, str], int] = {}
        self.by_url: dict[str, int] = {}
        for i, o in enumerate(self.offerings):
            self.by_endpoint.setdefault((o.method, o.endpoint), i)
            self.by_url.setdefault(o.endpoint, i)
        self._vocabulary: list[str] = []
        self._offsets = np.zeros(1, dtype=np.int64)
        self._postings = np.zeros(0, dtype=np.int32)
        self._build_words()
        self.vectors = vectors
        if self.vectors is None and embedder is not None:
            self.vectors = embedder.encode_uncached([o.document() for o in self.offerings])
        if self.semantic:
            self._infer_leaves()
            self._infer_from_labels()
        # Offering ids by the model, and the model family, they sell (C9).
        self.models: dict[str, set[int]] = {}
        self.families: dict[str, set[int]] = {}
        for o in self.offerings:
            for model, (family, _) in (o.models or {}).items():
                self.models.setdefault(model, set()).add(o.id)
                if family:
                    self.families.setdefault(family, set()).add(o.id)

    def __len__(self) -> int:
        return len(self.offerings)

    @property
    def semantic(self) -> bool:
        return self.vectors is not None and len(self.vectors) == len(self.offerings)

    def producers(self, leaf: str) -> list[Offering]:
        """Every offering that returns a leaf's artifact (classified there, or selling a model of its
        modality), whatever its words: "generate a video" considers every video generator."""
        cache = self.__dict__.setdefault("_producers", {})
        if leaf not in cache:
            cache[leaf] = [o for o in self.offerings if returns_artifact(o, leaf)]
        return cache[leaf]

    def reassess(self) -> None:
        """Re-read leaves and models after their classification changed, without rebuilding."""
        self.__dict__.pop("_producers", None)
        for o in self.offerings:
            o.inferred = ()
        if self.semantic:
            self._infer_leaves()
            self._infer_from_labels()
        self.models, self.families = {}, {}
        for o in self.offerings:
            for model, (family, _) in (o.models or {}).items():
                self.models.setdefault(model, set()).add(o.id)
                if family:
                    self.families.setdefault(family, set()).add(o.id)

    def _infer_leaves(self) -> None:
        """A leaf for each unclassified offering, from the classified offerings most like it."""
        classified = [i for i, o in enumerate(self.offerings) if o.leaves]
        others = [i for i, o in enumerate(self.offerings) if not o.leaves]
        if not classified or not others:
            return
        reference = self.vectors[classified]
        k = min(INFER_NEIGHBOURS, len(classified))
        # Small chunks: a chunk's similarities and their partition indices are its whole working set.
        for start in range(0, len(others), 128):
            chunk = others[start:start + 128]
            similarity = self.vectors[chunk] @ reference.T
            nearest = np.argpartition(-similarity, k - 1, axis=1)[:, :k]
            for row, i in enumerate(chunk):
                votes: dict[str, float] = {}
                for j in nearest[row]:
                    sim = float(similarity[row, j])
                    if sim < INFER_MIN_SIMILARITY:
                        continue
                    leaves = self.offerings[classified[j]].leaves
                    for leaf in leaves:
                        votes[leaf] = votes.get(leaf, 0.0) + sim / len(leaves)
                if votes:
                    leaf, weight = max(votes.items(), key=lambda kv: (kv[1], kv[0]))
                    if weight >= INFER_SHARE * sum(votes.values()):
                        self.offerings[i].inferred = (leaf,)

    def _infer_from_labels(self) -> None:
        """An unclassified offering still without a leaf: the leaf whose label it reads like."""
        if self.leaf_vectors is None or not self.leaf_ids:
            return
        rows = [i for i, o in enumerate(self.offerings) if not o.leaves and not o.inferred]
        for start in range(0, len(rows), 1000):
            chunk = rows[start:start + 1000]
            similarity = self.vectors[chunk] @ self.leaf_vectors.T
            best = similarity.argmax(axis=1)
            for row, i in enumerate(chunk):
                if similarity[row, best[row]] >= INFER_LABEL_SIMILARITY:
                    self.offerings[i].inferred = (self.leaf_ids[best[row]],)

    def _build_words(self) -> None:
        """Every word of every document, as one sorted vocabulary and one posting list (CSR), built from
        typed arrays: ~50k documents make ~2M postings, too many for a list per word on a 512 MB host."""
        from array import array

        ids: dict[str, int] = {}
        words, rows = array("i"), array("i")
        for i, o in enumerate(self.offerings):
            for word in set(re.findall(r"[a-z0-9]+", f"{o.document()} {o.endpoint}".lower())):
                words.append(ids.setdefault(word, len(ids)))
                rows.append(i)
        vocabulary = sorted(ids)
        rank = np.empty(len(ids), dtype=np.int32)
        rank[np.fromiter((ids[w] for w in vocabulary), dtype=np.int32, count=len(vocabulary))] = np.arange(len(vocabulary), dtype=np.int32)
        del ids
        word_rank = rank[np.frombuffer(words, dtype=np.int32)]
        del words
        order = np.argsort(word_rank, kind="stable")
        self._postings = np.frombuffer(rows, dtype=np.int32)[order].copy()
        counts = np.bincount(word_rank, minlength=len(vocabulary))
        self._offsets = np.concatenate(([0], np.cumsum(counts))).astype(np.int64)
        self._vocabulary = vocabulary

    def _rows_for(self, token: str) -> np.ndarray:
        """Rows whose text matches a query word (discovery.relevance): a word it begins, or, for a word of
        six letters or more, any word sharing its first six ("generation" and "generate-ics"). Both are one
        prefix range of the sorted vocabulary."""
        prefix = token[:6] if len(token) >= 6 else token
        first = bisect.bisect_left(self._vocabulary, prefix)
        last = bisect.bisect_left(self._vocabulary, prefix + "\uffff", lo=first)
        if first == last:
            return np.zeros(0, dtype=np.int32)
        return np.unique(self._postings[self._offsets[first]:self._offsets[last]])

    def keyword_scores(self, query_tokens: list[str]) -> np.ndarray:
        counts = np.zeros(len(self.offerings), dtype=np.float32)
        for token in query_tokens:
            counts[self._rows_for(token)] += 1.0
        return counts / max(1, len(query_tokens))

    def leaf_similarity(self, query_vector: np.ndarray | None) -> dict[str, float]:
        if query_vector is None or self.leaf_vectors is None or not self.leaf_ids:
            return {}
        return dict(zip(self.leaf_ids, (float(x) for x in self.leaf_vectors @ query_vector)))

    def query_vector(self, query: str, embedder: semantic.Embedder | None) -> np.ndarray | None:
        if embedder is None or not self.semantic:
            return None
        vectors = embedder.encode([query])
        return None if vectors is None else vectors[0]

    def match(self, query: str, embedder: semantic.Embedder | None) -> list[Match]:
        """Offerings matching the query, strongest first (at most CANDIDATE_LIMIT)."""
        from crowdcode.discovery import (KEYWORD_SHARE, SEMANTIC_ADMIT_FLOOR, SEMANTIC_ADMIT_GAP, SEMANTIC_FULL,
                                         SEMANTIC_ZERO)

        if not self.offerings:
            return []
        keyword = self.keyword_scores(tokens(query))
        similarity = None
        q = self.query_vector(query, embedder)
        if q is not None:
            similarity = self.vectors @ q
        admitted = keyword >= KEYWORD_ADMIT
        if similarity is not None:
            best = float(similarity.max())
            admitted |= similarity >= max(SEMANTIC_ADMIT_FLOOR, best - SEMANTIC_ADMIT_GAP)
            if self._listed.any():
                # CrowdCode's directory is matched among itself, as it always was: a listed offering
                # joins on a similarity close to the best listed one's, not the best of ~13k.
                best_listed = float(similarity[self._listed].max())
                admitted |= self._listed & (similarity >= max(SEMANTIC_ADMIT_FLOOR, best_listed - SEMANTIC_ADMIT_GAP))
        rows = np.flatnonzero(admitted)
        # discovery.match_strength, for every admitted row at once.
        kw = keyword[rows].astype(np.float64)
        if similarity is None:
            strength = kw
        else:
            meaning = np.clip((similarity[rows].astype(np.float64) - SEMANTIC_ZERO) / (SEMANTIC_FULL - SEMANTIC_ZERO), 0.0, 1.0)
            strength = KEYWORD_SHARE * kw + (1 - KEYWORD_SHARE) * meaning
        listed = self._listed[rows]
        if LISTED_SCALE and listed.any() and strength.size:
            # ...and scored against it: the best listed match counts as strong as the best match overall.
            top, top_listed = float(strength.max()), float(strength[listed].max())
            if top_listed > 0:
                factor = min(top / top_listed, LISTED_SCALE)
                strength = np.where(listed, np.minimum(strength * factor, top), strength)
        order = np.lexsort((self._ids[rows], -strength))[:CANDIDATE_LIMIT]
        return [Match(self.offerings[rows[j]], float(kw[j]),
                      float(similarity[rows[j]]) if similarity is not None else None, float(strength[j]))
                for j in order]

    def find(self, endpoint: str, method: str | None) -> Offering | None:
        if method:
            i = self.by_endpoint.get((method.upper(), endpoint))
            if i is not None:
                return self.offerings[i]
        i = self.by_url.get(endpoint)
        return self.offerings[i] if i is not None else None

    def models_in(self, query: str) -> tuple[set[int], str | None]:
        """Ids of the offerings selling a model the query names (C9), and that model or family; empty when none is."""
        text = query.lower()
        compact = re.sub(r"[\s_]+", "-", text)
        squeezed = re.sub(r"[^a-z0-9.]", "", text)
        best: tuple[int, str, set[int]] | None = None
        for model, rows in self.models.items():
            m = model.lower()
            if len(m) < 4:
                continue
            if m in compact or m.replace("-", "") in squeezed:
                if best is None or len(m) > best[0]:
                    best = (len(m), model, rows)
        if best:
            return set(best[2]), best[1]
        words = set(re.findall(r"[a-z0-9]+", text))
        for family, rows in self.families.items():
            if family.lower() in words and len(family) >= 3:
                return set(rows), family
        return set(), None


# --- Loading from the database ---------------------------------------------------------

def _columns(conn: Any, table: str) -> set[str]:
    rows = conn.execute(
        "select column_name from information_schema.columns where table_schema = 'public' and table_name = %s",
        (table,),
    ).fetchall()
    return {r["column_name"] for r in rows}


def _exists(conn: Any, table: str) -> bool:
    return bool(conn.execute("select to_regclass(%s) as t", (f"public.{table}",)).fetchone()["t"])


def _resource_sql(conn: Any) -> str:
    cols = _columns(conn, "index_resources")
    extra = "".join(f", r.{c}" for c in ("model", "model_family", "modality") if c in cols)
    return f"""
        select r.id, r.method, r.route_template, r.probe_url, r.has_params, r.service_name,
               left(r.description, {DESCRIPTION_STORED}) as description, r.input_schema, r.sources, r.networks,
               r.payable, r.protocol, r.price_atomic, r.price_asset, r.quoted_price_atomic, r.quoted_asset,
               r.quoted_network, r.bazaar_payers_30d, r.bazaar_calls_30d, r.tracked, r.liveness, r.service_id {extra},
               -- What CrowdCode's directory says about the service (discovery.listing_document).
               s.name as listed_name, s.metadata->>'description' as listed_description,
               left(s.metadata->>'listing_text', 1000) as listing_text,
               case when s.metadata->>'description' is null then s.review_summary->'pros' end as review_pros
        from index_resources r
        left join services s on s.id = r.service_id
          and coalesce(s.metadata->>'hosted_status', '') <> 'deleted' and {directory_listed('s')}
        where r.listing_type = 'http'
        """


def _resource_item(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["schema_text"] = schema_words(item.pop("input_schema", None))
    pros = item.pop("review_pros", None)
    if isinstance(pros, list) and pros:
        item["listing_text"] = " ".join(filter(None, [item.get("listing_text"), " ".join(str(p) for p in pros[:5])]))
    for key in ("price_atomic", "quoted_price_atomic"):
        if item.get(key) is not None:
            item[key] = str(item[key])
    return item


def load_context(conn: Any) -> dict[str, Any]:
    """Everything the search reads besides the offerings themselves: leaves and classifications (C7),
    reliability (C10), review scores, and the models each offering sells with their priors (C9)."""
    out: dict[str, Any] = {"classifications": {}, "leaves": {}, "leaf_text": {}, "reliability": {},
                           "offering_models": [], "model_priors": {}}
    if _exists(conn, "capability_classifications") and _exists(conn, "capability_leaves"):
        for leaf in conn.execute("select id, node, label, when_to_look, facets from capability_leaves").fetchall():
            for facet in leaf["facets"] or []:
                out["leaves"][f"{leaf['node']}/{facet}"] = leaf["id"]
            out["leaf_text"][leaf["id"]] = ". ".join(filter(None, [leaf["label"], leaf["when_to_look"]]))
        for row in conn.execute(
            "select item_key, assignments from capability_classifications where item_key like 'r:%'"
        ).fetchall():
            out["classifications"][row["item_key"]] = [
                f"{a.get('node')}/{a.get('facet')}" for a in row["assignments"] or [] if isinstance(a, dict)]
    if _exists(conn, "service_reliability"):
        rcols = _columns(conn, "service_reliability")
        v2 = [c for c in ("tier", "alpha", "beta", "quality") if c in rcols]
        rows = conn.execute(
            f"""
            select service_id, state, rule_version, reasons->>0 as reason, inputs->'judged_30d' as judged
                   {''.join(f', {c}' for c in v2)}
            from service_reliability
            """
        ).fetchall()
        for row in rows:
            item = dict(row)
            for key in ("alpha", "beta", "quality"):
                if item.get(key) is not None:
                    item[key] = float(item[key])
            out["reliability"][row["service_id"]] = item
    scores = conn.execute(
        "select id, score, n_eff from services where id in (select service_id from index_resources where service_id is not null)"
    ).fetchall()
    out["scores"] = {r["id"]: [float(r["score"]) if r["score"] is not None else None,
                               float(r["n_eff"] or 0)] for r in scores}
    if _exists(conn, "index_offering_models"):
        out["offering_models"] = [dict(r) for r in conn.execute(
            "select resource_id, model, model_family, modality from index_offering_models order by resource_id, model").fetchall()]
    if _exists(conn, "model_quality_priors"):
        out["model_priors"] = {r["model"]: float(r["prior_rating"]) for r in conn.execute(
            "select model, prior_rating from model_quality_priors where prior_rating is not null").fetchall()}
    return out




def load_rows(conn: Any) -> dict[str, Any]:
    """Everything the search reads, as plain rows (what the evaluation snapshot stores)."""
    out = load_context(conn)
    out["resources"] = [_resource_item(row) for row in conn.execute(_resource_sql(conn)).fetchall()]
    return out


def load_offerings(conn: Any, previous: dict[int, Offering] | None = None
                   ) -> tuple[list[Offering], dict[str, Any], bool, bool]:
    """The offerings, streamed: each row becomes an Offering as it arrives, so the ~50k rows are never all
    held at once (the backend has 512 MB). An offering whose document is unchanged since ``previous`` is kept
    and updated in place. Returns the offerings, the context, whether any document (or the set or order of
    offerings) changed, and whether only leaves or models did."""
    context = load_context(conn)
    build = _Builder(context)
    previous = previous or {}
    out: list[Offering] = []
    documents = shape = False
    with conn.cursor(name="offerings_search") as cursor:
        cursor.itersize = 2000
        cursor.execute(_resource_sql(conn) + " order by r.id")
        for row in cursor:
            item = _resource_item(row)
            old = previous.get(int(item["id"]))
            if old is not None and old.fingerprint == _fingerprint(item):
                before = (old.leaves, tuple(sorted(old.models or ())))
                fresh = build.make(item)
                for name in _MUTABLE_FIELDS:
                    setattr(old, name, getattr(fresh, name))
                build.assess(old)
                shape = shape or before != (old.leaves, tuple(sorted(old.models or ())))
                out.append(old)
            else:
                out.append(build(item))
                documents = True
    documents = documents or len(out) != len(previous) or not previous
    return out, context, documents, shape


def _quality(v31: list | None, prior_rating: float | None) -> float:
    """C10's quality on the judge's 1-5 scale: reviews shrunk to the prior (reliability-v2's rule)."""
    from crowdcode import reliability

    score, n_eff = (v31 or [None, 0.0])
    return float(reliability.quality_for({"v31_score": score, "n_eff": n_eff, "prior_rating": prior_rating}))


_V1_REASON_PREFIX = re.compile(r"^(?:[A-Z]+ https?://\S+: )")


def _short_reason(text: str | None) -> str | None:
    if not text:
        return None
    reason = _V1_REASON_PREFIX.sub("", text)
    reason = re.sub(r"\s*\(latest [^)]*\)", "", reason)
    reason = re.sub(r"^(proven|popular|new|bad): ", "", reason)
    # reliability-v2's first reason goes on to say what the next tier needs; the agent needs the evidence.
    first = reason.split("; ")[0]
    if len(first) >= 12:
        reason = first
    return reason if len(reason) <= REASON_MAX else reason[:REASON_MAX - 1] + "…"


def _intern(value: Any) -> str | None:
    return sys.intern(str(value)) if value is not None else None


class _Builder:
    """Turns one index row into an Offering, with the context every row shares."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.leaves_of = data.get("leaves") or {}
        self.classifications = data.get("classifications") or {}
        self.reliability = data.get("reliability") or {}
        self.scores = data.get("scores") or {}
        self.priors = data.get("model_priors") or {}
        self.models: dict[int, dict[str, tuple[str | None, float | None]]] = {}
        self.modalities: dict[int, set[str]] = {}
        for row in data.get("offering_models") or []:
            self.models.setdefault(int(row["resource_id"]), {})[row["model"]] = (
                row.get("model_family"), self.priors.get(row["model"]))
            if row.get("modality"):
                self.modalities.setdefault(int(row["resource_id"]), set()).add(str(row["modality"]))

    def __call__(self, row: dict[str, Any]) -> Offering:
        return self.assess(self.make(row))

    def make(self, row: dict[str, Any]) -> Offering:
        """What the row itself says: identity, listing, networks, price and payers."""
        networks = tuple(dict.fromkeys(n for n in (network_name(v) for v in [*(row.get("networks") or []),
                                                                            row.get("quoted_network")]) if n))
        payable = bool(row.get("payable"))
        # Interned: ~50k offerings repeat a handful of these strings.
        sources = tuple(sys.intern(str(v)) for v in row.get("sources") or ())
        if not networks and set(sources) & {"gateway", "reviewed", "hosted"}:
            # Paid or reviewed through CrowdCode but listed by no catalog: the network is unknown until inspected.
            networks, payable = (UNKNOWN_NETWORK,), True
        price = usd(row.get("quoted_price_atomic"), row.get("quoted_asset"))
        if price is None:
            price = usd(row.get("price_atomic"), row.get("price_asset"))
        endpoint = row["route_template"] if row.get("has_params") else (row.get("probe_url") or row["route_template"])
        return Offering(
            id=int(row["id"]), method=sys.intern(str(row.get("method") or "POST").upper()), endpoint=endpoint,
            name=row.get("service_name") or row.get("listed_name"),
            description=row.get("description") or row.get("listed_description"),
            schema_text=row.get("schema_text") or "", listing_text=row.get("listing_text"),
            listed=row.get("listed_name") is not None,
            networks=networks, protocol=_intern(row.get("protocol")), payable=payable, price_usd=price,
            payers=row.get("bazaar_payers_30d"), calls=row.get("bazaar_calls_30d"), sources=sources,
            tracked=bool(row.get("tracked")), liveness=sys.intern(row.get("liveness") or "unknown"),
            service_id=row.get("service_id"),
            model=_intern(row.get("model")), model_family=_intern(row.get("model_family")),
            modality=_intern(row.get("modality")), fingerprint=_fingerprint(row),
        )

    def assess(self, o: Offering) -> Offering:
        """What the rest of CrowdCode says about it, re-read on every refresh: its leaves (C7), the models
        it sells (C9), its tier and delivery estimate (C10) and its quality."""
        priors, models = self.priors, self.models
        o.leaves = tuple(dict.fromkeys(self.leaves_of[a] for a in self.classifications.get(f"r:{o.id}", [])
                                       if a in self.leaves_of))
        sold = dict(models[o.id]) if o.id in models else {}
        if o.model and o.model not in sold:
            sold[o.model] = (o.model_family, priors.get(o.model))
        o.models = sold or None
        o.modalities = tuple(sorted(self.modalities.get(o.id, set()) | ({o.modality} if o.modality else set())))
        rel = self.reliability.get(o.service_id) if o.service_id else None
        rating = priors.get(o.model) if o.model else None
        # One definition of the tier and the delivery estimate, shared with the capability tree (W6's
        # value_ranking): reliability-v2's stored answer when there is one, else C10 from the evidence.
        estimate = value_ranking.estimate(
            {"bazaar_payers_30d": o.payers, "bazaar_calls_30d": o.calls, "sources": list(o.sources), "liveness": o.liveness},
            {**rel, "reasons": [rel["reason"]] if rel.get("reason") else [], "judged_30d": rel.get("judged")} if rel else None)
        o.tier, o.alpha, o.beta = estimate.tier, estimate.alpha, estimate.beta
        o.reason = _intern(_short_reason(estimate.reason))
        o.quality = (float(rel["quality"]) if rel and rel.get("quality") is not None
                     else _quality(self.scores.get(o.service_id), rating))
        reviewed = bool((self.scores.get(o.service_id) or [None, 0.0])[1]) or bool(o.listing_text)
        o.evidenced = o.tier in _GOOD_TIERS or o.alpha + o.beta > 2.0 + 1e-9 or reviewed
        return o


# What an offering's document is made of. Prices, payers, liveness and networks change with every probe
# sweep; they are updated in place and never make the index rebuild.
_DOCUMENT_FIELDS = ("method", "route_template", "probe_url", "has_params", "service_name", "listed_name",
                    "description", "listed_description", "listing_text", "schema_text")


def _fingerprint(row: dict[str, Any]) -> int:
    text = repr(tuple(row.get(k) for k in _DOCUMENT_FIELDS))
    return int.from_bytes(hashlib.blake2b(text.encode(), digest_size=8).digest(), "big")


_MUTABLE_FIELDS = ("networks", "protocol", "payable", "price_usd", "payers", "calls", "sources", "tracked", "liveness",
                   "service_id", "model", "model_family", "modality", "listed")


def offerings_from_rows(data: dict[str, Any]) -> list[Offering]:
    build = _Builder(data)
    return [build(row) for row in data.get("resources") or []]


# --- Ranking ------------------------------------------------------------------------------


def best_offer(o: Offering, networks: tuple[str, ...], balances: dict[str, float] | None,
               price: float | None) -> dict[str, Any] | None:
    """The caller's best network for one offering: payable there, lowest effective price, then the larger balance."""
    choices = []
    if o.networks == (UNKNOWN_NETWORK,):
        return {"network": None, "effective_price_usd": price, "transfer_needed": False, "fundable": True,
                "balance_usd": None}
    for network in o.networks:
        if network not in networks:
            continue
        balance = balances.get(network) if balances is not None else None
        effective, transfer, fundable = price, False, True
        if balances is not None and price is not None and (balance or 0.0) < price:
            transfer = True
            effective = price + TRANSFER_COST_USD
            shortfall = price - (balance or 0.0) + TRANSFER_COST_USD
            fundable = any(v >= shortfall for n, v in balances.items() if n != network and n in networks)
        choices.append({"network": network, "effective_price_usd": round(effective, 6) if effective is not None else None,
                        "transfer_needed": transfer, "fundable": fundable, "balance_usd": balance})
    if not choices:
        return None
    choices.sort(key=lambda c: (not c["fundable"], c["effective_price_usd"] or 0.0, -(c["balance_usd"] or 0.0)))
    return choices[0]


def capability(matches: list[Match], leaf_similarity: dict[str, float] | None = None) -> str | None:
    """The leaf a query asks for: what the strongest classified matches agree on, weighed with how
    close the query is to each leaf's own label and when-to-look. None when nothing clearly leads."""
    votes: dict[str, float] = {}
    counted = 0
    for m in matches:
        leaves = m.offering.capabilities
        if not leaves:
            continue
        for leaf in leaves:
            votes[leaf] = votes.get(leaf, 0.0) + m.strength / len(leaves)
        counted += 1
        if counted >= VOTERS:
            break
    if not votes:
        return None
    total = sum(votes.values())
    shares = {leaf: v / total for leaf, v in votes.items()}
    if leaf_similarity:
        # The leaves the matches voted for, and any whose label is about as close to the query as
        # the closest one: the vote and the label decide together.
        closest = max(leaf_similarity.values(), default=0.0)
        options = set(shares) | {k for k, v in leaf_similarity.items() if v >= closest - 0.05}

        ordered = sorted(leaf_similarity.values(), reverse=True)
        if ordered[0] >= LEAF_LABEL_ALONE and (len(ordered) < 2 or ordered[0] - ordered[1] >= LEAF_LABEL_MARGIN):
            # The query reads like one leaf's own label ("send an email": "Send emails"), whatever the
            # words of the strongest matches ("verify an email before you send") voted for.
            return max(leaf_similarity, key=lambda k: (leaf_similarity[k], k))

        def score(leaf: str) -> float:
            return shares.get(leaf, 0.0) + LEAF_LABEL_WEIGHT * max(0.0, leaf_similarity.get(leaf, 0.0))
        leaf = max(options, key=lambda k: (score(k), k))
        return leaf if shares.get(leaf, 0.0) >= LEAF_MIN_SHARE or leaf_similarity.get(leaf, 0.0) >= LEAF_LABEL_ALONE else None
    leaf = max(shares, key=lambda k: (shares[k], k))
    return leaf if shares[leaf] >= LEAF_SHARE else None


@dataclass
class Ranked:
    offering: Offering
    match: float
    best: dict[str, Any]
    price: float | None  # listed or quoted; None when the listing names none
    value_price: float  # the price value is divided by (an estimate when unknown)
    quality: float  # 1-5
    sample: float
    in_group: bool
    explore: bool = False
    contender: bool = False
    model: str | None = None  # the model the query named, if any

    @property
    def fit(self) -> float:
        """How well it fits this request, before anyone has rated it (C11's task_fit): its match, sharpened."""
        return max(self.match, 0.01) ** FIT_POWER

    @property
    def mean_value(self) -> float:
        return self.offering.mean * quality_unit(self.quality) * self.fit / self.value_price

    @property
    def value(self) -> float:
        return self.sample * quality_unit(self.quality) * self.fit / self.value_price


def rank(matches: list[Match], *, networks: tuple[str, ...], balances: dict[str, float] | None, seed: int,
         model_rows: set[int] | None = None, model: str | None = None,
         leaf_similarity: dict[str, float] | None = None,
         producers: Callable[[str], list[Offering]] | None = None) -> tuple[list[Ranked], dict[str, Any]]:
    """C13 over the matches, best first.

    1. The matching set: offerings of the model the query names, or of the leaf the strongest
       matches agree on, within MATCH_BAND of the strongest match.
    2. Its contenders: every offering with evidence (C10's prior or judged purchases or reviews),
       plus the EXPLORE_SLOTS most relevant ones with none. Ranked by a seeded Thompson draw of
       delivery x quality per effective dollar. Bounding the offerings with no evidence is what
       keeps "uncertain offerings are tried in proportion to their uncertainty" true at index
       scale: with a hundred Beta(1,1) offerings in a set, one of them would draw near 1.0 on
       nearly every request and the best-known option would almost never come first.
    3. The rest of the matching set, then everything else that matched, by match then mean value.
    """
    left_out = {"bad": 0, "not_payable": 0}
    usable: list[tuple[Match, dict[str, Any]]] = []
    for m in matches:
        o = m.offering
        if o.tier == "bad":
            left_out["bad"] += 1
            continue
        best = best_offer(o, networks, balances, o.price_usd) if o.payable else None
        if best is None:
            left_out["not_payable"] += 1
            continue
        usable.append((m, best))
    if not usable:
        return [], {"left_out": left_out, "capability": None, "model": model, "matching": 0}
    top = max(m.strength for m, _ in usable)
    floor = max(MATCH_FLOOR, top - MATCH_BAND)
    leaf = None if model_rows else capability([m for m, _ in usable], leaf_similarity)

    def in_group(m: Match) -> bool:
        if model_rows:
            return m.offering.id in model_rows
        if m.strength < floor:
            return False
        if leaf is None:
            return True
        if m.offering.listed:
            # CrowdCode's directory lists it (reviewed, or ours): it competes on relevance within the band,
            # as the directory always did, whatever leaf the classifier or a neighbour put it in.
            return True
        if not m.offering.capabilities:
            return m.strength >= top - UNCLASSIFIED_BAND
        return leaf in m.offering.capabilities

    group = [(m, b) for m, b in usable if in_group(m)]
    if not group:  # a model nobody sells payably: the strongest matches stand alone
        group = [(m, b) for m, b in usable if m.strength >= floor]
    asked = {mod for m, _ in group if model_rows for mod in m.offering.modalities}
    rule = policy(leaf, asked)
    if rule == "quality_first":
        if leaf in ARTIFACT_LEAVES:
            # Every producer of the artifact is a candidate, matched or not; those that do not return
            # it (prompt writers, metadata) are not, however well they match.
            seen = {id(m.offering) for m, _ in usable}
            for o in producers(leaf) if producers else []:
                if id(o) in seen or o.tier == "bad" or not o.payable:
                    continue
                best = best_offer(o, networks, balances, o.price_usd)
                if best is not None:
                    usable.append((Match(o, 0.0, None, MATCH_FLOOR), best))
            group = [(m, b) for m, b in usable if returns_artifact(m.offering, leaf)] or group
        return _quality_first(usable, group, leaf, model, typical_of(group), left_out)
    # value_first: only offerings at least VALUE_MIN_QUALITY good are drawn.
    group = [(m, b) for m, b in group if _model_quality(m.offering, model) >= VALUE_MIN_QUALITY] or group
    unproven = sorted((m for m, _ in group if not m.offering.evidenced),
                      key=lambda m: (-round(m.strength, 2), -(m.offering.payers or 0), m.offering.id))
    explorers = {id(m.offering) for m in unproven[:EXPLORE_SLOTS]}
    contenders = {id(m.offering) for m, _ in group if m.offering.evidenced or id(m.offering) in explorers}
    members = {id(m.offering) for m, _ in group}
    typical = typical_of(group)
    rng = np.random.default_rng(seed)
    ranked: list[Ranked] = []
    # One draw per offering in a fixed order, so a seed reproduces the list.
    for m, best in sorted(usable, key=lambda item: item[0].offering.id):
        o = m.offering
        price = best["effective_price_usd"]
        value_price = (price if price is not None else typical) + CALL_OVERHEAD_USD
        sample = float(rng.beta(o.alpha, o.beta))
        r = Ranked(o, m.strength, best, o.price_usd, value_price, _model_quality(o, model), sample, id(o) in members)
        r.contender = id(o) in contenders
        ranked.append(r)
    inside = sorted((r for r in ranked if r.contender), key=lambda r: (not r.best["fundable"], -r.value, r.offering.id))
    by_mean = sorted(inside, key=lambda r: (not r.best["fundable"], -r.mean_value, r.offering.id))
    mean_position = {id(r): i for i, r in enumerate(by_mean)}
    for position, r in enumerate(inside):
        # Lifted by its draw above an offering whose posterior mean ranks higher.
        r.explore = r.sample > r.offering.mean and mean_position[id(r)] > position
    rest = sorted((r for r in ranked if not r.contender),
                  key=lambda r: (not r.in_group, -round(r.match, 1), not r.best["fundable"], -r.mean_value, r.offering.id))
    return inside + rest, {"left_out": left_out, "capability": leaf, "model": model, "matching": len(group),
                           "policy": "value_first"}


def typical_of(group: list[tuple[Match, dict[str, Any]]]) -> float:
    known = sorted(m.offering.price_usd for m, _ in group if m.offering.price_usd is not None)
    return known[len(known) // 2] if known else 0.01


def _quality_first(usable: list[tuple[Match, dict[str, Any]]], group: list[tuple[Match, dict[str, Any]]],
                   leaf: str | None, model: str | None, typical: float, left_out: dict[str, int]
                   ) -> tuple[list[Ranked], dict[str, Any]]:
    """C13 amendment, `quality_first`: models by quality (the leaderboard prior, then judged quality), in
    bands of QUALITY_BAND; within a band, sellers by expected delivery, then effective price. No draw:
    `explore` marks a seller with no delivery evidence yet (tier `new`), which is how a top model's
    untried sellers get tried. A cheaper, worse model never outranks a better one for its price."""
    members = {id(m.offering) for m, _ in group}
    ranked: list[Ranked] = []
    for m, best in usable:
        o = m.offering
        price = best["effective_price_usd"]
        value_price = (price if price is not None else typical) + CALL_OVERHEAD_USD
        r = Ranked(o, m.strength, best, o.price_usd, value_price, _model_quality(o, model), o.mean, id(o) in members)
        r.contender, r.model = r.in_group, model
        r.explore = r.contender and o.tier == "new"
        ranked.append(r)

    def band(q: float) -> float:
        return math.floor(q / QUALITY_BAND + 0.5) * QUALITY_BAND

    inside = sorted((r for r in ranked if r.contender), key=lambda r: (
        not r.best["fundable"], -band(r.quality), -round(r.offering.mean, 2), r.value_price, -r.match, r.offering.id))
    kinds = {mod for r in inside for mod in r.offering.modalities}
    rest = sorted((r for r in ranked if not r.contender and (not model or set(r.offering.modalities) & kinds)),
                  key=lambda r: (-round(r.match, 1), not r.best["fundable"], -band(r.quality), -r.offering.mean,
                                 r.offering.id))
    return inside + rest, {"left_out": left_out, "capability": leaf, "model": model, "matching": len(group),
                           "policy": "quality_first"}


def _model_quality(o: Offering, model: str | None) -> float:
    """An offering's quality: the asked-for model's (or family's) prior when it sells several; for an
    offering that sells several and none was asked for, its best model's; else its own (C10)."""
    own = o.quality if o.quality is not None else NEUTRAL_QUALITY
    if o.models and not o.model:
        priors = [p for name, (family, p) in o.models.items() if p is not None and (
            model is None or name == model or family == model)]
        if priors:
            return _quality(None, max(priors))
    return own


def best_model(o: Offering, model: str | None) -> str | None:
    """The model an offering's quality comes from: its own, the asked-for one, or its best by prior."""
    if o.model:
        return o.model
    rated = [(p, name) for name, (family, p) in (o.models or {}).items()
             if p is not None and (model is None or name == model or family == model)]
    return max(rated)[1] if rated else None


def policy(leaf: str | None, modalities: set[str]) -> str:
    """C13 amendment: `quality_first` for media and models, `value_first` for everything else."""
    if leaf is not None:
        return "quality_first" if leaf.split(".")[0] in QUALITY_FIRST_NODES else "value_first"
    return "quality_first" if modalities & QUALITY_FIRST_MODALITIES else "value_first"


# Until the tree re-classifies media leaves under the amendment, an endpoint whose own path or name says it
# writes prompts, scripts or drafts, or reads metadata, is not taken for a generator.
_NOT_ARTIFACT = re.compile(r"prompt|script|storyboard|draft|planner|metadata|transcri|caption|pricing|price-list", re.I)


def returns_artifact(o: Offering, leaf: str) -> bool:
    """A generator or editor belongs in its leaf only when it returns the artifact: classified there by
    the tree's model (C7, which applies the rule), or selling a model of that modality (C9). A leaf
    inferred from neighbours is not enough: prompt writers and metadata endpoints read like generators."""
    path = o.endpoint.split("://", 1)[-1].split("/", 1)[-1]
    if _NOT_ARTIFACT.search(path) or _NOT_ARTIFACT.search(o.name or ""):
        return False
    if leaf in o.leaves:
        return True
    if not set(o.modalities) & LEAF_MODALITY.get(leaf, set()):
        return False
    node = leaf.split(".")[0]
    # A path that names another kind of artifact only ("/generate/image" for a video model) is that kind.
    others = [w for n, w in _ARTIFACT_WORDS.items() if n != node and re.search(w, path, re.I)]
    if others and not re.search(_ARTIFACT_WORDS[node], path, re.I):
        return False
    # An editor says so; a generator of the same modality is not one.
    return not leaf.endswith(".edit") or bool(_EDIT.search(f"{path} {o.name or ''}"))


def describe(r: Ranked, position: int, detail: bool = False, capability: str | None = None) -> dict[str, Any]:
    """One result, compact: what an agent needs to choose and pay (C13)."""
    o = r.offering
    name = o.name or None
    if name and len(name) > NAME_MAX:
        name = name[:NAME_MAX - 1] + "…"
    description = (o.description or "").strip()
    out: dict[str, Any] = {
        "rank": position,
        "endpoint": o.endpoint,
        "method": o.method,
        "name": name,
    }
    if description and (detail or not name or name.lower() not in description.lower()[:len(name) + 5]):
        out["description"] = description[:300] if detail else (
            description if len(description) <= DESCRIPTION_MAX else description[:DESCRIPTION_MAX - 1] + "…")
    sold = best_model(o, r.model)
    if sold:
        out["model"] = sold
    if o.capabilities and (detail or o.capabilities[0] != capability):
        out["capability"] = o.capabilities[0]  # said once in the envelope when it is the one asked for
    out["tier"] = o.tier
    if o.reason:
        out["why"] = o.reason
    if r.explore:
        out["explore"] = True
    out["price_usd"] = round(r.price, 6) if r.price is not None else None
    out["networks"] = [] if o.networks == (UNKNOWN_NETWORK,) else list(o.networks)
    best = {k: v for k, v in r.best.items() if k != "balance_usd" and not (k == "transfer_needed" and not v)
            and not (k == "fundable" and v)}
    # Said only when it adds something: a choice between networks, a transfer, or a price unlike the listed one.
    if detail or len(out["networks"]) > 1 or set(best) - {"network", "effective_price_usd"} or \
            best.get("effective_price_usd") != out["price_usd"]:
        out["best"] = best
    if detail:
        out["delivery"] = {"alpha": round(o.alpha, 2), "beta": round(o.beta, 2), "mean": round(o.mean, 3),
                           "draw": round(r.sample, 3)}
        out["quality"] = round(r.quality, 2)
        out["match"] = round(r.match, 3)
        out["payers_30d"] = o.payers
        out["sources"] = list(o.sources)
    return out


RANKING = ("quality_first (media, models): best model, then likeliest seller, then price. value_first: a seeded "
           "draw of delivery x quality per $. bad left out.")


# --- The shared index ------------------------------------------------------------------------

REFRESH_SECONDS = 900.0
RETRY_SECONDS = 60.0


class SharedIndex:
    """The index this process searches, loaded in the background and swapped in whole."""

    def __init__(self) -> None:
        self._index: OfferingIndex | None = None
        self._lock = threading.Lock()
        self._loading = False
        self._next_at = 0.0
        # What the current index embedded: document hash -> its row, so a refresh embeds only what changed.
        self._rows: dict[int, int] = {}
        self._matrix: np.ndarray | None = None
        self._leaf_text: dict[str, str] = {}
        self.error: str | None = None

    def get(self) -> OfferingIndex | None:
        return self._index

    def set(self, index: OfferingIndex | None) -> None:
        self._index = index

    def refresh_due(self, connect: Callable[[], Any], embedder: semantic.Embedder | None) -> None:
        """Start a background refresh when one is due; never blocks a request."""
        if time.monotonic() < self._next_at:
            return
        with self._lock:
            if self._loading or time.monotonic() < self._next_at:
                return
            self._loading = True
        threading.Thread(target=self._refresh, args=(connect, embedder), name="offerings-refresh", daemon=True).start()

    def refresh(self, connect: Callable[[], Any], embedder: semantic.Embedder | None) -> OfferingIndex | None:
        """Refresh now, in this thread, unless a refresh is already running (two would double the memory)."""
        with self._lock:
            if self._loading:
                return self._index
            self._loading = True
        self._refresh(connect, embedder)
        return self._index

    def _refresh(self, connect: Callable[[], Any], embedder: semantic.Embedder | None) -> None:
        delay = RETRY_SECONDS
        try:
            current = self._index
            previous = {o.id: o for o in current.offerings} if current is not None else None
            with connect() as conn:
                offerings, data, documents, shape = load_offerings(conn, previous)
            leaf_text = data.get("leaf_text") or {}
            if current is not None and not documents and leaf_text == self._leaf_text and (
                    embedder is None or current.semantic):
                # Same listings: the offerings were updated in place (price, payers, tier, delivery,
                # quality), so the index built over them stands; leaves and models are re-read into it.
                if shape:
                    current.reassess()
                current.built_at = time.time()
                self.error = None
                delay = REFRESH_SECONDS
                return
            # Listings changed (the daily sweep): build anew. The old index is let go first, so the
            # two are never in memory together; searches use the live catalogs for these seconds.
            del previous, current
            self._index = None
            _release_memory()
            leaf_ids = sorted(leaf_text)
            vectors = leaf_vectors = None
            if embedder is not None:
                if not embedder.ready():
                    embedder.warm(wait=True)
                if embedder.ready():
                    # One call, so the cache keeps exactly what this index uses.
                    both = self._embed(embedder, [*(o.document() for o in offerings), *(leaf_text[i] for i in leaf_ids)])
                    if both is not None:
                        vectors, leaf_vectors = both[:len(offerings)], both[len(offerings):]
            if embedder is None or vectors is not None:
                self._index = OfferingIndex(offerings, vectors=vectors, leaf_text=leaf_text,
                                            leaf_vectors=leaf_vectors if leaf_ids else None)
                self._leaf_text = leaf_text
                for leaf in ARTIFACT_LEAVES:
                    self._index.producers(leaf)  # warm, so no search waits for the scan
                self.error = None
                delay = REFRESH_SECONDS
        except Exception as exc:
            traceback.print_exc()
            self.error = type(exc).__name__
        finally:
            _release_memory()
            with self._lock:
                self._loading = False
                self._next_at = time.monotonic() + delay

    def _embed(self, embedder: semantic.Embedder, documents: list[str]) -> np.ndarray | None:
        """One float32 row per document (BLAS; float16 is ~100x slower to search): copied from the last
        refresh when the text is unchanged, else encoded, chunk by chunk, straight into the new matrix."""
        keys = [int.from_bytes(hashlib.blake2b(d.encode(), digest_size=8).digest(), "big") for d in documents]
        missing = [i for i, k in enumerate(keys) if k not in self._rows]
        dims = self._matrix.shape[1] if self._matrix is not None and self._matrix.ndim == 2 and self._matrix.size else None
        if dims is None:
            probe = embedder.encode_uncached(documents[:1]) if documents else None
            if probe is None or not len(probe):
                return None if documents else np.zeros((0, 1), dtype=np.float32)
            dims = probe.shape[1]
        matrix = np.empty((len(documents), dims), dtype=np.float32)
        for i, key in enumerate(keys):
            row = self._rows.get(key)
            if row is not None:
                matrix[i] = self._matrix[row]
        for start in range(0, len(missing), 1000):
            chunk = missing[start:start + 1000]
            vectors = embedder.encode_uncached([documents[i] for i in chunk])
            if vectors is None:
                return None
            matrix[chunk] = vectors
        self._rows = {key: i for i, key in enumerate(keys)}
        self._matrix = matrix
        return matrix


def _release_memory() -> None:
    """Hand the refresh's transient memory back to the OS (glibc keeps freed heap otherwise)."""
    import gc

    gc.collect()
    try:
        import ctypes

        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


SHARED = SharedIndex()


def new_seed() -> int:
    return int.from_bytes(np.random.default_rng().bytes(4), "big")


def parse_seed(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 0 <= value < 2**63:
        return value
    if isinstance(value, str) and value.isdigit() and len(value) <= 18:
        return int(value)
    return None
