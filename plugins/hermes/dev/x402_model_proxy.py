"""Dev-only: an OpenAI-compatible endpoint for Hermes that pays for each model call with x402.

Not part of the plugin (Hermes never loads this file). It lets you run Hermes on
a model with no API key at all: every chat completion is bought from CrowdCode's
Venice service (``/s/venice-proxy/chat/completions``) with an x402 ``exact``
payment in USDC on Base, signed by a wallet you fund with a little USDC.

    X402_PROXY_KEY=0x... python dev/x402_model_proxy.py --port 8790 --max-total-usd 0.50
    hermes config set model.provider custom
    hermes config set model.base_url http://127.0.0.1:8790/v1
    hermes config set model.default zai-org-glm-4.7

Streaming requests are answered as one SSE chunk (the upstream does not stream).
The proxy refuses to spend more than ``--max-total-usd`` in total.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("crowdcode_plugin", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
plugin = importlib.util.module_from_spec(spec)
sys.modules["crowdcode_plugin"] = plugin
spec.loader.exec_module(plugin)
client = sys.modules["crowdcode_plugin.client"]
walletlib = sys.modules["crowdcode_plugin.wallet"]

UPSTREAM = "https://crowdcode.app/s/venice-proxy/chat/completions"
PASS = {"model", "messages", "tools", "tool_choice", "max_tokens", "temperature", "top_p", "stop",
        "response_format", "parallel_tool_calls", "prompt_cache_key"}


class Spend:
    lock = threading.Lock()
    total = 0.0
    calls = 0
    cap = 0.5


def _wallet():
    from eth_account import Account

    return walletlib.Wallet(Account.from_key(os.environ["X402_PROXY_KEY"]))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            return self._json(200, {"object": "list", "data": [
                {"id": m, "object": "model", "owned_by": "venice"} for m in ("zai-org-glm-4.7", "venice-uncensored-1-2")]})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.rstrip("/").endswith("/chat/completions"):
            return self._json(404, {"error": "not found"})
        request = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
        stream = bool(request.get("stream"))
        body = {k: v for k, v in request.items() if k in PASS}
        body["max_tokens"] = min(int(body.get("max_tokens") or 2048), 2048)
        body.setdefault("prompt_cache_key", "hermes-demo")
        api = client.CrowdCode("https://crowdcode.app", _wallet())
        try:
            quote = api.quote(UPSTREAM, body)
            price = walletlib.units_to_usd(quote["offer"]["amount"])
            with Spend.lock:
                if Spend.total + price > Spend.cap + 1e-9:
                    return self._json(402, {"error": {"message": f"proxy spend cap ${Spend.cap:.2f} reached"}})
                Spend.total += price
                Spend.calls += 1
            status, data, settlement = api.pay(UPSTREAM, body, quote)
        except client.ApiError as exc:
            return self._json(502, {"error": {"message": f"upstream {exc.status} {exc.error}", "detail": exc.detail}})
        tx = (settlement or {}).get("transaction", "")
        print(f"[x402] call {Spend.calls}: ${price:.4f} (total ${Spend.total:.4f}) {status} tx={tx}", file=sys.stderr, flush=True)
        if status >= 400:
            return self._json(status, {"error": {"message": str(data)[:500]}})
        if not stream:
            return self._json(200, data)
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        delta = {"role": "assistant", "content": message.get("content") or ""}
        if message.get("tool_calls"):
            delta["tool_calls"] = [{"index": i, **call} for i, call in enumerate(message["tool_calls"])]
        base = {"id": data.get("id") or f"chatcmpl-{uuid.uuid4().hex}", "object": "chat.completion.chunk",
                "created": int(time.time()), "model": data.get("model") or body.get("model")}
        chunks = [{**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                  {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": choice.get("finish_reason") or "stop"}],
                   "usage": data.get("usage")}]
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--max-total-usd", type=float, default=0.5)
    args = parser.parse_args()
    Spend.cap = args.max_total_usd
    print(f"x402 model proxy for {_wallet().address} on http://127.0.0.1:{args.port}/v1 (cap ${Spend.cap:.2f})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
