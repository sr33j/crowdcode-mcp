"""Which payment networks each service is seen on, for the public services API.

Two kinds of evidence, kept separate so the site never presents a guess as a
verified fact:

- verified: networks on which CrowdCode verified a real payment to the service
  (reviews at a verified payment level). The network recorded at verification
  time wins; older rows fall back to the reference spelling
  (x402:<network>:<tx>, mppx:tempo:<tx>) and then to the rail's default chain
  (a bare 0x hash is x402 on Base, mppx always settles on Tempo).
- advertised: networks pinned by the service's registered payment identifiers.
  An MPP payee settles on Tempo and a base58 x402 payee is a Solana address;
  an EVM x402 payee does not name its chain, so it advertises nothing here.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from crowdcode.payments import TX_HASH_RE, x402_network
from crowdcode.solana import is_solana_address, is_solana_signature

NETWORK_LABELS = {
    "base": "Base",
    "solana": "Solana",
    "tempo": "Tempo",
    "monad": "Monad",
}
# Display order for ties: the networks hosted agents pay on first.
NETWORK_ORDER = ("base", "solana", "tempo", "monad")

_REFERENCE_PREFIXES = (
    ("x402:base:", "base"),
    ("x402:monad:", "monad"),
    ("x402:solana:", "solana"),
    ("mppx:tempo:", "tempo"),
)


def _known(network: str | None) -> str | None:
    return network if network in NETWORK_LABELS else None


def review_payment_network(
    provider: str | None, recorded_network: str | None, reference: str | None
) -> str | None:
    """The network a verified review's payment settled on, or None."""
    if recorded_network:
        recorded = recorded_network.strip().lower()
        if recorded == "tempo":
            return "tempo"
        recorded = _known(x402_network(recorded))
        if recorded:
            return recorded
    cleaned = (reference or "").strip()
    lowered = cleaned.lower()
    for prefix, network in _REFERENCE_PREFIXES:
        if lowered.startswith(prefix):
            return network
    if provider == "mppx":
        return "tempo"
    if provider == "x402":
        if TX_HASH_RE.match(cleaned):
            return "base"
        if is_solana_signature(cleaned):
            return "solana"
    return None


def payment_target_network(provider: str | None, target: str | None) -> str | None:
    """The network a registered payee pins, or None when it does not name one."""
    if provider == "mppx":
        return "tempo"
    if provider == "x402" and is_solana_address((target or "").strip()):
        return "solana"
    return None


def payment_identifier_network(value: str | None) -> str | None:
    """Network for a service_identifiers payment_target value (provider:target)."""
    provider, _, target = (value or "").partition(":")
    return payment_target_network(provider.strip().lower(), target)


def _ordered(networks: Iterable[str]) -> list[str]:
    return sorted(set(networks), key=NETWORK_ORDER.index)


def service_networks(
    verified_counts: dict[str, int], advertised: Iterable[str]
) -> dict[str, Any]:
    """API shape shared by /api/services and /api/services/{id}."""
    verified = sorted(
        (network for network, count in verified_counts.items() if count > 0),
        key=lambda network: (-verified_counts[network], NETWORK_ORDER.index(network)),
    )
    advertised_list = _ordered(advertised)
    return {
        "networks": verified + [n for n in advertised_list if n not in verified],
        "verified_networks": [
            {
                "network": network,
                "label": NETWORK_LABELS[network],
                "verified_payments": verified_counts[network],
            }
            for network in verified
        ],
        "advertised_networks": advertised_list,
    }


def fetch_service_networks(
    conn: Any, service_id: str | None = None
) -> dict[str, dict[str, Any]]:
    """Per-service network evidence; every service with evidence is keyed."""
    where = "" if service_id is None else " and service_id = %s"
    params: tuple[Any, ...] = () if service_id is None else (service_id,)
    reviews = conn.execute(
        """
        select service_id, payment_provider,
               payment_verification_metadata->>'network' as network,
               coalesce(payment_reference, payment_reference_canonical) as reference
        from reviews
        where payment_verified
        """
        + where,
        params,
    ).fetchall()
    services = conn.execute(
        "select id as service_id, payment_provider, payment_target_ref from services"
        + ("" if service_id is None else " where id = %s"),
        params,
    ).fetchall()
    identifiers = conn.execute(
        """
        select service_id, identifier_value
        from service_identifiers
        where identifier_type = 'payment_target'
        """
        + where,
        params,
    ).fetchall()

    verified: dict[str, dict[str, int]] = {}
    for row in reviews:
        network = review_payment_network(
            row["payment_provider"], row["network"], row["reference"]
        )
        if network:
            counts = verified.setdefault(row["service_id"], {})
            counts[network] = counts.get(network, 0) + 1

    advertised: dict[str, set[str]] = {}
    for row in services:
        network = payment_target_network(row["payment_provider"], row["payment_target_ref"])
        if network:
            advertised.setdefault(row["service_id"], set()).add(network)
    for row in identifiers:
        network = payment_identifier_network(row["identifier_value"])
        if network:
            advertised.setdefault(row["service_id"], set()).add(network)

    return {
        sid: service_networks(verified.get(sid, {}), advertised.get(sid, ()))
        for sid in set(verified) | set(advertised)
    }


EMPTY_NETWORKS = service_networks({}, ())
