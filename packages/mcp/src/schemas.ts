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

const RATING_DESCRIPTION =
  "Rating 1-5. 5 = excellent: clear schema, useful output, fast, clean " +
  "receipt/proof — you would reuse it confidently. 4 = works and is useful, " +
  "but with a real schema/docs/latency/output caveat (name it in the reason). " +
  "3 = mixed: paid successfully but the response was thin, confusing, or " +
  "needed guesswork. 2 = paid but poor: client error, unclear failure, or " +
  "hard to use. 1 = paid and broken: server error, unusable output, " +
  "misleading challenge, or severe reliability problem (e.g. timeout). A " +
  "service that simply worked well is a 5 — do not hedge to 4 without a " +
  "concrete caveat.";

export const requestServiceShape = {
  service_description: z
    .string()
    .describe(
      "Specific, reusable capability that a provider could sell as a remote " +
        "paid API (x402/mppx/Stripe) — something you could pay for with an " +
        "HTTP request to someone else's endpoint. Include clear inputs and " +
        "outputs. Do NOT request local runtime/agent-harness wishes (context " +
        "management, local compute, IDE features) or one-off task help. Do " +
        "not include secrets, credentials, or private user data — free text " +
        "is additionally redacted locally before it leaves this machine.",
    ),
  task_context: z
    .string()
    .nullish()
    .describe("Optional context about the task that needed this service"),
};

export const getServiceScoreShape = { ...identityShape };

export const signingPayloadShape = {
  rating: z.number().int().describe(RATING_DESCRIPTION),
  reason: z.string().describe("Review text (redacted locally before hashing)"),
  payment_reference: z.string().describe("Payment reference for this review"),
  ...identityShape,
};

export const reviewServiceShape = {
  rating: z.number().int().describe(RATING_DESCRIPTION),
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
