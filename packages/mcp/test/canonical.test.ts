import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import {
  generateServiceId,
  normalizeApiEndpoint,
  normalizePaymentProvider,
} from "../src/canonical/identity.js";
import { canonicalReviewPayload, reasonHash } from "../src/canonical/payload.js";

interface NormalizationVector {
  input: string | null;
  expected?: string | null;
  error?: string;
}

interface ServiceIdVector {
  api_endpoint: string;
  payment_provider: string;
  payment_target_ref: string;
  expected: string;
}

interface PayloadVector {
  name: string;
  identity: {
    service_id: string | null;
    api_endpoint: string | null;
    payment_provider: string | null;
    payment_target_ref: string | null;
    directory_slug: string | null;
  };
  rating: number;
  reason: string;
  payment_reference: string;
  expected_reason_hash: string;
  expected_message: string;
}

const vectors = JSON.parse(
  readFileSync(
    new URL("../../../spec/review-payload-vectors.json", import.meta.url),
    "utf8",
  ),
) as {
  endpoint_normalization: NormalizationVector[];
  provider_normalization: NormalizationVector[];
  service_id: ServiceIdVector[];
  review_payload: PayloadVector[];
};

function checkNormalization(
  fn: (value: string | null) => string | null,
  vector: NormalizationVector,
) {
  if (vector.error !== undefined) {
    let message: string | undefined;
    try {
      fn(vector.input);
    } catch (err) {
      message = (err as Error).message;
    }
    expect(message).toBe(vector.error);
  } else {
    expect(fn(vector.input)).toBe(vector.expected);
  }
}

describe("normalizeApiEndpoint", () => {
  for (const vector of vectors.endpoint_normalization) {
    it(JSON.stringify(vector.input), () => {
      checkNormalization(normalizeApiEndpoint, vector);
    });
  }
});

describe("normalizePaymentProvider", () => {
  for (const vector of vectors.provider_normalization) {
    it(JSON.stringify(vector.input), () => {
      checkNormalization(normalizePaymentProvider, vector);
    });
  }
});

describe("generateServiceId", () => {
  for (const vector of vectors.service_id) {
    it(vector.api_endpoint, () => {
      expect(
        generateServiceId(
          vector.api_endpoint,
          vector.payment_provider,
          vector.payment_target_ref,
        ),
      ).toBe(vector.expected);
    });
  }
});

describe("canonicalReviewPayload", () => {
  for (const vector of vectors.review_payload) {
    it(vector.name, () => {
      const message = canonicalReviewPayload({
        identity: vector.identity,
        rating: vector.rating,
        reason: vector.reason,
        paymentReference: vector.payment_reference,
      });
      expect(message).toBe(vector.expected_message);
      expect(reasonHash(vector.reason)).toBe(vector.expected_reason_hash);
    });
  }
});
