"""The farm UI server: login, sessions, CSRF guard, state and the agent registry (with the fake `claude`)."""
import http.cookiejar
import json
import os
import threading
import time
import urllib.error
import urllib.request

import pytest

from clodfarm.config import load
from clodfarm.store import Store
from clodfarm.web import Auth, FarmUI, make_handler


@pytest.fixture
def ui(env, backend, monkeypatch):
    if backend != "sqlite":
        pytest.skip("the UI reads the same Store API on both backends; one is enough")
    from http.server import ThreadingHTTPServer
    monkeypatch.setenv("FARM_UI_PASSWORD", "correct horse")
    cfg = load()
    store = Store.from_config(cfg)
    store.ensure_table()
    farm_ui = FarmUI(cfg, store)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(farm_ui))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", farm_ui
    srv.shutdown()
    farm_ui.manager.shutdown()


def client():
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def call(url, body=None, headers=None):
        hdrs = {"Content-Type": "application/json", "X-Clodfarm": "1"} if body is not None else {}
        hdrs.update(headers or {})
        req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), headers=hdrs)
        try:
            with opener.open(req) as r:
                return r.status, json.loads(r.read() or b"{}"), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}"), dict(e.headers)
    return call


def login(call, base):
    code, body, _ = call(base + "/api/login", {"password": "correct horse"})
    assert code == 200, body


def test_login_required_and_wrong_password(ui):
    base, _ = ui
    call = client()
    assert call(base + "/api/state")[0] == 401
    assert call(base + "/api/login", {"password": "nope"})[0] == 401
    login(call, base)
    code, st, headers = call(base + "/api/state")
    assert code == 200 and st["farm"] == "test"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]


def test_session_cookie_flags_and_logout(ui):
    base, _ = ui
    call = client()
    _, _, headers = call(base + "/api/login", {"password": "correct horse"})
    cookie = headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    call(base + "/api/logout", {})
    assert call(base + "/api/state")[0] == 401


def test_writes_need_the_csrf_header(ui):
    base, _ = ui
    call = client()
    login(call, base)
    code, _, _ = call(base + "/api/pause", {}, headers={"X-Clodfarm": ""})
    assert code == 403


def test_lockout_after_five_wrong_tries(ui):
    base, _ = ui
    call = client()
    for _ in range(5):
        call(base + "/api/login", {"password": "wrong"})
    assert call(base + "/api/login", {"password": "correct horse"})[0] == 429


def test_state_pause_and_no_quest_endpoints(ui):
    base, farm_ui = ui
    call = client()
    login(call, base)
    assert call(base + "/api/tasks", {"text": "Plant tomatoes"})[0] == 404  # you talk to your Claude, not a form
    assert call(base + "/api/mission", {"text": "Grow"})[0] == 404
    farm_ui.store.event("rc.connected", f"Remote Control '{farm_ui.cfg.name}' is live: https://claude.ai/code/session_abc")
    assert call(base + "/api/pause", {"reason": "lunch"})[0] == 200
    time.sleep(1.1)  # the state is cached for a second
    st = call(base + "/api/state")[1]
    assert st["paused"] and st["pause_reason"] == "lunch" and "goal" not in st
    me = next(a for a in st["agents"] if a["primary"])
    assert me["remote_control"] == "https://claude.ai/code/session_abc"


def test_released_agent_takes_its_workers_along(ui):
    import socket
    base, farm_ui = ui
    call = client()
    login(call, base)
    a = call(base + "/api/agents", {"name": "gil"})[1]
    box = farm_ui.manager.farm_id(a["id"])
    t = farm_ui.store.add_task("gil works on this", "x")
    farm_ui.store.claim_next(f"{box}/w0", 300)
    farm_ui.store.heartbeat(box, "w0", "running", t["id"])
    farm_ui.store.heartbeat("ghost@" + socket.gethostname(), "w0", "idle")  # released before this fix
    farm_ui.store.heartbeat("far-box@elsewhere", "w0", "idle")  # a real visitor from another box
    time.sleep(1.1)
    names = {x["id"] for x in call(base + "/api/state")[1]["agents"]}
    assert a["id"] in names and "far-box" in names and "ghost" not in names
    assert call(base + f"/api/agents/{a['id']}/remove", {})[0] == 200
    assert not farm_ui.manager.alive(a["id"])
    stale = {**farm_ui.manager.primary(), **a, "primary": False, "config_dir": "/nonexistent"}
    for _ in range(3):  # keep_alive holding an old copy of the registry must not bring it back
        farm_ui.manager.spawn(stale)
    assert not farm_ui.manager.alive(a["id"])
    assert farm_ui.store.get_task(t["id"])["status"] == "queued"
    assert all(not w["SK"].startswith(box + "/") for w in farm_ui.store.workers())
    time.sleep(1.1)
    assert a["id"] not in {x["id"] for x in call(base + "/api/state")[1]["agents"]}


def test_hatch_an_agent_starts_its_own_farm_process(ui):
    base, farm_ui = ui
    call = client()
    login(call, base)
    code, a, _ = call(base + "/api/agents", {"name": "Gil's Claude"})
    assert code == 200 and a["id"] == "gil-s-claude"
    agent = farm_ui.manager.get(a["id"])
    assert agent["config_dir"].startswith(farm_ui.manager.base)
    assert farm_ui.manager.alive(a["id"])
    env = farm_ui.manager.env_for(agent)
    assert env["FARM_NAME"] == a["id"] and env["FARM_UI"] == "0" and env["CLAUDE_CONFIG_DIR"] == agent["config_dir"]
    assert call(base + "/api/agents", {"name": "Gil's Claude"})[1]["id"] != a["id"]  # names stay unique
    assert call(base + f"/api/agents/{a['id']}/remove", {})[0] == 200
    assert not farm_ui.manager.get(a["id"]) and not os.path.exists(agent["config_dir"])
    assert call(base + f"/api/agents/{farm_ui.cfg.name}/remove", {})[0] == 400  # the primary is the farm itself


def test_auth_generates_a_password_once(tmp_path, monkeypatch):
    monkeypatch.delenv("FARM_UI_PASSWORD", raising=False)
    p = str(tmp_path / "ui.json")
    a = Auth(p)
    assert a.generated and a.check(a.generated, "1.2.3.4")
    assert Auth(p).generated is None  # the next start reuses the stored hash
    assert "hash" in json.load(open(p)) and a.generated not in open(p).read()
    tok = a.issue("farmer")
    assert a.verify(tok) == "farmer" and a.verify(tok[:-2] + "00") is None
    a.set_password("new password")
    assert a.verify(tok) is None  # a new password signs every session out


def test_path_prefix_behind_a_proxy(env, backend, monkeypatch):
    if backend != "sqlite":
        pytest.skip("one backend is enough")
    import importlib
    from http.server import ThreadingHTTPServer
    import clodfarm.web as web
    monkeypatch.setenv("FARM_UI_PASSWORD", "correct horse")
    monkeypatch.setenv("FARM_UI_BASE", "/team/")
    monkeypatch.setenv("FARM_UI_TRUST_PROXY", "1")
    monkeypatch.setenv("FARM_UI_SECURE", "1")
    web = importlib.reload(web)
    try:
        cfg = load()
        Store.from_config(cfg).ensure_table()
        farm_ui = web.FarmUI(cfg)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(farm_ui))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        call = client()
        assert call(base + "/healthz")[0] == 200  # the container health check stays at the root
        assert call(base + "/api/state")[0] == 404  # outside the prefix
        code, _, headers = call(base + "/team/api/login", {"password": "correct horse"}, headers={"X-Forwarded-For": "1.2.3.4"})
        assert code == 200 and "Path=/team/" in headers["Set-Cookie"] and "Secure" in headers["Set-Cookie"]
        # the lockout counts the forwarded client, not the proxy: another client can still log in
        for _ in range(5):
            call(base + "/team/api/login", {"password": "x"}, headers={"X-Forwarded-For": "6.6.6.6"})
        assert call(base + "/team/api/login", {"password": "x"}, headers={"X-Forwarded-For": "6.6.6.6"})[0] == 429
        assert client()(base + "/team/api/login", {"password": "correct horse"}, headers={"X-Forwarded-For": "1.2.3.4"})[0] == 200
        srv.shutdown()
        farm_ui.manager.shutdown()
    finally:
        for k in ("FARM_UI_BASE", "FARM_UI_TRUST_PROXY", "FARM_UI_SECURE"):
            monkeypatch.delenv(k, raising=False)
        importlib.reload(web)


def test_state_is_claudes_and_their_sub_agents(ui):
    base, farm_ui = ui
    call = client()
    login(call, base)
    me = farm_ui.cfg.name
    top = farm_ui.store.add_task("refactor", "x", owner=me)
    farm_ui.store.add_task("part 1", "x", parent=top["id"], to="gil")
    farm_ui.store.claim_next(f"{me}@h/w0", 300, agent=me)
    farm_ui.store.heartbeat(f"{me}@h", "w0", "running", top["id"], seat="s1")
    st = call(base + "/api/state")[1]
    assert "workers" not in st["agents"][0] and "tasks" not in st and "counts" not in st
    subs = {t["title"]: t for t in st["subagents"]}
    assert subs["refactor"]["owner"] == me and subs["refactor"]["on"] == me
    assert subs["part 1"]["owner"] == me and subs["part 1"]["to"] == "gil" and subs["part 1"]["status"] == "queued"


def test_sessions_and_conversations_in_the_ui(ui):
    base, farm_ui = ui
    call = client()
    login(call, base)
    farm_ui.store.record_session("s1", claude="gil", kind="conversation", title="review the importer",
                                 turns=[{"role": "user", "kind": "text", "text": "review it"},
                                        {"role": "assistant", "kind": "text", "text": "done"}])
    rows = call(base + "/api/sessions?claude=gil")[1]
    assert [r["id"] for r in rows] == ["s1"] and rows[0]["turns"] == 2
    assert call(base + "/api/sessions?claude=nobody")[1] == []
    s = call(base + "/api/sessions/s1")[1]
    assert [t["text"] for t in s["conversation"]] == ["review it", "done"]
    assert call(base + "/api/sessions/nope")[0] == 404
