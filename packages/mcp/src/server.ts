/**
 * The stdio MCP server. Advertises the same tools as the CrowdCode backend;
 * free-text arguments are redacted locally (Rampart + secret recognizers)
 * before forwarding over streamable-HTTP, and get_review_signing_payload is
 * served entirely locally so raw review text never leaves this machine at
 * signing time.
 */

import { RedactionEngine } from "@crowdcode/redaction";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { toToolResult, withRedactionAttestation } from "./attestation.js";
import { getConfig } from "./config.js";
import { redactArgs } from "./redaction/policy.js";
import {
  MIRRORED_REMOTE_TOOLS,
  getServiceScoreShape,
  requestServiceShape,
  reviewServiceShape,
  signingPayloadShape,
} from "./schemas.js";
import {
  getReviewSigningPayload,
  type SigningPayloadArgs,
} from "./tools/signing-payload.js";
import { UpstreamClient, UpstreamError, type Upstream } from "./upstream.js";

export interface ServerDeps {
  engine: RedactionEngine;
  upstream: Upstream;
}

type ToolResult = ReturnType<typeof toToolResult>;

function errorPayload(
  tool: string,
  err: unknown,
): Record<string, unknown> {
  const message =
    err instanceof UpstreamError
      ? err.message
      : `could not reach the CrowdCode backend: ${(err as Error).message}`;
  if (tool === "get_service_score") {
    return {
      found: false,
      avg_rating: null,
      num_reviews: 0,
      recent_reviews: [],
      reason: message,
    };
  }
  return { accepted: false, reason: message };
}

export function createToolHandlers(deps: ServerDeps) {
  const { engine, upstream } = deps;

  async function forwardWithRedaction(
    tool: string,
    args: Record<string, unknown>,
  ): Promise<ToolResult> {
    const redacted = await redactArgs(engine, tool, args);
    try {
      const result = await upstream.call(tool, redacted.args);
      return toToolResult(
        withRedactionAttestation(result, {
          entitiesRemoved: redacted.entitiesRemoved,
          modelActive: redacted.modelActive,
        }),
      );
    } catch (err) {
      return toToolResult(errorPayload(tool, err));
    }
  }

  return {
    request_service: (args: Record<string, unknown>) =>
      forwardWithRedaction("request_service", args),

    get_service_score: async (
      args: Record<string, unknown>,
    ): Promise<ToolResult> => {
      try {
        return toToolResult(await upstream.call("get_service_score", args));
      } catch (err) {
        return toToolResult(errorPayload("get_service_score", err));
      }
    },

    get_review_signing_payload: async (
      args: SigningPayloadArgs,
    ): Promise<ToolResult> =>
      toToolResult(
        await getReviewSigningPayload(
          { redact: (text) => engine.redact(text), upstream },
          args,
        ),
      ),

    review_service: (args: Record<string, unknown>) =>
      forwardWithRedaction("review_service", args),
  };
}

const SERVER_INSTRUCTIONS = [
  "CrowdCode is a shared reputation layer for paid agent services. Use it",
  "whenever a task involves paying for, provisioning, or calling a paid",
  "service — especially x402 or mppx/MPP services.",
  "",
  "Before spending: call get_service_score on each candidate (prefer",
  "api_endpoint + payment_provider + payment_target_ref, or service_id /",
  "directory_slug). Prefer higher avg_rating, using num_reviews as sample-size",
  "context; with few or no reviews, fall back to price and the spend policy.",
  "",
  "After ANY paid use — success, slow response, or failure: submit a review.",
  "A slow or broken paid call is not a reason to skip the review; it IS the",
  "review (slowness or breakage goes in the rating and reason). Rate on a",
  "fixed scale: 5 = excellent (clear schema, useful output, fast, clean",
  "receipt — would reuse confidently); 4 = works but a real",
  "schema/docs/latency/output caveat; 3 = paid but response thin, confusing,",
  "or needed guesswork; 2 = paid but poor (client error, unclear failure,",
  "hard to use); 1 = paid and broken (server error, unusable output,",
  "misleading challenge, timeout/severe reliability). A service that simply",
  "worked well is a 5 — do not hedge to 4 without a concrete caveat.",
  "",
  "For x402/mppx, take the",
  "identity and proofs from the ACTUAL payment, not a directory listing:",
  "payment_reference = the settlement tx hash (x402) or Payment-Receipt",
  "reference (mppx); payment_proof = the base64 response header string",
  "(payment-response for x402, Payment-Receipt for mppx), not the tx hash or",
  "decoded JSON; payment_target_ref = the real on-chain payee (the 402",
  "challenge recipient / Transfer `to`), not a directory payTo; reviewer_wallet",
  "= the wallet that SENT the payment (the Transfer `from`, not the gasless",
  "facilitator), which must be a self-custody wallet you can EIP-191 sign with.",
  "Call get_review_signing_payload, sign the returned message verbatim, then",
  "call review_service in the same session.",
  "",
  "When no fitting paid service exists, call request_service once with a",
  "specific, reusable capability description. Only request things a provider",
  "could sell as a remote paid API — the test is: could you pay for it with",
  "an x402/mpp request to someone else's endpoint? Never runtime or",
  "agent-harness wishes (context management, local compute). Never send",
  "secrets or private data — free-text fields are redacted locally before",
  "anything is sent.",
].join("\n");

export function buildServer(deps: ServerDeps): McpServer {
  const server = new McpServer(
    { name: "crowdcode", version: "0.1.3" },
    { instructions: SERVER_INSTRUCTIONS },
  );
  const handlers = createToolHandlers(deps);

  server.registerTool(
    "request_service",
    {
      description:
        "Capture an unmet, reusable service request for future directory " +
        "coverage. Use only when no fitting paid or external service exists. " +
        "Only request capabilities a provider could sell as a remote paid API " +
        "(x402/mppx/Stripe) — the test: could you pay for it with an x402/mpp " +
        "request to someone else's endpoint? Describe a specific capability " +
        "with clear inputs and outputs, general enough to apply to multiple " +
        "users. Good: 'resolve a citation like Smith et al. 2019 to the " +
        "actual paper, or report that it does not exist'; 'semantic search " +
        "over paywalled full-text academic PDFs returning page-level " +
        "citations'; 'live versioned registry of current API schemas'. Bad: " +
        "wishes about your own runtime or harness ('cleaner context', 'more " +
        "memory', local compute/IDE features) and one-off task help ('fix my " +
        "CI'). Free-text fields are redacted locally (PII and secrets become " +
        "[PLACEHOLDER]s) before anything is sent to the shared CrowdCode " +
        "backend.",
      inputSchema: requestServiceShape,
    },
    (args) => handlers.request_service(args),
  );

  server.registerTool(
    "get_service_score",
    {
      description:
        "Return the average rating, review count, and recent reviews for a " +
        "service, identified by service_id, api_endpoint, payment target, or " +
        "directory_slug. Check this before paying for, provisioning, or calling " +
        "any paid agent service — especially x402 and mppx/MPP services.",
      inputSchema: getServiceScoreShape,
    },
    (args) => handlers.get_service_score(args),
  );

  server.registerTool(
    "get_review_signing_payload",
    {
      description:
        "Build the exact EIP-191 message to sign before submitting an mppx or " +
        "x402 review. Runs entirely locally: the review reason is redacted on " +
        "this machine and only its hash enters the payload. Sign the returned " +
        "`message` VERBATIM (byte-for-byte) with the payer wallet — the same " +
        "self-custody wallet that sent the payment, whose key you hold " +
        "(custodial or login-only wallets cannot sign an arbitrary message). " +
        "Then call review_service with the returned `reason` and `identity` " +
        "fields passed through verbatim, in this same session.",
      inputSchema: signingPayloadShape,
    },
    (args) => handlers.get_review_signing_payload(args as SigningPayloadArgs),
  );

  server.registerTool(
    "review_service",
    {
      description:
        "Submit a review after paying for a service — call this after EVERY " +
        "paid x402/mppx use, including slow responses and failures. A bad " +
        "outcome is not a reason to skip the review; it IS the review: rate " +
        "1-2 with the failure in the reason. Rating scale: 5 = excellent " +
        "(clear schema, useful output, fast, clean receipt — would reuse " +
        "confidently); 4 = works but a real schema/docs/latency/output " +
        "caveat; 3 = paid but thin/confusing/needed guesswork; 2 = paid but " +
        "poor (client error, unclear failure, hard to use); 1 = paid and " +
        "broken (server error, unusable output, misleading challenge, " +
        "timeout). A service that simply worked well is a 5 — do not hedge " +
        "to 4 without a concrete caveat. " +
        "Requires the payment reference; mppx/x402 reviews also " +
        "require payment_proof, reviewer_wallet, and review_signature over the " +
        "message from get_review_signing_payload. Get these from the ACTUAL " +
        "payment, not a directory listing: " +
        "payment_reference = the settlement tx hash (x402) or Payment-Receipt " +
        "`reference` (mppx); " +
        "payment_proof = the base64 response header STRING — `payment-response` " +
        "for x402, `Payment-Receipt` for mppx — NOT the tx hash and NOT decoded " +
        "JSON; " +
        "payment_target_ref = the real payee (the 402 challenge recipient / " +
        "on-chain Transfer `to`), not a bazaar/directory payTo; " +
        "reviewer_wallet = the wallet that SENT the payment (the ERC-20 " +
        "Transfer `from`) — for gasless x402/mppx the tx sender is a " +
        "facilitator, not the payer. Free-text is redacted locally; pass the " +
        "exact `reason` returned by get_review_signing_payload.",
      inputSchema: reviewServiceShape,
    },
    (args) => handlers.review_service(args),
  );

  return server;
}

async function warnOnToolDrift(upstream: Upstream): Promise<void> {
  try {
    const remote = new Set(await upstream.listToolNames());
    const missing = MIRRORED_REMOTE_TOOLS.filter((name) => !remote.has(name));
    if (missing.length > 0) {
      process.stderr.write(
        `crowdcode-mcp: backend no longer advertises: ${missing.join(", ")} — ` +
          "update the crowdcode-mcp package.\n",
      );
    }
  } catch {
    // Backend unreachable at startup (cold start); tool calls will retry.
  }
}

export async function startServer(): Promise<void> {
  const config = getConfig();
  const engine = await RedactionEngine.create({
    cacheDir: config.cacheDir,
    enableModel: !config.disableModel,
  });
  const upstream = new UpstreamClient(config.backendUrl, config.upstreamTimeoutMs);
  const server = buildServer({ engine, upstream });
  await server.connect(new StdioServerTransport());
  void warnOnToolDrift(upstream);
}
