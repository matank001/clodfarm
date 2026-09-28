"""The farm's browser: its screen over the UI's WebSocket (login and origin checked), its on/off state, the CLI, and
the `browser` MCP server every Claude gets. Chromium itself runs in the image (docs/browser.md), not in these tests."""
import base64
import json
import os
import socket
import threading

import pytest

from clodfarm import auth, browser, prompts
from clodfarm.config import load
from clodfarm.store import Store
from clodfarm.web import FarmUI, make_handler
from test_web import client, login


@pytest.fixture
def farm(env, backend, monkeypatch, tmp_path):
    if backend != "sqlite":
        pytest.skip("the browser is per box; one store backend is enough")
    from http.server import ThreadingHTTPServer
    monkeypatch.setenv("FARM_UI_PASSWORD", "correct horse")
    monkeypatch.setenv("FARM_BROWSER_BIN", "no-such-chromium")  # no browser here, unless a test gives one
    novnc = tmp_path / "novnc"
    (novnc / "core").mkdir(parents=True)
    (novnc / "core" / "rfb.js").write_text("export default class RFB {}\n")
    monkeypatch.setenv("FARM_NOVNC_DIR", str(novnc))
    cfg = load()
    store = Store.from_config(cfg)
    store.ensure_table()
    ui = FarmUI(cfg, store)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ui))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"127.0.0.1:{srv.server_address[1]}", ui
    srv.shutdown()
    ui.manager.shutdown()


@pytest.fixture
def vnc(monkeypatch):
    """A stand-in VNC server: it greets like one, then echoes what it gets."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    monkeypatch.setenv("FARM_BROWSER_VNC_PORT", str(srv.getsockname()[1]))

    def serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=echo, args=(c,), daemon=True).start()

    def echo(c):
        with c:
            c.sendall(b"RFB 003.008\n")
            while data := c.recv(65536):
                c.sendall(data)
    threading.Thread(target=serve, daemon=True).start()
    yield
    srv.close()


def cookie(host) -> str:
    call = client()
    _, _, headers = call(f"http://{host}/api/login", {"password": "correct horse"})
    return headers["Set-Cookie"].split(";", 1)[0]


def upgrade(host, headers: dict, profile: str = ""):
    h, port = host.split(":")
    s = socket.create_connection((h, int(port)), timeout=5)
    lines = [f"GET /api/browser/screen{'?profile=' + profile if profile else ''} HTTP/1.1", f"Host: {host}", "Upgrade: websocket", "Connection: Upgrade",
             "Sec-WebSocket-Key: " + base64.b64encode(os.urandom(16)).decode(), "Sec-WebSocket-Version: 13",
             "Sec-WebSocket-Protocol: binary"] + [f"{k}: {v}" for k, v in headers.items()]
    s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    f = s.makefile("rb")
    status = int(f.readline().split()[1])
    head = {}
    while (line := f.readline().strip()):
        k, v = line.decode().split(":", 1)
        head[k.strip().lower()] = v.strip()
    return s, f, status, head


def test_the_screen_needs_the_login_and_a_farm_page(farm, vnc):
    host, _ = farm
    ok_origin = {"Origin": f"http://{host}"}
    s, _, status, _ = upgrade(host, ok_origin)
    s.close()
    assert status == 401  # no session cookie
    c = cookie(host)
    s, _, status, _ = upgrade(host, {"Origin": "https://evil.example", "Cookie": c})
    s.close()
    assert status == 403  # another site can't open it with your cookie
    s, _, status, _ = upgrade(host, {"Cookie": c})
    s.close()
    assert status == 403  # nor a client that sends no Origin


def test_the_screen_is_bridged_to_vnc(farm, vnc):
    host, _ = farm
    s, f, status, head = upgrade(host, {"Origin": f"http://{host}", "Cookie": cookie(host)})
    assert status == 101 and head["sec-websocket-protocol"] == "binary"
    op, data = browser.ws_read(f, masked=False)
    assert (op, data) == (2, b"RFB 003.008\n")
    s.sendall(browser.ws_frame(2, b"hello", mask=os.urandom(4)))
    assert browser.ws_read(f, masked=False) == (2, b"hello")
    big = os.urandom(70000)  # a 64-bit length frame, split by the bridge as it arrives
    s.sendall(browser.ws_frame(2, big, mask=os.urandom(4)))
    got = b""
    while len(got) < len(big):
        got += browser.ws_read(f, masked=False)[1]
    assert got == big
    s.sendall(browser.ws_frame(9, b"hi", mask=os.urandom(4)))
    assert browser.ws_read(f, masked=False) == (0xA, b"hi")  # ping, pong
    s.sendall(browser.ws_frame(8, b"\x03\xe8", mask=os.urandom(4)))
    assert browser.ws_read(f, masked=False)[0] == 8
    s.close()


def test_an_unmasked_client_frame_ends_the_bridge(farm, vnc):
    host, _ = farm
    s, f, status, _ = upgrade(host, {"Origin": f"http://{host}", "Cookie": cookie(host)})
    assert status == 101
    browser.ws_read(f, masked=False)
    s.sendall(browser.ws_frame(2, b"x"))
    assert browser.ws_read(f, masked=False)[0] == 8
    s.close()


def test_the_screen_says_when_the_browser_is_off(farm, monkeypatch):
    host, _ = farm
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    monkeypatch.setenv("FARM_BROWSER_VNC_PORT", str(s.getsockname()[1]))  # nothing listens there
    s.close()
    ws, _, status, _ = upgrade(host, {"Origin": f"http://{host}", "Cookie": cookie(host)})
    ws.close()
    assert status == 503


def test_status_start_and_stop(farm, monkeypatch):
    host, ui = farm
    base, call = f"http://{host}", client()
    assert call(base + "/api/browser")[0] == 401
    login(call, base)
    code, st, _ = call(base + "/api/browser")
    assert code == 200 and not st["available"] and "chromium" in st["missing"]
    assert [(p["name"], p["on"], p["tools"]) for p in st["profiles"]] == [("default", False, "mcp__browser__*")]
    code, body, _ = call(base + "/api/browser/start", {})
    assert code == 400 and "no browser" in body["error"]
    monkeypatch.setattr(browser, "missing", lambda: [])  # as in the image
    monkeypatch.setattr(ui.browsers, "sync", lambda: None)
    code, st, _ = call(base + "/api/browser/start", {})
    assert code == 200 and st["profiles"][0]["on"] and ui.browsers.registry.get("default")["on"]
    assert any(e["type"] == "browser.started" for e in ui.store.events(0, 50))
    code, st, _ = call(base + "/api/browser/stop", {"profile": "default"})
    assert code == 200 and not st["profiles"][0]["on"]
    assert call(base + "/api/browser/open", {"url": "javascript:alert(1)"})[0] == 400
    assert call(base + "/api/browser/start", {"profile": "nope"})[0] == 400


def test_profiles_from_the_ui(farm, monkeypatch):
    host, ui = farm
    base, call = f"http://{host}", client()
    login(call, base)
    given = []
    monkeypatch.setattr(ui.manager, "share_browser_tools", lambda: given.append(1))
    monkeypatch.setattr(ui.browsers, "sync", lambda: None)
    code, st, _ = call(base + "/api/browser/add", {"profile": "linkedin-work"})
    assert code == 200 and [p["name"] for p in st["profiles"]] == ["default", "linkedin-work"] and given
    assert st["profiles"][1]["tools"] == "mcp__browser-linkedin-work__*"
    assert ui.browsers.slot("linkedin-work") == 1  # its own ports and screen
    assert call(base + "/api/browser/add", {"profile": "linkedin-work"})[0] == 400  # taken
    assert call(base + "/api/browser/add", {"profile": "Bad Name"})[0] == 400
    assert call(base + "/api/browser/remove", {"profile": "default"})[0] == 400  # the default one stays
    os.makedirs(ui.browsers.registry.dir("linkedin-work"))
    code, st, _ = call(base + "/api/browser/remove", {"profile": "linkedin-work"})
    assert code == 200 and [p["name"] for p in st["profiles"]] == ["default"]
    assert not os.path.exists(ui.browsers.registry.dir("linkedin-work"))  # its logins are gone with it
    assert any(e["type"] == "browser.removed" for e in ui.store.events(0, 50))


def test_the_screen_of_another_profile(farm, vnc, monkeypatch):
    host, ui = farm
    ui.browsers.registry.add("second")
    port = int(os.environ["FARM_BROWSER_VNC_PORT"])
    monkeypatch.setenv("FARM_BROWSER_VNC_PORT", str(port - 1))  # slot 1 is the stand-in's port
    s, f, status, _ = upgrade(host, {"Origin": f"http://{host}", "Cookie": cookie(host)}, "second")
    assert status == 101 and browser.ws_read(f, masked=False) == (2, b"RFB 003.008\n")
    s.close()
    s, _, status, _ = upgrade(host, {"Origin": f"http://{host}", "Cookie": cookie(host)}, "nope")
    s.close()
    assert status == 404


def test_the_page_and_novnc_are_served(farm):
    host, _ = farm
    import urllib.request
    import urllib.error
    with urllib.request.urlopen(f"http://{host}/browser") as r:
        page, csp = r.read().decode(), r.headers["Content-Security-Policy"]
    assert 'src="/browser.js"' in page and f"ws://{host}" in csp and "frame-ancestors 'none'" in csp
    with urllib.request.urlopen(f"http://{host}/browser/novnc/core/rfb.js") as r:
        assert b"class RFB" in r.read()
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(f"http://{host}/browser/novnc/../../../etc/passwd")
    assert e.value.code == 404


def test_profiles_survive_and_nothing_starts_without_a_browser(tmp_path, monkeypatch):
    monkeypatch.setenv("FARM_BROWSER_BIN", "no-such-chromium")
    bs = browser.Browsers(str(tmp_path))
    assert [p["name"] for p in bs.registry.all()] == ["default"] and not bs.registry.get("default")["on"]
    assert bs.want("default", True, by="test") and not bs.want("default", True)
    bs.registry.add("b")
    bs.registry.add("c")
    bs.registry.remove("b")
    assert bs.registry.add("d")["slot"] == 1  # a freed slot is reused
    again = browser.Browsers(str(tmp_path))  # a restarted farm
    assert again.registry.get("default")["on"] and [p["name"] for p in again.registry.all()] == ["default", "c", "d"]
    again.sync()
    assert not any(b.procs for b in again.running.values())  # this box has no chromium: nothing is started
    for i in range(browser.MAX_PROFILES - 3):
        again.registry.add(f"x{i}")
    with pytest.raises(ValueError):
        again.registry.add("one-too-many")


def test_the_first_build_s_browser_is_the_default_profile(tmp_path):
    os.makedirs(tmp_path / ".farm")
    json.dump({"on": True}, open(tmp_path / ".farm" / "browser.json", "w"))
    reg = browser.Registry(str(tmp_path))
    assert reg.get("default")["on"] and reg.dir("default") == str(tmp_path / ".farm" / "browser")


def test_each_profile_has_its_own_ports_screen_and_files(env, monkeypatch):
    monkeypatch.setattr(browser.shutil, "which", lambda b: f"/usr/bin/{b}")
    reg = browser.Registry(os.environ["FARM_WORKSPACE"])
    reg.add("work")
    servers = browser.mcp_servers()
    assert list(servers) == ["browser", "browser-work"]
    assert servers["browser-work"]["args"][:2] == ["--cdp-endpoint", "http://127.0.0.1:9223"]
    out = servers["browser-work"]["args"][servers["browser-work"]["args"].index("--output-dir") + 1]
    assert out == os.path.join(os.environ["FARM_WORKSPACE"], ".farm", "browser-files", "work")  # not the worktree
    b = browser.Browser("work", 1, reg.dir("work"), "/dev/null")
    assert b.display == ":100" and "5901" in b._cmd("vnc") and "--remote-debugging-port=9223" in b._cmd("chromium")
    monkeypatch.setenv("FARM_BROWSER", "0")
    assert browser.mcp_servers() == {}


def test_urls():
    assert browser.normalize_url("linkedin.com/login") == "https://linkedin.com/login"
    assert browser.normalize_url("http://localhost:3000/x") == "http://localhost:3000/x"
    assert browser.normalize_url("about:blank") == "about:blank"
    for bad in ("javascript:alert(1)", "file:///etc/passwd", "", "https://a b"):
        with pytest.raises(ValueError):
            browser.normalize_url(bad)


def server(port):
    return {"type": "stdio", "command": "/usr/local/bin/playwright-mcp", "env": {},
            "args": ["--cdp-endpoint", f"http://127.0.0.1:{port}"]}


def test_every_claude_gets_a_server_per_profile(env, monkeypatch):
    path = auth.claude_json_path()
    want = {"browser": server(9222), "browser-work": server(9223)}
    monkeypatch.setattr(browser, "mcp_servers", lambda workspace=None: dict(want))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump({"mcpServers": {"github": {"command": "gh-mcp"}}, "keep": 1}, open(path, "w"))
    assert auth.install_browser_mcp()
    cfg = json.load(open(path))
    assert cfg["mcpServers"]["browser-work"] == want["browser-work"] and cfg["mcpServers"]["github"] and cfg["keep"] == 1
    assert not auth.install_browser_mcp()  # already there
    assert "## The farm's browser" in prompts.farm_guide()
    del want["browser-work"]  # the profile was removed
    assert auth.install_browser_mcp()
    assert set(json.load(open(path))["mcpServers"]) == {"github", "browser"}
    want.clear()  # an image without the browser
    assert auth.install_browser_mcp()
    assert set(json.load(open(path))["mcpServers"]) == {"github"}
    assert "## The farm's browser" not in prompts.farm_guide()


def test_every_claude_on_the_box_gets_them(farm, monkeypatch, tmp_path):
    _, ui = farm
    monkeypatch.setattr(browser, "mcp_servers", lambda workspace=None: {"browser": server(9222)})
    other = tmp_path / "gil"
    other.mkdir()
    monkeypatch.setattr(ui.manager, "_load", lambda: [{"id": "gil", "name": "gil", "config_dir": str(other)}])
    ui.manager.share_browser_tools()
    for p in (auth.claude_json_path(), other / ".claude.json"):
        assert json.load(open(p))["mcpServers"]["browser"] == server(9222)


def test_a_browser_server_set_up_by_hand_is_kept(env, monkeypatch):
    path = auth.claude_json_path()
    mine = {"command": "npx", "args": ["@playwright/mcp@latest"]}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump({"mcpServers": {"browser": mine}}, open(path, "w"))
    monkeypatch.setattr(browser, "mcp_servers", lambda workspace=None: {"browser": server(9222)})
    assert not auth.install_browser_mcp()
    assert json.load(open(path))["mcpServers"]["browser"] == mine


def test_the_cli(env, monkeypatch, capsys):
    from clodfarm import cli
    monkeypatch.setenv("FARM_BROWSER_BIN", "no-such-chromium")
    assert cli.main(["browser", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["available"] is False
    assert cli.main(["browser", "start"]) == 1
    assert "no browser" in capsys.readouterr().err
    assert cli.main(["browser", "add", "work"]) == 0
    assert "mcp__browser-work__*" in capsys.readouterr().out
    assert cli.main(["browser", "--json"]) == 0
    assert [p["name"] for p in json.loads(capsys.readouterr().out)["profiles"]] == ["default", "work"]
    assert cli.main(["browser", "open", "x.com", "--profile", "work"]) == 1  # it's off
    assert cli.main(["browser", "remove", "work"]) == 0
    assert cli.main(["browser", "remove", "work"]) == 1  # gone already


# ------------------------------------------------------------------ the proxy
def test_a_proxy_is_written_the_ways_providers_write_it():
    di = {"host": "gw.dataimpulse.com", "port": 823, "user": "abc123", "password": "s3cret"}
    for text in ("abc123:s3cret@gw.dataimpulse.com:823", "http://abc123:s3cret@gw.dataimpulse.com:823/",
                 "gw.dataimpulse.com:823:abc123:s3cret", "  HTTP://abc123:s3cret@gw.dataimpulse.com:823\n"):
        assert browser.parse_proxy(text) == di
    assert browser.parse_proxy("abc__cr.us;sessid.7:p%40ss@74.81.81.81:10000") == \
        {"host": "74.81.81.81", "port": 10000, "user": "abc__cr.us;sessid.7", "password": "p@ss"}
    assert browser.parse_proxy("a:p@ss@gw.dataimpulse.com:823")["password"] == "p@ss"  # an @ in the password
    assert browser.parse_proxy("gw.dataimpulse.com:823")["user"] == ""  # IP-whitelisted: no login
    for bad in ("socks5://a:b@gw.dataimpulse.com:824", "gw.dataimpulse.com", "a:b@gw.dataimpulse.com:99999",
                "a:b@-bad-:823", "", "a b:c@h.com:1"):
        with pytest.raises(ValueError):
            browser.parse_proxy(bad)


@pytest.fixture
def upstream():
    """A stand-in for the proxy provider: it wants the login `u:p`, records each request's head, answers CONNECT
    with 200 and then echoes, and a plain request with a small keep-alive response."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    heads = []

    def serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=one, args=(c,), daemon=True).start()

    def one(c):
        with c:
            head, _ = browser._head(c)
            heads.append(head.decode())
            if f"Basic {base64.b64encode(b'u:p').decode()}" not in head.decode():
                return c.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                                 b"Proxy-Authenticate: Basic realm=\"x\"\r\nContent-Length: 0\r\n\r\n")
            if head.startswith(b"CONNECT"):
                c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                while data := c.recv(65536):
                    c.sendall(data)
            else:
                c.sendall(b"HTTP/1.1 200 OK\r\nConnection: keep-alive\r\nContent-Length: 2\r\n\r\nhi")
    threading.Thread(target=serve, daemon=True).start()
    yield {"host": "127.0.0.1", "port": srv.getsockname()[1], "user": "u", "password": "p"}, heads
    srv.close()


def ask(relay, request: bytes) -> tuple[socket.socket, bytes]:
    s = socket.create_connection(("127.0.0.1", relay.port), timeout=5)
    s.sendall(request)
    head, rest = browser._head(s)
    return s, head + rest


def test_the_relay_logs_in_for_chromium(upstream):
    up, heads = upstream
    relay = browser.Relay(up)
    try:
        s, got = ask(relay, b"CONNECT linkedin.com:443 HTTP/1.1\r\nHost: linkedin.com:443\r\n"
                            b"Proxy-Authorization: Basic Y2hyb21pdW06Z3Vlc3M=\r\nProxy-Connection: keep-alive\r\n\r\n")
        assert got.startswith(b"HTTP/1.1 200")
        s.sendall(b"\x16\x03\x01 a TLS hello")
        assert s.recv(100) == b"\x16\x03\x01 a TLS hello"  # the tunnel is a straight pipe
        s.close()
        h = heads[-1]
        assert h.startswith("CONNECT linkedin.com:443 HTTP/1.1\r\n") and h.count("Proxy-Authorization") == 1
        assert "Y2hyb21pdW06Z3Vlc3M=" not in h and "Proxy-Connection" not in h  # Chromium's own are dropped

        s, got = ask(relay, b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n"
                            b"Proxy-Connection: keep-alive\r\n\r\n")
        s.close()
        assert got.startswith(b"HTTP/1.1 200 OK") and b"Connection: close" in got and b"keep-alive" not in got
        assert "Connection: close" in heads[-1] and "keep-alive" not in heads[-1]  # one request per connection
        assert not relay.error

        s, got = ask(relay, b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")  # a page calling the relay directly
        s.close()
        assert got.startswith(b"HTTP/1.1 400") and len(heads) == 2
    finally:
        relay.close()


def test_a_wrong_login_is_the_farm_s_error_not_chromium_s_prompt(upstream):
    up, _ = upstream
    relay = browser.Relay({**up, "password": "wrong"})
    try:
        s, got = ask(relay, b"CONNECT linkedin.com:443 HTTP/1.1\r\nHost: linkedin.com:443\r\n\r\n")
        s.close()
        assert got.startswith(b"HTTP/1.1 502") and b"Proxy-Authenticate" not in got
        assert "refused its login" in relay.error
        with pytest.raises(ValueError, match="refused the login"):
            browser.check_proxy({**up, "password": "wrong"})
    finally:
        relay.close()
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    port = free.getsockname()[1]
    free.close()
    with pytest.raises(ValueError, match="can't reach"):
        browser.check_proxy({**up, "port": port}, timeout=2)


def test_turning_the_proxy_on_restarts_chromium_through_the_relay(tmp_path, monkeypatch, upstream):
    up, _ = upstream
    monkeypatch.setattr(browser, "available", lambda: True)
    monkeypatch.setattr(browser.Browser, "_wait", staticmethod(lambda *a: None))
    b = browser.Browser("default", 0, str(tmp_path / "profile"), str(tmp_path / "browser.log"))
    started = []
    real_cmd = b._cmd

    def cmd(name):
        started.append((name, real_cmd(name) if name == "chromium" else []))
        return ["sleep", "30"]
    monkeypatch.setattr(b, "_cmd", cmd)
    try:
        b.sync(True)
        assert [n for n, _ in started] == ["xvfb", "vnc", "chromium"] and b.relay is None
        assert not any(a.startswith("--proxy-server") for a in started[-1][1])
        b.sync(True)
        assert len(started) == 3  # nothing changed, nothing restarts
        b.sync(True, up)
        assert [n for n, _ in started[3:]] == ["chromium"] and b.relay and b.relay.upstream == up
        assert f"--proxy-server=http://127.0.0.1:{b.relay.port}" in started[-1][1]
        assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in started[-1][1]
        relay = b.relay
        b.sync(True)  # off again: a restart without it
        assert [n for n, _ in started[4:]] == ["chromium"] and b.relay is None and relay.closed.is_set()
    finally:
        b.shutdown()
    assert not b.procs and b.relay is None


def test_a_country_goes_in_a_dataimpulse_login():
    di = browser.parse_proxy("abc:pw@gw.dataimpulse.com:10000")
    assert browser.countries(di) and browser.with_country(di, "us")["user"] == "abc__cr.us"
    assert browser.with_country({**di, "user": "abc__cr.de;sessttl.60"}, "us")["user"] == "abc__cr.us;sessttl.60"
    assert browser.with_country(di, "") == di  # any country: the login as it was given
    assert browser.with_country(di, "us")["password"] == "pw" and di["user"] == "abc"  # a copy
    other = browser.parse_proxy("abc:pw@proxy.example.com:8080")
    assert not browser.countries(other) and browser.with_country(other, "us") == other
    assert not browser.countries(browser.parse_proxy("gw.dataimpulse.com:10000"))  # whitelisted: no login for it
    assert browser.country_code(" US ") == "us"
    for bad in ("usa", "u", "1a"):
        with pytest.raises(ValueError):
            browser.country_code(bad)


def seen(ip="203.0.113.7", country="us"):
    return {"ip": ip, "country": country, "city": "Ashburn"}


def test_a_profile_through_the_proxy_never_goes_out_direct(tmp_path, monkeypatch):
    checked = []
    monkeypatch.setattr(browser, "check_proxy", lambda p: checked.append(p) or seen())
    bs = browser.Browsers(str(tmp_path))
    with pytest.raises(ValueError, match="no proxy"):
        bs.proxy("default", True)
    browser.save_proxy(str(tmp_path), browser.parse_proxy("u:p@gw.dataimpulse.com:823"))
    assert oct(os.stat(browser._proxy_path(str(tmp_path))).st_mode & 0o777) == "0o600"
    assert bs.proxy("default", True, by="test") == (True, seen()) and checked[-1]["user"] == "u"
    assert bs.proxy("default", True)[0] is False  # already on
    assert bs.proxy("default", True, country="us")[0] and checked[-1]["user"] == "u__cr.us"  # checked from there
    assert bs.proxy("default", True)[0] is False and checked[-1]["user"] == "u__cr.us"  # it keeps its country
    bs.want("default", True)
    synced = []
    monkeypatch.setattr(browser.Browser, "sync", lambda self, on, proxy=None: synced.append((on, proxy)))
    bs.sync()
    assert synced[-1] == (True, {"host": "gw.dataimpulse.com", "port": 823, "user": "u__cr.us", "password": "p"})
    browser.save_proxy(str(tmp_path), None)  # the proxy went away: the profile stops, it doesn't go direct
    bs.sync()
    assert synced[-1] == (False, None) and "no proxy" in bs.status()["profiles"][0]["error"]
    monkeypatch.setenv(browser.PROXY_ENV, "http://envuser:envpw@gw.dataimpulse.com:10000")
    bs.sync()
    assert synced[-1] == (True, {"host": "gw.dataimpulse.com", "port": 10000, "user": "envuser__cr.us",
                                 "password": "envpw", "from_env": True})
    st = bs.status()
    assert st["proxy"] == {"set": True, "server": "gw.dataimpulse.com:10000", "login": True, "from_env": True,
                           "countries": True}
    p = st["profiles"][0]
    assert p["proxy"] and p["country"] == "us" and p["proxy_seen"]["ip"] == "203.0.113.7"
    assert "envpw" not in json.dumps(st)
    monkeypatch.setenv(browser.PROXY_ENV, "u:p@proxy.example.com:8080")  # not DataImpulse: no country to choose
    with pytest.raises(ValueError, match="DataImpulse"):
        bs.proxy("default", True, country="de")
    assert bs.status()["profiles"][0]["country"] == ""
    assert bs.proxy("default", False) == (True, None) and not bs.status()["profiles"][0]["proxy_seen"]


def test_the_proxy_from_the_ui(farm, monkeypatch):
    host, ui = farm
    base, call = f"http://{host}", client()
    login(call, base)
    monkeypatch.setattr(ui.browsers, "sync", lambda: None)
    code, body, _ = call(base + "/api/browser/proxy", {"profile": "default", "on": True})
    assert code == 400 and "no proxy" in body["error"]
    checked = []
    monkeypatch.setattr(browser, "check_proxy", lambda p: checked.append(p) or seen())
    assert call(base + "/api/browser/proxy/address", {"address": "socks5://a:b@gw.dataimpulse.com:824"})[0] == 400
    code, _, _ = call(base + "/api/browser/proxy/address", {"address": "abc:s3cret@gw.dataimpulse.com:823",
                                                            "profile": "nope"})
    assert code == 400 and not checked and browser.load_proxy(ui.cfg.workspace) is None  # nothing kept
    code, st, _ = call(base + "/api/browser/proxy/address", {"address": "abc:s3cret@gw.dataimpulse.com:823"})
    assert code == 200 and st["seen"] == seen() and checked[-1]["password"] == "s3cret"
    assert st["proxy"] == {"set": True, "server": "gw.dataimpulse.com:823", "login": True, "from_env": False,
                           "countries": True}
    assert "s3cret" not in json.dumps(st) and "s3cret" not in json.dumps(call(base + "/api/browser")[1])
    assert not st["profiles"][0]["proxy"]  # no profile given: none turned on
    code, st, _ = call(base + "/api/browser/proxy", {"profile": "default", "on": True})
    assert code == 200 and st["profiles"][0]["proxy"] and ui.browsers.registry.get("default")["proxy"]
    code, st, _ = call(base + "/api/browser/proxy", {"profile": "default", "on": True, "country": "de"})
    assert code == 200 and st["profiles"][0]["country"] == "de" and checked[-1]["user"] == "abc__cr.de"
    assert st["profiles"][0]["proxy_seen"]["ip"] == "203.0.113.7" and st["seen"]["city"] == "Ashburn"
    assert call(base + "/api/browser/proxy", {"profile": "default", "on": True, "country": "germany"})[0] == 400
    ui.browsers.registry.add("work")
    ui.browsers.registry.proxy("work", False, country="fr")
    code, st, _ = call(base + "/api/browser/proxy/address", {"address": "abc:s3cret@gw.dataimpulse.com:10000",
                                                             "profile": "work"})
    assert code == 200 and st["profiles"][1]["proxy"]  # set from a profile: that profile goes through it
    assert checked[-1]["user"] == "abc__cr.fr"  # from its country
    events = [e["msg"] for e in ui.store.events(0, 50) if e["type"] == "browser.proxy"]
    assert any("(203.0.113.7, US)" in t for t in events)
    monkeypatch.setattr(browser, "check_proxy", lambda p: (_ for _ in ()).throw(ValueError("the proxy refused the login")))
    code, body, _ = call(base + "/api/browser/proxy/address", {"address": "abc:wrong@gw.dataimpulse.com:823"})
    assert code == 400 and "refused" in body["error"]
    assert browser.load_proxy(ui.cfg.workspace)["password"] == "s3cret"  # a failed check keeps the old one
    code, body, _ = call(base + "/api/browser/proxy", {"profile": "default", "on": True, "country": "it"})
    assert code == 400 and ui.browsers.registry.get("default")["country"] == "de"  # a failed check changes nothing
    code, st, _ = call(base + "/api/browser/proxy", {"profile": "default", "on": False})
    assert code == 200 and not st["profiles"][0]["proxy"]  # going direct needs no check
    code, st, _ = call(base + "/api/browser/proxy/address", {"address": ""})
    assert code == 200 and st["proxy"] == {"set": False} and browser.load_proxy(ui.cfg.workspace) is None
    monkeypatch.setenv(browser.PROXY_ENV, "u:p@gw.dataimpulse.com:823")
    code, body, _ = call(base + "/api/browser/proxy/address", {"address": "abc:s3cret@gw.dataimpulse.com:823"})
    assert code == 400 and browser.PROXY_ENV in body["error"]


def test_the_proxy_from_the_cli(env, monkeypatch, capsys):
    from clodfarm import cli
    monkeypatch.setenv("FARM_BROWSER_BIN", "no-such-chromium")
    monkeypatch.setattr(browser, "check_proxy", lambda p: seen(country="gb"))
    assert cli.main(["browser", "proxy", "on"]) == 1
    assert "no proxy" in capsys.readouterr().err
    browser.save_proxy(os.environ["FARM_WORKSPACE"], browser.parse_proxy("u:p@gw.dataimpulse.com:823"))
    assert cli.main(["browser", "proxy", "on", "--country", "gb"]) == 0
    assert "through the proxy (the web sees 203.0.113.7, GB)" in capsys.readouterr().out
    monkeypatch.setattr(browser, "missing", lambda: [])  # as in the image
    assert cli.main(["browser"]) == 0
    out = capsys.readouterr().out
    assert "via the proxy from GB" in out and "proxy: gw.dataimpulse.com:823" in out and ":p@" not in out
    assert cli.main(["browser", "proxy", "off", "default"]) == 0
    assert cli.main(["browser", "proxy", "on", "nope"]) == 1
