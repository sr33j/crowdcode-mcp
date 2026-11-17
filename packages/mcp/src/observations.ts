/**
 * Paid-call observations (C5): sign and submit what the PostToolUse hook
 * recorded in ~/.crowdcode/pending.jsonl.
 *
 * The hook writes metadata only and never touches the network. This module
 * runs inside the crowdcode-mcp server (every 15 s while it is connected) and
 * through `crowdcode-mcp flush`. For each pending line it sends exactly the
 * eight OBSERVATION_FIELDS, signed by the local wallet that paid, to
 * `POST /api/observations`. Nothing else leaves the machine: no request, no
 * response, no tool name, no query string.
 *
 * `crowdcode-mcp off` (the saved default) stops both recording and sending.
 */

import { randomUUID } from "node:crypto";
import { appendFile, mkdir, readdir, readFile, rename, rm, stat } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";
import { loadSolanaWallet, loadWallet, type LoadedWallet, type SolanaWallet } from "./wallet.js";

export const OBSERVATION_FIELDS = [
  "endpoint",
  "method",
  "network",
  "payment_reference",
  "amount_atomic",
  "status",
  "response_bytes",
  "observed_at",
] as const;

export type ObservationRecord = {
  endpoint: string;
  method: string | null;
  network: string;
  payment_reference: string;
  amount_atomic: number | null;
  status: number | null;
  response_bytes: number | null;
  observed_at: string;
};

/** One line of pending.jsonl: the record plus local-only bookkeeping. */
export interface PendingLine extends ObservationRecord {
  payer?: string | null;
  tool?: string;
  attempts?: number;
}

export const MESSAGE_HEADER = "CrowdCode observation v1";
const MAX_ATTEMPTS = 20;
const MAX_AGE_MS = 6 * 24 * 3600 * 1000;
const STALE_CLAIM_MS = 5 * 60 * 1000;
const REQUEST_TIMEOUT_MS = 15_000;

/** Exactly the text the backend verifies (crowdcode.observations.observation_message). */
export function observationMessage(record: ObservationRecord): string {
  return [MESSAGE_HEADER, ...OBSERVATION_FIELDS.map((key) => `${key}:${record[key] ?? ""}`)].join("\n");
}

export function crowdcodeDir(env: NodeJS.ProcessEnv = process.env): string {
  return env.CROWDCODE_HOME ?? join(homedir(), ".crowdcode");
}

export function observationsUrl(backendUrl: string, env: NodeJS.ProcessEnv = process.env): string {
  if (env.CROWDCODE_OBSERVATIONS_URL) return env.CROWDCODE_OBSERVATIONS_URL;
  const url = new URL(backendUrl);
  url.pathname = url.pathname.replace(/\/mcp\/?$/, "") + "/api/observations";
  url.pathname = url.pathname.replace(/\/{2,}/g, "/");
  return url.toString();
}

/** Only the eight contract fields, in order, whatever else the line holds. */
export function toRecord(line: PendingLine): ObservationRecord {
  return Object.fromEntries(OBSERVATION_FIELDS.map((key) => [key, line[key] ?? null])) as ObservationRecord;
}

export interface SubmitBody {
  observation: ObservationRecord;
  signature: string;
  payer_wallet?: string;
  payer_signature?: string;
}

export interface FlushDeps {
  dir?: string;
  url: string;
  walletDir?: string;
  fetch?: typeof fetch;
  enabled?: () => Promise<boolean>;
  now?: () => number;
  log?: (message: string) => void;
}

export interface FlushResult {
  sent: number;
  duplicate: number;
  retry: number;
  dropped: number;
}

type Verdict = "sent" | "duplicate" | "retry" | "dropped";

export async function buildSubmission(
  line: PendingLine,
  wallet: LoadedWallet,
  solana: SolanaWallet | null,
): Promise<SubmitBody | null> {
  if (!wallet.account || !wallet.address) return null;
  const record = toRecord(line);
  const message = observationMessage(record);
  const body: SubmitBody = { observation: record, signature: await wallet.account.signMessage({ message }) };
  if (record.network === "solana") {
    // The Solana payer links the payment; only the local agentcash key can.
    if (!solana || (line.payer && line.payer !== solana.address)) return null;
    body.payer_wallet = solana.address;
    body.payer_signature = solana.sign(message);
  } else if (line.payer && line.payer.toLowerCase() !== wallet.address.toLowerCase()) {
    return null; // paid by some other wallet: this machine cannot vouch for it
  }
  return body;
}

async function submit(body: SubmitBody, deps: FlushDeps): Promise<Verdict> {
  const doFetch = deps.fetch ?? fetch;
  let response: Response;
  try {
    response = await doFetch(deps.url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
  } catch {
    return "retry";
  }
  let payload: Record<string, unknown> = {};
  try { payload = (await response.json()) as Record<string, unknown>; } catch { /* empty */ }
  if (response.ok && payload.accepted === true) return payload.duplicate === true ? "duplicate" : "sent";
  if (response.status >= 500 || response.status === 429 || payload.retryable === true) return "retry";
  deps.log?.(`crowdcode-mcp: observation not accepted (${String(payload.error_code ?? response.status)})`);
  return "dropped";
}

function parseLines(text: string): PendingLine[] {
  const lines: PendingLine[] = [];
  for (const raw of text.split("\n")) {
    if (!raw.trim()) continue;
    try {
      const parsed = JSON.parse(raw) as PendingLine;
      if (parsed && typeof parsed.endpoint === "string" && typeof parsed.payment_reference === "string") lines.push(parsed);
    } catch { /* a torn line is skipped */ }
  }
  return lines;
}

/** Return orphaned claims (a process that died mid-flush) to the queue. */
async function recoverStale(dir: string, now: number): Promise<void> {
  let names: string[];
  try { names = await readdir(dir); } catch { return; }
  for (const name of names) {
    if (!name.startsWith("pending.jsonl.") || !name.endsWith(".sending")) continue;
    const file = join(dir, name);
    try {
      if (now - (await stat(file)).mtimeMs < STALE_CLAIM_MS) continue;
      await appendFile(join(dir, "pending.jsonl"), await readFile(file, "utf8"), { mode: 0o600 });
      await rm(file, { force: true });
    } catch { /* another process got it */ }
  }
}

let flushing: Promise<FlushResult> | null = null;

/** Submit everything pending once. Concurrent calls in one process share a run. */
export function flushPending(deps: FlushDeps): Promise<FlushResult> {
  if (flushing) return flushing;
  flushing = flushOnce(deps).finally(() => { flushing = null; });
  return flushing;
}

async function flushOnce(deps: FlushDeps): Promise<FlushResult> {
  const result: FlushResult = { sent: 0, duplicate: 0, retry: 0, dropped: 0 };
  if (deps.enabled && !(await deps.enabled())) return result;
  const dir = deps.dir ?? crowdcodeDir();
  const now = deps.now?.() ?? Date.now();
  await recoverStale(dir, now);
  const pending = join(dir, "pending.jsonl");
  const claim = join(dir, `pending.jsonl.${process.pid}-${randomUUID()}.sending`);
  // The rename is the lock: the hook keeps appending to a fresh pending.jsonl,
  // and two servers never submit the same line.
  try { await rename(pending, claim); } catch { return result; }
  // A hook that opened the old file just before the rename finishes its write.
  await new Promise((resolve) => setTimeout(resolve, 50));
  const lines = parseLines(await readFile(claim, "utf8"));
  const wallet = await loadWallet({ walletDir: deps.walletDir, autoCreate: false });
  const solana = lines.some((line) => line.network === "solana") ? await loadSolanaWallet(deps.walletDir) : null;
  const requeue: PendingLine[] = [];
  const seen = new Set<string>();
  for (const line of lines) {
    if (seen.has(line.payment_reference)) continue;
    seen.add(line.payment_reference);
    const age = now - Date.parse(line.observed_at);
    if (!(age < MAX_AGE_MS) || (line.attempts ?? 0) >= MAX_ATTEMPTS) { result.dropped += 1; continue; }
    const body = await buildSubmission(line, wallet, solana);
    if (body === null) { result.dropped += 1; continue; }
    const verdict = await submit(body, deps);
    result[verdict] += 1;
    if (verdict === "retry") requeue.push({ ...line, attempts: (line.attempts ?? 0) + 1 });
  }
  if (requeue.length) {
    await mkdir(dir, { recursive: true, mode: 0o700 });
    await appendFile(pending, requeue.map((line) => JSON.stringify(line) + "\n").join(""), { mode: 0o600 });
  }
  await rm(claim, { force: true });
  return result;
}

/** What `crowdcode-mcp install` prints before turning observations on. */
export const OBSERVATION_DISCLOSURE = [
  "Paid-call records (on by default; `crowdcode-mcp off` stops them):",
  "  When an MCP tool such as AgentCash pays for an x402 or MPP call, a local hook",
  "  writes one line to ~/.crowdcode/pending.jsonl. crowdcode-mcp signs it with your",
  "  local wallet and sends exactly these fields to CrowdCode:",
  "    endpoint (no query string), method, network, payment_reference (the settlement",
  "    transaction), amount_atomic, status (HTTP), response_bytes, observed_at",
  "  Never the request, the response, your prompt or any file. CrowdCode checks the",
  "  payment on-chain and counts it as \"someone else paid and it answered\"; it is",
  "  never a rating. The Stop hook reads the local transcript only to decide whether",
  "  to ask for a reflection, and sends nothing.",
].join("\n");
