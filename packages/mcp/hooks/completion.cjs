"use strict";
/**
 * CrowdCode Stop hook (Claude Code and Codex), installed by
 * `crowdcode-mcp install`.
 *
 * It reads the local transcript the host passes as `transcript_path` (no
 * network) and asks for a reflection only when the turn earned one:
 *   - a paid call (by its payment receipt) that was not rated afterwards,
 *   - a failed tool call, or
 *   - at least 8 tool calls.
 * Otherwise it stays silent. It blocks at most once per stop
 * (`stop_hook_active`) and fails open on anything unexpected.
 */

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const MANY_TOOL_CALLS = 8;
const TAIL_BYTES = 4 * 1024 * 1024;
const REVIEW_TOOL_RE = /crowdcode.*review_service$/i;
const CROWDCODE_TOOL_RE = /^mcp__(plugin_)?crowdcode/i;

function disabled() {
  const dir = process.env.CROWDCODE_HOME || path.join(os.homedir(), ".crowdcode");
  const file = process.env.CROWDCODE_CONFIG_PATH || path.join(dir, "preferences.json");
  try { return JSON.parse(fs.readFileSync(file, "utf8")).enabled === false; } catch { return false; }
}

function tailLines(file) {
  const fd = fs.openSync(file, "r");
  try {
    const size = fs.fstatSync(fd).size;
    const length = Math.min(size, TAIL_BYTES);
    const buffer = Buffer.alloc(length);
    fs.readSync(fd, buffer, 0, length, size - length);
    const lines = buffer.toString("utf8").split("\n");
    if (length < size) lines.shift(); // a partial first line
    const parsed = [];
    for (const line of lines) {
      if (!line.trim()) continue;
      try { parsed.push(JSON.parse(line)); } catch { /* skip */ }
    }
    return parsed;
  } finally { fs.closeSync(fd); }
}

/**
 * The current turn as a flat list of steps:
 *   {kind: "call", id, name} | {kind: "result", id, output, failed}
 * Claude Code: `user`/`assistant` lines with tool_use and tool_result blocks.
 * Codex: `response_item` lines with function_call(_output) payloads.
 */
function currentTurn(entries) {
  let start = 0;
  entries.forEach((entry, index) => {
    if (isUserPrompt(entry)) start = index + 1;
  });
  const steps = [];
  for (const entry of entries.slice(start)) {
    const message = entry && entry.message;
    if (message && Array.isArray(message.content)) {
      for (const block of message.content) {
        if (!block || typeof block !== "object") continue;
        if (block.type === "tool_use") steps.push({ kind: "call", id: block.id, name: String(block.name || "") });
        else if (block.type === "tool_result") steps.push({ kind: "result", id: block.tool_use_id, output: block.content, failed: block.is_error === true });
      }
      continue;
    }
    const payload = entry && entry.type === "response_item" ? entry.payload : null;
    if (!payload) continue;
    if (payload.type === "function_call" || payload.type === "custom_tool_call" || payload.type === "mcp_tool_call") {
      steps.push({ kind: "call", id: payload.call_id, name: String(payload.name || "") });
    } else if (payload.type === "function_call_output" || payload.type === "custom_tool_call_output" || payload.type === "mcp_tool_call_output") {
      const output = payload.output;
      const text = typeof output === "string" ? output : JSON.stringify(output ?? "");
      const failed = /"exit_code"\s*:\s*[1-9]/.test(text) || /"is_?error"\s*:\s*true/i.test(text) ||
        /"success"\s*:\s*false/.test(text) || (output && typeof output === "object" && output.success === false);
      steps.push({ kind: "result", id: payload.call_id, output, failed: Boolean(failed) });
    }
  }
  return steps;
}

function isUserPrompt(entry) {
  if (!entry || entry.isMeta) return false;
  if (entry.type === "user" && entry.message) {
    const content = entry.message.content;
    if (typeof content === "string") return true;
    return Array.isArray(content) && content.length > 0 && !content.some(block => block && block.type === "tool_result");
  }
  if (entry.type === "event_msg" && entry.payload && entry.payload.type === "user_message") return true;
  return false;
}

function assess(steps, findReceipt) {
  const names = new Map();
  let calls = 0;
  let failed = 0;
  let unrated = 0;
  for (const step of steps) {
    if (step.kind === "call") {
      calls += 1;
      names.set(step.id, step.name);
      if (REVIEW_TOOL_RE.test(step.name) && unrated > 0) unrated -= 1;
      continue;
    }
    if (step.failed) failed += 1;
    const name = names.get(step.id) || "";
    if (!CROWDCODE_TOOL_RE.test(name) && findReceipt(step.output)) unrated += 1;
  }
  return { calls, failed, unrated };
}

function reasonFor(stats) {
  const parts = [
    "CrowdCode check (once): read crowdcode_status. If it is off or unavailable, or this turn only managed CrowdCode settings or reviews, finish now without reflecting or submitting anything.",
  ];
  if (stats.unrated > 0) {
    parts.push(`You paid for ${stats.unrated === 1 ? "a service call" : `${stats.unrated} service calls`} this turn without rating ${stats.unrated === 1 ? "it" : "them"}: rate each with review_service in one line (rating 1-5 and one sentence on what came back). Community reviews are published as unweighted evidence.`);
  }
  parts.push(
    "Then reflect once on what a service could have changed in this task, in two kinds. gap: a step that failed, was poor, slow or costly. leverage: a service that, at a fair price, would have let you deliver a much better result or something you could not do at all: act (book, call, buy, file, send), reach data you could not (licensed, real-time, behind the user's own login with consent), persist (monitor, alert, retry over days) or verify (an authoritative or human check).",
    "For each worthwhile one, call request_service with kind, input, output, problem, builds_on (what it could be built from today: an official API, a licensed dataset, existing paid services, a human-ops team), unlocks (what you could then have done for the user), frequency (one_off, per_task_like_this or recurring) and price_per_call (price the outcome's value). Describe a reusable service, never this task's answer restated; never invent a budget or repeat a request. If nothing qualifies, submit nothing. Then finish; do not reopen the user's task.",
  );
  return parts.join(" ");
}

function decide(event, findReceipt) {
  if (!event || event.hook_event_name !== "Stop" || event.stop_hook_active) return null;
  if (Array.isArray(event.background_tasks) && event.background_tasks.length) return null;
  if (typeof event.transcript_path !== "string" || !event.transcript_path) return null;
  const stats = assess(currentTurn(tailLines(event.transcript_path)), findReceipt);
  if (stats.unrated === 0 && stats.failed === 0 && stats.calls < MANY_TOOL_CALLS) return null;
  return { decision: "block", reason: reasonFor(stats) };
}

if (require.main === module) {
  const chunks = [];
  process.stdin.on("data", chunk => chunks.push(chunk));
  process.stdin.on("end", () => {
    try {
      if (disabled()) return;
      const { findReceipt } = require("./receipts.cjs");
      const decision = decide(JSON.parse(Buffer.concat(chunks).toString("utf8")), findReceipt);
      if (decision) process.stdout.write(JSON.stringify(decision));
    } catch { /* a broken optional reminder must never trap the user */ }
  });
  process.stdin.on("error", () => {});
}

module.exports = { decide, currentTurn, assess, reasonFor };
