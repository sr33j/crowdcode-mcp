import { mkdtemp, mkdir, readFile, readdir, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  CORE_CLIENTS,
  detectClients,
  doctorClients,
  getClientConfigPath,
  installForClients,
  mergeCodexConfig,
  mergeJsonMcpConfig,
  type InstallerEnvironment,
} from "../src/installer.js";

const PACKAGE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const FIXED_NOW = () => new Date("2026-07-31T12:34:56.000Z");

async function fixture(
  platform: "darwin" | "linux" | "win32" = "darwin",
): Promise<InstallerEnvironment & { homeDir: string }> {
  const homeDir = await mkdtemp(join(tmpdir(), "crowdcode-installer-"));
  return {
    homeDir,
    platform,
    packageRoot: PACKAGE_ROOT,
    now: FIXED_NOW,
    env:
      platform === "win32"
        ? { APPDATA: join(homeDir, "AppData", "Roaming") }
        : platform === "linux"
          ? { XDG_CONFIG_HOME: join(homeDir, ".config") }
          : {},
  };
}

describe("installer config merging", () => {
  it("preserves unrelated Codex TOML and replaces only CrowdCode", () => {
    const input = [
      "# keep this comment",
      'model = "gpt-5"',
      "",
      "[mcp_servers.other]",
      'command = "other"',
      "",
      '[mcp_servers."crowdcode"]',
      'command = "old"',
      'args = ["old"]',
      "",
      '[mcp_servers."crowdcode".env]',
      'STALE = "remove"',
      "",
      "[features]",
      "apps = true",
      "",
    ].join("\n");
    const merged = mergeCodexConfig(input);
    expect(merged).toContain("# keep this comment");
    expect(merged).toContain("[mcp_servers.other]");
    expect(merged).toContain("[features]");
    expect(merged).not.toContain('command = "old"');
    expect(merged).not.toContain("STALE");
    expect(merged).toContain("[mcp_servers.crowdcode]");
    expect(merged).toContain('args = ["-y","crowdcode-mcp@latest"]');
    expect(mergeCodexConfig(merged)).toBe(merged);
  });

  it("preserves unrelated JSON MCP servers and rejects malformed files", () => {
    const merged = JSON.parse(
      mergeJsonMcpConfig(
        JSON.stringify({ theme: "dark", mcpServers: { other: { command: "x" } } }),
      ),
    );
    expect(merged.theme).toBe("dark");
    expect(merged.mcpServers.other.command).toBe("x");
    expect(merged.mcpServers.crowdcode.args).toEqual([
      "-y",
      "crowdcode-mcp@latest",
    ]);
    expect(() => mergeJsonMcpConfig("{not json", "broken.json")).toThrow(
      /no changes were written/,
    );
  });
});

describe("cross-client installation", () => {
  it("installs all core clients, backs up unmanaged skills, and is idempotent", async () => {
    const env = await fixture("darwin");
    const oldSkillDir = join(env.homeDir, ".agents", "skills", "crowdcode");
    await mkdir(oldSkillDir, { recursive: true });
    await writeFile(join(oldSkillDir, "SKILL.md"), "user-authored skill\n");

    const cursorConfig = getClientConfigPath("cursor", env);
    await mkdir(join(env.homeDir, ".cursor"), { recursive: true });
    await writeFile(
      cursorConfig,
      JSON.stringify({ mcpServers: { existing: { command: "keep" } } }),
    );

    const results = await installForClients(CORE_CLIENTS, env);
    expect(results).toHaveLength(4);
    expect(results.find((item) => item.client === "claude-code")?.skillMode).toBe(
      "symlink",
    );
    expect(results.find((item) => item.client === "claude-desktop")?.skillMode).toBe(
      "mcp-instructions",
    );

    const skillEntries = await readdir(join(env.homeDir, ".agents", "skills"));
    expect(skillEntries).toContain("crowdcode");
    expect(skillEntries.some((entry) => entry.startsWith("crowdcode.backup-"))).toBe(
      true,
    );
    const skill = await readFile(join(oldSkillDir, "SKILL.md"), "utf8");
    expect(skill).toContain(
      "Mandatory reputation checks when purchasing third-party paid APIs",
    );

    const cursor = JSON.parse(await readFile(cursorConfig, "utf8"));
    expect(cursor.mcpServers.existing.command).toBe("keep");
    expect(cursor.mcpServers.crowdcode.command).toBe("npx");

    const doctor = await doctorClients(CORE_CLIENTS, env);
    expect(doctor.every((item) => item.mcpConfigured && item.skillCurrent)).toBe(
      true,
    );

    const beforeSecondInstall = await readdir(join(env.homeDir, ".agents", "skills"));
    await installForClients(CORE_CLIENTS, env);
    expect(await readdir(join(env.homeDir, ".agents", "skills"))).toEqual(
      beforeSecondInstall,
    );
  });

  it("uses a copy for Claude Code on Windows and platform-specific config paths", async () => {
    const env = await fixture("win32");
    const results = await installForClients(["claude-code", "claude-desktop"], env);
    expect(results[0]?.skillMode).toBe("copy");
    expect(results[1]?.configPath).toBe(
      join(env.homeDir, "AppData", "Roaming", "Claude", "claude_desktop_config.json"),
    );
    await expect(
      stat(join(env.homeDir, ".claude", "skills", "crowdcode", "SKILL.md")),
    ).resolves.toBeDefined();
  });

  it("detects supported clients from their standard directories", async () => {
    const env = await fixture("linux");
    await mkdir(join(env.homeDir, ".codex"), { recursive: true });
    await mkdir(join(env.homeDir, ".cursor"), { recursive: true });
    expect(await detectClients(env)).toEqual(["codex", "cursor"]);
  });

  it("does not truncate malformed existing config", async () => {
    const env = await fixture("darwin");
    const config = getClientConfigPath("cursor", env);
    await mkdir(join(env.homeDir, ".cursor"), { recursive: true });
    await writeFile(config, "{ malformed but valuable");
    await expect(installForClients(["cursor"], env)).rejects.toThrow(
      /no changes were written/,
    );
    expect(await readFile(config, "utf8")).toBe("{ malformed but valuable");
  });
});
