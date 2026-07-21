/**
 * Serialize exactly like Python
 * `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)`.
 *
 * JS JSON.stringify cannot be used: it does not escape non-ASCII characters,
 * and Python additionally escapes U+007F. Hand-rolled per
 * spec/CANONICAL_PAYLOAD.md.
 */

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
    if (code === 0x22) {
      out += '\\"';
    } else if (code === 0x5c) {
      out += "\\\\";
    } else if (short !== undefined) {
      out += short;
    } else if (code < 0x20 || code > 0x7e) {
      // ensure_ascii: everything outside printable ASCII becomes \uxxxx
      // (lowercase hex). Iterating UTF-16 code units makes astral characters
      // come out as escaped surrogate pairs, matching CPython.
      out += "\\u" + code.toString(16).padStart(4, "0");
    } else {
      out += value[i];
    }
  }
  return out + '"';
}

export function pythonCanonicalJson(
  obj: Record<string, string | number | null>,
): string {
  const parts = Object.keys(obj)
    .sort()
    .map((key) => {
      const value = obj[key] ?? null;
      let encoded: string;
      if (value === null) {
        encoded = "null";
      } else if (typeof value === "number") {
        if (!Number.isInteger(value)) {
          throw new Error(`non-integer number in canonical payload: ${key}`);
        }
        encoded = String(value);
      } else {
        encoded = encodeString(value);
      }
      return `${encodeString(key)}:${encoded}`;
    });
  return `{${parts.join(",")}}`;
}
