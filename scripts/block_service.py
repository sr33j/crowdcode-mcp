#!/usr/bin/env python3
"""Block or unblock a service for CrowdCode's reliability rule (SRI-115 C3).

A blocked service reads `unreliable` whatever its evidence says, and the
operator's note is its first public reason (crowdcode.app/reliability/<id>, and
the gateway's refusal). Every block and unblock is recorded in
`service_block_audit` with who did it and why, and the service's reliability
is recomputed straight away rather than at the next hourly run.

    python scripts/block_service.py --list
    python scripts/block_service.py --block svc_… --note "pays out to a wallet reported for fraud" --by srijith
    python scripts/block_service.py --unblock svc_… --note "seller confirmed the new wallet" --by srijith

The note is public: write it for the service's buyers, and never put a
person's name, an email or a secret in it.

Requires DATABASE_URL and supabase/reliability.sql applied.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from crowdcode import reliability
from crowdcode.db import connect


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--block", metavar="SERVICE_ID")
    action.add_argument("--unblock", metavar="SERVICE_ID")
    action.add_argument("--list", action="store_true", help="the services blocked now")
    parser.add_argument("--note", help="why (public for a block)")
    parser.add_argument("--by", help="who is doing this (recorded in the audit, not published)")
    args = parser.parse_args(argv)

    if args.list:
        with connect() as conn:
            rows = conn.execute(
                "select service_id, note, blocked_by, blocked_at from service_blocks order by blocked_at desc"
            ).fetchall()
        for row in rows:
            print(json.dumps(row, default=str))
        if not rows:
            print("no services are blocked")
        return 0

    if not args.note or not args.by:
        parser.error("--note and --by are required")
    service_id = args.block or args.unblock
    try:
        with connect() as conn:
            if args.block:
                reliability.block(conn, service_id, args.note, args.by)
            elif not reliability.unblock(conn, service_id, args.note, args.by):
                print(f"{service_id} was not blocked; nothing changed")
                return 1
            conn.commit()
    except reliability.BlockError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    reliability.recompute(connect, datetime.now(UTC), [service_id])
    with connect() as conn:
        payload = reliability.for_service(conn, service_id)
    print(json.dumps({k: payload[k] for k in ("service_id", "state", "reasons")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
