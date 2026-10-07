"""The farm as a remote MCP server: OAuth discovery, registration, consent, PKCE, tokens, scopes and the tools."""
import base64
import hashlib
import json
import re
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from clodfarm.config import load
from clodfarm.store import Store
from clodfarm.web import FarmUI, make_handler

REDIRECT = "http://localhost:53682/callback"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


OPEN = urllib.request.build_opener(NoRedirect)
SIGNED_IN = {}


@pytest.fixture
def farm(env, backend, monkeypatch):
    if backend != "sqlite":
        pytest.skip("same Store API on both backends; one is enough")
    from http.server import ThreadingHTTPServer
    monkeypatch.setenv("FARM_UI_PASSWORD", "correct horse")
    cfg = load()
    store = Store.from_config(cfg)
    store.ensure_table()
    ui = FarmUI(cfg, store)
    # a person signed in to their Claude on the farm connects their Claude Code (no farm password any more)
    SIGNED_IN["cookie"] = "clodfarm_owner=" + ui.keys.make("owner", [cfg.name, "1"], 365)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ui))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setenv("FARM_PUBLIC_URL", base)
    yield base, ui
    srv.shutdown()
    ui.manager.shutdown()


def req(url, data=None, headers=None, form=False, method=None):
    body = None
    hdrs = dict(headers or {})
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        else:
            body = json.dumps(data).encode()
            hdrs.setdefault("Content-Type", "application/json")
    r = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with OPEN.open(r) as resp:
            return resp.status, resp.read().decode(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


def pkce():
    v = secrets.token_urlsafe(48)
    return v, base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).decode().rstrip("=")


def register(base):
    code, body, _ = req(base + "/oauth/register", {"client_name": "Claude Code (farm)", "redirect_uris": [REDIRECT],
                                                   "token_endpoint_auth_method": "none"})
    assert code == 201, body
    return json.loads(body)["client_id"]


def authorize(base, cid, challenge, name="matan-laptop", access="work", signed_in=True, state="s1"):
    q = {"response_type": "code", "client_id": cid, "redirect_uri": REDIRECT, "code_challenge": challenge,
         "code_challenge_method": "S256", "state": state, "resource": base + "/mcp"}
    ck = {"Cookie": SIGNED_IN["cookie"]} if signed_in else {}
    code, page, hdrs = req(base + "/oauth/authorize?" + urllib.parse.urlencode(q), headers=ck)
    assert code == 200, page
    assert "form-action 'self' http://localhost:53682" in hdrs["Content-Security-Policy"]
    fields = dict(re.findall(r'<input type="hidden" name="([a-z_]+)" value="([^"]*)"', page))
    fields = {k: v.replace("&amp;", "&") for k, v in fields.items()}
    fields.update(decision="allow", name=name, access=access)
    return req(base + "/oauth/authorize", fields, form=True, headers=ck)


def connect(base, **kw):
    cid = register(base)
    verifier, challenge = pkce()
    code, _, hdrs = authorize(base, cid, challenge, **kw)
    assert code == 302, hdrs
    loc = urllib.parse.urlsplit(hdrs["Location"])
    q = dict(urllib.parse.parse_qsl(loc.query))
    assert loc.geturl().startswith(REDIRECT) and q["state"] == "s1" and q["iss"] == base
    code, body, _ = req(base + "/oauth/token", {"grant_type": "authorization_code", "code": q["code"],
                                                "code_verifier": verifier, "client_id": cid,
                                                "redirect_uri": REDIRECT, "resource": base + "/mcp"}, form=True)
    assert code == 200, body
    return cid, json.loads(body)


def call(base, token, method, params=None, mid=1):
    code, body, hdrs = req(base + "/mcp", {"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}},
                           {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"})
    return code, (json.loads(body) if body else None), hdrs


def tool(base, token, name, **args):
    code, out, _ = call(base, token, "tools/call", {"name": name, "arguments": args})
    assert code == 200, out
    r = out["result"]
    return r["isError"], (r["content"][0]["text"] if r["isError"] else json.loads(r["content"][0]["text"]))


def test_discovery_and_401_challenge(farm):
    base, _ = farm
    code, body, hdrs = req(base + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert code == 401
    assert f'resource_metadata="{base}/.well-known/oauth-protected-resource"' in hdrs["WWW-Authenticate"]
    prm = json.loads(req(base + "/.well-known/oauth-protected-resource")[1])
    assert prm["resource"] == base + "/mcp" and prm["authorization_servers"] == [base]
    asm = json.loads(req(base + "/.well-known/oauth-authorization-server")[1])
    assert asm["code_challenge_methods_supported"] == ["S256"] and asm["registration_endpoint"].endswith("/oauth/register")
    assert req(base + "/mcp")[0] == 405  # no server-initiated stream


def test_registration_rejects_non_loopback_http(farm):
    base, _ = farm
    for bad in (["http://evil.example/cb"], ["javascript:alert(1)"], [], ["https://x.test/cb#frag"]):
        assert req(base + "/oauth/register", {"client_name": "x", "redirect_uris": bad})[0] == 400
    assert req(base + "/oauth/register", {"client_name": "x", "redirect_uris": ["https://ok.test/cb"]})[0] == 201


def test_full_flow_tools_and_messages(farm):
    base, ui = farm
    _, tok = connect(base)
    assert tok["token_type"] == "Bearer" and tok["scope"] == "farm:read farm:work"
    code, out, _ = call(base, tok["access_token"], "initialize",
                        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
    assert out["result"]["protocolVersion"] == "2025-06-18" and "matan-laptop" in out["result"]["instructions"]
    assert req(base + "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"},
               {"Authorization": "Bearer " + tok["access_token"]})[0] == 202
    names = {t["name"] for t in call(base, tok["access_token"], "tools/list")[1]["result"]["tools"]}
    assert {"farm_status", "farm_spawn", "farm_msg", "farm_result"} <= names
    err, st = tool(base, tok["access_token"], "farm_status")
    assert not err and st["you"] == ui.cfg.name and st["computer"] == "matan-laptop"  # its person's Claude, from there
    err, started = tool(base, tok["access_token"], "farm_spawn", title="Add CSV export", prompt="Add CSV export, with tests.")
    assert not err
    t = ui.store.get_task(started["started"])
    assert t["owner"] == ui.cfg.name and t["created_by"] == "matan-laptop" and t["status"] == "queued"
    err, res = tool(base, tok["access_token"], "farm_result", id=t["id"])
    assert res["status"] == "queued" and res["prompt"] == "Add CSV export, with tests."
    # a farm Claude answers the connection by name; the connection reads it in its inbox
    ui.store.send_message(ui.cfg.name, "matan-laptop", "on it")
    err, inbox = tool(base, tok["access_token"], "farm_inbox")
    assert [m["text"] for m in inbox] == ["on it"]
    err, text = tool(base, tok["access_token"], "farm_msg", to="nobody", text="hi")
    assert err and "no Claude named" in text
    assert tool(base, tok["access_token"], "farm_spawn", title="x", prompt="y", on="ghost")[0]
    assert any(e["type"] == "mcp.connected" for e in ui.store.events(None, 50))


def test_read_only_connection_cannot_work(farm):
    base, _ = farm
    _, tok = connect(base, access="read")
    assert tok["scope"] == "farm:read"
    names = {t["name"] for t in call(base, tok["access_token"], "tools/list")[1]["result"]["tools"]}
    assert "farm_spawn" not in names and "farm_status" in names
    err, text = tool(base, tok["access_token"], "farm_spawn", title="x", prompt="y")
    assert err and "no farm:work access" in text


def test_consent_guards(farm):
    base, ui = farm
    cid = register(base)
    verifier, challenge = pkce()
    assert authorize(base, cid, challenge, signed_in=False)[0] == 400                  # not signed in: page again
    assert authorize(base, cid, challenge, name=ui.cfg.name)[0] == 400                  # a Claude's own name
    assert authorize(base, cid, challenge, name="Bad Name!")[0] == 400
    q = {"response_type": "code", "client_id": cid, "redirect_uri": "http://localhost:9999/other",
         "code_challenge": challenge, "code_challenge_method": "S256"}
    code, page, hdrs = req(base + "/oauth/authorize?" + urllib.parse.urlencode(q))
    assert code == 400 and "Location" not in hdrs                                        # never redirect to a stranger
    q.update(redirect_uri=REDIRECT, code_challenge_method="plain")
    code, _, hdrs = req(base + "/oauth/authorize?" + urllib.parse.urlencode(q))
    assert code == 302 and "error=invalid_request" in hdrs["Location"]                   # PKCE S256 only
    # a tampered form (redirect or scope changed after the page was shown) is refused
    code, page, _ = req(base + "/oauth/authorize?" + urllib.parse.urlencode(
        {**q, "code_challenge_method": "S256", "scope": "farm:read"}))
    fields = dict(re.findall(r'<input type="hidden" name="([a-z_]+)" value="([^"]*)"', page))
    fields.update(decision="allow", name="x-laptop", scope="farm:read farm:work")
    assert req(base + "/oauth/authorize", fields, form=True, headers={"Cookie": SIGNED_IN["cookie"]})[0] == 400


def test_pkce_code_reuse_refresh_rotation_and_revoke(farm):
    base, ui = farm
    cid = register(base)
    verifier, challenge = pkce()
    code, _, hdrs = authorize(base, cid, challenge)
    c = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(hdrs["Location"]).query))["code"]
    tok_req = {"grant_type": "authorization_code", "code": c, "client_id": cid, "redirect_uri": REDIRECT}
    assert req(base + "/oauth/token", {**tok_req, "code_verifier": pkce()[0]}, form=True)[0] == 400  # wrong verifier
    assert req(base + "/oauth/token", {**tok_req, "code_verifier": verifier}, form=True)[0] == 400   # code is spent
    _, tok = connect(base)
    r1 = tok["refresh_token"]
    code, body, _ = req(base + "/oauth/token", {"grant_type": "refresh_token", "refresh_token": r1,
                                                "client_id": tok_cid(ui, tok)}, form=True)
    assert code == 200
    tok2 = json.loads(body)
    assert call(base, tok["access_token"], "ping")[0] == 401           # the old access token ends with the refresh
    assert call(base, tok2["access_token"], "ping")[0] == 200
    # the old refresh token used again: someone copied it, so the whole connection ends
    assert req(base + "/oauth/token", {"grant_type": "refresh_token", "refresh_token": r1,
                                       "client_id": tok_cid(ui, tok)}, form=True)[0] == 400
    assert call(base, tok2["access_token"], "ping")[0] == 401
    _, tok3 = connect(base)
    gid = ui.oauth.connections()[-1]["id"]
    assert ui.oauth.disconnect(gid)
    assert call(base, tok3["access_token"], "ping")[0] == 401


def tok_cid(ui, tok):
    g = ui.oauth.check(tok["access_token"])
    return g["client_id"] if g else next(iter(ui.oauth.data["grants"].values()))["client_id"]


def test_tokens_are_stored_hashed(farm):
    base, ui = farm
    _, tok = connect(base)
    raw = open(ui.oauth.path).read()
    assert tok["access_token"] not in raw and tok["refresh_token"] not in raw


def test_foreign_origin_refused(farm):
    base, _ = farm
    _, tok = connect(base)
    code, _, _ = req(base + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                     {"Authorization": "Bearer " + tok["access_token"], "Origin": "https://evil.example"})
    assert code == 403
    code, _, _ = req(base + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"},   # a prefix of our origin is not it
                     {"Authorization": "Bearer " + tok["access_token"], "Origin": base[:-1]})
    assert code == 403
    assert call(base, tok["access_token"], "ping")[0] == 200


def test_open_registration_cannot_fill_up(farm, monkeypatch):
    base, ui = farm
    import clodfarm.mcp as m
    monkeypatch.setattr(m, "MAX_CLIENTS", 3)
    _, tok = connect(base)
    for _ in range(5):
        register(base)
    assert len(ui.oauth.data["clients"]) == 3 and call(base, tok["access_token"], "ping")[0] == 200


def manage(base, path, data=None):
    hdrs = {"Cookie": SIGNED_IN["cookie"], "X-Clodfarm": "1"}  # the person of the farm's own Claude: its manager
    code, body, _ = req(base + path, data, hdrs) if data is not None else req(base + path, headers=hdrs)
    assert code == 200, body
    return json.loads(body)


def test_the_manager_sees_disconnects_and_turns_mcp_off(farm):
    base, ui = farm
    _, tok = connect(base, name="gil-laptop")
    m = manage(base, "/api/manager")
    assert m["settings"]["mcp"] is True and [c["name"] for c in m["connections"]] == ["gil-laptop"]
    assert all("refresh" not in c for c in m["connections"]), "never a token"
    # off: every computer is refused, and none can connect
    assert manage(base, "/api/manager/settings", {"mcp": False})["settings"]["mcp"] is False
    assert manage(base, "/api/state")["mcp"] is False
    code, out, _ = call(base, tok["access_token"], "tools/list")
    assert code == 403 and "turned MCP off" in out["error"]
    assert req(base + "/oauth/register", {"client_name": "x", "redirect_uris": [REDIRECT]})[0] == 403
    assert req(base + "/.well-known/oauth-protected-resource")[0] == 404
    assert req(base + "/oauth/authorize?client_id=x")[0] == 403
    code, body, _ = req(base + "/oauth/token", {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                                "client_id": "x"}, form=True)
    assert code == 403
    # on again: the connection is back
    manage(base, "/api/manager/settings", {"mcp": True})
    assert call(base, tok["access_token"], "tools/list")[0] == 200
    # disconnect: its tokens stop working now
    gid = manage(base, "/api/manager")["connections"][0]["id"]
    assert manage(base, f"/api/manager/connections/{gid}/disconnect", {})["connections"] == []
    assert call(base, tok["access_token"], "tools/list")[0] == 401
    assert req(base + f"/api/manager/connections/{gid}/disconnect", {},
               {"Cookie": SIGNED_IN["cookie"], "X-Clodfarm": "1"})[0] == 404
    assert any(e["type"] == "mcp.disconnected" for e in ui.store.events(limit=20))
