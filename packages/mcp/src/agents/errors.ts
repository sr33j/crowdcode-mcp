/**
 * The error kinds the agent tools turn into `{ok: false, error, hint?}` results.
 * They mirror the Hermes plugin: ValueError/RuntimeError (ToolError),
 * WalletMissing, NotSignedIn and the API's own ApiError (in client.ts).
 */

/** A refused argument or a definite, explained failure. Its message is shown as is. */
export class ToolError extends Error {
  override name = "ToolError";
}

/** A money-moving request whose outcome is unknown (sent, but no definite answer). */
export class UncertainError extends ToolError {
  override name = "UncertainError";
}

/** No usable local wallet (wallet mode, claims, creation). */
export class WalletMissing extends Error {
  override name = "WalletMissing";
}

/** No usable CrowdCode account sign-in (account mode). */
export class NotSignedIn extends Error {
  override name = "NotSignedIn";
}

export const LOGIN_COMMAND = "npx crowdcode-mcp login";
export const WALLET_INIT_COMMAND = "npx crowdcode-mcp wallet init";
