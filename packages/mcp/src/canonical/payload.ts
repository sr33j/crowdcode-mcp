/**
 * Local construction of the crowdcode.review.v1 signing payload.
 * Byte-for-byte port of src/crowdcode/payments.py:canonical_review_payload;
 * conformance enforced against spec/review-payload-vectors.json.
 */

import { createHash } from "node:crypto";
import type { ServiceIdentity } from "./identity.js";
import { pythonCanonicalJson } from "./json.js";
import { pyStrip } from "./pystrip.js";

export const PAYLOAD_TYPE = "crowdcode.review.v1";

export function reasonHash(reason: string): string {
  return (
    "sha256:" +
    createHash("sha256").update(pyStrip(reason), "utf8").digest("hex")
  );
}

export function canonicalReviewPayload(args: {
  identity: Pick<
    ServiceIdentity,
    | "service_id"
    | "api_endpoint"
    | "payment_provider"
    | "payment_target_ref"
    | "directory_slug"
  >;
  rating: number;
  reason: string;
  paymentReference: string;
}): string {
  const { identity, rating, reason, paymentReference } = args;
  if (!Number.isInteger(rating)) {
    throw new Error("rating must be an integer");
  }
  return pythonCanonicalJson({
    type: PAYLOAD_TYPE,
    service_id: identity.service_id,
    api_endpoint: identity.api_endpoint,
    payment_provider: identity.payment_provider,
    payment_target_ref: identity.payment_target_ref,
    directory_slug: identity.directory_slug,
    payment_reference: pyStrip(paymentReference),
    rating,
    reason_hash: reasonHash(reason),
  });
}
