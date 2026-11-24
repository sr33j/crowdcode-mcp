/**
 * getReviewSigningPayload: the review message a wallet signs, built locally.
 *
 * The examples are the regression seeds. The properties generate reviews and
 * identities: the message parses back to exactly the reviewed fields; a
 * signature over it verifies when the backend rebuilds it and fails once any
 * single field changes; caller fields never replace the identity the backend
 * resolved (the bug fixed in 374446f); and src/crowdcode/payments.py and
 * identity.py (through test/fixtures/python_oracle.py) build the identical
 * message and identity, and recover the signer from the TS signature.
 */

import fc from "fast-check";
import { privateKeyToAccount } from "viem/accounts";
import { verifyMessage } from "viem";
import { describe, expect, it } from "vitest";
import { buildIdentity, PAYMENT_PROVIDERS } from "../src/canonical/identity.js";
import { canonicalReviewPayload, reasonHash } from "../src/canonical/payload.js";
import {
  getReviewSigningPayload,
  type SigningDeps,
} from "../src/tools/signing-payload.js";
import { pythonAvailable, pythonSkipNote, runPython } from "./fixtures/python-oracle.js";

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

  it("does not let caller fields override an existing service identity", async () => {
    const canonicalTarget = "0x" + "11".repeat(20);
    const deps = fakeDeps({
      score: {
        found: true,
        service_id: "svc_serverresolved00000",
        service_name: "OCR",
        canonical_endpoint: "https://api.example.com/v1",
        payment_provider: "x402",
        payment_target_ref: canonicalTarget,
        directory_slug: "example-ocr",
      },
    });
    const result = await getReviewSigningPayload(deps, {
      ...BASE_ARGS,
      service_id: "svc_serverresolved00000",
      api_endpoint: "https://attacker.example/api",
      payment_provider: "mppx",
      payment_target_ref: "0x" + "99".repeat(20),
    });

    expect(result.ok).toBe(true);
    expect(result.identity).toMatchObject({
      service_id: "svc_serverresolved00000",
      api_endpoint: "https://api.example.com/v1",
      payment_provider: "x402",
      payment_target_ref: canonicalTarget,
      directory_slug: "example-ocr",
    });
  });

  it("uses a database-authorized registered alias when returned", async () => {
    const aliasTarget = "0x" + "22".repeat(20);
    const deps = fakeDeps({
      score: {
        found: true,
        service_id: "svc_serverresolved00000",
        service_name: "OCR",
        canonical_endpoint: "https://api.example.com/v1",
        payment_provider: "x402",
        payment_target_ref: "0x" + "11".repeat(20),
        directory_slug: "example-ocr",
        resolved_identity: {
          service_id: "svc_serverresolved00000",
          api_endpoint: "https://api.example.com/v1",
          payment_provider: "mppx",
          payment_target_ref: aliasTarget,
          directory_slug: "example-ocr",
        },
      },
    });
    const result = await getReviewSigningPayload(deps, {
      ...BASE_ARGS,
      service_id: "svc_serverresolved00000",
      payment_provider: "mppx",
      payment_target_ref: aliasTarget,
    });

    expect(result.ok).toBe(true);
    expect(result.identity).toMatchObject({
      payment_provider: "mppx",
      payment_target_ref: aliasTarget,
    });
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

// ---------------------------------------------------------------------------
// Generators
// ---------------------------------------------------------------------------

const reviewer = privateKeyToAccount("0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318");

/** Every character Python's str.isspace() accepts, and a few it does not. */
const PY_SPACE = ["\t", "\n", "\x0b", "\x0c", "\r", "\x1c", "\x1d", "\x1e", "\x1f", " ", "\x85", "\xa0", " ", " ", " ", " ", " ", " ", " ", "　"];
const NOT_SPACE = ["​", "᠎", "﻿", "\x00"];
const pySpace = fc.string({ unit: fc.constantFrom(...PY_SPACE), maxLength: 3 });
const text = fc.oneof(
  fc.string({ unit: "binary", maxLength: 40 }),
  fc.string({ unit: fc.oneof(fc.constantFrom(...PY_SPACE, ...NOT_SPACE), fc.string({ unit: "binary-ascii", minLength: 1, maxLength: 1 })), maxLength: 20 }),
);
const padded = (arb: fc.Arbitrary<string>) => fc.tuple(pySpace, arb, pySpace).map(([a, b, c]) => a + b + c);
const optional = (arb: fc.Arbitrary<string>) => fc.option(arb, { nil: null });

const segment = fc.oneof(
  fc.stringMatching(/^[a-z][a-z0-9-]{0,10}$/),
  fc.stringMatching(/^[0-9]{1,8}$/),
  fc.uuid(),
  fc.stringMatching(/^(0x)?[0-9a-fA-F]{14,20}$/),
  fc.stringMatching(/^[A-Za-z0-9_-]{20,30}$/),
);
const host = fc.oneof(
  fc.domain(),
  fc.constantFrom("API.Example.com", "localhost", "127.0.0.1", "xn--caf-dma.example"),
);
/** Endpoints as reviewers type them: any case, scheme or none, ports, trailing slashes, queries. */
const endpoint = fc
  .record({
    scheme: fc.constantFrom("https://", "http://", "HTTPS://", ""),
    host,
    port: fc.oneof(fc.constant(""), fc.integer({ min: 0, max: 65535 }).map((p) => `:${p}`)),
    path: fc.array(segment, { maxLength: 4 }),
    slash: fc.constantFrom("", "/", "//"),
    query: fc.constantFrom("", "?a=1", "#frag", "?x=1#y"),
  })
  .map((e) => `${e.scheme}${e.host}${e.port}${e.path.length ? "/" + e.path.join("/") : ""}${e.slash}${e.query}`);
const provider = fc.oneof(
  fc.constantFrom(...PAYMENT_PROVIDERS),
  fc.constantFrom("X402", "mpp", "link", "stripe-link", "payment_link", "MPPX"),
);
const evmTarget = fc.uint8Array({ minLength: 20, maxLength: 20 }).map((b) => "0x" + Buffer.from(b).toString("hex"));
const identityArgs = fc.record({
  service_id: optional(fc.oneof(fc.stringMatching(/^svc_[0-9a-f]{20}$/), padded(text))),
  api_endpoint: optional(padded(endpoint)),
  payment_provider: optional(padded(provider)),
  payment_target_ref: optional(padded(fc.oneof(evmTarget, text))),
  directory_slug: optional(padded(fc.oneof(fc.stringMatching(/^[a-z0-9-]{1,20}$/), text))),
});
const txHash = fc.uint8Array({ minLength: 32, maxLength: 32 }).map((b) => "0x" + Buffer.from(b).toString("hex"));
const review = fc.record({
  args: identityArgs,
  rating: fc.integer({ min: 1, max: 5 }),
  reason: padded(text),
  // null: an unpaid v2 review; else a paid v1 reference, padded as callers send it.
  payment_reference: optional(padded(fc.oneof(txHash, txHash.map((h) => `x402:monad:${h}`), text.filter((t) => t.trim() !== "")))),
  review_nonce: fc.stringMatching(/^[A-Za-z0-9_-]{8,40}$/),
});
type Review = typeof review extends fc.Arbitrary<infer T> ? T : never;

const notFound = (): SigningDeps => fakeDeps({});

async function signingPayload(r: Review, deps: SigningDeps = notFound()) {
  return getReviewSigningPayload(deps, {
    ...r.args,
    rating: r.rating,
    reason: r.reason,
    payment_reference: r.payment_reference,
    review_nonce: r.payment_reference === null ? r.review_nonce : null,
  });
}

function builds(args: Review["args"]): boolean {
  try {
    buildIdentity(args);
    return true;
  } catch {
    return false;
  }
}
const buildable = review.filter((r) => builds(r.args));

const IDENTITY_KEYS = ["service_id", "api_endpoint", "payment_provider", "payment_target_ref", "directory_slug"] as const;

/** What the backend rebuilds from review_service's arguments (the result's identity, reason, rating, reference). */
function rebuild(fields: { identity: Record<string, string | null>; rating: number; reason: string; payment_reference: string | null; review_nonce: string | null }) {
  return canonicalReviewPayload({
    identity: fields.identity as never,
    rating: fields.rating,
    reason: fields.reason,
    paymentReference: fields.payment_reference,
    reviewNonce: fields.review_nonce,
  });
}

const pyStripped = (s: string) => s.replace(new RegExp(`^[${PY_SPACE.join("")}]+|[${PY_SPACE.join("")}]+$`, "g"), "");

// ---------------------------------------------------------------------------
// Properties
// ---------------------------------------------------------------------------

describe("properties: the review signing payload", () => {
  it("the message parses back to exactly the reviewed fields, sorted, in ASCII", async () => {
    await fc.assert(
      fc.asyncProperty(buildable, async (r) => {
        const result = await signingPayload(r);
        expect(result.ok).toBe(true);
        const message = result.message as string;
        expect(message).toMatch(/^[\x20-\x7e]*$/);
        const parsed = JSON.parse(message) as Record<string, unknown>;
        expect(Object.keys(parsed)).toEqual(Object.keys(parsed).sort());
        const paid = r.payment_reference !== null;
        const identity = buildIdentity(r.args);
        expect(parsed).toEqual({
          type: paid ? "crowdcode.review.v1" : "crowdcode.review.v2",
          ...(paid ? {} : { review_nonce: r.review_nonce }),
          ...Object.fromEntries(IDENTITY_KEYS.map((k) => [k, identity[k]])),
          payment_reference: paid ? pyStripped(r.payment_reference!) : null,
          rating: r.rating,
          reason_hash: reasonHash(r.reason),
        });
        expect(parsed.reason_hash).toBe(reasonHash(pyStripped(r.reason)));
        expect(result.identity).toEqual(Object.fromEntries(IDENTITY_KEYS.map((k) => [k, identity[k]])));
      }),
    );
  });

  it("a signature over the message verifies when the backend rebuilds it, and fails once any single field changes", async () => {
    const field = fc.constantFrom("rating", "reason", "payment_reference", "review_nonce", "paid", ...IDENTITY_KEYS);
    await fc.assert(
      fc.asyncProperty(buildable, field, fc.string({ unit: "binary", minLength: 1, maxLength: 3 }), pySpace, async (r, which, extra, space) => {
        const result = await signingPayload(r);
        const message = result.message as string;
        const signature = await reviewer.signMessage({ message });
        const fields = {
          identity: result.identity as Record<string, string | null>,
          rating: r.rating,
          reason: result.reason as string,
          payment_reference: r.payment_reference,
          review_nonce: r.payment_reference === null ? (result.review_nonce as string) : null,
        };
        expect(rebuild(fields)).toBe(message);
        expect(await verifyMessage({ address: reviewer.address, message: rebuild(fields), signature })).toBe(true);
        // Python whitespace around the reason or reference is not part of what is signed.
        const repadded = {
          ...fields,
          reason: space + fields.reason + space,
          payment_reference: fields.payment_reference === null ? null : space + fields.payment_reference,
        };
        expect(rebuild(repadded)).toBe(message);

        const changed = { ...fields, identity: { ...fields.identity } };
        const marker = pyStripped(extra) === "" ? "x" : extra;
        if (which === "rating") changed.rating = (r.rating % 5) + 1;
        if (which === "reason") changed.reason = pyStripped(fields.reason) + marker;
        if (which === "payment_reference") changed.payment_reference = (pyStripped(fields.payment_reference ?? "") || "0x") + marker;
        if (which === "review_nonce") {
          if (fields.review_nonce === null) {
            // A paid (v1) review does not sign a nonce: adding one changes nothing.
            expect(rebuild({ ...fields, review_nonce: r.review_nonce })).toBe(message);
            return;
          }
          changed.review_nonce = fields.review_nonce + "x";
        }
        if (which === "paid") {
          // The same review moved between v1 (paid) and v2 (unpaid).
          changed.payment_reference = fields.payment_reference === null ? r.review_nonce : null;
          changed.review_nonce = fields.payment_reference === null ? null : r.review_nonce;
        }
        if ((IDENTITY_KEYS as readonly string[]).includes(which)) {
          const key = which as (typeof IDENTITY_KEYS)[number];
          changed.identity[key] = fields.identity[key] === null ? marker : null;
        }
        expect(rebuild(changed)).not.toBe(message);
        expect(await verifyMessage({ address: reviewer.address, message: rebuild(changed), signature })).toBe(false);
      }),
      { numRuns: 30 },
    );
  });

  // 374446f ("fixes"): when the backend resolved the service, the caller's
  // endpoint, provider, target and slug used to win over it.
  it("once the backend resolves the service, the signed identity is the backend's, whatever the caller sent", async () => {
    const resolved = fc.record({
      service_id: fc.stringMatching(/^svc_[0-9a-f]{20}$/),
      api_endpoint: endpoint,
      payment_provider: fc.constantFrom(...PAYMENT_PROVIDERS),
      payment_target_ref: evmTarget,
      directory_slug: optional(fc.stringMatching(/^[a-z0-9-]{1,20}$/)),
    });
    await fc.assert(
      fc.asyncProperty(buildable, resolved, fc.boolean(), async (r, stored, asResolvedIdentity) => {
        const score = asResolvedIdentity
          ? { found: true, service_name: "S", resolved_identity: stored }
          : { found: true, service_name: "S", ...stored, api_endpoint: undefined, canonical_endpoint: stored.api_endpoint };
        const result = await signingPayload(r, fakeDeps({ score }));
        expect(result.ok).toBe(true);
        const expected = buildIdentity(stored);
        const signed = JSON.parse(result.message as string) as Record<string, unknown>;
        for (const key of IDENTITY_KEYS) {
          expect((result.identity as Record<string, unknown>)[key]).toBe(expected[key]);
          expect(signed[key]).toBe(expected[key]);
        }
      }),
    );
  });
});

// ---------------------------------------------------------------------------
// Differential against src/crowdcode (payments.py, identity.py)
// ---------------------------------------------------------------------------

describe(`differential: the backend's payments.py and identity.py${pythonSkipNote}`, () => {
  it.skipIf(!pythonAvailable)("builds the same identity and message, and recovers the signer from our signature", async () => {
    const seed = Date.now() | 0;
    const reviews = fc.sample(review, { numRuns: 120, seed });
    const built = [] as Array<{ r: Review; result: Record<string, unknown>; signature: string }>;
    for (const r of reviews.filter((x) => builds(x.args)).slice(0, 15)) {
      const result = await signingPayload(r);
      built.push({ r, result, signature: await reviewer.signMessage({ message: result.message as string }) });
    }
    const cases = [
      ...reviews.map((r) => ({ op: "identity", args: r.args })),
      ...built.map(({ r, result, signature }) => ({
        op: "review_payload",
        identity: result.identity,
        rating: r.rating,
        reason: result.reason,
        payment_reference: r.payment_reference,
        review_nonce: result.review_nonce ?? null,
        signature,
      })),
    ];
    const results = runPython(cases);
    let i = 0;
    for (const r of reviews) {
      const py = results[i++];
      let ts: unknown;
      try {
        const identity = buildIdentity(r.args);
        ts = { identity: Object.fromEntries(IDENTITY_KEYS.map((k) => [k, identity[k]])) };
      } catch (err) {
        ts = { error: (err as Error).message };
      }
      expect(ts, JSON.stringify({ seed, args: r.args })).toEqual(py);
    }
    for (const { r, result } of built) {
      const py = results[i++];
      expect(result.message, JSON.stringify({ seed, r })).toBe(py.message);
      expect(py.recovered).toBe(reviewer.address.toLowerCase());
    }
  });
});
