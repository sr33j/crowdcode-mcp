/**
 * Local implementation of get_review_signing_payload.
 *
 * This replaces the remote tool so raw review text is never sent at signing
 * time: the reason is redacted locally, hashed locally, and the canonical
 * EIP-191 message is constructed locally (spec/CANONICAL_PAYLOAD.md).
 *
 * The identity-merge logic mirrors the backend exactly
 * (src/crowdcode/server.py review_service effective_identity): the server
 * verifies signatures by REBUILDING the payload from its own resolved
 * identity, so the message signed here must match that reconstruction
 * byte-for-byte. Canonical identity comes from get_service_score when the
 * service exists; otherwise the caller-provided identity is used verbatim,
 * exactly like the old remote signing tool.
 */

import type { RedactionResult } from "@crowdcode/redaction";
import { buildIdentity, type ServiceIdentity } from "../canonical/identity.js";
import { canonicalReviewPayload } from "../canonical/payload.js";
import type { Upstream } from "../upstream.js";

export interface SigningPayloadArgs {
  rating: number;
  reason: string;
  payment_reference: string;
  service_id?: string | null;
  api_endpoint?: string | null;
  payment_provider?: string | null;
  payment_target_ref?: string | null;
  directory_slug?: string | null;
}

export interface SigningDeps {
  redact(text: string): Promise<RedactionResult>;
  upstream: Pick<Upstream, "call">;
}

function orNull(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

export async function getReviewSigningPayload(
  deps: SigningDeps,
  args: SigningPayloadArgs,
): Promise<Record<string, unknown>> {
  let identity: ServiceIdentity;
  try {
    identity = buildIdentity(args);
  } catch (err) {
    return { ok: false, reason: (err as Error).message };
  }

  const redacted = await deps.redact(args.reason);

  let score: Record<string, unknown>;
  try {
    score = await deps.upstream.call("get_service_score", {
      service_id: identity.service_id,
      api_endpoint: identity.api_endpoint,
      payment_provider: identity.payment_provider,
      payment_target_ref: identity.payment_target_ref,
      directory_slug: identity.directory_slug,
    });
  } catch (err) {
    return {
      ok: false,
      reason: `could not reach the CrowdCode backend to resolve the service: ${
        (err as Error).message
      }`,
    };
  }

  let effective = identity;
  if (score.found === true) {
    try {
      // Same merge as the backend: caller-normalized fields win, canonical
      // values fill the gaps, and the result is re-normalized.
      effective = buildIdentity({
        service_id: orNull(score.service_id),
        service_name: orNull(score.service_name),
        api_endpoint: identity.api_endpoint ?? orNull(score.canonical_endpoint),
        payment_provider:
          identity.payment_provider ?? orNull(score.payment_provider),
        payment_target_ref:
          identity.payment_target_ref ?? orNull(score.payment_target_ref),
        directory_slug: identity.directory_slug ?? orNull(score.directory_slug),
      });
    } catch (err) {
      return { ok: false, reason: (err as Error).message };
    }
  } else if (
    typeof score.reason === "string" &&
    score.reason !== "service not found"
  ) {
    // e.g. "service identity conflict" — same failure the remote tool returned.
    return { ok: false, reason: score.reason };
  }

  const message = canonicalReviewPayload({
    identity: effective,
    rating: args.rating,
    reason: redacted.text,
    paymentReference: args.payment_reference,
  });

  return {
    ok: true,
    signature_scheme: "eip191",
    message,
    reason: redacted.text,
    identity: {
      service_id: effective.service_id,
      api_endpoint: effective.api_endpoint,
      payment_provider: effective.payment_provider,
      payment_target_ref: effective.payment_target_ref,
      directory_slug: effective.directory_slug,
    },
    instructions:
      "Sign `message` with the reviewer wallet (EIP-191 personal_sign). Then " +
      "call review_service in this same session, passing this exact `reason` " +
      "string and every field of `identity` verbatim, plus the same rating " +
      "and payment_reference.",
    _redaction: {
      entities_removed: redacted.entitiesRemoved,
      model_active: redacted.modelActive,
    },
  };
}
