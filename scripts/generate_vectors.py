"""Generate spec/review-payload-vectors.json from the Python reference implementation.

Python is the source of truth for the canonical review payload and identity
normalization. This script computes every expected value by calling the real
functions in crowdcode.identity / crowdcode.payments, so the vector file is
never hand-typed. The TypeScript package's test suite consumes the same file.

Usage: python scripts/generate_vectors.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from crowdcode.identity import (  # noqa: E402
    ServiceIdentity,
    generate_service_id,
    normalize_api_endpoint,
    normalize_payment_provider,
)
from crowdcode.payments import canonical_review_payload  # noqa: E402


ENDPOINT_INPUTS = [
    "Example.COM/api/",
    "HTTPS://Example.COM:443/api//",
    "http://localhost:8000",
    "https://a.b/x?q=1#f",
    "api.example.com:8080/v1/",
    "https://user:pass@Host.COM/x",
    "example.com",
    "https://example.com//",
    "HTTP://EXAMPLE.com/Path/To/Api",
    "   ",
    "",
    None,
    "https:///nohost",
]

PROVIDER_INPUTS = [
    "Stripe-Link",
    "stripe_link",
    "payment_link",
    "MPP",
    "mppx",
    "  x402  ",
    "Stripe",
    "MANUAL",
    "venmo",
    "   ",
    None,
]

SERVICE_ID_INPUTS = [
    {
        "api_endpoint": "api.example.com/v1",
        "payment_provider": "x402",
        "payment_target_ref": " 0xAbCdEf0123456789aBcDeF0123456789AbCdEf01 ",
    },
    {
        "api_endpoint": "https://OCR.example.com:443/scan/",
        "payment_provider": "mppx",
        "payment_target_ref": "0x1111111111111111111111111111111111111111",
    },
    {
        "api_endpoint": "https://svc.example.com/",
        "payment_provider": "stripe",
        "payment_target_ref": "acct_123",
    },
]

REVIEW_PAYLOAD_INPUTS = [
    {
        "name": "full identity, ascii",
        "identity": {
            "service_id": "svc_0123456789abcdef0123",
            "api_endpoint": "https://api.example.com/v1",
            "payment_provider": "x402",
            "payment_target_ref": "0xabcdef0123456789abcdef0123456789abcdef01",
            "directory_slug": "example-ocr",
        },
        "rating": 5,
        "reason": "Fast and accurate OCR, receipts parsed correctly.",
        "payment_reference": "0x" + "ab" * 32,
    },
    {
        "name": "non-ascii and emoji in reason, whitespace padding",
        "identity": {
            "service_id": "svc_0123456789abcdef0123",
            "api_endpoint": "https://api.example.com/v1",
            "payment_provider": "mppx",
            "payment_target_ref": "0xabcdef0123456789abcdef0123456789abcdef01",
            "directory_slug": None,
        },
        "rating": 4,
        "reason": "  Great service — très rapide \U0001f680  ",
        "payment_reference": "  0x" + "cd" * 32 + "  ",
    },
    {
        "name": "non-ascii in identity fields (escaped in message itself)",
        "identity": {
            "service_id": None,
            "api_endpoint": "https://café.example.com/",
            "payment_provider": "manual",
            "payment_target_ref": "café-ref-über",
            "directory_slug": "café-ocr",
        },
        "rating": 3,
        "reason": "ok",
        "payment_reference": "ref-123",
    },
    {
        "name": "all-null identity fields",
        "identity": {
            "service_id": None,
            "api_endpoint": None,
            "payment_provider": None,
            "payment_target_ref": None,
            "directory_slug": None,
        },
        "rating": 1,
        "reason": "no identity provided",
        "payment_reference": "ref-null-identity",
    },
    {
        "name": "python str.strip control chars (x1c-x1f are python-space, not js-space)",
        "identity": {
            "service_id": "svc_ffffffffffffffffffff",
            "api_endpoint": "https://api.example.com/v1",
            "payment_provider": "x402",
            "payment_target_ref": "0xabcdef0123456789abcdef0123456789abcdef01",
            "directory_slug": None,
        },
        "rating": 2,
        "reason": "\x1c\x1d review with odd padding \x1e\x1f",
        "payment_reference": "\x1cref-ctrl\x1f",
    },
    {
        "name": "nbsp and line separator padding",
        "identity": {
            "service_id": "svc_ffffffffffffffffffff",
            "api_endpoint": "https://api.example.com/v1",
            "payment_provider": "x402",
            "payment_target_ref": "0xabcdef0123456789abcdef0123456789abcdef01",
            "directory_slug": None,
        },
        "rating": 5,
        "reason": "\u00a0\u2028 padded reason \u2028\u00a0",
        "payment_reference": "ref-unicode-ws",
    },
    {
        "name": "nfc reason (U+00E9 precomposed; no unicode normalization applied)",
        "identity": {
            "service_id": "svc_1234567890abcdef1234",
            "api_endpoint": None,
            "payment_provider": None,
            "payment_target_ref": None,
            "directory_slug": None,
        },
        "rating": 4,
        "reason": "caf\u00e9",
        "payment_reference": "ref-nfc",
    },
    {
        "name": "nfd reason (U+0065 U+0301; must hash differently from nfc)",
        "identity": {
            "service_id": "svc_1234567890abcdef1234",
            "api_endpoint": None,
            "payment_provider": None,
            "payment_target_ref": None,
            "directory_slug": None,
        },
        "rating": 4,
        "reason": "cafe\u0301",
        "payment_reference": "ref-nfd",
    },
    {
        "name": "redacted placeholder text (typical real input)",
        "identity": {
            "service_id": "svc_0123456789abcdef0123",
            "api_endpoint": "https://api.example.com/v1",
            "payment_provider": "x402",
            "payment_target_ref": "0xabcdef0123456789abcdef0123456789abcdef01",
            "directory_slug": "example-ocr",
        },
        "rating": 5,
        "reason": "Processed [EMAIL_1]'s invoices for [GIVEN_NAME_1], key [API_KEY_1] worked.",
        "payment_reference": "0x" + "ef" * 32,
    },
]


def endpoint_vectors() -> list[dict]:
    vectors = []
    for value in ENDPOINT_INPUTS:
        entry: dict = {"input": value}
        try:
            entry["expected"] = normalize_api_endpoint(value)
        except ValueError as exc:
            entry["error"] = str(exc)
        vectors.append(entry)
    return vectors


def provider_vectors() -> list[dict]:
    vectors = []
    for value in PROVIDER_INPUTS:
        entry: dict = {"input": value}
        try:
            entry["expected"] = normalize_payment_provider(value)
        except ValueError as exc:
            entry["error"] = str(exc)
        vectors.append(entry)
    return vectors


def service_id_vectors() -> list[dict]:
    vectors = []
    for case in SERVICE_ID_INPUTS:
        vectors.append(
            {
                **case,
                "expected": generate_service_id(
                    case["api_endpoint"],
                    case["payment_provider"],
                    case["payment_target_ref"],
                ),
            }
        )
    return vectors


def review_payload_vectors() -> list[dict]:
    vectors = []
    for case in REVIEW_PAYLOAD_INPUTS:
        identity = ServiceIdentity(**case["identity"])
        message = canonical_review_payload(
            identity=identity,
            rating=case["rating"],
            reason=case["reason"],
            payment_reference=case["payment_reference"],
        )
        reason_hash = json.loads(message)["reason_hash"]
        vectors.append(
            {
                "name": case["name"],
                "identity": case["identity"],
                "rating": case["rating"],
                "reason": case["reason"],
                "payment_reference": case["payment_reference"],
                "expected_reason_hash": reason_hash,
                "expected_message": message,
            }
        )
    return vectors


def main() -> None:
    out = {
        "version": 1,
        "generated_by": "scripts/generate_vectors.py",
        "endpoint_normalization": endpoint_vectors(),
        "provider_normalization": provider_vectors(),
        "service_id": service_id_vectors(),
        "review_payload": review_payload_vectors(),
    }
    target = REPO_ROOT / "spec" / "review-payload-vectors.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(out, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    print(f"wrote {target} ({len(out['review_payload'])} payload vectors)")


if __name__ == "__main__":
    main()
