/**
 * Static mirror of the backend tool signatures (src/crowdcode/server.py).
 * A static mirror — not dynamic passthrough — because get_review_signing_payload
 * is overridden locally and the redaction policy is keyed to known parameter
 * names; upstream drift should fail loudly, not silently forward unredacted
 * fields. Names/optionality must match the Python signatures exactly.
 */

import { z } from "zod";

export const identityShape = {
  service_id: z.string().nullish().describe("Canonical service id (svc_...)"),
  api_endpoint: z.string().nullish().describe("Service API endpoint URL"),
  payment_provider: z
    .string()
    .nullish()
    .describe("One of: stripe, stripe_payment_link, mppx, x402, manual"),
  payment_target_ref: z
    .string()
    .nullish()
    .describe("Payment recipient reference (wallet address, account id, ...)"),
  directory_slug: z.string().nullish().describe("Directory slug if known"),
};

export const requestServiceShape = {
  service_description: z
    .string()
    .describe(
      "Specific, reusable service capability with clear inputs and outputs. " +
        "Do not include secrets, credentials, or private user data — free text " +
        "is additionally redacted locally before it leaves this machine.",
    ),
  task_context: z
    .string()
    .nullish()
    .describe("Optional context about the task that needed this service"),
};

export const getServiceScoreShape = { ...identityShape };

export const signingPayloadShape = {
  rating: z.number().int().describe("Rating 1-5"),
  reason: z.string().describe("Review text (redacted locally before hashing)"),
  payment_reference: z.string().describe("Payment reference for this review"),
  ...identityShape,
};

export const reviewServiceShape = {
  rating: z.number().int().describe("Rating 1-5"),
  reason: z.string().describe("Review text (redacted locally before sending)"),
  payment_reference: z.string().describe("Unique payment reference"),
  service_id: identityShape.service_id,
  task_context: z.string().nullish(),
  service_name: z.string().nullish(),
  api_endpoint: identityShape.api_endpoint,
  payment_provider: identityShape.payment_provider,
  payment_target_ref: identityShape.payment_target_ref,
  directory_slug: identityShape.directory_slug,
  payment_proof: z.string().nullish(),
  payment_challenge: z.string().nullish(),
  reviewer_wallet: z.string().nullish(),
  review_signature: z.string().nullish(),
  signature_scheme: z.string().default("eip191"),
};

export const MIRRORED_REMOTE_TOOLS = [
  "request_service",
  "get_service_score",
  "review_service",
] as const;
