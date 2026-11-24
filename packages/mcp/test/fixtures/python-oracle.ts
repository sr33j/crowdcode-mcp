/**
 * Runs test/fixtures/python_oracle.py once over a batch of generated cases, for
 * the differentials against the Python wallet and backend code.
 *
 * The Python is CROWDCODE_PYTHON, else CROWDCODE_MOCK_PYTHON (CI's
 * agent-conformance job sets both to a venv with eth-account, httpx, psycopg,
 * python-dotenv and cryptography), else `python3` on the PATH. A Python named
 * by either variable must work: the differential fails rather than skips.
 * Only with neither variable set and no `python3` that can import those
 * modules is it skipped, and the test name then says so.
 */

import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const SCRIPT = fileURLToPath(new URL("./python_oracle.py", import.meta.url));
const MODULES = ["eth_account", "httpx", "psycopg", "dotenv", "cryptography"];

const configured = process.env.CROWDCODE_PYTHON || process.env.CROWDCODE_MOCK_PYTHON;
const python = configured || "python3";

/** A configured Python is used as is (a broken one fails the test); python3 is probed without importing anything. */
function usable(): boolean {
  if (configured) return true;
  const probe = `import importlib.util as u, sys; sys.exit(0 if all(u.find_spec(m) for m in ${JSON.stringify(MODULES)}) else 1)`;
  try {
    execFileSync(python, ["-I", "-c", probe], { stdio: "ignore" });
    return true;
  } catch {
    return false;
  }
}

export const pythonAvailable = usable();

/** Appended to a describe() name: empty when the differential runs. */
export const pythonSkipNote = pythonAvailable
  ? ""
  : " (SKIPPED: no python3 with eth-account, httpx and psycopg; set CROWDCODE_PYTHON)";

export function runPython<T = any>(cases: Array<Record<string, unknown>>): T[] {
  const out = execFileSync(python, ["-I", SCRIPT], {
    input: JSON.stringify({ cases }),
    maxBuffer: 64 * 1024 * 1024,
    encoding: "utf8",
  });
  return (JSON.parse(out) as { results: T[] }).results;
}

/** A JS number as the oracle's tagged Python float. */
export function pyFloatArg(value: number): { $float: string } {
  return { $float: Object.is(value, -0) ? "-0.0" : String(value) };
}
