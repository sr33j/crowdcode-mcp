"""Render's health check waits for search to load (backend deploys stalled for minutes without it).

`/startup` is the health check Render switches traffic on; `/ready` keeps its database and redaction checks
and adds the same wait. Until the model, the offering index and the directory are loaded, both answer 503,
so the old instance keeps serving while the new one warms up.
"""

from __future__ import annotations

import os
import threading

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from crowdcode import discovery
from crowdcode import server as server_mod


@pytest.fixture
def warmup(monkeypatch):
    fresh = discovery.Warmup()
    monkeypatch.setattr(discovery, "WARMUP", fresh)
    monkeypatch.setattr(server_mod, "WARMUP", fresh)
    return fresh


def client() -> TestClient:
    return TestClient(Starlette(routes=[Route("/startup", server_mod.startup), Route("/ready", server_mod.ready)]))


def test_startup_answers_503_until_search_has_loaded(warmup):
    http = client()
    early = http.get("/startup")
    assert early.status_code == 503 and early.json()["ready"] is False
    assert http.get("/ready").json()["error_code"] == "warming_up"
    warmup.mark("index (3 offerings)")
    warmup.done.set()
    later = http.get("/startup")
    assert later.status_code == 200
    assert later.json()["stages"]["index (3 offerings)"] >= 0


def test_an_instance_that_cannot_load_still_comes_up(warmup, monkeypatch):
    monkeypatch.setattr(discovery.Warmup, "MAX_SECONDS", 0.0)
    response = client().get("/startup")
    assert response.status_code == 200 and response.json()["overran"] is True


def test_warm_search_marks_the_warmup_done_even_when_loading_fails(warmup, monkeypatch):
    class Embedder:
        def warm(self, wait=False):
            pass

        def ready(self):
            return True

    def broken(*_args, **_kwargs):
        raise RuntimeError("database down")

    monkeypatch.setattr(discovery.offerings.SHARED, "refresh", broken)
    discovery.warm_search(Embedder()).join(timeout=10)
    state = warmup.state()
    assert state["ready"] is True and state["error"] == "RuntimeError"


def test_numeric_libraries_are_held_to_one_thread():
    # Set by crowdcode/__init__.py before numpy or the tokenizer load (a 0.5-CPU instance).
    assert os.environ["OPENBLAS_NUM_THREADS"] == "1" and os.environ["TOKENIZERS_PARALLELISM"] == "false"
    assert threading.active_count() >= 1


def test_startup_reports_this_processs_memory_and_its_peak(warmup):
    """The backend has 512 MB and the index build is its peak (2026-11-19): /startup says both."""
    warmup.done.set()
    memory = client().get("/startup").json()["memory"]
    assert 0 < memory["rss_mb"] <= memory["peak_mb"]
