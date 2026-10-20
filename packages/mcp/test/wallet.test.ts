import { mkdtemp, readFile, stat, writeFile, mkdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { privateKeyToAccount } from "viem/accounts";
import { beforeEach, describe, expect, it } from "vitest";
import { createPublicKey, verify } from "node:crypto";
import { base58Decode, base58Encode } from "../src/canonical/solana.js";
import { loadSolanaWallet, loadWallet, resetWalletCache } from "../src/wallet.js";

const KEY = ("0x" + "42".repeat(32)) as `0x${string}`;
const ACCOUNT = privateKeyToAccount(KEY);

async function tempDir(): Promise<string> {
  return mkdtemp(join(tmpdir(), "cc-wallet-"));
}

function validStored(): string {
  return JSON.stringify(
    { privateKey: KEY, address: ACCOUNT.address, createdAt: "2026-01-01T00:00:00Z" },
    null,
    2,
  );
}

describe("loadWallet", () => {
  beforeEach(() => resetWalletCache());

  it("rejects a configured env key without reading or replacing the wallet file", async () => {
    const dir = await tempDir();
    await mkdir(dir, { recursive: true });
    const file = join(dir, "wallet.json");
    await writeFile(file, "not json at all");
    const before = await readFile(file, "utf8");

    const wallet = await loadWallet({
      env: { X402_PRIVATE_KEY: KEY } as NodeJS.ProcessEnv,
      walletDir: dir,
      autoCreate: true,
    });
    expect(wallet.source).toBe("none");
    expect(wallet.address).toBeNull();
    expect(wallet.account).toBeNull();
    expect(wallet.errorCode).toBe("wallet_configuration_error");
    expect(wallet.error).toContain("X402_PRIVATE_KEY is no longer supported");
    expect(await readFile(file, "utf8")).toBe(before);
  });

  it("malformed env key is an error, not a fallthrough to the file", async () => {
    const dir = await tempDir();
    await writeFile(join(dir, "wallet.json"), validStored());
    const wallet = await loadWallet({
      env: { X402_PRIVATE_KEY: "0x1234" } as NodeJS.ProcessEnv,
      walletDir: dir,
    });
    expect(wallet.source).toBe("none");
    expect(wallet.error).toContain("X402_PRIVATE_KEY");
    expect(wallet.errorCode).toBe("wallet_configuration_error");
  });

  it("loads a valid agentcash wallet file", async () => {
    const dir = await tempDir();
    await writeFile(join(dir, "wallet.json"), validStored());
    const wallet = await loadWallet({ env: {} as NodeJS.ProcessEnv, walletDir: dir });
    expect(wallet.source).toBe("agentcash");
    expect(wallet.address).toBe(ACCOUNT.address);
    expect(wallet.account).not.toBeNull();
  });

  it("never overwrites an invalid file, even with autoCreate", async () => {
    const dir = await tempDir();
    const file = join(dir, "wallet.json");
    await writeFile(file, '{"broken": true}');
    const before = await readFile(file, "utf8");

    const wallet = await loadWallet({
      env: {} as NodeJS.ProcessEnv,
      walletDir: dir,
      autoCreate: true,
    });
    expect(wallet.source).toBe("none");
    expect(wallet.error).toContain("wallet file");
    expect(await readFile(file, "utf8")).toBe(before);
  });

  it("rejects a file whose address does not match its key", async () => {
    const dir = await tempDir();
    const other = privateKeyToAccount(("0x" + "43".repeat(32)) as `0x${string}`);
    await writeFile(
      join(dir, "wallet.json"),
      JSON.stringify({
        privateKey: KEY,
        address: other.address,
        createdAt: "2026-01-01T00:00:00Z",
      }),
    );
    const wallet = await loadWallet({ env: {} as NodeJS.ProcessEnv, walletDir: dir });
    expect(wallet.source).toBe("none");
    expect(wallet.error).toContain("does not match");
  });

  it("autoCreate writes an agentcash-shaped 0600 file and reloads it", async () => {
    const dir = await tempDir();
    const created = await loadWallet({
      env: {} as NodeJS.ProcessEnv,
      walletDir: dir,
      autoCreate: true,
    });
    expect(created.source).toBe("agentcash");
    expect(created.created).toBe(true);
    expect(created.address).toMatch(/^0x[a-fA-F0-9]{40}$/);

    const file = join(dir, "wallet.json");
    if (process.platform !== "win32") {
      // Windows has no POSIX permission bits; stat always reports 0o666.
      const mode = (await stat(file)).mode & 0o777;
      expect(mode).toBe(0o600);
    }
    const stored = JSON.parse(await readFile(file, "utf8"));
    expect(stored.privateKey).toMatch(/^0x[a-fA-F0-9]{64}$/);
    expect(stored.address).toBe(created.address);
    expect(typeof stored.createdAt).toBe("string");

    const reloaded = await loadWallet({
      env: {} as NodeJS.ProcessEnv,
      walletDir: dir,
      autoCreate: true,
    });
    expect(reloaded.source).toBe("agentcash");
    expect(reloaded.created).toBeUndefined();
    expect(reloaded.address).toBe(created.address);
  });

  it("autoCreate=false writes nothing when the file is missing", async () => {
    const dir = await tempDir();
    const wallet = await loadWallet({
      env: {} as NodeJS.ProcessEnv,
      walletDir: dir,
      autoCreate: false,
    });
    expect(wallet.source).toBe("none");
    await expect(stat(join(dir, "wallet.json"))).rejects.toThrow();
  });
});

describe("loadSolanaWallet", () => {
  // agentcash's svm wallet shape: base58 of the 32-byte ed25519 seed.
  const SEED = Uint8Array.from({ length: 32 }, (_, i) => i);
  const ADDRESS = "FAe4sisG95oZ42w7buUn5qEE4TAnfTTFPiguZUHmhiF"; // spec/review-payload-vectors.json

  async function withFile(contents: unknown): Promise<string> {
    const dir = await tempDir();
    await writeFile(join(dir, "solana-wallet.json"), JSON.stringify(contents));
    return dir;
  }

  it("loads the agentcash Solana wallet and signs ed25519 in base58", async () => {
    const dir = await withFile({ privateKey: base58Encode(SEED), address: ADDRESS, createdAt: "2026-01-01" });
    const wallet = await loadSolanaWallet(dir);
    expect(wallet?.address).toBe(ADDRESS);
    const signature = base58Decode(wallet!.sign("hello"))!;
    const publicKey = createPublicKey({
      key: Buffer.concat([Buffer.from("302a300506032b6570032100", "hex"), base58Decode(ADDRESS)!]),
      format: "der",
      type: "spki",
    });
    expect(verify(null, Buffer.from("hello"), publicKey, signature)).toBe(true);
  });

  it("accepts a 64-byte seed+pubkey secret", async () => {
    const secret = Uint8Array.from([...SEED, ...base58Decode(ADDRESS)!]);
    const dir = await withFile({ privateKey: base58Encode(secret), address: ADDRESS });
    expect((await loadSolanaWallet(dir))?.address).toBe(ADDRESS);
  });

  it("returns null for missing, malformed, or mismatched files and never creates one", async () => {
    const empty = await tempDir();
    expect(await loadSolanaWallet(empty)).toBeNull();
    await expect(stat(join(empty, "solana-wallet.json"))).rejects.toThrow();
    expect(await loadSolanaWallet(await withFile({ privateKey: "0OIl", address: ADDRESS }))).toBeNull();
    expect(await loadSolanaWallet(await withFile({
      privateKey: base58Encode(SEED), address: "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v" }))).toBeNull();
    const dir = await tempDir();
    await writeFile(join(dir, "solana-wallet.json"), "not json");
    expect(await loadSolanaWallet(dir)).toBeNull();
  });
});
