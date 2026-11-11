"""Load the plugin directory as a package and give it a fake Hermes context."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "dev"))


def _stub_hermes_credentials():
    """Without Hermes installed (plain CI), stand in for its .env writer: the same file it writes,
    plus the process environment, which is where the plugin's fallback read looks."""
    try:
        import hermes_cli.config  # noqa: F401
        return
    except ImportError:
        pass
    import types

    def save_env_value(key, value):
        home = Path(os.environ["HERMES_HOME"])
        home.mkdir(parents=True, exist_ok=True)
        with open(home / ".env", "a") as fh:
            fh.write(f"{key}={value}\n")
        os.environ[key] = value

    package = types.ModuleType("hermes_cli")
    config = types.ModuleType("hermes_cli.config")
    config.save_env_value = save_env_value
    package.config = config
    sys.modules["hermes_cli"] = package
    sys.modules["hermes_cli.config"] = config


_stub_hermes_credentials()


def _load_plugin():
    if "crowdcode_plugin" in sys.modules:
        return sys.modules["crowdcode_plugin"]
    spec = importlib.util.spec_from_file_location("crowdcode_plugin", ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["crowdcode_plugin"] = module
    spec.loader.exec_module(module)
    # The plugin directory is itself the package, so pytest also imports it by its directory name
    # (to look for package-level setup); hand it the module we already loaded.
    sys.modules.setdefault(ROOT.name, module)
    sys.modules.setdefault("__init__", module)
    return module


_load_plugin()


class FakeState:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


class FakeCtx:
    def __init__(self, settings=None):
        self.settings = dict(settings or {})
        self.state = FakeState()
        self.tools, self.hooks, self.commands, self.cli, self.skills = {}, {}, {}, {}, {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = handler

    def register_hook(self, name, callback, **kw):
        self.hooks[name] = callback

    def register_command(self, name, handler, **kw):
        self.commands[name] = handler

    def register_skill(self, name, path, **kw):
        self.skills[name] = path

    def register_cli_command(self, name, **kw):
        self.cli[name] = kw


@pytest.fixture()
def plugin():
    return _load_plugin()


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.delenv("CROWDCODE_WALLET_KEY", raising=False)
    yield tmp_path / "hermes"
    os.environ.pop("CROWDCODE_WALLET_KEY", None)


@pytest.fixture()
def mock_server():
    import mock_server as mock

    mock.STATE.__init__()
    server = mock.serve(0)
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture()
def ctx(plugin, hermes_home, mock_server, monkeypatch):
    c = FakeCtx({"base_url": mock_server, "base_rpc_url": mock_server + "/rpc"})
    plugin.register(c)
    tools_mod = sys.modules["crowdcode_plugin.tools"]
    monkeypatch.setattr(tools_mod, "CREATE_WAIT_SECONDS", 10)
    return c
