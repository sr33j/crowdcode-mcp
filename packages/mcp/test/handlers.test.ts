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
    expect(((await stat(join(dir, "wallet.json"))).mode & 0o777)).toBe(0o600);
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
