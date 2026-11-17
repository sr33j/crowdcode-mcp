/**
 * The 13 CrowdCode agent tools of spec/agent-tools.json, ported from the Hermes
 * plugin's tools.py: the same `{ok, error, hint}` shapes, field names and
 * validations, money as the API returns it.
 *
 * Deliberately not ported: Hermes's approvals, spend guard and local daily
 * cap. Codex and Claude Code keep CrowdCode's own controls (agent spend
 * policies; crowdcode.app approval for account funding). The per-call checks
 * that belong to the tools stay: a task needs a budget, creation refuses a
 * quote above max_price_usd before signing, the $0.50 minimums, and ids are
 * checked before they go into a URL path.
 *
 * Each handler resolves to a plain result object and never throws.
 */

import { createHash } from "node:crypto";
import { mkdir, realpath } from "node:fs/promises";
import { getConfig } from "../config.js";
import { loadWallet } from "../wallet.js";
import { AccountClient, type BrowserOpener, modeSetting, openInBrowser, readCredentials } from "./account.js";
import { ApiError, type OwnerApi, PublicClient, WalletClient } from "./client.js";
import { AGENT_TOOL_NAMES, type AgentToolName } from "./contract.js";
import {
  LOGIN_COMMAND,
  NotSignedIn,
  ToolError,
  UncertainError,
  WALLET_INIT_COMMAND,
  WalletMissing,
} from "./errors.js";
import { DEFAULT_MAX_DOWNLOAD_MB, downloadFile, type FileEntry, listFiles, planDest } from "./files.js";
import { crowdcodeHome, recentTasks, rememberTask, type TaskRecord } from "./home.js";
import { pyDumps, pyFloat } from "./pyjson.js";
import { Signer, unitsToUsd, usdToUnits } from "./signer.js";

export const DEFAULT_BASE_URL = "https://crowdcode.app";
export const DEFAULT_RPC = "https://mainnet.base.org";
export const NETWORKS = ["base", "solana", "tempo"] as const;
/** To the owner's EVM address; Tempo only for EOA owners. */
export const WITHDRAW_NETWORKS = ["base", "tempo"] as const;
/** The platform's floor for relayed deposits, withdrawals and transfers. */
export const MIN_MOVE_USD = 0.5;
export const DEFAULT_CREATE_MAX_USD = 0.5;
export const TERMINAL = ["completed", "failed", "cancelled"];
export const ID_RE = /^[A-Za-z0-9_-]{1,64}$/;
export const FUND_HINT =
  "Send USDC on Base to this address from any exchange or wallet (Coinbase, a Base wallet, etc.). " +
  "No ETH is needed: agent creation is paid with a signed x402 authorization and deposits are gasless.";
export const UNSURE =
  "CrowdCode may have received this; the outcome is unknown. Do NOT retry: check balances with " +
  "crowdcode_agents (and crowdcode_wallet) first.";
const WALLET_HINT = `Run \`${WALLET_INIT_COMMAND}\` (or crowdcode_wallet with init=true), or \`${LOGIN_COMMAND}\` to use your CrowdCode account.`;

export type ToolResult = Record<string, unknown> & { ok: boolean };
export type Args = Record<string, unknown>;
export type AgentHandler = (args?: Args) => Promise<ToolResult>;

export interface AgentToolsOptions {
  /** Read at every call; default process.env. */
  env?: NodeJS.ProcessEnv;
  /** The session's working directory (downloads); default process.cwd() at call time. */
  cwd?: string | (() => string);
  /**
   * Opens a crowdcode.app page for the owner (account-mode funding approvals). Default: the system
   * browser; `CROWDCODE_NO_BROWSER=1` turns it off. Tests inject a recorder.
   */
  openBrowser?: BrowserOpener;
  /** Polling knobs; tests shorten them. */
  createPollMs?: number;
  createWaitMs?: number;
  taskPollMs?: number;
}

const ok = (data: Record<string, unknown>): ToolResult => ({ ok: true, ...data });
const fail = (error: string, data: Record<string, unknown> = {}): ToolResult => ({ ok: false, error, ...data });
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
const nowSeconds = () => Date.now() / 1000;

/** Python truthiness for the values a model sends. */
function truthy(value: unknown): boolean {
  return !(value === undefined || value === null || value === false || value === 0 || value === "" ||
    (Array.isArray(value) && value.length === 0));
}

/** `float(args.get(key) or 0)`. */
function amount(args: Args, key: string, fallback = 0): number {
  const raw = args[key];
  if (!truthy(raw)) return fallback;
  const value = typeof raw === "number" ? raw : Number(String(raw).trim());
  if (Number.isNaN(value) || typeof raw === "boolean" || (typeof raw === "string" && !String(raw).trim())) {
    throw new ToolError(`${key} must be a number`);
  }
  return value;
}

/** A CrowdCode id from the model, checked before it goes into a URL path. */
export function checkId(args: Args, key: string): string {
  const value = truthy(args[key]) ? String(args[key]).trim() : "";
  if (!value) throw new ToolError(`${key} is required`);
  if (!ID_RE.test(value)) throw new ToolError(`${key} is not a valid CrowdCode id`);
  return value;
}

function words(value: unknown): string[] {
  if (typeof value === "string") return value.replace(/,/g, " ").split(/\s+/).filter(Boolean);
  if (Array.isArray(value)) return value.map((v) => String(v));
  return [];
}

function money(value: number): string {
  return `$${value.toFixed(2)}`;
}

function fileSummary(f: FileEntry): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const key of ["id", "name", "path", "size_bytes", "content_type", "task_id", "created_at"]) {
    if (key in f) out[key] = f[key] ?? null;
  }
  return out;
}

/** True when no money can have moved: never sent, or refused with a 4xx. */
function definitelyFailed(err: unknown): boolean {
  if (err instanceof ApiError) return err.status >= 400 && err.status < 500;
  if (err instanceof ToolError || err instanceof WalletMissing || err instanceof NotSignedIn) return true;
  const code = (err as { cause?: { code?: string } })?.cause?.code;
  return ["ECONNREFUSED", "ENOTFOUND", "EAI_AGAIN", "UND_ERR_CONNECT_TIMEOUT", "ERR_INVALID_URL"].includes(code ?? "");
}

export function hintFor(err: ApiError): string {
  if (err.status === 401 && err.error === "invalid_token") {
    return `The CrowdCode sign-in was revoked or expired. Sign in again: ${LOGIN_COMMAND}`;
  }
  if (err.status === 403 && err.error === "insufficient_scope") {
    return `Your CrowdCode sign-in does not allow this. Sign in again to grant it (${LOGIN_COMMAND}).`;
  }
  if (err.status === 403 && err.error === "daily_cap") {
    return "The daily cap you set when connecting crowdcode-mcp is used up for today (UTC).";
  }
  if (err.status === 401) {
    return (
      { expired: "The request signature expired: check this machine's clock.", replayed: "The request nonce was reused; retry once." }[
        err.error
      ] ?? "CrowdCode rejected the wallet signature."
    );
  }
  if (err.status === 403) return "This wallet does not own that agent. Use crowdcode_agents to list the agents you own.";
  if (err.status === 402) return "Not enough USDC. Check balances with crowdcode_agents / crowdcode_wallet and fund first.";
  if (err.status === 409 && err.error === "task_active") {
    return "That agent is already running a task (one at a time). Check it with crowdcode_task_status or cancel it.";
  }
  if (err.status === 404) return "Not found: check the agent or task id.";
  return "";
}

function describeError(err: unknown): string {
  if (!(err instanceof Error)) return String(err);
  const cause = (err as { cause?: { code?: string; message?: string } }).cause;
  const extra = cause?.code ?? (cause?.message && cause.message !== err.message ? cause.message : "");
  return `${err.name}: ${err.message}${extra ? ` (${extra})` : ""}`;
}

/** The result for an error, like Hermes's `_guarded`. */
export function failureFor(err: unknown): ToolResult {
  if (err instanceof WalletMissing) return fail(err.message, { hint: WALLET_HINT });
  if (err instanceof NotSignedIn) return fail(err.message, { hint: `Sign in with: ${LOGIN_COMMAND} (in a terminal)` });
  if (err instanceof ApiError) {
    return fail(`CrowdCode API: ${err.error}`, { status: err.status, hint: hintFor(err), detail: err.detail ?? null });
  }
  if (err instanceof ToolError) return fail(err.message);
  return fail(describeError(err));
}

export class AgentTools {
  private readonly options: AgentToolsOptions;

  constructor(options: AgentToolsOptions = {}) {
    this.options = options;
  }

  // settings ------------------------------------------------------------------
  get env(): NodeJS.ProcessEnv {
    return this.options.env ?? process.env;
  }

  get home(): string {
    return crowdcodeHome(this.env);
  }

  cwd(): string {
    const cwd = this.options.cwd;
    return typeof cwd === "function" ? cwd() : (cwd ?? process.cwd());
  }

  baseUrl(): string {
    const url = (this.env.CROWDCODE_APP_URL?.trim() || DEFAULT_BASE_URL).replace(/\/+$/, "");
    let parts: URL;
    try {
      parts = new URL(url);
    } catch {
      throw new ToolError("CROWDCODE_APP_URL must be an https:// URL");
    }
    const local = ["localhost", "127.0.0.1", "[::1]", "::1"].includes(parts.hostname);
    if (parts.protocol !== "https:" && !(local && parts.protocol === "http:")) {
      throw new ToolError("CROWDCODE_APP_URL must be an https:// URL (plain http is allowed only for localhost)");
    }
    return url;
  }

  rpcUrl(): string {
    return this.env.CROWDCODE_BASE_RPC_URL?.trim() || DEFAULT_RPC;
  }

  maxDownloadBytes(): number {
    const mb = Number(this.env.CROWDCODE_MAX_DOWNLOAD_MB || DEFAULT_MAX_DOWNLOAD_MB);
    return Math.floor((Number.isFinite(mb) && mb > 0 ? mb : DEFAULT_MAX_DOWNLOAD_MB) * 1_000_000);
  }

  // credentials -----------------------------------------------------------------
  /** The local wallet; with `create`, make one first if there is none (unless creation is turned off). */
  async loadSigner(create = false): Promise<{ signer: Signer; created: boolean }> {
    const config = getConfig(this.env);
    const wallet = await loadWallet({
      env: this.env,
      walletDir: config.walletDir,
      autoCreate: create && config.walletAutoCreate,
    });
    if (!wallet.account) {
      if (wallet.error) throw new WalletMissing(wallet.error);
      if (create && !config.walletAutoCreate) {
        throw new WalletMissing(
          "No CrowdCode wallet yet, and creating one is turned off here (CROWDCODE_DISABLE_WALLET_CREATE).",
        );
      }
      throw new WalletMissing(`No CrowdCode wallet yet. Run \`${WALLET_INIT_COMMAND}\` to create one.`);
    }
    return { signer: new Signer(wallet.account), created: wallet.created === true };
  }

  async walletExists(): Promise<boolean> {
    try {
      await this.loadSigner(false);
      return true;
    } catch {
      return false;
    }
  }

  /** `account` (the crowdcode-mcp sign-in) or `wallet` (the local key). `auto` = the account when signed in. */
  async mode(): Promise<"account" | "wallet"> {
    const { setting } = await modeSetting(this.env, this.home);
    if (setting !== "auto") return setting;
    return (await readCredentials(this.home, this.baseUrl())) ? "account" : "wallet";
  }

  account(): AccountClient {
    return new AccountClient(this.baseUrl(), this.home);
  }

  publicApi(): PublicClient {
    return new PublicClient(this.baseUrl());
  }

  async walletApi(): Promise<WalletClient> {
    return new WalletClient(this.baseUrl(), (await this.loadSigner()).signer);
  }

  /** The owner API client for the current mode. */
  async api(): Promise<OwnerApi> {
    if ((await this.mode()) === "account") return this.account();
    try {
      return await this.walletApi();
    } catch (err) {
      if (err instanceof WalletMissing) {
        throw new WalletMissing(`${err.message} Or sign in to your CrowdCode account instead: ${LOGIN_COMMAND}.`);
      }
      throw err;
    }
  }

  async recentTasks(): Promise<TaskRecord[]> {
    return recentTasks(this.home);
  }

  // plumbing --------------------------------------------------------------------
  /** Run a request that may move money; an unclear outcome says so instead of inviting a retry. */
  private async moneyCall<T>(call: () => Promise<T>): Promise<T> {
    try {
      return await call();
    } catch (err) {
      if (definitelyFailed(err)) throw err;
      throw new UncertainError(`${describeError(err)}. ${UNSURE}`);
    }
  }

  private guarded(fn: (args: Args) => Promise<ToolResult>): AgentHandler {
    return async (args?: Args) => {
      try {
        return await fn(args ?? {});
      } catch (err) {
        return failureFor(err);
      }
    };
  }

  handlers(): Record<AgentToolName, AgentHandler> {
    const table: Record<AgentToolName, (args: Args) => Promise<ToolResult>> = {
      crowdcode_wallet: (a) => this.wallet(a),
      crowdcode_agents: (a) => this.agents(a),
      crowdcode_create_agent: (a) => this.createAgent(a),
      crowdcode_fund_agent: (a) => this.fundAgent(a),
      crowdcode_run_task: (a) => this.runTask(a),
      crowdcode_task_status: (a) => this.taskStatus(a),
      crowdcode_services: (a) => this.services(a),
      crowdcode_withdraw: (a) => this.withdraw(a),
      crowdcode_transfer: (a) => this.transfer(a),
      crowdcode_demand: (a) => this.demand(a),
      crowdcode_files: (a) => this.files(a),
      crowdcode_download: (a) => this.download(a),
      crowdcode_claim: (a) => this.claim(a),
    };
    return Object.fromEntries(AGENT_TOOL_NAMES.map((name) => [name, this.guarded(table[name])])) as Record<
      AgentToolName,
      AgentHandler
    >;
  }

  // tools -------------------------------------------------------------------------
  async wallet(args: Args): Promise<ToolResult> {
    let created = false;
    if (truthy(args.init)) created = (await this.loadSigner(true)).created;
    if (!truthy(args.init) && (await this.mode()) === "account") {
      const me = await this.account().signed("GET", "/me");
      const main = typeof me.address === "string" ? me.address : null;
      const plain = main ? (main.split(":").pop() ?? null) : null;
      let balance: unknown = "unavailable (no main wallet yet)";
      if (plain && /^0x[0-9a-fA-F]{40}$/.test(plain)) balance = await this.balance(plain);
      return ok({
        mode: "account",
        address: plain,
        owner: main,
        usdc_base: balance,
        main_wallet: main,
        agents: (me.agents ?? []).length,
        connection: me.connection ?? {},
        wallet_key: await this.walletExists(),
        fund: `Fund agents from your main wallet at ${this.baseUrl()}/wallet; only you can sign for it.`,
      });
    }
    const { signer } = await this.loadSigner(false);
    return ok({
      mode: "wallet",
      created,
      address: signer.address,
      owner: signer.caip10,
      usdc_base: await this.balance(signer.address),
      fund: FUND_HINT,
    });
  }

  private async balance(address: string): Promise<number | string> {
    try {
      const usd = await this.publicApi().usdcBalance(this.rpcUrl(), address);
      return Math.round(usd * 1e6) / 1e6;
    } catch (err) {
      return `unavailable (${err instanceof Error ? err.name : typeof err})`;
    }
  }

  async agents(args: Args): Promise<ToolResult> {
    const api = await this.api();
    if (truthy(args.agent_id)) return ok({ agent: await api.signed("GET", `/agents/${checkId(args, "agent_id")}`) });
    const me = await api.signed("GET", "/me");
    const listed: unknown[] = [];
    for (const item of ((me.agents ?? []) as Record<string, any>[]).slice(0, 12)) {
      try {
        const detail = await api.signed("GET", `/agents/${item.id}`);
        listed.push({
          id: item.id,
          name: detail.name ?? item.name,
          status: detail.status ?? item.status,
          balances_usd: detail.balances_usd ?? null,
          addresses: detail.addresses ?? null,
        });
      } catch (err) {
        if (!(err instanceof ApiError)) throw err;
        listed.push(item);
      }
    }
    return ok({ owner: me.address ?? null, agents: listed });
  }

  async createAgent(args: Args): Promise<ToolResult> {
    const name = truthy(args.name) ? String(args.name).trim() : "";
    const length = [...name].length;
    if (length < 3 || length > 40) throw new ToolError("name must be 3-40 characters");
    const body: Record<string, unknown> = { name };
    if (truthy(args.model)) body.model = String(args.model);
    if (truthy(args.instructions)) {
      if ([...String(args.instructions)].length > 4000) {
        throw new ToolError("instructions must be at most 4000 characters");
      }
      body.instructions = String(args.instructions);
    }
    const maxPrice = amount(args, "max_price_usd", DEFAULT_CREATE_MAX_USD);
    if ((await this.mode()) === "account") {
      return fail(
        "In account mode, create agents at crowdcode.app (your main wallet pays for them); " +
          "only the wallet mode pays for creation from here.",
        { create_at: `${this.baseUrl()}/agents` },
      );
    }
    const api = (await this.api()) as WalletClient;
    const url = this.baseUrl() + "/s/crowdcode/agents";
    const quote = await api.quote(url, body);
    const price = unitsToUsd(BigInt(String(quote.offer.amount)));
    if (price > maxPrice + 1e-9) {
      return fail(
        `Creating an agent costs ${money(price)}, above max_price_usd ${money(maxPrice)}; nothing was paid.`,
        { price_usd: price },
      );
    }
    const paid = await this.moneyCall(() => api.pay(url, body, quote));
    const tx = paid.settlement?.transaction ?? null;
    let created = paid.body as Record<string, any>;
    if (paid.status >= 400) {
      const settled = Boolean(paid.settlement?.success);
      if (paid.status >= 500 && !settled) {
        return fail(`Agent creation answered ${paid.status}. ${UNSURE}`, { detail: created });
      }
      const why = created && typeof created === "object" ? created.error : created;
      return fail(`Agent creation failed (${paid.status}): ${why}`, {
        status: paid.status,
        paid: settled,
        detail: created,
      });
    }
    const agentId = String(created.agent_id ?? "");
    // Ownership is bound from the payment's settlement, a few seconds after this 201.
    let statusNow = String(created.status ?? "pending");
    const deadline = Date.now() + (this.options.createWaitMs ?? 90_000);
    while (agentId && statusNow === "pending" && Date.now() < deadline) {
      await sleep(this.options.createPollMs ?? 3_000);
      let detail: Record<string, any>;
      try {
        detail = await api.signed("GET", `/agents/${agentId}`);
      } catch (err) {
        if (err instanceof ApiError && (err.status === 403 || err.status === 404)) continue; // not bound to us yet
        throw err;
      }
      statusNow = String(detail.status ?? statusNow);
      const picked = Object.fromEntries(
        Object.entries(detail).filter(([k]) => ["addresses", "owner", "status", "name"].includes(k)),
      );
      created = { ...created, ...picked };
    }
    if (statusNow === "failed") {
      return fail(
        "CrowdCode could not settle the creation payment, so the agent was removed. You were not charged.",
        { agent_id: agentId },
      );
    }
    created.status = statusNow;
    return ok({
      paid_usd: price,
      payment_tx: tx,
      ...created,
      next:
        statusNow === "pending"
          ? "Payment is still settling; check with crowdcode_agents in a minute."
          : "Fund it with crowdcode_fund_agent, then give it work with crowdcode_run_task.",
    });
  }

  private async openApproval(url: string): Promise<boolean> {
    const env = this.options.env ?? process.env;
    if (truthy(env.CROWDCODE_NO_BROWSER)) return false;
    if (!url.startsWith(`${this.baseUrl()}/`)) return false;
    try {
      await (this.options.openBrowser ?? openInBrowser)(url);
      return true;
    } catch {
      return false;
    }
  }

  async fundAgent(args: Args): Promise<ToolResult> {
    const agentId = checkId(args, "agent_id");
    const value = amount(args, "amount_usd");
    const units = usdToUnits(value);
    if (value < MIN_MOVE_USD - 1e-9) {
      throw new ToolError(
        `the minimum relayed deposit is ${money(MIN_MOVE_USD)}; smaller amounts can be sent as a ` +
          "plain USDC transfer to the agent's Base address",
      );
    }
    const api = await this.api();
    if (api instanceof AccountClient) {
      // The main wallet is the owner's alone: CrowdCode answers with the page where they approve it.
      try {
        await api.signed("POST", `/agents/${agentId}/deposits`, { amount_usd: pyFloat(value) });
      } catch (err) {
        const detail = err instanceof ApiError && err.detail && typeof err.detail === "object" ? (err.detail as Record<string, any>) : {};
        if (err instanceof ApiError && err.error === "browser_approval_required" && detail.approve_url) {
          const url = String(detail.approve_url);
          // The owner approves on a page with just this request (amount filled in): open it for them,
          // but only a page of the CrowdCode origin this machine signed in to.
          const opened = await this.openApproval(url);
          return ok({
            agent_id: agentId,
            amount_usd: value,
            moved: false,
            approve_url: url,
            opened,
            next: opened
              ? `Opened the approval page in the browser (${url}). Approve ${money(value)} there; nothing moves until you do.`
              : `Open ${url} and approve ${money(value)} from your main wallet. Nothing moves until you do.`,
          });
        }
        throw err;
      }
      return fail("CrowdCode did not return an approval link; nothing was sent.");
    }
    const agent = await api.signed("GET", `/agents/${agentId}`);
    const to = agent?.addresses?.base;
    if (!to) return fail("CrowdCode did not return a Base address for this agent; nothing was sent.");
    const { authorization, signature } = await api.wallet!.authorizeTransfer({ to, value: units, validSeconds: 300 });
    const result = await this.moneyCall(() =>
      api.signed("POST", `/agents/${agentId}/deposits`, { authorization, signature }),
    );
    const txHash = result?.tx_hash ?? null;
    return ok({
      agent_id: agentId,
      amount_usd: value,
      to,
      tx_hash: txHash,
      explorer: txHash ? `https://basescan.org/tx/${txHash}` : null,
    });
  }

  async runTask(args: Args): Promise<ToolResult> {
    const agentId = checkId(args, "agent_id");
    const prompt = truthy(args.prompt) ? String(args.prompt).trim() : "";
    if (!prompt) throw new ToolError("prompt is required");
    if ([...prompt].length > 20000) throw new ToolError("prompt must be at most 20000 characters");
    const budget = amount(args, "budget_usd");
    if (!(budget > 0)) throw new ToolError("budget_usd is required and must be greater than zero");
    const body: Record<string, unknown> = { prompt, budget_usd: pyFloat(budget) };
    if (truthy(args.model)) body.model = String(args.model);
    const api = await this.api();
    // The same request within ten minutes reuses its key, so a retried call cannot start a second task.
    const key = createHash("sha256")
      .update(pyDumps([agentId, body, Math.floor(nowSeconds() / 600)], { sortKeys: true }))
      .digest("hex");
    const started = await this.moneyCall(() =>
      api.signed("POST", `/agents/${agentId}/tasks`, body, { "Idempotency-Key": key }),
    );
    const taskId = String(started?.task_id ?? "");
    const record: TaskRecord = {
      agent_id: agentId,
      task_id: taskId,
      prompt: [...prompt].slice(0, 120).join(""),
      budget_usd: budget,
      status: "queued",
      at: nowSeconds(),
    };
    await this.remember(record);
    if (args.wait === false) {
      return ok({
        task_id: taskId,
        agent_id: agentId,
        status: "queued",
        answer: null,
        spent_usd: null,
        budget_usd: budget,
        purchases: [],
        files: [],
        next: "Check it with crowdcode_task_status.",
      });
    }
    const timeout = amount(args, "timeout_seconds", 600);
    return this.poll(api, record, Date.now() + Math.max(10, Math.min(timeout, 1800)) * 1000);
  }

  private async remember(record: TaskRecord): Promise<void> {
    try {
      await rememberTask(this.home, record);
    } catch {
      // the dashboard's history is a convenience; never fail a task over it
    }
  }

  private async poll(api: OwnerApi, record: TaskRecord, deadline: number): Promise<ToolResult> {
    let delay = this.options.taskPollMs ?? 2_000;
    let task: Record<string, any>;
    for (;;) {
      task = await api.signed("GET", `/agents/${record.agent_id}/tasks/${record.task_id}`);
      if (TERMINAL.includes(task.status) || Date.now() >= deadline) break;
      await sleep(Math.min(delay, Math.max(0, deadline - Date.now())));
      delay = Math.min(delay * 1.5, 10_000);
    }
    return this.report(record, task);
  }

  private async report(record: TaskRecord, task: Record<string, any>): Promise<ToolResult> {
    const status = String(task.status ?? "unknown");
    record.status = status;
    if (task.spent_usd !== undefined && task.spent_usd !== null) record.spent_usd = task.spent_usd;
    await this.remember(record);
    const out: Record<string, unknown> = {
      task_id: record.task_id,
      agent_id: record.agent_id,
      status,
      answer: task.answer ?? null,
      spent_usd: task.spent_usd ?? null,
      budget_usd: record.budget_usd ?? null,
      purchases: task.purchases || [],
      files: ((task.artifacts || []) as FileEntry[]).map(fileSummary),
    };
    if (task.waiting_for) out.waiting_for = task.waiting_for;
    if ((out.files as unknown[]).length) {
      out.files_note = 'Nothing was downloaded. Save them with crowdcode_download (file ids or "all").';
    }
    if (!TERMINAL.includes(status)) out.next = "Still running; check again with crowdcode_task_status.";
    return ok(out);
  }

  async taskStatus(args: Args): Promise<ToolResult> {
    const agentId = checkId(args, "agent_id");
    const taskId = checkId(args, "task_id");
    const api = await this.api();
    if (truthy(args.cancel)) await api.signed("POST", `/agents/${agentId}/tasks/${taskId}/cancel`);
    const task = await api.signed("GET", `/agents/${agentId}/tasks/${taskId}`);
    const known = (await recentTasks(this.home).catch(() => [])).find((t) => t?.task_id === taskId);
    return this.report(known ?? { agent_id: agentId, task_id: taskId, at: nowSeconds() }, task);
  }

  async services(args: Args): Promise<ToolResult> {
    const agentId = checkId(args, "agent_id");
    const action = truthy(args.action) ? String(args.action) : "list";
    const api = await this.api();
    if (action === "stop" || action === "start") {
      const serviceId = checkId(args, "service_id");
      await api.signed("POST", `/agents/${agentId}/services/${serviceId}/${action}`);
    } else if (action !== "list") {
      throw new ToolError("action must be list, stop or start");
    }
    let services = await api.signed("GET", `/agents/${agentId}/services`);
    if (services && !Array.isArray(services)) services = services.services ?? [];
    const rows = (Array.isArray(services) ? services : []) as Record<string, any>[];
    const earned = rows.reduce((sum, s) => sum + (Number(s.earned_usd) || 0), 0);
    return ok({ agent_id: agentId, services: rows, total_earned_usd: Math.round(earned * 1e6) / 1e6 });
  }

  private moveAmount(args: Args): number {
    const value = amount(args, "amount_usd");
    usdToUnits(value);
    if (value < MIN_MOVE_USD - 1e-9) {
      throw new ToolError(`the minimum withdrawal or transfer is ${money(MIN_MOVE_USD)}`);
    }
    return value;
  }

  async withdraw(args: Args): Promise<ToolResult> {
    const agentId = checkId(args, "agent_id");
    const network = truthy(args.network) ? String(args.network) : "base";
    if (!(WITHDRAW_NETWORKS as readonly string[]).includes(network)) {
      throw new ToolError("withdrawals go to your wallet's EVM address: network must be base or tempo");
    }
    const value = this.moveAmount(args);
    const api = await this.api();
    const result = await this.moneyCall(() =>
      api.signed("POST", `/agents/${agentId}/withdraw`, { amount_usd: pyFloat(value), network }),
    );
    return ok({
      agent_id: agentId,
      amount_usd: value,
      network,
      to: result?.to || (api.wallet ? api.wallet.address : null),
      tx_hash: result?.tx_hash ?? null,
    });
  }

  async transfer(args: Args): Promise<ToolResult> {
    const source = checkId(args, "from_agent_id");
    const target = checkId(args, "to_agent_id");
    const network = truthy(args.network) ? String(args.network) : "base";
    if (!(NETWORKS as readonly string[]).includes(network)) {
      throw new ToolError(`network must be one of ${NETWORKS.join(", ")}`);
    }
    const value = this.moveAmount(args);
    const api = await this.api();
    const result = await this.moneyCall(() =>
      api.signed("POST", `/agents/${source}/transfer`, {
        to_agent_id: target,
        amount_usd: pyFloat(value),
        network,
      }),
    );
    return ok({ from_agent_id: source, to_agent_id: target, amount_usd: value, network, ...(result ?? {}) });
  }

  async demand(args: Args): Promise<ToolResult> {
    const data = await this.publicApi().demand();
    const proposals = (data && !Array.isArray(data) && Array.isArray(data.proposals) ? data.proposals : []) as Record<
      string,
      any
    >[];
    const query = truthy(args.query) ? String(args.query).toLowerCase().trim() : "";
    const rows: Record<string, any>[] = [];
    for (const p of proposals) {
      const text = ["title", "problem", "input", "output"].map((k) => String(p[k] ?? "")).join(" ").toLowerCase();
      if (query && !text.includes(query)) continue;
      const demand = (p.demand ?? {}) as Record<string, any>;
      rows.push({
        title: p.title ?? null,
        problem: p.problem ?? null,
        input: p.input ?? null,
        output: p.output ?? null,
        median_price_usd: p.price?.median ?? null,
        agents_asking: demand.requesters ?? null,
        would_pay: p.would_pay ?? null,
        last_requested_at: demand.last_requested_at ?? null,
      });
    }
    const rank = (r: Record<string, any>): [number, number] => [Number(r.agents_asking) || 0, Number(r.median_price_usd) || 0];
    rows.sort((a, b) => {
      const [a1, a2] = rank(a);
      const [b1, b2] = rank(b);
      return b1 - a1 || b2 - a2;
    });
    const limit = Math.max(1, Math.min(Math.trunc(amount(args, "limit", 10)), 50));
    return ok({
      count: rows.length,
      proposals: rows.slice(0, limit),
      tip:
        "Each one is a service other agents asked for. To have an agent build one, ask a CrowdCode agent to " +
        "with crowdcode_run_task.",
    });
  }

  async files(args: Args): Promise<ToolResult> {
    const agentId = await this.resolveAgent(checkId(args, "agent_id"));
    const taskId = truthy(args.task_id) ? checkId(args, "task_id") : "";
    const listed = await listFiles(await this.api(), agentId, taskId);
    const total = listed.reduce((sum, f) => sum + (Math.trunc(Number(f.size_bytes)) || 0), 0);
    return ok({
      agent_id: agentId,
      count: listed.length,
      total_bytes: total,
      files: listed.map(fileSummary),
      next: listed.length ? "Save some or all of them with crowdcode_download." : null,
    });
  }

  async download(args: Args): Promise<ToolResult> {
    const agentId = await this.resolveAgent(checkId(args, "agent_id"));
    const taskId = truthy(args.task_id) ? checkId(args, "task_id") : "";
    const wanted = words(args.file_ids);
    if (!wanted.length) throw new ToolError('file_ids is required: a list of file ids, or "all"');
    const api = await this.api();
    const listed = await listFiles(api, agentId, taskId);
    let chosen: FileEntry[];
    if (wanted.length === 1 && wanted[0]!.toLowerCase() === "all") {
      chosen = listed;
    } else {
      const byId = new Map(listed.map((f) => [String(f.id), f]));
      for (const w of wanted) {
        if (!ID_RE.test(w)) throw new ToolError(`${JSON.stringify(w)} is not a valid file id`);
      }
      const missing = wanted.filter((w) => !byId.has(w));
      if (missing.length) {
        return fail(`No such file for this agent: ${missing.join(", ")}. List them with crowdcode_files.`);
      }
      chosen = wanted.map((w) => byId.get(w)!);
    }
    if (!chosen.length) {
      return ok({
        agent_id: agentId,
        directory: null,
        downloaded: [],
        failed: [],
        total_bytes: 0,
        note: "This agent has no files yet.",
      });
    }
    const name = (await api.signed("GET", `/agents/${agentId}`))?.name || agentId;
    const target = planDest(truthy(args.dest) ? String(args.dest) : "", String(name), this.cwd());
    await mkdir(target, { recursive: true });
    const directory = await realpath(target);
    const maxBytes = this.maxDownloadBytes();
    const saved: { size_bytes: number }[] = [];
    const failed: Record<string, unknown>[] = [];
    for (const f of chosen) {
      try {
        saved.push(await downloadFile(api, agentId, f, directory, maxBytes));
      } catch (err) {
        const message = err instanceof ApiError || err instanceof ToolError ? err.message : describeError(err);
        failed.push({ id: f.id ?? null, name: f.name ?? null, error: message });
      }
    }
    return ok({
      agent_id: agentId,
      directory,
      downloaded: saved,
      failed,
      total_bytes: saved.reduce((sum, s) => sum + s.size_bytes, 0),
    });
  }

  /** Ask to move this wallet's agents into a CrowdCode account; the account's owner approves in the browser. */
  async claim(args: Args): Promise<ToolResult> {
    const api = await this.walletApi();
    const refs = words(args.agents);
    const body: Record<string, unknown> = {};
    if (refs.length) {
      const mine = ((await api.signed("GET", "/me")).agents ?? []) as Record<string, any>[];
      body.agent_ids = refs.map((r) => matchAgent(mine, r));
    }
    const made = await api.signed("POST", "/claims", body);
    return ok({
      approve_url: made.approve_url ?? null,
      expires_at: made.expires_at ?? null,
      agents: made.agents || [],
      next:
        "Open the link while signed in to your CrowdCode account and approve. It works once and expires " +
        "in 15 minutes. Withdrawals from these agents will then go only to that account's main wallet. " +
        `Afterwards run \`${LOGIN_COMMAND}\` so this machine keeps controlling them through the account.`,
    });
  }

  /** An agent id from an id, an id prefix or a name, among the agents of the current credential. */
  async resolveAgent(ref: string): Promise<string> {
    const wanted = (ref ?? "").trim();
    if (!wanted) throw new ToolError("name an agent (its name or id)");
    const me = await (await this.api()).signed("GET", "/me");
    return matchAgent((me.agents ?? []) as Record<string, any>[], wanted);
  }
}

export function matchAgent(agents: Record<string, any>[], ref: string): string {
  const tests: ((a: Record<string, any>) => boolean)[] = [
    (a) => a.id === ref,
    (a) => String(a.name ?? "").toLowerCase() === ref.toLowerCase(),
    (a) => String(a.id ?? "").startsWith(ref),
  ];
  for (const test of tests) {
    const found = agents.filter(test);
    if (found.length === 1) return String(found[0]!.id);
    if (found.length > 1) throw new ToolError(`'${ref}' matches more than one agent; use its id`);
  }
  throw new ToolError(`no agent of yours is called '${ref}'`);
}

export function createAgentTools(options: AgentToolsOptions = {}): AgentTools {
  return new AgentTools(options);
}
