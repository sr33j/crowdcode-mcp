/**
 * spec/conformance.json against crowdcode-mcp: the session every CrowdCode
 * agent client must pass, against spec/mock/mock_server.py (which checks real
 * signatures, x402 payments and EIP-3009 authorizations). The same steps run
 * against the Hermes plugin (plugins/hermes/tests/test_conformance.py); the
 * checks here are the ones that runner makes.
 *
 * The mock needs a Python with eth_account: set CROWDCODE_MOCK_PYTHON to it,
 * e.g. CROWDCODE_MOCK_PYTHON=/tmp/plain-venv/bin/python npm test. Without it
 * these tests are skipped.
 */

import { type ChildProcess, spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdtemp, readFile, readdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { login, readCredentials, saveCredentials } from "../src/agents/account.js";
import { CONTRACT } from "../src/agents/contract.js";
import { Dashboard } from "../src/agents/dashboard.js";
import { type AgentTools, createAgentTools, type ToolResult } from "../src/agents/tools.js";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
const SESSION = JSON.parse(
  await readFile(resolve(repoRoot, "spec/conformance.json"), "utf8"),
) as { steps: Step[] };
const RESULT_FIELDS = Object.fromEntries(CONTRACT.tools.map((t) => [t.name, t.result_fields]));

const PYTHON = process.env.CROWDCODE_MOCK_PYTHON;
if (!PYTHON) {
  console.warn(
    "conformance.test.ts: skipped. Set CROWDCODE_MOCK_PYTHON to a Python with eth_account " +
      "(e.g. CROWDCODE_MOCK_PYTHON=/tmp/plain-venv/bin/python) to run spec/conformance.json against the mock.",
  );
}

interface Step {
  name: string;
  tool?: string;
  harness?: string;
  args?: Record<string, unknown>;
  ok?: boolean;
  equals?: Record<string, unknown>;
  length?: Record<string, number>;
  min_length?: Record<string, number>;
  contains?: Record<string, string>;
  error_contains?: string;
  save?: Record<string, string>;
}

function substitute(value: unknown, saved: Record<string, unknown>): unknown {
  if (typeof value === "string" && value.startsWith("$")) return saved[value.slice(1)];
  if (Array.isArray(value)) return value.map((v) => substitute(v, saved));
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, substitute(v, saved)]));
  }
  return value;
}

function get(data: unknown, path: string): unknown {
  let current = data as any;
  for (const part of path.split(".")) current = Array.isArray(current) ? current[Number(part)] : current?.[part];
  return current;
}

function check(step: Step, result: ToolResult, saved: Record<string, unknown>): void {
  const where = `step '${step.name}': ${JSON.stringify(result).slice(0, 600)}`;
  expect(result.ok, where).toBe(step.ok ?? true);
  if (result.ok) {
    for (const field of RESULT_FIELDS[step.tool!] ?? []) expect(field in result, `missing ${field} in ${where}`).toBe(true);
  }
  for (const [path, want] of Object.entries(substitute(step.equals ?? {}, saved) as Record<string, unknown>)) {
    const got = get(result, path);
    if (typeof want === "number" && typeof got === "number") expect(Math.abs(got - want), `${path} in ${where}`).toBeLessThan(1e-9);
    else expect(got, `${path} in ${where}`).toEqual(want);
  }
  for (const [path, n] of Object.entries(step.length ?? {})) {
    expect((get(result, path) as unknown[]).length, `len(${path}) in ${where}`).toBe(n);
  }
  for (const [path, n] of Object.entries(step.min_length ?? {})) {
    expect((get(result, path) as unknown[]).length, `len(${path}) in ${where}`).toBeGreaterThanOrEqual(n);
  }
  for (const [path, part] of Object.entries(step.contains ?? {})) {
    expect(String(get(result, path)), `${path} in ${where}`).toContain(part);
  }
  if (step.error_contains !== undefined) expect(String(result.error ?? ""), where).toContain(step.error_contains);
  for (const [name, path] of Object.entries(step.save ?? {})) saved[name] = get(result, path);
}

/** What a browser does with the /connect link: the mock approves at once and redirects to the loopback. */
async function browser(url: string): Promise<void> {
  const response = await fetch(url, { redirect: "manual" });
  const location = response.headers.get("location");
  if (!location) throw new Error(`/connect answered ${response.status} without a redirect`);
  await (await fetch(location)).text();
}

async function startMock(python: string): Promise<{ url: string; child: ChildProcess }> {
  const child = spawn(python, ["-u", resolve(repoRoot, "spec/mock/mock_server.py"), "--port", "0"], {
    stdio: ["ignore", "pipe", "pipe"],
  });
  const url = await new Promise<string>((resolveUrl, reject) => {
    let out = "";
    let err = "";
    const timer = setTimeout(() => reject(new Error(`mock did not start: ${err}`)), 15_000);
    child.stdout!.on("data", (chunk) => {
      out += chunk;
      const match = /CrowdCode mock on (http:\/\/127\.0\.0\.1:\d+)/.exec(out);
      if (match) {
        clearTimeout(timer);
        resolveUrl(match[1]!);
      }
    });
    child.stderr!.on("data", (chunk) => (err += chunk));
    child.on("exit", (code) => reject(new Error(`mock exited ${code}: ${err}`)));
  });
  return { url, child };
}

describe.skipIf(!PYTHON || !existsSync(PYTHON))("spec/conformance.json against the mock", () => {
  let mock: { url: string; child: ChildProcess };
  let root: string;
  let env: NodeJS.ProcessEnv;
  let cwd: string;
  let tools: AgentTools;

  beforeAll(async () => {
    mock = await startMock(PYTHON!);
    root = await mkdtemp(join(tmpdir(), "cc-conformance-"));
    cwd = join(root, "session");
    await import("node:fs/promises").then((fs) => fs.mkdir(cwd));
    // A clean environment: nothing from this machine's CrowdCode or AgentCash setup leaks in.
    env = {
      CROWDCODE_APP_URL: mock.url,
      CROWDCODE_BASE_RPC_URL: mock.url + "/rpc", // the mock has no RPC: the balance reads "unavailable"
      CROWDCODE_HOME: join(root, "crowdcode"),
      CROWDCODE_WALLET_DIR: join(root, "agentcash"),
    };
    tools = createAgentTools({ env, cwd, createPollMs: 1_100, createWaitMs: 10_000, taskPollMs: 500 });
  }, 30_000);

  afterAll(async () => {
    mock?.child.kill();
    if (root) await rm(root, { recursive: true, force: true });
  });

  it("passes every step of the session", async () => {
    const handlers = tools.handlers();
    const saved: Record<string, unknown> = {};
    for (const step of SESSION.steps) {
      if (step.harness === "approve_claim") {
        const code = String(substitute(step.args!.url, saved)).split("#")[1];
        const response = await fetch(`${mock.url}/__mock/approve_claim`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ code }),
        });
        expect(response.status, step.name).toBe(200);
        continue;
      }
      if (step.harness === "login") {
        await login({ baseUrl: mock.url, home: env.CROWDCODE_HOME!, openBrowser: browser, log: () => undefined });
        expect(await tools.mode(), step.name).toBe("account");
        continue;
      }
      expect(step.harness, `unknown harness action in '${step.name}'`).toBeUndefined();
      const handler = handlers[step.tool as keyof typeof handlers];
      expect(handler, `no handler for ${step.tool}`).toBeDefined();
      const result = await handler(substitute(step.args ?? {}, saved) as Record<string, unknown>);
      check(step, result, saved);
    }

    // The downloads landed in ./crowdcode-files/scout/ of the session folder, none overwritten.
    const saved_files = (await readdir(join(cwd, "crowdcode-files", "scout"))).sort();
    expect(saved_files).toEqual(["notes.txt", "report-1.md", "report.md"]);
    // The account sign-in is a 0600 file holding the grant for this origin only.
    const credentials = await readCredentials(env.CROWDCODE_HOME!, mock.url);
    expect(credentials?.access_token).toMatch(/^cc_at_/);
    expect(credentials?.scope).toBe("read tasks transfers withdraw_to_main services files");
  }, 120_000);

  it("refreshes an expired or rejected access token, and signs out when the refresh is refused", async () => {
    const home = env.CROWDCODE_HOME!;
    const handlers = tools.handlers();
    const before = (await readCredentials(home, mock.url))!;

    await saveCredentials(home, { ...before, expires_at: Math.floor(Date.now() / 1000) - 10 });
    expect((await handlers.crowdcode_agents({})).ok).toBe(true);
    const refreshed = (await readCredentials(home, mock.url))!;
    expect(refreshed.access_token).not.toBe(before.access_token);
    expect(refreshed.refresh_token).not.toBe(before.refresh_token);

    await saveCredentials(home, { ...refreshed, access_token: "cc_at_revoked" });
    expect((await handlers.crowdcode_agents({})).ok).toBe(true);
    const again = (await readCredentials(home, mock.url))!;
    expect(again.access_token).not.toBe("cc_at_revoked");

    await saveCredentials(home, { ...again, access_token: "cc_at_revoked", refresh_token: "cc_rt_used" });
    const out = await handlers.crowdcode_agents({});
    expect(out).toMatchObject({ ok: false, hint: expect.stringContaining("npx crowdcode-mcp login") });
    expect(await readCredentials(home)).toBeNull();
    expect(await tools.mode()).toBe("wallet");
  }, 30_000);

  it("renders the dashboard, logs out and revokes", async () => {
    const dashboard = new Dashboard(tools);
    const overview = await dashboard.run("");
    expect(overview.ok).toBe(true);
    expect(overview.text).toContain("Wallet   0x");
    expect(overview.text).toContain("helper");
    expect(overview.text).toContain("Recent tasks");
    expect((await dashboard.run("tasks")).text).toContain("Build a tiny paid service that counts words");
    expect((await dashboard.run("demand")).text).toContain("Bill Table Extractor");
    expect((await dashboard.run("wallet init")).text).toContain("already exists (not replaced)");
    expect((await dashboard.run("mode")).text).toContain("Using this machine's wallet. mode setting: auto.");

    await login({ baseUrl: mock.url, home: env.CROWDCODE_HOME!, openBrowser: browser, log: () => undefined });
    const account = await dashboard.run([]);
    expect(account.text).toContain("Account  signed in with crowdcode-mcp; main wallet 0x37A1E4ef2fE11b6ec9DA1f2b2d7A55aE0f9A7B31");
    expect(account.text).toContain("scout");
    expect(account.text).toContain("scout/scout-svc");
    const files = await dashboard.run(["files", "scout"]);
    // three from each of the session's two tasks
    expect(files.text).toContain("Files of scout  (6,");
    const download = await dashboard.run(["download", "scout", "all", "--dest", "again"]);
    expect(download.ok).toBe(true);
    expect(download.text).toContain(`Saved 6 file(s)`);
    expect((await readdir(join(cwd, "again"))).sort()).toEqual([
      "notes-1.txt", "notes.txt", "report-1.md", "report-2.md", "report-3.md", "report.md",
    ]);

    const token = (await readCredentials(env.CROWDCODE_HOME!))!.access_token;
    const out = await dashboard.run("logout");
    expect(out.text).toContain("Disconnected this machine from your CrowdCode account");
    expect(await readCredentials(env.CROWDCODE_HOME!)).toBeNull();
    const revoked = await fetch(`${mock.url}/api/app/v1/me`, { headers: { authorization: `Bearer ${token}` } });
    expect(revoked.status).toBe(401);
    expect((await dashboard.run("logout")).text).toBe("This machine was not signed in to a CrowdCode account.");
  }, 60_000);
});
