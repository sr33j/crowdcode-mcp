/**
 * The stdio MCP server. Advertises the same tools as the CrowdCode backend;
 * free-text arguments are redacted locally (Rampart + secret recognizers)
 * before forwarding over streamable-HTTP, and get_review_signing_payload is
 * served entirely locally so raw review text never leaves this machine at
 * signing time.
 */

import { z } from "zod";
import { Preferences } from "./preferences.js";
import { managementMessage } from "./tools/manage-reviews.js";
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
  preferences?: Preferences;
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
  const preferences = deps.preferences ?? new Preferences();
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
    const stopped = await disabled();
    if (stopped) return stopped.structuredContent;
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

  async function disabled(): Promise<ToolResult | null> {
    try {
      if ((await preferences.status()).enabled) return null;
      return toToolResult({ status: "disabled", accepted: false, enabled: false,
        reason: "CrowdCode is off. Continue the user's task without CrowdCode. Do not retry or queue submissions. History, deletion, and settings remain available." });
    } catch {
      return toToolResult({ status: "unavailable", accepted: false,
        error_code: "preferences_unavailable", reason: "Cannot read CrowdCode preferences; no data was sent." });
    }
  }

  async function manageReviews(action: "list" | "delete", args: { review_id?: number; before_id?: number; limit?: number }) {
    const wallet = await loadWallet({ ...walletOptions, autoCreate: false });
    if (!wallet.account || !wallet.address) return toToolResult({ accepted: false,
      error_code: wallet.errorCode ?? "wallet_unavailable",
      reason: "Use the original reviewing wallet to access or delete its reviews. No wallet was created." });
    const expires_at = Math.floor(Date.now() / 1000) + 300;
    const message = managementMessage({ action, wallet: wallet.address, expires_at, ...args });
    const authorization = await wallet.account.signMessage({ message });
    const tool = action === "list" ? "list_my_reviews" : "delete_my_review";
    try {
      return toToolResult(await upstream.call(tool, { ...args, reviewer_wallet: wallet.address,
        expires_at, authorization }));
    } catch (error) {
      return toToolResult(errorPayload(tool, error));
    }
  }

  return {
    crowdcode_status: async () => toToolResult(await preferences.status()),
    set_crowdcode_enabled: async (args: { enabled: boolean; scope?: "session" | "default" }) =>
      toToolResult(await preferences.set(args.enabled, args.scope)),
    list_my_reviews: async (args: { before_id?: number; limit?: number }) =>
      manageReviews("list", { ...args, limit: args.limit ?? 25 }),
    delete_my_review: async (args: { review_id: number }) => manageReviews("delete", args),
    request_service: async (
      args: Record<string, unknown>,
    ): Promise<ToolResult> => {
      const stopped = await disabled();
      if (stopped) return stopped;
      let outgoing = args;
      let walletCreated = false;
      if (!outgoing.requester_wallet) {
        // Demand needs an identity, not funds or permission to make a payment.
        const wallet = await loadWallet({
          walletDir: walletOptions.walletDir,
          env: walletOptions.env,
          autoCreate: walletOptions.autoCreate,
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
          walletCreated = wallet.created === true;
        }
      }
      return toToolResult({ ...await forwardPayload("request_service", outgoing),
        ...(walletCreated ? { wallet_created: true, wallet_source: "agentcash" } : {}) });
    },

    get_service_score: async (
      args: Record<string, unknown>,
    ): Promise<ToolResult> => {
      const stopped = await disabled();
      if (stopped) return stopped;
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
      return toToolResult({ ...payload, task_completion_reminder:
        "Before your final answer, reflect on concrete paid services that would fix observed shortcomings. Use request_service for worthwhile gaps; submit nothing if all worked well. Respect CrowdCode off." });
    },

    get_review_signing_payload: async (
      args: SigningPayloadArgs & { auto_sign?: boolean | null },
    ): Promise<ToolResult> => {
      const stopped = await disabled();
      if (stopped) return stopped;
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
      const stopped = await disabled();
      if (stopped) return stopped;
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
      return toToolResult({ ...withWalletInfo(payload, prepared), task_completion_reminder: "Before your final answer, consider request_service for concrete improvements worth paying for, grounded in this task. Skip if all worked well; do not duplicate earlier requests." });
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
  "Before the final answer to each substantive user task, reflect once: what concrete",
  "paid service would have avoided an observed failure, poor result, expensive workaround,",
  "or wrong turn? This applies even if no purchase was attempted. Call request_service",
  "for each distinct, worthwhile gap (do not repeat the same gap on retries or follow-ups).",
  "Describe the real use case, precise input, deliverable, acceptance criteria, observed",
  "shortcoming, and why someone would pay for that outcome. An inadequate existing service",
  "qualifies if you name the specific improvement. No actual payment or spend authority",
  "is required to report demand; do not invent a budget or claim the user agreed to pay.",
  "Skip generic local compute, run-Python wishes, ordinary agent mistakes without a",
  "sellable remedy, and services that already worked well at reasonable cost. If nothing",
  "qualifies, submit nothing. Never send secrets or private user data.",
  "At task start check crowdcode_status. User says 'Crowdcode on'/'Crowdcode off': call",
  "set_crowdcode_enabled with enabled=true/false and scope=session, or scope=default",
  "when they ask to remember it. Off overrides all automatic CrowdCode workflows:",
  "continue the user's task, skip reflection, checks and submissions; never backfill.",
  "Use list_my_reviews for 'show my reviews'; delete_my_review only for review IDs the",
  "user asks to delete. These management tools remain available while CrowdCode is off.",
].join("\n");

export function buildServer(deps: ServerDeps): McpServer {
  const server = new McpServer(
    { name: "crowdcode", version: "0.5.3" },
    { instructions: SERVER_INSTRUCTIONS },
  );
  const handlers = createToolHandlers(deps);

  server.registerTool("crowdcode_status", {
    description: "Read CrowdCode's effective on/off state and saved default locally. Check at task start.",
    inputSchema: {}, annotations: { readOnlyHint: true },
  }, () => handlers.crowdcode_status());
  server.registerTool("set_crowdcode_enabled", {
    description: "Handle 'Crowdcode on' or 'Crowdcode off'. Session scope lasts for this MCP connection; default saves the choice for future connections and updates this one. Off stops checks, signing, reviews, and requests. History and deletion stay available. No queued backfill.",
    inputSchema: { enabled: z.boolean(), scope: z.enum(["session", "default"]).default("session") },
  }, (args) => handlers.set_crowdcode_enabled(args));
  server.registerTool("list_my_reviews", {
    description: "Show reviews submitted by the local wallet, newest first, including review IDs for deletion. Follow next_before_id to see older reviews. Works while CrowdCode is off. Locally signs an expiring ownership proof; never creates a wallet.",
    inputSchema: { before_id: z.number().int().positive().max(Number.MAX_SAFE_INTEGER).optional(), limit: z.number().int().min(1).max(100).default(25) },
    annotations: { readOnlyHint: true },
  }, (args) => handlers.list_my_reviews(args));
  server.registerTool("delete_my_review", {
    description: "Permanently delete the specific review ID requested by the user, owned by the local wallet. List reviews first if the ID is unknown. Removes review text and invalidates its summary and score; retains only a hashed replay-prevention key. Works while off. Never bulk-delete or choose IDs without user direction.",
    inputSchema: { review_id: z.number().int().positive().max(Number.MAX_SAFE_INTEGER) },
    annotations: { destructiveHint: true, idempotentHint: true },
  }, (args) => handlers.delete_my_review(args));


  server.registerTool(
    "request_service",
    {
      description:
        "Record a concrete paid service that would have materially improved the task. " +
        "Before your final answer, reflect on failures, poor results, excessive cost, and " +
        "avoidable detours, even when no purchase was attempted. Describe the actual use " +
        "case, exact input, paid deliverable, acceptance criteria, what failed, and why " +
        "the outcome is worth paying for. Existing services may qualify when you explain " +
        "the specific deficiency. Example: scanned annual reports to reconciled tables " +
        "with page citations and confidence flags, because ordinary OCR dropped columns " +
        "and required manual reconciliation. Skip generic web search that worked well, " +
        "run-Python/local runtime wishes, and unsupported hypothetical needs. No payment " +
        "or spending authority is needed; never invent a budget. One request per distinct " +
        "gap per task; respect the returned wallet limit. Submit nothing if all worked " +
        "well at reasonable cost. Free text is redacted locally.",
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
  if ((await new Preferences().status()).enabled) void warnOnToolDrift(upstream);
}
