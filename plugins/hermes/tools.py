"""Tool handlers. Each takes ``(args, **kwargs)`` and returns a JSON string; none raises."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from . import client as clientlib
from . import files as fileslib
from . import guard as guardlib
from . import wallet as walletlib

DEFAULT_BASE_URL = "https://crowdcode.app"
DEFAULT_RPC = "https://mainnet.base.org"
NETWORKS = ("base", "solana", "tempo")
WITHDRAW_NETWORKS = ("base", "tempo")  # to the owner's EVM address; Tempo only for EOA owners
MIN_MOVE_USD = 0.50  # the platform's floor for relayed deposits, withdrawals and transfers
CREATE_WAIT_SECONDS = 90
TERMINAL = ("completed", "failed", "cancelled")
FUND_HINT = ("Send USDC on Base to this address from any exchange or wallet (Coinbase, a Base wallet, etc.). "
             "No ETH is needed: agent creation is paid with a signed x402 authorization and deposits are gasless.")


def _ok(**data: Any) -> str:
    return json.dumps({"ok": True, **data}, default=str)


def _fail(error: str, **data: Any) -> str:
    return json.dumps({"ok": False, "error": error, **data}, default=str)


def _id(args: dict, key: str) -> str:
    """A CrowdCode id from the model, checked before it goes into a signed URL path."""
    value = str(args.get(key) or "").strip()
    if not value:
        raise ValueError(f"{key} is required")
    if not guardlib.ID_RE.match(value):
        raise ValueError(f"{key} is not a valid CrowdCode id")
    return value


UNSURE = ("CrowdCode may have received this; the outcome is unknown. Do NOT retry: check balances with "
          "crowdcode_agents (and crowdcode_wallet) first. The amount stays counted against today's cap.")


def _definitely_failed(exc: Exception) -> bool:
    """True when no money can have moved: never sent, or refused with a 4xx."""
    if isinstance(exc, clientlib.ApiError):
        return 400 <= exc.status < 500
    return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, ValueError, PermissionError,
                            walletlib.WalletMissing))


def _file_summary(f: dict) -> dict:
    return {k: f.get(k) for k in ("id", "name", "path", "size_bytes", "content_type", "task_id", "created_at") if k in f}


def session_dir() -> Path:
    """The Hermes session's working directory (not the gateway's), else the process's."""
    try:
        from agent.runtime_cwd import resolve_agent_cwd

        return Path(resolve_agent_cwd())
    except Exception:
        return Path.cwd()


def _hint_for(exc: clientlib.ApiError) -> str:
    if exc.status == 401:
        return {"expired": "The request signature expired: check this machine's clock.",
                "replayed": "The request nonce was reused; retry once.",
                }.get(exc.error, "CrowdCode rejected the wallet signature.")
    if exc.status == 403:
        return "This wallet does not own that agent. Use crowdcode_agents to list the agents you own."
    if exc.status == 402:
        return "Not enough USDC. Check balances with crowdcode_agents / crowdcode_wallet and fund first."
    if exc.status == 409 and exc.error == "task_active":
        return "That agent is already running a task (one at a time). Check it with crowdcode_task_status or cancel it."
    if exc.status == 404:
        return "Not found: check the agent or task id."
    return ""


class Tools:
    def __init__(self, ctx: Any, policy: guardlib.Policy, transport: Any = None):
        self.ctx = ctx
        self.policy = policy
        self.transport = transport

    # helpers -------------------------------------------------------------
    def setting(self, key: str, default: Any) -> Any:
        try:
            value = self.ctx.get_config(key, default=default)
        except Exception:
            return default
        return default if value in (None, "") else value

    def base_url(self) -> str:
        url = str(self.setting("base_url", DEFAULT_BASE_URL)).rstrip("/")
        parts = urlsplit(url)
        local = parts.hostname in ("localhost", "127.0.0.1", "::1")
        if parts.scheme != "https" and not (local and parts.scheme == "http"):
            raise ValueError("base_url must be an https:// URL (plain http is allowed only for localhost)")
        return url

    def api(self, need_wallet: bool = True) -> clientlib.CrowdCode:
        return clientlib.CrowdCode(self.base_url(), walletlib.load() if need_wallet else None, transport=self.transport)

    def _state_list(self, key: str) -> list:
        value = self.ctx.state.get(key, default=[]) or []
        return value if isinstance(value, list) else []

    def remember_task(self, record: dict) -> None:
        tasks = [t for t in self._state_list("tasks") if t.get("task_id") != record.get("task_id")]
        tasks.append(record)
        self.ctx.state.set("tasks", tasks[-50:])

    def _money_call(self, entry: str, call: Callable[[], Any]) -> Any:
        """Run a request that may move money; release the reservation only on a definite failure."""
        try:
            return call()
        except Exception as exc:
            if _definitely_failed(exc):
                self.policy.release(entry)
                raise
            self.policy.uncertain(entry)
            raise RuntimeError(f"{type(exc).__name__}: {exc}. {UNSURE}") from exc

    def _guarded(self, fn: Callable[[dict], str]) -> Callable[..., str]:
        def handler(args: dict | None = None, **_: Any) -> str:
            try:
                return fn(args or {})
            except walletlib.WalletMissing as exc:
                return _fail(str(exc), hint="Run /crowdcode wallet init (or call crowdcode_wallet with init=true).")
            except clientlib.ApiError as exc:
                return _fail(f"CrowdCode API: {exc.error}", status=exc.status, hint=_hint_for(exc), detail=exc.detail)
            except PermissionError as exc:
                return _fail(str(exc))
            except RuntimeError as exc:
                return _fail(str(exc))
            except ValueError as exc:
                return _fail(str(exc))
            except Exception as exc:  # network errors and anything unexpected
                return _fail(f"{type(exc).__name__}: {exc}")
        handler.__name__ = fn.__name__
        return handler

    def handlers(self) -> dict[str, Callable[..., str]]:
        return {name: self._guarded(getattr(self, name.removeprefix("crowdcode_"))) for name in (
            "crowdcode_wallet", "crowdcode_agents", "crowdcode_create_agent", "crowdcode_fund_agent",
            "crowdcode_run_task", "crowdcode_task_status", "crowdcode_services", "crowdcode_withdraw",
            "crowdcode_transfer", "crowdcode_demand", "crowdcode_files", "crowdcode_download")}

    # tools ---------------------------------------------------------------
    def wallet(self, args: dict) -> str:
        created = False
        if args.get("init") and not walletlib.exists():
            walletlib.create()
            created = True
        w = walletlib.load()
        balance = None
        try:
            balance = round(self.api(False).usdc_balance(str(self.setting("base_rpc_url", DEFAULT_RPC)), w.address), 6)
        except Exception as exc:
            balance = f"unavailable ({type(exc).__name__})"
        return _ok(created=created, address=w.address, owner=w.caip10, usdc_base=balance,
                   spent_today_usd=self.policy.spent_today(), daily_cap_usd=self.policy.daily_cap,
                   approve_above_usd=self.policy.approve_above, fund=FUND_HINT)

    def agents(self, args: dict) -> str:
        api = self.api()
        if args.get("agent_id"):
            return _ok(agent=api.signed("GET", f"/agents/{_id(args, 'agent_id')}"))
        me = api.signed("GET", "/me")
        listed = []
        for item in (me.get("agents") or [])[:12]:
            try:
                detail = api.signed("GET", f"/agents/{item['id']}")
                listed.append({"id": item["id"], "name": detail.get("name", item.get("name")),
                               "status": detail.get("status", item.get("status")),
                               "balances_usd": detail.get("balances_usd"), "addresses": detail.get("addresses")})
            except clientlib.ApiError:
                listed.append(item)
        return _ok(owner=me.get("address"), agents=listed)

    def create_agent(self, args: dict) -> str:
        name = str(args.get("name") or "").strip()
        if not 3 <= len(name) <= 40:
            raise ValueError("name must be 3-40 characters")
        body: dict[str, Any] = {"name": name}
        if args.get("model"):
            body["model"] = str(args["model"])
        if args.get("instructions"):
            if len(str(args["instructions"])) > 4000:
                raise ValueError("instructions must be at most 4000 characters")
            body["instructions"] = str(args["instructions"])
        max_price = float(args.get("max_price_usd") or guardlib.DEFAULT_CREATE_MAX)
        api = self.api()
        url = self.base_url() + "/s/crowdcode/agents"
        quote = api.quote(url, body)
        price = walletlib.units_to_usd(quote["offer"]["amount"])
        if price > max_price + 1e-9:
            return _fail(f"Creating an agent costs ${price:.2f}, above max_price_usd ${max_price:.2f}; nothing was paid.",
                         price_usd=price)
        entry = self.policy.reserve("crowdcode_create_agent", price, note=name)
        status, created, settlement = self._money_call(entry, lambda: api.pay(url, body, quote))
        tx = (settlement or {}).get("transaction")
        if status >= 400:
            if settlement and settlement.get("success"):
                self.policy.settle(entry, amount=price, ref=str(tx or ""))
            elif status < 500:
                self.policy.release(entry)
            else:
                self.policy.uncertain(entry)
                return _fail(f"Agent creation answered {status}. {UNSURE}", detail=created)
            return _fail(f"Agent creation failed ({status}): {created.get('error') if isinstance(created, dict) else created}",
                         status=status, paid=bool(settlement and settlement.get("success")), detail=created)
        agent_id = str(created.get("agent_id") or "")
        self.policy.settle(entry, amount=price, ref=agent_id or str(tx or ""))
        # Ownership is bound from the payment's settlement, a few seconds after this 201.
        status_now = created.get("status", "pending")
        deadline = time.time() + CREATE_WAIT_SECONDS
        while agent_id and status_now == "pending" and time.time() < deadline:
            time.sleep(3)
            try:
                detail = api.signed("GET", f"/agents/{agent_id}")
            except clientlib.ApiError as exc:
                if exc.status in (403, 404):  # not bound to us yet
                    continue
                raise
            status_now = detail.get("status", status_now)
            created = {**created, **{k: v for k, v in detail.items() if k in ("addresses", "owner", "status", "name")}}
        if status_now == "failed":
            self.policy.release(entry)
            return _fail("CrowdCode could not settle the creation payment, so the agent was removed. You were not charged.",
                         agent_id=agent_id)
        created["status"] = status_now
        out = {"paid_usd": price, "payment_tx": tx, **created}
        if status_now == "pending":
            out["next"] = "Payment is still settling; check with crowdcode_agents in a minute."
        else:
            out["next"] = "Fund it with crowdcode_fund_agent, then give it work with crowdcode_run_task."
        return _ok(**out)

    def fund_agent(self, args: dict) -> str:
        agent_id = _id(args, "agent_id")
        amount = float(args.get("amount_usd") or 0)
        units = walletlib.usd_to_units(amount)
        if amount < MIN_MOVE_USD - 1e-9:
            raise ValueError(f"the minimum relayed deposit is ${MIN_MOVE_USD:.2f}; smaller amounts can be sent as a "
                             "plain USDC transfer to the agent's Base address")
        api = self.api()
        agent = api.signed("GET", f"/agents/{agent_id}")
        to = (agent.get("addresses") or {}).get("base")
        if not to:
            return _fail("CrowdCode did not return a Base address for this agent; nothing was sent.")
        entry = self.policy.reserve("crowdcode_fund_agent", amount, agent=agent_id)
        authorization, signature = api.wallet.authorize_transfer(to=to, value=units, valid_seconds=300)
        result = self._money_call(entry, lambda: api.signed(
            "POST", f"/agents/{agent_id}/deposits", {"authorization": authorization, "signature": signature}))
        self.policy.settle(entry, ref=str(result.get("tx_hash") or ""))
        return _ok(agent_id=agent_id, amount_usd=amount, to=to, tx_hash=result.get("tx_hash"),
                   explorer=f"https://basescan.org/tx/{result.get('tx_hash')}" if result.get("tx_hash") else None)

    def run_task(self, args: dict) -> str:
        agent_id = _id(args, "agent_id")
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("prompt is required")
        if len(prompt) > 20000:
            raise ValueError("prompt must be at most 20000 characters")
        budget = float(args.get("budget_usd") or 0)
        if not budget > 0:
            raise ValueError("budget_usd is required and must be greater than zero")
        body: dict[str, Any] = {"prompt": prompt, "budget_usd": budget}
        if args.get("model"):
            body["model"] = str(args["model"])
        api = self.api()
        entry = self.policy.reserve("crowdcode_run_task", budget, agent=agent_id, note=prompt[:80])
        # The same request within ten minutes reuses its key, so a retried call cannot start a second task.
        key = hashlib.sha256(json.dumps([agent_id, body, int(time.time() // 600)], sort_keys=True).encode()).hexdigest()
        started = self._money_call(entry, lambda: api.signed(
            "POST", f"/agents/{agent_id}/tasks", body, extra_headers={"Idempotency-Key": key}))
        task_id = started.get("task_id")
        record = {"agent_id": agent_id, "task_id": task_id, "prompt": prompt[:120], "budget_usd": budget,
                  "status": "queued", "ledger": entry, "at": time.time()}
        self.remember_task(record)
        if args.get("wait") is False:
            return _ok(task_id=task_id, status="queued", next="Check it with crowdcode_task_status.")
        timeout = float(args.get("timeout_seconds") or self.setting("task_timeout_seconds", 600))
        return self._poll(api, record, deadline=time.time() + max(10.0, min(timeout, 1800.0)))

    def _poll(self, api: clientlib.CrowdCode, record: dict, deadline: float) -> str:
        delay = 2.0
        task: dict = {}
        while True:
            task = api.signed("GET", f"/agents/{record['agent_id']}/tasks/{record['task_id']}")
            if task.get("status") in TERMINAL or time.time() >= deadline:
                break
            time.sleep(min(delay, max(0.0, deadline - time.time())))
            delay = min(delay * 1.5, 10.0)
        return self._report(record, task)

    def _report(self, record: dict, task: dict) -> str:
        status = task.get("status", "unknown")
        record["status"] = status
        if task.get("spent_usd") is not None:
            record["spent_usd"] = task.get("spent_usd")
        if status in TERMINAL and record.get("ledger"):
            self.policy.settle(record["ledger"], amount=float(task.get("spent_usd") or 0))
            record["ledger"] = ""
        self.remember_task(record)
        out = {"task_id": record["task_id"], "agent_id": record["agent_id"], "status": status,
               "answer": task.get("answer"), "spent_usd": task.get("spent_usd"),
               "budget_usd": record.get("budget_usd"), "purchases": task.get("purchases") or [],
               "files": [_file_summary(f) for f in task.get("artifacts") or []]}
        if task.get("waiting_for"):
            out["waiting_for"] = task["waiting_for"]
        if out["files"]:
            out["files_note"] = "Nothing was downloaded. Save them with crowdcode_download (file ids or \"all\")."
        if status not in TERMINAL:
            out["next"] = "Still running; check again with crowdcode_task_status."
        return _ok(**out)

    def task_status(self, args: dict) -> str:
        agent_id, task_id = _id(args, "agent_id"), _id(args, "task_id")
        api = self.api()
        if args.get("cancel"):
            api.signed("POST", f"/agents/{agent_id}/tasks/{task_id}/cancel")
        task = api.signed("GET", f"/agents/{agent_id}/tasks/{task_id}")
        record = next((t for t in self._state_list("tasks") if t.get("task_id") == task_id),
                      {"agent_id": agent_id, "task_id": task_id, "at": time.time()})
        return self._report(record, task)

    def services(self, args: dict) -> str:
        agent_id = _id(args, "agent_id")
        action = str(args.get("action") or "list")
        api = self.api()
        if action in ("stop", "start"):
            service_id = _id(args, "service_id")
            api.signed("POST", f"/agents/{agent_id}/services/{service_id}/{action}")
        elif action != "list":
            raise ValueError("action must be list, stop or start")
        services = api.signed("GET", f"/agents/{agent_id}/services")
        if isinstance(services, dict):
            services = services.get("services", [])
        earned = round(sum(float(s.get("earned_usd") or 0) for s in services), 6)
        return _ok(agent_id=agent_id, services=services, total_earned_usd=earned)

    @staticmethod
    def _move_amount(args: dict) -> float:
        amount = float(args.get("amount_usd") or 0)
        walletlib.usd_to_units(amount)
        if amount < MIN_MOVE_USD - 1e-9:
            raise ValueError(f"the minimum withdrawal or transfer is ${MIN_MOVE_USD:.2f}")
        return amount

    def withdraw(self, args: dict) -> str:
        agent_id = _id(args, "agent_id")
        network = str(args.get("network") or "base")
        if network not in WITHDRAW_NETWORKS:
            raise ValueError("withdrawals go to your wallet's EVM address: network must be base or tempo")
        amount = self._move_amount(args)
        api = self.api()
        entry = self.policy.reserve("crowdcode_withdraw", amount, agent=agent_id)
        result = self._money_call(entry, lambda: api.signed(
            "POST", f"/agents/{agent_id}/withdraw", {"amount_usd": amount, "network": network}))
        self.policy.settle(entry, ref=str(result.get("tx_hash") or ""))
        return _ok(agent_id=agent_id, amount_usd=amount, network=network, to=api.wallet.address,
                   tx_hash=result.get("tx_hash"))

    def transfer(self, args: dict) -> str:
        source, target = _id(args, "from_agent_id"), _id(args, "to_agent_id")
        network = str(args.get("network") or "base")
        if network not in NETWORKS:
            raise ValueError(f"network must be one of {', '.join(NETWORKS)}")
        amount = self._move_amount(args)
        api = self.api()
        entry = self.policy.reserve("crowdcode_transfer", amount, agent=source, note=target)
        result = self._money_call(entry, lambda: api.signed(
            "POST", f"/agents/{source}/transfer", {"to_agent_id": target, "amount_usd": amount, "network": network}))
        self.policy.settle(entry, ref=str(result.get("tx_hash") or ""))
        return _ok(from_agent_id=source, to_agent_id=target, amount_usd=amount, network=network, **result)

    def demand(self, args: dict) -> str:
        data = self.api(False).demand()
        proposals = data.get("proposals", []) if isinstance(data, dict) else []
        query = str(args.get("query") or "").lower().strip()
        rows = []
        for p in proposals:
            text = " ".join(str(p.get(k, "")) for k in ("title", "problem", "input", "output")).lower()
            if query and query not in text:
                continue
            demand = p.get("demand") or {}
            rows.append({"title": p.get("title"), "problem": p.get("problem"), "input": p.get("input"),
                         "output": p.get("output"), "median_price_usd": (p.get("price") or {}).get("median"),
                         "agents_asking": demand.get("requesters"), "would_pay": p.get("would_pay"),
                         "last_requested_at": demand.get("last_requested_at")})
        rows.sort(key=lambda r: (r.get("agents_asking") or 0, r.get("median_price_usd") or 0), reverse=True)
        limit = max(1, min(int(args.get("limit") or 10), 50))
        return _ok(count=len(rows), proposals=rows[:limit],
                   tip=("Each one is a service other agents asked for. To have an agent build one, load the skill "
                        "crowdcode:build-a-paid-service, or ask an agent to with crowdcode_run_task."))

    def files(self, args: dict) -> str:
        agent_id = _id(args, "agent_id")
        task_id = _id(args, "task_id") if args.get("task_id") else ""
        listed = fileslib.list_files(self.api(), agent_id, task_id)
        total = sum(int(f.get("size_bytes") or 0) for f in listed)
        return _ok(agent_id=agent_id, count=len(listed), total_bytes=total,
                   files=[_file_summary(f) for f in listed],
                   next="Save some or all of them with crowdcode_download." if listed else None)

    def download(self, args: dict, base: Path | None = None) -> str:
        agent_id = _id(args, "agent_id")
        task_id = _id(args, "task_id") if args.get("task_id") else ""
        wanted = args.get("file_ids")
        if isinstance(wanted, str):
            wanted = [w for w in wanted.replace(",", " ").split() if w]
        if not wanted:
            raise ValueError('file_ids is required: a list of file ids, or "all"')
        api = self.api()
        listed = fileslib.list_files(api, agent_id, task_id)
        if [w.lower() for w in wanted] == ["all"]:
            chosen = listed
        else:
            by_id = {str(f.get("id")): f for f in listed}
            for w in wanted:
                if not guardlib.ID_RE.match(str(w)):
                    raise ValueError(f"{w!r} is not a valid file id")
            missing = [w for w in wanted if str(w) not in by_id]
            if missing:
                return _fail(f"No such file for this agent: {', '.join(missing)}. List them with crowdcode_files.")
            chosen = [by_id[str(w)] for w in wanted]
        if not chosen:
            return _ok(agent_id=agent_id, downloaded=[], note="This agent has no files yet.")
        name = api.signed("GET", f"/agents/{agent_id}").get("name") or agent_id
        directory = fileslib.resolve_dest(str(args.get("dest") or ""), name, base or session_dir())
        max_bytes = int(float(self.setting("max_download_mb", 500)) * 1_000_000)
        saved, failed = [], []
        for f in chosen:
            try:
                saved.append(fileslib.download(api, agent_id, f, directory, max_bytes))
            except (clientlib.ApiError, ValueError, OSError, httpx.HTTPError) as exc:
                failed.append({"id": f.get("id"), "name": f.get("name"), "error": str(exc)})
        return _ok(agent_id=agent_id, directory=str(directory), downloaded=saved, failed=failed,
                   total_bytes=sum(s["size_bytes"] for s in saved))

    def resolve_agent(self, ref: str) -> str:
        """An agent id from an id, an id prefix or a name, among the agents this wallet owns."""
        ref = (ref or "").strip()
        if not ref:
            raise ValueError("name an agent (its name or id)")
        agents = self.api().signed("GET", "/me").get("agents") or []
        for match in (lambda a: a.get("id") == ref,
                      lambda a: str(a.get("name", "")).lower() == ref.lower(),
                      lambda a: str(a.get("id", "")).startswith(ref)):
            found = [a for a in agents if match(a)]
            if len(found) == 1:
                return str(found[0]["id"])
            if len(found) > 1:
                raise ValueError(f"'{ref}' matches more than one agent; use its id")
        raise ValueError(f"no agent of yours is called '{ref}'")
