/**
 * Transparent in-process signing for make_post / comment_on_post.
 *
 * The board write is redacted locally, canonicalized locally
 * (canonical/board.ts), and signed with the local agentcash wallet — the
 * same no-signing-oracle rule as reviews: the server only ever verifies a
 * payload it rebuilds itself. The signature covers the REDACTED text; the
 * redaction engine memoizes, so the per-forward redaction pass produces the
 * byte-identical string the backend hashes when rebuilding the payload.
 */

import { randomBytes } from "node:crypto";
import type { RedactionResult } from "@crowdcode/redaction";
import {
  boardTimestamp,
  canonicalBoardPayload,
  canonicalBountyAmount,
} from "../canonical/board.js";
import { loadWallet, type WalletSource } from "../wallet.js";
import { installWalletNextStep, type NextStep, type WalletOptions } from "./sign-review.js";

export interface BoardWriteDeps {
  redact(text: string): Promise<RedactionResult>;
}

export interface PreparedBoardWrite {
  ok: boolean;
  args?: Record<string, unknown>;
  wallet_source?: WalletSource;
  wallet_created?: boolean;
  error?: Record<string, unknown>;
  next_step?: NextStep;
}

export async function prepareSignedBoardWrite(
  deps: BoardWriteDeps,
  input: {
    tool: "make_post" | "comment_on_post";
    text: unknown;
    bounty_amount?: unknown;
    post_id?: unknown;
  },
  walletOptions: WalletOptions,
): Promise<PreparedBoardWrite> {
  const text = typeof input.text === "string" ? input.text : "";
  if (text.trim() === "") {
    return {
      ok: false,
      error: {
        status: "rejected",
        error_code: "board_input_invalid",
        retryable: false,
        accepted: false,
        reason: "text is required",
      },
    };
  }

  let bounty: string | null;
  try {
    bounty = canonicalBountyAmount(
      input.bounty_amount as string | number | null | undefined,
    );
  } catch (err) {
    return {
      ok: false,
      error: {
        status: "rejected",
        error_code: "board_input_invalid",
        retryable: false,
        accepted: false,
        reason: (err as Error).message,
      },
    };
  }

  const wallet = await loadWallet(walletOptions);
  if (wallet.errorCode === "wallet_configuration_error") {
    return {
      ok: false,
      error: {
        status: "rejected",
        error_code: wallet.errorCode,
        retryable: false,
        accepted: false,
        reason: wallet.error,
        wallet_source: "none",
      },
    };
  }
  if (wallet.source === "none" || wallet.account === null) {
    return {
      ok: false,
      error: {
        status: "rejected",
        error_code: "wallet_required",
        retryable: false,
        accepted: false,
        reason:
          "board writes are wallet-signed and no local signing wallet was found",
        wallet_source: "none",
        next_step: installWalletNextStep(input.tool),
      },
    };
  }

  const redacted = await deps.redact(text);
  const timestamp = boardTimestamp();
  const nonce = randomBytes(16).toString("hex");
  const parentPostId =
    input.tool === "comment_on_post" ? String(input.post_id ?? "") : null;
  const message = canonicalBoardPayload({
    wallet: wallet.address!,
    text: redacted.text,
    bountyAmount: bounty,
    timestamp,
    nonce,
    parentPostId,
  });
  const signature = await wallet.account.signMessage({ message });

  const args: Record<string, unknown> = {
    text: redacted.text,
    bounty_amount: bounty,
    wallet: wallet.address!.toLowerCase(),
    signature,
    timestamp,
    nonce,
  };
  if (parentPostId) args.post_id = parentPostId;
  return {
    ok: true,
    args,
    wallet_source: wallet.source,
    wallet_created: wallet.created,
  };
}
