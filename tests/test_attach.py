"""`clodfarm attach`: a computer's own Claude Code sessions on the farm. The farm's /mcp/hook, and the hook on the
computer: which sessions are connected (folders, FARM=, /farm), what is sent (scrubbed), mail, tokens and sign-in."""
import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from argparse import Namespace

import pytest

from clodfarm import attach
from test_mcp import SIGNED_IN, connect, req  # noqa: F401 - the MCP farm fixture
from test_mcp import farm  # noqa: F401

SID = "0c6f2a8e-1111-4222-8333-444455556666"


@pytest.fixture
def laptop(farm, tmp_path, monkeypatch):  # noqa: F811
    """A computer attached to the farm (its connection made the way `clodfarm attach` makes it, minus the browser)."""
    base, ui = farm
    monkeypatch.setenv("CLODFARM_HOME", str(tmp_path / "laptop"))
    monkeypatch.delenv("FARM", raising=False)
    cid, tok = connect(base)
    conf = {"url": base, "client_id": cid, "name": tok["name"], "scope": tok["scope"], "access": tok["access_token"],
            "access_exp": time.time() + 3000, "refresh": tok["refresh_token"], "folders": []}
    attach.save_conf(conf)
    repo = tmp_path / "repo"
    repo.mkdir()
    return base, ui, repo


class Transcript:
    def __init__(self, path):
        self.path = str(path)
        open(self.path, "w").close()

    def say(self, role, text):
        msg = {"role": role, "content": text if role == "user" else [{"type": "text", "text": text}]}
        if role == "assistant":
            msg["id"] = f"m{time.time_ns()}"
        with open(self.path, "a") as f:
            f.write(json.dumps({"type": role, "message": msg, "timestamp": "2026-10-07T10:00:00Z"}) + "\n")


def ev(name, t, cwd, sid=SID, **kw):
    return {"session_id": sid, "hook_event_name": name, "transcript_path": t.path, "cwd": str(cwd), **kw}


def texts(ui, sid=SID):
    return [t["text"] for t in ui.store.turns(sid)]


def hook_post(base, token, body):
    code, out, _ = req(base + "/mcp/hook", body, {"Authorization": "Bearer " + token})
    return code, json.loads(out) if out else {}


# ------------------------------------------------------------------ the farm's side
def test_the_farm_keeps_a_guest_session_and_hands_it_mail(farm):  # noqa: F811
    base, ui = farm
    _, tok = connect(base)
    turns = [{"role": "user", "kind": "text", "text": "add csv export"}, {"role": "assistant", "kind": "text", "text": "ok"}]
    code, out = hook_post(base, tok["access_token"], {"session": SID, "event": "SessionStart", "turns": turns,
                                                      "cwd": "/Users/x/repo"})
    assert code == 200 and out["name"] == "matan-laptop" and out["mail"] == ""
    s = ui.store.session(SID)
    # shown under the Claude whose person connected the computer, and counted for nobody's budget
    assert s["kind"] == "guest" and s["runs_on"] == "matan-laptop" and s["claude"] == ui.cfg.name
    assert s["title"] == "add csv export" and texts(ui) == ["add csv export", "ok"]
    assert any(e["type"] == "session.guest" for e in ui.store.events(None, 20))
    ui.store.send_message(ui.cfg.name, "matan-laptop", "the release is out")
    code, out = hook_post(base, tok["access_token"], {"session": SID, "event": "UserPromptSubmit", "mail": True})
    assert "the release is out" in out["mail"] and ui.store.session(SID)["busy"] is True
    code, out = hook_post(base, tok["access_token"], {"session": SID, "event": "SessionEnd", "mail": True})
    assert out["mail"] == "" and ui.store.session(SID)["ended"] is True


def test_the_farm_refuses_what_isnt_the_computers(farm):  # noqa: F811
    base, ui = farm
    _, tok = connect(base)
    _, other = connect(base, name="gil-laptop")
    ui.store.record_session("farm-session-1", claude=ui.cfg.name, kind="conversation")
    assert hook_post(base, tok["access_token"], {"session": "farm-session-1", "event": "Stop"})[0] == 403
    assert hook_post(base, tok["access_token"], {"session": SID, "event": "Stop"})[0] == 200
    assert hook_post(base, other["access_token"], {"session": SID, "event": "Stop"})[0] == 403  # another computer's
    assert hook_post(base, tok["access_token"], {"session": "x/../y", "event": "Stop"})[0] == 400
    assert hook_post(base, tok["access_token"], {"session": SID, "event": "Stop",
                                                 "turns": [{"role": "system", "kind": "text", "text": "x"}]})[0] == 400
    assert req(base + "/mcp/hook", {"session": SID, "event": "Stop"})[0] == 401
    _, ro = connect(base, name="ro-laptop", access="read")  # a computer that may only look still shows its own
    assert hook_post(base, ro["access_token"], {"session": "1c6f2a8e-1111", "event": "Stop"})[0] == 200


# -------------------------------------------------------------- the computer's side
def test_only_the_attached_folders_sessions_go_and_secrets_stay(laptop, tmp_path):
    base, ui, repo = laptop
    t = Transcript(tmp_path / "s.jsonl")
    t.say("user", "deploy with sk-ant-abcdefghijklmnopqrstuvwx please")
    t.say("assistant", "deploying")
    assert attach.hook(ev("SessionStart", t, repo), env={}) is None  # not attached yet: nothing sent
    assert ui.store.session(SID) is None
    conf = attach.load_conf()
    conf["folders"] = [os.path.realpath(repo)]
    attach.save_conf(conf)
    other = "1c6f2a8e-2222-4222-8333-444455556666"  # started elsewhere: it stays off, even once it works in the folder
    attach.hook(ev("SessionStart", t, tmp_path, sid=other), env={})
    sub = repo / "pkg"
    sub.mkdir()
    out = attach.hook(ev("SessionStart", t, sub), env={})  # a session started inside an attached folder
    assert out.startswith("[farm] This session is connected") and "matan-laptop" in out
    assert texts(ui) == ["deploy with [an API key] please", "deploying"]
    assert attach.hook(ev("Stop", t, repo, sid=other), env={}) is None and ui.store.session(other) is None
    t.say("user", "and the docs")
    assert attach.hook(ev("UserPromptSubmit", t, repo, prompt="and the docs"), env={}) is None
    assert texts(ui)[-1] == "and the docs" and ui.store.session(SID)["busy"] is True
    attach.hook(ev("SessionEnd", t, repo, reason="logout"), env={})
    s = ui.store.session(SID)
    assert s["ended"] is True and s["end_reason"] == "logout"


def test_farm_env_decides_before_the_folders(laptop, tmp_path):
    base, ui, repo = laptop
    t = Transcript(tmp_path / "s.jsonl")
    t.say("user", "hello")
    conf = attach.load_conf()
    conf["folders"] = [os.path.realpath(repo)]
    attach.save_conf(conf)
    assert attach.hook(ev("SessionStart", t, repo), env={"FARM": "0"}) is None
    assert ui.store.session(SID) is None
    other = "2c6f2a8e-1111-4222-8333-444455556666"
    assert attach.hook(ev("SessionStart", t, tmp_path, sid=other), env={"FARM": "1"})
    assert texts(ui, other) == ["hello"]


def test_slash_farm_turns_one_session_on_and_off(laptop, tmp_path):
    base, ui, repo = laptop
    t = Transcript(tmp_path / "s.jsonl")
    t.say("user", "first")
    t.say("assistant", "one")
    farm_cmd = lambda arg="": json.loads(attach.hook(ev("UserPromptSubmit", t, repo, prompt=f"/farm {arg}".strip()), env={}))  # noqa: E731
    assert attach.hook(ev("SessionStart", t, repo), env={}) is None
    st = farm_cmd("status")
    assert st["decision"] == "block" and "not on the farm" in st["reason"]
    on = farm_cmd()
    assert on["decision"] == "block" and "on the farm" in on["reason"]
    assert texts(ui) == ["first", "one"]  # the conversation so far goes with it
    t.say("user", "second")
    off = farm_cmd("off")
    assert "off the farm" in off["reason"] and ui.store.session(SID)["ended"] is True
    assert texts(ui) == ["first", "one", "second"]  # said while it was on
    t.say("user", "private")
    assert attach.hook(ev("Stop", t, repo), env={}) is None
    farm_cmd("on")
    t.say("user", "third")
    attach.hook(ev("Stop", t, repo), env={})
    assert texts(ui) == ["first", "one", "second", "third"] and not ui.store.session(SID)["ended"]
    assert attach.hook(ev("UserPromptSubmit", t, repo, prompt="/farmer"), env={}) is None  # not ours


def test_mail_arrives_at_a_prompt_and_keeps_a_turn_going(laptop, tmp_path):
    base, ui, repo = laptop
    t = Transcript(tmp_path / "s.jsonl")
    env = {"FARM": "1"}
    attach.hook(ev("SessionStart", t, repo), env=env)
    ui.store.send_message(ui.cfg.name, "matan-laptop", "rebase on main first")
    out = attach.hook(ev("UserPromptSubmit", t, repo, prompt="go"), env=env)
    assert "rebase on main first" in out
    for n in range(attach.MAIL_STOP_BLOCKS + 1):
        ui.store.send_message(ui.cfg.name, "matan-laptop", f"more {n}")
        out = attach.hook(ev("Stop", t, repo, stop_hook_active=n > 0), env=env)
        if n < attach.MAIL_STOP_BLOCKS:
            d = json.loads(out)
            assert d["decision"] == "block" and f"more {n}" in d["reason"]
        else:
            assert out is None  # never more than MAIL_STOP_BLOCKS in a row: the message waits for the next prompt


def test_tokens_refresh_once_and_a_disconnect_stops_the_hook(laptop, tmp_path):
    base, ui, repo = laptop
    t = Transcript(tmp_path / "s.jsonl")
    t.say("user", "hi")
    conf = attach.load_conf()
    old = conf["refresh"]
    conf["access_exp"] = 0
    attach.save_conf(conf)
    assert attach.hook(ev("SessionStart", t, repo), env={"FARM": "1"})
    conf = attach.load_conf()
    assert conf["refresh"] != old and conf["access_exp"] > time.time()
    # the farm's manager disconnects the computer: the hook goes quiet, and /farm says what to do
    ui.oauth.disconnect(ui.oauth.connections()[-1]["id"])
    t.say("user", "after")
    assert attach.hook(ev("Stop", t, repo), env={"FARM": "1"}) is None
    assert attach.load_conf()["ended"] and texts(ui) == ["hi"]
    assert "ended this computer's connection" in json.loads(
        attach.hook(ev("UserPromptSubmit", t, repo, prompt="/farm"), env={}))["reason"]


def test_a_farm_that_cant_be_reached_costs_nothing(laptop, tmp_path):
    base, ui, repo = laptop
    t = Transcript(tmp_path / "s.jsonl")
    t.say("user", "offline work")
    conf = attach.load_conf()
    attach.save_conf({**conf, "url": "http://127.0.0.1:9"})
    assert attach.hook(ev("SessionStart", t, repo), env={"FARM": "1"}) is None
    attach.save_conf(conf)  # back: what waited on disk goes with the next report
    t.say("assistant", "done")
    attach.hook(ev("Stop", t, repo), env={"FARM": "1"})
    assert texts(ui) == ["offline work", "done"]


def test_the_installed_hook_runs(laptop, tmp_path):
    base, ui, repo = laptop
    t = Transcript(tmp_path / "s.jsonl")
    t.say("user", "via the real hook")
    p = subprocess.run(attach.hook_command(), shell=True, input=json.dumps(ev("SessionStart", t, repo)),
                       capture_output=True, text=True, env={**os.environ, "FARM": "1"}, timeout=60)
    assert p.returncode == 0 and p.stdout.startswith("[farm]"), p.stderr
    assert texts(ui) == ["via the real hook"]
    p = subprocess.run(attach.hook_command(), shell=True, input="not json", capture_output=True, text=True, timeout=60)
    assert p.returncode == 0 and "clodfarm attach:" in p.stderr  # never fails the session


# ----------------------------------------------------------- sign-in and install
def test_sign_in_through_the_farms_page(farm, tmp_path, monkeypatch):  # noqa: F811
    base, ui = farm
    monkeypatch.setenv("CLODFARM_HOME", str(tmp_path / "laptop"))
    seen = {}

    def browser(url):  # the person, signed in to their Claude on the farm, allows it
        def go():
            ck = {"Cookie": SIGNED_IN["cookie"]}
            code, page, _ = req(url, headers=ck)
            seen["name"] = re.search(r'id="name" name="name" type="text" value="([^"]*)"', page).group(1)
            fields = {k: v.replace("&amp;", "&") for k, v in
                      re.findall(r'<input type="hidden" name="([a-z_]+)" value="([^"]*)"', page)}
            fields.update(decision="allow", name=seen["name"], access="work")
            code, _, hdrs = req(base + "/oauth/authorize", fields, form=True, headers=ck)
            urllib.request.urlopen(hdrs["Location"], timeout=10).read()
        threading.Thread(target=go, daemon=True).start()

    conf = attach.sign_in(base + "/mcp", "sahar-laptop", open_browser=browser, wait=30)
    assert seen["name"] == "sahar-laptop" and conf["name"] == "sahar-laptop" and conf["url"] == base
    grant = ui.oauth.check(conf["access"])
    assert grant["owner"] == ui.cfg.name and grant["scope"] == "farm:read farm:work"
    with pytest.raises(attach.AttachError):
        attach.farm_url("http://farm.example.com")  # plain http only to this computer


def test_install_keeps_other_hooks_and_detach_removes_ours(laptop, tmp_path):
    base, ui, repo = laptop
    home = os.environ["CLAUDE_CONFIG_DIR"]
    os.makedirs(home, exist_ok=True)
    mine = {"hooks": [{"type": "command", "command": "echo mine"}]}
    with open(os.path.join(home, "settings.json"), "w") as f:
        json.dump({"model": "opus", "hooks": {"Stop": [mine]}}, f)
    a = Namespace(url=None, only=[str(repo)], all=False, manual=False, name=None, status=False, json=False)
    assert attach.cmd_attach(a) == 0 and attach.cmd_attach(a) == 0  # twice: still one of each
    cfg = json.load(open(os.path.join(home, "settings.json")))
    assert cfg["model"] == "opus" and cfg["hooks"]["Stop"][0] == mine
    for e in attach.HOOK_EVENTS:
        assert sum(attach._ours(g) for g in cfg["hooks"][e]) == 1
    assert attach.installed() and os.path.exists(os.path.join(home, "commands", "farm.md"))
    assert attach.load_conf()["folders"] == [os.path.realpath(repo)]
    assert attach.cmd_detach(Namespace(only=[str(repo)], all=False)) == 0
    assert attach.load_conf()["folders"] == []
    assert attach.cmd_detach(Namespace(only=None, all=True)) == 0
    cfg = json.load(open(os.path.join(home, "settings.json")))
    assert cfg["hooks"] == {"Stop": [mine]} and not os.path.exists(os.path.join(home, "commands", "farm.md"))
    assert attach.load_conf() == {} and ui.oauth.connections() == []  # the farm ended the connection too
