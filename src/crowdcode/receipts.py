"""Gateway receipt verification (docs/SCORING.md §3.6).

Only reviews backed by a hosted OpenCrowd gateway receipt and a platform-judge
score carry ranking weight. A hosted gateway is the transport for every paid
call its agents make, so it is the one party that observes the request, the
response and the settlement together, and it signs that observation twice:

* a **purchase receipt** at settlement, binding sha256 of the request and the
  response to the payment reference, payee, amount, network and owner;
* a **review attestation** when the agent reviews that purchase, binding the
  receipt to the judge's rating and published text.

Both are flat JSON objects signed with Ed25519 over
``json.dumps(payload, sort_keys=True, separators=(",", ":"))`` — the same
canonical bytes the gateway's TypeScript signer produces. Flatness is load
bearing: nested values do not serialize identically across the two languages.

Trusted public keys come from ``OPENCROWD_RECEIPT_KEYS`` as
``<kid>:<base64url 32-byte key>`` pairs. Several may be configured at once so a
key can be rotated without invalidating receipts already issued under the old
one. Nothing here consults the network or the hosted database: a receipt is
either signed by a configured key or it is not evidence.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

RECEIPT_TYPE = "opencrowd.purchase-receipt.v1"
ATTESTATION_TYPE = "opencrowd.review-attestation.v1"

# A purchase is reviewable for this long. Old receipts stay verifiable (the
# stored review keeps its weight); only new submissions are bounded, so a
# cache of receipts cannot be replayed into a burst of fresh evidence.
RECEIPT_MAX_AGE = timedelta(days=30)
# The attestation is signed at submission time, so it must be fresh. The
# allowance covers clock skew between the gateway and this backend.
ATTESTATION_MAX_AGE = timedelta(minutes=30)
ATTESTATION_MAX_SKEW = timedelta(minutes=5)

KID_RE = re.compile(r"^[A-Za-z0-9._-]{3,64}$")
_OWNER_REF_RE = re.compile(r"^[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def canonical_payload(payload: dict[str, Any]) -> str:
    """The exact bytes the gateway signed."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def payload_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_payload(payload).encode("utf-8")).hexdigest()


def load_receipt_keys(value: str | None = None) -> dict[str, bytes]:
    """Trusted gateway receipt keys, keyed by published key id."""
    raw = os.environ.get("OPENCROWD_RECEIPT_KEYS", "") if value is None else value
    keys: dict[str, bytes] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        kid, _, material = entry.partition(":")
        kid = kid.strip()
        if not KID_RE.match(kid):
            raise ValueError(f"OPENCROWD_RECEIPT_KEYS has an invalid key id: {kid[:32]!r}")
        try:
            # Accept base64url with or without padding.
            decoded = base64.urlsafe_b64decode(material.strip() + "=" * (-len(material.strip()) % 4))
        except Exception as exc:  # noqa: BLE001 - surfaced as a configuration error
            raise ValueError(f"OPENCROWD_RECEIPT_KEYS entry {kid} is not base64url") from exc
        if len(decoded) != 32:
            raise ValueError(f"OPENCROWD_RECEIPT_KEYS entry {kid} is not a 32-byte Ed25519 key")
        keys[kid] = decoded
    return keys


def _verify(payload: dict[str, Any], signature: str, keys: dict[str, bytes]) -> str | None:
    """None when the signature is good, else the reason it is not."""
    kid = payload.get("kid")
    if not isinstance(kid, str) or not KID_RE.match(kid):
        return "attestation names no gateway key"
    key = keys.get(kid)
    if key is None:
        return "attestation key id is not a published OpenCrowd gateway key"
    if not isinstance(signature, str) or not signature:
        return "attestation signature is missing"
    try:
        raw = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    except Exception:  # noqa: BLE001
        return "attestation signature is not base64url"
    try:
        Ed25519PublicKey.from_public_bytes(key).verify(
            raw, canonical_payload(payload).encode("utf-8")
        )
    except (InvalidSignature, ValueError):
        return "attestation signature does not verify"
    return None


def _flat(payload: Any) -> bool:
    """Only flat string/int/None objects sign identically in both languages."""
    return isinstance(payload, dict) and all(
        isinstance(key, str) and (value is None or isinstance(value, (str, int)))
        and not isinstance(value, bool)
        for key, value in payload.items()
    )


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Attestation:
    """A verified hosted purchase review, or the reason it is not one."""

    ok: bool
    reason: str
    kid: str | None = None
    owner_ref: str | None = None
    payee_owner_ref: str | None = None
    endpoint: str | None = None
    payee: str | None = None
    payment_reference: str | None = None
    network: str | None = None
    amount_atomic: int | None = None
    rating: int | None = None
    review_text_sha256: str | None = None
    delivery: str | None = None
    judge_model: str | None = None
    judge_prompt_version: str | None = None
    reviewer_wallet: str | None = None
    receipt_sha256: str | None = None
    attempt_id: str | None = None


def _rejected(reason: str) -> Attestation:
    return Attestation(False, reason)


def verify_attestation(
    raw: Any,
    *,
    keys: dict[str, bytes] | None = None,
    now: datetime | None = None,
) -> Attestation:
    """Verify a ``gateway_attestation`` submitted with a review.

    Everything load bearing is checked here: both signatures, that the
    attestation names the receipt it was actually built from, and that the two
    payloads agree about the payment. Consistency with the review's own fields
    (rating, text, payee, endpoint) is checked by the caller, which knows the
    resolved service identity.
    """
    keys = load_receipt_keys() if keys is None else keys
    if not keys:
        return _rejected("this CrowdCode deployment has no published gateway receipt keys")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return _rejected("gateway_attestation is not JSON")
    if not isinstance(raw, dict):
        return _rejected("gateway_attestation must be an object")

    receipt = raw.get("receipt")
    review = raw.get("review")
    if not _flat(receipt) or not _flat(review):
        return _rejected("gateway_attestation must carry a flat receipt and review payload")
    if receipt.get("type") != RECEIPT_TYPE:
        return _rejected("receipt is not an OpenCrowd purchase receipt")
    if review.get("type") != ATTESTATION_TYPE:
        return _rejected("attestation is not an OpenCrowd review attestation")

    for payload, signature in ((receipt, raw.get("receipt_signature")), (review, raw.get("review_signature"))):
        failure = _verify(payload, signature, keys)
        if failure:
            return _rejected(failure)
    # One gateway key signs both halves: a receipt from one deployment must not
    # be re-attested by another.
    if receipt.get("kid") != review.get("kid"):
        return _rejected("the receipt and the attestation were signed by different gateway keys")
    if review.get("receipt_sha256") != payload_sha256(receipt):
        return _rejected("the attestation does not name the receipt it was submitted with")

    # The attestation restates the payment facts CrowdCode weights on; they
    # must be the receipt's, not a second opinion about them.
    for field in ("attempt_id", "owner_ref", "payee_owner_ref", "payee", "payment_reference", "network", "amount_atomic"):
        if receipt.get(field) != review.get(field):
            return _rejected(f"the attestation and the receipt disagree about {field}")
    if receipt.get("endpoint") != review.get("endpoint"):
        return _rejected("the attestation and the receipt disagree about the endpoint")

    owner_ref = receipt.get("owner_ref")
    if not isinstance(owner_ref, str) or not _OWNER_REF_RE.match(owner_ref):
        return _rejected("the receipt names no owner")
    payee_owner_ref = receipt.get("payee_owner_ref")
    if payee_owner_ref is not None and (
        not isinstance(payee_owner_ref, str) or not _OWNER_REF_RE.match(payee_owner_ref)
    ):
        return _rejected("the receipt names an invalid payee owner")
    text_hash = review.get("review_text_sha256")
    if not isinstance(text_hash, str) or not _SHA256_RE.match(text_hash):
        return _rejected("the attestation names no review text")
    rating = review.get("rating")
    if not isinstance(rating, int) or isinstance(rating, bool) or not 1 <= rating <= 5:
        return _rejected("the attestation carries no judge rating")
    if review.get("delivery") not in {"delivered", "partial", "not_delivered"}:
        return _rejected("the attestation carries no delivery outcome")
    if not isinstance(review.get("judge_prompt_version"), str) or not review["judge_prompt_version"]:
        return _rejected("the attestation names no judge prompt version")

    amount = receipt.get("amount_atomic")
    try:
        amount_atomic = int(str(amount))
    except (TypeError, ValueError):
        return _rejected("the receipt carries no payment amount")
    if amount_atomic <= 0:
        return _rejected("the receipt carries no payment amount")

    moment = now or datetime.now(UTC)
    settled_at = _timestamp(receipt.get("settled_at"))
    issued_at = _timestamp(review.get("issued_at"))
    if settled_at is None or issued_at is None:
        return _rejected("the receipt or attestation has no valid timestamp")
    if settled_at > moment + ATTESTATION_MAX_SKEW:
        return _rejected("the receipt is dated in the future")
    if moment - settled_at > RECEIPT_MAX_AGE:
        return _rejected("the purchase is too old to review for ranking weight")
    if issued_at > moment + ATTESTATION_MAX_SKEW or moment - issued_at > ATTESTATION_MAX_AGE:
        return _rejected("the attestation is stale; resubmit the review")
    if issued_at < settled_at - ATTESTATION_MAX_SKEW:
        return _rejected("the attestation predates the purchase it reviews")

    reviewer = review.get("reviewer_wallet")
    if not isinstance(reviewer, str) or not re.fullmatch(r"0x[0-9a-f]{40}", reviewer):
        return _rejected("the attestation names no reviewer wallet")

    return Attestation(
        True,
        "gateway receipt and judge verdict verified",
        kid=str(receipt["kid"]),
        owner_ref=owner_ref,
        payee_owner_ref=payee_owner_ref,
        endpoint=str(receipt.get("endpoint") or ""),
        payee=str(receipt.get("payee") or ""),
        payment_reference=str(receipt.get("payment_reference") or ""),
        network=str(receipt.get("network") or ""),
        amount_atomic=amount_atomic,
        rating=rating,
        review_text_sha256=text_hash,
        delivery=str(review["delivery"]),
        judge_model=str(review.get("judge_model") or ""),
        judge_prompt_version=str(review["judge_prompt_version"]),
        reviewer_wallet=reviewer,
        receipt_sha256=str(review["receipt_sha256"]),
        attempt_id=str(receipt.get("attempt_id") or ""),
    )


def published_keys(keys: dict[str, bytes] | None = None) -> list[dict[str, str]]:
    """The gateway keys this deployment trusts, for public publication."""
    keys = load_receipt_keys() if keys is None else keys
    return [
        {
            "kid": kid,
            "alg": "ed25519",
            "public_key": base64.urlsafe_b64encode(material).decode("ascii").rstrip("="),
        }
        for kid, material in sorted(keys.items())
    ]


@dataclass(frozen=True)
class AttestationOutcome:
    """What a submitted gateway_attestation entitles a review to.

    `ok` False means the attestation itself did not verify: the review is
    rejected outright, exactly as a bad payment claim is, because accepting it
    silently as community evidence would hide a forgery. `ok` True with a
    `weight_exclusion` means the attestation verified but the review is not
    allowed to move the ranking — it is stored, published and labelled.
    """

    ok: bool
    reason: str
    attestation: Attestation | None = None
    weight_exclusion: str | None = None


def evaluate_attestation(
    raw: Any,
    *,
    api_endpoint: str | None,
    payment_target_ref: str | None,
    payment_reference: str | None,
    rating: int,
    reason: str,
    reviewer_wallet: str | None,
    min_amount_atomic: int,
    keys: dict[str, bytes] | None = None,
    now: datetime | None = None,
) -> AttestationOutcome:
    """Bind a verified gateway attestation to the review it was submitted with.

    The attestation only proves what the gateway observed. This is where that
    observation has to agree with the review actually being stored: the same
    service, the same payee, the same settlement, the judge's rating and the
    judge's text byte for byte. The text is hashed before redaction, like the
    reviewer's EIP-191 signature, so the hash covers what was submitted rather
    than what storage later rewrote.

    The weighting rules themselves live here too: a review of a service the
    reviewer's own owner is paid by, and a review citing a dust payment, both
    verify and both carry zero weight.
    """
    # A receipt describes a settled purchase. An unpaid review has none to
    # cite, so citing one is a mismatch, not a free upgrade.
    if payment_reference is None:
        return AttestationOutcome(False, "an unpaid review cannot carry a gateway purchase receipt")
    verified = verify_attestation(raw, keys=keys, now=now)
    if not verified.ok:
        return AttestationOutcome(False, verified.reason)
    if api_endpoint is None or verified.endpoint != api_endpoint:
        return AttestationOutcome(False, "the gateway receipt is for a different service endpoint")
    if payment_target_ref is None or verified.payee.lower() != payment_target_ref.strip().lower():
        return AttestationOutcome(False, "the gateway receipt is for a different payee")
    if canonical_payment_reference_text(verified.payment_reference) != canonical_payment_reference_text(
        payment_reference
    ):
        return AttestationOutcome(False, "the gateway receipt is for a different payment")
    if verified.rating != rating:
        return AttestationOutcome(False, "the review rating is not the judge's rating")
    if hashlib.sha256(reason.strip().encode("utf-8")).hexdigest() != verified.review_text_sha256:
        return AttestationOutcome(False, "the review text is not the judge's text")
    # The attestation names the wallet the gateway was about to sign with. A
    # different wallet means somebody is submitting an attestation that was
    # not issued for them.
    if reviewer_wallet is None or verified.reviewer_wallet != reviewer_wallet.strip().lower():
        return AttestationOutcome(False, "the gateway receipt was issued for a different reviewer wallet")

    # Hosted services know both sides of a sale, so an owner reviewing a
    # service its own agent is paid by is not independent evidence.
    if verified.payee_owner_ref is not None and verified.payee_owner_ref == verified.owner_ref:
        return AttestationOutcome(True, "verified; the reviewer's owner owns the payee", verified, "self_dealing")
    if (verified.amount_atomic or 0) < min_amount_atomic:
        return AttestationOutcome(True, "verified; the payment is below the weighting minimum", verified, "dust_payment")
    return AttestationOutcome(True, verified.reason, verified, None)


def canonical_payment_reference_text(reference: str) -> str:
    """Local copy of the dedup canonicalization, without importing payments
    (which imports this module's caller). Strips a provider prefix and
    lowercases EVM hashes; Solana base58 signatures stay case-sensitive."""
    cleaned = re.sub(r"^(?:x402:[a-z0-9]+:|mppx:tempo:)", "", reference.strip(), flags=re.IGNORECASE)
    return cleaned.lower() if re.fullmatch(r"0x[0-9a-fA-F]{64}", cleaned) else cleaned
