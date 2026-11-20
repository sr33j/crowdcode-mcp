#!/usr/bin/env python3
"""Fold listings filed under one job's or one record's URL into their route (SRI-135).

Until crowdcode.route.v1 a service's endpoint was the exact URL reviewed, so
an asynchronous service whose collect step has a URL per job became one
listing per job: the six clips of one BlockRun cartoon on 2026-11-20 were six
services (`.../videos/generations/<job id>`), one review each, and BlockRun's
video listing itself had none. `build_identity` now files such URLs under
their route template (`.../generations/{id}`); this script moves what was
already filed the old way.

For every service whose `canonical_endpoint` is not its own route template:

- the **target** is, in order: the listing of the async job's submit endpoint
  when the pair is known below (`ASYNC_PAIRS`: the provider documents the two
  together and its submit answer names the collect URL), an existing listing
  at the route template, or else the oldest of the group, renamed to the
  template;
- every review, identifier and other row naming a merged service is moved to
  the target (a per-service row that already exists there, such as a stored
  reliability verdict, is dropped and recomputed); the merged service is then
  deleted;
- the target gets `api_endpoint` identifiers for its route template (and its
  submit endpoint), its score is recomputed and its summary queued.

Reviews are moved, never edited: rating, text, weight and attestation stay as
they were stored.

Dry run by default:

    python scripts/merge_route_listings.py
    python scripts/merge_route_listings.py --apply

Requires DATABASE_URL.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from typing import Any

import psycopg

from crowdcode.db import connect
from crowdcode.identity import listing_endpoint, normalize_api_endpoint
from crowdcode.payments import utc_now
from crowdcode.reputation import recompute_service_score

# Collect routes whose submit endpoint is the same service, documented together
# by the provider. BlockRun: `POST /api/v1/videos/generations` answers
# `{"id": "v1_...", "poll_url": "/api/v1/videos/generations/v1_..."}` and
# charges when `GET .../generations/{id}` returns the clip.
ASYNC_PAIRS = {
    "https://blockrun.ai/api/v1/videos/generations/{id}": "https://blockrun.ai/api/v1/videos/generations",
}


def _route(endpoint: str | None) -> str | None:
    try:
        return listing_endpoint(normalize_api_endpoint(endpoint))
    except ValueError:
        return None


def plan(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Each merge: the target and the services folded into it."""
    rows = conn.execute(
        "select id, name, canonical_endpoint, created_at from services where canonical_endpoint is not null order by created_at, id"
    ).fetchall()
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        route = _route(row["canonical_endpoint"])
        if route is not None and route != row["canonical_endpoint"]:
            groups[route].append(row)
    by_endpoint = {row["canonical_endpoint"]: row for row in rows}
    merges = []
    for route, members in sorted(groups.items()):
        submit = ASYNC_PAIRS.get(route)
        target = by_endpoint.get(submit) if submit else None
        rename = None
        if target is None:
            target = by_endpoint.get(route)
        if target is None:
            target, members = members[0], members[1:]
            rename = route
        paired = submit if submit and by_endpoint.get(submit) is target else None
        # The route (and submit) endpoint must not already identify a third
        # service: resolution would then find that one instead.
        involved = {target["id"], *(m["id"] for m in members)}
        held = conn.execute(
            """select identifier_value, service_id from service_identifiers
               where identifier_type = 'api_endpoint' and identifier_value = any(%s)""",
            ([value for value in (route, paired) if value],),
        ).fetchall()
        merges.append({
            "route": route,
            "submit": paired,
            "target": target["id"],
            "target_endpoint": target["canonical_endpoint"],
            "rename_to": rename,
            "merged": [m["id"] for m in members],
            "merged_endpoints": [m["canonical_endpoint"][:120] for m in members],
            **({"conflicts": sorted(f"{r['identifier_value']} -> {r['service_id']}" for r in held if r["service_id"] not in involved)}
               if any(r["service_id"] not in involved for r in held) else {}),
        })
    return merges


def _service_columns(conn: psycopg.Connection) -> list[str]:
    """Every table in this schema with a text `service_id` column, except the ones handled by name.

    Only text: a `service_id` that is a uuid names something else (a hosted
    agent's own service), never a directory listing."""
    rows = conn.execute(
        """
        select table_name from information_schema.columns
        where table_schema = current_schema() and column_name = 'service_id'
          and data_type in ('text', 'character varying')
          and table_name not in ('services', 'reviews', 'service_identifiers')
          and table_name in (select table_name from information_schema.tables
                             where table_schema = current_schema() and table_type = 'BASE TABLE')
        order by table_name
        """
    ).fetchall()
    return [row["table_name"] for row in rows]


def _move(conn: psycopg.Connection, table: str, target: str, member: str) -> str:
    """Point a table's rows at the target; a per-service row the target already has is dropped."""
    try:
        with conn.transaction():
            moved = conn.execute(f'update "{table}" set service_id = %s where service_id = %s', (target, member)).rowcount
        return f"{table}: moved {moved}"
    except psycopg.errors.UniqueViolation:
        dropped = conn.execute(f'delete from "{table}" where service_id = %s', (member,)).rowcount
        return f"{table}: target already has one; dropped {dropped}"


def _identifier(conn: psycopg.Connection, service_id: str, kind: str, value: str) -> None:
    conn.execute(
        """
        insert into service_identifiers (service_id, identifier_type, identifier_value)
        values (%s, %s, %s) on conflict do nothing
        """,
        (service_id, kind, value),
    )


def apply(conn: psycopg.Connection, merge: dict[str, Any], tables: list[str]) -> list[str]:
    target = merge["target"]
    log = []
    if merge["rename_to"]:
        conn.execute("update services set canonical_endpoint = %s where id = %s", (merge["rename_to"], target))
        log.append(f"renamed {target} to {merge['rename_to']}")
    for member in merge["merged"]:
        reviews = conn.execute("update reviews set service_id = %s where service_id = %s", (target, member)).rowcount
        log.append(f"{member}: moved {reviews} review(s)")
        # Identifiers: moved one by one, so one the target already holds is
        # dropped rather than failing the move.
        for row in conn.execute(
            "select id, identifier_type, identifier_value from service_identifiers where service_id = %s", (member,)
        ).fetchall():
            try:
                with conn.transaction():
                    conn.execute("update service_identifiers set service_id = %s where id = %s", (target, row["id"]))
            except psycopg.errors.UniqueViolation:
                conn.execute("delete from service_identifiers where id = %s", (row["id"],))
        for table in tables:
            log.append(f"{member}: {_move(conn, table, target, member)}")
        conn.execute("delete from services where id = %s", (member,))
    _identifier(conn, target, "api_endpoint", merge["route"])
    if merge["submit"]:
        _identifier(conn, target, "api_endpoint", merge["submit"])
    conn.execute("update services set last_summarized_at = null where id = %s", (target,))
    result = recompute_service_score(conn, target, utc_now())
    log.append(f"{target}: score {result.score:.6f} n_eff {result.n_eff:.6f}; summary queued")
    return log


def main_with(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write the merges (default: report what would change)")
    args = parser.parse_args(argv)
    with connect() as conn:
        merges = plan(conn)
        for merge in merges:
            print(json.dumps(merge, sort_keys=True))
        if not merges:
            print("nothing to do")
            return 0
        if any("conflicts" in merge for merge in merges):
            print("REFUSING: a route endpoint already identifies another service (see conflicts)", file=sys.stderr)
            return 1
        if not args.apply:
            print(f"\ndry run: {len(merges)} route(s), {sum(len(m['merged']) for m in merges)} listing(s) to fold; rerun with --apply")
            return 0
        tables = _service_columns(conn)
        for merge in merges:
            for line in apply(conn, merge, tables):
                print(line)
        conn.commit()
        print(f"\nfolded {sum(len(m['merged']) for m in merges)} listing(s) into {len(merges)} route(s)")
    # Reliability verdicts for the targets are recomputed by the next hourly
    # pass; asking now keeps the directory consistent meanwhile.
    try:
        from crowdcode import reliability

        reliability.recompute(connect, utc_now(), [m["target"] for m in merges])
    except Exception as error:  # the merge is committed either way
        print(f"reliability recompute deferred to the hourly pass: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main_with())
