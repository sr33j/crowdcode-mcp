import { mkdtemp, stat, writeFile, mkdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import {
  login,
  modeSetting,
  pkceChallenge,
  pkcePair,
  randomState,
  readCredentials,
  saveCredentials,
  saveModeSetting,
} from "../src/agents/account.js";
import { createAgentTools } from "../src/agents/tools.js";

const posix = process.platform !== "win32";

describe("PKCE and state", () => {
  it("computes the RFC 7636 S256 challenge", () => {
    expect(pkceChallenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")).toBe(
      "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
    );
  });

  it("makes fresh, URL-safe verifiers and states", () => {
    const a = pkcePair();
    const b = pkcePair();
    expect(a.verifier).toMatch(/^[A-Za-z0-9_-]{64}$/);
    expect(a.verifier).not.toBe(b.verifier);
    expect(a.challenge).toBe(pkceChallenge(a.verifier));
    expect(randomState()).toMatch(/^[A-Za-z0-9_-]{32}$/);
    expect(randomState()).not.toBe(randomState());
  });

  it("sends client crowdcode-mcp, S256 and a loopback redirect, and refuses an answer with the wrong state", async () => {
    const home = await mkdtemp(join(tmpdir(), "cc-login-"));
    let seen: URL | undefined;
    const attempt = login({
      baseUrl: "http://127.0.0.1:9",
      home,
      log: () => undefined,
      openBrowser: async (url) => {
        seen = new URL(url);
        const redirect = seen.searchParams.get("redirect_uri")!;
        await fetch(`${redirect}?code=cc_code_x&state=forged`);
      },
    });
    await expect(attempt).rejects.toThrow("state mismatch");
    expect(seen!.origin + seen!.pathname).toBe("http://127.0.0.1:9/connect");
    const q = seen!.searchParams;
    expect(q.get("client_id")).toBe("crowdcode-mcp");
    expect(q.get("response_type")).toBe("code");
    expect(q.get("code_challenge_method")).toBe("S256");
    expect(q.get("scope")).toBe("read tasks transfers withdraw_to_main services files");
    expect(q.get("redirect_uri")).toMatch(/^http:\/\/127\.0\.0\.1:\d+\/callback$/);
    expect(q.get("state")).toMatch(/^[A-Za-z0-9_-]{32}$/);
    expect(await readCredentials(home)).toBeNull();
  });

  it("gives up after the timeout", async () => {
    const home = await mkdtemp(join(tmpdir(), "cc-login-"));
    await expect(
      login({ baseUrl: "http://127.0.0.1:9", home, log: () => undefined, openBrowser: () => undefined, timeoutMs: 50 }),
    ).rejects.toThrow("Timed out");
  });
});

describe("credentials file", () => {
  it.skipIf(!posix)("is 0600 in a 0700 folder", async () => {
    const home = join(await mkdtemp(join(tmpdir(), "cc-home-")), "crowdcode");
    const path = await saveCredentials(home, {
      access_token: "at",
      refresh_token: "rt",
      expires_at: 1,
      scope: "read",
      base_url: "https://crowdcode.app",
    });
    expect((await stat(path)).mode & 0o777).toBe(0o600);
    expect((await stat(home)).mode & 0o777).toBe(0o700);
  });

  it("belongs to one origin only", async () => {
    const home = await mkdtemp(join(tmpdir(), "cc-home-"));
    await saveCredentials(home, {
      access_token: "at",
      refresh_token: null,
      expires_at: null,
      scope: null,
      base_url: "https://crowdcode.app/",
    });
    expect((await readCredentials(home, "https://crowdcode.app"))?.access_token).toBe("at");
    expect(await readCredentials(home, "https://evil.example")).toBeNull();
  });
});

describe("mode", () => {
  it("CROWDCODE_MODE wins over the saved setting; auto means account only when signed in", async () => {
    const home = await mkdtemp(join(tmpdir(), "cc-mode-"));
    expect(await modeSetting({}, home)).toEqual({ setting: "auto", from: "default" });
    await saveModeSetting(home, "wallet");
    expect(await modeSetting({}, home)).toEqual({ setting: "wallet", from: "config" });
    expect(await modeSetting({ CROWDCODE_MODE: "Account" }, home)).toEqual({ setting: "account", from: "CROWDCODE_MODE" });
    await expect(saveModeSetting(home, "both")).rejects.toThrow("must be one of auto, account, wallet");

    await saveModeSetting(home, "auto");
    const env = { CROWDCODE_HOME: home, CROWDCODE_APP_URL: "https://crowdcode.app" };
    const tools = createAgentTools({ env });
    expect(await tools.mode()).toBe("wallet");
    await saveCredentials(home, {
      access_token: "at",
      refresh_token: null,
      expires_at: null,
      scope: null,
      base_url: "https://crowdcode.app",
    });
    expect(await tools.mode()).toBe("account");
    expect(await createAgentTools({ env: { ...env, CROWDCODE_APP_URL: "http://localhost:1" } }).mode()).toBe("wallet");
  });

  it("only allows https, or http for localhost", async () => {
    const tools = createAgentTools({ env: { CROWDCODE_APP_URL: "http://crowdcode.app" } });
    const result = await tools.handlers().crowdcode_demand({});
    expect(result).toEqual({
      ok: false,
      error: "CROWDCODE_APP_URL must be an https:// URL (plain http is allowed only for localhost)",
    });
  });
});

describe("wallet creation", () => {
  it("respects CROWDCODE_DISABLE_WALLET_CREATE and never replaces a wallet", async () => {
    const root = await mkdtemp(join(tmpdir(), "cc-wallet-"));
    const env = {
      CROWDCODE_HOME: join(root, "home"),
      CROWDCODE_WALLET_DIR: join(root, "agentcash"),
      CROWDCODE_BASE_RPC_URL: "http://127.0.0.1:9/rpc",
      CROWDCODE_APP_URL: "http://127.0.0.1:9",
    };
    const off = await createAgentTools({ env: { ...env, CROWDCODE_DISABLE_WALLET_CREATE: "1" } })
      .handlers()
      .crowdcode_wallet({ init: true });
    expect(off.ok).toBe(false);
    expect(off.error).toContain("CROWDCODE_DISABLE_WALLET_CREATE");

    const handlers = createAgentTools({ env }).handlers();
    const made = await handlers.crowdcode_wallet({ init: true });
    expect(made).toMatchObject({ ok: true, mode: "wallet", created: true });
    expect(String(made.usdc_base)).toMatch(/^unavailable/);
    if (posix) expect((await stat(join(env.CROWDCODE_WALLET_DIR, "wallet.json"))).mode & 0o777).toBe(0o600);
    const again = await handlers.crowdcode_wallet({ init: true });
    expect(again).toMatchObject({ ok: true, created: false, address: made.address });
  });

  it("reports a broken wallet file instead of replacing it", async () => {
    const root = await mkdtemp(join(tmpdir(), "cc-wallet-"));
    await mkdir(join(root, "agentcash"));
    await writeFile(join(root, "agentcash", "wallet.json"), "{not json");
    const result = await createAgentTools({
      env: { CROWDCODE_HOME: join(root, "home"), CROWDCODE_WALLET_DIR: join(root, "agentcash") },
    })
      .handlers()
      .crowdcode_wallet({ init: true });
    expect(result.ok).toBe(false);
    expect(result.error).toContain("not valid JSON");
  });
});
