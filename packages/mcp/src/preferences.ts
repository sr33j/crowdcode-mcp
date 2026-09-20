import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { homedir } from "node:os";
import { randomUUID } from "node:crypto";

export function preferencesPath(): string {
  return process.env.CROWDCODE_CONFIG_PATH ?? join(homedir(), ".crowdcode", "preferences.json");
}

/** One instance per stdio connection. A persistent default never stores session state. */
export class Preferences {
  private override: boolean | undefined;
  constructor(private readonly path = preferencesPath()) {}

  async status() {
    let defaultEnabled = true;
    try {
      const data = JSON.parse(await readFile(this.path, "utf8"));
      if (typeof data.enabled !== "boolean") throw new Error("Invalid CrowdCode preferences");
      defaultEnabled = data.enabled;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    }
    return { enabled: this.override ?? defaultEnabled, default_enabled: defaultEnabled,
      scope: this.override === undefined ? "default" : "session",
      session_boundary: "this MCP connection" };
  }

  async set(enabled: boolean, scope: "session" | "default" = "session") {
    if (scope === "default") {
      await mkdir(dirname(this.path), { recursive: true });
      const temporary = `${this.path}.${randomUUID()}.tmp`;
      await writeFile(temporary, JSON.stringify({ enabled }) + "\n", { mode: 0o600 });
      await rename(temporary, this.path);
      this.override = undefined;
    } else {
      this.override = enabled;
    }
    return this.status();
  }
}
