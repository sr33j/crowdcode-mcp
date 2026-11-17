"""The services index: the x402/MPP resources that matter, probed daily (SRI-115 C1).

Three jobs, all run by ``crowdcode-cron`` every hour (cron.jobs):

``index_sweep``
    Reads every free feed whose last good sweep is older than SWEEP_EVERY (so
    once a day), plus our own records every hour: hosted services, endpoints
    bought through the gateway (reviews carrying a gateway attestation) and
    every reviewed endpoint. Listings are merged into ``index_resources`` by
    ``(method, route template)``, marked ``tracked`` per C1 and linked to one
    ``services`` row each (created with ``created_from_review = false`` when
    none exists). One feed failing does not stop the others; the job then
    raises, so the cron status shows it.

``index_purchases``
    Records every gateway-attested purchase as an observation, and links
    observations (ours and C5's) that arrived before their resource did.

``index_probes``
    Probes the tracked resources that are due, at most once per resource per
    PROBE_EVERY, spreading the day's probes across the hours. Each result is an
    ``index_observations`` row; changes in liveness, price and payee are
    ``index_changes`` rows, and the latest state sits on the resource.

No database transaction or connection is held across a network call.
"""

from __future__ import annotations

import gc
import json
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable

import psycopg
from psycopg.types.json import Jsonb

from crowdcode.advertised import public_endpoint
from crowdcode.identity import canonical_origin, generate_service_id, normalize_api_endpoint
from crowdcode.index_feeds import (
    BASE,
    SOLANA,
    TEMPO,
    Listing,
    Route,
    caip2,
    feed_client,
    fetch_bazaar,
    fetch_mpp,
    matches_template,
    route,
)
from crowdcode.index_probe import ProbeResult, ProbeTarget, probe_all

FEEDS: dict[str, Callable[..., tuple[list[Listing], int]]] = {"bazaar": fetch_bazaar, "mpp": fetch_mpp}
FEED_SOURCES = tuple(FEEDS)
INTERNAL_SOURCES = ("hosted", "gateway", "reviewed")
SOURCE_ORDER = (*FEED_SOURCES, *INTERNAL_SOURCES)

SWEEP_EVERY = timedelta(hours=20)
# A day, less the drift of an hourly cron that starts a few minutes late.
PROBE_EVERY = timedelta(hours=23)
DEAD_FAILURES = 3
DEAD_SPAN = timedelta(hours=48)
TRACK_MIN_PAYERS = 3
# The listed tier: Bazaar resources with fewer payers are not stored at all.
# Two thirds of Bazaar has one payer or none, usually the seller testing itself,
# and the database this lives in is shared with the hosted app.
LISTED_MIN_PAYERS = 2
# Unpaid probe rows; C3 reads the last three and seven days of prices.
# Purchases, C5 observations and index_changes are kept.
PROBE_RETENTION = timedelta(days=14)
# Each hour probes a 24th of the tracked set, with headroom for new and overdue ones.
PROBE_HEADROOM = 1.25
PROBE_EXTRA = 20
PROPOSAL_MATCHES = 3
PROPOSAL_MIN_SIMILARITY = 0.62


class IndexJobError(RuntimeError):
    """A job finished what it could and failed part of its work."""


def _norm(url: str | None) -> str | None:
    if not url:
        return None
    try:
        return normalize_api_endpoint(url)
    except ValueError:
        return None


# --- Our own records ---------------------------------------------------------------


def internal_listings(conn: psycopg.Connection) -> dict[str, list[Listing]]:
    """Hosted service routes, gateway purchases and reviewed endpoints."""
    hosted = []
    for row in conn.execute(
        """
        select id, name, canonical_endpoint, metadata
        from services
        where metadata ? 'crowdcode_service_id'
          and coalesce(metadata->>'hosted_status', '') = 'live'
          and canonical_endpoint is not null
        """
    ).fetchall():
        metadata = row["metadata"] or {}
        method = str(metadata.get("hosted_route") or "POST /").split(" ")[0].upper()
        body = metadata.get("probe_body")  # from the route's input schema (agent_listings)
        hosted.append(Listing(
            source="hosted", method=method or "POST", url=row["canonical_endpoint"],
            service_name=row["name"], description=metadata.get("description"),
            service_id=row["id"], own_record=True,
            probe_body=json.dumps(body, separators=(",", ":")) if isinstance(body, dict) and body else None,
        ))

    gateway = []
    for row in conn.execute(
        """
        select gateway_attestation->'receipt'->>'endpoint' as endpoint,
               upper(coalesce(gateway_attestation->'receipt'->>'method', 'POST')) as method,
               service_id, count(*)::int as n
        from reviews
        where receipt_key_id is not null
          and gateway_attestation->'receipt'->>'endpoint' is not null
        group by 1, 2, 3
        order by 1, 2, n desc
        """
    ).fetchall():
        gateway.append(Listing(source="gateway", method=row["method"], url=row["endpoint"],
                               service_id=row["service_id"], calls_30d=row["n"], own_record=True))

    reviewed = []
    for row in conn.execute(
        """
        select s.id, s.name, s.canonical_endpoint, count(r.id)::int as n
        from services s join reviews r on r.service_id = s.id
        where s.canonical_endpoint is not null
          and not (s.metadata ? 'crowdcode_service_id')
        group by s.id
        """
    ).fetchall():
        reviewed.append(Listing(source="reviewed", method="", url=row["canonical_endpoint"],
                                service_name=row["name"], service_id=row["id"],
                                calls_30d=row["n"], own_record=True))
    return {"hosted": hosted, "gateway": gateway, "reviewed": reviewed}


# --- Merging -----------------------------------------------------------------------


@dataclass(slots=True)
class Resource:
    key: str
    method: str
    route: Route
    probe_url: str
    sources: set[str] = field(default_factory=set)
    feed: Listing | None = None  # the listing whose fields the row shows
    feed_weight: int = -1
    networks: set[str] = field(default_factory=set)
    pay_to: list[str] = field(default_factory=list)
    payable: bool = False
    bazaar_payers: int | None = None
    bazaar_calls: int | None = None
    bazaar_last_called_at: datetime | None = None
    service_id: str | None = None
    service_rank: tuple[int, int] = (-1, -1)
    hosted_body: str | None = None


_SERVICE_RANK = {"hosted": 3, "gateway": 2, "reviewed": 1}


class Merger:
    """Folds listings into resources, keyed by (method, route template)."""

    def __init__(self, known: Callable[[str, str | None], tuple[str, str] | None] | None = None) -> None:
        self.resources: dict[str, Resource] = {}
        self.urls: dict[tuple[str, str, str], tuple[str, int | None]] = {}
        # Feed listings by normalized URL, and the templates they define per origin.
        self._by_url: dict[str, dict[str, str]] = defaultdict(dict)
        self._templates: dict[str, list[tuple[Route, str]]] = defaultdict(list)
        self._known = known
        # Hosted route rows by service id: an old URL of a renamed hosted
        # service (its reviews keep the URL they were bought at) is the route.
        self._hosted: dict[str, tuple[str, str]] = {}

    def set_known(self, known: Callable[[str, str | None], tuple[str, str] | None]) -> None:
        self._known = known

    def _resource(self, key: str, method: str, r: Route, url: str) -> Resource:
        resource = self.resources.get(key)
        if resource is None:
            resource = self.resources[key] = Resource(key=key, method=method, route=r, probe_url=url)
        return resource

    def _locate(self, listing: Listing, r: Route) -> tuple[str, str] | None:
        """For our own records: the feed resource this URL is, or instantiates."""
        if listing.source != "hosted" and listing.service_id in self._hosted:
            return self._hosted[listing.service_id]
        url = _norm(listing.url) or listing.url
        methods = self._by_url.get(url, {})
        if methods:
            method = listing.method if listing.method in methods else ("POST" if "POST" in methods else sorted(methods)[0])
            return methods[method], method
        plain = route(listing.url)
        for template, key in self._templates.get(r.origin, []):
            if plain and matches_template(template, plain):
                method = key.split(" ", 1)[0]
                if not listing.method or listing.method == method:
                    return key, method
        if not listing.method:
            # A reviewed endpoint names no method: any method another record gave it.
            for method in ("POST", "GET", "PUT", "PATCH", "DELETE"):
                if r.key(method) in self.resources:
                    return r.key(method), method
        if self._known is not None:
            return self._known(listing.url, listing.method or None)
        return None

    def add(self, listing: Listing) -> None:
        r = route(listing.url, own_record=listing.own_record)
        if r is None:
            return
        url = _norm(listing.url) or listing.url
        located = self._locate(listing, r) if listing.own_record else None
        if located is not None:
            key, method = located
            resource = self.resources.get(key)
            if resource is None:
                # Known from an earlier sweep, not from today's feeds.
                parts = key.split(" ", 1)
                known_route = route(parts[1]) or r
                resource = self._resource(key, method, known_route, listing.url)
        else:
            method = (listing.method or "POST").upper()
            key = r.key(method)
            resource = self._resource(key, method, r, listing.url)
            self._by_url[url].setdefault(method, key)
            if not listing.own_record:
                if r.has_params and (r, key) not in self._templates[r.origin]:
                    self._templates[r.origin].append((r, key))
        resource.sources.add(listing.source)
        self.urls[(listing.source, resource.method, url)] = (key, listing.payers_30d)

        if listing.source in FEED_SOURCES:
            # The busiest listed instance speaks for the resource.
            weight = (1 if listing.source == "bazaar" else 0, listing.calls_30d or 0, listing.payers_30d or 0)
            if resource.feed is None or weight > resource.feed_weight:
                resource.feed, resource.feed_weight = listing, weight
                if not r.has_params or "{" not in listing.url and ":" not in listing.url.split("//", 1)[-1]:
                    resource.probe_url = listing.url
            resource.networks |= listing.networks
            resource.pay_to = list(dict.fromkeys([*resource.pay_to, *listing.pay_to]))[:20]
            resource.payable = resource.payable or listing.payable
            if listing.source == "bazaar":
                if listing.payers_30d is not None:
                    resource.bazaar_payers = max(resource.bazaar_payers or 0, listing.payers_30d)
                if listing.calls_30d is not None:
                    resource.bazaar_calls = (resource.bazaar_calls or 0) + listing.calls_30d
                if listing.last_called_at and (resource.bazaar_last_called_at is None
                                               or listing.last_called_at > resource.bazaar_last_called_at):
                    resource.bazaar_last_called_at = listing.last_called_at
        elif listing.service_id:
            rank = (_SERVICE_RANK.get(listing.source, 0), listing.calls_30d or 0)
            if rank > resource.service_rank:
                resource.service_id, resource.service_rank = listing.service_id, rank
            if listing.source == "hosted":
                self._hosted.setdefault(listing.service_id, (key, resource.method))
                resource.hosted_body = listing.probe_body or resource.hosted_body
                if resource.feed is None:
                    resource.probe_url = listing.url


# --- Writing a sweep ---------------------------------------------------------------

_TMP_COLUMNS = [
    ("resource_key", "text"), ("method", "text"), ("origin", "text"), ("route_template", "text"),
    ("has_params", "bool"), ("probe_url", "text"), ("probe_body", "text"), ("listing_type", "text"),
    ("protocol", "text"), ("sources", "text[]"), ("networks", "text[]"), ("payable", "bool"),
    ("pay_to", "text[]"), ("price_atomic", "numeric"), ("price_asset", "text"), ("price_network", "text"),
    ("price_scheme", "text"), ("bazaar_payers_30d", "int4"), ("bazaar_calls_30d", "int8"),
    ("bazaar_last_called_at", "timestamptz"), ("service_name", "text"), ("description", "text"),
    ("input_schema", "text"), ("service_id", "text"), ("has_feed", "bool"), ("has_bazaar", "bool"),
]


def _clean(value: Any) -> Any:
    """Postgres text and jsonb refuse NUL; feeds sometimes carry it."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {_clean(k): _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def _row(resource: Resource) -> tuple[Any, ...]:
    return tuple(_clean(v) for v in _raw_row(resource))


def _raw_row(resource: Resource) -> tuple[Any, ...]:
    feed = resource.feed
    sources = sorted(resource.sources, key=SOURCE_ORDER.index)
    return (
        resource.key, resource.method, resource.route.origin, resource.route.template,
        resource.route.has_params, resource.probe_url,
        resource.hosted_body or (feed.probe_body if feed else None),
        feed.listing_type if feed else "http",
        feed.protocol if feed else ("x402" if "hosted" in resource.sources else None),
        sources, sorted(resource.networks), resource.payable, resource.pay_to,
        feed.price_atomic if feed else None, feed.price_asset if feed else None,
        feed.price_network if feed else None, feed.price_scheme if feed else None,
        resource.bazaar_payers, resource.bazaar_calls, resource.bazaar_last_called_at,
        feed.service_name if feed else None, feed.description if feed else None,
        feed.input_schema if feed else None,
        resource.service_id, feed is not None, "bazaar" in resource.sources,
    )


def write_sweep(conn: psycopg.Connection, merger: Merger, swept: list[str], now: datetime) -> dict[str, int]:
    """Upsert every merged resource and URL in the caller's transaction."""
    conn.execute(
        "create temp table tmp_index_resources ("
        + ", ".join(f"{name} {kind}" for name, kind in _TMP_COLUMNS)
        + ") on commit drop"
    )
    with conn.cursor().copy(
        f"copy tmp_index_resources ({', '.join(n for n, _ in _TMP_COLUMNS)}) from stdin"
    ) as copy:
        copy.set_types([kind for _, kind in _TMP_COLUMNS])
        for resource in merger.resources.values():
            copy.write_row(_row(resource))

    feed_swept = [s for s in swept if s in FEED_SOURCES]
    params = {"swept": swept, "now": now, "bazaar_swept": "bazaar" in swept, "feed_swept": feed_swept}
    _upsert(conn, params)

    # Rows a swept source no longer lists lose that source (and Bazaar's counts).
    dropped = conn.execute(
        """
        update index_resources r set
          sources = array(select unnest(r.sources) except select unnest(%(swept)s::text[])),
          bazaar_payers_30d = case when %(bazaar_swept)s then null else r.bazaar_payers_30d end,
          bazaar_calls_30d = case when %(bazaar_swept)s then null else r.bazaar_calls_30d end
        where r.sources && %(swept)s::text[]
          and not exists (select 1 from tmp_index_resources t where t.resource_key = r.resource_key)
        """,
        params,
    ).rowcount

    conn.execute(
        "create temp table tmp_index_urls (source text, method text, url text, resource_key text, payers int4) on commit drop"
    )
    with conn.cursor().copy("copy tmp_index_urls (source, method, url, resource_key, payers) from stdin") as copy:
        copy.set_types(["text", "text", "text", "text", "int4"])
        for (source, method, url), (key, payers) in merger.urls.items():
            copy.write_row((source, method, _clean(url), _clean(key), payers))
    conn.execute(
        """
        insert into index_resource_urls (source, method, url, resource_id, payers_30d, last_seen_at)
        select u.source, u.method, u.url, r.id, u.payers, %(now)s
        from tmp_index_urls u join index_resources r on r.resource_key = u.resource_key
        on conflict (source, method, url) do update set
          resource_id = excluded.resource_id, payers_30d = excluded.payers_30d,
          last_seen_at = excluded.last_seen_at
        """,
        params,
    )
    conn.execute(
        "delete from index_resource_urls where source = any(%(swept)s) and last_seen_at < %(now)s",
        params,
    )
    return {"resources": len(merger.resources), "urls": len(merger.urls), "delisted": dropped}


def _upsert(conn: psycopg.Connection, params: dict[str, Any]) -> None:
    # `excluded` cannot see the temp table's flags, so they travel through a CTE join.
    conn.execute(
        """
        with t as (select * from tmp_index_resources)
        insert into index_resources as r (
          resource_key, method, origin, route_template, has_params, probe_url, probe_body,
          listing_type, protocol, sources, networks, payable, pay_to, price_atomic, price_asset,
          price_network, price_scheme, bazaar_payers_30d, bazaar_calls_30d, bazaar_last_called_at,
          first_listed_at, last_listed_at, source_seen, service_name, description, input_schema, service_id
        )
        select resource_key, method, origin, route_template, has_params, probe_url, probe_body::jsonb,
               listing_type, protocol, sources, networks, payable, pay_to, price_atomic, price_asset,
               price_network, price_scheme, bazaar_payers_30d, bazaar_calls_30d, bazaar_last_called_at,
               %(now)s, %(now)s, (select jsonb_object_agg(s, %(now)s) from unnest(t.sources) s),
               service_name, description, input_schema::jsonb, service_id
        from t
        on conflict (resource_key) do nothing
        """,
        params,
    )
    conn.execute(
        """
        update index_resources r set
          sources = array(
            select s from unnest(array_cat(
              array(select unnest(r.sources) except select unnest(%(swept)s::text[])), t.sources)) s
            group by s order by min(array_position(array['bazaar','mpp','hosted','gateway','reviewed'], s))),
          source_seen = r.source_seen || coalesce((select jsonb_object_agg(s, %(now)s) from unnest(t.sources) s), '{}'::jsonb),
          last_listed_at = %(now)s,
          service_id = coalesce(t.service_id, r.service_id),
          probe_url = case when t.has_feed or not (r.sources && array['bazaar','mpp']) then t.probe_url else r.probe_url end,
          probe_body = case when t.has_feed or t.probe_body is not null then t.probe_body::jsonb else r.probe_body end,
          listing_type = case when t.has_feed then t.listing_type else r.listing_type end,
          protocol = coalesce(case when t.has_feed then t.protocol end, r.protocol, t.protocol),
          networks = case when t.has_feed then t.networks else r.networks end,
          payable = case when t.has_feed then t.payable else r.payable end,
          pay_to = case when t.has_feed then t.pay_to else r.pay_to end,
          price_atomic = case when t.has_feed then t.price_atomic else r.price_atomic end,
          price_asset = case when t.has_feed then t.price_asset else r.price_asset end,
          price_network = case when t.has_feed then t.price_network else r.price_network end,
          price_scheme = case when t.has_feed then t.price_scheme else r.price_scheme end,
          service_name = coalesce(case when t.has_feed then t.service_name end, r.service_name),
          description = coalesce(case when t.has_feed then t.description end, r.description),
          input_schema = coalesce(case when t.has_feed then t.input_schema::jsonb end, r.input_schema),
          bazaar_payers_30d = case when t.has_bazaar then t.bazaar_payers_30d
                                   when %(bazaar_swept)s then null else r.bazaar_payers_30d end,
          bazaar_calls_30d = case when t.has_bazaar then t.bazaar_calls_30d
                                  when %(bazaar_swept)s then null else r.bazaar_calls_30d end,
          bazaar_last_called_at = case when t.has_bazaar then t.bazaar_last_called_at
                                       when %(bazaar_swept)s then null else r.bazaar_last_called_at end
        from tmp_index_resources t
        where t.resource_key = r.resource_key and r.last_listed_at < %(now)s
          -- Rewrite a row only when its listing changed (or weekly, to show it is
          -- still listed): a daily rewrite of every row doubles the table.
          and (
            r.last_listed_at < %(now)s - interval '6 days'
            or (t.service_id is not null and t.service_id is distinct from r.service_id)
            or not (r.sources @> t.sources)
            or (r.sources && %(swept)s::text[] and not (t.sources @> array(
                  select unnest(r.sources) intersect select unnest(%(swept)s::text[]))))
            or (t.has_feed and (r.probe_url, r.networks, r.payable, r.pay_to, r.price_atomic, r.price_asset,
                                r.price_network, r.price_scheme, r.service_name, r.description, r.listing_type,
                                r.protocol, r.route_template)
                is distinct from (t.probe_url, t.networks, t.payable, t.pay_to, t.price_atomic, t.price_asset,
                                  t.price_network, t.price_scheme, t.service_name, t.description, t.listing_type,
                                  t.protocol, t.route_template))
            or (t.has_feed and r.input_schema is distinct from t.input_schema::jsonb)
            or ((t.has_feed or t.probe_body is not null) and r.probe_body is distinct from t.probe_body::jsonb)
            or (t.has_bazaar and (r.bazaar_payers_30d, r.bazaar_calls_30d)
                is distinct from (t.bazaar_payers_30d, t.bazaar_calls_30d))
            or (not t.has_bazaar and %(bazaar_swept)s and r.bazaar_payers_30d is not null)
          )
        """,
        params,
    )


# --- Tracking and the link to services ---------------------------------------------


def update_tracking(conn: psycopg.Connection, now: datetime) -> int:
    """C1: tracked when hosted, bought through our gateway, used by >= 3 Bazaar
    payers in 30 days, or matching a published proposal. Feed listings must be
    on Base, Solana or Tempo; MCP listings are not tracked (the gateway calls
    HTTP endpoints). A seller that opted out (index_opt_outs) is never tracked."""
    return conn.execute(
        """
        with t as (
          select id,
            case
              when exists (select 1 from index_opt_outs x where starts_with(probe_url, x.prefix)) then null
              when 'hosted' = any(sources) then 'hosted'
              when 'gateway' = any(sources) then 'gateway_purchase'
              when listing_type = 'http' and cardinality(networks) > 0
                   and coalesce(bazaar_payers_30d, 0) >= %(payers)s then 'bazaar_payers'
              when listing_type = 'http' and payable and cardinality(proposal_ids) > 0 then 'proposal'
            end as reason
          from index_resources
        )
        update index_resources r set
          tracked = t.reason is not null,
          tracked_reason = t.reason,
          tracked_since = case when t.reason is null then null
                               when r.tracked then r.tracked_since else %(now)s end
        from t
        where t.id = r.id
          and (r.tracked is distinct from (t.reason is not null) or r.tracked_reason is distinct from t.reason)
        """,
        {"payers": TRACK_MIN_PAYERS, "now": now},
    ).rowcount


def _service_name(row: dict[str, Any]) -> str:
    parts = route(row["route_template"])
    path = parts.template_path if parts else ""
    host = (parts.origin.split("//", 1)[-1] if parts else row["origin"])
    label = row.get("service_name") or host
    return (f"{label} {path}" if path not in ("", "/") else label)[:120]


def link_services(conn: psycopg.Connection) -> dict[str, int]:
    """Every tracked resource gets exactly one services row: the reviewed or
    registered one that already names its URL (the most reviewed, if several
    do), else a new one with created_from_review = false."""
    rows = conn.execute(
        """
        select id, resource_key, method, origin, route_template, probe_url, service_name, description
        from index_resources where tracked and service_id is null
        """
    ).fetchall()
    stats = {"linked": 0, "created": 0}
    if not rows:
        return stats
    urls: dict[int, set[str]] = defaultdict(set)
    for row in rows:
        for url in (row["route_template"], row["probe_url"]):
            if _norm(url):
                urls[row["id"]].add(_norm(url))
    for row in conn.execute(
        "select resource_id, url from index_resource_urls where resource_id = any(%s)",
        ([r["id"] for r in rows],),
    ).fetchall():
        urls[row["resource_id"]].add(row["url"])
    every = sorted({u for us in urls.values() for u in us})
    owners: dict[str, tuple[int, str]] = {}
    for match in conn.execute(
        """
        with m as (
          select identifier_value as url, service_id from service_identifiers
          where identifier_type = 'api_endpoint' and identifier_value = any(%(urls)s)
          union
          select canonical_endpoint, id from services where canonical_endpoint = any(%(urls)s)
        )
        select m.url, m.service_id, (select count(*) from reviews r where r.service_id = m.service_id)::int as n
        from m
        """,
        {"urls": every},
    ).fetchall():
        best = owners.get(match["url"])
        if best is None or (match["n"], match["service_id"]) > best:
            owners[match["url"]] = (match["n"], match["service_id"])

    for row in rows:
        found = [owners[u] for u in urls[row["id"]] if u in owners]
        if found:
            service_id = max(found)[1]
            stats["linked"] += 1
        else:
            endpoint = _norm(row["route_template"]) or _norm(row["probe_url"])
            if endpoint is None:
                continue
            service_id = generate_service_id(endpoint, "", "")
            metadata = {"listing_source": "index", "index_resource_key": row["resource_key"]}
            if row.get("description"):
                metadata["description"] = row["description"]
            inserted = conn.execute(
                """
                insert into services (id, name, canonical_origin, canonical_endpoint, created_from_review, metadata)
                values (%s, %s, %s, %s, false, %s)
                on conflict (id) do nothing
                returning id
                """,
                (service_id, _service_name(row), canonical_origin(endpoint), endpoint, Jsonb(metadata)),
            ).fetchone()
            conn.execute(
                """
                insert into service_identifiers (service_id, identifier_type, identifier_value)
                values (%s, 'api_endpoint', %s) on conflict do nothing
                """,
                (service_id, endpoint),
            )
            stats["created" if inserted else "linked"] += 1
        conn.execute("update index_resources set service_id = %s where id = %s", (service_id, row["id"]))
    return stats


def match_proposals(conn: psycopg.Connection, embedder: Any = None) -> int | None:
    """Mark payable listings that answer a published service proposal.

    Each proposal (title, problem, output) is compared by meaning with the
    descriptions of payable HTTP listings that someone has used (Bazaar) or
    that mpp.dev curates; its PROPOSAL_MATCHES closest above
    PROPOSAL_MIN_SIMILARITY are marked. Returns None when the embedding model
    is unavailable, leaving earlier matches in place.
    """
    from crowdcode import semantic

    row = conn.execute("select payload from app_cache where key = 'service_proposals'").fetchone()
    proposals = [p for p in ((row or {}).get("payload") or {}).get("proposals") or [] if isinstance(p, dict) and p.get("id")]
    candidates = conn.execute(
        """
        select id, service_name, description, route_template from index_resources
        where payable and listing_type = 'http' and description is not null
          and (coalesce(bazaar_payers_30d, 0) >= 1 or 'mpp' = any(sources))
        """
    ).fetchall()
    if embedder is None:
        embedder = semantic.EMBEDDER
        embedder.warm(wait=True)
    docs = [semantic.document(c["service_name"], c["description"], endpoint=c["route_template"]) for c in candidates]
    matched: dict[int, list[str]] = defaultdict(list)
    if proposals and docs:
        import numpy as np
        # In chunks: the embedder's cache holds 8,192 texts, and evicts the rest.
        encode = getattr(embedder, "encode_uncached", embedder.encode)
        chunks = [encode(docs[i:i + 2000]) for i in range(0, len(docs), 2000)]
        if any(chunk is None for chunk in chunks):
            return None
        vectors = np.vstack(chunks)
        for proposal in proposals:
            query = ". ".join(str(proposal.get(k) or "") for k in ("title", "problem", "output"))
            qv = embedder.encode([query])
            if qv is None:
                return None
            scores = vectors @ qv[0]
            best = sorted(range(len(candidates)), key=lambda i: -float(scores[i]))[:PROPOSAL_MATCHES]
            for i in best:
                if float(scores[i]) >= PROPOSAL_MIN_SIMILARITY:
                    matched[candidates[i]["id"]].append(str(proposal["id"]))
    conn.execute("update index_resources set proposal_ids = '{}' where cardinality(proposal_ids) > 0 and not (id = any(%s))",
                 (list(matched),))
    for resource_id, ids in matched.items():
        conn.execute("update index_resources set proposal_ids = %s where id = %s", (sorted(set(ids)), resource_id))
    return len(matched)


# --- The sweep job -----------------------------------------------------------------


def _last_ok(conn: psycopg.Connection) -> dict[str, datetime]:
    return {
        row["source"]: row["at"]
        for row in conn.execute(
            "select source, max(finished_at) as at from index_sweeps where ok group by source"
        ).fetchall()
    }


def _known_resolver(conn: psycopg.Connection) -> Callable[[str, str | None], tuple[str, str] | None]:
    def known(url: str, method: str | None) -> tuple[str, str] | None:
        row = resolve_resource(conn, url, method, heuristic=False)
        return (row["resource_key"], row["method"]) if row else None
    return known


def run_sweep(connect: Callable[[], Any], now: datetime, *, feeds: dict[str, Callable[..., Any]] | None = None,
              client_factory: Callable[[], Any] = feed_client, force: bool = False,
              embedder: Any = None) -> dict[str, Any]:
    feeds = FEEDS if feeds is None else feeds
    began = time.monotonic()

    def clock() -> datetime:
        return now + timedelta(seconds=time.monotonic() - began)

    with connect() as conn:
        last = _last_ok(conn)
    due = [s for s in feeds if force or s not in last or now - last[s] >= SWEEP_EVERY]

    fetched: list[str] = []
    failures: dict[str, str] = {}
    counts: dict[str, int] = {}
    started: dict[str, datetime] = {}
    # Feeds are folded in as soon as each is read, so only one feed's listings
    # are held at a time; our own records come after, and may name templates.
    merger = Merger()
    if due:
        with client_factory() as client:
            for source in [s for s in FEED_SOURCES if s in due] + [s for s in due if s not in FEED_SOURCES]:
                started[source] = clock()
                try:
                    listings, counts[source] = feeds[source](client)
                except Exception as error:  # one feed failing never stops the others
                    failures[source] = f"{type(error).__name__}: {error}"[:500]
                    continue
                for listing in listings:
                    if listing.source != "bazaar" or (listing.payers_30d or 0) >= LISTED_MIN_PAYERS:
                        merger.add(listing)
                del listings
                fetched.append(source)

    stats: dict[str, Any] = {"due": due, "failed": failures}
    with connect() as conn:
        internal = internal_listings(conn)
        merger.set_known(_known_resolver(conn))
        for source in INTERNAL_SOURCES:
            for listing in internal[source]:
                merger.add(listing)
        swept = [*fetched, *INTERNAL_SOURCES]
        stats.update(write_sweep(conn, merger, swept, now))
        finished = clock()
        for source in due:
            conn.execute(
                "insert into index_sweeps (source, started_at, finished_at, ok, items, resources, error) values (%s, %s, %s, %s, %s, %s, %s)",
                (source, started[source], finished, source in fetched, counts.get(source),
                 len({k for (s, _, _), (k, _) in merger.urls.items() if s == source}) if source in fetched else None,
                 failures.get(source)),
            )
        for source in INTERNAL_SOURCES:
            conn.execute(
                "insert into index_sweeps (source, started_at, finished_at, ok, items, resources) values (%s, %s, %s, true, %s, %s)",
                (source, now, finished, len(internal[source]),
                 len({k for (s, _, _), (k, _) in merger.urls.items() if s == source})),
            )
        conn.execute("delete from index_sweeps where finished_at < %s", (now - timedelta(days=60),))
        conn.commit()

    del merger
    gc.collect()
    if fetched:
        # The model may download on first use: no transaction is open meanwhile.
        if embedder is None:
            from crowdcode import semantic
            semantic.EMBEDDER.warm(wait=True)
        with connect() as conn:
            try:
                stats["proposal_matches"] = match_proposals(conn, embedder)
                conn.commit()
            except Exception as error:
                conn.rollback()
                stats["proposal_matches"] = f"failed: {type(error).__name__}"
                failures["proposal matching"] = f"{type(error).__name__}: {error}"[:300]
    with connect() as conn:
        stats["tracking_changed"] = update_tracking(conn, now)
        stats.update(link_services(conn))
        stats["tracked"] = conn.execute("select count(*)::int as n from index_resources where tracked").fetchone()["n"]
        conn.commit()
    print(f"index sweep: {json.dumps(stats, default=str)}")
    if failures:
        raise IndexJobError("index sweep failed for " + ", ".join(f"{s} ({e})" for s, e in failures.items()))
    return stats


# --- Observations and change detection ---------------------------------------------

_STATE_COLUMNS = (
    "id, method, probe_url, last_probed_at, state_at, liveness, consecutive_failures, failing_since, "
    "last_ok_at, quoted_price_atomic, quoted_asset, quoted_network, price_since, payees, payee_since, "
    "previous_payees, payee_changed_at"
)


def _price_label(amount: Any, asset: Any, network: Any) -> str | None:
    if amount is None:
        return None
    return f"{int(amount)} {asset or '?'} on {network or '?'}"


def apply_probe(state: dict[str, Any], result: ProbeResult, at: datetime) -> tuple[dict[str, Any], list[tuple[str, str | None, str | None]]]:
    """The resource's next state after one probe, and the changes it shows.

    - down: a resource last seen alive fails;
    - dead: DEAD_FAILURES failed probes in a row spanning at least DEAD_SPAN;
    - revived: a failing or dead resource answers again (one success is enough);
    - price: the quoted amount, asset or network differs from the last valid quote;
    - payee: the set of advertised payees differs from the last valid answer's.
    """
    new = dict(state)
    changes: list[tuple[str, str | None, str | None]] = []
    new["last_probed_at"] = at
    if state.get("state_at") and at < state["state_at"]:
        return new, changes  # an older answer arriving late changes nothing
    new["state_at"] = at
    if result.ok:
        if state["liveness"] in ("failing", "dead"):
            changes.append(("revived", state["liveness"], "alive"))
        new.update(liveness="alive", consecutive_failures=0, failing_since=None, last_ok_at=at)
        old_price = _price_label(state.get("quoted_price_atomic"), state.get("quoted_asset"), state.get("quoted_network"))
        price = _price_label(result.quoted_amount_atomic, result.asset, result.network)
        if old_price != price:
            if old_price is not None and price is not None:
                changes.append(("price", old_price, price))
            new["price_since"] = at
        new.update(quoted_price_atomic=result.quoted_amount_atomic, quoted_asset=result.asset,
                   quoted_network=result.network)
        payees = sorted(set(result.pay_to))
        old_payees = sorted(set(state.get("payees") or []))
        if payees != old_payees:
            if old_payees:
                changes.append(("payee", ",".join(old_payees), ",".join(payees)))
                new.update(previous_payees=old_payees, payee_changed_at=at)
            new.update(payees=payees, payee_since=at)
    else:
        failures = int(state.get("consecutive_failures") or 0) + 1
        failing_since = state.get("failing_since") or at
        liveness = state["liveness"]
        if liveness == "alive":
            changes.append(("down", "alive", result.error_code))
        if liveness != "dead":
            liveness = "failing"
        if failures >= DEAD_FAILURES and at - failing_since >= DEAD_SPAN and state["liveness"] != "dead":
            changes.append(("dead", state["liveness"], result.error_code))
            liveness = "dead"
        new.update(liveness=liveness, consecutive_failures=failures, failing_since=failing_since)
    return new, changes


def record_probes(conn: psycopg.Connection, results: Iterable[tuple[ProbeTarget, ProbeResult]], at: datetime) -> dict[str, int]:
    """Store each probe as an observation, and move each resource's state."""
    results = list(results)
    if not results:
        return {"probed": 0, "ok": 0, "changes": 0}
    states = {
        row["id"]: row
        for row in conn.execute(
            f"select {_STATE_COLUMNS} from index_resources where id = any(%s) for update",
            ([t.resource_id for t, _ in results],),
        ).fetchall()
    }
    stats = {"probed": 0, "ok": 0, "changes": 0}
    for target, result in results:
        state = states.get(target.resource_id)
        if state is None:
            continue
        observation = conn.execute(
            """
            insert into index_observations (resource_id, endpoint, method, at, kind, ok, http_status, network,
              payee, quoted_amount_atomic, error_code, asset, pay_to, protocol, latency_ms, detail)
            values (%s, %s, %s, %s, 'probe', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            returning id
            """,
            (target.resource_id, _norm(target.url) or target.url, target.method, at, result.ok, result.http_status,
             result.network, result.payee, result.quoted_amount_atomic, result.error_code, result.asset,
             result.pay_to or None, result.protocol, result.latency_ms, result.detail),
        ).fetchone()["id"]
        new, changes = apply_probe(state, result, at)
        for kind, old, value in changes:
            conn.execute(
                "insert into index_changes (resource_id, at, kind, old_value, new_value, observation_id) values (%s, %s, %s, %s, %s, %s)",
                (target.resource_id, at, kind, old, value, observation),
            )
        conn.execute(
            """
            update index_resources set last_probed_at = %(last_probed_at)s, state_at = %(state_at)s,
              liveness = %(liveness)s, consecutive_failures = %(consecutive_failures)s,
              failing_since = %(failing_since)s, last_ok_at = %(last_ok_at)s,
              quoted_price_atomic = %(quoted_price_atomic)s, quoted_asset = %(quoted_asset)s,
              quoted_network = %(quoted_network)s, price_since = %(price_since)s, payees = %(payees)s,
              payee_since = %(payee_since)s, previous_payees = %(previous_payees)s,
              payee_changed_at = %(payee_changed_at)s
            where id = %(id)s
            """,
            new,
        )
        stats["probed"] += 1
        stats["ok"] += int(result.ok)
        stats["changes"] += len(changes)
    return stats


# --- The probe job -----------------------------------------------------------------


def probe_budget(tracked: int) -> int:
    return math.ceil(tracked * PROBE_HEADROOM / 24) + PROBE_EXTRA


def due_targets(conn: psycopg.Connection, now: datetime, limit: int) -> list[ProbeTarget]:
    rows = conn.execute(
        """
        select id, method, probe_url, probe_body, input_schema from index_resources r
        where tracked and (last_probed_at is null or last_probed_at <= %s)
          and not exists (select 1 from index_opt_outs x where starts_with(r.probe_url, x.prefix))
        order by last_probed_at is not null desc, last_probed_at asc,
                 case tracked_reason when 'hosted' then 0 when 'gateway_purchase' then 1 else 2 end,
                 bazaar_payers_30d desc nulls last, id
        limit %s
        """,
        (now - PROBE_EVERY, limit),
    ).fetchall()
    return [ProbeTarget(r["id"], r["method"], r["probe_url"], r["probe_body"], r["input_schema"]) for r in rows]


def run_probes(connect: Callable[[], Any], now: datetime, *, allow: Callable[[str], bool] = public_endpoint,
               budget: int | None = None, workers: int | None = None) -> dict[str, Any]:
    """Probe what is due: overdue first, then never probed (hosted, then bought, then busiest)."""
    with connect() as conn:
        tracked = conn.execute("select count(*)::int as n from index_resources where tracked").fetchone()["n"]
        limit = probe_budget(tracked) if budget is None else budget
        targets = due_targets(conn, now, limit)
        conn.commit()
    kwargs = {} if workers is None else {"workers": workers}
    results = probe_all(targets, allow=allow, **kwargs)
    at = now
    with connect() as conn:
        stats = record_probes(conn, results, at)
        pruned = conn.execute(
            "delete from index_observations where kind = 'probe' and at < %s", (now - PROBE_RETENTION,)
        ).rowcount
        conn.commit()
    stats.update(tracked=tracked, budget=limit, pruned=pruned)
    print(f"index probes: {json.dumps(stats)}")
    return stats


# --- Purchases and late links ------------------------------------------------------

_NETWORK_NAMES = {"base": BASE, "solana": SOLANA, "tempo": TEMPO}


def run_purchases(connect: Callable[[], Any], now: datetime, limit: int = 5000) -> dict[str, int]:
    """Every gateway-attested purchase becomes a `purchase` observation; and
    observations recorded before their resource existed are linked to it."""
    with connect() as conn:
        rows = conn.execute(
            """
            select r.id, r.created_at, r.gateway_attestation->'receipt' as receipt
            from reviews r
            where r.receipt_key_id is not null
              and r.gateway_attestation->'receipt'->>'endpoint' is not null
              and not exists (select 1 from index_observations o where o.kind = 'purchase' and o.review_id = r.id)
            order by r.id
            limit %s
            """,
            (limit,),
        ).fetchall()
        added = 0
        for row in rows:
            receipt = row["receipt"] or {}
            method = str(receipt.get("method") or "POST").upper()
            endpoint = _norm(receipt.get("endpoint"))
            if endpoint is None:
                continue
            resource = resolve_resource(conn, endpoint, method)
            network = _NETWORK_NAMES.get(str(receipt.get("network") or "").lower()) or caip2(receipt.get("network"))
            status = receipt.get("response_status")
            try:
                at = datetime.fromisoformat(str(receipt.get("settled_at")).replace("Z", "+00:00"))
            except ValueError:
                at = row["created_at"]
            payee = receipt.get("payee")
            token = receipt.get("token")
            conn.execute(
                """
                insert into index_observations (resource_id, endpoint, method, at, kind, ok, http_status, network,
                  payee, quoted_amount_atomic, asset, protocol, review_id)
                values (%s, %s, %s, %s, 'purchase', %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (review_id) where kind = 'purchase' do nothing
                """,
                (resource["id"] if resource else None, endpoint, method, at,
                 isinstance(status, int) and 200 <= status < 300, status if isinstance(status, int) else None,
                 network, payee.lower() if isinstance(payee, str) and payee.startswith("0x") else payee,
                 int(receipt["amount_atomic"]) if str(receipt.get("amount_atomic") or "").isdigit() else None,
                 token.lower() if isinstance(token, str) and token.startswith("0x") else token,
                 "mpp" if network == TEMPO else "x402", row["id"]),
            )
            added += 1
        linked = 0
        for row in conn.execute(
            "select id, endpoint, method from index_observations where resource_id is null order by id limit 2000"
        ).fetchall():
            resource = resolve_resource(conn, row["endpoint"], row["method"])
            if resource:
                conn.execute("update index_observations set resource_id = %s where id = %s", (resource["id"], row["id"]))
                linked += 1
        conn.commit()
    stats = {"purchases": added, "linked": linked}
    print(f"index purchases: {json.dumps(stats)}")
    return stats


# --- Resolution --------------------------------------------------------------------


def resolve_resource(conn: psycopg.Connection, url: str, method: str | None = None, *,
                     heuristic: bool = True) -> dict[str, Any] | None:
    """The index_resources row a concrete URL is, or is an instance of.

    Exact listed URLs first, then the resource key, then the origin's route
    templates, then (``heuristic``) the identifier-collapsing key our own
    records use. ``method`` None matches any, preferring tracked and busy rows.
    Runs in a savepoint: callers in other modules (C5 observations) may catch
    an error here, and it must not abort their transaction.
    """
    with conn.transaction():
        return _resolve(conn, url, method, heuristic)


def _resolve(conn: psycopg.Connection, url: str, method: str | None, heuristic: bool) -> dict[str, Any] | None:
    endpoint = _norm(url)
    if endpoint is None:
        return None
    method = method.upper() if method else None
    order = "order by r.tracked desc, r.bazaar_payers_30d desc nulls last, (r.method = 'POST') desc, r.id limit 1"
    row = conn.execute(
        f"""
        select r.* from index_resource_urls u join index_resources r on r.id = u.resource_id
        where u.url = %(url)s and (%(method)s::text is null or u.method = %(method)s) {order}
        """,
        {"url": endpoint, "method": method},
    ).fetchone()
    if row:
        return row
    plain = route(url)
    if plain is None:
        return None
    keys = [plain.key(method)] if method else [plain.key(m) for m in ("POST", "GET", "PUT", "PATCH", "DELETE")]
    row = conn.execute(f"select r.* from index_resources r where r.resource_key = any(%s) {order}", (keys,)).fetchone()
    if row:
        return row
    templates = conn.execute(
        f"""
        select r.* from index_resources r
        where r.origin = %(origin)s and r.has_params and (%(method)s::text is null or r.method = %(method)s)
        order by r.tracked desc, r.bazaar_payers_30d desc nulls last, r.id
        """,
        {"origin": plain.origin, "method": method},
    ).fetchall()
    for template in templates:
        parsed = route(template["route_template"])
        if parsed and matches_template(parsed, plain):
            return template
    if heuristic:
        own = route(url, own_record=True)
        if own and own.key_path != plain.key_path:
            keys = [own.key(method)] if method else [own.key(m) for m in ("POST", "GET")]
            return conn.execute(f"select r.* from index_resources r where r.resource_key = any(%s) {order}", (keys,)).fetchone()
    return None


# --- Health ------------------------------------------------------------------------


def health(conn: psycopg.Connection, now: datetime) -> dict[str, Any]:
    counts = conn.execute(
        """
        select count(*)::int as listed,
               count(*) filter (where payable)::int as payable,
               count(*) filter (where tracked)::int as tracked,
               count(*) filter (where tracked and last_probed_at > %(day)s)::int as probed_24h,
               count(*) filter (where tracked and service_id is not null)::int as linked,
               count(*) filter (where tracked and liveness = 'alive')::int as alive,
               count(*) filter (where tracked and liveness = 'failing')::int as failing,
               count(*) filter (where tracked and liveness = 'dead')::int as dead,
               count(*) filter (where tracked and liveness = 'unknown')::int as unknown
        from index_resources
        """,
        {"day": now - timedelta(hours=24)},
    ).fetchone()
    observed = conn.execute(
        """
        select count(distinct o.resource_id)::int as n from index_observations o
        join index_resources r on r.id = o.resource_id
        where r.tracked and o.at > %s
        """,
        (now - timedelta(hours=24),),
    ).fetchone()["n"]
    reasons = {
        row["tracked_reason"]: row["n"]
        for row in conn.execute(
            "select tracked_reason, count(*)::int as n from index_resources where tracked group by 1 order by 1"
        ).fetchall()
    }
    sources = []
    for row in conn.execute(
        """
        select distinct on (source) source, finished_at, ok, items, resources, error,
          (select max(finished_at) from index_sweeps s2 where s2.source = s.source and s2.ok) as last_ok_at
        from index_sweeps s order by source, started_at desc
        """
    ).fetchall():
        last_ok = row["last_ok_at"]
        sources.append({
            "source": row["source"],
            "last_ok_at": last_ok.isoformat() if last_ok else None,
            "age_hours": round((now - last_ok).total_seconds() / 3600, 1) if last_ok else None,
            "last_run_ok": row["ok"],
            "items": row["items"],
            "resources": row["resources"],
            "error": row["error"],
        })
    coverage = conn.execute(
        """
        select count(*)::int as resources,
               count(*) filter (where r.tracked)::int as tracked
        from index_resource_urls u join index_resources r on r.id = u.resource_id
        where u.source = 'bazaar' and u.payers_30d >= 10
        """
    ).fetchone()
    changes = {
        row["kind"]: row["n"]
        for row in conn.execute(
            "select kind, count(*)::int as n from index_changes where at > %s group by 1", (now - timedelta(hours=24),)
        ).fetchall()
    }
    tracked = counts["tracked"]
    share = round(counts["probed_24h"] / tracked, 4) if tracked else None
    stale = [s["source"] for s in sources if s["source"] in FEED_SOURCES and (s["age_hours"] is None or s["age_hours"] > 26)]
    return {
        "ok": bool(tracked) and share is not None and share >= 0.95 and not stale,
        "generated_at": now.isoformat(),
        "tracked": tracked,
        "probed_24h": counts["probed_24h"],
        "probed_24h_share": share,
        "observed_24h_share": round(observed / tracked, 4) if tracked else None,
        "linked_to_service": counts["linked"],
        "liveness": {k: counts[k] for k in ("alive", "failing", "dead", "unknown")},
        "tracked_by_reason": reasons,
        "listed": counts["listed"],
        "payable": counts["payable"],
        "bazaar_10_payers": {
            "urls": coverage["resources"], "tracked": coverage["tracked"],
            "share": round(coverage["tracked"] / coverage["resources"], 4) if coverage["resources"] else None,
        },
        "changes_24h": changes,
        "sources": sources,
        "stale_sources": stale,
    }


def _jsonable(value: Any) -> Any:
    from decimal import Decimal
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(int(value)) if value == value.to_integral_value() else str(value)
    return value


_PUBLIC_FIELDS = (
    "id", "method", "route_template", "probe_url", "sources", "networks", "payable", "pay_to",
    "price_atomic", "price_asset", "price_network", "price_scheme", "bazaar_payers_30d",
    "bazaar_calls_30d", "bazaar_last_called_at", "first_listed_at", "last_listed_at",
    "service_name", "description", "tracked", "tracked_reason", "tracked_since", "proposal_ids",
    "service_id", "listing_type", "protocol",
)


def resource_detail(conn: psycopg.Connection, url: str, method: str | None = None) -> dict[str, Any] | None:
    """One resource with what C3 reads (the signals view), its latest observations and changes."""
    resource = resolve_resource(conn, url, method)
    if resource is None:
        return None
    signals = conn.execute(
        "select * from index_resource_signals where resource_id = %s", (resource["id"],)
    ).fetchone()
    observations = conn.execute(
        """
        select at, kind, ok, http_status, error_code, network, payee, pay_to, quoted_amount_atomic, asset,
               protocol, latency_ms
        from index_observations where resource_id = %s order by at desc limit 10
        """,
        (resource["id"],),
    ).fetchall()
    changes = conn.execute(
        "select at, kind, old_value, new_value from index_changes where resource_id = %s order by at desc limit 20",
        (resource["id"],),
    ).fetchall()
    return _jsonable({
        "ok": True,
        "resource": {k: resource.get(k) for k in _PUBLIC_FIELDS},
        "signals": {k: v for k, v in (signals or {}).items() if k not in ("route_template", "probe_url", "method")},
        "observations": observations,
        "changes": changes,
    })
