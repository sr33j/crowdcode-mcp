import { describe, expect, it } from "vitest";
import { reasonHash } from "../src/canonical/payload.js";
import {
  getReviewSigningPayload,
  type SigningDeps,
} from "../src/tools/signing-payload.js";

function fakeDeps(options: {
  score?: Record<string, unknown>;
  scoreError?: Error;
  redactedSuffix?: string;
}): SigningDeps & { calls: Array<Record<string, unknown>> } {
  const calls: Array<Record<string, unknown>> = [];
  return {
    calls,
    redact: async (text: string) => ({
      text: options.redactedSuffix ? text + options.redactedSuffix : text,
      entitiesRemoved: options.redactedSuffix ? 1 : 0,
      modelActive: true,
    }),
    upstream: {
      call: async (_name: string, args: Record<string, unknown>) => {
        calls.push(args);
        if (options.scoreError) throw options.scoreError;
        return options.score ?? { found: false, reason: "service not found" };
      },
    },
  };
}

const BASE_ARGS = {
  rating: 5,
  reason: "great service",
  payment_reference: "0x" + "ab".repeat(32),
};

describe("getReviewSigningPayload", () => {
  it("uses canonical identity from the backend when the service is found", async () => {
    const deps = fakeDeps({
      score: {
        found: true,
        service_id: "svc_serverresolved00000",
        service_name: "OCR",
        canonical_endpoint: "https://api.example.com/v1",
        payment_provider: "x402",
        payment_target_ref: "0x" + "11".repeat(20),
        directory_slug: "example-ocr",
      },
    });
    const result = await getReviewSigningPayload(deps, {
      ...BASE_ARGS,
      api_endpoint: "API.example.com/v1/",
      payment_provider: "x402",
      payment_target_ref: "0x" + "11".repeat(20),
    });
    expect(result.ok).toBe(true);
    const identity = result.identity as Record<string, unknown>;
    expect(identity.service_id).toBe("svc_serverresolved00000");
    // Caller-normalized endpoint wins over canonical (mirrors backend merge).
    expect(identity.api_endpoint).toBe("https://api.example.com/v1");
    expect(identity.directory_slug).toBe("example-ocr");
    const message = JSON.parse(result.message as string);
    expect(message.service_id).toBe("svc_serverresolved00000");
    expect(message.type).toBe("crowdcode.review.v1");
  });

  it("keeps caller identity verbatim when the service is not found", async () => {
    const deps = fakeDeps({});
    const result = await getReviewSigningPayload(deps, {
      ...BASE_ARGS,
      api_endpoint: "https://new.example.com/api",
      payment_provider: "mppx",
      payment_target_ref: "0x" + "22".repeat(20),
    });
    expect(result.ok).toBe(true);
    const identity = result.identity as Record<string, unknown>;
    expect(identity.service_id).toBeNull();
    expect(identity.api_endpoint).toBe("https://new.example.com/api");
  });

  it("propagates resolution conflicts as failures", async () => {
    const deps = fakeDeps({
      score: { found: false, reason: "service identity conflict" },
    });
    const result = await getReviewSigningPayload(deps, {
      ...BASE_ARGS,
      directory_slug: "conflicted",
    });
    expect(result).toEqual({ ok: false, reason: "service identity conflict" });
  });

  it("fails cleanly when the backend is unreachable", async () => {
    const deps = fakeDeps({ scoreError: new Error("fetch failed") });
    const result = await getReviewSigningPayload(deps, {
      ...BASE_ARGS,
      api_endpoint: "https://api.example.com/v1",
    });
    expect(result.ok).toBe(false);
    expect(String(result.reason)).toContain("fetch failed");
  });

  it("rejects invalid identity input with the backend's error message", async () => {
    const deps = fakeDeps({});
    const result = await getReviewSigningPayload(deps, {
      ...BASE_ARGS,
      payment_provider: "venmo",
    });
    expect(result.ok).toBe(false);
    expect(String(result.reason)).toContain("payment_provider must be one of");
    expect(deps.calls).toHaveLength(0);
  });

  it("hashes the REDACTED reason and echoes it for review_service", async () => {
    const deps = fakeDeps({ redactedSuffix: " [EMAIL_1]" });
    const result = await getReviewSigningPayload(deps, {
      ...BASE_ARGS,
      reason: "contact me",
      api_endpoint: "https://api.example.com/v1",
    });
    expect(result.ok).toBe(true);
    expect(result.reason).toBe("contact me [EMAIL_1]");
    const message = JSON.parse(result.message as string);
    expect(message.reason_hash).toBe(reasonHash("contact me [EMAIL_1]"));
    expect(message.reason_hash).not.toBe(reasonHash("contact me"));
    expect(
      (result._redaction as Record<string, unknown>).entities_removed,
    ).toBe(1);
  });
});
