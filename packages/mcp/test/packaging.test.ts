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

  // Only the frontmatter's shape. The skill's wording is not pinned (SRI-175): phrase pins here
  // were rewritten on 5 of this file's 7 changes.
  it("uses only trigger-relevant canonical frontmatter", async () => {
    const skill = await text("skills/crowdcode/SKILL.md");
    const match = /^---\n([\s\S]*?)\n---/.exec(skill);
    expect(match).not.toBeNull();
    const keys = match![1]!
      .split("\n")
      .filter((line) => /^[a-z][a-z0-9_-]*:/.test(line))
      .map((line) => line.split(":", 1)[0]);
    expect(keys).toEqual(["name", "description"]);
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
      // An exact version: Codex waits only about a second for an optional server's first start.
      args: ["-y", `crowdcode-mcp@${packageJson.version}`],
    });
  });
});

describe("installing crowdcode-mcp never depends on a native download", () => {
  // On 2026-11-17 onnxruntime-node 1.21.0's install script started failing
  // on Linux x64 (its CUDA asset answers 404), and `npm i crowdcode-mcp`
  // failed with it. The model stack is optional: if its install fails, npm
  // drops it and the engine redacts with the deterministic layers only.
  it("keeps the PII model stack optional and the redaction engine bundled", async () => {
    const pkg = JSON.parse(await text("packages/mcp/package.json"));
    expect(pkg.dependencies).not.toHaveProperty("@huggingface/transformers");
    expect(pkg.dependencies).not.toHaveProperty("@crowdcode/redaction");
    expect(pkg.optionalDependencies).toHaveProperty("@huggingface/transformers");
    expect(pkg.dependencies).toHaveProperty("@nationaldesignstudio/rampart");
    const redaction = JSON.parse(await text("packages/redaction/package.json"));
    expect(redaction.dependencies).not.toHaveProperty("@huggingface/transformers");
    expect(redaction.optionalDependencies).toHaveProperty("@huggingface/transformers");
  });

  it("imports transformers at runtime instead of bundling it", async () => {
    const { readdir } = await import("node:fs/promises");
    const dist = resolve(packageRoot, "dist");
    const files = (await readdir(dist)).filter((name) => name.endsWith(".js"));
    const code = (await Promise.all(files.map((name) => readFile(resolve(dist, name), "utf8")))).join("\n");
    expect(code).not.toMatch(/from "@crowdcode\/redaction"|import\("@crowdcode\/redaction"\)/);
    expect(files.some((name) => name.includes("transformers"))).toBe(false);
    expect(code).toContain("RedactionEngine");
  });
});
