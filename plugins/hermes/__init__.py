"""CrowdCode for Hermes: cloud agents with their own wallets, owned by this install's wallet.

``register(ctx)`` wires ten tools, a ``pre_tool_call`` spend guard, the
``/crowdcode`` slash command and the ``hermes crowdcode`` terminal command.
"""
from __future__ import annotations

import sys
from pathlib import Path

from . import wallet as walletlib
from .dashboard import USAGE, Dashboard
from .guard import Policy
from .schemas import SCHEMAS
from .tools import Tools

TOOLSET = "crowdcode"
EMOJI = {"crowdcode_wallet": "👛", "crowdcode_agents": "🤖", "crowdcode_create_agent": "✨",
         "crowdcode_fund_agent": "💸", "crowdcode_run_task": "🚀", "crowdcode_task_status": "📋",
         "crowdcode_services": "🛠", "crowdcode_withdraw": "🏦", "crowdcode_transfer": "🔁",
         "crowdcode_demand": "📈"}


def _cli_setup(parser) -> None:
    sub = parser.add_subparsers(dest="crowdcode_command")
    wallet = sub.add_parser("wallet", help="Manage this install's CrowdCode wallet")
    wallet_sub = wallet.add_subparsers(dest="wallet_command")
    wallet_sub.add_parser("init", help="Create the wallet if there is none")
    wallet_sub.add_parser("address", help="Print the wallet address")
    export = wallet_sub.add_parser("export", help="Print the private key (to import it into another wallet)")
    export.add_argument("--yes-show-private-key", action="store_true", dest="confirm",
                        help="Required: confirms you want the key printed in this terminal")


def _cli_handler(args) -> int:
    command = getattr(args, "crowdcode_command", None)
    if command != "wallet":
        print(USAGE)
        return 0
    action = getattr(args, "wallet_command", None) or "address"
    try:
        if action == "init":
            if walletlib.exists():
                print(f"A CrowdCode wallet already exists: {walletlib.load().address}")
                return 0
            print(f"Created CrowdCode wallet {walletlib.create().address}")
            print("Fund it with USDC on Base. The key is stored in Hermes's credential store as CROWDCODE_WALLET_KEY.")
            return 0
        w = walletlib.load()
        if action == "address":
            print(w.address)
            return 0
        if action == "export":
            if not getattr(args, "confirm", False):
                print("This prints the private key that controls the wallet and every agent it owns.\n"
                      "Re-run with --yes-show-private-key in a terminal nobody else can see.", file=sys.stderr)
                return 2
            print(w.export_private_key())
            return 0
    except walletlib.WalletMissing as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(USAGE)
    return 0


def register(ctx) -> None:
    policy = Policy(ctx)
    tools = Tools(ctx, policy)
    for name, handler in tools.handlers().items():
        schema = SCHEMAS[name]
        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema, handler=handler,
                          description=schema["description"], emoji=EMOJI.get(name, ""))
    ctx.register_hook("pre_tool_call", policy.pre_tool_call)
    for skill in sorted((Path(__file__).parent / "skills").glob("*/SKILL.md")):
        ctx.register_skill(skill.parent.name, skill)
    ctx.register_command("crowdcode", Dashboard(tools),
                         description="CrowdCode wallet, cloud agents, tasks and earnings",
                         args_hint="[wallet|agents|tasks|services|demand|limits]")
    ctx.register_cli_command(name="crowdcode", help="CrowdCode wallet (init, address, export)",
                             setup_fn=_cli_setup, handler_fn=_cli_handler)


__all__ = ["register"]
