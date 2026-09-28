"""The TASKS page and what it manages: every sub-agent and schedule, cancel and retry, run, pause, resume and remove."""
import json
import time
import urllib.request

from conftest import cli
from test_web import client, login, ui  # noqa: F401 - the ui fixture


def test_a_paused_schedule_doesnt_fire_and_resumes_from_its_next_time(store):
    sch = store.add_schedule("report", "write the report", every=3600)
    store.b.put({**store.b.get("SCHEDULE", sch["id"]), "next_at": time.time() - 10}, expect_ver=None)  # it's due
    assert store.pause_schedule(sch["id"], True, by="ui")["paused"]
    assert store.fire_due() == [], "paused: nothing starts"
    assert store.pause_schedule(sch["id"], True) is None, "already paused"
    back = store.pause_schedule(sch["id"], False, by="ui")
    assert "paused" not in back and back["next_at"] > time.time() + 3000, "resumed: its next time from now, not the missed one"
    assert store.fire_due() == []
    types = [e["type"] for e in store.events()]
    assert "schedule.paused" in types and "schedule.resumed" in types


def test_a_one_off_whose_time_went_by_while_paused_runs_on_resume(store):
    sch = store.add_schedule("once", "x", at=time.time() + 5)
    store.pause_schedule(sch["id"], True)
    store.b.put({**store.b.get("SCHEDULE", sch["id"]), "at": time.time() - 60}, expect_ver=None)
    store.pause_schedule(sch["id"], False)
    fired = store.fire_due()
    assert [t["title"] for t in fired] == ["once"] and not store.b.get("SCHEDULE", sch["id"]), "it ran once and is done"


def test_run_now_starts_its_sub_agent_and_keeps_its_next_time(store):
    sch = store.add_schedule("digest", "send the digest", cron="0 9 * * 1-5", tz="Europe/Berlin", to="gil", owner="gil")
    t = store.run_schedule(sch["id"], by="ui")
    assert t["title"] == "digest" and t["prompt"] == "send the digest" and t["to"] == "gil" and t["owner"] == "gil"
    assert t["created_by"] == f"schedule:{sch['id']}" and t["status"] == "queued"
    after = store.b.get("SCHEDULE", sch["id"])
    assert after["runs"] == 1 and after["next_at"] == sch["next_at"]
    assert store.run_schedule("snope") is None


def test_the_cli_pauses_resumes_and_runs_a_schedule(env, store):
    sid = store.add_schedule("nightly", "x", every=86400)["id"]
    assert "paused" in cli("schedule", "pause", sid).stdout
    assert "PAUSED" in cli("schedule", "list").stdout
    assert "resumed; next run" in cli("schedule", "resume", sid).stdout
    out = json.loads(cli("schedule", "run", sid, "--json").stdout)
    assert out["title"] == "nightly" and store.get_task(out["id"])["status"] == "queued"
    assert cli("schedule", "pause", "snope", check=False).returncode == 1


def test_the_tasks_page_lists_and_manages_everything(ui):  # noqa: F811
    base, farm_ui = ui
    store = farm_ui.store
    with urllib.request.urlopen(base + "/tasks") as r:
        assert r.status == 200 and b"tasks.js" in r.read()
    call = client()
    assert call(base + "/api/tasks")[0] == 401, "behind the farm password"
    login(call, base)
    busy = store.add_task("build the importer", "do it", owner="gil")
    store.claim_next("gil@box/w0", 300)
    waiting = store.add_task("review", "look", to="sahar")
    gone = store.add_task("old job", "x")
    store.cancel(gone["id"])
    sch = store.add_schedule("morning report", "report", cron="0 9 * * 1-5", tz="Asia/Jerusalem")

    code, v, _ = call(base + "/api/tasks")
    assert code == 200
    active = {t["id"]: t for t in v["active"]}
    assert active[busy["id"]]["status"] == "running" and active[busy["id"]]["on"] == "gil"
    assert active[waiting["id"]]["status"] == "queued" and active[waiting["id"]]["on"] == "sahar"
    assert [t["id"] for t in v["finished"]] == [gone["id"]]
    assert v["schedules"][0]["id"] == sch["id"] and v["schedules"][0]["when"] == "cron '0 9 * * 1-5' (Asia/Jerusalem)"
    assert "prompt" not in active[busy["id"]], "the list is light; one sub-agent's details come on their own"
    code, d, _ = call(base + f"/api/tasks/{busy['id']}")
    assert code == 200 and d["prompt"] == "do it" and d["on"] == "gil"
    assert call(base + "/api/tasks/nosuchtask99")[0] == 404

    # sub-agents: cancel, and retry what finished
    code, v, _ = call(base + f"/api/tasks/{waiting['id']}/cancel", {})
    assert code == 200 and store.get_task(waiting["id"])["status"] == "cancelled"
    assert waiting["id"] in [t["id"] for t in v["finished"]], "the answer is the page's new state"
    code, body, _ = call(base + f"/api/tasks/{waiting['id']}/cancel", {})
    assert code == 400 and "already finished" in body["error"]
    assert call(base + f"/api/tasks/{gone['id']}/retry", {})[0] == 200
    assert store.get_task(gone["id"])["status"] == "queued"

    # schedules: add, pause, resume, run now, remove
    code, body, _ = call(base + "/api/schedules", {"title": "x", "cron": "0 9 * * *", "every": "2h"})
    assert code == 400 and "exactly one" in body["error"]
    code, body, _ = call(base + "/api/schedules", {"title": "x", "cron": "0 9 * * *", "tz": "Mars/Olympus"})
    assert code == 400 and "bad schedule" in body["error"]
    code, body, _ = call(base + "/api/schedules", {"title": "x", "every": "10s"})
    assert code == 400 and "once a minute" in body["error"]
    code, v, _ = call(base + "/api/schedules", {"title": "weekly review", "prompt": "review the week", "every": "1w",
                                                "tz": "Europe/Berlin", "on": "gil"})
    assert code == 200
    new = next(s for s in v["schedules"] if s["title"] == "weekly review")
    assert new["to"] == "gil" and new["owner"] == "gil" and new["created_by"] == "ui" and new["when"] == "every 1w"
    code, v, _ = call(base + f"/api/schedules/{new['id']}/pause", {})
    assert code == 200 and next(s for s in v["schedules"] if s["id"] == new["id"])["paused"]
    assert store.b.get("SCHEDULE", new["id"])["paused"]
    assert call(base + f"/api/schedules/{new['id']}/resume", {})[0] == 200
    assert "paused" not in store.b.get("SCHEDULE", new["id"])
    code, v, _ = call(base + f"/api/schedules/{new['id']}/run", {})
    assert code == 200 and any(t["title"] == "weekly review" and t["on"] == "gil" for t in v["active"])
    assert call(base + f"/api/schedules/{new['id']}/remove", {})[0] == 200 and not store.b.get("SCHEDULE", new["id"])
    assert call(base + "/api/schedules/snope123/run", {})[0] == 404
    by_ui = [e for e in store.events() if e["type"] in ("schedule.paused", "schedule.resumed", "schedule.run")]
    assert len(by_ui) == 3 and all(e["by"] == "ui" for e in by_ui)
