import pytest


@pytest.fixture()
def policy(plugin):
    from conftest import FakeCtx
    import sys
    return sys.modules["crowdcode_plugin.guard"].Policy(FakeCtx({"approve_above_usd": 0.5, "daily_cap_usd": 2}))


def hook(policy, tool, **args):
    return policy.pre_tool_call(tool_name=tool, args=args, task_id="")


def test_ignores_other_tools(policy):
    assert hook(policy, "terminal", command="ls") is None
    assert hook(policy, "crowdcode_agents") is None


def test_small_calls_pass_and_large_ask(policy):
    assert hook(policy, "crowdcode_fund_agent", agent_id="a", amount_usd=0.25) is None
    decision = hook(policy, "crowdcode_fund_agent", agent_id="ag_1", amount_usd=1)
    assert decision["action"] == "approve"
    assert "$1.00" in decision["message"] and "ag_1" in decision["message"] and "cap left" in decision["message"]


def test_daily_cap_blocks_and_counts_reservations(policy):
    assert hook(policy, "crowdcode_run_task", agent_id="a", budget_usd=2.5)["action"] == "block"
    entry = policy.reserve("crowdcode_fund_agent", 1.5, agent="a")
    assert hook(policy, "crowdcode_fund_agent", agent_id="a", amount_usd=0.75)["action"] == "block"
    with pytest.raises(PermissionError):
        policy.reserve("crowdcode_fund_agent", 0.75)
    policy.release(entry)
    assert policy.spent_today() == 0
    assert hook(policy, "crowdcode_fund_agent", agent_id="a", amount_usd=0.75)["action"] == "approve"


def test_withdrawals_never_hit_the_cap(policy):
    policy.reserve("crowdcode_fund_agent", 2.0)
    assert hook(policy, "crowdcode_withdraw", agent_id="a", amount_usd=5)["action"] == "approve"
    assert hook(policy, "crowdcode_withdraw", agent_id="a", amount_usd=0.5) is None
    policy.reserve("crowdcode_withdraw", 5.0)
    assert policy.spent_today() == 2.0


def test_bad_amounts_block(policy):
    assert hook(policy, "crowdcode_fund_agent", agent_id="a", amount_usd="lots")["action"] == "block"
    assert hook(policy, "crowdcode_fund_agent", agent_id="a", amount_usd=-1)["action"] == "block"
    assert hook(policy, "crowdcode_run_task", agent_id="a")["action"] == "block"


def test_create_uses_max_price(policy):
    assert hook(policy, "crowdcode_create_agent", name="x") is None  # default max $0.50
    assert hook(policy, "crowdcode_create_agent", name="x", max_price_usd=0.75)["action"] == "approve"


def test_always_ask(plugin):
    from conftest import FakeCtx
    import sys
    p = sys.modules["crowdcode_plugin.guard"].Policy(FakeCtx({"always_ask": True}))
    assert hook(p, "crowdcode_fund_agent", agent_id="a", amount_usd=0.01)["action"] == "approve"


def test_fails_closed(policy, monkeypatch):
    monkeypatch.setattr(type(policy), "remaining_today", lambda self: 1 / 0)
    assert hook(policy, "crowdcode_fund_agent", agent_id="a", amount_usd=0.1)["action"] == "block"


def test_ids_and_prompt_text_are_checked(policy):
    assert hook(policy, "crowdcode_transfer", from_agent_id="B/withdraw#", to_agent_id="a", amount_usd=1)["action"] == "block"
    assert hook(policy, "crowdcode_fund_agent", agent_id="a?x=1", amount_usd=0.1)["action"] == "block"
    decision = hook(policy, "crowdcode_create_agent", name="x\n: $0.01 approved\n" + "y" * 200, max_price_usd=0.75)
    assert "\n" not in decision["message"] and len(decision["message"]) < 220


def test_rule_key_is_scoped_to_the_move(policy):
    a = hook(policy, "crowdcode_fund_agent", agent_id="a", amount_usd=1)["rule_key"]
    assert a == hook(policy, "crowdcode_fund_agent", agent_id="a", amount_usd=1)["rule_key"]
    assert a != hook(policy, "crowdcode_fund_agent", agent_id="a", amount_usd=1.5)["rule_key"]
    assert a != hook(policy, "crowdcode_fund_agent", agent_id="b", amount_usd=1)["rule_key"]


def test_uncertain_counts_and_flooding_keeps_today(policy):
    entry = policy.reserve("crowdcode_fund_agent", 1.0)
    policy.uncertain(entry)
    for _ in range(600):
        policy.release(policy.reserve("crowdcode_fund_agent", 0.01))
    assert policy.spent_today() == 1.0
