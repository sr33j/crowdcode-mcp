"""Login mode (the Hermes sign-in to a CrowdCode account) and claims, against the mock server.

Hermes's ``credentialed_provider_request`` is stood in for by a function that adds the bearer token the
way Hermes does; the plugin itself never sees a token.
"""
import json
import sys
import types

import httpx

from test_flow import call

MAIN = "0x37A1E4ef2fE11b6ec9DA1f2b2d7A55aE0f9A7B31"


def _hermes(box):
    accountlib = sys.modules["crowdcode_plugin.account"]

    def request(method, url, json_body=None, headers=None, timeout=30.0):
        if not box.get("token"):
            raise accountlib.NotSignedIn("Not signed in to crowdcode for plugin 'crowdcode'. Run `hermes auth add crowdcode:crowdcode`.")
        h = {**(headers or {}), "authorization": f"Bearer {box['token']}"}
        body = None if json_body is None else json.dumps(json_body).encode()
        if body:
            h["content-type"] = "application/json"
        r = httpx.request(method, url, content=body, headers=h)
        return types.SimpleNamespace(status=r.status_code, headers=dict(r.headers), body=r.content)

    return request


def test_claim_then_login_drives_the_account_without_a_wallet_key(ctx, hermes_home, monkeypatch):
    import mock_server as mock

    dashboard = ctx.commands["crowdcode"]
    tools = dashboard.tools
    box: dict = {}
    tools.account_request = _hermes(box)

    # A wallet-owned agent, made the wallet way.
    dashboard("wallet init")
    agent = call(ctx, "crowdcode_create_agent", name="scout", max_price_usd=0.5)["agent_id"]
    assert call(ctx, "crowdcode_fund_agent", agent_id=agent, amount_usd=1)["ok"]
    assert tools.mode() == "wallet"

    # /crowdcode claim signs with the wallet and hands back the approval link.
    text = dashboard("claim scout")
    assert "https://crowdcode.app/claim#cc_claim_" in text and "/crowdcode login" in text
    code = text.split("claim#")[1].split()[0]
    assert mock.approve_claim(code, MAIN) == [agent]  # the owner approves in the browser
    assert call(ctx, "crowdcode_agents")["agents"] == []  # the wallet no longer owns it

    # /crowdcode login: stores the public client id, and says how to sign in (Hermes runs it, in a terminal).
    text = dashboard("login")
    assert "hermes auth add crowdcode:crowdcode" in text
    env = (hermes_home / ".env").read_text()
    assert "CROWDCODE_OAUTH_CLIENT_ID=hermes" in env
    assert tools.mode(refresh=True) == "wallet"  # not signed in yet

    box["token"] = "tok-1"
    mock.STATE.bearers["tok-1"] = MAIN
    assert tools.mode(refresh=True) == "account"
    assert "Signed in to your CrowdCode account" in dashboard("login")
    assert "Account" in dashboard("") and "scout" in dashboard("")

    # The claimed agent, now the account's, runs a task through the sign-in.
    listed = call(ctx, "crowdcode_agents")
    assert [a["id"] for a in listed["agents"]] == [agent]
    task = call(ctx, "crowdcode_run_task", agent_id=agent, budget_usd=0.25, prompt="Summarize what you did last time")
    assert task["ok"] and task["status"] == "completed", task
    # Funding is the owner's approval in the browser: nothing moves, and the guard asks nothing.
    assert ctx.hooks["pre_tool_call"](tool_name="crowdcode_fund_agent", args={"agent_id": agent, "amount_usd": 1}) is None
    funded = call(ctx, "crowdcode_fund_agent", agent_id=agent, amount_usd=1)
    assert funded["ok"] and funded["moved"] is False and funded["approve_url"].endswith(f"/agent/{agent}?fund=1")
    # Withdrawals go to the account's main wallet.
    out = call(ctx, "crowdcode_withdraw", agent_id=agent, amount_usd=0.5, network="base")
    assert out["ok"] and out["to"] == MAIN, out
    assert "main wallet" in out["_approval"]
    # Creating is a wallet-mode act.
    assert "crowdcode.app" in call(ctx, "crowdcode_create_agent", name="another")["error"]

    # The wallet key is not needed in account mode.
    monkeypatch.delenv("CROWDCODE_WALLET_KEY", raising=False)
    assert call(ctx, "crowdcode_agents")["ok"]

    # /crowdcode logout revokes the grant at CrowdCode; auto mode falls back to the wallet.
    assert "stopped working" in dashboard("logout") and "hermes auth logout crowdcode:crowdcode" in dashboard("logout")
    assert "tok-1" not in mock.STATE.bearers
    assert tools.mode(refresh=True) == "wallet"


def test_account_mode_without_a_sign_in_says_how_to_sign_in(ctx, hermes_home):
    tools = ctx.commands["crowdcode"].tools
    tools.account_request = _hermes({})
    ctx.settings["mode"] = "account"
    out = call(ctx, "crowdcode_agents")
    assert out["ok"] is False and "hermes auth add crowdcode:crowdcode" in out["hint"]
    assert "hermes auth add crowdcode:crowdcode" in ctx.commands["crowdcode"]("agents")


def test_a_revoked_token_is_reported_with_the_way_back(ctx, hermes_home):
    import mock_server as mock

    tools = ctx.commands["crowdcode"].tools
    tools.account_request = _hermes({"token": "gone"})
    ctx.settings["mode"] = "account"
    out = call(ctx, "crowdcode_agents")
    assert out["status"] == 401 and "Sign in again" in out["hint"]
    assert "gone" not in mock.STATE.bearers


def test_the_manifest_declares_the_oauth_client_hermes_runs():
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "plugin.yaml").read_text()
    for line in ("id: crowdcode", "authorize_url: https://crowdcode.app/connect", "token_url: https://crowdcode.app/api/app/" + "oauth/" + "token",
                 "client_id_env: CROWDCODE_OAUTH_CLIENT_ID", "hosts: [crowdcode.app]", "- crowdcode_claim", "name: CROWDCODE_OAUTH_CLIENT_ID"):
        assert line in text
    # Hermes refreshes the grant: no plugin code names the token URL (catalog check).
    for path in (Path(__file__).resolve().parent.parent).glob("*.py"):
        assert "oauth/" + "token" not in path.read_text()
