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


def test_quests_mission_pause(ui):
    base, farm_ui = ui
    call = client()
    login(call, base)
    code, t, _ = call(base + "/api/tasks", {"title": "Plant tomatoes", "priority": 7})
    assert code == 200 and t["status"] == "queued" and t["priority"] == 7
    code, d, _ = call(base + f"/api/tasks/{t['id']}")
    assert d["prompt"] == "Plant tomatoes" and d["runs"] == []
    assert call(base + f"/api/tasks/{t['id']}/cancel", {})[0] == 200
    assert farm_ui.store.get_task(t["id"])["status"] == "cancelled"
    os.makedirs(farm_ui.cfg.repo_dir, exist_ok=True)
    import subprocess
    subprocess.run(["git", "init", "-q", farm_ui.cfg.repo_dir], check=True)
    assert call(base + "/api/mission", {"text": "# Grow a garden\nwith tests"})[0] == 200
    assert call(base + "/api/pause", {"reason": "lunch"})[0] == 200
    time.sleep(1.1)  # the state is cached for a second
    st = call(base + "/api/state")[1]
    assert st["goal"] == "Grow a garden" and st["paused"] and st["pause_reason"] == "lunch"


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
