import { defineConfig } from "tsup";

export default defineConfig({
  entry: ["src/cli.ts", "src/index.ts"],
  format: ["esm"],
  target: "node20",
  platform: "node",
  sourcemap: true,
  clean: true,
  // Dependencies stay external: rampart -> @huggingface/transformers ->
  // onnxruntime-node ships native binaries that cannot be bundled.
  dts: false,
});
