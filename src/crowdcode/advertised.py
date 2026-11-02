"""The payees a service advertises in its own 402, and learning them.

A machine-payment service may legitimately collect at more than one address
in the same family. Exa (`https://api.exa.ai/search`) advertises two Base
payees in one 402, `0x6d6E…9192` and `0xB98e…2dbC`, plus a Solana payee, and
which one a buyer pays depends on which `accepts` entry it picks. The service
was registered from an mppx payment to `0xB98e…`, so every Base purchase the
hosted gateway made — it picks `0x6d6E…` — was refused as
`service identity conflict`: 62 hosted reviews of the most-bought paid search
in the week to 2026-11-02 (SRI-69).

A service therefore holds a *set* of payees: its canonical payee plus every
`payment_target` identifier. Two things add to it. A verified payment adds the
first payee of an address family it has none of (identity.
`_machine_payment_alias_authorized`). And the service's own 402 adds every
payee it advertises, which is what this module does: when a supplied payee is
the only reason a lookup conflicts, ask the service's registered endpoint what
it accepts, register every payee that answer advertises, and accept the
supplied payee if it is one of them.

What keeps a stranger's address out: the payee must appear in the 402 served
by an endpoint already registered to this service, fetched by CrowdCode
itself, at the moment of the lookup. A caller chooses neither the endpoint nor
the answer. A payee the 402 does not advertise is not registered and the
lookup still conflicts. Nothing here weights a review: ranking weight still
needs a gateway receipt for a settled purchase (receipts.evaluate_attestation),
and the on-chain check still proves the transfer reached the named payee.

The probe is an unpaid call to a third party, so it never runs while a
database connection is held, never follows a redirect, refuses any host that
resolves to a non-public address, and runs at most once per service per
PROBE_COOLDOWN, whatever payees callers name.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import logging
import re
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from eth_utils import to_checksum_address

logger = logging.getLogger(__name__)

PROBE_TIMEOUT = 5.0
# A paywall answers 402 before it validates input, so `{}` is enough to look
# like a real call; a GET is the fallback for services that only take GET.
PROBE_BODY: dict[str, Any] = {}
PROBE_COOLDOWN = 600.0
PROBE_RETRY = 60.0
_COOLDOWN_LIMIT = 10_000


@dataclass(frozen=True)
class Offer:
    provider: str
    payee: str
    network: str | None


def _normalize(payee: str) -> str:
    """Checksum an EVM payee; leave a base58 Solana payee exactly as given."""
    if re.fullmatch(r"0x[0-9a-fA-F]{40}", payee):
        return to_checksum_address(payee)
    return payee


def x402_offers(body: Any, header: str | None) -> list[Offer]:
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


def mpp_offers(header: str | None) -> list[Offer]:
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


def public_endpoint(endpoint: str) -> bool:
    """Whether CrowdCode may call this endpoint unprompted.

    Endpoints are registered by whoever first reviewed them, so one could name
    a host on our own network. Only https to a host whose every address is
    public is probed.
    """
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    addresses = {info[4][0] for info in infos}
    return bool(addresses) and all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addresses)


def probe(endpoint: str) -> tuple[list[Offer], str]:
    """One unpaid call to the endpoint, and the payees its 402 advertised."""
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
    offers = x402_offers(body, response.headers.get("payment-required"))
    offers += mpp_offers(response.headers.get("www-authenticate"))
    seen: dict[tuple[str, str], Offer] = {}
    for offer in offers:
        seen.setdefault((offer.provider, offer.payee.lower()), offer)
    note = f"HTTP {response.status_code}; " + (
        ", ".join(f"{o.provider} {o.payee} ({o.network})" for o in seen.values())
        or "no payee advertised"
    )
    return list(seen.values()), note


def advertises(offers: list[Offer], payee: str) -> bool:
    """Whether the offers name this payee (EVM case-insensitively)."""
    if payee.startswith("0x"):
        return any(o.payee.lower() == payee.lower() for o in offers)
    return any(o.payee == payee for o in offers)


# What each service's 402 said, by service ID: (monotonic time, endpoint,
# offers, note). One probe per service per PROBE_COOLDOWN however many payees
# callers name, so a lookup can never turn into a stream of calls to a third
# party; an empty answer (an outage, a timeout) is retried sooner.
_cooldown: dict[str, tuple[float, str, list[Offer], str]] = {}
_cooldown_lock = threading.Lock()


def _advertised(service_id: str, endpoint: str) -> tuple[list[Offer], str]:
    now = time.monotonic()
    with _cooldown_lock:
        cached = _cooldown.get(service_id)
        if cached is not None and cached[1] == endpoint:
            ttl = PROBE_COOLDOWN if cached[2] else PROBE_RETRY
            if now - cached[0] < ttl:
                return cached[2], cached[3]
        if len(_cooldown) >= _COOLDOWN_LIMIT:
            _cooldown.clear()
        # Claimed before the call, so concurrent lookups do not all probe.
        _cooldown[service_id] = (now, endpoint, [], "probe in progress")
    if public_endpoint(endpoint):
        offers, note = probe(endpoint)
    else:
        offers, note = [], "not probed: not a public https endpoint"
    with _cooldown_lock:
        _cooldown[service_id] = (time.monotonic(), endpoint, offers, note)
    return offers, note


def learn_advertised_payee(connect: Any, identity: Any) -> bool:
    """Register the payee set a service advertises, if `identity` needs it.

    `connect` is the caller's connection factory. Returns True when the
    supplied payee is advertised, and so now registered to the service, so the
    caller's own resolution will accept it. Every failure is a quiet False:
    the caller then resolves exactly as it would have, and reports the
    conflict. Whatever the 402 advertised is registered either way; it is the
    service's own word.
    """
    from crowdcode import identity as identity_mod

    payee = identity.payment_target_ref
    # Most lookups name no machine payee; they cost no extra connection.
    if identity.payment_provider not in identity_mod.MACHINE_PAYMENT_PROVIDERS or not payee:
        return False
    with connect() as conn:
        candidate = identity_mod.unregistered_payee_service(conn, identity)
    if candidate is None:
        return False
    service_id, endpoint = candidate
    offers, note = _advertised(service_id, endpoint)
    if offers:
        with connect() as conn:
            registered = identity_mod.register_advertised_payees(
                conn, service_id, endpoint, offers, note
            )
            conn.commit()
        if registered:
            logger.info(json.dumps({"event": "payees_learned_from_402", "service_id": service_id,
                                    "endpoint": endpoint, "registered": registered}))
    if advertises(offers, payee):
        return True
    logger.info(json.dumps({"event": "payee_not_advertised", "service_id": service_id,
                            "endpoint": endpoint, "payee": payee, "probe": note}))
    return False
