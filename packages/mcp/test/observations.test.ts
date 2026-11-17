import { execFileSync } from "node:child_process";
import { mkdir, mkdtemp, readdir, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { recoverMessageAddress } from "viem";
import { generatePrivateKey, privateKeyToAccount } from "viem/accounts";
import { beforeEach, describe, expect, it } from "vitest";
import {
  OBSERVATION_FIELDS,
  flushPending,
  observationMessage,
  observationsUrl,
  type ObservationRecord,
} from "../src/observations.js";
import { resetWalletCache } from "../src/wallet.js";
import { SECRET_BODY, SECRET_QUERY, TX, postToolUse, x402Result } from "./fixtures/agentcash.js";

const HOOK = resolve(__dirname, "..", "hooks", "paid-call.cjs");

async function setup() {
  const root = await mkdtemp(join(tmpdir(), "crowdcode-obs-"));
  const dir = join(root, "crowdcode");
  const walletDir = join(root, "agentcash");
  await mkdir(walletDir, { recursive: true });
  const privateKey = generatePrivateKey();
  const account = privateKeyToAccount(privateKey);
  await writeFile(join(walletDir, "wallet.json"), JSON.stringify({ privateKey, address: account.address, createdAt: "2026-11-17" }));
  return { dir, walletDir, account };
}

function fakeBackend(responses: Array<{ status: number; body: Record<string, unknown> }>) {
  const sent: Array<{ url: string; body: any; raw: string }> = [];
  const fetchFn = (async (url: string, init: RequestInit) => {
    sent.push({ url, body: JSON.parse(String(init.body)), raw: String(init.body) });
    const next = responses.shift() ?? { status: 200, body: { accepted: true } };
    return new Response(JSON.stringify(next.body), { status: next.status, headers: { "content-type": "application/json" } });
  }) as unknown as typeof fetch;
  return { sent, fetchFn };
}

beforeEach(() => resetWalletCache());

describe("observation message", () => {
  it("matches the backend's canonical text (crowdcode.observations.observation_message)", () => {
    const record: ObservationRecord = { endpoint: "https://api.exa.ai/search", method: "POST", network: "base",
      payment_reference: TX, amount_atomic: 7000, status: null, response_bytes: 812, observed_at: "2026-11-17T00:00:00.000Z" };
    expect(observationMessage(record)).toBe([
      "CrowdCode observation v1", "endpoint:https://api.exa.ai/search", "method:POST", "network:base",
      `payment_reference:${TX}`, "amount_atomic:7000", "status:", "response_bytes:812",
      "observed_at:2026-11-17T00:00:00.000Z"].join("\n"));
  });
  it("derives the endpoint from the MCP backend URL", () => {
    expect(observationsUrl("https://crowdcode-backend.onrender.com/mcp", {})).toBe("https://crowdcode-backend.onrender.com/api/observations");
  });
});

describe("submitting pending paid calls", () => {
  it("sends exactly the eight signed fields and no request or response content", async () => {
    const { dir, walletDir, account } = await setup();
    // The real hook, fed a real AgentCash-shaped result for a call this wallet paid.
    const result = x402Result();
    const header = JSON.parse(result[1]!.text);
    header.headers["payment-response"] = Buffer.from(JSON.stringify({ success: true, transaction: TX,
      network: "eip155:8453", payer: account.address })).toString("base64");
    result[1]!.text = JSON.stringify(header);
    execFileSync(process.execPath, [HOOK], { input: JSON.stringify(postToolUse(result)), env: { ...process.env, CROWDCODE_HOME: dir } });

    const backend = fakeBackend([{ status: 200, body: { accepted: true, observation_id: 1 } }]);
    const outcome = await flushPending({ dir, walletDir, url: "https://backend.test/api/observations", fetch: backend.fetchFn });
    expect(outcome).toEqual({ sent: 1, duplicate: 0, retry: 0, dropped: 0 });
    const [{ body, raw, url }] = backend.sent;
    expect(url).toBe("https://backend.test/api/observations");
    expect(Object.keys(body).sort()).toEqual(["observation", "signature"]);
    expect(Object.keys(body.observation)).toEqual([...OBSERVATION_FIELDS]);
    expect(body.observation).toMatchObject({ endpoint: "https://api.exa.ai/search", method: "POST", network: "base",
      payment_reference: TX, amount_atomic: 7000 });
    for (const secret of [SECRET_QUERY, "acme", "Acme", SECRET_BODY, "CONFIDENTIAL", "agentcash__fetch", "results"]) {
      expect(raw).not.toContain(secret);
    }
    const signer = await recoverMessageAddress({ message: observationMessage(body.observation), signature: body.signature });
    expect(signer).toBe(account.address);
    // Delivered lines leave the queue.
    expect(await readdir(dir)).not.toContain("pending.jsonl");
  });

  it("keeps retryable failures queued and drops permanent rejections", async () => {
    const { dir, walletDir, account } = await setup();
    await mkdir(dir, { recursive: true });
    const line = (ref: string) => JSON.stringify({ endpoint: "https://api.exa.ai/search", method: "POST", network: "base",
      payment_reference: ref, amount_atomic: null, status: 200, response_bytes: 10, observed_at: new Date().toISOString(),
      payer: account.address }) + "\n";
    await writeFile(join(dir, "pending.jsonl"), line("0x" + "01".repeat(32)) + line("0x" + "02".repeat(32)) + "torn{\n");
    const backend = fakeBackend([
      { status: 503, body: { accepted: false, retryable: true, error_code: "payment_not_found" } },
      { status: 422, body: { accepted: false, error_code: "payee_not_advertised" } },
    ]);
    const outcome = await flushPending({ dir, walletDir, url: "https://b/api/observations", fetch: backend.fetchFn });
    expect(outcome).toEqual({ sent: 0, duplicate: 0, retry: 1, dropped: 1 });
    const left = (await readFile(join(dir, "pending.jsonl"), "utf8")).trim().split("\n").map((l) => JSON.parse(l));
    expect(left).toHaveLength(1);
    expect(left[0]).toMatchObject({ payment_reference: "0x" + "01".repeat(32), attempts: 1 });
  });

  it("sends nothing while CrowdCode is off, or for a payment another wallet made", async () => {
    const { dir, walletDir } = await setup();
    await mkdir(dir, { recursive: true });
    await writeFile(join(dir, "pending.jsonl"), JSON.stringify({ endpoint: "https://a.example/x", method: null,
      network: "base", payment_reference: TX, amount_atomic: null, status: null, response_bytes: 1,
      observed_at: new Date().toISOString(), payer: "0x" + "99".repeat(20) }) + "\n");
    const backend = fakeBackend([]);
    expect(await flushPending({ dir, walletDir, url: "https://b", fetch: backend.fetchFn, enabled: async () => false }))
      .toEqual({ sent: 0, duplicate: 0, retry: 0, dropped: 0 });
    expect(await flushPending({ dir, walletDir, url: "https://b", fetch: backend.fetchFn }))
      .toEqual({ sent: 0, duplicate: 0, retry: 0, dropped: 1 });
    expect(backend.sent).toHaveLength(0);
  });
});
