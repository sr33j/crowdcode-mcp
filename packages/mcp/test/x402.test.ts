import { describe, expect, it } from "vitest";
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
