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
  // Mirrors the backend: a service endpoint is callable over HTTP. Anything
  // else is an injection vector once stored and rendered as a link.
  if (scheme !== "http" && scheme !== "https")
    throw new Error("api_endpoint must be an http or https URL");
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

/**
 * Route templates (`crowdcode.route.v1`, SRI-135): record ids in a path are
 * written `{id}`, so every job of an async service is one listing. Mirrors
 * identity.py `ID_SEGMENT` / `listing_endpoint`; spec/review-payload-vectors.json
 * (`listing_endpoint`) pins both.
 */
export const ID_SEGMENT = new RegExp([
  "^(?:[0-9]{4,}",
  "|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
  "|(?:0x)?[0-9a-fA-F]{16,}",
  "|(?=[A-Za-z0-9_-]*[a-z])(?=[A-Za-z0-9_-]*[A-Z])(?=(?:[A-Za-z_-]*[0-9]){3})[A-Za-z0-9_-]{24,}",
  "|(?=[A-Za-z0-9_-]*[0-9])[A-Za-z0-9_-]{64,})$",
].join(""));
export function templatePath(path: string): string {
  return path.split("/").map((segment) => (segment && ID_SEGMENT.test(segment) ? "{id}" : segment)).join("/");
}
/** The endpoint a listing is filed under: a normalized endpoint with its record ids written `{id}`. */
export function listingEndpoint(normalized: string | null): string | null {
  if (normalized === null) return null;
  const start = normalized.indexOf("/", normalized.indexOf("://") + 3);
  if (start === -1) return normalized;
  return normalized.slice(0, start) + templatePath(normalized.slice(start));
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
    api_endpoint: listingEndpoint(normalizeApiEndpoint(args.api_endpoint)),
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
