# CrowdCode Canonical Review Payload — Cross-Language Spec

This document is the normative contract between the Python backend
(`src/crowdcode/payments.py:canonical_review_payload`,
`src/crowdcode/identity.py`) and any client that constructs review signing
payloads locally (the TypeScript MCP package in `packages/mcp`). The two
implementations must produce **byte-identical** output for the same inputs.
Conformance is enforced by `spec/review-payload-vectors.json`, generated from
the Python reference by `scripts/generate_vectors.py` and consumed by both the
pytest and vitest suites. Never hand-edit the vector file; regenerate it.

## Payload version

`type` is fixed at `"crowdcode.review.v1"`. Any change to the field set,
serialization, or normalization rules below requires bumping to `.v2` in a
coordinated backend + client release.

## Message construction

The message is a JSON object serialized with Python
`json.dumps(payload, sort_keys=True, separators=(",", ":"))` semantics:

- Keys sorted lexicographically (all keys are ASCII).
- Separators `,` and `:` with no whitespace anywhere.
- **`ensure_ascii=True` escaping**: every character above U+007F is escaped as
  `\uXXXX` with **lowercase** hex digits. Characters outside the BMP are
  encoded as a UTF-16 surrogate pair, each half escaped (U+1F680 →
  backslash `ud83d` then backslash `ude80`). Iterating UTF-16 code units and
  escaping each one ≥ 0x80 reproduces this exactly.
- Control characters use the short escapes `\b \t \n \f \r` where they exist,
  otherwise `\u00XX` (lowercase hex). `"` → `\"` and `\` → `\\`. Forward slash
  is **not** escaped.
- Every key is always present. Absent optional values serialize as `null` —
  never omit a key (JS `undefined` omission is a conformance bug).
- No trailing newline.

### Fields (shown in sorted order)

| key | value |
|---|---|
| `api_endpoint` | normalized endpoint (see below) or `null` |
| `directory_slug` | cleaned (trimmed, empty→`null`) or `null` |
| `payment_provider` | normalized provider (see below) or `null` |
| `payment_reference` | input with Python `str.strip()` applied |
| `payment_target_ref` | cleaned (trimmed, empty→`null`) or `null` |
| `rating` | JSON integer (1–5) |
| `reason_hash` | `"sha256:" + lowercase_hex(sha256(utf8(strip(reason))))` |
| `service_id` | string or `null` |
| `type` | literal `"crowdcode.review.v1"` |

Identity fields are inserted **verbatim** — normalization happens earlier,
when the identity is built (`build_identity`), not inside payload
construction.

### `reason_hash`

- The reason text is the **already-redacted** text (redaction happens before
  hashing on clients that redact; the server hashes whatever `reason` it
  receives in `review_service` — the two must be the identical string).
- Trim with **Python `str.strip()` semantics**: strips all characters for
  which Python `str.isspace()` is true. This is a superset of JS
  `String.prototype.trim()` — notably U+001C–U+001F are Python-space but not
  JS-space. Clients must implement a `pyStrip` equivalent.
- Hash the raw UTF-8 bytes of the trimmed string. **No Unicode
  normalization** (no NFC/NFD): `café` (NFC) and `café` (NFD) hash
  differently, by design.

### Signing

The exact message string is signed with EIP-191 personal-message semantics
(`personal_sign` / eth-account `encode_defunct(text=message)`). The server
recovers the signer and requires it to equal `reviewer_wallet`
(case-insensitive). Signatures are only required for `mppx` and `x402`
providers.

The server verifies by **rebuilding the payload from its own resolved
identity** — it never parses the client's message. The client's signed message
must therefore match the server's reconstruction byte-for-byte, including the
resolved `service_id` (see Identity resolution below).

## Identity normalization (normative, feeds the payload)

### `normalize_api_endpoint`

1. Trim; empty → `null`.
2. If the string does not contain `://`, prefix `https://`.
3. Split as a URL. No host → error `"api_endpoint must include a host"`.
4. Scheme: lowercased.
5. Host: lowercased. Userinfo (`user:pass@`) is **dropped**. An explicit port
   is **kept verbatim, even if it is the scheme default** (`:443` stays).
   (IPv6 literals are unsupported/undefined.)
6. Path: strip **all** trailing `/` characters; empty path → `/`.
7. Query and fragment: dropped.
8. Reassemble `scheme://host[:port]path`.

### `normalize_payment_provider`

1. Trim; empty → `null`.
2. Lowercase; replace `-` with `_`.
3. Aliases: `link`, `stripe_link`, `payment_link` → `stripe_payment_link`;
   `mpp` → `mppx`.
4. Must be one of `manual, mppx, stripe, stripe_payment_link, x402`, else
   error `"payment_provider must be one of: ..."`.

### `generate_service_id`

`"svc_" + lowercase_hex(sha256(utf8(normalized_endpoint + "|" + provider + "|" + strip(target_ref))))[:20]`

Only valid for services not yet registered: `resolve_service` on the backend
may return a pre-existing row whose id differs. Clients should prefer the
canonical identity returned by `get_service_score` (`found: true`) and fall
back to local generation only when the service is unknown. If a locally
generated `service_id` loses a creation race, `review_service` returns
`resolved_identity` and `expected_message` in its signature-mismatch error so
the client can re-sign and retry.

## Conformance

- Regenerate vectors: `python scripts/generate_vectors.py`
- Python: `pytest tests/test_canonical_vectors.py`
- TypeScript: `npm test -w packages/mcp` (`test/canonical.test.ts`)
