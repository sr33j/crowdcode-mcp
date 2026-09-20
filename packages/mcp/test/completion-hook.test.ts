import { execFileSync } from "node:child_process";
import { describe, expect, it } from "vitest";
import { COMPLETION_HOOK } from "../src/completion-hook.js";

function run(input: unknown) {
  return execFileSync(process.execPath, ["-e", COMPLETION_HOOK], {
    input: typeof input === "string" ? input : JSON.stringify(input), encoding: "utf8",
  });
}

describe("completion reminder", () => {
  it("prompts exactly one continuation and allows the second stop", () => {
    expect(JSON.parse(run({ hook_event_name: "Stop", stop_hook_active: false })).decision).toBe("block");
    expect(run({ hook_event_name: "Stop", stop_hook_active: true })).toBe("");
  });
  it("does not interrupt background work or unrelated events; malformed input fails open", () => {
    expect(run({ hook_event_name: "Stop", background_tasks: [{}] })).toBe("");
    expect(run({ hook_event_name: "SubagentStop" })).toBe("");
    expect(run("broken")).toBe("");
  });
});
