/**
 * ~/.crowdcode (CROWDCODE_HOME): the account sign-in (credentials.json), the
 * mode setting (config.json) and the recent tasks the dashboard shows
 * (state.json). Written atomically with 0600 files in a 0700 folder.
 */

import { randomBytes } from "node:crypto";
import { chmod, mkdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";

export function crowdcodeHome(env: NodeJS.ProcessEnv = process.env): string {
  return env.CROWDCODE_HOME?.trim() || join(homedir(), ".crowdcode");
}

export const CREDENTIALS_FILE = "credentials.json";
export const CONFIG_FILE = "config.json";
export const STATE_FILE = "state.json";

export async function readJsonFile(path: string): Promise<unknown> {
  let raw: string;
  try {
    raw = await readFile(path, "utf8");
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === "ENOENT") return undefined;
    throw err;
  }
  try {
    return JSON.parse(raw);
  } catch {
    return undefined;
  }
}

/** Write `value` as JSON to `home/name`: the folder 0700, the file 0600, replaced atomically. */
export async function writePrivateJson(home: string, name: string, value: unknown): Promise<string> {
  await mkdir(home, { recursive: true, mode: 0o700 });
  await chmod(home, 0o700);
  const path = join(home, name);
  const temporary = join(home, `.${name}.${randomBytes(6).toString("hex")}.tmp`);
  try {
    await writeFile(temporary, JSON.stringify(value, null, 2) + "\n", { mode: 0o600, flag: "wx" });
    await chmod(temporary, 0o600);
    await rename(temporary, path);
  } catch (err) {
    await rm(temporary, { force: true });
    throw err;
  }
  return path;
}

export async function removeFile(home: string, name: string): Promise<boolean> {
  try {
    await rm(join(home, name));
    return true;
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === "ENOENT") return false;
    throw err;
  }
}

export interface TaskRecord {
  agent_id: string;
  task_id: string;
  prompt?: string;
  budget_usd?: number;
  status?: string;
  spent_usd?: unknown;
  /** Unix seconds. */
  at: number;
}

export async function recentTasks(home: string): Promise<TaskRecord[]> {
  const state = (await readJsonFile(join(home, STATE_FILE))) as { tasks?: unknown } | undefined;
  return Array.isArray(state?.tasks) ? (state!.tasks as TaskRecord[]) : [];
}

export async function rememberTask(home: string, record: TaskRecord): Promise<void> {
  const state = ((await readJsonFile(join(home, STATE_FILE))) ?? {}) as Record<string, unknown>;
  const tasks = (Array.isArray(state.tasks) ? (state.tasks as TaskRecord[]) : []).filter(
    (t) => t?.task_id !== record.task_id,
  );
  tasks.push(record);
  await writePrivateJson(home, STATE_FILE, { ...state, tasks: tasks.slice(-50) });
}
