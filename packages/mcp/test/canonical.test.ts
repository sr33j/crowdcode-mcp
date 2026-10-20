import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import {
  generateServiceId,
  normalizeApiEndpoint,
  normalizePaymentProvider,
} from "../src/canonical/identity.js";
import { canonicalReviewPayload, reasonHash } from "../src/canonical/payload.js";
import { base58Decode, base58Encode, payerLinkMessage } from "../src/canonical/solana.js";
import { createPrivateKey, sign } from "node:crypto";

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
  payment_reference: string | null;
  review_nonce?: string;
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
  payer_link: PayerLinkVector[];
};

interface PayerLinkVector {
  payload: string;
  reviewer_wallet: string;
  payer_seed_base58: string;
  payer: string;
  expected_message: string;
  expected_signature: string;
}

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
        reviewNonce: vector.review_nonce,
      });
      expect(message).toBe(vector.expected_message);
      expect(reasonHash(vector.reason)).toBe(vector.expected_reason_hash);
    });
  }
});

describe("payer link (Solana payer_signature)", () => {
  it.each(vectors.payer_link.map((v) => [v.reviewer_wallet, v] as const))(
    "matches the Python reference for %s",
    (_name, vector) => {
      const message = payerLinkMessage(vector.reviewer_wallet, vector.payload);
      expect(message).toBe(vector.expected_message);
      const seed = base58Decode(vector.payer_seed_base58)!;
      const key = createPrivateKey({
        key: Buffer.concat([Buffer.from("302e020100300506032b657004220420", "hex"), seed]),
        format: "der",
        type: "pkcs8",
      });
      // Ed25519 is deterministic: the signature is a vector too.
      expect(base58Encode(sign(null, Buffer.from(message, "utf8"), key))).toBe(vector.expected_signature);
    },
  );
});
