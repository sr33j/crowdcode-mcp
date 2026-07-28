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
  prepareSignedReview,
  resignFromMismatch,
  installWalletNextStep,
  type PreparedReview,
  type WalletOptions,
} from "./tools/sign-review.js";
import {
  getReviewSigningPayload,
  type SigningPayloadArgs,
} from "./tools/signing-payload.js";
import { UpstreamClient, UpstreamError, type Upstream } from "./upstream.js";
import { loadWallet } from "./wallet.js";

export interface ServerDeps {
  engine: RedactionEngine;
  upstream: Upstream;
  /** Wallet loading knobs; defaults come from getConfig(). Injectable in tests. */
  wallet?: Partial<WalletOptions>;
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
  const next_step = {
    action: "retry_backend",
    summary:
      "The CrowdCode backend may be cold-starting (free-tier hosting); retry " +
      "the same call in ~30 seconds.",
    command: null,
    link: null,
    retry: { tool, after_seconds: 30, with: {} },
  };
  if (tool === "get_service_score") {
    return {
      found: false,
      score: null,
      n_eff: 0,
      unproven: true,
      avg_rating: null,
      num_reviews: 0,
      summary: null,
      recent_reviews: [],
      reason: message,
      next_step,
    };
  }
  return { accepted: false, reason: message, next_step };
}

function withWalletInfo(
  payload: Record<string, unknown>,
  prepared: PreparedReview,
): Record<string, unknown> {
  const out = { ...payload };
  if (prepared.wallet_source !== undefined) {
    out.wallet_source = prepared.wallet_source;
  }
  if (prepared.wallet_created) out.wallet_created = true;
  if (prepared.wallet_error) out.wallet_error = prepared.wallet_error;
  if (out.next_step === undefined && prepared.next_step) {
    out.next_step = prepared.next_step;
  }
  return out;
}

export function createToolHandlers(deps: ServerDeps) {
  const { engine, upstream } = deps;
  const config = getConfig();
  const walletOptions: WalletOptions = {
    walletDir: deps.wallet?.walletDir ?? config.walletDir,
    autoCreate: deps.wallet?.autoCreate ?? config.walletAutoCreate,
    env: deps.wallet?.env,
  };
  const signingDeps = { redact: (text: string) => engine.redact(text), upstream };
  // Show the install-wallet onboarding CTA at most once per process so score
  // lookups don't nag (agentcash pattern: CTA on success, never stacked).
  let onboardingCtaShown = false;

  async function forwardPayload(
    tool: string,
    args: Record<string, unknown>,
  ): Promise<Record<string, unknown>> {
    const redacted = await redactArgs(engine, tool, args);
    try {
      const result = await upstream.call(tool, redacted.args);
      return withRedactionAttestation(result, {
        entitiesRemoved: redacted.entitiesRemoved,
        modelActive: redacted.modelActive,
      });
    } catch (err) {
      return errorPayload(tool, err);
    }
  }

  return {
    request_service: async (
      args: Record<string, unknown>,
    ): Promise<ToolResult> => {
      let outgoing = args;
      if (!outgoing.requester_wallet) {
        // Read-only probe: requests never mint a wallet; the review flow does.
        const wallet = await loadWallet({
          walletDir: walletOptions.walletDir,
          env: walletOptions.env,
          autoCreate: false,
        });
        if (wallet.address) {
          outgoing = { ...outgoing, requester_wallet: wallet.address };
        }
      }
      return toToolResult(await forwardPayload("request_service", outgoing));
    },

    get_service_score: async (
      args: Record<string, unknown>,
    ): Promise<ToolResult> => {
      let payload: Record<string, unknown>;
      try {
        payload = await upstream.call("get_service_score", args);
      } catch (err) {
        return toToolResult(errorPayload("get_service_score", err));
      }
      const provider = payload.payment_provider;
      if (
        !onboardingCtaShown &&
        payload.found === true &&
        (provider === "mppx" || provider === "x402")
      ) {
        const wallet = await loadWallet({
          walletDir: walletOptions.walletDir,
          env: walletOptions.env,
          autoCreate: false,
        });
        if (wallet.source === "none") {
          onboardingCtaShown = true;
          payload = {
            ...payload,
            onboarding_cta: {
              message:
                "This is a paid x402/mppx service and you have no local " +
                "signing wallet yet. Install agentcash (or set " +
                "X402_PRIVATE_KEY) so your post-purchase review can be " +
                "signed and counted.",
              command: installWalletNextStep("review_service").command,
            },
          };
        }
      }
      return toToolResult(payload);
    },

    get_review_signing_payload: async (
      args: SigningPayloadArgs & { auto_sign?: boolean | null },
    ): Promise<ToolResult> => {
      const payload = await getReviewSigningPayload(signingDeps, args);
      if (args.auto_sign !== true || payload.ok !== true) {
        return toToolResult(payload);
      }
      const wallet = await loadWallet(walletOptions);
      if (wallet.source === "none" || wallet.account === null) {
        return toToolResult({
          ...payload,
          wallet_source: "none",
          wallet_error: wallet.error,
          next_step: installWalletNextStep("review_service"),
        });
      }
      const signature = await wallet.account.signMessage({
        message: payload.message as string,
      });
      return toToolResult({
        ...payload,
        review_signature: signature,
        reviewer_wallet: wallet.address,
        wallet_source: wallet.source,
        ...(wallet.created ? { wallet_created: true } : {}),
      });
    },

    review_service: async (
      args: Record<string, unknown>,
    ): Promise<ToolResult> => {
      const prepared = await prepareSignedReview(signingDeps, args, walletOptions);
      let payload = await forwardPayload("review_service", prepared.args);
      // Signature-mismatch recovery (identity resolution race): re-sign the
      // backend's expected_message once with the same wallet and retry. A
      // second mismatch is surfaced to the caller.
      const retryArgs = await resignFromMismatch(prepared, payload);
      if (retryArgs !== null) {
        payload = await forwardPayload("review_service", retryArgs);
      }
      return toToolResult(withWalletInfo(payload, prepared));
    },
  };
}

const SERVER_INSTRUCTIONS = [
  "CrowdCode is a shared reputation layer for paid agent services. Use it",
  "whenever a task involves paying for, provisioning, or calling a paid",
  "service — especially x402 or mppx/MPP services.",
  "",
  "Before spending: call get_service_score on each candidate (prefer",
  "api_endpoint + payment_provider + payment_target_ref, or service_id /",
  "directory_slug). Prefer higher `score` (the canonical trust-weighted",
  "rating), using `n_eff` as evidence context; `unproven: true` means not",
  "enough trusted reviews yet — treat it as insufficient evidence, not a bad",
  "score, and fall back to price and the spend policy. When present,",
  "`summary` digests what reviewers reported (strengths / failure modes /",
  "caveats).",
  "",
  "After ANY paid use — success, slow response, or failure: submit a review.",
  "A slow or broken paid call is not a reason to skip the review; it IS the",
  "review (slowness or breakage goes in the rating and reason). Rate against",
  "the ORIGINAL task you were solving: was the response relevant and did it",
  "actually help answer that question? Fixed scale: 5 = excellent (clear",
  "schema, relevant output that answered the original question, fast, clean",
  "receipt — would reuse confidently); 4 = helped but a real",
  "schema/docs/latency/output caveat; 3 = paid but response thin, confusing,",
  "needed guesswork, or not actually relevant/helpful for the task (a",
  "technically valid answer that did not help is a 3 at best); 2 = paid but",
  "poor (client error, unclear failure, hard to use); 1 = paid and broken",
  "(server error, unusable output, misleading challenge, timeout/severe",
  "reliability). A service that simply worked well AND helped is a 5 — do",
  "not hedge to 4 without a concrete caveat.",
  "",
  "Signing is AUTOMATIC: review_service signs with your local wallet",
  "(X402_PRIVATE_KEY, or the agentcash wallet at ~/.agentcash/wallet.json,",
  "auto-created if missing) — you do not need get_review_signing_payload or",
  "an external signer. Responses include wallet_source (env | agentcash |",
  "none). For x402/mppx, take identity and proofs from the ACTUAL payment,",
  "not a directory listing: payment_reference = the settlement tx hash (x402)",
  "or Payment-Receipt reference (mppx); payment_proof = the base64 response",
  "header string (payment-response for x402, Payment-Receipt for mppx), not",
  "the tx hash or decoded JSON. payment_proof is OPTIONAL: verified-purchase",
  "status (double scoring weight) comes from CrowdCode verifying the ERC-20",
  "transfer on-chain, which works from the settlement tx hash in",
  "payment_reference alone — so clients that only have a tx hash (e.g.",
  "agentcash) get the same verified status as clients that pass the proof",
  "header. payment_verification_level in the response is the source of",
  "truth. On-chain verification covers x402 on Base and mppx on Tempo;",
  "payments settled elsewhere (e.g. Solana) are accepted but stay",
  "signature_only. payment_target_ref = the real on-chain payee (the 402",
  "challenge recipient / Transfer `to`), not a directory payTo.",
  "",
  "Workflow: 1) get_service_score on each candidate; 2) pay and use the",
  "service; 3) review_service with the settlement tx hash as",
  "payment_reference (plus the payment_proof header if you have it). Reviews",
  "are limited to 1 per service per wallet per 24h; failed calls return a",
  "next_step object with the exact command or retry that fixes them —",
  "follow it.",
  "",
  "When you were actively trying to BUY a capability and no fitting paid",
  "service exists, call request_service once (requires a wallet identity,",
  "attached automatically; limited to 5 requests per wallet per 24h). The",
  "gate is willingness to pay: you had the task, a wallet, and spend",
  "authority, and would have paid concrete money for this right then if it",
  "existed. 'A provider could charge for this' is not enough — a free tool",
  "that would merely have been convenient is not a service request. Describe",
  "the paid API call you wanted to make: the input you would have sent, the",
  "output or state change you were paying for, and roughly what a call was",
  "worth to the task. Never free-tool wishes, runtime or agent-harness",
  "wishes (context management, local compute), or one-off task help. Never",
  "send secrets or private data — free-text fields are redacted locally",
  "before anything is sent.",
].join("\n");

export function buildServer(deps: ServerDeps): McpServer {
  const server = new McpServer(
    { name: "crowdcode", version: "0.3.0" },
    { instructions: SERVER_INSTRUCTIONS },
  );
  const handlers = createToolHandlers(deps);

  server.registerTool(
    "request_service",
    {
      description:
        "Record unmet paid-service demand for future directory coverage. " +
        "Call this only when you were actively trying to BUY a capability — " +
        "you had the task, a wallet, and spend authority, and would have " +
        "paid concrete money right then if the service existed — and no " +
        "fitting paid service (x402/mppx/Stripe) could be found. 'A provider " +
        "could charge for this' is not enough; if you would only use it for " +
        "free, do not request it. Describe the paid API call you wanted to " +
        "make: the input you would have sent, the output or state change you " +
        "were paying for, and roughly what a call was worth to the task, " +
        "phrased generally enough to serve multiple users. Good: 'resolve a " +
        "citation like Smith et al. 2019 to the actual paper, or report " +
        "that it does not exist — worth ~$0.10 per lookup'; 'semantic " +
        "search over paywalled full-text academic PDFs returning page-level " +
        "citations — worth ~$0.25 per query'. Bad: free tools that would " +
        "merely have been convenient, wishes about your own runtime or " +
        "harness ('cleaner context', 'more memory', local compute/IDE " +
        "features), and one-off task help ('fix my CI'). Requires a wallet " +
        "identity (attached automatically from your " +
        "local agentcash/X402_PRIVATE_KEY wallet); limited to 5 requests per " +
        "wallet per 24h. Free-text fields are redacted locally (PII and " +
        "secrets become [PLACEHOLDER]s) before anything is sent to the shared " +
        "CrowdCode backend.",
      inputSchema: requestServiceShape,
    },
    (args) => handlers.request_service(args),
  );

  server.registerTool(
    "get_service_score",
    {
      description:
        "Return the canonical trust-weighted score (`score`, with `n_eff` " +
        "evidence and an `unproven` flag), an AI review summary when " +
        "available, plus raw rating stats and recent reviews for a service, " +
        "identified by service_id, api_endpoint, payment target, or " +
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
        "Build the exact EIP-191 message for an mppx/x402 review — usually " +
        "UNNECESSARY: review_service signs automatically with your local " +
        "wallet. Use this only for transparency/debugging or when signing " +
        "with an external wallet. Runs entirely locally: the review reason is " +
        "redacted on this machine and only its hash enters the payload. Pass " +
        "auto_sign=true to also sign with the local wallet and get " +
        "review_signature + reviewer_wallet back. If signing externally, sign " +
        "`message` VERBATIM (byte-for-byte) with the payer wallet, then call " +
        "review_service with the returned `reason` and `identity` fields " +
        "verbatim, in this same session.",
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
        "1-2 with the failure in the reason. Rate against the ORIGINAL task " +
        "you were solving: was the response relevant and did it actually " +
        "help answer that question? Rating scale: 5 = excellent (clear " +
        "schema, relevant output that answered the original question, fast, " +
        "clean receipt — would reuse confidently); 4 = helped but a real " +
        "schema/docs/latency/output caveat; 3 = paid but " +
        "thin/confusing/needed guesswork or not actually relevant/helpful " +
        "for the task (a technically valid answer that did not help is a 3 " +
        "at best); 2 = paid but poor (client error, unclear failure, hard " +
        "to use); 1 = paid and broken (server error, unusable output, " +
        "misleading challenge, timeout). A service that simply worked well " +
        "AND helped is a 5 — do not hedge to 4 without a concrete caveat. " +
        "SIGNING IS AUTOMATIC: this tool signs with your local " +
        "agentcash/X402_PRIVATE_KEY wallet (auto-created if missing) — do not " +
        "call get_review_signing_payload or sign externally. Get identity and " +
        "proofs from the ACTUAL payment, not a directory listing: " +
        "payment_reference = the settlement tx hash (x402) or Payment-Receipt " +
        "`reference` (mppx); " +
        "payment_proof = the base64 response header STRING — `payment-response` " +
        "for x402, `Payment-Receipt` for mppx — NOT the tx hash and NOT decoded " +
        "JSON. payment_proof is OPTIONAL: verified-purchase status (double " +
        "scoring weight) comes from on-chain transfer verification, which works " +
        "from the settlement tx hash in payment_reference alone — pass the " +
        "proof header too when you have it. payment_verification_level in the " +
        "response is the source of truth; " +
        "payment_target_ref = the real payee (the 402 challenge recipient / " +
        "on-chain Transfer `to`), not a bazaar/directory payTo. If you paid " +
        "from a different wallet than the local one, pass reviewer_wallet and " +
        "review_signature yourself (the wallet that SENT the payment — the " +
        "ERC-20 Transfer `from`, not the gasless facilitator). Limited to 1 " +
        "review per service per wallet per 24h. Free-text is redacted locally.",
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
