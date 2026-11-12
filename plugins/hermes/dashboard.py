"""The ``/crowdcode`` slash command: one screen with the wallet, agents, recent tasks and earnings.

Read-only apart from ``/crowdcode wallet init``, which creates a key only when none exists.
Nothing here moves money; that only happens through the tools, behind the spend guard.
"""
from __future__ import annotations

import json
import time
from typing import Any

from . import client as clientlib
from . import wallet as walletlib

USAGE = """CrowdCode: cloud agents with their own wallets.

  /crowdcode                    wallet, agents, balances, recent tasks, services
  /crowdcode wallet             address, USDC balance and how to fund it
  /crowdcode wallet init        create this install's wallet (key kept in Hermes's credential store)
  /crowdcode agents             agents and their balances
  /crowdcode tasks              recent tasks started from here
  /crowdcode services <agent>   an agent's paid services and earnings
  /crowdcode files <agent> [task-id]            files the agent produced (nothing is downloaded)
  /crowdcode download <agent> <file-id…|all>    save them to ./crowdcode-files/<agent>/
  /crowdcode demand [keyword]   services other agents are asking for
  /crowdcode limits             spending limits and today's spend

Ask Hermes in plain words for the rest, e.g. "create a CrowdCode agent called scout and fund it with $1"."""


def _usd(value: Any) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "-"


def _size(value: Any) -> str:
    n = _num(value)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} GB"


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _ago(ts: float) -> str:
    seconds = max(0, int(time.time() - float(ts or 0)))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return f"{seconds}s ago"


class Dashboard:
    def __init__(self, tools: Any):
        self.tools = tools

    def __call__(self, raw_args: str = "", **_: Any) -> str:
        words = (raw_args or "").split()
        try:
            if not words:
                return self.overview()
            head, rest = words[0].lower(), words[1:]
            if head in ("help", "-h", "--help"):
                return USAGE
            if head == "wallet":
                return self.wallet(init=bool(rest and rest[0].lower() == "init"))
            if head == "agents":
                return self.agents_text(self._agents())
            if head == "tasks":
                return self.tasks_text()
            if head == "services":
                return self.services_text(rest[0]) if rest else "Usage: /crowdcode services <agent-id>"
            if head == "files":
                return self.files_text(rest[0], rest[1] if len(rest) > 1 else "") if rest else \
                    "Usage: /crowdcode files <agent> [task-id]"
            if head == "download":
                if len(rest) < 2:
                    return "Usage: /crowdcode download <agent> <file-id…|all>"
                return self.download_text(rest[0], rest[1:])
            if head == "demand":
                return self.demand_text(" ".join(rest))
            if head == "limits":
                return self.limits_text()
            return f"Unknown subcommand '{head}'.\n\n{USAGE}"
        except walletlib.WalletMissing:
            return ("No CrowdCode wallet yet.\n\n  /crowdcode wallet init   creates one for this Hermes install.\n\n"
                    "Its key is stored through Hermes's credential store (CROWDCODE_WALLET_KEY) and never printed.")
        except clientlib.ApiError as exc:
            return f"CrowdCode API error: {exc.status} {exc.error}"
        except Exception as exc:
            return f"CrowdCode: {type(exc).__name__}: {exc}"

    # sections ------------------------------------------------------------
    def _wallet_lines(self, data: dict) -> list[str]:
        lines = [f"Wallet   {data['address']}",
                 f"         {_usd(data['usdc_base']) if isinstance(data['usdc_base'], (int, float)) else data['usdc_base']} USDC on Base"]
        lines.append(f"Today    {_usd(data['spent_today_usd'])} of {_usd(data['daily_cap_usd'])} daily cap used; "
                     + ("asks before every money move" if not data['approve_above_usd'] else f"asks at {_usd(data['approve_above_usd'])} or more"))
        return lines

    def wallet(self, init: bool = False) -> str:
        if init and walletlib.exists():
            data = json.loads(self.tools.wallet({}))
            return "A CrowdCode wallet already exists (not replaced).\n\n" + "\n".join(self._wallet_lines(data))
        data = json.loads(self.tools.wallet({"init": init}))
        if not data.get("ok"):
            raise walletlib.WalletMissing(data.get("error"))
        head = ["Created your CrowdCode wallet.", ""] if data.get("created") else []
        return "\n".join(head + self._wallet_lines(data) + ["", "Fund it: " + data["fund"]])

    def _agents(self) -> list[dict]:
        data = json.loads(self.tools.agents({}))
        if not data.get("ok"):
            raise RuntimeError(data.get("error"))
        return data.get("agents") or []

    def agents_text(self, agents: list[dict]) -> str:
        if not agents:
            return "Agents   none yet. Ask: \"create a CrowdCode agent called scout\"."
        lines = ["Agents"]
        for a in agents:
            b = a.get("balances_usd") or {}
            total = sum(_num(v) for v in b.values()) if b else None
            parts = "  ".join(f"{k} {_usd(v)}" for k, v in b.items() if _num(v) > 0)
            short = str(a.get("id", "?"))[:8]
            lines.append(f"  {a.get('name', '?'):<18} {short:<9} {a.get('status', ''):<8} "
                         f"{_usd(total) if total is not None else '-':>8}  {parts}")
        return "\n".join(lines)

    def tasks_text(self, limit: int = 5) -> str:
        tasks = self.tools._state_list("tasks")[-limit:]
        if not tasks:
            return "Tasks    none started from this install yet."
        lines = ["Recent tasks"]
        for t in reversed(tasks):
            spent = f"{_usd(t.get('spent_usd'))}/{_usd(t.get('budget_usd'))}"
            lines.append(f"  {t.get('status', '?'):<10} {spent:<13} {_ago(t.get('at')):<8} {t.get('prompt', '')[:60]}")
        return "\n".join(lines)

    def services_text(self, agent_id: str) -> str:
        data = json.loads(self.tools.services({"agent_id": agent_id}))
        if not data.get("ok"):
            return f"CrowdCode: {data.get('error')}"
        services = data.get("services") or []
        if not services:
            return f"{agent_id} has no paid services yet."
        lines = [f"Services of {agent_id}  (earned {_usd(data.get('total_earned_usd'))})"]
        for s in services:
            lines.append(f"  {s.get('slug', '?'):<22} {s.get('status', ''):<8} {_usd(s.get('price_usd'))}/call  "
                         f"{s.get('calls', 0)} calls  earned {_usd(s.get('earned_usd'))}\n    {s.get('url', '')}")
        return "\n".join(lines)

    def files_text(self, agent: str, task_id: str = "") -> str:
        agent_id = self.tools.resolve_agent(agent)
        data = json.loads(self.tools.files({"agent_id": agent_id, "task_id": task_id}))
        if not data.get("ok"):
            return f"CrowdCode: {data.get('error')}"
        files = data.get("files") or []
        if not files:
            return f"{agent} has no files yet."
        lines = [f"Files of {agent}  ({len(files)}, {_size(data.get('total_bytes'))})"]
        for f in files:
            lines.append(f"  {str(f.get('id', '?')):<38} {_size(f.get('size_bytes')):>9}  {str(f.get('created_at', ''))[:16]:<16}  "
                         f"{f.get('name', '')}")
        lines.append(f"\nSave them: /crowdcode download {agent} <id…|all>")
        return "\n".join(lines)

    def download_text(self, agent: str, ids: list[str], base: Any = None) -> str:
        agent_id = self.tools.resolve_agent(agent)
        data = json.loads(self.tools.download({"agent_id": agent_id, "file_ids": ids}, base=base))
        if not data.get("ok"):
            return f"CrowdCode: {data.get('error')}"
        saved, failed = data.get("downloaded") or [], data.get("failed") or []
        if not saved and not failed:
            return data.get("note") or "Nothing to download."
        lines = [f"Saved {len(saved)} file(s), {_size(data.get('total_bytes'))}, to {data.get('directory')}"]
        lines += [f"  {_size(s['size_bytes']):>9}  {s['path']}" for s in saved]
        lines += [f"  failed  {f.get('name') or f.get('id')}: {f.get('error')}" for f in failed]
        return "\n".join(lines)

    def demand_text(self, query: str = "") -> str:
        data = json.loads(self.tools.demand({"query": query, "limit": 8}))
        if not data.get("ok"):
            return f"CrowdCode: {data.get('error')}"
        lines = ["What agents are asking for (crowdcode.app/api/service-proposals)"]
        for p in data.get("proposals") or []:
            lines.append(f"  {p.get('title', '?'):<34} {p.get('agents_asking') or 0} agents  ~{_usd(p.get('median_price_usd'))}")
        return "\n".join(lines)

    def limits_text(self) -> str:
        policy = self.tools.policy
        lines = [("Asks for approval before every money-moving call" if not policy.approve_above
                  else f"Asks for approval at {_usd(policy.approve_above)} or more per call")
                 + (" (and for every money-moving call: always_ask is on)" if policy.always_ask else ""),
                 f"Daily cap {_usd(policy.daily_cap)}: {_usd(policy.spent_today())} used today (UTC)",
                 "Change them in the plugin settings (approve_above_usd, daily_cap_usd, always_ask)."]
        recent = policy.recent(5)
        if recent:
            lines.append("Recent money moves")
            for e in reversed(recent):
                lines.append(f"  {e.get('status'):<8} {_usd(e.get('amount')):>7}  {e.get('tool', '').removeprefix('crowdcode_'):<12} "
                             f"{e.get('agent') or e.get('note', '')}  {_ago(e.get('at'))}")
        return "\n".join(lines)

    def overview(self) -> str:
        data = json.loads(self.tools.wallet({}))
        if not data.get("ok"):
            raise walletlib.WalletMissing(data.get("error"))
        sections = ["\n".join(self._wallet_lines(data))]
        agents: list[dict] = []
        try:
            agents = self._agents()
            sections.append(self.agents_text(agents))
        except Exception as exc:
            sections.append(f"Agents   unavailable ({exc})")
        sections.append(self.tasks_text())
        earned_lines = []
        for a in agents[:6]:
            try:
                s = json.loads(self.tools.services({"agent_id": a["id"]}))
            except Exception:
                continue
            for svc in (s.get("services") or []) if s.get("ok") else []:
                earned_lines.append(f"  {a.get('name', a['id'])}/{svc.get('slug', '?'):<20} {svc.get('status', ''):<8} "
                                    f"{svc.get('calls', 0)} calls  earned {_usd(svc.get('earned_usd'))}")
        sections.append("Services\n" + "\n".join(earned_lines) if earned_lines else "Services none deployed yet.")
        return "\n\n".join(sections)
