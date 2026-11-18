/**
 * spec/agent-tools.json, the contract every CrowdCode agent client follows
 * (names, descriptions, argument schemas and result fields). It is bundled
 * into dist by the import below, so the published package carries the exact
 * file its tests checked.
 *
 * The spec's descriptions were written from the Hermes plugin. Where one
 * names something that exists only in Hermes (its credential store, its
 * approval prompt for folders outside the session), this host says what it
 * does instead; `HOST_DESCRIPTIONS` is the whole list, and
 * test/agents-contract.test.ts checks every other description is the spec's.
 */

import { z, type ZodTypeAny } from "zod";
import contract from "../../../../spec/agent-tools.json" with { type: "json" };

export interface JsonProperty {
  type: "string" | "number" | "integer" | "boolean" | "array";
  description?: string;
  enum?: string[];
  items?: { type: "string" };
}

export interface ToolContract {
  name: string;
  description: string;
  input_schema: { type: "object"; properties: Record<string, JsonProperty>; required: string[] };
  result_fields: string[];
  result_shapes?: Record<string, string[]>;
}

export const CONTRACT = contract as unknown as { version: number; tools: ToolContract[] };

export const AGENT_TOOL_NAMES = [
  "crowdcode_wallet",
  "crowdcode_agents",
  "crowdcode_create_agent",
  "crowdcode_fund_agent",
  "crowdcode_run_task",
  "crowdcode_task_status",
  "crowdcode_task_events",
  "crowdcode_services",
  "crowdcode_withdraw",
  "crowdcode_transfer",
  "crowdcode_demand",
  "crowdcode_files",
  "crowdcode_download",
  "crowdcode_claim",
] as const;
export type AgentToolName = (typeof AGENT_TOOL_NAMES)[number];

/** Tool and argument descriptions that differ from the spec in this host, and why (see the header). */
export const HOST_DESCRIPTIONS: Partial<Record<AgentToolName, { tool?: string; args?: Record<string, string> }>> = {
  crowdcode_wallet: {
    tool:
      "Show how this machine reaches CrowdCode. In wallet mode: its Base address (the local AgentCash wallet, " +
      "~/.agentcash/wallet.json, the same key that signs CrowdCode reviews), USDC balance (one read-only Base RPC " +
      "call) and how to fund it. In account mode (signed in with `npx crowdcode-mcp login`): the account's main " +
      "wallet and what the sign-in allows. With init=true, create the wallet first if there is none (the key stays " +
      "in that file and is never shown).",
  },
  crowdcode_fund_agent: {
    tool:
      "Send USDC on Base from this wallet to one of its agents. Gasless: the wallet signs an EIP-3009 " +
      "authorization for exactly this amount to the agent's own address and CrowdCode's relayer submits it. " +
      "Minimum $0.50. In account mode nothing moves from here: it returns the crowdcode.app link where the owner " +
      "approves the deposit from their main wallet; give the user that link.",
  },
  crowdcode_withdraw: {
    tool:
      "Withdraw USDC from an agent. It can only go to the agent's owner, never anywhere else: this wallet, or in " +
      "account mode the account's main wallet. Minimum $0.50.",
  },
  crowdcode_download: {
    args: {
      dest:
        "Folder to save into, relative to the session's working directory or absolute (created if missing). " +
        "Default ./crowdcode-files/<agent name>/.",
    },
  },
};

export function toolContract(name: AgentToolName): ToolContract {
  const tool = CONTRACT.tools.find((t) => t.name === name);
  if (!tool) throw new Error(`spec/agent-tools.json has no tool ${name}`);
  return tool;
}

export function hostDescription(name: AgentToolName): string {
  return HOST_DESCRIPTIONS[name]?.tool ?? toolContract(name).description;
}

function zodFor(property: JsonProperty): ZodTypeAny {
  switch (property.type) {
    case "string":
      return property.enum && property.enum.length > 0
        ? z.enum(property.enum as [string, ...string[]])
        : z.string();
    case "number":
      return z.number();
    case "integer":
      return z.number().int();
    case "boolean":
      return z.boolean();
    case "array":
      return z.array(z.string());
    default:
      throw new Error(`unsupported argument type ${(property as JsonProperty).type}`);
  }
}

/** The MCP argument shape of a tool, from its JSON schema in the spec. */
export function zodShape(name: AgentToolName): Record<string, ZodTypeAny> {
  const tool = toolContract(name);
  const required = new Set(tool.input_schema.required ?? []);
  const overrides = HOST_DESCRIPTIONS[name]?.args ?? {};
  const shape: Record<string, ZodTypeAny> = {};
  for (const [key, property] of Object.entries(tool.input_schema.properties)) {
    let schema = zodFor(property);
    const description = overrides[key] ?? property.description;
    if (description) schema = schema.describe(description);
    shape[key] = required.has(key) ? schema : schema.optional();
  }
  return shape;
}
