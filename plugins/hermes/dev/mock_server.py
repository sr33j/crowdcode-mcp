"""A local stand-in for CrowdCode's Wallet API v1, for tests and offline demos.

It checks what the real service checks: EIP-191 request signatures (with the
timestamp window and single-use nonces), x402 ``exact`` payments and EIP-3009
deposit authorizations (signer, amount, destination). No money moves; balances
are numbers in memory. Tasks finish a few seconds after they start, buy one
fake x402 service and, when the prompt mentions a service, "deploy" one.

    python dev/mock_server.py --port 8787
    hermes config set plugins.entries.crowdcode.settings.base_url http://127.0.0.1:8787
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from eth_account import Account
from eth_account.messages import encode_defunct, encode_typed_data

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
TREASURY = "0x000000000000000000000000000000000000c0de"
CREATE_PRICE_UNITS = 350_000  # $0.35
TASK_SECONDS = 3.0


def _dec(value: float) -> str:
    return f"{value:.6f}"


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.nonces: dict[str, float] = {}
        self.agents: dict[str, dict] = {}
        self.tasks: dict[str, dict] = {}
        self.used_auth_nonces: set[str] = set()
        self.wallet_balance: dict[str, float] = {}  # owner address -> fake USDC

    def new_agent(self, owner: str, name: str) -> dict:
        agent_id = "ag_" + secrets.token_hex(5)
        agent = {"id": agent_id, "name": name, "owner": f"eip155:8453:{owner}", "status": "pending",
                 "created": time.time(), "addresses": {"base": Account.create().address,
                                                       "solana": "So1" + secrets.token_hex(20),
                                                       "tempo": Account.create().address},
                 "balances": {"base": 0.0, "solana": 0.0, "tempo": 0.0}, "services": [], "active_task": None}
        self.agents[agent_id] = agent
        return agent


STATE = State()


def _verify_eip3009(authorization: dict, signature: str) -> str:
    typed = {
        "types": {"EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                                   {"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"}],
                  "TransferWithAuthorization": [{"name": "from", "type": "address"}, {"name": "to", "type": "address"},
                                                {"name": "value", "type": "uint256"}, {"name": "validAfter", "type": "uint256"},
                                                {"name": "validBefore", "type": "uint256"}, {"name": "nonce", "type": "bytes32"}]},
        "primaryType": "TransferWithAuthorization",
        "domain": {"name": "USD Coin", "version": "2", "chainId": 8453, "verifyingContract": USDC},
        "message": {"from": authorization["from"], "to": authorization["to"], "value": int(authorization["value"]),
                    "validAfter": int(authorization["validAfter"]), "validBefore": int(authorization["validBefore"]),
                    "nonce": bytes.fromhex(authorization["nonce"][2:])},
    }
    signer = Account.recover_message(encode_typed_data(full_message=typed), signature=signature)
    if signer.lower() != authorization["from"].lower():
        raise ValueError("authorization not signed by from")
    if int(authorization["validBefore"]) < time.time():
        raise ValueError("authorization expired")
    if authorization["nonce"] in STATE.used_auth_nonces:
        raise ValueError("authorization nonce reused")
    STATE.used_auth_nonces.add(authorization["nonce"])
    return signer


class Handler(BaseHTTPRequestHandler):
    server_version = "crowdcode-mock/0.1"

    def log_message(self, *args) -> None:  # quiet
        pass

    def _send(self, status: int, body: object, headers: dict | None = None) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> bytes:
        length = int(self.headers.get("content-length") or 0)
        return self.rfile.read(length) if length else b""

    def do_GET(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        self._route("POST")

    # -- auth --------------------------------------------------------------
    def _owner(self, method: str, raw_path: str, body: bytes) -> str | None:
        h = self.headers
        address, ts, nonce, sig = (h.get("x-crowdcode-address"), h.get("x-crowdcode-timestamp"),
                                   h.get("x-crowdcode-nonce"), h.get("x-crowdcode-signature"))
        if not all((address, ts, nonce, sig)) or not address.lower().startswith("eip155:8453:0x"):
            self._send(401, {"error": "bad_signature"})
            return None
        if abs(time.time() - int(ts)) > 300:
            self._send(401, {"error": "expired"})
            return None
        digest = hashlib.sha256(body).hexdigest() if body else ""
        message = "\n".join(["CrowdCode API request", f"{method} {raw_path}", digest, ts, nonce])
        try:
            signer = Account.recover_message(encode_defunct(text=message), signature=sig)
        except Exception:
            signer = ""
        if signer.lower() != address.split(":")[2].lower():
            self._send(401, {"error": "bad_signature"})
            return None
        with STATE.lock:
            if nonce in STATE.nonces:
                self._send(401, {"error": "replayed"})
                return None
            STATE.nonces[nonce] = time.time()
        return signer

    def _agent(self, owner: str, agent_id: str) -> dict | None:
        agent = STATE.agents.get(agent_id)
        if agent is None:
            self._send(404, {"error": "not_found"})
            return None
        if agent["owner"].split(":")[2].lower() != owner.lower():
            self._send(403, {"error": "not_owner"})
            return None
        return agent

    # -- routes ------------------------------------------------------------
    def _route(self, method: str) -> None:
        raw_path = self.path
        path = urlsplit(raw_path).path.rstrip("/")
        body = self._body()
        if body and "application/json" not in (self.headers.get("content-type") or ""):
            return self._send(415, {"error": "unsupported_media_type"})
        if path == "/api/service-proposals" and method == "GET":
            return self._send(200, {"ok": True, "proposals": [
                {"id": "p1", "title": "Bill Table Extractor", "problem": "OCR misses columns", "input": "PDF",
                 "output": "spreadsheet", "price": {"median": 0.75}, "demand": {"requesters": 3}, "would_pay": "likely"},
                {"id": "p2", "title": "Word Count", "problem": "count words", "input": "text", "output": "number",
                 "price": {"median": 0.01}, "demand": {"requesters": 1}, "would_pay": "maybe"}]})
        if path == "/s/crowdcode/agents" and method == "POST":
            return self._create(body)
        if not path.startswith("/api/app/v1"):
            return self._send(404, {"error": "not_found"})
        owner = self._owner(method, raw_path, body)
        if owner is None:
            return
        self._settle_pending()
        parts = path.removeprefix("/api/app/v1").strip("/").split("/")
        payload = json.loads(body) if body else {}
        if parts == ["me"]:
            mine = [{"id": a["id"], "name": a["name"], "status": a["status"]} for a in STATE.agents.values()
                    if a["owner"].split(":")[2].lower() == owner.lower()]
            return self._send(200, {"address": f"eip155:8453:{owner}", "agents": mine})
        if parts[0] != "agents" or len(parts) < 2:
            return self._send(404, {"error": "not_found"})
        agent = self._agent(owner, parts[1])
        if agent is None:
            return
        rest = parts[2:]
        if not rest and method == "GET":
            return self._send(200, self._agent_view(agent))
        if rest == ["deposits"] and method == "POST":
            return self._deposit(agent, owner, payload)
        if rest == ["tasks"] and method == "POST":
            return self._start_task(agent, payload)
        if len(rest) >= 2 and rest[0] == "tasks":
            task = STATE.tasks.get(rest[1])
            if task is None or task["agent_id"] != agent["id"]:
                return self._send(404, {"error": "not_found"})
            if len(rest) == 3 and rest[2] == "cancel" and method == "POST":
                if task["status"] not in ("completed", "failed", "cancelled"):
                    task["status"] = "cancelled"
                    agent["active_task"] = None
                return self._send(200, {"ok": True})
            return self._send(200, self._task_view(agent, task))
        if rest == ["withdraw"] and method == "POST":
            return self._move(agent, payload, None)
        if rest == ["transfer"] and method == "POST":
            target = STATE.agents.get(str(payload.get("to_agent_id")))
            if target is None or target["owner"] != agent["owner"]:
                return self._send(403, {"error": "not_owner"})
            return self._move(agent, payload, target)
        if rest == ["services"] and method == "GET":
            return self._send(200, [{k: v for k, v in s.items()} for s in agent["services"]])
        if len(rest) == 3 and rest[0] == "services" and rest[2] in ("stop", "start") and method == "POST":
            for s in agent["services"]:
                if s["id"] == rest[1]:
                    s["status"] = "stopped" if rest[2] == "stop" else "active"
                    return self._send(200, {"ok": True})
            return self._send(404, {"error": "not_found"})
        return self._send(404, {"error": "not_found"})

    # -- handlers ----------------------------------------------------------
    def _settle_pending(self) -> None:
        for agent in STATE.agents.values():
            if agent["status"] == "pending" and time.time() - agent["created"] > 1.0:
                agent["status"] = "active"

    def _agent_view(self, agent: dict) -> dict:
        return {"id": agent["id"], "name": agent["name"], "owner": agent["owner"], "addresses": agent["addresses"],
                "balances_usd": {k: _dec(v) for k, v in agent["balances"].items()}, "status": agent["status"]}

    def _create(self, body: bytes) -> None:
        payload = json.loads(body or b"{}")
        name = str(payload.get("name") or "")
        if not 3 <= len(name) <= 40:
            return self._send(400, {"error": "invalid_name"})
        host = self.headers.get("host")
        required = {"x402Version": 2, "error": "Payment required",
                    "resource": {"url": f"http://{host}/s/crowdcode/agents", "description": "Create a CrowdCode agent",
                                 "mimeType": "application/json"},
                    "accepts": [{"scheme": "exact", "network": "eip155:8453", "amount": str(CREATE_PRICE_UNITS),
                                 "asset": USDC, "payTo": TREASURY, "maxTimeoutSeconds": 300,
                                 "extra": {"name": "USD Coin", "version": "2"}}]}
        header = self.headers.get("payment-signature")
        if not header:
            return self._send(402, {}, {"PAYMENT-REQUIRED": base64.b64encode(json.dumps(required).encode()).decode()})
        try:
            paid = json.loads(base64.b64decode(header))
            auth = paid["payload"]["authorization"]
            if int(auth["value"]) != CREATE_PRICE_UNITS or auth["to"].lower() != TREASURY.lower():
                raise ValueError("wrong amount or payee")
            if paid.get("accepted", {}).get("network") != "eip155:8453":
                raise ValueError("wrong network")
            payer = _verify_eip3009(auth, paid["payload"]["signature"])
        except Exception as exc:
            required["error"] = f"invalid payment: {exc}"
            return self._send(402, {}, {"PAYMENT-REQUIRED": base64.b64encode(json.dumps(required).encode()).decode()})
        agent = STATE.new_agent(payer, name)
        settlement = {"success": True, "transaction": "0x" + secrets.token_hex(32), "network": "eip155:8453",
                      "payer": payer}
        view = self._agent_view(agent)
        return self._send(201, {"agent_id": agent["id"], "owner": agent["owner"], "addresses": agent["addresses"],
                                "status": "pending", "api": f"http://{host}/api/app/v1", "name": view["name"]},
                          {"PAYMENT-RESPONSE": base64.b64encode(json.dumps(settlement).encode()).decode()})

    def _deposit(self, agent: dict, owner: str, payload: dict) -> None:
        try:
            auth = payload["authorization"]
            if auth["from"].lower() != owner.lower():
                raise ValueError("from must be the owner")
            if auth["to"].lower() != agent["addresses"]["base"].lower():
                raise ValueError("to must be the agent's Base address")
            _verify_eip3009(auth, payload["signature"])
            if int(auth["value"]) < 500_000:
                raise ValueError("below the $0.50 minimum")
        except Exception as exc:
            return self._send(400, {"error": f"invalid_authorization: {exc}"})
        agent["balances"]["base"] += int(auth["value"]) / 1e6
        return self._send(200, {"tx_hash": "0x" + secrets.token_hex(32)})

    def _start_task(self, agent: dict, payload: dict) -> None:
        if agent["active_task"]:
            active = STATE.tasks.get(agent["active_task"])
            if active and active["status"] in ("queued", "running"):
                return self._send(409, {"error": "task_active"})
        budget = float(payload.get("budget_usd") or 0)
        if budget <= 0:
            return self._send(400, {"error": "budget_required"})
        if agent["balances"]["base"] < min(budget, 0.05):
            return self._send(402, {"error": "insufficient_funds"})
        task_id = "tk_" + secrets.token_hex(5)
        STATE.tasks[task_id] = {"id": task_id, "agent_id": agent["id"], "prompt": payload.get("prompt", ""),
                                "budget": budget, "status": "queued", "started": time.time()}
        agent["active_task"] = task_id
        return self._send(202, {"task_id": task_id})

    def _task_view(self, agent: dict, task: dict) -> dict:
        elapsed = time.time() - task["started"]
        if task["status"] in ("queued", "running"):
            task["status"] = "running" if elapsed < TASK_SECONDS else "completed"
            if task["status"] == "completed":
                spend = min(task["budget"], 0.02)
                agent["balances"]["base"] = max(0.0, agent["balances"]["base"] - spend)
                task["spent"] = spend
                task["purchases"] = [{"service": "exa-search", "url": "https://x402.example/search",
                                      "amount_usd": _dec(spend), "network": "base", "tx": "0x" + secrets.token_hex(32)}]
                task["answer"] = f"Done: {task['prompt'][:80]}"
                if "service" in task["prompt"].lower():
                    slug = f"{agent['name']}-svc"[:30]
                    agent["services"].append({"id": "sv_" + secrets.token_hex(4), "slug": slug,
                                              "url": f"https://crowdcode.app/s/{slug}", "price_usd": "0.010000",
                                              "status": "active", "calls": 0, "earned_usd": "0.000000"})
                    task["answer"] += f"\nDeployed https://crowdcode.app/s/{slug} at $0.01 per call."
                agent["active_task"] = None
        return {"status": task["status"], "answer": task.get("answer"), "spent_usd": _dec(task.get("spent", 0.0)),
                "purchases": task.get("purchases", []),
                "artifacts": [{"name": "report.md", "url": "https://example.invalid/report.md"}]
                if task["status"] == "completed" else []}

    def _move(self, agent: dict, payload: dict, target: dict | None) -> None:
        amount = float(payload.get("amount_usd") or 0)
        network = payload.get("network", "base")
        if amount < 0.5:
            return self._send(400, {"error": "below_minimum"})
        if agent["balances"].get(network, 0.0) + 1e-9 < amount:
            return self._send(402, {"error": "insufficient_funds"})
        agent["balances"][network] -= amount
        if target is not None:
            target["balances"][network] += amount
        return self._send(200, {"tx_hash": "0x" + secrets.token_hex(32)})


def serve(port: int = 0) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    srv = serve(args.port)
    print(f"CrowdCode mock on http://127.0.0.1:{srv.server_address[1]}")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
