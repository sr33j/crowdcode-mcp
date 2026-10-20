/**
 * Local signing wallet, shared with agentcash.
 *
 * Reads (and lazily creates) the same wallet file agentcash uses —
 * ~/.agentcash/wallet.json — so reviews are signed by the identical identity
 * that pays for x402/mppx services. Private keys supplied through environment
 * variables are deliberately unsupported: the agent gets addresses and
 * canonical review signatures, never a key-ingestion API.
 *   1. An existing file is zod-validated and its address cross-checked
 *      against the key; an unreadable or invalid file is NEVER overwritten.
 *   2. A missing file is created on demand (opt-out via
 *      CROWDCODE_DISABLE_WALLET_CREATE) with agentcash's exact shape and
 *      0600 permissions, so a later agentcash install picks up the same
 *      wallet.
 */

import { createPrivateKey, createPublicKey, sign as cryptoSign } from "node:crypto";
import { chmod, mkdir, readFile, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";
import {
  generatePrivateKey,
  privateKeyToAccount,
  type PrivateKeyAccount,
} from "viem/accounts";
import { getAddress } from "viem/utils";
import { z } from "zod";
import { base58Decode, base58Encode } from "./canonical/solana.js";

export type WalletSource = "agentcash" | "none";

export interface LoadedWallet {
  source: WalletSource;
  account: PrivateKeyAccount | null;
  address: string | null;
  error?: string;
  errorCode?: string;
  created?: boolean;
}

export interface LoadWalletOptions {
  env?: NodeJS.ProcessEnv;
  walletDir?: string;
  autoCreate?: boolean;
}

const PRIVATE_KEY_RE = /^0x[a-fA-F0-9]{64}$/;

// Same shape agentcash's storedWalletSchema validates (src/wallet/evm.ts).
const storedWalletSchema = z.object({
  privateKey: z.string().regex(PRIVATE_KEY_RE),
  address: z.string().regex(/^0x[a-fA-F0-9]{40}$/),
  createdAt: z.string(),
});

export function defaultWalletDir(): string {
  return join(homedir(), ".agentcash");
}

let memo: LoadedWallet | null = null;

/** Test hook: clear the process-level memo. */
export function resetWalletCache(): void {
  memo = null;
}

export async function loadWallet(
  opts: LoadWalletOptions = {},
): Promise<LoadedWallet> {
  const isDefault = opts.env === undefined && opts.walletDir === undefined;
  if (isDefault && memo) return memo;

  const result = await load(
    opts.env ?? process.env,
    opts.walletDir ?? defaultWalletDir(),
    opts.autoCreate ?? false,
  );
  // Memoize only usable wallets: a "none" result must stay retryable (a later
  // call with autoCreate, or after the user installs agentcash, should see
  // the new state). `created` is dropped from the memo so only the call that
  // actually wrote the file reports it.
  if (isDefault && result.source !== "none") {
    memo = { ...result, created: undefined };
  }
  return result;
}

async function load(
  env: NodeJS.ProcessEnv,
  dir: string,
  autoCreate: boolean,
): Promise<LoadedWallet> {
  if (Object.prototype.hasOwnProperty.call(env, "X402_PRIVATE_KEY")) {
    return none(
      "X402_PRIVATE_KEY is no longer supported; remove it from the CrowdCode " +
        "process and use the local agentcash wallet file instead",
      "wallet_configuration_error",
    );
  }

  const file = join(dir, "wallet.json");
  let raw: string | null = null;
  try {
    raw = await readFile(file, "utf8");
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code !== "ENOENT") {
      return none(`wallet file is unreadable: ${(err as Error).message}`);
    }
  }

  if (raw !== null) {
    return parseStored(raw, file);
  }

  if (!autoCreate) {
    return none();
  }

  const privateKey = generatePrivateKey();
  const account = privateKeyToAccount(privateKey);
  const stored = {
    privateKey,
    address: account.address,
    createdAt: new Date().toISOString(),
  };
  try {
    await mkdir(dir, { recursive: true, mode: 0o700 });
    // "wx" fails on a concurrently created file instead of clobbering it.
    await writeFile(file, JSON.stringify(stored, null, 2), {
      mode: 0o600,
      flag: "wx",
    });
    await chmod(file, 0o600);
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === "EEXIST") {
      try {
        return parseStored(await readFile(file, "utf8"), file);
      } catch (readErr) {
        return none(`wallet file is unreadable: ${(readErr as Error).message}`);
      }
    }
    return none(`could not create wallet file: ${(err as Error).message}`);
  }
  return { source: "agentcash", account, address: account.address, created: true };
}

function parseStored(raw: string, file: string): LoadedWallet {
  let parsedJson: unknown;
  try {
    parsedJson = JSON.parse(raw);
  } catch {
    return none(`wallet file is not valid JSON: ${file} — fix or remove it`);
  }
  const parsed = storedWalletSchema.safeParse(parsedJson);
  if (!parsed.success) {
    return none(
      `wallet file does not match the agentcash wallet shape: ${file} — ` +
        "fix or remove it",
    );
  }
  const account = privateKeyToAccount(parsed.data.privateKey as `0x${string}`);
  // Defense against a swapped/corrupted file: the stored address must be the
  // one the stored key derives.
  if (getAddress(account.address) !== getAddress(parsed.data.address)) {
    return none(
      `wallet file address does not match its private key: ${file} — ` +
        "fix or remove it",
    );
  }
  return { source: "agentcash", account, address: getAddress(parsed.data.address) };
}

function none(error?: string, errorCode?: string): LoadedWallet {
  return { source: "none", account: null, address: null, error, errorCode };
}

/**
 * agentcash's Solana wallet, ~/.agentcash/solana-wallet.json (agentcash
 * src/wallet/svm.ts): `{ privateKey: base58(32-byte ed25519 seed), address,
 * createdAt }`. agentcash pays x402 Solana with this key, so it can sign the
 * payer link that makes a Solana review verified. Read-only: CrowdCode never
 * creates a Solana key, and a missing or invalid file just means reviews go
 * out without payer_signature.
 */
export interface SolanaWallet {
  address: string;
  /** ed25519 over the UTF-8 bytes of `message`; base58 signature. */
  sign(message: string): string;
}

const solanaWalletSchema = z.object({
  privateKey: z.string().regex(/^[1-9A-HJ-NP-Za-km-z]+$/),
  address: z.string(),
  createdAt: z.string().optional(),
});

// PKCS#8 DER prefix for a raw 32-byte Ed25519 seed (RFC 8410).
const ED25519_PKCS8_PREFIX = Buffer.from("302e020100300506032b657004220420", "hex");

export async function loadSolanaWallet(walletDir?: string): Promise<SolanaWallet | null> {
  let raw: string;
  try {
    raw = await readFile(join(walletDir ?? defaultWalletDir(), "solana-wallet.json"), "utf8");
  } catch {
    return null;
  }
  let parsed: z.infer<typeof solanaWalletSchema>;
  try {
    parsed = solanaWalletSchema.parse(JSON.parse(raw));
  } catch {
    return null;
  }
  const secret = base58Decode(parsed.privateKey);
  // agentcash stores the 32-byte seed; Solana CLI keypairs are seed + pubkey.
  if (secret === null || (secret.length !== 32 && secret.length !== 64)) return null;
  const key = createPrivateKey({
    key: Buffer.concat([ED25519_PKCS8_PREFIX, Buffer.from(secret.subarray(0, 32))]),
    format: "der",
    type: "pkcs8",
  });
  const jwk = createPublicKey(key).export({ format: "jwk" });
  const address = base58Encode(Buffer.from(String(jwk.x), "base64url"));
  // Same defense as the EVM file: the stored address must derive from the key.
  if (address !== parsed.address.trim()) return null;
  if (secret.length === 64 && base58Encode(secret.subarray(32)) !== address) return null;
  return {
    address,
    sign: (message) => base58Encode(cryptoSign(null, Buffer.from(message, "utf8"), key)),
  };
}
