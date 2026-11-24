"""CrowdCode's hosted services, kept in the index by CrowdCode itself.

A hosted agent's service (``https://crowdcode.app/s/<slug>/<route>``) used to
enter the index only when somebody first reviewed it, under whatever URL that
reviewer used, and never moved after that. So a service nobody had reviewed
was missing from the rankings, and a renamed one (``venice-reseller`` became
``venice-proxy``) kept its reviews under the old URL, where its owner's page,
looking it up by the new one, found nothing.

Now CrowdCode's control plane pushes the whole list of hosted services after
every deploy, rename, stop and delete (``POST /api/internal/hosted-services/sync``).
For each route of each service:

- the row is found by a stable key, the CrowdCode service id and the route
  (identifier ``crowdcode_service``), else by any URL the service has had,
  else it is created, live services only;
- its canonical endpoint becomes the current URL, and every URL it has had
  stays an ``api_endpoint`` identifier, so a review naming an old URL still
  lands on the same row;
- its name, description and status come from CrowdCode, except where a
  curated directory entry names it.

A service missing from the list, or reported deleted, is marked
``hosted_status = 'deleted'`` and leaves the rankings; its reviews stay.
Only endpoints under the configured services origin are accepted.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

import psycopg
from psycopg.types.json import Jsonb

from crowdcode.agent_listings import LISTING_SOURCE, _clean, agent_service_slug, services_origin
from crowdcode.identity import build_identity, canonical_origin, create_service_from_identity, normalize_api_endpoint, _insert_identifier

KEY_TYPE = "crowdcode_service"
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
METHODS = {"GET", "POST"}
STATUSES = {"live", "stopped", "failed", "deploying", "deleted"}
EVM = re.compile(r"^0x[0-9a-fA-F]{40}$")
SOLANA = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
MAX_SERVICES = 500


class SyncError(ValueError):
    """The pushed list is malformed; nothing was written."""


def _route_url(base: str, path: str) -> str:
    return base if path == "/" else f"{base}{path}"


def parse_services(payload: Any, origin: str | None = None) -> list[dict[str, Any]]:
    """Validates the pushed list. Every URL is built here from slugs, never taken from the body."""
    origin = (origin or services_origin()).rstrip("/")
    services = payload.get("services") if isinstance(payload, dict) else None
    if not isinstance(services, list) or len(services) > MAX_SERVICES:
        raise SyncError("services must be a list of at most %d" % MAX_SERVICES)
    out = []
    for raw in services:
        if not isinstance(raw, dict):
            raise SyncError("each service must be an object")
        sid, slug, status = str(raw.get("id") or ""), str(raw.get("slug") or ""), str(raw.get("status") or "")
        if not UUID.match(sid) or not SLUG.match(slug) or status not in STATUSES:
            raise SyncError(f"service {sid or '?'}: id, slug and status are required")
        aliases = [a for a in raw.get("aliases") or [] if isinstance(a, str) and SLUG.match(a) and a != slug][:10]
        routes = []
        for route in raw.get("routes") or []:
            method, path = str((route or {}).get("method") or "").upper(), str((route or {}).get("path") or "")
            if method in METHODS and path.startswith("/") and len(path) <= 200 and not re.search(r"[\s?#]", path):
                routes.append((method, path.rstrip("/") or "/", _clean((route or {}).get("description"), 600)))
        payee = raw.get("payee")
        if payee is not None and not (isinstance(payee, str) and (EVM.match(payee) or SOLANA.match(payee))):
            raise SyncError(f"service {sid}: payee must be an address")
        out.append({
            "id": sid, "slug": slug, "status": status, "aliases": aliases, "routes": routes[:20], "payee": payee,
            "name": _clean(raw.get("name"), 120), "description": _clean(raw.get("description"), 600),
            "base": f"{origin}/s/{slug}", "alias_bases": [f"{origin}/s/{a}" for a in aliases],
        })
    return out


def _find(conn: psycopg.Connection, key: str, urls: Iterable[str]) -> dict[str, Any] | None:
    """The row for one route: by its stable key first, then by any URL it has had."""
    row = conn.execute(
        "select s.* from service_identifiers si join services s on s.id = si.service_id "
        "where si.identifier_type = %s and si.identifier_value = %s", (KEY_TYPE, key)).fetchone()
    if row:
        return row
    for url in urls:
        row = conn.execute(
            "select s.* from service_identifiers si join services s on s.id = si.service_id "
            "where si.identifier_type = 'api_endpoint' and si.identifier_value = %s", (url,)).fetchone()
        if row:
            return row
    return None


def sync_hosted_services(conn: psycopg.Connection, services: list[dict[str, Any]], *, origin: str | None = None) -> dict[str, int]:
    """Applies a pushed list (see ``parse_services``). Runs in the caller's transaction."""
    origin = (origin or services_origin()).rstrip("/")
    stats = {"created": 0, "updated": 0, "moved": 0, "deleted": 0, "skipped": 0}
    seen: set[str] = set()
    conn.execute("select pg_advisory_xact_lock(hashtextextended('crowdcode.hosted.sync', 0))")
    for service in services:
        for method, path, route_description in service["routes"]:
            key = f"{service['id']}:{method} {path}"
            url = normalize_api_endpoint(_route_url(service["base"], path))
            old = [normalize_api_endpoint(_route_url(base, path)) for base in service["alias_bases"]]
            row = _find(conn, key, [url, *old])
            if row is None:
                if service["status"] != "live" or not service["payee"]:
                    stats["skipped"] += 1
                    continue
                created = create_service_from_identity(conn, build_identity(
                    api_endpoint=url, payment_provider="x402", payment_target_ref=service["payee"], service_name=service["name"] or service["slug"]))
                if created.row is None:
                    stats["skipped"] += 1
                    continue
                row, fresh = created.row, True
            else:
                fresh = False
            seen.add(row["id"])
            moved = row.get("canonical_endpoint") != url
            metadata = {
                "listing_source": LISTING_SOURCE, "crowdcode_service_id": service["id"], "hosted_slug": service["slug"],
                "hosted_status": service["status"], "hosted_route": f"{method} {path}",
                # Each route reads as what it does (SRI-203): venice-proxy's image route, listed under the
                # service's text, read like its chat route and was matched, and classified, as chat.
                **({"description": route_description or service["description"]} if (route_description or service["description"]) else {}),
            }
            conn.execute(
                """
                update services set
                  canonical_endpoint = %s, canonical_origin = %s,
                  name = case when directory_slug is null and %s::text is not null then %s::text else name end,
                  metadata = metadata || %s
                where id = %s
                """,
                (url, canonical_origin(url), service["name"], service["name"], Jsonb(metadata), row["id"]))
            for identifier_type, value in [(KEY_TYPE, key), ("api_endpoint", url), *[("api_endpoint", u) for u in old]]:
                _insert_identifier(conn, row["id"], identifier_type, value)
            stats["created" if fresh else "updated"] += 1
            if moved and not fresh:
                stats["moved"] += 1
    # Hosted services no longer on CrowdCode's list leave the rankings; their reviews stay.
    gone = conn.execute(
        """
        update services set metadata = metadata || '{"hosted_status": "deleted"}'::jsonb
        where metadata ? 'crowdcode_service_id' and coalesce(metadata->>'hosted_status', '') <> 'deleted'
          and not (id = any(%s))
        returning id
        """, (list(seen),)).fetchall()
    stats["deleted"] = len(gone)
    return stats


def is_hidden(metadata: dict[str, Any] | None) -> bool:
    """A hosted service its owner deleted is not ranked."""
    return bool(metadata) and metadata.get("hosted_status") == "deleted"


__all__ = ["SyncError", "parse_services", "sync_hosted_services", "is_hidden", "agent_service_slug"]
