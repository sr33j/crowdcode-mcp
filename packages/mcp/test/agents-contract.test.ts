/** The MCP server registers exactly the tools of spec/agent-tools.json, plus the `crowdcode` prompt. */

import { readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { RedactionEngine } from "@crowdcode/redaction";
import { describe, expect, it } from "vitest";
import { getConfig } from "../src/config.js";
import { AGENT_TOOL_NAMES, HOST_DESCRIPTIONS, type ToolContract } from "../src/agents/contract.js";
import { USAGE } from "../src/agents/dashboard.js";
import { buildServer } from "../src/server.js";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");

async function connect() {
  const engine = await RedactionEngine.create({ cacheDir: getConfig().cacheDir, enableModel: false });
  const server = buildServer({
    engine,
    upstream: { call: async () => ({}), listToolNames: async () => [] },
    agents: { env: { CROWDCODE_APP_URL: "http://127.0.0.1:9", CROWDCODE_HOME: "/nonexistent-crowdcode-home" } },
  });
  const [a, b] = InMemoryTransport.createLinkedPair();
  const client = new Client({ name: "test", version: "1" });
  await Promise.all([server.connect(a), client.connect(b)]);
  return client;
}

describe("agent tool contract", () => {
  it("registers every spec tool with its arguments and required arguments", async () => {
    const spec = JSON.parse(await readFile(resolve(repoRoot, "spec/agent-tools.json"), "utf8")) as {
      tools: ToolContract[];
    };
    expect(spec.tools.map((t) => t.name)).toEqual([...AGENT_TOOL_NAMES]);
    const client = await connect();
    const { tools } = await client.listTools();
    for (const tool of spec.tools) {
      const mine = tools.find((t) => t.name === tool.name);
      expect(mine, tool.name).toBeDefined();
      const schema = mine!.inputSchema as { properties?: Record<string, any>; required?: string[] };
      expect(Object.keys(schema.properties ?? {}).sort(), tool.name).toEqual(Object.keys(tool.input_schema.properties).sort());
      expect([...(schema.required ?? [])].sort(), tool.name).toEqual([...(tool.input_schema.required ?? [])].sort());
      for (const [key, property] of Object.entries(tool.input_schema.properties)) {
        const got = schema.properties![key];
        expect(got.type, `${tool.name}.${key}`).toBe(property.type);
        if (property.enum) expect(got.enum, `${tool.name}.${key}`).toEqual(property.enum);
        const override = HOST_DESCRIPTIONS[tool.name as keyof typeof HOST_DESCRIPTIONS]?.args?.[key];
        expect(got.description, `${tool.name}.${key}`).toBe(override ?? property.description);
      }
      const override = HOST_DESCRIPTIONS[tool.name as keyof typeof HOST_DESCRIPTIONS]?.tool;
      expect(mine!.description, tool.name).toBe(override ?? tool.description);
    }
    // the review tools are still there
    for (const name of ["get_service_score", "review_service", "request_service", "find_service", "crowdcode_status"]) {
      expect(tools.some((t) => t.name === name), name).toBe(true);
    }
  });

  it("only overrides descriptions that name Hermes-only behaviour", async () => {
    const spec = JSON.parse(await readFile(resolve(repoRoot, "spec/agent-tools.json"), "utf8")) as {
      tools: ToolContract[];
    };
    for (const name of Object.keys(HOST_DESCRIPTIONS)) {
      const tool = spec.tools.find((t) => t.name === name)!;
      const texts = [tool.description, ...Object.values(tool.input_schema.properties).map((p) => p.description ?? "")];
      expect(texts.join(" "), name).toMatch(/Hermes|asks the user first/);
    }
    for (const tool of spec.tools) {
      if (tool.name in HOST_DESCRIPTIONS) continue;
      expect(tool.description, tool.name).not.toMatch(/Hermes|\/crowdcode/);
    }
  });

  it("returns results as JSON text", async () => {
    const client = await connect();
    const result = await client.callTool({ name: "crowdcode_files", arguments: { agent_id: "../etc" } });
    const content = result.content as { type: string; text: string }[];
    expect(JSON.parse(content[0]!.text)).toEqual({ ok: false, error: "agent_id is not a valid CrowdCode id" });
  });

  it("offers the crowdcode prompt with an optional args string", async () => {
    const client = await connect();
    const { prompts } = await client.listPrompts();
    const prompt = prompts.find((p) => p.name === "crowdcode");
    expect(prompt?.arguments).toEqual([expect.objectContaining({ name: "args", required: false })]);
    const help = await client.getPrompt({ name: "crowdcode", arguments: { args: "help" } });
    expect((help.messages[0]!.content as { text: string }).text).toBe(USAGE);
  });
});
