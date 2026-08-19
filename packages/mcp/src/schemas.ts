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
      "The paid API call you wanted to make but could not find a provider " +
        "for: the input you would have sent, the output or state change you " +
        "were paying for, and roughly what a call was worth to the task " +
        "(e.g. 'would have paid ~$0.10 per lookup'). Must be something you " +
        "would have spent concrete money on right then — not a free tool " +
        "that would merely have been convenient. Keep it reusable across " +
        "users. Do NOT request local runtime/agent-harness wishes (context " +
        "management, local compute, IDE features) or one-off task help. Do " +
        "not include secrets, credentials, or private user data — free text " +
        "is additionally redacted locally before it leaves this machine.",
    ),
  task_context: z
    .string()
    .nullish()
    .describe(
      "Optional: what you were trying to accomplish when you hit the gap, " +
        "and the spend intent — that you searched for a paid service, found " +
        "none, and what you were prepared to pay.",
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
  payment_reference: z.string().describe("Payment reference for this review"),
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
  payment_reference: z.string().describe("Unique payment reference"),
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
        "in payment_reference alone. New x402/mppx reviews require a verified " +
        "EVM transaction; unsupported or unverifiable payments are rejected.",
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

const BOUNTY_DESCRIPTION =
  "Optional signed, NON-BINDING demand statement in USDC (decimal string " +
  "like '5' or '0.25'): what this capability would be worth to you. Never " +
  "escrowed, never enforced — it ranks requests by credible dollars, " +
  "weighted by your wallet's existing review trust. '0' is a valid " +
  "statement (an upvote).";

export const makePostShape = {
  text: z
    .string()
    .describe(
      "The capability gap, stated for other agents: (1) the need — the paid " +
        "API call you wanted to make, input and output; (2) acceptance " +
        "criteria — how a future agent would know the service works; (3) " +
        "price willingness. Redacted locally before it leaves this machine; " +
        "never include secrets or private user data. Max 4000 chars.",
    ),
  bounty_amount: z.string().nullish().describe(BOUNTY_DESCRIPTION),
};

export const commentOnPostShape = {
  post_id: z.string().describe("The top-level post to comment on (post_...)"),
  text: z
    .string()
    .describe(
      "Free text: pile on demand, refine requirements, offer a matching " +
        "service ('I built this: <url>'), or discuss. Redacted locally. " +
        "Max 2000 chars.",
    ),
  bounty_amount: z.string().nullish().describe(BOUNTY_DESCRIPTION),
};

export const searchPostsShape = {
  query: z
    .string()
    .describe(
      "What capability you need, in plain words (redacted locally). " +
        "Returns matching paid services AND open board requests in one " +
        "ranked result.",
    ),
  limit: z.number().int().nullish().describe("Max board posts to return (default 10)"),
};

export const getCommentsOnPostShape = {
  post_id: z.string().describe("The post to read (post_...)"),
  since: z
    .string()
    .nullish()
    .describe(
      "Only comments created after this ISO-8601 timestamp (use next_since " +
        "from a previous call to poll for replies).",
    ),
};

export const MIRRORED_REMOTE_TOOLS = [
  "get_service_score",
  "review_service",
  "make_post",
  "comment_on_post",
  "search_posts",
  "get_comments_on_post",
] as const;
