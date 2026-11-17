// Local-only E2E driver (not committed): an MCP client that launches the built server over stdio, like Codex/Claude do.
//   node .e2e.mjs tools | prompt '<args>' | call <tool> '<json args>'
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
const [what, a, b] = process.argv.slice(2);
const transport = new StdioClientTransport({ command: "node", args: [new URL("./dist/cli.js", import.meta.url).pathname],
  env: { ...process.env }, cwd: process.env.E2E_CWD ?? process.cwd() });
const client = new Client({ name: "e2e", version: "0" });
await client.connect(transport);
if (what === "tools") console.log((await client.listTools()).tools.map((t) => t.name).join(" "));
else if (what === "prompt") console.log((await client.getPrompt({ name: "crowdcode", arguments: a ? { args: a } : {} })).messages[0].content.text);
else {
  const r = await client.callTool({ name: a, arguments: JSON.parse(b ?? "{}") }, undefined, { timeout: 900_000 });
  console.log(r.content[0].text);
}
await client.close();
