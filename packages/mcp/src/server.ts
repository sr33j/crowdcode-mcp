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

export function buildServer(deps: ServerDeps): McpServer {
  const server = new McpServer({ name: "crowdcode", version: "0.1.0" });
  const handlers = createToolHandlers(deps);

  server.registerTool(
    "request_service",
    {
      description:
        "Capture an unmet, reusable service request for future directory " +
        "coverage. Use only when no fitting paid or external service exists. " +
        "Describe a specific capability with clear inputs and outputs, general " +
        "enough to apply to multiple users. Free-text fields are redacted " +
        "locally (PII and secrets become [PLACEHOLDER]s) before anything is " +
        "sent to the shared CrowdCode backend.",
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
        "directory_slug. Check this before paying for a service.",
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
        "`message` with the reviewer wallet, then call review_service with the " +
        "returned `reason` and `identity` fields passed through verbatim, in " +
        "this same session.",
      inputSchema: signingPayloadShape,
    },
    (args) => handlers.get_review_signing_payload(args as SigningPayloadArgs),
  );

  server.registerTool(
    "review_service",
    {
      description:
        "Submit a review for a service you paid for. Requires the payment " +
        "reference; mppx/x402 reviews additionally require payment_proof, " +
        "reviewer_wallet, and review_signature over the message from " +
        "get_review_signing_payload. Free-text fields are redacted locally " +
        "before submission — pass the exact `reason` returned by " +
        "get_review_signing_payload when submitting a signed review.",
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
