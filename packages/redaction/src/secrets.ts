/**
 * Deterministic credential/secret recognizers. Rampart covers PII (names,
 * emails, cards, addresses); it does not cover API keys and tokens, which are
 * the things agents paste into task context most often. This layer runs
 * before Rampart and uses the same stable-placeholder convention
 * ([API_KEY_1], [PRIVATE_KEY_1], ...), with its own in-memory session table.
 */

interface SecretPattern {
  label: string;
  re: RegExp;
}

// Order matters: multi-line PEM blocks first (their base64 body could
// otherwise partially match token patterns), then specific vendor keys,
// then generic token shapes.
const SECRET_PATTERNS: SecretPattern[] = [
  {
    label: "PRIVATE_KEY",
    re: /-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----/g,
  },
  // OpenAI / Anthropic style
  { label: "API_KEY", re: /\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}\b/g },
  // AWS access key id
  { label: "API_KEY", re: /\bAKIA[0-9A-Z]{16}\b/g },
  // GitHub tokens (classic + fine-grained)
  { label: "API_KEY", re: /\bgh[pousr]_[A-Za-z0-9]{36,255}\b/g },
  { label: "API_KEY", re: /\bgithub_pat_[A-Za-z0-9_]{22,255}\b/g },
  // Slack
  { label: "API_KEY", re: /\bxox[baprs]-[A-Za-z0-9-]{10,}\b/g },
  // Stripe secret/restricted keys
  { label: "API_KEY", re: /\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}\b/g },
  // JWTs (three base64url segments, first decoding to {"...)
  {
    label: "TOKEN",
    re: /\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b/g,
  },
  // Authorization: Bearer <token>
  { label: "TOKEN", re: /\bbearer\s+[A-Za-z0-9._~+/=-]{16,}/gi },
  // Connection strings with inline credentials (postgres://user:pass@host/db)
  {
    label: "CREDENTIAL_URL",
    re: /\b[a-z][a-z0-9+.-]*:\/\/[^\s:@/]+:[^\s@/]+@[^\s"']+/gi,
  },
];

export class SecretRedactor {
  private readonly table = new Map<string, string>();
  private readonly counters = new Map<string, number>();

  /** Replace secrets with stable placeholders; same raw value always maps
   *  to the same placeholder for the lifetime of this instance. */
  redact(text: string): { text: string; found: number } {
    let found = 0;
    let out = text;
    for (const { label, re } of SECRET_PATTERNS) {
      out = out.replace(re, (raw) => {
        found += 1;
        const existing = this.table.get(raw);
        if (existing !== undefined) return existing;
        const n = (this.counters.get(label) ?? 0) + 1;
        this.counters.set(label, n);
        const placeholder = `[${label}_${n}]`;
        this.table.set(raw, placeholder);
        return placeholder;
      });
    }
    return { text: out, found };
  }

  /** Restore raw values (used only by the `check` CLI demo — never applied
   *  to tool traffic). */
  reveal(text: string): string {
    let out = text;
    for (const [raw, placeholder] of this.table) {
      out = out.split(placeholder).join(raw);
    }
    return out;
  }

  entries(): Array<{ placeholder: string; kind: string }> {
    return [...this.table.values()].map((placeholder) => ({
      placeholder,
      kind: placeholder.replace(/^\[|_\d+\]$/g, ""),
    }));
  }
}
