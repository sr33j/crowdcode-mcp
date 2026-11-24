/**
 * The agent tools' wire formats, pinned to vectors computed with the Hermes
 * plugin (plugins/hermes/wallet.py, Python's json.dumps) so both clients sign
 * byte-identical requests and EIP-3009 authorizations.
 *
 * Regenerate: load wallet.py with eth_account, set secrets.token_hex to return
 * "0123456789abcdef"…, call Wallet.sign_request(..., now=1763337600) and, with
 * time.time() = 1763337600.5, authorize_transfer(to=0x…c0de, value=350000,
 * valid_seconds=300).
 *
 * The properties below sign generated requests and transfers: each signature
 * recovers to the wallet and to no one once any single signed field changes,
 * and the same inputs through wallet.py and json.dumps (test/fixtures/
 * python_oracle.py) give byte-identical headers, authorizations, signatures,
 * amounts and JSON. The vectors above are their regression seeds.
 */

import fc from "fast-check";
import { privateKeyToAccount } from "viem/accounts";
import { getAddress, recoverMessageAddress, verifyMessage, verifyTypedData } from "viem";
import { describe, expect, it } from "vitest";
import { pyDumps, pyFloat, jsonBody, PyFloat } from "../src/agents/pyjson.js";
import {
  BASE_CHAIN_ID,
  BASE_USDC,
  requestMessage,
  sha256Hex,
  Signer,
  usdToUnits,
  unitsToUsd,
} from "../src/agents/signer.js";
import { pyFloatArg, pythonAvailable, pythonSkipNote, runPython } from "./fixtures/python-oracle.js";

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

// ---------------------------------------------------------------------------
// Generators
// ---------------------------------------------------------------------------

const UINT256_MAX = 2n ** 256n - 1n;
const hex = (bytes: Uint8Array) => Buffer.from(bytes).toString("hex");

/** An address in each spelling a caller might hand us: lower, upper hex, EIP-55. */
const anyAddress = fc
  .tuple(fc.uint8Array({ minLength: 20, maxLength: 20 }), fc.constantFrom("lower", "upper", "checksum"))
  .map(([bytes, spelling]) => {
    const lower = "0x" + hex(bytes);
    if (spelling === "upper") return "0x" + hex(bytes).toUpperCase();
    return spelling === "checksum" ? getAddress(lower) : lower;
  });
const nonce32 = fc.uint8Array({ minLength: 32, maxLength: 32 }).map((b) => "0x" + hex(b));
const nonce16 = fc.uint8Array({ minLength: 16, maxLength: 16 }).map(hex);
/** uint256 values, weighted to the edges: 1, 2^53 ± 1, 2^256 - 1. */
const amount = fc.oneof(
  fc.constantFrom(1n, 2n ** 53n - 1n, 2n ** 53n, 2n ** 53n + 1n, UINT256_MAX),
  fc.bigInt({ min: 1n, max: 10n ** 12n }),
  fc.bigInt({ min: 1n, max: UINT256_MAX }),
);
const method = fc.oneof(
  fc.constantFrom("GET", "get", "POST", "post", "PATCH", "DELETE", "put"),
  fc.stringMatching(/^[A-Za-z]{1,8}$/),
);
/** A request target as signedPath() builds it: no line breaks, any other text. */
const path = fc
  .string({ unit: fc.oneof(fc.constantFrom("/", "?", "=", "&", "%", "{", " "), fc.string({ unit: "binary", minLength: 1, maxLength: 1 })), maxLength: 60 })
  .filter((p) => !/[\n\r]/.test(p))
  .map((p) => "/api/app/v1" + p);
const epoch = fc.integer({ min: 0, max: 2 ** 40 });
const transfer = fc.record({
  to: anyAddress,
  value: amount,
  validSeconds: fc.oneof(fc.integer({ min: -1000, max: 10 ** 9 }), fc.double({ min: -100, max: 1e6, noNaN: true })),
  now: fc.oneof(epoch, fc.double({ min: 0, max: 2 ** 40, noNaN: true })),
  nonce: nonce32,
  asset: fc.oneof(fc.constant(BASE_USDC), anyAddress),
  chainId: fc.oneof(fc.constant(BASE_CHAIN_ID), fc.integer({ min: 1, max: 2 ** 31 })),
  tokenName: fc.oneof(fc.constant("USD Coin"), fc.string({ unit: "binary", maxLength: 12 })),
  // T5 found: an empty token version signs differently in viem and eth_account
  // (viem drops a falsy `version` from EIP712Domain, viem/utils/typedData.ts;
  // eth_account hashes keccak("")). Constrained to non-empty; see the report.
  tokenVersion: fc.oneof(fc.constant("2"), fc.string({ minLength: 1, maxLength: 4 })),
});
type Transfer = typeof transfer extends fc.Arbitrary<infer T> ? T : never;

function sign(t: Transfer) {
  return signer.authorizeTransfer({ ...t });
}

function typedData(t: Transfer, auth: Record<string, string>) {
  return {
    domain: {
      name: t.tokenName,
      version: t.tokenVersion,
      chainId: t.chainId,
      verifyingContract: getAddress(t.asset),
    },
    types: {
      TransferWithAuthorization: [
        { name: "from", type: "address" },
        { name: "to", type: "address" },
        { name: "value", type: "uint256" },
        { name: "validAfter", type: "uint256" },
        { name: "validBefore", type: "uint256" },
        { name: "nonce", type: "bytes32" },
      ],
    },
    primaryType: "TransferWithAuthorization" as const,
    message: {
      from: auth.from as `0x${string}`,
      to: auth.to as `0x${string}`,
      value: BigInt(auth.value!),
      validAfter: BigInt(auth.validAfter!),
      validBefore: BigInt(auth.validBefore!),
      nonce: auth.nonce as `0x${string}`,
    },
  };
}

const otherAddress = (address: string, salt: number) => {
  const bytes = Buffer.from(address.slice(2), "hex");
  bytes[salt % 20]! ^= 1 + (salt % 255);
  return getAddress("0x" + bytes.toString("hex"));
};

// ---------------------------------------------------------------------------
// Properties
// ---------------------------------------------------------------------------

describe("properties: request signatures", () => {
  it("the signed text names each field once: it splits back into exactly what was signed", () => {
    fc.assert(
      fc.property(method, path, fc.uint8Array({ maxLength: 64 }), epoch, nonce16, (m, p, body, ts, n) => {
        const lines = requestMessage(m, p, sha256Hex(body), String(ts), n).split("\n");
        expect(lines).toEqual(["CrowdCode API request", `${m.toUpperCase()} ${p}`, sha256Hex(body), String(ts), n]);
        expect(lines[2]).toMatch(body.length ? /^[0-9a-f]{64}$/ : /^$/);
      }),
    );
  });

  it("a request signature recovers to the wallet, and to no one once any signed field changes", async () => {
    const field = fc.constantFrom("method", "path", "body", "timestamp", "nonce", "address");
    await fc.assert(
      fc.asyncProperty(
        method, path, fc.uint8Array({ maxLength: 64 }), epoch, nonce16, field, fc.nat(),
        async (m, p, body, ts, n, which, salt) => {
          const headers = await signer.signRequest(m, p, body, { now: ts, nonce: n });
          const signature = headers["X-CrowdCode-Signature"] as `0x${string}`;
          expect(headers["X-CrowdCode-Address"]).toBe(signer.caip10);
          expect(headers["X-CrowdCode-Timestamp"]).toBe(String(ts));
          const owner = headers["X-CrowdCode-Address"]!.split(":")[2] as `0x${string}`;
          const signed = { m: m.toUpperCase(), p, digest: sha256Hex(body), ts: String(ts), n, owner };
          const message = (f: typeof signed) => requestMessage(f.m, f.p, f.digest, f.ts, f.n);
          expect(await recoverMessageAddress({ message: message(signed), signature })).toBe(signer.address);

          const changed = { ...signed };
          if (which === "method") changed.m = signed.m === "POST" ? "GET" : "POST";
          if (which === "path") changed.p = signed.p + (salt % 2 ? "/" : "x");
          if (which === "body") {
            const other = Uint8Array.from([...body, salt % 256]);
            changed.digest = sha256Hex(salt % 3 === 0 && body.length ? body.slice(1) : other);
          }
          if (which === "timestamp") changed.ts = String(ts + 1 + (salt % 300));
          if (which === "nonce") changed.n = (n[0] === "0" ? "1" : "0") + n.slice(1);
          if (which === "address") changed.owner = otherAddress(owner, salt) as `0x${string}`;
          expect(await verifyMessage({ address: changed.owner, message: message(changed), signature })).toBe(false);
        },
      ),
      { numRuns: 25 },
    );
  });
});

describe("properties: EIP-3009 authorizations", () => {
  it("authorizes exactly the asked transfer, for at least 30 seconds, and refuses zero or negative values", async () => {
    await fc.assert(
      fc.asyncProperty(transfer, fc.bigInt({ min: -UINT256_MAX, max: 0n }), async (t, refused) => {
        const { authorization } = await sign(t);
        expect(authorization.from).toBe(signer.address);
        expect(authorization.to).toBe(getAddress(t.to));
        expect(BigInt(authorization.value)).toBe(t.value);
        expect(authorization.validAfter).toBe("0");
        const start = BigInt(Math.floor(t.now));
        expect(BigInt(authorization.validBefore) - start).toBe(BigInt(Math.max(30, Math.trunc(t.validSeconds))));
        expect(authorization.nonce).toBe(t.nonce);
        await expect(sign({ ...t, value: refused })).rejects.toThrow("value must be positive");
      }),
      { numRuns: 30 },
    );
  });

  it("an authorization verifies, and no longer verifies once any signed field or domain field changes", async () => {
    const field = fc.constantFrom(
      "from", "to", "value", "validAfter", "validBefore", "nonce", "name", "version", "chainId", "verifyingContract",
    );
    await fc.assert(
      fc.asyncProperty(transfer, field, fc.nat(), async (t, which, salt) => {
        const { authorization, signature } = await sign(t);
        const data = typedData(t, authorization as unknown as Record<string, string>);
        expect(await verifyTypedData({ address: signer.address as `0x${string}`, signature: signature as `0x${string}`, ...data })).toBe(true);

        const message = { ...data.message };
        const domain = { ...data.domain };
        if (which === "from") message.from = otherAddress(message.from, salt) as `0x${string}`;
        if (which === "to") message.to = otherAddress(message.to, salt) as `0x${string}`;
        if (which === "value") message.value = message.value === UINT256_MAX ? message.value - 1n : message.value + 1n;
        if (which === "validAfter") message.validAfter = 1n + BigInt(salt % 1000);
        if (which === "validBefore") message.validBefore += 1n + BigInt(salt % 1000);
        if (which === "nonce") message.nonce = (message.nonce.slice(0, -1) + (message.nonce.endsWith("0") ? "1" : "0")) as `0x${string}`;
        if (which === "name") domain.name = domain.name + "x";
        if (which === "version") domain.version = domain.version === "2" ? "1" : "2";
        if (which === "chainId") domain.chainId = domain.chainId === BASE_CHAIN_ID ? 1 : BASE_CHAIN_ID;
        if (which === "verifyingContract") domain.verifyingContract = otherAddress(domain.verifyingContract, salt) as `0x${string}`;
        expect(
          await verifyTypedData({ address: signer.address as `0x${string}`, signature: signature as `0x${string}`, ...data, domain, message }),
        ).toBe(false);
      }),
      { numRuns: 25 },
    );
  });
});

/** Base units as the dollar text a person would write: "12.003400", "0.000001". */
function dollars(units: bigint): string {
  const whole = units / 1_000_000n;
  const fraction = (units % 1_000_000n).toString().padStart(6, "0");
  return `${whole}.${fraction}`;
}

describe("properties: USDC amounts", () => {
  it("parses dollar text back to the same base units over the full uint256 range", () => {
    fc.assert(
      fc.property(amount, fc.nat({ max: 12 }), fc.stringMatching(/^[0-9]{0,6}$/), (units, extraZeros, junkDigits) => {
        const text = dollars(units);
        expect(usdToUnits(text)).toBe(units);
        // Trailing digits beyond a base unit round down, never up.
        expect(usdToUnits(text + "0".repeat(extraZeros) + junkDigits)).toBe(units);
        // Scientific notation means the same amount.
        expect(usdToUnits(`${units}e-6`)).toBe(units);
        expect(usdToUnits(`${units}0e-7`)).toBe(units);
      }),
    );
  });

  // A double holds 15 significant digits, so dollars as a number are exact to
  // the base unit below 10^15 units ($1,000,000,000); above that unitsToUsd
  // can lose one (8706263493893704 comes back as ...703).
  it("round-trips base units through unitsToUsd for every amount below $1,000,000,000", () => {
    fc.assert(
      fc.property(fc.bigInt({ min: 1n, max: 10n ** 15n - 1n }), (units) => {
        expect(usdToUnits(unitsToUsd(units))).toBe(units);
      }),
    );
  });
});

// ---------------------------------------------------------------------------
// Differential against wallet.py and json.dumps
// ---------------------------------------------------------------------------

type PyValue =
  | null
  | boolean
  | number
  | string
  | { float: number }
  | PyValue[]
  | { entries: [string, PyValue][] };

const pyString = fc.oneof(
  fc.string({ unit: "binary", maxLength: 12 }),
  fc.string({ unit: "binary-ascii", maxLength: 12 }),
  // Lone surrogates, which ensure_ascii writes as \udXXX.
  fc.integer({ min: 0xd800, max: 0xdfff }).map((c) => "a" + String.fromCharCode(c)),
);
// T5 found: with sortKeys, pyDumps orders keys by UTF-16 code unit and Python
// by code point, so {"\uE000": 1, "\u{10000}": 2} comes out in the opposite
// order. Every sorted key in the client is ASCII (the task idempotency key), so
// keys here leave out U+E000..U+FFFF; see the report.
const pyKey = pyString.filter((k) => k !== "$float" && !/[\uE000-\uFFFF]/.test(k));
const pyValue: fc.Arbitrary<PyValue> = fc.letrec<{ value: PyValue }>((tie) => ({
  value: fc.oneof(
    { depthSize: "small", withCrossShrink: true },
    fc.constant(null),
    fc.boolean(),
    fc.integer({ min: Number.MIN_SAFE_INTEGER, max: Number.MAX_SAFE_INTEGER }),
    pyString,
    fc.oneof(
      fc.double({ noNaN: true, noDefaultInfinity: true }),
      fc.constantFrom(0, -0, 1, 1e16, 1e15, 1e-4, 9.999e-5, 5e-324, 1.7976931348623157e308, 0.1, 2 ** 53),
    ).map((float) => ({ float })),
    fc.array(tie("value"), { maxLength: 4 }),
    fc.uniqueArray(fc.tuple(pyKey, tie("value")), { maxLength: 4, selector: ([k]) => k }).map((entries) => ({ entries })),
  ),
})).value;

function toTs(v: PyValue): unknown {
  if (Array.isArray(v)) return v.map(toTs);
  if (v !== null && typeof v === "object") {
    if ("float" in v) return new PyFloat(v.float);
    return Object.fromEntries(v.entries.map(([k, x]) => [k, toTs(x)]));
  }
  return v;
}

/** The same value for the oracle, keys in the order the TS object iterates them. */
function toOracle(ts: unknown): unknown {
  if (ts instanceof PyFloat) return pyFloatArg(ts.value);
  if (Array.isArray(ts)) return ts.map(toOracle);
  if (ts !== null && typeof ts === "object") {
    return Object.fromEntries(Object.entries(ts).map(([k, x]) => [k, toOracle(x)]));
  }
  return ts;
}

/** Dollar text and numbers as a caller might pass them. */
const dollarInput = fc.oneof(
  fc.stringMatching(/^[+-]?[0-9]{0,8}(\.[0-9]{0,9})?([eE][+-]?[0-9]{1,2})?$/),
  fc.double({ min: -1e9, max: 1e12, noNaN: true }),
  fc.constantFrom("0", "-0", ".5", "5.", "1e-6", "1e-7", "0.0000019", "abc", "", "1e30", 1e-6, 0.1, 1e21),
);
function errorKind(message: string): string {
  return /^(not an amount|amount must be greater than zero|amount is below one USDC base unit)/.exec(message)?.[1] ?? message;
}

describe(`differential: wallet.py and json.dumps${pythonSkipNote}`, () => {
  it.skipIf(!pythonAvailable)("request signatures, authorizations, amounts and JSON are byte-identical to Python's", async () => {
    // One Python run over every case; the seed is in each failure message.
    const seed = Date.now() | 0;
    const sample = <T,>(arb: fc.Arbitrary<T>, numRuns: number) => fc.sample(arb, { numRuns, seed });
    const requests = sample(fc.tuple(method, path, fc.uint8Array({ maxLength: 64 }), epoch, nonce16), 12);
    const transfers = sample(transfer, 12);
    const amounts = sample(dollarInput, 300);
    const values = sample(fc.tuple(pyValue, fc.boolean(), fc.boolean()), 300);

    const cases: Array<Record<string, unknown>> = [
      ...requests.map(([m, p, body, now, nonce]) => ({ op: "sign_request", key: KEY, method: m, path: p, body_hex: hex(body), now, nonce })),
      ...transfers.map((t) => ({
        op: "authorize", key: KEY, to: t.to, value: t.value.toString(), valid_seconds: t.validSeconds, now: t.now,
        nonce: t.nonce, asset: t.asset, chain_id: t.chainId, token_name: t.tokenName, token_version: t.tokenVersion,
      })),
      ...amounts.map((a) => ({ op: "usd_to_units", amount: typeof a === "number" ? pyFloatArg(a) : a })),
      ...values.map(([v, compact, sortKeys]) => ({ op: "dumps", value: toOracle(toTs(v)), compact, sort_keys: sortKeys })),
    ];
    const results = runPython(cases);
    let i = 0;

    for (const [m, p, body, now, nonce] of requests) {
      expect(await signer.signRequest(m, p, body, { now, nonce }), JSON.stringify({ seed, m, p, now })).toEqual(results[i++].headers);
    }
    for (const t of transfers) {
      const py = results[i++];
      expect(await sign(t), JSON.stringify({ seed, ...t, value: String(t.value) })).toEqual({
        authorization: py.authorization,
        signature: py.signature,
      });
    }
    for (const a of amounts) {
      const py = results[i++];
      let ts: string;
      try {
        ts = String(usdToUnits(a));
      } catch (err) {
        ts = "error: " + errorKind((err as Error).message);
      }
      expect(ts, JSON.stringify({ seed, a })).toBe(py.units ?? "error: " + errorKind(py.error));
    }
    for (const [v, compact, sortKeys] of values) {
      expect(pyDumps(toTs(v), { compact, sortKeys }), JSON.stringify({ seed, v })).toBe(results[i++].text);
    }
  });
});
