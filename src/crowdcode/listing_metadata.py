"""Descriptions for listings that have none, read from the service itself.

Most services enter the directory through their first review, and a review
names an endpoint, not what the service does: on 2026-11-04, 345 of 349
listings had no description. Search matched them on a name and a URL, so
"sell-side research" never found **Drip — Paid Financial Newsletter Research**
(`dripstack.xyz/api/v1/publications/{publicationSlug}/{postSlug}`), whose own
OpenAPI document calls it "a data feed and API for tracking smart money —
what top investors are buying, selling, and thinking" (SRI-86).

Every service already describes itself somewhere a machine can read. This
module reads, in order of how specifically the text is about *this endpoint*:

1. the endpoint's own 402 (x402 ``resource.description`` or
   ``accepts[].description``; an MPP challenge's ``description``);
2. the origin's OpenAPI document: the operation at this path, then ``info``;
3. the mpp.dev catalog entry the service published, for MPP services;
4. the origin's ``/llms.txt`` summary line;
5. the origin's landing page ``<meta name="description">``.

The first good text becomes ``metadata.description`` (what listings show);
everything found, plus request field names from the 402 and OpenAPI, becomes
``metadata.listing_text``, which only search reads. Nothing is ever written
that the service did not say about itself, ``metadata.description_source``
says where it came from, and a description a person or an agent declared
(``agent_listings``) is never replaced.

Every fetch is an unpaid GET (or the same ``{}`` POST the payee probe sends)
to a host whose every address is public, with redirects followed by hand and
re-checked hop by hop, a size cap and a short timeout, and never with a
database connection open.
"""

from __future__ import annotations

import base64
import html
import json
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable
from urllib.parse import urljoin, urlsplit

import httpx
import psycopg
from psycopg.types.json import Jsonb

from crowdcode.advertised import public_endpoint
from crowdcode.agent_listings import LISTING_SOURCE as AGENT_LISTING_SOURCE
from crowdcode.agent_listings import _clean, agent_service_slug

FETCH_TIMEOUT = 6.0
# However slowly a server answers, one fetch (with its redirects) ends here.
FETCH_DEADLINE = 15.0
MAX_REDIRECTS = 3
MAX_BYTES = 1_500_000
DESCRIPTION_MAX = 600
LISTING_TEXT_MAX = 1500
MIN_DESCRIPTION_CHARS = 24
WORKERS = 8
# How long before a listing is looked at again: one that yielded nothing may
# have published something since; one that was filled may have changed.
RETRY_EMPTY = timedelta(days=7)
REFRESH_FILLED = timedelta(days=30)
MPP_CATALOG_URL = "https://mpp.dev/api/services"
USER_AGENT = "CrowdCode-directory/1 (+https://crowdcode.app; listing metadata)"

# Text that is about HTTP, not about the service.
_BOILERPLATE = re.compile(
    r"^(payment required|redirecting|not found|unauthorized|forbidden|bad request|"
    r"internal server error|method not allowed|ok|hello world|x402|mpp|api)\b[.!]?$",
    re.IGNORECASE,
)
_PATH_PARAM = re.compile(r"^(\{[^/]+\}|:[^/]+)$")


# Sources that describe this endpoint, as opposed to the whole origin. A host that
# sells forty tools publishes one llms.txt for all of them; that text says what the
# host does, not what this endpoint does, and only describes a listing that has
# nothing more specific.
ENDPOINT_SOURCES = frozenset({"x402", "mpp", "openapi operation", "mpp.dev endpoint"})


@dataclass
class Found:
    """What one service said about itself, most specific first."""

    snippets: list[tuple[str, str, str]] = field(default_factory=list)  # (source, url, text)
    fields: list[str] = field(default_factory=list)

    def add(self, source: str, url: str, text: Any) -> None:
        cleaned = _clean(html.unescape(text), DESCRIPTION_MAX) if isinstance(text, str) else None
        if not cleaned:
            return
        lowered = cleaned.lower()
        for index, (_, _, existing) in enumerate(self.snippets):
            if lowered in existing.lower():
                return
            if existing.lower() in lowered and len(existing) < len(cleaned):
                # The same words and more: keep the fuller text at the better source's place.
                self.snippets[index] = (self.snippets[index][0], self.snippets[index][1], cleaned)
                return
        self.snippets.append((source, url, cleaned))

    def add_fields(self, names: Iterable[Any]) -> None:
        for name in names:
            if isinstance(name, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.\-]{0,40}", name) and name not in self.fields:
                self.fields.append(name)

    def _scoped(self) -> list[tuple[str, str, str]]:
        specific = [s for s in self.snippets if s[0] in ENDPOINT_SOURCES]
        return specific or self.snippets

    def description(self, name: str | None) -> tuple[str, str, str] | None:
        for source, url, text in self._scoped() + self.snippets:
            if usable_description(text, name):
                return source, url, text
        return None

    def listing_text(self) -> str | None:
        parts = [text for _, _, text in self._scoped()]
        if self.fields:
            parts.append("fields: " + " ".join(self.fields[:40]))
        joined = " | ".join(parts)
        return _clean(joined, LISTING_TEXT_MAX)


def usable_description(text: str, name: str | None = None) -> bool:
    stripped = text.strip()
    if len(stripped) < MIN_DESCRIPTION_CHARS or len(stripped.split()) < 4:
        return False
    if _BOILERPLATE.match(stripped):
        return False
    if name and stripped.lower().rstrip(".") == name.strip().lower().rstrip("."):
        return False
    return not stripped.startswith(("{", "<", "["))


# --- Fetching -------------------------------------------------------------------


Fetch = Callable[[str, str], "httpx.Response | None"]


def safe_fetch(client: httpx.Client, method: str, url: str) -> httpx.Response | None:
    """One request to a public host, redirects re-checked per hop, body and total time capped."""
    deadline = time.monotonic() + FETCH_DEADLINE
    for _ in range(MAX_REDIRECTS + 1):
        if not public_endpoint(url):
            return None
        try:
            request = client.build_request(
                method, url, json={} if method == "POST" else None,
                headers={"accept": "application/json, text/html;q=0.9, */*;q=0.5", "user-agent": USER_AGENT},
            )
            response = client.send(request, stream=True)
        except httpx.HTTPError:
            return None
        try:
            if response.status_code in (301, 302, 303, 307, 308) and response.headers.get("location"):
                url = urljoin(url, response.headers["location"])
                if response.status_code == 303:
                    method = "GET"
                continue
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > MAX_BYTES or time.monotonic() > deadline:
                    return None
            # iter_bytes() already decoded the body: drop the headers that describe the wire form.
            headers = [(k, v) for k, v in response.headers.multi_items()
                       if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")]
            return httpx.Response(response.status_code, headers=headers, content=bytes(body),
                                  request=httpx.Request(method, url))
        except httpx.HTTPError:
            return None
        finally:
            response.close()
    return None


def _json(response: httpx.Response | None) -> Any:
    if response is None:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def _templated(endpoint: str) -> bool:
    return any(_PATH_PARAM.match(part) or "{" in part for part in urlsplit(endpoint).path.split("/"))


def _origin(endpoint: str) -> str:
    parts = urlsplit(endpoint)
    return f"{parts.scheme}://{parts.netloc}"


# --- What each source says --------------------------------------------------------


def read_402(found: Found, response: httpx.Response | None, url: str) -> None:
    if response is None or response.status_code != 402:
        return
    payloads = []
    body = _json(response)
    if isinstance(body, dict):
        payloads.append(body)
    header = response.headers.get("payment-required")
    if header:
        try:
            decoded = json.loads(base64.b64decode(header + "=" * (-len(header) % 4)))
            if isinstance(decoded, dict):
                payloads.append(decoded)
        except Exception:
            pass
    for payload in payloads:
        resource = payload.get("resource")
        if isinstance(resource, dict):
            found.add("x402", url, resource.get("description"))
        bazaar = (payload.get("extensions") or {}).get("bazaar") if isinstance(payload.get("extensions"), dict) else None
        if isinstance(bazaar, dict):
            info = bazaar.get("info") if isinstance(bazaar.get("info"), dict) else {}
            found.add("x402", url, bazaar.get("description") or info.get("description"))
            _schema_fields(found, info.get("input"))
        for accept in payload.get("accepts") or []:
            if isinstance(accept, dict):
                found.add("x402", url, accept.get("description"))
                output = accept.get("outputSchema")
                if isinstance(output, dict):
                    _schema_fields(found, output.get("input"))
    challenge = response.headers.get("www-authenticate") or ""
    for match in re.finditer(r'description="([^"]{1,1000})"', challenge):
        found.add("mpp", url, match.group(1))
    for match in re.finditer(r'request="([A-Za-z0-9_\-=]+)"', challenge):
        raw = match.group(1)
        try:
            request = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        except Exception:
            continue
        if isinstance(request, dict):
            found.add("mpp", url, request.get("description"))


def _schema_fields(found: Found, schema: Any, depth: int = 0) -> None:
    if not isinstance(schema, dict) or depth > 3:
        return
    for key in ("queryParams", "bodyFields", "body", "properties", "params", "query"):
        value = schema.get(key)
        if isinstance(value, dict):
            nested = value.get("properties") if isinstance(value.get("properties"), dict) else value
            found.add_fields(nested.keys())
    if isinstance(schema.get("schema"), dict):
        _schema_fields(found, schema["schema"], depth + 1)


def _path_matches(template: str, path: str) -> bool:
    a = [p for p in template.strip("/").split("/") if p]
    b = [p for p in path.strip("/").split("/") if p]
    if len(a) != len(b):
        return False
    return all(x == y or _PATH_PARAM.match(x) or _PATH_PARAM.match(y) for x, y in zip(a, b))


def read_openapi(found: Found, document: Any, url: str, endpoint: str) -> None:
    if not isinstance(document, dict) or not isinstance(document.get("paths"), dict):
        return
    path = urlsplit(endpoint).path or "/"
    servers = [s.get("url") for s in document.get("servers") or [] if isinstance(s, dict) and isinstance(s.get("url"), str)]
    prefixes = {urlsplit(s).path.rstrip("/") for s in servers} | {""}
    for template, operations in document["paths"].items():
        if not isinstance(operations, dict) or not isinstance(template, str):
            continue
        if not any(_path_matches(prefix + template, path) for prefix in prefixes):
            continue
        for method in ("post", "get", "put"):
            operation = operations.get(method)
            if isinstance(operation, dict):
                summary, detail = operation.get("summary"), operation.get("description")
                if isinstance(summary, str) and isinstance(detail, str) and detail.strip():
                    found.add("openapi operation", url, f"{summary.rstrip('.')}. {detail}")
                else:
                    found.add("openapi operation", url, detail or summary)
                found.add_fields(p.get("name") for p in operation.get("parameters") or [] if isinstance(p, dict))
                content = ((operation.get("requestBody") or {}).get("content") or {}) if isinstance(operation.get("requestBody"), dict) else {}
                for media in content.values():
                    if isinstance(media, dict):
                        _schema_fields(found, media.get("schema"))
                break
        break
    info = document.get("info")
    if isinstance(info, dict):
        summary, detail = info.get("summary"), info.get("description")
        found.add("openapi", url, summary if isinstance(summary, str) and len(summary) >= MIN_DESCRIPTION_CHARS else None)
        found.add("openapi", url, detail)


def read_llms(found: Found, response: httpx.Response | None, url: str) -> None:
    if response is None or response.status_code != 200 or "html" in response.headers.get("content-type", ""):
        return
    for line in response.text.splitlines()[:40]:
        if line.startswith(">"):
            found.add("llms.txt", url, line.lstrip("> "))
            return


_META = re.compile(r"<meta\s+[^>]*>", re.IGNORECASE)
_ATTR = re.compile(r'([a-zA-Z:-]+)\s*=\s*("([^"]*)"|\'([^\']*)\')')


def read_landing(found: Found, response: httpx.Response | None, url: str) -> None:
    if response is None or response.status_code != 200 or "html" not in response.headers.get("content-type", ""):
        return
    head = response.text[:200_000]
    best: dict[str, str] = {}
    for tag in _META.findall(head):
        attrs = {m.group(1).lower(): (m.group(3) if m.group(3) is not None else m.group(4)) for m in _ATTR.finditer(tag)}
        key = (attrs.get("name") or attrs.get("property") or "").lower()
        if key in ("description", "og:description", "twitter:description") and attrs.get("content"):
            best.setdefault(key, attrs["content"])
    found.add("landing page", url, best.get("description") or best.get("og:description") or best.get("twitter:description"))


def read_mpp_catalog(found: Found, catalog: Any, endpoint: str) -> None:
    for service in (catalog or {}).get("services") or [] if isinstance(catalog, dict) else []:
        base = str(service.get("serviceUrl") or "").rstrip("/")
        if not base or not endpoint.startswith(base):
            continue
        rest = endpoint[len(base):] or "/"
        for item in service.get("endpoints") or []:
            if isinstance(item, dict) and _path_matches(str(item.get("path") or "/"), rest):
                found.add("mpp.dev endpoint", MPP_CATALOG_URL, item.get("description"))
        found.add("mpp.dev catalog", MPP_CATALOG_URL, service.get("description"))


# --- One service, then many ----------------------------------------------------------


class OriginCache:
    """Origin documents fetched once per backfill, however many endpoints share them."""

    def __init__(self, fetch: Fetch) -> None:
        self._fetch = fetch
        self._lock = threading.Lock()
        self._docs: dict[str, Any] = {}

    def get(self, url: str) -> httpx.Response | None:
        with self._lock:
            if url in self._docs:
                return self._docs[url]
            event = threading.Event()
            self._docs[url] = event
        response = self._fetch("GET", url)
        with self._lock:
            self._docs[url] = response
        event.set()
        return response

    def __getitem__(self, url: str) -> httpx.Response | None:
        value = self.get(url)
        while isinstance(value, threading.Event):
            value.wait(FETCH_TIMEOUT * (MAX_REDIRECTS + 2))
            with self._lock:
                value = self._docs.get(url)
        return value


def describe_endpoint(endpoint: str, *, fetch: Fetch, origins: OriginCache, mpp_catalog: Any = None,
                      mpp: bool = False) -> Found:
    found = Found()
    if not _templated(endpoint):
        response = fetch("POST", endpoint)
        if response is None or response.status_code != 402:
            response = fetch("GET", endpoint) or response
        read_402(found, response, endpoint)
    if mpp and mpp_catalog is not None:
        read_mpp_catalog(found, mpp_catalog, endpoint)
    origin = _origin(endpoint)
    for path in ("/openapi.json", "/.well-known/openapi.json"):
        document = _json(origins[origin + path])
        if isinstance(document, dict) and document.get("paths"):
            # The document may live where the origin redirects to.
            read_openapi(found, document, origin + path, endpoint)
            break
    read_llms(found, origins[origin + "/llms.txt"], origin + "/llms.txt")
    read_landing(found, origins[origin + "/"], origin + "/")
    return found


def _due(row: dict[str, Any], now: datetime) -> bool:
    metadata = row.get("metadata") or {}
    if metadata.get("listing_source") == AGENT_LISTING_SOURCE or agent_service_slug(row.get("canonical_endpoint")):
        return False  # declared by the agent that sells it, in its manifest (agent_listings)
    if metadata.get("description") and not metadata.get("description_source"):
        return False  # curated or declared some other way: never replaced
    checked = metadata.get("listing_checked_at")
    if not checked:
        return True
    try:
        at = datetime.fromisoformat(checked)
    except ValueError:
        return True
    return now - at >= (REFRESH_FILLED if metadata.get("description_source") else RETRY_EMPTY)


def fill_descriptions(
    conn: psycopg.Connection,
    *,
    service_ids: Iterable[str] | None = None,
    fetch: Fetch | None = None,
    mpp_catalog: Any = None,
    apply: bool = True,
    force: bool = False,
    now: datetime | None = None,
    report: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, int]:
    """Describe every due listing from its own metadata.

    Reads candidates, closes the transaction, fetches with no connection in
    use, then writes in one short transaction. ``conn`` must be in autocommit
    mode or have no transaction in progress.
    """
    now = now or datetime.now(timezone.utc)
    sql = "select id, name, canonical_endpoint, payment_provider, metadata from services where canonical_endpoint is not null"
    params: list[Any] = []
    if service_ids is not None:
        sql += " and id = any(%s)"
        params.append(list(service_ids))
    rows = [r for r in conn.execute(sql, params).fetchall() if force or _due(r, now)]
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        conn.commit()  # never hold a transaction across the calls below
    stats = {"candidates": len(rows), "described": 0, "nothing_found": 0, "unchanged": 0}
    if not rows:
        return stats

    client: httpx.Client | None = None
    if fetch is None:
        client = httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=False)
        fetch = lambda method, url: safe_fetch(client, method, url)  # noqa: E731
    try:
        if mpp_catalog is None and any(r.get("payment_provider") == "mppx" for r in rows):
            mpp_catalog = _json(fetch("GET", MPP_CATALOG_URL))
        origins = OriginCache(fetch)
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            results = list(pool.map(
                lambda r: _describe_safely(r, fetch, origins, mpp_catalog), rows))
    finally:
        if client is not None:
            client.close()

    stamp = now.isoformat()
    updates = []
    for row, found in zip(rows, results):
        metadata = row.get("metadata") or {}
        best = found.description(row.get("name")) if found else None
        listing_text = found.listing_text() if found else None
        patch: dict[str, Any] = {"listing_checked_at": stamp}
        if best:
            source, url, text = best
            patch.update({"description": text, "description_source": source, "description_url": url})
            stats["described"] += 1
            if metadata.get("description") == text and metadata.get("listing_text") == listing_text:
                stats["unchanged"] += 1
        else:
            stats["nothing_found"] += 1
        if listing_text:
            patch["listing_text"] = listing_text
        if report:
            report({"service_id": row["id"], "name": row.get("name"), "endpoint": row["canonical_endpoint"],
                    "description": best[2] if best else None, "source": best[0] if best else None,
                    "listing_text": listing_text})
        updates.append((Jsonb(patch), row["id"]))
    if apply and updates:
        with conn.transaction():
            for patch, service_id in updates:
                # Merged, not replaced: other keys (payee corrections, an agent's listing) are
                # never touched, and a description someone declared meanwhile is never replaced.
                conn.execute(
                    """
                    update services set metadata = metadata || %s
                    where id = %s
                      and (metadata->>'description' is null or metadata ? 'description_source')
                      and coalesce(metadata->>'listing_source', '') <> %s
                    """,
                    (patch, service_id, AGENT_LISTING_SOURCE),
                )
    return stats


def _describe_safely(row: dict[str, Any], fetch: Fetch, origins: OriginCache, mpp_catalog: Any) -> Found | None:
    try:
        return describe_endpoint(row["canonical_endpoint"], fetch=fetch, origins=origins, mpp_catalog=mpp_catalog,
                                 mpp=row.get("payment_provider") == "mppx")
    except Exception:
        traceback.print_exc()
        return None


def fill_in_background(service_ids: Iterable[str] | None = None, *, then: Callable[[], None] | None = None) -> threading.Thread:
    """Best effort, off the request path: a new listing is described a moment after its first review."""
    ids = None if service_ids is None else list(service_ids)

    def run() -> None:
        from crowdcode.db import connect

        try:
            with connect() as conn:
                fill_descriptions(conn, service_ids=ids)
                conn.commit()
        except Exception:
            traceback.print_exc()
        if then is not None:
            try:
                then()
            except Exception:
                traceback.print_exc()

    thread = threading.Thread(target=run, name="listing-metadata", daemon=True)
    thread.start()
    return thread
