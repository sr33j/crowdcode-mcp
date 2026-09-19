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
import { UpstreamClient, type Upstream } from "./upstream.js";
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
  _err: unknown,
): Record<string, unknown> {
  const next_step = {
    action: "retry_backend",
    summary:
      "The CrowdCode backend or one of its dependencies is temporarily " +
      "unavailable; retry the same call in ~30 seconds.",
    command: null,
    link: null,
    retry: { tool, after_seconds: 30, with: {} },
  };
  if (tool === "get_service_score") {
    return {
      status: "unavailable",
      error_code: "backend_unavailable",
      retryable: true,
      found: false,
      score: null,
      n_eff: 0,
      avg_rating: null,
      num_reviews: 0,
      summary: null,
      recent_reviews: [],
      reason: "CrowdCode is temporarily unavailable",
      next_step,
    };
  }
  return {
    status: "unavailable",
    error_code: "backend_unavailable",
    retryable: true,
    accepted: false,
    reason: "CrowdCode is temporarily unavailable",
    next_step,
  };
}

function withWalletInfo(
  payload: Record<string, unknown>,
  prepared: PreparedReview,
): Record<string, unknown> {
  const out = { ...payload };
  if (prepared.args.review_nonce != null) out.review_nonce = prepared.args.review_nonce;
  if (prepared.wallet_source !== undefined) {
    out.wallet_source = prepared.wallet_source;
  }
  if (prepared.wallet_created) out.wallet_created = true;
  if (prepared.wallet_error) out.wallet_error = prepared.wallet_error;
  if (out.error_code === undefined && prepared.wallet_error_code) {
    out.error_code = prepared.wallet_error_code;
  }
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
        if (wallet.errorCode === "wallet_configuration_error") {
          return toToolResult({
            status: "rejected",
            error_code: wallet.errorCode,
            retryable: false,
            accepted: false,
            reason: wallet.error,
          });
        }
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
              message: wallet.errorCode === "wallet_configuration_error"
                ? wallet.error
                : "This is a paid x402/mppx service and you have no local " +
                  "signing wallet yet. Install agentcash so your post-purchase " +
                  "review can be signed and counted.",
              command: wallet.errorCode === "wallet_configuration_error"
                ? null
                : installWalletNextStep("review_service").command,
            },
          };
        }
      }
      return toToolResult(payload);
    },

    get_review_signing_payload: async (
      args: SigningPayloadArgs & { auto_sign?: boolean | null },
    ): Promise<ToolResult> => {
      const signingWallet =
        args.auto_sign === true ? await loadWallet(walletOptions) : null;
      if (signingWallet?.errorCode === "wallet_configuration_error") {
          return toToolResult({
            status: "rejected",
            error_code: signingWallet.errorCode,
            retryable: false,
            ok: false,
            reason: signingWallet.error,
            wallet_source: "none",
          });
      }
      const payload = await getReviewSigningPayload(signingDeps, args);
      if (args.auto_sign !== true || payload.ok !== true) {
        return toToolResult(payload);
      }
      const wallet = signingWallet!;
      if (wallet.source === "none" || wallet.account === null) {
        return toToolResult({
          ...payload,
          status: "rejected",
          error_code: wallet.errorCode ?? "wallet_unavailable",
          retryable: false,
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
      if (prepared.wallet_error_code === "wallet_configuration_error") {
        return toToolResult({
          status: "rejected",
          error_code: prepared.wallet_error_code,
          retryable: false,
          accepted: false,
          reason: prepared.wallet_error,
          wallet_source: "none",
        });
      }
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
  "This means third-party APIs, remote services, and provisioned",
  "infrastructure. Do not use CrowdCode to gate trades, swaps, market orders,",
  "transfers, deposits, withdrawals, bridges, staking, lending, escrow",
  "settlement, or purchases of financial instruments.",
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
  "Review each paid service outcome with review_service. Unpaid experiences use",
  "the same tool: omit payment_reference and payment_proof, and reuse a stable",
  "review_nonce on retry (automatically generated when omitted). Payment is",
  "explicitly marked unverified. The reviewer still signs with the local",
  "AgentCash wallet; signing is automatic and does not make a payment.",
  "Rate usefulness for the original task from 1 (unusable) to 5 (excellent).",
  "Describe observed outcomes; distinguish provider faults from client bugs,",
  "insufficient funds, and uncertain causes. Do not invent payment evidence.",
  "When claiming payment, use the actual settlement reference and payee.",
  "An optional payment_proof is the original base64 response header. Payment",
  "claims must verify on x402 Base USDC or mppx Tempo. Invalid supplied claims",
  "are rejected, never silently downgraded. Payment verification gives the",
  "existing verified scoring weight; unpaid signed reviews share the same",
  "history, score, and per-wallet/service/day influence cap.",
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
    { name: "crowdcode", version: "0.5.3" },
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
        "local agentcash wallet); limited to 5 requests per " +
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
        "Review a service experience, paid or unpaid, with a rating from 1 to 5 " +
        "and concrete observations about usefulness for the original task. " +
        "Review every paid outcome; unpaid successes and failures can use the same tool. " +
        "Distinguish provider faults from client errors, insufficient funds, and uncertain causes. " +
        "Signing with the local AgentCash wallet is automatic. Omit payment_reference " +
        "and payment_proof when no payment is claimed; payment is then marked unverified. " +
        "Use a stable review_nonce for unpaid retries (generated automatically if omitted). " +
        "For paid reviews, supply the actual settlement reference and payee; optional " +
        "payment_proof is the original base64 response header. Claims must verify on " +
        "x402 Base USDC or mppx Tempo; invalid payment evidence is rejected, never downgraded. " +
        "Daily scoring influence is capped. Review text is redacted locally.",
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
