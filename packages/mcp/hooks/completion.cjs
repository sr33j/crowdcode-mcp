"use strict";
/**
 * CrowdCode Stop hook (Claude Code and Codex), installed by
 * `crowdcode-mcp install`.
 *
 * It reads the local transcript the host passes as `transcript_path` (no
 * network) and nudges only when the turn earned it:
 *   - the task ended short: the final answer says it failed, stalled, was
 *     partial or needed a workaround, or a paid call returned nothing useful.
 *     Then it asks for a review of every paid service tried and one
 *     request_service for the service that would have let the agent finish
 *     (SRI-122);
 *   - a paid call (by its payment receipt) that was not rated afterwards;
 *   - a failed tool call (a light reflection: request only if one qualifies).
 * A task that finished with no paid calls and no failures gets nothing, however
 * many steps it took. An agent that already reviewed and requested is left
 * alone. One nudge per task, never a loop: it is silent when the host says the
 * stop follows a hook (`stop_hook_active`) and when its own nudge is already in
 * the turn, and it fails open on anything unexpected.
 */

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const TAIL_BYTES = 4 * 1024 * 1024;
const REVIEW_TOOL_RE = /crowdcode.*review_service$/i;
const REQUEST_TOOL_RE = /crowdcode.*request_service$/i;
// Every nudge starts with this; finding it in the turn means we already nudged.
const MARKER = "CrowdCode check (once)";
// A final answer that admits the task ended short of what was asked.
const SHORT_RE = new RegExp([
  "couldn[\u2019']?t", "could not", "can[\u2019']?t", "cannot", "unable to",
  "(?:was|were|am|is|are)(?: not|n[\u2019']?t) able to", "not possible",
  "did(?: not|n[\u2019']?t) (?:finish|complete|succeed|work|get|find|return)",
  "failed to", "no (?:service|api|provider|tool|endpoint)s? (?:could|can|that|was|were|is|for|available|offers|returned)",
  "partial(?:ly)?", "incomplete", "work ?around", "fell back", "gave up", "stalled",
  "ran out of", "insufficient (?:funds|balance)", "out of (?:funds|budget|credits)",
  "timed out", "not available",
].map((part) => `\\b${part}\\b`).join("|"), "i");
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
    if (JSON.stringify(entry).includes(MARKER)) steps.push({ kind: "nudged" });
    const message = entry && entry.message;
    const text = assistantText(entry);
    if (text) steps.push({ kind: "text", text });
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

/** The assistant's prose in one transcript line (Claude Code or Codex), if any. */
function assistantText(entry) {
  if (!entry) return "";
  const message = entry.message;
  if (entry.type === "assistant" && message && Array.isArray(message.content)) {
    return message.content.filter((b) => b && b.type === "text" && typeof b.text === "string").map((b) => b.text).join("\n");
  }
  const payload = entry.payload;
  if (entry.type === "response_item" && payload && payload.type === "message" && payload.role === "assistant" &&
      Array.isArray(payload.content)) {
    return payload.content.filter((b) => b && typeof b.text === "string").map((b) => b.text).join("\n");
  }
  if (entry.type === "event_msg" && payload && payload.type === "agent_message" && typeof payload.message === "string") {
    return payload.message;
  }
  return "";
}

function isUserPrompt(entry) {
  if (!entry || entry.isMeta) return false;
  // Our own nudge, echoed back by a host, is not the user's next task.
  if (JSON.stringify(entry).includes(MARKER)) return false;
  if (entry.type === "user" && entry.message) {
    const content = entry.message.content;
    if (typeof content === "string") return true;
    return Array.isArray(content) && content.length > 0 && !content.some(block => block && block.type === "tool_result");
  }
  if (entry.type === "event_msg" && entry.payload && entry.payload.type === "user_message") return true;
  return false;
}

function emptyOutput(output) {
  if (output == null) return true;
  const text = typeof output === "string" ? output : JSON.stringify(output);
  return !text.replace(/[\s"\[\]{}]/g, "");
}

/**
 * calls, failed tools, paid calls (by receipt), paid calls that returned
 * nothing useful (an error status or no body), how many are still unrated,
 * whether request_service was called, whether the final answer admits the
 * task ended short, and whether this turn was already nudged.
 */
function assess(steps, findReceipt, finalText) {
  const names = new Map();
  let calls = 0;
  let failed = 0;
  let paid = 0;
  let paidUseless = 0;
  let unrated = 0;
  let requested = 0;
  let nudged = false;
  let lastText = "";
  for (const step of steps) {
    if (step.kind === "nudged") { nudged = true; continue; }
    if (step.kind === "text") { lastText = step.text; continue; }
    if (step.kind === "call") {
      calls += 1;
      names.set(step.id, step.name);
      if (REVIEW_TOOL_RE.test(step.name) && unrated > 0) unrated -= 1;
      if (REQUEST_TOOL_RE.test(step.name)) requested += 1;
      continue;
    }
    if (step.failed) failed += 1;
    const name = names.get(step.id) || "";
    if (CROWDCODE_TOOL_RE.test(name)) continue;
    const receipt = findReceipt(step.output);
    if (!receipt) continue;
    paid += 1;
    unrated += 1;
    if (step.failed || (receipt.status && receipt.status >= 400) || emptyOutput(step.output)) paidUseless += 1;
  }
  const text = typeof finalText === "string" && finalText ? finalText : lastText;
  const short = SHORT_RE.test(text) || paidUseless > 0;
  return { calls, failed, paid, paidUseless, unrated, requested, short, nudged };
}

const plural = (n, one, many) => (n === 1 ? one : many);

function reasonFor(stats) {
  const parts = [
    `${MARKER}: read crowdcode_status. If it is off or unavailable, or this turn only managed CrowdCode settings or reviews, finish now without reflecting or submitting anything.`,
  ];
  if (stats.short) {
    const steps = [];
    if (stats.paid === 0 || stats.unrated > 0) {
      const tried = stats.paid === 0 ? "each paid service you tried this task, if any"
        : stats.unrated === stats.paid
          ? `the ${plural(stats.paid, "paid service", `${stats.paid} paid service calls`)} you tried this task`
          : `the ${stats.unrated} paid service ${plural(stats.unrated, "call", "calls")} you tried this task and have not rated yet`;
      steps.push(`review_service ${tried}, including any that returned nothing useful: rating 1-5 and one sentence on what came back.`);
    }
    if (stats.requested === 0) {
      steps.push("request_service once, for the service that would have let you finish this task: input, output, problem, builds_on (what it could be built from today: an official API, a licensed dataset, existing paid services, a human-ops team), frequency (one_off, per_task_like_this or recurring) and price_per_call. Describe a reusable service, never this task's answer restated. Skip this only if the task did finish without a workaround.");
    }
    parts.push(
      `This task looks unfinished: it failed, stalled, was partial or needed a costly workaround. Before you finish, ${steps.length === 1 ? "do this:" : "do both:"}`,
      ...steps.map((step, index) => `${index + 1}. ${step}`),
      "Then tell the user plainly what is still missing and finish; do not reopen the user's task.",
    );
    return parts.join(" ");
  }
  if (stats.unrated > 0) {
    parts.push(`You paid for ${stats.unrated === 1 ? "a service call" : `${stats.unrated} service calls`} this turn without rating ${stats.unrated === 1 ? "it" : "them"}: rate each with review_service in one line (rating 1-5 and one sentence on what came back). Community reviews are published as unweighted evidence.`);
  }
  parts.push(
    "Then reflect once: what concrete paid service would have fixed an observed failure, a poor result or excessive cost in this task?",
    "For each worthwhile one, call request_service with input, output, problem, builds_on (what it could be built from today: an official API, a licensed dataset, existing paid services, a human-ops team), frequency (one_off, per_task_like_this or recurring) and price_per_call. Describe a reusable service, never this task's answer restated; never invent a budget or repeat a request. If nothing qualifies, submit nothing. Then finish; do not reopen the user's task.",
  );
  return parts.join(" ");
}

function decide(event, findReceipt) {
  if (!event || event.hook_event_name !== "Stop" || event.stop_hook_active) return null;
  if (Array.isArray(event.background_tasks) && event.background_tasks.length) return null;
  if (typeof event.transcript_path !== "string" || !event.transcript_path) return null;
  const stats = assess(currentTurn(tailLines(event.transcript_path)), findReceipt, event.last_assistant_message);
  if (stats.nudged) return null; // one nudge per task, even where the host never sets stop_hook_active
  if (stats.short) {
    if (stats.unrated === 0 && stats.requested > 0) return null; // already reviewed and requested
  } else if (stats.unrated === 0 && stats.failed === 0) {
    return null;
  }
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
