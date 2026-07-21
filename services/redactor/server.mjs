// CrowdCode redaction sidecar.
//
// Private HTTP service the Python backend calls to redact free text on
// ingest (request_service / review_service writes) and egress
// (get_service_score reviews, project-idea requests before OpenRouter and
// the public frontend). Not exposed to the public internet; optionally
// protected with a shared secret via REDACTOR_TOKEN.
//
//   POST /redact   {"texts": ["...", ...]}
//     -> {"texts": ["...", ...], "entities_removed": n, "model_active": bool}
//   GET  /healthz  -> {"ok": true, "model_active": bool}

import http from "node:http";
import { homedir } from "node:os";
import { join } from "node:path";
import { RedactionEngine } from "@crowdcode/redaction";

const PORT = Number(process.env.PORT ?? 8090);
const HOST = process.env.HOST ?? "127.0.0.1";
const TOKEN = process.env.REDACTOR_TOKEN ?? null;
const CACHE_DIR =
  process.env.CROWDCODE_CACHE_DIR ?? join(homedir(), ".cache", "crowdcode-redactor");
const MAX_BODY_BYTES = 2 * 1024 * 1024;

const engine = await RedactionEngine.create({
  cacheDir: CACHE_DIR,
  enableModel: process.env.CROWDCODE_DISABLE_MODEL !== "1",
});

function respond(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    "content-type": "application/json",
    "content-length": Buffer.byteLength(body),
  });
  res.end(body);
}

async function readBody(req) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > MAX_BODY_BYTES) throw new Error("body too large");
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString("utf8");
}

const server = http.createServer(async (req, res) => {
  try {
    if (req.method === "GET" && req.url === "/healthz") {
      respond(res, 200, { ok: true, model_active: engine.modelActive });
      return;
    }
    if (req.method !== "POST" || req.url !== "/redact") {
      respond(res, 404, { ok: false, error: "not found" });
      return;
    }
    if (TOKEN !== null && req.headers["x-redactor-token"] !== TOKEN) {
      respond(res, 401, { ok: false, error: "unauthorized" });
      return;
    }

    const parsed = JSON.parse(await readBody(req));
    if (!Array.isArray(parsed.texts) || !parsed.texts.every((t) => typeof t === "string")) {
      respond(res, 400, { ok: false, error: "texts must be an array of strings" });
      return;
    }

    let entitiesRemoved = 0;
    let modelActive = engine.modelActive;
    const texts = [];
    for (const text of parsed.texts) {
      if (text === "") {
        texts.push("");
        continue;
      }
      const result = await engine.redact(text);
      texts.push(result.text);
      entitiesRemoved += result.entitiesRemoved;
      modelActive = result.modelActive;
    }
    respond(res, 200, {
      texts,
      entities_removed: entitiesRemoved,
      model_active: modelActive,
    });
  } catch (err) {
    respond(res, 500, { ok: false, error: String(err?.message ?? err) });
  }
});

server.listen(PORT, HOST, () => {
  console.error(`crowdcode-redactor listening on ${HOST}:${PORT}`);
});
