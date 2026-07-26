/**
 * Transparent in-process signing for review_service.
 *
 * For mppx/x402 reviews where the caller did not supply review_signature,
 * this builds the canonical message (identity-merge + local redaction via
 * getReviewSigningPayload), signs it with the local agentcash wallet (or
 * X402_PRIVATE_KEY), and augments the outgoing arguments — so an agent can
 * call review_service end-to-end with no external signing step. A
 * caller-supplied signature always wins.
 *
 * The signature covers the REDACTED reason; the redaction engine memoizes,
 * so the later per-forward redaction pass produces the byte-identical string
 * the backend hashes when rebuilding the payload.
 */

import { normalizePaymentProvider } from "../canonical/identity.js";
import {
  loadWallet,
  type LoadedWallet,
  type WalletSource,
} from "../wallet.js";
import { getReviewSigningPayload, type SigningDeps } from "./signing-payload.js";

export const AGENTCASH_INSTALL_COMMAND =
  "claude mcp add --scope user agentcash -- npx -y agentcash@latest";

export interface NextStep {
  action: string;
  summary: string;
  command: string | null;
  link: string | null;
  retry: { tool: string; after_seconds: number | null; with: Record<string, unknown> } | null;
}

export function installWalletNextStep(retryTool: string): NextStep {
  return {
    action: "install_wallet",
    summary:
      "No local signing wallet found. Install agentcash (or set " +
      "X402_PRIVATE_KEY) so crowdcode-mcp can sign automatically, then retry " +
      retryTool +
      ".",
    command: AGENTCASH_INSTALL_COMMAND,
    link: null,
    retry: { tool: retryTool, after_seconds: null, with: {} },
  };
}

export interface WalletOptions {
  walletDir?: string;
  autoCreate: boolean;
  env?: NodeJS.ProcessEnv;
}

export interface PreparedReview {
  args: Record<string, unknown>;
  /** Set when local signing was attempted (mppx/x402, no caller signature). */
  wallet_source?: WalletSource;
  wallet_created?: boolean;
  wallet_error?: string;
  next_step?: NextStep;
  /** Wallet used for signing — kept so a signature-mismatch retry can re-sign. */
  wallet?: LoadedWallet;
  signed: boolean;
}

function isMachineProvider(value: unknown): boolean {
  if (typeof value !== "string") return false;
  try {
    const provider = normalizePaymentProvider(value);
    return provider === "mppx" || provider === "x402";
  } catch {
    // Unknown provider: forward as-is and let the backend produce its error.
    return false;
  }
}

export async function prepareSignedReview(
  deps: SigningDeps,
  args: Record<string, unknown>,
  wallet: WalletOptions,
): Promise<PreparedReview> {
  if (!isMachineProvider(args.payment_provider) || args.review_signature) {
    return { args, signed: false };
  }

  const loaded = await loadWallet({
    walletDir: wallet.walletDir,
    autoCreate: wallet.autoCreate,
    env: wallet.env,
  });
  if (loaded.source === "none" || loaded.account === null) {
    return {
      args,
      signed: false,
      wallet_source: "none",
      wallet_error: loaded.error,
      next_step: installWalletNextStep("review_service"),
    };
  }

  const payload = await getReviewSigningPayload(deps, {
    rating: args.rating as number,
    reason: String(args.reason ?? ""),
    payment_reference: String(args.payment_reference ?? ""),
    service_id: args.service_id as string | null | undefined,
    api_endpoint: args.api_endpoint as string | null | undefined,
    payment_provider: args.payment_provider as string | null | undefined,
    payment_target_ref: args.payment_target_ref as string | null | undefined,
    directory_slug: args.directory_slug as string | null | undefined,
  });
  if (payload.ok !== true || typeof payload.message !== "string") {
    // Forward unsigned; the backend rejection (plus its next_step) is more
    // actionable than failing locally with a half-built payload.
    return {
      args,
      signed: false,
      wallet_source: loaded.source,
      wallet_created: loaded.created,
      wallet_error: `could not build the signing payload locally: ${String(
        payload.reason ?? "unknown error",
      )}`,
    };
  }

  const signature = await loaded.account.signMessage({
    message: payload.message,
  });
  const identity = payload.identity as Record<string, unknown>;
  return {
    args: {
      ...args,
      // Pass the resolved identity verbatim so the backend rebuilds the exact
      // payload that was just signed.
      service_id: identity.service_id ?? args.service_id,
      api_endpoint: identity.api_endpoint ?? args.api_endpoint,
      payment_provider: identity.payment_provider ?? args.payment_provider,
      payment_target_ref: identity.payment_target_ref ?? args.payment_target_ref,
      directory_slug: identity.directory_slug ?? args.directory_slug,
      reviewer_wallet: loaded.address,
      review_signature: signature,
      signature_scheme: "eip191",
    },
    signed: true,
    wallet_source: loaded.source,
    wallet_created: loaded.created,
    wallet: loaded,
  };
}

/**
 * Handle the backend's signature-mismatch recovery contract: re-sign the
 * returned expected_message with the same wallet and rebuild the args using
 * resolved_identity. Returns null when the response is not a recoverable
 * mismatch (or we did not sign locally in the first place).
 */
export async function resignFromMismatch(
  prepared: PreparedReview,
  response: Record<string, unknown>,
): Promise<Record<string, unknown> | null> {
  if (!prepared.signed || !prepared.wallet?.account) return null;
  if (response.accepted !== false) return null;
  if (typeof response.expected_message !== "string") return null;

  const identity = (response.resolved_identity ?? {}) as Record<string, unknown>;
  const signature = await prepared.wallet.account.signMessage({
    message: response.expected_message,
  });
  return {
    ...prepared.args,
    service_id: identity.service_id ?? prepared.args.service_id,
    api_endpoint: identity.api_endpoint ?? prepared.args.api_endpoint,
    payment_provider: identity.payment_provider ?? prepared.args.payment_provider,
    payment_target_ref:
      identity.payment_target_ref ?? prepared.args.payment_target_ref,
    directory_slug: identity.directory_slug ?? prepared.args.directory_slug,
    review_signature: signature,
  };
}
