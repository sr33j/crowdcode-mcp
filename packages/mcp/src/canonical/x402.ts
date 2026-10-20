/**
 * x402 settlement references (mirrors X402_CHAINS in
 * src/crowdcode/payments.py). The backend checks a bare transaction hash on
 * Base; a settlement on another chain is referenced as
 * `x402:<network>:<tx>`, e.g. `x402:monad:0x…` for Monad (eip155:143) or
 * `x402:solana:<base58 signature>` for Solana (case-sensitive).
 */

import { isSolanaSignature } from "./solana.js";

const X402_NETWORKS: Record<string, string> = {
  base: "base",
  "eip155:8453": "base",
  "8453": "base",
  monad: "monad",
  "eip155:143": "monad",
  "143": "monad",
  solana: "solana",
  // Lowercased, like every lookup key here.
  "solana:5eykt4usfv8p8njdtrepy1vzqkqzkvdp": "solana",
};

const DEFAULT_X402_NETWORK = "base";
const TX_HASH_RE = /^0x[0-9a-fA-F]{64}$/;

/** Short x402 network name for a name, CAIP-2 id, or chain id; else null. */
export function x402Network(value: unknown): string | null {
  if (typeof value !== "string" && typeof value !== "number") return null;
  return X402_NETWORKS[String(value).trim().toLowerCase()] ?? null;
}

function parseProof(proof: unknown): Record<string, unknown> | null {
  if (typeof proof === "object" && proof !== null && !Array.isArray(proof)) {
    return proof as Record<string, unknown>;
  }
  if (typeof proof !== "string" || proof.trim() === "") return null;
  const cleaned = proof.trim();
  for (const text of [cleaned, Buffer.from(cleaned, "base64").toString("utf8")]) {
    try {
      const parsed: unknown = JSON.parse(text);
      if (typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)) {
        return parsed as Record<string, unknown>;
      }
    } catch {
      // Try the next encoding.
    }
  }
  return null;
}

/**
 * The settlement named by an x402 payment-response proof (raw JSON, base64
 * header value, or object): its transaction hash and short network name.
 * A proof without a network is Base, as on the backend.
 */
export function x402Settlement(
  proof: unknown,
): { transaction: string; network: string } | null {
  const parsed = parseProof(proof);
  if (parsed === null) return null;
  const transaction =
    parsed.transaction ?? parsed.transactionHash ?? parsed.txHash ?? parsed.reference;
  const rawNetwork = parsed.network ?? parsed.chain;
  const network =
    rawNetwork === undefined || rawNetwork === null
      ? DEFAULT_X402_NETWORK
      : x402Network(rawNetwork);
  if (typeof transaction !== "string" || network === null) return null;
  const valid = network === "solana" ? isSolanaSignature(transaction) : TX_HASH_RE.test(transaction);
  return valid ? { transaction, network } : null;
}

/**
 * The payment_reference to sign for an x402 review. A non-Base settlement
 * gets its `x402:<network>:` prefix so the reference alone names the chain;
 * a missing reference is taken from the proof. Anything else (Base, a
 * mismatched hash, an unparseable proof) is returned unchanged for the
 * backend to judge.
 */
export function x402PaymentReference(
  reference: string | null | undefined,
  proof: unknown,
): string | null | undefined {
  const settlement = x402Settlement(proof);
  if (settlement === null) return reference;
  const { transaction, network } = settlement;
  const prefix = network === DEFAULT_X402_NETWORK ? "" : `x402:${network}:`;
  if (reference === null || reference === undefined) return prefix + transaction;
  const bare = reference.trim();
  // Base58 signatures are case-sensitive; EVM hashes are not.
  const same = network === "solana" ? bare === transaction : bare.toLowerCase() === transaction.toLowerCase();
  if (prefix !== "" && same) return prefix + bare;
  return reference;
}

/** The `payer` an x402 payment-response proof names, if any (unverified hint). */
export function x402ProofPayer(proof: unknown): string | null {
  const payer = parseProof(proof)?.payer;
  return typeof payer === "string" && payer.trim() !== "" ? payer.trim() : null;
}
