/* clodfarm UI. A living pixel farm: every Claude on the farm is a Claude critter that wanders, watches its sub-agents
 * at their plot, or naps when its budget says so; sub-agents are mini Claudes. Plain JS, no build step, no dependencies. */
"use strict";

// ================================================================== utilities
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const lerp = (a, b, t) => a + (b - a) * t;
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
function mulberry32(a) {
  return () => { a |= 0; a = (a + 0x6d2b79f5) | 0; let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
}
// the providers a bot can use (clodfarm/bots.py has the same list; the farm checks what's sent)
const BOT_PROVIDERS = {
  openrouter: { label: "OpenRouter", url: "https://openrouter.ai/api", key: true, example: "qwen/qwen3-coder:free",
    hint: "Free models end in :free (openrouter.ai/models, filter by price). Make a key at openrouter.ai/keys. Free tiers allow a few requests a minute: the bot pauses when it hits that." },
  ollama: { label: "Ollama", url: "http://host.docker.internal:11434", key: false, example: "qwen3-coder",
    hint: "Ollama on the machine running the farm's container: pull a model that can use tools first (ollama pull qwen3-coder)." },
  custom: { label: "Anthropic-compatible", url: "", key: false, example: "",
    hint: "Any endpoint that speaks Anthropic's Messages API, like a LiteLLM gateway. The address is its base URL, without /v1." },
};
function hashStr(s) { let x = 2166136261; for (const c of String(s)) { x ^= c.charCodeAt(0); x = Math.imul(x, 16777619); } return x >>> 0; }
/** replaceChildren that flattens arrays and drops null/false (plain replaceChildren would print "null"). */
function fill(el, ...kids) { el.replaceChildren(...kids.flat(3).filter(k => k != null && k !== false)); return el; }
const nowS = () => Date.now() / 1000;
function ago(ts) {
  if (!ts) return "-";
  const s = Math.max(0, nowS() - ts);
  return s < 90 ? `${Math.round(s)}s ago` : s < 5400 ? `${Math.round(s / 60)}m ago` : s < 172800 ? `${(s / 3600).toFixed(1)}h ago` : `${(s / 86400).toFixed(1)}d ago`;
}
function until(ts) {
  if (!ts) return "";
  const s = Math.max(0, ts - nowS());
  return s < 5400 ? `${Math.round(s / 60)}m` : s < 172800 ? `${(s / 3600).toFixed(1)}h` : `${(s / 86400).toFixed(1)}d`;
}
const REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches;

// ===================================================================== sprites
function canvas(w, hgt) { const c = document.createElement("canvas"); c.width = w; c.height = hgt; return c; }
/** Paint string rows ("." = transparent) with a palette into a new canvas. */
function paint(rows, pal, w = 16) {
  const c = canvas(w, rows.length), g = c.getContext("2d");
  rows.forEach((row, y) => [...row.padEnd(w, ".").slice(0, w)].forEach((ch, x) => {
    if (ch !== "." && pal[ch]) { g.fillStyle = pal[ch]; g.fillRect(x, y, 1, 1); }
  }));
  return c;
}

const CLAY = { o: "#5a2616", h: "#f2a07a", b: "#d97757", s: "#b45a3c", d: "#86381f", e: "#231815", w: "#ffffff", k: "#f6c9b3" };
const HAT_COLORS = ["#3b7dd8", "#7b4bb7", "#2f9e6b", "#d2a21d", "#c0392b", "#2c3e50", "#e06f9f", "#16a0a0"];
const HAT_H = 6; // rows above the body for hats
// the skin picker's swatches: hat and band colours, and body tints (clay first)
const SWATCHES = ["#3b7dd8", "#7b4bb7", "#2f9e6b", "#d2a21d", "#c0392b", "#2c3e50", "#e06f9f", "#16a0a0", "#e85b5b", "#f0cf7a", "#f7f3ea", "#1c2233"];
const BODY_TINTS = ["#d97757", "#c0603f", "#e89a74", "#d9b27a", "#8fb573", "#7fa8d9", "#a98bd1", "#e68aa8", "#8a8f9c", "#9a6a4a"];
const ACCESSORIES = ["", "scarf", "glasses", "bowtie", "backpack", "cape"];
// what the picker calls them
const SWATCH_NAMES = ["blue", "purple", "green", "mustard", "red", "navy", "pink", "teal", "coral", "butter", "cream", "ink"];
const BODY_NAMES = ["clay", "rust", "peach", "sand", "sage", "sky", "lilac", "rose", "stone", "cocoa"];
const HAT_NAMES = { straw: "STRAW", beanie: "BEANIE", cap: "CAP", flower: "FLOWER", headphones: "PHONES", bow: "BOW", crown: "CROWN", sprout: "SPROUT", leaf: "LEAF", wizard: "WIZARD", chef: "CHEF", none: "NONE" };
const ACC_NAMES = { "": "NONE", scarf: "SCARF", glasses: "GLASSES", bowtie: "BOWTIE", backpack: "BACKPACK", cape: "CAPE" };

/** Clawd, the Claude Code critter: a clay block with two eyes, stubby arms and four legs. 16 x 12. */
function clawdGrid({ look = 0, legs = 0, blink = false, sleep = false, arms = 0 }) {
  const W = 16, H = 12, g = Array.from({ length: H }, () => Array(W).fill("."));
  const set = (x, y, c) => { if (x >= 0 && x < W && y >= 0 && y < H) g[y][x] = c; };
  for (let y = 0; y <= 9; y++) for (let x = 2; x <= 13; x++) {
    const edge = x === 2 || x === 13 || y === 0 || y === 9;
    if ((x === 2 || x === 13) && (y === 0 || y === 9)) continue; // rounded corners
    set(x, y, edge ? "o" : (y === 1 || x === 3) ? "h" : (x === 12 || y === 8) ? "s" : "b");
  }
  // arms (arms=1 raises them: typing / cheering)
  const ay = 5 - arms;
  for (const [x0, dir] of [[0, 1], [15, -1]]) {
    for (let y = ay - 1; y <= ay + 2; y++) for (let k = 0; k < 3; k++) {
      const x = x0 + k * dir, rim = y === ay - 1 || y === ay + 2;
      if (k === 2) set(x, y, rim ? "o" : "b");
      else set(x, y, rim || k === 0 ? "o" : (dir === 1 ? "h" : "s"));
    }
  }
  // eyes: tall and dark with a shine; closed = a line
  for (const ex of [5, 9]) {
    const x = ex + look;
    if (blink || sleep) { set(x, 5, "e"); set(x + 1, 5, "e"); }
    else { for (let y = 3; y <= 5; y++) { set(x, y, "e"); set(x + 1, y, "e"); } set(x, 3, "w"); }
  }
  if (!sleep) { set(4 + look, 6, "k"); set(11 + look, 6, "k"); } // blush
  // legs: 4 pairs of 2px; the lifted pair is shorter
  const pairs = [[3, 4], [5, 6], [9, 10], [11, 12]];
  pairs.forEach(([a, b], i) => {
    const lifted = !sleep && legs !== 0 && ((i % 2 === 0) === (legs === 1));
    for (const x of [a, b]) {
      if (sleep) return;
      if (lifted) set(x, 10, "o");
      else { set(x, 10, "d"); set(x, 11, "o"); }
    }
  });
  return g.map(r => r.join(""));
}

/** A sub-agent: a small Claude, 10 x 8, with a band in its agent's colour. Cached per colour and pose. */
const miniCache = new Map();
function miniSprite(color, { legs = 0, arms = 0, blink = false }) {
  let byPose = miniCache.get(color);
  if (!byPose) miniCache.set(color, (byPose = []));
  const code = legs + 3 * arms + 6 * (blink ? 1 : 0);
  if (byPose[code]) return byPose[code];
  const rows = Array.from({ length: 8 }, () => Array(10).fill("."));
  const set = (x, y, ch) => { if (x >= 0 && x < 10 && y >= 0 && y < 8) rows[y][x] = ch; };
  for (let y = 0; y <= 5; y++) for (let x = 1; x <= 8; x++) {
    if ((x === 1 || x === 8) && (y === 0 || y === 5)) continue;
    set(x, y, x === 1 || x === 8 || y === 0 || y === 5 ? "o" : y === 1 ? "c" : x === 7 || y === 4 ? "s" : "b");
  }
  const ay = 2 - arms;
  set(0, ay, "o"); set(0, ay + 1, "o"); set(9, ay, "o"); set(9, ay + 1, "o");
  for (const x of [3, 6]) { set(x, blink ? 3 : 2, "e"); set(x, 3, "e"); }
  [2, 4, 5, 7].forEach((x, i) => { const up = legs !== 0 && ((i % 2 === 0) === (legs === 1)); set(x, 6, up ? "o" : "d"); if (!up) set(x, 7, "o"); });
  return (byPose[code] = paint(rows.map(r => r.join("")), { ...CLAY, c: color }, 10));
}

/** Hats: "@" is the hat's colour (@d darker, @l lighter), "#" its band / trim colour. `base` and `band` are the colours
 * when the Claude picked none; `tint` hats take the Claude's own colour then (the old look). */
const HATS = {
  straw: { base: "#f0cf7a", band: "#c0392b", pal: { o: "#6b4f1d", a: "@", b: "@d", r: "#" }, rows: [
    "................", "......oooo......", ".....oaaaao.....", "....oaaaaaao....", "...orrrrrrrro...", "ooaaaaaaaaaaaaoo", ".oobbbbbbbbbboo."] },
  beanie: { tint: true, band: "#f7f3ea", pal: { o: "#1c2233", w: "#", c: "@", d: "@d", l: "@l" }, rows: [
    ".......ww.......", "......owwo......", ".....occcco.....", "...occccccco....", "..occclcccccco..", "..oddddddddddo..", "..oddddddddddo.."] },
  cap: { tint: true, band: "#f7f3ea", pal: { o: "#1c2233", w: "#", c: "@", d: "@d" }, rows: [
    "................", "................", ".....occcco.....", "...occcwccccoo..", "..occcccccccccoo", "..ooooooooodddddo", "................"] },
  flower: { base: "#ffd0dc", band: "#f5c542", pal: { o: "#6b2f1f", p: "@", y: "#", g: "#3f8f35" }, rows: [
    "................", "..........opo...", ".........opypo..", "..........opo...", "...........g....", "................", "................"] },
  headphones: { tint: true, band: "#5b6272", pal: { o: "#15171f", c: "@", l: "@l", g: "#" }, rows: [
    "................", "................", "....oooooooo....", "...oggggggggo...", "..og........go..", "oo.o........o.oo", "oco..........oco"] },
  bow: { base: "#e0508a", band: "#f59cc0", pal: { o: "#5b1330", c: "@", l: "#" }, rows: [
    "................", "................", "................", "....oo....oo....", "...olco..oclo...", "...occcoocccoo..", "....oo.oo..oo..."] },
  crown: { base: "#f5c542", band: "#d63a3a", pal: { o: "#6b4a07", y: "@", l: "@l", r: "#", b: "#3b7dd8" }, rows: [
    "................", "................", "...o...o...o....", "..oyo.oyo.oyo...", "..oyyoyyyoyyo...", "..oylyryybyylo..", "..oooooooooooo.."] },
  sprout: { base: "#6fcf5b", band: "#3f8f35", pal: { o: "#1f4d1d", g: "@", l: "@l", s: "#" }, rows: [
    "................", "...oo.....oo....", "..ollo...oglo...", "..oglgo.ogllo...", "...oogosoggo....", "......os.o......", "......os........"] },
  leaf: { base: "#63a93f", band: "#8a5a2b", pal: { o: "#1f3d14", g: "@", l: "@l", d: "@d", s: "#" }, rows: [
    ".........oo.....", ".......oolgo....", ".....ooglggo....", "....oglgggdo....", "....ogggddo.....", ".....oddoo......", "......os........"] },
  wizard: { base: "#5b4bb7", band: "#f5c542", pal: { o: "#1c1440", c: "@", l: "@l", d: "@d", y: "#" }, rows: [
    ".........oo.....", "........oco.....", ".......occo.....", "......ocycco....", ".....occcclco...", "...occcccccccoo.", "oodddyddddyddddo"] },
  chef: { base: "#f7f3ea", band: "#d8d2c4", pal: { o: "#5b5b66", w: "@", g: "#" }, rows: [
    "....oo.oo.oo....", "...owwowwowwo...", "...owwwwwwwwo...", "....owwwwwwo....", "....owwwwwwo....", "....oggggggo....", "....oooooooo...."] },
  none: { base: "#000000", band: "#000000", pal: {}, rows: [] },
};

function shade(hex, amt) {
  const n = parseInt(hex.slice(1), 16), f = (v) => clamp(Math.round(v + amt * 255), 0, 255);
  return "#" + [f(n >> 16), f((n >> 8) & 255), f(n & 255)].map(v => v.toString(16).padStart(2, "0")).join("");
}
const HEX = /^#[0-9a-fA-F]{6}$/;
const bodyPal = (body) => body === CLAY.b ? CLAY
  : { ...CLAY, o: shade(body, -0.5), h: shade(body, 0.12), b: body, s: shade(body, -0.13), d: shade(body, -0.33), k: shade(body, 0.3) };

/** A Claude's look, resolved: its hat, the hat / band / body colours and its accessory. `colors` is what the person
 * picked (may be null or partial); `fallback` is its farm colour, which tints the beanie, cap and headphones. */
const skinCache = new Map();
function skinOf(hat, colors, accessory, fallback) {
  hat = HATS[hat] ? hat : "straw";
  const def = HATS[hat], c = colors || {};
  const hc = HEX.test(c.hat || "") ? c.hat : def.tint ? (fallback || HAT_COLORS[0]) : def.base;
  const band = HEX.test(c.band || "") ? c.band : def.band, body = HEX.test(c.body || "") ? c.body : CLAY.b;
  const acc = ACCESSORIES.includes(accessory || "") ? accessory || "" : "";
  const key = `${hat}|${hc}|${band}|${body}|${acc}`;
  let s = skinCache.get(key);
  if (!s) skinCache.set(key, (s = { key, hat, hatC: hc, band, body, acc, frames: [] }));
  return s;
}
const agentSkin = (a) => skinOf(a.hat || "straw", a.colors, a.accessory, colorFor(a.id));

/** Accessories, drawn on the 16 x 18 critter in the same pixel grid ("#" = the band colour). `back` ones go behind it. */
const ACC = {
  cape: { back: ["", "", "", "", "", "", "", "..o##########o..", ".o############o.", ".o############o.", "o##############o", "o##############o", "o##############o", "o##############o", "oo############oo", ".oooooooooooooo."] },
  backpack: { pal: { b: "#8a5a2b", d: "#5e3a1a" },
    back: ["", "", "", "", "", "", "", "", "", "", "", "", ".............oo.", "............obbo", "............obbo", "............oddo", "............obbo", ".............oo."],
    front: ["", "", "", "", "", "", "", "", "", "", "", "", "", "...d.......d....", "...d.......d...."] },
  scarf: { front: ["", "", "", "", "", "", "", "", "", "", "", "", "", "..o##########o..", "..o#d#d#d#d#do..", "..........o#o...", "..........o#o...", "...........o...."] },
  glasses: { front: ["", "", "", "", "", "", "", "", "....oooooooo....", "....o..oo..o....", "....o..oo..o....", "....o..oo..o....", "....oooooooo...."] },
  bowtie: { front: ["", "", "", "", "", "", "", "", "", "", "", "", "", ".....##..##.....", ".....##dd##.....", ".....##..##....."] },
};
function drawAcc(g, skin, back, look) {
  const a = ACC[skin.acc], rows = a && (back ? a.back : a.front);
  if (!rows) return;
  const pal = { o: "#1c2233", "#": skin.band, d: shade(skin.band, -0.2), ...(a.pal || {}) };
  g.drawImage(paint(rows, pal), skin.acc === "glasses" ? look : 0, 0);
}

/** One pose of a skin, 16 x 18 (HAT_H rows of hat, then the 12-row body). Poses are cached per skin by a small number,
 * so the frame loop never builds a string key. */
const poseCode = (o) => (o.look || 0) + 1 + 3 * (o.legs || 0) + 9 * (o.blink ? 1 : 0) + 18 * (o.sleep ? 1 : 0) + 36 * (o.arms || 0);
function skinFrame(skin, o) {
  const code = poseCode(o);
  let c = skin.frames[code];
  if (c) return c;
  c = canvas(16, HAT_H + 12);
  const g = c.getContext("2d");
  drawAcc(g, skin, true, o.look || 0);
  g.drawImage(paint(clawdGrid(o), bodyPal(skin.body)), 0, HAT_H);
  drawAcc(g, skin, false, o.look || 0);
  const def = HATS[skin.hat];
  if (def.rows.length) {
    const pal = {}, hc = skin.hatC, bc = skin.band;
    for (const [k, v] of Object.entries(def.pal)) pal[k] = v === "@" ? hc : v === "@d" ? shade(hc, -0.18) : v === "@l" ? shade(hc, 0.2) : v === "#" ? bc : v;
    // hats end on the body's first row; shift down 1 when the eyes are closed so it sits snug
    g.drawImage(paint(def.rows, pal), 0, o.sleep ? 1 : 0);
  }
  return (skin.frames[code] = c);
}
/** A whole critter (kept for the landing page and the demos): critterSprite(hat, colour or skin, pose). */
function critterSprite(hat, color, opts = {}) {
  const skin = color && typeof color === "object" ? color : skinOf(hat, null, "", color);
  return skinFrame(skin, opts);
}

const EGG = paint([
  "....oooo....", "...occcco...", "..occsccco..", "..occcccco..", ".occccccsco.", ".ocsccccccco", ".occcccsccco",
  ".occcccccdco", ".odcccccdcco", "..oddcccddo.", "...odddddo..", "....oooo....",
], { o: "#6b5a3a", c: "#f6eed8", s: "#e2875f", d: "#d8c9a3" }, 12);

const LAPTOP = (on) => paint([
  ".ooooooo.", on ? ".ogsggso." : ".osssssso", on ? ".osgssso." : ".osssssso", on ? ".oggsgso." : ".osssssso", ".ooooooo.", "ommmmmmmo", "ooooooooo",
].map(r => r.slice(0, 9)), { o: "#1b1f2a", s: "#22303c", g: "#7cfc9a", m: "#9aa3b2" }, 9);
const LAPTOP_ON = LAPTOP(true), LAPTOP_OFF = LAPTOP(false);

// a 3 x 5 pixel font for the field's little signs ("+12")
const DIGITS = { "0": "111101101101111", "1": "010110010010111", "2": "111001111100111", "3": "111001011001111", "4": "101101111001001",
  "5": "111100111001111", "6": "111100111101111", "7": "111001010010010", "8": "111101111101111", "9": "111101111001111", "+": "000010111010000" };
function pxText(g, text, x, y, color) {
  g.fillStyle = color;
  [...text].forEach((ch, i) => { const b = DIGITS[ch]; if (b) for (let k = 0; k < 15; k++) if (b[k] === "1") g.fillRect(x + i * 4 + (k % 3), y + Math.floor(k / 3), 1, 1); });
}
/** A wooden sign stuck in the ground with a count on it: "+N" minis the plot has but doesn't draw. */
function drawSign(g, x, y, text) {
  const w = text.length * 4 + 3;
  g.fillStyle = "#3a2616"; g.fillRect(x + Math.floor(w / 2) - 1, y + 6, 2, 4); g.fillRect(x, y, w, 8);
  g.fillStyle = "#c9964a"; g.fillRect(x + 1, y + 1, w - 2, 6);
  pxText(g, text, x + 2, y + 2, "#3a2616");
}

/** The planner: a scarecrow at the field's edge. Awake it sways and looks round; asleep it sags, grey. 16 x 22. */
const SCARECROW = [0, 1, 2].map(f => paint([
  ".....oooooo.....", "....oyyyyyyo....", "...oyyyyyyyyo...", "..oooooooooooo..", ".....obbbbo.....",
  f === 2 ? ".....obbbbo....." : ".....oebbeo.....", ".....obbbbo.....", f === 2 ? ".....obeebo....." : ".....obmmbo.....",
  "......obbo......", f === 1 ? "yy.ooorrrrooo.yy" : ".yyooorrrrooyy..", f === 1 ? ".oooorrrrrroooo." : "yooorrrrrrrrooyy",
  "......orrrro....", "......orrrro....", "......oyyyyo....", "......y.ww.y....", ".......oww......", ".......oww......",
  ".......oww......", ".......oww......", ".......oww......", "......owwww.....", ".....oooooooo...",
], f === 2 ? { o: "#3a3a40", y: "#9a9384", b: "#a8a294", e: "#3a3a40", m: "#6b6860", r: "#7a7a82", w: "#6b6158" }
  : { o: "#3a2616", y: "#e0b64e", b: "#d8c29a", e: "#231815", m: "#86381f", r: "#c0392b", w: "#8a6038" }));

// 10 x 10 icons for bubbles and buttons
const ICONS = {
  terminal: [["oooooooooo", "osssssssso", "osgsssssso", "ossgssssso", "osgsssssso", "osssggggso", "osssssssso", "oooooooooo", "...oooo...", "..oooooo.."],
    { o: "#1b1f2a", s: "#22303c", g: "#7cfc9a" }],
  zzz: [["......oooo", ".......oo.", "......oo..", "..oooooooo", "....oo....", "...oo.....", "..oooo....", "oooo......", "..oo......", ".oooo....."],
    { o: "#3c4a6b" }],
  pause: [["..........", ".oo....oo.", ".oo....oo.", ".oo....oo.", ".oo....oo.", ".oo....oo.", ".oo....oo.", ".oo....oo.", ".oo....oo.", ".........."],
    { o: "#3c4a6b" }],
  alert: [["....oo....", "...orro...", "...orro...", "...orro...", "...orro...", "....rr....", "..........", "....rr....", "...orro...", "....oo...."],
    { o: "#6b1a10", r: "#e0513c" }],
  ask: [["..oooooo..", ".oo....oo.", ".......oo.", "......oo..", ".....oo...", "....oo....", "....oo....", "..........", "....oo....", "....oo...."],
    { o: "#3c4a6b" }],
  dots: [["..........", "..........", "..........", "..........", ".oo.oo.oo.", ".oo.oo.oo.", "..........", "..........", "..........", ".........."],
    { o: "#3c4a6b" }],
  scroll: [[".oooooooo.", "oyyyyyyyyo", ".oppppppo.", ".opooopo..", ".oppppppo.", ".opoooopo.", ".oppppppo.", ".opooppo..", "oyyyyyyyyo", ".oooooooo."],
    { o: "#5a3d1e", p: "#f6ecd0", y: "#c9964a" }],
  plan: [[".oooooooo.", "oyyyyyyyyo", ".oppppppo.", ".oprpppro.", ".oppprppo.", ".opprpppo.", ".oppppppo.", ".opppppo..", "oyyyyyyyyo", ".oooooooo."],
    { o: "#5a3d1e", p: "#f6ecd0", y: "#c9964a", r: "#d97757" }],
  swords: [["o........o", ".o......o.", "..o....o..", "...o..o...", "....oo....", "....oo....", "...o..o...", ".bo....ob.", "bb......bb", "b........b"],
    { o: "#9aa3b2", b: "#6b4a2b" }],
  chat: [["..........", ".oooooooo.", "owwwwwwwwo", "owwwwwwwwo", "owdwdwdwwo", "owwwwwwwwo", ".oooooooo.", "..oow.....", "..ow......", "..o......."],
    { o: "#3c4a6b", w: "#fff8e8", d: "#d97757" }],
  heart: [["..........", ".rr...rr..", "rllr.rrrr.", "rlrrrrrrr.", "rrrrrrrrr.", ".rrrrrrr..", "..rrrrr...", "...rrr....", "....r.....", ".........."],
    { r: "#e0513c", l: "#f7a296" }],
  egg: [["...oooo...", "..occcco..", ".occsccco.", ".occcccco.", "occccccsco", "ocscccccco", "occcccccco", "oddcccccdo", ".oddcccdo.", "..oooooo.."],
    { o: "#6b5a3a", c: "#f6eed8", s: "#e2875f", d: "#d8c9a3" }],
  chart: [["o.........", "o.......gg", "o.......gg", "o....bb.gg", "o....bb.gg", "o.rr.bb.gg", "o.rr.bb.gg", "o.rr.bb.gg", "o.rr.bb.gg", "oooooooooo"],
    { o: "#3c4a6b", b: "#1c9fd6", g: "#3cc36b", r: "#d97757" }],
  globe: [["...oooo...", "..obwbbo..", ".obwbbwbo.", "obbwbbwbbo", "owwwwwwwwo", "obbwbbwbbo", "owwwwwwwwo", ".obwbbwbo.", "..obwbbo..", "...oooo..."],
    { o: "#1b3a5a", b: "#1c9fd6", w: "#d8eef8" }],
  tasks: [["..oyyyyo..", ".oooooooo.", ".owwwwwwo.", ".ogwllllo.", ".owwwwwwo.", ".ogwllllo.", ".owwwwwwo.", ".ogwllllo.", ".owwwwwwo.", ".oooooooo."],
    { o: "#5a3d1e", y: "#c9964a", w: "#f6ecd0", g: "#3cc36b", l: "#9aa3b2" }],
  gear: [["....oo....", ".oo.gg.oo.", ".oggggggo.", "..gglogg..", "oggl..lggo", "ogg....ggo", "..gg..gg..", ".oggggggo.", ".oo.gg.oo.", "....oo...."],
    { o: "#2f251b", g: "#8d8d96", l: "#c9c9cf" }],
  roster: [["oooooooooo", "obbowwwwwo", "obbowlllwo", "oooooooooo", "obbowwwwwo", "obbowlllwo", "oooooooooo", "obbowwwwwo", "obbowlllwo", "oooooooooo"],
    { o: "#3c4a6b", b: "#d97757", w: "#fff8e8", l: "#9aa3b2" }],
  key: [["..oooo....", ".oyyyyo...", "oyyooyyo..", "oyyooyyo..", ".oyyyyo...", "..oyyo....", "..oyyoo...", "..oyyyyo..", "..oyyo....", "..oyyyo..."],
    { o: "#5a3d1e", y: "#f5c542" }],
  plus: [["..........", "....oo....", "....oo....", "....oo....", ".oooooooo.", ".oooooooo.", "....oo....", "....oo....", "....oo....", ".........."], { o: "#2f251b" }],
  minus: [["..........", "..........", "..........", "..........", ".oooooooo.", ".oooooooo.", "..........", "..........", "..........", ".........."], { o: "#2f251b" }],
  fit: [["ooo....ooo", "oo......oo", "o.o....o.o", "..........", "...gggg...", "...gggg...", "..........", "o.o....o.o", "oo......oo", "ooo....ooo"], { o: "#2f251b", g: "#3cc36b" }],
  dice: [[".oooooooo.", "owwwwwwwwo", "owkkwwkkwo", "owkkwwkkwo", "owwwkkwwwo", "owwwkkwwwo", "owkkwwkkwo", "owkkwwkkwo", "owwwwwwwwo", ".oooooooo."],
    { o: "#1f2a44", w: "#fff8e8", k: "#d97757" }],
  plug: [["..mn..mn..", "..mn..mn..", "oooooooooo", "ohhhhhhhbo", "ohbbbbbbso", "obbbbbbbso", ".obbbbbso.", "..osssso..", "...occo...", "....cc...."],
    { o: "#2f251b", m: "#b7bfcc", n: "#6b7383", h: "#f2a07a", b: "#d97757", s: "#b45a3c", c: "#3c4a6b" }],
  quill: [["........oo", ".......owo", "......owwo", ".....owwo.", "....owwo..", "...owwo...", "..oowo....", "..ooo.....", ".ooo......", "oo........"],
    { o: "#3a2a1a", w: "#f6ecd0" }],
};
const iconURL = {};
function icon(name, scale = 1) {
  const k = name + scale;
  if (iconURL[k]) return iconURL[k];
  if (name === "party") {
    const s = critterSprite("straw", HAT_COLORS[0], { legs: 0 }), c = canvas(18, 18), g = c.getContext("2d");
    g.drawImage(s, 1, 0);
    return (iconURL[k] = c.toDataURL());
  }
  if (!ICONS[name]) return ""; // an unknown icon: no picture rather than a broken page
  const [rows, pal] = ICONS[name];
  return (iconURL[k] = paint(rows, pal, 10).toDataURL());
}

// ================================================================= HD sprites
/* The farm draws on a pixel grid twice as fine as the classics above: a critter is 32 x 36 HD pixels in the same
 * 16 x 18 world box, so the layout keeps its world pixels while the art gets shading, shines and crisp outlines. The
 * classics (critterSprite, miniSprite, EGG, LAPTOP_ON, SCARECROW) stay for the landing page's faller and the demos.
 * Shapes are filled on a Pix grid, then ringed in ink by outline(). */
const HD = 2;
const rgbOf = (hx) => [parseInt(hx.slice(1, 3), 16), parseInt(hx.slice(3, 5), 16), parseInt(hx.slice(5, 7), 16)];
function mix(a, b, t) { const A = rgbOf(a), B = rgbOf(b); return "#" + A.map((v, i) => Math.round(v + (B[i] - v) * t).toString(16).padStart(2, "0")).join(""); }
class Pix {
  constructor(w, hgt) { this.w = w; this.h = hgt; this.p = new Array(w * hgt).fill(null); }
  set(x, y, c) { x = Math.floor(x); y = Math.floor(y); if (c && x >= 0 && y >= 0 && x < this.w && y < this.h) this.p[y * this.w + x] = c; return this; }
  get(x, y) { return x >= 0 && y >= 0 && x < this.w && y < this.h ? this.p[y * this.w + x] : null; }
  rect(x, y, w, hgt, c) { for (let j = 0; j < hgt; j++) for (let i = 0; i < w; i++) this.set(x + i, y + j, c); return this; }
  /** An ellipse over pixel centres, optionally turned by `rot`; `c` may be a function (x, y, u, v) -> colour, where
   * u, v run -1..1 across it (so shading is "u < -0.4: lit side"). */
  oval(cx, cy, rx, ry, c, rot = 0) {
    const cs = Math.cos(rot), sn = Math.sin(rot), R = Math.max(rx, ry) + 1;
    for (let y = Math.floor(cy - R); y <= cy + R; y++) for (let x = Math.floor(cx - R); x <= cx + R; x++) {
      const dx = x + 0.5 - cx, dy = y + 0.5 - cy, u = (dx * cs + dy * sn) / rx, v = (-dx * sn + dy * cs) / ry;
      if (u * u + v * v <= 1) this.set(x, y, typeof c === "function" ? c(x, y, u, v) : c);
    }
    return this;
  }
  line(x0, y0, x1, y1, c) { const n = Math.max(Math.abs(x1 - x0), Math.abs(y1 - y0), 1); for (let i = 0; i <= n; i++) this.set(Math.round(x0 + (x1 - x0) * i / n), Math.round(y0 + (y1 - y0) * i / n), c); return this; }
  /** Ring every filled shape in `c` (the empty pixels next to a filled one). */
  outline(c, diag = false) {
    const add = [], f = (x, y) => !!this.get(x, y);
    for (let y = 0; y < this.h; y++) for (let x = 0; x < this.w; x++) {
      if (this.p[y * this.w + x]) continue;
      if (f(x - 1, y) || f(x + 1, y) || f(x, y - 1) || f(x, y + 1) || (diag && (f(x - 1, y - 1) || f(x + 1, y - 1) || f(x - 1, y + 1) || f(x + 1, y + 1)))) add.push(y * this.w + x);
    }
    for (const i of add) this.p[i] = c;
    return this;
  }
  /** Recolour: fn(x, y, colour) returns the new colour, or undefined to keep it. */
  map(fn) { for (let i = 0; i < this.p.length; i++) if (this.p[i]) { const n = fn(i % this.w, Math.floor(i / this.w), this.p[i]); if (n !== undefined) this.p[i] = n; } return this; }
  canvas() {
    const c = canvas(this.w, this.h), g = c.getContext("2d"), img = g.createImageData(this.w, this.h), d = img.data, memo = {};
    this.p.forEach((col, i) => { if (!col) return; const v = memo[col] || (memo[col] = rgbOf(col)); d[i * 4] = v[0]; d[i * 4 + 1] = v[1]; d[i * 4 + 2] = v[2]; d[i * 4 + 3] = 255; });
    g.putImageData(img, 0, 0);
    return c;
  }
}
const EYE = "#231815", SHINE = "#ffffff";
/** A body colour's HD palette: ink outline, spec, highlight, base, shade, dark, blush. */
const bodyPalHD = (body) => body === CLAY.b
  ? { o: "#4a1f12", l: "#f7c3a3", h: "#ec9670", b: "#d97757", s: "#b95d3f", d: "#8a3a21", k: "#f3ab98" }
  : { o: mix(shade(body, -0.5), "#1a1216", 0.25), l: shade(body, 0.2), h: shade(body, 0.09), b: body, s: shade(body, -0.11), d: shade(body, -0.28), k: mix(body, "#ff9f9f", 0.45) };

/** Clawd in HD: the clay block with tall dark eyes, side nubs and four legs, 32 x 36 (the body starts at row 12, under
 * the hat). Poses: look (-1, 0, 1), legs (0 stand, 1 / 2 the walk's two steps), blink, sleep (sat down, eyes shut),
 * arms (0 down, 1 up: cheering, 2 / 3 tapping at a laptop), happy (a little open smile). */
function clawdHD(pal, { look = 0, legs = 0, blink = false, sleep = false, arms = 0, happy = false }) {
  const P = new Pix(32, 36);
  const x0 = sleep ? 4 : 5, x1 = sleep ? 27 : 26, y0 = sleep ? 18 : 13, y1 = sleep ? 33 : 30;
  for (let y = y0; y <= y1; y++) for (let x = x0; x <= x1; x++) {
    if ((x === x0 || x === x1) && (y === y0 || y === y1)) continue; // rounded corners
    let c = pal.b;
    if (y <= y0 + 1 || x <= x0 + 1) c = pal.h;
    if (y === y0 && x >= x0 + 2 && x <= x0 + 9) c = pal.l;
    if (x === x0 + 1 && y === y0 + 1) c = pal.l;
    if (x >= x1 - 1 || y >= y1 - 1) c = pal.s;
    if (y === y1 && x > x0 + 1 && x < x1 - 1) c = pal.d;
    P.set(x, y, c);
  }
  // nubs: down, both up (cheer), or tapping one then the other (typing)
  const ay = sleep ? [y1 - 5, y1 - 5] : arms === 1 ? [15, 15] : arms === 2 ? [19, 22] : arms === 3 ? [22, 19] : [21, 21];
  const tall = sleep ? 3 : 4;
  for (let k = 0; k < tall; k++) {
    P.set(x0 - 3, ay[0] + k, k === 0 ? pal.l : pal.h); P.set(x0 - 2, ay[0] + k, pal.h); P.set(x0 - 1, ay[0] + k, pal.b);
    P.set(x1 + 1, ay[1] + k, pal.b); P.set(x1 + 2, ay[1] + k, pal.s); P.set(x1 + 3, ay[1] + k, k === tall - 1 ? pal.d : pal.s);
  }
  if (!sleep) [6, 11, 19, 24].forEach((lx, i) => { // legs: the lifted pair is shorter
    const up = legs !== 0 && ((i % 2 === 0) === (legs === 1));
    for (let y = 31; y <= (up ? 32 : 33); y++) { P.set(lx, y, y === 31 ? pal.s : pal.d); P.set(lx + 1, y, pal.d); }
  });
  P.outline(pal.o);
  // eyes: tall and dark with a shine; blinking a line; asleep, happy little arcs
  const ey = y0 + 4, sh = look * 2;
  for (const ex of [10 + sh, 19 + sh]) {
    if (sleep) { P.set(ex, ey + 3, EYE); P.set(ex + 1, ey + 4, EYE); P.set(ex + 2, ey + 3, EYE); }
    else if (blink) { P.rect(ex, ey + 4, 3, 1, EYE); P.set(ex - 1, ey + 3, pal.s); P.set(ex + 3, ey + 3, pal.s); }
    else { P.rect(ex, ey, 3, 6, EYE); P.set(ex, ey, SHINE); P.set(ex, ey + 1, SHINE); P.set(ex + 2, ey + 4, "#4a3a36"); }
  }
  P.rect(7 + sh, ey + 7, 3, 1, pal.k); P.rect(22 + sh, ey + 7, 3, 1, pal.k); // blush
  if (happy && !sleep) { P.rect(15 + sh, ey + 7, 2, 1, EYE); P.rect(15 + sh, ey + 8, 2, 1, "#c8505a"); }
  return P;
}

/** HD hats, drawn over the body on their own layer and ringed in their own ink. c: {h, l, d, dd, b, bl, bd}. */
const HATS_HD = {
  straw(P, c) {
    const brim = (from) => P.oval(16, 12, 15.2, 2.9, (x, y, u, v) => y < from ? null : v < -0.25 ? c.l : v > 0.45 ? c.d : c.h);
    brim(0);
    P.oval(16, 8.5, 7.2, 5.5, (x, y, u) => y > 11 ? null : y === 9 ? c.bl : y === 10 ? c.b : y === 11 ? c.bd : u < -0.5 ? c.l : u > 0.62 ? c.d : c.h);
    brim(12);
    P.map((x, y, col) => col === c.h && (x * 3 + y * 5) % 7 === 0 ? c.d : undefined); // the weave
  },
  beanie(P, c) {
    P.oval(16, 14.5, 12.6, 11, (x, y, u, v) => y > 15 ? null : y >= 13 ? (x % 2 ? c.d : c.dd) : u < -0.45 && v < -0.1 ? c.l : x % 3 === 0 ? c.d : c.h);
    P.oval(16, 3.2, 3.3, 3.1, (x, y, u, v) => u < -0.1 && v < -0.1 ? c.bl : v > 0.5 ? c.bd : c.b);
  },
  cap(P, c) {
    P.oval(15, 13.5, 10.6, 8.6, (x, y, u, v) => y > 13 ? null : u < -0.45 && v < -0.2 ? c.l : u > 0.55 ? c.d : c.h);
    P.line(15, 6, 15, 12, c.d); P.rect(14, 4, 3, 1, c.b);
    P.rect(8, 9, 4, 3, c.b); P.set(8, 9, c.bl);
    P.rect(21, 12, 10, 1, c.h); P.rect(21, 13, 10, 1, c.dd); P.set(30, 12, c.d); P.rect(22, 11, 6, 1, c.d);
  },
  flower(P, c) {
    P.line(23, 9, 21, 13, "#3f8f35"); P.oval(19.5, 11, 2.2, 1.2, "#63b04a", -0.5);
    for (const [px, py] of [[20.5, 5], [26.5, 5], [23.5, 2.2], [23.5, 7.8]]) P.oval(px, py, 2.6, 2.4, (x, y, u, v) => u < -0.2 && v < -0.2 ? c.l : v > 0.5 ? c.d : c.h);
    P.oval(23.5, 5, 1.9, 1.9, (x, y, u, v) => u < 0 && v < 0 ? c.bl : c.b);
  },
  headphones(P, c) {
    P.oval(16, 15.5, 13.6, 11.5, (x, y, u, v) => y > 14 || u * u + v * v < 0.7 ? null : v < -0.9 ? c.bl : c.b);
    for (let y = 14; y <= 21; y++) {
      const edge = y === 14 || y === 21;
      if (!edge) { P.set(1, y, c.l); P.set(30, y, c.d); }
      P.rect(2, y, 2, 1, c.h); P.set(4, y, c.bd); P.set(27, y, c.bd); P.rect(28, y, 2, 1, y > 18 ? c.d : c.h);
    }
  },
  bow(P, c) {
    const loop = (x, y, u, v) => Math.abs(v) < 0.35 && Math.abs(u) > 0.55 ? c.d : v < -0.3 ? c.l : c.h;
    P.oval(10.5, 8.5, 4.6, 3.5, loop, -0.3); P.oval(21.5, 8.5, 4.6, 3.5, loop, 0.3);
    P.rect(13, 11, 2, 3, c.d); P.rect(17, 11, 2, 3, c.d);
    P.oval(16, 9.2, 2.3, 2.5, (x, y, u, v) => u < 0 && v < 0 ? c.bl : c.b);
  },
  crown(P, c) {
    for (const [px, top] of [[8, 4], [14, 2], [20, 4]]) {
      for (let y = top; y <= 9; y++) { const half = (y - top + 1) / (10 - top) * 2.2; for (let x = px; x < px + 4; x++) if (Math.abs(x + 0.5 - (px + 2)) <= half) P.set(x, y, x === px ? c.l : c.h); }
      P.rect(px + 1, top - 1, 2, 1, "#fff4c2");
    }
    P.rect(8, 9, 16, 4, c.h); P.rect(8, 9, 16, 1, c.l); P.rect(8, 12, 16, 1, c.d);
    P.rect(10, 10, 2, 2, c.b); P.rect(15, 10, 2, 2, "#3b7dd8"); P.rect(20, 10, 2, 2, c.b);
    P.set(10, 10, c.bl); P.set(15, 10, "#9cc6f5"); P.set(20, 10, c.bl);
  },
  sprout(P, c) {
    P.line(16, 13, 16, 7, c.bd); P.set(15, 13, c.bd);
    const leaf = (x, y, u, v) => Math.abs(v) < 0.2 && Math.abs(u) < 0.8 ? c.d : v < -0.25 ? c.l : c.h;
    P.oval(11.2, 6.5, 5.2, 2.6, leaf, 0.4); P.oval(20.8, 5.5, 5.2, 2.6, leaf, -0.4);
  },
  leaf(P, c) {
    P.line(5, 13, 8, 11, c.b);
    P.oval(16, 7.5, 10.4, 4.1, (x, y, u, v) => v < -0.35 ? c.l : v > 0.5 ? c.d : c.h, -0.45);
    P.line(8, 11, 24, 4, c.d);
    for (const k of [11, 15, 19]) P.line(k, 10 - (k - 8) * 0.43, k + 2, 7 - (k - 8) * 0.43, c.d);
  },
  wizard(P, c) {
    const brim = (from) => P.oval(16, 12.5, 14.6, 2.6, (x, y, u, v) => y < from ? null : v < -0.3 ? c.l : v > 0.4 ? c.dd : c.d);
    brim(0);
    for (let y = 0; y <= 11; y++) {
      const t = (11 - y) / 11, half = 7.6 * (1 - t) + 0.7, cx = 16 + t * t * 6;
      for (let x = 0; x < 32; x++) { const dx = x + 0.5 - cx; if (Math.abs(dx) <= half) P.set(x, y, y >= 9 && y <= 10 ? (y === 9 ? c.bl : c.b) : dx < -half * 0.45 ? c.l : dx > half * 0.5 ? c.d : c.h); }
    }
    for (const [sx, sy] of [[13, 6], [19, 3]]) { P.set(sx, sy, c.bl); P.set(sx - 1, sy, c.b); P.set(sx + 1, sy, c.b); P.set(sx, sy - 1, c.b); P.set(sx, sy + 1, c.b); }
    brim(12);
  },
  chef(P, c) {
    P.rect(9, 6, 14, 6, c.h);
    const puff = (x, y, u, v) => u < -0.3 && v < -0.2 ? c.l : v > 0.55 ? c.d : c.h;
    P.oval(11, 5.6, 4.3, 3.7, puff); P.oval(21, 5.6, 4.3, 3.7, puff); P.oval(16, 3.9, 4.7, 3.9, puff);
    for (const x of [12, 16, 20]) P.line(x, 7, x, 9, c.d);
    P.rect(9, 10, 14, 3, c.b); P.rect(9, 10, 14, 1, c.bl); P.rect(9, 12, 14, 1, c.bd); P.set(22, 11, c.bd);
  },
  none() {},
};
/** Accessories: `back` goes behind the body, `front` over it. dy: the body's drop when it sits down to nap. */
const ACC_HD = {
  scarf: { front(P, c, lk, dy) {
    P.rect(4, 25 + dy, 24, 3, c.b); P.rect(4, 25 + dy, 24, 1, c.bl);
    for (let x = 6; x < 28; x += 4) P.rect(x, 26 + dy, 1, 2, c.bd);
    if (!dy) { P.rect(21, 28, 3, 5, c.b); P.rect(21, 30, 3, 1, c.bd); P.set(21, 33, c.bd); P.set(23, 33, c.bd); }
  } },
  glasses: { ink: true, front(P, c, lk, dy) {
    const sh = lk * 2;
    for (const ex of [8 + sh, 17 + sh]) {
      P.rect(ex, 16 + dy, 7, 1, "#1c2233"); P.rect(ex, 23 + dy, 7, 1, "#1c2233"); P.rect(ex, 17 + dy, 1, 6, "#1c2233"); P.rect(ex + 6, 17 + dy, 1, 6, "#1c2233");
      P.set(ex + 5, 17 + dy, "#e9f6ff"); P.set(ex + 5, 18 + dy, "#bfe0f5");
    }
    P.rect(15 + sh, 18 + dy, 2, 1, "#1c2233");
  } },
  bowtie: { front(P, c, lk, dy) {
    const w = (x, y, u, v) => v < -0.3 ? c.bl : Math.abs(u) > 0.6 ? c.bd : c.b;
    P.oval(12.4, 26.5 + dy, 3.2, 2.3, w); P.oval(19.6, 26.5 + dy, 3.2, 2.3, w); P.rect(15, 25 + dy, 2, 3, c.bd);
  } },
  backpack: {
    back(P, c, lk, dy) { P.rect(26, 17 + dy, 6, 12, "#8a5a2b"); P.rect(26, 17 + dy, 6, 3, "#6d4420"); P.rect(29, 21 + dy, 2, 2, "#e0b64e"); P.rect(31, 20 + dy, 1, 9, "#6d4420"); },
    front(P, c, lk, dy) { P.rect(6, 13 + dy, 1, 16, "#6d4420"); P.rect(25, 13 + dy, 1, 16, "#6d4420"); P.set(6, 22 + dy, "#e0b64e"); P.set(25, 22 + dy, "#e0b64e"); },
  },
  cape: { back(P, c, lk, dy) {
    for (let y = 15 + dy; y <= 35; y++) { const half = 12.5 + (y - 15 - dy) * 0.16; for (let x = 0; x < 32; x++) { const d = x + 0.5 - 16; if (Math.abs(d) <= half) P.set(x, y, y === 15 + dy ? c.bl : Math.round(d) % 5 === 0 ? c.bd : c.b); } }
  } },
};
const hatPal = (hc, band) => ({ h: hc, l: shade(hc, 0.17), d: shade(hc, -0.17), dd: shade(hc, -0.3), b: band, bl: shade(band, 0.18), bd: shade(band, -0.2) });
const inkOf = (hc) => mix(hc, "#1b1420", 0.74);

/** One HD pose of a skin, 32 x 36. Bodies are shared by body colour and pose; the hat and accessory layers per skin. */
const bodyHDCache = new Map();
function skinFrameHD(skin, o) {
  const code = poseCode(o) + 144 * (o.happy ? 1 : 0);
  skin.hd = skin.hd || [];
  let c = skin.hd[code];
  if (c) return c;
  const bkey = skin.body + "|" + code;
  let body = bodyHDCache.get(bkey);
  if (!body) bodyHDCache.set(bkey, (body = clawdHD(bodyPalHD(skin.body), o).canvas()));
  c = canvas(32, 36);
  const g = c.getContext("2d"), dy = o.sleep ? 5 : 0, lk = o.look || 0, cp = hatPal(skin.band, skin.band), acc = ACC_HD[skin.acc];
  const layer = (fn, ink) => { const P = new Pix(32, 36); fn(P); if (ink) P.outline(ink); g.drawImage(P.canvas(), 0, 0); };
  if (acc?.back) layer((P) => acc.back(P, cp, lk, dy), "#1c1a24");
  g.drawImage(body, 0, 0);
  if (acc?.front) layer((P) => acc.front(P, cp, lk, dy), acc.ink ? null : "#1c1a24");
  if (skin.hat !== "none") {
    if (!skin.hatHD) { const P = new Pix(32, 36); HATS_HD[skin.hat](P, hatPal(skin.hatC, skin.band)); P.outline(inkOf(skin.hatC)); skin.hatHD = P.canvas(); }
    g.drawImage(skin.hatHD, 0, dy);
  }
  return (skin.hd[code] = c);
}

/** A sub-agent in HD: a small Claude with a headband in its colour, 20 x 16 (world 10 x 8). */
const miniHDCache = new Map();
function miniHD(color, { legs = 0, arms = 0, blink = false }) {
  const code = color + (legs + 3 * arms + 6 * (blink ? 1 : 0));
  let c = miniHDCache.get(code);
  if (c) return c;
  const P = new Pix(20, 16), pal = bodyPalHD(CLAY.b);
  for (let y = 2; y <= 11; y++) for (let x = 3; x <= 16; x++) {
    if ((x === 3 || x === 16) && (y === 2 || y === 11)) continue;
    P.set(x, y, y === 4 ? shade(color, 0.16) : y === 5 ? color : y === 2 || x === 3 ? pal.h : x === 16 || y === 11 ? pal.s : pal.b);
  }
  P.set(17, 5, color); P.set(18, 6, shade(color, -0.15)); // the headband's knot
  const ay = 7 - arms * 3;
  P.rect(1, ay, 2, 3, pal.h); P.rect(17, ay + (arms ? 0 : 0), 2, 3, pal.s);
  [4, 7, 11, 14].forEach((lx, i) => { const up = legs !== 0 && ((i % 2 === 0) === (legs === 1)); P.rect(lx, 12, 2, up ? 1 : 2, pal.d); });
  P.outline(pal.o);
  for (const ex of [6, 12]) { if (blink) P.rect(ex, 9, 2, 1, EYE); else { P.rect(ex, 7, 2, 3, EYE); P.set(ex, 7, SHINE); } }
  miniHDCache.set(code, (c = P.canvas()));
  return c;
}

/** The egg, the laptop and the scarecrow in HD. */
const EGG_HD = (() => {
  const P = new Pix(24, 24), cy = 13.5, ry = 10;
  for (let y = 3; y < 24; y++) {
    const t = (y + 0.5 - cy) / ry; if (Math.abs(t) > 1) continue;
    const half = 8.4 * Math.sqrt(1 - t * t) * (t < 0 ? 1 + t * 0.2 : 1);
    for (let x = 0; x < 24; x++) { const d = (x + 0.5 - 12) / half; if (Math.abs(d) <= 1) P.set(x, y, d < -0.35 && t < -0.1 ? "#fffaf0" : d > 0.55 || t > 0.7 ? "#dccba2" : "#f6eed8"); }
  }
  for (const [x, y, c] of [[9, 8, "#e2875f"], [10, 8, "#e2875f"], [15, 11, "#e2875f"], [7, 14, "#f0a57f"], [13, 17, "#e2875f"], [14, 17, "#e2875f"], [16, 20, "#f0a57f"], [10, 20, "#e2875f"], [17, 14, "#c9b489"]]) P.set(x, y, c);
  P.set(9, 6, "#ffffff"); P.set(8, 7, "#ffffff");
  return P.outline("#6b5a3a").canvas();
})();
const LAPTOP_HD = [0, 1, 2].map(f => {
  const P = new Pix(18, 14);
  P.rect(1, 0, 16, 9, "#1b1f2a"); P.rect(2, 1, 14, 7, f === 2 ? "#1d2833" : "#22303c");
  if (f < 2) {
    const rows = f ? [[3, 5, "#7cfc9a"], [4, 3, "#d97757"], [3, 7, "#7cfc9a"]] : [[3, 7, "#7cfc9a"], [5, 4, "#7cfc9a"], [3, 4, "#d97757"]];
    rows.forEach(([x, w, c], i) => P.rect(x, 2 + i * 2, w, 1, c));
    P.rect(12, 6, 2, 1, f ? "#7cfc9a" : "#22303c");
  } else P.set(3, 2, "#3a4a58");
  P.rect(0, 9, 18, 1, "#c3cad6"); P.rect(0, 10, 18, 2, "#9aa3b2"); P.rect(0, 12, 18, 1, "#1b1f2a");
  for (let x = 2; x < 16; x += 2) P.set(x, 10, "#6b7383");
  P.set(0, 9, "#1b1f2a"); P.set(17, 9, "#1b1f2a"); P.rect(0, 10, 1, 2, "#1b1f2a"); P.rect(17, 10, 1, 2, "#1b1f2a");
  return P.canvas();
});
const SCARECROW_HD = [0, 1, 2].map(f => {
  const off = f === 2, C = off ? { o: "#3a3a40", y: "#a39d8c", yd: "#857f70", b: "#b4ad9e", bd: "#958e80", r: "#80808a", rd: "#66666e", w: "#6b6158", e: "#3a3a40" }
    : { o: "#3a2616", y: "#e8be55", yd: "#c28f2c", b: "#dcc49a", bd: "#bfa57a", r: "#c0392b", rd: "#8e2a20", w: "#8a6038", e: "#231815" };
  const P = new Pix(32, 44), lift = f === 1 ? -1 : 0;
  P.rect(15, 24, 2, 19, C.w); P.set(15, 24, shade(C.w, 0.15));
  for (let x = 2; x <= 29; x++) for (let y = 18; y <= 22; y++) { const armY = y + (x < 10 ? lift : x > 21 ? -lift : 0); P.set(x, armY, (Math.floor(x / 3) + y) % 3 === 0 ? C.rd : C.r); }
  for (const [x, s] of [[0, -1], [31, 1]]) for (let k = 0; k < 4; k++) P.set(x - s * (k % 2), 18 + k + (x < 10 ? lift : -lift), k % 2 ? C.yd : C.y);
  for (let y = 17; y <= 30; y++) for (let x = 9; x <= 22; x++) P.set(x, y, (x % 4 === 1 || y % 4 === 1) ? C.rd : C.r);
  P.rect(9, 28, 14, 1, C.y); P.rect(10, 31, 3, 2, C.y); P.rect(15, 31, 2, 3, C.yd); P.rect(19, 31, 3, 2, C.y);
  const hy = off ? 1 : 0, hx = off ? 1 : 0;
  P.oval(16 + hx, 12 + hy, 6.4, 6, (x, y, u, v) => u < -0.4 && v < -0.2 ? shade(C.b, 0.08) : u > 0.5 || v > 0.6 ? C.bd : C.b);
  P.rect(8 + hx, 6 + hy, 17, 2, C.y); P.rect(8 + hx, 7 + hy, 17, 1, C.yd);
  P.oval(16 + hx, 3.5 + hy, 5.5, 3.6, (x, y, u) => y > 6 + hy ? null : u < -0.3 ? shade(C.y, 0.1) : C.y);
  P.rect(11 + hx, 5 + hy, 11, 1, C.r);
  P.outline(C.o);
  if (off) { P.rect(13 + hx, 12 + hy, 2, 1, C.e); P.rect(18 + hx, 12 + hy, 2, 1, C.e); }
  else for (const ex of [13, 18]) { P.set(ex, 10, C.e); P.set(ex + 1, 11, C.e); P.set(ex + 1, 10, C.e); P.set(ex, 11, C.e); }
  for (let x = 13; x <= 19; x++) P.set(x + hx, 15 + hy + (x === 13 || x === 19 ? -1 : 0), x % 2 ? C.e : C.bd); // stitched smile
  return P.canvas();
});
/** The field's crops, 8 x 16 HD (world 4 x 8), three sway frames each: seed, sprout, grow, ripe (Claude's spark), wilt. */
const SPARK = ["....o....", ".o..o..o.", "..o.o.o..", "...ooo...", "oooowoooo", "...ooo...", "..o.o.o..", ".o..o..o.", "....o...."];
const CROPS = {};
for (const stage of ["seed", "sprout", "grow", "ripe", "wilt"]) CROPS[stage] = [-1, 0, 1].map(sw => {
  const P = new Pix(10, 18), G1 = "#3f8f35", G2 = "#6fcf5b", G3 = "#a6e27a";
  if (stage === "seed") { P.rect(3, 15, 4, 1, "#4a3220"); P.set(4, 14, G2); P.set(5, 13, G2); P.set(3, 13, G1); P.set(6, 14, G3); }
  else if (stage === "wilt") { P.line(4, 16, 4, 10, "#7a6a3a"); P.line(4, 10, 7, 11, "#7a6a3a"); P.set(7, 12, "#5c4b27"); P.line(3, 13, 1, 14, "#8a7a45"); P.set(8, 12, "#5c4b27"); }
  else {
    const top = stage === "sprout" ? 10 : 6;
    for (let y = top; y <= 16; y++) P.set(4 + (y < top + 3 ? sw : 0), y, G1);
    P.rect(1 + sw, top + 3, 3, 1, G2); P.set(1 + sw, top + 2, G3); P.rect(5 + sw, top + 5, 3, 1, G2); P.set(7 + sw, top + 4, G3);
    if (stage !== "sprout") { P.rect(1, top + 8, 3, 1, G2); P.rect(5, top + 9, 3, 1, G1); }
    if (stage === "grow") { P.rect(3 + sw, top - 2, 3, 2, "#d97757"); P.set(4 + sw, top - 2, "#f2a07a"); }
    if (stage === "ripe") SPARK.forEach((row, y) => [...row].forEach((ch, x) => { if (ch !== ".") P.set(x + sw - 0.5, y - 1, ch === "w" ? "#fff1cf" : x === 4 || y === 4 ? "#e8845f" : "#d97757"); }));
  }
  return P.canvas();
});
/** Small things drawn on the canvas: a shadow per size, the selection arrows, the waiting flag, a z for napping. */
const shadowHD = new Map();
function shadowSprite(rx, ry) {
  const k = rx + "x" + ry;
  if (!shadowHD.has(k)) { const P = new Pix(rx * 2 + 2, ry * 2 + 2); P.oval(rx + 1, ry + 1, rx, ry, "#14200c"); const c = P.canvas(); shadowHD.set(k, c); }
  return shadowHD.get(k);
}
const ARROW = (fill, ink) => { const P = new Pix(9, 7); for (let y = 0; y < 5; y++) P.rect(y, y, 9 - y * 2, 1, fill); P.set(1, 0, shade(fill, 0.2)); return P.outline(ink).canvas(); };
const ARROW_SEL = ARROW("#ffffff", "#1f2a44"), ARROW_MINE = ARROW("#ffd24a", "#6b4a07");
const FLAG_HD = [0, 1].map(f => { const P = new Pix(12, 20); P.rect(1, 0, 1, 19, "#3a1410"); const c = f ? "#ff8a70" : "#e0513c";
  for (let y = 1; y < 11; y++) P.rect(2, y, 8 - (y > 8 ? (y - 8) * 2 : 0) + (f && y > 3 && y < 7 ? 1 : 0), 1, c); P.rect(5, 3, 2, 3, "#fff"); P.rect(5, 7, 2, 1, "#fff"); return P.outline("#3a1410").canvas(); });
const ZED = (() => { const P = new Pix(6, 6); P.rect(0, 0, 5, 1, "#f4f1e8"); P.line(4, 1, 0, 4, "#f4f1e8"); P.rect(0, 4, 5, 1, "#f4f1e8"); return P.outline("#3c4a6b").canvas(); })();
const SPARKLE = ["#fff4c2", "#ffd59e", "#f2a07a", "#ffffff"].map(c => { const P = new Pix(7, 7); P.rect(3, 1, 1, 5, c); P.rect(1, 3, 5, 1, c); P.set(3, 3, "#ffffff"); P.set(3, 0, shade(c, -0.1)); P.set(3, 6, shade(c, -0.1)); P.set(0, 3, shade(c, -0.1)); P.set(6, 3, shade(c, -0.1)); return P.canvas(); });

/** The little meadow behind a portrait (drawn in device pixels, in blocks of u so it matches the sprite's grain). */
function tileBg(g, W) {
  const u = Math.max(2, Math.round(W / 48)), rnd = mulberry32(W);
  g.fillStyle = "#71923c"; g.fillRect(0, 0, W, W);
  for (let i = 0; i < 46; i++) { g.fillStyle = ["#648436", "#80a246", "#648436", "#93b552"][i % 4]; g.fillRect(Math.floor(rnd() * W / u) * u, Math.floor(rnd() * W / u) * u, u, u * (i % 3 ? 1 : 2)); }
  const cx = W / 2, cy = W - u * 4, rx = W * 0.38, ry = u * 3;
  for (let y = -ry; y <= ry; y += u) { const w = Math.floor(rx * Math.sqrt(Math.max(0, 1 - (y * y) / (ry * ry))) / u) * u; g.fillStyle = y < 0 ? "#8ea456" : "#7d9446"; g.fillRect(Math.round(cx - w), Math.round(cy + y), w * 2, u); }
}
/** Draw an HD sprite to a canvas element crisply at `css` px: the backing store follows devicePixelRatio and the sprite
 * scales by a whole number. */
function paintSprite(cv, img, css, { bg = null, pad = 0, bottom = false, fixed = false } = {}) {
  const dpr = Math.min(3, devicePixelRatio || 1), W = Math.round(css * dpr);
  if (cv.width !== W) { cv.width = W; cv.height = W; }
  if (!fixed) cv.style.width = cv.style.height = css + "px"; // (fixed: CSS sizes it, css is its content width)
  const g = cv.getContext("2d"); g.imageSmoothingEnabled = false; g.clearRect(0, 0, W, W);
  if (bg) bg(g, W);
  const k = Math.max(1, Math.floor((W - pad * 2 * dpr) / Math.max(img.width, img.height)));
  const x = Math.round((W - img.width * k) / 2), y = bottom ? Math.round(W - pad * dpr - img.height * k) : Math.round((W - img.height * k) / 2);
  g.drawImage(img, x, y, img.width * k, img.height * k);
  return k;
}

// ======================================================================= world
const G = { // Thronglet-ish meadow palette
  g0: "#4a6524", g1: "#577530", g2: "#648436", g3: "#71923c", g4: "#80a246", worn: "#8ea456",
  t0: "#1c3519", t1: "#28501f", t2: "#346a27", t3: "#468a31", t4: "#63a93f", t5: "#8cc65a", trunk: "#4b3421",
  r0: "#34343c", r1: "#55555e", r2: "#6f6f78", r3: "#8d8d96", r4: "#aeaeb5",
  soil: "#6b4a2e", soil2: "#57391f", soil3: "#7d5a3a", soilo: "#3f2915",
  w0: "#2a5a8a", w1: "#3f7fb8", w2: "#5b9ed0", w3: "#9fd0ee",
};
const BAYER = [[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]];

function valueNoise(seed) {
  const rnd = mulberry32(seed), N = 64, grid = Array.from({ length: N * N }, rnd);
  const at = (x, y) => grid[((y % N + N) % N) * N + ((x % N + N) % N)];
  return (x, y) => {
    const xi = Math.floor(x), yi = Math.floor(y), xf = x - xi, yf = y - yi;
    const u = xf * xf * (3 - 2 * xf), v = yf * yf * (3 - 2 * yf);
    return lerp(lerp(at(xi, yi), at(xi + 1, yi), u), lerp(at(xi, yi + 1), at(xi + 1, yi + 1), u), v);
  };
}

function pxCircle(g, cx, cy, r, color) {
  g.fillStyle = color;
  for (let y = -r; y <= r; y++) {
    const w = Math.floor(Math.sqrt(r * r - y * y + r * 0.8));
    g.fillRect(Math.round(cx - w), Math.round(cy + y), w * 2 + 1, 1);
  }
}
function pxEllipse(g, cx, cy, rx, ry, color) {
  g.fillStyle = color;
  for (let y = -ry; y <= ry; y++) {
    const w = Math.floor(rx * Math.sqrt(Math.max(0, 1 - (y * y) / (ry * ry + 0.6))));
    g.fillRect(Math.round(cx - w), Math.round(cy + y), w * 2 + 1, 1);
  }
}

const PLOT = { w: 30, h: 16, cx: 60, cy: 40 }, YARD = { cx: 19, cy: 17 };
// where a plot's sub-agents stand (their feet, from the plot's corner): above it, below it, then on its right
const SUB_SPOTS = [[5, -2], [25, -2], [5, PLOT.h + 9], [25, PLOT.h + 9], [15, -2], [15, PLOT.h + 9], [PLOT.w + 7, 5], [PLOT.w + 7, PLOT.h + 1]];
const MINIS_PER_PLOT = SUB_SPOTS.length;
const CROW_LANE = 22; // the scarecrow stands inside the field's fence, bottom left

function layoutWorld(W, H, opts = {}) {
  const T = clamp(Math.round(Math.min(W, H) * 0.14), 20, 40);
  const play = { x0: T + 2, y0: T + 8, x1: W - T - 2, y1: H - T - 4 };
  const portrait = H > W * 1.15;
  const barn = { w: 46, h: 44 };
  barn.x = opts.barnRight && !portrait ? play.x1 - barn.w - 6 : play.x0 + 6; barn.y = play.y0 - 2;
  const board = { x: opts.barnRight && !portrait ? barn.x - 36 : barn.x + barn.w + 10, y: barn.y + 16, w: 26, h: 20 };
  // opts.clearCenter keeps the middle free for a title (the landing page): the field goes to the right
  const reserve = opts.reserve || null;
  const cols = portrait || reserve ? 2 : (play.x1 - play.x0 > 300 ? 4 : 3), rows = portrait && reserve ? 2 : portrait || reserve ? 3 : 2;
  const pw = PLOT.w, ph = PLOT.h, gx = 22, gy = 18;
  const fw = cols * pw + (cols - 1) * gx, fh = rows * ph + (rows - 1) * gy;
  const fx = portrait ? Math.round((W - fw) / 2 + 8)
    : reserve ? Math.round(Math.min((reserve.x + reserve.w + play.x1) / 2 - fw / 2 + 6, play.x1 - fw - 4)) : Math.round(play.x1 - fw - 12);
  const fy = portrait ? Math.round(reserve ? reserve.y + reserve.h + 14 : barn.y + barn.h + 34) : Math.round(Math.max((play.y0 + play.y1) / 2 - fh / 2 + 12, opts.barnRight ? barn.y + barn.h + 20 : 0));
  const plots = [];
  for (let r = 0; r < rows; r++) for (let c = 0; c < cols; c++)
    plots.push({ x: fx + c * (pw + gx), y: fy + r * (ph + gy), w: pw, h: ph, i: plots.length });
  const field = { x: fx - 12, y: fy - 8, w: fw + 16, h: fh + 14 };
  const pond = { cx: play.x0 + 30, cy: play.y1 - 14, rx: 24, ry: 11 };
  const rest = { x: barn.x + 10, y: barn.y + barn.h + 12 };
  const door = { x: barn.x + barn.w / 2, y: barn.y + barn.h + 2 };
  return { W, H, T, play, portrait, barn, board, plots, field, pond, rest, door, rocks: [], decor: [], paths: [], reserve, yard: null, crow: null, z: 0 };
}

/** The real farm's layout, grown to fit it: a fenced field with a plot for every busy Claude (the scarecrow keeps its
 * corner), the barn with a yard where resting Claudes nap, the pond, and dirt paths between them. It tries field and yard
 * widths and keeps the one that shows the whole farm biggest in this window (`view`: the window in CSS px, the HUD's
 * insets, S, the classic zoom, and snap(), which rounds a zoom down to one that draws whole pixels). */
function layoutFarm(need, view) {
  const nP = Math.max(6, need.plots), nY = Math.max(5, need.yard);
  const avW = Math.max(120, view.w - view.left - view.right), avH = Math.max(120, view.h - view.top - view.bottom);
  const portrait = view.h > view.w * 1.15, GAP = 16;
  let best = null;
  for (let fc = 1; fc <= Math.min(nP, 30); fc++) {
    const fr = Math.ceil(nP / fc);
    if (fc > 1 && Math.ceil(nP / (fc - 1)) === fr) continue; // same rows as a narrower field
    for (const yc of [5, 7, 9, 12, 16, 22, 30]) {
      if (yc > 5 && yc > nY + 4) break;
      const fw = fc * PLOT.cx - (PLOT.cx - PLOT.w) + 35 + CROW_LANE, fh = fr * PLOT.cy - (PLOT.cy - PLOT.h) + 28;
      const yw = yc * YARD.cx + 8, yh = Math.ceil(nY / yc) * YARD.cy + 10;
      const lw = Math.max(yw, 96), lh = 8 + 44 + 16 + yh + 40; // weathervane, barn, path, yard, pond
      const cw = portrait ? Math.max(lw, fw) : lw + GAP + fw, ch = portrait ? lh + 8 + fh : Math.max(lh, fh);
      const z = Math.min(avW / cw, avH / ch);
      if (!best || z > best.z + 1e-6) best = { z, fc, fr, yc, cw, ch, fw, fh, lw, lh, yw, yh };
    }
  }
  const b = best, z = view.snap(Math.min(view.S, b.z));
  const T = clamp(Math.round(Math.min(view.w, view.h) / view.S * 0.14), 20, 40);
  const x0 = Math.max(T + 4, Math.round(view.left / z + (avW / z - b.cw) / 2)), y0 = Math.max(T + 6, Math.round(view.top / z + (avH / z - b.ch) / 2));
  const W = Math.max(Math.ceil(view.w / z), x0 + b.cw + T + 4), H = Math.max(Math.ceil(view.h / z), y0 + b.ch + T + 4);
  const play = { x0: T + 2, y0: T + 8, x1: W - T - 2, y1: H - T - 4 };
  const colX = portrait ? x0 + Math.round((b.cw - b.lw) / 2) : x0;
  const barn = { w: 46, h: 44, x: colX + Math.round((b.lw - 46) / 2), y: y0 + 8 };
  const yard = { x: colX + Math.round((b.lw - b.yw) / 2), y: barn.y + barn.h + 16, cols: b.yc, w: b.yw, h: b.yh, n: nY };
  const pond = { cx: colX + Math.round(b.lw / 2), cy: yard.y + yard.h + 21, rx: Math.min(30, Math.round(b.lw / 2) - 6), ry: 11 };
  const field = portrait ? { x: x0 + Math.round((b.cw - b.fw) / 2), y: y0 + b.lh + 8, w: b.fw, h: b.fh }
    : { x: x0 + b.lw + GAP, y: y0 + Math.round((b.ch - b.fh) / 2), w: b.fw, h: b.fh };
  const fx = field.x + 19 + CROW_LANE, fy = field.y + 15;
  const plots = [];
  for (let i = 0; i < b.fc * b.fr; i++) plots.push({ x: fx + (i % b.fc) * PLOT.cx, y: fy + Math.floor(i / b.fc) * PLOT.cy, w: PLOT.w, h: PLOT.h, i });
  const crow = { x: field.x + 13, y: field.y + field.h - 9 };
  const door = { x: barn.x + barn.w / 2, y: barn.y + barn.h + 2 };
  // dirt paths: the barn door to the yard's gate, and on to the field's gate
  const py = door.y + 7, paths = [[{ x: door.x, y: door.y - 1 }, { x: door.x, y: yard.y + 2 }]];
  let gate, sign;
  if (!portrait) {
    const gy = clamp(py, field.y + 12, Math.max(field.y + 12, field.y + field.h - 38)), mx = field.x - 8;
    paths.push(gy === py ? [{ x: door.x, y: py }, { x: field.x + 4, y: py }] : [{ x: door.x, y: py }, { x: mx, y: py }, { x: mx, y: gy }, { x: field.x + 4, y: gy }]);
    gate = { side: "left", at: gy }; sign = { x: field.x - 7, y: gy - 9 };
  } else {
    const rx = Math.min(W - T - 8, Math.max(yard.x + yard.w, pond.cx + pond.rx) + 10), gx = clamp(rx, field.x + CROW_LANE + 16, field.x + field.w - 12);
    paths.push([{ x: door.x, y: py }, { x: rx, y: py }, { x: rx, y: field.y - 6 }, { x: gx, y: field.y - 6 }, { x: gx, y: field.y + 4 }]);
    gate = { side: "top", at: gx }; sign = { x: gx + 12, y: field.y - 10 };
  }
  const board = { x: barn.x + barn.w + 10, y: barn.y + 16, w: 26, h: 20 };
  const content = { x: x0, y: y0, w: b.cw, h: b.ch };
  return { W, H, T, play, portrait, barn, board, plots, field, gate, pond, rest: yard, yard, door, rocks: [], decor: [], paths, sign, reserve: null, crow, content, z,
    cap: { plots: plots.length, yard: b.yc * Math.ceil(nY / b.yc) } };
}
/** Where the n-th resting Claude sits in the yard. */
function yardSpot(L, i) {
  const Y = L.yard;
  if (!Y) return { x: L.barn.x - 2 + (i % 5) * 19, y: L.barn.y + L.barn.h + 16 + Math.floor(i / 5) * 16 };
  return { x: Y.x + 13 + (i % Y.cols) * YARD.cx, y: Y.y + 19 + Math.floor(i / Y.cols) * YARD.cy };
}

function blocked(L, x, y) {
  const inR = (r, pad = 0) => x > r.x - pad && x < r.x + r.w + pad && y > r.y - pad && y < r.y + r.h + pad + 4;
  if (inR(L.barn, 8) || (Scene.passive && inR(L.board, 6)) || inR(L.field, 2) || (L.reserve && inR(L.reserve, 12))) return true;
  if (L.yard && inR(L.yard, 6)) return true;
  if (L.crow && Math.abs(x - L.crow.x) < 12 && y > L.crow.y - 24 && y < L.crow.y + 6) return true;
  if (L.sign && Math.abs(x - L.sign.x) < 8 && y > L.sign.y - 4 && y < L.sign.y + 14) return true;
  if (((x - L.pond.cx) / (L.pond.rx + 8)) ** 2 + ((y - L.pond.cy) / (L.pond.ry + 6)) ** 2 < 1) return true;
  if (L.decor.some(d => d.r && Math.abs(x - d.x) < d.r + 5 && y > d.y - d.r - 2 && y < d.y + 5)) return true;
  return L.rocks.some(r => Math.abs(x - r.x) < r.w / 2 + 6 && y > r.y - 4 && y < r.y + r.h + 4);
}
const onPath = (L, x, y, pad = 5) => L.paths.some(pts => pts.some((p, i) => i > 0 && x > Math.min(p.x, pts[i - 1].x) - pad && x < Math.max(p.x, pts[i - 1].x) + pad && y > Math.min(p.y, pts[i - 1].y) - pad && y < Math.max(p.y, pts[i - 1].y) + pad));

// ---- the background's pieces, drawn at HD pixels (world x HD) on the background canvas
const hdRect = (g, x, y, w, hgt, c) => { g.fillStyle = c; g.fillRect(x, y, w, hgt); };
function drawTreeHD(g, x, y, r, rnd) {
  pxEllipse(g, x + 4, y + r - 2, r + 3, Math.max(5, Math.round(r / 3)), "rgba(20,32,10,.32)");
  hdRect(g, x - 4, y + r - 15, 8, 15, G.trunk); hdRect(g, x - 4, y + r - 15, 2, 15, "#5e4330"); hdRect(g, x + 2, y + r - 15, 2, 15, "#35241a");
  hdRect(g, x - 6, y + r - 2, 3, 2, G.trunk); hdRect(g, x + 3, y + r - 2, 3, 2, "#35241a");
  const blobs = [[0, 0, 1], [-0.5, -0.15, 0.62], [0.5, -0.1, 0.6], [0, -0.52, 0.62], [-0.3, 0.3, 0.55], [0.35, 0.32, 0.5]].map(([dx, dy, k]) => [x + dx * r, y + dy * r, Math.max(3, r * k)]);
  for (const [bx, by, br] of blobs) pxCircle(g, bx, by, br + 2, G.t0);
  for (const [bx, by, br] of blobs) pxCircle(g, bx, by, br, G.t1);
  for (const [bx, by, br] of blobs) pxCircle(g, bx - br * 0.18, by - br * 0.22, br * 0.78, G.t2);
  for (const [bx, by, br] of blobs) if (by < y + r * 0.2) pxCircle(g, bx - br * 0.32, by - br * 0.38, br * 0.46, G.t3);
  for (const [bx, by, br] of blobs.slice(1, 4)) pxCircle(g, bx - br * 0.4, by - br * 0.46, br * 0.2, G.t4);
  for (let i = 0; i < r * 2.2; i++) { // leaves
    const a = rnd() * Math.PI * 2, d = rnd() * r * 0.95, lx = Math.round(x + Math.cos(a) * d), ly = Math.round(y + Math.sin(a) * d * 0.9);
    hdRect(g, lx, ly, 2, 1, ly < y ? G.t4 : G.t1); if (ly < y - r * 0.3) hdRect(g, lx, ly - 1, 1, 1, G.t5);
  }
}
function drawRockHD(g, r) { // a mossy boulder; some have Claude's spark carved in
  const cx = Math.round(r.x * HD), rx = r.w, ry = Math.round(r.h * 0.9), cy = Math.round((r.y + r.h) * HD - ry);
  pxEllipse(g, cx + 3, cy + ry, rx + 2, 4, "rgba(20,32,10,.38)");
  pxEllipse(g, cx, cy, rx + 1, ry + 1, G.r0); pxEllipse(g, cx, cy, rx, ry, G.r1);
  pxEllipse(g, cx - Math.round(rx * 0.15), cy - Math.round(ry * 0.2), Math.round(rx * 0.72), Math.round(ry * 0.62), G.r2);
  pxEllipse(g, cx - Math.round(rx * 0.35), cy - Math.round(ry * 0.45), Math.round(rx * 0.32), Math.round(ry * 0.22), G.r3);
  hdRect(g, cx - Math.round(rx * 0.5), cy - Math.round(ry * 0.55), 2, 1, G.r4);
  pxEllipse(g, cx + Math.round(rx * 0.3), cy - ry + 1, Math.round(rx * 0.32), 1, "#5f8a34"); hdRect(g, cx + Math.round(rx * 0.2), cy - ry, 3, 1, "#86b04a");
  if (Math.round(r.x + r.y) % 2) SPARK.forEach((row, j) => [...row].forEach((ch, i) => { if (ch !== "." && (i + j) % 2 === 0) hdRect(g, cx - 4 + i, cy - 3 + j, 1, 1, "#45454e"); }));
}
function drawHay(g, x, y) { // a bale, 18 x 14 HD
  hdRect(g, x, y, 18, 14, "#7a5a1a"); hdRect(g, x + 1, y + 1, 16, 12, "#e0b64e"); hdRect(g, x + 1, y + 1, 16, 3, "#f0cf7a");
  hdRect(g, x + 1, y + 10, 16, 3, "#c7973a"); hdRect(g, x + 5, y + 1, 2, 12, "#a8782a"); hdRect(g, x + 12, y + 1, 2, 12, "#a8782a");
  for (let i = 0; i < 6; i++) hdRect(g, x + 2 + i * 3, y - 1 + (i % 2), 1, 2, "#f0cf7a");
}
function drawBarnHD(g, b) {
  const x = b.x * HD, y = b.y * HD, w = b.w * HD, hh = b.h * HD, roofH = 32;
  pxEllipse(g, x + w / 2 + 6, y + hh + 1, w / 2 + 12, 8, "rgba(20,32,10,.36)");
  // walls: red planks, white corner trim, a stone footing
  const wy = y + roofH - 2, wh = hh - roofH + 2;
  hdRect(g, x, wy, w, wh, "#3a1410");
  for (let px = x + 2, i = 0; px < x + w - 2; px += 6, i++) { hdRect(g, px, wy, 6, wh - 2, i % 2 ? "#a8432f" : "#b34a33"); hdRect(g, px, wy, 1, wh - 2, "#8e3624"); }
  hdRect(g, x + 2, wy, w - 4, 2, "#c95c42");
  for (let i = 0; i < 18; i++) hdRect(g, x + 4 + ((i * 37) % (w - 10)), wy + 6 + ((i * 23) % (wh - 12)), 2, 1, "#933826");
  hdRect(g, x + 2, wy, 4, wh - 2, "#efe4cf"); hdRect(g, x + w - 6, wy, 4, wh - 2, "#d6c8ad");
  hdRect(g, x, y + hh - 5, w, 5, "#6f6a66"); for (let px = x + 2; px < x + w - 2; px += 7) hdRect(g, px, y + hh - 5, 5, 3, "#8d8783");
  // roof: a stepped gable of shingles with white trim
  for (let i = 0; i < roofH; i++) {
    const inset = Math.max(0, Math.round((roofH - i) * 0.95) - 6), rx = x - 4 + inset, rw = w + 8 - inset * 2;
    hdRect(g, rx, y + i, rw, 1, "#2d1511");
    hdRect(g, rx + 2, y + i, Math.max(0, rw - 4), 1, i % 4 === 3 ? "#4a2019" : "#6a3024");
    if (i % 4 !== 3) for (let sx = rx + 4 + ((i >> 2) % 2) * 4; sx < rx + rw - 4; sx += 8) hdRect(g, sx, y + i, 1, 1, "#4f231b");
    hdRect(g, rx + 2, y + i, 2, 1, "#f3ead8"); hdRect(g, rx + rw - 4, y + i, 2, 1, "#d6c8ad");
  }
  hdRect(g, x - 4, y + roofH - 2, w + 8, 2, "#f3ead8");
  // loft window with hay in it
  const lx = x + w / 2 - 10, ly = y + 10;
  hdRect(g, lx, ly, 20, 16, "#f3ead8"); hdRect(g, lx + 2, ly + 2, 16, 12, "#2d1a10"); hdRect(g, lx + 2, ly + 9, 16, 5, "#e0b64e");
  for (let i = 0; i < 5; i++) hdRect(g, lx + 3 + i * 3, ly + 7 + (i % 2), 1, 3, "#f0cf7a");
  hdRect(g, lx + 9, ly + 2, 2, 12, "#f3ead8");
  // the big door with its white X
  const dw = 34, dh = wh - 9, dx = x + w / 2 - dw / 2, dy = wy + 6;
  hdRect(g, dx - 2, dy - 2, dw + 4, dh + 2, "#f3ead8"); hdRect(g, dx, dy, dw, dh, "#7a2c1f");
  for (let px = dx + 3; px < dx + dw; px += 4) hdRect(g, px, dy, 1, dh, "#6a2519");
  for (let i = 0; i < dh; i++) { const t = Math.round((i / dh) * (dw / 2 - 2)); hdRect(g, dx + t, dy + i, 2, 1, "#f3ead8"); hdRect(g, dx + dw / 2 - 2 - t, dy + i, 2, 1, "#f3ead8"); hdRect(g, dx + dw / 2 + t, dy + i, 2, 1, "#f3ead8"); hdRect(g, dx + dw - 2 - t, dy + i, 2, 1, "#f3ead8"); }
  hdRect(g, dx + dw / 2 - 1, dy, 2, dh, "#f3ead8");
  hdRect(g, dx - 1, dy + 4, 3, 2, "#2d1a10"); hdRect(g, dx - 1, dy + dh - 7, 3, 2, "#2d1a10"); hdRect(g, dx + dw - 2, dy + 4, 3, 2, "#2d1a10"); hdRect(g, dx + dw - 2, dy + dh - 7, 3, 2, "#2d1a10");
  // a lantern and the weathervane: Claude's spark on the ridge
  hdRect(g, x + w / 2 + 22, wy + 8, 4, 6, "#2d1a10"); hdRect(g, x + w / 2 + 23, wy + 9, 2, 4, "#ffd27a");
  const vx = x + w / 2, vy = y - 12;
  hdRect(g, vx - 1, vy + 6, 2, 8, "#2d1511");
  SPARK.forEach((row, j) => [...row].forEach((ch, i) => { if (ch !== ".") hdRect(g, vx - 5 + i, vy - 4 + j, 1, 1, ch === "w" ? "#ffd59e" : "#d97757"); }));
  drawHay(g, x + w + 2, y + hh - 14); drawHay(g, x - 20, y + hh - 14); drawHay(g, x + w + 6, y + hh - 26);
}
function drawPlotHD(g, p) {
  const x = p.x * HD, y = p.y * HD, w = p.w * HD, hh = p.h * HD;
  hdRect(g, x - 2, y, w + 4, hh + 2, G.soilo); hdRect(g, x, y - 2, w, hh + 6, G.soilo);
  hdRect(g, x, y, w, hh + 2, "#4f3420");
  hdRect(g, x, y, w, hh, G.soil);
  for (let yy = y + 3; yy < y + hh - 1; yy += 6) { hdRect(g, x + 2, yy, w - 4, 2, G.soil3); hdRect(g, x + 2, yy + 2, w - 4, 2, G.soil2); hdRect(g, x + 3, yy, w - 8, 1, "#8e6a46"); }
  for (let i = 0; i < 10; i++) hdRect(g, x + 3 + ((i * 17) % (w - 6)), y + 2 + ((i * 11) % (hh - 4)), 1, 1, i % 2 ? "#3f2915" : "#9a7650");
}
function drawPathHD(g, pts, rnd) {
  const seg = (a, b, pad, c) => { const x0 = Math.min(a.x, b.x) * HD - pad, y0 = Math.min(a.y, b.y) * HD - pad; hdRect(g, x0, y0, Math.abs(a.x - b.x) * HD + pad * 2, Math.abs(a.y - b.y) * HD + pad * 2, c); };
  for (let i = 1; i < pts.length; i++) seg(pts[i - 1], pts[i], 8, "#7f6a3d");
  for (let i = 1; i < pts.length; i++) seg(pts[i - 1], pts[i], 7, "#a88a58");
  for (let i = 1; i < pts.length; i++) seg(pts[i - 1], pts[i], 5, "#b69866");
  for (let i = 1; i < pts.length; i++) {
    const a = pts[i - 1], b = pts[i], n = Math.round((Math.abs(a.x - b.x) + Math.abs(a.y - b.y)) * 0.8);
    for (let k = 0; k < n; k++) { const t = rnd(), px = Math.round(lerp(a.x, b.x, t) * HD + (rnd() - 0.5) * 11), py = Math.round(lerp(a.y, b.y, t) * HD + (rnd() - 0.5) * 11); hdRect(g, px, py, 2, 1, rnd() < 0.5 ? "#cdb07c" : "#8b7045"); }
  }
}
function drawFenceHD(g, F, gate) {
  const x0 = F.x * HD, y0 = F.y * HD, x1 = (F.x + F.w) * HD, y1 = (F.y + F.h) * HD, gw = 16 * HD;
  const gl = gate?.side === "left" ? [gate.at * HD - gw / 2, gate.at * HD + gw / 2] : null, gt = gate?.side === "top" ? [gate.at * HD - gw / 2, gate.at * HD + gw / 2] : null;
  const post = (x, y) => { hdRect(g, x - 2, y - 11, 5, 13, "#3a2616"); hdRect(g, x - 1, y - 10, 3, 11, "#8a6038"); hdRect(g, x - 1, y - 10, 3, 2, "#b08250"); hdRect(g, x - 2, y + 2, 6, 2, "rgba(20,32,10,.3)"); };
  const hrail = (xa, xb, y) => { hdRect(g, xa, y - 8, xb - xa, 2, "#3a2616"); hdRect(g, xa, y - 7, xb - xa, 1, "#a37446"); hdRect(g, xa, y - 4, xb - xa, 2, "#3a2616"); hdRect(g, xa, y - 3, xb - xa, 1, "#8a6038"); };
  const vrail = (x, ya, yb) => { hdRect(g, x - 1, ya, 3, yb - ya, "#3a2616"); hdRect(g, x, ya, 1, yb - ya, "#8a6038"); };
  // top edge (maybe with a gate), the sides, then the bottom edge
  if (gt) { hrail(x0, gt[0], y0); hrail(gt[1], x1, y0); } else hrail(x0, x1, y0);
  if (gl) { vrail(x0, y0, gl[0]); vrail(x0, gl[1], y1); } else vrail(x0, y0, y1);
  vrail(x1, y0, y1);
  for (let x = x0; x <= x1; x += 20) if (!gt || x < gt[0] - 2 || x > gt[1] + 2) post(Math.min(x, x1), y0);
  for (let y = y0 + 20; y < y1; y += 20) { if (!gl || y < gl[0] - 2 || y > gl[1] + 2) post(x0, y); post(x1, y); }
  if (gt) { post(gt[0], y0); post(gt[1], y0); } if (gl) { post(x0, gl[0]); post(x0, gl[1]); }
  hrail(x0, x1, y1);
  for (let x = x0; x <= x1; x += 20) post(Math.min(x, x1), y1);
}
function drawYardHD(g, Y, rnd) {
  const x = Y.x * HD, y = Y.y * HD, w = Y.w * HD, hh = Y.h * HD;
  hdRect(g, x - 2, y - 2, w + 4, hh + 4, "rgba(90,70,30,.3)");
  hdRect(g, x, y, w, hh, "#b99a55");
  for (let i = 0; i < (w * hh) / 18; i++) hdRect(g, Math.round(x + rnd() * (w - 3)), Math.round(y + rnd() * (hh - 1)), 3, 1, ["#d8bb6c", "#a4843f", "#c9a95c", "#e6cc84"][i % 4]);
  const gate = [Math.round(((Y.x + Y.w / 2) * HD) - 16), Math.round(((Y.x + Y.w / 2) * HD) + 16)];
  const post = (px, py) => { hdRect(g, px - 2, py - 9, 5, 12, "#3a2616"); hdRect(g, px - 1, py - 8, 3, 10, "#8a6038"); hdRect(g, px - 1, py - 8, 3, 2, "#b08250"); };
  const rail = (xa, xb, py) => { hdRect(g, xa, py - 6, xb - xa, 2, "#3a2616"); hdRect(g, xa, py - 5, xb - xa, 1, "#a37446"); hdRect(g, xa, py - 2, xb - xa, 2, "#3a2616"); };
  rail(x - 4, gate[0], y); rail(gate[1], x + w + 4, y);
  for (const px of [x - 4, x + w + 3]) { hdRect(g, px - 1, y, 3, hh, "#3a2616"); hdRect(g, px, y, 1, hh, "#8a6038"); }
  rail(x - 4, x + w + 4, y + hh);
  for (let px = x - 4; px <= x + w + 4; px += 18) { if (px < gate[0] - 3 || px > gate[1] + 3) post(px, y); post(px, y + hh); }
  post(gate[0], y); post(gate[1], y);
  for (let py = y + 18; py < y + hh; py += 18) { post(x - 4, py); post(x + w + 3, py); }
}
function drawPondHD(g, P, rnd) {
  const cx = P.cx * HD, cy = P.cy * HD, rx = P.rx * HD, ry = P.ry * HD;
  pxEllipse(g, cx, cy + 2, rx + 7, ry + 6, "#8e7a4c");
  pxEllipse(g, cx, cy + 1, rx + 5, ry + 4, "#c8b27a");
  pxEllipse(g, cx, cy + 1, rx + 2, ry + 2, "#3d5a22");
  pxEllipse(g, cx, cy, rx, ry, G.w0);
  pxEllipse(g, cx, cy + 2, rx - 1, ry - 1, G.w1);
  pxEllipse(g, cx - 6, cy - 3, rx - 14, ry - 8, G.w2);
  for (let i = 0; i < 10; i++) hdRect(g, Math.round(cx - rx * 0.6 + rnd() * rx * 1.2), Math.round(cy - ry * 0.5 + rnd() * ry), 4 + Math.round(rnd() * 4), 1, G.w3);
  for (const [dx, dy, s] of [[-0.45, 0.2, 1], [0.35, -0.15, 0.8], [0.15, 0.45, 0.7]]) { // lily pads
    const px = cx + dx * rx, py = cy + dy * ry, r = 5 * s + 2;
    pxEllipse(g, px, py, r + 1, r * 0.6 + 1, "#1f4d1d"); pxEllipse(g, px, py, r, r * 0.6, "#4f9a3a"); hdRect(g, px, py - 1, Math.ceil(r), 2, G.w1); hdRect(g, px - r * 0.5, py - 2, 3, 1, "#7cc35a");
  }
  hdRect(g, cx - rx * 0.45 - 1, cy + ry * 0.2 - 4, 3, 3, "#f59cc0"); hdRect(g, cx - rx * 0.45, cy + ry * 0.2 - 3, 1, 1, "#fff4c2");
  for (const s of [-1, 1]) for (let i = 0; i < 4; i++) { // reeds and cattails
    const rxp = Math.round(cx + s * (rx - 6 + i * 4)), top = cy - 10 - i * 3 - (i % 2) * 3;
    hdRect(g, rxp, top, 1, cy + 4 - top, i % 2 ? "#2f5a1f" : "#3f7a2a"); hdRect(g, rxp - 1, top - 1, 3, 5, "#6b4a2b"); hdRect(g, rxp, top - 3, 1, 2, "#3f7a2a");
  }
}
function drawSignHD(g, s) {
  const x = s.x * HD, y = s.y * HD;
  hdRect(g, x - 1, y + 6, 3, 18, "#3a2616"); hdRect(g, x, y + 6, 1, 18, "#8a6038"); hdRect(g, x - 3, y + 23, 8, 2, "rgba(20,32,10,.35)");
  hdRect(g, x - 11, y - 2, 22, 11, "#3a2616"); hdRect(g, x - 10, y - 1, 20, 9, "#c9964a"); hdRect(g, x - 10, y - 1, 20, 2, "#dcae62");
  hdRect(g, x - 7, y + 3, 9, 2, "#3a2616"); hdRect(g, x + 2, y + 1, 1, 6, "#3a2616"); hdRect(g, x + 3, y + 2, 1, 4, "#3a2616"); hdRect(g, x + 4, y + 3, 1, 2, "#3a2616"); // an arrow: to the field
}
const FLOWER_C = ["#f4f1e8", "#f5c542", "#f59cc0", "#b9d9ff", "#ff8f6b", "#c99bf0"];
function drawDecorHD(g, d, rnd) {
  const x = Math.round(d.x * HD), y = Math.round(d.y * HD);
  if (d.kind === "flowers") for (let i = 0; i < 3 + Math.floor(rnd() * 4); i++) {
    const fx = x + Math.round((rnd() - 0.5) * 16), fy = y + Math.round((rnd() - 0.5) * 8), c = FLOWER_C[(d.c + i) % FLOWER_C.length];
    hdRect(g, fx, fy, 1, 3, "#3f7a2a"); hdRect(g, fx - 1, fy - 2, 3, 1, c); hdRect(g, fx, fy - 3, 1, 3, c); hdRect(g, fx, fy - 2, 1, 1, "#f5c542");
  }
  if (d.kind === "bush") {
    pxEllipse(g, x + 2, y + 1, 12, 4, "rgba(20,32,10,.3)");
    for (const [dx, dy, r] of [[-5, -4, 6], [5, -4, 6], [0, -8, 6], [0, -3, 7]]) pxCircle(g, x + dx, y + dy, r + 1, G.t0);
    for (const [dx, dy, r] of [[-5, -4, 6], [5, -4, 6], [0, -8, 6], [0, -3, 7]]) { pxCircle(g, x + dx, y + dy, r, G.t2); pxCircle(g, x + dx - 1, y + dy - 2, r * 0.55, G.t3); }
    if (d.c % 2) for (const [dx, dy] of [[-6, -5], [3, -9], [6, -3], [-1, -2]]) { hdRect(g, x + dx, y + dy, 2, 2, "#d6334a"); hdRect(g, x + dx, y + dy, 1, 1, "#ff8a9a"); }
  }
  if (d.kind === "pumpkin") for (const [dx, s] of [[-5, 0.8], [4, 1]]) {
    const px = x + dx, r = 5 * s;
    pxEllipse(g, px + 1, y + 1, r + 2, 2, "rgba(20,32,10,.3)"); pxEllipse(g, px, y - r * 0.8, r + 2, r * 0.8 + 1, "#6b2f10"); pxEllipse(g, px, y - r * 0.8, r + 1, r * 0.8, "#e07b24");
    hdRect(g, px - 1, y - r * 1.6, 2, r * 1.6 - 1, "#c4631a"); hdRect(g, px - r * 0.6, y - r * 1.2, 2, 3, "#f5a04a"); hdRect(g, px - 1, y - r * 1.6 - 3, 2, 3, "#4f6b25");
  }
  if (d.kind === "log") {
    pxEllipse(g, x + 1, y + 1, 13, 3, "rgba(20,32,10,.3)"); hdRect(g, x - 12, y - 8, 22, 9, "#3a2616"); hdRect(g, x - 11, y - 7, 20, 7, "#7a5433"); hdRect(g, x - 11, y - 7, 20, 2, "#9a6d45");
    pxEllipse(g, x + 10, y - 4, 3, 4, "#3a2616"); pxEllipse(g, x + 10, y - 4, 2, 3, "#d8b27a"); hdRect(g, x + 10, y - 4, 1, 1, "#9a6d45");
  }
  if (d.kind === "mushrooms") for (const [dx, s] of [[-3, 1], [3, 0.7], [0, 0.55]]) {
    const px = x + dx, r = 3 * s + 1;
    hdRect(g, px - 1, y - 4 * s, 2, 4 * s, "#f4ecd8"); pxEllipse(g, px, y - 4 * s - 1, r + 1, r * 0.7 + 1, "#5c130e"); pxEllipse(g, px, y - 4 * s - 1, r, r * 0.7, "#d6334a"); hdRect(g, px - 1, y - 4 * s - 2, 1, 1, "#fff"); hdRect(g, px + 1, y - 4 * s - 1, 1, 1, "#fff");
  }
}

function buildWorld(L, seed = 7) {
  const W = L.W, H = L.H, rnd = mulberry32(seed);
  const bg = canvas(W * HD, H * HD), g = bg.getContext("2d");
  g.imageSmoothingEnabled = false;
  // grass: two octaves of value noise, ordered dithering between five tones, a worn clearing round the farm; at world
  // pixels, blown up to HD, with HD blades and flowers on top
  const lo = canvas(W, H), lg = lo.getContext("2d");
  const n1 = valueNoise(seed + 1), n2 = valueNoise(seed + 2), img = lg.createImageData(W, H), d = img.data;
  const tones = [G.g0, G.g1, G.g2, G.g3, G.g4, G.worn].map(rgbOf);
  const C = L.content || { x: L.play.x0, y: L.play.y0, w: L.play.x1 - L.play.x0, h: L.play.y1 - L.play.y0 };
  const cx = C.x + C.w / 2, cy = C.y + C.h / 2 + 6, rx = Math.max(60, C.w * 0.62), ry = Math.max(50, C.h * 0.64);
  for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
    const e = ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2;
    let t = n1(x / 22, y / 22) * 0.62 + n2(x / 7, y / 7) * 0.38 + (1 - Math.min(1.4, e)) * 0.3;
    t += (BAYER[y & 3][x & 3] / 16 - 0.5) * 0.14;
    const k = clamp(Math.floor((t - 0.18) * 5.2), 0, 5), i = (y * W + x) * 4;
    [d[i], d[i + 1], d[i + 2]] = tones[k]; d[i + 3] = 255;
  }
  lg.putImageData(img, 0, 0);
  g.drawImage(lo, 0, 0, W * HD, H * HD);
  for (let i = 0; i < (W * H) / 40; i++) { // blades and flecks
    const x = Math.floor(rnd() * W * HD), y = Math.floor(rnd() * H * HD), r = rnd();
    if (r < 0.55) { const c = rnd() < 0.5 ? G.g0 : G.g1; hdRect(g, x, y, 1, 3, c); hdRect(g, x + 2, y + 1, 1, 2, c); hdRect(g, x + 1, y + 2, 1, 1, c); }
    else if (r < 0.85) hdRect(g, x, y, 1, 2, rnd() < 0.5 ? G.g4 : "#93b552");
    else if (!blocked(L, x / HD, y / HD)) { const c = FLOWER_C[Math.floor(rnd() * FLOWER_C.length)]; hdRect(g, x - 1, y, 3, 1, c); hdRect(g, x, y - 1, 1, 3, c); hdRect(g, x, y, 1, 1, "#f5e27a"); }
  }
  for (const pts of L.paths || []) drawPathHD(g, pts, rnd);
  drawPondHD(g, L.pond, rnd);
  // rocks and bits of nature in the meadow, where nothing else is
  const clear = (x, y, pad) => !blocked(L, x, y) && !onPath(L, x, y, pad) && Math.hypot(x - L.door.x, y - L.door.y) > 30
    && !(C && L.content && x > C.x - 4 && x < C.x + C.w + 4 && y > C.y - 4 && y < C.y + C.h + 6);
  for (let tries = 0, want = clamp(Math.round((W * H) / 45000), 2, 10); L.rocks.length < want && tries < 400; tries++) {
    const w = 12 + Math.floor(rnd() * 8), r = { x: L.play.x0 + 10 + rnd() * (L.play.x1 - L.play.x0 - 20), y: L.play.y0 + rnd() * (L.play.y1 - L.play.y0 - 12), w, h: Math.round(w * 0.75) };
    if (clear(r.x, r.y, 10) && clear(r.x, r.y + r.h, 10) && L.rocks.every(o => Math.hypot(o.x - r.x, o.y - r.y) > 44)) L.rocks.push(r);
  }
  const KINDS = [["flowers", 0.5, 0], ["bush", 0.27, 7], ["mushrooms", 0.09, 0], ["pumpkin", 0.09, 6], ["log", 0.05, 8]];
  for (let tries = 0, want = clamp(Math.round((W * H) / 2600), 8, 70); L.decor.length < want && tries < 900; tries++) {
    const x = L.play.x0 + 8 + rnd() * (L.play.x1 - L.play.x0 - 16), y = L.play.y0 + 10 + rnd() * (L.play.y1 - L.play.y0 - 14);
    let p = rnd(), kind = KINDS[0];
    for (const k of KINDS) { if (p < k[1]) { kind = k; break; } p -= k[1]; }
    if (!clear(x, y, 9) || L.decor.some(o => Math.hypot(o.x - x, o.y - y) < 22) || L.rocks.some(o => Math.hypot(o.x - x, o.y - y) < 20)) continue;
    L.decor.push({ kind: kind[0], x, y, r: kind[2], c: Math.floor(rnd() * 6) });
  }
  // flowers along the barn's front and round the pond
  const B = L.barn;
  for (const fx of [B.x + 4, B.x + B.w - 6]) L.decor.push({ kind: "flowers", x: fx, y: B.y + B.h + 4, r: 0, c: Math.floor(rnd() * 6) });
  if (L.yard) L.decor.push({ kind: "flowers", x: L.pond.cx - L.pond.rx - 8, y: L.pond.cy + 6, r: 0, c: 2 }, { kind: "flowers", x: L.pond.cx + L.pond.rx + 8, y: L.pond.cy + 4, r: 0, c: 4 });
  L.rocks.forEach(r => drawRockHD(g, r));
  L.decor.sort((a, b) => a.y - b.y).forEach(dd => drawDecorHD(g, dd, rnd));
  if (L.yard) drawYardHD(g, L.yard, rnd);
  if (L.gate) drawFenceHD(g, L.field, L.gate);
  L.plots.forEach(p => drawPlotHD(g, p));
  drawBarnHD(g, L.barn);
  if (L.sign) drawSignHD(g, L.sign);
  // trees round the edge: back rows on the background, the bottom row in front of everything (its own strip)
  const trees = [], T = L.T;
  for (let x = -6; x < W + 12; x += 12 + rnd() * 9) { trees.push([x, 2 + rnd() * (T - 16), 10 + rnd() * 5]); if (rnd() < 0.6) trees.push([x + 6, -6 + rnd() * 8, 11 + rnd() * 4]); }
  for (let y = T - 4; y < H - T + 6; y += 12 + rnd() * 8) {
    trees.push([2 + rnd() * (T - 14), y, 9 + rnd() * 5]); trees.push([W - 2 - rnd() * (T - 14), y, 9 + rnd() * 5]);
    if (rnd() < 0.5) trees.push([-4, y + 6, 11]); if (rnd() < 0.5) trees.push([W + 4, y + 6, 11]);
  }
  const front = [];
  for (let x = -6; x < W + 12; x += 12 + rnd() * 9) front.push([x, H - T + 12 + rnd() * 10, 11 + rnd() * 5]);
  trees.sort((a, b) => a[1] - b[1]).forEach(([x, y, r]) => drawTreeHD(g, Math.round(x * HD), Math.round(y * HD), Math.round(r * HD), rnd));
  const fgY = Math.max(0, H - T - 8), fg = canvas(W * HD, (H - fgY) * HD), f = fg.getContext("2d");
  front.sort((a, b) => a[1] - b[1]).forEach(([x, y, r]) => drawTreeHD(f, Math.round(x * HD), Math.round((y - fgY) * HD), Math.round(r * HD), rnd));
  return { L, bg, fg, fgY };
}

/** One crop at a world point (the demos draw their own fields with it). */
function drawCrop(g, x, y, stage, t) { g.drawImage(CROPS[stage][REDUCED ? 1 : Math.round(Math.sin(t * 1.6 + x) * 0.9) + 1], x - 2.5, y - 9, 5, 9); }
/** Crops: the field shows the work. Running tasks grow; finished ones bloom into Claude's spark; failed ones wilt.
 * Two staggered rows of three per plot. */
function drawCrops(g, p, stage, t, glow) {
  for (let row = 0; row < 2; row++) for (let k = 0; k < 3; k++) {
    const x = p.x + 6 + k * 9 + row * 4, y = row ? p.y + p.h - 1 : p.y + 8, sw = Math.round(Math.sin(t * 1.6 + x * 0.7) * 0.9) + 1;
    g.drawImage(CROPS[stage][REDUCED ? 1 : sw], x - 2.5, y - 9, 5, 9);
  }
}

// ==================================================================== critters
class Critter {
  constructor(key, x, y) {
    Object.assign(this, { key, x, y, tx: x, ty: y, wait: Math.random() * 2, dir: 0, moving: false, phase: 0,
      phaseT: 0, blinkT: 2 + Math.random() * 3, blink: 0, born: performance.now(), hop: 0, speed: 20 + Math.random() * 6 });
    this.mode = "wander"; this.kind = "claude"; this.hat = "straw"; this.color = HAT_COLORS[0]; this.skin = null;
    this.label = ""; this.bubble = null; this.gone = 0; this.home = null;
    this.seed = (hashStr(key) % 97) / 7; this.look = 0; this.lookT = 1 + Math.random() * 4; this.step = 0; this.cheerUntil = 0;
  }
  setTarget(x, y) { this.tx = x; this.ty = y; }
  update(dt, world) {
    const L = world.L;
    if (this.kind === "egg") { this.hop = (this.hop + dt) % 3; return; }
    this.blinkT -= dt;
    if (this.blinkT < 0) { this.blink = 0.14; this.blinkT = 2 + Math.random() * 4; }
    this.blink = Math.max(0, this.blink - dt);
    let goal = null;
    if ((this.mode === "work" || this.mode === "subwait") && this.spot) {
      // at work it doesn't just sit: now and then it walks round its plot to look at the crops, then goes back
      this.strollT = (this.strollT ?? 4 + Math.random() * 10) - dt;
      if (this.strollT < 0) {
        if (this.stroll) { this.stroll = null; this.strollT = 6 + Math.random() * 12; }
        else { this.stroll = { x: this.spot.x + 4 + Math.random() * 30, y: this.spot.y - 2 - Math.random() * 24 }; this.strollT = 2 + Math.random() * 2.5; }
      }
      goal = this.stroll || this.spot;
    }
    else if (this.mode === "sleep") goal = this.home || world.restSpot(this);
    else if (this.mode === "starting") goal = { x: L.door.x + ((hashStr(this.key) % 5) - 2) * 7, y: L.door.y + 8 };
    this.lookT -= dt; // idle: it looks round now and then
    if (this.lookT < 0) { this.look = this.look ? 0 : Math.random() < 0.5 ? -1 : 1; this.lookT = this.look ? 0.8 + Math.random() : 2 + Math.random() * 4; }
    if (goal) { this.tx = goal.x; this.ty = goal.y; }
    else if (this.mode === "wander" || this.mode === "offline" || this.mode === "rest") { // strolling round the farm
      if (Math.abs(this.tx - this.x) + Math.abs(this.ty - this.y) < 1.5) {
        this.wait -= dt;
        if (this.wait <= 0) { const p = world.randomSpot(); this.tx = p.x; this.ty = p.y; this.wait = 1 + Math.random() * 4; }
      }
    } else { this.tx = this.x; this.ty = this.y; }
    const dx = this.tx - this.x, dy = this.ty - this.y;
    if (Math.abs(dx) + Math.abs(dy) < 1.2) { this.moving = false; this.phase = 0; this.step = 0; if (this.mode === "work") this.dir = 1; }
    else {
      const dist = Math.hypot(dx, dy);
      this.moving = true;
      const sp = (this.mode === "work" ? 34 : this.speed) * dt * (REDUCED ? 0.7 : 1);
      const nx = this.x + (dx / dist) * Math.min(sp, dist), ny = this.y + (dy / dist) * Math.min(sp, dist);
      const R = L.reserve, inside = (x, y) => R && x > R.x - 6 && x < R.x + R.w + 6 && y > R.y - 2 && y < R.y + R.h + 16;
      if (!inside(nx, ny) || inside(this.x, this.y)) { this.x = nx; this.y = ny; }
      else if (!inside(nx, this.y)) this.x = nx; // slide round the title instead of walking through it
      else if (!inside(this.x, ny)) this.y = ny;
      else if (this.mode === "wander" || this.mode === "offline" || this.mode === "rest") { const p = world.randomSpot(); this.tx = p.x; this.ty = p.y; }
      this.dir = Math.abs(dx) > 0.5 ? Math.sign(dx) : this.dir;
      this.phaseT += dt; // the walk: step, pass, the other step, pass
      if (this.phaseT > 0.11) { this.phaseT = 0; this.step = (this.step + 1) % 4; this.phase = [1, 0, 2, 0][this.step]; }
    }
    if (this.mode === "error") this.hop = (this.hop + dt * 6) % (Math.PI * 2);
  }
  sprite(t) {
    if (this.kind === "egg") return EGG_HD;
    const blink = this.blink > 0, now = performance.now();
    if (this.mini) return miniHD(this.color, { legs: this.phase, blink: blink || this.mode === "subwait",
      arms: this.mode === "work" && !this.moving && Math.floor(t * 6 + this.seed) % 2 ? 1 : 0 });
    const sleep = this.mode === "sleep" && !this.moving;
    const typing = this.mode === "work" && !this.moving;
    const cheer = now < this.cheerUntil || now - this.born < 1800;
    return skinFrameHD(this.skin || (this.skin = skinOf(this.hat, null, "", this.color)), {
      look: this.moving || typing ? this.dir : this.look, legs: this.phase, blink: blink || this.mode === "offline", sleep,
      arms: typing ? (Math.floor(t * 7 + this.seed) % 2 ? 2 : 3) : cheer && !this.moving ? (Math.floor(t * 5) % 2 ? 1 : 0) : 0, happy: cheer && !sleep,
    });
  }
  /** How far it's lifted this frame, in world pixels (halves: the HD grid). */
  bob(t) {
    if (this.kind === "egg") return 0;
    if (this.mode === "error") return -Math.abs(Math.sin(this.hop)) * 3;
    if (!this.mini && !this.moving && performance.now() < this.cheerUntil) return -Math.round(Math.abs(Math.sin(t * 8)) * 5) / 2;
    if (this.moving) return this.step % 2 ? -0.5 : 0;
    if (this.mini && this.mode === "subwait") return 0;
    if (this.mode === "sleep") return Math.sin(t * 1.6 + this.seed) > 0.2 ? 0.5 : 0; // breathing
    return Math.sin(t * 2 + this.seed) > 0.7 ? -0.5 : 0; // idle breathing
  }
}

// ======================================================================= scene
/** The farm's canvas. The world can be bigger than the window: a camera (x, y in world pixels, z = CSS px per world
 * pixel) fits it by default; drag or swipe to pan, wheel or pinch to zoom. The landing page and the title screen keep
 * the old fixed view (the world is the window, z = S). The canvas's backing store is the window at devicePixelRatio,
 * and it draws at a zoom snapped to whole device pixels (an even number per world pixel once there's room, so every
 * HD sprite pixel is whole too): crisp, and no shimmer while panning. cam.z is the zoom asked for; snap() the drawn one. */
const Scene = {
  cv: $("#world"), ctx: null, S: 3, world: null, critters: new Map(), sparkles: [],
  labels: $("#labels"), selected: null, hover: null, plotTasks: [], plotMore: [], boardCount: 0, demo: false, t: 0,
  cam: { x: 0, y: 0, z: 3, auto: true }, goal: null, dpr: 1, need: null, farm: false, planner: null, pendingFor: {},
  TAGS_AT: 2.6, BUBBLES_AT: 1.9, MINI_BUBBLES_AT: 3.6, // zoom (CSS px per world px) from which every name tag / bubble shows
  init() {
    this.ctx = this.cv.getContext("2d", { alpha: false });
    addEventListener("resize", () => { clearTimeout(this.resizeT); this.resizeT = setTimeout(() => this.resize(), 80); });
    const watchDpr = () => { // dragged to another screen, or the browser zoomed: a new backing store
      const m = matchMedia(`(resolution: ${devicePixelRatio || 1}dppx)`);
      m.addEventListener?.("change", () => { this.resize(); watchDpr(); }, { once: true });
    };
    watchDpr();
    this.bindPointer();
    this.resize();
    const hudTop = $("#hud-top");
    if (hudTop && window.ResizeObserver) new ResizeObserver(() => { // the HUD grew (your Claude's card, approvals): refit
      if (!this.farm || !this.insets || !hudTop.getBoundingClientRect().width) return;
      const v = this.viewBox(), o = this.insets;
      if (Math.abs(v.left - o.left) > 30 || Math.abs(v.top - o.top) > 30) { clearTimeout(this.resizeT); this.resizeT = setTimeout(() => this.resize(), 150); }
    }).observe(hudTop);
    let last = performance.now();
    const loop = (now) => { const dt = Math.min(0.05, (now - last) / 1000); last = now; this.t += dt; this.frame(dt); requestAnimationFrame(loop); };
    requestAnimationFrame(loop);
  },
  /** The window in CSS px, and how much of it the HUD covers (the fit keeps the farm clear of it). */
  viewBox() {
    const w = innerWidth, hh = innerHeight, phone = w < 760, S = clamp(Math.round(Math.min(w / 330, hh / 205)), 2, 6);
    if (!this.farm) return { w, h: hh, S, top: 0, bottom: 0, left: 0, right: 0 };
    // the counter and chips sit top-left: beside the farm on a wide window, above it on a tall one
    const r = $("#hud-top")?.getBoundingClientRect(), hud = r && r.width ? r : { right: 300, bottom: 200 };
    this.hudBox = { right: Math.round(hud.right), bottom: Math.round(hud.bottom) };
    const wide = w > hh * 1.15 && hud.right < w * 0.4;
    const dock = $(".dock")?.getBoundingClientRect(), bottom = dock && dock.height ? Math.round(hh - dock.top + 8) : phone ? 150 : 100;
    return { w, h: hh, S, top: wide ? 8 : hud.bottom + 6, bottom: Math.max(bottom, phone ? 80 : 90), left: wide ? hud.right + 10 : 0, right: phone ? 52 : 64, snap: (z) => this.snap(z, true) };
  },
  /** The zoom actually drawn for a zoom z: whole device pixels per world pixel (even ones on a sharp screen, from 4 up,
   * so the half-pixel HD sprites land on whole pixels too). Far out, below 2 device px, it stays as asked. */
  snap(z, down = false) {
    const Zr = z * this.dpr;
    if (Zr < 2) return z;
    const step = Zr >= 4 && this.dpr >= 1.5 ? 2 : 1, n = down ? Math.floor(Zr / step + 1e-6) : Math.round(Zr / step);
    return Math.max(step, n * step) / this.dpr;
  },
  resize() {
    this.dpr = Math.min(3, devicePixelRatio || 1);
    const v = this.viewBox(), S = v.S;
    this.S = S; this.insets = { left: v.left, top: v.top };
    this.cv.width = Math.round(v.w * this.dpr); this.cv.height = Math.round(v.h * this.dpr);
    this.cv.style.width = v.w + "px"; this.cv.style.height = v.h + "px";
    let L;
    if (this.farm) L = layoutFarm(this.need || { plots: 6, yard: 5 }, v);
    else {
      const zS = this.snap(S, true), W = Math.ceil(v.w / zS), H = Math.ceil(v.h / zS), opts = { ...(this.layout || {}) };
      const keep = opts.clearOf && document.querySelector(opts.clearOf);
      if (keep) { // keep this element's area free of the field and the critters (in world pixels)
        const r = keep.getBoundingClientRect();
        opts.reserve = { x: Math.floor(r.left / zS) - 10, y: Math.floor(r.top / zS) - 4, w: Math.ceil(r.width / zS) + 20, h: Math.ceil(r.height / zS) + 8 };
      }
      L = layoutWorld(W, H, opts); L.z = zS;
    }
    this.world = buildWorld(L, 7);
    this.world.randomSpot = () => this.randomSpot();
    this.world.restSpot = (c) => yardSpot(L, hashStr(c.key) % 10);
    if (this.cam.auto || !this.farm) this.fit(); else { this.cam.z = Math.max(this.cam.z, L.z); this.clampCam(); }
    this.goal = null;
    for (const c of this.critters.values()) {
      if (c.x > L.W || c.y > L.H || (c.mode === "wander" && blocked(L, c.x, c.y))) { const p = this.randomSpot(); c.x = p.x; c.y = p.y; }
      c.tx = c.x; c.ty = c.y;
    }
    this.relayout = true; // reconcile moves everyone to their new spots
    if (this.farm && App.state && !this.reconciling) reconcile(App.state);
  },
  /** Grow (or shrink) the world when the farm outgrows its plots or yard. True when it rebuilt. */
  setNeed(n) {
    const cap = this.world?.L.cap;
    if (cap && n.plots <= cap.plots && n.yard <= cap.yard && !(cap.plots > 8 && n.plots < cap.plots * 0.45)
      && !(cap.yard > 12 && n.yard < cap.yard * 0.45)) return false;
    this.need = { plots: Math.ceil(n.plots * 1.15) + 1, yard: Math.ceil(n.yard * 1.1) + 2 };
    this.resize();
    return true;
  },
  // ------------------------------------------------------------------ camera
  fit() {
    const L = this.world.L, v = this.viewBox(), c = this.cam, box = L.content;
    c.z = L.z; c.auto = true;
    if (box) {
      c.x = box.x + box.w / 2 - (v.left + (v.w - v.left - v.right) / 2) / c.z;
      c.y = box.y + box.h / 2 - (v.top + (v.h - v.top - v.bottom) / 2) / c.z;
    } else { c.x = 0; c.y = 0; }
    this.clampCam();
  },
  clampCam(c = this.cam) {
    const L = this.world.L, z = this.snap(c.z), vw = innerWidth / z, vh = innerHeight / z;
    c.x = L.W <= vw ? (L.W - vw) / 2 : clamp(c.x, 0, L.W - vw);
    c.y = L.H <= vh ? (L.H - vh) / 2 : clamp(c.y, 0, L.H - vh);
  },
  zoomLimits() { const z0 = this.world.L.z; return [z0, Math.max(8, z0 * 3)]; },
  zoomAt(sx, sy, factor) {
    const c = this.cam, [lo, hi] = this.zoomLimits(), z0 = this.snap(c.z), wx = c.x + sx / z0, wy = c.y + sy / z0;
    this.goal = null;
    c.z = clamp(c.z * factor, lo, hi);
    const z1 = this.snap(c.z); c.x = wx - sx / z1; c.y = wy - sy / z1; c.auto = false;
    this.clampCam();
  },
  zoomBy(factor) { this.zoomAt(innerWidth / 2, innerHeight / 2, factor); },
  /** Glide the camera to a world point, zoomed in enough to see names. */
  focus(x, y, z) {
    const [lo, hi] = this.zoomLimits();
    z = this.snap(clamp(z || Math.max(this.cam.z, this.TAGS_AT + 0.6, this.S), lo, hi));
    const g = { x: x - innerWidth / 2 / z, y: y - innerHeight / 2 / z, z };
    this.clampCam(g); this.goal = g; this.cam.auto = false;
  },
  focusCritter(c) { if (c) { this.focus(c.x, c.y - 8); this.follow = { key: c.key, until: performance.now() + 6000 }; } },
  // ------------------------------------------------------------------- input
  toWorld(e) { const r = this.rt; return r ? { x: (e.clientX * r.dpr - r.ox) / r.Zr, y: (e.clientY * r.dpr - r.oy) / r.Zr } : { x: this.cam.x + e.clientX / this.cam.z, y: this.cam.y + e.clientY / this.cam.z }; },
  bindPointer() {
    const cv = this.cv, pts = new Map();
    let drag = null, pinch = null;
    const off = () => this.demo || this.passive || !this.farm;
    cv.addEventListener("pointerdown", (e) => {
      if (off()) return;
      if (e.pointerType === "mouse" && e.button !== 0) return;
      try { cv.setPointerCapture(e.pointerId); } catch { /* fine */ }
      pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (pts.size === 1) drag = { x: e.clientX, y: e.clientY, cx: this.cam.x, cy: this.cam.y, moved: false };
      else if (pts.size === 2) {
        const [a, b] = [...pts.values()];
        pinch = { d: Math.hypot(a.x - b.x, a.y - b.y) || 1, z: this.cam.z, mx: (a.x + b.x) / 2, my: (a.y + b.y) / 2 };
        if (drag) drag.moved = true;
      }
    });
    cv.addEventListener("pointermove", (e) => {
      if (off()) return;
      if (!pts.has(e.pointerId)) { if (e.pointerType === "mouse") this.onMove(e); return; }
      pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (pinch && pts.size >= 2) {
        const [a, b] = [...pts.values()], d = Math.hypot(a.x - b.x, a.y - b.y) || 1, mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
        this.zoomAt(mx, my, (pinch.z * d / pinch.d) / this.cam.z);
        this.cam.x -= (mx - pinch.mx) / this.cam.z; this.cam.y -= (my - pinch.my) / this.cam.z; this.clampCam();
        pinch.mx = mx; pinch.my = my;
      } else if (drag) {
        const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
        if (!drag.moved && Math.abs(dx) + Math.abs(dy) > 6) { drag.moved = true; cv.classList.add("grabbing"); }
        if (drag.moved) { this.goal = null; this.cam.auto = false; this.cam.x = drag.cx - dx / this.cam.z; this.cam.y = drag.cy - dy / this.cam.z; this.clampCam(); }
      }
    });
    const up = (e) => {
      if (!pts.has(e.pointerId)) return;
      pts.delete(e.pointerId);
      if (pts.size < 2) pinch = null;
      if (pts.size === 0) {
        if (drag && !drag.moved && e.type === "pointerup") this.onClick(e);
        drag = null; cv.classList.remove("grabbing");
      } else if (drag) { const [p] = [...pts.values()]; drag = { x: p.x, y: p.y, cx: this.cam.x, cy: this.cam.y, moved: true }; }
    };
    cv.addEventListener("pointerup", up);
    cv.addEventListener("pointercancel", up);
    cv.addEventListener("wheel", (e) => {
      if (off()) return;
      e.preventDefault();
      const k = e.deltaMode === 1 ? 16 : 1; // lines -> pixels
      this.zoomAt(e.clientX, e.clientY, Math.exp(-e.deltaY * k * (e.ctrlKey ? 0.012 : 0.0018)));
    }, { passive: false });
    cv.addEventListener("pointerleave", () => { if (this.hover) { this.hover = null; } });
  },
  randomSpot() {
    const L = this.world.L;
    for (let i = 0; i < 60; i++) {
      const x = L.play.x0 + 8 + Math.random() * (L.play.x1 - L.play.x0 - 16), y = L.play.y0 + 16 + Math.random() * (L.play.y1 - L.play.y0 - 16);
      if (!blocked(L, x, y)) return { x, y };
    }
    return { x: L.door.x, y: L.door.y + 12 };
  },
  restSpot(c) { return this.world.restSpot(c); },
  hit(p) {
    let best = null;
    for (const c of this.critters.values()) {
      if (c.gone) continue;
      const w = c.kind === "egg" ? 7 : c.mini ? 6 : 9, top = c.mini ? 10 : 19;
      if (p.x > c.x - w && p.x < c.x + w && p.y > c.y - top && p.y < c.y + 2 && (!best || c.y > best.y)) best = c;
    }
    if (best) return { critter: best };
    const cr = this.world.L.crow;
    if (cr && this.planner && Math.abs(p.x - cr.x) < 9 && p.y > cr.y - 23 && p.y < cr.y + 2) return { crow: true };
    return null;
  },
  onMove(e) {
    if (this.demo || this.passive) return;
    const r = this.hit(this.toWorld(e));
    this.hover = r?.critter || null; this.hoverCrow = !!r?.crow;
    this.cv.classList.toggle("pointing", !!r);
  },
  onClick(e) {
    if (this.demo || this.passive) return;
    const r = this.hit(this.toWorld(e));
    if (!r) { this.selected = null; return; }
    if (r.crow) return UI.openPlanner();
    if (r.critter) { this.selected = r.critter; UI.openCritter(r.critter); }
  },
  sparkle(x, y, n = 14, color) {
    if (this.sparkles.length > 400) return;
    for (let i = 0; i < n; i++) { const a = Math.random() * Math.PI * 2, s = 10 + Math.random() * 26;
      this.sparkles.push({ x, y, vx: Math.cos(a) * s, vy: Math.sin(a) * s - 12, life: 0.7 + Math.random() * 0.5, k: i % 4, c: color || null }); }
  },
  frame(dt) {
    const g = this.ctx, w = this.world, L = w.L, t = this.t, cam = this.cam, now = performance.now();
    if (this.goal) { // glide
      const k = 1 - Math.exp(-dt * 7), G2 = this.goal;
      cam.x = lerp(cam.x, G2.x, k); cam.y = lerp(cam.y, G2.y, k); cam.z = lerp(cam.z, G2.z, k);
      if (Math.abs(cam.x - G2.x) + Math.abs(cam.y - G2.y) < 0.3 && Math.abs(cam.z - G2.z) < 0.01) { Object.assign(cam, { x: G2.x, y: G2.y, z: G2.z }); this.goal = null; }
    }
    // the drawn zoom, and the camera's offset rounded to whole device pixels (the labels and clicks use the same)
    const dpr = this.dpr, z = this.snap(cam.z), Zr = z * dpr, ox = Math.round(-cam.x * Zr), oy = Math.round(-cam.y * Zr);
    this.rt = { z, Zr, ox, oy, dpr };
    const vx0 = -ox / Zr, vy0 = -oy / Zr, vx1 = vx0 + this.cv.width / Zr, vy1 = vy0 + this.cv.height / Zr;
    g.setTransform(1, 0, 0, 1, 0, 0);
    if (vx0 < 0 || vy0 < 0 || vx1 > L.W || vy1 > L.H) { g.fillStyle = G.t0; g.fillRect(0, 0, this.cv.width, this.cv.height); }
    g.setTransform(Zr, 0, 0, Zr, ox, oy);
    g.imageSmoothingEnabled = Zr < 1.5; // far out (a huge farm on a small screen), smoothing beats dropped pixels
    const sx = Math.max(0, Math.floor(vx0)), sy = Math.max(0, Math.floor(vy0));
    const sw = Math.min(L.W, Math.ceil(vx1) + 1) - sx, sh = Math.min(L.H, Math.ceil(vy1) + 1) - sy;
    if (sw > 0 && sh > 0) g.drawImage(w.bg, sx * HD, sy * HD, sw * HD, sh * HD, sx, sy, sw, sh);
    const seen = (x, y, m = 24) => x > vx0 - m && x < vx1 + m && y > vy0 - m && y < vy1 + m + 20;
    const hx = (v) => Math.round(v * HD) / HD; // on the HD grid
    // crops, and a sign on plots with more minis than they draw
    this.plotTasks.forEach((task, i) => {
      const p = L.plots[i]; if (!p || !task || !seen(p.x, p.y, 40)) return;
      const stage = task.status === "done" ? "ripe" : task.status === "waiting" ? "grow" : task.status === "failed" ? "wilt"
        : (nowS() - (task.started || task.updated || nowS())) < 120 ? "seed" : (nowS() - (task.started || 0)) < 900 ? "sprout" : "grow";
      drawCrops(g, p, stage, t, task.status === "done");
    });
    this.plotMore.forEach((n, i) => { const p = L.plots[i]; if (n > 0 && p && seen(p.x, p.y, 40)) drawSign(g, p.x + p.w + 1, p.y - 13, "+" + n); });
    // the planner's scarecrow
    const cr = L.crow;
    if (cr && this.planner && seen(cr.x, cr.y)) {
      const on = this.planner.on, f = on ? (REDUCED ? 0 : Math.floor(t * 1.4) % 2) : 2, sh2 = shadowSprite(8, 2);
      g.globalAlpha = 0.35; g.drawImage(sh2, cr.x - sh2.width / 4, cr.y - sh2.height / 4, sh2.width / 2, sh2.height / 2); g.globalAlpha = 1;
      g.drawImage(SCARECROW_HD[f], cr.x - 8, cr.y - 21.5 + (on && !REDUCED && Math.floor(t * 2.8) % 2 ? -0.5 : 0), 16, 22);
      if (this.hoverCrow) g.drawImage(ARROW_SEL, cr.x - 2.25, hx(cr.y - 28 + Math.sin(t * 5)), 4.5, 3.5);
    }
    // the quest board: the landing page's scripted farm only (the real farm has no queue to pin up)
    if (this.passive) {
      const b = L.board;
      g.fillStyle = "rgba(20,32,10,.35)"; g.fillRect(b.x + 1, b.y + b.h + 5, b.w, 3);
      g.fillStyle = "#4a3120"; g.fillRect(b.x + 2, b.y + 4, 2, b.h + 4); g.fillRect(b.x + b.w - 4, b.y + 4, 2, b.h + 4);
      g.fillStyle = "#3a2616"; g.fillRect(b.x, b.y, b.w, b.h - 2);
      g.fillStyle = "#8a6038"; g.fillRect(b.x + 1, b.y + 1, b.w - 2, b.h - 4);
      g.fillStyle = "#a37446"; g.fillRect(b.x + 1, b.y + 1, b.w - 2, 1);
      for (let i = 0; i < Math.min(6, this.boardCount); i++) {
        const nx = b.x + 3 + (i % 3) * 7, ny = b.y + 3 + Math.floor(i / 3) * 7;
        g.fillStyle = i % 2 ? "#fff4c2" : "#f6f1e4"; g.fillRect(nx, ny, 5, 5);
        g.fillStyle = "#b9ad90"; g.fillRect(nx + 1, ny + 2, 3, 1);
        g.fillStyle = "#c0392b"; g.fillRect(nx + 2, ny, 1, 1);
      }
    }
    // critters, depth-sorted; only the ones in view are drawn
    const list = [...this.critters.values()];
    for (const c of list) c.update(dt, w);
    list.sort((a, c) => a.y - c.y);
    const vis = [], shC = shadowSprite(7, 2), shM = shadowSprite(4, 1), shE = shadowSprite(5, 2);
    for (const c of list) {
      if (!seen(c.x, c.y)) continue;
      vis.push(c);
      const s = c.sprite(t), bob = c.bob(t), X = hx(c.x), Y = hx(c.y);
      const fade = c.gone ? clamp(1 - (now - c.gone) / 500, 0, 1) : clamp((now - c.born) / 350, 0, 1);
      const sh2 = c.kind === "egg" ? shE : c.mini ? shM : shC;
      g.globalAlpha = fade * 0.35; g.drawImage(sh2, X - sh2.width / 4, Y - sh2.height / 4, sh2.width / 2, sh2.height / 2);
      g.globalAlpha = fade;
      if (c.mini) {
        g.drawImage(s, X - 5, Y - 8 + bob, 10, 8);
        if (c.mode === "work" && !c.moving) { // a tiny laptop
          g.fillStyle = "#1b1f2a"; g.fillRect(X + 3, Y - 4, 4.5, 3); g.fillStyle = Math.floor(t * 3 + c.seed) % 2 ? "#7cfc9a" : "#2c4a3c"; g.fillRect(X + 3.5, Y - 3.5, 3.5, 1);
          g.fillStyle = "#d97757"; g.fillRect(X + 3.5, Y - 2.5, 2, 0.5); g.fillStyle = "#9aa3b2"; g.fillRect(X + 2.5, Y - 1, 5.5, 1);
        }
      } else if (c.kind === "egg") {
        const wob = Math.floor(c.hop * 4) % 6 === 0 ? (Math.floor(c.hop * 8) % 2 ? 0.5 : -0.5) : 0;
        g.drawImage(s, X - 6 + wob, Y - 12, 12, 12);
      } else {
        g.drawImage(s, X - 8, Y - 17 + bob, 16, 18);
        if (c.mode === "work" && !c.moving) g.drawImage(LAPTOP_HD[Math.floor(t * 3 + c.seed) % 2], X + 4, Y - 6, 9, 7);
        if (c.mode === "sleep" && !c.moving && !REDUCED) for (let k = 0; k < 2; k++) { // Zzz
          const ph = (t * 0.4 + k * 0.5 + c.seed) % 1;
          g.globalAlpha = fade * Math.min(1, (1 - ph) * 1.6);
          g.drawImage(ZED, hx(X + 5 + ph * 4 + Math.sin(ph * 6 + k) * 1.5), hx(Y - 14 - ph * 11), 3, 3);
        }
      }
      g.globalAlpha = 1;
      if (c.gone || c.mini) continue;
      const top = Y - (c.kind === "egg" ? 19 : 26);
      if (c === this.selected) g.drawImage(ARROW_SEL, X - 2.25, hx(top + Math.sin(t * 5) * 1.5), 4.5, 3.5);
      else if (c.agent?.mine) g.drawImage(ARROW_MINE, X - 2.25, top + (Math.floor(t * 2) % 2) * 0.5, 4.5, 3.5);
      if (this.pendingFor[c.agent?.id]) g.drawImage(FLAG_HD[Math.floor(t * 3) % 2], X + 4.5, Y - 27, 6, 10); // missions waiting for its person
    }
    for (const c of list) if (c.gone && now - c.gone > 520) { this.critters.delete(c.key); c.el?.remove(); c.tagEl?.remove(); }
    const fy0 = Math.max(sy, w.fgY), fh = Math.min(L.H, sy + sh) - fy0;
    if (sw > 0 && fh > 0) g.drawImage(w.fg, sx * HD, (fy0 - w.fgY) * HD, sw * HD, fh * HD, sx, fy0, sw, fh);
    // sparkles: little four-point stars that twinkle as they fall
    this.sparkles = this.sparkles.filter(p => (p.life -= dt) > 0);
    for (const p of this.sparkles) {
      p.x += p.vx * dt; p.y += p.vy * dt; p.vy += 40 * dt;
      if (p.c) { g.fillStyle = p.c; g.fillRect(hx(p.x), hx(p.y), 1, 1); continue; }
      if (p.life < 0.3 && Math.floor(p.life * 20) % 2) continue;
      g.drawImage(SPARKLE[p.k], hx(p.x - 1.75), hx(p.y - 1.75), 3.5, 3.5);
    }
    this.placeLabels(list, vis);
  },
  /** Speech bubbles and name tags (DOM, so they stay crisp): only for critters in view, and only when zoomed in far
   * enough to read them (or on hover / selection). They sit on whole CSS pixels, centred by their measured width
   * (translate(-50%) would land half of them between pixels and blur the text). */
  placeLabels(list, vis) {
    if (this.demo) return;
    const r = this.rt, z = r.z, px = (x) => Math.round((r.ox + x * r.Zr) / r.dpr), py = (y) => Math.round((r.oy + y * r.Zr) / r.dpr);
    const put = (el, x, y, above) => {
      if (el._w == null) { el._w = el.offsetWidth; el._h = el.offsetHeight; }
      const tr = `translate(${x - Math.round(el._w / 2)}px, ${above ? y - el._h : y}px)`;
      if (el._tr !== tr) { el._tr = tr; el.style.transform = tr; }
    };
    const inView = new Set(vis);
    for (const c of list) {
      const focus = c === this.selected || c === this.hover;
      let want = c.gone || !inView.has(c) ? null : c.bubble;
      if (want && !focus && z < (c.mini ? this.MINI_BUBBLES_AT : this.BUBBLES_AT) && !want.alert) want = null;
      if (want && !focus && c.mode === "offline" && z < this.TAGS_AT + 1.4) want = null; // a yard of "not running" icons is noise
      if (want?.title) want = { ...want, text: focus ? want.title.slice(0, 30) : "" };
      if (want) {
        if (!c.el) { c.el = h("div", { class: "bubble" }); this.labels.append(c.el); }
        const sig = want.icon + "|" + (want.text || "") + "|" + (want.alert ? 1 : 0);
        if (c.el.dataset.sig !== sig) {
          c.el.dataset.sig = sig; c.el._w = null; c.el.className = "bubble" + (want.alert ? " alert" : ""); fill(c.el, h("img", { src: icon(want.icon), alt: "" }), want.text ? h("span", { text: want.text }) : null);
        }
        put(c.el, px(c.x), py(c.y - (c.kind === "egg" ? 16 : c.mini ? 13 : 25)), true);
      } else if (c.el) { c.el.remove(); c.el = null; }
      const showTag = inView.has(c) && !c.gone && c.label && (focus || c.agent?.mine || (z >= this.TAGS_AT && c.mode !== "sleep" && c.mode !== "offline"));
      if (showTag) {
        if (!c.tagEl) { c.tagEl = h("div", { class: "tag" }); this.labels.append(c.tagEl); }
        if (c.tagEl.textContent !== c.label) { c.tagEl.textContent = c.label; c.tagEl._w = null; }
        c.tagEl.classList.toggle("sel", focus);
        c.tagEl.classList.toggle("mine", !!c.agent?.mine);
        put(c.tagEl, px(c.x), py(c.y + 2.5), false);
      } else if (c.tagEl) { c.tagEl.remove(); c.tagEl = null; }
    }
    // the scarecrow says what the planner is doing
    const cr = this.world.L.crow, P = this.planner;
    if (cr && P && this.farm) {
      if (!this.crowEl) { this.crowEl = h("div", { class: "bubble crow" }); this.labels.append(this.crowEl); }
      const text = !P.on ? "ZZZ" : z < this.BUBBLES_AT && !this.hoverCrow ? "…" : String(P.state || "planning").slice(0, 28).toUpperCase();
      const sig = (P.on ? 1 : 0) + text;
      if (this.crowEl.dataset.sig !== sig) { this.crowEl.dataset.sig = sig; this.crowEl._w = null; fill(this.crowEl, h("img", { src: icon(P.on ? "plan" : "zzz"), alt: "" }), h("span", { text })); this.crowEl.classList.toggle("off", !P.on); }
      put(this.crowEl, px(cr.x), py(cr.y - 24), true);
    } else if (this.crowEl) { this.crowEl.remove(); this.crowEl = null; }
  },
};

// ============================================================== farm state sync
const App = { state: null, me: null, seenAgents: null, seenEvents: 0, lastTitles: {}, polling: null, user: null, plotOf: new Map(), approvals: null };

/** How a Claude's Remote Control session is named in the Claude app (runner.session_name). */
const sessionName = (st, name) => `[clodfarm] ${name === st.farm ? name : st.farm + " · " + name}`;

function colorFor(id) { return HAT_COLORS[hashStr(id) % HAT_COLORS.length]; }


/** The farm from the state: one critter per Claude, and a mini Claude for each of its sub-agents (tinted with the
 * colour of the Claude whose account runs it). A Claude with sub-agents at work gets a plot and watches them there (a
 * plot draws at most 8 minis and a "+N" sign); resting Claudes nap in the yard by the barn. The world grows to fit. */
function reconcile(st) {
  Scene.reconciling = true;
  try { reconcileNow(st); } finally { Scene.reconciling = false; }
}
function reconcileNow(st) {
  const first = App.seenAgents === null;
  App.seenAgents = App.seenAgents || new Set();
  const subs = st.subagents || [], byId = new Map(st.agents.map(a => [a.id, a]));
  const home = (st.agents.find(a => a.primary) || st.agents[0] || {}).id;
  const ownerOf = (t) => byId.has(t.owner) ? t.owner : home;
  const subsOf = new Map();
  for (const t of subs) { const o = ownerOf(t); if (!subsOf.has(o)) subsOf.set(o, []); subsOf.get(o).push(t); }
  const active = st.agents.filter(a => a.talking || subsOf.has(a.id)); // at work: a turn or sub-agents
  const isActive = new Set(active.map(a => a.id));
  const modeOf = (a) => !a.loggedIn && !a.remote ? "egg" : !a.alive && !a.up ? "offline" : !a.up ? "starting" : a.error ? "error"
    : isActive.has(a.id) ? "work" : st.paused ? "rest" : a.resting ? "sleep" : "wander";
  const modes = new Map(st.agents.map(a => [a.id, modeOf(a)]));
  // the yard is where napping Claudes (paced by their budget) sleep; everyone free walks round the farm
  const yardIds = st.agents.filter(a => modes.get(a.id) === "sleep").map(a => a.id).sort();
  if (Scene.farm) Scene.setNeed({ plots: active.length + Math.min(2, (st.recent || []).length), yard: yardIds.length });
  const L = Scene.world.L, relayout = Scene.relayout || first;
  Scene.relayout = false;
  // plots stay put: a Claude keeps its plot while it's busy, a newly busy one takes the first free plot
  const plotOf = App.plotOf;
  for (const [id, i] of plotOf) if (!isActive.has(id) || i >= L.plots.length) plotOf.delete(id);
  const taken = new Set(plotOf.values());
  let free = 0;
  for (const a of active) {
    if (plotOf.has(a.id)) continue;
    while (taken.has(free)) free++;
    if (free >= L.plots.length) break;
    plotOf.set(a.id, free); taken.add(free);
  }
  const yardIdx = new Map(yardIds.map((id, i) => [id, i]));
  const want = new Map();
  const more = [];
  for (const a of st.agents) {
    const pi = plotOf.get(a.id), plot = pi != null ? L.plots[pi] : null, mode = modes.get(a.id);
    const skin = agentSkin(a);
    want.set((mode === "egg" ? "egg:" : "claude:") + a.id, { agent: a, hat: skin.hat, color: skin.hatC, skin,
      kind: mode === "egg" ? "egg" : "claude", mode, spot: plot ? { x: plot.x - 7, y: plot.y + plot.h } : null,
      home: yardIdx.has(a.id) ? yardSpot(L, yardIdx.get(a.id)) : null });
    if (!plot) continue;
    const mine = (subsOf.get(a.id) || []).slice().sort((x, y) => (y.status === "running") - (x.status === "running"));
    more[pi] = Math.max(0, mine.length - MINIS_PER_PLOT);
    mine.slice(0, MINIS_PER_PLOT).forEach((t, n) => {
      const [dx, dy] = SUB_SPOTS[n];
      const on = t.on && byId.get(t.on);
      want.set("sub:" + t.id, { agent: a, sub: t, kind: "claude", mini: true, hat: "", color: on ? agentSkin(on).hatC : t.on ? colorFor(t.on) : "#9aa3b2",
        mode: t.status === "running" ? "work" : "subwait", spot: { x: plot.x + dx, y: plot.y + dy } });
    });
  }
  Scene.plotMore = more;
  const eggs = new Map();
  for (const c of Scene.critters.values()) if (c.kind === "egg" && !c.gone) eggs.set(c.agent.id, c);
  const nSubs = (id) => (subsOf.get(id) || []).length;
  for (const [key, d] of want) {
    let c = Scene.critters.get(key);
    const at = d.mode === "work" || d.mini ? d.spot : d.home;
    if (!c) {
      const egg = !d.mini && eggs.get(d.agent.id), owner = d.mini && Scene.critters.get("claude:" + d.agent.id);
      let p = first ? Scene.randomSpot() : { x: L.door.x + (Math.random() - 0.5) * 6, y: L.door.y + 4 };
      if (egg && d.kind === "claude") { p = { x: egg.x, y: egg.y }; Scene.sparkle(egg.x, egg.y - 6, 24); }
      if (owner) p = { x: owner.x, y: owner.y }; // a new sub-agent pops out of its Claude
      if (d.kind === "egg" && !first) p = Scene.randomSpot();
      if (first && at) p = { ...at }; // on the first look, everyone is already where they belong
      c = new Critter(key, p.x, p.y);
      if (!first && d.kind === "claude") { c.born = performance.now(); if (!egg) Scene.sparkle(p.x, p.y - 8, d.mini ? 8 : 16); }
      else c.born = performance.now() - 5000;
      Scene.critters.set(key, c);
      if (!d.mini && !first && !App.seenAgents.has(d.agent.id)) UI.say(d.kind === "egg" ? `An egg appeared! ${d.agent.name.toUpperCase()} waits for its login.` : `A wild ${d.agent.name.toUpperCase()} appeared!`);
      else if (!first && egg && d.kind === "claude") UI.say(`${d.agent.name.toUpperCase()} hatched! Welcome to the farm.`);
    } else if (relayout && at) { c.x = at.x; c.y = at.y; } // the world was rebuilt: everyone is at their new spot
    c.gone = 0;
    Object.assign(c, { kind: d.kind, agent: d.agent, sub: d.sub || null, hat: d.hat, color: d.color, skin: d.skin || null, mode: d.mode, mini: !!d.mini, spot: d.spot, home: d.home || null });
    const n = nSubs(d.agent.id);
    c.label = d.mini ? "" : d.agent.name;
    c.bubble = c.kind === "egg" ? { icon: d.agent.login?.state === "waiting_code" ? "dots" : "ask" }
      : d.mini ? (d.mode === "work" ? { icon: "terminal", title: d.sub.title } : { icon: "dots", title: d.sub.title })
      : c.mode === "work" ? { icon: "terminal", title: n ? `${n} SUB-AGENT${n === 1 ? "" : "S"}` : "TALKING: " + (d.agent.talking?.title || "a conversation").toUpperCase() }
      : c.mode === "sleep" ? null : c.mode === "rest" ? { icon: "pause" } : c.mode === "error" ? { icon: "alert", alert: true }
      : c.mode === "starting" ? { icon: "dots" } : c.mode === "offline" ? { icon: "alert", alert: true } : null;
  }
  for (const [key, c] of Scene.critters) if (!want.has(key) && !c.gone) { c.gone = performance.now(); if (c.kind !== "egg" && Scene.sparkles.length < 200) Scene.sparkle(c.x, c.y - 8, 8, "#d8e6c4"); }
  for (const a of st.agents) App.seenAgents.add(a.id);
  // the field: a plot growing per Claude with sub-agents at work, then the latest harvests on the free plots
  const since = (a) => Math.min(a.talking?.since || nowS(), ...(subsOf.get(a.id) || []).map(t => t.started || t.created || nowS()));
  const crops = [], recent = [...(st.recent || [])];
  for (const a of active) { const i = plotOf.get(a.id); if (i != null) crops[i] = { id: "claude:" + a.id, status: "running", started: since(a) }; }
  for (let i = 0; i < L.plots.length && recent.length; i++) if (!crops[i]) crops[i] = recent.shift();
  Scene.plotTasks = crops;
  const f = Scene.follow; // the world was rebuilt while the camera was on its way to a Claude: go where it is now
  if (relayout && f && performance.now() < f.until) { const c = Scene.critters.get(f.key); if (c) Scene.focus(c.x, c.y - 8, Scene.goal?.z || Scene.cam.z); }
  Scene.planner = Scene.farm ? st.planner || null : null;
}

// =========================================================================== UI
const api = async (path, body) => {
  const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json", "X-Clodfarm": "1" }, body: JSON.stringify(body) };
  const r = await fetch(path, { credentials: "same-origin", ...opt }); // relative: works under a path prefix too
  let data = {};
  try { data = await r.json(); } catch { /* empty */ }
  if (r.status === 401 && !["api/login", "api/me", "api/pair"].includes(path)) { UI.showTitle(); throw Object.assign(new Error("log in first"), { status: 401 }); }
  if (r.status === 403) throw Object.assign(new Error(data.error ? `🔒 ${data.error}` : "🔒 Only the farm's manager, or this Claude's person, can do that."), { status: 403 });
  if (!r.ok) throw Object.assign(new Error(data.error || `HTTP ${r.status}`), { status: r.status });
  return data;
};
const fmtN = (n) => Math.round(Number(n) || 0).toLocaleString("en-US");
const fmtShort = (n) => { n = Number(n) || 0; return n >= 1e9 ? (n / 1e9).toFixed(n >= 1e10 ? 0 : 1) + "B" : n >= 1e6 ? (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M" : n >= 1e3 ? (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + "K" : String(Math.round(n)); };
const pct = (u) => u == null ? "–" : `${Math.round(clamp(u, 0, 9.99) * 100)}%`;
const EVERY = [["5m", 300], ["15m", 900], ["30m", 1800], ["1h", 3600], ["3h", 10800], ["6h", 21600], ["24h", 86400]];
const everyLabel = (s) => (EVERY.find(e => e[1] === s) || [])[0] || (s ? (s >= 3600 ? `${Math.round(s / 3600)}h` : `${Math.round(s / 60)}m`) : "–");

const EVENT_TEXT = {
  "task.added": (e, T) => { const on = (e.msg.match(/\(for ([^)]+)\)$/) || [])[1]; return `${(e.by && !/^\d/.test(e.by) ? e.by : "A Claude").toUpperCase()} started a sub-agent${on ? " on " + on.toUpperCase() : ""}: “${T(e).replace(/ \(for [^)]+\)$/, "")}”.`; },
  "task.done": (e, T) => `★ Sub-agent done: “${T(e)}”!`,
  "task.failed": (e, T) => `A sub-agent failed: “${T(e)}”. Ask its Claude what went wrong.`,
  "task.waiting": (e, T) => `“${T(e)}” started its own sub-agents and waits for them.`,
  "verify.passed": (e, T) => `Tests passed for “${T(e)}”. Harvesting it into main!`,
  "verify.failed": (e, T) => `Tests failed for “${T(e)}”. Sent back to fix them.`,
  "msg.sent": (e) => { const m = e.msg.match(/^(\S+) -> (\S+): ([\s\S]*)$/); return m ? `${m[1].toUpperCase()} → ${m[2].toUpperCase()}: “${m[3].slice(0, 90)}”` : null; },
  "schedule.added": (e) => `Scheduled: “${e.msg.replace(/^\S+\s*/, "")}”.`,
  "farm.paused": (e) => `The farm is paused: ${e.msg || "by hand"}.`,
  "farm.resumed": () => "The farm is back at work!",
  "budget.rejected": () => "A Claude hit its usage limit. It rests until the window resets; the others carry on.",
  "rc.connected": (e) => `${((e.msg.match(/'([^']+)'/) || [])[1] || "A Claude").toUpperCase()} is live in the Claude app: talk to it from your phone.`,
  "agent.added": (e) => / a bot on /.test(e.msg) ? `${e.msg.split(" ")[0].toUpperCase()} joined the farm: a bot on ${e.msg.split(" a bot on ")[1].replace(/\)?;.*$|\)$/, "")}.`
    : `A new egg for ${e.msg.split(" ")[0].toUpperCase()}. It hatches once its person logs it in.`,
  "slack.received": (e) => `FROM SLACK · ${e.msg.slice(0, 120)}`,
  "slack.connected": () => "The farm is on Slack! DM it or @mention it in a channel, and a sub-agent answers in the thread.",
  "agent.removed": (e) => `${e.msg.split(" ")[0].toUpperCase()} left the farm.`,
  "connector.stripe": (e) => /disconnected/.test(e.msg) ? "Stripe is disconnected: the Claudes' Stripe tools are gone."
    : `The farm is on Stripe${/\(live mode/.test(e.msg) ? " in LIVE MODE" : /\(test mode/.test(e.msg) ? " (test mode)" : ""}: every Claude can use it now.`,
  "connector.blender": (e) => /disconnected/.test(e.msg) ? "Blender is disconnected: the Claudes' Blender tools are gone."
    : "The farm is connected to Blender: every Claude can use it now.",
};

/** Who this browser is, from /api/state's `me` (fresh every poll) and /api/me (hatching, every 30s). */
const role = () => { const m = App.state?.me || App.me || {}; return { manager: !!m.manager, owner: m.owner || m.claude || null, viewer: !!m.viewer }; };
const canManage = (a) => role().manager || !!a?.mine;

const UI = {
  queue: [], typing: null, hatchFor: null, hatchPoll: null,

  // ------------------------------------------------------------ title / login
  async boot() {
    for (const img of $$("img[data-icon]")) img.src = icon(img.dataset.icon);
    Scene.init();
    this.bind();
    const q = new URLSearchParams(location.search);
    this.approveId = q.get("approve") || null;
    this.paired = q.get("paired");
    const invited = q.get("invited");
    if (q.has("paired") || q.has("approve") || q.has("invited")) { q.delete("paired"); q.delete("approve"); q.delete("invited"); history.replaceState(null, "", location.pathname + (q.toString() ? "?" + q : "")); }
    const me = await this.loadMe();
    if (this.paired === "0") this.notice("That link was used or expired: ask your Claude for a new one.");
    if (invited === "0") this.notice("That invite was used or has expired: ask for a new one.");
    if (me?.invite && !me.owner) return this.showInvite(me);
    if (me?.can_view) { if (!this.goNext()) this.showFarm(); }
    else this.showTitle();
  },
  async loadMe() {
    try {
      const r = await fetch("api/me", { credentials: "same-origin" }), me = await r.json();
      App.me = me; App.user = me.user;
      if (App.state) this.renderHud(App.state);
      return me;
    } catch { return null; }
  },
  /** A line for the person: the farm's textbox, or under the title screen's form. */
  notice(text) {
    if (!$("#hud").hidden) return this.say(text);
    this.titleNote = text;
    const p = $("#title-note"); if (p) p.textContent = text;
  },
  /** Back to the page that sent you to log in (?next=dashboards/<name>, ?next=browser, ?next=tasks); only farm-local paths. */
  goNext() {
    const next = new URLSearchParams(location.search).get("next") || "";
    if (!/^(dashboards(\/[a-z0-9-]{1,48})?|browser|tasks)$/.test(next)) return false;
    location.replace(next);
    return true;
  },
  showTitle() {
    clearInterval(App.polling); App.polling = null; clearInterval(App.mePoll);
    for (const d of $$("dialog[open]")) d.close();
    $("#hud").hidden = true; $("#title").hidden = false;
    const priv = App.me ? App.me.private && !App.me.can_view : true;
    // you sign in with your Claude: it gives you a link, or a code to type here (or an invite link lets you in)
    this.accountForms($("#title-forms"), { tabs: ["mine"], note: this.titleNote,
      onDone: async () => { const me = await this.loadMe(); if (me?.can_view) { if (!this.goNext()) this.showFarm(); } else this.showTitle(); } });
    Scene.farm = false; Scene.layout = { clearOf: ".title-card" }; Scene.cam.auto = true; Scene.resize(); // the demo Claudes keep off the title and the form
    Scene.demo = true; fill(Scene.labels, null); Scene.crowEl = null; Scene.critters.clear(); Scene.plotTasks = []; Scene.plotMore = []; Scene.boardCount = 3;
    App.seenAgents = null;
    const hats = ["straw", "beanie", "cap", "sprout", "bow", "headphones"];
    hats.forEach((hat, i) => { const p = Scene.randomSpot(), c = new Critter("demo" + i, p.x, p.y); c.hat = hat; c.color = HAT_COLORS[i + 1]; c.born -= 5000; Scene.critters.set(c.key, c); });
  },
  /** An invite: one thing to do, LOG IN WITH YOUR CLAUDE. Its login hatches this person's own Claude on the farm. */
  showInvite(me) {
    this.invited = true;
    this.showTitle();
    const err = h("p", { class: "form-error", role: "alert" });
    const go = h("button", { class: "btn primary invite-go", type: "button" }, "▶ LOG IN WITH YOUR CLAUDE");
    go.addEventListener("click", async () => {
      go.disabled = true; err.textContent = "";
      try {
        const r = await api("api/agents", { invite: true });
        this.hatchFor = r.id; $("#hatch-body").dataset.key = ""; $("#hatch-h").textContent = "LOG IN YOUR CLAUDE";
        $("#dlg-hatch").showModal(); this.renderHatch({ state: "starting" }); this.beginLogin(r.id);
      } catch (x) { err.textContent = x.message.toUpperCase(); go.disabled = false; }
    });
    fill($("#title-forms"), h("div", { class: "acct-form invite" },
      h("h2", { class: "invite-h", text: `YOU'RE INVITED TO ${String(me.farm || "the farm").toUpperCase()}` }),
      h("p", { text: "Log in with your Claude account and your own Claude joins this farm: it works around the clock on your plan, and you talk to it from the Claude app." }),
      /room for/.test(me.hatch?.why || "") ? h("p", { class: "form-error", text: `${me.hatch.why.toUpperCase()}: ASK WHOEVER INVITED YOU.` }) : [err, go],
      h("p", { class: "muted small", text: "Anthropic's own sign-in: open its link, approve, paste the code back. Your login stays yours. This invite works once." })));
  },
  /** Sign-in: the code (or link) your Claude gives you in the Claude
   * app (MY CLAUDE). The farm's manager is the person of a manager Claude: they sign in to it like anyone. */
  accountForms(box, { tabs, tab, note, onDone }) {
    const NAMES = { viewer: "FARM PASSWORD", mine: "WITH YOUR CLAUDE" };
    const draw = (cur) => {
      const err = h("p", { class: "form-error", role: "alert", id: box.id === "title-forms" ? "title-note" : null, text: note || "" });
      note = "";
      const bar = tabs.length > 1 ? h("div", { class: "tabs", role: "tablist" }, tabs.map(k => h("button", { type: "button", role: "tab",
        class: "tab" + (k === cur ? " on" : ""), "aria-selected": String(k === cur), onclick: () => draw(k) }, NAMES[k]))) : null;
      let form;
      if (cur === "mine") {
        const code = h("input", { name: "code", class: "code-input", maxlength: 6, minlength: 6, autocomplete: "one-time-code", autocapitalize: "characters",
          spellcheck: "false", required: true, placeholder: "ABC123", "aria-label": "6-character code" });
        code.addEventListener("input", () => { code.value = code.value.toUpperCase().replace(/[^A-Z0-9]/g, ""); });
        form = h("form", { class: "acct-form" },
          h("h2", { class: "invite-h", text: "SIGN IN WITH YOUR CLAUDE" }),
          h("ol", { class: "signin-steps" },
            h("li", {}, "Open the ", h("b", { text: "Claude app" }), " (or claude.ai/code) and go to ", h("b", { text: "Code" }), "."),
            h("li", {}, "Open your Claude's session: ", h("b", { text: `[clodfarm] ${App.me?.farm || "your farm"}` }), "."),
            h("li", {}, "Send it: ", h("b", { class: "say", text: "farm login" }))),
          h("p", { class: "muted small hint-line", text: "It answers with a link that signs this device in, or a code to type here." }),
          h("label", {}, "CODE FROM YOUR CLAUDE", code),
          err, h("button", { class: "btn primary", type: "submit" }, "▶ SIGN IN"),
          h("p", { class: "muted small", text: "Invited? Just open your invite link." }));
        form.addEventListener("submit", async (e) => {
          e.preventDefault(); const btn = form.querySelector("button[type=submit]"); btn.disabled = true; err.textContent = "";
          try { const r = await api("api/pair", { code: code.value.trim() }); this.pairedTo = r.claude; await onDone("owner", r); }
          catch (x) { err.textContent = x.message.toUpperCase(); btn.disabled = false; }
        });
      }
      fill(box, bar, form);
      setTimeout(() => form.querySelector("input:not([hidden])")?.focus({ preventScroll: true }), 50);
    };
    draw(tab || tabs[0]);
  },
  showFarm() {
    $("#title").hidden = true; $("#hud").hidden = false;
    App.state = null;
    Scene.farm = true; Scene.layout = {}; Scene.cam.auto = true; Scene.need = null; Scene.resize();
    Scene.demo = false; Scene.critters.clear(); fill(Scene.labels, null); Scene.crowEl = null;
    App.seenAgents = null; App.seenEvents = nowS() - 1; App.plotOf = new Map(); App.approvals = null; App.apprCount = -1;
    this.refresh(true);
    clearInterval(App.polling); clearInterval(App.mePoll);
    App.polling = setInterval(() => this.refresh(), 2500);
    App.mePoll = setInterval(() => this.loadMe(), 30000);
  },
  async refresh(first = false) {
    let st;
    try { st = await api("api/state"); } catch { return; }
    App.state = st;
    for (const t of [...st.subagents, ...st.recent]) App.lastTitles[t.id] = t.title;
    reconcile(st);
    this.renderHud(st);
    this.syncApprovals(st);
    if (first) this.welcome(st);
    else this.pushEvents(st);
    if ($("#dlg-summary").open && this.summaryKey) { const c = Scene.critters.get(this.summaryKey); if (c) this.renderSummary(c); }
    if ($("#dlg-roster").open) this.renderRoster();
    if ($("#dlg-planner").open) this.renderPlanner();
    if (first && this.approveId) this.openApprovals(this.approveId);
  },
  welcome(st) {
    const R = role(), mine = st.agents.find(a => a.mine);
    if (this.paired === "1" || this.pairedTo) { this.say(`You're signed in to ${(mine?.name || this.pairedTo || "your Claude").toUpperCase()}. It's the one with the gold arrow.`); this.paired = null; this.pairedTo = null; }
    const live = st.agents.filter(a => a.loggedIn);
    const primary = st.agents.find(a => a.primary);
    if (!live.length && R.manager && primary && !primary.remote) { this.say(`Welcome to ${st.farm.toUpperCase()}! Your farm's own Claude is still an egg. Tap ▶ LOG IN YOUR CLAUDE (bottom right): name it, dress it, log it in. Nothing grows until it's in.`); return; }
    if (!live.length) { this.say(`Welcome to ${st.farm.toUpperCase()}! No Claude lives here yet. Tap + NEW CLAUDE to hatch the first one.`); return; }
    const n = live.length, busy = st.subagents.filter(t => t.status === "running").length;
    this.say(`Welcome to ${st.farm.toUpperCase()}! ${n} Claude${n === 1 ? "" : "s"} on the farm, ${busy} sub-agent${busy === 1 ? "" : "s"} at work.`);
    if (R.manager || R.owner) this.say("Talk to your Claude from the Claude app on your phone (Remote Control). Tap a Claude for its link.");
    else this.say(n > 12 ? "Drag to look round, pinch or scroll to zoom. Press R (or the list button) to find any Claude." : "Tap a Claude to see what it's doing.");
  },
  pushEvents(st) {
    const T = (e) => App.lastTitles[e.task] || (e.msg || "").replace(/^\S+\s*/, "").slice(0, 60) || e.task;
    for (const e of st.events) {
      if (e.at <= App.seenEvents) continue;
      App.seenEvents = Math.max(App.seenEvents, e.at);
      const f = EVENT_TEXT[e.type];
      const text = f && f(e, T);
      if (text) this.say(text);
      if (e.type === "task.done") { // the plot sparkles and the Claude that ran it cheers
        const p = Scene.plotTasks.findIndex(t => t && t.id === e.task), pl = Scene.world.L.plots[p]; if (pl) Scene.sparkle(pl.x + pl.w / 2, pl.y, 20);
        const who = [...(st.recent || []), ...(st.subagents || [])].find(t => t.id === e.task)?.owner, c = who && Scene.critters.get("claude:" + who);
        if (c && !c.gone) { c.cheerUntil = performance.now() + 2200; Scene.sparkle(c.x, c.y - 18, 12); }
      }
    }
  },
  renderHud(st) {
    const R = role(), me = st.me || {}, hatch = App.me?.hatch;
    const claudes = st.agents.filter(a => a.loggedIn || a.remote).length, eggs = st.agents.length - claudes;
    const busy = st.subagents.filter(t => t.status === "running").length, waiting = st.subagents.length - busy;
    this.renderTokens(st);
    // yours: what your Claude burned, and its usage windows
    const mb = $("#mine-block"), mine = me.claude && st.agents.find(a => a.id === me.claude);
    mb.hidden = !mine;
    if (mine) {
      const b = me.budget || mine.budget || {}, sig = JSON.stringify([mine.name, me.tokens?.total, me.tokens_today?.total, b.five_hour, b.seven_day, me.pending]);
      if (mb.dataset.sig !== sig) {
        mb.dataset.sig = sig;
        fill(mb, h("span", { class: "mine-h" }, h("img", { src: this.spriteURL(agentSkin(mine)), alt: "" }), h("span", {}, "YOUR CLAUDE ", h("b", { text: mine.name.toUpperCase() }))),
          h("span", { class: "mine-t" }, h("b", { text: fmtN(me.tokens_today?.total) }), " tokens today · ", h("b", { text: fmtShort(me.tokens?.total) }), " total"),
          this.hp("5H", b.five_hour, b.five_hour_resets, true), this.hp("7D", b.seven_day, b.seven_day_resets, true));
        mb.setAttribute("aria-label", `Your Claude ${mine.name}: ${fmtN(me.tokens_today?.total)} tokens today. Show it on the farm.`);
      }
    }
    // missions waiting for you
    const n = R.manager ? me.pending_all || 0 : me.pending || 0, chip = $("#approve-chip");
    chip.hidden = !n;
    if (n) { const t = `⚑ ${n} TO APPROVE`; if (chip.textContent !== t) chip.textContent = t; }
    const chips = [
      h("span", { class: "chip" }, h("i", { class: "dot" + (claudes ? "" : " off") }), `${st.farm.toUpperCase()}`, st.private ? " 🔒" : "",
        h("span", { class: "role", text: R.manager ? " · MANAGER" : R.owner ? " · OWNER" : st.private ? " · VIEWER" : "" })),
      h("span", { class: "chip" }, "CLAUDES ", h("b", { text: String(claudes) }), eggs ? ` · EGGS ${eggs}` : ""),
      h("span", { class: "chip" }, "SUB-AGENTS ", h("b", { text: String(busy) }), waiting ? [" · WAITING ", h("b", { text: String(waiting) })] : null),
    ];
    if (st.paused) chips.push(h("span", { class: "chip warn" }, "⏸ PAUSED: " + (st.pause_reason || "").slice(0, 40).toUpperCase()));
    const sig = JSON.stringify([st.farm, st.private, R, claudes, eggs, busy, waiting, st.paused, st.pause_reason]);
    if ($("#chips").dataset.sig !== sig) { $("#chips").dataset.sig = sig; fill($("#chips"), ...chips); }
    // the dock, by role: watchers see the farm and the tasks; people see their Claude; the manager sees everything
    const person = R.manager || !!R.owner;
    $("#hud").classList.toggle("spectator", !person && !R.viewer); // a public farm's visitor: the tokens and the Claudes at work
    $("#talk-tool").hidden = !person;
    this.renderConnTool(st, R);
    $("#dash-tool").hidden = !person;
    $("#browser-tool").hidden = !R.owner; // the browser is a Claude's tool: signed in to yours, or no browser
    $("#manager-tool").hidden = !R.manager;
    // a new farm: its own Claude is still an egg, and the manager's first job is to log it in (openHatch does that)
    const primary = st.agents.find(a => a.primary), live = st.agents.some(a => a.loggedIn || a.remote);
    const first = R.manager && !!primary && !primary.loggedIn && !primary.remote;
    const capped = !!hatch && !hatch.can && /room for/.test(hatch.why || ""); // the farm's size (its host's): no more, the manager's either
    const ht = $("#hatch-tool"), can = first || (R.manager ? !capped : hatch ? hatch.can : !R.owner);
    ht.hidden = !!R.owner && !R.manager;
    ht.classList.toggle("off", !can);
    ht.classList.toggle("call", can && !live); // nothing works until a Claude is in: point at the one thing to do
    ht.setAttribute("aria-disabled", String(!can));
    const inviting = R.manager && !!primary && (primary.loggedIn || !!primary.remote); // more Claudes come by invite
    const lbl = ht.querySelector(".lbl"), want = first ? "login" : inviting ? "invite" : "new";
    if (lbl.dataset.mode !== want) {
      lbl.dataset.mode = want;
      if (first) fill(lbl, "▶ LOG IN", h("span", { class: "long", text: " YOUR CLAUDE" }));
      else if (inviting) fill(lbl, h("span", { class: "plus", text: "+ " }), "INVITE", h("span", { class: "long", text: " A CLAUDE" }));
      else fill(lbl, h("span", { class: "plus", text: "+ " }), "NEW", h("span", { class: "long", text: " CLAUDE" }));
    }
    ht.dataset.tip = first ? "Start here: log in your Claude" : inviting ? "Invite a Claude" : can ? "Add a Claude or a bot" : `Can't hatch: ${hatch?.why || "not now"}`;
    ht.dataset.desc = first ? "Your farm's own Claude: name it, dress it, then log it in with your Claude account"
      : inviting ? "A one-time link: someone logs in with their Claude account and joins the farm"
      : can ? "Hatch a new Claude: pick its look, log it in" : capped ? `Full: ${hatch.why}` : "The farm's manager decides who can hatch";
    ht.setAttribute("aria-label", `${ht.dataset.tip} (C)`);
    if ($("#dlg-claude").open) this.renderClaude(st);
    for (const gp of $$(".dock-group")) { // a tray shows when it has buttons; on a phone it's as wide as its buttons
      const n = [...gp.children].filter(b => !b.hidden && getComputedStyle(b).display !== "none").length;
      gp.hidden = !n; gp.style.setProperty("--n", String(n || 1));
    }
    this.placeDock();
  },
  /** Keep the textbox clear of the dock: beside it when there's room, else just above it. And tell the farm's fit
   * how tall the dock is. */
  placeDock() {
    const dock = $("#dock"), box = $("#textbox");
    if (!dock || !box) return;
    const r = dock.getBoundingClientRect(), gap = 12, room = r.left - 14 - gap, sig = [Math.round(r.left), Math.round(r.top), innerWidth].join();
    if (this.dockSig === sig) return;
    this.dockSig = sig;
    const beside = room >= 340 && innerWidth > 760;
    box.classList.toggle("above", !beside);
    box.style.width = beside ? Math.min(560, room) + "px" : "";
    box.style.bottom = beside ? "" : Math.round(innerHeight - r.top + 10) + "px";
    if (Scene.farm && Scene.cam.auto) { clearTimeout(Scene.resizeT); Scene.resizeT = setTimeout(() => Scene.resize(), 60); }
  },

  // ------------------------------------------------------------------ tokens
  /** The farm's burn counter: it glides to each new total over the time between polls, so it never stops ticking. */
  renderTokens(st) {
    const T = st.tokens || {}, tot = T.total || {}, day = T.today || {};
    const target = Number(tot.total) || 0, k = this.tok || (this.tok = { shown: 0, from: 0, target: 0, t0: 0, dur: 1200 });
    if (target !== k.target) {
      Object.assign(k, { from: k.shown, target, t0: performance.now(), dur: k.target ? 2400 : 1400 });
      if (!this.tokRAF) { const step = () => { this.tokRAF = null; this.tickTokens(); if (this.tok.shown !== this.tok.target) this.tokRAF = requestAnimationFrame(step); }; this.tokRAF = requestAnimationFrame(step); }
    }
    $("#tok-today").textContent = `today: ${fmtN(day.total)}`;
    const sig = JSON.stringify([tot, day]), sign = $("#tokens-sign");
    if (sign.dataset.sig === sig) return;
    sign.dataset.sig = sig;
    const row = (label, key) => [h("span", { text: label }), h("span", { text: fmtN(tot[key]) }), h("span", { text: fmtN(day[key]) })];
    fill(sign, h("span", { class: "tok-grid" }, h("span", {}), h("b", { text: "TOTAL" }), h("b", { text: "TODAY" }),
      row("INPUT", "input"), row("OUTPUT", "output"), row("CACHE WRITE", "cache_write"), row("CACHE READ", "cache_read"),
      h("b", { text: "ALL" }), h("b", { text: fmtN(tot.total) }), h("b", { text: fmtN(day.total) })),
      h("span", { class: "tok-note", text: "Every Claude on the farm, every sub-agent and conversation." }));
  },
  tickTokens() {
    const k = this.tok, p = REDUCED ? 1 : clamp((performance.now() - k.t0) / k.dur, 0, 1), e = 1 - (1 - p) * (1 - p);
    k.shown = p >= 1 ? k.target : Math.round(k.from + (k.target - k.from) * e);
    const t = fmtN(k.shown), el = $("#tok-total");
    if (el.textContent !== t) el.textContent = t;
  },
  focusMine(open = false) {
    const id = App.state?.me?.claude; if (!id) return;
    this.focusAgent(id, open);
  },
  /** Point the camera at a Claude (zoomed in enough to read names) and select it. */
  focusAgent(id, open = true) {
    const c = Scene.critters.get("claude:" + id) || Scene.critters.get("egg:" + id);
    if (!c) return;
    Scene.focusCritter(c); Scene.selected = c;
    if (open) this.openCritter(c);
  },

  // ---------------------------------------------------------------- textbox
  say(text) { if (!$("#textbox")) return; this.queue.push(text); if (this.queue.length > 6) this.queue.splice(0, this.queue.length - 6); if (!this.typing) this.next(); },
  next() {
    const box = $("#textbox"), p = $("#textbox-text");
    clearTimeout(this.hideT);
    const text = this.queue.shift();
    if (text == null) { this.typing = null; this.hideT = setTimeout(() => box.classList.add("idle"), 7000); return; }
    box.classList.remove("idle");
    let i = 0;
    this.typing = { text, done: false };
    clearInterval(this.typeT);
    this.typeT = setInterval(() => {
      i = Math.min(text.length, i + (REDUCED ? text.length : 2));
      p.textContent = text.slice(0, i);
      if (i >= text.length) { clearInterval(this.typeT); this.typing.done = true; this.hideT = setTimeout(() => this.next(), this.queue.length ? 1800 : 5000); }
    }, 28);
  },
  skip() {
    if (!this.typing) return $("#textbox").classList.add("idle");
    if (!this.typing.done) { clearInterval(this.typeT); $("#textbox-text").textContent = this.typing.text; this.typing.done = true; clearTimeout(this.hideT); this.hideT = setTimeout(() => this.next(), 4000); }
    else { clearTimeout(this.hideT); this.next(); }
  },

  // ------------------------------------------------------------------- binds
  bind() {
    document.addEventListener("click", (e) => {
      const act = e.target.closest("[data-act]")?.dataset.act;
      if (act) { this.act(act, e); return; }
      if (e.target.closest("[data-close]")) e.target.closest("dialog").close();
      if (!e.target.closest("#tokens")) $("#tokens").classList.remove("open");
    });
    $("#textbox").addEventListener("click", () => this.skip());
    addEventListener("resize", () => { this.dockSig = null; this.placeDock(); if ($("#dlg-conn-menu").open) this.placeConnMenu(); });
    $("#dlg-conn-menu").addEventListener("keydown", (e) => this.connMenuKeys(e));
    $("#connector-back").addEventListener("click", () => this.openConnMenu());
    $("#tokens").addEventListener("click", () => $("#tokens").classList.toggle("open"));
    $("#mine-block").addEventListener("click", () => this.focusMine(true)); // your Claude's card, right away
    $("#roster-q").addEventListener("input", () => this.renderRoster());
    $("#roster-sort").addEventListener("change", () => this.renderRoster());
    for (const d of $$("dialog")) {
      d.addEventListener("click", (e) => { // a click on the backdrop (outside the box) closes it
        const r = d.getBoundingClientRect();
        if (e.target === d && (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom)) d.close();
      });
      d.addEventListener("close", () => {
        if (d.id === "dlg-slack") clearTimeout(this.slackPoll);
        if (d.id === "dlg-conn-menu") { $("#conn-tool").setAttribute("aria-expanded", "false"); if (this.connBack) $("#conn-tool").focus({ preventScroll: true }); }
        if (d.id === "dlg-hatch") this.stopHatchPoll();
        if (d.id === "dlg-summary") { this.summaryKey = null; Scene.selected = null; }
        if (d.id === "dlg-settings") clearInterval(this.previewT);
        if (d.id === "dlg-hatch") clearInterval(this.previewT);
      });
    }
    addEventListener("keydown", (e) => {
      if ($("#hud").hidden || $$("dialog[open]").length || /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName) || e.metaKey || e.ctrlKey || e.altKey) return;
      const k = { c: "hatch", h: "hatch", n: "hatch", s: "connectors", t: "talk", d: "dashboards", b: "browser", j: "tasks", r: "roster", g: "manager",
        a: "approvals", m: "menu", p: "planner", "?": "help", "0": "fit", f: "fit", "+": "zoomin", "=": "zoomin", "-": "zoomout" }[e.key.toLowerCase()];
      if (k) { e.preventDefault(); this.act(k); }
    });
  },
  act(a) {
    const R = role(), person = R.manager || !!R.owner;
    if (a === "hatch") { // the farm's own Claude first; then more Claudes come by invite
      const R = role(), p = App.state?.agents.find(x => x.primary);
      return R.manager && p && (p.loggedIn || p.remote) ? this.openInvite() : this.openHatch(null, true);
    }
    if (a === "roster") return this.openRoster();
    if (a === "approvals") return this.openApprovals();
    if (a === "menu") return this.openMenu();
    if (a === "help") return this.openHelp();
    if (a === "planner") return this.openPlanner();
    if (a === "fit") { Scene.goal = null; return Scene.fit(); }
    if (a === "zoomin") return Scene.zoomBy(1.4);
    if (a === "zoomout") return Scene.zoomBy(1 / 1.4);
    if (a === "manager") return R.manager ? this.openManager() : null;
    if (a === "slack") return R.manager ? this.openSlack() : null;
    if (a === "connectors") return person ? this.openConnMenu() : null;
    if (a === "talk") return person ? this.openClaude() : null;
    if (a === "dashboards" && person) location.href = "dashboards";
    if (a === "browser" && R.owner) location.href = "browser";
    if (a === "tasks") location.href = "tasks";
  },

  // ------------------------------------------------------ talk to your Claude
  openClaude() {
    for (const d of $$("dialog[open]")) d.close();
    $("#claude-body").dataset.key = "";
    this.renderClaude(App.state);
    const d = $("#dlg-claude");
    d.showModal();
    requestAnimationFrame(() => { d.scrollTop = 0; d.querySelector(".x").focus({ preventScroll: true }); }); // open at the top
  },
  renderClaude(st) {
    if (!st) return;
    const R = role();
    const me = st.agents.find(a => a.mine) || (R.manager ? st.agents.find(a => a.primary) || st.agents.find(a => a.loggedIn) : null), name = me?.name || st.farm;
    const none = !me || !me.loggedIn;
    const link = me?.remote_control, body = $("#claude-body"), key = JSON.stringify([name, link, none, R.manager]);
    if (body.dataset.key === key) return;
    body.dataset.key = key;
    const mcp = `claude mcp add --transport http --scope user ${st.farm} ${location.origin}${location.pathname.replace(/\/$/, "")}/mcp`;
    const copy = h("button", { type: "button", class: "btn", text: "COPY", onclick: async (e) => {
      try { await navigator.clipboard.writeText(mcp); e.target.textContent = "COPIED ✓"; } catch { e.target.textContent = "SELECT IT"; }
      setTimeout(() => (e.target.textContent = "COPY"), 1500);
    } });
    fill(body,
      none ? h("p", { class: "banner-note" }, h("strong", { text: "FIRST, LOG IN A CLAUDE. " }), me ? `${name.toUpperCase()} waits for its login: tap its egg.` : "No Claude of yours lives on this farm yet: tap ",
        me ? null : h("strong", { text: "+ NEW CLAUDE" }), me ? null : " and log in with your Claude account. Then:") : null,
      h("p", {}, me?.mine ? "Your Claude, " : "Your farm's own Claude, ", h("strong", { text: name.toUpperCase() }), ", is always on. Talk to it from the Claude app on your phone or computer."),
      h("ol", { class: "hatch-steps" },
        h("li", {}, "Open the ", h("strong", { text: "Claude app" }), " (or claude.ai)."),
        h("li", {}, "Go to ", h("strong", { text: "Code" }), "."),
        h("li", {}, "Pick the session ", h("strong", { class: "session-name", text: sessionName(st, name) }), link ? " (or use the button below)." : ".")),
      h("h3", { class: "kicker", text: "ASK IT ANYTHING" }),
      h("ul", { class: "examples" },
        h("li", { text: "“Fix the flaky login test and tell me when it's on main.”" }),
        h("li", { text: "“Split the migration into 3 sub-agents.”" }),
        h("li", { text: "“Every weekday at 9, check the errors and keep a dashboard of them.”" })),
      h("p", { class: "muted small", text: "It starts sub-agents (you see them here as mini Claudes), asks the other Claudes for help, schedules work and builds dashboards, and it watches its budget." }),
      R.manager ? [h("h3", { class: "kicker", text: "OR FROM CLAUDE CODE ON YOUR COMPUTER" }),
        h("div", { class: "copy-row" }, h("code", { class: "pre", text: mcp }), copy),
        h("p", { class: "muted small", text: "Then run /mcp in Claude Code: it opens this farm, where you're signed in to your Claude, to connect." })] : null);
    fill($("#claude-actions"), none && !me
      ? h("button", { class: "btn primary", type: "button", onclick: () => { $("#dlg-claude").close(); this.openHatch(null, true); } }, "+ NEW CLAUDE")
      : h("a", { class: "btn primary", href: link || "https://claude.ai/code", target: "_blank", rel: "noopener noreferrer", text: "OPEN IN CLAUDE ↗" }));
  },

  // ----------------------------------------------------------------- dialogs
  hp(label, util, resets, compact) {
    if (util == null) { const i = h("i"); i.style.width = "0"; return h("span", { class: "hp" + (compact ? " compact" : "") }, h("span", { text: label }), h("span", { class: "bar" }, i), h("span", { class: "lbl", text: compact ? "–" : "NOT MEASURED YET" })); }
    // like Claude's own usage page: how much of the limit is used, filling up to 100%, and when it resets
    const used = clamp(util, 0, 1), cls = used < 0.5 ? "" : used < 0.8 ? "mid" : "low";
    const bar = h("i", { class: cls }); bar.style.width = `${Math.round(used * 100)}%`;
    return h("span", { class: "hp" + (compact ? " compact" : ""), title: `${Math.round(util * 100)}% used` + (resets ? `, resets in ${until(resets)}` : "") }, h("span", { text: label }), h("span", { class: "bar" }, bar),
      h("span", { class: "lbl", text: compact ? `${Math.round(used * 100)}%` : `${Math.round(used * 100)}% USED${resets ? " · RESETS IN " + until(resets) : ""}` }));
  },
  /** A Claude's portrait as an image URL (cached per skin): for lists, the HUD and buttons. */
  spriteURL(skin, pose = { legs: 0 }) { // 36 x 36: show it at 36px (or a whole multiple) so its pixels stay square
    this.urls = this.urls || new Map();
    const k = skin.key + poseCode(pose);
    if (!this.urls.has(k)) { const c = canvas(36, 36), g = c.getContext("2d"); g.drawImage(skinFrameHD(skin, pose), 2, 0); this.urls.set(k, c.toDataURL()); }
    return this.urls.get(k);
  },
  spriteCanvas(c, size = 40) {
    const cv = h("canvas", { "aria-hidden": "true" });
    paintSprite(cv, c.kind === "egg" ? EGG_HD : skinFrameHD(c.skin || skinOf(c.hat, null, "", c.color), { legs: 0 }), size);
    return cv;
  },
  agentState(a) {
    if (!a.loggedIn && !a.remote) return a.login?.state === "waiting_code" ? "WAITING FOR ITS LOGIN CODE" : "AN EGG: NEEDS A CLAUDE LOGIN";
    if (!a.alive) return "NOT RUNNING";
    if (!a.up) return "WAKING UP… (MEASURING ITS USAGE)";
    if (a.error) return a.error.toUpperCase();
    const n = App.state.subagents.filter(t => t.owner === a.id).length;
    if (n) return `${n} SUB-AGENT${n === 1 ? "" : "S"} AT WORK`;
    if (a.bot) return a.resting ? "RESTING: " + (a.budget?.reason || "paced").toUpperCase() : "READY: SEND IT WORK";
    if (a.talking) return a.talking.n > 1 ? `WORKING IN ${a.talking.n} CONVERSATIONS` : "WORKING IN A CONVERSATION";
    if (a.resting) return "RESTING: " + (a.budget?.reason || "paced by its budget").toUpperCase();
    return "READY: TALK TO IT";
  },
  openCritter(c) {
    if (c.kind === "egg" && canManage(c.agent)) return this.openHatch(c.agent.id);
    for (const d of $$("dialog[open]")) d.close();
    this.summaryKey = c.key; Scene.selected = c;
    this.renderSummary(c);
    $("#dlg-summary").showModal();
  },
  renderSummary(c) {
    const st = App.state, R = role(), badge = (t) => h("span", { class: `badge ${t.status}`, text: { running: "WORKING", queued: "WAITING", waiting: "WAITING", done: "DONE", failed: "FAILED", cancelled: "CANCELLED", pending: "TO APPROVE", denied: "DENIED" }[t.status] || t.status.toUpperCase() });
    paintSprite($("#sum-sprite"), c.mini ? miniHD(c.color, {}) : c.kind === "egg" ? EGG_HD : skinFrameHD(c.skin || skinOf(c.hat, null, "", c.color), { legs: 0 }), 96, { bg: tileBg, pad: 8, bottom: true });
    let parts;
    if (c.mini) { // a sub-agent
      const t = st.subagents.find(x => x.id === c.sub.id) || c.sub, all = new Map(st.subagents.map(x => [x.id, x]));
      const kids = (t.children || []).map(id => all.get(id)).filter(Boolean), parent = t.parent && all.get(t.parent);
      const see = canManage(c.agent);
      $("#sum-name").textContent = `SUB-AGENT · ${c.agent.name}`.toUpperCase();
      $("#sum-sub").textContent = `Started by ${c.agent.name}` + (t.on && t.on !== c.agent.id ? `, running on ${t.on}'s account` : "");
      parts = [h("dl", { class: "stat-row" },
        h("dt", { text: "STATUS" }), h("dd", { text: t.status === "running" ? `WORKING ON ${String(t.on || "").toUpperCase()}'S BUDGET`
          : t.status === "waiting" ? "WAITING FOR ITS OWN SUB-AGENTS" : t.to ? `WAITING FOR ${t.to.toUpperCase()}` : "WAITING FOR A CLAUDE WITH BUDGET" }),
        t.started ? [h("dt", { text: "STARTED" }), h("dd", { text: ago(t.started) })] : null,
        parent ? [h("dt", { text: "HELPING" }), h("dd", { text: parent.title })] : null),
        h("h3", { text: "ITS JOB" }), h("p", { class: "job-text", text: t.title }),
        kids.length ? [h("h3", { text: `ITS SUB-AGENTS (${kids.length})` }), h("ul", { class: "subs" }, kids.map(k => h("li", {}, badge(k), h("span", { text: k.title }))))] : null,
        see ? [h("h3", { text: "ITS SESSION" }), this.sessionList(c.agent.id, t.id)] : null,
        see ? h("p", { class: "muted small", text: `Its result: ask ${c.agent.name}, or run clodfarm result ${t.id}` }) : null,
        h("p", { class: "small" }, h("a", { href: "tasks", text: "See every sub-agent on the TASKS page →" }))];
      fill($("#sum-body"), parts); fill($("#sum-actions")); fill($("#sum-top"));
      return;
    }
    const a = st.agents.find(x => x.id === c.agent.id) || c.agent, b = a.budget || {}, see = canManage(a);
    const mine = st.subagents.filter(t => t.owner === a.id), elsewhere = st.subagents.filter(t => t.on === a.id && t.owner !== a.id);
    const tokens = st.tokens?.by_claude?.[a.id], waitingN = Scene.pendingFor[a.id] || 0;
    $("#sum-name").textContent = a.name.toUpperCase();
    $("#sum-sub").textContent = a.bot ? `BOT · ${a.bot.model}` + (a.bot.via ? ` via ${a.bot.via}` : "")
      : [a.plan && `Claude ${a.plan}`, a.email].filter(Boolean).join(" · ");
    const tags = [a.mine ? h("span", { class: "badge mine", text: "★ YOURS" }) : null,
      a.approve_missions ? h("span", { class: "badge waiting", text: "✓ APPROVES MISSIONS", title: "Its person OKs every mission sent to it" }) : null,
      a.tools_off?.length ? h("span", { class: "badge cancelled", text: `TOOLS OFF: ${a.tools_off.join(", ").toUpperCase()}` }) : null,
      a.planner_host_ok ? h("span", { class: "badge done", text: "PLANNER CAN RUN HERE" }) : null].filter(Boolean);
    parts = [tags.length ? h("p", { class: "tags" }, tags) : null,
      h("dl", { class: "stat-row" },
        h("dt", { text: "STATUS" }), h("dd", { text: this.agentState(a) }),
        tokens != null ? [h("dt", { text: "TOKENS" }), h("dd", { text: fmtN(tokens) })] : null,
        a.stats ? [h("dt", { text: "RAN (7D)" }), h("dd", { text: `${a.stats.ran} sub-agents · ${a.stats.done} done · ${a.stats.failed} failed` })] : null)];
    if (c.kind === "egg") parts.push(h("p", { class: "muted small", text: see ? "Tap it on the farm to log it in." : "It hatches once its person logs it in to a Claude account." }));
    if (waitingN && (R.manager || a.mine)) parts.push(h("button", { class: "btn danger pulse wide-btn", type: "button", onclick: () => this.openApprovals(null, a.id) }, `⚑ ${waitingN} MISSION${waitingN === 1 ? "" : "S"} TO APPROVE`));
    if (a.bot) parts.push(h("h3", { text: "SEND IT WORK" }),
      h("p", { class: "muted small", text: `It's Claude Code on ${a.bot.model}, not Claude: it uses no Claude account's usage, but it's weaker. ` +
        (a.bot.takes === "any" ? "It takes any sub-agent, " : "It takes only the sub-agents sent to it, ") +
        `so ask a Claude to hand it a well-specified job with --on ${a.id}, or run: clodfarm spawn "<title>" --prompt "…" --on ${a.id}` }));
    else if (a.loggedIn && see) parts.push(h("h3", { text: "TALK TO IT" }), a.remote_control
      ? [h("a", { class: "btn primary login-link", href: a.remote_control, target: "_blank", rel: "noopener noreferrer" }, "OPEN IN THE CLAUDE APP ↗"),
        h("p", { class: "muted small", text: `Or open the Claude app, go to Code and pick “${sessionName(st, a.name)}”. It starts sub-agents, asks the other Claudes for help and schedules work, and it watches its budget.` })]
      : h("p", { class: "muted small", text: a.remote ? "It lives on another box: talk to it from its own Claude app." : `Its Remote Control session is starting. It shows up in the Claude app under Code as “${sessionName(st, a.name)}”.` }));
    if (a.bot) parts.push(h("h3", { text: "PACE" }), h("p", { class: "muted small", text: (b.can_start ? `It can start ${b.can_start} more sub-agent${b.can_start === 1 ? "" : "s"} now. ` : b.reason ? `No new sub-agents now: ${b.reason}. ` : "")
      + `It pauses when ${a.bot.via || "its provider"} rate-limits it.` }));
    else if (c.kind !== "egg") parts.push(h("h3", { text: "USAGE" }), this.hp("5H", b.five_hour, b.five_hour_resets), this.hp("7D", b.seven_day, b.seven_day_resets),
      h("p", { class: "muted small", text: b.measured ? `Measured ${ago(b.measured)}. ` + (b.can_start ? `It can start ${b.can_start} more sub-agent${b.can_start === 1 ? "" : "s"} now.` : `No new sub-agents on its account now: ${b.reason}.`)
        : "Measuring its usage…" }));
    if (mine.length) parts.push(h("h3", { text: `ITS SUB-AGENTS (${mine.length})` }), h("ul", { class: "subs" }, mine.slice(0, 30).map(t => h("li", {}, badge(t),
      h("span", { text: t.title + (t.on && t.on !== a.id ? ` · on ${t.on}` : "") })))), mine.length > 30 ? h("p", { class: "muted small", text: `…and ${mine.length - 30} more on the TASKS page.` }) : null);
    if ((a.loggedIn || a.remote) && see) parts.push(h("h3", { text: "TOOLS" }), this.toolList(a.id));
    if (see) parts.push(h("h3", { text: "SESSIONS" }), this.sessionList(a.id));
    if (elsewhere.length) parts.push(h("h3", { text: `HELPING OTHERS (${elsewhere.length})` }), h("ul", { class: "subs" }, elsewhere.slice(0, 20).map(t => h("li", {}, badge(t),
      h("span", { text: `${t.title} · for ${t.owner}` })))));
    fill($("#sum-body"), parts);
    // its person's own controls, up top next to its name: its look, and everything else
    fill($("#sum-top"), see && !a.remote ? [
      h("button", { class: "btn primary small-btn", type: "button", onclick: () => this.openSettings(a.id, "LOOK") }, "✎ CUSTOMIZE"),
      h("button", { class: "btn small-btn", type: "button", onclick: () => this.openSettings(a.id) }, "⚙ SETTINGS")] : null);
    fill($("#sum-actions"));
  },

  // ------------------------------------------------------------------- tools
  /** What a Claude can use: its model, MCP servers (and whether they're connected), tools, skills and plugins, as
   * Claude Code reported them when it last ran a sub-agent on the farm. */
  toolList(claude) {
    const box = h("div", { class: "tools" });
    this.toolsOpen = this.toolsOpen || new Set();
    const group = (id, title, items, render) => {
      if (!items.length) return null;
      const d = h("details", { class: "tool-group", open: this.toolsOpen.has(id) },
        h("summary", {}, title, h("span", { class: "muted", text: ` (${items.length})` })), h("div", { class: "tool-chips" }, items.map(render)));
      d.addEventListener("toggle", () => d.open ? this.toolsOpen.add(id) : this.toolsOpen.delete(id));
      return d;
    };
    const chip = (text, cls = "") => h("span", { class: `tool-chip ${cls}`, text });
    const draw = (t) => {
      if (!t || !t.tools) return fill(box, h("p", { class: "muted small", text: "Shown after its first sub-agent or usage check runs." }));
      const slug = n => "mcp__" + n.replace(/[^A-Za-z0-9_-]/g, "_") + "__";
      const mcpTools = t.tools.filter(x => x.startsWith("mcp__")), builtIn = t.tools.filter(x => !x.startsWith("mcp__"));
      const STATUS = { connected: ["ok", "CONNECTED"], "needs-auth": ["wait", "NEEDS SIGN-IN"], pending: ["wait", "STARTING"], failed: ["bad", "FAILED"], disabled: ["off", "OFF"] };
      fill(box,
        h("p", { class: "small tool-meta", text: [t.model && `Model ${t.model}`, t.version && `Claude Code ${t.version}`, t.permission_mode && `permissions: ${t.permission_mode}`].filter(Boolean).join(" · ") }),
        t.mcp_servers.length ? h("ul", { class: "mcp-list" }, t.mcp_servers.map(m => {
          const [cls, label] = STATUS[m.status] || ["off", String(m.status || "?").toUpperCase()], n = mcpTools.filter(x => x.startsWith(slug(m.name))).length;
          return h("li", {}, h("i", { class: `mcp-dot ${cls}`, "aria-hidden": "true" }), h("span", { class: "mcp-name", text: m.name }),
            h("span", { class: "muted", text: n ? ` · ${n} tool${n === 1 ? "" : "s"}` : "" }), h("span", { class: `mcp-status ${cls}`, text: label }));
        })) : h("p", { class: "muted small", text: "No MCP servers." }),
        group("builtin", "BUILT-IN TOOLS", builtIn, x => chip(x)),
        group("mcp", "MCP TOOLS", mcpTools, x => { // "mcp__claude_ai_Gmail__search" -> "claude.ai Gmail › search"
          const m = t.mcp_servers.find(m => x.startsWith(slug(m.name)));
          return chip(m ? `${m.name} › ${x.slice(slug(m.name).length)}` : x.replace(/^mcp__/, "").replace("__", " › "), "mcp");
        }),
        group("skills", "SKILLS", t.skills, x => chip(x)),
        group("plugins", "PLUGINS", t.plugins, p => chip(p.version ? `${p.name} ${p.version}` : p.name)),
        group("agents", "SUB-AGENT TYPES", t.agents, x => chip(x)),
        h("p", { class: "muted small", text: `As its ${t.where === "usage check" ? "hourly usage check" : "last sub-agent"} saw it, ${ago(t.at)}.` }));
    };
    const hit = this.toolCache?.[claude];
    if (hit) draw(hit.t);
    if (!hit || Date.now() - hit.at > 30000) api(`api/agents/${encodeURIComponent(claude)}/tools`).then(t => {
      this.toolCache = { ...(this.toolCache || {}), [claude]: { at: Date.now(), t } }; draw(t);
    }).catch(() => {});
    return box;
  },

  // ---------------------------------------------------------------- sessions
  /** A Claude's sessions (conversations, sub-agent runs), every one recorded in the farm's store by its hook. */
  sessionList(claude, task) {
    const box = h("div", { class: "sessions" }), key = claude + "|" + (task || "");
    const draw = (rows) => {
      rows = rows.filter(s => task ? s.task === task : s.kind !== "usage");
      if (!rows.length) return fill(box, h("p", { class: "muted small", text: task ? "Its session is recorded once it starts." : "No sessions recorded yet. Talk to it in the Claude app: every conversation shows up here." }));
      fill(box, h("ul", { class: "subs" }, rows.slice(0, 8).map(s => h("li", {},
        h("span", { class: `badge ${s.kind === "conversation" ? "done" : "running"}`, text: s.kind === "conversation" ? "TALK" : "SUB-AGENT" }),
        h("a", { href: "#", onclick: (e) => { e.preventDefault(); this.openSession(s.id, this.summaryKey); } }, (s.title || "(untitled)").slice(0, 70)),
        h("span", { class: "muted", text: ` · ${s.turns || 0} turns · ${ago(s.last_at)}` })))));
    };
    const hit = this.sessCache?.[key];
    if (hit) draw(hit.rows);
    if (!hit || Date.now() - hit.at > 8000) api(`api/sessions?claude=${encodeURIComponent(claude)}`).then(rows => {
      this.sessCache = { ...(this.sessCache || {}), [key]: { at: Date.now(), rows } }; draw(rows);
    }).catch(() => {});
    return box;
  },
  async openSession(id, back) {
    for (const d of $$("dialog[open]")) d.close();
    $("#talk-kicker").textContent = "SESSION"; $("#talk-h").textContent = "…"; fill($("#talk-body"), h("p", { class: "muted", text: "Loading…" }));
    fill($("#talk-actions"), back ? h("button", { class: "btn", type: "button", onclick: () => { const c = Scene.critters.get(back); if (c) this.openCritter(c); } }, "◀ BACK") : null);
    $("#dlg-talk").showModal();
    let s;
    try { s = await api(`api/sessions/${id}`); } catch (x) { fill($("#talk-body"), h("p", { class: "form-error", text: x.message })); return; }
    $("#talk-kicker").textContent = `${s.kind === "conversation" ? "CONVERSATION" : "SUB-AGENT SESSION"} · ${String(s.claude || "").toUpperCase()} · ${ago(s.started)}`;
    $("#talk-h").textContent = (s.title || "(untitled)").slice(0, 90);
    const who = (t) => t.kind === "tool_result" ? "TOOL" : t.role === "assistant" ? String(s.claude || "CLAUDE").toUpperCase() : s.kind === "conversation" ? "YOU" : "THE FARM";
    fill($("#talk-body"), s.conversation.length ? s.conversation.map(t => h("div", { class: `turn ${t.role} k-${t.kind}` },
      h("b", { text: who(t) + (t.kind === "tool" ? " · TOOL CALL" : "") }), h("div", { text: t.text }))) : h("p", { class: "muted", text: "Nothing said yet." }));
  },

  // -------------------------------------------------------------------- slack
  /** Give the farm work from Slack: one Slack app made from a prefilled manifest, two tokens pasted back. */
  async openSlack() {
    for (const d of $$("dialog[open]")) d.close();
    fill($("#slack-body"), h("p", { class: "muted", text: "Loading…" }));
    $("#dlg-slack").showModal();
    await this.loadSlack();
  },
  async loadSlack() {
    clearTimeout(this.slackPoll);
    if (!$("#dlg-slack").open) return;
    let s;
    try { s = await api("api/slack"); } catch (x) { fill($("#slack-body"), h("p", { class: "form-error", text: x.message })); return; }
    this.renderSlack(s);
    if (s.configured && !["live", "error", "off"].includes(s.state)) this.slackPoll = setTimeout(() => this.loadSlack(), 1500);
  },
  renderSlack(s) {
    const body = $("#slack-body"), key = JSON.stringify([s.configured, s.state, s.error, s.team, s.allow, s.last?.at]);
    if (body.dataset.key === key && body.contains(document.activeElement)) return; // don't wipe what's being typed
    body.dataset.key = key;
    const splitAllow = (v) => String(v || "").split(/[\s,]+/).filter(Boolean);
    const allowInput = (value) => h("input", { name: "allow", autocomplete: "off", spellcheck: "false", value: (value || []).join(", "),
      placeholder: "everyone in the workspace (no guests)" });
    if (s.configured) {
      const cls = s.state === "live" ? "live" : s.state === "error" ? "error" : "wait";
      const status = { live: `● CONNECTED TO ${String(s.team || "SLACK").toUpperCase()}`, error: "● NOT CONNECTED", wait: "● CONNECTING…" }[cls];
      const bot = s.bot ? "@" + s.bot : "the farm's app";
      const allowForm = h("form", { class: "slack-form" },
        h("div", { class: "row" }, h("label", {}, "WHO CAN GIVE IT WORK", allowInput(s.allow)), h("button", { class: "btn", type: "submit" }, "SAVE")),
        h("p", { class: "muted small", text: "Emails or Slack member IDs, separated by commas. Empty: every full member of the workspace. Guests, bots and people from other companies in shared channels never can." }),
        h("p", { class: "form-error", role: "alert" }));
      allowForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const btn = allowForm.querySelector("button"); btn.disabled = true;
        try { const r = await api("api/slack/allow", { allow: new FormData(allowForm).get("allow") }); btn.textContent = "SAVED ✓"; setTimeout(() => this.renderSlack(r), 900); }
        catch (x) { allowForm.querySelector(".form-error").textContent = x.message; btn.disabled = false; }
      });
      const acts = [];
      if (!s.from_env) {
        const off = h("button", { class: "btn danger", type: "button" }, "DISCONNECT");
        off.addEventListener("click", async () => {
          if (off.dataset.sure !== "1") { off.dataset.sure = "1"; off.textContent = "SURE? IT STOPS LISTENING"; return; }
          off.disabled = true;
          try { const r = await api("api/slack/disconnect", {}); this.say("The farm left Slack."); body.dataset.key = ""; this.renderSlack(r); this.refresh(); }
          catch (x) { off.textContent = x.message.slice(0, 40); }
        });
        acts.push(off);
      }
      if (cls === "error") acts.push(h("button", { class: "btn primary", type: "button", onclick: () => { body.dataset.key = ""; this.renderSlack({ ...s, configured: false }); } }, "ENTER NEW TOKENS"));
      fill(body,
        h("p", { class: `slack-status ${cls}`, text: status }),
        s.error && cls !== "live" ? h("p", { class: "form-error", text: s.error }) : null,
        h("p", { text: `DM ${bot} in Slack, or @mention it in a channel (invite it there first: /invite ${bot}). A sub-agent does the job and answers in the thread. Type “status” for the farm.` }),
        s.dm_url ? h("a", { class: "btn primary login-link", href: s.dm_url, target: "_blank", rel: "noopener noreferrer" }, "OPEN IN SLACK ↗") : null,
        s.last ? h("p", { class: "muted small", text: `Last message: ${s.last.from}, ${ago(s.last.at)}: “${s.last.text}”` }) : null,
        allowForm,
        s.from_env ? h("p", { class: "muted small", text: "The tokens come from the environment (FARM_SLACK_BOT_TOKEN, FARM_SLACK_APP_TOKEN): change them there." }) : null,
        h("div", { class: "dlg-actions" }, acts));
      return;
    }
    const tok = (name, prefix) => h("input", { name, type: "password", autocomplete: "off", spellcheck: "false", required: true, placeholder: prefix + "…",
      oninput: (e) => e.target.closest("li").classList.toggle("done", e.target.value.trim().startsWith(prefix)) });
    const form = h("form", { class: "slack-form" },
      h("ol", { class: "hatch-steps" },
        h("li", {}, "Create the farm's Slack app. On the Slack page: Create an App → From a manifest → Continue. Everything is filled in: Next → Create (pick your workspace if it asks).",
          h("a", { class: "btn primary login-link", href: s.manifest_url, target: "_blank", rel: "noopener noreferrer",
            onclick: (e) => e.target.closest("li").classList.add("done") }, "CREATE THE SLACK APP ↗")),
        h("li", {}, "On the app's page: Install to Workspace → Allow. Then open OAuth & Permissions and copy the Bot User OAuth Token.",
          h("label", {}, "BOT TOKEN", tok("bot_token", "xoxb-"))),
        h("li", {}, "Open Basic Information → App-Level Tokens → Generate Token and Scopes. Any name, add the scope connections:write, Generate, and copy it.",
          h("label", {}, "APP-LEVEL TOKEN", tok("app_token", "xapp-"))),
        h("li", {}, h("label", {}, "WHO CAN GIVE IT WORK ", h("span", { class: "muted", text: "(optional)" }), allowInput(s.allow)),
          h("p", { class: "muted small", text: "Emails or Slack member IDs. Empty: every full member of the workspace (never guests)." }))),
      h("p", { class: "muted small", text: "The farm connects out to Slack (Socket Mode): no public URL, no open port. The tokens stay on the farm and are never shown again." }),
      h("p", { class: "form-error", role: "alert" }),
      h("div", { class: "dlg-actions" }, h("button", { class: "btn primary", type: "submit" }, "▶ CONNECT")));
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = new FormData(form), btn = form.querySelector("button[type=submit]"), err = form.querySelector(".form-error");
      btn.disabled = true; btn.textContent = "CHECKING WITH SLACK…"; err.textContent = "";
      try {
        const r = await api("api/slack", { bot_token: f.get("bot_token"), app_token: f.get("app_token"), allow: splitAllow(f.get("allow")).join(",") });
        body.dataset.key = ""; this.renderSlack(r); this.loadSlack(); this.refresh();
      } catch (x) { err.textContent = x.message; btn.disabled = false; btn.textContent = "▶ CONNECT"; }
    });
    fill(body, h("p", { text: "Give the farm work from Slack: DM it or @mention it, and a sub-agent does the job and answers in the thread. About two minutes, once." }), form);
  },

  // --------------------------------------------------------------- connectors
  /* CONNECTORS: what the farm is plugged into. A wooden menu opens over its dock button (a sheet on a phone), one row
   * per service with its status; a row opens that service's panel. The manager connects them; people with a Claude
   * see how things stand. */
  /** The dock button: shown to the manager (in MANAGE) and to people with a Claude (in THE FARM), with a count. */
  renderConnTool(st, R) {
    const b = $("#conn-tool"), person = R.manager || !!R.owner;
    b.hidden = !person;
    if (!person) return;
    const home = R.manager ? $("#grp-run") : $("#grp-farm");
    if (b.parentElement !== home) { if (R.manager) home.insertBefore(b, $("#manager-tool")); else home.append(b); }
    const slack = st.slack?.state, bad = slack === "error";
    const n = (slack === "live" ? 1 : 0) + (st.connectors?.stripe ? 1 : 0) + (st.connectors?.blender ? 1 : 0) + (st.connectors?.google_ads ? 1 : 0);
    const badge = $("#conn-badge");
    badge.hidden = !n && !bad;
    badge.textContent = n ? String(n) : "!";
    badge.classList.toggle("bad", !n && bad);
    b.dataset.desc = R.manager ? "Plug the farm into Slack, Stripe, Blender and Google Ads" : "What the farm is plugged into";
    b.dataset.help = (R.manager ? "Plug the farm in: Slack gives it work, Stripe, Blender and Google Ads go to every Claude." : "What the farm is plugged into: Slack, Stripe, Blender, Google Ads.")
      + " The green number: how many are on.";
    b.dataset.tip = n ? `Connectors · ${n} on` : bad ? "Connectors · Slack needs a look" : "Connectors";
    b.setAttribute("aria-label", `Connectors: Slack, Stripe, Blender and Google Ads, ${n ? n + " connected" : bad ? "Slack needs a look" : "none connected"} (S)`);
    if ($("#dlg-conn-menu").open) this.renderConnMenu();
  },
  async loadConnectors() {
    try { this.conn = await api("api/connectors"); } catch { /* keep what we had: the menu still shows /api/state's view */ }
    return this.conn;
  },
  /** Slack, Stripe and Google Ads as the menu shows them: [pill class, pill text]. /api/state is fresh every poll; api/connectors
   * adds the details (Stripe's mode and account), used while it agrees with the state. */
  connView() {
    const st = App.state || {}, c = this.conn || {}, sl = st.slack || c.slack || {};
    const on = !!st.connectors?.stripe, sp = c.stripe && !!c.stripe.connected === on ? c.stripe : { connected: on };
    const slack = sl.state === "live" ? ["on", `CONNECTED · ${String(sl.team || "SLACK").toUpperCase()}`]
      : sl.state === "error" ? ["bad", "NEEDS A LOOK"] : ["connecting", "retrying"].includes(sl.state) ? ["wait", "CONNECTING…"] : ["off", "NOT CONNECTED"];
    const who = sp.account?.name || (sp.kind === "restricted" ? "restricted key" : "");
    const stripe = !sp.connected ? ["off", "NOT CONNECTED"] : sp.mode === "live" ? ["live", `LIVE MODE${who ? " · " + who.toUpperCase() : ""}`]
      : sp.mode ? ["on", `CONNECTED · TEST MODE${who ? " · " + who.toUpperCase() : ""}`] : ["on", "CONNECTED"];
    const bon = !!st.connectors?.blender, bl = c.blender && !!c.blender.connected === bon ? c.blender : { connected: bon };
    const bname = bl.server?.title || bl.server?.name || "";
    const blender = !bl.connected ? ["off", "NOT CONNECTED"] : ["on", `CONNECTED${bname ? " · " + bname.toUpperCase() : ""}`];
    const gon = !!st.connectors?.google_ads, ga = c.google_ads && !!c.google_ads.connected === gon ? c.google_ads : { connected: gon };
    const nAds = (ga.customers || []).filter(x => !x.manager).length;
    const gads = !ga.connected ? ["off", "NOT CONNECTED"] : ["on", ga.customers ? `CONNECTED · ${nAds} AD ACCOUNT${nAds === 1 ? "" : "S"}` : "CONNECTED"];
    return { slack: { pill: slack, ...sl }, stripe: { pill: stripe, ...sp }, blender: { pill: blender, ...bl }, gads: { pill: gads, ...ga } };
  },
  openConnMenu() {
    const d = $("#dlg-conn-menu");
    if (d.open) return d.close();
    for (const x of $$("dialog[open]")) x.close();
    this.connBack = true; // Esc or a click outside hands focus back to the dock button
    $("#connm-list").dataset.sig = "";
    this.renderConnMenu();
    d.showModal();
    this.placeConnMenu();
    $("#conn-tool").setAttribute("aria-expanded", "true");
    d.querySelector(".conn-row")?.focus({ preventScroll: true });
    this.loadConnectors().then(() => { if (d.open) this.renderConnMenu(); });
  },
  /** Over the dock button, its nub pointing at it; a phone's CSS makes it a bottom sheet instead. */
  placeConnMenu() {
    const d = $("#dlg-conn-menu"), b = $("#conn-tool");
    if (innerWidth <= 760 || b.hidden) { for (const p of ["left", "bottom", "--nub"]) d.style.removeProperty(p); return; }
    const r = b.getBoundingClientRect(), tray = b.closest(".dock-group")?.getBoundingClientRect() || r, w = d.offsetWidth, mid = r.left + r.width / 2;
    const left = clamp(mid - w / 2, 8, innerWidth - w - 8);
    d.style.left = Math.round(left) + "px";
    d.style.bottom = Math.round(innerHeight - tray.top + 34) + "px"; // clear of the tray's name tab
    d.style.setProperty("--nub", Math.round(clamp(mid - left, 22, w - 22)) + "px");
  },
  renderConnMenu() {
    const R = role(), v = this.connView(), list = $("#connm-list");
    const sig = JSON.stringify([R.manager, v.slack.pill, v.stripe.pill, v.blender.pill, v.gads.pill]);
    if (list.dataset.sig === sig) return;
    list.dataset.sig = sig;
    const at = [...list.children].indexOf(document.activeElement);
    const row = (id, logo, name, what, [cls, text], go) => h("button", { class: "conn-row", type: "button", role: "menuitem", "data-conn": id,
      "aria-label": `${name}: ${text.toLowerCase()}. ${what}`, onclick: () => { this.connBack = false; $("#dlg-conn-menu").close(); go(); } },
      h("span", { class: "conn-logo" }, h("img", { src: logo, alt: "" })),
      h("span", { class: "conn-txt" }, h("b", { class: "conn-name", text: name }), h("span", { class: "conn-what", text: what }),
        h("span", { class: `pill pill-${cls}` }, h("i", { "aria-hidden": "true" }), h("span", { text }))),
      h("span", { class: "conn-go", "aria-hidden": "true", text: "▶" }));
    fill(list,
      row("slack", "slack.svg", "SLACK", "Give the farm work from Slack", v.slack.pill, () => R.manager ? this.openSlack() : this.openConnector("slack")),
      row("stripe", "stripe.svg", "STRIPE", "Every Claude can use your Stripe account", v.stripe.pill, () => this.openConnector("stripe")),
      row("blender", "blender.svg", "BLENDER", "Every Claude can drive a Blender on another machine", v.blender.pill, () => this.openConnector("blender")),
      row("gads", "google-ads.svg", "GOOGLE ADS", "Reports and live dashboards of your ad accounts", v.gads.pill, () => this.openConnector("gads")));
    if (at >= 0) list.children[at]?.focus({ preventScroll: true });
    $("#connm-foot").textContent = R.manager ? "MORE CONNECTORS COMING" : "THE FARM'S MANAGER CONNECTS THESE";
  },
  /** Arrows move through the menu's rows (Enter opens one, Esc closes it). */
  connMenuKeys(e) {
    const rows = $$("#connm-list .conn-row"), i = rows.indexOf(document.activeElement);
    const to = { ArrowDown: i + 1, ArrowRight: i + 1, ArrowUp: i - 1, ArrowLeft: i - 1, Home: 0, End: rows.length - 1 }[e.key];
    if (to == null || !rows.length) return;
    e.preventDefault();
    rows[(to + rows.length) % rows.length].focus();
  },

  /** A connector's panel: Stripe (the manager connects it; people see how it stands), or Slack for a person. */
  async openConnector(id) {
    for (const x of $$("dialog[open]")) x.close();
    this.connPanel = id; this.stripeReplace = false; this.gadsReplace = false; this.blenderReplace = false;
    $("#connector-logo").src = { slack: "slack.svg", gads: "google-ads.svg", blender: "blender.svg" }[id] || "stripe.svg";
    $("#connector-h").textContent = { slack: "SLACK", gads: "GOOGLE ADS", blender: "BLENDER" }[id] || "STRIPE";
    const body = $("#connector-body");
    body.dataset.key = "";
    if (this.conn) this.renderConnector(); else fill(body, h("p", { class: "muted", text: "Loading…" }));
    const d = $("#dlg-connector");
    d.showModal();
    requestAnimationFrame(() => { d.scrollTop = 0; });
    await this.loadConnectors();
    if (!this.conn) return fill(body, h("p", { class: "form-error", text: "Couldn't load the connectors. Try again in a moment." }));
    if (d.open) this.renderConnector();
  },
  renderConnector() {
    if (this.connPanel === "slack") return this.renderSlackInfo();
    if (this.connPanel === "gads") return this.renderGads();
    if (this.connPanel === "blender") return this.renderBlender();
    return this.renderStripe();
  },
  pill([cls, text]) { return h("span", { class: `pill pill-${cls}` }, h("i", { "aria-hidden": "true" }), h("span", { text })); },
  /** Slack, as a person sees it (the manager gets the real Slack panel). */
  renderSlackInfo() {
    const v = this.connView().slack, live = v.state === "live";
    fill($("#connector-body"),
      h("p", { class: "conn-status" }, this.pill(v.pill)),
      h("p", { text: live ? `The farm is on Slack${v.team ? ` (${v.team})` : ""}: DM its app, or @mention it in a channel, and a sub-agent does the job and answers in the thread. Type “status” for the farm.`
        : "The farm isn't on Slack yet. Once it is, people DM it or @mention it in a channel, and a sub-agent does the job and answers in the thread." }),
      live ? null : h("p", { class: "muted small", text: "Ask the farm's manager to connect it: it's the CONNECTORS button on their farm." }));
  },
  /** What the Claudes get: Stripe's MCP tools. */
  stripeCan() {
    return h("div", { class: "tool-chips conn-can" }, ["Customers", "Products & prices", "Payment links", "Invoices", "Subscriptions", "Balances", "Refunds", "Stripe docs"]
      .map(t => h("span", { class: "tool-chip", text: t })));
  },
  renderStripe() {
    const R = role(), c = this.conn || {}, s = c.stripe || { connected: false }, manage = !!c.manage, body = $("#connector-body");
    const key = JSON.stringify([s, manage, this.stripeReplace]);
    if (body.dataset.key === key) return;
    body.dataset.key = key;
    if (!s.connected) {
      const what = h("p", { class: "conn-lede" }, "Every Claude on the farm gets Stripe's tools: it can look up customers and balances, make products, prices and payment links, send invoices and run subscriptions. ",
        h("b", { text: "They move real money only when their person asks." }));
      if (!manage) return fill(body, what, this.stripeCan(),
        h("p", { class: "banner-note" }, h("strong", { text: "NOT CONNECTED. " }), "Ask the farm's manager to connect Stripe: then your Claude can use it too, and you can still turn it off for your Claude in its SETTINGS."));
      return fill(body, what, this.stripeCan(), this.stripeKeyForm(true, null));
    }
    const a = s.account || {}, live = s.mode === "live", restricted = s.kind === "restricted";
    const title = a.name || (restricted ? "Restricted key" : a.id || "Your Stripe account");
    const by = s.by ? String(s.by).replace(/^owner:/, "") : "";
    const card = h("div", { class: "conn-card" + (live ? " live" : "") },
      h("div", { class: "conn-card-main" },
        h("p", { class: "conn-card-top" }, this.pill(["on", "CONNECTED"]), this.modeChip(s.mode)),
        h("h3", { class: "conn-card-name", text: title.toUpperCase() }),
        h("p", { class: "muted small", text: a.name ? [a.id, a.country].filter(Boolean).join(" · ") : restricted ? "A restricted key may not read the account's name. That's fine: it works." : [a.id, a.country].filter(Boolean).join(" · ") })),
      h("dl", { class: "stat-row conn-dl" },
        manage && s.last4 ? [h("dt", { text: "KEY" }), h("dd", { text: `${restricted ? "Restricted" : "Secret"} key ending …${s.last4}` })] : null,
        manage && s.at ? [h("dt", { text: "CONNECTED" }), h("dd", { text: `${nowS() - s.at < 20 ? "just now" : ago(s.at)}${by ? ` by ${by === "manager" ? "the manager" : by}` : ""}` })] : null,
        h("dt", { text: "TOOLS" }), h("dd", {}, h("code", { class: "conn-code", text: s.tools || "mcp__stripe__*" }), " on every Claude")));
    const warn = live ? h("p", { class: "banner-note live-note" }, h("strong", { text: "LIVE MODE: REAL MONEY. " }),
      "The Claudes create charges, refunds or payouts, cancel or delete only when their person asks for it.") : null;
    const mineId = R.owner && App.state?.agents.find(x => x.id === R.owner && !x.remote) ? R.owner : null;
    const optOut = h("p", { class: "muted small" }, "Anyone can turn Stripe off for their own Claude: its ", h("b", { text: "SETTINGS → RULES" }), ", untick ", h("b", { text: "Stripe (payments)" }), ". ",
      mineId ? h("button", { class: "linkish", type: "button", onclick: () => this.openSettings(mineId, "RULES") }, "Open my Claude's rules →") : null);
    const ask = [h("h3", { class: "kicker", text: "ASK YOUR CLAUDE" }),
      h("ul", { class: "examples" },
        h("li", { text: "“How much did we take in this week?”" }),
        h("li", { text: "“Make a $20 a month plan and a payment link for it.”" }),
        h("li", { text: "“Which invoices are overdue?”" }))];
    if (!manage) return fill(body, card, warn, ask, optOut);
    if (this.stripeReplace) return fill(body, card, h("h3", { class: "kicker", text: "REPLACE THE KEY" }),
      this.stripeKeyForm(false, () => { this.stripeReplace = false; this.renderStripe(); }));
    const off = h("button", { class: "btn danger", type: "button" }, "DISCONNECT"), err = h("p", { class: "form-error", role: "alert" });
    off.addEventListener("click", async () => {
      if (off.dataset.sure !== "1") { off.dataset.sure = "1"; off.textContent = "SURE? THE CLAUDES LOSE STRIPE"; return; }
      off.disabled = true; off.textContent = "DISCONNECTING…"; err.textContent = "";
      try { this.conn = await api("api/connectors/stripe/disconnect", {}); this.say("Stripe is disconnected: the Claudes' Stripe tools are gone."); this.renderStripe(); this.refresh(); }
      catch (x) { err.textContent = x.message; off.disabled = false; off.dataset.sure = ""; off.textContent = "DISCONNECT"; }
    });
    const replace = h("button", { class: "btn", type: "button", onclick: () => { this.stripeReplace = true; this.renderStripe(); $("#connector-body input[name=key]")?.focus(); } }, "REPLACE KEY");
    fill(body, card, warn, ask, optOut, err, h("div", { class: "dlg-actions" }, replace, off));
  },
  modeChip(mode) {
    return mode ? h("span", { class: `mode-chip ${mode === "live" ? "live" : "test"}`, text: mode === "live" ? "LIVE" : "TEST" }) : null;
  },
  /** The key: pasted, hidden unless shown, checked for its shape as you type (TEST or LIVE from its prefix), then
   * checked with Stripe by the farm. `steps`: the first connect, with where to find the key. */
  stripeKeyForm(steps, onCancel) {
    const KEY = /^(sk|rk)_(test|live)_[A-Za-z0-9]{10,250}$/, PREFIXES = ["sk_test_", "sk_live_", "rk_test_", "rk_live_"];
    const input = h("input", { name: "key", type: "password", autocomplete: "off", spellcheck: "false", autocapitalize: "off", maxlength: 300,
      placeholder: "sk_test_…  or  rk_live_…", "aria-describedby": "stripe-hint", "data-1p-ignore": "true", "data-lpignore": "true", id: "stripe-key" });
    const eye = h("button", { class: "btn tiny key-eye", type: "button", "aria-pressed": "false", "aria-label": "Show the key", text: "SHOW" });
    eye.addEventListener("click", () => {
      const show = input.type === "password";
      input.type = show ? "text" : "password"; eye.textContent = show ? "HIDE" : "SHOW";
      eye.setAttribute("aria-pressed", String(show)); eye.setAttribute("aria-label", show ? "Hide the key" : "Show the key"); input.focus();
    });
    const chip = h("span", { class: "mode-chip", hidden: true });
    const hint = h("p", { class: "key-hint", id: "stripe-hint", "aria-live": "polite" });
    const warn = h("p", { class: "banner-note live-note", hidden: true }, h("strong", { text: "LIVE KEY: REAL MONEY. " }),
      "The Claudes would act on your real Stripe account: real customers, charges, refunds and payouts. They move money only when their person asks; a restricted key with only what they need is safer.");
    const err = h("p", { class: "form-error", role: "alert" });
    const go = h("button", { class: "btn primary", type: "submit", disabled: true }, "▶ CONNECT");
    const field = h("div", { class: "key-field" },
      h("label", { for: "stripe-key" }, "SECRET OR RESTRICTED KEY ", chip),
      h("div", { class: "key-row" }, input, eye), hint, err);
    let li = [];
    const check = () => {
      const v = input.value.trim(), m = v.match(/^(sk|rk)_(test|live)_/), ok = KEY.test(v);
      let text = "Starts with sk_ (secret) or rk_ (restricted), then test_ or live_.", cls = "";
      if (m) {
        const kind = m[1] === "rk" ? "restricted" : "secret", live = m[2] === "live";
        chip.hidden = false; chip.className = `mode-chip ${live ? "live" : "test"}`; chip.textContent = `${live ? "LIVE" : "TEST"} · ${kind.toUpperCase()}`;
        if (ok) { text = live ? `Looks right: a LIVE ${kind} key. Real money: read the warning below.` : `Looks right: a TEST-mode ${kind} key. Pretend money, safe to try.`; cls = live ? "warn" : "ok"; }
        else if (/[^A-Za-z0-9]/.test(v.slice(m[0].length))) { text = "Only letters and digits come after the prefix: check for a space or a cut-off key."; cls = "bad"; }
        else text = "Keep pasting: the whole key is longer.";
      } else {
        chip.hidden = true;
        if (/^pk_/.test(v)) { text = "That's the publishable key (pk_…). The Claudes need the secret key (sk_…) or a restricted key (rk_…)."; cls = "bad"; }
        else if (/^whsec_/.test(v)) { text = "That's a webhook signing secret. Copy the secret key (sk_…) or a restricted key (rk_…)."; cls = "bad"; }
        else if (v && !PREFIXES.some(p => p.startsWith(v))) { text = "That isn't a Stripe secret key: it starts with sk_test_, sk_live_, rk_test_ or rk_live_."; cls = "bad"; }
      }
      hint.textContent = text; hint.className = "key-hint" + (cls ? " " + cls : "");
      warn.hidden = !(ok && m[2] === "live");
      go.disabled = !ok || form.dataset.busy === "1";
      go.textContent = ok && m[2] === "live" ? "▶ CONNECT LIVE" : "▶ CONNECT";
      if (li.length) { li[1].classList.toggle("done", ok); li[2].classList.toggle("done", ok); if (ok) li[0].classList.add("done"); }
    };
    input.addEventListener("input", () => { err.textContent = ""; check(); });
    const form = h("form", { class: "stripe-form", autocomplete: "off" });
    if (steps) {
      li = [
        h("li", {}, "Open Stripe → ", h("b", { text: "Developers → API keys" }), ".",
          h("a", { class: "btn login-link key-link", href: "https://dashboard.stripe.com/test/apikeys", target: "_blank", rel: "noopener noreferrer",
            onclick: () => li[0].classList.add("done") }, "OPEN STRIPE API KEYS ↗")),
        h("li", {}, "Create a ", h("b", { text: "restricted key" }), " with only what the Claudes need ", h("span", { class: "rec", text: "RECOMMENDED" }), ", or copy the ", h("b", { text: "secret key" }), ".",
          h("span", { class: "muted small step-note", text: "Test keys (…_test_) use pretend money: a good first try. Switch Stripe to live for a live key." })),
        h("li", {}, "Paste it here.", field)];
      form.append(h("ol", { class: "hatch-steps" }, li));
    } else form.append(field);
    form.append(warn,
      h("p", { class: "muted small", text: "The farm checks the key with Stripe, then keeps it on its box. It's never shown again, not even to you." }),
      h("div", { class: "dlg-actions save-bar" }, onCancel ? h("button", { class: "btn", type: "button", onclick: onCancel }, "CANCEL") : null, go));
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!KEY.test(input.value.trim()) || form.dataset.busy === "1") return;
      form.dataset.busy = "1"; go.disabled = true; input.readOnly = true; err.textContent = "";
      go.replaceChildren(h("span", { class: "spin", "aria-hidden": "true" }), "CHECKING WITH STRIPE…");
      try {
        const r = await api("api/connectors/stripe", { key: input.value.trim() });
        input.value = ""; this.conn = r; this.stripeReplace = false;
        const s = r.stripe || {};
        this.say(`Stripe is connected${s.mode === "live" ? " in LIVE MODE" : " (test mode)"}${s.account?.name ? ": " + s.account.name : ""}. Every Claude can use it now.`);
        this.renderStripe(); this.refresh();
        $("#dlg-connector").scrollTop = 0;
      } catch (x) {
        form.dataset.busy = ""; input.readOnly = false; check(); hint.textContent = ""; hint.className = "key-hint"; err.textContent = x.message;
        input.focus({ preventScroll: true }); input.select(); field.scrollIntoView({ block: "center" });
      }
    });
    check();
    setTimeout(() => { if (!steps) input.focus({ preventScroll: true }); }, 30);
    return form;
  },

  /** Blender: the manager connects a Blender MCP server that runs elsewhere (its URL, and its token if it wants one);
   * every Claude gets its tools as mcp__blender__*. People see that it's there and what it is. */
  renderBlender() {
    const R = role(), c = this.conn || {}, b = c.blender || { connected: false }, manage = !!c.manage, body = $("#connector-body");
    const key = JSON.stringify([b, manage, this.blenderReplace]);
    if (body.dataset.key === key) return;
    body.dataset.key = key;
    const lede = h("p", { class: "conn-lede" }, "Every Claude on the farm can drive a Blender that runs on another machine, through its MCP server: ",
      "build scenes and game assets, set materials, animate, render, import and export, and run Python in it. ",
      h("b", { text: "It runs there, not on the farm's box." }));
    const can = h("div", { class: "tool-chips conn-can" }, ["Scenes", "Game assets", "Materials", "Animation", "Renders", "Import & export", "Python"]
      .map(t => h("span", { class: "tool-chip", text: t })));
    if (!b.connected) {
      if (!manage) return fill(body, lede, can,
        h("p", { class: "banner-note" }, h("strong", { text: "NOT CONNECTED. " }), "Ask the farm's manager to connect a Blender server: then your Claude can use it too, and you can still turn it off for your Claude in its SETTINGS."));
      return fill(body, lede, can, this.blenderForm(null));
    }
    const srv = b.server || {}, by = b.by ? String(b.by).replace(/^owner:/, "") : "";
    const card = h("div", { class: "conn-card" },
      h("div", { class: "conn-card-main" },
        h("p", { class: "conn-card-top" }, this.pill(["on", "CONNECTED"])),
        h("h3", { class: "conn-card-name", text: (srv.title || srv.name || "Blender MCP server").toUpperCase() }),
        srv.version ? h("p", { class: "muted small", text: `version ${srv.version}` }) : null),
      h("dl", { class: "stat-row conn-dl" },
        manage && b.url ? [h("dt", { text: "SERVER" }), h("dd", {}, h("code", { class: "conn-code", text: b.url }))] : null,
        manage ? [h("dt", { text: "TOKEN" }), h("dd", { text: b.last4 ? `ending …${b.last4}` : "none" })] : null,
        manage && b.at ? [h("dt", { text: "CONNECTED" }), h("dd", { text: `${nowS() - b.at < 20 ? "just now" : ago(b.at)}${by ? ` by ${by === "manager" ? "the manager" : by}` : ""}` })] : null,
        h("dt", { text: "TOOLS" }), h("dd", {}, h("code", { class: "conn-code", text: b.tools || "mcp__blender__*" }), " on every Claude")));
    const mineId = R.owner && App.state?.agents.find(x => x.id === R.owner && !x.remote) ? R.owner : null;
    const optOut = h("p", { class: "muted small" }, "Anyone can turn Blender off for their own Claude: its ", h("b", { text: "SETTINGS → RULES" }), ", untick ", h("b", { text: "Blender (3D)" }), ". ",
      mineId ? h("button", { class: "linkish", type: "button", onclick: () => this.openSettings(mineId, "RULES") }, "Open my Claude's rules →") : null);
    const ask = [h("h3", { class: "kicker", text: "ASK YOUR CLAUDE" }),
      h("ul", { class: "examples" },
        h("li", { text: "“Model a low-poly treasure chest with an opening lid and export it as GLB.”" }),
        h("li", { text: "“Render the scene from the main camera and show me.”" }),
        h("li", { text: "“Rig this character and give it a walk cycle.”" }))];
    if (!manage) return fill(body, card, ask, optOut);
    if (this.blenderReplace) return fill(body, card, h("h3", { class: "kicker", text: "CHANGE THE SERVER" }),
      this.blenderForm(() => { this.blenderReplace = false; this.renderBlender(); }, b.url));
    const off = h("button", { class: "btn danger", type: "button" }, "DISCONNECT"), err = h("p", { class: "form-error", role: "alert" });
    off.addEventListener("click", async () => {
      if (off.dataset.sure !== "1") { off.dataset.sure = "1"; off.textContent = "SURE? THE CLAUDES LOSE BLENDER"; return; }
      off.disabled = true; off.textContent = "DISCONNECTING…"; err.textContent = "";
      try { this.conn = await api("api/connectors/blender/disconnect", {}); this.say("Blender is disconnected: the Claudes' Blender tools are gone."); this.renderBlender(); this.refresh(); }
      catch (x) { err.textContent = x.message; off.disabled = false; off.dataset.sure = ""; off.textContent = "DISCONNECT"; }
    });
    const change = h("button", { class: "btn", type: "button", onclick: () => { this.blenderReplace = true; this.renderBlender(); $("#connector-body input[name=url]")?.focus(); } }, "CHANGE SERVER");
    fill(body, card, ask, optOut, err, h("div", { class: "dlg-actions" }, change, off));
  },
  /** The server's MCP URL and its token (optional), checked by the farm opening an MCP session with it. */
  blenderForm(onCancel, url) {
    const input = h("input", { name: "url", type: "url", autocomplete: "off", spellcheck: "false", autocapitalize: "off", maxlength: 1000,
      placeholder: "https://blender.example.com/mcp", id: "blender-url", value: url || "" });
    const tok = h("input", { name: "token", type: "password", autocomplete: "off", spellcheck: "false", autocapitalize: "off", maxlength: 2000,
      placeholder: "optional", id: "blender-token", "data-1p-ignore": "true", "data-lpignore": "true" });
    const hint = h("p", { class: "key-hint", id: "blender-hint", "aria-live": "polite" });
    const err = h("p", { class: "form-error", role: "alert" });
    const go = h("button", { class: "btn primary", type: "submit", disabled: true }, "▶ CONNECT");
    const form = h("form", { class: "stripe-form", autocomplete: "off" });
    const ok = () => /^https?:\/\/[^\s/]+/.test(input.value.trim());
    const check = () => {
      const v = input.value.trim();
      hint.textContent = !v ? "Its streamable-HTTP MCP endpoint, often ending in /mcp." : !ok() ? "Starts with https:// (or http:// on a private network)."
        : /^http:\/\//.test(v) && !/^http:\/\/(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/.test(v) ? "Plain http over the internet sends the token in the clear: use https." : "Looks right.";
      hint.className = "key-hint" + (v && !ok() ? " bad" : v && /clear/.test(hint.textContent) ? " warn" : v ? " ok" : "");
      go.disabled = !ok() || form.dataset.busy === "1";
    };
    input.addEventListener("input", () => { err.textContent = ""; check(); });
    form.append(
      h("div", { class: "key-field" }, h("label", { for: "blender-url" }, "SERVER URL"), h("div", { class: "key-row" }, input), hint),
      h("div", { class: "key-field" }, h("label", { for: "blender-token" }, "TOKEN"), h("div", { class: "key-row" }, tok), err),
      h("p", { class: "muted small", text: "The farm checks that the server answers, then keeps the URL and token on its box. The token is never shown again. Such a server usually runs Python in Blender: connect only one whose token you're happy for any of the farm's Claudes to use." }),
      h("div", { class: "dlg-actions save-bar" }, onCancel ? h("button", { class: "btn", type: "button", onclick: onCancel }, "CANCEL") : null, go));
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!ok() || form.dataset.busy === "1") return;
      form.dataset.busy = "1"; go.disabled = true; input.readOnly = tok.readOnly = true; err.textContent = "";
      go.replaceChildren(h("span", { class: "spin", "aria-hidden": "true" }), "CHECKING THE SERVER…");
      try {
        const r = await api("api/connectors/blender", { url: input.value.trim(), token: tok.value.trim() });
        tok.value = ""; this.conn = r; this.blenderReplace = false;
        const srv = r.blender?.server || {};
        this.say(`Blender is connected${srv.title || srv.name ? ": " + (srv.title || srv.name) : ""}. Every Claude can use it now.`);
        this.renderBlender(); this.refresh();
        $("#dlg-connector").scrollTop = 0;
      } catch (x) {
        form.dataset.busy = ""; input.readOnly = tok.readOnly = false; go.textContent = "▶ CONNECT"; check(); err.textContent = x.message;
        input.focus({ preventScroll: true });
      }
    });
    check();
    setTimeout(() => input.focus({ preventScroll: true }), 30);
    return form;
  },

  /** Google Ads: the manager connects it with the API's four credentials (and a manager account's ID); every Claude
   * runs reports (`clodfarm gads`) and keeps live dashboards of the ad accounts. */
  renderGads() {
    const c = this.conn || {}, g = c.google_ads || { connected: false }, manage = !!c.manage, body = $("#connector-body");
    const key = JSON.stringify([g, manage, this.gadsReplace]);
    if (body.dataset.key === key) return;
    body.dataset.key = key;
    const lede = h("p", { class: "conn-lede" }, "Every Claude on the farm can read your ad accounts: campaigns, spend, clicks and conversions, as reports and ",
      h("b", { text: "live dashboards" }), " the farm keeps fresh. ", h("b", { text: "They change budgets or campaigns only when their person asks." }));
    const can = h("div", { class: "tool-chips conn-can" }, ["Accounts", "Campaign reports", "Live dashboards", "Budgets & bids", "Keywords", "GAQL"]
      .map(t => h("span", { class: "tool-chip", text: t })));
    if (!g.connected) {
      if (!manage) return fill(body, lede, can,
        h("p", { class: "banner-note" }, h("strong", { text: "NOT CONNECTED. " }), "Ask the farm's manager to connect Google Ads: then your Claude can use it too."));
      return fill(body, lede, can, this.gadsForm(true, null));
    }
    const by = g.by ? String(g.by).replace(/^owner:/, "") : "";
    const accts = g.customers || [];
    const card = h("div", { class: "conn-card" },
      h("div", { class: "conn-card-main" },
        h("p", { class: "conn-card-top" }, this.pill(["on", "CONNECTED"]), g.api_version ? h("span", { class: "mode-chip test", text: `API ${g.api_version}` }) : null),
        h("h3", { class: "conn-card-name", text: `${accts.length} ACCOUNT${accts.length === 1 ? "" : "S"}${g.more ? ` (+${g.more})` : ""}` }),
        h("ul", { class: "gads-accts" }, accts.map(a => h("li", {},
          h("code", { class: "conn-code", text: String(a.id).replace(/^(\d{3})(\d{3})(\d{4})$/, "$1-$2-$3") }), " ", a.name || "(no name)",
          a.manager ? h("span", { class: "mode-chip", text: "MANAGER" }) : null)))),
      h("dl", { class: "stat-row conn-dl" },
        manage && g.developer_token_last4 ? [h("dt", { text: "TOKEN" }), h("dd", { text: `Developer token ending …${g.developer_token_last4}` })] : null,
        manage && g.login_customer_id ? [h("dt", { text: "THROUGH" }), h("dd", { text: `Manager account ${String(g.login_customer_id).replace(/^(\d{3})(\d{3})(\d{4})$/, "$1-$2-$3")}` })] : null,
        manage && g.at ? [h("dt", { text: "CONNECTED" }), h("dd", { text: `${nowS() - g.at < 20 ? "just now" : ago(g.at)}${by ? ` by ${by === "manager" ? "the manager" : by}` : ""}` })] : null,
        h("dt", { text: "TOOLS" }), h("dd", {}, h("code", { class: "conn-code", text: "clodfarm gads" }), " on every Claude")));
    const first = accts.find(a => !a.manager) || accts[0];
    const ask = [h("h3", { class: "kicker", text: "ASK YOUR CLAUDE" }),
      h("ul", { class: "examples" },
        h("li", { text: "“Make a live dashboard of our Google Ads spend and conversions.”" }),
        h("li", { text: "“Which campaigns had the worst cost per conversion last week?”" }),
        h("li", { text: "“Pause the keywords that spent over $50 with no conversions.”" })),
      first ? h("p", { class: "muted small" }, "Or a ready dashboard, refreshed hourly: ",
        h("code", { class: "conn-code", text: `clodfarm dashboard push ads --run "clodfarm gads dashboard --customer ${first.id}" --every 1h` })) : null];
    if (!manage) return fill(body, card, ask);
    if (this.gadsReplace) return fill(body, card, h("h3", { class: "kicker", text: "REPLACE THE CREDENTIALS" }),
      this.gadsForm(false, () => { this.gadsReplace = false; this.renderGads(); }));
    const off = h("button", { class: "btn danger", type: "button" }, "DISCONNECT"), err = h("p", { class: "form-error", role: "alert" });
    off.addEventListener("click", async () => {
      if (off.dataset.sure !== "1") { off.dataset.sure = "1"; off.textContent = "SURE? THE CLAUDES LOSE GOOGLE ADS"; return; }
      off.disabled = true; off.textContent = "DISCONNECTING…"; err.textContent = "";
      try { this.conn = await api("api/connectors/google-ads/disconnect", {}); this.say("Google Ads is disconnected."); this.renderGads(); this.refresh(); }
      catch (x) { err.textContent = x.message; off.disabled = false; off.dataset.sure = ""; off.textContent = "DISCONNECT"; }
    });
    const replace = h("button", { class: "btn", type: "button", onclick: () => { this.gadsReplace = true; this.renderGads(); $("#connector-body input")?.focus(); } }, "REPLACE");
    fill(body, card, ask, err, h("div", { class: "dlg-actions" }, replace, off));
  },
  /** The Google Ads API's credentials: three, plus a manager account's ID when the ad accounts sit under one, and an
   *  old API Center developer token if the farm has one (Google checks the Cloud project's access level now). */
  gadsForm(steps, onCancel) {
    const F = [
      ["client_id", "OAUTH CLIENT ID", false, "…apps.googleusercontent.com"],
      ["client_secret", "OAUTH CLIENT SECRET", true, "GOCSPX-…"],
      ["refresh_token", "REFRESH TOKEN", true, "1//…"],
      ["login_customer_id", "MANAGER ACCOUNT ID (OPTIONAL)", false, "123-456-7890"],
      ["developer_token", "DEVELOPER TOKEN (OPTIONAL)", true, "Only if you have one from API Center"]];
    const err = h("p", { class: "form-error", role: "alert" });
    const go = h("button", { class: "btn primary", type: "submit", disabled: true }, "▶ CONNECT");
    const inputs = {};
    const fields = F.map(([name, label, secret, ph]) => {
      const input = inputs[name] = h("input", { name, id: `gads-${name}`, type: secret ? "password" : "text", autocomplete: "off", spellcheck: "false",
        autocapitalize: "off", maxlength: 600, placeholder: ph, "data-1p-ignore": "true", "data-lpignore": "true" });
      const row = [input];
      if (secret) {
        const eye = h("button", { class: "btn tiny key-eye", type: "button", "aria-pressed": "false", "aria-label": `Show the ${label.toLowerCase()}`, text: "SHOW" });
        eye.addEventListener("click", () => {
          const show = input.type === "password";
          input.type = show ? "text" : "password"; eye.textContent = show ? "HIDE" : "SHOW"; eye.setAttribute("aria-pressed", String(show)); input.focus();
        });
        row.push(eye);
      }
      return h("div", { class: "key-field" }, h("label", { for: `gads-${name}`, text: label }), h("div", { class: "key-row" }, row));
    });
    const hint = h("p", { class: "key-hint", "aria-live": "polite" });
    const form = h("form", { class: "stripe-form gads-form", autocomplete: "off" });
    const check = () => {
      const v = k => inputs[k].value.trim(), lc = v("login_customer_id").replace(/\D/g, "");
      const missing = F.slice(0, 3).filter(([k]) => !v(k)).map(([, l]) => l.toLowerCase());
      let text = missing.length ? `Still needed: ${missing.join(", ")}.` : "Looks complete: the farm checks it with Google.", cls = missing.length ? "" : "ok";
      if (v("client_id") && !/\.apps\.googleusercontent\.com$/.test(v("client_id"))) { text = "An OAuth client ID ends in .apps.googleusercontent.com."; cls = "bad"; }
      if (v("login_customer_id") && lc.length !== 10) { text = "A manager account ID is 10 digits (123-456-7890)."; cls = "bad"; }
      hint.textContent = text; hint.className = "key-hint" + (cls ? " " + cls : "");
      go.disabled = !!missing.length || cls === "bad" || form.dataset.busy === "1";
    };
    for (const i of Object.values(inputs)) i.addEventListener("input", () => { err.textContent = ""; check(); });
    if (steps) form.append(h("ol", { class: "hatch-steps" },
      h("li", {}, "In Google Cloud, enable the ", h("b", { text: "Google Ads API" }), " and make an ", h("b", { text: "OAuth client ID" }), " (Desktop app).",
        h("a", { class: "btn login-link key-link", href: "https://console.cloud.google.com/apis/credentials", target: "_blank", rel: "noopener noreferrer" }, "OPEN CREDENTIALS ↗")),
      h("li", {}, "On the project's ", h("b", { text: "Google Ads API" }), " page: Access levels → ", h("b", { text: "Manage" }), ", apply for ", h("b", { text: "Explorer" }), ".",
        h("span", { class: "muted small step-note", text: "A new project has Test access (test accounts only). Explorer, usually granted in minutes, reaches real ones." }),
        h("a", { class: "btn login-link key-link", href: "https://console.cloud.google.com/google/ads-apis/overview", target: "_blank", rel: "noopener noreferrer" }, "OPEN ACCESS LEVELS ↗")),
      h("li", {}, "Get a ", h("b", { text: "refresh token" }), " for a Google user who can see the ad accounts (scope ", h("code", { class: "conn-code", text: "adwords" }), ").",
        h("span", { class: "muted small step-note", text: "Google's generate_user_credentials.py, or the OAuth Playground with your own client." })),
      h("li", {}, "Paste them here.", ...fields, hint, err)));
    else form.append(...fields, hint, err);
    form.append(
      h("p", { class: "muted small", text: "The farm checks them with Google, then keeps them on its box. The secrets are never shown again." }),
      h("div", { class: "dlg-actions save-bar" }, onCancel ? h("button", { class: "btn", type: "button", onclick: onCancel }, "CANCEL") : null, go));
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (go.disabled) return;
      form.dataset.busy = "1"; go.disabled = true; err.textContent = "";
      for (const i of Object.values(inputs)) i.readOnly = true;
      go.replaceChildren(h("span", { class: "spin", "aria-hidden": "true" }), "CHECKING WITH GOOGLE…");
      try {
        const r = await api("api/connectors/google-ads", Object.fromEntries(Object.entries(inputs).map(([k, i]) => [k, i.value.trim()])));
        for (const i of Object.values(inputs)) i.value = "";
        this.conn = r; this.gadsReplace = false;
        const n = (r.google_ads?.customers || []).length;
        this.say(`Google Ads is connected: ${n} account${n === 1 ? "" : "s"}. Every Claude can use it now.`);
        this.renderGads(); this.refresh();
        $("#dlg-connector").scrollTop = 0;
      } catch (x) {
        form.dataset.busy = ""; for (const i of Object.values(inputs)) i.readOnly = false;
        go.textContent = "▶ CONNECT"; check(); hint.textContent = ""; hint.className = "key-hint"; err.textContent = x.message;
        err.scrollIntoView({ block: "center" });
      }
    });
    check();
    return form;
  },

  // ----------------------------------------------------------------- hatching
  /** + NEW CLAUDE: name (or a bot's provider), then its look, then its rules; then the Claude login (or the bot check). */
  openHatch(agentId, fromButton) {
    const R = role(), st = App.state, hatch = App.me?.hatch;
    if (!agentId && fromButton && !R.manager && hatch && !hatch.can) { this.say(`You can't hatch a Claude here: ${hatch.why}.`); return; }
    if (!agentId && fromButton && R.owner && !R.manager) { this.say("You have a Claude on this farm already: tap the gold arrow."); return; }
    for (const d of $$("dialog[open]")) d.close();
    const primary = st?.agents.find(a => a.primary);
    if (!agentId && R.manager && primary && !primary.loggedIn) agentId = primary.id; // the farm's own login comes first
    this.hatchFor = agentId || null;
    $("#hatch-body").dataset.key = "";
    const own = agentId && st?.agents.find(a => a.id === agentId);
    // the farm's own Claude, still an egg: make it yours first (name, look, rules), like any hatch; then its login
    this.ownFirst = !!(own?.primary && !own.loggedIn && !own.remote && R.manager);
    $("#hatch-h").textContent = own?.primary ? "YOUR CLAUDE" : own ? `LOG IN ${String(own.name || own.id).toUpperCase()}` : "NEW CLAUDE";
    $("#dlg-hatch").showModal();
    if (agentId && !this.ownFirst) { this.renderHatch({ state: "starting" }); this.beginLogin(agentId); }
    else {
      this.draft = { kind: "claude", name: this.ownFirst ? (own.name || own.id) : "", bot: null, approve: !this.ownFirst,
        allTools: true, deny: new Set(),
        skin: { hat: own?.hat || "straw", colors: { hat: SWATCHES[hashStr(String(Date.now())) % 8], band: HATS[own?.hat || "straw"]?.band || HATS.straw.band, body: CLAY.b }, accessory: "" } };
      this.renderHatchName();
    }
  },
  /** + INVITE A CLAUDE: a one-time link for one person, who logs in with their own Claude account and joins the farm.
   * Hatching one here stays one tap away (a bot, or another Claude account of your own). */
  openInvite() {
    for (const d of $$("dialog[open]")) d.close();
    this.ownFirst = false;
    $("#hatch-h").textContent = "INVITE A CLAUDE";
    const out = h("div", { class: "invite-out" }), err = h("p", { class: "form-error", role: "alert" });
    const make = h("button", { class: "btn primary invite-go", type: "button" }, "▶ MAKE AN INVITE LINK");
    make.addEventListener("click", async () => {
      err.textContent = ""; make.disabled = true;
      try {
        const r = await api("api/manager/invite", {});
        const link = h("input", { class: "invite-link", readonly: true, value: r.link, "aria-label": "Invite link", onclick: (e) => e.target.select() });
        const copy = h("button", { class: "btn", type: "button", text: "COPY", onclick: async (e) => {
          try { await navigator.clipboard.writeText(r.link); e.target.textContent = "COPIED ✓"; } catch { link.select(); e.target.textContent = "SELECT IT"; }
        } });
        fill(out, h("div", { class: "dlg-actions left invite-row" }, link, copy),
          h("p", { class: "muted small", text: r.room ? "Send it to one person. It works once, for 7 days: they log in with their Claude account and their own Claude joins the farm."
            : "This farm has no room for another Claude right now, so the link will say so until one leaves." }));
        make.hidden = true; link.select();
      } catch (x) { err.textContent = x.message; make.disabled = false; }
    });
    const here = h("button", { class: "btn tiny", type: "button", text: "OR HATCH ONE HERE (A BOT, OR ANOTHER ACCOUNT OF YOURS)" });
    here.addEventListener("click", () => this.openHatch(null, true));
    $("#hatch-body").dataset.key = "";
    fill($("#hatch-body"),
      h("p", {}, "A link for one person: they log in with their own Claude account and their own Claude joins this farm, working on their plan. They see their Claude; you see everyone."),
      make, out, err, h("div", { class: "dlg-actions left" }, here));
    $("#dlg-hatch").showModal();
  },
  /** Where you are in hatching: three pips, the done ones ticked. */
  hatchStep(n, title) {
    const names = [this.draft?.kind === "bot" ? "THE BOT" : "NAME", "LOOK", "RULES"];
    return h("ol", { class: "stepper", "aria-label": `Step ${n} of 3: ${title.toLowerCase()}` }, names.map((nm, i) => h("li", {
      class: i + 1 < n ? "done" : i + 1 === n ? "on" : "", "aria-current": i + 1 === n ? "step" : null },
      h("span", { class: "pip", "aria-hidden": "true", text: i + 1 < n ? "✓" : String(i + 1) }), h("span", { class: "pip-name", text: nm }))));
  },
  renderHatchName(kind) {
    const D = this.draft;
    if (kind) D.kind = kind;
    kind = D.kind;
    const pick = this.ownFirst ? null : h("div", { class: "hatch-kind", role: "group", "aria-label": "What to add" },
      h("button", { class: "btn" + (kind === "claude" ? " primary" : ""), type: "button", "aria-pressed": String(kind === "claude"), onclick: () => this.renderHatchName("claude") }, "CLAUDE ACCOUNT"),
      h("button", { class: "btn" + (kind === "bot" ? " primary" : ""), type: "button", "aria-pressed": String(kind === "bot"), onclick: () => this.renderHatchName("bot") }, "BOT: OTHER MODEL"));
    if (kind === "bot") return this.renderBotForm(pick);
    const form = h("form", {}, this.hatchStep(1, "NAME"), pick,
      h("canvas", { class: "egg-anim", id: "egg-cv", "aria-hidden": "true" }),
      h("label", {}, "NAME ", h("span", { class: "muted", text: "(optional)" }), h("input", { name: "name", maxlength: 24, placeholder: "e.g. gil or night-shift", autocomplete: "off", value: D.name })),
      h("p", { class: "muted", text: this.ownFirst ? "Your farm's own Claude: give it a name and a look, choose its rules, then log it in with your Claude account."
        : "A new Claude Code login with its own agents. Log in with another Claude account to add capacity: each account is paced on its own budget. The same account again just shares its budget." }),
      h("p", { class: "form-error", role: "alert" }),
      h("div", { class: "dlg-actions" }, h("button", { class: "btn primary", type: "submit" }, "NEXT: ITS LOOK ▶")));
    form.addEventListener("submit", (e) => { e.preventDefault(); D.name = new FormData(form).get("name"); this.renderHatchLook(); });
    fill($("#hatch-body"), form);
    paintSprite($("#egg-cv"), EGG_HD, 96, { bg: tileBg, pad: 10, bottom: true });
    form.querySelector("input[name=name]").focus();
  },
  renderHatchLook() {
    const D = this.draft;
    const picker = this.skinPicker(D.skin, (s) => { D.skin = s; });
    fill($("#hatch-body"), this.hatchStep(2, "ITS LOOK"), picker,
      h("div", { class: "dlg-actions save-bar" },
        h("button", { class: "btn", type: "button", onclick: () => this.renderHatchName() }, "◀ BACK"),
        h("button", { class: "btn primary", type: "button", onclick: () => this.renderHatchRules() }, "NEXT: ITS RULES ▶")));
  },
  /** Who may start work on it, and which tools it may use: the same checklist SETTINGS has. */
  rulesFields(state, groups) {
    const approve = h("input", { type: "checkbox", checked: state.approve, onchange: (e) => { state.approve = e.target.checked; } });
    const list = h("ul", { class: "checklist", hidden: state.allTools });
    const all = h("input", { type: "checkbox", checked: state.allTools, onchange: (e) => { state.allTools = e.target.checked; list.hidden = state.allTools; } });
    fill(list, (groups || []).map(gp => h("li", {}, h("label", { class: "check" },
      h("input", { type: "checkbox", checked: !state.deny.has(gp.id), onchange: (e) => { if (e.target.checked) state.deny.delete(gp.id); else state.deny.add(gp.id); } }),
      this.groupLabel(gp)))));
    if (!groups) this.toolGroups().then(gs => fill(list, gs.map(gp => h("li", {}, h("label", { class: "check" },
      h("input", { type: "checkbox", checked: !state.deny.has(gp.id), onchange: (e) => { if (e.target.checked) state.deny.delete(gp.id); else state.deny.add(gp.id); } }),
      this.groupLabel(gp)))))).catch(() => fill(list, h("li", { class: "muted small", text: "Couldn't load the tool groups." })));
    return [
      this.switchRow(approve, "APPROVE EVERY MISSION", "Other Claudes and the planner can't start work on it without your OK, on your phone. Its own work (what you ask it in the Claude app) always runs."),
      this.switchRow(all, "ALL TOOLS", "Off: pick what it may use. Unticked tools are blocked at its next tool call."),
      list];
  },
  /** A tool group's name in the checklist; Stripe and Blender get their logos, and say when the farm isn't connected to them. */
  groupLabel(gp) {
    if (gp.id !== "stripe" && gp.id !== "blender") return h("span", { text: gp.label });
    const on = !!App.state?.connectors?.[gp.id];
    return h("span", { class: "grp-lbl" }, h("img", { src: `${gp.id}.svg`, alt: "" }),
      h("span", {}, gp.label, h("small", { class: on ? "grp-on" : "muted", text: on ? (gp.id === "stripe" ? "the farm's account" : "the farm's server") : "not connected yet" })));
  },
  /** A checkbox drawn as a switch, with what it does under its name. */
  switchRow(input, name, why) {
    input.setAttribute("role", "switch");
    return h("label", { class: "switch" }, input, h("span", { class: "switch-ui", "aria-hidden": "true" }), h("span", { class: "switch-txt" }, h("b", { text: name }), why ? h("span", { class: "why", text: why }) : null));
  },
  async toolGroups() {
    if (!this.groups) this.groups = await api("api/tools");
    return this.groups;
  },
  renderHatchRules() {
    const D = this.draft, err = h("p", { class: "form-error", role: "alert" });
    const go = h("button", { class: "btn primary", type: "button" }, D.kind === "bot" ? "▶ CHECK & ADD BOT" : this.ownFirst ? "▶ LOG IT IN" : "▶ HATCH IT");
    go.addEventListener("click", () => this.submitHatch(go, err));
    fill($("#hatch-body"), this.hatchStep(3, "ITS RULES"), this.rulesFields(D), err,
      h("div", { class: "dlg-actions" },
        h("button", { class: "btn", type: "button", onclick: () => this.renderHatchLook() }, "◀ BACK"), go));
  },
  async submitHatch(btn, err) {
    const D = this.draft;
    const body = { name: D.name || "", approve_missions: D.approve, tools: D.allTools ? "all" : { deny: [...D.deny] }, skin: D.skin };
    if (D.kind === "bot") body.bot = D.bot;
    btn.disabled = true; btn.textContent = D.kind === "bot" ? "ASKING THE MODEL…" : "HATCHING…"; err.textContent = "";
    if (this.ownFirst) { // the farm's own Claude: keep what was chosen, then its login
      try {
        await api(`api/agents/${encodeURIComponent(this.hatchFor)}/settings`, { ...(body.name.trim() ? { name: body.name.trim() } : {}),
          skin: body.skin, approve_missions: body.approve_missions, tools: body.tools });
        this.ownFirst = false; $("#hatch-body").dataset.key = ""; this.renderHatch({ state: "starting" }); this.beginLogin(this.hatchFor); this.refresh();
      } catch (x) { err.textContent = x.message; btn.disabled = false; btn.textContent = "▶ LOG IT IN"; }
      return;
    }
    try {
      const a = await api("api/agents", body);
      this.hatchFor = a.id; this.loadMe();
      if (D.kind === "bot") {
        const cv = h("canvas", { class: "egg-anim", "aria-hidden": "true" });
        paintSprite(cv, skinFrameHD(skinOf(D.skin.hat, D.skin.colors, D.skin.accessory), { arms: 1, happy: true }), 96, { bg: tileBg, pad: 8, bottom: true });
        fill($("#hatch-body"), cv, h("p", { class: "center", text: `${a.name.toUpperCase()} joined! ${D.bot.model} answered “${a.said}”. It's on the farm in a few seconds: send it work with --on ${a.id}.` }),
          h("div", { class: "dlg-actions" }, h("button", { class: "btn primary", type: "button", onclick: () => $("#dlg-hatch").close() }, "▶ YAY")));
        this.say(`${a.name.toUpperCase()} joined the farm!`);
      } else { $("#hatch-body").dataset.key = ""; this.renderHatch({ state: "starting" }); this.pollHatch(); }
      this.refresh();
    } catch (x) {
      err.textContent = x.status === 409 ? "You have a Claude on this farm already." : x.status === 429 ? x.message : x.message;
      btn.disabled = false; btn.textContent = D.kind === "bot" ? "▶ CHECK & ADD BOT" : "▶ HATCH IT";
    }
  },
  renderBotForm(pick) { // a bot: Claude Code on another model, through a provider that speaks Anthropic's API
    const P = BOT_PROVIDERS, D = this.draft, B = D.bot || {};
    const url = h("input", { name: "url", autocomplete: "off", spellcheck: "false", required: true }),
      model = h("input", { name: "model", autocomplete: "off", spellcheck: "false", required: true, maxlength: 128, value: B.model || "" }),
      key = h("input", { name: "key", type: "password", autocomplete: "off", spellcheck: "false", maxlength: 500, value: B.key || "" }),
      keyNote = h("span", { class: "muted" }), hint = h("p", { class: "muted small" });
    const provider = h("select", { name: "provider" }, Object.entries(P).map(([k, p]) => h("option", { value: k, text: p.label, selected: B.provider === k })));
    const sync = (keepUrl) => {
      const p = P[provider.value];
      url.value = keepUrl && B.url ? B.url : p.url; url.placeholder = p.url || "https://your-gateway.example.com";
      model.placeholder = p.example ? `e.g. ${p.example}` : "the model, as the provider names it";
      key.required = p.key; keyNote.textContent = p.key ? "" : " (optional)"; hint.textContent = p.hint;
    };
    provider.addEventListener("change", () => sync(false));
    const form = h("form", {}, this.hatchStep(1, "THE BOT"), pick,
      h("p", { class: "muted", text: "A bot is Claude Code on another model: a free one on OpenRouter, or your own through Ollama. It uses no Claude account's usage, but it's weaker than Claude, so it takes only the sub-agents sent to it." }),
      h("label", {}, "NAME ", h("span", { class: "muted", text: "(optional)" }), h("input", { name: "name", maxlength: 24, placeholder: "e.g. qwen or night-bot", autocomplete: "off", value: D.name })),
      h("label", {}, "PROVIDER", provider),
      h("label", {}, "ADDRESS", url),
      h("label", {}, "MODEL", model),
      h("label", {}, "API KEY", keyNote, key),
      hint,
      h("label", { class: "check" }, h("input", { name: "any", type: "checkbox", checked: B.takes === "any" }), "ALSO TAKE ANY SUB-AGENT ", h("span", { class: "muted", text: "(not only the ones sent to it)" })),
      h("p", { class: "form-error", role: "alert" }),
      h("div", { class: "dlg-actions" }, h("button", { class: "btn primary", type: "submit" }, "NEXT: ITS LOOK ▶")));
    sync(true);
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const f = new FormData(form);
      D.name = f.get("name");
      D.bot = { provider: f.get("provider"), url: f.get("url"), model: f.get("model"), key: f.get("key"), takes: f.get("any") ? "any" : "sent" };
      if (!B.provider) D.skin = { ...D.skin, hat: D.skin.hat === "straw" ? "headphones" : D.skin.hat };
      this.renderHatchLook();
    });
    fill($("#hatch-body"), form);
    model.focus();
  },
  async beginLogin(id) {
    try { const s = await api(`api/agents/${id}/login`, {}); this.renderHatch(s); this.pollHatch(); }
    catch (x) { this.renderHatch({ state: "failed", error: x.message }); }
  },
  pollHatch() {
    this.stopHatchPoll();
    this.hatchPoll = setInterval(async () => {
      if (!this.hatchFor) return;
      try { const s = await api(`api/agents/${this.hatchFor}/login`); this.renderHatch(s); if (s.state === "done" || s.state === "failed") this.stopHatchPoll(); }
      catch { /* keep polling */ }
    }, 1000);
  },
  stopHatchPoll() { clearInterval(this.hatchPoll); this.hatchPoll = null; },
  renderHatch(s) {
    const body = $("#hatch-body"), key = s.state + "|" + (s.url || "") + "|" + (s.error || "");
    if (body.dataset.key === key) return;
    body.dataset.key = key;
    const agent = App.state?.agents.find(a => a.id === this.hatchFor);
    const name = (agent?.name || this.hatchFor || "claude").toUpperCase();
    const egg = h("canvas", { class: "egg-anim", "aria-hidden": "true" });
    paintSprite(egg, EGG_HD, 96, { bg: tileBg, pad: 10, bottom: true });
    if (s.state === "done") {
      const cv = h("canvas", { class: "egg-anim", "aria-hidden": "true" });
      const skin = agent ? agentSkin(agent) : this.draft ? skinOf(this.draft.skin.hat, this.draft.skin.colors, this.draft.skin.accessory) : skinOf("straw", null, "", colorFor(this.hatchFor || ""));
      paintSprite(cv, skinFrameHD(skin, { arms: 1, happy: true }), 96, { bg: tileBg, pad: 8, bottom: true });
      fill(body, cv, h("p", { class: "center", text: `${name} hatched! It's logged in and joins the farm in a few seconds.` }),
        h("div", { class: "dlg-actions" }, h("button", { class: "btn primary", type: "button", onclick: () => { $("#dlg-hatch").close(); if (this.invited) location.replace(location.pathname); } }, "▶ YAY")));
      this.say(`${name} hatched!`); if (!this.invited) this.refresh();
      return;
    }
    if (s.state === "failed") {
      fill(body, egg, h("p", { class: "form-error", text: s.error || "The login didn't finish." }),
        h("div", { class: "dlg-actions" }, h("button", { class: "btn primary", type: "button", onclick: () => { body.dataset.key = ""; this.renderHatch({ state: "starting" }); this.beginLogin(this.hatchFor); } }, "↻ TRY AGAIN")));
      return;
    }
    const codeForm = h("form", {},
      h("label", {}, "LOGIN CODE", h("input", { name: "code", autocomplete: "off", spellcheck: "false", required: true, placeholder: "paste the code from the Claude page", disabled: s.state !== "waiting_code" })),
      h("p", { class: "form-error", role: "alert" }),
      h("div", { class: "dlg-actions" }, h("button", { class: "btn primary", type: "submit", disabled: s.state !== "waiting_code" }, s.state === "checking" ? "HATCHING…" : "▶ DONE")));
    codeForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      try { const r = await api(`api/agents/${this.hatchFor}/code`, { code: new FormData(codeForm).get("code") }); this.renderHatch(r); this.pollHatch(); }
      catch (x) { codeForm.querySelector(".form-error").textContent = x.message; }
    });
    const link = s.url ? h("a", { class: "btn primary login-link", href: s.url, target: "_blank", rel: "noopener noreferrer" }, "OPEN THE CLAUDE LOGIN ↗")
      : h("p", { class: "muted", text: "Warming the egg… (starting Claude Code's login)" });
    fill(body, egg,
      h("p", { class: "center", text: `Log ${name} in to a Claude account.` }),
      h("ol", { class: "hatch-steps" },
        h("li", { class: s.url ? "done" : "" }, "Open the Claude login page and approve.", link),
        h("li", {}, "Copy the code it shows you and paste it here.", codeForm),
        h("li", {}, "The egg hatches: talk to the new Claude from its Claude app, or let yours hand it work.")));
    if (s.state === "waiting_code") setTimeout(() => codeForm.querySelector("input")?.focus(), 30);
  },

  // -------------------------------------------------------------- skin picker
  /** A Claude's look: a live preview on a patch of meadow, hats as tiles, one palette for the hat, band and body colours
   * (pick what to paint, then the colour), accessories as tiles, and SURPRISE ME. Every group is a radio group: arrow keys
   * move and pick. `onChange` gets {hat, colors: {hat, band, body}, accessory} (what POST /api/agents and SETTINGS take
   * as `skin`). */
  skinPicker(start, onChange) {
    const s = { hat: start.hat || "straw", colors: { ...start.colors }, accessory: start.accessory || "" };
    let target = "hat", hopUntil = 0, sparkUntil = 0;
    const skinNow = () => skinOf(s.hat, s.colors, s.accessory);
    // the preview: it walks, looks round, blinks; it hops when you change something
    const stage = h("canvas", { class: "skin-stage", role: "img" }), comp = canvas(48, 44), cg = comp.getContext("2d");
    const caption = h("p", { class: "skin-caption", "aria-live": "polite" });
    const t0 = performance.now();
    const drawStage = () => {
      const now = performance.now(), t = (now - t0) / 1000, cyc = t % 7, hop = now < hopUntil, walk = [1, 0, 2, 0][Math.floor(t * 9) % 4];
      const pose = hop ? { arms: Math.floor(t * 7) % 2 ? 1 : 0, happy: true } : REDUCED ? {}
        : cyc < 1.2 ? { legs: walk, look: 1 } : cyc < 2.4 ? { look: 1 } : cyc < 2.55 ? { blink: true } : cyc < 3.6 ? {} : cyc < 4.6 ? { look: -1 } : cyc < 5.6 ? { legs: walk, look: -1 } : cyc < 5.75 ? { blink: true } : {};
      const lift = hop ? Math.round(Math.abs(Math.sin(t * 10)) * 4) : !REDUCED && Math.sin(t * 2.2) > 0.75 ? 1 : 0;
      cg.clearRect(0, 0, 48, 44);
      cg.globalAlpha = 0.3; cg.drawImage(shadowSprite(8, 2), 23 - 8 - 1 + 1, 44 - 6); cg.globalAlpha = 1;
      cg.drawImage(skinFrameHD(skinNow(), pose), 8, 44 - 36 - 3 - lift);
      if (now < sparkUntil) for (let i = 0; i < 4; i++) { const a = t * 3 + i * 1.57; cg.drawImage(SPARKLE[i], Math.round(21 + Math.cos(a) * 18), Math.round(18 + Math.sin(a) * 13)); }
      paintSprite(stage, comp, stage.clientWidth || 176, { bg: tileBg, bottom: true, fixed: true });
    };
    clearInterval(this.previewT);
    this.previewT = setInterval(() => { if (!stage.isConnected) return clearInterval(this.previewT); drawStage(); }, REDUCED ? 400 : 90);
    requestAnimationFrame(drawStage);
    // the tiles: little canvases painted from the skin they'd give
    const art = (cls) => h("canvas", { class: "tile-art " + cls, "aria-hidden": "true" }), phone = innerWidth < 760, hatCss = phone ? 48 : 64, accCss = phone ? 56 : 72;
    const headCrop = (sk) => { const c = canvas(32, 27); c.getContext("2d").drawImage(skinFrameHD(sk, {}), 0, -1); return c; };
    const hats = this.radioGrid("Hat", Object.keys(HATS), () => s.hat, (v) => { if (s.hat !== v && !start.keepBand) s.colors.band = HATS[v].band; s.hat = v; changed(); }, {
      build: (v) => [art("hat-art"), h("span", { class: "tile-lbl", text: HAT_NAMES[v] })], label: (v) => `${HAT_NAMES[v].toLowerCase()} hat`,
      paint: (b, v) => { const sk = skinOf(v, s.colors, ""); if (b._k !== sk.key) { b._k = sk.key; paintSprite(b.firstChild, headCrop(sk), hatCss); } } }, "hat");
    const accs = this.radioGrid("Extra", ACCESSORIES, () => s.accessory, (v) => { s.accessory = v; changed(); }, {
      build: (v) => [art("acc-art"), h("span", { class: "tile-lbl", text: ACC_NAMES[v] })], label: (v) => ACC_NAMES[v].toLowerCase(),
      paint: (b, v) => { const sk = skinOf(s.hat, s.colors, v); if (b._k !== sk.key) { b._k = sk.key; paintSprite(b.firstChild, skinFrameHD(sk, {}), accCss); } } }, "acc");
    // colours: what to paint, then one palette
    const TARGETS = [["hat", "HAT"], ["band", "BAND"], ["body", "BODY"]];
    const chipOf = {};
    const targets = this.radioGrid("Paint", TARGETS.map(t => t[0]), () => target, (v) => { if (v === "hat" && s.hat === "none") return; target = v; drawPalette(); targets.refresh(); }, {
      build: (v) => [(chipOf[v] = h("i", { class: "chip-swatch", "aria-hidden": "true" })), h("span", { text: TARGETS.find(t => t[0] === v)[1] })], label: (v) => `paint the ${v}`,
      paint: (b, v) => { chipOf[v].style.background = s.colors[v]; b.disabled = v === "hat" && s.hat === "none"; } }, "target");
    const paletteBox = h("div", { class: "palette-box" });
    let palette = null;
    const drawPalette = () => {
      const list = target === "body" ? BODY_TINTS : SWATCHES, names = target === "body" ? BODY_NAMES : SWATCH_NAMES, cur = s.colors[target];
      const values = list.includes(cur) || !HEX.test(cur || "") ? list : [cur, ...list];
      palette = this.radioGrid(`${target} colour`, values, () => s.colors[target], (v) => { s.colors[target] = v; changed(); }, {
        build: (v) => { const i = h("i", { "aria-hidden": "true" }); i.style.background = v; return [i]; },
        label: (v) => `${target} ${names[list.indexOf(v)] || "custom colour"}` }, "swatch");
      fill(paletteBox, palette.el);
      palette.refresh();
    };
    const surprise = h("button", { type: "button", class: "btn surprise" }, h("img", { src: icon("dice"), alt: "" }), "SURPRISE ME");
    surprise.addEventListener("click", () => {
      const r = Math.random, pick = (a) => a[Math.floor(r() * a.length)];
      s.hat = r() < 0.05 ? "none" : pick(Object.keys(HATS).filter(x => x !== "none"));
      const hc = pick(SWATCHES);
      s.colors = { hat: hc, band: pick(SWATCHES.filter(c => c !== hc)), body: r() < 0.5 ? CLAY.b : pick(BODY_TINTS) };
      s.accessory = r() < 0.4 ? "" : pick(ACCESSORIES.slice(1));
      if (s.hat === "none" && target === "hat") target = "band";
      sparkUntil = performance.now() + 900;
      changed(); drawPalette();
    });
    const describe = () => {
      const nm = (list, names, c) => names[list.indexOf(String(c).toLowerCase())] || "custom";
      const hn = nm(SWATCHES, SWATCH_NAMES, s.colors.hat);
      const hatTxt = s.hat === "none" ? "No hat" : `${HAT_NAMES[s.hat][0]}${HAT_NAMES[s.hat].slice(1).toLowerCase()} in ${hn === "custom" ? "a custom colour" : hn}`;
      return `${hatTxt}, ${nm(SWATCHES, SWATCH_NAMES, s.colors.band)} trim, ${nm(BODY_TINTS, BODY_NAMES, s.colors.body)} body${s.accessory ? ", " + ACC_NAMES[s.accessory].toLowerCase() : ""}.`;
    };
    const changed = () => {
      if (s.hat === "none" && target === "hat") { target = "band"; drawPalette(); }
      onChange({ hat: s.hat, colors: { ...s.colors }, accessory: s.accessory });
      hats.refresh(); accs.refresh(); targets.refresh(); palette?.refresh();
      caption.textContent = describe();
      stage.setAttribute("aria-label", "Preview: " + describe());
      hopUntil = performance.now() + 650;
    };
    drawPalette();
    changed(); hopUntil = 0;
    return h("div", { class: "skin" },
      h("div", { class: "skin-left" }, stage, caption, surprise),
      h("div", { class: "skin-right" },
        h("p", { class: "skin-h", text: "HAT" }), hats.el,
        h("p", { class: "skin-h", text: "COLOURS" }), targets.el, paletteBox,
        h("p", { class: "skin-h", text: "EXTRA" }), accs.el));
  },
  /** Tiles that act as radio buttons: click or arrow keys pick; only the picked one is in the tab order. */
  radioGrid(label, values, get, set, { build, paint, label: aria }, kind) {
    const btns = values.map(v => {
      const b = h("button", { type: "button", role: "radio", class: `tile ${kind}-tile`, "aria-label": aria ? aria(v) : String(v) }, build(v));
      b.addEventListener("click", () => set(v));
      return b;
    });
    const el = h("div", { class: `tiles ${kind}-grid`, role: "radiogroup", "aria-label": label }, btns);
    el.addEventListener("keydown", (e) => {
      const i = btns.indexOf(document.activeElement); if (i < 0) return;
      const cols = Math.max(1, getComputedStyle(el).gridTemplateColumns.split(" ").length);
      const d = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: cols, ArrowUp: -cols, Home: -i, End: btns.length - 1 - i }[e.key];
      if (d == null) return;
      e.preventDefault();
      let j = clamp(i + d, 0, btns.length - 1);
      while (btns[j].disabled && j !== i) j = clamp(j + Math.sign(d), 0, btns.length - 1);
      btns[j].focus(); set(values[j]);
    });
    const refresh = () => btns.forEach((b, i) => { paint?.(b, values[i]); const on = values[i] === get(); b.setAttribute("aria-checked", String(on)); b.tabIndex = on ? 0 : -1; b.classList.toggle("on", on); });
    refresh();
    return { el, refresh };
  },

  // --------------------------------------------------------------- approvals
  /** Missions waiting for a person's OK. The count comes with every poll; the list only when it changes (or every 20s). */
  async syncApprovals(st) {
    const R = role(), n = R.manager ? st.me?.pending_all || 0 : st.me?.pending || 0;
    if (!n) { if (App.apprCount !== 0) { App.approvals = []; Scene.pendingFor = {}; App.apprCount = 0; if ($("#dlg-approvals").open) this.renderApprovals(); } return; }
    if (n === App.apprCount && Date.now() - (App.apprAt || 0) < 20000) return;
    App.apprCount = n;
    await this.loadApprovals();
  },
  async loadApprovals() {
    App.apprAt = Date.now();
    try { this.setApprovals(await api("api/approvals")); }
    catch (x) { App.apprError = x.message; if ($("#dlg-approvals").open) this.renderApprovals(); }
  },
  setApprovals(list) {
    App.approvals = Array.isArray(list) ? list : []; App.apprError = null;
    const by = {};
    for (const p of App.approvals) if (p.to) by[p.to] = (by[p.to] || 0) + 1;
    Scene.pendingFor = by;
    if ($("#dlg-approvals").open) this.renderApprovals();
  },
  async openApprovals(focusId, forClaude) {
    for (const d of $$("dialog[open]")) d.close();
    this.apprFocus = focusId || null; this.apprFor = forClaude || null;
    fill($("#appr-body"), h("p", { class: "muted", text: "Loading…" }));
    $("#dlg-approvals").showModal();
    const R = role();
    if (R.manager || R.owner) await this.loadApprovals();
    this.renderApprovals();
  },
  renderApprovals() {
    const R = role(), body = $("#appr-body"), st = App.state;
    if (body.contains(document.activeElement) && document.activeElement.tagName === "INPUT") return; // someone is typing a reason
    const nameOf = (id) => (st?.agents.find(a => a.id === id)?.name || id || "?").toUpperCase();
    if (!R.manager && !R.owner) {
      $("#appr-sub").textContent = "";
      const box = h("div", { class: "login inset" });
      fill(body, h("p", { text: "Missions for your Claude wait here for your OK. Sign in to your Claude first:" }), box);
      this.accountForms(box, { tabs: ["mine"], onDone: async () => { await this.loadMe(); await this.refresh(); this.openApprovals(this.apprFocus); } });
      return;
    }
    let list = App.approvals || [];
    if (this.apprFor) list = list.filter(p => p.to === this.apprFor);
    $("#appr-sub").textContent = R.manager ? "Every Claude's missions that wait for their person (you can decide for any of them)." : "Missions other Claudes (or the planner) want your Claude to do. Nothing starts until you say so.";
    if (App.apprError) { fill(body, h("p", { class: "form-error", text: App.apprError })); return; }
    if (!list.length) {
      fill(body, h("p", { class: "center big-ok", text: "✓ NOTHING WAITING" }), this.apprFor ? h("p", { class: "center" }, h("button", { class: "btn", type: "button", onclick: () => { this.apprFor = null; this.renderApprovals(); } }, "SEE EVERY CLAUDE'S")) : null);
      return;
    }
    const card = (p) => {
      const reason = h("input", { class: "reason", placeholder: "why not? (optional, it's told)", maxlength: 300, hidden: true, "aria-label": "Reason" });
      const err = h("p", { class: "form-error", role: "alert" });
      const yes = h("button", { class: "btn primary big", type: "button" }, "✓ APPROVE"), no = h("button", { class: "btn danger big", type: "button" }, "✕ DENY");
      const el = h("article", { class: "appr" + (p.id === this.apprFocus ? " focus" : ""), "data-id": p.id },
        h("p", { class: "appr-who" }, h("b", { text: String(p.from || "someone").toUpperCase() }), p.type === "message" ? " wants to send " : " asks ", h("b", { text: nameOf(p.to) }),
          p.type === "message" ? " a message" : " to do a mission"),
        h("h3", { text: p.title || "(untitled)" }),
        h("pre", { class: "appr-prompt", text: p.prompt || "(no prompt)" }),
        h("p", { class: "muted small", text: `Asked ${ago(p.at)}` + (p.expires_at ? ` · expires in ${until(p.expires_at)} (then it's denied)` : "") }),
        reason, err, h("div", { class: "appr-acts" }, no, yes));
      const decide = async (ok) => {
        yes.disabled = no.disabled = true; err.textContent = "";
        try {
          const list2 = await api(`api/approvals/${encodeURIComponent(p.id)}/${ok ? "approve" : "deny"}`, ok ? {} : { reason: reason.value.trim() || undefined });
          el.classList.add("decided"); this.say(ok ? `Approved: “${p.title}”. ${nameOf(p.to)} starts on it.` : `Denied: “${p.title}”.`);
          setTimeout(() => { this.setApprovals(list2); this.refresh(); }, 250);
        } catch (x) { err.textContent = x.message; yes.disabled = no.disabled = false; }
      };
      yes.addEventListener("click", () => decide(true));
      no.addEventListener("click", () => {
        if (reason.hidden) { reason.hidden = false; no.textContent = "✕ DENY IT"; reason.focus(); return; }
        decide(false);
      });
      return el;
    };
    fill(body, list.slice(0, 200).map(card), list.length > 200 ? h("p", { class: "muted small", text: `…and ${list.length - 200} more.` }) : null);
    if (this.apprFocus && !this.apprFocusT) { // the ?approve=<id> link: show that one (once the list has settled)
      this.apprFocusT = setTimeout(() => { body.querySelector(".appr.focus")?.scrollIntoView({ block: "center" }); this.apprFocus = null; this.apprFocusT = null; }, 150);
    }
  },

  // ------------------------------------------------------------------ roster
  openRoster() {
    for (const d of $$("dialog[open]")) d.close();
    $("#dlg-roster").showModal();
    this.renderRoster();
    if (matchMedia("(hover: hover)").matches) $("#roster-q").focus();
  },
  /** Every Claude in one searchable list: with 100 of them, this is how you find one. */
  renderRoster() {
    const st = App.state; if (!st) return;
    const q = $("#roster-q").value.trim().toLowerCase(), sort = $("#roster-sort").value, by = st.tokens?.by_claude || {};
    const subsOf = new Map();
    for (const t of st.subagents) { if (!subsOf.has(t.owner)) subsOf.set(t.owner, []); subsOf.get(t.owner).push(t); }
    const rank = (a) => !a.loggedIn && !a.remote ? 5 : a.error ? 1 : subsOf.has(a.id) || a.talking ? 0 : !a.alive ? 4 : a.resting ? 3 : 2;
    const task = (a) => a.talking ? `talking: ${a.talking.title || "a conversation"}` : subsOf.has(a.id) ? subsOf.get(a.id)[0].title + (subsOf.get(a.id).length > 1 ? ` (+${subsOf.get(a.id).length - 1})` : "") : "";
    let rows = st.agents.map(a => ({ a, task: task(a), tokens: by[a.id] || 0, h5: a.budget?.five_hour ?? -1, d7: a.budget?.seven_day ?? -1, rank: rank(a) }));
    if (q) rows = rows.filter(r => (r.a.name + " " + r.a.id + " " + r.task).toLowerCase().includes(q));
    const cmp = { name: (x, y) => x.a.name.localeCompare(y.a.name), tokens: (x, y) => y.tokens - x.tokens, h5: (x, y) => y.h5 - x.h5, d7: (x, y) => y.d7 - x.d7,
      status: (x, y) => (y.a.mine - x.a.mine) || (x.rank - y.rank) || x.a.name.localeCompare(y.a.name) }[sort];
    rows.sort(cmp);
    $("#roster-count").textContent = `${rows.length} of ${st.agents.length} · tap one to find it on the farm`;
    const list = $("#roster-list"), sig = JSON.stringify([q, sort, rows.map(r => [r.a.id, r.task, r.tokens, r.h5, r.d7, r.rank, Scene.pendingFor[r.a.id] || 0])]);
    if (list.dataset.sig === sig) return;
    list.dataset.sig = sig;
    const STATE = ["BUSY", "ERROR", "READY", "RESTING", "NOT RUNNING", "EGG"];
    const bar = (u) => { const i = h("i", { class: u < 0.5 ? "" : u < 0.8 ? "mid" : "low" }); i.style.width = `${Math.round(clamp(u, 0, 1) * 100)}%`; return h("span", { class: "mbar" }, i); };
    fill(list, rows.map(({ a, task: tk, tokens, rank: rk }) => {
      const b = a.budget || {}, waitingN = Scene.pendingFor[a.id] || 0;
      return h("li", {}, h("button", { type: "button", class: "roster-row" + (a.mine ? " mine" : ""), onclick: () => { $("#dlg-roster").close(); this.focusAgent(a.id); } },
        !a.loggedIn && !a.remote ? h("img", { class: "r-sprite", src: icon("egg"), alt: "" }) : h("img", { class: "r-sprite", src: this.spriteURL(agentSkin(a)), alt: "" }),
        h("span", { class: "r-main" },
          h("span", { class: "r-name" }, h("b", { text: a.name.toUpperCase() }),
            a.mine ? h("span", { class: "badge mine", text: "★ MINE" }) : null,
            a.approve_missions ? h("span", { class: "badge waiting", text: "✓ OK'S MISSIONS", title: "Its person approves every mission" }) : null,
            a.bot ? h("span", { class: "badge queued", text: "BOT" }) : null,
            waitingN ? h("span", { class: "badge failed", text: `⚑ ${waitingN}` }) : null),
          h("span", { class: "r-sub" }, h("span", { class: `r-state s${rk}`, text: STATE[rk] }), tk ? h("span", { class: "r-task", text: " · " + tk }) : null)),
        h("span", { class: "r-nums" },
          h("span", { class: "r-tok", text: fmtShort(tokens) + " TOK" }),
          a.bot ? null : h("span", { class: "r-bars" }, h("span", { text: "5H" }), bar(b.five_hour || 0), h("span", { text: pct(b.five_hour) }),
            h("span", { text: "7D" }), bar(b.seven_day || 0), h("span", { text: pct(b.seven_day) })))));
    }));
  },

  // ----------------------------------------------------------------- planner
  openPlanner() {
    if (!App.state?.planner) return;
    for (const d of $$("dialog[open]")) d.close();
    this.renderPlanner();
    $("#dlg-planner").showModal();
  },
  renderPlanner() {
    const P = App.state?.planner; if (!P) return;
    const R = role();
    paintSprite($("#plan-sprite"), SCARECROW_HD[P.on ? 0 : 2], 96, { bg: tileBg, pad: 6, bottom: true });
    $("#plan-sub").textContent = P.on ? "AWAKE: IT PLANS THE FARM'S WORK" : "ASLEEP";
    const next = P.idle_until ? `resting until ${until(P.idle_until)} from now` : P.next_at ? `in ${until(P.next_at)}` : P.on ? "when its sub-agents finish" : "–";
    const task = P.task && (typeof P.task === "object" ? P.task : { id: P.task, title: P.task });
    const host = P.host ? (App.state.agents.find(a => a.id === P.host)?.name || P.host) : "any Claude that lets it";
    const sig = JSON.stringify([P, R.manager]), body = $("#plan-body");
    if (body.dataset.sig === sig) return;
    body.dataset.sig = sig;
    fill(body,
      h("h3", { text: "ITS GOAL" }), h("p", { class: "job-text big", text: P.goal || "(no goal yet: the manager sets one)" }),
      h("dl", { class: "stat-row" },
        h("dt", { text: "STATE" }), h("dd", { text: (P.state || (P.on ? "planning" : "asleep")).toUpperCase() }),
        h("dt", { text: "CYCLES" }), h("dd", { text: String(P.cycles || 0) }),
        h("dt", { text: "LAST CYCLE" }), h("dd", { text: ago(P.last_at) }),
        h("dt", { text: "NEXT CYCLE" }), h("dd", { text: next }),
        h("dt", { text: "EVERY" }), h("dd", { text: everyLabel(P.every_s) }),
        h("dt", { text: "RUNS ON" }), h("dd", { text: host }),
        task ? [h("dt", { text: "ITS TASK" }), h("dd", {}, h("a", { href: "tasks", text: `${task.title || task.id}${task.status ? " · " + String(task.status).toUpperCase() : ""} →` }))] : null),
      h("p", { class: "muted small", text: "Each cycle it looks at the goal and the farm, then hands missions to Claudes that have room. Claudes whose person approves every mission wait for that OK." }));
    const acts = [];
    if (R.manager) {
      const tog = h("button", { class: "btn" + (P.on ? " danger" : " primary"), type: "button" }, P.on ? "PUT IT TO SLEEP" : "▶ WAKE IT UP");
      tog.addEventListener("click", async () => {
        tog.disabled = true;
        try { await api("api/manager/planner", { on: !P.on }); this.say(P.on ? "The planner went to sleep." : "The planner woke up!"); await this.refresh(); }
        catch (x) { tog.textContent = x.message.slice(0, 40); }
      });
      acts.push(h("button", { class: "btn", type: "button", onclick: () => this.openManager() }, "⚙ GOAL & CADENCE"), tog);
    }
    fill($("#plan-actions"), acts);
  },

  // ----------------------------------------------------------------- manager
  async openManager() {
    if (!role().manager) return;
    for (const d of $$("dialog[open]")) d.close();
    fill($("#mgr-body"), h("p", { class: "muted", text: "Loading…" }));
    $("#dlg-manager").showModal();
    try { this.renderManager(await api("api/manager")); }
    catch (x) { fill($("#mgr-body"), h("p", { class: "form-error", text: x.message })); }
  },
  /** One form per section; each posts only its own fields and shows the server's answer (or error) under it. */
  renderManager(m) {
    const S = m.settings || {}, P = m.planner || {};
    const section = (title, fields, save, extra) => {
      const err = h("p", { class: "form-error", role: "alert" }), btn = h("button", { class: "btn primary", type: "submit" }, "SAVE");
      const f = h("form", { class: "mgr-sec" }, h("h3", { text: title }), fields, err, save ? h("div", { class: "dlg-actions" }, extra || null, btn) : null);
      f.addEventListener("submit", async (e) => {
        e.preventDefault(); err.textContent = ""; btn.disabled = true;
        try { await save(f); btn.textContent = "SAVED ✓"; setTimeout(() => { btn.textContent = "SAVE"; btn.disabled = false; }, 1200); this.refresh(); }
        catch (x) { err.textContent = x.message; btn.disabled = false; }
      });
      return f;
    };
    const num = (name, value, min, max) => h("input", { name, type: "number", min, max, value: value ?? "", inputmode: "numeric" });
    // who runs the farm: the persons of these Claudes
    const nameOf = id => (App.state?.agents.find(a => a.id === id)?.name || id).toUpperCase();
    const mgrs = m.managers || [], mgrErr = h("p", { class: "form-error", role: "alert" });
    const change = async (action, claude, btn) => {
      mgrErr.textContent = "";
      if (btn && btn.dataset.sure !== "1") { btn.dataset.sure = "1"; btn.textContent = "SURE?"; return; }
      try { const r = await api("api/manager/managers", { action, claude }); await this.loadMe(); this.renderManager(r); if (!role().manager) { $("#dlg-manager").close(); this.say(`${nameOf(claude)}'s person runs the farm now.`); } }
      catch (x) { mgrErr.textContent = x.message; }
    };
    const others = (m.hosts || []).filter(id => !mgrs.includes(id));
    const pick = h("select", { "aria-label": "a Claude" }, others.map(id => h("option", { value: id, text: nameOf(id) })));
    const managers = h("div", { class: "mgr-sec" }, h("h3", { text: "WHO RUNS THE FARM" }),
      h("p", { class: "muted small", text: "The person of each of these Claudes is a manager (signed in to their Claude, like you). No password." }),
      h("ul", { class: "owners" }, mgrs.map(id => {
        const rm = h("button", { class: "btn tiny danger", type: "button", disabled: mgrs.length < 2 }, "REMOVE");
        rm.addEventListener("click", () => change("remove", id, rm));
        return h("li", {}, h("span", { class: "o-name", text: nameOf(id) }), id === role().owner ? h("span", { class: "badge", text: "YOU" }) : null, rm);
      })),
      others.length ? h("div", { class: "dlg-actions left" }, pick,
        h("button", { class: "btn", type: "button", onclick: () => change("add", pick.value) }, "+ MAKE A MANAGER TOO"),
        (() => { const b = h("button", { class: "btn danger", type: "button" }, "HAND IT OVER"); b.addEventListener("click", () => change("set", pick.value, b)); return b; })()) : null,
      mgrErr);
    // planner
    const everyS = P.every_s || 900, everySel = h("select", { name: "every" }, EVERY.map(([l, s]) => h("option", { value: l, text: `every ${l}`, selected: s === everyS })));
    if (!EVERY.some(e => e[1] === everyS)) everySel.prepend(h("option", { value: "", text: `every ${everyLabel(everyS)} (now)`, selected: true }));
    const hostSel = h("select", { name: "host" }, h("option", { value: "", text: "any Claude that lets it" }),
      (m.hosts || []).map(id => h("option", { value: id, text: App.state?.agents.find(a => a.id === id)?.name || id, selected: P.host === id })));
    const onBox = h("input", { type: "checkbox", name: "on", checked: !!P.on });
    const planner = section("THE PLANNER", [
      h("label", { class: "check toggle" }, onBox, "PLANNER ON"),
      h("p", { class: "muted small", text: `${(P.state || "").toUpperCase() || "IDLE"} · ${P.cycles || 0} cycles · last ${ago(P.last_at)}` }),
      h("label", {}, "GOAL", h("textarea", { name: "goal", maxlength: 4000, rows: 3, placeholder: "What should the farm work towards?", text: P.goal || "" })),
      h("div", { class: "two" }, h("label", {}, "RUNS ON", hostSel), h("label", {}, "CADENCE", everySel))],
      async (f) => { const d = new FormData(f), body = { on: onBox.checked, goal: d.get("goal"), host: d.get("host") }; if (d.get("every")) body.every = d.get("every"); await api("api/manager/planner", body); });
    // privacy
    const privBox = h("input", { type: "checkbox", name: "private", checked: !!S.private });
    const privacy = section("PRIVACY", [
      h("label", { class: "check toggle" }, privBox, "PRIVATE FARM"),
      h("p", { class: "muted small", text: S.private ? "Only the people of its Claudes (and you) see it: they sign in with their Claude." : "Public: anyone with the address watches the tokens and the Claudes at work (no prompts, no results, no logins)." })],
      async () => { await api("api/manager/settings", { private: privBox.checked }); });
    // hatching
    const openBox = h("input", { type: "checkbox", name: "hatch_open", checked: !!S.hatch_open });
    const hatching = section("HATCHING", [
      h("label", { class: "check toggle" }, openBox, "ANYONE WHO SEES THE FARM CAN HATCH ONE CLAUDE"),
      h("div", { class: "two" }, h("label", {}, "MAX CLAUDES", num("max_claudes", S.max_claudes, 1, 1000)), h("label", {}, "PER ADDRESS / HOUR", num("hatch_per_ip_hour", S.hatch_per_ip_hour, 1, 100)))],
      async (f) => { const d = new FormData(f); await api("api/manager/settings", { hatch_open: openBox.checked, max_claudes: Number(d.get("max_claudes")), hatch_per_ip_hour: Number(d.get("hatch_per_ip_hour")) }); });
    // owners
    const owned = (m.claudes || []).filter(c => c.owned), q = h("input", { type: "search", placeholder: `search ${owned.length} Claudes with a person…`, "aria-label": "Search owners" });
    const ul = h("ul", { class: "owners" });
    const drawOwners = () => {
      const s = q.value.trim().toLowerCase(), rows = owned.filter(c => !s || c.id.toLowerCase().includes(s));
      fill(ul, rows.slice(0, 150).map(c => {
        const out = h("button", { class: "btn tiny danger", type: "button" }, "SIGN OUT");
        out.addEventListener("click", async () => {
          if (out.dataset.sure !== "1") { out.dataset.sure = "1"; out.textContent = "SURE?"; return; }
          out.disabled = true;
          try { await api(`api/manager/owners/${encodeURIComponent(c.id)}/signout`, {}); c.owned = false; out.textContent = "SIGNED OUT ✓"; }
          catch (x) { out.textContent = x.message.slice(0, 30); }
        });
        return h("li", {}, h("span", { class: "o-name", text: (App.state?.agents.find(a => a.id === c.id)?.name || c.id).toUpperCase() }),
          c.approve_missions ? h("span", { class: "badge waiting", text: "✓ OK'S MISSIONS" }) : null, out);
      }), rows.length > 150 ? h("li", { class: "muted small", text: `…${rows.length - 150} more: search` }) : null,
      !rows.length ? h("li", { class: "muted small", text: "No Claude has a person signed in." }) : null);
    };
    q.addEventListener("input", drawOwners); drawOwners();
    const owners = section("PEOPLE", [h("p", { class: "muted small", text: "Signing a person out forgets every device they signed in on; their Claude stays. They sign in again with a code from their Claude." }), q, ul], null);
    // release
    const roll = h("button", { class: "btn", type: "button" }, "↻ ROLL UI");
    const rollErr = h("p", { class: "form-error", role: "alert" });
    roll.addEventListener("click", async () => {
      if (roll.dataset.sure !== "1") { roll.dataset.sure = "1"; roll.textContent = "SURE? RESTARTS THE UI"; return; }
      roll.disabled = true; roll.textContent = "ROLLING…";
      try { await api("api/manager/roll-ui", {}); roll.textContent = "ROLLED ✓"; this.say("The UI restarted on the new code. The farm kept working."); }
      catch (x) { rollErr.textContent = x.message; roll.disabled = false; roll.textContent = "↻ ROLL UI"; }
    });
    const rel = typeof m.release === "object" && m.release ? Object.entries(m.release).map(([k, v]) => `${k}: ${v}`).join(" · ") : String(m.release || "–");
    const release = h("div", { class: "mgr-sec" }, h("h3", { text: "RELEASE" }),
      h("p", {}, "Version ", h("b", { text: m.version || "?" }), h("span", { class: "muted", text: ` · ${rel}` })),
      h("p", { class: "muted small", text: "ROLL UI restarts the web UI on the code that's installed now (the Claudes keep working, the page stays up)." }),
      rollErr, h("div", { class: "dlg-actions" }, roll));
    // invites: a link for one person, who logs in with their own Claude account
    const invOut = h("div", { class: "invite-out" }), invErr = h("p", { class: "form-error", role: "alert" });
    const invBtn = h("button", { class: "btn primary", type: "button" }, "▶ INVITE A CLAUDE");
    invBtn.addEventListener("click", async () => {
      invErr.textContent = "";
      try {
        const r = await api("api/manager/invite", {});
        const link = h("input", { class: "invite-link", readonly: true, value: r.link, "aria-label": "Invite link", onclick: (e) => e.target.select() });
        const copy = h("button", { class: "btn", type: "button", text: "COPY", onclick: async (e) => {
          try { await navigator.clipboard.writeText(r.link); e.target.textContent = "COPIED ✓"; } catch { link.select(); e.target.textContent = "SELECT IT"; }
        } });
        fill(invOut, h("div", { class: "dlg-actions left invite-row" }, link, copy),
          h("p", { class: "muted small", text: r.room ? "Send it to one person. It works once, for 7 days: they log in with their Claude account and their own Claude joins the farm."
            : "This farm has no room for another Claude right now, so the link will say so until one leaves." }));
        link.select();
      } catch (x) { invErr.textContent = x.message; }
    });
    const invite = h("div", { class: "mgr-sec" }, h("h3", { text: "INVITE A CLAUDE" }),
      h("p", { class: "muted small", text: "A link for one person: they log in with their Claude account and get their own Claude here, even when the farm is private or hatching is closed." }),
      h("div", { class: "dlg-actions left" }, invBtn), invOut, invErr);
    fill($("#mgr-body"), managers, invite, planner, privacy, hatching, owners, release);
  },

  // ---------------------------------------------------------------- settings
  async openSettings(id, section) {
    for (const d of $$("dialog[open]")) d.close();
    fill($("#set-body"), h("p", { class: "muted", text: "Loading…" }));
    $("#set-h").textContent = "SETTINGS";
    $("#dlg-settings").showModal();
    try {
      this.renderSettings(await api(`api/agents/${encodeURIComponent(id)}/settings`));
      if (section) { // CUSTOMIZE: straight to its look
        const sec = [...$$("#set-body .set-sec")].find(x => x.querySelector("h3")?.textContent === section);
        sec?.scrollIntoView({ block: "start" });
      }
    } catch (x) { fill($("#set-body"), h("p", { class: "form-error", text: x.message })); }
  },
  /** A Claude's own page for its person (or the manager): its name and look, who may start work on it, its tools,
   * phone pushes, the planner, and RELEASE. */
  renderSettings(s) {
    const id = s.id, a = App.state?.agents.find(x => x.id === id);
    $("#set-h").textContent = `SETTINGS · ${String(s.name || id).toUpperCase()}`;
    const sk = skinOf(s.hat || a?.hat || "straw", s.colors, s.accessory, colorFor(id));
    const D = { skin: { hat: sk.hat, colors: { hat: sk.hatC, band: sk.band, body: sk.body }, accessory: sk.acc },
      approve: !!s.approve_missions, allTools: !(s.tools?.deny || []).length, deny: new Set(s.tools?.deny || []) };
    const name = h("input", { name: "name", maxlength: 24, required: true, value: s.name || id, autocomplete: "off" });
    const topic = h("input", { name: "notify_topic", maxlength: 200, value: s.notify_topic || "", placeholder: "e.g. clodfarm-" + id + "-" + (hashStr(id + Date.now()) % 9000 + 1000), autocomplete: "off", spellcheck: "false" });
    const hostOk = h("input", { type: "checkbox", checked: !!s.planner_host_ok });
    const err = h("p", { class: "form-error", role: "alert" }), save = h("button", { class: "btn primary", type: "submit" }, "SAVE");
    const sec = (title, ...kids) => h("section", { class: "set-sec" }, h("h3", { text: title }), ...kids); // spread: RULES is a list of rows
    const form = h("form", { class: "settings" },
      sec("NAME", h("label", { class: "sr-only", for: "set-name" }, "NAME"), Object.assign(name, { id: "set-name" })),
      sec("LOOK", this.skinPicker(D.skin, (v) => { D.skin = v; })),
      sec("RULES", this.rulesFields(D, s.groups)),
      sec("PHONE NOTIFICATIONS",
        h("label", {}, "NTFY TOPIC ", h("span", { class: "muted", text: "(optional)" }), topic),
        h("p", { class: "muted small", text: "Install the ntfy app (iPhone or Android), tap + and subscribe to this topic: missions that need your OK ping your phone. Anyone who knows the topic sees the pings, so make it hard to guess." })),
      sec("THE PLANNER", this.switchRow(hostOk, "LET THE PLANNER RUN ON MY CLAUDE", "Its planning cycles then use your Claude's account (a few minutes every cycle).")),
      h("div", { class: "save-bar" }, err, save));
    form.addEventListener("submit", async (e) => {
      e.preventDefault(); err.textContent = ""; save.disabled = true;
      try {
        const r = await api(`api/agents/${encodeURIComponent(id)}/settings`, { name: name.value.trim(), skin: D.skin, approve_missions: D.approve,
          tools: D.allTools ? "all" : { deny: [...D.deny] }, notify_topic: topic.value.trim(), planner_host_ok: hostOk.checked });
        save.textContent = "SAVED ✓"; setTimeout(() => { save.textContent = "SAVE"; save.disabled = false; }, 1200);
        if (r && r.name) $("#set-h").textContent = `SETTINGS · ${String(r.name).toUpperCase()}`;
        this.say(`${name.value.trim().toUpperCase()}'s settings are saved.`); this.refresh();
      } catch (x) { err.textContent = x.message; save.disabled = false; }
    });
    const parts = [form];
    if (!s.primary && !a?.remote) {
      const rel = h("button", { class: "btn danger", type: "button" }, "RELEASE");
      const relErr = h("p", { class: "form-error", role: "alert" });
      rel.addEventListener("click", async () => {
        if (rel.dataset.sure !== "1") { rel.dataset.sure = "1"; rel.textContent = a?.bot ? "SURE? FORGETS ITS KEY" : "SURE? LOGS IT OUT FOR GOOD"; return; }
        rel.disabled = true; rel.textContent = "RELEASING…";
        try { await api(`api/agents/${encodeURIComponent(id)}/remove`, {}); $("#dlg-settings").close(); this.say(`${String(s.name || id).toUpperCase()} left the farm. Bye bye!`); this.loadMe(); this.refresh(); }
        catch (x) { relErr.textContent = x.message; rel.disabled = false; rel.textContent = "RELEASE"; }
      });
      parts.push(h("div", { class: "mgr-sec danger-zone" }, h("h3", { text: "RELEASE" }),
        h("p", { class: "muted small", text: "It leaves the farm: its login is removed and its sub-agents stop. You can hatch a new one after." }), relErr,
        h("div", { class: "dlg-actions" }, rel)));
    }
    fill($("#set-body"), parts);
  },

  // -------------------------------------------------------------------- help
  /** One screen that says what each button does and what the farm shows. */
  openHelp() {
    for (const d of $$("dialog[open]")) d.close();
    this.renderHelp();
    $("#dlg-help").showModal();
  },
  renderHelp() {
    const scene = (w, hgt, draw) => { const c = canvas(w, hgt), g = c.getContext("2d"); g.imageSmoothingEnabled = false; draw(g); return c; };
    const art = (img) => { const cv = h("canvas", { class: "help-art", "aria-hidden": "true" }); paintSprite(cv, img, 60, { bg: tileBg, pad: 2, bottom: true }); return cv; };
    const clay = skinOf("straw", null, "", HAT_COLORS[0]), mineSkin = App.state?.agents.find(a => a.mine) ? agentSkin(App.state.agents.find(a => a.mine)) : skinOf("beanie", null, "", HAT_COLORS[1]);
    const soil = (g, x, y, w, hgt) => { g.fillStyle = G.soilo; g.fillRect(x - 1, y - 1, w + 2, hgt + 2); g.fillStyle = G.soil; g.fillRect(x, y, w, hgt); g.fillStyle = G.soil2; for (let yy = y + 3; yy < y + hgt; yy += 5) g.fillRect(x + 1, yy, w - 2, 1); };
    const things = [
      [skinFrameHD(clay, { look: 1 }), "A CLAUDE", "One Claude Code login with its own usage budget. It wanders when it's free. Tap one to see what it's doing."],
      [scene(32, 46, g => { g.drawImage(ARROW_MINE, 11, 0); g.drawImage(skinFrameHD(mineSkin, {}), 0, 10); }), "YOURS", "The Claude you're signed in to wears the gold arrow. Tap your card, top left, to open it."],
      [scene(40, 26, g => { g.drawImage(miniHD("#3b7dd8", { arms: 1 }), 0, 8); g.drawImage(LAPTOP_HD[0], 20, 12); }), "A SUB-AGENT", "A job a Claude handed off. These minis work round their Claude's plot, in the colour of the account paying for them."],
      [scene(46, 30, g => { soil(g, 1, 14, 44, 14); g.drawImage(CROPS.sprout[1], 3, 10); g.drawImage(CROPS.grow[1], 18, 10); g.drawImage(CROPS.ripe[1], 33, 10); }), "A PLOT", "A busy Claude gets one. Crops grow while the work runs, bloom into Claude's spark when it's done, and wilt if it failed."],
      [EGG_HD, "AN EGG", "A Claude waiting for its person to log it in. It hatches once they do."],
      [SCARECROW_HD[0], "THE SCARECROW", "The planner. Awake, it hands out missions towards the farm's goal. Tap it to see its plan."],
      [scene(38, 36, g => { g.drawImage(skinFrameHD(clay, { sleep: true }), 0, 0); g.drawImage(ZED, 28, 6); g.drawImage(ZED, 32, 0); }), "NAPPING", "Resting in the yard by the barn until its usage window resets. The others carry on."],
      [scene(34, 40, g => { g.drawImage(skinFrameHD(clay, { blink: true }), 0, 4); g.drawImage(FLAG_HD[0], 20, 0); }), "A RED FLAG", "Missions waiting for its person's OK. The red TO APPROVE button opens them."],
    ].map(([img, name, text]) => h("li", { class: "help-thing" }, art(img), h("span", {}, h("b", { text: name }), h("span", { text }))));
    things.push(h("li", { class: "help-thing" }, h("span", { class: "help-tok", "aria-hidden": "true" }, h("span", { class: "fire", text: "🔥" }), h("b", { text: "123" })),
      h("span", {}, h("b", { text: "THE COUNTER" }), h("span", { text: "Every token the whole farm has used, all Claudes together, ticking live. Tap it for the breakdown." }))));
    const tools = $$("#dock .tool").filter(b => !b.hidden && !b.closest("[hidden]") && getComputedStyle(b).display !== "none" && b.id !== "help-tool");
    const buttons = tools.map(b => {
      const img = b.querySelector("img"), label = b.querySelector(".lbl")?.textContent.replace(/\s+/g, " ").trim();
      return h("li", { class: "help-btn" }, h("span", { class: "help-ico" + (b.id === "hatch-tool" ? " hatch" : "") }, img ? h("img", { src: img.src, alt: "" }) : null),
        h("span", {}, h("b", { text: label }), h("span", { text: b.dataset.help || b.dataset.desc || b.dataset.tip })), h("kbd", { text: b.dataset.key }));
    });
    buttons.push(h("li", { class: "help-btn" }, h("span", { class: "help-ico" }, h("img", { src: icon("key"), alt: "" })),
      h("span", {}, h("b", { text: "TOP RIGHT" }), h("span", { text: "Sign in and the menu, zoom in and out, and see the whole farm." })), h("kbd", { text: "M" })));
    fill($("#help-body"),
      h("p", { class: "help-lede", text: "A farm of Claude Code agents. Each critter is a Claude working (or napping) here; watch the field to see the work grow." }),
      h("div", { class: "help-cols" },
        h("section", { class: "help-buttons" }, h("h3", { text: "THE BUTTONS" }), h("ul", { class: "help-list" }, buttons)),
        h("section", { class: "help-farm" }, h("h3", { text: "ON THE FARM" }), h("ul", { class: "help-list" }, things))),
      h("p", { class: "muted small help-foot", text: "Drag to look round, scroll or pinch to zoom. Press ? any time to open this again." }));
  },

  // -------------------------------------------------------------------- menu
  /** Who you are here, sign in / out, and every page (on a phone some toolbar buttons live only here). */
  openMenu() {
    for (const d of $$("dialog[open]")) d.close();
    this.renderMenu();
    $("#dlg-menu").showModal();
  },
  renderMenu() {
    const R = role(), st = App.state, me = App.me || {}, mine = st?.agents.find(a => a.mine);
    const person = R.manager || !!R.owner;
    const who = R.manager ? `the farm's MANAGER (the person of ${String(mine?.name || R.owner).toUpperCase()})` : R.owner ? `the person of ${String(mine?.name || R.owner).toUpperCase()}` : me.viewer ? "a VIEWER (farm password)" : "a VISITOR: you watch";
    const err = h("p", { class: "form-error", role: "alert" });
    const out = async (path, msg) => {
      err.textContent = "";
      try { await api(path, {}); this.say(msg); const m = await this.loadMe(); if (!m?.can_view) return this.showTitle(); await this.refresh(); this.renderMenu(); }
      catch (x) { err.textContent = x.message; }
    };
    const tabs = [!R.owner ? "mine" : null].filter(Boolean);
    const forms = h("div", { class: "login inset" });
    const link = (text, act, key, href) => href ? h("a", { class: "menu-item", href }, text, h("kbd", { text: key }))
      : h("button", { class: "menu-item", type: "button", "data-act": act, onclick: () => $("#dlg-menu").close() }, text, h("kbd", { text: key }));
    fill($("#menu-body"),
      h("p", {}, "You're ", h("b", { text: who }), "."),
      h("div", { class: "dlg-actions left" },
        R.owner && mine ? h("button", { class: "btn", type: "button", onclick: () => { $("#dlg-menu").close(); this.focusMine(); } }, "★ SHOW MY CLAUDE") : null,
        me.viewer ? h("button", { class: "btn", type: "button", onclick: () => out("api/logout", "Logged out.") }, "LOG OUT") : null,
        R.owner ? h("button", { class: "btn danger", type: "button", onclick: () => out("api/owner/forget", "This device forgot your Claude. Sign in again with a code from it.") }, "FORGET MY CLAUDE ON THIS DEVICE") : null),
      err,
      tabs.length ? [h("h3", { text: "SIGN IN" }), forms] : null,
      h("h3", { text: "GO TO" }),
      h("nav", { class: "menu-list" },
        link("WHAT'S WHAT (HELP)", "help", "?"),
        link("EVERY CLAUDE", "roster", "R"),
        (st?.me?.pending || st?.me?.pending_all) ? link("MISSIONS TO APPROVE", "approvals", "A") : null,
        st?.planner ? link("THE PLANNER", "planner", "P") : null,
        person ? link("TALK TO YOUR CLAUDE", "talk", "T") : null,
        link("TASKS AND SCHEDULES", null, "J", "tasks"),
        person ? link("DASHBOARDS", null, "D", "dashboards") : null,
        R.owner ? link("YOUR CLAUDE'S BROWSER", null, "B", "browser") : null,
        person ? link("CONNECTORS: SLACK, STRIPE, GOOGLE ADS", "connectors", "S") : null,
        R.manager ? link("RUN THE FARM", "manager", "G") : null,
        link("SEE THE WHOLE FARM", "fit", "0")),
      h("p", { class: "muted small", text: "Drag to look round the farm, scroll or pinch to zoom." }));
    if (tabs.length) this.accountForms(forms, { tabs, onDone: async (as) => {
      await this.loadMe(); await this.refresh();
      $("#dlg-menu").close();
      this.say(as === "manager" ? "You're the manager now: the gear button runs the farm." : as === "owner" ? "You're signed in to your Claude: it has the gold arrow." : "Welcome in!");
      if (as === "owner") this.focusMine();
    } });
  },
};

// ================================================================ landing demo
/** clod.farm's landing page runs the same farm on a scripted day: sub-agents grow crops, Claudes help each other, one naps,
 * a new one arrives. No server, no data: just the renderer. */
const Demo = {
  start() {
    Scene.layout = { clearOf: ".hero" }; // the title sits in the middle
    Scene.init();
    Scene.passive = true;
    this.t0 = nowS();
    this.tick();
    setInterval(() => this.tick(), 1500);
  },
  tick() {
    const T = nowS(), el = (T - this.t0) % 60, phase = el < 15 ? 0 : el < 30 ? 1 : el < 45 ? 2 : 3;
    const sub = (id, title, owner, on, status, extra = {}) => ({ id, title, owner, on: status === "running" ? on : null, status, started: T - 600, created: T - 700, ...extra });
    const subagents = [
      sub("s1", "Add CSV export to the report page", "matan", "matan", phase < 2 ? "running" : "done"),
      sub("s2", "Refactor the importer", "matan", "matan", "waiting", { children: ["c1", "c2", "c3"] }),
      sub("c1", "Parse the header row", "matan", "gil", phase < 1 ? "running" : "done", { parent: "s2" }),
      sub("c2", "Stream rows in batches", "matan", "gil", "running", { parent: "s2" }),
      sub("c3", "Tests for quoted commas", "matan", "matan", phase < 1 ? "queued" : "running", { parent: "s2" }),
      sub("s3", "Write the API docs", "gil", "gil", "running"),
      ...(phase >= 2 ? [sub("s4", "Dark mode for the dashboard", "noa", "noa", "running")] : []),
    ].filter(t => t.status !== "done");
    const claude = (id, hat, extra = {}) => ({ id, name: id, hat, loggedIn: true, alive: true, up: true, ...extra });
    const agents = [claude("matan", "straw", { primary: true }), claude("gil", "beanie")];
    if (phase >= 2) agents.push(claude("noa", "cap"));
    if (phase === 1) agents.push(claude("dana", "bow", { resting: true }));
    if (phase === 3) agents.push({ id: "egg", name: "new", hat: "straw", loggedIn: false, alive: true });
    const recent = [{ id: "d1", title: "Set up CI", status: "done" }, { id: "d2", title: "Fix flaky login test", status: "done" }];
    reconcile({ agents, subagents, recent, paused: false });
    Scene.boardCount = 3;
  },
};

const PAGE = document.body.dataset.page; // "diagram": scripts/architecture.html only borrows the sprites
if (PAGE === "landing") Demo.start(); else if (PAGE !== "diagram") UI.boot();
