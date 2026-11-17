/** Tool results shaped exactly like AgentCash 0.17's `fetch` (mcpSuccessResponse:
 * the parsed body, then the payment metadata with every response header). */
export const TX = "0x" + "ab".repeat(32);
export const PAYER = "0x1111111111111111111111111111111111111111";
export const SECRET_QUERY = "acme merger rumours";
export const SECRET_BODY = "CONFIDENTIAL: the user's private search results about Acme";

const b64 = (value: unknown) => Buffer.from(JSON.stringify(value)).toString("base64");
const b64url = (value: unknown) => Buffer.from(JSON.stringify(value)).toString("base64url");

export function x402Result(overrides: Record<string, unknown> = {}) {
  return [
    { type: "text", text: JSON.stringify({ results: [{ title: SECRET_BODY }] }) },
    { type: "text", text: JSON.stringify({
      protocol: "x402", network: "base", price: "$0.007",
      payment: { success: true, transactionHash: TX },
      headers: {
        "content-type": "application/json",
        "payment-response": b64({ success: true, transaction: TX, network: "eip155:8453", payer: PAYER }),
      },
      ...overrides,
    }) },
  ];
}

export function mppResult(reference = "0x" + "cd".repeat(32)) {
  return [
    { type: "text", text: "plain text answer: " + SECRET_BODY },
    { type: "text", text: JSON.stringify({
      protocol: "mpp", network: "tempo", price: "$0.01",
      payment: { success: true, transactionHash: reference },
      headers: { "payment-receipt": b64url({ method: "tempo", reference, status: "success", timestamp: "2026-11-17T00:00:00Z" }) },
    }) },
  ];
}

export function postToolUse(toolResponse: unknown, toolInput: Record<string, unknown> = {
  url: `https://api.exa.ai/search?q=${encodeURIComponent(SECRET_QUERY)}`, method: "post", body: { query: SECRET_QUERY },
}, toolName = "mcp__agentcash__fetch") {
  return { session_id: "s", transcript_path: "/tmp/none", cwd: "/tmp", hook_event_name: "PostToolUse",
    tool_name: toolName, tool_input: toolInput, tool_response: toolResponse };
}
