"use strict";
/**
 * CrowdCode PostToolUse hook (Claude Code and Codex), installed by
 * `crowdcode-mcp install`.
 *
 * When any MCP tool's output carries an x402 or MPP payment receipt, append
 * one line of metadata to ~/.crowdcode/pending.jsonl:
 *   {endpoint, method, network, payment_reference, amount_atomic, status,
 *    response_bytes, observed_at, payer, tool}
 * The endpoint loses its query string; nothing from the request or the
 * response body is written. The crowdcode-mcp server signs and submits the
 * line as an observation (packages/mcp/src/observations.ts).
 *
 * No network, no output, always exit 0: a broken hook must never interrupt
 * the user's session. `crowdcode-mcp off` stops recording.
 */

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

function crowdcodeDir() {
  return process.env.CROWDCODE_HOME || path.join(os.homedir(), ".crowdcode");
}

function disabled() {
  const file = process.env.CROWDCODE_CONFIG_PATH || path.join(crowdcodeDir(), "preferences.json");
  try { return JSON.parse(fs.readFileSync(file, "utf8")).enabled === false; } catch { return false; }
}

function record(raw) {
  const receipts = require("./receipts.cjs");
  if (!receipts.mayHoldReceipt(raw)) return null;
  const event = JSON.parse(raw);
  if (event.hook_event_name && event.hook_event_name !== "PostToolUse") return null;
  const tool = typeof event.tool_name === "string" ? event.tool_name : "";
  // CrowdCode's own tools echo payment evidence; they never make a payment.
  if (!tool.startsWith("mcp__") || /^mcp__(plugin_)?crowdcode/i.test(tool)) return null;
  const receipt = receipts.findReceipt(event.tool_response);
  if (!receipt) return null;
  const endpoint = receipts.endpointOf(event.tool_input);
  if (!endpoint) return null;
  const response = typeof event.tool_response === "string" ? event.tool_response : JSON.stringify(event.tool_response ?? "");
  return {
    endpoint,
    method: receipts.methodOf(event.tool_input),
    network: receipt.network,
    payment_reference: receipt.reference,
    amount_atomic: receipts.priceToAtomic(receipt.price),
    status: receipt.status,
    response_bytes: Buffer.byteLength(response, "utf8"),
    observed_at: new Date().toISOString(),
    payer: receipt.payer,
    tool,
  };
}

function main(raw) {
  if (disabled()) return;
  const line = record(raw);
  if (!line) return;
  const dir = crowdcodeDir();
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  fs.appendFileSync(path.join(dir, "pending.jsonl"), JSON.stringify(line) + "\n", { mode: 0o600 });
}

if (require.main === module) {
  const chunks = [];
  process.stdin.on("data", chunk => chunks.push(chunk));
  process.stdin.on("end", () => {
    try { main(Buffer.concat(chunks).toString("utf8")); } catch { /* never break the session */ }
  });
  process.stdin.on("error", () => {});
}

module.exports = { record, main };
