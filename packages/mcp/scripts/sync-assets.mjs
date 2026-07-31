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
const serverSource = await readFile(serverSourcePath, "utf8");
if (!serverSource.includes(`{ name: "crowdcode", version: "${packageJson.version}" }`)) {
  throw new Error(
    `MCP server version must match package version ${packageJson.version}`,
  );
}

if (process.argv.includes("--check")) {
  const packaged = await readFile(packagedPath, "utf8");
  if (packaged !== canonical) {
    throw new Error(
      `Packaged CrowdCode skill drift: ${packagedPath} must match ${canonicalPath}`,
    );
  }
} else {
  await mkdir(dirname(packagedPath), { recursive: true });
  await writeFile(packagedPath, canonical);
}
