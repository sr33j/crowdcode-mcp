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
