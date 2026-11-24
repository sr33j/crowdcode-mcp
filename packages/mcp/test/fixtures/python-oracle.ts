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
const REQUIRED = "import eth_account, httpx, psycopg, dotenv, cryptography";

const configured = process.env.CROWDCODE_PYTHON || process.env.CROWDCODE_MOCK_PYTHON;
const python = configured || "python3";

function usable(): boolean {
  try {
    execFileSync(python, ["-I", "-c", REQUIRED], { stdio: "ignore" });
    return true;
  } catch (err) {
    if (configured) {
      throw new Error(`${python} (CROWDCODE_PYTHON / CROWDCODE_MOCK_PYTHON) cannot run the Python oracle: ${(err as Error).message}`);
    }
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
