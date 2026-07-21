/**
 * Local-first redaction pipeline: secret recognizers + Rampart.
 *
 * - The deterministic layers (secret regexes + Rampart heuristics: emails,
 *   cards, SSNs, IPs, URLs) are always active from the first call.
 * - The Rampart ONNX token-classification model (names, phones, addresses)
 *   initializes lazily in the background; until it is ready — or if it fails
 *   to download — results are deterministic-only and report
 *   `modelActive: false`.
 * - `redact()` is memoized per exact input string for the process lifetime.
 *   This is load-bearing: get_review_signing_payload hashes the redacted
 *   reason, and review_service must later forward the byte-identical string
 *   even if the model finished loading between the two calls.
 * - All placeholder session tables live in memory only. Nothing raw is ever
 *   logged, persisted, or transmitted.
 */

import { SecretRedactor } from "./secrets.js";

export interface RedactionResult {
  text: string;
  entitiesRemoved: number;
  modelActive: boolean;
}

interface Guard {
  protect(text: string): Promise<{ text: string; placeholders?: unknown[] }>;
  reveal(text: string): string | Promise<string>;
}

export interface EngineOptions {
  cacheDir: string;
  enableModel: boolean;
  log?: (message: string) => void;
}

export class RedactionEngine {
  private readonly secrets = new SecretRedactor();
  private readonly memo = new Map<string, RedactionResult>();
  private heuristicGuard: Guard;
  private fullGuard: Guard | null = null;
  private modelInit: Promise<boolean> | null = null;
  private readonly log: (message: string) => void;

  private constructor(heuristicGuard: Guard, opts: EngineOptions) {
    this.heuristicGuard = heuristicGuard;
    this.log = opts.log ?? ((m) => process.stderr.write(m + "\n"));
  }

  static async create(opts: EngineOptions): Promise<RedactionEngine> {
    // Dynamic imports so cli.ts can patch stdout logging before any
    // transformers/onnxruntime code loads (stdout carries the MCP protocol).
    const { createGuard } = await import("@nationaldesignstudio/rampart");
    const heuristic = (await createGuard({ heuristicsOnly: true })) as Guard;
    const engine = new RedactionEngine(heuristic, opts);

    if (opts.enableModel) {
      engine.modelInit = (async () => {
        try {
          const transformers = await import("@huggingface/transformers");
          transformers.env.cacheDir = opts.cacheDir;
          const full = (await createGuard({ device: "cpu" })) as Guard;
          engine.fullGuard = full;
          return true;
        } catch (err) {
          engine.log(
            `crowdcode-mcp: PII model unavailable, running deterministic-only (${
              (err as Error).message
            })`,
          );
          return false;
        }
      })();
    }
    return engine;
  }

  get modelActive(): boolean {
    return this.fullGuard !== null;
  }

  /** Await background model initialization (used by the check CLI). */
  async waitForModel(): Promise<boolean> {
    if (this.modelInit === null) return false;
    return this.modelInit;
  }

  async redact(text: string): Promise<RedactionResult> {
    const memoized = this.memo.get(text);
    if (memoized !== undefined) return memoized;

    const secretPass = this.secrets.redact(text);
    const guard = this.fullGuard ?? this.heuristicGuard;
    const protectedResult = await guard.protect(secretPass.text);
    const entities = Array.isArray(protectedResult.placeholders)
      ? protectedResult.placeholders.length
      : 0;

    const result: RedactionResult = {
      text: protectedResult.text,
      entitiesRemoved: secretPass.found + entities,
      modelActive: this.modelActive,
    };
    this.memo.set(text, result);
    return result;
  }

  /** Restore raw values — used only by the `check` CLI round-trip demo. */
  async reveal(text: string): Promise<string> {
    const guard = this.fullGuard ?? this.heuristicGuard;
    const revealed = await guard.reveal(text);
    return this.secrets.reveal(revealed);
  }
}
