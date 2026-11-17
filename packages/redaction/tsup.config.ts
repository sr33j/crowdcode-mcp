import { defineConfig } from "tsup";

export default defineConfig({
  entry: ["src/index.ts"],
  format: ["esm"],
  target: "node20",
  platform: "node",
  sourcemap: true,
  clean: true,
  // An optional dependency (onnxruntime-node's install can fail): tsup only
  // externalizes `dependencies`, so name it, or it would be bundled and fail
  // at runtime ("Dynamic require of child_process is not supported").
  external: ["@huggingface/transformers"],
  // Consumers (packages/mcp, services/redactor) typecheck against these.
  dts: true,
});
