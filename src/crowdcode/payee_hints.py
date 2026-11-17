"""The payees CrowdCode already knows for an endpoint, for a review that names none.

A review of a paid call has to say which payee it paid, because the on-chain
check proves a transfer *to the service*. Until SRI-118 that payee came only
from the reviewer (`payment_target_ref`) or from the service's registration.
The services index creates a `services` row for every tracked endpoint with no
payee, so an agent that reviewed, say, `api.financialdatasets.ai/financials`
without passing the x402 `payTo` was refused as `payment_payee_unknown`, and
its verified review was lost. On 2026-11-17 5,374 listed services had no
registered payee while the index knew theirs.

So when a paid review names no payee, the candidates are the ones CrowdCode
learned itself:

1. ``index_resources.payees``: what the endpoint's own 402 advertised when
   CrowdCode's probe called it (crowdcode.index_probe);
2. ``index_resources.pay_to`` from the feeds (Bazaar, mpp.dev), but only when
   the endpoint's own 402 still advertises it now (crowdcode.advertised). A
   listing is a third party's word; the 402 is the seller's;
3. with no index entry at all, whatever the endpoint's 402 advertises now.

The reviewer never chooses a candidate. The on-chain check then accepts the
transfer from the reviewer's wallet to whichever candidate it reached, and that
payee is stored with the review and registered to the service. The probe runs
with no database connection held, at most once per service per cooldown, and
only against public https hosts.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

import psycopg

from crowdcode import advertised
from crowdcode.identity import (
    MACHINE_PAYMENT_PROVIDERS,
    ServiceIdentity,
    payee_rotates,
    resolve_service,
)

logger = logging.getLogger(__name__)

# More than this many payees for one endpoint is not a payee set, it is noise.
MAX_CANDIDATES = 8

_PROTOCOLS = {"x402": "x402", "mpp": "mppx", "mppx": "mppx"}


@dataclass(frozen=True)
class PayeeHint:
    """Payees an endpoint is known to collect at, in the order to try them."""

    payees: tuple[str, ...]
    # The machine-payment protocol the index lists for the endpoint, used when
    # the review names none.
    provider: str | None = None
    # payee -> how CrowdCode knows it ('index_probe' or 'live_402').
    sources: dict[str, str] = field(default_factory=dict)
    endpoint: str | None = None
    service_id: str | None = None


def _key(payee: str) -> str:
    return payee.lower() if payee.startswith("0x") else payee


def _index_rows(conn: psycopg.Connection, endpoint: str, service_id: str | None) -> list[dict[str, Any]]:
    """The index rows for this endpoint: those linked to its service, and the
    one its URL resolves to. A missing index (an older schema) is no rows."""
    from crowdcode.index import resolve_resource

    rows: list[dict[str, Any]] = []
    try:
        with conn.transaction():
            if service_id:
                rows += conn.execute(
                    "select id, protocol, pay_to, payees from index_resources where service_id = %s",
                    (service_id,),
                ).fetchall()
        row = resolve_resource(conn, endpoint)
    except psycopg.Error:
        logger.warning("index lookup for a review's payee failed")
        return rows
    if row is not None and all(r["id"] != row["id"] for r in rows):
        rows.append(row)
    return rows


def payee_hint(connect: Callable[[], Any], identity: ServiceIdentity) -> PayeeHint | None:
    """Candidate payees for a paid review that names none, or None.

    `connect` is the caller's connection factory; no connection is held while
    the endpoint's 402 is fetched.
    """
    if identity.payment_target_ref:
        return None
    if identity.payment_provider is not None and identity.payment_provider not in MACHINE_PAYMENT_PROVIDERS:
        return None
    with connect() as conn:
        resolved = resolve_service(conn, identity)
        if resolved.error:
            return None
        service = resolved.row
        if service is not None and payee_rotates(service):
            # A fresh payee per request: nothing stable to look up.
            return None
        endpoint = (resolved.identity.api_endpoint if resolved.identity else None) or identity.api_endpoint
        if not endpoint:
            return None
        rows = _index_rows(conn, endpoint, service["id"] if service else None)
        registered = service.get("payment_target_ref") if service else None

    probed: list[str] = []
    listed: list[str] = []
    protocols: set[str] = set()
    for row in rows:
        probed += [p for p in row.get("payees") or [] if p]
        listed += [p for p in row.get("pay_to") or [] if p]
        if _PROTOCOLS.get(row.get("protocol") or ""):
            protocols.add(_PROTOCOLS[row["protocol"]])

    sources: dict[str, str] = {}
    order: list[str] = []

    def add(payee: str, source: str) -> None:
        if _key(payee) not in {_key(p) for p in order}:
            order.append(payee)
            sources[payee] = source

    for payee in probed:
        add(payee, "index_probe")
    known = {_key(p) for p in order}
    unconfirmed = [p for p in listed if _key(p) not in known]
    # Ask the endpoint itself when a listing names a payee no probe has seen,
    # or when nothing at all is known about where the service collects.
    if unconfirmed or (not order and not registered):
        key = service["id"] if service else f"endpoint:{endpoint}"
        offers, note = advertised._advertised(key, endpoint)
        for offer in offers:
            if offer.provider in MACHINE_PAYMENT_PROVIDERS:
                add(offer.payee, "live_402")
                protocols.add(offer.provider)
        if unconfirmed:
            logger.info(json.dumps({"event": "review_payee_listing_checked",
                                    "endpoint": endpoint, "probe": note}))
    if not order:
        return None
    return PayeeHint(
        payees=tuple(order[:MAX_CANDIDATES]),
        provider=next(iter(protocols)) if len(protocols) == 1 else None,
        sources={p: sources[p] for p in order[:MAX_CANDIDATES]},
        endpoint=endpoint,
        service_id=service["id"] if service else None,
    )
