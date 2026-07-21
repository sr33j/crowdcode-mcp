import { describe, expect, it } from "vitest";
import { SecretRedactor } from "../src/secrets.js";

function redactOne(text: string): string {
  return new SecretRedactor().redact(text).text;
}

describe("SecretRedactor", () => {
  it("redacts OpenAI/Anthropic style keys", () => {
    expect(redactOne("key sk-abcdefghij0123456789 ok")).toBe(
      "key [API_KEY_1] ok",
    );
    expect(redactOne("sk-ant-api03-abcdefghij0123456789")).toBe("[API_KEY_1]");
  });

  it("leaves short sk- prefixes alone (near miss)", () => {
    expect(redactOne("task sk-shortid done")).toBe("task sk-shortid done");
  });

  it("redacts AWS access key ids", () => {
    expect(redactOne("AKIAIOSFODNN7EXAMPLE")).toBe("[API_KEY_1]");
    expect(redactOne("akiaiosfodnn7example")).toBe("akiaiosfodnn7example");
  });

  it("redacts GitHub tokens", () => {
    expect(
      redactOne("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"),
    ).toBe("[API_KEY_1]");
    expect(
      redactOne("github_pat_11ABCDEFGHIJKLMNOPQRSTUV_wxyz0123456789"),
    ).toBe("[API_KEY_1]");
  });

  it("redacts Slack and Stripe keys", () => {
    expect(redactOne("xoxb-123456789012-abcdef")).toBe("[API_KEY_1]");
    expect(redactOne("sk_live_abcdefghij0123456789")).toBe("[API_KEY_1]");
    expect(redactOne("rk_test_abcdefghij0123456789")).toBe("[API_KEY_1]");
  });

  it("redacts JWTs and bearer tokens", () => {
    const jwt =
      "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U";
    expect(redactOne(`token ${jwt}`)).toBe("token [TOKEN_1]");
    expect(redactOne("Authorization: Bearer abcdef0123456789abcdef")).toBe(
      "Authorization: [TOKEN_1]",
    );
  });

  it("redacts PEM private key blocks", () => {
    const pem =
      "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----";
    expect(redactOne(`cfg ${pem} end`)).toBe("cfg [PRIVATE_KEY_1] end");
  });

  it("redacts connection strings with credentials", () => {
    expect(
      redactOne("db postgres://admin:hunter2@db.internal:5432/prod down"),
    ).toBe("db [CREDENTIAL_URL_1] down");
    expect(redactOne("visit https://example.com/path ok")).toBe(
      "visit https://example.com/path ok",
    );
  });

  it("assigns stable placeholders for repeated values and counts finds", () => {
    const redactor = new SecretRedactor();
    const first = redactor.redact("sk_live_abcdefghij0123456789");
    const second = redactor.redact(
      "again sk_live_abcdefghij0123456789 and xoxb-123456789012-abcdef",
    );
    expect(first.text).toBe("[API_KEY_1]");
    expect(second.text).toBe("again [API_KEY_1] and [API_KEY_2]");
    expect(second.found).toBe(2);
  });

  it("reveal restores raw values", () => {
    const redactor = new SecretRedactor();
    const raw = "key sk_live_abcdefghij0123456789";
    const { text } = redactor.redact(raw);
    expect(redactor.reveal(text)).toBe(raw);
  });
});
