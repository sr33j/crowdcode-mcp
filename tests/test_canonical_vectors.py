"""Conformance tests: Python reference vs spec/review-payload-vectors.json.

The vector file is generated from this same Python implementation
(scripts/generate_vectors.py), so these tests mostly guard against
regressions in identity/payments changing the canonical output without a
regenerated spec. The TypeScript suite runs the identical vectors.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crowdcode.identity import (
    ServiceIdentity,
    generate_service_id,
    normalize_api_endpoint,
    normalize_payment_provider,
)
from crowdcode.payments import canonical_review_payload

VECTORS = json.loads(
    (Path(__file__).resolve().parent.parent / "spec" / "review-payload-vectors.json")
    .read_text(encoding="utf-8")
)


@pytest.mark.parametrize(
    "vector",
    VECTORS["endpoint_normalization"],
    ids=lambda v: repr(v["input"])[:40],
)
def test_endpoint_normalization(vector):
    if "error" in vector:
        with pytest.raises(ValueError, match=None) as excinfo:
            normalize_api_endpoint(vector["input"])
        assert str(excinfo.value) == vector["error"]
    else:
        assert normalize_api_endpoint(vector["input"]) == vector["expected"]


@pytest.mark.parametrize(
    "vector",
    VECTORS["provider_normalization"],
    ids=lambda v: repr(v["input"])[:40],
)
def test_provider_normalization(vector):
    if "error" in vector:
        with pytest.raises(ValueError) as excinfo:
            normalize_payment_provider(vector["input"])
        assert str(excinfo.value) == vector["error"]
    else:
        assert normalize_payment_provider(vector["input"]) == vector["expected"]


@pytest.mark.parametrize(
    "vector",
    VECTORS["service_id"],
    ids=lambda v: v["api_endpoint"],
)
def test_service_id(vector):
    assert (
        generate_service_id(
            vector["api_endpoint"],
            vector["payment_provider"],
            vector["payment_target_ref"],
        )
        == vector["expected"]
    )


@pytest.mark.parametrize(
    "vector",
    VECTORS["review_payload"],
    ids=lambda v: v["name"],
)
def test_review_payload(vector):
    message = canonical_review_payload(
        identity=ServiceIdentity(**vector["identity"]),
        rating=vector["rating"],
        reason=vector["reason"],
        payment_reference=vector["payment_reference"],
    )
    assert message == vector["expected_message"]
    assert json.loads(message)["reason_hash"] == vector["expected_reason_hash"]
