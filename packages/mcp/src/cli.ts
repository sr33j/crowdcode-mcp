#!/usr/bin/env node
/**
 * crowdcode-mcp — CrowdCode MCP client with local-first PII redaction.
 *
 * Subcommands:
 *   (none) | serve   start the stdio MCP server (default)
 *   install          install the skill and configure supported agents
 *   doctor           verify skill and MCP installation state
 *   check [text]     show what the redaction pipeline does to sample text
 *   clear-cache      delete the downloaded model cache
 */

import { rm, stat } from "node:fs/promises";
import { createInterface } from "node:readline/promises";
import { getConfig } from "./config.js";
import {
  CORE_CLIENTS,
  detectClients,
  doctorClients,
  installForClients,
  type ClientId,
} from "./installer.js";

// stdout carries the MCP protocol in serve mode; anything that would print
// there (transformers download progress, stray library logs) corrupts it.
// Patch before any heavy import happens.
function routeStdoutToStderr(): void {
  console.log = (...args: unknown[]) => console.error(...args);
  console.info = (...args: unknown[]) => console.error(...args);
}

async function readStdin(): Promise<string> {
  const chunks: Buffer[] = [];
  for await (const chunk of process.stdin) {
    chunks.push(chunk as Buffer);
  }
  return Buffer.concat(chunks).toString("utf8");
}

async function runCheck(argText: string | undefined): Promise<void> {
  const config = getConfig();
  const text = argText ?? (await readStdin());
  if (!text.trim()) {
    console.error('usage: crowdcode-mcp check "some text" (or pipe via stdin)');
    process.exit(2);
  }

  const { RedactionEngine } = await import("@crowdcode/redaction");
  const engine = await RedactionEngine.create({
    cacheDir: config.cacheDir,
    enableModel: !config.disableModel,
  });

  if (!config.disableModel) {
    console.error(
      "loading PII model (downloads ~15 MB to " +
        config.cacheDir +
        " on first run; deterministic recognizers work without it) ...",
    );
    await engine.waitForModel();
  }

  const result = await engine.redact(text);
  const revealed = await engine.reveal(result.text);

  console.error("");
  console.error("input:     " + JSON.stringify(text));
  console.error("redacted:  " + JSON.stringify(result.text));
  console.error("revealed:  " + JSON.stringify(revealed) + "  (local round-trip)");
  console.error("");
  console.error(
    `entities removed: ${result.entitiesRemoved}   model active: ${result.modelActive}`,
  );
  console.error(
    "only the redacted form would ever be sent to the CrowdCode backend.",
  );
}

async function runClearCache(): Promise<void> {
  const config = getConfig();
  try {
    await stat(config.cacheDir);
  } catch {
    console.error(`nothing to clear (${config.cacheDir} does not exist)`);
    return;
  }
  await rm(config.cacheDir, { recursive: true, force: true });
  console.error(`cleared ${config.cacheDir}`);
}

interface InstallerFlags {
  clients: ClientId[];
  allDetected: boolean;
  yes: boolean;
}

function parseInstallerFlags(args: string[]): InstallerFlags {
  const clients: ClientId[] = [];
  let allDetected = false;
  let yes = false;
  for (let index = 0; index < args.length; index += 1) {
    const arg = args[index]!;
    if (arg === "--yes" || arg === "-y") {
      yes = true;
      continue;
    }
    if (arg === "--all-detected") {
      allDetected = true;
      continue;
    }
    let value: string | undefined;
    if (arg === "--client") {
      value = args[++index];
      if (!value) throw new Error("--client requires a client identifier");
    } else if (arg.startsWith("--client=")) {
      value = arg.slice("--client=".length);
    } else {
      throw new Error(`unknown install option: ${arg}`);
    }
    for (const candidate of value.split(",")) {
      if (!(CORE_CLIENTS as readonly string[]).includes(candidate)) {
        throw new Error(
          `unknown client ${JSON.stringify(candidate)}; use ${CORE_CLIENTS.join(", ")}`,
        );
      }
      if (!clients.includes(candidate as ClientId)) clients.push(candidate as ClientId);
    }
  }
  if (allDetected && clients.length > 0) {
    throw new Error("use either --all-detected or --client, not both");
  }
  return { clients, allDetected, yes };
}

async function chooseInstallClients(flags: InstallerFlags): Promise<ClientId[]> {
  if (flags.clients.length > 0) return flags.clients;
  const detected = await detectClients();
  if (flags.allDetected) {
    if (detected.length === 0) {
      throw new Error("no supported clients were detected; use --client <client>");
    }
    return detected;
  }
  if (flags.yes || !process.stdin.isTTY || !process.stderr.isTTY) {
    return detected.length > 0 ? detected : [...CORE_CLIENTS];
  }

  const defaults = detected.length > 0 ? detected : [...CORE_CLIENTS];
  console.error("Supported clients:");
  CORE_CLIENTS.forEach((client, index) => {
    console.error(`  ${index + 1}. ${client}${detected.includes(client) ? " (detected)" : ""}`);
  });
  const prompt = createInterface({ input: process.stdin, output: process.stderr });
  try {
    const answer = await prompt.question(
      `Install for comma-separated names/numbers [${defaults.join(",")}]: `,
    );
    if (!answer.trim()) return defaults;
    const selected: ClientId[] = [];
    for (const token of answer.split(",").map((item) => item.trim())) {
      const numbered = Number(token);
      const client = Number.isInteger(numbered)
        ? CORE_CLIENTS[numbered - 1]
        : (token as ClientId);
      if (!client || !(CORE_CLIENTS as readonly string[]).includes(client)) {
        throw new Error(`unknown client selection: ${token}`);
      }
      if (!selected.includes(client)) selected.push(client);
    }
    return selected;
  } finally {
    prompt.close();
  }
}

async function runInstall(args: string[]): Promise<void> {
  const flags = parseInstallerFlags(args);
  const clients = await chooseInstallClients(flags);
  const results = await installForClients(clients);
  console.error("CrowdCode installation complete:");
  for (const result of results) {
    console.error(
      `  ${result.client}: MCP ${result.configPath}; skill ${result.skillPath} (${result.skillMode})`,
    );
  }
  console.error("Restart these clients so they reload the CrowdCode skill and MCP server.");
}

async function runDoctor(args: string[]): Promise<void> {
  const flags = parseInstallerFlags(args);
  const clients =
    flags.clients.length > 0
      ? flags.clients
      : flags.allDetected
        ? await detectClients()
        : [...CORE_CLIENTS];
  if (clients.length === 0) {
    throw new Error("no supported clients were detected; use --client <client>");
  }
  const results = await doctorClients(clients);
  let healthy = true;
  for (const result of results) {
    const ok = result.mcpConfigured && result.skillCurrent;
    healthy &&= ok;
    console.error(
      `${ok ? "ok" : "missing/stale"}  ${result.client}` +
        `  MCP=${result.mcpConfigured ? "ok" : "missing"}` +
        `  skill=${result.skillCurrent ? "ok" : "missing/stale"}`,
    );
    if (result.detail) console.error(`  ${result.detail}`);
  }
  if (!healthy) process.exitCode = 1;
}

function printHelp(): void {
  console.error(
    "usage: crowdcode-mcp [serve|install|doctor|check <text>|clear-cache]\n" +
      "  serve        start the stdio MCP server (default)\n" +
      "  install      install the CrowdCode skill and MCP config\n" +
      "               [--client <client>] [--all-detected] [--yes]\n" +
      "  doctor       verify skill and MCP config [--client <client>] [--all-detected]\n" +
      "  check        demo the local redaction pipeline on sample text\n" +
      "  clear-cache  delete the downloaded model cache\n" +
      `  clients      ${CORE_CLIENTS.join(", ")}`,
  );
}

async function main(): Promise<void> {
  const [, , command, ...rest] = process.argv;
  switch (command) {
    case undefined:
    case "serve": {
      routeStdoutToStderr();
      const { startServer } = await import("./server.js");
      await startServer();
      break;
    }
    case "check":
      await runCheck(rest.length > 0 ? rest.join(" ") : undefined);
      break;
    case "install":
      await runInstall(rest);
      break;
    case "doctor":
      await runDoctor(rest);
      break;
    case "clear-cache":
      await runClearCache();
      break;
    case "--help":
    case "-h":
      printHelp();
      break;
    default:
      console.error(`unknown command: ${command} (try --help)`);
      process.exit(2);
  }
}

main().catch((err) => {
  console.error("crowdcode-mcp fatal:", err);
  process.exit(1);
});
