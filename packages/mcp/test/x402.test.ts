import { describe, expect, it } from "vitest";
import { x402Network, x402PaymentReference, x402Settlement } from "../src/canonical/x402.js";

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
    for (const value of ["solana", "eip155:1", "", null]) expect(x402Network(value)).toBeNull();
  });

  it("reads the payment-response header in any encoding", () => {
    const expected = { transaction: TX, network: "monad" };
    expect(x402Settlement(MONAD_HEADER)).toEqual(expected);
    expect(x402Settlement(JSON.stringify(MONAD_RESPONSE))).toEqual(expected);
    expect(x402Settlement(MONAD_RESPONSE)).toEqual(expected);
    expect(x402Settlement({ transaction: TX })).toEqual({ transaction: TX, network: "base" });
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
