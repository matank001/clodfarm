/* The TASKS page: every sub-agent at work or waiting, every schedule, and what finished in the last day, with what
 * you can do to each: cancel or retry a sub-agent, run, pause, resume or remove a schedule, add one, and pause the
 * whole farm. A sub-agent's title opens its instructions and its result. It refreshes every few seconds and only
 * redraws what changed, so an open row or a half-typed schedule stays as it is. */
const BASE = document.body.dataset.base || "";
const $ = s => document.querySelector(s);
const ZONE = (() => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch { return ""; } })();
let st = null, busy = false, who = "", shown = "";
const open = new Set();          // rows showing their details: "t:<id>" or "s:<id>"
const detail = new Map();        // a sub-agent's full record, by id (fetched when its row opens)
let armed = null;                // the destructive button waiting for its second click: "<action>:<id>"

function h(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "text") e.textContent = v;
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k != null && k !== false) e.append(k.nodeType ? k : document.createTextNode(String(k)));
  return e;
}

async function api(path, body) {
  const opts = { credentials: "same-origin", headers: { Accept: "application/json" } };
  if (body !== undefined) Object.assign(opts, { method: "POST", body: JSON.stringify(body),
    headers: { ...opts.headers, "Content-Type": "application/json", "X-Clodfarm": "1" } });
  const r = await fetch(`${BASE}/api/${path}`, opts);
  if (r.status === 401) { location.replace(`${BASE}/?next=tasks`); throw new Error("log in first"); }
  const out = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(out.error || r.statusText);
  return out;
}

// ------------------------------------------------------------------ time
const now = () => (st ? st.now + (Date.now() - st.fetched) / 1000 : Date.now() / 1000);
function span(sec) {
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.round(sec / 60)}m`;
  if (sec < 86400) { const hh = Math.floor(sec / 3600), mm = Math.round((sec % 3600) / 60); return mm ? `${hh}h ${mm}m` : `${hh}h`; }
  return `${Math.round(sec / 86400)}d`;
}
const ago = t => (t ? `${span(now() - t)} ago` : "");
const until = t => (t > 0 ? (t - now() < 60 ? "now" : `in ${span(t - now())}`) : "never again");
function clock(t) {
  if (!t) return "";
  const d = new Date(t * 1000), same = d.toDateString() === new Date().toDateString();
  return d.toLocaleString([], same ? { hour: "2-digit", minute: "2-digit" } : { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}
const upper = s => String(s || "").toUpperCase();

// ---------------------------------------------------------------- actions
async function act(path, body = {}, errors = $("#error")) {
  if (busy) return;
  busy = true; armed = null; errors.textContent = "";
  for (const b of document.querySelectorAll("main .btn, .top .btn")) b.disabled = true;
  try { const out = await api(path, body); if (out.active) take(out); return true; }
  catch (e) { errors.textContent = e.message; return false; }
  finally { busy = false; for (const b of document.querySelectorAll("main .btn, .top .btn")) b.disabled = false; render(true); }
}

function button(label, action, id, path, opts = {}) {
  // a destructive action asks once more on the button itself ("SURE?"), and forgets after a few seconds
  const key = `${action}:${id}`, sure = armed === key;
  return h("button", { type: "button", class: "btn small-btn" + (opts.danger ? " danger" : opts.primary ? " primary" : ""),
    title: opts.title, onclick: () => {
      if (opts.confirm && !sure) { armed = key; render(true); setTimeout(() => { if (armed === key) { armed = null; render(true); } }, 4000); return; }
      act(path);
    } }, sure ? "SURE?" : label);
}

function status(s, text) { return h("span", { class: `st ${s}` }, h("i"), text || upper(s)); }

function toggle(key, id) {
  if (open.has(key)) open.delete(key);
  else {
    open.add(key);
    if (key.startsWith("t:")) api(`tasks/${id}`).then(t => { detail.set(id, t); render(true); }).catch(e => { detail.set(id, { error: e.message }); render(true); });
  }
  render(true);
}

// ---------------------------------------------------------------- the rows
function onWhat(t) {
  if (t.status === "running") return t.on ? `${t.on}'s account` : "";
  return t.on ? `waits for ${t.on}` : "any Claude with budget";
}
function since(t) {
  if (t.status === "running") return ago(t.started);
  if (t.status === "waiting") return `for ${t.children_open || 0} sub-agent${t.children_open === 1 ? "" : "s"}`;
  return ago(t.created);
}
function titleCell(t, key) {
  const bits = [t.id, t.attempts ? `attempt ${t.attempts}/${t.max_attempts || 3}` : "", t.created_by?.startsWith("schedule:") ? "from a schedule" : "",
    t.parent ? `sub-agent of ${t.parent}` : "", t.depth ? `depth ${t.depth}` : ""].filter(Boolean).join(" · ");
  return h("td", { class: "title" },
    h("button", { type: "button", class: "open", "aria-expanded": String(open.has(key)), onclick: () => toggle(key, t.id) }, t.title),
    h("span", { class: "sub", text: bits }));
}
function taskDetails(t, cols) {
  const d = detail.get(t.id);
  const body = !d ? [h("p", { class: "muted", text: "Loading…" })] : d.error ? [h("p", { class: "form-error", text: d.error })] : [
    h("dl", {},
      h("dt", { text: "id" }), h("dd", { text: d.id }),
      d.branch ? [h("dt", { text: "branch" }), h("dd", { text: d.branch })] : null,
      d.created_by ? [h("dt", { text: "started by" }), h("dd", { text: d.created_by })] : null,
      d.children?.length ? [h("dt", { text: "its sub-agents" }), h("dd", { text: d.children.join(", ") })] : null,
      d.runs?.length ? [h("dt", { text: "runs" }), h("dd", { text: d.runs.map(r => `${(r.worker || "").split("@")[0]} ${r.ok ? "ok" : "not ok"} ${Math.round(r.duration_s || 0)}s`).join(" · ") })] : null),
    h("h3", { text: "ITS INSTRUCTIONS" }), h("pre", { text: d.prompt || "" }),
    d.result ? [h("h3", { text: t.status === "running" ? "ITS LAST RESULT" : "ITS RESULT" }), h("pre", { text: d.result })] : null,
    h("p", { class: "muted small", text: `In a shell: clodfarm result ${d.id}` })];
  return h("tr", { class: "details" }, h("td", { colspan: cols }, h("div", { class: "details" }, body)));
}

function activeRow(t) {
  const key = `t:${t.id}`;
  return [h("tr", {},
    h("td", {}, status(t.status)), titleCell(t, key),
    h("td", { class: "who", text: t.owner || "" }), h("td", { class: "who", text: onWhat(t) }),
    h("td", { class: "who", text: since(t) }),
    h("td", { class: "acts" }, button("CANCEL", "cancel", t.id, `tasks/${t.id}/cancel`, { danger: true, confirm: true,
      title: "Stop it (and its own sub-agents). Nothing it did lands." }))),
    open.has(key) ? taskDetails(t, 6) : null];
}

function finishedRow(t) {
  const key = `t:${t.id}`;
  return [h("tr", {},
    h("td", {}, status(t.status)), titleCell(t, key),
    h("td", { class: "who", text: t.owner || "" }), h("td", { class: "who", text: (t.worker || "").split("@")[0] }),
    h("td", { class: "who", text: ago(t.finished || t.updated) }),
    h("td", { class: "acts" }, button("RETRY", "retry", t.id, `tasks/${t.id}/retry`, { title: "Start it again from scratch" }))),
    open.has(key) ? taskDetails(t, 6) : null];
}

function scheduleRow(s) {
  const key = `s:${s.id}`;
  const next = s.paused ? "paused" : `${until(s.next_at)}${s.next_at > 0 ? ` · ${clock(s.next_at)}` : ""}`;
  return [h("tr", {},
    h("td", {}, status(s.paused ? "paused" : "on", s.paused ? "PAUSED" : "ON")),
    h("td", { class: "title" },
      h("button", { type: "button", class: "open", "aria-expanded": String(open.has(key)), onclick: () => toggle(key, s.id) }, s.title),
      h("span", { class: "sub", text: [s.id, `for ${s.owner || st.me}`, s.runs ? `ran ${s.runs}× · last ${ago(s.last_at)}` : "hasn't run yet"].join(" · ") })),
    h("td", { class: "when" }, s.when, h("span", { class: "sub", text: next })),
    h("td", { class: "who", text: s.to || "any" }),
    h("td", { class: "acts" },
      button("RUN NOW", "run", s.id, `schedules/${s.id}/run`, { title: "Start its sub-agent now, once; its next time stays" }),
      s.paused ? button("RESUME", "resume", s.id, `schedules/${s.id}/resume`, { primary: true, title: "Fire again, from its next time" })
        : button("PAUSE", "pause", s.id, `schedules/${s.id}/pause`, { title: "Stop it firing until you resume it" }),
      button("REMOVE", "remove", s.id, `schedules/${s.id}/remove`, { danger: true, confirm: true, title: "Delete this schedule" }))),
    open.has(key) ? h("tr", { class: "details" }, h("td", { colspan: 5 }, h("div", { class: "details" },
      h("dl", {}, h("dt", { text: "id" }), h("dd", { text: s.id }), h("dt", { text: "time zone" }), h("dd", { text: s.tz || "UTC" }),
        h("dt", { text: "added by" }), h("dd", { text: s.created_by || "" }), h("dt", { text: "added" }), h("dd", { text: clock(s.created) })),
      h("h3", { text: "WHAT ITS SUB-AGENT IS TOLD" }), h("pre", { text: s.prompt || "" }),
      h("p", { class: "muted small", text: `In a shell: clodfarm schedule pause|resume|run|remove ${s.id}` })))) : null];
}

// ------------------------------------------------------------------ render
function take(data) { st = { ...data, fetched: Date.now() }; }

function render(force) {
  if (!st) return;
  const mine = t => !who || t.owner === who || t.on === who || t.to === who;
  const active = st.active.filter(mine), finished = st.finished.filter(mine), schedules = st.schedules.filter(s => !who || s.owner === who || s.to === who);
  // redraw only when something shown changed (the minute-level times included), or after an action
  const key = JSON.stringify([active, finished, schedules, st.paused, who, [...open], armed, [...detail.keys()], Math.floor(now() / 30)]);
  if (!force && key === shown) return;
  shown = key;

  const running = st.active.filter(t => t.status === "running").length, waiting = st.active.length - running;
  $("#state").className = "chip " + (st.paused ? "wait" : running ? "" : "off");
  $("#state-text").textContent = `${running} AT WORK · ${waiting} WAITING · ${st.schedules.length} SCHEDULE${st.schedules.length === 1 ? "" : "S"}`;
  $("#pause").hidden = st.paused; $("#resume").hidden = !st.paused;
  $("#paused-note").hidden = !st.paused;
  $("#paused-note").textContent = `The farm is paused${st.pause_reason ? `: ${st.pause_reason}` : ""}. No new sub-agents start and schedules only queue theirs, until you resume.`;

  const whoSel = $("#who"), names = [...new Set([...st.claudes, ...st.active.map(t => t.owner)].filter(Boolean))].sort();
  if (whoSel.options.length - 1 !== names.length) {
    whoSel.replaceChildren(h("option", { value: "" }, "EVERY CLAUDE"), ...names.map(n => h("option", { value: n }, upper(n))));
    whoSel.value = who;
  }
  const onSel = $("#add-form [name=on]");
  if (onSel.options.length - 1 !== st.claudes.length) {
    const v = onSel.value;
    onSel.replaceChildren(h("option", { value: "" }, "any Claude with budget"), ...st.claudes.map(n => h("option", { value: n }, n)));
    onSel.value = v;
  }

  const order = { running: 0, waiting: 1, queued: 2 };
  $("#active").replaceChildren(...active.sort((a, b) => order[a.status] - order[b.status] || (a.created || 0) - (b.created || 0)).flatMap(activeRow).filter(Boolean));
  $("#active-empty").hidden = active.length > 0;
  $("#schedules").replaceChildren(...schedules.flatMap(scheduleRow).filter(Boolean));
  $("#sched-empty").hidden = schedules.length > 0;
  $("#finished").replaceChildren(...finished.flatMap(finishedRow).filter(Boolean));
  $("#done-empty").hidden = finished.length > 0;
}

async function refresh() {
  if (busy) return;
  try {
    take(await api("tasks"));
    for (const id of [...detail.keys()]) { // an open row's record is fetched again when its sub-agent moved on
      const t = [...st.active, ...st.finished].find(x => x.id === id);
      if (!open.has(`t:${id}`)) detail.delete(id);
      else if (t && detail.get(id).updated !== t.updated) api(`tasks/${id}`).then(d => { detail.set(id, d); render(true); }).catch(() => {});
    }
    render();
  } catch (e) { $("#state-text").textContent = "FARM UNREACHABLE"; }
}

// ------------------------------------------------------------ the new schedule
const HINTS = {
  cron: ["0 9 * * 1-5", "minute hour day month weekday: 0 9 * * 1-5 is weekdays at 9:00"],
  every: ["2h", "30m, 2h, 1d or 1w (at least a minute); the first run is one interval from now"],
  at: ["in 3h", "once: 2026-10-01T09:00 (in the time zone), or in 3h"],
};
function kind() { return new FormData($("#add-form")).get("kind"); }
function syncKind() {
  const [ph, hint] = HINTS[kind()], w = $("#add-form [name=when]");
  w.placeholder = ph; $("#when-hint").textContent = hint;
}
$("#add-open").addEventListener("click", () => {
  const f = $("#add-form");
  f.hidden = false; $("#add-open").hidden = true;
  if (!f.tz.value) f.tz.value = ZONE || st?.tz || "UTC";
  syncKind(); f.querySelector("[name=title]").focus();
});
$("#add-cancel").addEventListener("click", () => { $("#add-form").reset(); $("#add-form").hidden = true; $("#add-open").hidden = false; $("#add-error").textContent = ""; });
for (const r of document.querySelectorAll("#add-form [name=kind]")) r.addEventListener("change", syncKind);
$("#add-form").addEventListener("submit", async e => {
  e.preventDefault();
  const f = new FormData(e.target), body = { title: f.get("title"), prompt: f.get("prompt"), tz: f.get("tz"), on: f.get("on") };
  body[kind()] = f.get("when");
  if (await act("schedules", body, $("#add-error"))) { e.target.reset(); e.target.hidden = true; $("#add-open").hidden = false; }
});

// ------------------------------------------------------------------ the farm
$("#pause").addEventListener("click", () => act("pause", { reason: "paused from the TASKS page" }).then(refresh));
$("#resume").addEventListener("click", () => act("resume", {}).then(refresh));
$("#who").addEventListener("change", e => { who = e.target.value; render(true); });

refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 4000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
