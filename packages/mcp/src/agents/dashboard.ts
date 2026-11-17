/**
 * The CrowdCode screen: `npx crowdcode-mcp <subcommand>` in a terminal and the
 * `crowdcode` MCP prompt (a slash command in Codex). Ported from the Hermes
 * plugin's dashboard.py, with the same wording; the `/crowdcode` usage lines
 * read `npx crowdcode-mcp` here, and Hermes's spending-limit lines are gone
 * because this host has no local spend guard.
 *
 * Read-only apart from `wallet init` (creates a key only when there is none),
 * `download` (writes files where asked), `login`/`logout`/`mode` (the local
 * sign-in and setting) and `claim` (asks for a link; moves nothing).
 */

import { join } from "node:path";
import {
  type BrowserOpener,
  login as oauthLogin,
  logout as oauthLogout,
  modeSetting,
  saveModeSetting,
} from "./account.js";
import { ApiError } from "./client.js";
import { LOGIN_COMMAND, NotSignedIn, ToolError, WalletMissing } from "./errors.js";
import { CREDENTIALS_FILE } from "./home.js";
import type { AgentTools, ToolResult } from "./tools.js";

export const CLI = "npx crowdcode-mcp";

export const USAGE = `CrowdCode: cloud agents with their own wallets.

  ${CLI} dashboard                 wallet or account, agents, balances, recent tasks, services
  ${CLI} login                     use your CrowdCode account (sign in once in the browser; no wallet key needed)
  ${CLI} logout                    disconnect this machine from your CrowdCode account
  ${CLI} claim [agent…]            move this wallet's agents into your CrowdCode account
  ${CLI} mode [auto|account|wallet]  which credential is in use: account or wallet (or set it)
  ${CLI} wallet                    address, USDC balance and how to fund it
  ${CLI} wallet init               create the wallet (~/.agentcash/wallet.json, the key CrowdCode reviews sign with)
  ${CLI} agents                    agents and their balances
  ${CLI} tasks                     recent tasks started from here
  ${CLI} services <agent>          an agent's paid services and earnings
  ${CLI} files <agent> [--task ID]                         files the agent produced (nothing is downloaded)
  ${CLI} download <agent> <file-id…|all> [--task ID] [--dest DIR]   save them to ./crowdcode-files/<agent>/
  ${CLI} demand [keyword]          services other agents are asking for

Ask Codex or Claude Code in plain words for the rest, e.g. "create a CrowdCode agent called scout and fund it with $1".`;

export const NO_WALLET = `No CrowdCode wallet yet.

  ${CLI} wallet init   creates one (~/.agentcash/wallet.json, shared with AgentCash and CrowdCode reviews).
  ${CLI} login         or use your CrowdCode account instead.

Its key stays in that file (mode 600) and is never printed.`;

export const SUBCOMMANDS = [
  "dashboard",
  "wallet",
  "agents",
  "tasks",
  "services",
  "files",
  "download",
  "demand",
  "login",
  "logout",
  "mode",
  "claim",
] as const;

function usd(value: unknown): string {
  const n = typeof value === "number" ? value : typeof value === "string" && value.trim() ? Number(value) : NaN;
  if (!Number.isFinite(n)) return "-";
  const [whole, cents] = Math.abs(n).toFixed(2).split(".") as [string, string];
  return `$${n < 0 ? "-" : ""}${whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",")}.${cents}`;
}

function num(value: unknown): number {
  const n = Number(value || 0);
  return Number.isFinite(n) ? n : 0;
}

function size(value: unknown): string {
  let n = num(value);
  for (const unit of ["B", "KB", "MB", "GB"]) {
    if (n < 1000 || unit === "GB") return unit === "B" ? `${n.toFixed(0)} B` : `${n.toFixed(1)} ${unit}`;
    n /= 1000;
  }
  return `${n.toFixed(1)} GB`;
}

function ago(ts: unknown): string {
  const seconds = Math.max(0, Math.trunc(Date.now() / 1000 - num(ts)));
  for (const [unit, span] of [["d", 86400], ["h", 3600], ["m", 60]] as const) {
    if (seconds >= span) return `${Math.floor(seconds / span)}${unit} ago`;
  }
  return `${seconds}s ago`;
}

const ljust = (value: unknown, width: number) => String(value).padEnd(width);
const rjust = (value: unknown, width: number) => String(value).padStart(width);

/** Splits `--task ID`, `--task=ID`, `--dest DIR` out of the words. */
function flags(words: string[], names: string[]): { rest: string[]; values: Record<string, string> } {
  const rest: string[] = [];
  const values: Record<string, string> = {};
  for (let i = 0; i < words.length; i++) {
    const word = words[i]!;
    const name = names.find((n) => word === `--${n}` || word.startsWith(`--${n}=`));
    if (!name) {
      rest.push(word);
      continue;
    }
    if (word.includes("=")) values[name] = word.slice(word.indexOf("=") + 1);
    else values[name] = words[++i] ?? "";
  }
  return { rest, values };
}

class Failure extends Error {}

export interface DashboardOptions {
  /** Run the browser sign-in for `login` (the terminal). The MCP prompt only explains how. */
  interactive?: boolean;
  openBrowser?: BrowserOpener;
  log?: (line: string) => void;
}

export class Dashboard {
  constructor(
    private readonly tools: AgentTools,
    private readonly options: DashboardOptions = {},
  ) {}

  private get handlers() {
    return this.tools.handlers();
  }

  /** The text for `args` (words, or one string), and whether it worked. */
  async run(args: string[] | string = []): Promise<{ text: string; ok: boolean }> {
    const words = (typeof args === "string" ? args.split(/\s+/) : args).filter(Boolean);
    try {
      return { text: await this.dispatch(words), ok: true };
    } catch (err) {
      return { text: await this.explain(err), ok: false };
    }
  }

  private async explain(err: unknown): Promise<string> {
    if (err instanceof Failure) return err.message;
    if (err instanceof WalletMissing) return NO_WALLET + (err.message.includes("No CrowdCode wallet yet") ? "" : `\n\n(${err.message})`);
    if (err instanceof NotSignedIn) return `Not signed in to a CrowdCode account: ${err.message}\n\nSign in from a terminal: ${LOGIN_COMMAND}`;
    if (err instanceof ApiError) return `CrowdCode API error: ${err.status} ${err.error}`;
    if (err instanceof ToolError) return `CrowdCode: ${err.message}`;
    return `CrowdCode: ${err instanceof Error ? `${err.name}: ${err.message}` : String(err)}`;
  }

  private async dispatch(words: string[]): Promise<string> {
    if (!words.length) return this.overview();
    const head = words[0]!.toLowerCase();
    const rest = words.slice(1);
    switch (head) {
      case "dashboard":
      case "status":
        // `dashboard <subcommand …>` runs the subcommand, so one entry point (the Claude Code
        // /crowdcode command, `npx crowdcode-mcp dashboard $ARGUMENTS`) covers every screen.
        return rest.length && head === "dashboard" ? this.dispatch(rest) : this.overview();
      case "help":
      case "-h":
      case "--help":
        return USAGE;
      case "wallet":
        return this.wallet(Boolean(rest[0] && rest[0].toLowerCase() === "init"));
      case "login":
        return this.login();
      case "logout":
        return this.logout();
      case "claim":
        return this.claim(rest);
      case "mode":
        return this.modeText(rest[0]);
      case "agents":
        return this.agentsText(await this.agents());
      case "tasks":
        return this.tasksText();
      case "services":
        if (!rest.length) throw new Failure(`Usage: ${CLI} services <agent>`);
        return this.servicesText(rest[0]!);
      case "files": {
        const { rest: positional, values } = flags(rest, ["task"]);
        if (!positional.length) throw new Failure(`Usage: ${CLI} files <agent> [--task ID]`);
        return this.filesText(positional[0]!, values.task ?? positional[1] ?? "");
      }
      case "download": {
        const { rest: positional, values } = flags(rest, ["task", "dest"]);
        if (positional.length < 2) {
          throw new Failure(`Usage: ${CLI} download <agent> <file-id…|all> [--task ID] [--dest DIR]`);
        }
        return this.downloadText(positional[0]!, positional.slice(1), values.task ?? "", values.dest ?? "");
      }
      case "demand":
        return this.demandText(rest.join(" "));
      default:
        throw new Failure(`Unknown subcommand '${head}'.\n\n${USAGE}`);
    }
  }

  private check(result: ToolResult): ToolResult {
    if (!result.ok) {
      throw new Failure(`CrowdCode: ${result.error}` + (result.hint ? `\n${result.hint}` : ""));
    }
    return result;
  }

  // sections ---------------------------------------------------------------------
  walletLines(data: ToolResult): string[] {
    if (data.mode === "account") {
      const c = (data.connection ?? {}) as Record<string, any>;
      const agents = c.agents;
      const main = String(data.main_wallet || "not set up yet").split(":").pop();
      return [
        `Account  signed in with crowdcode-mcp; main wallet ${main}`,
        `         allows ${((c.scopes ?? []) as string[]).join(" ")}; agents ${agents === "all" ? "all" : (agents ?? []).length}; ` +
          // A connection has no cap and lasts until disconnected unless one was asked for (null = none).
          (c.daily_cap_usd == null ? `no daily cap, ${usd(c.spent_today_usd)} spent today; `
            : `CrowdCode cap ${usd(c.daily_cap_usd)}/day, ${usd(c.spent_today_usd)} used; `) +
          (c.expires_at == null ? "until you disconnect it" : `until ${String(c.expires_at).slice(0, 10)}`),
      ];
    }
    const balance = typeof data.usdc_base === "number" ? usd(data.usdc_base) : data.usdc_base;
    return [`Wallet   ${data.address}`, `         ${balance} USDC on Base`];
  }

  async wallet(init = false): Promise<string> {
    if (init && (await this.tools.walletExists())) {
      const data = await this.handlers.crowdcode_wallet({});
      const lines = data.ok ? this.walletLines(data) : [`CrowdCode: ${data.error}`];
      return ["A CrowdCode wallet already exists (not replaced).", "", ...lines].join("\n");
    }
    const data = await this.handlers.crowdcode_wallet({ init });
    if (!data.ok) await this.walletFailure(data);
    const head = data.created ? ["Created your CrowdCode wallet.", ""] : [];
    return [...head, ...this.walletLines(data), "", "Fund it: " + data.fund].join("\n");
  }

  private async walletFailure(data: ToolResult): Promise<never> {
    if ((await this.tools.mode().catch(() => "wallet")) === "wallet" && !(await this.tools.walletExists())) {
      throw new WalletMissing(String(data.error));
    }
    this.check(data);
    throw new Failure(String(data.error));
  }

  // account ----------------------------------------------------------------------
  async login(): Promise<string> {
    if (!this.options.interactive) {
      const signedIn = await this.signedInLines();
      return [
        ...(signedIn ? ["Signed in to your CrowdCode account.", "", ...signedIn, ""] : []),
        "Sign this machine in to your CrowdCode account from a terminal (not this chat):",
        "",
        `  ${LOGIN_COMMAND}`,
        "",
        "It opens crowdcode.app/connect. Sign in there with your email, choose which agents crowdcode-mcp may use,",
        "a daily cap and an expiry, and approve. The sign-in is kept in ~/.crowdcode/credentials.json (mode 600).",
        "Then run it again here: your account's agents are available to Codex and Claude Code, no wallet key needed.",
      ].join("\n");
    }
    const baseUrl = this.tools.baseUrl();
    await oauthLogin({
      baseUrl,
      home: this.tools.home,
      openBrowser: this.options.openBrowser,
      log: this.options.log,
    });
    const lines = (await this.signedInLines()) ?? [];
    const { setting } = await modeSetting(this.tools.env, this.tools.home);
    return [
      "Signed in to your CrowdCode account.",
      "",
      ...lines,
      ...(setting === "wallet"
        ? ["", `The mode setting is "wallet", so this machine still uses its wallet. Switch: ${CLI} mode auto`]
        : []),
    ].join("\n");
  }

  private async signedInLines(): Promise<string[] | null> {
    try {
      if ((await this.tools.mode()) !== "account") return null;
      const data = await this.handlers.crowdcode_wallet({});
      return data.ok && data.mode === "account" ? this.walletLines(data) : null;
    } catch {
      return null;
    }
  }

  async logout(): Promise<string> {
    const { signedIn, revoked } = await oauthLogout(this.tools.home);
    if (!signedIn) return "This machine was not signed in to a CrowdCode account.";
    return revoked
      ? "Disconnected this machine from your CrowdCode account; its tokens stopped working and the local sign-in was removed."
      : `Removed the local sign-in (${join(this.tools.home, CREDENTIALS_FILE)}). CrowdCode did not confirm the revoke; ` +
          "you can also disconnect crowdcode-mcp under connected apps on crowdcode.app.";
  }

  async claim(refs: string[]): Promise<string> {
    const data = this.check(await this.handlers.crowdcode_claim({ agents: refs }));
    const names = ((data.agents ?? []) as Record<string, any>[]).map((a) => a.name ?? a.id ?? "?").join(", ");
    return [
      `Claim ${names} into your CrowdCode account:`,
      "",
      `  ${data.approve_url}`,
      "",
      "Open it while signed in to crowdcode.app and approve. It works once and expires in 15 minutes.",
      "After that the agents belong to your account and withdraw only to its main wallet.",
      `Then run ${LOGIN_COMMAND} so this machine keeps controlling them through the account.`,
    ].join("\n");
  }

  async modeText(set?: string): Promise<string> {
    let saved = "";
    if (set) {
      const mode = await saveModeSetting(this.tools.home, set);
      saved = `Saved mode ${mode} in ${join(this.tools.home, "config.json")}.\n`;
    }
    const { setting, from } = await modeSetting(this.tools.env, this.tools.home);
    const source = from === "CROWDCODE_MODE" ? " (from CROWDCODE_MODE, which overrides the saved setting)" : "";
    const mode = await this.tools.mode();
    if (mode === "account") return `${saved}Using your CrowdCode account (crowdcode-mcp sign-in). mode setting: ${setting}${source}.`;
    return (
      `${saved}Using this machine's wallet. mode setting: ${setting}${source}.` +
      (setting === "auto" ? ` Sign in to an account with ${LOGIN_COMMAND}.` : "")
    );
  }

  // agents -----------------------------------------------------------------------
  private async agents(): Promise<Record<string, any>[]> {
    const data = await this.handlers.crowdcode_agents({});
    if (!data.ok) {
      if ((await this.tools.mode().catch(() => "wallet")) === "wallet" && !(await this.tools.walletExists())) {
        throw new WalletMissing(String(data.error));
      }
      this.check(data);
    }
    return (data.agents ?? []) as Record<string, any>[];
  }

  agentsText(agents: Record<string, any>[]): string {
    if (!agents.length) return 'Agents   none yet. Ask: "create a CrowdCode agent called scout".';
    const lines = ["Agents"];
    for (const a of agents) {
      const balances = (a.balances_usd ?? null) as Record<string, unknown> | null;
      const entries = balances ? Object.entries(balances) : [];
      const total = entries.length ? entries.reduce((sum, [, v]) => sum + num(v), 0) : null;
      const parts = entries.filter(([, v]) => num(v) > 0).map(([k, v]) => `${k} ${usd(v)}`).join("  ");
      const short = String(a.id ?? "?").slice(0, 8);
      lines.push(
        `  ${ljust(a.name ?? "?", 18)} ${ljust(short, 9)} ${ljust(a.status ?? "", 8)} ` +
          `${rjust(total !== null ? usd(total) : "-", 8)}  ${parts}`,
      );
    }
    return lines.join("\n");
  }

  async tasksText(limit = 5): Promise<string> {
    const tasks = (await this.tools.recentTasks()).slice(-limit);
    if (!tasks.length) return "Tasks    none started from this machine yet.";
    const lines = ["Recent tasks"];
    for (const t of [...tasks].reverse()) {
      const spent = `${usd(t.spent_usd)}/${usd(t.budget_usd)}`;
      lines.push(`  ${ljust(t.status ?? "?", 10)} ${ljust(spent, 13)} ${ljust(ago(t.at), 8)} ${String(t.prompt ?? "").slice(0, 60)}`);
    }
    return lines.join("\n");
  }

  async servicesText(agent: string): Promise<string> {
    const agentId = await this.tools.resolveAgent(agent);
    const data = this.check(await this.handlers.crowdcode_services({ agent_id: agentId }));
    const services = (data.services ?? []) as Record<string, any>[];
    if (!services.length) return `${agent} has no paid services yet.`;
    const lines = [`Services of ${agent}  (earned ${usd(data.total_earned_usd)})`];
    for (const s of services) {
      lines.push(
        `  ${ljust(s.slug ?? "?", 22)} ${ljust(s.status ?? "", 8)} ${usd(s.price_usd)}/call  ` +
          `${s.calls ?? 0} calls  earned ${usd(s.earned_usd)}\n    ${s.url ?? ""}`,
      );
    }
    return lines.join("\n");
  }

  async filesText(agent: string, taskId = ""): Promise<string> {
    const agentId = await this.tools.resolveAgent(agent);
    const data = this.check(await this.handlers.crowdcode_files({ agent_id: agentId, task_id: taskId }));
    const files = (data.files ?? []) as Record<string, any>[];
    if (!files.length) return `${agent} has no files yet.`;
    const lines = [`Files of ${agent}  (${files.length}, ${size(data.total_bytes)})`];
    for (const f of files) {
      lines.push(
        `  ${ljust(f.id ?? "?", 38)} ${rjust(size(f.size_bytes), 9)}  ${ljust(String(f.created_at ?? "").slice(0, 16), 16)}  ` +
          `${f.name ?? ""}`,
      );
    }
    lines.push(`\nSave them: ${CLI} download ${agent} <id…|all>`);
    return lines.join("\n");
  }

  async downloadText(agent: string, ids: string[], taskId = "", dest = ""): Promise<string> {
    const agentId = await this.tools.resolveAgent(agent);
    const data = this.check(
      await this.handlers.crowdcode_download({ agent_id: agentId, file_ids: ids, task_id: taskId, dest }),
    );
    const saved = (data.downloaded ?? []) as Record<string, any>[];
    const failed = (data.failed ?? []) as Record<string, any>[];
    if (!saved.length && !failed.length) return String(data.note ?? "Nothing to download.");
    const lines = [`Saved ${saved.length} file(s), ${size(data.total_bytes)}, to ${data.directory}`];
    lines.push(...saved.map((s) => `  ${rjust(size(s.size_bytes), 9)}  ${s.path}`));
    lines.push(...failed.map((f) => `  failed  ${f.name || f.id}: ${f.error}`));
    if (failed.length && !saved.length) throw new Failure(lines.join("\n"));
    return lines.join("\n");
  }

  async demandText(query = ""): Promise<string> {
    const data = this.check(await this.handlers.crowdcode_demand({ query, limit: 8 }));
    const lines = ["What agents are asking for (crowdcode.app/api/service-proposals)"];
    for (const p of (data.proposals ?? []) as Record<string, any>[]) {
      lines.push(`  ${ljust(p.title ?? "?", 34)} ${p.agents_asking || 0} agents  ~${usd(p.median_price_usd)}`);
    }
    return lines.join("\n");
  }

  async overview(): Promise<string> {
    const data = await this.handlers.crowdcode_wallet({});
    if (!data.ok) await this.walletFailure(data);
    const sections = [this.walletLines(data).join("\n")];
    let agents: Record<string, any>[] = [];
    try {
      agents = await this.agents();
      sections.push(this.agentsText(agents));
    } catch (err) {
      sections.push(`Agents   unavailable (${err instanceof Error ? err.message : String(err)})`);
    }
    sections.push(await this.tasksText());
    const earned: string[] = [];
    for (const a of agents.slice(0, 6)) {
      const s = await this.handlers.crowdcode_services({ agent_id: a.id });
      for (const svc of (s.ok ? s.services : []) as Record<string, any>[]) {
        earned.push(
          `  ${a.name ?? a.id}/${ljust(svc.slug ?? "?", 20)} ${ljust(svc.status ?? "", 8)} ` +
            `${svc.calls ?? 0} calls  earned ${usd(svc.earned_usd)}`,
        );
      }
    }
    sections.push(earned.length ? "Services\n" + earned.join("\n") : "Services none deployed yet.");
    return sections.join("\n\n");
  }
}
