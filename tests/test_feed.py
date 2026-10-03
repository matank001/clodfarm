"""The live feed (what agents think, say and do), broadcasting it, scrubbing it, and MCP servers from the host."""
import json
import os
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

from clodfarm import connectors, feed, scrub
from clodfarm.config import load
from clodfarm.store import Store
from conftest import cli
from test_farm import start_farm, stop_farm, wait_for
from test_web import client, login


def test_a_stream_event_becomes_feed_items():
    ev = {"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "  plan   it  ", "signature": "s"},
        {"type": "thinking", "thinking": "", "signature": "only a signature"},
        {"type": "text", "text": "Done."},
        {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q", "description": "Run the tests"}},
        {"type": "tool_use", "name": "mcp__arena__arena_idea", "input": {"title": "A price tracker"}},
        {"type": "tool_use", "name": "TodoWrite", "input": {"todos": []}}]}}
    assert feed.items(ev) == [
        {"kind": "thought", "text": "plan it"}, {"kind": "say", "text": "Done."},
        {"kind": "tool", "text": "Bash: pytest -q", "tool": "Bash"},
        {"kind": "tool", "text": "mcp__arena__arena_idea: A price tracker", "tool": "mcp__arena__arena_idea"},
        {"kind": "tool", "text": "TodoWrite", "tool": "TodoWrite"}]
    assert feed.items({"type": "result"}) == [] and feed.items({"type": "user", "message": {}}) == []
    assert len(feed.items({"type": "assistant", "message": {"content": [{"type": "text", "text": "x" * 900}]}})[0]
               ["text"]) == feed.CLIP


def test_secrets_are_scrubbed(env, monkeypatch):
    monkeypatch.setenv("SOME_SERVICE_TOKEN", "a-very-private-value-123")
    scrub._cache = (0.0, [])
    known = scrub.secrets(str(env / "workspace"))
    assert "a-very-private-value-123" in known
    s = scrub.text("token a-very-private-value-123 and sk-proj-AbCdEfGhIjKlMnOpQrSt and xai-" + "a" * 30 +
                   " AIza" + "b" * 35 + " rk_live_" + "c" * 20 + " mail me at noa@example.com, card 4242 4242 4242 4242,"
                   " order 1234567890123 and Bearer abcdefghijklmnop1234 and password=hunter2hunter2", known)
    for leaked in ("a-very-private-value-123", "sk-proj-", "xai-aaa", "AIzabbb", "rk_live_", "noa@example.com",
                   "4242 4242", "abcdefghijklmnop1234", "hunter2hunter2"):
        assert leaked not in s, leaked
    assert "order 1234567890123" in s, "a number that isn't a card number stays"
    assert scrub.obj({"a": ["sk-ant-" + "z" * 30, {"b": 1}]}, []) == {"a": ["[an API key]", {"b": 1}]}


@pytest.fixture
def ui(env, backend, monkeypatch):
    if backend != "sqlite":
        pytest.skip("the UI reads the same Store API on both backends; one is enough")
    from clodfarm.web import FarmUI, make_handler
    cfg = load()
    store = Store.from_config(cfg)
    store.ensure_table()
    farm_ui = FarmUI(cfg, store)
    farm_ui.manager.stopping.set()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(farm_ui))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", farm_ui
    srv.shutdown()
    farm_ui.manager.shutdown()


def test_the_feed_is_public_only_when_the_farm_broadcasts(ui, monkeypatch):
    base, farm_ui = ui
    farm_ui.store.live_add([{"kind": "thought", "text": "my key is sk-proj-" + "q" * 24}], claude="gpt", task="t1")
    visitor = client()
    assert visitor(base + "/api/live")[0] == 403, "a public farm shows its agents at work, not their thoughts"
    manager = client()
    login(manager, base)
    code, body, _ = manager(base + "/api/live")
    assert code == 200 and body["items"][0]["claude"] == "gpt" and "sk-proj-" not in json.dumps(body), "scrubbed"
    assert manager(base + "/api/manager/settings", {"broadcast": True})[0] == 200
    code, body, _ = visitor(base + "/api/live")
    assert code == 200 and body["items"][0]["kind"] == "thought"
    cursor = body["cursor"]
    farm_ui.store.live_add([{"kind": "say", "text": "next"}], claude="gpt")
    body = visitor(base + f"/api/live?since={cursor}")[1]
    assert [i["text"] for i in body["items"]] == ["next"] and body["cursor"] != cursor
    farm_ui.store.add_spend(1.25, "bot-gpt")
    assert any(r["seat"] == "bot-gpt" and r["usd"] == 1.25 for r in visitor(base + "/api/live")[1]["spend"])
    manager(base + "/api/manager/settings", {"broadcast": False})
    monkeypatch.setenv("FARM_UI_BROADCAST", "1")
    assert visitor(base + "/api/live")[0] == 200, "or its host turns it on"
    monkeypatch.setenv("FARM_UI_PRIVATE", "1")
    assert visitor(base + "/api/live")[0] == 401, "a private farm stays private"


def test_a_sub_agent_feeds_what_it_thinks_and_does(env):
    farm, t = start_farm()
    try:
        tid = json.loads(cli("spawn", "look around", "--prompt", "THINK then COMMIT x", "--json").stdout)["id"]
        wait_for(lambda: farm.store.get_task(tid)["status"] == "done", timeout=60)
        got = [i for i in farm.store.live() if i.get("task") == tid]
        kinds = [i["kind"] for i in got]
        assert kinds[:3] == ["thought", "tool", "say"], kinds
        assert got[1]["text"] == "Bash: ls -la" and got[0]["claude"] == farm.cfg.name
        cli("msg", farm.cfg.name, "hello from the test")
        assert any(i["kind"] == "msg" and i["text"] == "hello from the test" for i in farm.store.live())
    finally:
        stop_farm(farm, t)


def test_the_host_gives_every_claude_its_mcp_servers(env, monkeypatch):
    monkeypatch.setenv("FARM_REMOTE_MCP", json.dumps({
        "arena": {"url": "https://clod.farm/live/mcp", "token": "team-token", "guide": "You are Team GPT."},
        "Bad Name": {"url": "https://x"}, "stripe": {"url": "https://evil"}, "nourl": {"token": "t"}}))
    ws = str(env / "workspace")
    servers = connectors.mcp_servers(ws)
    assert list(servers) == ["arena"]
    assert servers["arena"] == {"type": "http", "url": "https://clod.farm/live/mcp",
                                "headers": {"Authorization": "Bearer team-token", "X-Clodfarm-Connector": "arena"}}
    assert connectors.is_ours("arena", servers["arena"]) and not connectors.is_ours("arena", {"type": "http"})
    g = connectors.guide_section(ws)
    assert "## arena" in g and "mcp__arena__*" in g and "You are Team GPT." in g
