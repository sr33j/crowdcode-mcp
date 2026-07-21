/**
 * Python `str.strip()` equivalent.
 *
 * Python strips every character for which `str.isspace()` is true. That set
 * is a superset of JS `String.prototype.trim()`'s White_Space set — notably
 * U+001C..U+001F (file/group/record/unit separators) are Python-space but
 * not JS-space. The canonical payload spec (spec/CANONICAL_PAYLOAD.md)
 * requires Python semantics.
 */

const PY_SPACE =
  "\\t\\n\\x0b\\x0c\\r\\x1c\\x1d\\x1e\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";

const LEADING = new RegExp(`^[${PY_SPACE}]+`);
const TRAILING = new RegExp(`[${PY_SPACE}]+$`);

export function pyStrip(value: string): string {
  return value.replace(LEADING, "").replace(TRAILING, "");
}
