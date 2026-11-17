import { defineConfig } from "tsup";

export default defineConfig({
  entry: ["src/cli.ts", "src/index.ts"],
  format: ["esm"],
  target: "node20",
  platform: "node",
  sourcemap: true,
  clean: true,
  // @crowdcode/redaction (this repository's own workspace) is bundled, so an
  // install never resolves its published dependency range: 0.1.0 asks for
  // @huggingface/transformers ^3, whose onnxruntime-node 1.21 install script
  // downloads a GPU tarball GitHub stopped serving (404) on 2026-11-17, which
  // broke every fresh Linux install. Its own dependencies stay external:
  // rampart -> @huggingface/transformers -> onnxruntime-node ships native
  // binaries that cannot be bundled, and crowdcode-mcp depends on them directly.
  noExternal: ["@crowdcode/redaction"],
  dts: false,
});
