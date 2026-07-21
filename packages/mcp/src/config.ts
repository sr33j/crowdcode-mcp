import { homedir } from "node:os";
import { join } from "node:path";

export interface Config {
  backendUrl: string;
  cacheDir: string;
  disableModel: boolean;
  upstreamTimeoutMs: number;
}

const TRUTHY = new Set(["1", "true", "yes"]);

export function getConfig(env: NodeJS.ProcessEnv = process.env): Config {
  return {
    backendUrl:
      env.CROWDCODE_BACKEND_URL ?? "https://crowdcode-backend.onrender.com/mcp",
    cacheDir: env.CROWDCODE_CACHE_DIR ?? join(homedir(), ".cache", "crowdcode-mcp"),
    disableModel: TRUTHY.has((env.CROWDCODE_DISABLE_MODEL ?? "").toLowerCase()),
    // Generous default: the free-tier Render backend cold-starts slowly.
    upstreamTimeoutMs: Number(env.CROWDCODE_UPSTREAM_TIMEOUT_MS ?? 60_000),
  };
}
