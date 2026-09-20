/** Domain-separated, expiring authorization; never sign a backend-supplied message. */
export function managementMessage(args: {
  action: "list" | "delete"; wallet: string; expires_at: number;
  review_id?: number; before_id?: number; limit?: number;
}): string {
  return ["CrowdCode review management v1", `action:${args.action}`,
    `wallet:${args.wallet.toLowerCase()}`, `review_id:${args.review_id ?? 0}`,
    `before_id:${args.before_id ?? 0}`, `limit:${args.limit ?? 0}`,
    `expires_at:${args.expires_at}`].join("\n");
}
