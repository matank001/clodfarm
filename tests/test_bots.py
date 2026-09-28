"""Bots: Claude Code on another model, through a provider that speaks Anthropic's Messages API."""
import json
import os
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import cli
from clodfarm import bots
from clodfarm.agents import AgentManager
from clodfarm.config import load
from test_farm import calls, repo_files, start_farm, stop_farm, wait_for


@pytest.fixture
def provider():
    """A stand-in for OpenRouter: key `good` works; model `missing` is unknown, `busy` is rate limited and
    `chat` answers in another API's format."""
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            seen.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
            model, auth = body.get("model"), self.headers.get("Authorization")
            code, out = 200, {"type": "message", "role": "assistant", "content": [{"type": "text", "text": "ok"}]}
            if self.path != "/api/v1/messages":
                code, out = 404, {"error": {"message": "no route"}}
            elif auth not in ("Bearer good", None) or (auth is None and model != "local"):
                code, out = 401, {"error": {"message": "No auth credentials found"}}
            elif model == "missing":
                code, out = 404, {"error": {"message": "model not found"}}
            elif model == "busy":
                code, out = 429, {"error": {"message": "slow down"}}
            elif model == "chat":
                out = {"choices": [{"message": {"content": "ok"}}]}
            data = json.dumps(out).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/api", seen
    srv.shutdown()


def test_a_bot_is_parsed_the_way_providers_write_it():
    b = bots.parse({"provider": "openrouter", "model": "qwen/qwen3-coder:free"})
    assert b == {"provider": "openrouter", "url": "https://openrouter.ai/api", "model": "qwen/qwen3-coder:free",
                 "takes": "sent", "workers": 1}
    assert bots.parse({"provider": "custom", "url": "https://gw.example.com/v1/", "model": "m"})["url"] == \
        "https://gw.example.com", "Claude Code adds /v1/messages itself"
    assert bots.parse({"provider": "ollama", "model": "qwen3-coder", "takes": "any"})["takes"] == "any"
    for bad in ({"provider": "gpt", "model": "m"}, {"provider": "custom", "model": "m"},
                {"provider": "custom", "url": "https://user:pw@gw.example.com", "model": "m"},
                {"provider": "custom", "url": "ftp://gw.example.com", "model": "m"},
                {"provider": "openrouter", "model": ""}, {"provider": "openrouter", "model": "a b"},
                {"provider": "openrouter", "model": "m", "workers": 9}):
        with pytest.raises(ValueError):
            bots.parse(bad)
    with pytest.raises(ValueError, match="needs an API key"):
        bots.check_key("openrouter", "")
    assert bots.check_key("ollama", "") == ""
    with pytest.raises(ValueError):
        bots.check_key("custom", "two words")


def test_a_bot_is_kept_only_once_its_model_answers(provider):
    url, seen = provider
    bot = bots.parse({"provider": "custom", "url": url, "model": "qwen/qwen3-coder:free"})
    assert bots.check(bot, "good") == "ok"
    assert seen[-1]["auth"] == "Bearer good", "the way Claude Code sends ANTHROPIC_AUTH_TOKEN"
    assert seen[-1]["body"]["model"] == "qwen/qwen3-coder:free"
    assert bots.check({**bot, "model": "local"}, "") == "ok", "a provider without keys (Ollama)"
    with pytest.raises(ValueError, match="refused the key.*No auth credentials"):
        bots.check(bot, "wrong")
    with pytest.raises(ValueError, match="no such model"):
        bots.check({**bot, "model": "missing"}, "good")
    with pytest.raises(ValueError, match="not with Anthropic's Messages API"):
        bots.check({**bot, "model": "chat"}, "good")
    assert "the key works" in bots.check({**bot, "model": "busy"}, "good"), "a free tier that's busy right now is fine"
    with pytest.raises(ValueError, match="can't reach"):
        bots.check({**bot, "url": "http://127.0.0.1:9"}, "good")


def test_a_bot_runs_on_its_provider_never_on_a_claude_login(env, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "the-farms-own-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "the-farms-own-key")
    mgr = AgentManager(load())
    mgr.stopping.set()  # no child process in this test
    bot = bots.parse({"provider": "openrouter", "model": "qwen/qwen3-coder:free", "workers": 2})
    a = mgr.create("Qwen", bot=bot, key="sk-or-secret")
    assert a["id"] == "qwen" and a["bot"] == bot
    path = os.path.join(a["config_dir"], "bot.json")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert "sk-or-secret" not in open(mgr.registry).read(), "the key stays in the bot's own config dir"
    e = mgr.env_for(mgr.get("qwen"))
    assert e["ANTHROPIC_BASE_URL"] == "https://openrouter.ai/api" and e["ANTHROPIC_AUTH_TOKEN"] == "sk-or-secret"
    assert e["ANTHROPIC_API_KEY"] == "" and "CLAUDE_CODE_OAUTH_TOKEN" not in e, "never the farm's own login"
    assert e["ANTHROPIC_MODEL"] == e["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == e["FARM_MODEL"] == "qwen/qwen3-coder:free"
    assert e["FARM_BOT"] == "qwen/qwen3-coder:free" and e["FARM_REMOTE_CONTROL"] == "0" and e["FARM_SEAT"] == "bot-qwen"
    assert e["FARM_MAX_WORKERS"] == "2" and e["FARM_DAILY_BUDGET_USD"] == "0"
    assert mgr.auth(mgr.get("qwen"))["loggedIn"], "no `claude auth status`: its provider answered when it was added"
    with pytest.raises(ValueError, match="no Claude login"):
        mgr.start_login("qwen")
    mgr.remove("qwen")
    assert not os.path.exists(a["config_dir"]), "its key goes with it"


def test_a_bot_takes_only_what_is_sent_to_it_and_keeps_its_own_sub_agents(env, monkeypatch):
    mgr = AgentManager(load())
    mgr.stopping.set()
    bot = bots.parse({"provider": "custom", "url": "http://127.0.0.1:9/api", "model": "qwen3-coder"})
    agent = mgr.create("qwen", bot=bot, key="k")
    for k, v in mgr.env_for(agent).items():  # this test's farm is that bot's `clodfarm run`
        monkeypatch.setenv(k, v)
    farm, t = start_farm()
    try:
        assert farm.cfg.bot == "qwen3-coder" and farm.cfg.policy.api_mode and farm.seat == "bot-qwen"
        free = json.loads(cli("spawn", "anyone's job", "--prompt", "COMMIT free", "--json").stdout)["id"]
        wait_for(lambda: "qwen" in cli("agents").stdout)
        sent = json.loads(cli("spawn", "for the bot", "--prompt", "SPAWN 1", "--on", "qwen", "--json").stdout)["id"]
        wait_for(lambda: farm.store.get_task(sent)["status"] == "done", timeout=60)
        kid = farm.store.get_task(farm.store.get_task(sent)["children"][0])
        assert kid["to"] == "qwen" and kid["status"] == "done", "a bot's own sub-agents stay on it"
        assert farm.store.get_task(free)["status"] == "queued", "not sent to it: it waits for a Claude"
        runs = [c for c in calls(env) if c["cmd"] == "print"]
        assert runs and all(c["base_url"] == "http://127.0.0.1:9/api" and c["model"] == "qwen3-coder"
                            and c["token"] == "k" for c in runs)
        assert not [c for c in calls(env) if c["cmd"] == "remote-control"], "a bot has no Claude login to talk through"
        assert "child0.txt" in repo_files(env)
        out = cli("agents").stdout
        assert "BOT on qwen3-coder via Anthropic-compatible" in out and "--on qwen" in out
        assert "a bot on another model" in cli("budget").stdout
        cli("spawn", "too much", "--prompt", "REJECT", "--on", "qwen")  # its provider says 429
        wait_for(lambda: [e for e in farm.store.events(time.time() - 60) if e["type"] == "budget.rejected"
                          and "rate-limited bot qwen" in e["msg"]])
        assert "API rate limited" in cli("agents").stdout, "it rests until its provider lets it go on"
    finally:
        stop_farm(farm, t)


def test_clodfarm_bot_add_checks_it_first(env, provider):
    url, seen = provider
    run = lambda *args, key: subprocess.run([sys.executable, "-m", "clodfarm", "bot", "add", *args],  # noqa: E731
                                            input=key + "\n", capture_output=True, text=True)
    bad = run("nightbot", "--provider", "custom", "--url", url, "--model", "m", key="wrong")
    assert bad.returncode == 1 and "refused the key" in bad.stderr
    assert not AgentManager(load()).get("nightbot"), "nothing is kept until its model answers"
    ok = run("nightbot", "--provider", "custom", "--url", url, "--model", "m", "--any", key="good")
    assert ok.returncode == 0, ok.stderr
    assert "answered \"ok\"" in ok.stdout and "--on nightbot" in ok.stdout
    a = AgentManager(load()).get("nightbot")
    assert a["bot"]["takes"] == "any" and bots.load_key(a["config_dir"]) == "good"
    assert not os.path.exists(os.path.join(str(env / "workspace"), ".farm", "agents", "nightbot.log")), \
        "the command only registers it: the farm UI's process starts it, once"


def test_the_farm_ui_adds_a_bot(env, backend, monkeypatch, provider):
    if backend != "sqlite":
        pytest.skip("the UI reads the same Store API on both backends; one is enough")
    from test_web import client, login
    from clodfarm.store import Store
    from clodfarm.web import FarmUI, make_handler
    url, _ = provider
    monkeypatch.setenv("FARM_UI_PASSWORD", "correct horse")
    cfg = load()
    store = Store.from_config(cfg)
    store.ensure_table()
    farm_ui = FarmUI(cfg, store)
    farm_ui.manager.stopping.set()  # its `clodfarm run` isn't needed here
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(farm_ui))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        call = client()
        login(call, base)
        code, body, _ = call(base + "/api/agents", {"name": "Qwen", "bot": {"provider": "custom", "url": url,
                                                                            "model": "m", "key": "wrong"}})
        assert code == 400 and "refused the key" in body["error"]
        code, a, _ = call(base + "/api/agents", {"name": "Qwen", "bot": {"provider": "custom", "url": url,
                                                                         "model": "m", "key": "good"}})
        assert code == 200 and a["id"] == "qwen" and a["said"] == "ok"
        farm_ui._state_cache = None
        st = call(base + "/api/state")[1]
        v = next(x for x in st["agents"] if x["id"] == "qwen")
        assert v["bot"] == {"model": "m", "via": "Anthropic-compatible", "takes": "sent"} and v["loggedIn"]
        assert "good" not in json.dumps(st), "the key is never shown again"
        assert [e for e in store.events(time.time() - 60) if e["type"] == "agent.added" and "a bot on m" in e["msg"]]
    finally:
        srv.shutdown()
        farm_ui.manager.shutdown()
