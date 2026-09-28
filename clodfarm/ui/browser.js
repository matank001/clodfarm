/* The farm's browser: one tab per profile (each its own Chromium with its own logins), and the selected profile's
 * screen over the farm's own WebSocket (/api/browser/screen?profile=..., behind the farm password), drawn by noVNC.
 * START and STOP turn a profile on and off for everyone on the farm; it stays on until stopped. CONNECTION sends a
 * profile DIRECT or VIA PROXY (the farm's one proxy, from a country the profile picks), checked before it's used.
 * Paste goes through the page's paste event (no clipboard permission needed) and what you copy there comes back
 * to your clipboard. On a Mac, ⌘ shortcuts (⌘A, ⌘C, ⌘L, ...) become Ctrl in the farm's Linux Chromium. */
// noVNC is loaded when a screen is first shown, not with the page: if one of its ~55 modules can't load, the profiles,
// START and CONNECTION still work, and the screen says what happened
let RFB = null, rfbLoad = null, rfbFailed = false;
const loadRFB = () => (rfbLoad = rfbLoad || import("./browser/novnc/core/rfb.js").then(m => (RFB = m.default),
  e => { rfbFailed = true; throw e; }));
const NO_SCREEN = "THE SCREEN DIDN'T LOAD (THE FARM WAS BUSY). RELOAD THE PAGE.";

const BASE = document.body.dataset.base || "";
const $ = s => document.querySelector(s);
const MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
const XK_CONTROL = 0xffe3;
const CMD_KEYS = new Set(["a", "c", "x", "z", "y", "f", "l", "r"]); // ⌘ + these → Ctrl + these in the farm's browser
let rfb = null, rfbFor = null, st = null, busy = false, retry = null, checking = false;
// the countries a DataImpulse address can come from (ISO 3166 codes, named in your language); the common ones first
const COMMON = ["us", "gb", "de", "fr", "ca", "au", "nl", "es", "it", "in", "br", "il"];
const ALL = ("ad ae af ag ai al am ao ar as at au aw ax az ba bb bd be bf bg bh bi bj bl bm bn bo bq br bs bt bw by bz " +
  "ca cc cd cf cg ch ci ck cl cm cn co cr cu cv cw cx cy cz de dj dk dm do dz ec ee eg eh er es et fi fj fk fm fo fr " +
  "ga gb gd ge gf gg gh gi gl gm gn gp gq gr gt gu gw gy hk hn hr ht hu id ie il im in io iq ir is it je jm jo jp ke " +
  "kg kh ki km kn kp kr kw ky kz la lb lc li lk lr ls lt lu lv ly ma mc md me mf mg mh mk ml mm mn mo mp mq mr ms mt " +
  "mu mv mw mx my mz na nc ne nf ng ni nl no np nr nu nz om pa pe pf pg ph pk pl pm pn pr ps pt pw py qa re ro rs ru " +
  "rw sa sb sc sd se sg sh si sj sk sl sm sn so sr ss st sv sx sy sz tc td tg th tj tk tl tm tn to tr tt tv tw tz ua " +
  "ug us uy uz va vc ve vg vi vn vu wf ws ye yt za zm zw").split(" ");
const REGION = (() => { try { return new Intl.DisplayNames([navigator.language || "en", "en"], { type: "region" }); } catch { return null; } })();
const countryName = cc => { try { return (REGION && REGION.of(cc.toUpperCase())) || cc.toUpperCase(); } catch { return cc.toUpperCase(); } };
let sel = new URLSearchParams(location.search).get("profile") || stored() || "default";

function stored() { try { return localStorage.getItem("clodfarm.browser.profile"); } catch { return null; } }
function remember(name) { try { localStorage.setItem("clodfarm.browser.profile", name); } catch { /* private mode */ } }

async function api(path, body) {
  const opts = { credentials: "same-origin", headers: { Accept: "application/json" } };
  if (body !== undefined) Object.assign(opts, { method: "POST", body: JSON.stringify(body),
    headers: { ...opts.headers, "Content-Type": "application/json", "X-Clodfarm": "1" } });
  const r = await fetch(`${BASE}/api/${path}`, opts);
  if (r.status === 401) {
    location.replace(`${BASE}/?next=browser`);
    throw new Error("log in first");
  }
  const out = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(out.error || r.statusText);
  return out;
}

function toast(text) {
  const t = $("#toast");
  t.textContent = text; t.hidden = false;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => (t.hidden = true), 1800);
}

function message(text) {
  $("#screen-msg").textContent = text || "";
  $("#screen-msg").hidden = !text;
}

const current = () => (st && st.profiles.find(p => p.name === sel)) || null;

// ---------------------------------------------------------------- the screen
function connect(name) {
  if (rfb && rfbFor === name) return;
  if (!RFB) { // the screen's code first (once); a module that failed to load stays failed until a reload
    if (rfbFailed) return message(NO_SCREEN);
    message("LOADING THE SCREEN…");
    loadRFB().then(() => { if (sel === name && current()?.ready) connect(name); }, () => message(NO_SCREEN));
    return;
  }
  disconnect();
  message("CONNECTING…");
  const url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${BASE}/api/browser/screen?profile=${encodeURIComponent(name)}`;
  const r = rfb = new RFB($("#screen"), url, { shared: true, wsProtocols: ["binary"] });
  rfbFor = name;
  r.scaleViewport = true;   // the whole window fits the frame
  r.resizeSession = false;  // the screen keeps its size: the Claudes see the same page you do
  r.focusOnClick = true;
  r.background = "#1b1f2a";
  r.addEventListener("connect", () => { message(""); r.focus(); });
  r.addEventListener("disconnect", () => {
    if (rfb !== r) return; // replaced by another profile's screen
    rfb = rfbFor = null;
    const p = current();
    if (p && p.on) { message("RECONNECTING…"); clearTimeout(retry); retry = setTimeout(refresh, 1500); }
  });
  r.addEventListener("clipboard", e => { // copied in the farm's browser: onto your clipboard
    const text = e.detail.text || "";
    if (text && navigator.clipboard) navigator.clipboard.writeText(text).then(() => toast("COPIED"), () => {});
  });
}

function disconnect() {
  clearTimeout(retry);
  if (rfb) { const r = rfb; rfb = rfbFor = null; r.disconnect(); }
  $("#screen").replaceChildren();
}

function ctrl(key) { // Ctrl + a letter in the farm's browser
  const ks = key.toLowerCase().charCodeAt(0);
  rfb.sendKey(XK_CONTROL, "ControlLeft", true);
  rfb.sendKey(ks, "Key" + key.toUpperCase(), true);
  rfb.sendKey(ks, "Key" + key.toUpperCase(), false);
  rfb.sendKey(XK_CONTROL, "ControlLeft", false);
}

// Before noVNC sees the key (capture phase): ⌘/Ctrl+V is left to the page, so a paste event brings your clipboard;
// on a Mac, ⌘ + a few letters becomes Ctrl, the way the farm's Linux Chromium expects.
$("#screen").addEventListener("keydown", e => {
  if (!rfb) return;
  const k = (e.key || "").toLowerCase(), mod = MAC ? e.metaKey : e.ctrlKey;
  if (mod && k === "v" && !e.altKey) { e.stopPropagation(); return; }
  if (MAC && e.metaKey && !e.ctrlKey && !e.altKey && CMD_KEYS.has(k)) {
    e.preventDefault(); e.stopPropagation();
    ctrl(k);
  }
}, true);
$("#screen").addEventListener("keyup", e => {
  if (rfb && (MAC ? e.metaKey : e.ctrlKey) && (e.key || "").toLowerCase() === "v") e.stopPropagation();
}, true);
document.addEventListener("paste", e => {
  if (!rfb || !$("#screen").contains(document.activeElement)) return;
  const text = e.clipboardData && e.clipboardData.getData("text/plain");
  if (!text) return;
  e.preventDefault();
  rfb.clipboardPasteFrom(text);
  setTimeout(() => rfb && ctrl("v"), 60); // the text is on the farm's clipboard first, then Ctrl+V pastes it
});

// ------------------------------------------------------------------ state
function render() {
  if (!st) return;
  const avail = st.available;
  if (!st.profiles.some(p => p.name === sel)) sel = "default";
  const p = current(), on = !!(p && p.on), ready = !!(p && p.ready);
  const up = st.profiles.filter(x => x.ready).length;
  $("#state").className = "chip " + (!avail ? "off" : up ? "" : st.profiles.some(x => x.on) ? "wait" : "off");
  $("#state-text").textContent = !avail ? "NOT IN THIS IMAGE" : `${up} OF ${st.profiles.length} ON · ${st.size}`;

  $("#profiles").hidden = !avail;
  renderProfiles();
  $("#add-open").hidden = st.profiles.length >= st.max || !$("#add-form").hidden;

  $("#off").hidden = on && avail;
  $("#on").hidden = !(on && avail);
  $("#start").hidden = !avail;
  $("#start").textContent = `▶ START ${sel === "default" ? "THE BROWSER" : sel.toUpperCase()}`;
  if (!avail) {
    $("#off-h").textContent = "NO BROWSER IN THIS IMAGE";
    $("#off-text").textContent = `This farm's image doesn't have the browser (missing: ${st.missing.join(", ")}). ` +
      "The clodfarm image has it unless it was built with BROWSER=0.";
  } else {
    $("#off-h").textContent = sel === "default" ? "THE FARM'S BROWSER" : `PROFILE ${sel.toUpperCase()}`;
  }
  $("#off-error").textContent = p && p.error && !on ? p.error : "";
  $("#profile-actions").hidden = !avail || !p || (!on && sel === "default");
  $("#stop").hidden = !on;
  $("#remove").hidden = sel === "default" || !$("#confirm").hidden;
  $("#conn").hidden = !avail || !p;
  if (avail && p) renderConnection(p);
  if (on && avail) {
    $("#tools-note").textContent = `the Claudes use it with ${p.tools}`;
    if (ready) connect(sel);
    else { disconnect(); message(p.error ? `NOT STARTED: ${p.error}` : "STARTING THE BROWSER…"); }
    renderTabs(p.tabs || []);
  } else disconnect();
}

function renderConnection(p) { // DIRECT or VIA PROXY for this profile, from which country, and what sites see
  const px = st.proxy || { set: false }, form = !$("#proxy-form").hidden, pon = !!p.proxy;
  $("#conn-direct").setAttribute("aria-pressed", String(!pon));
  $("#conn-proxy").setAttribute("aria-pressed", String(pon));
  const pick = $("#conn-country");
  pick.hidden = !(pon && px.countries) || form;
  if (!pick.hidden) {
    if (!pick.options.length) fillCountries(pick);
    if (document.activeElement !== pick) pick.value = p.country || "";
  }
  $("#proxy-set").hidden = form || !!px.from_env;
  $("#proxy-set").textContent = px.set ? "CHANGE PROXY" : "SET PROXY";
  $("#proxy-forget").hidden = !px.set;
  const state = $("#conn-state"), seen = p.proxy_seen;
  let bad = p.proxy_error || (pon && !px.set ? "No proxy is set: set one, or go DIRECT." : "");
  if (!pon && !px.set && p.error && /PROXY/.test(p.error)) bad = p.error;
  state.hidden = form;
  state.className = "conn-state" + (bad ? " bad" : "");
  state.replaceChildren();
  if (checking) state.textContent = "Checking the proxy…";
  else if (bad) state.textContent = bad;
  else if (!pon) state.textContent = px.set ? "Sites see this box's own IP address." :
    "Sites see this box's own IP address. SET PROXY to show them another one.";
  else if (p.on && !p.proxied) state.textContent = "Switching to the proxy: the browser restarts…";
  else {
    const b = document.createElement("b");
    b.textContent = p.country ? countryName(p.country) : "any country";
    const where = seen ? ` · checked: ${seen.ip}${seen.city ? ", " + seen.city : ""}${seen.country ? " (" + seen.country.toUpperCase() + ")" : ""}` : "";
    state.append("Sites see a proxy IP in ", b, `${where} · ${px.server}`);
  }
  if (!pon && p.on && p.proxied) state.textContent = "Switching to direct: the browser restarts…";
}

function fillCountries(pick) {
  const opt = (cc, name) => { const o = document.createElement("option"); o.value = cc; o.textContent = name; return o; };
  const group = (label, codes) => {
    const g = document.createElement("optgroup"); g.label = label;
    g.append(...codes.map(cc => opt(cc, countryName(cc))).sort((x, y) => x.textContent.localeCompare(y.textContent)));
    return g;
  };
  pick.append(opt("", "Any country"), group("Common", COMMON), group("All countries", ALL));
}

function closeProxyForm() {
  $("#proxy-form").hidden = true; $("#proxy-address").value = ""; $("#proxy-error").textContent = "";
}

function renderProfiles() {
  const box = $("#profile-tabs");
  const key = JSON.stringify([sel, st.profiles.map(p => [p.name, p.on, p.ready, p.proxy, p.country])]);
  if (box.dataset.key === key) return;
  box.dataset.key = key;
  box.replaceChildren(...st.profiles.map(p => {
    const b = document.createElement("button"), dot = document.createElement("span");
    b.type = "button"; b.setAttribute("role", "tab");
    b.className = "ptab " + (p.ready ? "up" : p.on ? "wait" : "");
    b.setAttribute("aria-selected", String(p.name === sel));
    b.title = `${p.name}: ${p.ready ? "on" : p.on ? "starting" : "off"}` +
      `${p.proxy ? ", through the proxy" + (p.country ? " from " + countryName(p.country) : "") : ", direct"}; ` +
      `the Claudes use it with ${p.tools}`;
    dot.className = "dot"; dot.setAttribute("aria-hidden", "true");
    b.append(dot, p.name.toUpperCase());
    b.addEventListener("click", () => select(p.name));
    return b;
  }));
}

function renderTabs(tabs) {
  const list = $("#tabs");
  const key = JSON.stringify(tabs.map(t => [t.title, t.url]));
  if (list.dataset.key === key) return;
  list.dataset.key = key;
  list.replaceChildren(...(tabs.length ? tabs : [{ title: "No tabs open", url: "" }]).map(t => {
    const li = document.createElement("li"), b = document.createElement("b"), s = document.createElement("span");
    b.textContent = t.title || "(untitled)"; s.textContent = t.url;
    li.append(b, s);
    return li;
  }));
}

function select(name) {
  if (name === sel) return;
  sel = name; remember(name);
  history.replaceState(null, "", `?profile=${encodeURIComponent(name)}`);
  $("#confirm").hidden = true; $("#bar-error").textContent = "";
  render();
}

async function refresh() {
  try { st = await api("browser"); render(); }
  catch (e) { $("#state-text").textContent = "FARM UNREACHABLE"; }
}

async function act(path, body, after, errors = $("#bar-error")) {
  if (busy) return;
  busy = true;
  for (const b of document.querySelectorAll("main .btn")) b.disabled = true;
  errors.textContent = "";
  try { st = await api(path, body); if (after) after(); render(); }
  catch (e) { errors.textContent = e.message; }
  finally { busy = false; for (const b of document.querySelectorAll("main .btn")) b.disabled = false; }
}

$("#start").addEventListener("click", () => act("browser/start", { profile: sel }));
$("#stop").addEventListener("click", () => act("browser/stop", { profile: sel }));
$("#remove").addEventListener("click", () => { $("#confirm").hidden = false; render(); });
$("#remove-no").addEventListener("click", () => { $("#confirm").hidden = true; render(); });
$("#remove-yes").addEventListener("click", () => {
  const gone = sel;
  $("#confirm").hidden = true;
  act("browser/remove", { profile: gone }, () => select("default"));
});
async function proxyAct(path, body, after) { // a proxy check takes a few seconds: say so meanwhile
  const controls = document.querySelectorAll("#conn .seg, #conn-country");
  checking = true; render();
  for (const c of controls) c.disabled = true;
  try { await act(path, body, after, $("#proxy-error")); }
  finally { checking = false; for (const c of controls) c.disabled = false; render(); }
}
function openProxyForm() { $("#proxy-form").hidden = false; render(); $("#proxy-address").focus(); }
$("#conn-direct").addEventListener("click", () => {
  const p = current();
  if (p && p.proxy) proxyAct("browser/proxy", { profile: sel, on: false });
});
$("#conn-proxy").addEventListener("click", () => {
  const p = current();
  if (!p || p.proxy) return;
  if (!(st.proxy && st.proxy.set)) return openProxyForm(); // no proxy yet: its address first
  proxyAct("browser/proxy", { profile: sel, on: true });
});
$("#conn-country").addEventListener("change", e => {
  e.target.blur(); // so a failed check puts back the country it had
  proxyAct("browser/proxy", { profile: sel, on: true, country: e.target.value });
});
$("#proxy-set").addEventListener("click", openProxyForm);
$("#proxy-cancel").addEventListener("click", () => { closeProxyForm(); render(); });
$("#proxy-forget").addEventListener("click", () => proxyAct("browser/proxy/address", { address: "" }, closeProxyForm));
$("#proxy-form").addEventListener("submit", e => {
  e.preventDefault();
  const address = $("#proxy-address").value.trim();
  if (address && !busy) proxyAct("browser/proxy/address", { address, profile: sel }, closeProxyForm); // and it's on
});
$("#add-open").addEventListener("click", () => { $("#add-form").hidden = false; render(); $("#add-name").focus(); });
$("#add-cancel").addEventListener("click", () => { $("#add-form").hidden = true; $("#add-name").value = ""; render(); });
$("#add-form").addEventListener("submit", e => {
  e.preventDefault();
  const name = $("#add-name").value.trim().toLowerCase();
  if (!name) return;
  act("browser/add", { profile: name }, () => {
    $("#add-form").hidden = true; $("#add-name").value = "";
    select(name);
  });
});
$("#go").addEventListener("submit", async e => {
  e.preventDefault();
  const url = $("#url").value.trim();
  if (!url) return;
  try { await api("browser/open", { url, profile: sel }); $("#url").value = ""; toast("OPENED"); refresh(); if (rfb) rfb.focus(); }
  catch (err) { toast(err.message.toUpperCase().slice(0, 60)); }
});

refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 3000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
