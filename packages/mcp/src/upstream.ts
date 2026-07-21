import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

/** A tool-level failure reported by the backend (not a transport problem). */
export class UpstreamError extends Error {}

export interface Upstream {
  call(
    name: string,
    args: Record<string, unknown>,
  ): Promise<Record<string, unknown>>;
  listToolNames(): Promise<string[]>;
}

export class UpstreamClient implements Upstream {
  private client: Client | null = null;

  constructor(
    private readonly baseUrl: string,
    private readonly timeoutMs: number,
  ) {}

  private async connect(): Promise<Client> {
    if (this.client !== null) return this.client;
    const client = new Client({ name: "crowdcode-mcp-local", version: "0.1.0" });
    const transport = new StreamableHTTPClientTransport(new URL(this.baseUrl));
    await client.connect(transport);
    this.client = client;
    return client;
  }

  private async reset(): Promise<void> {
    const client = this.client;
    this.client = null;
    if (client !== null) {
      try {
        await client.close();
      } catch {
        // already broken
      }
    }
  }

  async call(
    name: string,
    args: Record<string, unknown>,
  ): Promise<Record<string, unknown>> {
    try {
      return await this.callOnce(name, args);
    } catch (err) {
      if (err instanceof UpstreamError) throw err;
      // Transport-level failure: reconnect once (absorbs dropped sessions and
      // Render cold starts).
      await this.reset();
      return this.callOnce(name, args);
    }
  }

  private async callOnce(
    name: string,
    args: Record<string, unknown>,
  ): Promise<Record<string, unknown>> {
    const client = await this.connect();
    const result = await client.callTool({ name, arguments: args }, undefined, {
      timeout: this.timeoutMs,
    });

    if (result.isError) {
      throw new UpstreamError(
        `backend tool ${name} failed: ${firstText(result.content)}`,
      );
    }
    const structured = result.structuredContent;
    if (structured !== undefined && isRecord(structured)) {
      return structured;
    }
    const text = firstText(result.content);
    try {
      const parsed: unknown = JSON.parse(text);
      if (isRecord(parsed)) return parsed;
    } catch {
      // fall through
    }
    throw new UpstreamError(
      `backend tool ${name} returned an unparseable result: ${text.slice(0, 200)}`,
    );
  }

  async listToolNames(): Promise<string[]> {
    const client = await this.connect();
    const { tools } = await client.listTools();
    return tools.map((tool) => tool.name);
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function firstText(content: unknown): string {
  if (Array.isArray(content)) {
    for (const item of content) {
      if (
        isRecord(item) &&
        item.type === "text" &&
        typeof item.text === "string"
      ) {
        return item.text;
      }
    }
  }
  return "";
}
