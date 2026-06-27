from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from crowdcode.payments import verify_payment_reference


BASE_ENV = {
    "DATABASE_URL": "postgresql://postgres:postgres@localhost:5432/postgres",
    "CROWDCODE_REVIEWER_SALT": "test-salt",
}


class PaymentVerificationTests(unittest.TestCase):
    def test_placeholder_accepts_non_empty_reference(self) -> None:
        with patch.dict(
            os.environ,
            {**BASE_ENV, "CROWDCODE_PAYMENT_VERIFICATION_MODE": "placeholder"},
            clear=True,
        ):
            result = verify_payment_reference("svc_code_review", "demo_payment_001")

        self.assertTrue(result.ok)
        self.assertEqual(result.protocol, "unknown")
        self.assertEqual(result.rail, "placeholder")
        self.assertEqual(result.reference_type, "opaque")
        self.assertIsNotNone(result.reviewer_id)

    def test_structured_evidence_can_supply_reference_and_protocol(self) -> None:
        with patch.dict(
            os.environ,
            {**BASE_ENV, "CROWDCODE_PAYMENT_VERIFICATION_MODE": "placeholder"},
            clear=True,
        ):
            result = verify_payment_reference(
                "svc_code_review",
                "",
                payment_evidence={
                    "protocol": "mpp",
                    "reference": "receipt_123",
                    "reference_type": "receipt",
                },
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.protocol, "mpp")
        self.assertEqual(result.rail, "placeholder")
        self.assertEqual(result.reference, "receipt_123")
        self.assertEqual(result.reference_type, "receipt")

    def test_empty_reference_is_rejected(self) -> None:
        with patch.dict(os.environ, BASE_ENV, clear=True):
            result = verify_payment_reference("svc_code_review", " ")

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "payment_reference is required")

    def test_stripe_x402_requires_payment_intent_reference(self) -> None:
        with patch.dict(
            os.environ,
            {
                **BASE_ENV,
                "CROWDCODE_PAYMENT_VERIFICATION_MODE": "stripe_x402",
                "STRIPE_SECRET_KEY": "sk_test_123",
            },
            clear=True,
        ):
            result = verify_payment_reference("svc_code_review", "not_a_pi")

        self.assertFalse(result.ok)
        self.assertIn("PaymentIntent", result.reason)

    def test_stripe_x402_accepts_succeeded_crypto_payment_bound_to_service(self) -> None:
        payment_intent = {
            "id": "pi_123",
            "status": "succeeded",
            "amount_received": 100,
            "currency": "usd",
            "payment_method_types": ["crypto"],
            "metadata": {"crowdcode_service_id": "svc_code_review"},
        }

        with patch.dict(
            os.environ,
            {
                **BASE_ENV,
                "CROWDCODE_PAYMENT_VERIFICATION_MODE": "stripe_x402",
                "STRIPE_SECRET_KEY": "sk_test_123",
            },
            clear=True,
        ), patch(
            "crowdcode.payments._retrieve_stripe_payment_intent",
            return_value=payment_intent,
        ):
            result = verify_payment_reference("svc_code_review", "pi_123")

        self.assertTrue(result.ok)
        self.assertEqual(result.protocol, "x402")
        self.assertEqual(result.rail, "stripe_crypto_payment_intent")
        self.assertEqual(result.payment_amount, 100)
        self.assertEqual(result.payment_currency, "usd")

    def test_stripe_x402_rejects_service_mismatch(self) -> None:
        payment_intent = {
            "id": "pi_123",
            "status": "succeeded",
            "amount_received": 100,
            "currency": "usd",
            "payment_method_types": ["crypto"],
            "metadata": {"crowdcode_service_id": "svc_other"},
        }

        with patch.dict(
            os.environ,
            {
                **BASE_ENV,
                "CROWDCODE_PAYMENT_VERIFICATION_MODE": "stripe_x402",
                "STRIPE_SECRET_KEY": "sk_test_123",
            },
            clear=True,
        ), patch(
            "crowdcode.payments._retrieve_stripe_payment_intent",
            return_value=payment_intent,
        ):
            result = verify_payment_reference("svc_code_review", "pi_123")

        self.assertFalse(result.ok)
        self.assertIn("metadata does not match", result.reason)

    def test_stripe_x402_rejects_unbound_payment_intent(self) -> None:
        payment_intent = {
            "id": "pi_123",
            "status": "succeeded",
            "amount_received": 100,
            "currency": "usd",
            "payment_method_types": ["crypto"],
            "metadata": {},
        }

        with patch.dict(
            os.environ,
            {
                **BASE_ENV,
                "CROWDCODE_PAYMENT_VERIFICATION_MODE": "stripe_x402",
                "STRIPE_SECRET_KEY": "sk_test_123",
            },
            clear=True,
        ), patch(
            "crowdcode.payments._retrieve_stripe_payment_intent",
            return_value=payment_intent,
        ):
            result = verify_payment_reference("svc_code_review", "pi_123")

        self.assertFalse(result.ok)
        self.assertIn("not bound", result.reason)


if __name__ == "__main__":
    unittest.main()
