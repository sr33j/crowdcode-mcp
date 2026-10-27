import { describe, expect, it } from "vitest";
import { z } from "zod";
import { REDACTION_POLICY } from "../src/redaction/policy.js";
import { requestServiceShape } from "../src/schemas.js";

const schema = z.object(requestServiceShape);
const good = {
  input: "a public MP4 URL (up to 60 s) and the post text",
  output: "the published post ID and URL, posted exactly once",
  problem: "agents publishing video to X fail on chunked uploads",
  price_per_call: 0.05,
};

describe("request_service v2 schema", () => {
  it("requires the four concrete fields", () => {
    expect(schema.safeParse(good).success).toBe(true);
    for (const field of ["input", "output", "problem", "price_per_call"] as const) {
      const { [field]: _omitted, ...rest } = good;
      expect(schema.safeParse(rest).success).toBe(false);
    }
  });

  it("rejects vague text and impossible prices", () => {
    expect(schema.safeParse({ ...good, output: "results" }).success).toBe(false);
    expect(schema.safeParse({ ...good, price_per_call: 0 }).success).toBe(false);
    expect(schema.safeParse({ ...good, price_per_call: 500 }).success).toBe(false);
    expect(schema.safeParse({ ...good, price_per_call: "0.05" }).success).toBe(false);
  });

  it("no longer accepts the v1 free-text field", () => {
    expect(schema.safeParse({ service_description: "OCR service for scanned reports" }).success).toBe(false);
  });

  it("redacts every free-text field locally", () => {
    expect(REDACTION_POLICY.request_service).toEqual(["input", "output", "problem"]);
  });
});
