from __future__ import annotations

import hashlib

from crowdcode.settings import get_settings


def verify_payment_reference(payment_reference: str) -> tuple[bool, str]:
    """V1 placeholder gate.

    Real Stripe verification will replace this function. For the PoC, a review
    is eligible when the reference is non-empty and has not already been used.
    Uniqueness is enforced by the database.
    """
    if not payment_reference or not payment_reference.strip():
        return False, "payment_reference is required"

    return True, "verified by v1 placeholder"


def reviewer_id_from_payment(payment_reference: str) -> str:
    settings = get_settings()
    material = f"{settings.reviewer_salt}:{payment_reference.strip()}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()

