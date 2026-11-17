/**
 * Listing and downloading the files an agent produced. Nothing here runs
 * unless asked. Ported from the Hermes plugin's files.py, without its
 * Hermes-only write policy: Codex and Claude Code apply their own file
 * permissions.
 *
 * Files come one at a time from the owner API, which streams the bytes (no
 * links are handed out). Each is written straight to disk under a sanitised
 * name, created exclusively so nothing is ever overwritten, and removed again
 * if the download fails or passes the size limit.
 */

import { open, rm, type FileHandle } from "node:fs/promises";
import { homedir } from "node:os";
import { basename, isAbsolute, join, resolve } from "node:path";
import type { OwnerApi } from "./client.js";
import { ToolError } from "./errors.js";

export const DEFAULT_DIR = "crowdcode-files";
export const PAGE = 200;
export const MAX_LIST = 2000;
export const DEFAULT_MAX_DOWNLOAD_MB = 500;

const BAD = /[<>:"|?*\x00-\x1f\x7f]/g;
const RESERVED = new Set([
  "con",
  "prn",
  "aux",
  "nul",
  ...Array.from({ length: 9 }, (_, i) => `com${i + 1}`),
  ...Array.from({ length: 9 }, (_, i) => `lpt${i + 1}`),
]);
// Python's str.strip(): the characters str.isspace() accepts.
const PY_SPACE = "\\t\\n\\x0b\\x0c\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";
const PY_STRIP = new RegExp(`^[${PY_SPACE}]+|[${PY_SPACE}]+$`, "g");

/** Python's `str.rpartition(".")`. */
function rpartition(text: string): [string, string, string] {
  const at = text.lastIndexOf(".");
  return at < 0 ? ["", "", text] : [text.slice(0, at), ".", text.slice(at + 1)];
}

/** The first `max` bytes of `text` as UTF-8, dropping a character cut in half (`decode(errors="ignore")`). */
function truncateUtf8(text: string, max: number): string {
  const bytes = Buffer.from(text, "utf8");
  if (bytes.length <= max) return text;
  let cut = max;
  while (cut > 0 && (bytes[cut]! & 0xc0) === 0x80) cut--;
  return bytes.subarray(0, cut).toString("utf8");
}

/** A single path component: no directories, no traversal, no control or reserved characters. */
export function safeName(name: unknown, fallback = "file"): string {
  let text = String(name ?? "").normalize("NFC");
  text = text.replace(/\\/g, "/").split("/").pop() ?? "";
  text = text.replace(BAD, "_").replace(PY_STRIP, "").replace(/^\.+|\.+$/g, "");
  if (!text || text === "." || text === "..") text = fallback;
  const [stem, dot, ext] = rpartition(text);
  if (RESERVED.has((dot ? stem : text).toLowerCase())) text = "_" + text;
  if (Buffer.byteLength(text, "utf8") > 200) {
    // keep the extension, trim the stem
    const keep = dot && [...ext].length <= 16 ? "." + ext : "";
    text = truncateUtf8(text, 200 - Buffer.byteLength(keep, "utf8")) + keep;
  }
  return text;
}

/** Create `directory/name` exclusively, adding `-1`, `-2`… while that name exists. */
export async function uniquePath(directory: string, name: string): Promise<{ path: string; handle: FileHandle }> {
  let [stem, dot, ext] = rpartition(name);
  if (!dot || !stem) {
    stem = name;
    ext = "";
  } else {
    ext = "." + ext;
  }
  for (let n = 0; n < 10000; n++) {
    const candidate = join(directory, n === 0 ? name : `${stem}-${n}${ext}`);
    try {
      return { path: candidate, handle: await open(candidate, "wx", 0o644) };
    } catch (err) {
      if ((err as NodeJS.ErrnoException).code === "EEXIST") continue;
      throw err;
    }
  }
  throw new ToolError(`too many files named ${name} in ${directory}`);
}

/** The folder files would be saved into: `dest` (relative to `base`, or absolute), else ./crowdcode-files/<agent>/. */
export function planDest(dest: string, agentName: string, base: string): string {
  const root = resolve(base);
  if (!dest) return join(root, DEFAULT_DIR, safeName(agentName, "agent"));
  let target = dest;
  if (target === "~" || target.startsWith("~/") || target.startsWith("~\\")) target = join(homedir(), target.slice(1));
  return isAbsolute(target) ? resolve(target) : resolve(root, target);
}

export interface FileEntry {
  id?: string;
  name?: string;
  path?: string;
  size_bytes?: number;
  content_type?: string;
  task_id?: string;
  created_at?: string;
  [key: string]: unknown;
}

/** Every ready file of the agent, newest first, following `before` pages. */
export async function listFiles(api: OwnerApi, agentId: string, taskId = ""): Promise<FileEntry[]> {
  const files: FileEntry[] = [];
  let before = "";
  while (files.length < MAX_LIST) {
    const query = [`limit=${PAGE}`];
    if (taskId) query.push(`task_id=${encodeURIComponent(taskId)}`);
    if (before) query.push(`before=${encodeURIComponent(before)}`);
    let page = await api.signed("GET", `/agents/${agentId}/files?${query.join("&")}`);
    if (page && !Array.isArray(page) && typeof page === "object") page = page.files ?? [];
    const rows = (Array.isArray(page) ? page : []) as FileEntry[];
    files.push(...rows);
    const last = rows[rows.length - 1];
    if (rows.length < PAGE || !last?.created_at) break;
    before = String(last.created_at);
  }
  return files;
}

export function tooLarge(name: string, maxBytes: number): ToolError {
  return new ToolError(`${name} is larger than the ${Math.floor(maxBytes / 1_000_000)} MB download limit`);
}

/** Stream one file into `directory`; `{id, name, path, size_bytes}`. A failed or oversized download leaves no file. */
export async function downloadFile(
  api: OwnerApi,
  agentId: string,
  file: FileEntry,
  directory: string,
  maxBytes: number,
): Promise<{ id: unknown; name: string; path: string; size_bytes: number }> {
  const name = safeName(file.name || file.path, `file-${file.id ?? "unknown"}`);
  if (Number(file.size_bytes) > maxBytes) throw tooLarge(name, maxBytes);
  const { path, handle } = await uniquePath(directory, name);
  let written = 0;
  try {
    try {
      const stream = await api.signedStream(`/agents/${agentId}/files/${file.id}`);
      for await (const chunk of stream) {
        written += chunk.length;
        if (written > maxBytes) throw tooLarge(name, maxBytes);
        await handle.write(chunk);
      }
    } finally {
      await handle.close();
    }
  } catch (err) {
    await rm(path, { force: true });
    throw err;
  }
  return { id: file.id ?? null, name: basename(path), path, size_bytes: written };
}
