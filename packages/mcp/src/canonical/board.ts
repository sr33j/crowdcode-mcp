/**
 * Local construction of the crowdcode.post.v1 / crowdcode.comment.v1 signing
 * payloads. Byte-for-byte port of src/crowdcode/board.py; conformance
 * enforced against spec/board-payload-vectors.json.
 *
 * Same rules as the review payload: Python-canonical JSON, text entering the
 * payload only as a sha256 hash of the (already redacted) text, EIP-191
 * personal-message signing. Post ids are content-addressed:
 * `post_` + sha256(canonical payload)[:20].
 */

import { createHash } from "node:crypto";
import { pythonCanonicalJson } from "./json.js";
import { pyStrip } from "./pystrip.js";

export const POST_PAYLOAD_TYPE = "crowdcode.post.v1";
export const COMMENT_PAYLOAD_TYPE = "crowdcode.comment.v1";

const BOUNTY_RE = /^[0-9]+(\.[0-9]{1,6})?$/;
const MAX_BOUNTY_USD = 1_000_000;

export function textHash(text: string): string {
  return (
    "sha256:" + createHash("sha256").update(pyStrip(text), "utf8").digest("hex")
  );
}

/**
 * Canonical decimal-string form of a stated USDC amount. String-based on
 * purpose (mirrors board.py canonical_bounty_amount exactly): validate
 * ^[0-9]+(\.[0-9]{1,6})?$, drop leading zeros in the integer part and
 * trailing zeros in the fraction. "0" is a valid statement (an upvote).
 */
export function canonicalBountyAmount(
  value: string | number | null | undefined,
): string | null {
  if (value === null || value === undefined) return null;
  const raw = typeof value === "number" ? String(value) : value;
  const cleaned = raw.trim();
  if (cleaned === "") return null;
  if (!BOUNTY_RE.test(cleaned)) {
    throw new Error(
      "bounty_amount must be a decimal USDC string like '5' or '0.25' " +
        "(up to 6 decimal places)",
    );
  }
  let integer = cleaned;
  let fraction = "";
  if (cleaned.includes(".")) {
    [integer, fraction] = cleaned.split(".", 2) as [string, string];
    fraction = fraction.replace(/0+$/, "");
  }
  integer = integer.replace(/^0+/, "") || "0";
  if (Number.parseInt(integer, 10) > MAX_BOUNTY_USD) {
    throw new Error(`bounty_amount must be at most ${MAX_BOUNTY_USD} USDC`);
  }
  return fraction ? `${integer}.${fraction}` : integer;
}

export function canonicalBoardPayload(args: {
  wallet: string;
  text: string;
  bountyAmount: string | null;
  timestamp: string;
  nonce: string;
  parentPostId?: string | null;
}): string {
  const parent = args.parentPostId ?? null;
  const payload: Record<string, string | number | null> = {
    type: parent ? COMMENT_PAYLOAD_TYPE : POST_PAYLOAD_TYPE,
    wallet: args.wallet.toLowerCase(),
    text_hash: textHash(args.text),
    bounty_amount: args.bountyAmount,
    timestamp: args.timestamp,
    nonce: args.nonce,
  };
  if (parent) payload.parent_post_id = parent;
  return pythonCanonicalJson(payload);
}

export function postIdFromPayload(payload: string): string {
  return (
    "post_" +
    createHash("sha256").update(payload, "utf8").digest("hex").slice(0, 20)
  );
}

/** UTC second-precision timestamp in the exact form the payload requires. */
export function boardTimestamp(date: Date = new Date()): string {
  return date.toISOString().replace(/\.\d{3}Z$/, "Z");
}
