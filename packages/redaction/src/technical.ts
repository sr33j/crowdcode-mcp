/**
 * Narrow false-positive filter for the Rampart NER model on technical text.
 *
 * The model reads HTTP status lines as street addresses ("502 Bad Gateway" →
 * BUILDING_NUMBER + STREET_NAME). Their confidence overlaps real addresses,
 * so a global score threshold cannot separate them. Instead, drop only:
 *
 * - STREET_NAME spans that are exactly an HTTP reason phrase, and
 * - BUILDING_NUMBER / STREET_NAME spans that are a 1xx–5xx status code
 *   directly after "HTTP", "status", "code" or "error", or directly before a
 *   reason phrase or "error" / "response" / "status".
 *
 * Every other span, including names and real street lines, is kept.
 */

import type { Span } from "@nationaldesignstudio/rampart";

const REASON_PHRASES = [
  "bad request", "unauthorized", "payment required", "forbidden", "not found",
  "method not allowed", "not acceptable", "request timeout", "conflict", "gone",
  "precondition failed", "payload too large", "unsupported media type",
  "unprocessable entity", "unprocessable content", "too many requests",
  "internal server error", "not implemented", "bad gateway",
  "service unavailable", "gateway timeout",
];
const REASON = REASON_PHRASES.map((p) => p.replace(/ /g, "\\s+")).join("|");
const REASON_ONLY = new RegExp(`^(?:${REASON})$`, "i");
const STATUS_CODE = /^[1-5]\d\d$/;
const BEFORE = /\b(?:https?(?:\/[\d.]+)?|status(?:\s+code)?|code|error)[\s:=]*$/i;
const AFTER = new RegExp(`^\\s*(?:${REASON}|errors?\\b|response\\b|status\\b)`, "i");

export function isTechnicalFalsePositive(text: string, span: Span): boolean {
  const value = span.text.trim();
  if (span.label === "STREET_NAME" && REASON_ONLY.test(value)) return true;
  if ((span.label === "BUILDING_NUMBER" || span.label === "STREET_NAME") && STATUS_CODE.test(value)) {
    return BEFORE.test(text.slice(Math.max(0, span.start - 24), span.start)) || AFTER.test(text.slice(span.end, span.end + 40));
  }
  return false;
}

export function dropTechnicalFalsePositives(text: string, spans: readonly Span[]): Span[] {
  return spans.filter((span) => !isTechnicalFalsePositive(text, span));
}
