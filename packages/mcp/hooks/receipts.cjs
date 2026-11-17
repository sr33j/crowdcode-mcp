"use strict";
/**
 * CrowdCode: recognize a paid call in any tool's output by its payment
 * receipt, never by the tool's name. Shared by the PostToolUse hook (which
 * records the call) and the Stop hook (which asks for a rating).
 *
 * - x402: the `PAYMENT-RESPONSE` / `X-PAYMENT-RESPONSE` header, base64 JSON
 *   `{success, transaction, network, payer}`.
 * - MPP: the `Payment-Receipt` header, base64url JSON
 *   `{method, reference, status, timestamp}`.
 * - AgentCash also summarizes either one as `paymentInfo`
 *   `{protocol, network, price, payment: {success, transactionHash}}`.
 *
 * Only metadata is extracted: where the call went, which settlement paid for
 * it, and how big the answer was. Never the request or the response.
 * Self-contained: Node built-ins only, no network.
 */

const MAX_DEPTH = 8;
const MAX_STRINGS = 400;
const TX_RE = /^0x[0-9a-fA-F]{64}$/;
const SOLANA_SIG_RE = /^[1-9A-HJ-NP-Za-km-z]{64,90}$/;
const HEADER_TEXT_RE = /(x-payment-response|payment-response|payment-receipt)["'\s]*[:=]\s*["']?([A-Za-z0-9+/_-]{20,}={0,2})/gi;

function decodeJson(value) {
  if (typeof value !== "string") return null;
  const text = value.trim();
  if (!/^[A-Za-z0-9+/_-]+={0,2}$/.test(text)) return null;
  try {
    const parsed = JSON.parse(Buffer.from(text.replace(/-/g, "+").replace(/_/g, "/"), "base64").toString("utf8"));
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : null;
  } catch { return null; }
}

function normalizeNetwork(value) {
  const text = String(value || "").trim().toLowerCase();
  if (!text) return null;
  if (text === "base" || text === "eip155:8453" || text === "8453") return "base";
  if (text === "tempo" || text === "eip155:4217" || text === "4217") return "tempo";
  if (text === "solana" || text.startsWith("solana:5eykt4u")) return "solana";
  return text;
}

function fromX402(settle) {
  if (!settle || settle.success === false) return null;
  const reference = settle.transaction || settle.transactionHash || settle.txHash;
  if (typeof reference !== "string") return null;
  return { protocol: "x402", network: normalizeNetwork(settle.network) || "base", reference: reference.trim(),
    payer: typeof settle.payer === "string" ? settle.payer : null };
}

function fromMpp(receipt) {
  if (!receipt || (receipt.status && receipt.status !== "success")) return null;
  const reference = receipt.reference || receipt.txHash || receipt.transactionHash;
  if (typeof reference !== "string") return null;
  return { protocol: "mpp", network: "tempo", reference: reference.trim(), payer: null };
}

function fromPaymentInfo(info) {
  if (!info || typeof info !== "object") return null;
  const payment = info.payment;
  if (!payment || payment.success === false || typeof payment.transactionHash !== "string") return null;
  return { protocol: info.protocol === "mpp" ? "mpp" : "x402", network: normalizeNetwork(info.network) || "base",
    reference: payment.transactionHash.trim(), payer: null, price: info.price };
}

function validReference(found) {
  return found && (TX_RE.test(found.reference) || (found.network === "solana" && SOLANA_SIG_RE.test(found.reference)));
}

/** Every string and object in a tool result, including JSON nested in text blocks. */
function walk(value, visit, depth = 0, budget = { strings: 0 }) {
  if (depth > MAX_DEPTH || value === null || value === undefined) return;
  if (typeof value === "string") {
    if (++budget.strings > MAX_STRINGS) return;
    visit(value, null);
    const text = value.trim();
    if ((text.startsWith("{") || text.startsWith("[")) && text.length < 2_000_000) {
      try { walk(JSON.parse(text), visit, depth + 1, budget); } catch { /* plain text */ }
    }
    return;
  }
  if (Array.isArray(value)) { for (const item of value) walk(item, visit, depth + 1, budget); return; }
  if (typeof value === "object") {
    visit(null, value);
    for (const key of Object.keys(value)) walk(value[key], visit, depth + 1, budget);
  }
}

/**
 * The payment receipt in one tool result, or null.
 * Returns {protocol, network, reference, payer, price?, status}.
 */
function findReceipt(toolResponse) {
  let found = null;
  let price;
  let status = null;
  walk(toolResponse, (text, object) => {
    if (object) {
      // AgentCash spreads its paymentInfo into the result metadata.
      if ((object.protocol === "x402" || object.protocol === "mpp") && object.payment) {
        const info = fromPaymentInfo(object);
        if (info && info.price !== undefined) price = info.price;
        if (!found && info) found = info;
      }
      for (const [key, value] of Object.entries(object)) {
        const name = key.toLowerCase();
        if (!found && (name === "payment-response" || name === "x-payment-response")) found = fromX402(decodeJson(value));
        else if (!found && name === "payment-receipt") found = fromMpp(decodeJson(value));
        else if (name === "paymentinfo") {
          const info = fromPaymentInfo(value);
          if (info && info.price !== undefined) price = info.price;
          if (!found && info) found = info;
        } else if ((name === "status" || name === "statuscode" || name === "http_status") &&
            Number.isInteger(value) && value >= 100 && value <= 599 && status === null) status = value;
      }
      return;
    }
    if (!found && text && /payment-(response|receipt)/i.test(text)) {
      HEADER_TEXT_RE.lastIndex = 0;
      let match;
      while (!found && (match = HEADER_TEXT_RE.exec(text))) {
        const decoded = decodeJson(match[2]);
        found = match[1].toLowerCase() === "payment-receipt" ? fromMpp(decoded) : fromX402(decoded);
      }
    }
  });
  if (!validReference(found)) return null;
  if (price !== undefined && found.price === undefined) found.price = price;
  found.status = status;
  return found;
}

/** USD like "$0.01" or 0.01 to USDC atomic units (6 decimals), or null. */
function priceToAtomic(price) {
  const text = typeof price === "number" ? String(price) : typeof price === "string" ? price.replace(/[$,\s]/g, "") : "";
  if (!/^\d+(\.\d+)?$/.test(text)) return null;
  const atomic = Math.round(Number(text) * 1e6);
  return Number.isSafeInteger(atomic) && atomic >= 0 ? atomic : null;
}

/** The endpoint a tool called, without query or fragment (they can hold the request). */
function endpointOf(toolInput) {
  if (!toolInput || typeof toolInput !== "object") return null;
  for (const key of ["url", "endpoint", "resource", "uri", "api_endpoint"]) {
    const value = toolInput[key];
    if (typeof value !== "string") continue;
    try {
      const url = new URL(value.trim());
      if (url.protocol !== "https:") return null;
      return `${url.protocol}//${url.host}${url.pathname.replace(/\/+$/, "") || "/"}`;
    } catch { return null; }
  }
  return null;
}

function methodOf(toolInput) {
  const method = toolInput && typeof toolInput.method === "string" ? toolInput.method.trim().toUpperCase() : "";
  return /^[A-Z]{3,7}$/.test(method) ? method : null;
}

/** Cheap pre-check on the raw hook input, before any parsing. */
function mayHoldReceipt(raw) {
  return /payment-(response|receipt)|paymentInfo|transactionHash/i.test(raw);
}

module.exports = { findReceipt, priceToAtomic, endpointOf, methodOf, mayHoldReceipt, decodeJson };
