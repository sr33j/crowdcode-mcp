import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(scriptDir, "../../..");
const canonicalPath = resolve(repoRoot, "skills/crowdcode/SKILL.md");
const pluginPath = resolve(
  repoRoot,
  "plugins/crowdcode/skills/crowdcode/SKILL.md",
);
const frontendPath = resolve(repoRoot, "frontend/SKILL.md");
const packagedPath = resolve(scriptDir, "../dist/skills/crowdcode/SKILL.md");
const packageJsonPath = resolve(scriptDir, "../package.json");
const pluginManifestPath = resolve(
  repoRoot,
  "plugins/crowdcode/.codex-plugin/plugin.json",
);
const claudeManifestPath = resolve(
  repoRoot,
  "plugins/crowdcode/.claude-plugin/plugin.json",
);
const serverSourcePath = resolve(scriptDir, "../src/server.ts");

const canonical = await readFile(canonicalPath, "utf8");
const plugin = await readFile(pluginPath, "utf8");
if (plugin !== canonical) {
  throw new Error(
    `CrowdCode skill drift: ${pluginPath} must exactly match ${canonicalPath}`,
  );
}
const frontend = await readFile(frontendPath, "utf8");
if (frontend !== canonical) {
  throw new Error(
    `Website skill drift: ${frontendPath} must exactly match ${canonicalPath}`,
  );
}

const packageJson = JSON.parse(await readFile(packageJsonPath, "utf8"));
const pluginManifest = JSON.parse(await readFile(pluginManifestPath, "utf8"));
if (pluginManifest.version !== packageJson.version) {
  throw new Error(
    `Plugin version ${pluginManifest.version} must match package version ${packageJson.version}`,
  );
}
const claudeManifest = JSON.parse(await readFile(claudeManifestPath, "utf8"));
if (claudeManifest.version !== packageJson.version) {
  throw new Error(
    `Claude Code plugin version ${claudeManifest.version} must match package version ${packageJson.version}`,
  );
}
const pluginMcp = JSON.parse(await readFile(resolve(repoRoot, "plugins/crowdcode/.mcp.json"), "utf8"));
const pluginArgs = JSON.stringify(pluginMcp.mcpServers?.crowdcode?.args);
if (pluginArgs !== JSON.stringify(["-y", `crowdcode-mcp@${packageJson.version}`])) {
  throw new Error(
    `plugins/crowdcode/.mcp.json must launch crowdcode-mcp@${packageJson.version} (an exact version starts fast enough for Codex), not ${pluginArgs}`,
  );
}
const serverSource = await readFile(serverSourcePath, "utf8");
if (!serverSource.includes(`{ name: "crowdcode", version: "${packageJson.version}" }`)) {
  throw new Error(
    `MCP server version must match package version ${packageJson.version}`,
  );
}

// The local hooks (PostToolUse paid-call records, conditional Stop reminder)
// ship as plain files the installer copies to ~/.crowdcode/hooks.
const hookNames = ["receipts.cjs", "paid-call.cjs", "completion.cjs"];
const hookSources = Object.fromEntries(await Promise.all(hookNames.map(async (name) =>
  [name, await readFile(resolve(scriptDir, "../hooks", name), "utf8")])));
const packagedHook = (name) => resolve(scriptDir, "../dist/hooks", name);

if (process.argv.includes("--check")) {
  for (const name of hookNames) {
    if ((await readFile(packagedHook(name), "utf8")) !== hookSources[name]) {
      throw new Error(`Packaged hook drift: ${packagedHook(name)} must match hooks/${name}`);
    }
  }
  const packaged = await readFile(packagedPath, "utf8");
  if (packaged !== canonical) {
    throw new Error(
      `Packaged CrowdCode skill drift: ${packagedPath} must match ${canonicalPath}`,
    );
  }
} else {
  await mkdir(dirname(packagedPath), { recursive: true });
  await writeFile(packagedPath, canonical);
  await mkdir(dirname(packagedHook(hookNames[0])), { recursive: true });
  for (const name of hookNames) await writeFile(packagedHook(name), hookSources[name]);
}
