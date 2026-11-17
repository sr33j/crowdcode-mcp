import { execFileSync } from "node:child_process";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { PAYER, SECRET_BODY, SECRET_QUERY, TX, mppResult, postToolUse, x402Result } from "./fixtures/agentcash.js";

const require = createRequire(import.meta.url);
const HOOKS = resolve(__dirname, "..", "hooks");
const receipts = require(join(HOOKS, "receipts.cjs"));
const paidCall = require(join(HOOKS, "paid-call.cjs"));
const completion = require(join(HOOKS, "completion.cjs"));

async function home() {
  return mkdtemp(join(tmpdir(), "crowdcode-hooks-"));
}

function runHook(name: string, input: unknown, env: Record<string, string>) {
  const started = process.hrtime.bigint();
  const stdout = execFileSync(process.execPath, [join(HOOKS, name)], {
    input: typeof input === "string" ? input : JSON.stringify(input), encoding: "utf8",
    env: { ...process.env, ...env },
  });
  return { stdout, ms: Number(process.hrtime.bigint() - started) / 1e6 };
}

describe("payment receipts", () => {
  it("finds x402 and MPP receipts in any tool's output, not by tool name", () => {
    expect(receipts.findReceipt(x402Result())).toMatchObject({ protocol: "x402", network: "base", reference: TX, payer: PAYER, price: "$0.007" });
    expect(receipts.findReceipt(mppResult())).toMatchObject({ protocol: "mpp", network: "tempo" });
    // A header printed as text by some other wallet tool.
    const header = Buffer.from(JSON.stringify({ success: true, transaction: TX, network: "base" })).toString("base64");
    expect(receipts.findReceipt(`HTTP 200\nPAYMENT-RESPONSE: ${header}\n\n{"ok":true}`)).toMatchObject({ reference: TX });
  });
  it("ignores failed settlements, missing references and ordinary output", () => {
    const failed = Buffer.from(JSON.stringify({ success: false, transaction: TX })).toString("base64");
    expect(receipts.findReceipt([{ type: "text", text: JSON.stringify({ headers: { "payment-response": failed } }) }])).toBeNull();
    expect(receipts.findReceipt(x402Result({ payment: { success: true, channelId: "c" }, headers: {} }))).toBeNull();
    expect(receipts.findReceipt("the word payment-response in prose")).toBeNull();
    expect(receipts.findReceipt([{ type: "text", text: "hello" }])).toBeNull();
  });
  it("strips the query from the endpoint and converts the price", () => {
    expect(receipts.endpointOf({ url: "https://api.exa.ai/search/?q=secret#x" })).toBe("https://api.exa.ai/search");
    expect(receipts.endpointOf({ url: "http://insecure.example/x" })).toBeNull();
    expect(receipts.priceToAtomic("$0.007")).toBe(7000);
    expect(receipts.priceToAtomic("free")).toBeNull();
  });
});

describe("PostToolUse paid-call hook", () => {
  it("appends metadata only, never the request or the response", async () => {
    const dir = await home();
    const { stdout, ms } = runHook("paid-call.cjs", postToolUse(x402Result()), { CROWDCODE_HOME: dir });
    expect(stdout).toBe("");
    const text = await readFile(join(dir, "pending.jsonl"), "utf8");
    const line = JSON.parse(text);
    expect(Object.keys(line).sort()).toEqual(["amount_atomic", "endpoint", "method", "network", "observed_at",
      "payer", "payment_reference", "response_bytes", "status", "tool"]);
    expect(line).toMatchObject({ endpoint: "https://api.exa.ai/search", method: "POST", network: "base",
      payment_reference: TX, amount_atomic: 7000, payer: PAYER });
    expect(line.response_bytes).toBeGreaterThan(100);
    expect(text).not.toContain(SECRET_QUERY.split(" ")[0]);
    expect(text).not.toContain("CONFIDENTIAL");
    // The whole process (Node start-up included) stays well inside a hook budget;
    // the work itself is measured below.
    expect(ms).toBeLessThan(1000);
  });
  it("records the parsing work in well under 50 ms", () => {
    const event = JSON.stringify(postToolUse(x402Result()));
    const started = process.hrtime.bigint();
    for (let i = 0; i < 20; i += 1) paidCall.record(event);
    expect(Number(process.hrtime.bigint() - started) / 1e6 / 20).toBeLessThan(50);
  });
  it("skips non-MCP tools, CrowdCode's own tools, unpaid calls, and off", async () => {
    const dir = await home();
    runHook("paid-call.cjs", postToolUse(x402Result(), undefined, "Bash"), { CROWDCODE_HOME: dir });
    runHook("paid-call.cjs", postToolUse(x402Result(), undefined, "mcp__crowdcode__review_service"), { CROWDCODE_HOME: dir });
    runHook("paid-call.cjs", postToolUse([{ type: "text", text: "free answer" }]), { CROWDCODE_HOME: dir });
    await expect(readFile(join(dir, "pending.jsonl"), "utf8")).rejects.toThrow();
    await writeFile(join(dir, "preferences.json"), JSON.stringify({ enabled: false }));
    runHook("paid-call.cjs", postToolUse(x402Result()), { CROWDCODE_HOME: dir });
    await expect(readFile(join(dir, "pending.jsonl"), "utf8")).rejects.toThrow();
  });
  it("never breaks the session on malformed input", async () => {
    const dir = await home();
    expect(runHook("paid-call.cjs", "not json payment-response", { CROWDCODE_HOME: dir }).stdout).toBe("");
    expect(runHook("paid-call.cjs", "", { CROWDCODE_HOME: dir }).stdout).toBe("");
  });
});

// --- the conditional Stop hook ---------------------------------------------

type Line = Record<string, unknown>;
const user = (text: string): Line => ({ type: "user", message: { role: "user", content: text } });
const call = (id: string, name: string, input: unknown = {}): Line =>
  ({ type: "assistant", message: { role: "assistant", content: [{ type: "tool_use", id, name, input }] } });
const result = (id: string, content: unknown, isError = false): Line =>
  ({ type: "user", message: { role: "user", content: [{ type: "tool_result", tool_use_id: id, content, is_error: isError }] } });
const say = (text: string): Line => ({ type: "assistant", message: { role: "assistant", content: [{ type: "text", text }] } });

async function stopWith(lines: Line[], extra: Record<string, unknown> = {}) {
  const dir = await home();
  const transcript = join(dir, "transcript.jsonl");
  await writeFile(transcript, lines.map((l) => JSON.stringify(l)).join("\n") + "\n");
  const { stdout } = runHook("completion.cjs", { hook_event_name: "Stop", stop_hook_active: false,
    transcript_path: transcript, ...extra }, { CROWDCODE_HOME: dir });
  return stdout ? JSON.parse(stdout) : null;
}

describe("conditional Stop hook", () => {
  it("stays silent after a one-line answer", async () => {
    expect(await stopWith([user("what is 2+2?"), say("4")])).toBeNull();
  });
  it("fires after a purchase turn and asks for a one-line rating and both kinds of request", async () => {
    const decision = await stopWith([
      user("earlier task"), call("a", "mcp__agentcash__fetch"), result("a", x402Result()), say("done"),
      user("find recent news on Acme"), call("b", "mcp__agentcash__fetch"), result("b", x402Result()), say("here"),
    ]);
    expect(decision.decision).toBe("block");
    expect(decision.reason).toContain("without rating it");
    expect(decision.reason).toContain("fixed an observed failure, a poor result or excessive cost");
    expect(decision.reason).not.toMatch(/leverage|unlocks|kind,/);
    expect(decision.reason).toContain("builds_on");
  });
  it("only looks at the current turn, and a rated purchase with few calls is silent", async () => {
    expect(await stopWith([
      user("task one"), call("a", "mcp__agentcash__fetch"), result("a", x402Result()), say("done"),
      user("thanks!"), say("you're welcome"),
    ])).toBeNull();
    expect(await stopWith([
      user("search"), call("a", "mcp__agentcash__fetch"), result("a", x402Result()),
      call("r", "mcp__crowdcode__review_service"), result("r", "{\"accepted\":true}"), say("done"),
    ])).toBeNull();
  });
  it("fires after a failed tool or at least 8 tool calls", async () => {
    const failed = await stopWith([user("build it"), call("a", "Bash"), result("a", "exit 1", true), say("failed")]);
    expect(failed.decision).toBe("block");
    expect(failed.reason).not.toContain("without rating");
    const seven = [user("refactor")];
    for (let i = 0; i < 7; i += 1) seven.push(call(`c${i}`, "Read"), result(`c${i}`, "file"));
    expect(await stopWith([...seven, say("done")])).toBeNull();
    expect((await stopWith([...seven, call("c7", "Edit"), result("c7", "ok"), say("done")])).decision).toBe("block");
  });
  it("reads Codex rollout transcripts too", async () => {
    const codex = (payload: Line): Line => ({ timestamp: "t", type: "response_item", payload });
    const prompt = { timestamp: "t", type: "event_msg", payload: { type: "user_message", message: "go" } };
    expect(await stopWith([prompt, codex({ type: "message", role: "assistant", content: [] })])).toBeNull();
    const decision = await stopWith([prompt,
      codex({ type: "function_call", name: "mcp__agentcash__fetch", call_id: "x", arguments: "{}" }),
      codex({ type: "function_call_output", call_id: "x", output: JSON.stringify(x402Result()) })]);
    expect(decision.decision).toBe("block");
    const failed = await stopWith([prompt,
      codex({ type: "function_call", name: "shell", call_id: "y", arguments: "{}" }),
      codex({ type: "function_call_output", call_id: "y", output: "{\"output\":\"boom\",\"metadata\":{\"exit_code\":2}}" })]);
    expect(failed.decision).toBe("block");
  });
  it("blocks at most once, and fails open", async () => {
    const lines = [user("build"), call("a", "Bash"), result("a", "x", true)];
    expect(await stopWith(lines, { stop_hook_active: true })).toBeNull();
    expect(await stopWith(lines, { background_tasks: [{}] })).toBeNull();
    expect(await stopWith(lines, { transcript_path: "/nonexistent/transcript.jsonl" })).toBeNull();
    const dir = await home();
    expect(runHook("completion.cjs", "broken", { CROWDCODE_HOME: dir }).stdout).toBe("");
    expect(completion.decide({ hook_event_name: "SubagentStop" }, receipts.findReceipt)).toBeNull();
  });
});
