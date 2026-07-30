from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import psycopg


PAYMENT_PROVIDERS = {"stripe", "stripe_payment_link", "mppx", "x402", "manual"}
PAYMENT_PROVIDER_ALIASES = {
    "link": "stripe_payment_link",
    "stripe_link": "stripe_payment_link",
    "payment_link": "stripe_payment_link",
    "mpp": "mppx",
}


@dataclass(frozen=True)
class ServiceIdentity:
    service_id: str | None = None
    service_name: str | None = None
    api_endpoint: str | None = None
    payment_provider: str | None = None
    payment_target_ref: str | None = None
    directory_slug: str | None = None


@dataclass(frozen=True)
class ResolvedService:
    row: dict[str, Any] | None
    created: bool = False
    error: str | None = None
    identity: ServiceIdentity | None = None


def clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def normalize_payment_provider(value: str | None) -> str | None:
    cleaned = clean_optional(value)
    if cleaned is None:
        return None
    provider = cleaned.lower().replace("-", "_")
    provider = PAYMENT_PROVIDER_ALIASES.get(provider, provider)
    if provider not in PAYMENT_PROVIDERS:
        raise ValueError(
            "payment_provider must be one of: "
            + ", ".join(sorted(PAYMENT_PROVIDERS))
        )
    return provider


def normalize_api_endpoint(value: str | None) -> str | None:
    cleaned = clean_optional(value)
    if cleaned is None:
        return None

    candidate = cleaned if "://" in cleaned else f"https://{cleaned}"
    parsed = urlsplit(candidate)
    if not parsed.netloc:
        raise ValueError("api_endpoint must include a host")

    scheme = (parsed.scheme or "https").lower()
    host = parsed.hostname.lower() if parsed.hostname else ""
    if not host:
        raise ValueError("api_endpoint must include a host")

    netloc = host
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"

    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, "", ""))


def canonical_origin(api_endpoint: str | None) -> str | None:
    normalized = normalize_api_endpoint(api_endpoint)
    if normalized is None:
        return None
    parsed = urlsplit(normalized)
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def payment_identifier(payment_provider: str, payment_target_ref: str) -> str:
    return f"{payment_provider}:{payment_target_ref.strip()}"


def generate_service_id(
    api_endpoint: str,
    payment_provider: str,
    payment_target_ref: str,
) -> str:
    material = "|".join(
        [
            normalize_api_endpoint(api_endpoint) or "",
            payment_provider,
            payment_target_ref.strip(),
        ]
    )
    return "svc_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def build_identity(
    *,
    service_id: str | None = None,
    service_name: str | None = None,
    api_endpoint: str | None = None,
    payment_provider: str | None = None,
    payment_target_ref: str | None = None,
    directory_slug: str | None = None,
) -> ServiceIdentity:
    provider = normalize_payment_provider(payment_provider)
    endpoint = normalize_api_endpoint(api_endpoint)
    return ServiceIdentity(
        service_id=clean_optional(service_id),
        service_name=clean_optional(service_name),
        api_endpoint=endpoint,
        payment_provider=provider,
        payment_target_ref=clean_optional(payment_target_ref),
        directory_slug=clean_optional(directory_slug),
    )


def has_strong_identity(identity: ServiceIdentity) -> bool:
    return bool(
        identity.api_endpoint
        and identity.payment_provider
        and identity.payment_target_ref
    )


def _fetch_service(conn: psycopg.Connection, service_id: str) -> dict[str, Any] | None:
    return conn.execute(
        """
        select id, name, directory_slug, canonical_origin, canonical_endpoint,
               payment_provider, payment_target_ref, created_from_review
        from services
        where id = %s
        """,
        (service_id,),
    ).fetchone()


def _fetch_by_identifier(
    conn: psycopg.Connection,
    identifier_type: str,
    identifier_value: str,
) -> dict[str, Any] | None:
    return conn.execute(
        """
        select s.id, s.name, s.directory_slug, s.canonical_origin,
               s.canonical_endpoint, s.payment_provider, s.payment_target_ref,
               s.created_from_review
        from service_identifiers si
        join services s on s.id = si.service_id
        where si.identifier_type = %s
          and si.identifier_value = %s
        """,
        (identifier_type, identifier_value),
    ).fetchone()


def _fetch_by_directory_slug(
    conn: psycopg.Connection,
    directory_slug: str,
) -> dict[str, Any] | None:
    return conn.execute(
        """
        select id, name, directory_slug, canonical_origin, canonical_endpoint,
               payment_provider, payment_target_ref, created_from_review
        from services
        where directory_slug = %s
        """,
        (directory_slug,),
    ).fetchone()


def _same_payment_target(
    payment_provider: str | None,
    left: str | None,
    right: str | None,
) -> bool:
    if left is None or right is None:
        return left is right
    if (
        payment_provider in {"mppx", "x402"}
        and len(left) == 42
        and len(right) == 42
        and left[:2].lower() == "0x"
        and right[:2].lower() == "0x"
    ):
        return left.lower() == right.lower()
    return left == right


def _identifier_belongs_to_service(
    conn: psycopg.Connection,
    service_id: str,
    identifier_type: str,
    identifier_value: str,
) -> bool:
    matched = _fetch_by_identifier(conn, identifier_type, identifier_value)
    return matched is not None and matched["id"] == service_id


def _resolved_identity(
    conn: psycopg.Connection,
    supplied: ServiceIdentity,
    service: dict[str, Any],
) -> ServiceIdentity | None:
    """Return a database-authorized identity for an existing service.

    Canonical service fields fill omitted values. A supplied alias may replace
    a canonical field only when that exact identifier is already registered
    to this service. This prevents service_id + attacker payment target from
    verifying a payment to the attacker and storing the review on the victim.
    """
    canonical = build_identity(
        service_id=service["id"],
        service_name=service["name"],
        api_endpoint=service.get("canonical_endpoint"),
        payment_provider=service.get("payment_provider"),
        payment_target_ref=service.get("payment_target_ref"),
        directory_slug=service.get("directory_slug"),
    )

    if supplied.service_id and supplied.service_id != canonical.service_id:
        return None

    endpoint = canonical.api_endpoint
    if supplied.api_endpoint:
        if supplied.api_endpoint == canonical.api_endpoint or _identifier_belongs_to_service(
            conn, service["id"], "api_endpoint", supplied.api_endpoint
        ):
            endpoint = supplied.api_endpoint
        else:
            return None

    directory_slug = canonical.directory_slug
    if supplied.directory_slug:
        if (
            supplied.directory_slug == canonical.directory_slug
            or _identifier_belongs_to_service(
                conn, service["id"], "directory_slug", supplied.directory_slug
            )
        ):
            directory_slug = supplied.directory_slug
        else:
            return None

    provider = canonical.payment_provider
    target = canonical.payment_target_ref
    if supplied.payment_provider and supplied.payment_target_ref:
        supplied_payment_identifier = payment_identifier(
            supplied.payment_provider, supplied.payment_target_ref
        )
        canonical_pair = (
            supplied.payment_provider == canonical.payment_provider
            and _same_payment_target(
                supplied.payment_provider,
                supplied.payment_target_ref,
                canonical.payment_target_ref,
            )
        )
        if canonical_pair or _identifier_belongs_to_service(
            conn,
            service["id"],
            "payment_target",
            supplied_payment_identifier,
        ):
            provider = supplied.payment_provider
            target = supplied.payment_target_ref
        else:
            return None
    elif supplied.payment_provider:
        if supplied.payment_provider != canonical.payment_provider:
            return None
    elif supplied.payment_target_ref:
        if not _same_payment_target(
            canonical.payment_provider,
            supplied.payment_target_ref,
            canonical.payment_target_ref,
        ):
            return None

    return build_identity(
        service_id=canonical.service_id,
        service_name=canonical.service_name,
        api_endpoint=endpoint,
        payment_provider=provider,
        payment_target_ref=target,
        directory_slug=directory_slug,
    )


def resolve_service(
    conn: psycopg.Connection,
    identity: ServiceIdentity,
) -> ResolvedService:
    matches: list[dict[str, Any]] = []

    if identity.service_id:
        service = _fetch_service(conn, identity.service_id)
        if service is not None:
            matches.append(service)

    if identity.directory_slug:
        service = _fetch_by_directory_slug(conn, identity.directory_slug)
        if service is not None:
            matches.append(service)
        service = _fetch_by_identifier(conn, "directory_slug", identity.directory_slug)
        if service is not None:
            matches.append(service)

    if identity.api_endpoint:
        service = _fetch_by_identifier(conn, "api_endpoint", identity.api_endpoint)
        if service is not None:
            matches.append(service)

    if identity.payment_provider and identity.payment_target_ref:
        service = _fetch_by_identifier(
            conn,
            "payment_target",
            payment_identifier(identity.payment_provider, identity.payment_target_ref),
        )
        if service is not None:
            matches.append(service)

    unique = {service["id"]: service for service in matches}
    if len(unique) > 1:
        return ResolvedService(None, error="service identity conflict")
    if unique:
        service = next(iter(unique.values()))
        resolved_identity = _resolved_identity(conn, identity, service)
        if resolved_identity is None:
            return ResolvedService(None, error="service identity conflict")
        return ResolvedService(service, identity=resolved_identity)

    return ResolvedService(None)


def _fallback_service_name(identity: ServiceIdentity) -> str:
    if identity.service_name:
        return identity.service_name
    if identity.api_endpoint:
        parsed = urlsplit(identity.api_endpoint)
        return parsed.netloc
    return "Unregistered service"


def _insert_identifier(
    conn: psycopg.Connection,
    service_id: str,
    identifier_type: str,
    identifier_value: str | None,
) -> None:
    if not identifier_value:
        return
    conn.execute(
        """
        insert into service_identifiers (service_id, identifier_type, identifier_value)
        values (%s, %s, %s)
        on conflict (identifier_type, identifier_value) do nothing
        """,
        (service_id, identifier_type, identifier_value),
    )


def create_service_from_identity(
    conn: psycopg.Connection,
    identity: ServiceIdentity,
) -> ResolvedService:
    if not has_strong_identity(identity):
        return ResolvedService(None, error="service not found")

    assert identity.api_endpoint is not None
    assert identity.payment_provider is not None
    assert identity.payment_target_ref is not None

    service_id = identity.service_id or generate_service_id(
        identity.api_endpoint,
        identity.payment_provider,
        identity.payment_target_ref,
    )
    origin = canonical_origin(identity.api_endpoint)
    service_name = _fallback_service_name(identity)

    try:
        row = conn.execute(
            """
            insert into services (
              id, name, directory_slug, canonical_origin, canonical_endpoint,
              payment_provider, payment_target_ref, created_from_review, metadata
            )
            values (%s, %s, %s, %s, %s, %s, %s, true, '{}'::jsonb)
            on conflict (id) do update
            set
              name = coalesce(services.name, excluded.name),
              directory_slug = coalesce(services.directory_slug, excluded.directory_slug),
              canonical_origin = coalesce(services.canonical_origin, excluded.canonical_origin),
              canonical_endpoint = coalesce(services.canonical_endpoint, excluded.canonical_endpoint),
              payment_provider = coalesce(services.payment_provider, excluded.payment_provider),
              payment_target_ref = coalesce(services.payment_target_ref, excluded.payment_target_ref)
            returning id, name, directory_slug, canonical_origin, canonical_endpoint,
                      payment_provider, payment_target_ref, created_from_review
            """,
            (
                service_id,
                service_name,
                identity.directory_slug,
                origin,
                identity.api_endpoint,
                identity.payment_provider,
                identity.payment_target_ref,
            ),
        ).fetchone()

        _insert_identifier(conn, row["id"], "api_endpoint", identity.api_endpoint)
        _insert_identifier(
            conn,
            row["id"],
            "payment_target",
            payment_identifier(identity.payment_provider, identity.payment_target_ref),
        )
        _insert_identifier(conn, row["id"], "directory_slug", identity.directory_slug)
    except psycopg.errors.UniqueViolation:
        conn.rollback()
        return ResolvedService(None, error="service identity conflict")

    return ResolvedService(row, created=True)
