"""Tool schemas (OpenAI function format) shown to the model."""

_AGENT = {"type": "string", "description": "CrowdCode agent id (from crowdcode_agents or crowdcode_create_agent)."}
_AGENT_REF = {"type": "string", "description": "The agent's id or its name."}
_NETWORK = {"type": "string", "enum": ["base", "solana", "tempo"], "description": "Chain the USDC is on. Default base."}


def _tool(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required or []}}


SCHEMAS = {
    "crowdcode_wallet": _tool(
        "crowdcode_wallet",
        "Show this Hermes install's CrowdCode wallet: its Base address, USDC balance (one read-only Base RPC call), "
        "today's spending against the daily cap, and how to fund it. With init=true, create the wallet first if "
        "there is none (the key is stored in Hermes's credential store and never shown).",
        {"init": {"type": "boolean", "description": "Create the wallet if it does not exist yet."}}),
    "crowdcode_agents": _tool(
        "crowdcode_agents",
        "List the CrowdCode cloud agents this wallet owns with their USDC balances on Base, Solana and Tempo, "
        "or show one agent in detail. Read-only.",
        {"agent_id": _AGENT}),
    "crowdcode_create_agent": _tool(
        "crowdcode_create_agent",
        "Create a CrowdCode cloud agent owned by this wallet. Pays CrowdCode's creation price (about $0.35 USDC on "
        "Base) with an x402 payment signed by the wallet. The agent gets its own wallets on Base, Solana and Tempo, "
        "can run tasks, call paid x402/MPP services and deploy paid services of its own.",
        {"name": {"type": "string", "description": "3-40 characters."},
         "model": {"type": "string", "description": "Optional CrowdCode model catalog id."},
         "instructions": {"type": "string", "description": "Optional standing instructions, at most 4000 characters."},
         "max_price_usd": {"type": "number", "description": "Refuse to pay more than this. Default 0.50."}},
        ["name"]),
    "crowdcode_fund_agent": _tool(
        "crowdcode_fund_agent",
        "Send USDC on Base from this wallet to one of its agents. Gasless: the wallet signs an EIP-3009 "
        "authorization for exactly this amount to the agent's own address and CrowdCode's relayer submits it. "
        "Minimum $0.50.",
        {"agent_id": _AGENT, "amount_usd": {"type": "number", "description": "USDC to send, e.g. 1.00."}},
        ["agent_id", "amount_usd"]),
    "crowdcode_run_task": _tool(
        "crowdcode_run_task",
        "Give a CrowdCode agent a task. It runs in the cloud and spends from the agent's own wallet, never more than "
        "budget_usd: it can research, write code, buy x402/MPP services and deploy paid services (deploy_service). "
        "Waits for the result by default and returns the answer, the amount spent, every purchase with its "
        "transaction, and any files it produced.",
        {"agent_id": _AGENT,
         "prompt": {"type": "string", "description": "The task, at most 20000 characters."},
         "budget_usd": {"type": "number", "description": "Required spending cap for this task in USD."},
         "model": {"type": "string", "description": "Optional model id for this task."},
         "wait": {"type": "boolean", "description": "Wait for completion (default true)."},
         "timeout_seconds": {"type": "number", "description": "How long to wait before returning (default 600)."}},
        ["agent_id", "prompt", "budget_usd"]),
    "crowdcode_task_status": _tool(
        "crowdcode_task_status",
        "Check a CrowdCode task: status, answer, spend, purchases and the files it produced (ids; download them "
        "with crowdcode_download). With cancel=true, cancel it first.",
        {"agent_id": _AGENT, "task_id": {"type": "string"},
         "cancel": {"type": "boolean", "description": "Cancel the task."}},
        ["agent_id", "task_id"]),
    "crowdcode_services": _tool(
        "crowdcode_services",
        "List the paid services an agent has deployed, with price, calls and USDC earned, or stop/start one.",
        {"agent_id": _AGENT,
         "action": {"type": "string", "enum": ["list", "stop", "start"], "description": "Default list."},
         "service_id": {"type": "string", "description": "Required for stop/start."}},
        ["agent_id"]),
    "crowdcode_withdraw": _tool(
        "crowdcode_withdraw",
        "Withdraw USDC from an agent. It can only go to this wallet (the agent's owner), never anywhere else. "
        "Minimum $0.50.",
        {"agent_id": _AGENT, "amount_usd": {"type": "number"},
         "network": {"type": "string", "enum": ["base", "tempo"], "description": "Default base."}},
        ["agent_id", "amount_usd"]),
    "crowdcode_transfer": _tool(
        "crowdcode_transfer",
        "Move USDC between two agents this wallet owns, on one network. Minimum $0.50.",
        {"from_agent_id": _AGENT, "to_agent_id": _AGENT, "amount_usd": {"type": "number"}, "network": _NETWORK},
        ["from_agent_id", "to_agent_id", "amount_usd"]),
    "crowdcode_demand": _tool(
        "crowdcode_demand",
        "What other AI agents have asked CrowdCode for and would pay for: public service proposals with the number "
        "of agents asking and the price they named. Good ideas for a paid service an agent could build. Read-only.",
        {"query": {"type": "string", "description": "Optional keyword filter."},
         "limit": {"type": "integer", "description": "How many to return (default 10, max 50)."}}),
    "crowdcode_files": _tool(
        "crowdcode_files",
        "List the files an agent has produced across its tasks (newest first): id, name, size, type, task. "
        "Read-only; nothing is downloaded.",
        {"agent_id": _AGENT_REF, "task_id": {"type": "string", "description": "Only files from this task."}},
        ["agent_id"]),
    "crowdcode_download": _tool(
        "crowdcode_download",
        "Download an agent's files to this machine, only when the user asks for them. Saves into "
        "./crowdcode-files/<agent name>/ under the session's working directory unless dest is given, keeps the "
        "file names (made safe), never overwrites (adds -1, -2…), and returns the local paths and sizes.",
        {"agent_id": _AGENT_REF,
         "file_ids": {"type": "array", "items": {"type": "string"},
                      "description": 'File ids from crowdcode_files, or ["all"] for every file.'},
         "task_id": {"type": "string", "description": "With \"all\": only that task's files."},
         "dest": {"type": "string", "description": "Folder to save into, inside the session's working directory "
                  "(created if missing). A folder outside it asks the user first."}},
        ["agent_id", "file_ids"]),
}
