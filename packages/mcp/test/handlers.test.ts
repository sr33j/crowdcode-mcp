import { createPublicKey, verify } from "node:crypto";
import { mkdtemp, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { privateKeyToAccount } from "viem/accounts";
import { verifyMessage } from "viem";
import { beforeEach, describe, expect, it } from "vitest";
import { getConfig } from "../src/config.js";
import { RedactionEngine } from "@crowdcode/redaction";
import { buildIdentity } from "../src/canonical/identity.js";
import { canonicalReviewPayload } from "../src/canonical/payload.js";
import { base58Decode, base58Encode, payerLinkMessage } from "../src/canonical/solana.js";
import { createToolHandlers } from "../src/server.js";
import type { Upstream } from "../src/upstream.js";
import { UpstreamError } from "../src/upstream.js";
import { resetWalletCache } from "../src/wallet.js";

function fakeUpstream(
  respond: (name: string, args: Record<string, unknown>) => Record<string, unknown>,
): Upstream & { calls: Array<{ name: string; args: Record<string, unknown> }> } {
  const calls: Array<{ name: string; args: Record<string, unknown> }> = [];
  return {
    calls,
    call: async (name, args) => {
      calls.push({ name, args });
      return respond(name, args);
    },
    listToolNames: async () => [],
  };
}

async function makeEngine(): Promise<RedactionEngine> {
  // Deterministic-only: no model download in tests.
  return RedactionEngine.create({
    cacheDir: getConfig().cacheDir,
    enableModel: false,
  });
}

describe("tool handlers", () => {
  it("redacts free text before forwarding request_service and attests", async () => {
    const engine = await makeEngine();
    const upstream = fakeUpstream(() => ({
      accepted: true,
      request_id: 1,
      directory_match: "missing",
    }));
    const handlers = createToolHandlers({ engine, upstream });

    const result = await handlers.request_service({
      service_description:
        "OCR service; my email is jane@corp.com and key sk-abcdefghij0123456789",
      task_context: "billing at jane@corp.com",
    });
    const payload = JSON.parse(result.content[0]!.text);

    const sent = upstream.calls[0]!.args;
    expect(sent.service_description).not.toContain("jane@corp.com");
    expect(sent.service_description).not.toContain("sk-abcdefghij0123456789");
    expect(sent.service_description).toContain("[EMAIL_1]");
    expect(sent.service_description).toContain("[API_KEY_1]");
    expect(sent.task_context).toContain("[EMAIL_");
    expect(payload.accepted).toBe(true);
    expect(payload._redaction.entities_removed).toBeGreaterThanOrEqual(3);
    expect(payload._redaction.model_active).toBe(false);
  });

  it("redacts reason identically to a prior signing call (memoization)", async () => {
    const engine = await makeEngine();
    const upstream = fakeUpstream((name) =>
      name === "get_service_score"
        ? { found: false, reason: "service not found" }
        : { accepted: true },
    );
    const handlers = createToolHandlers({ engine, upstream });

    const reason = "great, invoice sent to jane@corp.com";
    const signing = JSON.parse(
      (
        await handlers.get_review_signing_payload({
          rating: 5,
          reason,
          payment_reference: "ref-1",
          api_endpoint: "https://api.example.com/v1",
        })
      ).content[0]!.text,
    );

    await handlers.review_service({
      rating: 5,
      reason,
      payment_reference: "ref-1",
      api_endpoint: "https://api.example.com/v1",
    });
    const submitted = upstream.calls.find((c) => c.name === "review_service")!;
    expect(submitted.args.reason).toBe(signing.reason);
    expect(String(submitted.args.reason)).toContain("[EMAIL_1]");
  });

  it("redacts the find_service query and forwards ranking inputs", async () => {
    const engine = await makeEngine();
    const upstream = fakeUpstream(() => ({ ok: true, services: [{ rank: 1, endpoint: "https://s.example/a" }] }));
    const handlers = createToolHandlers({ engine, upstream });
    const result = await handlers.find_service({
      query: "token risk scan, email me at jane@corp.com",
      balances: { base: 1.5, solana: 0 },
      candidates: [{ endpoint: "https://a.example/x", source: "agentcash" }],
    });
    const sent = upstream.calls[0]!;
    expect(sent.name).toBe("find_service");
    expect(sent.args.query).not.toContain("jane@corp.com");
    expect(sent.args.balances).toEqual({ base: 1.5, solana: 0 });
    expect(sent.args.candidates).toEqual([{ endpoint: "https://a.example/x", source: "agentcash" }]);
    expect(JSON.parse(result.content[0]!.text).services[0].rank).toBe(1);
  });

  it("forwards get_service_score untouched", async () => {
    const engine = await makeEngine();
    const upstream = fakeUpstream(() => ({ found: true, avg_rating: 4.5 }));
    const handlers = createToolHandlers({ engine, upstream });
    const result = await handlers.get_service_score({
      api_endpoint: "https://api.example.com/v1",
    });
    expect(JSON.parse(result.content[0]!.text).avg_rating).toBe(4.5);
  });

  it("returns a structured error when the backend is down", async () => {
    const engine = await makeEngine();
    const upstream: Upstream = {
      call: async () => {
        throw new UpstreamError("backend tool request_service failed: boom");
      },
      listToolNames: async () => [],
    };
    const handlers = createToolHandlers({ engine, upstream });
    const payload = JSON.parse(
      (await handlers.request_service({ service_description: "x" })).content[0]!
        .text,
    );
    expect(payload.accepted).toBe(false);
    expect(payload.status).toBe("unavailable");
    expect(payload.error_code).toBe("backend_unavailable");
    expect(payload.retryable).toBe(true);
    expect(payload.reason).toBe("CrowdCode is temporarily unavailable");
    expect(payload.reason).not.toContain("boom");
    expect(payload.next_step.action).toBe("retry_backend");
    expect(payload.next_step.retry.tool).toBe("request_service");
  });

  it("rejects env-key auto-signing before any upstream call", async () => {
    const engine = await makeEngine();
    const upstream = fakeUpstream(() => {
      throw new Error("upstream must not be called");
    });
    const handlers = createToolHandlers({
      engine,
      upstream,
      wallet: {
        env: { X402_PRIVATE_KEY: KEY } as NodeJS.ProcessEnv,
        autoCreate: true,
      },
    });
    const result = await handlers.get_review_signing_payload({
      auto_sign: true,
      rating: 5,
      reason: "worked",
      payment_reference: "0x" + "ab".repeat(32),
      api_endpoint: "https://api.example.com/v1",
      payment_provider: "mppx",
      payment_target_ref: "0x" + "11".repeat(20),
    });
    const payload = JSON.parse(result.content[0]!.text);
    expect(payload.status).toBe("rejected");
    expect(payload.error_code).toBe("wallet_configuration_error");
    expect(payload.ok).toBe(false);
    expect(payload.reason).toContain("remove it");
    expect(upstream.calls).toHaveLength(0);
  });
});

const KEY = ("0x" + "42".repeat(32)) as `0x${string}`;
const ACCOUNT = privateKeyToAccount(KEY);
const MPPX_ARGS = {
  rating: 5,
  reason: "great data, receipt emailed to jane@corp.com",
  payment_reference: "0x" + "ab".repeat(32),
  api_endpoint: "https://api.example.com/v1",
  payment_provider: "mppx",
  payment_target_ref: "0x" + "11".repeat(20),
};

async function walletDirWith(key: `0x${string}` | null): Promise<string> {
  const dir = await mkdtemp(join(tmpdir(), "cc-handlers-"));
  if (key !== null) {
    const account = privateKeyToAccount(key);
    await writeFile(
      join(dir, "wallet.json"),
      JSON.stringify({
        privateKey: key,
        address: account.address,
        createdAt: "2026-01-01T00:00:00Z",
      }),
    );
  }
  return dir;
}

describe("transparent review signing", () => {
  beforeEach(() => resetWalletCache());

  it("signs unpaid endpoint reviews and returns the nonce for retries", async () => {
    const engine = await makeEngine();
    const upstream = fakeUpstream((name) => name === "get_service_score"
      ? { found: false, reason: "service not found" }
      : { accepted: true, payment_verified: false });
    const handlers = createToolHandlers({ engine, upstream,
      wallet: { walletDir: await walletDirWith(KEY), autoCreate: false, env: {} } });
    const args = { api_endpoint: "https://api.example.com/free", rating: 2, reason: "HTTP 500" };
    const first = JSON.parse((await handlers.review_service(args)).content[0]!.text);
    expect(first.review_nonce).toEqual(expect.any(String));
    await handlers.review_service({ ...args, review_nonce: first.review_nonce });
    const submitted = upstream.calls.filter(call => call.name === "review_service");
    expect(submitted).toHaveLength(2);
    for (const { args: sent } of submitted) {
      expect(sent.payment_reference).toBeUndefined();
      expect(sent.review_nonce).toBe(first.review_nonce);
      const message = canonicalReviewPayload({ identity: buildIdentity(sent), rating: 2,
        reason: String(sent.reason), reviewNonce: String(sent.review_nonce) });
      expect(JSON.parse(message).type).toBe("crowdcode.review.v2");
      expect(await verifyMessage({ address: ACCOUNT.address, message,
        signature: sent.review_signature as `0x${string}` })).toBe(true);
    }
  });

  it("auto-signs mppx reviews over the redacted reason", async () => {
    const engine = await makeEngine();
    const upstream = fakeUpstream((name) =>
      name === "get_service_score"
        ? { found: false, reason: "service not found" }
        : { accepted: true, payment_verified: false },
    );
    const dir = await walletDirWith(KEY);
    const handlers = createToolHandlers({
      engine,
      upstream,
      wallet: { walletDir: dir, autoCreate: false, env: {} as NodeJS.ProcessEnv },
    });

    const result = await handlers.review_service({ ...MPPX_ARGS });
    const payload = JSON.parse(result.content[0]!.text);
    expect(payload.wallet_source).toBe("agentcash");

    const sent = upstream.calls.find((c) => c.name === "review_service")!.args;
    expect(sent.reviewer_wallet).toBe(ACCOUNT.address);
    expect(sent.signature_scheme).toBe("eip191");
    expect(String(sent.reason)).toContain("[EMAIL_1]");

    // The signature must verify against the canonical payload the backend
    // rebuilds: identity resolved locally, reason REDACTED.
    const identity = buildIdentity(MPPX_ARGS);
    const message = canonicalReviewPayload({
      identity,
      rating: MPPX_ARGS.rating,
      reason: String(sent.reason),
      paymentReference: MPPX_ARGS.payment_reference,
    });
    expect(
      await verifyMessage({
        address: ACCOUNT.address,
        message,
        signature: sent.review_signature as `0x${string}`,
      }),
    ).toBe(true);
  });

  it("signs x402 Monad receipts with an x402:monad:<tx> reference", async () => {
    const engine = await makeEngine();
    const upstream = fakeUpstream((name) =>
      name === "get_service_score"
        ? { found: false, reason: "service not found" }
        : { accepted: true, payment_verified: true },
    );
    const handlers = createToolHandlers({
      engine,
      upstream,
      wallet: { walletDir: await walletDirWith(KEY), autoCreate: false, env: {} as NodeJS.ProcessEnv },
    });
    const tx = "0x" + "cd".repeat(32);
    const args = {
      ...MPPX_ARGS,
      payment_provider: "x402",
      payment_reference: tx,
      payment_proof: Buffer.from(JSON.stringify({ success: true, transaction: tx,
        network: "eip155:143" })).toString("base64"),
    };
    await handlers.review_service(args);
    const sent = upstream.calls.find((c) => c.name === "review_service")!.args;
    expect(sent.payment_reference).toBe(`x402:monad:${tx}`);
    expect(sent.payment_proof).toBe(args.payment_proof);
    const message = canonicalReviewPayload({
      identity: buildIdentity(args),
      rating: args.rating,
      reason: String(sent.reason),
      paymentReference: `x402:monad:${tx}`,
    });
    expect(await verifyMessage({ address: ACCOUNT.address, message,
      signature: sent.review_signature as `0x${string}` })).toBe(true);
  });

  it("passes a caller-supplied signature through untouched", async () => {
    const engine = await makeEngine();
    const upstream = fakeUpstream(() => ({ accepted: true }));
    const dir = await walletDirWith(null);
    const handlers = createToolHandlers({
      engine,
      upstream,
      wallet: { walletDir: dir, autoCreate: true, env: {} as NodeJS.ProcessEnv },
    });

    await handlers.review_service({
      ...MPPX_ARGS,
      reviewer_wallet: "0x" + "77".repeat(20),
      review_signature: "0xdeadbeef",
    });
    const sent = upstream.calls.find((c) => c.name === "review_service")!.args;
    expect(sent.review_signature).toBe("0xdeadbeef");
    expect(sent.reviewer_wallet).toBe("0x" + "77".repeat(20));
    // No wallet load happened: nothing was auto-created.
    await expect(stat(join(dir, "wallet.json"))).rejects.toThrow();
  });

  it("re-signs a locally reconstructed canonical message once", async () => {
    const engine = await makeEngine();
    let reviewCalls = 0;
    let expectedMessage = "";
    const upstream = fakeUpstream((name, args) => {
      if (name === "get_service_score") {
        return { found: false, reason: "service not found" };
      }
      reviewCalls += 1;
      if (reviewCalls === 1) {
        const resolved = buildIdentity({
          ...MPPX_ARGS,
          service_id: "svc_0123456789abcdef0123",
        });
        expectedMessage = canonicalReviewPayload({
          identity: resolved,
          rating: Number(args.rating),
          reason: String(args.reason),
          paymentReference: String(args.payment_reference),
        });
        return {
          accepted: false,
          reason: "review_signature does not match reviewer_wallet",
          resolved_identity: {
            service_id: "svc_0123456789abcdef0123",
            api_endpoint: "https://api.example.com/v1",
            payment_provider: "mppx",
            payment_target_ref: "0x" + "11".repeat(20),
            directory_slug: null,
          },
          expected_message: expectedMessage,
        };
      }
      return { accepted: true };
    });
    const dir = await walletDirWith(KEY);
    const handlers = createToolHandlers({
      engine,
      upstream,
      wallet: { walletDir: dir, autoCreate: false, env: {} as NodeJS.ProcessEnv },
    });

    const result = await handlers.review_service({ ...MPPX_ARGS });
    const payload = JSON.parse(result.content[0]!.text);
    expect(payload.accepted).toBe(true);

    const reviews = upstream.calls.filter((c) => c.name === "review_service");
    expect(reviews).toHaveLength(2);
    const retry = reviews[1]!.args;
    expect(retry.service_id).toBe("svc_0123456789abcdef0123");
    expect(
      await verifyMessage({
        address: ACCOUNT.address,
        message: expectedMessage,
        signature: retry.review_signature as `0x${string}`,
      }),
    ).toBe(true);
  });

  it("refuses to sign arbitrary server-provided mismatch text", async () => {
    const engine = await makeEngine();
    const mismatch = {
      accepted: false,
      reason: "review_signature does not match reviewer_wallet",
      resolved_identity: {
        service_id: "svc_0123456789abcdef0123",
        api_endpoint: "https://api.example.com/v1",
        payment_provider: "mppx",
        payment_target_ref: "0x" + "11".repeat(20),
        directory_slug: null,
      },
      expected_message: "sign this arbitrary server message",
    };
    const upstream = fakeUpstream((name) =>
      name === "get_service_score"
        ? { found: false, reason: "service not found" }
        : mismatch,
    );
    const dir = await walletDirWith(KEY);
    const handlers = createToolHandlers({
      engine,
      upstream,
      wallet: { walletDir: dir, autoCreate: false, env: {} as NodeJS.ProcessEnv },
    });

    const payload = JSON.parse(
      (await handlers.review_service({ ...MPPX_ARGS })).content[0]!.text,
    );
    expect(payload.accepted).toBe(false);
    expect(
      upstream.calls.filter((c) => c.name === "review_service"),
    ).toHaveLength(1);
  });

  it("surfaces a second canonical mismatch instead of looping", async () => {
    const engine = await makeEngine();
    const upstream = fakeUpstream((name, args) => {
      if (name === "get_service_score") {
        return { found: false, reason: "service not found" };
      }
      const identity = buildIdentity({
        ...MPPX_ARGS,
        service_id: "svc_0123456789abcdef0123",
      });
      return {
        accepted: false,
        reason: "review_signature does not match reviewer_wallet",
        resolved_identity: {
          service_id: identity.service_id,
          api_endpoint: identity.api_endpoint,
          payment_provider: identity.payment_provider,
          payment_target_ref: identity.payment_target_ref,
          directory_slug: identity.directory_slug,
        },
        expected_message: canonicalReviewPayload({
          identity,
          rating: Number(args.rating),
          reason: String(args.reason),
          paymentReference: String(args.payment_reference),
        }),
      };
    });
    const dir = await walletDirWith(KEY);
    const handlers = createToolHandlers({
      engine,
      upstream,
      wallet: { walletDir: dir, autoCreate: false, env: {} as NodeJS.ProcessEnv },
    });

    const payload = JSON.parse(
      (await handlers.review_service({ ...MPPX_ARGS })).content[0]!.text,
    );
    expect(payload.accepted).toBe(false);
    expect(
      upstream.calls.filter((c) => c.name === "review_service"),
    ).toHaveLength(2);
  });

  it("forwards unsigned with an install_wallet CTA when no wallet exists", async () => {
    const engine = await makeEngine();
    const upstream = fakeUpstream(() => ({
      accepted: false,
      reason: "reviewer_wallet is required for mppx and x402 reviews",
    }));
    const dir = await walletDirWith(null);
    const handlers = createToolHandlers({
      engine,
      upstream,
      wallet: { walletDir: dir, autoCreate: false, env: {} as NodeJS.ProcessEnv },
    });

    const payload = JSON.parse(
      (await handlers.review_service({ ...MPPX_ARGS })).content[0]!.text,
    );
    expect(payload.accepted).toBe(false);
    expect(payload.wallet_source).toBe("none");
    expect(payload.next_step.action).toBe("install_wallet");
    expect(payload.next_step.command).toContain("agentcash");
    const sent = upstream.calls.find((c) => c.name === "review_service")!.args;
    expect(sent.review_signature).toBeUndefined();
  });

  it("auto-creates the wallet when signing is needed and autoCreate is on", async () => {
    const engine = await makeEngine();
    const upstream = fakeUpstream((name) =>
      name === "get_service_score"
        ? { found: false, reason: "service not found" }
        : { accepted: true },
    );
    const dir = await walletDirWith(null);
    const handlers = createToolHandlers({
      engine,
      upstream,
      wallet: { walletDir: dir, autoCreate: true, env: {} as NodeJS.ProcessEnv },
    });

    const payload = JSON.parse(
      (await handlers.review_service({ ...MPPX_ARGS })).content[0]!.text,
    );
    expect(payload.wallet_source).toBe("agentcash");
    expect(payload.wallet_created).toBe(true);
    if (process.platform !== "win32") {
      // Windows has no POSIX permission bits; stat always reports 0o666.
      expect(((await stat(join(dir, "wallet.json"))).mode & 0o777)).toBe(0o600);
    }
  });

  it("attaches requester_wallet to request_service from the local wallet", async () => {
    const engine = await makeEngine();
    const upstream = fakeUpstream(() => ({ accepted: true, request_id: 1 }));
    const dir = await walletDirWith(KEY);
    const handlers = createToolHandlers({
      engine,
      upstream,
      wallet: { walletDir: dir, autoCreate: false, env: {} as NodeJS.ProcessEnv },
    });

    await handlers.request_service({ service_description: "OCR service" });
    expect(upstream.calls[0]!.args.requester_wallet).toBe(ACCOUNT.address);
  });

  it("adds an onboarding CTA to paid-service scores when no wallet exists", async () => {
    const engine = await makeEngine();
    const upstream = fakeUpstream(() => ({
      found: true,
      payment_provider: "x402",
      avg_rating: 4.5,
    }));
    const dir = await walletDirWith(null);
    const handlers = createToolHandlers({
      engine,
      upstream,
      wallet: { walletDir: dir, autoCreate: false, env: {} as NodeJS.ProcessEnv },
    });

    const first = JSON.parse(
      (await handlers.get_service_score({ api_endpoint: "https://a.example" }))
        .content[0]!.text,
    );
    expect(first.onboarding_cta.command).toContain("agentcash");
    // Once per process: the second lookup does not nag.
    const second = JSON.parse(
      (await handlers.get_service_score({ api_endpoint: "https://a.example" }))
        .content[0]!.text,
    );
    expect(second.onboarding_cta).toBeUndefined();
  });
});

describe("x402 Solana payer linkage", () => {
  beforeEach(() => resetWalletCache());

  const SIG = "5eHrTHo8wSNNNf857kCqzX15YW1AaETvyDW5bAyv3EBZp57PFhXMGj4zoi9mYi9XGKEA5bCzNFTdm2mdaeLwH5uj";
  const SOLANA_PAYEE = "53JhuF8bgxvUQ59nDG6kWs4awUQYCS3wswQmUsV5uC7t";
  const SEED = Uint8Array.from({ length: 32 }, (_, i) => i);
  const SOLANA_ADDRESS = "FAe4sisG95oZ42w7buUn5qEE4TAnfTTFPiguZUHmhiF";
  const proof = (payer: string) => Buffer.from(JSON.stringify({
    success: true, transaction: SIG, network: "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp", payer,
  })).toString("base64");
  const ARGS = {
    rating: 5,
    reason: "fresh results",
    api_endpoint: "https://api.solana-seller.example/v1/search",
    payment_provider: "x402",
    payment_target_ref: SOLANA_PAYEE,
  };

  async function dirWithSolana(withSolana = true): Promise<string> {
    const dir = await walletDirWith(KEY);
    if (withSolana) {
      await writeFile(join(dir, "solana-wallet.json"), JSON.stringify({
        privateKey: base58Encode(SEED), address: SOLANA_ADDRESS, createdAt: "2026-01-01T00:00:00Z" }));
    }
    return dir;
  }

  function verifiesPayerLink(sent: Record<string, unknown>): boolean {
    const message = canonicalReviewPayload({ identity: buildIdentity(sent), rating: 5,
      reason: String(sent.reason), paymentReference: String(sent.payment_reference) });
    const publicKey = createPublicKey({
      key: Buffer.concat([Buffer.from("302a300506032b6570032100", "hex"), base58Decode(SOLANA_ADDRESS)!]),
      format: "der", type: "spki" });
    return verify(null, Buffer.from(payerLinkMessage(ACCOUNT.address, message), "utf8"), publicKey,
      base58Decode(String(sent.payer_signature))!);
  }

  async function run(args: Record<string, unknown>, dir: string,
                     respond: (args: Record<string, unknown>) => Record<string, unknown> = () => ({ accepted: true })) {
    const engine = await makeEngine();
    const upstream = fakeUpstream((name, sent) =>
      name === "get_service_score" ? { found: false, reason: "service not found" } : respond(sent));
    const handlers = createToolHandlers({ engine, upstream,
      wallet: { walletDir: dir, autoCreate: false, env: {} as NodeJS.ProcessEnv } });
    const result = JSON.parse((await handlers.review_service(args)).content[0]!.text);
    return { result, reviews: upstream.calls.filter((c) => c.name === "review_service").map((c) => c.args) };
  }

  it("signs payer_signature with the agentcash Solana wallet", async () => {
    const { reviews } = await run({ ...ARGS, payment_proof: proof(SOLANA_ADDRESS) }, await dirWithSolana());
    expect(reviews).toHaveLength(1);
    const sent = reviews[0]!;
    expect(sent.payment_reference).toBe(`x402:solana:${SIG}`);
    expect(sent.payer_wallet).toBeUndefined();
    expect(verifiesPayerLink(sent)).toBe(true);
    const message = canonicalReviewPayload({ identity: buildIdentity(sent), rating: 5,
      reason: String(sent.reason), paymentReference: `x402:solana:${SIG}` });
    expect(await verifyMessage({ address: ACCOUNT.address, message,
      signature: sent.review_signature as `0x${string}` })).toBe(true);
  });

  it("signs proofless x402:solana references too", async () => {
    const { reviews } = await run({ ...ARGS, payment_reference: `x402:solana:${SIG}` }, await dirWithSolana());
    expect(verifiesPayerLink(reviews[0]!)).toBe(true);
  });

  it("does not sign when the proof names another payer or no Solana wallet exists", async () => {
    const other = await run({ ...ARGS, payment_proof: proof("2b1JD7tcidGtAYCVm1dDKTgVHRoihSJ1uqz3jf9WwSLP") },
      await dirWithSolana());
    expect(other.reviews[0]!.payer_signature).toBeUndefined();
    const none = await run({ ...ARGS, payment_reference: `x402:solana:${SIG}` }, await dirWithSolana(false));
    expect(none.reviews[0]!.payer_signature).toBeUndefined();
    expect(none.reviews[0]!.review_signature).toEqual(expect.any(String));
  });

  it("retries once as an unlinked observation when the local key was not the payer", async () => {
    const { result, reviews } = await run({ ...ARGS, payment_reference: `x402:solana:${SIG}` },
      await dirWithSolana(), (sent) => sent.payer_signature
        ? { accepted: false, error_code: "payer_signature_invalid", reason: "payer_signature does not match" }
        : { accepted: true, payment_verification_level: "signature_only" });
    expect(reviews).toHaveLength(2);
    expect(reviews[1]!.payer_signature).toBeUndefined();
    expect(reviews[1]!.review_signature).toBe(reviews[0]!.review_signature);
    expect(result.accepted).toBe(true);
  });

  it("never strips a caller-supplied payer_signature", async () => {
    const { reviews, result } = await run({ ...ARGS, payment_reference: `x402:solana:${SIG}`, payer_signature: "abc" },
      await dirWithSolana(), () => ({ accepted: false, error_code: "payer_signature_invalid" }));
    expect(reviews).toHaveLength(1);
    expect(reviews[0]!.payer_signature).toBe("abc");
    expect(result.error_code).toBe("payer_signature_invalid");
  });
});
