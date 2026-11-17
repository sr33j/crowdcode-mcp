/**
 * HTTP clients: the signed CrowdCode owner API, x402 `exact` payments, the
 * public demand list and one read-only Base balance call. Ported from the
 * Hermes plugin's client.py.
 *
 * Every network call of the agent tools is made here or in account.ts, to:
 * - the CrowdCode origin (CROWDCODE_APP_URL, default https://crowdcode.app):
 *   `/api/app/v1` (signed or bearer), paid creation at `/s/crowdcode/agents`,
 *   the public `/api/service-proposals` list, and the OAuth endpoints;
 * - a Base JSON-RPC endpoint (CROWDCODE_BASE_RPC_URL, default
 *   https://mainnet.base.org) for one `balanceOf` call.
 * Redirects are never followed, so a signed request or a bearer token never
 * goes anywhere else.
 */

import { BASE_CAIP2, BASE_USDC, type Signer, unitsToUsd } from "./signer.js";
import { WalletMissing } from "./errors.js";
import { jsonBody, pyDumps } from "./pyjson.js";

export const USER_AGENT = "crowdcode-mcp (agents)";
export const TIMEOUT_MS = 30_000;
export const PAY_TIMEOUT_MS = 120_000;
export const STREAM_TIMEOUT_MS = 300_000;

export class ApiError extends Error {
  override name = "ApiError";
  constructor(
    readonly status: number,
    readonly error: string,
    readonly detail: unknown = undefined,
  ) {
    super(`${status} ${error}`);
  }
}

export type Json = Record<string, any>;

/** What the tools need from either credential: wallet signatures or an account sign-in. */
export interface OwnerApi {
  readonly wallet: Signer | null;
  readonly baseUrl: string;
  signed(method: string, path: string, payload?: unknown, extraHeaders?: Record<string, string>): Promise<any>;
  /** GET `/api/app/v1<path>`; the response body as a stream (status already checked). */
  signedStream(path: string): Promise<AsyncIterable<Uint8Array>>;
}

export async function http(url: string, init: RequestInit, timeoutMs = TIMEOUT_MS): Promise<Response> {
  const headers = new Headers(init.headers);
  headers.set("user-agent", USER_AGENT);
  return fetch(url, { ...init, headers, redirect: "manual", signal: AbortSignal.timeout(timeoutMs) });
}

export async function decodeError(response: Response): Promise<ApiError> {
  const text = await response.text().catch(() => "");
  let body: unknown;
  try {
    body = JSON.parse(text);
  } catch {
    body = { error: text.trim().slice(0, 300) || response.statusText || String(response.status) };
  }
  const error = body && typeof body === "object" && !Array.isArray(body) ? (body as Json).error : undefined;
  return new ApiError(response.status, String(error || response.statusText || response.status), body);
}

/** The JSON body of a successful response; `{}` for an empty one. Redirects count as errors. */
export async function readJson(response: Response): Promise<any> {
  if (response.status >= 300) {
    if (response.status < 400) {
      await response.body?.cancel().catch(() => undefined);
      throw new ApiError(response.status, "unexpected redirect", response.headers.get("location"));
    }
    throw await decodeError(response);
  }
  const text = await response.text();
  return text ? JSON.parse(text) : {};
}

export async function streamOf(response: Response): Promise<AsyncIterable<Uint8Array>> {
  if (response.status >= 300) {
    if (response.status < 400) throw new ApiError(response.status, "unexpected redirect");
    throw await decodeError(response);
  }
  const body = response.body;
  if (!body) return (async function* () {})();
  return body as unknown as AsyncIterable<Uint8Array>;
}

function signedPath(url: string): string {
  const parts = new URL(url);
  return parts.pathname + (parts.search && parts.search !== "?" ? parts.search : "");
}

export interface Quote {
  required: Json;
  offer: Json;
}

export interface Payment {
  status: number;
  body: any;
  settlement: Json | null;
}

/** Calls that need no credential, plus x402 (signed with the wallet when there is one). */
export class PublicClient {
  constructor(
    readonly baseUrl: string,
    readonly wallet: Signer | null = null,
  ) {}

  get apiRoot(): string {
    return this.baseUrl.replace(/\/+$/, "") + "/api/app/v1";
  }

  protected needWallet(): Signer {
    if (!this.wallet) throw new WalletMissing(`No CrowdCode wallet yet. Run \`npx crowdcode-mcp wallet init\` to create one.`);
    return this.wallet;
  }

  async demand(): Promise<any> {
    return readJson(await http(this.baseUrl.replace(/\/+$/, "") + "/api/service-proposals", { method: "GET" }));
  }

  /** POST without payment and return the x402 offer we would pay (Base USDC `exact`). */
  async quote(url: string, payload: unknown): Promise<Quote> {
    const response = await http(url, {
      method: "POST",
      body: jsonBody(payload),
      headers: { "content-type": "application/json" },
    });
    if (response.status !== 402) {
      if (response.status >= 400) throw await decodeError(response);
      const text = (await response.text().catch(() => "")).slice(0, 300);
      throw new ApiError(response.status, "expected a 402 payment challenge", text);
    }
    const required = await paymentRequired(response);
    return { required, offer: pickBaseExact(required) };
  }

  /** Sign the quoted offer and POST again with `PAYMENT-SIGNATURE`. */
  async pay(url: string, payload: unknown, quote: Quote): Promise<Payment> {
    const wallet = this.needWallet();
    const { required, offer } = quote;
    const extra = (offer.extra ?? {}) as Json;
    const { authorization, signature } = await wallet.authorizeTransfer({
      to: String(offer.payTo),
      value: BigInt(String(offer.amount)),
      validSeconds: Math.min(Math.trunc(Number(offer.maxTimeoutSeconds || 300)), 1500),
      asset: String(offer.asset),
      chainId: Number(String(offer.network).split(":")[1]),
      tokenName: extra.name ?? "USD Coin",
      tokenVersion: extra.version ?? "2",
    });
    const payment: Json = {
      x402Version: required.x402Version ?? 2,
      resource: required.resource ?? null,
      accepted: offer,
      payload: { authorization, signature },
    };
    if (required.extensions) payment.extensions = required.extensions;
    const header = Buffer.from(pyDumps(payment, { compact: true })).toString("base64");
    const response = await http(
      url,
      {
        method: "POST",
        body: jsonBody(payload),
        headers: { "content-type": "application/json", "PAYMENT-SIGNATURE": header },
      },
      PAY_TIMEOUT_MS,
    );
    let settlement: Json | null = null;
    const raw = response.headers.get("payment-response") ?? response.headers.get("x-payment-response");
    if (raw) {
      try {
        settlement = JSON.parse(Buffer.from(raw, "base64").toString("utf8"));
      } catch {
        settlement = null;
      }
    }
    const text = await response.text().catch(() => "");
    let body: any;
    try {
      body = JSON.parse(text);
    } catch {
      body = { error: text.slice(0, 300) };
    }
    if (response.status === 402) {
      try {
        const challenge = await paymentRequired(response, body);
        body = { error: challenge.error || "payment refused", ...(body ?? {}) };
      } catch {
        // keep the body as it is
      }
    }
    return { status: response.status, body, settlement };
  }

  /** USDC on Base held by `address`, in dollars: one read-only `eth_call`. */
  async usdcBalance(rpcUrl: string, address: string): Promise<number> {
    const data = "0x70a08231" + address.toLowerCase().replace(/^0x/, "").padStart(64, "0");
    const call = { jsonrpc: "2.0", id: 1, method: "eth_call", params: [{ to: BASE_USDC, data }, "latest"] };
    const response = await http(rpcUrl, {
      method: "POST",
      body: JSON.stringify(call),
      headers: { "content-type": "application/json" },
    });
    const body = await readJson(response);
    const result = body?.result;
    if (typeof result !== "string") throw new ApiError(502, "base rpc returned no result", body);
    return unitsToUsd(BigInt(result === "0x" ? "0x0" : result));
  }
}

/** The signed owner API, authenticated by the local wallet. */
export class WalletClient extends PublicClient implements OwnerApi {
  async signed(method: string, path: string, payload?: unknown, extraHeaders: Record<string, string> = {}): Promise<any> {
    const wallet = this.needWallet();
    const url = this.apiRoot + path;
    const body = jsonBody(payload);
    const headers: Record<string, string> = {
      ...extraHeaders,
      ...(await wallet.signRequest(method, signedPath(url), body)),
    };
    if (body.length) headers["content-type"] = "application/json";
    const response = await http(url, { method: method.toUpperCase(), headers, body: body.length ? body : undefined });
    return readJson(response);
  }

  async signedStream(path: string): Promise<AsyncIterable<Uint8Array>> {
    const wallet = this.needWallet();
    const url = this.apiRoot + path;
    const headers = await wallet.signRequest("GET", signedPath(url), new Uint8Array());
    return streamOf(await http(url, { method: "GET", headers }, STREAM_TIMEOUT_MS));
  }
}

async function paymentRequired(response: Response, parsedBody?: unknown): Promise<Json> {
  const raw = response.headers.get("payment-required");
  if (raw) {
    try {
      return JSON.parse(Buffer.from(raw, "base64").toString("utf8"));
    } catch {
      throw new ApiError(402, "unreadable PAYMENT-REQUIRED header");
    }
  }
  let body = parsedBody;
  if (body === undefined) {
    // x402 v1 servers put the challenge in the body
    try {
      body = JSON.parse(await response.text());
    } catch {
      throw new ApiError(402, "402 without a payment challenge");
    }
  }
  if (!body || typeof body !== "object" || !("accepts" in body)) {
    throw new ApiError(402, "402 without a payment challenge", body);
  }
  return body as Json;
}

function pickBaseExact(required: Json): Json {
  for (const offer of (required.accepts ?? []) as Json[]) {
    if (
      offer?.scheme === "exact" &&
      offer.network === BASE_CAIP2 &&
      String(offer.asset ?? "").toLowerCase() === BASE_USDC.toLowerCase()
    ) {
      // v1 spelling
      if (!("amount" in offer) && "maxAmountRequired" in offer) return { ...offer, amount: offer.maxAmountRequired };
      return offer;
    }
  }
  throw new ApiError(402, "no x402 exact USDC-on-Base offer in the challenge", required.accepts);
}
