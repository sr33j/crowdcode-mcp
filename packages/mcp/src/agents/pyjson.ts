/**
 * JSON exactly as Python's `json.dumps` writes it, so request bodies (and the
 * SHA-256 a request signature covers) are byte-identical to the Hermes plugin's:
 * `ensure_ascii` escaping, Python's float repr, insertion-ordered keys unless
 * `sortKeys`, and either the compact `(",", ":")` or the default `(", ", ": ")`
 * separators.
 *
 * JavaScript has one number type; Python bodies carry `float(...)` amounts
 * (`1.0`, not `1`). Wrap a value in `pyFloat` to write it the way a Python float
 * is written. Other integral numbers are written as Python ints.
 */

export class PyFloat {
  constructor(readonly value: number) {}
  toJSON(): number {
    return this.value;
  }
}

export function pyFloat(value: number): PyFloat {
  return new PyFloat(value);
}

const SHORT_ESCAPES: Record<number, string> = {
  0x08: "\\b",
  0x09: "\\t",
  0x0a: "\\n",
  0x0c: "\\f",
  0x0d: "\\r",
};

function encodeString(value: string): string {
  let out = '"';
  for (let i = 0; i < value.length; i++) {
    const code = value.charCodeAt(i);
    const short = SHORT_ESCAPES[code];
    if (code === 0x22) out += '\\"';
    else if (code === 0x5c) out += "\\\\";
    else if (short !== undefined) out += short;
    // ensure_ascii: everything outside printable ASCII (including U+007F) is
    // escaped; UTF-16 code units give surrogate pairs for astral characters,
    // as CPython writes them.
    else if (code < 0x20 || code > 0x7e) out += "\\u" + code.toString(16).padStart(4, "0");
    else out += value[i];
  }
  return out + '"';
}

/** `repr(float(x))` in Python 3: the shortest round-trip digits, fixed for 1e-4 <= |x| < 1e16. */
export function pyFloatRepr(x: number): string {
  if (Number.isNaN(x)) return "NaN";
  if (x === Infinity) return "Infinity";
  if (x === -Infinity) return "-Infinity";
  if (x === 0) return Object.is(x, -0) ? "-0.0" : "0.0";
  const sign = x < 0 ? "-" : "";
  const [mantissa, expText] = Math.abs(x).toExponential().split("e") as [string, string];
  const digits = mantissa.replace(".", "");
  const exp = Number(expText);
  if (exp >= -4 && exp < 16) {
    if (exp < 0) return `${sign}0.${"0".repeat(-exp - 1)}${digits}`;
    const whole = digits.slice(0, exp + 1).padEnd(exp + 1, "0");
    return `${sign}${whole}.${digits.slice(exp + 1) || "0"}`;
  }
  const lead = digits.length > 1 ? `${digits[0]}.${digits.slice(1)}` : digits;
  return `${sign}${lead}e${exp < 0 ? "-" : "+"}${String(Math.abs(exp)).padStart(2, "0")}`;
}

function encodeNumber(value: number): string {
  if (Number.isInteger(value)) {
    return Number.isSafeInteger(value) ? String(value) : BigInt(value).toString();
  }
  return pyFloatRepr(value);
}

export interface PyDumpsOptions {
  /** `separators=(",", ":")`; default is Python's `(", ", ": ")`. */
  compact?: boolean;
  sortKeys?: boolean;
}

export function pyDumps(value: unknown, options: PyDumpsOptions = {}): string {
  const item = options.compact ? "," : ", ";
  const key = options.compact ? ":" : ": ";
  const encode = (v: unknown): string => {
    if (v === null || v === undefined) return "null";
    if (v instanceof PyFloat) return pyFloatRepr(v.value);
    if (typeof v === "boolean") return v ? "true" : "false";
    if (typeof v === "number") return encodeNumber(v);
    if (typeof v === "bigint") return v.toString();
    if (typeof v === "string") return encodeString(v);
    if (Array.isArray(v)) return `[${v.map(encode).join(item)}]`;
    if (typeof v === "object") {
      let entries = Object.entries(v as Record<string, unknown>).filter(([, x]) => x !== undefined);
      if (options.sortKeys) entries = entries.sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
      return `{${entries.map(([k, x]) => encodeString(k) + key + encode(x)).join(item)}}`;
    }
    return encodeString(String(v));
  };
  return encode(value);
}

/** `json.dumps(payload, separators=(",", ":")).encode()`, or empty bytes for no payload. */
export function jsonBody(payload: unknown): Uint8Array {
  return payload === undefined || payload === null
    ? new Uint8Array()
    : new TextEncoder().encode(pyDumps(payload, { compact: true }));
}
