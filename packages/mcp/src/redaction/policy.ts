/**
 * Which tool arguments get redacted before leaving the machine.
 *
 * Only free-text fields are listed. Identity fields (api_endpoint,
 * payment_target_ref, directory_slug, service_id, service_name) and payment
 * material (payment_reference, payment_proof, reviewer_wallet,
 * review_signature) pass through untouched — redacting them would break
 * service resolution and payment verification, and they are minimized
 * server-side instead.
 */

import type { RedactionEngine } from "@crowdcode/redaction";

export const REDACTION_POLICY: Record<string, readonly string[]> = {
  request_service: ["service_description", "task_context"],
  review_service: ["reason", "task_context"],
};

export interface RedactedArgs {
  args: Record<string, unknown>;
  entitiesRemoved: number;
  modelActive: boolean;
}

export async function redactArgs(
  engine: RedactionEngine,
  tool: string,
  args: Record<string, unknown>,
): Promise<RedactedArgs> {
  const fields = REDACTION_POLICY[tool] ?? [];
  const out = { ...args };
  let entitiesRemoved = 0;
  let modelActive = engine.modelActive;
  for (const field of fields) {
    const value = out[field];
    if (typeof value !== "string" || value === "") continue;
    const result = await engine.redact(value);
    out[field] = result.text;
    entitiesRemoved += result.entitiesRemoved;
    modelActive = result.modelActive;
  }
  return { args: out, entitiesRemoved, modelActive };
}
