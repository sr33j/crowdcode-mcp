#!/usr/bin/env python3
"""Repair services registered with a non-address machine-payment payee.

`services.payment_target_ref` *is* the payee address for an x402 or mppx
service: it is what an on-chain check runs against and what decides whether a
supplied payee names this service. identity.normalize_payment_target now
refuses to store anything else, but rows predate that guard — notably
svc_e91d4c0dfb719d9fefe8 ("Parallel MPP Search"), registered with the literal
string `parallel`, its own directory slug. The consequence is permanent and
silent: `_payee_family("parallel")` is None, so the service has no canonical
payee of any address family while a registered alias holds the EVM slot, and
every real payee therefore resolves to `service identity conflict`. Two
reviews of a genuine paid call were lost to it on 2026-10-27.

The payee cannot be guessed, so it is read from the only authority on it: the
service's own 402. This script calls each affected endpoint **twice** and
parses the payees it advertises — x402 `accepts[].payTo`, and the `recipient`
of an MPP `WWW-Authenticate: Payment` challenge — because what the two probes
disagree about decides the repair:

  * Same payees both times: the service has a stable payee. Set
    `payment_target_ref` to the one advertised for the rail the service is
    registered under, and register the others as `payment_target` identifiers,
    so a purchase settled on either rail still resolves to this service (a
    provider may collect on Base and on Tempo at different addresses, and a
    service holds only one canonical payee per address family).
  * Different payees: the provider issues a single-use destination per
    request, which is what Parallel does. There is nothing to store, and
    pinning the first one seen is exactly what broke it, so set `payee_rotates`
    and leave the payee empty (identity.payee_rotates).

Either way the non-address `payment_target` identifiers, which name nothing,
are deleted, and what was observed is recorded in
`services.metadata.payee_corrections`.

Registering an advertised payee is a deliberate exception to the rule that
aliases come only from a verified payment: this is the provider's own live
offer, the same document an agent would pay against, and an operator ran it.
The evidence is kept in the audit entry.

Dry run by default:

    python scripts/fix_machine_payees.py
    python scripts/fix_machine_payees.py --apply

Services whose endpoint advertises nothing usable are reported and left
untouched; a `.example.test` smoke-test row cannot be fixed this way and
should be deleted by hand instead.

Requires DATABASE_URL, and supabase/rotating-payees.sql applied.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from dataclasses import dataclass
from typing import Any

import httpx
from eth_utils import to_checksum_address
from psycopg.types.json import Jsonb

from crowdcode.db import connect
from crowdcode.identity import (
    MACHINE_PAYMENT_PROVIDERS,
    normalize_payment_target,
    payment_identifier,
)
from crowdcode.payments import utc_now

PROBE_TIMEOUT = 20.0
# Two probes, because one cannot tell a stable payee from a rotating one.
PROBES = 2
# The 402 is the response to a real call, so the probe has to look like one.
# A body of `{}` is enough: a paywall answers 402 before it validates input.
PROBE_BODY: dict[str, Any] = {}


@dataclass(frozen=True)
class Offer:
    provider: str
    payee: str
    network: str | None


@dataclass(frozen=True)
class Advertised:
    offers: list[Offer]
    rotating: bool
    note: str


def _normalize(payee: str) -> str:
    """Checksum an EVM payee; leave a base58 Solana payee exactly as given."""
    if re.fullmatch(r"0x[0-9a-fA-F]{40}", payee):
        return to_checksum_address(payee)
    return payee


def _x402_offers(body: Any, header: str | None) -> list[Offer]:
    """x402 `accepts[].payTo`, from the JSON body or the base64 header twin."""
    payload = body if isinstance(body, dict) else None
    if payload is None and header:
        try:
            payload = json.loads(base64.b64decode(header + "=" * (-len(header) % 4)))
        except Exception:
            payload = None
    if not isinstance(payload, dict):
        return []
    offers = []
    for accept in payload.get("accepts") or []:
        if not isinstance(accept, dict):
            continue
        payee = accept.get("payTo")
        if isinstance(payee, str) and payee.strip():
            network = accept.get("network")
            offers.append(Offer("x402", _normalize(payee.strip()),
                                network if isinstance(network, str) else None))
    return offers


def _mpp_offers(header: str | None) -> list[Offer]:
    """`Payment method="tempo", request="<base64url JSON>"` challenges.

    The challenge's `request` payload names the `recipient` the payment must
    reach; mppx settles on Tempo.
    """
    if not header:
        return []
    offers = []
    for segment in re.split(r",\s*(?=Payment\s)", header):
        if not re.match(r"^\s*Payment\s", segment, re.IGNORECASE):
            continue
        params = dict(
            (match.group(1), match.group(2) if match.group(2) is not None else match.group(3))
            for match in re.finditer(
                r'(\w+)=(?:"([^"]*)"|([^\s,]+))',
                re.sub(r"^\s*Payment\s+", "", segment, flags=re.IGNORECASE),
            )
        )
        raw = params.get("request")
        if not raw:
            continue
        try:
            request = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        except Exception:
            continue
        payee = request.get("recipient") if isinstance(request, dict) else None
        if isinstance(payee, str) and payee.strip():
            details = request.get("methodDetails")
            chain = details.get("chainId") if isinstance(details, dict) else None
            method = params.get("method") or "tempo"
            offers.append(Offer("mppx", _normalize(payee.strip()),
                                f"{method}:{chain}" if chain else method))
    return offers


def probe(endpoint: str) -> tuple[list[Offer], str]:
    """One call to the endpoint, and the payees its 402 advertised."""
    with httpx.Client(timeout=PROBE_TIMEOUT, follow_redirects=False) as client:
        try:
            response = client.post(endpoint, json=PROBE_BODY)
            if response.status_code != 402:
                response = client.get(endpoint)
        except httpx.HTTPError as error:
            return [], f"probe failed: {type(error).__name__}: {error}"
    try:
        body = response.json()
    except ValueError:
        body = None
    offers = _x402_offers(body, response.headers.get("payment-required"))
    offers += _mpp_offers(response.headers.get("www-authenticate"))
    seen: dict[tuple[str, str], Offer] = {}
    for offer in offers:
        seen.setdefault((offer.provider, offer.payee.lower()), offer)
    note = f"HTTP {response.status_code}; " + (
        ", ".join(f"{o.provider} {o.payee} ({o.network})" for o in seen.values())
        or "no payee advertised"
    )
    return list(seen.values()), note


def advertised_payees(endpoint: str) -> Advertised:
    """What the endpoint's own 402 advertises, probed twice.

    Two probes that disagree about the payees mean the provider issues one per
    request, which calls for a different repair; see the module docstring.
    """
    rounds = [probe(endpoint) for _ in range(PROBES)]
    keys = [{(o.provider, o.payee.lower()) for o in offers} for offers, _ in rounds]
    return Advertised(
        offers=rounds[0][0],
        rotating=bool(rounds[0][0]) and any(key != keys[0] for key in keys[1:]),
        note=" | ".join(note for _, note in rounds),
    )


def _is_payee(value: str | None) -> bool:
    """Whether identity would accept this as a machine-payment payee."""
    try:
        normalize_payment_target("x402", value)
    except ValueError:
        return False
    return value is not None and value.strip() != ""


def broken_services(conn: Any) -> list[dict[str, Any]]:
    """Machine-payment services whose canonical payee is not an address, plus
    any service holding a non-address `payment_target` identifier."""
    rows = conn.execute(
        """
        select s.id, s.name, s.canonical_endpoint, s.payment_provider,
               s.payment_target_ref, s.payee_rotates, s.metadata,
               coalesce(array_agg(si.identifier_value)
                        filter (where si.identifier_value is not null),
                        '{}'::text[]) as targets
        from services s
        left join service_identifiers si
          on si.service_id = s.id and si.identifier_type = 'payment_target'
        where s.payment_provider = any(%s)
        group by s.id
        order by s.id
        """,
        (sorted(MACHINE_PAYMENT_PROVIDERS),),
    ).fetchall()
    broken = []
    for row in rows:
        bad_identifiers = [
            value
            for value in row["targets"]
            if value.partition(":")[0] in MACHINE_PAYMENT_PROVIDERS
            and not _is_payee(value.partition(":")[2])
        ]
        # A rotating-payee service is settled with no payee at all; every
        # other machine-payment service needs an address.
        settled = (
            bool(row["payee_rotates"])
            if row["payment_target_ref"] is None
            else _is_payee(row["payment_target_ref"])
        )
        if settled and not bad_identifiers:
            continue
        broken.append({**row, "bad_identifiers": bad_identifiers})
    return broken


def main_with(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the corrections (default: report what would change)",
    )
    args = parser.parse_args(argv)

    now = utc_now()
    failures = 0
    with connect() as conn:
        broken = broken_services(conn)
        if not broken:
            print("no machine-payment service has a non-address payee")
            return 0
        print(f"{len(broken)} service(s) with a non-address payee:\n")

        for service in broken:
            print(f"{service['id']}  {service['name']}")
            print(f"  endpoint          {service['canonical_endpoint']}")
            print(f"  registered        {service['payment_provider']}:"
                  f"{service['payment_target_ref']}")
            if service["bad_identifiers"]:
                print(f"  bad identifiers   {', '.join(service['bad_identifiers'])}")

            advertised = advertised_payees(service["canonical_endpoint"])
            print(f"  402               {advertised.note}")
            canonical: Offer | None = None
            aliases: list[Offer] = []
            if advertised.rotating:
                print("  payee             rotates per request; payee_rotates "
                      "= true and no payee stored")
            else:
                canonical = next(
                    (o for o in advertised.offers
                     if o.provider == service["payment_provider"]),
                    None,
                )
                if canonical is None and len(advertised.offers) == 1:
                    canonical = advertised.offers[0]
                if canonical is None:
                    print("  SKIPPED: the endpoint advertises no payee for this "
                          "rail; fix or remove this service by hand\n")
                    failures += 1
                    continue
                aliases = [o for o in advertised.offers if o is not canonical]
                print(f"  payee             {service['payment_target_ref']} -> "
                      f"{canonical.payee}")
                for alias in aliases:
                    print("  alias             "
                          f"{payment_identifier(alias.provider, alias.payee)}")
            if not args.apply:
                print()
                continue

            if canonical is None:
                conn.execute(
                    """update services
                       set payee_rotates = true, payment_target_ref = null
                       where id = %s""",
                    (service["id"],),
                )
            else:
                conn.execute(
                    """update services
                       set payment_target_ref = %s, payee_rotates = false
                       where id = %s""",
                    (canonical.payee, service["id"]),
                )
                for offer in advertised.offers:
                    conn.execute(
                        """
                        insert into service_identifiers
                          (service_id, identifier_type, identifier_value)
                        values (%s, 'payment_target', %s)
                        on conflict do nothing
                        """,
                        (service["id"],
                         payment_identifier(offer.provider, offer.payee)),
                    )
            for value in service["bad_identifiers"]:
                conn.execute(
                    """
                    delete from service_identifiers
                    where service_id = %s and identifier_type = 'payment_target'
                      and identifier_value = %s
                    """,
                    (service["id"], value),
                )
            metadata = dict(service["metadata"] or {})
            corrections = list(metadata.get("payee_corrections") or [])
            corrections.append(
                {
                    "at": now.isoformat(),
                    "from": service["payment_target_ref"],
                    "to": canonical.payee if canonical else None,
                    "payee_rotates": canonical is None,
                    "aliases_registered": [
                        payment_identifier(o.provider, o.payee) for o in aliases
                    ],
                    "identifiers_removed": service["bad_identifiers"],
                    "source": "advertised 402 offer",
                    "evidence": advertised.note,
                    "script": "scripts/fix_machine_payees.py",
                }
            )
            metadata["payee_corrections"] = corrections
            conn.execute(
                "update services set metadata = %s where id = %s",
                (Jsonb(metadata), service["id"]),
            )
            print("  applied\n")

        if args.apply:
            conn.commit()
        else:
            print("dry run: rerun with --apply to write these corrections")

    if failures:
        print(f"{failures} service(s) could not be derived from a live 402",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main_with())
