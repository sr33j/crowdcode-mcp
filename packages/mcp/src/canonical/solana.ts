/**
 * Solana pieces of the review contract (mirrors src/crowdcode/solana.py and
 * payer_link_message in src/crowdcode/payments.py; spec/CANONICAL_PAYLOAD.md).
 *
 * Solana identifiers are case-sensitive base58 and are never lowercased.
 */

import { createHash } from "node:crypto";

const ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
const BASE58_RE = /^[1-9A-HJ-NP-Za-km-z]+$/;

export function base58Decode(value: string): Uint8Array | null {
  if (typeof value !== "string" || !BASE58_RE.test(value)) return null;
  let n = 0n;
  for (const char of value) n = n * 58n + BigInt(ALPHABET.indexOf(char));
  const body: number[] = [];
  while (n > 0n) {
    body.unshift(Number(n & 0xffn));
    n >>= 8n;
  }
  const zeros = value.length - value.replace(/^1+/, "").length;
  return Uint8Array.from([...new Array<number>(zeros).fill(0), ...body]);
}

export function base58Encode(bytes: Uint8Array): string {
  let n = 0n;
  for (const byte of bytes) n = (n << 8n) | BigInt(byte);
  let out = "";
  while (n > 0n) {
    out = ALPHABET[Number(n % 58n)] + out;
    n /= 58n;
  }
  let zeros = 0;
  while (zeros < bytes.length && bytes[zeros] === 0) zeros++;
  return "1".repeat(zeros) + out;
}

function decodesTo(value: unknown, size: number): boolean {
  if (typeof value !== "string") return false;
  return base58Decode(value.trim())?.length === size;
}

/** A base58 32-byte public key. */
export const isSolanaAddress = (value: unknown): boolean => decodesTo(value, 32);
/** A base58 64-byte ed25519 signature (also a Solana transaction id). */
export const isSolanaSignature = (value: unknown): boolean => decodesTo(value, 64);

/**
 * The transaction signature named by `x402:solana:<sig>` (prefix
 * case-insensitive) or a bare base58 signature; null otherwise.
 */
export function solanaReferenceSignature(reference: unknown): string | null {
  if (typeof reference !== "string") return null;
  let cleaned = reference.trim();
  if (cleaned.slice(0, 12).toLowerCase() === "x402:solana:") cleaned = cleaned.slice(12);
  return isSolanaSignature(cleaned) ? cleaned : null;
}

export const PAYER_LINK_MESSAGE_HEADER = "CrowdCode review payer link v1";

/**
 * The text a Solana payer signs (ed25519 over its raw UTF-8 bytes; the
 * signature is sent base58 as `payer_signature`) to link its payment to a
 * review. It commits to the exact canonical review payload the reviewer
 * signed and to the reviewer's EVM wallet.
 */
export function payerLinkMessage(reviewerWallet: string, payload: string): string {
  return [
    PAYER_LINK_MESSAGE_HEADER,
    `reviewer_wallet:${reviewerWallet.trim().toLowerCase()}`,
    `payload_sha256:${createHash("sha256").update(payload, "utf8").digest("hex")}`,
  ].join("\n");
}
