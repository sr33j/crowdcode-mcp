export interface RedactionInfo {
  entitiesRemoved: number;
  modelActive: boolean;
}

/** Attach the local-redaction attestation so agents (and users reading
 *  transcripts) can see that redaction ran and at what coverage level. */
export function withRedactionAttestation(
  payload: Record<string, unknown>,
  info: RedactionInfo,
): Record<string, unknown> {
  return {
    ...payload,
    _redaction: {
      entities_removed: info.entitiesRemoved,
      model_active: info.modelActive,
    },
  };
}

export function toToolResult(payload: Record<string, unknown>): {
  content: Array<{ type: "text"; text: string }>;
} {
  return { content: [{ type: "text", text: JSON.stringify(payload) }] };
}
