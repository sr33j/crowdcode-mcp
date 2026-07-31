import { createHash } from "node:crypto";
import {
  copyFile,
  lstat,
  mkdir,
  readFile,
  readlink,
  rename,
  rm,
  stat,
  symlink,
  writeFile,
} from "node:fs/promises";
import { homedir } from "node:os";
import { basename, dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const CORE_CLIENTS = [
  "codex",
  "claude-code",
  "claude-desktop",
  "cursor",
] as const;

export type ClientId = (typeof CORE_CLIENTS)[number];
export type InstallerPlatform = "darwin" | "linux" | "win32";

export interface InstallerEnvironment {
  homeDir?: string;
  platform?: InstallerPlatform;
  env?: NodeJS.ProcessEnv;
  packageRoot?: string;
  now?: () => Date;
}

interface InstallState {
  packageVersion: string;
  skillHash: string;
  installedAt: string;
}

export interface ClientInstallResult {
  client: ClientId;
  configPath: string;
  skillPath: string;
  skillMode: "canonical" | "symlink" | "copy" | "mcp-instructions";
}

export interface DoctorResult {
  client: ClientId;
  configPath: string;
  mcpConfigured: boolean;
  skillPath: string;
  skillCurrent: boolean;
  detail?: string;
}

const MCP_SERVER = {
  command: "npx",
  args: ["-y", "crowdcode-mcp@latest"],
};

function resolvedEnvironment(input: InstallerEnvironment = {}) {
  const platform = input.platform ?? process.platform;
  if (platform !== "darwin" && platform !== "linux" && platform !== "win32") {
    throw new Error(`unsupported platform: ${platform}`);
  }
  return {
    homeDir: input.homeDir ?? homedir(),
    platform,
    env: input.env ?? process.env,
    packageRoot: input.packageRoot ?? findPackageRoot(import.meta.url),
    now: input.now ?? (() => new Date()),
  };
}

function findPackageRoot(moduleUrl: string): string {
  // installer.ts lives in src during development and is bundled into dist/cli.js
  // for publication. Both locations are exactly one directory below package.json.
  return resolve(dirname(fileURLToPath(moduleUrl)), "..");
}

async function exists(path: string): Promise<boolean> {
  try {
    await stat(path);
    return true;
  } catch {
    return false;
  }
}

async function lexists(path: string): Promise<boolean> {
  try {
    await lstat(path);
    return true;
  } catch {
    return false;
  }
}

async function readOptional(path: string): Promise<string | null> {
  try {
    return await readFile(path, "utf8");
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
    throw error;
  }
}

function hash(content: string): string {
  return createHash("sha256").update(content).digest("hex");
}

async function packageVersion(packageRoot: string): Promise<string> {
  const parsed = JSON.parse(await readFile(join(packageRoot, "package.json"), "utf8"));
  if (typeof parsed.version !== "string") {
    throw new Error("crowdcode-mcp package.json has no version");
  }
  return parsed.version;
}

async function bundledSkill(packageRoot: string): Promise<string> {
  const packaged = join(packageRoot, "dist", "skills", "crowdcode", "SKILL.md");
  const content = await readOptional(packaged);
  if (content !== null) return content;

  // Development fallback. Published packages always use the dist asset.
  const source = resolve(packageRoot, "../../skills/crowdcode/SKILL.md");
  return readFile(source, "utf8");
}

function statePath(homeDir: string): string {
  return join(homeDir, ".crowdcode", "install-state.json");
}

function canonicalSkillDir(homeDir: string): string {
  return join(homeDir, ".agents", "skills", "crowdcode");
}

async function loadState(homeDir: string): Promise<InstallState | null> {
  const raw = await readOptional(statePath(homeDir));
  if (raw === null) return null;
  try {
    const parsed = JSON.parse(raw) as Partial<InstallState>;
    if (
      typeof parsed.packageVersion === "string" &&
      typeof parsed.skillHash === "string" &&
      typeof parsed.installedAt === "string"
    ) {
      return parsed as InstallState;
    }
  } catch {
    // Treat malformed state as unmanaged; user content is backed up below.
  }
  return null;
}

async function atomicWrite(path: string, content: string): Promise<void> {
  await mkdir(dirname(path), { recursive: true });
  const temp = join(
    dirname(path),
    `.${basename(path)}.${process.pid}.${Math.random().toString(16).slice(2)}.tmp`,
  );
  await writeFile(temp, content, { mode: 0o600 });
  try {
    await rename(temp, path);
  } catch (error) {
    await rm(temp, { force: true });
    throw error;
  }
}

function backupSuffix(now: Date): string {
  return now.toISOString().replace(/[:.]/g, "-");
}

async function unusedBackupPath(path: string, now: Date): Promise<string> {
  const base = `${path}.backup-${backupSuffix(now)}`;
  let candidate = base;
  for (let index = 2; await lexists(candidate); index += 1) {
    candidate = `${base}-${index}`;
  }
  return candidate;
}

async function replaceSkillDirectory(
  path: string,
  skill: string,
  managedHash: string | null,
  now: Date,
): Promise<void> {
  let existingSkill: string | null;
  try {
    existingSkill = await readOptional(join(path, "SKILL.md"));
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOTDIR") existingSkill = null;
    else throw error;
  }
  const newHash = hash(skill);
  if (existingSkill !== null && hash(existingSkill) === newHash) return;

  if (await lexists(path)) {
    const existingIsManaged =
      existingSkill !== null && managedHash !== null && hash(existingSkill) === managedHash;
    if (existingIsManaged) {
      await rm(path, { recursive: true, force: true });
    } else {
      await rename(path, await unusedBackupPath(path, now));
    }
  }

  const temp = `${path}.installing-${process.pid}`;
  await rm(temp, { recursive: true, force: true });
  await mkdir(temp, { recursive: true });
  await writeFile(join(temp, "SKILL.md"), skill);
  await rename(temp, path);
}

async function installCanonicalSkill(env: ReturnType<typeof resolvedEnvironment>) {
  const skill = await bundledSkill(env.packageRoot);
  const previous = await loadState(env.homeDir);
  const path = canonicalSkillDir(env.homeDir);
  await mkdir(dirname(path), { recursive: true });
  await replaceSkillDirectory(path, skill, previous?.skillHash ?? null, env.now());

  const nextState: InstallState = {
    packageVersion: await packageVersion(env.packageRoot),
    skillHash: hash(skill),
    installedAt: env.now().toISOString(),
  };
  await atomicWrite(statePath(env.homeDir), `${JSON.stringify(nextState, null, 2)}\n`);
  return { path, skill, state: nextState };
}

function claudeCodeSkillDir(homeDir: string): string {
  return join(homeDir, ".claude", "skills", "crowdcode");
}

async function installClaudeCodeSkill(
  env: ReturnType<typeof resolvedEnvironment>,
  canonicalPath: string,
  skill: string,
): Promise<"symlink" | "copy"> {
  const target = claudeCodeSkillDir(env.homeDir);
  await mkdir(dirname(target), { recursive: true });

  let targetManaged = false;
  try {
    const targetStat = await lstat(target);
    if (targetStat.isSymbolicLink()) {
      const link = await readlink(target);
      targetManaged = resolve(dirname(target), link) === canonicalPath;
    } else {
      const current = await readOptional(join(target, "SKILL.md"));
      targetManaged = current !== null && hash(current) === hash(skill);
    }
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
  }

  if (await lexists(target)) {
    if (targetManaged) await rm(target, { recursive: true, force: true });
    else await rename(target, await unusedBackupPath(target, env.now()));
  }

  if (env.platform !== "win32") {
    try {
      await symlink(relative(dirname(target), canonicalPath), target, "dir");
      return "symlink";
    } catch {
      // Some filesystems disallow symlinks. Fall through to a portable copy.
    }
  }

  await mkdir(target, { recursive: true });
  await copyFile(join(canonicalPath, "SKILL.md"), join(target, "SKILL.md"));
  return "copy";
}

function configBase(
  homeDir: string,
  platform: InstallerPlatform,
  env: NodeJS.ProcessEnv,
): string {
  if (platform === "win32") {
    return env.APPDATA ?? join(homeDir, "AppData", "Roaming");
  }
  if (platform === "darwin") return join(homeDir, "Library", "Application Support");
  return env.XDG_CONFIG_HOME ?? join(homeDir, ".config");
}

export function getClientConfigPath(
  client: ClientId,
  input: InstallerEnvironment = {},
): string {
  const env = resolvedEnvironment(input);
  switch (client) {
    case "codex":
      return join(env.env.CODEX_HOME ?? join(env.homeDir, ".codex"), "config.toml");
    case "claude-code":
      return join(env.homeDir, ".claude.json");
    case "cursor":
      return join(env.homeDir, ".cursor", "mcp.json");
    case "claude-desktop":
      return join(
        configBase(env.homeDir, env.platform, env.env),
        "Claude",
        "claude_desktop_config.json",
      );
  }
}

function removeCrowdCodeTomlSection(content: string): string {
  const lines = content.split(/\r?\n/);
  const kept: string[] = [];
  let skipping = false;
  for (const line of lines) {
    const section = /^\s*\[([^\]]+)\]\s*(?:#.*)?$/.exec(line);
    if (section) {
      const normalized = section[1]!.replace(/["']/g, "").replace(/\s/g, "");
      skipping =
        normalized === "mcp_servers.crowdcode" ||
        normalized.startsWith("mcp_servers.crowdcode.");
      if (skipping) continue;
    }
    if (!skipping) kept.push(line);
  }
  return kept.join("\n").trimEnd();
}

export function mergeCodexConfig(content: string): string {
  const before = removeCrowdCodeTomlSection(content);
  const section = [
    "[mcp_servers.crowdcode]",
    `command = ${JSON.stringify(MCP_SERVER.command)}`,
    `args = ${JSON.stringify(MCP_SERVER.args)}`,
  ].join("\n");
  return `${before ? `${before}\n\n` : ""}${section}\n`;
}

function parseJsonConfig(content: string, path: string): Record<string, unknown> {
  try {
    const parsed = JSON.parse(content) as unknown;
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error("root must be an object");
    }
    return parsed as Record<string, unknown>;
  } catch (error) {
    throw new Error(`cannot parse ${path}; no changes were written: ${(error as Error).message}`);
  }
}

export function mergeJsonMcpConfig(content: string, path = "config.json"): string {
  const config = content.trim() ? parseJsonConfig(content, path) : {};
  const current = config.mcpServers;
  if (current !== undefined && (current === null || typeof current !== "object" || Array.isArray(current))) {
    throw new Error(`${path} has a non-object mcpServers value; no changes were written`);
  }
  config.mcpServers = {
    ...((current as Record<string, unknown> | undefined) ?? {}),
    crowdcode: MCP_SERVER,
  };
  return `${JSON.stringify(config, null, 2)}\n`;
}

async function configureClient(
  client: ClientId,
  env: ReturnType<typeof resolvedEnvironment>,
): Promise<string> {
  const path = getClientConfigPath(client, env);
  const current = (await readOptional(path)) ?? "";
  const next =
    client === "codex" ? mergeCodexConfig(current) : mergeJsonMcpConfig(current, path);
  if (next !== current) await atomicWrite(path, next);
  return path;
}

export async function detectClients(
  input: InstallerEnvironment = {},
): Promise<ClientId[]> {
  const env = resolvedEnvironment(input);
  const markers: Record<ClientId, string[]> = {
    codex: [env.env.CODEX_HOME ?? join(env.homeDir, ".codex")],
    "claude-code": [join(env.homeDir, ".claude"), join(env.homeDir, ".claude.json")],
    cursor: [join(env.homeDir, ".cursor")],
    "claude-desktop": [dirname(getClientConfigPath("claude-desktop", env))],
  };
  const detected: ClientId[] = [];
  for (const client of CORE_CLIENTS) {
    if ((await Promise.all(markers[client].map(exists))).some(Boolean)) detected.push(client);
  }
  return detected;
}

export async function installForClients(
  clients: readonly ClientId[],
  input: InstallerEnvironment = {},
): Promise<ClientInstallResult[]> {
  const env = resolvedEnvironment(input);
  const canonical = await installCanonicalSkill(env);
  let claudeMode: "symlink" | "copy" | null = null;
  if (clients.includes("claude-code")) {
    claudeMode = await installClaudeCodeSkill(env, canonical.path, canonical.skill);
  }

  const results: ClientInstallResult[] = [];
  for (const client of clients) {
    const configPath = await configureClient(client, env);
    if (client === "claude-code") {
      results.push({
        client,
        configPath,
        skillPath: claudeCodeSkillDir(env.homeDir),
        skillMode: claudeMode!,
      });
    } else if (client === "claude-desktop") {
      results.push({
        client,
        configPath,
        skillPath: "MCP initialize instructions",
        skillMode: "mcp-instructions",
      });
    } else {
      results.push({
        client,
        configPath,
        skillPath: canonical.path,
        skillMode: "canonical",
      });
    }
  }
  return results;
}

function jsonHasCrowdCode(content: string): boolean {
  try {
    const parsed = JSON.parse(content) as {
      mcpServers?: Record<string, { command?: string; args?: string[] }>;
    };
    const server = parsed.mcpServers?.crowdcode;
    return server?.command === MCP_SERVER.command &&
      JSON.stringify(server.args) === JSON.stringify(MCP_SERVER.args);
  } catch {
    return false;
  }
}

function codexHasCrowdCode(content: string): boolean {
  const merged = removeCrowdCodeTomlSection(content);
  return merged !== content.trimEnd() &&
    /\[mcp_servers\.(?:["']?crowdcode["']?)\]/.test(content) &&
    /crowdcode-mcp@latest/.test(content);
}

export async function doctorClients(
  clients: readonly ClientId[] = CORE_CLIENTS,
  input: InstallerEnvironment = {},
): Promise<DoctorResult[]> {
  const env = resolvedEnvironment(input);
  const expectedSkill = await bundledSkill(env.packageRoot);
  const canonicalPath = canonicalSkillDir(env.homeDir);
  const canonicalCurrent =
    (await readOptional(join(canonicalPath, "SKILL.md"))) === expectedSkill;
  const results: DoctorResult[] = [];

  for (const client of clients) {
    const configPath = getClientConfigPath(client, env);
    const config = (await readOptional(configPath)) ?? "";
    const mcpConfigured =
      client === "codex" ? codexHasCrowdCode(config) : jsonHasCrowdCode(config);
    if (client === "claude-code") {
      const skillPath = claudeCodeSkillDir(env.homeDir);
      const skillCurrent =
        (await readOptional(join(skillPath, "SKILL.md"))) === expectedSkill;
      results.push({ client, configPath, mcpConfigured, skillPath, skillCurrent });
    } else if (client === "claude-desktop") {
      results.push({
        client,
        configPath,
        mcpConfigured,
        skillPath: "MCP initialize instructions",
        skillCurrent: mcpConfigured,
        detail: "Claude Desktop uses CrowdCode's MCP initialization instructions.",
      });
    } else {
      results.push({
        client,
        configPath,
        mcpConfigured,
        skillPath: canonicalPath,
        skillCurrent: canonicalCurrent,
      });
    }
  }
  return results;
}
