import { describe, expect, it } from "vitest";
import { getConfig } from "../src/config.js";
import { RedactionEngine } from "@crowdcode/redaction";
import { createToolHandlers } from "../src/server.js";
import type { Upstream } from "../src/upstream.js";
import { UpstreamError } from "../src/upstream.js";

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
    expect(payload.reason).toContain("boom");
  });
});
