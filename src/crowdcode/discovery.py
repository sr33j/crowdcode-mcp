"""Cross-chain service discovery and ranking (`find_service`).

One search over every public catalog of machine-payable services, merged by
endpoint and ranked for a payer that holds USDC on several networks:

- ``cdp-bazaar``: Coinbase's x402 Bazaar search (x402 on Base, Solana, ...)
- ``mpp``: the mpp.dev service catalog (MPP charges on Tempo)
- ``crowdcode``: services CrowdCode already has reviews for
- ``caller``: candidates the caller found elsewhere (e.g. AgentCash search)

Ranking, best first:

1. Quality: the CrowdCode canonical score in half-point bands. A service with
   no reviews sits at the scoring prior (3.0), so positive evidence ranks above
   unknown services and poor evidence below them.
2. Effective price on the payer's best network: the quoted price, plus one
   transfer between the payer's own networks when that network's balance does
   not cover it.
3. The payer's balance on that network.

Prices come from catalogs and can be dynamic; callers must inspect the
endpoint for the live quote before paying. The query is used only for the
search and is never stored.
"""

from __future__ import annotations

import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import httpx
import psycopg

from crowdcode.identity import normalize_api_endpoint
from crowdcode.scoring import as_float, is_unproven

CDP_SEARCH_URL = "https://api.cdp.coinbase.com/platform/v2/x402/discovery/search"
MPP_CATALOG_URL = "https://mpp.dev/api/services"

BASE_NETWORK = "eip155:8453"
SOLANA_NETWORK = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
TEMPO_NETWORK = "eip155:4217"
MONAD_NETWORK = "eip155:143"

# The token each network's payers settle in (USDC; Stargate USDC.e on Tempo).
NETWORK_TOKENS = {
    "base": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    "solana": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "tempo": "0x20c000000000000000000000b9537d11c60e8b50",
    "monad": "0x754704bc059f8c67012fed69bc8a327a5aafb603",
}
NETWORK_ALIASES = {
    BASE_NETWORK: "base", "base": "base",
    SOLANA_NETWORK.lower(): "solana", "solana": "solana", "solana:mainnet": "solana",
    TEMPO_NETWORK: "tempo", "tempo": "tempo", "tempo:4217": "tempo", "tempo:mainnet": "tempo",
    MONAD_NETWORK: "monad", "monad": "monad",
}
DEFAULT_NETWORKS = ("base", "solana", "tempo")
# Moving USDC between one's own networks (Relay) costs about $0.02-0.03.
TRANSFER_COST_USD = 0.03
# Services rated below this with real evidence are refused by OpenCrowd's gateway.
SCORE_FLOOR = 2.0
PRIOR_SCORE = 3.0
# Within a quality band, services that several distinct payers used recently rank first.
ADOPTED_PAYERS = 3

SOURCE_LIMIT = 12
DEFAULT_LIMIT = 10
MAX_LIMIT = 25
MAX_CANDIDATES = 50
HTTP_TIMEOUT = 8.0
MPP_CACHE_SECONDS = 600
SEARCH_CACHE_SECONDS = 60

_STOPWORDS = frozenset(
    "a an and api apis are as at be by data for from get how i in into is it of on or "
    "service services that the this to tool use using via with".split()
)


@dataclass
class Offer:
    network: str | None  # short name when recognised ("base"), else as advertised; None = unknown
    protocol: str  # "x402" | "mpp"
    price_usd: float | None
    payable: bool | None  # None: the network is unknown until the endpoint is inspected

    def as_dict(self) -> dict[str, Any]:
        return {
            "network": self.network,
            "protocol": self.protocol,
            "price_usd": self.price_usd,
            "payable": self.payable,
        }


@dataclass
class Candidate:
    endpoint: str
    method: str | None = None
    name: str | None = None
    description: str | None = None
    offers: list[Offer] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    order: int = 0  # best position within any source's own relevance order
    unique_payers: int | None = None  # catalog usage: distinct payers over the last 30 days

    def merge(self, other: "Candidate") -> None:
        self.order = min(self.order, other.order)
        if other.unique_payers is not None:
            self.unique_payers = max(self.unique_payers or 0, other.unique_payers)
        self.method = self.method or other.method
        self.name = self.name or other.name
        self.description = self.description or other.description
        known = {(o.network, o.protocol) for o in self.offers}
        for offer in other.offers:
            if (offer.network, offer.protocol) in known:
                continue
            # A concrete network replaces an "unknown network" placeholder of the same protocol.
            self.offers = [o for o in self.offers if not (o.network is None and o.protocol == offer.protocol)]
            self.offers.append(offer)
            known.add((offer.network, offer.protocol))
        for source in other.sources:
            if source not in self.sources:
                self.sources.append(source)


def network_name(value: Any) -> str | None:
    text = str(value or "").strip()
    return NETWORK_ALIASES.get(text.lower()) or NETWORK_ALIASES.get(text)


def price_from_atomic(amount: Any, decimals: Any = 6) -> float | None:
    try:
        atomic = int(str(amount))
        places = int(decimals) if decimals is not None else 6
    except (TypeError, ValueError):
        return None
    return atomic / 10**places if atomic >= 0 else None


def price_from_text(value: Any) -> float | None:
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    match = re.search(r"\d+(?:\.\d+)?", str(value or ""))
    return float(match.group(0)) if match else None


def tokens(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return [w for w in dict.fromkeys(words) if len(w) >= 3 and w not in _STOPWORDS]


def relevance(query_tokens: list[str], text: str) -> float:
    if not query_tokens:
        return 0.0
    haystack = set(re.findall(r"[a-z0-9]+", text.lower()))
    hits = sum(1 for t in query_tokens if t in haystack or any(w.startswith(t) for w in haystack))
    return hits / len(query_tokens)


def _endpoint(value: Any) -> str | None:
    try:
        return normalize_api_endpoint(str(value)) if value else None
    except ValueError:
        return None


# --- Sources -----------------------------------------------------------------


def x402_offer(accept: dict[str, Any], networks: Iterable[str]) -> Offer:
    name = network_name(accept.get("network"))
    asset = str(accept.get("asset") or "")
    token = NETWORK_TOKENS.get(name or "")
    scheme = accept.get("scheme")
    payable = bool(
        name in networks
        and token is not None
        and (asset == token if name == "solana" else asset.lower() == token)
        and scheme in (None, "exact")
    )
    return Offer(
        network=name or str(accept.get("network") or "") or None,
        protocol="x402",
        price_usd=price_from_atomic(accept.get("amount") or accept.get("maxAmountRequired"),
                                    (accept.get("extra") or {}).get("decimals", 6)),
        payable=payable,
    )


def cdp_candidates(payload: Any, networks: Iterable[str]) -> list[Candidate]:
    networks = tuple(networks)
    out: list[Candidate] = []
    for resource in (payload or {}).get("resources") or (payload or {}).get("items") or []:
        endpoint = _endpoint(resource.get("resource"))
        if not endpoint:
            continue
        bazaar = (resource.get("extensions") or {}).get("bazaar") or {}
        info = bazaar.get("info") or {}
        method = ((info.get("input") or {}).get("method") or None)
        usage = (resource.get("quality") or {}).get("l30DaysUniquePayers")
        out.append(Candidate(
            endpoint=endpoint,
            method=method.upper() if isinstance(method, str) else None,
            name=bazaar.get("name"),
            description=resource.get("description") or bazaar.get("description"),
            offers=[x402_offer(a, networks) for a in resource.get("accepts") or [] if isinstance(a, dict)],
            sources=["cdp-bazaar"],
            unique_payers=usage if isinstance(usage, int) else None,
        ))
    return out


def mpp_candidates(catalog: Any, query: str, networks: Iterable[str], limit: int = SOURCE_LIMIT) -> list[Candidate]:
    query_tokens = tokens(query)
    scored: list[tuple[float, Candidate]] = []
    for service in (catalog or {}).get("services") or []:
        base = service.get("serviceUrl")
        if not base or service.get("status") not in (None, "active"):
            continue
        service_text = " ".join(str(v) for v in (
            service.get("name"), service.get("description"),
            " ".join(service.get("tags") or []), " ".join(service.get("categories") or []),
        ) if v)
        for endpoint in service.get("endpoints") or []:
            payment = endpoint.get("payment") or {}
            if payment.get("method") != "tempo" or payment.get("intent") != "charge":
                continue  # sessions open payment channels; other methods are not payable
            url = _endpoint(str(base).rstrip("/") + str(endpoint.get("path") or ""))
            if not url:
                continue
            endpoint_score = relevance(query_tokens, f"{endpoint.get('description') or ''} {endpoint.get('path') or ''}")
            score = max(endpoint_score, 0.6 * relevance(query_tokens, service_text) + 0.4 * endpoint_score)
            if score < 0.5:
                continue
            payable = "tempo" in networks and str(payment.get("currency") or "").lower() == NETWORK_TOKENS["tempo"]
            scored.append((score, Candidate(
                endpoint=url,
                method=str(endpoint.get("method") or "GET").upper(),
                name=f"{service.get('name')}: {endpoint.get('description')}" if endpoint.get("description") else service.get("name"),
                description=service.get("description"),
                offers=[Offer("tempo", "mpp", price_from_atomic(payment.get("amount"), payment.get("decimals", 6)), payable)],
                sources=["mpp"],
            )))
    scored.sort(key=lambda item: -item[0])
    return [candidate for _, candidate in scored[:limit]]


def caller_candidates(raw: Any, networks: Iterable[str]) -> list[Candidate]:
    """Candidates the caller already found, e.g. AgentCash search results.

    Accepts ``{endpoint|url, method?, name?, description?, price_usd?,
    network?|networks?, protocol?|protocols?, unique_payers?, source?}``.
    """
    networks = tuple(networks)
    out: list[Candidate] = []
    for item in (raw or [])[:MAX_CANDIDATES]:
        if not isinstance(item, dict):
            continue
        endpoint = _endpoint(item.get("endpoint") or item.get("url"))
        if not endpoint:
            continue
        price = price_from_text(item.get("price_usd", item.get("price")))
        listed = item.get("networks") or ([item["network"]] if item.get("network") else [])
        protocols = item.get("protocols") or ([item["protocol"]] if item.get("protocol") else ["x402"])
        offers: list[Offer] = []
        for value in listed:
            name = network_name(value)
            protocol = "mpp" if name == "tempo" else "x402"
            offers.append(Offer(name or str(value), protocol, price, name in networks if name else False))
        if not offers:
            offers = [Offer("tempo" if p == "mpp" else None, "mpp" if p == "mpp" else "x402", price,
                            ("tempo" in networks) if p == "mpp" else None)
                      for p in dict.fromkeys(str(p).lower() for p in protocols)]
        source = str(item.get("source") or "caller")[:40]
        usage = item.get("unique_payers")
        out.append(Candidate(endpoint=endpoint, method=(str(item["method"]).upper() if item.get("method") else None),
                             name=item.get("name"), description=item.get("description"),
                             offers=offers, sources=[source],
                             unique_payers=int(usage) if isinstance(usage, (int, float)) and usage >= 0 else None))
    return out


def directory_candidates(rows: list[dict[str, Any]], query: str, limit: int = 6) -> list[Candidate]:
    query_tokens = tokens(query)
    scored = []
    for row in rows:
        endpoint = _endpoint(row.get("canonical_endpoint"))
        if not endpoint:
            continue
        score = relevance(query_tokens, f"{row.get('name') or ''} {row.get('directory_slug') or ''} {endpoint}")
        if score < 0.5:
            continue
        protocol = "mpp" if row.get("payment_provider") == "mppx" else "x402"
        scored.append((score, Candidate(
            endpoint=endpoint, name=row.get("name"),
            offers=[Offer("tempo" if protocol == "mpp" else None, protocol, None, True if protocol == "mpp" else None)],
            sources=["crowdcode"],
        )))
    scored.sort(key=lambda item: -item[0])
    return [candidate for _, candidate in scored[:limit]]


class CatalogClient:
    """HTTP access to the public catalogs, with short caches."""

    def __init__(self, get: Callable[..., httpx.Response] | None = None) -> None:
        self._get = get or httpx.get
        self._mpp: tuple[float, Any] | None = None
        self._searches: dict[str, tuple[float, Any]] = {}

    def cdp_search(self, query: str) -> Any:
        key = query.strip().lower()
        cached = self._searches.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        response = self._get(CDP_SEARCH_URL, params={"query": query, "limit": SOURCE_LIMIT},
                             headers={"accept": "application/json"}, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        payload = response.json()
        if len(self._searches) > 256:
            self._searches.clear()
        self._searches[key] = (time.monotonic() + SEARCH_CACHE_SECONDS, payload)
        return payload

    def mpp_catalog(self) -> Any:
        if self._mpp and self._mpp[0] > time.monotonic():
            return self._mpp[1]
        response = self._get(MPP_CATALOG_URL, headers={"accept": "application/json"}, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        payload = response.json()
        self._mpp = (time.monotonic() + MPP_CACHE_SECONDS, payload)
        return payload


CATALOGS = CatalogClient()


# --- Quality -------------------------------------------------------------------


def directory_rows(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return conn.execute(
        "select id, name, directory_slug, canonical_endpoint, payment_provider from services"
    ).fetchall()


def evidence_by_endpoint(conn: psycopg.Connection, endpoints: list[str]) -> dict[str, dict[str, Any]]:
    """CrowdCode evidence for each endpoint that resolves to a reviewed service."""
    if not endpoints:
        return {}
    rows = conn.execute(
        """
        with matched as (
          select si.identifier_value as endpoint, si.service_id
          from service_identifiers si
          where si.identifier_type = 'api_endpoint' and si.identifier_value = any(%s)
          union
          select s.canonical_endpoint, s.id from services s where s.canonical_endpoint = any(%s)
        )
        select m.endpoint, s.id as service_id, s.score, s.n_eff,
               count(r.id)::int as num_reviews,
               count(r.id) filter (where r.payment_verified)::int as num_verified_reviews
        from matched m
        join services s on s.id = m.service_id
        left join reviews r on r.service_id = s.id
        group by m.endpoint, s.id, s.score, s.n_eff
        """,
        (endpoints, endpoints),
    ).fetchall()
    return {row["endpoint"]: row for row in rows}


def quality(evidence: dict[str, Any] | None) -> dict[str, Any]:
    if not evidence:
        return {"score": PRIOR_SCORE, "reviewed": False, "unproven": True, "n_eff": 0.0, "num_reviews": 0,
                "num_verified_reviews": 0}
    score = as_float(evidence.get("score"))
    n_eff = as_float(evidence.get("n_eff")) or 0.0
    return {
        "service_id": evidence.get("service_id"),
        "score": score if score is not None else PRIOR_SCORE,
        "reviewed": (evidence.get("num_reviews") or 0) > 0,
        "unproven": is_unproven(n_eff),
        "n_eff": n_eff,
        "num_reviews": evidence.get("num_reviews") or 0,
        "num_verified_reviews": evidence.get("num_verified_reviews") or 0,
    }


def quality_band(q: dict[str, Any]) -> float:
    return math.floor(float(q["score"]) * 2 + 0.5) / 2


# --- Ranking -------------------------------------------------------------------


def best_offer(candidate: Candidate, networks: tuple[str, ...], balances: dict[str, float] | None) -> dict[str, Any] | None:
    """The payer's best offer for one service: lowest effective price, then the larger balance."""
    choices = []
    for offer in candidate.offers:
        if offer.payable is False:
            continue
        balance = balances.get(offer.network) if balances is not None and offer.network else None
        transfer = False
        fundable = True
        effective = offer.price_usd
        if balances is not None and offer.network in networks and offer.price_usd is not None:
            if (balance or 0.0) < offer.price_usd:
                transfer = True
                effective = offer.price_usd + TRANSFER_COST_USD
                shortfall = offer.price_usd - (balance or 0.0) + TRANSFER_COST_USD
                fundable = any(v >= shortfall for n, v in balances.items() if n != offer.network and n in networks)
        choices.append({
            "network": offer.network,
            "protocol": offer.protocol,
            "price_usd": offer.price_usd,
            "effective_price_usd": round(effective, 6) if effective is not None else None,
            "transfer_needed": transfer,
            "fundable": fundable,
            "balance_usd": balance,
        })
    if not choices:
        return None
    choices.sort(key=lambda c: (not c["fundable"], c["effective_price_usd"] is None,
                                c["effective_price_usd"] or 0.0, -(c["balance_usd"] or 0.0)))
    return choices[0]


def rank(candidates: list[Candidate], evidence: dict[str, dict[str, Any]], networks: tuple[str, ...],
         balances: dict[str, float] | None) -> list[dict[str, Any]]:
    ranked = []
    for candidate in candidates:
        best = best_offer(candidate, networks, balances)
        if best is None:
            continue  # nothing this payer can settle
        q = quality(evidence.get(candidate.endpoint))
        below_floor = not q["unproven"] and q["score"] < SCORE_FLOOR
        adopted = (candidate.unique_payers or 0) >= ADOPTED_PAYERS
        key = (
            below_floor,
            -quality_band(q),
            not adopted,
            not best["fundable"],
            best["effective_price_usd"] is None,
            best["effective_price_usd"] or 0.0,
            -(best["balance_usd"] or 0.0),
            candidate.order,
        )
        ranked.append((key, candidate, q, best, below_floor))
    ranked.sort(key=lambda item: item[0])
    out = []
    for position, (_, candidate, q, best, below_floor) in enumerate(ranked, start=1):
        out.append({
            "rank": position,
            "endpoint": candidate.endpoint,
            "method": candidate.method,
            "name": candidate.name,
            "description": (candidate.description or "")[:300] or None,
            "quality": {**q, "score": round(q["score"], 2), "n_eff": round(q["n_eff"], 2),
                        "unique_payers_30d": candidate.unique_payers,
                        **({"below_floor": True} if below_floor else {})},
            "best": best,
            "offers": [o.as_dict() for o in candidate.offers],
            "sources": candidate.sources,
        })
    return out


def parse_networks(value: Any) -> tuple[str, ...]:
    if not value:
        return DEFAULT_NETWORKS
    items = value.split(",") if isinstance(value, str) else value
    names = [network_name(v) for v in items]
    return tuple(dict.fromkeys(n for n in names if n)) or DEFAULT_NETWORKS


def parse_balances(value: Any) -> dict[str, float] | None:
    if not isinstance(value, dict):
        return None
    out: dict[str, float] = {}
    for key, amount in value.items():
        name = network_name(key)
        number = price_from_text(amount) if amount is not None else None
        if name and number is not None:
            out[name] = number
    return out or None


RANKING = (
    "Ranked by CrowdCode quality (canonical score in half-point bands; unreviewed services sit at the 3.0 prior; "
    f"within a band, services with {ADOPTED_PAYERS}+ distinct payers in 30 days first), then effective price on your best network (plus ~${TRANSFER_COST_USD:.2f} for one transfer between your own "
    "networks when that network's balance is short), then your balance there. Catalog prices can be dynamic: "
    "inspect the endpoint for the live quote before paying."
)


def find_services(
    query: str,
    *,
    networks: Any = None,
    balances: Any = None,
    candidates: Any = None,
    limit: Any = None,
    conn_factory: Callable[[], Any] | None = None,
    catalogs: CatalogClient | None = None,
) -> dict[str, Any]:
    query = (query or "").strip()[:500]
    if not query:
        return {"ok": False, "error": "query is required", "services": []}
    payer_networks = parse_networks(networks)
    payer_balances = parse_balances(balances)
    try:
        size = max(1, min(MAX_LIMIT, int(limit))) if limit is not None else DEFAULT_LIMIT
    except (TypeError, ValueError):
        size = DEFAULT_LIMIT
    catalogs = catalogs or CATALOGS
    sources: dict[str, dict[str, Any]] = {}

    with ThreadPoolExecutor(max_workers=2) as pool:
        cdp_future = pool.submit(catalogs.cdp_search, query)
        mpp_future = pool.submit(catalogs.mpp_catalog)
        found: list[Candidate] = _ordered(caller_candidates(candidates, payer_networks))
        sources["caller"] = {"ok": True, "count": len(found)}
        for name, future, parse in (
            ("cdp-bazaar", cdp_future, lambda payload: cdp_candidates(payload, payer_networks)),
            ("mpp", mpp_future, lambda payload: mpp_candidates(payload, query, payer_networks)),
        ):
            try:
                parsed = _ordered(parse(future.result(timeout=HTTP_TIMEOUT + 2)))
                sources[name] = {"ok": True, "count": len(parsed)}
                found.extend(parsed)
            except Exception as exc:  # one catalog failing must not hide the others
                sources[name] = {"ok": False, "error": type(exc).__name__}

    evidence: dict[str, dict[str, Any]] = {}
    if conn_factory is not None:
        try:
            with conn_factory() as conn:
                listed = _ordered(directory_candidates(directory_rows(conn), query))
                sources["crowdcode"] = {"ok": True, "count": len(listed)}
                found.extend(listed)
                merged = _merge(found)
                evidence = evidence_by_endpoint(conn, [c.endpoint for c in merged])
        except Exception as exc:
            sources["crowdcode"] = {"ok": False, "error": type(exc).__name__}
            merged = _merge(found)
    else:
        merged = _merge(found)

    ranked = rank(merged, evidence, payer_networks, payer_balances)
    return {
        "ok": True,
        "query": query,
        "networks": list(payer_networks),
        "balances_considered": payer_balances is not None,
        "ranking": RANKING,
        "services": ranked[:size],
        "total_matches": len(ranked),
        "sources": sources,
    }


def _ordered(candidates: list[Candidate]) -> list[Candidate]:
    for index, candidate in enumerate(candidates):
        candidate.order = index
    return candidates


def _merge(found: list[Candidate]) -> list[Candidate]:
    merged: dict[str, Candidate] = {}
    for candidate in found:
        existing = merged.get(candidate.endpoint)
        if existing is None:
            merged[candidate.endpoint] = candidate
        else:
            existing.merge(candidate)
    return list(merged.values())
