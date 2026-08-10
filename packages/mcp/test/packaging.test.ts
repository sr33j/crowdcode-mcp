import { readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = resolve(packageRoot, "../..");

async function text(path: string): Promise<string> {
  return readFile(resolve(repoRoot, path), "utf8");
}

describe("skill and plugin packaging", () => {
  it("keeps canonical, packaged, and plugin skills byte-identical", async () => {
    const canonical = await text("skills/crowdcode/SKILL.md");
    expect(await text("packages/mcp/dist/skills/crowdcode/SKILL.md")).toBe(
      canonical,
    );
    expect(await text("plugins/crowdcode/skills/crowdcode/SKILL.md")).toBe(
      canonical,
    );
    expect(await text("frontend/SKILL.md")).toBe(canonical);
  });

  it("uses only trigger-relevant canonical frontmatter", async () => {
    const skill = await text("skills/crowdcode/SKILL.md");
    const match = /^---\n([\s\S]*?)\n---/.exec(skill);
    expect(match).not.toBeNull();
    const keys = match![1]!
      .split("\n")
      .filter((line) => /^[a-z][a-z0-9_-]*:/.test(line))
      .map((line) => line.split(":", 1)[0]);
    expect(keys).toEqual(["name", "description"]);
    expect(match![1]).toMatch(/third-party paid APIs/);
    expect(match![1]).toMatch(/uniquely paid use/);
    expect(match![1]).toMatch(/Do not use CrowdCode to gate trades/);
    expect(skill).toMatch(/Call `get_service_score`/);
    expect(skill).toMatch(/Call `review_service` after every uniquely paid use/);
    expect(skill).toMatch(/signs automatically/);
  });

  it("keeps plugin and npm versions aligned with the expected MCP command", async () => {
    const packageJson = JSON.parse(await text("packages/mcp/package.json"));
    const manifest = JSON.parse(
      await text("plugins/crowdcode/.codex-plugin/plugin.json"),
    );
    const mcp = JSON.parse(await text("plugins/crowdcode/.mcp.json"));
    expect(manifest.version).toBe(packageJson.version);
    expect(manifest.skills).toBe("./skills/");
    expect(manifest.mcpServers).toBe("./.mcp.json");
    expect(mcp.mcpServers.crowdcode).toEqual({
      command: "npx",
      args: ["-y", "crowdcode-mcp@latest"],
    });
  });
});
