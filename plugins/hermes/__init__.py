"""CrowdCode for Hermes: cloud agents with their own wallets, owned by this install's wallet.

``register(ctx)`` wires thirteen tools, a ``pre_tool_call`` spend guard, the
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
         "crowdcode_demand": "📈", "crowdcode_files": "🗂", "crowdcode_download": "⬇️",
         "crowdcode_claim": "🔑"}


def _cli_setup(parser) -> None:
    sub = parser.add_subparsers(dest="crowdcode_command")
    wallet = sub.add_parser("wallet", help="Manage this install's CrowdCode wallet")
    wallet_sub = wallet.add_subparsers(dest="wallet_command")
    wallet_sub.add_parser("init", help="Create the wallet if there is none")
    wallet_sub.add_parser("address", help="Print the wallet address")
    export = wallet_sub.add_parser("export", help="Print the private key (to import it into another wallet)")
    export.add_argument("--yes-show-private-key", action="store_true", dest="confirm",
                        help="Required: confirms you want the key printed in this terminal")
    sub.add_parser("login", help="Use your CrowdCode account (prints the Hermes sign-in command)")
    sub.add_parser("logout", help="Disconnect Hermes from your CrowdCode account")
    claim = sub.add_parser("claim", help="Move this wallet's agents into your CrowdCode account")
    claim.add_argument("agents", nargs="*", help="Agent names or ids (default: all)")
    files = sub.add_parser("files", help="List the files an agent produced")
    files.add_argument("agent", help="Agent name or id")
    files.add_argument("--task", default="", help="Only files from this task id")
    download = sub.add_parser("download", help="Download an agent's files into ./crowdcode-files/<agent>/")
    download.add_argument("agent", help="Agent name or id")
    download.add_argument("ids", nargs="+", help="File ids, or 'all'")
    download.add_argument("--task", default="", help="With 'all': only this task's files")
    download.add_argument("--dest", default="", help="Folder to save into")


def _cli_files(args, dashboard: Dashboard) -> int:
    from pathlib import Path

    try:
        if args.crowdcode_command == "files":
            print(dashboard.files_text(args.agent, args.task))
        else:
            tools = dashboard.tools
            agent_id = tools.resolve_agent(args.agent)
            result = tools.download({"agent_id": agent_id, "file_ids": args.ids, "task_id": args.task,
                                     "dest": args.dest}, base=Path.cwd(), allow_outside=True)
            import json

            data = json.loads(result)
            if not data.get("ok"):
                print(f"CrowdCode: {data.get('error')}", file=sys.stderr)
                return 1
            for s in data.get("downloaded") or []:
                print(f"{s['size_bytes']:>10}  {s['path']}")
            for f in data.get("failed") or []:
                print(f"failed  {f.get('name') or f.get('id')}: {f.get('error')}", file=sys.stderr)
            return 1 if data.get("failed") else 0
    except (ValueError, walletlib.WalletMissing) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def _cli_handler(args, dashboard: Dashboard | None = None) -> int:
    command = getattr(args, "crowdcode_command", None)
    if command in ("files", "download") and dashboard is not None:
        return _cli_files(args, dashboard)
    if command in ("login", "logout", "claim") and dashboard is not None:
        print(dashboard(" ".join([command, *getattr(args, "agents", [])])))
        return 0
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
    dashboard = Dashboard(tools)
    ctx.register_command("crowdcode", dashboard,
                         description="CrowdCode wallet, cloud agents, tasks and earnings",
                         args_hint="[login|claim|wallet|agents|tasks|services|files|demand|limits]")
    ctx.register_cli_command(name="crowdcode", help="CrowdCode wallet, account sign-in, claims and agent files",
                             setup_fn=_cli_setup, handler_fn=lambda args: _cli_handler(args, dashboard))


__all__ = ["register"]
