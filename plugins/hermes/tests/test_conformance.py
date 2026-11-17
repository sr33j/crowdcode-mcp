"""spec/conformance.json against this plugin: the session every CrowdCode agent client must pass.

The same steps run against crowdcode-mcp (packages/mcp/test/conformance.test.ts). Tool steps call the
plugin's handlers directly; Hermes's approval prompts are a Hermes extra and are not part of the contract.
"""
import base64
import hashlib
import json
import secrets
import sys
import types
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

SPEC = Path(__file__).resolve().parents[3] / "spec"
CONTRACT = json.loads((SPEC / "agent-tools.json").read_text())
SESSION = json.loads((SPEC / "conformance.json").read_text())
RESULT_FIELDS = {t["name"]: t["result_fields"] for t in CONTRACT["tools"]}


def resolve(value, saved):
    if isinstance(value, str) and value.startswith("$"):
        return saved[value[1:]]
    if isinstance(value, list):
        return [resolve(v, saved) for v in value]
    if isinstance(value, dict):
        return {k: resolve(v, saved) for k, v in value.items()}
    return value


def get(data, path):
    for part in path.split("."):
        data = data[int(part)] if isinstance(data, list) else data[part]
    return data


def check(step, result, saved):
    where = f"step '{step['name']}': {json.dumps(result)[:600]}"
    assert result.get("ok") is step.get("ok", True), where
    if result.get("ok"):
        for field in RESULT_FIELDS[step["tool"]]:
            assert field in result, f"missing result field {field!r} in {where}"
    for path, want in resolve(step.get("equals", {}), saved).items():
        got = get(result, path)
        assert got == want or (isinstance(want, float) and isinstance(got, (int, float)) and abs(got - want) < 1e-9), \
            f"{path}={got!r}, want {want!r} in {where}"
    for path, n in step.get("length", {}).items():
        assert len(get(result, path)) == n, f"len({path}) != {n} in {where}"
    for path, part in step.get("contains", {}).items():
        assert part in str(get(result, path)), f"{path} lacks {part!r} in {where}"
    if "error_contains" in step:
        assert step["error_contains"] in result.get("error", ""), where
    for name, path in step.get("save", {}).items():
        saved[name] = get(result, path)


def oauth_login(base: str) -> str:
    """What Hermes's `hermes auth add crowdcode:crowdcode` does, against the mock (which approves at once)."""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    redirect = "http://127.0.0.1:1/callback"
    query = {"client_id": "hermes", "redirect_uri": redirect, "response_type": "code", "state": "s",
             "scope": "read tasks transfers withdraw_to_main services files", "code_challenge": challenge,
             "code_challenge_method": "S256"}
    r = httpx.get(f"{base}/connect?{urlencode(query)}", follow_redirects=False)
    code = parse_qs(urlsplit(r.headers["location"]).query)["code"][0]
    token = httpx.post(f"{base}/api/app/oauth/token", data={"grant_type": "authorization_code", "code": code,
                       "code_verifier": verifier, "redirect_uri": redirect, "client_id": "hermes"}).json()
    return token["access_token"]


def hermes_request(box):
    """Hermes's credentialed_provider_request: it adds the bearer token; the plugin never sees it."""
    def request(method, url, json_body=None, headers=None, timeout=30.0):
        h = {**(headers or {}), "authorization": f"Bearer {box['token']}"}
        body = None if json_body is None else json.dumps(json_body).encode()
        if body:
            h["content-type"] = "application/json"
        r = httpx.request(method, url, content=body, headers=h, timeout=timeout)
        return types.SimpleNamespace(status=r.status_code, headers=dict(r.headers), body=r.content)
    return request


def test_tool_names_and_arguments_match_the_contract(plugin):
    schemas = sys.modules["crowdcode_plugin.schemas"].SCHEMAS
    assert [t["name"] for t in CONTRACT["tools"]] == list(schemas)
    for tool in CONTRACT["tools"]:
        mine = schemas[tool["name"]]["parameters"]
        assert set(mine["properties"]) == set(tool["input_schema"]["properties"]), tool["name"]
        assert set(mine.get("required", [])) == set(tool["input_schema"].get("required", [])), tool["name"]


def test_conformance_session(ctx, tmp_path, monkeypatch):
    files = sys.modules["crowdcode_plugin.files"]
    monkeypatch.setattr(files, "session_dir", lambda: tmp_path)
    tools = ctx.commands["crowdcode"].tools
    base = ctx.settings["base_url"]
    saved: dict = {}
    for step in SESSION["steps"]:
        if step.get("harness") == "approve_claim":
            code = resolve(step["args"]["url"], saved).split("#", 1)[1]
            assert httpx.post(f"{base}/__mock/approve_claim", json={"code": code}).status_code == 200
            continue
        if step.get("harness") == "login":
            tools.account_request = hermes_request({"token": oauth_login(base)})
            sys.modules["crowdcode_plugin.account"].prepare_client_id()
            assert tools.mode(refresh=True) == "account"
            continue
        result = json.loads(ctx.tools[step["tool"]](resolve(step.get("args", {}), saved)))
        check(step, result, saved)
