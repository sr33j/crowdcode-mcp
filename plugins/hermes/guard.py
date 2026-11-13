"""Spending policy: a ``pre_tool_call`` guard plus a local ledger the tool handlers keep.

Two limits, both plugin settings:

* ``approve_above_usd``: a money-moving call **at or above** this asks the human
  first, through Hermes's own approval gate (``{"action": "approve"}``). The
  default is 0, so every money-moving call asks; a user who raises it opts out
  of prompts below that amount themselves. The prompt names the amount, the
  destination and the agent. Unattended runs (cron) get no prompt, so Hermes
  denies those calls.
* ``daily_cap_usd``: money that leaves the wallet or can be spent by an agent
  (creating an agent, funding one, a task budget) is summed per UTC day; a
  call that would go over the cap is blocked outright, approval or not.

Withdrawals and transfers between your own agents keep money with you, so they
ask for approval like any other money move but never count against the daily cap
(a cap must never stop you rescuing funds).

The guard only reads the call's arguments and local state: no network, so it
stays well inside Hermes's hook timeout. The handlers re-check the cap under a
lock and reserve the amount before any money moves, so parallel calls cannot
slip past it together.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

DEFAULT_APPROVE_ABOVE = 0.0  # ask for every money-moving call unless the user raises it
DEFAULT_DAILY_CAP = 5.00
DEFAULT_CREATE_MAX = 0.50
LEDGER_KEY = "ledger"
KEEP_DAYS = 31

# tool -> (argument holding the amount, counts toward the daily cap, how the approval prompt describes it)
MONEY_TOOLS: dict[str, tuple[str, bool, str]] = {
    "crowdcode_create_agent": ("max_price_usd", True, "pay CrowdCode to create agent"),
    "crowdcode_fund_agent": ("amount_usd", True, "send USDC from your wallet to agent"),
    "crowdcode_run_task": ("budget_usd", True, "let agent spend up to"),
    "crowdcode_withdraw": ("amount_usd", False, "withdraw from agent to your wallet"),
    "crowdcode_transfer": ("amount_usd", False, "move USDC between your agents"),
}

_lock = threading.Lock()
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ID_FIELDS = ("agent_id", "from_agent_id", "to_agent_id", "task_id", "service_id")
COUNTED = ("reserved", "spent", "uncertain")


def clean(text: object, limit: int = 60) -> str:
    """Model-supplied text shown in an approval prompt: one line, bounded."""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class Policy:
    def __init__(self, ctx: Any):
        self._ctx = ctx
        # Set by the tools once they know the mode. In account mode funding moves nothing from Hermes:
        # CrowdCode answers with the page where the owner approves it from the main wallet.
        self.account_mode = False

    def _get(self, key: str, default: Any = None) -> Any:
        return self._ctx.get_config(key, default=default)

    @property
    def state(self) -> Any:
        # Read lazily: registration-time probes hand register() a context without state.
        return self._ctx.state

    # settings ------------------------------------------------------------
    def _num(self, key: str, default: float) -> float:
        try:
            value = float(self._get(key, default=default))
        except (TypeError, ValueError):
            return default
        return value if value >= 0 else default

    @property
    def approve_above(self) -> float:
        return self._num("approve_above_usd", DEFAULT_APPROVE_ABOVE)

    @property
    def daily_cap(self) -> float:
        return self._num("daily_cap_usd", DEFAULT_DAILY_CAP)

    @property
    def always_ask(self) -> bool:
        return bool(self._get("always_ask", default=False))

    # ledger --------------------------------------------------------------
    def _entries(self) -> list[dict]:
        entries = self.state.get(LEDGER_KEY, default=[]) or []
        return entries if isinstance(entries, list) else []

    def _save(self, entries: list[dict]) -> None:
        horizon, day = time.time() - KEEP_DAYS * 86400, today()
        kept = [e for e in entries if e.get("at", 0) >= horizon]
        if len(kept) > 500:
            # Trim old or released entries first; never anything that counts toward today's cap.
            protected = [e for e in kept if e.get("day") == day and e.get("status") in COUNTED]
            rest = [e for e in kept if e not in protected and e.get("status") != "released"]
            keep_rest = rest[-max(0, 500 - len(protected)):] if len(protected) < 500 else []
            kept = sorted(protected + keep_rest, key=lambda e: e.get("at", 0))
        self.state.set(LEDGER_KEY, kept)

    def spent_today(self) -> float:
        day = today()
        return round(sum(float(e.get("amount", 0)) for e in self._entries()
                         if e.get("day") == day and e.get("counts") and e.get("status") in COUNTED), 6)

    def remaining_today(self) -> float:
        return max(0.0, round(self.daily_cap - self.spent_today(), 6))

    def recent(self, limit: int = 10) -> list[dict]:
        return [e for e in self._entries() if e.get("status") != "released"][-limit:]

    def reserve(self, tool: str, amount: float, *, agent: str = "", note: str = "") -> str:
        """Hold ``amount`` against today's cap. Raises ``PermissionError`` when it would not fit."""
        counts = MONEY_TOOLS.get(tool, ("", True, ""))[1]
        with _lock:
            if counts and amount > self.remaining_today() + 1e-9:
                raise PermissionError(
                    f"Blocked: ${amount:.2f} would exceed today's CrowdCode cap of ${self.daily_cap:.2f} "
                    f"(${self.spent_today():.2f} already used). Raise daily_cap_usd in the plugin settings to allow more.")
            entries = self._entries()
            entry_id = uuid.uuid4().hex[:12]
            entries.append({"id": entry_id, "day": today(), "at": time.time(), "tool": tool, "amount": round(amount, 6),
                            "counts": counts, "agent": agent, "note": note, "status": "reserved"})
            self._save(entries)
        return entry_id

    def settle(self, entry_id: str, *, amount: float | None = None, ref: str = "", status: str = "spent") -> None:
        """Mark a reservation spent (optionally at its real amount) or released (``status="released"``)."""
        with _lock:
            entries = self._entries()
            for entry in entries:
                if entry.get("id") == entry_id:
                    entry["status"] = status
                    if amount is not None:
                        entry["amount"] = round(max(0.0, float(amount)), 6)
                    if ref:
                        entry["ref"] = ref
            self._save(entries)

    def release(self, entry_id: str) -> None:
        self.settle(entry_id, status="released")

    def uncertain(self, entry_id: str) -> None:
        """The request may have moved money (timeout, 5xx after sending): keep it counted."""
        self.settle(entry_id, status="uncertain")

    # the hook ------------------------------------------------------------
    def pre_tool_call(self, tool_name: str = "", args: dict | None = None, **_: Any) -> dict | None:
        try:
            return self._decide(tool_name, args or {})
        except Exception as exc:  # a policy hook must decide, never crash: fail closed
            if tool_name in MONEY_TOOLS or tool_name == "crowdcode_download":
                return {"action": "block", "message": f"CrowdCode guard could not check this call ({exc}); blocked."}
            return None

    def _decide(self, tool_name: str, args: dict) -> dict | None:
        if tool_name == "crowdcode_download":
            return self._download_dest(args)
        spec = MONEY_TOOLS.get(tool_name)
        if spec is None:
            return None
        field, counts, verb = spec
        if self.account_mode and tool_name == "crowdcode_fund_agent":
            return None
        if self.account_mode and tool_name == "crowdcode_withdraw":
            verb = "withdraw from agent to your CrowdCode main wallet"
        for key in ID_FIELDS:
            if key in args and not ID_RE.match(str(args.get(key) or "")):
                return {"action": "block", "message": f"{tool_name}: {key} is not a valid CrowdCode id."}
        raw = args.get(field, DEFAULT_CREATE_MAX if tool_name == "crowdcode_create_agent" else None)
        try:
            amount = float(raw)
        except (TypeError, ValueError):
            return {"action": "block", "message": f"{tool_name} needs a numeric {field}."}
        if not amount > 0:
            return {"action": "block", "message": f"{tool_name}: {field} must be greater than zero."}
        if counts and amount > self.remaining_today() + 1e-9:
            return {"action": "block", "message": (
                f"Blocked by the CrowdCode daily cap: ${amount:.2f} requested, ${self.remaining_today():.2f} left of "
                f"${self.daily_cap:.2f} today. The user can raise daily_cap_usd in the plugin settings.")}
        if self.always_ask or amount >= self.approve_above - 1e-9:
            # "Always allow" then covers only this exact move (tool, amount, destination);
            # with always_ask on, every call gets its own key so nothing is remembered.
            target = "|".join(str(args.get(k, "")) for k in ("agent_id", "from_agent_id", "to_agent_id", "network", "name"))
            scope = (uuid.uuid4().hex if self.always_ask
                     else hashlib.sha256(f"{amount:.6f}|{target}".encode()).hexdigest()[:16])
            return {"action": "approve", "message": self.summary(tool_name, args, amount, verb),
                    "rule_key": f"crowdcode:{tool_name}:{scope}"}
        return None

    def _download_dest(self, args: dict) -> dict | None:
        """Downloads stay in the session folder; a folder outside it needs the human, Hermes-protected paths never."""
        from . import files as fileslib

        dest = str(args.get("dest") or "").strip()
        root = fileslib.session_dir()
        target, inside = fileslib.plan_dest(dest, "agent", root)
        if dest:
            denied = fileslib.dest_denied(target)
            if denied:
                return {"action": "block", "message": f"crowdcode_download: {denied}"}
        if inside:
            return None
        fileslib.note_approval_requested(target)
        agent = clean(args.get("agent_id", "?"), 64)
        which = "all files" if [str(i).lower() for i in (args.get("file_ids") or [])] == ["all"] else "files"
        return {"action": "approve", "rule_key": f"crowdcode:download:{uuid.uuid4().hex}",
                "message": (f"CrowdCode: save {which} from agent {agent} to {clean(str(target), 160)}, "
                            f"outside this session's folder {clean(str(Path(root).resolve()), 120)}")}

    def summary(self, tool_name: str, args: dict, amount: float, verb: str) -> str:
        agent = clean(args.get("agent_id") or args.get("from_agent_id") or "", 64)
        network = clean(args.get("network", "base"), 10)
        if tool_name == "crowdcode_create_agent":
            text = f"{verb} '{clean(args.get('name', '?'), 40)}' (at most ${amount:.2f} USDC on Base, paid to CrowdCode via x402)"
        elif tool_name == "crowdcode_fund_agent":
            text = f"{verb} {agent}: ${amount:.2f} USDC on Base"
        elif tool_name == "crowdcode_run_task":
            text = f"{verb} ${amount:.2f} from agent {agent}'s wallet on one task"
        elif tool_name == "crowdcode_withdraw":
            text = f"{verb}: ${amount:.2f} USDC on {network} from {agent}"
        else:
            text = f"{verb}: ${amount:.2f} USDC on {network} from {agent} to {clean(args.get('to_agent_id', '?'), 64)}"
        cap = f"; ${self.remaining_today():.2f} of today's ${self.daily_cap:.2f} cap left" if MONEY_TOOLS[tool_name][1] else ""
        return f"CrowdCode: {text}{cap}"
