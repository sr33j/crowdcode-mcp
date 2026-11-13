import json
from pathlib import Path
import sys


def call(ctx, tool, **args):
    """Run a tool the way Hermes does: guard first, then the handler."""
    decision = ctx.hooks["pre_tool_call"](tool_name=tool, args=args, task_id="t")
    if decision and decision.get("action") == "block":
        return {"ok": False, "blocked": decision["message"]}
    out = json.loads(ctx.tools[tool](args))
    if decision and decision.get("action") == "approve":
        out["_approval"] = decision["message"]
    return out


def test_registers_everything(ctx):
    assert len(ctx.tools) == 12
    assert "pre_tool_call" in ctx.hooks and "crowdcode" in ctx.commands and "crowdcode" in ctx.cli
    assert "build-a-paid-service" in ctx.skills


def test_no_wallet_is_a_clear_error(ctx):
    out = call(ctx, "crowdcode_agents")
    assert out["ok"] is False and "wallet init" in out["error"]
    assert "No CrowdCode wallet" in ctx.commands["crowdcode"]("")


def test_full_flow_against_mock(ctx, hermes_home):
    # wallet init stores the key through Hermes (.env under HERMES_HOME), never prints it
    text = ctx.commands["crowdcode"]("wallet init")
    assert "Created your CrowdCode wallet" in text
    env = (hermes_home / ".env").read_text()
    assert "CROWDCODE_WALLET_KEY=" in env
    key = env.split("CROWDCODE_WALLET_KEY=")[1].split()[0].strip("'\"")
    assert key not in text
    assert "already exists" in ctx.commands["crowdcode"]("wallet init")

    created = call(ctx, "crowdcode_create_agent", name="scout", max_price_usd=0.5)
    assert created["ok"], created
    assert created["status"] == "active" and created["paid_usd"] == 0.35 and created["payment_tx"]
    agent = created["agent_id"]

    funded = call(ctx, "crowdcode_fund_agent", agent_id=agent, amount_usd=1)
    assert funded["ok"], funded
    assert "_approval" in funded and "$1.00" in funded["_approval"] and agent in funded["_approval"]

    assert "minimum" in call(ctx, "crowdcode_fund_agent", agent_id=agent, amount_usd=0.25)["error"]

    listed = call(ctx, "crowdcode_agents")
    assert listed["agents"][0]["balances_usd"]["base"] == "1.000000"

    task = call(ctx, "crowdcode_run_task", agent_id=agent, budget_usd=0.25,
                prompt="Build a tiny paid service that counts words")
    assert task["ok"] and task["status"] == "completed", task
    assert task["purchases"][0]["tx"] and float(task["spent_usd"]) == 0.02
    services = call(ctx, "crowdcode_services", agent_id=agent)
    assert services["services"][0]["slug"] == "scout-svc"
    sid = services["services"][0]["id"]
    stopped = call(ctx, "crowdcode_services", agent_id=agent, action="stop", service_id=sid)
    assert stopped["services"][0]["status"] == "stopped"

    # second agent, transfer, withdraw
    other = call(ctx, "crowdcode_create_agent", name="helper")["agent_id"]
    moved = call(ctx, "crowdcode_transfer", from_agent_id=agent, to_agent_id=other, amount_usd=0.5)
    assert moved["ok"], moved
    out = call(ctx, "crowdcode_withdraw", agent_id=other, amount_usd=0.5)
    assert out["ok"] and out["tx_hash"]
    assert call(ctx, "crowdcode_withdraw", agent_id=other, amount_usd=0.1)["ok"] is False  # below minimum
    assert call(ctx, "crowdcode_withdraw", agent_id=other, amount_usd=1, network="solana")["ok"] is False

    # the ledger counts creation x2 + funding + the task's real spend, not withdraw/transfer
    policy = ctx.hooks["pre_tool_call"].__self__
    assert abs(policy.spent_today() - (0.35 * 2 + 1 + 0.02)) < 1e-6

    dash = ctx.commands["crowdcode"]("")
    assert "scout" in dash and "Recent tasks" in dash and "scout-svc" in dash
    assert "Bill Table Extractor" in ctx.commands["crowdcode"]("demand")


def test_create_refuses_price_above_max(ctx):
    ctx.commands["crowdcode"]("wallet init")
    out = call(ctx, "crowdcode_create_agent", name="pricey", max_price_usd=0.1)
    assert out["ok"] is False and "nothing was paid" in out["error"]
    import mock_server
    assert not mock_server.STATE.agents


def test_not_owner(ctx, monkeypatch):
    ctx.commands["crowdcode"]("wallet init")
    agent = call(ctx, "crowdcode_create_agent", name="mine")["agent_id"]
    # another wallet
    import mock_server
    mock_server.STATE.agents[agent]["owner"] = "eip155:8453:0x0000000000000000000000000000000000000001"
    out = call(ctx, "crowdcode_agents", agent_id=agent)
    assert out["status"] == 403 and "does not own" in out["hint"]


def test_replayed_and_tampered_signatures_rejected(ctx):
    ctx.commands["crowdcode"]("wallet init")
    import httpx
    wallet = sys.modules["crowdcode_plugin.wallet"].load()
    base = ctx.settings["base_url"]
    headers = wallet.sign_request("GET", "/api/app/v1/me", b"")
    assert httpx.get(base + "/api/app/v1/me", headers=headers).status_code == 200
    replay = httpx.get(base + "/api/app/v1/me", headers=headers)
    assert replay.status_code == 401 and replay.json()["error"] == "replayed"
    headers = wallet.sign_request("POST", "/api/app/v1/agents/x/withdraw", b'{"amount_usd":1}')
    headers["content-type"] = "application/json"
    tampered = httpx.post(base + "/api/app/v1/agents/x/withdraw", content=b'{"amount_usd":9}', headers=headers)
    assert tampered.status_code == 401 and tampered.json()["error"] == "bad_signature"
    old = wallet.sign_request("GET", "/api/app/v1/me", b"", now=1)
    assert httpx.get(base + "/api/app/v1/me", headers=old).json()["error"] == "expired"


def test_server_error_after_sending_keeps_the_reservation(ctx, monkeypatch):
    ctx.commands["crowdcode"]("wallet init")
    agent = call(ctx, "crowdcode_create_agent", name="scout")["agent_id"]
    client = sys.modules["crowdcode_plugin.client"]
    real = client.CrowdCode.signed

    def flaky(self, method, path, payload=None, extra_headers=None):
        if path.endswith("/deposits"):
            raise client.ApiError(502, "bad_gateway")
        return real(self, method, path, payload, extra_headers)

    monkeypatch.setattr(client.CrowdCode, "signed", flaky)
    policy = ctx.hooks["pre_tool_call"].__self__
    before = policy.spent_today()
    out = call(ctx, "crowdcode_fund_agent", agent_id=agent, amount_usd=0.5)
    assert out["ok"] is False and "Do NOT retry" in out["error"]
    assert abs(policy.spent_today() - before - 0.5) < 1e-9


def test_files_list_and_download(ctx, tmp_path):
    ctx.commands["crowdcode"]("wallet init")
    agent = call(ctx, "crowdcode_create_agent", name="scout")["agent_id"]
    call(ctx, "crowdcode_fund_agent", agent_id=agent, amount_usd=1)
    task = call(ctx, "crowdcode_run_task", agent_id=agent, budget_usd=0.25, prompt="write a report")
    assert task["status"] == "completed" and len(task["files"]) == 3
    assert all("url" not in f and f["id"] for f in task["files"]) and "crowdcode_download" in task["files_note"]
    assert not (tmp_path / "crowdcode-files").exists()  # nothing downloads by itself

    listed = call(ctx, "crowdcode_files", agent_id=agent)
    assert listed["count"] == 3 and listed["files"][0]["name"] == "report.md"

    tools = sys.modules["crowdcode_plugin.tools"]
    t = tools.Tools(ctx, ctx.hooks["pre_tool_call"].__self__)
    one = json.loads(t.download({"agent_id": agent, "file_ids": [listed["files"][-1]["id"]]}, base=tmp_path))
    assert one["ok"] and len(one["downloaded"]) == 1
    target = tmp_path / "crowdcode-files" / "scout"
    every = json.loads(t.download({"agent_id": agent, "file_ids": ["all"]}, base=tmp_path))
    names = sorted(p.name for p in target.iterdir())
    # the traversal name became a plain file inside the folder; nothing was overwritten
    assert names == ["notes.txt", "report-1.md", "report-2.md", "report.md"], names
    assert all(Path(s["path"]).parent == target for s in every["downloaded"])
    assert (target / "report.md").read_bytes().startswith(b"# Report")
    assert not (tmp_path / "outside").exists()

    missing = json.loads(t.download({"agent_id": agent, "file_ids": ["fl_nope"]}, base=tmp_path))
    assert missing["ok"] is False and "No such file" in missing["error"]
    text = ctx.commands["crowdcode"]("files scout")
    assert "Files of scout" in text and "notes.txt" in text


def test_safe_names():
    files = sys.modules["crowdcode_plugin.files"]
    assert files.safe_name("../../outside/notes.txt") == "notes.txt"
    assert files.safe_name("..\\\\win\\\\evil.exe") == "evil.exe"
    assert files.safe_name("..") == "file" and files.safe_name("") == "file"
    assert files.safe_name("a\x00b<c>.txt") == "a_b_c_.txt"
    assert files.safe_name("CON.txt") == "_CON.txt"
    assert len(files.safe_name("x" * 400 + ".pdf").encode()) <= 200 and files.safe_name("x" * 400 + ".pdf").endswith(".pdf")


def _agent_with_files(ctx):
    ctx.commands["crowdcode"]("wallet init")
    agent = call(ctx, "crowdcode_create_agent", name="scout")["agent_id"]
    call(ctx, "crowdcode_fund_agent", agent_id=agent, amount_usd=1)
    call(ctx, "crowdcode_run_task", agent_id=agent, budget_usd=0.25, prompt="write a report")
    return agent


def test_download_dest_outside_session_needs_the_human(ctx, tmp_path, monkeypatch):
    agent = _agent_with_files(ctx)
    files = sys.modules["crowdcode_plugin.files"]
    session = tmp_path / "session"
    session.mkdir()
    monkeypatch.setattr(files, "session_dir", lambda: session)
    outside = tmp_path / "elsewhere"

    # inside the session folder: no prompt, saved
    assert ctx.hooks["pre_tool_call"](tool_name="crowdcode_download", args={"agent_id": agent, "file_ids": ["all"],
                                      "dest": "out"}, task_id="t") is None
    # outside it: the hook asks the human
    decision = ctx.hooks["pre_tool_call"](tool_name="crowdcode_download",
                                          args={"agent_id": agent, "file_ids": ["all"], "dest": str(outside)}, task_id="t")
    assert decision["action"] == "approve" and str(outside) in decision["message"]

    tools = sys.modules["crowdcode_plugin.tools"]
    t = tools.Tools(ctx, ctx.hooks["pre_tool_call"].__self__)
    # without the hook's approval (called directly), the handler refuses an outside folder ...
    files.consume_approval(outside.resolve())
    refused = json.loads(t.download({"agent_id": agent, "file_ids": ["all"], "dest": str(outside)}))
    assert refused["ok"] is False and "outside this session's folder" in refused["error"] and not outside.exists()
    # ... "~" and "../" count as outside too
    for sneaky in ("~/crowdcode-test-should-not-exist", "../escape"):
        out = json.loads(t.download({"agent_id": agent, "file_ids": ["all"], "dest": sneaky}))
        assert out["ok"] is False, sneaky
    assert not (tmp_path / "escape").exists()
    # after the hook asked (and Hermes let the call through), it goes ahead once
    ctx.hooks["pre_tool_call"](tool_name="crowdcode_download",
                               args={"agent_id": agent, "file_ids": ["all"], "dest": str(outside)}, task_id="t")
    ok = json.loads(t.download({"agent_id": agent, "file_ids": ["all"], "dest": str(outside)}))
    assert ok["ok"] and len(ok["downloaded"]) == 3 and outside.is_dir()
    again = json.loads(t.download({"agent_id": agent, "file_ids": ["all"], "dest": str(outside)}))
    assert again["ok"] is False  # the approval was used up


def test_download_respects_hermes_write_policy(ctx, tmp_path, monkeypatch):
    agent = _agent_with_files(ctx)
    files = sys.modules["crowdcode_plugin.files"]
    monkeypatch.setattr(files, "session_dir", lambda: tmp_path)
    protected = tmp_path / "keys"
    calls = []

    def denied(path):
        calls.append(str(path))
        return f"Write denied: '{path}' is a protected system/credential file." if "keys" in Path(path).parts else None

    monkeypatch.setattr(files, "write_denied", denied)
    decision = ctx.hooks["pre_tool_call"](tool_name="crowdcode_download",
                                          args={"agent_id": agent, "file_ids": ["all"], "dest": "keys"}, task_id="t")
    assert decision["action"] == "block" and "protected" in decision["message"]
    tools = sys.modules["crowdcode_plugin.tools"]
    t = tools.Tools(ctx, ctx.hooks["pre_tool_call"].__self__)
    out = json.loads(t.download({"agent_id": agent, "file_ids": ["all"], "dest": "keys"}))
    assert out["ok"] is False and "protected" in out["error"] and not protected.exists()
    # the default folder is checked too, and so is every file before it is created
    ok = json.loads(t.download({"agent_id": agent, "file_ids": ["all"]}))
    assert ok["ok"] and len(ok["downloaded"]) == 3
    saved = {s["path"] for s in ok["downloaded"]}
    assert str((tmp_path / "crowdcode-files" / "scout").resolve()) in calls and saved <= set(calls)


def test_write_denied_uses_hermes_file_safety():
    files = sys.modules["crowdcode_plugin.files"]
    try:
        import agent.file_safety  # noqa: F401
    except ImportError:
        import pytest
        pytest.skip("Hermes not installed")
    runtime = Path(sys.prefix)  # Hermes protects its own interpreter/venv
    assert files.write_denied(runtime / "lib" / "planted.py")
    assert files.dest_denied(runtime / "new-folder")
    assert files.write_denied(Path.home() / "crowdcode-files" / "ok.txt") is None


def test_never_roots(monkeypatch, tmp_path):
    files = sys.modules["crowdcode_plugin.files"]
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    for bad in (tmp_path / ".config" / "autostart" / "x.desktop", tmp_path / "Library" / "LaunchAgents" / "x.plist",
                tmp_path / ".config" / "systemd" / "user" / "x.service", tmp_path / "hermes-home" / "plugins" / "x"):
        assert files.write_denied(bad), bad
    assert files.dest_denied(tmp_path / ".config" / "autostart")
    assert files.write_denied(tmp_path / "Downloads" / "x.txt") is None
