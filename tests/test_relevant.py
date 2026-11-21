"""w107 phase 1: find_relevant_services (relevant.py)."""

from typing import Any

from crowdcode.relevant import Budget, find_relevant

SERVICES = {
    "text to video generation": [
        {"endpoint": "https://weather.example/forecast", "method": "GET", "name": "Weather", "tier": "new"},
        {"endpoint": "https://video.example/generate", "method": "POST", "name": "Video gen", "tier": "popular"},
    ],
    "video generation": [
        {"endpoint": "https://video.example/generate", "method": "POST", "name": "Video gen", "tier": "popular"},
        {"endpoint": "https://other.example/clip", "method": "POST", "name": "Clips", "tier": "new"},
    ],
}


def fake_search(query: str, *, limit: int, **_: Any) -> dict[str, Any]:
    return {"ok": True, "query": query, "source": "index", "services": SERVICES.get(query, [])[:limit],
            "ranking": "r", "total_matches": 2}


class Chat:
    def __init__(self, picks: Any = None, none_fit: bool = False, fail: str = "") -> None:
        self.calls: list[dict[str, Any]] = []
        self.picks, self.none_fit, self.fail = picks, none_fit, fail

    def __call__(self, system: str, payload: dict[str, Any], usage: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(payload)
        usage.update({"prompt_tokens": 100, "completion_tokens": 10})
        if "candidates" not in payload:
            if self.fail == "rewrite":
                raise RuntimeError("down")
            return {"queries": ["text to video generation", "video generation", "text to video generation"]}
        if self.fail == "rerank":
            raise RuntimeError("down")
        return {"picks": self.picks or [], "none_fit": self.none_fit}


TASK = "Make a short (about 5 second) video of ocean waves at sunset and save it here so I can watch it."


def test_rewrites_pools_and_picks_with_why():
    chat = Chat(picks=[{"n": 2, "fit": "generates video from a prompt"}, {"n": 3}])
    out = find_relevant(TASK, search=fake_search, chat=chat, budget=Budget(10, 10))
    assert out["queries"] == ["text to video generation", "video generation"]
    assert [s["endpoint"] for s in out["services"]] == ["https://video.example/generate", "https://other.example/clip"]
    assert out["services"][0]["fit"] == "generates video from a prompt" and out["services"][0]["rank"] == 1
    assert out["reranked"] is True and out["pool"] == 3 and out["tokens"] == 220
    # The rerank sees numbered cards, deduplicated across queries.
    assert [c["n"] for c in chat.calls[1]["candidates"]] == [1, 2, 3]


def test_says_none_fit_rather_than_offering_loose_matches():
    out = find_relevant(TASK, search=fake_search, chat=Chat(none_fit=True), budget=Budget(10, 10))
    assert out["services"] == [] and out["none_fit"] is True


def test_falls_back_to_todays_ranking_when_the_model_fails_or_is_over_budget():
    for chat in (Chat(fail="rerank"),):
        out = find_relevant(TASK, search=fake_search, chat=chat, budget=Budget(10, 10))
        assert out["reranked"] is False and out["services"][0]["endpoint"] == "https://weather.example/forecast"
    rewrite_down = find_relevant(TASK, search=fake_search, chat=Chat(fail="rewrite", picks=[1]), budget=Budget(10, 10))
    assert rewrite_down["queries"] == [TASK]
    budget = Budget(per_client=1, overall=5)
    chat = Chat(picks=[2])
    assert find_relevant(TASK, search=fake_search, chat=chat, budget=budget, client="a")["reranked"] is True
    capped = find_relevant(TASK, search=fake_search, chat=chat, budget=budget, client="a")
    assert capped["reranked"] is False and len(chat.calls) == 2


def test_a_short_capability_query_is_not_rewritten():
    chat = Chat(picks=[1])
    assert find_relevant("stock price of aapl?", search=fake_search, chat=Chat(picks=[1]), budget=Budget(10, 10))["rewritten"] is True
    out = find_relevant("video generation", search=fake_search, chat=chat, budget=Budget(10, 10))
    assert out["queries"] == ["video generation"] and len(chat.calls) == 1


def test_budget_window_rolls():
    now = [0.0]
    budget = Budget(per_client=1, overall=1, window_s=10, clock=lambda: now[0])
    assert budget.take("x") and not budget.take("y")
    now[0] = 11
    assert budget.take("y")


def test_none_fit_is_not_trusted_when_the_rewrite_failed():
    out = find_relevant(TASK, search=fake_search, chat=Chat(fail="rewrite", none_fit=True), budget=Budget(10, 10))
    assert out["reranked"] is False and "none_fit" not in out
