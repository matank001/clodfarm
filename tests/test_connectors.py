"""Connectors: the farm's Stripe, connected once by the manager, used by every Claude (checked against a stand-in
for Stripe's API)."""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from clodfarm import auth, connectors, policy
from clodfarm.config import load
from clodfarm.store import Store
from clodfarm.web import FarmUI, make_handler

from test_web import client, login

GOOD, RESTRICTED, REVOKED = "sk_test_" + "a" * 24, "rk_test_" + "b" * 24, "sk_test_" + "c" * 24


class FakeStripe(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        key = (self.headers.get("Authorization") or "")[7:]
        if key == REVOKED:
            return self._send(401, {"error": {"message": "Invalid API Key provided"}})
        if self.path == "/v1/account":
            if key == RESTRICTED:  # a restricted key without the account permission
                return self._send(403, {"error": {"message": "The provided key does not have the required permissions"}})
            return self._send(200, {"id": "acct_123", "email": "shop@example.com", "country": "DE",
                                    "settings": {"dashboard": {"display_name": "Jestr Shop"}}})
        if self.path == "/v1/balance":
            return self._send(200, {"object": "balance", "available": []})
        self._send(404, {})

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def ui(env, backend, monkeypatch):
    if backend != "sqlite":
        pytest.skip("one backend is enough")
    stripe = ThreadingHTTPServer(("127.0.0.1", 0), FakeStripe)
    threading.Thread(target=stripe.serve_forever, daemon=True).start()
    monkeypatch.setenv("FARM_STRIPE_API", f"http://127.0.0.1:{stripe.server_address[1]}")
    cfg = load()
    store = Store.from_config(cfg)
    store.ensure_table()
    farm_ui = FarmUI(cfg, store)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(farm_ui))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", farm_ui
    srv.shutdown()
    stripe.shutdown()


def servers(path):
    try:
        return json.load(open(path)).get("mcpServers") or {}
    except OSError:
        return {}


def test_the_manager_connects_stripe_and_every_claude_gets_it(ui):
    base, farm_ui = ui
    gil = farm_ui.manager.create("gil", start=False)
    manager, person = client(), client()
    login(manager, base)
    login(person, base, claude=gil["id"])
    assert person(base + "/api/connectors")[1]["stripe"] == {"connected": False}
    assert person(base + "/api/connectors/stripe", {"key": GOOD})[0] == 403, "the manager's to connect"
    code, body, _ = manager(base + "/api/connectors/stripe", {"key": GOOD})
    assert code == 200, body
    s = body["stripe"]
    assert s["connected"] and s["mode"] == "test" and s["account"]["name"] == "Jestr Shop" and s["last4"] == "aaaa"
    assert GOOD not in json.dumps(body), "the key is never shown again"
    assert oct(os.stat(connectors._path(farm_ui.cfg.workspace)).st_mode & 0o777) == "0o600"
    # every Claude: the MCP server, and the guide says what it may do with it
    gil_json = os.path.join(gil["config_dir"], ".claude.json")
    assert servers(gil_json)["stripe"] == {"type": "http", "url": "https://mcp.stripe.com",
                                           "headers": {"Authorization": f"Bearer {GOOD}"}}
    assert "## Stripe (a connector)" in open(os.path.join(gil["config_dir"], "CLAUDE.md")).read()
    import time
    time.sleep(1.05)  # the state is built once a second
    assert person(base + "/api/state")[1]["connectors"]["stripe"] is True
    seen = person(base + "/api/connectors")[1]["stripe"]
    assert seen["connected"] and seen["mode"] == "test" and "last4" not in seen, "a person sees it's there"
    # disconnect: the tools go
    assert manager(base + "/api/connectors/stripe/disconnect", {})[1]["stripe"] == {"connected": False}
    assert "stripe" not in servers(gil_json)
    assert "## Stripe (a connector)" not in open(os.path.join(gil["config_dir"], "CLAUDE.md")).read()


def test_keys_that_dont_work_are_refused(ui):
    base, farm_ui = ui
    manager = client()
    login(manager, base)
    code, body, _ = manager(base + "/api/connectors/stripe", {"key": "pk_test_notsecret"})
    assert code == 400 and "sk_test_" in body["error"]
    code, body, _ = manager(base + "/api/connectors/stripe", {"key": REVOKED})
    assert code == 400 and "not valid" in body["error"]
    code, body, _ = manager(base + "/api/connectors/stripe", {"key": RESTRICTED})
    assert code == 200 and body["stripe"]["kind"] == "restricted" and body["stripe"]["account"] == {}, \
        "a restricted key without the account permission works through its balance"


def test_a_hand_made_stripe_server_is_kept(env, tmp_path, monkeypatch):
    path = auth.claude_json_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump({"mcpServers": {"stripe": {"command": "npx", "args": ["-y", "@stripe/mcp"]}}}, open(path, "w"))
    auth.install_browser_mcp()
    assert servers(path)["stripe"]["command"] == "npx", "not ours: left alone"


def test_a_person_can_turn_stripe_off_for_their_claude():
    assert not policy.decide({"deny": ["stripe"]}, "mcp__stripe__create_refund")[0]
    assert policy.decide({"deny": ["mcp"]}, "mcp__stripe__list_customers")[0], "Stripe is its own group"
    assert policy.decide({"deny": ["stripe"]}, "mcp__github__x")[0]


def test_the_cli(ui, env):
    import subprocess
    import sys
    base, farm_ui = ui
    run = lambda *a, stdin=None: subprocess.run([sys.executable, "-m", "clodfarm", "stripe", *a], input=stdin,  # noqa
                                                capture_output=True, text=True,
                                                env={k: v for k, v in os.environ.items() if k != "CLAUDECODE"})
    assert "not connected" in run().stdout
    r = run("connect", stdin=GOOD + "\n")
    assert r.returncode == 0 and "TEST mode" in r.stdout and "Jestr Shop" in r.stdout and GOOD not in r.stdout
    assert run("disconnect").returncode == 0 and "not connected" in run().stdout


# --------------------------------------------------------------------- Blender
BLENDER_TOKEN = "bl-" + "d" * 30


class FakeBlenderMCP(BaseHTTPRequestHandler):
    """An MCP server's initialize, answered as an SSE stream the way the Python SDK does."""
    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path != "/mcp":
            return self._send(404, b"not found", "text/plain")
        if self.headers.get("Authorization") != f"Bearer {BLENDER_TOKEN}":
            return self._send(401, b'{"error": "unauthorized"}', "application/json")
        msg = {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                                                     "serverInfo": {"name": "blender", "title": "Blender 5.2",
                                                                    "version": "1.0.0"}}}
        self._send(200, f"event: message\ndata: {json.dumps(msg)}\n\n".encode(), "text/event-stream")

    def _send(self, code, data, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def blender_mcp():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeBlenderMCP)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/mcp"
    srv.shutdown()


def test_the_manager_connects_blender_and_every_claude_gets_it(ui, env, blender_mcp):
    import subprocess
    import sys
    base, farm_ui = ui
    gil = farm_ui.manager.create("gil", start=False)
    run = lambda *a, stdin=None: subprocess.run([sys.executable, "-m", "clodfarm", "blender", *a], input=stdin,  # noqa
                                                capture_output=True, text=True,
                                                env={k: v for k, v in os.environ.items() if k != "CLAUDECODE"})
    assert "not connected" in run().stdout
    r = run("connect", blender_mcp, stdin="wrong\n")
    assert r.returncode == 1 and "refused the token" in r.stderr
    r = run("connect", blender_mcp.replace("/mcp", "/nope"), stdin=BLENDER_TOKEN + "\n")
    assert r.returncode == 1 and "HTTP 404" in r.stderr
    r = run("connect", blender_mcp, stdin=BLENDER_TOKEN + "\n")
    assert r.returncode == 0, r.stderr
    assert "Blender 5.2" in r.stdout and "…dddd" in r.stdout and BLENDER_TOKEN not in r.stdout
    assert oct(os.stat(connectors._blender_path(farm_ui.cfg.workspace)).st_mode & 0o777) == "0o600"
    # every Claude: the MCP server, and the guide says it's there
    gil_json = os.path.join(gil["config_dir"], ".claude.json")
    assert servers(gil_json)["blender"] == {"type": "http", "url": blender_mcp, "headers": {
        "Authorization": f"Bearer {BLENDER_TOKEN}", "X-Clodfarm-Connector": "blender"}}
    assert "## Blender (a connector)" in open(os.path.join(gil["config_dir"], "CLAUDE.md")).read()
    # disconnect: the tools go
    assert run("disconnect").returncode == 0 and "not connected" in run().stdout
    assert "blender" not in servers(gil_json)
    assert "## Blender (a connector)" not in open(os.path.join(gil["config_dir"], "CLAUDE.md")).read()


def test_the_manager_connects_blender_from_the_ui(ui, blender_mcp):
    base, farm_ui = ui
    gil = farm_ui.manager.create("gil", start=False)
    manager, person = client(), client()
    login(manager, base)
    login(person, base, claude=gil["id"])
    assert person(base + "/api/connectors")[1]["blender"] == {"connected": False}
    assert person(base + "/api/connectors/blender", {"url": blender_mcp, "token": BLENDER_TOKEN})[0] == 403
    code, body, _ = manager(base + "/api/connectors/blender", {"url": blender_mcp, "token": "wrong"})
    assert code == 400 and "refused the token" in body["error"]
    code, body, _ = manager(base + "/api/connectors/blender", {"url": "ftp://nope"})
    assert code == 400 and "MCP server URL" in body["error"]
    code, body, _ = manager(base + "/api/connectors/blender", {"url": blender_mcp, "token": BLENDER_TOKEN})
    assert code == 200, body
    b = body["blender"]
    assert b["connected"] and b["url"] == blender_mcp and b["last4"] == "dddd" and b["server"]["title"] == "Blender 5.2"
    assert BLENDER_TOKEN not in json.dumps(body), "the token is never shown again"
    assert servers(os.path.join(gil["config_dir"], ".claude.json"))["blender"]["url"] == blender_mcp
    import time
    time.sleep(1.05)  # the state is built once a second
    assert person(base + "/api/state")[1]["connectors"]["blender"] is True
    seen = person(base + "/api/connectors")[1]["blender"]
    assert seen == {"connected": True, "server": b["server"], "tools": "mcp__blender__*"}, "not where it runs"
    assert not any(blender_mcp in (e.get("msg") or "") for e in person(base + "/api/state")[1]["events"])
    assert manager(base + "/api/connectors/blender/disconnect", {})[1]["blender"] == {"connected": False}
    assert "blender" not in servers(os.path.join(gil["config_dir"], ".claude.json"))


def test_a_claude_cant_connect_blender(env, blender_mcp):
    import subprocess
    import sys
    r = subprocess.run([sys.executable, "-m", "clodfarm", "blender", "connect", blender_mcp], input=BLENDER_TOKEN,
                       capture_output=True, text=True, env={**os.environ, "CLAUDECODE": "1"})
    assert r.returncode == 2 and "farm manager" in r.stderr


def test_a_hand_made_blender_server_is_kept(env, tmp_path):
    path = auth.claude_json_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mine = {"type": "http", "url": "http://127.0.0.1:9999/mcp"}
    json.dump({"mcpServers": {"blender": mine}}, open(path, "w"))
    auth.install_browser_mcp()
    assert servers(path)["blender"] == mine, "not ours (no marker): left alone"


def test_a_person_can_turn_blender_off_for_their_claude():
    assert not policy.decide({"deny": ["blender"]}, "mcp__blender__execute_python")[0]
    assert policy.decide({"deny": ["mcp"]}, "mcp__blender__execute_python")[0], "Blender is its own group"
    assert policy.decide({"deny": ["blender"]}, "mcp__stripe__list_customers")[0]


# ------------------------------------------------------------------ Google Ads
GADS = {"developer_token": "devtok_abcd1234", "client_id": "123.apps.googleusercontent.com",
        "client_secret": "GOCSPX-secret", "refresh_token": "good", "login_customer_id": "123-456-7890"}


@pytest.fixture
def google(monkeypatch):
    from fake_google_ads import serve
    srv = serve()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setenv("FARM_GOOGLE_OAUTH", base + "/token")
    monkeypatch.setenv("FARM_GOOGLE_ADS_API", base)
    yield base
    srv.shutdown()


def test_the_manager_connects_google_ads(ui, google):
    base, farm_ui = ui
    gil = farm_ui.manager.create("gil", start=False)
    manager, person = client(), client()
    login(manager, base)
    login(person, base, claude=gil["id"])
    assert person(base + "/api/connectors")[1]["google_ads"] == {"connected": False}
    assert person(base + "/api/connectors/google-ads", GADS)[0] == 403
    code, body, _ = manager(base + "/api/connectors/google-ads", GADS)
    assert code == 200, body
    g = body["google_ads"]
    assert g["connected"] and g["api_version"] == "v24", "v25 isn't there: the one before answers"
    assert [c["name"] for c in g["customers"]] == ["Jestr Ads (manager)", "Jestr Shop", "Jestr App installs"]
    assert g["customers"][0]["manager"] and g["login_customer_id"] == "1234567890"
    assert g["developer_token_last4"] == "1234" and "GOCSPX" not in json.dumps(body) and "devtok" not in json.dumps(body)
    js, yml = connectors._gads_paths(farm_ui.cfg.workspace)
    assert oct(os.stat(js).st_mode & 0o777) == oct(os.stat(yml).st_mode & 0o777) == "0o600"
    y = open(yml).read()
    assert 'refresh_token: "good"' in y and 'login_customer_id: "1234567890"' in y and "use_proto_plus: true" in y
    assert "## Google Ads (a connector)" in open(os.path.join(gil["config_dir"], "CLAUDE.md")).read()
    seen = person(base + "/api/connectors")[1]["google_ads"]
    assert seen["connected"] and len(seen["customers"]) == 3 and "developer_token_last4" not in seen
    rows = connectors.gads_query(farm_ui.cfg.workspace, "234-567-8901", "SELECT campaign.name FROM campaign")
    assert rows[0]["campaign"]["name"] == "Jestr Shop · Search"
    assert manager(base + "/api/connectors/google-ads/disconnect", {})[1]["google_ads"] == {"connected": False}
    assert not os.path.exists(js) and not os.path.exists(yml)
    assert "## Google Ads" not in open(os.path.join(gil["config_dir"], "CLAUDE.md")).read()


def test_google_ads_credentials_that_dont_work_are_refused(ui, google):
    base, _ = ui
    manager = client()
    login(manager, base)
    for bad, says in (({"refresh_token": "expired"}, "invalid_grant"), ({"developer_token": "revoked"}, "NOT_APPROVED"),
                      ({"client_secret": ""}, "missing: client secret"), ({"login_customer_id": "123"}, "10 digits")):
        code, body, _ = manager(base + "/api/connectors/google-ads", {**GADS, **bad})
        assert code == 400 and says in body["error"], (bad, body)


def test_the_gads_cli(ui, google, env):
    import subprocess
    import sys
    _, farm_ui = ui
    connectors.gads_connect(farm_ui.cfg.workspace, GADS)
    run = lambda *a: subprocess.run([sys.executable, "-m", "clodfarm", "gads", *a], capture_output=True,  # noqa
                                    text=True, env={**os.environ, "CLAUDECODE": "1"})
    r = run("accounts")
    assert r.returncode == 0 and "2345678901  Jestr Shop" in r.stdout and "[manager]" in r.stdout
    r = run("query", "SELECT campaign.name FROM campaign", "--customer", "2345678901")
    assert r.returncode == 0 and json.loads(r.stdout)[0]["metrics"]["clicks"] == "80"
    t = json.loads(run("token").stdout)
    assert t["headers"]["developer-token"] == GADS["developer_token"] and t["headers"]["login-customer-id"] == "1234567890"
    assert t["base"].endswith("/v24")



def test_google_ads_connects_without_a_developer_token(ui, google):
    _, farm_ui = ui
    creds = {k: v for k, v in GADS.items() if k != "developer_token"}
    v = connectors.gads_connect(farm_ui.cfg.workspace, creds)
    assert v["connected"] and len(v["customers"]) == 3 and v["developer_token_last4"] is None
    d = connectors.gads_load(farm_ui.cfg.workspace)
    assert "developer-token" not in connectors._gads_headers(d, "ya29.fake"), "no empty header for Google to refuse"
    assert "developer_token" not in open(connectors._gads_paths(farm_ui.cfg.workspace)[1]).read()


def test_the_manager_connects_google_ads_from_a_shell_and_a_live_dashboard_uses_it(ui, google, env):
    import subprocess
    import sys
    from clodfarm import dashboards
    _, farm_ui = ui
    shell = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "FARM_TASK_ID")}
    run = lambda *a, stdin=None, env=shell: subprocess.run(  # noqa
        [sys.executable, "-m", "clodfarm", "gads", *a], input=stdin, capture_output=True, text=True, env=env)
    r = run("connect", stdin=json.dumps({**GADS, "refresh_token": "expired"}))
    assert r.returncode == 1 and "invalid_grant" in r.stderr
    assert run("connect", stdin=json.dumps(GADS), env={**shell, "CLAUDECODE": "1"}).returncode == 2, "not a Claude's"
    r = run("connect", stdin=json.dumps(GADS))
    assert r.returncode == 0 and "3 account(s)" in r.stdout and "2345678901  Jestr Shop" in r.stdout, r.stderr
    assert "GOCSPX" not in r.stdout
    # the dashboard it prints is a valid spec, with the numbers added up
    r = run("dashboard", "--customer", "234-567-8901", "--days", "7")
    spec = dashboards.normalize(json.loads(r.stdout))
    assert spec["title"] == "Google Ads · Jestr Shop"
    stat = {w["key"]: w["value"] for w in spec["widgets"] if w["type"] == "stat"}
    assert stat == {"cost": 15.0, "clicks": 30, "impressions": 1200, "conversions": 3.0, "ctr": 2.5, "cpc": 0.5,
                    "cpa": 5.0}
    table = next(w for w in spec["widgets"] if w["type"] == "table")
    assert table["rows"][0][:4] == ["Jestr Shop · Search", "enabled", 49.5, 88], "a campaign's rows are added up"
    # and as a live dashboard, the farm runs it
    d = dashboards.push(farm_ui.store, "ads", {"title": "ads", "widgets": []})
    dashboards.set_refresh(farm_ui.store, "ads", f"{sys.executable} -m clodfarm gads dashboard --customer 2345678901",
                           3600)
    out = dashboards.refresh(farm_ui.store, dashboards.get(farm_ui.store, "ads"), farm_ui.cfg.workspace)
    assert out["ok"], out["error"]
    assert dashboards.get(farm_ui.store, "ads")["title"] == "Google Ads · Jestr Shop" and d
    assert run("disconnect").returncode == 0 and not connectors.gads_load(farm_ui.cfg.workspace)
