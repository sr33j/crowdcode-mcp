import { mkdtemp, readFile, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { privateKeyToAccount } from "viem/accounts";
import { verifyMessage } from "viem";
import { describe, expect, it } from "vitest";
import { RedactionEngine } from "@crowdcode/redaction";
import { Preferences } from "../src/preferences.js";
import { createToolHandlers } from "../src/server.js";
import { managementMessage } from "../src/tools/manage-reviews.js";

const KEY = `0x${"42".repeat(32)}` as const;
const account = privateKeyToAccount(KEY);
const unpack = (result: any) => JSON.parse(result.content[0].text);

async function fixture(autoCreate = true) {
  const dir = await mkdtemp(join(tmpdir(), "crowdcode-management-"));
  const preferences = new Preferences(join(dir, "preferences.json"));
  const calls: { name: string; args: Record<string, unknown> }[] = [];
  const engine = await RedactionEngine.create({ cacheDir: dir, enableModel: false });
  const handlers = createToolHandlers({ engine, preferences,
    wallet: { walletDir: dir, autoCreate, env: {} },
    upstream: { listToolNames: async () => [], call: async (name, args) => {
      calls.push({ name, args }); return { ok: true, accepted: true };
    } },
  });
  return { dir, preferences, handlers, calls };
}

describe("CrowdCode controls", () => {
  it("keeps session overrides isolated and persists defaults across instances", async () => {
    const { dir, preferences } = await fixture();
    const other = new Preferences(join(dir, "preferences.json"));
    await preferences.set(false);
    expect((await preferences.status()).enabled).toBe(false);
    expect((await other.status()).enabled).toBe(true);
    await other.set(false, "default");
    await preferences.set(true);
    expect((await preferences.status()).enabled).toBe(true);
    expect((await new Preferences(join(dir, "preferences.json")).status()).enabled).toBe(false);
    await preferences.set(true, "default");
    expect((await other.status()).enabled).toBe(true);
  });

  it("blocks every automatic operation before signing, wallet creation, or network use", async () => {
    const { dir, handlers, calls } = await fixture();
    await handlers.set_crowdcode_enabled({ enabled: false });
    const results = await Promise.all([
      handlers.request_service({ service_description: "A service" }),
      handlers.get_service_score({ service_id: "svc_1" }),
      handlers.review_service({ rating: 5, reason: "Good" }),
      handlers.get_review_signing_payload({ rating: 5, reason: "Good", auto_sign: true }),
    ]);
    expect(results.map(result => unpack(result).status)).toEqual(Array(4).fill("disabled"));
    expect(calls).toEqual([]);
    await expect(stat(join(dir, "wallet.json"))).rejects.toMatchObject({ code: "ENOENT" });
    await handlers.set_crowdcode_enabled({ enabled: true });
    await handlers.get_service_score({ service_id: "svc_1" });
    expect(calls.map(call => call.name)).toEqual(["get_service_score"]);
  });

  it("fails closed for malformed preferences", async () => {
    const { dir, handlers, calls } = await fixture();
    await writeFile(join(dir, "preferences.json"), "broken");
    expect(unpack(await handlers.request_service({})).error_code).toBe("preferences_unavailable");
    expect(calls).toEqual([]);
  });

  it("creates an unfunded identity for requests", async () => {
    const { dir, handlers, calls } = await fixture();
    await handlers.request_service({ service_description: "Scanned reports to reconciled CSV" });
    const stored = JSON.parse(await readFile(join(dir, "wallet.json"), "utf8"));
    expect(calls[0]!.args.requester_wallet).toBe(stored.address);
    expect(calls).toHaveLength(1);
  });

  it("honors the wallet creation opt-out for requests", async () => {
    const { dir, handlers, calls } = await fixture(false);
    await handlers.request_service({ service_description: "Scanned reports to reconciled CSV" });
    await expect(stat(join(dir, "wallet.json"))).rejects.toMatchObject({ code: "ENOENT" });
    expect(calls[0]!.args.requester_wallet).toBeUndefined();
  });
});

describe("review management", () => {
  it("matches the Python authorization messages and signatures", async () => {
    const vectors = JSON.parse(await readFile(fileURLToPath(new URL("../../../spec/review-management-vectors.json", import.meta.url)), "utf8"));
    for (const vector of vectors) {
      expect(managementMessage(vector.args)).toBe(vector.message);
      expect(await account.signMessage({ message: managementMessage(vector.args) })).toBe(vector.signature);
    }
  });
  it("signs narrow ownership proofs locally, including while off", async () => {
    const { dir, handlers, calls } = await fixture();
    await writeFile(join(dir, "wallet.json"), JSON.stringify({ privateKey: KEY,
      address: account.address, createdAt: new Date().toISOString() }));
    await handlers.set_crowdcode_enabled({ enabled: false });
    await handlers.list_my_reviews({ before_id: 87, limit: 10 });
    await handlers.delete_my_review({ review_id: 42 });
    expect(calls.map(call => call.name)).toEqual(["list_my_reviews", "delete_my_review"]);
    for (const call of calls) {
      const a = call.args;
      const message = managementMessage({ action: call.name === "list_my_reviews" ? "list" : "delete",
        wallet: account.address, expires_at: a.expires_at as number,
        review_id: a.review_id as number, before_id: a.before_id as number, limit: a.limit as number });
      expect(await verifyMessage({ address: account.address, message, signature: a.authorization as `0x${string}` })).toBe(true);
      expect(await verifyMessage({ address: account.address, message: message.replace("review_id:42", "review_id:43") + "tampered", signature: a.authorization as `0x${string}` })).toBe(false);
      expect(JSON.stringify(a)).not.toContain(KEY);
    }
  });

  it("never invents a wallet to access existing reviews", async () => {
    const { dir, handlers, calls } = await fixture();
    expect(unpack(await handlers.list_my_reviews({})).error_code).toBe("wallet_unavailable");
    expect(unpack(await handlers.delete_my_review({ review_id: 1 })).error_code).toBe("wallet_unavailable");
    expect(calls).toEqual([]);
    await expect(stat(join(dir, "wallet.json"))).rejects.toMatchObject({ code: "ENOENT" });
  });
});
