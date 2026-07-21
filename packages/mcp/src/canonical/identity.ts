/**
 * Byte-for-byte port of src/crowdcode/identity.py (normative source).
 * Conformance is enforced against spec/review-payload-vectors.json; any
 * behavior difference from the Python implementation is a bug here.
 */

import { createHash } from "node:crypto";
import { pyStrip } from "./pystrip.js";

export const PAYMENT_PROVIDERS = new Set([
  "stripe",
  "stripe_payment_link",
  "mppx",
  "x402",
  "manual",
]);

const PAYMENT_PROVIDER_ALIASES: Record<string, string> = {
  link: "stripe_payment_link",
  stripe_link: "stripe_payment_link",
  payment_link: "stripe_payment_link",
  mpp: "mppx",
};

export interface ServiceIdentity {
  service_id: string | null;
  service_name: string | null;
  api_endpoint: string | null;
  payment_provider: string | null;
  payment_target_ref: string | null;
  directory_slug: string | null;
}

export function cleanOptional(value: string | null | undefined): string | null {
  if (value === null || value === undefined) return null;
  const cleaned = pyStrip(value);
  return cleaned === "" ? null : cleaned;
}

export function normalizePaymentProvider(
  value: string | null | undefined,
): string | null {
  const cleaned = cleanOptional(value);
  if (cleaned === null) return null;
  let provider = cleaned.toLowerCase().replaceAll("-", "_");
  provider = PAYMENT_PROVIDER_ALIASES[provider] ?? provider;
  if (!PAYMENT_PROVIDERS.has(provider)) {
    throw new Error(
      "payment_provider must be one of: " +
        [...PAYMENT_PROVIDERS].sort().join(", "),
    );
  }
  return provider;
}

// Mirrors Python urlsplit for the subset of URLs the backend accepts.
// Deliberately NOT the WHATWG URL class: `new URL` drops default ports and
// percent-normalizes paths, which Python's urlsplit does not.
const URL_RE = /^([A-Za-z][A-Za-z0-9+.-]*):\/\/([^/?#]*)([^?#]*)(?:\?[^#]*)?(?:#.*)?$/;

export function normalizeApiEndpoint(
  value: string | null | undefined,
): string | null {
  const cleaned = cleanOptional(value);
  if (cleaned === null) return null;

  const candidate = cleaned.includes("://") ? cleaned : `https://${cleaned}`;
  const match = URL_RE.exec(candidate);
  if (!match) {
    throw new Error("api_endpoint must include a host");
  }
  const scheme = (match[1] ?? "https").toLowerCase();
  const authority = match[2] ?? "";
  const rawPath = match[3] ?? "";

  if (authority === "") {
    throw new Error("api_endpoint must include a host");
  }

  // Userinfo is dropped, exactly like the Python code (it rebuilds netloc
  // from parsed.hostname + parsed.port only).
  const atIndex = authority.lastIndexOf("@");
  const hostPort = atIndex === -1 ? authority : authority.slice(atIndex + 1);

  let host = hostPort;
  let port: string | null = null;
  const colonIndex = hostPort.lastIndexOf(":");
  if (colonIndex !== -1) {
    host = hostPort.slice(0, colonIndex);
    const portStr = hostPort.slice(colonIndex + 1);
    if (portStr !== "") {
      if (!/^\d+$/.test(portStr)) {
        throw new Error(
          `Port could not be cast to integer value as '${portStr}'`,
        );
      }
      const parsed = Number.parseInt(portStr, 10);
      if (parsed > 65535) {
        throw new Error("Port out of range 0-65535");
      }
      port = String(parsed);
    }
  }

  host = host.toLowerCase();
  if (host === "") {
    throw new Error("api_endpoint must include a host");
  }

  const netloc = port === null ? host : `${host}:${port}`;
  const path = rawPath.replace(/\/+$/, "") || "/";
  return `${scheme}://${netloc}${path}`;
}

export function generateServiceId(
  apiEndpoint: string,
  paymentProvider: string,
  paymentTargetRef: string,
): string {
  const material = [
    normalizeApiEndpoint(apiEndpoint) ?? "",
    paymentProvider,
    pyStrip(paymentTargetRef),
  ].join("|");
  return (
    "svc_" +
    createHash("sha256").update(material, "utf8").digest("hex").slice(0, 20)
  );
}

export function buildIdentity(args: {
  service_id?: string | null;
  service_name?: string | null;
  api_endpoint?: string | null;
  payment_provider?: string | null;
  payment_target_ref?: string | null;
  directory_slug?: string | null;
}): ServiceIdentity {
  return {
    service_id: cleanOptional(args.service_id),
    service_name: cleanOptional(args.service_name),
    api_endpoint: normalizeApiEndpoint(args.api_endpoint),
    payment_provider: normalizePaymentProvider(args.payment_provider),
    payment_target_ref: cleanOptional(args.payment_target_ref),
    directory_slug: cleanOptional(args.directory_slug),
  };
}

export function hasStrongIdentity(identity: ServiceIdentity): boolean {
  return Boolean(
    identity.api_endpoint &&
      identity.payment_provider &&
      identity.payment_target_ref,
  );
}
