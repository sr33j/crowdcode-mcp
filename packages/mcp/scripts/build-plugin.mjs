import { mkdir, readFile, readdir, writeFile } from "node:fs/promises";
import { dirname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { strToU8, zipSync } from "fflate";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const packageRoot = resolve(scriptDir, "..");
const repoRoot = resolve(packageRoot, "../..");
const pluginRoot = join(repoRoot, "plugins", "crowdcode");
const packageJson = JSON.parse(
  await readFile(join(packageRoot, "package.json"), "utf8"),
);
const output = resolve(
  process.cwd(),
  process.argv[2] ??
    join(repoRoot, "artifacts", `crowdcode-plugin-${packageJson.version}.zip`),
);

const entries = {};
async function collect(directory) {
  for (const item of await readdir(directory, { withFileTypes: true })) {
    const path = join(directory, item.name);
    if (item.isDirectory()) {
      await collect(path);
    } else if (item.isFile()) {
      const archivePath = relative(pluginRoot, path).split(sep).join("/");
      entries[archivePath] = strToU8(await readFile(path, "utf8"));
    }
  }
}

await collect(pluginRoot);
await mkdir(dirname(output), { recursive: true });
await writeFile(output, zipSync(entries, { level: 9 }));
process.stdout.write(`${output}\n`);
