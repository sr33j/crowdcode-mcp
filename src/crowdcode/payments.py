from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from crowdcode.settings import get_settings


@dataclass(frozen=True)
class PaymentVerification:
    ok: bool
    reason: str
    protocol: str = "unknown"
    rail: str | None = None
    reference: str | None = None
    reference_type: str | None = None
    payment_status: str | None = None
    payment_amount: int | None = None
    payment_currency: str | None = None
    reviewer_id: str | None = None
    metadata: dict[str, Any] | None = None


def verify_payment_reference(
    service_id: str,
    payment_reference: str,
    service_payment_ref: str | None = None,
    payment_protocol: str = "auto",
    payment_evidence: dict[str, Any] | None = None,
) -> PaymentVerification:
    """Verify the payment evidence behind a review.

    The public MCP API intentionally stays small: agents pass a single
    payment_reference to review_service. Internally, this function can verify
    different machine-payment protocols as we learn their durable receipt shape.
    """
    evidence = payment_evidence or {}
    reference = _evidence_reference(payment_reference, evidence)
    if not reference:
        return PaymentVerification(ok=False, reason="payment_reference is required")

    requested_protocol = _requested_protocol(payment_protocol, evidence)
    settings = get_settings()
    mode = settings.payment_verification_mode

    if mode == "placeholder":
        return _placeholder_verification(reference, requested_protocol, evidence)

    if mode in {"stripe_x402", "stripe_machine_payment"}:
        return verify_stripe_x402_payment_intent(
            service_id=service_id,
            payment_reference=reference,
            service_payment_ref=service_payment_ref,
            payment_evidence=evidence,
        )

    return PaymentVerification(
        ok=False,
        reason=f"unsupported payment verification mode: {mode}",
        reference=reference,
    )


def _placeholder_verification(
    payment_reference: str,
    requested_protocol: str,
    payment_evidence: dict[str, Any],
) -> PaymentVerification:
    return PaymentVerification(
        ok=True,
        reason="verified by v1 placeholder",
        protocol=requested_protocol if requested_protocol != "auto" else "unknown",
        rail="placeholder",
        reference=payment_reference,
        reference_type=_evidence_string(payment_evidence, "reference_type") or "opaque",
        reviewer_id=reviewer_id_from_payment(payment_reference),
        metadata={"payment_evidence": payment_evidence} if payment_evidence else None,
    )


def verify_stripe_x402_payment_intent(
    service_id: str,
    payment_reference: str,
    service_payment_ref: str | None = None,
    payment_evidence: dict[str, Any] | None = None,
) -> PaymentVerification:
    """Verify a Stripe-backed x402 payment by retrieving its PaymentIntent.

    Stripe's x402 machine-payment sample creates crypto deposit PaymentIntents.
    For CrowdCode v1, the payment_reference is the durable `pi_...` id returned
    by the paid service or demo harness after the machine payment completes.
    """
    if not payment_reference.startswith("pi_"):
        return PaymentVerification(
            ok=False,
            reason="stripe_x402 payment_reference must be a PaymentIntent id",
            protocol="x402",
            rail="stripe_crypto_payment_intent",
            reference=payment_reference,
            reference_type="payment_intent",
        )

    settings = get_settings()
    if not settings.stripe_secret_key:
        return PaymentVerification(
            ok=False,
            reason="STRIPE_SECRET_KEY is required for stripe_x402 verification",
            protocol="x402",
            rail="stripe_crypto_payment_intent",
            reference=payment_reference,
            reference_type="payment_intent",
        )

    try:
        payment_intent = _retrieve_stripe_payment_intent(
            payment_reference,
            settings.stripe_secret_key,
            settings.stripe_api_version,
        )
    except Exception as exc:
        return PaymentVerification(
            ok=False,
            reason=f"could not retrieve Stripe PaymentIntent: {exc}",
            protocol="x402",
            rail="stripe_crypto_payment_intent",
            reference=payment_reference,
            reference_type="payment_intent",
        )

    status = _get(payment_intent, "status")
    if status != "succeeded":
        return PaymentVerification(
            ok=False,
            reason=f"Stripe PaymentIntent is not succeeded: {status}",
            protocol="x402",
            rail="stripe_crypto_payment_intent",
            reference=payment_reference,
            reference_type="payment_intent",
            payment_status=status,
        )

    metadata = _as_dict(_get(payment_intent, "metadata") or {})
    metadata_service_id = (
        metadata.get("crowdcode_service_id")
        or metadata.get("service_id")
        or metadata.get("crowdcode.service_id")
    )
    if metadata_service_id and metadata_service_id != service_id:
        return PaymentVerification(
            ok=False,
            reason="Stripe PaymentIntent service metadata does not match service_id",
            protocol="x402",
            rail="stripe_crypto_payment_intent",
            reference=payment_reference,
            reference_type="payment_intent",
            payment_status=status,
            metadata={"stripe_metadata": metadata},
        )

    stripe_payment_ref = (
        metadata.get("crowdcode_payment_ref")
        or metadata.get("service_payment_ref")
        or metadata.get("stripe_payee_ref")
    )
    if service_payment_ref and stripe_payment_ref and stripe_payment_ref != service_payment_ref:
        return PaymentVerification(
            ok=False,
            reason="Stripe PaymentIntent payee metadata does not match service",
            protocol="x402",
            rail="stripe_crypto_payment_intent",
            reference=payment_reference,
            reference_type="payment_intent",
            payment_status=status,
            metadata={"stripe_metadata": metadata},
        )

    service_bound = metadata_service_id == service_id
    payment_ref_bound = bool(
        service_payment_ref
        and stripe_payment_ref
        and stripe_payment_ref == service_payment_ref
    )
    if not service_bound and not payment_ref_bound:
        return PaymentVerification(
            ok=False,
            reason="Stripe PaymentIntent is not bound to the reviewed service",
            protocol="x402",
            rail="stripe_crypto_payment_intent",
            reference=payment_reference,
            reference_type="payment_intent",
            payment_status=status,
            metadata={"stripe_metadata": metadata},
        )

    payment_method_types = list(_get(payment_intent, "payment_method_types") or [])
    if payment_method_types and "crypto" not in payment_method_types:
        return PaymentVerification(
            ok=False,
            reason="Stripe PaymentIntent is not a crypto machine payment",
            protocol="x402",
            rail="stripe_crypto_payment_intent",
            reference=payment_reference,
            reference_type="payment_intent",
            payment_status=status,
            metadata={"payment_method_types": payment_method_types},
        )

    amount = _get(payment_intent, "amount_received")
    if amount is None:
        amount = _get(payment_intent, "amount")

    return PaymentVerification(
        ok=True,
        reason="Stripe x402 PaymentIntent verified",
        protocol="x402",
        rail="stripe_crypto_payment_intent",
        reference=payment_reference,
        reference_type="payment_intent",
        payment_status=status,
        payment_amount=amount,
        payment_currency=_get(payment_intent, "currency"),
        reviewer_id=reviewer_id_from_payment(payment_reference),
        metadata={
            "stripe_payment_intent": _get(payment_intent, "id"),
            "stripe_metadata": metadata,
            "payment_method_types": payment_method_types,
            "payment_evidence": payment_evidence or {},
        },
    )


def _retrieve_stripe_payment_intent(
    payment_intent_id: str,
    stripe_secret_key: str,
    stripe_api_version: str,
) -> Any:
    import stripe

    stripe.api_key = stripe_secret_key
    stripe.api_version = stripe_api_version
    return stripe.PaymentIntent.retrieve(payment_intent_id)


def _get(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        return dict(value)
    except (TypeError, ValueError):
        return {}


def _evidence_reference(
    payment_reference: str | None,
    payment_evidence: dict[str, Any],
) -> str:
    if payment_reference and payment_reference.strip():
        return payment_reference.strip()

    for key in ("reference", "payment_reference", "payment_intent", "receipt"):
        value = _evidence_string(payment_evidence, key)
        if value:
            return value

    return ""


def _requested_protocol(
    payment_protocol: str | None,
    payment_evidence: dict[str, Any],
) -> str:
    evidence_protocol = _evidence_string(payment_evidence, "protocol")
    if evidence_protocol:
        return evidence_protocol
    if payment_protocol and payment_protocol.strip():
        return payment_protocol.strip()
    return "auto"


def _evidence_string(payment_evidence: dict[str, Any], key: str) -> str | None:
    value = payment_evidence.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def reviewer_id_from_payment(payment_reference: str) -> str:
    settings = get_settings()
    material = f"{settings.reviewer_salt}:{payment_reference.strip()}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()
