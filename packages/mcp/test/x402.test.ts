/**
 * x402 on the wire: the settlement a payment-response proof names, the
 * payment_reference signed for it, and the PAYMENT-REQUIRED offers and
 * PAYMENT-SIGNATURE payloads of the agent client's x402 payments.
 *
 * The examples are recorded mainnet settlements (the regression seeds). The
 * properties generate proofs, challenges and payments and check them against
 * @x402/core, the library sellers and the hosted gateway encode and decode
 * them with: whatever it encodes we read the same way, and whatever we send
 * it decodes to the offer we were quoted, signed for exactly that amount.
 */

import { once } from "node:events";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import fc from "fast-check";
import {
  decodePaymentRequiredHeader,
  decodePaymentResponseHeader,
  decodePaymentSignatureHeader,
  encodePaymentRequiredHeader,
  encodePaymentResponseHeader,
} from "@x402/core/http";
import { getAddress, verifyTypedData } from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { PublicClient } from "../src/agents/client.js";
import { BASE_USDC, Signer } from "../src/agents/signer.js";
import { x402Network, x402PaymentReference, x402ProofPayer, x402Settlement } from "../src/canonical/x402.js";
import {
  base58Decode,
  base58Encode,
  isSolanaAddress,
  isSolanaSignature,
  solanaReferenceSignature,
} from "../src/canonical/solana.js";

// Real Monad mainnet x402 settlement (agent402.tools); see
// tests/test_monad_x402.py for the recorded receipt.
const TX = "0x1e24b5403debdc44f5da23a9cb5f2d59ac0d6620dae6f172382a8a58cb181c5c";
const MONAD_RESPONSE = {
  success: true,
  payer: "0x8443b3E7B593ac30b0AD759802767E7AA17C987d",
  transaction: TX,
  network: "eip155:143",
};
const MONAD_HEADER = Buffer.from(JSON.stringify(MONAD_RESPONSE)).toString("base64");

describe("x402 references", () => {
  it("maps network aliases", () => {
    for (const value of ["monad", "MONAD", "eip155:143", "143", 143]) {
      expect(x402Network(value)).toBe("monad");
    }
    for (const value of ["base", "eip155:8453", "8453"]) expect(x402Network(value)).toBe("base");
    for (const value of ["solana", "SOLANA", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"]) {
      expect(x402Network(value)).toBe("solana");
    }
    for (const value of ["solana:devnet", "eip155:1", "", null]) expect(x402Network(value)).toBeNull();
  });

  it("reads the payment-response header in any encoding", () => {
    const expected = { transaction: TX, network: "monad" };
    expect(x402Settlement(MONAD_HEADER)).toEqual(expected);
    expect(x402Settlement(JSON.stringify(MONAD_RESPONSE))).toEqual(expected);
    expect(x402Settlement(MONAD_RESPONSE)).toEqual(expected);
    expect(x402Settlement({ transaction: TX })).toEqual({ transaction: TX, network: "base" });
    // Solana settlements are base58 signatures, never 0x hashes.
    expect(x402Settlement({ transaction: TX, network: "solana" })).toBeNull();
    expect(x402Settlement("not a proof")).toBeNull();
  });

  it("emits x402:monad:<tx> for Monad settlements", () => {
    expect(x402PaymentReference(TX, MONAD_HEADER)).toBe(`x402:monad:${TX}`);
    expect(x402PaymentReference(undefined, MONAD_HEADER)).toBe(`x402:monad:${TX}`);
    expect(x402PaymentReference(`x402:monad:${TX}`, MONAD_HEADER)).toBe(`x402:monad:${TX}`);
  });

  it("leaves Base, mismatched, and unparseable cases to the backend", () => {
    const base = JSON.stringify({ transaction: TX, network: "eip155:8453" });
    expect(x402PaymentReference(TX, base)).toBe(TX);
    expect(x402PaymentReference(null, base)).toBe(TX);
    const other = "0x" + "ef".repeat(32);
    expect(x402PaymentReference(other, MONAD_HEADER)).toBe(other);
    expect(x402PaymentReference(`x402:base:${TX}`, MONAD_HEADER)).toBe(`x402:base:${TX}`);
    expect(x402PaymentReference(TX, "bogus")).toBe(TX);
  });
});

// Real Solana mainnet x402 settlement (PayAI facilitator); see
// tests/test_solana_x402.py for the recorded transaction.
const SIG = "5eHrTHo8wSNNNf857kCqzX15YW1AaETvyDW5bAyv3EBZp57PFhXMGj4zoi9mYi9XGKEA5bCzNFTdm2mdaeLwH5uj";
const SOLANA_RESPONSE = Buffer.from(JSON.stringify({
  success: true,
  payer: "2b1JD7tcidGtAYCVm1dDKTgVHRoihSJ1uqz3jf9WwSLP",
  transaction: SIG,
  network: "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
})).toString("base64");

describe("x402 Solana references", () => {
  it("round-trips base58 and recognizes Solana shapes", () => {
    for (const bytes of [Uint8Array.from([0, 0, 1]), Uint8Array.from({ length: 32 }, (_, i) => i)]) {
      expect(base58Decode(base58Encode(bytes))).toEqual(bytes);
    }
    expect(base58Decode("0OIl")).toBeNull();
    expect(isSolanaSignature(SIG)).toBe(true);
    expect(isSolanaAddress("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")).toBe(true);
    expect(isSolanaAddress(SIG)).toBe(false);
    expect(isSolanaSignature(TX)).toBe(false);
  });

  it("reads a Solana payment-response header and keeps the signature's case", () => {
    expect(x402Settlement(SOLANA_RESPONSE)).toEqual({ transaction: SIG, network: "solana" });
    expect(x402ProofPayer(SOLANA_RESPONSE)).toBe("2b1JD7tcidGtAYCVm1dDKTgVHRoihSJ1uqz3jf9WwSLP");
    expect(x402ProofPayer(MONAD_HEADER)).toBe(MONAD_RESPONSE.payer);
    expect(x402ProofPayer({ transaction: TX })).toBeNull();
  });

  it("emits x402:solana:<signature> for Solana settlements", () => {
    expect(x402PaymentReference(undefined, SOLANA_RESPONSE)).toBe(`x402:solana:${SIG}`);
    expect(x402PaymentReference(SIG, SOLANA_RESPONSE)).toBe(`x402:solana:${SIG}`);
    expect(x402PaymentReference(`x402:solana:${SIG}`, SOLANA_RESPONSE)).toBe(`x402:solana:${SIG}`);
    // A differently-cased signature is a different transaction: left alone.
    expect(x402PaymentReference(SIG.toLowerCase(), SOLANA_RESPONSE)).toBe(SIG.toLowerCase());
  });

  it("extracts the signature from Solana references", () => {
    expect(solanaReferenceSignature(`x402:solana:${SIG}`)).toBe(SIG);
    expect(solanaReferenceSignature(`X402:Solana:${SIG}`)).toBe(SIG);
    expect(solanaReferenceSignature(SIG)).toBe(SIG);
    expect(solanaReferenceSignature(TX)).toBeNull();
    expect(solanaReferenceSignature(`x402:monad:${TX}`)).toBeNull();
    expect(solanaReferenceSignature(null)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Generators
// ---------------------------------------------------------------------------

const bytesHex = (n: number) => fc.uint8Array({ minLength: n, maxLength: n }).map((b) => Buffer.from(b).toString("hex"));
/** An EVM transaction hash in any letter case. */
const evmHash = fc.tuple(bytesHex(32), fc.uint8Array({ minLength: 64, maxLength: 64 })).map(
  ([h, flips]) => "0x" + [...h].map((c, i) => (flips[i]! % 2 ? c.toUpperCase() : c)).join(""),
);
const solanaSig = fc.uint8Array({ minLength: 64, maxLength: 64 }).map((b) => base58Encode(b)).filter(isSolanaSignature);
const junkTx = fc.oneof(
  fc.string({ unit: "binary", maxLength: 20 }),
  bytesHex(31).map((h) => "0x" + h),
  bytesHex(33).map((h) => "0x" + h),
  fc.uint8Array({ minLength: 32, maxLength: 32 }).map(base58Encode),
);
/** Spellings of each supported network, as sellers write them. */
const SPELLINGS: Record<string, Array<string | number>> = {
  base: ["base", "eip155:8453", "8453", 8453],
  monad: ["monad", "eip155:143", "143", 143],
  solana: ["solana", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"],
};
const caseOf = (value: string, flips: number) =>
  [...value].map((c, i) => ((flips >> i % 30) & 1 ? c.toUpperCase() : c.toLowerCase())).join("");
const spelled = (label: string) =>
  fc.tuple(fc.constantFrom(...SPELLINGS[label]!), fc.nat(), fc.constantFrom("", " ", "\t")).map(([v, flips, pad]) =>
    typeof v === "number" ? v : pad + caseOf(v, flips) + pad,
  );
const network = fc.oneof(
  ...Object.keys(SPELLINGS).map((label) => spelled(label).map((value) => ({ label: label as string | null, value: value as unknown }))),
  fc.constant({ label: "base", value: undefined }),
  fc.constantFrom("eip155:1", "solana:devnet", "eip155:84532", "tempo", "").map((value) => ({ label: null, value })),
);
const settleResponse = fc
  .record({
    success: fc.boolean(),
    network,
    transaction: fc.oneof(evmHash, solanaSig, junkTx),
    payer: fc.option(fc.oneof(fc.string({ maxLength: 50 }), bytesHex(20).map((h) => ` 0x${h} `)), { nil: undefined }),
    errorReason: fc.option(fc.string({ maxLength: 20 }), { nil: undefined }),
    extensionResponses: fc.option(fc.dictionary(fc.string({ maxLength: 5 }), fc.jsonValue({ maxDepth: 2 })), { nil: undefined }),
  })
  .map(({ network: n, ...rest }) => ({ label: n.label, response: { ...rest, network: n.value } }));

const validFor = (label: string, tx: string) => (label === "solana" ? isSolanaSignature(tx) : /^0x[0-9a-fA-F]{64}$/.test(tx));

// ---------------------------------------------------------------------------
// Properties: payment-response proofs
// ---------------------------------------------------------------------------

describe("properties: payment-response proofs against @x402/core", () => {
  it("reads any settlement @x402/core encodes, in every encoding, and nothing it should not", () => {
    fc.assert(
      fc.property(settleResponse, ({ label, response }) => {
        const header = encodePaymentResponseHeader(response as never);
        const decoded = decodePaymentResponseHeader(header) as Record<string, unknown>;
        const tx = decoded.transaction as string;
        const expected = label !== null && validFor(label, tx) ? { transaction: tx, network: label } : null;
        expect(x402Settlement(header)).toEqual(expected);
        expect(x402Settlement(decoded)).toEqual(expected);
        expect(x402Settlement(JSON.stringify(decoded))).toEqual(expected);
        const payer = typeof decoded.payer === "string" && decoded.payer.trim() !== "" ? decoded.payer.trim() : null;
        expect(x402ProofPayer(header)).toBe(payer);
      }),
    );
  });

  it("signs a reference that names its chain, round-trips, and matches EVM hashes in any case but Solana signatures exactly", () => {
    const settled = fc
      .constantFrom("base", "monad", "solana")
      .chain((label) => fc.record({ label: fc.constant(label), network: spelled(label), tx: label === "solana" ? solanaSig : evmHash }));
    fc.assert(
      fc.property(settled, fc.nat(), fc.constantFrom("", " ", "\n"), evmHash, (s, flips, pad, unrelated) => {
        const header = encodePaymentResponseHeader({ success: true, transaction: s.tx, network: s.network } as never);
        const prefix = s.label === "base" ? "" : `x402:${s.label}:`;
        const reference = x402PaymentReference(undefined, header);
        expect(reference).toBe(prefix + s.tx);
        expect(x402PaymentReference(null, header)).toBe(reference);
        // Signing again with the reference we produced changes nothing.
        expect(x402PaymentReference(reference, header)).toBe(reference);
        expect(solanaReferenceSignature(reference)).toBe(s.label === "solana" ? s.tx : null);
        // A caller's spelling of the same settlement keeps its case and gains the prefix (fe8dce1: hex is case-insensitive).
        const respelled = s.label === "solana" ? s.tx : "0x" + caseOf(s.tx.slice(2), flips);
        expect(x402PaymentReference(pad + respelled + pad, header)).toBe(prefix === "" ? pad + respelled + pad : prefix + respelled);
        // A Solana signature in another case is another transaction: left for the backend.
        const othercase = s.tx.toLowerCase() === s.tx ? s.tx.toUpperCase() : s.tx.toLowerCase();
        if (s.label === "solana" && othercase !== s.tx) expect(x402PaymentReference(othercase, header)).toBe(othercase);
        // Any other reference is the backend's to judge.
        if (unrelated.toLowerCase() !== s.tx.toLowerCase()) expect(x402PaymentReference(unrelated, header)).toBe(unrelated);
      }),
    );
  });
});

// ---------------------------------------------------------------------------
// Properties: the agent client's x402 payments (src/agents/client.ts)
// ---------------------------------------------------------------------------

const payer = new Signer(privateKeyToAccount("0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"));
const UINT256_MAX = 2n ** 256n - 1n;
const address = fc.tuple(bytesHex(20), fc.constantFrom("lower", "upper", "checksum")).map(([h, spelling]) =>
  spelling === "lower" ? "0x" + h : spelling === "upper" ? "0x" + h.toUpperCase() : getAddress("0x" + h),
);
const baseUsdc = fc.constantFrom(BASE_USDC, BASE_USDC.toLowerCase(), "0x" + BASE_USDC.slice(2).toUpperCase());
const extra = fc.record(
  // Non-empty: see the T5 note in agents-signing.test.ts (viem drops an empty domain field).
  { name: fc.string({ unit: "binary", minLength: 1, maxLength: 10 }), version: fc.string({ minLength: 1, maxLength: 3 }) },
  { requiredKeys: [] },
);
const baseOffer = fc.record({
  scheme: fc.constant("exact"),
  network: fc.constant("eip155:8453"),
  asset: baseUsdc,
  amount: fc.oneof(fc.constantFrom(1n, 2n ** 53n + 1n, UINT256_MAX), fc.bigInt({ min: 1n, max: UINT256_MAX })).map(String),
  payTo: address,
  maxTimeoutSeconds: fc.oneof(fc.integer({ min: 1, max: 5000 }), fc.constant(0)),
  extra,
});
/** Offers the client must pass over: other schemes, chains and tokens. */
const otherOffer = fc.oneof(
  baseOffer.map((o) => ({ ...o, scheme: "upto" })),
  baseOffer.map((o) => ({ ...o, network: "eip155:84532" })),
  baseOffer.chain((o) => address.map((asset) => ({ ...o, asset }))),
  baseOffer.map((o) => ({ ...o, network: "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp" })),
);
const challenge = fc.record({
  version: fc.constantFrom(1, 2),
  before: fc.array(otherOffer, { maxLength: 3 }),
  offer: baseOffer,
  after: fc.array(fc.oneof(otherOffer, baseOffer), { maxLength: 2 }),
  resource: fc.record(
    {
      url: fc.webUrl(),
      description: fc.string({ unit: "binary", maxLength: 40 }),
      mimeType: fc.constantFrom("application/json", "text/plain"),
    },
    { requiredKeys: ["url"] },
  ),
  error: fc.option(fc.string({ unit: "binary", maxLength: 20 }), { nil: undefined }),
  extensions: fc.option(fc.dictionary(fc.string({ maxLength: 6 }), fc.jsonValue({ maxDepth: 2 }), { maxKeys: 3 }), { nil: undefined }),
  body: fc.dictionary(fc.string({ maxLength: 6 }), fc.oneof(fc.string({ unit: "binary", maxLength: 10 }), fc.double({ noNaN: true, noDefaultInfinity: true })), { maxKeys: 3 }),
  tx: bytesHex(32).map((h) => "0x" + h),
});
type Challenge = typeof challenge extends fc.Arbitrary<infer T> ? T : never;

/** A seller speaking x402 through @x402/core (v2: PAYMENT-REQUIRED header; v1: the challenge in the body). */
let scenario: Challenge | null = null;
let received: { body: string; payment: Record<string, any> } | null = null;
let server: Server;
let url: string;

function required(c: Challenge): Record<string, any> {
  const accepts = [...c.before, c.offer, ...c.after].map((o) =>
    c.version === 1 ? { ...Object.fromEntries(Object.entries(o).filter(([k]) => k !== "amount")), maxAmountRequired: o.amount } : o,
  );
  return { x402Version: c.version, resource: c.resource, accepts, ...(c.error === undefined ? {} : { error: c.error }), ...(c.extensions === undefined ? {} : { extensions: c.extensions }) };
}

beforeAll(async () => {
  server = createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", (c: Buffer) => chunks.push(c));
    req.on("end", () => {
      const c = scenario!;
      const signature = req.headers["payment-signature"];
      if (typeof signature !== "string") {
        if (c.version === 1) {
          res.writeHead(402, { "content-type": "application/json" }).end(JSON.stringify(required(c)));
        } else {
          res.writeHead(402, { "PAYMENT-REQUIRED": encodePaymentRequiredHeader(required(c) as never) }).end("{}");
        }
        return;
      }
      received = { body: Buffer.concat(chunks).toString("utf8"), payment: decodePaymentSignatureHeader(signature) as never };
      const settlement = { success: true, transaction: c.tx, network: "eip155:8453", payer: received.payment.payload?.authorization?.from };
      res.writeHead(200, { "PAYMENT-RESPONSE": encodePaymentResponseHeader(settlement as never), "content-type": "application/json" }).end('{"ok":true}');
    });
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  url = `http://127.0.0.1:${(server.address() as AddressInfo).port}/s/crowdcode/agents`;
});

afterAll(() => {
  server?.close();
});

describe("properties: x402 payments against an @x402/core seller", () => {
  it("pays the first Base USDC exact offer, exactly as quoted, with an authorization that verifies", async () => {
    const client = new PublicClient("http://127.0.0.1", payer);
    await fc.assert(
      fc.asyncProperty(challenge, async (c) => {
        scenario = c;
        received = null;
        const quote = await client.quote(url, c.body);
        // What the seller's library makes of its own challenge.
        const asSent = c.version === 2
          ? (decodePaymentRequiredHeader(encodePaymentRequiredHeader(required(c) as never)) as Record<string, any>)
          : (JSON.parse(JSON.stringify(required(c))) as Record<string, any>);
        const first = (asSent.accepts as Array<Record<string, any>>).find(
          (o) => o.scheme === "exact" && o.network === "eip155:8453" && String(o.asset).toLowerCase() === BASE_USDC.toLowerCase(),
        )!;
        const offer = c.version === 1 ? { ...first, amount: first.maxAmountRequired } : first;
        expect(quote.offer).toEqual(offer);

        const startedAt = Math.floor(Date.now() / 1000);
        const payment = await client.pay(url, c.body, quote);
        expect(payment.status).toBe(200);
        expect(payment.settlement).toEqual({ success: true, transaction: c.tx, network: "eip155:8453", payer: payer.address });
        const sent = received!.payment;
        expect(sent.x402Version).toBe(c.version);
        expect(sent.accepted).toEqual(offer);
        expect(sent.resource).toEqual(asSent.resource);
        if (asSent.extensions !== undefined) expect(sent.extensions).toEqual(asSent.extensions);
        else expect(sent).not.toHaveProperty("extensions");
        expect(JSON.parse(received!.body)).toEqual(JSON.parse(JSON.stringify(c.body)));

        const auth = sent.payload.authorization as Record<string, string>;
        expect(auth.from).toBe(payer.address);
        expect(auth.to).toBe(getAddress(offer.payTo));
        expect(BigInt(auth.value!)).toBe(BigInt(offer.amount));
        const lifetime = Number(auth.validBefore) - startedAt;
        const asked = Math.min(Math.trunc(Number(offer.maxTimeoutSeconds || 300)), 1500);
        expect(lifetime).toBeGreaterThanOrEqual(Math.max(30, asked) - 1);
        expect(lifetime).toBeLessThanOrEqual(Math.max(30, asked) + 5);
        const valid = await verifyTypedData({
          address: payer.address as `0x${string}`,
          signature: sent.payload.signature,
          domain: {
            name: (offer.extra?.name as string | undefined) ?? "USD Coin",
            version: (offer.extra?.version as string | undefined) ?? "2",
            chainId: 8453,
            verifyingContract: getAddress(offer.asset),
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
          primaryType: "TransferWithAuthorization",
          message: {
            from: auth.from as `0x${string}`,
            to: auth.to as `0x${string}`,
            value: BigInt(auth.value!),
            validAfter: BigInt(auth.validAfter!),
            validBefore: BigInt(auth.validBefore!),
            nonce: auth.nonce as `0x${string}`,
          },
        });
        expect(valid).toBe(true);
      }),
      { numRuns: 30 },
    );
  });

  it("refuses a challenge with no Base USDC exact offer, and pays nothing", async () => {
    const client = new PublicClient("http://127.0.0.1", payer);
    await fc.assert(
      fc.asyncProperty(challenge, fc.array(otherOffer, { minLength: 1, maxLength: 4 }), async (c, others) => {
        // Every offer is one the client must pass over.
        scenario = { ...c, before: others.slice(1), after: [], offer: others[0]! as Challenge["offer"] };
        received = null;
        await expect(client.quote(url, c.body)).rejects.toThrow("no x402 exact USDC-on-Base offer");
        expect(received).toBeNull();
      }),
      { numRuns: 20 },
    );
  });
});
