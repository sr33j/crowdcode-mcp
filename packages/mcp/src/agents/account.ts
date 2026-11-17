/**
 * Account mode: `crowdcode-mcp login` signs this machine in to a CrowdCode
 * account with OAuth 2.0 (authorization code + PKCE S256, a loopback
 * redirect), and the tools then call the same `/api/app/v1` routes with
 * `Authorization: Bearer`. What the sign-in allows (scopes, agents, a daily
 * cap, an expiry) is enforced by CrowdCode, as for every connected app.
 *
 * The grant is kept in ~/.crowdcode/credentials.json (0600; CROWDCODE_HOME
 * overrides the folder), with the origin it belongs to: it is never sent to
 * any other CROWDCODE_APP_URL.
 */

import { spawn } from "node:child_process";
import { createHash, randomBytes, timingSafeEqual } from "node:crypto";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { join } from "node:path";
import {
  ApiError,
  decodeError,
  http,
  type OwnerApi,
  readJson,
  STREAM_TIMEOUT_MS,
  streamOf,
} from "./client.js";
import { LOGIN_COMMAND, NotSignedIn, ToolError } from "./errors.js";
import { CONFIG_FILE, CREDENTIALS_FILE, readJsonFile, removeFile, writePrivateJson } from "./home.js";
import { jsonBody } from "./pyjson.js";

export const CLIENT_ID = "crowdcode-mcp";
export const SCOPE = "read tasks transfers withdraw_to_main services files";
export const LOGIN_TIMEOUT_MS = 5 * 60_000;
/** Refresh this many seconds before the access token's stated expiry. */
const EXPIRY_SKEW_SECONDS = 30;

export const MODES = ["auto", "account", "wallet"] as const;
export type ModeSetting = (typeof MODES)[number];

export interface Credentials {
  access_token: string;
  refresh_token: string | null;
  /** Unix seconds; null when the server did not say. */
  expires_at: number | null;
  scope: string | null;
  base_url: string;
}

function origin(url: string): string {
  return url.replace(/\/+$/, "");
}

// -- PKCE ------------------------------------------------------------------

export function base64url(bytes: Uint8Array): string {
  return Buffer.from(bytes).toString("base64url");
}

/** RFC 7636 S256: a 64-character verifier and its challenge. */
export function pkcePair(): { verifier: string; challenge: string } {
  const verifier = base64url(randomBytes(48));
  return { verifier, challenge: pkceChallenge(verifier) };
}

export function pkceChallenge(verifier: string): string {
  return base64url(createHash("sha256").update(verifier).digest());
}

export function randomState(): string {
  return base64url(randomBytes(24));
}

function sameSecret(a: string, b: string): boolean {
  const x = Buffer.from(a);
  const y = Buffer.from(b);
  return x.length === y.length && timingSafeEqual(x, y);
}

// -- credentials -------------------------------------------------------------

export async function readCredentials(home: string, baseUrl?: string): Promise<Credentials | null> {
  const data = (await readJsonFile(join(home, CREDENTIALS_FILE))) as Partial<Credentials> | undefined;
  if (!data || typeof data.access_token !== "string" || !data.access_token || typeof data.base_url !== "string") {
    return null;
  }
  if (baseUrl !== undefined && origin(data.base_url) !== origin(baseUrl)) return null;
  return {
    access_token: data.access_token,
    refresh_token: typeof data.refresh_token === "string" ? data.refresh_token : null,
    expires_at: typeof data.expires_at === "number" ? data.expires_at : null,
    scope: typeof data.scope === "string" ? data.scope : null,
    base_url: origin(data.base_url),
  };
}

export async function saveCredentials(home: string, credentials: Credentials): Promise<string> {
  return writePrivateJson(home, CREDENTIALS_FILE, credentials);
}

export async function deleteCredentials(home: string): Promise<boolean> {
  return removeFile(home, CREDENTIALS_FILE);
}

function fromTokenResponse(body: any, baseUrl: string, previous?: Credentials): Credentials {
  if (!body || typeof body.access_token !== "string" || !body.access_token) {
    throw new ApiError(502, "the token endpoint returned no access_token", body);
  }
  const expiresIn = Number(body.expires_in);
  return {
    access_token: body.access_token,
    refresh_token: typeof body.refresh_token === "string" ? body.refresh_token : (previous?.refresh_token ?? null),
    expires_at: Number.isFinite(expiresIn) && expiresIn > 0 ? Math.floor(Date.now() / 1000) + expiresIn : null,
    scope: typeof body.scope === "string" ? body.scope : (previous?.scope ?? null),
    base_url: origin(baseUrl),
  };
}

async function tokenRequest(baseUrl: string, form: Record<string, string>): Promise<Response> {
  return http(origin(baseUrl) + "/api/app/oauth/token", {
    method: "POST",
    body: new URLSearchParams(form).toString(),
    headers: { "content-type": "application/x-www-form-urlencoded", accept: "application/json" },
  });
}

/**
 * Trade the refresh token for a new grant. A refused refresh (4xx) means the
 * sign-in is gone: the credentials are deleted. A network failure keeps them.
 */
export async function refreshCredentials(home: string, credentials: Credentials): Promise<Credentials> {
  if (!credentials.refresh_token) {
    await deleteCredentials(home);
    throw new NotSignedIn(`The CrowdCode sign-in expired. Sign in again: ${LOGIN_COMMAND}`);
  }
  const response = await tokenRequest(credentials.base_url, {
    grant_type: "refresh_token",
    refresh_token: credentials.refresh_token,
    client_id: CLIENT_ID,
  });
  if (response.status >= 400 && response.status < 500) {
    await response.body?.cancel().catch(() => undefined);
    await deleteCredentials(home);
    throw new NotSignedIn(`The CrowdCode sign-in was revoked or expired. Sign in again: ${LOGIN_COMMAND}`);
  }
  const next = fromTokenResponse(await readJson(response), credentials.base_url, credentials);
  await saveCredentials(home, next);
  return next;
}

// -- login -------------------------------------------------------------------

export type BrowserOpener = (url: string) => void | Promise<void>;

/** Open `url` in the default browser; quietly does nothing where that is not possible. */
export const openInBrowser: BrowserOpener = (url) => {
  const [command, args] =
    process.platform === "darwin"
      ? ["open", [url]]
      : process.platform === "win32"
        ? ["cmd", ["/c", "start", '""', url.replace(/&/g, "^&")]]
        : ["xdg-open", [url]];
  try {
    const child = spawn(command, args as string[], {
      stdio: "ignore",
      detached: true,
      windowsVerbatimArguments: process.platform === "win32",
    });
    child.on("error", () => undefined);
    child.unref();
  } catch {
    // no browser here: the printed link is enough
  }
};

export interface LoginOptions {
  baseUrl: string;
  home: string;
  /** Opens the sign-in page; tests drive the flow with their own. */
  openBrowser?: BrowserOpener;
  /** Where the link and progress go (default stderr). */
  log?: (line: string) => void;
  timeoutMs?: number;
}

const DONE_PAGE = `<!doctype html><meta charset="utf-8"><title>CrowdCode</title>
<body style="font-family:system-ui;margin:3em">
<h1>Signed in to CrowdCode</h1><p>crowdcode-mcp is connected. You can close this tab.</p></body>`;

const FAILED_PAGE = (why: string) => `<!doctype html><meta charset="utf-8"><title>CrowdCode</title>
<body style="font-family:system-ui;margin:3em"><h1>Not signed in</h1><p>${why.replace(/[<>&"]/g, "")}</p></body>`;

/**
 * Sign in: listen on `http://127.0.0.1:<random port>/callback`, send the
 * browser to `<base>/connect` (client `crowdcode-mcp`, PKCE S256, a random
 * `state`), wait up to five minutes for the redirect, exchange the code and
 * store the grant.
 */
export async function login(options: LoginOptions): Promise<Credentials> {
  const baseUrl = origin(options.baseUrl);
  const log = options.log ?? ((line: string) => process.stderr.write(line + "\n"));
  const { verifier, challenge } = pkcePair();
  const state = randomState();

  let settle!: { resolve: (code: string) => void; reject: (err: Error) => void };
  const codePromise = new Promise<string>((resolve, reject) => {
    settle = { resolve, reject };
  });
  const server: Server = createServer((req, res) => {
    const url = new URL(req.url ?? "/", "http://127.0.0.1");
    if (url.pathname !== "/callback") {
      res.writeHead(404, { "content-type": "text/plain" }).end("not found");
      return;
    }
    const error = url.searchParams.get("error");
    const code = url.searchParams.get("code");
    const returned = url.searchParams.get("state") ?? "";
    let failure: string | null = null;
    if (!sameSecret(returned, state)) failure = "The sign-in answer did not match this login (state mismatch).";
    else if (error) failure = `CrowdCode did not sign you in: ${error}${url.searchParams.get("error_description") ? ` (${url.searchParams.get("error_description")})` : ""}.`;
    else if (!code) failure = "The sign-in answer carried no authorization code.";
    if (failure) {
      res.writeHead(400, { "content-type": "text/html; charset=utf-8" }).end(FAILED_PAGE(failure));
      settle.reject(new ToolError(failure));
      return;
    }
    res.writeHead(200, { "content-type": "text/html; charset=utf-8" }).end(DONE_PAGE);
    settle.resolve(code!);
  });
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => resolve());
  });
  const port = (server.address() as AddressInfo).port;
  const redirectUri = `http://127.0.0.1:${port}/callback`;
  const query = new URLSearchParams({
    response_type: "code",
    client_id: CLIENT_ID,
    redirect_uri: redirectUri,
    scope: SCOPE,
    state,
    code_challenge: challenge,
    code_challenge_method: "S256",
  });
  const authorizeUrl = `${baseUrl}/connect?${query.toString()}`;
  const timer = setTimeout(
    () => settle.reject(new ToolError("Timed out waiting for the CrowdCode sign-in (5 minutes). Run login again.")),
    options.timeoutMs ?? LOGIN_TIMEOUT_MS,
  );
  try {
    log("Sign in to CrowdCode in your browser. If it does not open, visit:");
    log("");
    log(`  ${authorizeUrl}`);
    log("");
    void Promise.resolve()
      .then(() => (options.openBrowser ?? openInBrowser)(authorizeUrl))
      .catch(() => undefined);
    const code = await codePromise;
    const response = await tokenRequest(baseUrl, {
      grant_type: "authorization_code",
      code,
      code_verifier: verifier,
      redirect_uri: redirectUri,
      client_id: CLIENT_ID,
    });
    if (response.status >= 400) throw await decodeError(response);
    const credentials = fromTokenResponse(await readJson(response), baseUrl);
    await saveCredentials(options.home, credentials);
    return credentials;
  } finally {
    clearTimeout(timer);
    server.closeAllConnections?.();
    server.close();
  }
}

/** Revoke the sign-in at CrowdCode (best effort), then delete it locally. */
export async function logout(home: string): Promise<{ signedIn: boolean; revoked: boolean }> {
  const credentials = await readCredentials(home);
  if (!credentials) {
    await deleteCredentials(home);
    return { signedIn: false, revoked: false };
  }
  let revoked = false;
  try {
    // The token goes only to the origin it was issued by.
    const response = await http(credentials.base_url + "/api/app/v1/connection/revoke", {
      method: "POST",
      body: jsonBody({}),
      headers: { authorization: `Bearer ${credentials.access_token}`, "content-type": "application/json" },
    });
    revoked = response.ok;
    await response.body?.cancel().catch(() => undefined);
  } catch {
    revoked = false;
  } finally {
    await deleteCredentials(home);
  }
  return { signedIn: true, revoked };
}

// -- mode ----------------------------------------------------------------------

export interface ModeSource {
  setting: ModeSetting;
  from: "CROWDCODE_MODE" | "config" | "default";
}

export async function modeSetting(env: NodeJS.ProcessEnv, home: string): Promise<ModeSource> {
  const fromEnv = env.CROWDCODE_MODE?.trim().toLowerCase();
  if (fromEnv) return { setting: checkMode(fromEnv), from: "CROWDCODE_MODE" };
  const config = (await readJsonFile(join(home, CONFIG_FILE))) as { mode?: unknown } | undefined;
  if (typeof config?.mode === "string" && config.mode) {
    return { setting: checkMode(config.mode.toLowerCase()), from: "config" };
  }
  return { setting: "auto", from: "default" };
}

export function checkMode(value: string): ModeSetting {
  if (!(MODES as readonly string[]).includes(value)) {
    throw new ToolError(`the mode setting must be one of ${MODES.join(", ")}`);
  }
  return value as ModeSetting;
}

export async function saveModeSetting(home: string, mode: string): Promise<ModeSetting> {
  const checked = checkMode(mode.trim().toLowerCase());
  const config = ((await readJsonFile(join(home, CONFIG_FILE))) ?? {}) as Record<string, unknown>;
  await writePrivateJson(home, CONFIG_FILE, { ...config, mode: checked });
  return checked;
}

// -- the bearer client -------------------------------------------------------

/** The `signed`/`signedStream` interface of WalletClient, authenticated by the account sign-in. */
export class AccountClient implements OwnerApi {
  readonly wallet = null;

  constructor(
    readonly baseUrl: string,
    private readonly home: string,
  ) {}

  get apiRoot(): string {
    return origin(this.baseUrl) + "/api/app/v1";
  }

  private async credentials(): Promise<Credentials> {
    const credentials = await readCredentials(this.home, this.baseUrl);
    if (!credentials) {
      throw new NotSignedIn(`Not signed in to a CrowdCode account at ${origin(this.baseUrl)}.`);
    }
    if (credentials.expires_at !== null && Date.now() / 1000 >= credentials.expires_at - EXPIRY_SKEW_SECONDS) {
      return refreshCredentials(this.home, credentials);
    }
    return credentials;
  }

  private async call(
    method: string,
    path: string,
    payload: unknown,
    extraHeaders: Record<string, string>,
    timeoutMs?: number,
  ): Promise<Response> {
    const body = jsonBody(payload);
    const send = (token: string) => {
      const headers: Record<string, string> = { ...extraHeaders, authorization: `Bearer ${token}` };
      if (body.length) headers["content-type"] = "application/json";
      return http(
        this.apiRoot + path,
        { method: method.toUpperCase(), headers, body: body.length ? body : undefined },
        timeoutMs,
      );
    };
    let credentials = await this.credentials();
    let response = await send(credentials.access_token);
    if (response.status === 401) {
      const error = await decodeError(response);
      if (error.error !== "invalid_token") throw error;
      // One refresh, then one retry: the first request was refused, nothing happened.
      credentials = await refreshCredentials(this.home, credentials);
      response = await send(credentials.access_token);
    }
    return response;
  }

  async signed(method: string, path: string, payload?: unknown, extraHeaders: Record<string, string> = {}): Promise<any> {
    return readJson(await this.call(method, path, payload, extraHeaders));
  }

  async signedStream(path: string): Promise<AsyncIterable<Uint8Array>> {
    return streamOf(await this.call("GET", path, undefined, {}, STREAM_TIMEOUT_MS));
  }
}
