"""The free feeds the services index sweeps, and how their listings are normalized.

Two public directories are read in full once a day (SRI-115 ticket 1):

- the CDP x402 Bazaar discovery API, which also carries Coinbase's usage counts
  per resource (``quality.l30DaysUniquePayers``), the only free "who else paid"
  signal; and
- the mpp.dev catalog of MPP services.

Every listing becomes a ``Listing``. Its identity is ``(method, route template)``:

- network ids become CAIP-2 (``base`` and ``eip155:8453`` are one network);
  only Base, Solana and Tempo are kept;
- a listing is *payable* when a hosted agent could pay it: USDC on one of
  those networks, under a scheme the gateway signs;
- route templates collapse: a path segment written as a parameter
  (``:slug``, ``{slug}``) or shaped like an identifier (a UUID, a long hex
  string, a number, a date, an address, a domain name) becomes ``{}`` in the
  key, so a seller that lists one URL per article is one resource.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator
from urllib.parse import urlsplit, urlunsplit

import httpx

USER_AGENT = "CrowdCodeIndex/1 (+https://crowdcode.app/bot)"

BAZAAR_URL = "https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources"
BAZAAR_PAGE = 1000
BAZAAR_MAX_PAGES = 120
MPP_URL = "https://mpp.dev/api/services"
FEED_TIMEOUT = 30.0

BASE = "eip155:8453"
SOLANA = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
TEMPO = "eip155:4217"
PAYABLE_NETWORKS = (BASE, SOLANA, TEMPO)
# The USDC each network's hosted agents hold (Tempo: USDC.e).
USDC = {
    BASE: "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    SOLANA: "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    TEMPO: "0x20c000000000000000000000b9537d11c60e8b50",
}
X402_SCHEMES = frozenset({"exact", "upto"})
MPP_INTENTS = frozenset({"charge"})

_NETWORK_ALIASES = {
    "base": BASE, "base-mainnet": BASE, "base_mainnet": BASE, "eip155:8453": BASE,
    "base-sepolia": "eip155:84532", "base_sepolia": "eip155:84532",
    "solana": SOLANA, "solana-mainnet": SOLANA, "solana_mainnet": SOLANA,
    "solana:mainnet": SOLANA, "solana:mainnet-beta": SOLANA, SOLANA.lower(): SOLANA,
    "tempo": TEMPO, "tempo-mainnet": TEMPO, "tempo:4217": TEMPO, "eip155:4217": TEMPO,
    "polygon": "eip155:137", "arbitrum": "eip155:42161", "optimism": "eip155:10",
    "monad": "eip155:143", "avalanche": "eip155:43114", "ethereum": "eip155:1",
}

# Limits on what one listing stores.
DESCRIPTION_MAX = 1000
NAME_MAX = 120
SCHEMA_MAX = 8000
BODY_MAX = 4000
# Input schemas are kept for listings that could be tracked; the long tail of
# one-payer listings does not need them stored.
SCHEMA_MIN_PAYERS = 3


def caip2(network: Any) -> str | None:
    """CAIP-2 for any spelling the feeds use, or None when it names nothing."""
    if not isinstance(network, str) or not network.strip():
        return None
    raw = network.strip()
    lowered = raw.lower()
    if lowered in _NETWORK_ALIASES:
        return _NETWORK_ALIASES[lowered]
    match = re.fullmatch(r"eip155:(0x[0-9a-f]+|\d+)", lowered)
    if match:
        chain = match.group(1)
        chain_id = int(chain, 16) if chain.startswith("0x") else int(chain)
        return _NETWORK_ALIASES.get(f"eip155:{chain_id}", f"eip155:{chain_id}")
    if ":" in raw:
        namespace, _, reference = raw.partition(":")
        return f"{namespace.lower()}:{reference}"
    return None


def same_asset(network: str, asset: Any) -> bool:
    if not isinstance(asset, str):
        return False
    usdc = USDC.get(network)
    if usdc is None:
        return False
    return asset == usdc if network == SOLANA else asset.lower() == usdc


# --- Route templates ---------------------------------------------------------------

_PARAM = re.compile(r"^(?::[A-Za-z_][\w-]*|\{[^/{}]*\}|\[[^/\[\]]+\]|<[^/<>]+>)$")
_UUID = re.compile(r"^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$", re.I)
_HEX = re.compile(r"^(?:0x)?[0-9a-f]{16,}$", re.I)
_NUMBER = re.compile(r"^\d+$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T_ ][\d:.\-]+Z?)?$")
_BASE58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_DOMAIN = re.compile(r"^(?:[a-z0-9-]+\.){2,}[a-z]{2,}$|^[a-z0-9-]+\.(?:com|org|net|io|ai|xyz|dev|app|co|me)$", re.I)
# Article-style slugs: only for URLs from our own records, never for a feed's
# listings (a feed lists `/crosschain-gas-priority-fee` as a tool of its own).
_SLUG = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+){3,}$|^(?=.*[-_]).{30,}$", re.I)


def _looks_like_id(segment: str) -> bool:
    if _NUMBER.match(segment) or _UUID.match(segment) or _HEX.match(segment) or _DATE.match(segment):
        return True
    if _DOMAIN.match(segment):
        return True
    # A base58 address has digits and both cases; a word of 32+ letters does not.
    return bool(_BASE58.match(segment) and re.search(r"\d", segment)
                and re.search(r"[a-z]", segment) and re.search(r"[A-Z]", segment))


def _param_name(segment: str) -> str | None:
    if not _PARAM.match(segment):
        return None
    return re.sub(r"[^\w-]", "", segment) or "param"


@dataclass(frozen=True)
class Route:
    """A URL reduced to its identity."""

    origin: str
    key_path: str  # "/a/{}/b"
    template_path: str  # "/a/:id/b"
    has_params: bool

    def key(self, method: str) -> str:
        return f"{method} {self.origin}{self.key_path}"

    @property
    def template(self) -> str:
        return f"{self.origin}{self.template_path}"

    @property
    def literal_segments(self) -> list[str | None]:
        return [None if s == "{}" else s for s in self.key_path.strip("/").split("/")] if self.key_path != "/" else []


def route(url: str, *, own_record: bool = False) -> Route | None:
    """The route of a URL: origin lowercased, query and trailing slash dropped,
    parameter and identifier segments collapsed."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if scheme not in ("http", "https") or not host:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    origin = urlunsplit((scheme, host if port is None else f"{host}:{port}", "", "", ""))
    segments = [s for s in parts.path.split("/") if s]
    key, template, params = [], [], False
    for index, segment in enumerate(segments):
        name = _param_name(segment)
        if name is None and (_looks_like_id(segment) or (own_record and index >= 1 and _SLUG.match(segment))):
            name = "id"
        if name is None:
            key.append(segment)
            template.append(segment)
        else:
            params = True
            key.append("{}")
            template.append(f":{name}")
    return Route(origin, "/" + "/".join(key), "/" + "/".join(template), params)


def matches_template(template: Route, url_route: Route) -> bool:
    """Whether a concrete route is an instance of a listed template."""
    if template.origin != url_route.origin or not template.has_params:
        return False
    a, b = template.key_path.split("/"), url_route.key_path.split("/")
    return len(a) == len(b) and all(x == "{}" or x == y for x, y in zip(a, b))


# --- Listings ----------------------------------------------------------------------


@dataclass(slots=True)
class Listing:
    source: str
    method: str
    url: str
    protocol: str = "x402"
    listing_type: str = "http"
    description: str | None = None
    service_name: str | None = None
    networks: set[str] = field(default_factory=set)
    payable: bool = False
    pay_to: list[str] = field(default_factory=list)
    price_atomic: int | None = None
    price_asset: str | None = None
    price_network: str | None = None
    price_scheme: str | None = None
    payers_30d: int | None = None
    calls_30d: int | None = None
    last_called_at: datetime | None = None
    # Compact JSON text: a sweep holds tens of thousands of listings at once.
    input_schema: str | None = None
    probe_body: str | None = None
    # Set by the internal sources: the service row a purchase or hosted route belongs to.
    service_id: str | None = None
    own_record: bool = False


def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned[:limit] or None


def _int(value: Any) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _capped(value: Any, limit: int) -> str | None:
    """Compact JSON text of a value, or None when it is absent or too large."""
    if value is None:
        return None
    try:
        text = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    # jsonb refuses NUL, escaped or not.
    text = text.replace("\\u0000", "").replace("\x00", "")
    return text if len(text) <= limit else None


def _payee(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    payee = value.strip()
    return payee.lower() if payee.startswith("0x") else payee


_NETWORK_RANK = {BASE: 0, SOLANA: 1, TEMPO: 2}


def x402_offers(accepts: Any) -> list[dict[str, Any]]:
    """The accepts entries on our networks, normalized, best first."""
    offers = []
    for accept in accepts if isinstance(accepts, list) else []:
        if not isinstance(accept, dict):
            continue
        network = caip2(accept.get("network"))
        if network not in _NETWORK_RANK:
            continue
        scheme = str(accept.get("scheme") or "").lower() or None
        amount = _int(accept.get("amount") if accept.get("amount") is not None else accept.get("maxAmountRequired"))
        asset = accept.get("asset") if isinstance(accept.get("asset"), str) else None
        payable = scheme in X402_SCHEMES and same_asset(network, asset)
        offers.append({
            "network": network, "scheme": scheme, "amount": amount,
            "asset": (asset.lower() if asset and asset.startswith("0x") else asset),
            "payee": _payee(accept.get("payTo")), "payable": payable,
        })
    offers.sort(key=lambda o: (not o["payable"], _NETWORK_RANK[o["network"]]))
    return offers


def _apply_offers(listing: Listing, offers: list[dict[str, Any]]) -> None:
    listing.networks = {o["network"] for o in offers}
    listing.payable = any(o["payable"] for o in offers)
    listing.pay_to = list(dict.fromkeys(o["payee"] for o in offers if o["payee"]))
    if offers:
        best = offers[0]
        listing.price_atomic = best["amount"]
        listing.price_asset = best["asset"]
        listing.price_network = best["network"]
        listing.price_scheme = best["scheme"]


_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def parse_x402_item(item: Any, source: str = "bazaar") -> Listing | None:
    """One x402 discovery item, from any of the feeds that share the shape.

    The Bazaar, PayAI, Dexter and thirdweb all list ``resource`` + ``accepts``;
    they differ in where the method, description and input schema live. Only
    the Bazaar's usage counts are kept (the others copy them, or have none).
    A feed that names no method leaves it empty, and the sweep attaches the
    listing to the resource another feed listed at the same URL.
    """
    if not isinstance(item, dict) or not isinstance(item.get("resource"), str):
        return None
    metadata = _dict(item.get("metadata"))
    accepts = item.get("accepts") if isinstance(item.get("accepts"), list) else []
    first = _dict(accepts[0]) if accepts else {}
    bazaar = _dict(_dict(item.get("extensions")).get("bazaar"))
    info_input = _dict(_dict(bazaar.get("info")).get("input")) or _dict(_dict(first.get("outputSchema")).get("input")) \
        or _dict(metadata.get("input"))
    listing_type = "mcp" if item.get("type") == "mcp" else "http"
    method = str(item.get("method") or metadata.get("method") or info_input.get("method") or "").upper()
    if not method:
        method = "POST" if listing_type == "mcp" else ("GET" if source == "bazaar" else "")
    if method and method not in _METHODS:
        method = "POST"
    quality = _dict(item.get("quality")) if source == "bazaar" else {}
    listing = Listing(
        source=source, method=method, url=item["resource"].strip(), protocol="x402",
        listing_type=listing_type,
        description=_text(item.get("description") or metadata.get("description") or first.get("description"),
                          DESCRIPTION_MAX),
        service_name=_text(item.get("serviceName") or metadata.get("serviceName") or metadata.get("displayName"),
                           NAME_MAX),
        payers_30d=_int(quality.get("l30DaysUniquePayers")),
        calls_30d=_int(quality.get("l30DaysTotalCalls")),
        last_called_at=_time(quality.get("lastCalledAt")),
    )
    _apply_offers(listing, x402_offers(accepts))
    if (listing.payers_30d or 0) >= SCHEMA_MIN_PAYERS:
        schema = bazaar.get("schema") if isinstance(bazaar, dict) else None
        input_schema = ((schema or {}).get("properties") or {}).get("input") if isinstance(schema, dict) else None
        listing.input_schema = _capped({"info": info_input, "schema": input_schema}, SCHEMA_MAX) \
            or _capped({"info": info_input}, SCHEMA_MAX)
    if method in ("POST", "PUT", "PATCH") and (listing.payers_30d or 0) >= 1:
        listing.probe_body = _capped(info_input.get("body"), BODY_MAX)
    return listing


def parse_bazaar_item(item: Any) -> Listing | None:
    return parse_x402_item(item, "bazaar")


def parse_mpp_catalog(payload: Any) -> list[Listing]:
    services = payload.get("services") if isinstance(payload, dict) else payload
    listings = []
    for service in services if isinstance(services, list) else []:
        if not isinstance(service, dict) or not isinstance(service.get("serviceUrl"), str):
            continue
        base = service["serviceUrl"].rstrip("/")
        for endpoint in service.get("endpoints") or []:
            if not isinstance(endpoint, dict) or not isinstance(endpoint.get("path"), str):
                continue
            payment = endpoint.get("payment")
            if not isinstance(payment, dict):
                continue  # a free endpoint: nothing to pay, nothing to index
            method = str(endpoint.get("method") or "GET").upper()
            rail = str(payment.get("method") or "").lower()
            intent = str(payment.get("intent") or "").lower()
            currency = payment.get("currency") if isinstance(payment.get("currency"), str) else None
            network = TEMPO if rail == "tempo" else caip2(payment.get("network") or (
                f"eip155:{payment['chainId']}" if payment.get("chainId") else ("base" if rail == "evm" else None)))
            path = endpoint["path"] if endpoint["path"].startswith("/") else "/" + endpoint["path"]
            listing = Listing(
                source="mpp", method=method, url=base + path, protocol="mpp",
                description=_text(endpoint.get("description") or service.get("description"), DESCRIPTION_MAX),
                service_name=_text(service.get("name"), NAME_MAX),
            )
            if network in _NETWORK_RANK:
                listing.networks = {network}
                listing.payable = intent in MPP_INTENTS and same_asset(network, currency)
                listing.price_atomic = _int(payment.get("amount"))
                listing.price_asset = currency.lower() if currency and currency.startswith("0x") else currency
                listing.price_network = network
                listing.price_scheme = intent or None
            listings.append(listing)
    return listings


# --- Fetching ----------------------------------------------------------------------


class FeedError(RuntimeError):
    pass


def _get_json(client: httpx.Client, url: str, params: dict[str, Any] | None = None, attempts: int = 3) -> Any:
    last: str = ""
    for attempt in range(attempts):
        try:
            response = client.get(url, params=params)
            if response.status_code == 200:
                return response.json()
            last = f"HTTP {response.status_code}"
        except (httpx.HTTPError, ValueError) as error:
            last = f"{type(error).__name__}: {error}"
        if attempt + 1 < attempts:
            import time
            time.sleep(2 * (attempt + 1))
    raise FeedError(f"{url}: {last}")


def feed_client() -> httpx.Client:
    return httpx.Client(timeout=FEED_TIMEOUT, follow_redirects=False,
                        headers={"User-Agent": USER_AGENT, "Accept": "application/json"})


PAYAI_URL = "https://facilitator.payai.network/discovery/resources"
DEXTER_URL = "https://x402.dexter.cash/discovery/resources"
THIRDWEB_URL = "https://api.thirdweb.com/v1/payments/x402/discovery/resources"
THIRDWEB_PAGE = 100  # its maximum
THIRDWEB_WORKERS = 4
MAX_PAGES = 1000


def x402_pages(client: httpx.Client, url: str, source: str, *, page: int = BAZAAR_PAGE,
               workers: int = 1) -> Iterator[list[Listing]]:
    """Every page of an x402 discovery feed, parsed. Raises FeedError on a page it cannot read.

    The first page gives the total; with ``workers`` > 1 the rest are read a
    few at a time (thirdweb serves 100 per page, so it has ~470 pages).
    """
    payload = _get_json(client, url, {"limit": page, "offset": 0})
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise FeedError(f"{url}: no items")
    total = _int(_dict(payload.get("pagination")).get("total"))
    yield [listing for listing in (parse_x402_item(i, source) for i in items) if listing]
    if not items:
        return
    if total is None:
        # No total: walk until a short or empty page.
        offset = len(items)
        while len(items) >= page and offset < page * MAX_PAGES:
            items = _get_json(client, url, {"limit": page, "offset": offset}).get("items") or []
            if items:
                yield [listing for listing in (parse_x402_item(i, source) for i in items) if listing]
            offset += len(items)
        return
    if len(items) >= total:
        return
    offsets = list(range(len(items), min(total, page * MAX_PAGES), page))

    def read(offset: int) -> list[Listing]:
        body = _get_json(client, url, {"limit": page, "offset": offset})
        return [listing for listing in (parse_x402_item(i, source) for i in (body.get("items") or [])) if listing]

    if workers <= 1:
        for offset in offsets:
            yield read(offset)
        return
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for start in range(0, len(offsets), workers * 4):
            yield from pool.map(read, offsets[start:start + workers * 4])


def fetch_bazaar(client: httpx.Client, url: str = BAZAAR_URL) -> Iterator[list[Listing]]:
    return x402_pages(client, url, "bazaar")


def fetch_payai(client: httpx.Client, url: str = PAYAI_URL) -> Iterator[list[Listing]]:
    return x402_pages(client, url, "payai")


def fetch_dexter(client: httpx.Client, url: str = DEXTER_URL) -> Iterator[list[Listing]]:
    return x402_pages(client, url, "dexter")


def fetch_thirdweb(client: httpx.Client, url: str = THIRDWEB_URL) -> Iterator[list[Any]]:
    return x402_pages(client, url, "thirdweb", page=THIRDWEB_PAGE, workers=THIRDWEB_WORKERS)


def fetch_mpp(client: httpx.Client, url: str = MPP_URL) -> Iterator[list[Any]]:
    """mpp.dev's listings, then the free model catalogs its services publish."""
    from crowdcode.index_models import mpp_catalogs
    payload = _get_json(client, url)
    yield parse_mpp_catalog(payload)
    services = payload.get("services") if isinstance(payload, dict) else payload
    yield mpp_catalogs(services if isinstance(services, list) else [])
