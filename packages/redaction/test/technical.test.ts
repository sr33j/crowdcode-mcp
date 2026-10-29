import type { Span } from "@nationaldesignstudio/rampart";
import { describe, expect, it } from "vitest";
import { isTechnicalFalsePositive } from "../src/technical.js";

function span(text: string, value: string, label: Span["label"]): [string, Span] {
  const start = text.indexOf(value);
  return [text, { start, end: start + value.length, label, score: 0.99, source: "ner", text: value }];
}

describe("isTechnicalFalsePositive", () => {
  it("drops HTTP reason phrases read as street names", () => {
    expect(isTechnicalFalsePositive(...span("got 502 Bad Gateway", "Bad Gateway", "STREET_NAME"))).toBe(true);
    expect(isTechnicalFalsePositive(...span("503 Service  Unavailable", "Service  Unavailable", "STREET_NAME"))).toBe(true);
  });

  it("drops status codes read as building numbers in HTTP context", () => {
    expect(isTechnicalFalsePositive(...span("origin returned HTTP 530", "530", "BUILDING_NUMBER"))).toBe(true);
    expect(isTechnicalFalsePositive(...span("then 502 Bad Gateway", "502", "BUILDING_NUMBER"))).toBe(true);
    expect(isTechnicalFalsePositive(...span("status code: 429", "429", "BUILDING_NUMBER"))).toBe(true);
    expect(isTechnicalFalsePositive(...span("a 500 error twice", "500", "BUILDING_NUMBER"))).toBe(true);
  });

  it("keeps real address components", () => {
    expect(isTechnicalFalsePositive(...span("lives at 42 Wallaby Way", "42", "BUILDING_NUMBER"))).toBe(false);
    expect(isTechnicalFalsePositive(...span("ship to 502 Main Street", "502", "BUILDING_NUMBER"))).toBe(false);
    expect(isTechnicalFalsePositive(...span("ship to 502 Main Street", "Main Street", "STREET_NAME"))).toBe(false);
    expect(isTechnicalFalsePositive(...span("1600 Pennsylvania Avenue", "1600", "BUILDING_NUMBER"))).toBe(false);
    expect(isTechnicalFalsePositive(...span("221B Baker Street", "221B", "BUILDING_NUMBER"))).toBe(false);
  });

  it("never drops names or identifiers", () => {
    expect(isTechnicalFalsePositive(...span("HTTP 404 from John", "John", "GIVEN_NAME"))).toBe(false);
    expect(isTechnicalFalsePositive(...span("error 404 passport X1234567", "X1234567", "PASSPORT"))).toBe(false);
  });
});
