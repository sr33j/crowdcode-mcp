/**
 * Signing with the local wallet: CrowdCode's signed owner API (EIP-191 over a
 * fixed request message) and EIP-3009 `transferWithAuthorization` (x402 `exact`
 * payments and gasless deposits). Byte-for-byte the formats of the Hermes
 * plugin's wallet.py; test/agents-signing.test.ts pins both to vectors computed
 * with it.
 *
 * The key itself stays in ~/.agentcash/wallet.json (src/wallet.ts loads it);
 * callers get an address and signatures.
 */

import { createHash, randomBytes } from "node:crypto";
import type { PrivateKeyAccount } from "viem/accounts";
import { getAddress } from "viem/utils";
import { ToolError } from "./errors.js";

export const BASE_CHAIN_ID = 8453;
export const BASE_CAIP2 = `eip155:${BASE_CHAIN_ID}`;
/** Circle's USDC on Base. Its EIP-712 domain is ("USD Coin", "2"). */
export const BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
export const USDC_DECIMALS = 6;

const TRANSFER_WITH_AUTHORIZATION = [
  { name: "from", type: "address" },
  { name: "to", type: "address" },
  { name: "value", type: "uint256" },
  { name: "validAfter", type: "uint256" },
  { name: "validBefore", type: "uint256" },
  { name: "nonce", type: "bytes32" },
] as const;

export interface Authorization {
  from: string;
  to: string;
  value: string;
  validAfter: string;
  validBefore: string;
  nonce: string;
}

export interface AuthorizeOptions {
  to: string;
  value: bigint;
  validSeconds: number;
  asset?: string;
  chainId?: number;
  tokenName?: string;
  tokenVersion?: string;
  /** Fixed clock and nonce, for test vectors only. */
  now?: number;
  nonce?: string;
}

/** The exact text the request signature covers. */
export function requestMessage(
  method: string,
  path: string,
  bodySha256: string,
  timestamp: string,
  nonce: string,
): string {
  return ["CrowdCode API request", `${method.toUpperCase()} ${path}`, bodySha256, timestamp, nonce].join("\n");
}

export function sha256Hex(body: Uint8Array): string {
  return body.length ? createHash("sha256").update(body).digest("hex") : "";
}

export class Signer {
  constructor(private readonly account: PrivateKeyAccount) {}

  /** Checksummed. */
  get address(): string {
    return getAddress(this.account.address);
  }

  get caip10(): string {
    return `${BASE_CAIP2}:${this.address}`;
  }

  /** EIP-191 personal_sign over the UTF-8 text, 0x-hex. */
  signMessage(text: string): Promise<string> {
    return this.account.signMessage({ message: text });
  }

  /** Headers for the CrowdCode signed owner API (Wallet API v1). */
  async signRequest(
    method: string,
    path: string,
    body: Uint8Array,
    fixed: { now?: number; nonce?: string } = {},
  ): Promise<Record<string, string>> {
    const timestamp = String(Math.floor(fixed.now ?? Date.now() / 1000));
    const nonce = fixed.nonce ?? randomBytes(16).toString("hex");
    const message = requestMessage(method, path, sha256Hex(body), timestamp, nonce);
    return {
      "X-CrowdCode-Address": this.caip10,
      "X-CrowdCode-Timestamp": timestamp,
      "X-CrowdCode-Nonce": nonce,
      "X-CrowdCode-Signature": await this.signMessage(message),
    };
  }

  /**
   * Sign an EIP-3009 `transferWithAuthorization`. It can only move exactly
   * `value` to `to`, once, before `validBefore`. String fields, as x402 and the
   * deposits endpoint expect.
   */
  async authorizeTransfer(options: AuthorizeOptions): Promise<{ authorization: Authorization; signature: string }> {
    if (options.value <= 0n) throw new ToolError("value must be positive");
    const now = Math.floor(options.now ?? Date.now() / 1000);
    const nonce = (options.nonce ?? "0x" + randomBytes(32).toString("hex")) as `0x${string}`;
    const validBefore = BigInt(now + Math.max(30, Math.trunc(options.validSeconds)));
    const authorization: Authorization = {
      from: this.address,
      to: getAddress(options.to),
      value: options.value.toString(),
      validAfter: "0",
      validBefore: validBefore.toString(),
      nonce,
    };
    const signature = await this.account.signTypedData({
      domain: {
        name: options.tokenName ?? "USD Coin",
        version: options.tokenVersion ?? "2",
        chainId: options.chainId ?? BASE_CHAIN_ID,
        verifyingContract: getAddress(options.asset ?? BASE_USDC),
      },
      types: { TransferWithAuthorization: TRANSFER_WITH_AUTHORIZATION },
      primaryType: "TransferWithAuthorization",
      message: {
        from: authorization.from as `0x${string}`,
        to: authorization.to as `0x${string}`,
        value: options.value,
        validAfter: 0n,
        validBefore,
        nonce,
      },
    });
    return { authorization, signature };
  }
}

/** Dollars to USDC base units, refusing anything that is not a positive amount (rounds down, like Decimal). */
export function usdToUnits(amount: number | string): bigint {
  if (typeof amount === "number" && !Number.isFinite(amount)) {
    throw new ToolError("amount must be greater than zero");
  }
  const text = String(amount).trim();
  const match = /^([+-]?)(\d*)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/.exec(text);
  if (!match || (!match[2] && !match[3])) throw new ToolError(`not an amount: ${JSON.stringify(amount)}`);
  const [, sign, whole = "", fraction = "", expText = "0"] = match;
  const digits = BigInt((whole + fraction).replace(/^0+(?=\d)/, "") || "0");
  if (sign === "-" || digits === 0n) throw new ToolError("amount must be greater than zero");
  const shift = Number(expText) - fraction.length + USDC_DECIMALS;
  const units = shift >= 0 ? digits * 10n ** BigInt(shift) : digits / 10n ** BigInt(-shift);
  if (units <= 0n) throw new ToolError("amount is below one USDC base unit");
  return units;
}

export function unitsToUsd(units: bigint | number | string): number {
  return Number(BigInt(units)) / 10 ** USDC_DECIMALS;
}
