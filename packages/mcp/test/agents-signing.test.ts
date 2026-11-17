/**
 * The agent tools' wire formats, pinned to vectors computed with the Hermes
 * plugin (plugins/hermes/wallet.py, Python's json.dumps) so both clients sign
 * byte-identical requests and EIP-3009 authorizations.
 *
 * Regenerate: load wallet.py with eth_account, set secrets.token_hex to return
 * "0123456789abcdef"…, call Wallet.sign_request(..., now=1763337600) and, with
 * time.time() = 1763337600.5, authorize_transfer(to=0x…c0de, value=350000,
 * valid_seconds=300).
 */

import { privateKeyToAccount } from "viem/accounts";
import { describe, expect, it } from "vitest";
import { pyDumps, pyFloat, jsonBody } from "../src/agents/pyjson.js";
import { requestMessage, sha256Hex, Signer, usdToUnits, unitsToUsd } from "../src/agents/signer.js";

const KEY = "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318";
const NONCE16 = "0123456789abcdef0123456789abcdef";
const signer = new Signer(privateKeyToAccount(KEY));

describe("request signatures (Wallet API v1)", () => {
  it("derives the same checksummed address and CAIP-10 owner", () => {
    expect(signer.address).toBe("0x2c7536E3605D9C16a7a3D7b1898e529396a65c23");
    expect(signer.caip10).toBe("eip155:8453:0x2c7536E3605D9C16a7a3D7b1898e529396a65c23");
  });

  it("serializes bodies like json.dumps(payload, separators=(',', ':'))", () => {
    const body = jsonBody({ amount_usd: pyFloat(1.5), prompt: "héllo \u{1F600}", budget_usd: pyFloat(1) });
    expect(new TextDecoder().decode(body)).toBe(
      '{"amount_usd":1.5,"prompt":"h\\u00e9llo \\ud83d\\ude00","budget_usd":1.0}',
    );
    expect(sha256Hex(body)).toBe("80934698837f2f64fa0d76209e4179706c0d325767ec9d7a93f86a84cd683e75");
  });

  it("signs a POST with a body exactly like wallet.py", async () => {
    const body = jsonBody({ amount_usd: pyFloat(1.5), prompt: "héllo \u{1F600}", budget_usd: pyFloat(1) });
    const headers = await signer.signRequest("post", "/api/app/v1/agents/ag_123/deposits?x=1", body, {
      now: 1763337600,
      nonce: NONCE16,
    });
    expect(headers).toEqual({
      "X-CrowdCode-Address": "eip155:8453:0x2c7536E3605D9C16a7a3D7b1898e529396a65c23",
      "X-CrowdCode-Timestamp": "1763337600",
      "X-CrowdCode-Nonce": NONCE16,
      "X-CrowdCode-Signature":
        "0xc9f31e78da5fe6d8b110dbb3458ebd8d07068e099f8b79707a4f919cd8eb991f3f07fabeb7e893d0c49dcbd3951440318f255ed6d563342d159e3eb29410ac8e1b",
    });
  });

  it("signs an empty-body GET with an empty digest line", async () => {
    expect(requestMessage("GET", "/api/app/v1/me", sha256Hex(new Uint8Array()), "1763337600", NONCE16)).toBe(
      `CrowdCode API request\nGET /api/app/v1/me\n\n1763337600\n${NONCE16}`,
    );
    const headers = await signer.signRequest("GET", "/api/app/v1/me", new Uint8Array(), {
      now: 1763337600,
      nonce: NONCE16,
    });
    expect(headers["X-CrowdCode-Signature"]).toBe(
      "0x1847b076d0bd67d26be0932d1d5b94b94a47c27a530944aafee2e9f6f235167872a4192046a8b26a11c371d5b01c236d37c784ada58703ea262632453f2d7ba61b",
    );
  });

  it("uses a fresh 32-hex-character nonce per request", async () => {
    const a = await signer.signRequest("GET", "/api/app/v1/me", new Uint8Array());
    const b = await signer.signRequest("GET", "/api/app/v1/me", new Uint8Array());
    expect(a["X-CrowdCode-Nonce"]).toMatch(/^[0-9a-f]{32}$/);
    expect(a["X-CrowdCode-Nonce"]).not.toBe(b["X-CrowdCode-Nonce"]);
  });
});

describe("EIP-3009 transferWithAuthorization", () => {
  it("matches wallet.py's authorization and signature", async () => {
    const { authorization, signature } = await signer.authorizeTransfer({
      to: "0x000000000000000000000000000000000000c0de",
      value: 350_000n,
      validSeconds: 300,
      now: 1763337600.5,
      nonce: "0x" + "0123456789abcdef".repeat(4),
    });
    expect(authorization).toEqual({
      from: "0x2c7536E3605D9C16a7a3D7b1898e529396a65c23",
      to: "0x000000000000000000000000000000000000c0DE",
      value: "350000",
      validAfter: "0",
      validBefore: "1763337900",
      nonce: "0x0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    });
    expect(signature).toBe(
      "0x6ba18d34fefab58d6e938c1c1b3cb75987a34b4d0e7f9cbdd357e754b07f01a27baa2c803ef0d04724a1402a2d3fe6092e96d7554e8a8cb7321fa4a95bf78ee51c",
    );
  });

  it("never signs a zero value and keeps at least 30 seconds of validity", async () => {
    await expect(
      signer.authorizeTransfer({ to: "0x000000000000000000000000000000000000c0de", value: 0n, validSeconds: 300 }),
    ).rejects.toThrow("value must be positive");
    const { authorization } = await signer.authorizeTransfer({
      to: "0x000000000000000000000000000000000000c0de",
      value: 1n,
      validSeconds: 1,
      now: 100,
    });
    expect(authorization.validBefore).toBe("130");
    expect(authorization.nonce).toMatch(/^0x[0-9a-f]{64}$/);
  });
});

describe("Python json.dumps", () => {
  it("writes floats with Python's repr", () => {
    const cases: [number, string][] = [
      [1, "1.0"],
      [0.5, "0.5"],
      [0.1, "0.1"],
      [1e-5, "1e-05"],
      [1e-7, "1e-07"],
      [123456789012345678, "1.2345678901234568e+17"],
      [1e16, "1e+16"],
      [1e15, "1000000000000000.0"],
      [0.35, "0.35"],
      [2.5e-4, "0.00025"],
      [100, "100.0"],
      [1234567.125, "1234567.125"],
    ];
    for (const [value, repr] of cases) expect(pyDumps(pyFloat(value))).toBe(repr);
  });

  it("escapes like ensure_ascii and keeps insertion order", () => {
    expect(pyDumps({ b: [1, pyFloat(2), null, true, false], a: 'x"\\\n\t\x7f ', k: {} }, { compact: true })).toBe(
      '{"b":[1,2.0,null,true,false],"a":"x\\"\\\\\\n\\t\\u007f\\u2028","k":{}}',
    );
  });

  it("sorts keys with the default separators (the task idempotency key)", () => {
    expect(pyDumps(["ag_1", { prompt: "p", budget_usd: pyFloat(0.25) }, 2939000], { sortKeys: true })).toBe(
      '["ag_1", {"budget_usd": 0.25, "prompt": "p"}, 2939000]',
    );
  });
});

describe("USDC amounts", () => {
  it("converts dollars to base units rounding down, like Decimal", () => {
    expect(usdToUnits(1.5)).toBe(1_500_000n);
    expect(usdToUnits(0.1)).toBe(100_000n);
    expect(usdToUnits("0.0000019")).toBe(1n);
    expect(usdToUnits(1e-6)).toBe(1n);
    expect(usdToUnits(12.3456789)).toBe(12_345_678n);
    expect(unitsToUsd(350_000n)).toBe(0.35);
  });

  it("refuses zero, negative and tiny amounts", () => {
    expect(() => usdToUnits(0)).toThrow("amount must be greater than zero");
    expect(() => usdToUnits(-1)).toThrow("amount must be greater than zero");
    expect(() => usdToUnits(Number.NaN)).toThrow("amount must be greater than zero");
    expect(() => usdToUnits(1e-7)).toThrow("amount is below one USDC base unit");
    expect(() => usdToUnits("abc")).toThrow("not an amount");
  });
});
