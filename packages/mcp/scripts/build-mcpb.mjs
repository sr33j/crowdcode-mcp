import { execFile } from "node:child_process";
import {
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  rm,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

const run = promisify(execFile);
const scriptDir = dirname(fileURLToPath(import.meta.url));
const packageRoot = resolve(scriptDir, "..");
const repoRoot = resolve(packageRoot, "../..");
const packageJson = JSON.parse(
  await readFile(join(packageRoot, "package.json"), "utf8"),
);
const npm = process.platform === "win32" ? "npm.cmd" : "npm";
const outputArg = process.argv[2];
const output = resolve(
  process.cwd(),
  outputArg ??
    join(
      repoRoot,
      "artifacts",
      `crowdcode-mcp-${packageJson.version}-${process.platform}-${process.arch}.mcpb`,
    ),
);

if (!["darwin", "linux", "win32"].includes(process.platform)) {
  throw new Error(`MCPB builds are not supported on ${process.platform}`);
}

const temp = await mkdtemp(join(tmpdir(), "crowdcode-mcpb-"));
try {
  const packDir = join(temp, "package");
  const bundleDir = join(temp, "bundle");
  await mkdir(packDir, { recursive: true });
  await mkdir(bundleDir, { recursive: true });
  await run(npm, ["pack", packageRoot, "--pack-destination", packDir, "--silent"], {
    cwd: repoRoot,
  });
  const tarballName = (await readdir(packDir)).find((name) => name.endsWith(".tgz"));
  if (!tarballName) throw new Error("npm pack did not produce a tarball");
  const tarball = join(packDir, tarballName);

  await writeFile(
    join(bundleDir, "package.json"),
    `${JSON.stringify({ private: true }, null, 2)}\n`,
  );
  await run(
    npm,
    [
      "install",
      "--prefix",
      bundleDir,
      "--omit=dev",
      "--no-package-lock",
      "--no-audit",
      "--no-fund",
      tarball,
    ],
    { cwd: repoRoot, maxBuffer: 10 * 1024 * 1024 },
  );

  const manifest = {
    manifest_version: "0.3",
    name: "crowdcode",
    display_name: "CrowdCode",
    version: packageJson.version,
    description:
      "Check paid agent services before spending and submit payment-verified reviews afterward.",
    long_description:
      "CrowdCode adds a local, privacy-preserving reputation layer around paid agent services. Free text is redacted on-device before it reaches the CrowdCode backend.",
    author: {
      name: "CrowdCode",
      url: "https://github.com/sr33j/crowdcode-mcp",
    },
    repository: {
      type: "git",
      url: "https://github.com/sr33j/crowdcode-mcp.git",
    },
    homepage: "https://github.com/sr33j/crowdcode-mcp",
    documentation: "https://github.com/sr33j/crowdcode-mcp#readme",
    support: "https://github.com/sr33j/crowdcode-mcp/issues",
    license: "MIT",
    keywords: ["reputation", "paid APIs", "x402", "MPP", "agent commerce"],
    privacy_policies: [
      "https://github.com/sr33j/crowdcode-mcp#privacy-what-leaves-your-machine",
    ],
    tools: [
      {
        name: "get_service_score",
        description: "Check a paid service's reputation before spending.",
      },
      {
        name: "review_service",
        description: "Submit a payment-verified review after paid use.",
      },
      {
        name: "request_service",
        description: "Record unmet demand for a paid remote API.",
      },
      {
        name: "get_review_signing_payload",
        description: "Build or debug a review-signing payload locally.",
      },
    ],
    tools_generated: false,
    compatibility: {
      platforms: [process.platform],
      runtimes: { node: ">=20" },
    },
    server: {
      type: "node",
      entry_point: "node_modules/crowdcode-mcp/dist/cli.js",
      mcp_config: {
        command: "node",
        args: ["${__dirname}/node_modules/crowdcode-mcp/dist/cli.js"],
        env: {},
      },
    },
  };
  const manifestPath = join(bundleDir, "manifest.json");
  await writeFile(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`);

  await run(npm, ["exec", "--", "mcpb", "validate", manifestPath], {
    cwd: repoRoot,
  });
  await mkdir(dirname(output), { recursive: true });
  await run(npm, ["exec", "--", "mcpb", "pack", bundleDir, output], {
    cwd: repoRoot,
    maxBuffer: 10 * 1024 * 1024,
  });
  process.stdout.write(`${output}\n`);
} finally {
  await rm(temp, { recursive: true, force: true });
}
