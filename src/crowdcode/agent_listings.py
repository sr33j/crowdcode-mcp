"""Listings for services that CrowdCode agents sell.

A hosted CrowdCode agent can publish a paid service at
``https://crowdcode.app/s/<slug>/...``. Every one of those endpoints shares
the host ``crowdcode.app``, so a service first seen in a review that named no
service was listed as "crowdcode.app": the venice-reseller, a word counter
and a text-statistics service all had the same name in the rankings.

The agent declared a name and a description when it deployed the service,
and the platform publishes them, free, at ``https://crowdcode.app/s/<slug>``
(the service's manifest). This module reads them from there:

- **At registration**, with no network call, the service is named after its
  slug, which is at least its own (``identity._fallback_service_name``).
- **Right after registration, at startup, and nightly**, the manifest is read
  and its name and description are stored (``metadata.description``). A
  service that is stopped or deleted answers with no manifest and keeps the
  listing it had.

Only an endpoint on the configured services origin, whose path is
``/s/<slug>``, is treated as an agent service, and only the manifest at that
exact origin is read: what a listing says comes from the platform that hosts
the service, never from the reviewer. A service with a curated
``directory_slug`` keeps its curated name.
"""

from __future__ import annotations

import os
import re
import threading
import traceback
import unicodedata
from datetime import datetime, timezone
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

import httpx
import psycopg
from psycopg.types.json import Jsonb

DEFAULT_SERVICES_ORIGIN = "https://crowdcode.app"
SLUG = re.compile(r"^/s/([a-z0-9-]{3,40})(?:/.*)?$")
NAME_MAX = 120
DESCRIPTION_MAX = 600
FETCH_TIMEOUT = 5.0
LISTING_SOURCE = "crowdcode-agent-service"

Listing = dict[str, str]
Fetcher = Callable[[str], "Listing | None"]


def services_origin() -> str:
    origin = os.environ.get("CROWDCODE_SERVICES_ORIGIN", "").strip() or DEFAULT_SERVICES_ORIGIN
    return origin.rstrip("/")


def _hosts(origin: str) -> set[str]:
    host = (urlsplit(origin).hostname or "").lower()
    bare = host[4:] if host.startswith("www.") else host
    return {bare, f"www.{bare}"} if bare else set()


def agent_service_slug(endpoint: str | None, origin: str | None = None) -> str | None:
    """The slug of a CrowdCode agent service endpoint, or None for anything else."""
    if not endpoint:
        return None
    parsed = urlsplit(endpoint)
    origin = origin or services_origin()
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in _hosts(origin):
        return None
    if parsed.port not in (None, 443):
        return None
    match = SLUG.match(parsed.path or "")
    return match.group(1) if match else None


def _clean(value: Any, limit: int) -> str | None:
    """One line of printable text, bounded; None if nothing is left."""
    if not isinstance(value, str):
        return None
    text = "".join(" " if unicodedata.category(ch)[0] in "CZ" else ch for ch in value)
    text = " ".join(text.split())
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def parse_manifest(slug: str, payload: Any, origin: str | None = None) -> Listing | None:
    """The name and description out of a service manifest, if it is this service's."""
    if not isinstance(payload, dict):
        return None
    origin = origin or services_origin()
    # The manifest names its own URL; it must be the slug that was asked for.
    if agent_service_slug(str(payload.get("url") or ""), origin) != slug:
        return None
    name = _clean(payload.get("name"), NAME_MAX)
    if not name:
        return None
    listing: Listing = {"name": name}
    description = _clean(payload.get("description"), DESCRIPTION_MAX)
    if description:
        listing["description"] = description
    return listing


def fetch_listing(slug: str, *, origin: str | None = None, client: httpx.Client | None = None) -> Listing | None:
    """GET the free manifest. None when the service has none to give (stopped, deleted, a paid root)."""
    origin = origin or services_origin()
    url = f"{origin}/s/{slug}"
    try:
        if client is not None:
            response = client.get(url, timeout=FETCH_TIMEOUT)
        else:
            response = httpx.get(url, timeout=FETCH_TIMEOUT, follow_redirects=False)
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    return parse_manifest(slug, payload, origin)


def _candidates(conn: psycopg.Connection, service_ids: Iterable[str] | None, origin: str) -> list[dict[str, Any]]:
    hosts = sorted(_hosts(origin))
    patterns = [f"https://{host}/s/%" for host in hosts]
    sql = """
        select id, name, canonical_endpoint, metadata
        from services
        where directory_slug is null
          and canonical_endpoint like any(%s)
    """
    params: list[Any] = [patterns]
    if service_ids is not None:
        sql += " and id = any(%s)"
        params.append(list(service_ids))
    return conn.execute(sql, params).fetchall()


def refresh_agent_listings(
    conn: psycopg.Connection,
    *,
    service_ids: Iterable[str] | None = None,
    fetch: Fetcher | None = None,
    origin: str | None = None,
) -> dict[str, int]:
    """Bring agent-service listings up to date with what each agent declared.

    Reads candidates, then fetches every manifest with no transaction open,
    then writes the changes in one short transaction. ``conn`` must be in
    autocommit mode or have no transaction in progress.
    """
    origin = origin or services_origin()
    rows = _candidates(conn, service_ids, origin)
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        # Never hold a transaction (and its locks) across the HTTP calls below.
        conn.commit()
    by_slug: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        slug = agent_service_slug(row["canonical_endpoint"], origin)
        if slug:
            by_slug.setdefault(slug, []).append(row)

    if fetch is None:
        with httpx.Client(follow_redirects=False) as client:
            listings = {slug: fetch_listing(slug, origin=origin, client=client) for slug in by_slug}
    else:
        listings = {slug: fetch(slug) for slug in by_slug}

    stats = {"candidates": len(rows), "updated": 0, "unchanged": 0, "unavailable": 0}
    now = datetime.now(timezone.utc).isoformat()
    with conn.transaction():
        for slug, services in by_slug.items():
            listing = listings.get(slug)
            for row in services:
                if listing is None:
                    stats["unavailable"] += 1
                    continue
                metadata = dict(row["metadata"] or {})
                if row["name"] == listing["name"] and metadata.get("description") == listing.get("description"):
                    stats["unchanged"] += 1
                    continue
                metadata.pop("description", None)
                if listing.get("description"):
                    metadata["description"] = listing["description"]
                metadata["listing_source"] = LISTING_SOURCE
                metadata["listing_refreshed_at"] = now
                conn.execute(
                    "update services set name = %s, metadata = %s where id = %s and directory_slug is null",
                    (listing["name"], Jsonb(metadata), row["id"]),
                )
                stats["updated"] += 1
    return stats


def refresh_in_background(service_ids: Iterable[str] | None = None) -> threading.Thread:
    """Best effort, off the request path: a new listing gets its name a moment after its first review."""
    ids = None if service_ids is None else list(service_ids)

    def run() -> None:
        from crowdcode.db import connect

        try:
            with connect() as conn:
                refresh_agent_listings(conn, service_ids=ids)
                conn.commit()
        except Exception:
            traceback.print_exc()

    thread = threading.Thread(target=run, name="agent-listings", daemon=True)
    thread.start()
    return thread
