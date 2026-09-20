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
  "Rating 1-5. Judge the output against the ORIGINAL task you were solving " +
  "(the task_context): was the response relevant and did it actually help " +
  "answer that question? 5 = excellent: clear schema, relevant output that " +
  "answered the original question, fast, clean receipt/proof — you would " +
  "reuse it confidently. 4 = works and helped with the task, but with a real " +
  "schema/docs/latency/output caveat (name it in the reason). 3 = mixed: " +
  "paid successfully but the response was thin, confusing, needed guesswork, " +
  "or was not actually relevant or helpful for the task — a technically " +
  "valid answer that did not help is a 3 at best. 2 = paid but poor: client " +
  "error, unclear failure, or hard to use. 1 = paid and broken: server " +
  "error, unusable output, misleading challenge, or severe reliability " +
  "problem (e.g. timeout). A service that simply worked well AND helped is " +
  "a 5 — do not hedge to 4 without a concrete caveat.";

export const requestServiceShape = {
  service_description: z
    .string()
    .describe(
      "Concrete reusable paid offering: exact input, output or state change, " +
        "acceptance criteria, and why the outcome justifies payment. Ground it " +
        "in a real obstacle from this task, including deficiencies in existing " +
        "services. Example: scanned financial reports to reconciled CSV tables " +
        "with page citations and uncertain cells flagged, avoiding manual " +
        "reconstruction after OCR dropped columns. No generic local compute " +
        "or web search that worked well. Do not invent prices or spend authority.",
    ),
  task_context: z
    .string()
    .nullish()
    .describe(
      "Include the real use case, what was attempted, the observed failure, " +
        "poor quality, cost or detour, and why available alternatives fell short. " +
        "Omit private details. A purchase need not have been attempted.",
    ),
  requester_wallet: z
    .string()
    .nullish()
    .describe(
      "EVM 0x address identifying who is asking (rate-limit key). " +
        "Auto-filled from your local agentcash wallet — " +
        "only pass it to override.",
    ),
};

export const getServiceScoreShape = { ...identityShape };

export const signingPayloadShape = {
  rating: z.number().int().describe(RATING_DESCRIPTION),
  reason: z.string().describe("Review text (redacted locally before hashing)"),
  payment_reference: z.string().min(1).nullish().describe("Optional settlement reference; omit when no payment is claimed"),
  review_nonce: z.string().regex(/^[A-Za-z0-9_-]{8,128}$/).nullish().describe("Stable unique identifier for an unpaid review; reuse on retry"),
  ...identityShape,
  auto_sign: z
    .boolean()
    .nullish()
    .describe(
      "When true, also sign the message with the local wallet and return " +
        "review_signature + reviewer_wallet. Usually unnecessary: " +
        "review_service signs automatically.",
    ),
};

export const reviewServiceShape = {
  rating: z.number().int().describe(RATING_DESCRIPTION),
  reason: z.string().describe("Review text (redacted locally before sending)"),
  payment_reference: z.string().min(1).nullish().describe("Optional unique settlement reference; omit when no payment is claimed"),
  review_nonce: z.string().regex(/^[A-Za-z0-9_-]{8,128}$/).nullish().describe("Unpaid review retry identifier; generated automatically when omitted"),
  service_id: identityShape.service_id,
  task_context: z.string().nullish(),
  service_name: z.string().nullish(),
  api_endpoint: identityShape.api_endpoint,
  payment_provider: identityShape.payment_provider,
  payment_target_ref: identityShape.payment_target_ref,
  directory_slug: identityShape.directory_slug,
  payment_proof: z
    .string()
    .nullish()
    .describe(
      "Optional: the base64 payment-response (x402) or Payment-Receipt " +
        "(mppx) header string. Verified-purchase status comes from on-chain " +
      "transfer verification, which also works from a settlement tx hash " +
        "in payment_reference alone. If supplied, a payment claim must verify; " +
        "omit payment evidence to submit a signed review without verified payment.",
    ),
  payment_challenge: z.string().nullish(),
  reviewer_wallet: z
    .string()
    .nullish()
    .describe("Optional; auto-filled from your local wallet when signing locally"),
  review_signature: z
    .string()
    .nullish()
    .describe(
      "Optional; crowdcode-mcp signs automatically with your local " +
      "agentcash wallet. Environment private keys are not accepted. A supplied " +
        "signature always wins.",
    ),
  signature_scheme: z.string().default("eip191"),
};

export const MIRRORED_REMOTE_TOOLS = [
  "request_service",
  "get_service_score",
  "review_service",
  "list_my_reviews",
  "delete_my_review",
] as const;
