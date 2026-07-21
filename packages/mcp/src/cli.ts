#!/usr/bin/env node
/**
 * crowdcode-mcp — CrowdCode MCP client with local-first PII redaction.
 *
 * Subcommands:
 *   (none) | serve   start the stdio MCP server (default)
 *   check [text]     show what the redaction pipeline does to sample text
 *   clear-cache      delete the downloaded model cache
 */

import { rm, stat } from "node:fs/promises";
import { getConfig } from "./config.js";

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
    case "clear-cache":
      await runClearCache();
      break;
    case "--help":
    case "-h":
      console.error(
        "usage: crowdcode-mcp [serve|check <text>|clear-cache]\n" +
          "  serve        start the stdio MCP server (default)\n" +
          "  check        demo the local redaction pipeline on sample text\n" +
          "  clear-cache  delete the downloaded model cache",
      );
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
