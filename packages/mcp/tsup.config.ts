import { defineConfig } from "tsup";

export default defineConfig({
  entry: ["src/cli.ts", "src/index.ts"],
  format: ["esm"],
  target: "node20",
  platform: "node",
  sourcemap: true,
  clean: true,
  // @crowdcode/redaction is this repository's own workspace package: bundle it,
  // so installing crowdcode-mcp never depends on that package's npm release.
  // Its runtime dependencies are declared here instead: Rampart, and
  // @huggingface/transformers as an *optional* dependency. transformers pulls
  // onnxruntime-node, whose install script downloads native binaries; when
  // that fails (on 2026-11-17 its v1.21.0 CUDA asset started answering 404 on
  // Linux x64), npm drops the optional subtree and the install still succeeds.
  // The engine then redacts with the deterministic layers only.
  // Those dependencies stay external: onnxruntime-node ships native binaries
  // that cannot be bundled.
  noExternal: ["@crowdcode/redaction"],
  // tsup externalizes `dependencies` but not `optionalDependencies`.
  external: ["@huggingface/transformers"],
  dts: false,
});
